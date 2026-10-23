# -*- coding: utf-8 -*-
"""gui_main.py -- 《异键演奏》一体化主界面。

顶部全局状态栏 + 两个大标签页：
  【AI 智能扒谱】复用 transcriber/gui_a_legacy.py 的完整三步流水线；
  【吉他自动演奏】复用 player/gui_b_legacy.py 的选曲/角色/播放/校准/自检。

另含 WheelOverlayDialog：半透明置顶指示圈，鼠标拖圆心、滚轮调半径，
确认后把屏幕坐标换算成客户区比例写入 player_config.json。
"""
from __future__ import annotations

import ctypes
import os
import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for _p in (ROOT, ROOT / "player", ROOT / "transcriber"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

_QT_PLUGINS = Path(sys.prefix) / "Lib" / "site-packages" / "PyQt5" / "Qt5" / "plugins"
if _QT_PLUGINS.is_dir() and not os.environ.get("QT_PLUGIN_PATH"):
    os.environ["QT_PLUGIN_PATH"] = str(_QT_PLUGINS)

from PyQt5.QtCore import (QPoint, QProcess, QRect, Qt, QThread, QTimer,
                          pyqtSignal)  # noqa: E402
from PyQt5.QtGui import QColor, QFont, QPainter, QPen, QTextCursor  # noqa: E402
from PyQt5.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel,  # noqa: E402
                             QMainWindow, QMessageBox, QPlainTextEdit,
                             QProgressBar, QPushButton, QTabWidget,
                             QVBoxLayout, QWidget)

import gui_a_legacy as AGui  # noqa: E402
import gui_b_legacy as BGui  # noqa: E402
import game_io as GIO  # noqa: E402
import player_config as PC  # noqa: E402
import midi_compiler as MC  # noqa: E402
import gui_theme as GT  # noqa: E402

SECTOR_ANGLES = GIO.SECTOR_ANGLES

def _is_dark_mode():
    env = (os.environ.get("DSH_GUI_THEME") or "").strip().lower()
    if env in ("dark", "light"):
        return env == "dark"
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return int(val) == 0
    except Exception:
        return False


def apply_theme(app, mode=None):
    """Studio Dark / Modern Light 双主题（gui_theme 统一 QSS）。"""
    return GT.apply_theme(app, mode=mode)


