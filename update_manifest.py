# -*- coding: utf-8 -*-
"""update_manifest.py -- 生成/刷新 version.json（版本号、更新日志、受管文件 sha256）。

    python update_manifest.py                  # 只重算哈希
    python update_manifest.py --set 1.2.1      # 指定版本
    python update_manifest.py --bump minor     # 版本 +1
    python update_manifest.py --changelog "…"  # 覆盖日志（可多次）
    python update_manifest.py --verify         # 核对清单与本地是否一致

改完代码先跑一次，再把改动文件连同 version.json 一起推送。
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(ROOT, "version.json")

# 受管文件：这些更新时会跟着远端清单一起替换
MANAGED_PATTERNS = (
    "*.py",
    "player/*.py",
    "transcriber/*.py",
    "setup/*.ps1",
    "vendor/sheetsage2/transcribe_cli.py",
)

# 只是开发者本机的内部工具，不参与发布/更新
EXCLUDE = {
    "verify_release.py",
    "_verify_7f.py",
}


def sha256_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def collect_files(root=ROOT):
    """按受管规则收集 ``{相对路径: sha256}``（正斜杠、按路径排序）。"""
    files = {}
    for pat in MANAGED_PATTERNS:
        for path in sorted(glob.glob(os.path.join(root, pat))):
            if not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if rel in EXCLUDE or rel == "version.json":
                continue
            files[rel] = sha256_file(path)
    return dict(sorted(files.items()))


def load_current():
    try:
        with open(VERSION_FILE, "r", encoding="utf-8-sig") as f:
            man = json.load(f)
        return man if isinstance(man, dict) else {}
    except Exception:
        return {}


def _bump(version, part):
    nums = [0, 0, 0]
    for i, seg in enumerate(str(version or "0.0.0").split(".")[:3]):
        try:
            nums[i] = int(seg)
        except ValueError:
            nums[i] = 0
    idx = {"major": 0, "minor": 1, "patch": 2}.get(str(part).lower())
    if idx is None:
        raise SystemExit("--bump 只接受 major / minor / patch")
    nums[idx] += 1
    for j in range(idx + 1, 3):
        nums[j] = 0
    return "%d.%d.%d" % tuple(nums)


def build(version, changelog, root=ROOT):
    return {
        "version": str(version),
        "changelog": list(changelog or []),
        "files": collect_files(root),
    }


def verify(root=ROOT):
    """核对本地文件与清单是否完全一致；返回不一致列表。"""
    man = load_current()
    want = man.get("files") or {}
    bad = []
    for rel, digest in want.items():
        path = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.isfile(path):
            bad.append("%s（文件不存在）" % rel)
        elif sha256_file(path) != digest:
            bad.append("%s（sha256 不符）" % rel)
    return bad


def main(argv):
    args = list(argv[1:])
    man = load_current()
    version = str(man.get("version") or "1.0.0")
    old_changelog = list(man.get("changelog") or [])
    new_changelog = []
    keep_old = "--keep-changelog" in args
    do_verify = False

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--set" and i + 1 < len(args):
            version = args[i + 1]
            i += 2
        elif a == "--bump" and i + 1 < len(args):
            version = _bump(version, args[i + 1])
            i += 2
        elif a == "--changelog" and i + 1 < len(args):
            new_changelog.append(args[i + 1])
            i += 2
        elif a in ("--keep-changelog", "--verify"):
            do_verify = do_verify or a == "--verify"
            i += 1
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            print("不认识的参数：%s（用 --help 看用法）" % a)
            return 2

    changelog = (old_changelog + new_changelog) if keep_old else new_changelog
    if not changelog:
        changelog = old_changelog          # 没给新日志就沿用旧的

    if do_verify:
        bad = verify()
        if bad:
            print("× 清单与本地文件不一致（%d 处）：" % len(bad))
            for line in bad:
                print("  - %s" % line)
            return 1
        print("√ 清单与本地文件完全一致（%d 个文件，v%s）"
              % (len(man.get("files") or {}), version))
        return 0

    data = build(version, changelog)
    tmp = VERSION_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, VERSION_FILE)
    print("√ 已写入 %s" % VERSION_FILE)
    print("  版本：%s" % data["version"])
    print("  受管文件：%d 个" % len(data["files"]))
    for rel in data["files"]:
        print("    %s" % rel)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    raise SystemExit(main(sys.argv))
