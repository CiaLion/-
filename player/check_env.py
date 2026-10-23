# -*- coding: utf-8 -*-
"""check_env.py -- 跨机环境与硬件一键体检。

`run_env_diagnostics()` 返回 5 大项字典：
  system / python / gpu / models / runtime
主界面顶部「🩺 环境与显卡体检」弹窗直接显示 `format_report()`。
"""
from __future__ import annotations

import ctypes
import os
import platform
import shutil
import sys
from pathlib import Path

OK = "[ok]"
NO = "[--]"

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (str(ROOT), str(ROOT / "player"), str(ROOT / "transcriber")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

MODEL_FILES = {
    "SheetSage2": (ROOT / "vendor" / "sheetsage2" / "models" / "SheetSage2"
                   / "model.safetensors", 228_738_564),
    "MERT-v2-FullSong": (ROOT / "vendor" / "sheetsage2" / "models" / "MERT-v2-FullSong"
                         / "model.safetensors", 2_529_812_848),
}


def _admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _dpi_aware():
    try:
        awareness = ctypes.c_int()
        ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(awareness))
        return {0: "unaware", 1: "system", 2: "per-monitor"}.get(awareness.value, "?")
    except Exception:
        try:
            return "system" if ctypes.windll.user32.GetProcessDPIAware() else "unaware"
        except Exception:
            return "?"


def _import_state():
    mods = {}
    for mod in ("PyQt5", "mido", "pretty_midi", "soundcard", "numpy", "PIL"):
        try:
            __import__(mod)
            mods[mod] = True
        except Exception as exc:  # noqa: BLE001
            mods[mod] = "缺失（%s）" % type(exc).__name__
    return mods


def _qt_plugin():
    plug = Path(sys.prefix) / "Lib" / "site-packages" / "PyQt5" / "Qt5" / "plugins"
    env = os.environ.get("QT_PLUGIN_PATH") or ""
    return {"path": env or str(plug), "exists": plug.is_dir() or bool(env),
            "has_platforms": (plug / "platforms").is_dir()}


def _gpu():
    info = {"name": "", "backend": "CPU", "bf16": False, "fp16": False,
            "torch": "", "cuda_build": "",
            "hint": "", "error": ""}
    try:
        import pipeline as PL
        p = PL.probe_device(force=True)
        info.update({"name": p.get("name") or "", "torch": p.get("torch") or "",
                     "cuda_build": p.get("cuda_build") or "",
                     "error": p.get("error") or ""})
        info["bf16"] = bool(p.get("bf16"))
        info["fp16"] = bool(p.get("cuda") or p.get("xpu"))
        best = (p.get("best") or "cpu").lower()
        info["backend"] = {"xpu": "XPU (Intel Arc)", "cuda": "CUDA (NVIDIA)",
                           "cpu": "CPU"}.get(best, best.upper())
        if not info["name"]:
            try:
                import winreg
                key = winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SYSTEM\CurrentControlSet\Control\Class"
                    r"\{4d36e968-e325-11ce-bfc1-08002be10318}")
                names = []
                for i in range(32):
                    try:
                        sub = winreg.EnumKey(key, i)
                        vk = winreg.OpenKey(key, sub)
                        try:
                            name, _ = winreg.QueryValueEx(vk, "DriverDesc")
                        except OSError:
                            continue
                        if name and name not in names:
                            names.append(str(name))
                    except OSError:
                        break
                info["name"] = " / ".join(names[:3])
            except Exception:
                pass
        low = (info.get("name") or "").lower()
        if info["backend"] == "CPU" and low:
            if "nvidia" in low:
                if info.get("cuda_build"):
                    info["hint"] = ("检测到 NVIDIA 显卡，但 CUDA 驱动不可用；"
                                    "请更新 N 卡驱动后重启，再运行 setup\setup_env.ps1 -Backend cuda。")
                else:
                    info["hint"] = ("检测到 NVIDIA 显卡，但当前 torch 是 CPU 版；"
                                    "请在 setup 目录运行 setup_env.ps1 -Backend cuda 重装推理环境。")
            elif "intel" in low:
                info["hint"] = ("检测到 Intel 显卡，但 XPU 不可用；"
                                "请安装 Intel Arc 驱动，再运行 setup\setup_env.ps1 -Backend xpu。")
            elif "amd" in low or "radeon" in low:
                info["hint"] = "检测到 AMD 显卡：使用 CPU 转录（一首歌约 2~4 分钟，属正常）。"
    except Exception as exc:  # noqa: BLE001
        info["error"] = "%s: %s" % (type(exc).__name__, exc)
    return info