class WheelOverlayDialog(QDialog):
    """半透明置顶指示圈：拖圆心、滚轮调半径，确认后写比例。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        io = GIO.GameIO()
        m = io.metrics()
        if m:
            self._client = tuple(m)
        else:
            self._client = (0, 0, 1920, 1080)
        # 覆盖层铺满整个虚拟桌面：指示圈/控制条拖到游戏窗口外面也仍然看得见
        geo = QApplication.desktop().geometry()
        self.setGeometry(geo)
        left, top, cw, ch = self._client
        cfg = PC.load()
        self.cx = left + cw * float(cfg.get("wheel_x_ratio", 0.6937))
        self.cy = top + ch * float(cfg.get("wheel_y_ratio", 0.4378))
        self.radius = ch * float(cfg.get("wheel_radius_ratio", 0.2957))
        self._drag = None
        self._bar = QWidget(self)
        self._bar.setStyleSheet(
            "background: rgba(20,24,32,235); border-radius: 8px;")
        lay = QHBoxLayout(self._bar)
        lay.setContentsMargins(8, 4, 8, 4)
        tip = QLabel("左键拖动圆心对准游戏轮盘 | 滚轮或 [-][+] 调整大小")
        tip.setStyleSheet("color:#eaf2ff; padding:4px; font-size:13px;"
                          "background: transparent;")
        lay.addWidget(tip, 1)
        btn_css = ("QPushButton{background:#2f81f7;color:#ffffff;border:none;"
                   "border-radius:6px;padding:6px 12px;font-size:13px;"
                   "font-weight:bold;} QPushButton:hover{background:#4b96ff;}")
        self.btn_minus = QPushButton("[-]")
        self.btn_plus = QPushButton("[+]")
        self.btn_ok = QPushButton("确认锁定")
        self.btn_cancel = QPushButton("取消")
        for b in (self.btn_minus, self.btn_plus, self.btn_ok, self.btn_cancel):
            b.setStyleSheet(btn_css)
            b.setMinimumHeight(30)
            lay.addWidget(b)
        self.btn_minus.clicked.connect(lambda: self._scale(0.95))
        self.btn_plus.clicked.connect(lambda: self._scale(1.05))
        self.btn_ok.clicked.connect(self.confirm)
        self.btn_cancel.clicked.connect(self.reject)
        self._bar.setGeometry(14, 14, min(self.width() - 28, 860), 48)
        self.raise_()

    def _scale(self, k):
        self.radius = max(40.0, min(self.height() * 0.5, self.radius * k))
        self.update()

    def set_circle(self, cx, cy, radius):
        self.cx, self.cy, self.radius = float(cx), float(cy), float(radius)
        self.update()

    def compute_ratios(self, client=None):
        left, top, cw, ch = tuple(client or self._client)
        return {"wheel_x_ratio": (self.cx - left) / cw,
                "wheel_y_ratio": (self.cy - top) / ch,
                "wheel_radius_ratio": self.radius / ch}

    def save_ratios(self, client=None):
        ratios = self.compute_ratios(client)
        PC.save(ratios)
        return ratios

    def confirm(self):
        m = GIO.GameIO().metrics()
        ratios = self.save_ratios(m if m else None)
        self.accept()
        return ratios

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.confirm()
        elif ev.key() == Qt.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(ev)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            lcx = self.cx - self.x()
            lcy = self.cy - self.y()
            dx = ev.pos().x() - lcx
            dy = ev.pos().y() - lcy
            if dx * dx + dy * dy <= self.radius * self.radius:
                self._drag = (dx, dy)          # 记录“按下点相对圆心的偏移”（窗口内坐标）
                ev.accept()
                return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._drag is not None:
            self.cx = self.x() + ev.pos().x() - self._drag[0]
            self.cy = self.y() + ev.pos().y() - self._drag[1]
            self.update()
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._drag = None

    def wheelEvent(self, ev):
        self._scale(1.04 if ev.angleDelta().y() > 0 else 1 / 1.04)

    def paintEvent(self, ev):
        import math
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.translate(-self.x(), -self.y())
        c = QColor(0, 230, 255, 55)
        p.setBrush(c)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPoint(int(self.cx), int(self.cy)), int(self.radius), int(self.radius))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(0, 230, 255), 3))
        p.drawEllipse(QPoint(int(self.cx), int(self.cy)), int(self.radius), int(self.radius))
        p.drawLine(int(self.cx - 15), int(self.cy), int(self.cx + 15), int(self.cy))
        p.drawLine(int(self.cx), int(self.cy - 15), int(self.cx), int(self.cy + 15))
        pen = QPen(QColor(255, 210, 80), 2, Qt.DashLine)
        p.setPen(pen)
        for ang in range(0, 360, 60):
            t = math.radians(ang)
            p.drawLine(QPoint(int(self.cx), int(self.cy)),
                       QPoint(int(self.cx + self.radius * math.cos(t)),
                              int(self.cy + self.radius * math.sin(t))))
        p.setPen(QPen(QColor(255, 90, 140), 2))
        for i, ang in enumerate(SECTOR_ANGLES):
            t = math.radians(ang)
            tx = self.cx + self.radius * GIO.SECTOR_CLICK_FRACTION * math.cos(t)
            ty = self.cy + self.radius * GIO.SECTOR_CLICK_FRACTION * math.sin(t)
            p.drawEllipse(QPoint(int(tx), int(ty)), 10, 10)
            p.setFont(QFont("Microsoft YaHei UI", 10, QFont.Bold))
            p.drawText(QRect(int(tx - 12), int(ty - 30), 24, 24), Qt.AlignCenter, str(i + 1))
        # 安全停靠点
        px = self.x() + self.width() * GIO.STRUM_PARK[0]
        py = self.y() + self.height() * GIO.STRUM_PARK[1]
        p.setPen(QPen(QColor(180, 255, 120), 2))
        p.drawEllipse(QPoint(int(px), int(py)), 9, 9)
        p.end()


class EnvCheckWorker(QThread):
    """后台跑 check_env.run_env_diagnostics()，避免体检时界面卡住。"""

    done = pyqtSignal(object, str)

    def run(self):
        try:
            import check_env
            info = check_env.run_env_diagnostics()
            self.done.emit(info, check_env.format_report(info))
        except Exception as exc:  # noqa: BLE001
            self.done.emit({}, "体检失败：%s: %s" % (type(exc).__name__, exc))


class EnvCheckDialog(QDialog):
    """环境体检 + 一键安装 / 修复（GUI 内直接调用 setup/bootstrap.ps1）。

    安装走 QProcess（powershell -File setup\\bootstrap.ps1 -Force），
    实时把日志打到窗口里，并通过进度条 + 磁盘文件字节监控实时展示进度；装完自动重新体检。
    """

    TOTAL_WEIGHT_BYTES = 228738564 + 2529812848  # SheetSage2 + MERT-v2-FullSong (~2.57 GB)

    def __init__(self, parent=None):
        super(EnvCheckDialog, self).__init__(parent)
        self.setWindowTitle("环境与显卡体检 / 一键安装")
        self.resize(820, 680)
        self.setMinimumSize(720, 580)
        self._worker = None
        self._proc = None
        self._mode = "idle"  # "idle" | "check" | "install"
        self._t0 = 0.0
        self._stage_pct = 0
        self._stage_label = "准备就绪"

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        head = QLabel("环境与显卡体检 / 一键安装")
        head.setObjectName("sectionTitle")
        lay.addWidget(head)
        tip = QLabel("先看下面的体检结果；如果有模型缺失、推理 Python 不存在、"
                     "或显卡显示 CPU，点「一键安装 / 修复环境」。"
                     "安装会联网下载约 8GB，可能需要 5~15 分钟，期间不要关闭程序。")
        tip.setObjectName("sub")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        self.install_bar = QProgressBar()
        self.install_bar.setObjectName("stageBar")
        self.install_bar.setRange(0, 100)
        self.install_bar.setValue(0)
        self.install_bar.setTextVisible(True)
        self.install_bar.setFormat("准备体检…")
        lay.addWidget(self.install_bar)

        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(360)
        lay.addWidget(self.log, 1)

        self.status = QLabel("准备体检…")
        self.status.setObjectName("sub")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        row = QHBoxLayout()
        self.btn_check = QPushButton("重新体检")
        self.btn_check.setObjectName("ghost")
        self.btn_check.clicked.connect(self.start_check)
        row.addWidget(self.btn_check)
        self.btn_install = QPushButton("一键安装 / 修复环境")
        self.btn_install.setObjectName("primary")
        self.btn_install.clicked.connect(self.start_install)
        row.addWidget(self.btn_install)
        row.addStretch(1)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.close)
        row.addWidget(self.btn_close)
        lay.addLayout(row)

        self._prog_timer = QTimer(self)
        self._prog_timer.setInterval(400)
        self._prog_timer.timeout.connect(self._tick_progress)

    # ── 进度监控辅助 ──────────────────────────────────────────────────
    def _current_weight_bytes(self) -> int:
        models_dir = ROOT / "vendor" / "sheetsage2" / "models"
        total = 0
        for sub, expected in (("SheetSage2", 228738564), ("MERT-v2-FullSong", 2529812848)):
            d = models_dir / sub
            w = d / "model.safetensors"
            part = d / "model.safetensors.part"
            b = 0
            try:
                if w.is_file() and w.stat().st_size > 0:
                    b = w.stat().st_size
                elif part.is_file() and part.stat().st_size > 0:
                    b = part.stat().st_size
            except Exception:
                b = 0
            total += min(b, expected)
        return total

    def _update_bar(self, pct: int, text: str):
        pct = max(0, min(100, int(pct)))
        self.install_bar.setValue(pct)
        self.install_bar.setFormat(text)

    def _parse_install_log_stage(self, chunk: str):
        for line in (chunk or "").splitlines():
            s = line.strip()
            if not s:
                continue
            if "[heal]" in s or "Locating base Python" in s:
                self._stage_pct = max(self._stage_pct, 5)
                self._stage_label = "正在检查并修复 Python 运行时路径"
            elif "Main environment (.venv)" in s:
                self._stage_pct = max(self._stage_pct, 12)
                self._stage_label = "正在检查/配置主程序环境 (.venv)"
            elif "Main environment already verified" in s or "main program dependencies verified" in s:
                self._stage_pct = max(self._stage_pct, 20)
                self._stage_label = "主程序环境已就绪，准备检查 AI 推理环境"
            elif "SheetSage2 environment" in s or "installing torch" in s:
                self._stage_pct = max(self._stage_pct, 24)
                self._stage_label = "正在配置显卡 AI 推理核心 (PyTorch)"
            elif "downloading from " in s:
                self._stage_pct = max(self._stage_pct, 28)
                self._stage_label = "正在下载显卡 PyTorch 加速组件"
            elif "installing SheetSage2 from" in s:
                self._stage_pct = max(self._stage_pct, 38)
                self._stage_label = "正在验证/安装 SheetSage2 依赖库"
            elif "SheetSage2 environment already verified" in s or "SheetSage2 dependencies verified" in s:
                self._stage_pct = max(self._stage_pct, 45)
                self._stage_label = "Python 环境已就绪，准备核验 AI 模型权重"
            elif "Downloading model weights" in s or "Fetching m-a-p/SheetSage2" in s:
                self._stage_pct = max(self._stage_pct, 48)
                self._stage_label = "正在下载 SheetSage2 模型权重 (0.21 GB)"
            elif "Fetching m-a-p/MERT-v2-FullSong" in s:
                self._stage_pct = max(self._stage_pct, 56)
                self._stage_label = "正在下载 MERT-v2 音乐底座权重 (2.36 GB)"
            elif "restored " in s and "patched model code" in s:
                self._stage_pct = max(self._stage_pct, 98)
                self._stage_label = "正在同步显卡加速补丁与校验哈希"

    def _tick_progress(self):
        elapsed = max(0.0, time.time() - self._t0)
        if self._mode == "check":
            # 体检通常耗时 2~6 秒，平滑推进至 90%
            pct = min(90, int(15 + 75 * (1.0 - (2.71828 ** (-elapsed / 2.5)))))
            self._update_bar(pct, "正在探测系统、显卡与模型状态… (%d%%) · 已用时 %.1fs" % (pct, elapsed))
            return
        if self._mode == "install":
            if self._stage_pct >= 45:
                cur_b = self._current_weight_bytes()
                ratio = min(1.0, float(cur_b) / float(self.TOTAL_WEIGHT_BYTES))
                pct = max(self._stage_pct, 45 + int(52 * ratio))
                gb_cur = cur_b / (1024.0 ** 3)
                gb_tot = self.TOTAL_WEIGHT_BYTES / (1024.0 ** 3)
                self._update_bar(
                    pct,
                    "%s：%.2f / %.2f GB (%d%%) · 已用时 %ds"
                    % (self._stage_label, gb_cur, gb_tot, pct, int(elapsed))
                )
            else:
                # 环境与 PyTorch 阶段（0~44%），随耗时微幅渐进防假死感
                creep = min(44, self._stage_pct + int(elapsed / 8.0))
                self._update_bar(
                    creep,
                    "%s (%d%%) · 已用时 %ds" % (self._stage_label, creep, int(elapsed))
                )

    # ── 体检 ──────────────────────────────────────────────────────────
    def _append(self, text):
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        if not text:
            return
        self.log.moveCursor(QTextCursor.End)
        self.log.insertPlainText(text)
        self.log.moveCursor(QTextCursor.End)
        self.log.verticalScrollBar().setValue(
            self.log.verticalScrollBar().maximum())

    def _set_status(self, text):
        self.status.setText(text)

    def start_check(self):
        if self._worker is not None and self._worker.isRunning():
            return
        if self._proc is not None and self._proc.state() != QProcess.NotRunning:
            return
        self.btn_install.setEnabled(False)
        self._mode = "check"
        self._t0 = time.time()
        self._update_bar(10, "正在探测系统、显卡与模型状态… (10%)")
        self._prog_timer.start()
        self._set_status("正在体检…（探测显卡/模型可能要几秒）")
        self._append("\n=== 环境体检 ===\n")
        self._worker = EnvCheckWorker(self)
        self._worker.done.connect(self._on_check_done)
        self._worker.finished.connect(self._on_check_finished)
        self._worker.start()

    def _on_check_done(self, info, text):
        self._prog_timer.stop()
        self._mode = "idle"
        elapsed = max(0.1, time.time() - self._t0)
        self._update_bar(100, "✔ 环境与显卡体检完成 (100%%) · 耗时 %.1fs" % elapsed)
        self._append(text)
        self._set_status("体检完成。如果上面有缺失项，点「一键安装 / 修复环境」。")

    def _on_check_finished(self):
        self._prog_timer.stop()
        self.btn_install.setEnabled(True)

    # ── 一键安装 ──────────────────────────────────────────────────────
    def start_install(self):
        if self._proc is not None and self._proc.state() != QProcess.NotRunning:
            return
        script = ROOT / "setup" / "bootstrap.ps1"
        if not script.is_file():
            QMessageBox.warning(self, "缺少安装脚本",
                                "找不到：\n%s\n\n请用完整包，或把 setup 文件夹补齐。" % script)
            return
        if QMessageBox.question(
                self, "一键安装 / 修复环境",
                "将自动完成：\n"
                "1. 创建/修复 Python 环境\n"
                "2. 按显卡安装 XPU / CUDA / CPU 版 PyTorch\n"
                "3. 从 hf-mirror 直链下载两个模型（约 2.6GB，免登录）\n\n"
                "总共约 8GB，需要联网，可能 5~15 分钟。继续吗？") != QMessageBox.Yes:
            return
        self.btn_check.setEnabled(False)
        self.btn_close.setEnabled(False)
        self.btn_install.setEnabled(False)
        self._mode = "install"
        self._t0 = time.time()
        self._stage_pct = 2
        self._stage_label = "正在初始化安装脚本"
        self._update_bar(2, "正在启动一键安装 / 修复流程… (2%)")
        self._prog_timer.start()
        self._append("\n=== 一键安装开始（setup/bootstrap.ps1 -Force）===\n")
        self._append("提示：安装期间可以看日志，但不要关闭这个窗口。\n\n")
        self._set_status("正在安装…（上方进度条实时显示阶段与模型下载进度，请勿关闭窗口）")

        self._proc = QProcess(self)
        self._proc.setProgram("powershell.exe")
        self._proc.setArguments([
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(script), "-Force",
        ])
        self._proc.setProcessChannelMode(QProcess.MergedChannels)
        self._proc.readyReadStandardOutput.connect(self._read_proc)
        self._proc.finished.connect(self._install_finished)
        self._proc.errorOccurred.connect(self._install_error)
        self._proc.start()

    def _decode(self, data):
        raw = bytes(data)
        for enc in ("utf-8", "gbk"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    def _read_proc(self):
        if self._proc is None:
            return
        try:
            chunk = self._decode(self._proc.readAllStandardOutput())
            self._parse_install_log_stage(chunk)
            self._append(chunk)
            self._tick_progress()
        except Exception:
            pass

    def _install_finished(self, code, _status):
        self._prog_timer.stop()
        self._mode = "idle"
        elapsed = max(0.1, time.time() - self._t0)
        self._append("\n=== 安装结束，退出码 %s ===\n" % code)
        self.btn_check.setEnabled(True)
        self.btn_close.setEnabled(True)
        self.btn_install.setEnabled(True)
        if code == 0:
            self._update_bar(100, "✔ 一键安装与校验全部完成 (100%%) · 总耗时 %ds" % int(elapsed))
            # 重新探测一次设备，让顶部“AI 加速”状态也刷新。
            try:
                owner = self.parent()
                if owner is not None and hasattr(owner, "a"):
                    owner.a._start_probe()
            except Exception:
                pass
            self._set_status("安装完成，正在重新体检…")
            self.start_check()
        else:
            self._update_bar(self.install_bar.value(),
                             "✖ 安装未完成（退出码 %s）· 请查看下方日志" % code)
            self._set_status("安装没有完成（退出码 %s）。看上面的日志，修好后再点一次。" % code)

    def _install_error(self, err):
        self._prog_timer.stop()
        self._mode = "idle"
        self._update_bar(0, "✖ 安装进程启动失败")
        self._append("\n[启动安装进程失败] %s\n" % err)
        self.btn_check.setEnabled(True)
        self.btn_close.setEnabled(True)
        self.btn_install.setEnabled(True)
        self._set_status("安装进程启动失败。")

    def closeEvent(self, ev):
        if self._proc is not None and self._proc.state() != QProcess.NotRunning:
            QMessageBox.information(self, "安装进行中",
                                    "一键安装还在运行，请等它结束再关闭这个窗口。")
            ev.ignore()
            return
        self._prog_timer.stop()
        super(EnvCheckDialog, self).closeEvent(ev)


class UnifiedWindow(QMainWindow):
    sig_external = pyqtSignal(str, object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("《异键演奏》")
        self.resize(1180, 900)
        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        top = QHBoxLayout()
        title = QLabel("《异键演奏》")
        title.setObjectName("appTitle")
        top.addWidget(title)
        self.lbl_ai = QLabel("AI 加速：检测中…")
        self.lbl_game = QLabel("游戏窗口：检测中…")
        self.lbl_admin = QLabel("管理员：检测中…")
        for w in (self.lbl_ai, self.lbl_game, self.lbl_admin):
            top.addWidget(w)
        top.addStretch(1)
        self.theme_mode = GT.load_mode() or "dark"
        self._env_dialog = None
        self.btn_env_check = QPushButton("环境与显卡体检")
        self.btn_env_check.clicked.connect(self.show_env_check)
        top.addWidget(self.btn_env_check)
        self.btn_theme = QPushButton()
        self.btn_theme.clicked.connect(self.toggle_theme)
        top.addWidget(self.btn_theme)
        self._sync_theme_button()
        top.addWidget(QLabel("[F8] 开始/暂停  [F12] 紧急停止"))
        lay.addLayout(top)

        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)

        # 标签 1：扒谱（单屏全景，由 A 端自己排布）
        self.tab_a = QWidget()
        la = QVBoxLayout(self.tab_a)
        self.a = AGui.MainWindow()
        a_central = self.a.takeCentralWidget()
        self.btn_send = QPushButton("一键送入演奏台")
        self.btn_send.clicked.connect(self.send_current_to_player)
        self.btn_send.hide()
        self.chk_auto_send = QPushButton("全流程完成后自动送入（点此切换）")
        self.chk_auto_send.setCheckable(True)
        self.chk_auto_send.hide()
        if a_central is not None:
            la.addWidget(a_central, 1)
        self.tabs.addTab(self.tab_a, "AI 智能扒谱（音频转 MIDI）")

        # 标签 2：演奏
        self.tab_b = QWidget()
        lb = QVBoxLayout(self.tab_b)
        self.b = BGui.MainWindow()
        b_central = self.b.takeCentralWidget()
        if b_central is not None:
            lb.addWidget(b_central, 1)
        self.tabs.addTab(self.tab_b, "吉他自动演奏（编译与键鼠执行）")

        # 一键全流程完成后自动送入演奏台
        try:
            self.a.sig_all_done.connect(self._on_a_all_done)
            self.a.sendRequested.connect(self.send_current_to_player)
        except Exception:
            pass

        self.sig_external.connect(self._apply_external)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh_status)
        self._timer.start(2000)
        self.refresh_status()

    def _sync_theme_button(self):
        if self.theme_mode == "dark":
            self.btn_theme.setText("浅色")
            self.btn_theme.setToolTip("当前：Studio Dark；点击切换浅色")
        else:
            self.btn_theme.setText("深色")
            self.btn_theme.setToolTip("当前：Modern Light；点击切换深色")

    def toggle_theme(self):
        self.theme_mode = "light" if self.theme_mode == "dark" else "dark"
        GT.apply_theme(QApplication.instance(), self.theme_mode)
        GT.save_mode(self.theme_mode)
        self._sync_theme_button()

    def show_env_check(self):
        dlg = getattr(self, "_env_dialog", None)
        if dlg is None:
            dlg = EnvCheckDialog(self)
            self._env_dialog = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        if not dlg.log.toPlainText().strip():
            dlg.start_check()
        return dlg

    def _on_a_all_done(self, path):
        try:
            if self.a.chk_auto_send.isChecked():
                self.send_to_player(path)
                self.tabs.setCurrentIndex(1)
        except Exception:
            pass

    def refresh_status(self):
        try:
            dev = self.a.device_state.text()
        except Exception:
            dev = "未知"
        self.lbl_ai.setText("AI 加速：%s" % dev)
        try:
            m = self.b.io.metrics()
        except Exception:
            m = None
        if m:
            self.lbl_game.setText("游戏窗口：已连接 (%d×%d)" % (m[2], m[3]))
        else:
            self.lbl_game.setText("游戏窗口：未检测到")
        try:
            adm = ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            adm = False
        self.lbl_admin.setText("管理员：%s" % ("有" if adm else "没有（按键送不进游戏）"))

    def _apply_external(self, path, compiled):
        """主线程里刷新演奏页（后台线程只 emit，不碰控件）。"""
        try:
            self.b.sync_external(path, compiled or {})
            self.b._append_log("√ 已送入演奏台并预编译四角色：%s"
                               % os.path.basename(path))
        except Exception as exc:  # noqa: BLE001
            try:
                self.b._append_log("× 送入演奏台失败：%s" % exc)
            except Exception:
                pass

    def send_to_player(self, path, wait=False):
        """内部联动：加载 .mid 到演奏页并预编译四角色（UI 状态一并同步）。"""
        path = str(path)
        self.tabs.setCurrentIndex(1)
        self.b.path = path
        try:
            self.b.lbl_file.setText(os.path.basename(path))
        except Exception:
            pass
        roles = (MC.ROLE_SOLO, MC.ROLE_DUET_LEAD,
                 MC.ROLE_DUET_RHYTHM, MC.ROLE_DUET_RHYTHM_FRET)

        def job():
            compiled = {}
            try:
                ap = self.b.chk_arpeggio.isChecked()
            except Exception:
                ap = True
            try:
                cr = self.b.chk_chord_rhythm.isChecked()
            except Exception:
                cr = True
            for role in roles:
                try:
                    tl = MC.compile_file(path, role=role, enable_arpeggio=ap,
                                         enable_chord_rhythm=cr)
                    compiled[role] = tl
                except Exception as exc:  # noqa: BLE001
                    self.b._append_log("× 预编译 %s 失败：%s" % (role, exc))
            return compiled

        if wait:
            self._apply_external(path, job())
        else:
            def bg():
                compiled = job()
                try:
                    self.sig_external.emit(path, compiled)
                except Exception:
                    pass
            threading.Thread(target=bg, daemon=True).start()
        return len(self.b.timelines) >= 4

    def send_current_to_player(self):
        path = None
        try:
            r3 = (self.a.results or {}).get(3)
            if r3:
                path = (r3.get("outputs") or {}).get("final_midi")
        except Exception:
            path = None
        if not path or not os.path.isfile(path):
            from PyQt5.QtWidgets import QFileDialog
            path, _ = QFileDialog.getOpenFileName(self, "选择要演奏的 MIDI",
                                                  str(ROOT / "out"), "MIDI (*.mid)")
        if path:
            self.send_to_player(path)
            self.tabs.setCurrentIndex(1)

    def closeEvent(self, ev):
        try:
            self.a.close()
        except Exception:
            pass
        try:
            if self.b.player and self.b.player.is_alive():
                self.b.player.stop()
        except Exception:
            pass
        try:
            self.b.close()
        except Exception:
            pass
        ev.accept()


def _load_app_icon():
    """窗口/任务栏图标：优先 assets\\icon.ico，缺失时退回 icon.png；没有就忽略。"""
    try:
        from PyQt5.QtGui import QIcon
    except Exception:
        return None
    for name in ("icon.ico", "icon.png"):
        path = ROOT / "assets" / name
        if path.is_file():
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon
    return None


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    # High-DPI 抗锯齿（必须在 QApplication 创建之前设置）
    try:
        from PyQt5.QtCore import Qt, QCoreApplication
        QCoreApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
        QCoreApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    except Exception:
        pass
    # 让任务栏把本程序当成独立应用（用自己的图标，不跟 python 合并）
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "YijianYanzou.Desktop")
    except Exception:
        pass
    app = QApplication(argv)
    _icon = _load_app_icon()
    if _icon is not None:
        app.setWindowIcon(_icon)
    apply_theme(app)
    win = UnifiedWindow()
    if "--smoke" in argv:
        win.show()

        def _finish():
            try:
                if getattr(win.a, "probe", None) is not None:
                    win.a.probe.wait(20000)
            except Exception:
                pass
            app.quit()

        QTimer.singleShot(1000, _finish)
        rc = app.exec_()
        print("GUI_SMOKE_OK title=%s tabs=%d" % (win.windowTitle(), win.tabs.count()))
        return 0 if rc == 0 else rc
    win.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
