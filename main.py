# -*- coding: utf-8 -*-
"""main.py -- 《异键演奏》统一入口。

用法：
    .venv\\Scripts\\python.exe main.py
    .venv\\Scripts\\python.exe main.py --smoke     # 构造界面 1 秒后退出
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _ensure_qt_paths():
    """直接调用 runtime\\python311\\python.exe main.py 时也能找到 Qt 插件。

    启动器本来会设置这些变量；这里再兜底一次，保证“不跑 heal_venv、
    不经过 .bat、复制改名后直接调 pythonw.exe”同样能启动。
    """
    qt = ROOT / ".venv" / "Lib" / "site-packages" / "PyQt5" / "Qt5"
    plugins = qt / "plugins"
    if plugins.is_dir():
        os.environ.setdefault("QT_PLUGIN_PATH", str(plugins))
        os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", str(plugins / "platforms"))
    bin_dir = qt / "bin"
    if bin_dir.is_dir():
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
        try:
            os.add_dll_directory(str(bin_dir))
        except Exception:
            pass


_ensure_qt_paths()


def _ensure_streams():
    """pythonw 下 stdout/stderr 是 None；重定向到日志文件，避免 print 直接崩。"""
    if sys.stdout is not None and sys.stderr is not None:
        return None
    try:
        logs = ROOT / "logs"
        logs.mkdir(exist_ok=True)
        f = open(logs / "main_stdout.log", "a", encoding="utf-8", buffering=1)
    except Exception:
        return None
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f
    return f


def _install_excepthook():
    """把未捕获异常写日志 + 弹框，避免 PyQt5 直接 abort（表现为"闪退"）。"""
    def _hook(etype, value, tb):
        text = "".join(traceback.format_exception(etype, value, tb))
        try:
            logs = ROOT / "logs"
            logs.mkdir(exist_ok=True)
            with open(logs / "error.log", "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, ("程序出错，已写入 logs\\error.log：\n\n" + text[-1200:]),
                "《异键演奏》出错", 0x10)
        except Exception:
            pass
    sys.excepthook = _hook


def run() -> int:
    try:
        from input_engine import make_dpi_aware
        make_dpi_aware()
    except Exception:
        pass
    # High-DPI 抗锯齿（必须在 QApplication 创建之前设置）
    try:
        from PyQt5.QtCore import Qt, QCoreApplication
        QCoreApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
        QCoreApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    except Exception:
        pass
    import gui_main
    return gui_main.main()


if __name__ == "__main__":
    _log = _ensure_streams()
    _install_excepthook()
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    try:
        raise SystemExit(run())
    except SystemExit:
        raise
    except Exception:
        tb = traceback.format_exc()
        try:
            (ROOT / "logs").mkdir(exist_ok=True)
            (ROOT / "logs" / "main_error.log").write_text(tb, encoding="utf-8")
        except Exception:
            pass
        print(tb, file=sys.stderr)
        raise SystemExit(1)