def _models():
    out = {}
    for name, (path, expect) in MODEL_FILES.items():
        got = path.stat().st_size if path.is_file() else 0
        out[name] = {"path": str(path), "exists": got > 0,
                     "size": got, "expect": expect,
                     "ok": abs(got - expect) < 1024 * 1024 if got else False}
    return out


def _runtime():
    info = {"game": False, "audio_device": "", "qt": _qt_plugin()}
    try:
        import game_io as GIO
        info["game"] = bool(GIO.GameIO().has_window())
    except Exception as exc:  # noqa: BLE001
        info["game_error"] = str(exc)
    try:
        import recorder as REC
        info["audio_device"] = REC.default_speaker_name() or ""
    except Exception as exc:  # noqa: BLE001
        info["audio_error"] = str(exc)
    return info


def run_env_diagnostics() -> dict:
    """返回 5 大项体检结果（不抛异常）。"""
    try:
        return {
            "system": {"os": platform.platform(), "machine": platform.machine(),
                       "admin": _admin(), "dpi": _dpi_aware(),
                       "python": sys.version.split()[0]},
            "python": {"venv": sys.prefix, "exe": sys.executable,
                       "modules": _import_state()},
            "gpu": _gpu(),
            "models": _models(),
            "runtime": _runtime(),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


def format_report(info: dict) -> str:
    if not info:
        return "体检没有返回结果。"
    if info.get("error"):
        return "体检失败：%s" % info["error"]
    s = info.get("system") or {}
    p = info.get("python") or {}
    g = info.get("gpu") or {}
    m = info.get("models") or {}
    r = info.get("runtime") or {}
    lines = []
    lines.append("【1. 系统与权限】")
    lines.append("  %s（%s），Python %s" % (s.get("os"), s.get("machine"), s.get("python")))
    lines.append("  管理员：%s ｜ DPI 感知：%s"
                 % ("有" if s.get("admin") else "没有（发送键鼠需要）", s.get("dpi")))
    lines.append("【2. 主程序环境】")
    lines.append("  venv：%s" % p.get("venv"))
    mods = p.get("modules") or {}
    lines.append("  " + " ｜ ".join("%s=%s" % (k, "ok" if v is True else v)
                                   for k, v in mods.items()))
    qt = r.get("qt") or {}
    lines.append("  QT_PLUGIN_PATH：%s（%s）"
                 % (qt.get("path"), "有效" if qt.get("exists") else "无效"))
    lines.append("【3. 显卡与 AI 推理】")
    lines.append("  显卡：%s" % (g.get("name") or "未识别"))
    lines.append("  后端：%s ｜ torch %s ｜ bf16=%s fp16=%s"
                 % (g.get("backend"), g.get("torch") or "?",
                    "支持" if g.get("bf16") else "不支持/未探测",
                    "支持" if g.get("fp16") else "不支持"))
    if g.get("error"):
        lines.append("  探测提示：%s" % g["error"])
    if g.get("hint"):
        lines.append("  ⚠ %s" % g["hint"])
    lines.append("【4. 模型权重】")
    for name, item in m.items():
        size = item.get("size") or 0
        lines.append("  %s：%s（%.2f GB / 期望 %.2f GB）%s"
                     % (name, item.get("path"), size / 1024 ** 3,
                        (item.get("expect") or 0) / 1024 ** 3,
                        "" if item.get("ok") else "  ⚠ 大小不一致"))
    lines.append("【5. 游戏与音频】")
    lines.append("  《异环》窗口：%s" % ("已打开" if r.get("game") else "未检测到"))
    lines.append("  默认播放设备：%s" % (r.get("audio_device") or "未找到（录音不可用）"))
    return "\n".join(lines)


def main():
    print("=" * 56)
    print(" 异键演奏 · 环境与显卡体检")
    print("=" * 56)
    print(format_report(run_env_diagnostics()))
    print("-" * 56)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.exit(main())
