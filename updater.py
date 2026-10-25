# -*- coding: utf-8 -*-
"""updater.py -- 联网检查更新：多镜像探测、暂存区校验、备份替换与回退。

校验不通过（sha256 不符 / 语法错）时整批放弃，原文件不动；
用户配置与 out/ 目录永不覆盖。仅用标准库。
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(ROOT, "version.json")
STAGING_DIR = os.path.join(ROOT, ".update_staging")
BACKUP_DIR = os.path.join(ROOT, "logs", "backup_prev")
BACKUP_INDEX = os.path.join(BACKUP_DIR, "backup.json")

# 多镜像：按顺序探测，单源超时自动换下一个。
# 前两个是国内加速镜像，最后一个是官方源（镜像全挂时兜底）。
SOURCE_TIMEOUT = 4.0
SOURCES = (
    "https://ghfast.top/https://raw.githubusercontent.com/CiaLion/NTE-GuitarFlow/main/",
    "https://mirror.ghproxy.com/https://raw.githubusercontent.com/CiaLion/NTE-GuitarFlow/main/",
    "https://raw.githubusercontent.com/CiaLion/NTE-GuitarFlow/main/",
)

# 用户配置白名单：更新严禁碰这些（相对项目根、统一小写、正斜杠）
PROTECTED_FILES = (
    "player/player_config.json",
    "gui_settings.json",
    "transcriber/gui_settings.json",
    "version.json",
)
PROTECTED_DIRS = (
    "out",
)

# 清理 __pycache__ 时不必进入的重目录
_WALK_SKIP = (".venv", "vendor", "runtime", "licenses", ".update_staging",
              "logs", "_backup")


class UpdateError(Exception):
    """检查/下载/替换过程中的可预期错误（界面直接显示 message）。"""


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return ""


def safe_rel(rel):
    """把远端给的相对路径规范化；越界路径（绝对路径 / ``..``）一律拒绝。"""
    rel = str(rel or "").replace("\\", "/").strip()
    while rel.startswith("./"):
        rel = rel[2:]
    if not rel or rel.startswith("/") or ":" in rel:
        return None
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def is_protected(rel):
    """这个文件是不是「用户配置 / 输出目录」，更新时要跳过。"""
    low = str(rel or "").replace("\\", "/").lower()
    if low in PROTECTED_FILES:
        return True
    for d in PROTECTED_DIRS:
        if low == d or low.startswith(d + "/"):
            return True
    return False


def local_path(rel):
    return os.path.join(ROOT, rel.replace("/", os.sep))


def current_version():
    man = load_local_manifest()
    return str(man.get("version") or "")


def load_local_manifest():
    """读本地 version.json；文件缺失/损坏都当成空清单，绝不抛异常。"""
    try:
        with open(VERSION_FILE, "r", encoding="utf-8-sig") as f:
            man = json.load(f)
        return man if isinstance(man, dict) else {}
    except Exception:
        return {}


def _http_get(url, timeout=SOURCE_TIMEOUT):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "NTE-GuitarFlow-Updater/1.2",
            "Cache-Control": "no-cache",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _norm_files(man):
    """把清单里的 files 规范化成 {相对路径: sha256}，顺手丢掉非法路径。"""
    out = {}
    for rel, digest in (man.get("files") or {}).items():
        nrel = safe_rel(rel)
        if not nrel:
            continue
        out[nrel] = str(digest or "").strip().lower()
    return out


def fetch_remote_manifest(sources=None, timeout=SOURCE_TIMEOUT, log=None):
    """按顺序探测镜像，返回 ``(manifest, base_url, errors)``。

    全部失败时抛 ``UpdateError``，message 里带每个镜像的失败原因，方便排查。
    """
    errors = []
    for base in (sources or SOURCES):
        if not base.endswith("/"):
            base += "/"
        try:
            raw = _http_get(base + "version.json", timeout)
            man = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(man, dict) or not isinstance(man.get("files"), dict):
                raise ValueError("version.json 不是预期格式（缺 files）")
            if log:
                log("已连接镜像：%s" % base)
            return man, base, errors
        except Exception as exc:  # noqa: BLE001
            errors.append("%s → %s" % (base, exc))
            if log:
                log("镜像不可用：%s（%s）" % (base, exc))
    if errors and all("404" in e for e in errors):
        raise UpdateError(
            "远端仓库里还没有 version.json —— 可能还没把新版本推上 GitHub。\n"
            "（三个镜像都返回 404）\n" + "\n".join(errors))
    raise UpdateError("所有更新源都没能取到清单：\n" + "\n".join(errors))


def check_for_update(sources=None, timeout=SOURCE_TIMEOUT, log=None):
    """检查有没有更新。

    返回 dict：
      status        "latest" / "update"
      local_version / remote_version / changelog
      changed       需要更新的文件（相对路径列表）
      skipped       因属于用户配置/输出目录而跳过的文件
      base          实际连上的镜像根地址
      manifest      远端清单原文（下载阶段要用）
    """
    local = load_local_manifest()
    remote, base, mirror_errors = fetch_remote_manifest(sources, timeout, log)
    remote_files = _norm_files(remote)

    changed, same, skipped = [], 0, []
    for rel, want in remote_files.items():
        if is_protected(rel):
            skipped.append(rel)
            continue
        cur = sha256_file(local_path(rel))
        if cur and cur == want:
            same += 1
        else:
            changed.append(rel)

    return {
        "status": "update" if changed else "latest",
        "local_version": str(local.get("version") or ""),
        "remote_version": str(remote.get("version") or ""),
        "changelog": list(remote.get("changelog") or []),
        "changed": sorted(changed),
        "same_count": same,
        "skipped": sorted(skipped),
        "base": base,
        "manifest": remote,
        "mirror_errors": mirror_errors,
    }


def _check_py_syntax(rel, data):
    """``.py`` 文件必须能被 ast.parse 解析，否则视为镜像返回了错误页。"""
    if not rel.lower().endswith(".py"):
        return
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UpdateError("%s 不是 UTF-8 文本（可能下载到了错误页）：%s" % (rel, exc))
    try:
        ast.parse(text, filename=rel)
    except SyntaxError as exc:
        raise UpdateError("%s 语法校验失败（第 %s 行）：%s"
                          % (rel, exc.lineno, exc.msg))


def prepare_staging():
    """清空并重建 ``.update_staging/``。"""
    try:
        if os.path.isdir(STAGING_DIR):
            shutil.rmtree(STAGING_DIR, ignore_errors=True)
        os.makedirs(STAGING_DIR, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        raise UpdateError("无法创建暂存目录 .update_staging：%s" % exc)


def download_update(info, timeout=None, log=None, progress=None):
    """把 ``info["changed"]`` 里的文件下载到暂存区并逐个校验。

    返回 ``{相对路径: sha256}``；任何一项不通过都会清空暂存区并抛
    ``UpdateError``，调用方据此「什么都不做」即可。
    """
    changed = list(info.get("changed") or [])
    if not changed:
        return {}
    remote_files = _norm_files(info.get("manifest") or {})
    base = info.get("base") or ""
    if not base.endswith("/"):
        base += "/"
    timeout = timeout or max(SOURCE_TIMEOUT * 5, 20.0)

    prepare_staging()
    staged = {}
    try:
        for i, rel in enumerate(changed, 1):
            want = remote_files.get(rel)
            if not want:
                raise UpdateError("清单里找不到 %s 的 sha256" % rel)
            if progress:
                progress("正在下载 %d/%d：%s" % (i, len(changed), rel))
            if log:
                log("下载 %s" % rel)
            try:
                data = _http_get(base + rel.replace(" ", "%20"), timeout)
            except Exception as exc:  # noqa: BLE001
                raise UpdateError("下载 %s 失败：%s" % (rel, exc))

            got = sha256_bytes(data)
            if got != want:
                raise UpdateError(
                    "%s 的 sha256 与清单不符（镜像可能返回了错误页）\n"
                    "期望 %s\n实际 %s" % (rel, want, got))
            _check_py_syntax(rel, data)

            dst = os.path.join(STAGING_DIR, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(dst) or STAGING_DIR, exist_ok=True)
            with open(dst, "wb") as f:
                f.write(data)
            staged[rel] = want
        if progress:
            progress("校验通过：%d 个文件已就绪" % len(staged))
        return staged
    except Exception:
        # 任何一步失败都不留半成品，避免下次误用
        shutil.rmtree(STAGING_DIR, ignore_errors=True)
        raise


def _write_manifest(man):
    """把远端清单写成新的本地 version.json（原子替换）。"""
    tmp = VERSION_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, VERSION_FILE)


def apply_update(info, staged, log=None, progress=None):
    """备份旧文件 → 原子替换 → 写新 version.json → 清 __pycache__。

    返回摘要 dict：``{"files": n, "backup_dir": ..., "version": ...}``。
    """
    staged = dict(staged or {})
    if not staged:
        raise UpdateError("没有已校验的待更新文件")

    os.makedirs(BACKUP_DIR, exist_ok=True)
    index = {
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "from_version": str(info.get("local_version") or ""),
        "to_version": str(info.get("remote_version") or ""),
        "files": {},
    }

    # ① 先把旧文件整体备份（含 SHA，回退时能核对）
    for rel in staged:
        dst = local_path(rel)
        if os.path.isfile(dst):
            bak = os.path.join(BACKUP_DIR, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(bak) or BACKUP_DIR, exist_ok=True)
            shutil.copy2(dst, bak)
            index["files"][rel] = sha256_file(dst)
        else:
            index["files"][rel] = None       # 更新前没有这个文件 → 回退时删掉
    with open(BACKUP_INDEX, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    # ② 逐个原子替换
    done = 0
    for rel in sorted(staged):
        src = os.path.join(STAGING_DIR, rel.replace("/", os.sep))
        dst = local_path(rel)
        if not os.path.isfile(src):
            raise UpdateError("暂存文件不见了：%s（请重新检查更新）" % rel)
        os.makedirs(os.path.dirname(dst) or ROOT, exist_ok=True)
        os.replace(src, dst)
        done += 1
        if progress:
            progress("已替换 %d/%d：%s" % (done, len(staged), rel))
        if log:
            log("替换 %s" % rel)

    # ③ 写新清单（版本号/日志/哈希都来自远端）
    man = info.get("manifest") or {}
    try:
        _write_manifest(man)
    except Exception as exc:  # noqa: BLE001
        raise UpdateError("代码已更新，但 version.json 写入失败：%s" % exc)

    # ④ 清字节码 + 清暂存区
    removed = clean_pycache()
    shutil.rmtree(STAGING_DIR, ignore_errors=True)
    if log:
        log("已清理 %d 个 __pycache__" % removed)

    return {
        "files": done,
        "backup_dir": BACKUP_DIR,
        "version": str(man.get("version") or ""),
        "pycache_removed": removed,
    }


def clean_pycache():
    """删掉全项目的 ``__pycache__``（跳过 venv/vendor 等重目录）。"""
    removed = 0
    for dirpath, dirnames, _filenames in os.walk(ROOT):
        low = dirpath.replace("\\", "/").lower()
        if any(("/" + s + "/") in (low + "/") for s in _WALK_SKIP):
            dirnames[:] = []
            continue
        for name in list(dirnames):
            if name == "__pycache__":
                try:
                    shutil.rmtree(os.path.join(dirpath, name))
                    removed += 1
                except Exception:
                    pass
                dirnames.remove(name)
    return removed


def has_backup():
    return os.path.isfile(BACKUP_INDEX)


def rollback_update(log=None):
    """用 ``logs/backup_prev/`` 把上一次更新覆盖掉的文件恢复回去。"""
    if not has_backup():
        raise UpdateError("没有找到可回退的备份（logs\\backup_prev\\backup.json）")
    try:
        with open(BACKUP_INDEX, "r", encoding="utf-8") as f:
            index = json.load(f)
    except Exception as exc:  # noqa: BLE001
        raise UpdateError("备份清单读不出来：%s" % exc)

    restored, deleted, missing = 0, 0, []
    for rel, old_sha in (index.get("files") or {}).items():
        src = os.path.join(BACKUP_DIR, rel.replace("/", os.sep))
        dst = local_path(rel)
        if old_sha is None:          # 更新前本来没有这个文件 → 回退时删掉
            try:
                if os.path.isfile(dst):
                    os.remove(dst)
                    deleted += 1
            except Exception:
                pass
            continue
        if not os.path.isfile(src):
            missing.append(rel)
            continue
        try:
            os.makedirs(os.path.dirname(dst) or ROOT, exist_ok=True)
            shutil.copy2(src, dst)
            restored += 1
            if log:
                log("已回退 %s" % rel)
        except Exception as exc:  # noqa: BLE001
            missing.append("%s（%s）" % (rel, exc))

    clean_pycache()
    return {
        "restored": restored,
        "deleted": deleted,
        "missing": missing,
        "from_version": index.get("from_version"),
        "to_version": index.get("to_version"),
    }


def restart_argv():
    """返回 ``(python 可执行文件, 参数列表)``，供界面用 QProcess 重启。"""
    script = ""
    try:
        script = os.path.abspath(sys.argv[0])
    except Exception:
        script = ""
    if not script.lower().endswith(".py"):
        script = os.path.join(ROOT, "main.py")
    return sys.executable, [script] + list(sys.argv[1:])


# ══════════════════════════════════════════════════════════════════════════
# 命令行：python updater.py check | apply | rollback
# ══════════════════════════════════════════════════════════════════════════
def _main(argv):
    cmd = (argv[1] if len(argv) > 1 else "check").lower()
    say = lambda m: print(m, flush=True)          # noqa: E731
    try:
        if cmd == "check":
            info = check_for_update(log=say)
            print("本地版本：%s" % (info["local_version"] or "未知"))
            print("远端版本：%s" % (info["remote_version"] or "未知"))
            if info["status"] == "latest":
                print("当前已是最新版本 (v%s)" % (info["remote_version"] or "?"))
            else:
                print("有 %d 个文件需要更新：" % len(info["changed"]))
                for rel in info["changed"]:
                    print("  - %s" % rel)
            return 0
        if cmd == "apply":
            info = check_for_update(log=say)
            if info["status"] == "latest":
                print("当前已是最新版本 (v%s)" % (info["remote_version"] or "?"))
                return 0
            staged = download_update(info, log=say, progress=say)
            summary = apply_update(info, staged, log=say, progress=say)
            print("更新完成：%d 个文件 → v%s（重启软件后生效）"
                  % (summary["files"], summary["version"]))
            return 0
        if cmd == "rollback":
            summary = rollback_update(log=say)
            print("已回退 %d 个文件（删除 %d 个）→ v%s"
                  % (summary["restored"], summary["deleted"],
                     summary["from_version"] or "?"))
            return 0
        print("用法：python updater.py [check|apply|rollback]")
        return 2
    except UpdateError as exc:
        print("× %s" % exc)
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    raise SystemExit(_main(sys.argv))
