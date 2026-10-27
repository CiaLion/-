# -*- coding: utf-8 -*-
"""《异键演奏》图形界面（PyQt5）。

界面就是需求要求的三步流水线：
  ① SheetSage2 音频转录
  ② 物理秒对齐 + 主旋律 / 和弦合并
  ③ 超音域修正 + 最终 MIDI 导出

每一步都能单独跑、单独导入中间文件、单独导出本步产物；
顶部还有「一键全流程」，从原始音频直接出成品。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

# 确保子进程（transcribe_cli.py）标准输出实时刷新，不因管道缓冲卡住日志与进度条
os.environ.setdefault("PYTHONUNBUFFERED", "1")

# Qt 平台插件装在含中文的项目路径下时，Qt 自身解析会失败；
# 显式用环境变量指向本 venv 的插件目录（必须在 import PyQt5 之前设置）。
_QT_PLUGINS = Path(sys.prefix) / "Lib" / "site-packages" / "PyQt5" / "Qt5" / "plugins"
if _QT_PLUGINS.is_dir() and not os.environ.get("QT_PLUGIN_PATH"):
    os.environ["QT_PLUGIN_PATH"] = str(_QT_PLUGINS)

from PyQt5.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QFont, QTextCursor
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSizePolicy, QSpinBox, QTabWidget, QVBoxLayout,
    QWidget,
)

HERE = Path(__file__).resolve().parent
PROJECT_DIR = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

try:
    from . import pipeline  # noqa: E402
except ImportError:            # 直接运行本文件
    import pipeline  # noqa: E402,F401

APP_TITLE = "《异键演奏》"
SETTINGS_PATH = HERE / "gui_settings.json"
AUDIO_FILTER = "音频文件 (*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.wma);;所有文件 (*.*)"
MIDI_FILTER = "MIDI / JSON (*.mid *.midi *.json);;所有文件 (*.*)"


def _resolve_out(value) -> Path:
    """输出目录跨电脑解析：空 / "out" / 旧绝对路径不存在 → 项目内 out。"""
    s = str(value or "").strip()
    if not s or s.lower() in ("out", ".\\out", "out\\", "out/"):
        return PROJECT_DIR / "out"
    p = Path(s).expanduser()
    if not p.is_absolute():
        return (PROJECT_DIR / p).resolve()
    try:
        if p.exists():
            return p
    except Exception:
        pass
    return PROJECT_DIR / "out"


def _out_display(value) -> str:
    """界面显示：项目内目录用相对 "out"，项目外保留实际路径。"""
    p = _resolve_out(value)
    try:
        rel = p.relative_to(PROJECT_DIR)
        return str(rel).replace("\\", "/") or "out"
    except Exception:
        return str(p)


def _out_store(value) -> str:
    """写盘：项目内目录存相对 "out"，项目外存绝对路径。"""
    p = _resolve_out(value)
    try:
        return str(p.relative_to(PROJECT_DIR)).replace("\\", "/")
    except Exception:
        return str(p)

def _read_json(path: Path, default: dict) -> dict:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else default
    except Exception:  # noqa: BLE001
        return default


class _Stream(QObject):
    """把 print() 的输出转成 Qt 信号，供界面日志框实时显示。"""

    text = pyqtSignal(str)

    def write(self, chunk: str) -> None:
        if chunk:
            self.text.emit(chunk)

    def flush(self) -> None:
        pass


class Worker(QThread):
    log = pyqtSignal(str)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self) -> None:
        old = sys.stdout
        stream = _Stream()
        stream.text.connect(self.log)
        sys.stdout = stream
        try:
            pipeline.reset_cancel()
            result = self.fn()
            self.done.emit(result)
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            if not isinstance(exc, (AssertionError, FileNotFoundError, ValueError, RuntimeError)):
                detail += "\n" + traceback.format_exc(limit=6)
            self.failed.emit(detail)
        finally:
            sys.stdout = old


class ProbeWorker(QThread):
    ready = pyqtSignal(dict)

    def run(self) -> None:
        try:
            self.ready.emit(pipeline.probe_device(force=True))
        except Exception as exc:  # noqa: BLE001
            self.ready.emit({"best": "cpu", "xpu": False, "cuda": False, "name": "",
                             "error": str(exc)})


class MainWindow(QMainWindow):
    sig_all_done = pyqtSignal(str)
    sendRequested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        try:
            _avail = QApplication.desktop().availableGeometry(self)
            _w = min(1080, int(_avail.width() * 0.94))
            _h = min(780, int(_avail.height() * 0.92))
            _minw = min(760, int(_avail.width() * 0.72))
            _minh = min(520, int(_avail.height() * 0.62))
        except Exception:
            _w, _h, _minw, _minh = 1080, 780, 760, 520
        self.resize(max(_minw, _w), max(_minh, _h))
        self.setMinimumSize(_minw, _minh)

        self.worker = None
        self.probe = None
        self._sep_pct = 0
        self._sep_active = False

        self.results = {1: None, 2: None, 3: None}
        self._run_t0 = 0.0
        self._stage_t0 = 0.0
        self._run_label = ""
        self._run_stage_pct = 0
        self._run_stage_text = ""
        self._active_step = 0  # 0=全流程, 1/2/3=单步

        self._build_ui()
        self._load_settings()
        self._start_probe()
        self._prog_timer = QTimer(self)
        self._prog_timer.setInterval(200)
        self._prog_timer.timeout.connect(self._tick_transcribe_progress)
        self.log("就绪：选择音频文件后点击「开始扒谱」。")
        self.log(f"配置文件：{pipeline.config_file()}")

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        """项目组 单屏三区工作台（不再用 ①②③ 标签页藏参数）。"""
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        outer.addWidget(self._build_inputs_card(), 0)
        self._pipeline_card = self._build_pipeline_row()
        outer.addWidget(self._pipeline_card, 0)
        self._pipeline_card.hide()
        outer.addWidget(self._build_log(), 1)

        # 兼容旧引用：保留一个隐藏的 tabs（不再显示，也不承载控件）
        self.tabs = QTabWidget(self)
        for name in ("① 音频转录", "② 对齐合并", "③ 修正导出"):
            self.tabs.addTab(QWidget(self), name)
        self.tabs.hide()

    def _mini_label(self, text, width=96, obj="fieldname"):
        lab = QLabel(text)
        lab.setObjectName(obj)
        if width:
            lab.setFixedWidth(width)
        return lab

    def _build_inputs_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(7)

        title = QLabel("音频转 MIDI（AI 智能扒谱）")
        title.setObjectName("cardtitle")
        v.addWidget(title)

        self.s1_audio = QLineEdit()
        self.s1_audio.setPlaceholderText("选择 mp3 / wav / flac / m4a 音频")
        row_audio = QHBoxLayout()
        row_audio.setSpacing(8)
        row_audio.addWidget(self._mini_label("原始音频", 84))
        row_audio.addWidget(self.s1_audio, 1)
        row_audio.addWidget(self._mini_label("成品文件名", 78))
        self.s3_name = QLineEdit()
        self.s3_name.setPlaceholderText("留空 = 与音频同名")
        self.s3_name.setFixedWidth(190)
        row_audio.addWidget(self.s3_name)
        btn_audio = QPushButton("浏览…")
        btn_audio.setObjectName("ghost")
        btn_audio.setFixedWidth(72)
        btn_audio.clicked.connect(lambda: self._browse(self.s1_audio, "file", AUDIO_FILTER, "选择原始音频"))
        row_audio.addWidget(btn_audio)
        v.addLayout(row_audio)

        self.s1_out = QLineEdit(card)
        self.s1_out.setText("out")
        self.s1_out.setVisible(False)
        self.s1_out.textChanged.connect(self._sync_out_dirs)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.addWidget(self._mini_label("推理设备", 84), 0, 0)
        box_dev = QHBoxLayout()
        box_dev.setSpacing(8)
        self.device_combo = QComboBox()
        self.device_combo.addItems(["自动检测", "XPU（Intel Arc）", "CUDA（NVIDIA）",
                                    "ROCm（AMD）", "CPU"])
        self.device_combo.setMinimumWidth(190)
        box_dev.addWidget(self.device_combo, 1)
        self.device_state = QLabel("正在探测设备…")
        self.device_state.setObjectName("dev")
        box_dev.addWidget(self.device_state, 2)
        grid.addLayout(box_dev, 0, 1, 1, 3)

        grid.addWidget(self._mini_label("音色适配", 84), 1, 0)
        self.s3_instrument = QComboBox()
        self.s3_instrument.addItem("电吉他模式", "electric")
        self.s3_instrument.addItem("木吉他模式", "acoustic")
        self.s3_instrument.setMinimumWidth(150)
        grid.addWidget(self.s3_instrument, 1, 1, 1, 3)

        self.s3_low = QSpinBox(card)
        self.s3_low.setRange(0, 127)
        self.s3_low.setValue(40)
        self.s3_low.setVisible(False)
        self.s3_high = QSpinBox(card)
        self.s3_high.setRange(0, 127)
        self.s3_high.setValue(88)
        self.s3_high.setVisible(False)

        grid.addWidget(self._mini_label("扒谱声部", 84), 2, 0)
        row_stem = QHBoxLayout()
        row_stem.setSpacing(12)
        self.chk_vocal = QCheckBox("人声主旋律")
        self.chk_inst = QCheckBox("器乐旋律")
        for _cb in (self.chk_vocal, self.chk_inst):
            _cb.setChecked(True)
            _cb.toggled.connect(self._on_stem_toggled)
            row_stem.addWidget(_cb)
        self.cmb_sep_model = QComboBox()
        self.cmb_sep_model.addItem("内置双轨直出（免额外模型，最快）", "native")
        self.cmb_sep_model.addItem("HT-Demucs 极速分离 (约319MB，原生免装包)", "htdemucs")
        self.cmb_sep_model.addItem("Mel-Band RoFormer 高精分离 (约871MB，最高音质)", "roformer")
        self.cmb_sep_model.setMinimumWidth(280)
        self.cmb_sep_model.setEnabled(False)
        row_stem.addWidget(self.cmb_sep_model)
        self.btn_open_sep = QPushButton("打开分离音频所在目录")
        self.btn_open_sep.setObjectName("ghost")
        self.btn_open_sep.clicked.connect(self._open_temp_sep)
        row_stem.addWidget(self.btn_open_sep)
        row_stem.addStretch(1)
        grid.addLayout(row_stem, 2, 1, 1, 3)

        # 单模型管线固定按“时长更长优先”合并；控件保留但不显示，兼容旧设置
        self.s2_mode = QComboBox()
        self.s2_mode.addItems(["时长更长优先", "力度更高优先"])
        self.s2_mode.setVisible(False)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        v.addLayout(grid)

        self.btn_paths_adv = QPushButton("▶ 手动指定分步文件（可选）")
        self.btn_paths_adv.setObjectName("ghost")
        self.btn_paths_adv.setCheckable(True)
        self.btn_paths_adv.toggled.connect(self._toggle_paths)
        row_adv = QHBoxLayout()
        row_adv.addWidget(self.btn_paths_adv)
        row_adv.addStretch(1)
        self.btn_paths_adv.setVisible(False) 
        v.addLayout(row_adv)

        self.paths_box = QWidget()
        pg = QGridLayout(self.paths_box)
        pg.setContentsMargins(0, 4, 0, 0)
        pg.setHorizontalSpacing(8)
        pg.setVerticalSpacing(5)
        self.s2_vocal = QLineEdit()
        self.s2_vocal.setPlaceholderText("melody_vocal.mid（可留空）")
        self.s2_ins = QLineEdit()
        self.s2_ins.setPlaceholderText("melody_instrumental.mid（可留空）")
        self.s2_chords = QLineEdit()
        self.s2_chords.setPlaceholderText("chords.mid（可留空）")
        self.s2_integrated = QLineEdit()
        self.s2_integrated.setPlaceholderText("transcription.mid（可选）")
        self.s3_melody = QLineEdit()
        self.s3_melody.setPlaceholderText("main_melody.mid（可留空）")
        self.s3_chords = QLineEdit()
        self.s3_chords.setPlaceholderText("chords_merged.mid（可留空）")
        self.s2_out = QLineEdit()
        self.s3_out = QLineEdit()
        fields = (("人声旋律", self.s2_vocal), ("器乐旋律", self.s2_ins),
                  ("和弦轨", self.s2_chords), ("综合 MIDI", self.s2_integrated),
                  ("主旋律", self.s3_melody), ("和弦轨（③）", self.s3_chords))
        for i, (lab, edit) in enumerate(fields):
            r, c = divmod(i, 2)
            pg.addWidget(self._mini_label(lab, 78), r, c * 2)
            pg.addWidget(edit, r, c * 2 + 1)
        pg.addWidget(self._mini_label("② 输出目录", 78), 3, 0)
        pg.addWidget(self.s2_out, 3, 1)
        pg.addWidget(self._mini_label("③ 输出目录", 78), 3, 2)
        pg.addWidget(self.s3_out, 3, 3)
        pg.setColumnStretch(1, 1)
        pg.setColumnStretch(3, 1)
        self.paths_box.setVisible(False)
        v.addWidget(self.paths_box)

        row_btns = QHBoxLayout()
        row_btns.setSpacing(8)
        self.btn_run_all = QPushButton("▶ 开始扒谱")
        self.btn_run_all.setObjectName("primary")
        self.btn_run_all.setMinimumHeight(38)
        self.btn_run_all.clicked.connect(self._run_all)
        row_btns.addWidget(self.btn_run_all)
        self.btn_stop = QPushButton("⏹ 停止")
        self.btn_stop.setObjectName("danger")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        row_btns.addWidget(self.btn_stop)
        self.btn_open_out = QPushButton("打开输出目录")
        self.btn_open_out.setObjectName("ghost")
        self.btn_open_out.clicked.connect(self._open_out_dir)
        row_btns.addWidget(self.btn_open_out)
        self.btn_roll_final = QPushButton("卷帘微调成品")
        self.btn_roll_final.setObjectName("ghost")
        self.btn_roll_final.clicked.connect(lambda: self.open_piano_roll(3))
        row_btns.addWidget(self.btn_roll_final)
        row_btns.addStretch(1)
        self.chk_auto_send = QCheckBox("完成后自动送入演奏台")
        self.chk_auto_send.setVisible(False)
        row_btns.addWidget(self.chk_auto_send)
        self.btn_send = QPushButton("送入演奏台")
        self.btn_send.clicked.connect(self.sendRequested.emit)
        row_btns.addWidget(self.btn_send)
        v.addLayout(row_btns)

        self.transcribe_bar = QProgressBar()
        self.transcribe_bar.setObjectName("stageBar")
        self.transcribe_bar.setRange(0, 100)
        self.transcribe_bar.setValue(0)
        self.transcribe_bar.setTextVisible(True)
        self.transcribe_bar.setFormat("就绪 — 选择音频文件后点击「开始扒谱」")
        v.addWidget(self.transcribe_bar)
        return card

    def _build_pipeline_row(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        outer = QVBoxLayout(card)
        outer.setContentsMargins(14, 10, 14, 12)
        outer.setSpacing(6)
        title = QLabel("② 分步状态与微调")
        title.setObjectName("cardtitle")
        outer.addWidget(title)
        row = QHBoxLayout()
        row.setSpacing(10)
        specs = ((1, "① 音频转录（SheetSage2）", "s1_status", "btn_s1", "btn_roll1", "btn_exp1"),
                 (2, "② 声部对齐与单音合并", "s2_status", "btn_s2", "btn_roll2", "btn_exp2"),
                 (3, "③ 音域修正与成品导出", "s3_status", "btn_s3", "btn_roll3", "btn_exp3"))
        for idx, head, status, run, roll, exp in specs:
            box = QFrame()
            box.setObjectName("card_inner")
            bv = QVBoxLayout(box)
            bv.setContentsMargins(10, 8, 10, 8)
            bv.setSpacing(5)
            h = QLabel(head)
            h.setObjectName("stepTitle")
            bv.addWidget(h)
            st = QLabel("未运行")
            st.setObjectName("statusBadge")
            st.setWordWrap(True)
            st.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
            st.setMinimumWidth(220)
            st.setMinimumHeight(30)
            setattr(self, status, st)
            bv.addWidget(st)
            br = QHBoxLayout()
            b_roll = QPushButton("卷帘微调")
            b_roll.setObjectName("ghost")
            b_roll.setMinimumHeight(36)
            b_roll.clicked.connect(lambda _=False, i=idx: self.open_piano_roll(i))
            setattr(self, roll, b_roll)
            br.addWidget(b_roll)
            b_exp = QPushButton("导出")
            b_exp.setObjectName("ghost")
            b_exp.setMinimumHeight(36)
            b_exp.clicked.connect(lambda _=False, i=idx: self._export(i))
            setattr(self, exp, b_exp)
            br.addWidget(b_exp)
            bv.addLayout(br)
            b_run = QPushButton("▶ 仅运行步骤 %s" % "①②③"[idx - 1])
            b_run.setMinimumHeight(36)
            b_run.clicked.connect(lambda _=False, i=idx: self._run_step(i))
            setattr(self, run, b_run)
            bv.addWidget(b_run)
            row.addWidget(box, 1)
        outer.addLayout(row)
        return card

    def _toggle_paths(self, on: bool) -> None:
        self.paths_box.setVisible(bool(on))
        self.btn_paths_adv.setText("▼ 手动指定分步文件（可选）" if on
                                   else "▶ 手动指定分步文件（可选）")

    def _sync_out_dirs(self, text: str) -> None:
        if hasattr(self, "s2_out"):
            self.s2_out.setText(text)
            self.s3_out.setText(text)

    def _run_step(self, idx: int) -> None:
        {1: self._run_step1, 2: self._run_step2, 3: self._run_step3}.get(idx, lambda: None)()



    def _field(self, parent, label: str, edit: QLineEdit, mode: str,
               filt: str = "", caption: str = "选择") -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        name = QLabel(label)
        name.setObjectName("fieldname")
        name.setFixedWidth(96)
        row.addWidget(name)
        row.addWidget(edit, 1)
        btn = QPushButton("浏览…")
        btn.setObjectName("ghost")
        btn.setFixedWidth(72)
        btn.clicked.connect(lambda: self._browse(edit, mode, filt, caption))
        row.addWidget(btn)
        parent.addLayout(row)
        return row





    def _build_log(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("card")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(14, 10, 14, 12)
        lay.setSpacing(6)
        head = QHBoxLayout()
        title = QLabel("运行日志")
        title.setObjectName("cardtitle")
        head.addWidget(title)
        head.addStretch(1)
        btn_clear = QPushButton("清空")
        btn_clear.setObjectName("ghost")
        btn_clear.clicked.connect(lambda: self.log_view.clear())
        head.addWidget(btn_clear)
        btn_copy = QPushButton("复制日志")
        btn_copy.setObjectName("ghost")
        btn_copy.clicked.connect(self._copy_log)
        head.addWidget(btn_copy)
        lay.addLayout(head)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("log")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(6000)
        self.log_view.setMinimumHeight(80)
        lay.addWidget(self.log_view, 1)
        return frame

    # -------------------------------------------------------------- helpers
    def log(self, text: str) -> None:
        self.log_view.appendPlainText(
            f"[{time.strftime('%H:%M:%S')}] {text}" if "\n" not in text else text.rstrip("\n"))
        self.log_view.moveCursor(QTextCursor.End)

    def _copy_log(self) -> None:
        QApplication.clipboard().setText(self.log_view.toPlainText())
        self.log("日志已复制到剪贴板。")

    def _browse(self, edit: QLineEdit, mode: str, filt: str, caption: str) -> None:
        start = edit.text().strip() or str(HERE)
        if mode == "dir":
            path = QFileDialog.getExistingDirectory(self, caption, start)
        elif mode == "save":
            path, _ = QFileDialog.getSaveFileName(self, caption, start, filt)
        else:
            path, _ = QFileDialog.getOpenFileName(self, caption, start, filt)
        if path:
            edit.setText(path)

    def _device_key(self) -> str:
        return {0: "auto", 1: "xpu", 2: "cuda", 3: "amd", 4: "cpu"}.get(
            self.device_combo.currentIndex(), "auto")

    def _dtype_key(self) -> str:
        # 精度不再手选：按设备能力自动分配（bf16 / fp16 / fp32）
        return "auto"

    def _merge_key(self) -> str:
        # 单模型管线：人声与器乐轨固定按时长更长优先合并
        return "duration"

    def _start_probe(self) -> None:
        self.probe = ProbeWorker(self)
        self.probe.ready.connect(self._on_probe)
        self.probe.start()

    def _on_probe(self, info: dict) -> None:
        best = (info.get("best") or "cpu").lower()
        vendor = (info.get("vendor") or "").lower()
        name = info.get("name") or ""
        if best == "xpu":
            self.device_state.setText(f"当前可用：{name or 'Intel Arc'}（XPU 加速）")
        elif best == "cuda" and vendor == "amd":
            self.device_state.setText(f"当前可用：{name or 'AMD GPU'}（ROCm 加速）")
        elif best == "cuda":
            self.device_state.setText(f"当前可用：{name or 'NVIDIA'}（CUDA 加速）")
        else:
            self.device_state.setText("当前可用：CPU（速度较慢）")
        if info.get("ai_ready") is False:
            self.device_state.setText("未安装 AI 扒谱模型（点右上角「环境与显卡体检」一键安装）")
        if info.get("error"):
            self.log(f"设备探测提示：{info['error']}")

    def _set_running(self, running: bool) -> None:
        btns = [self.btn_run_all, self.btn_s1, self.btn_s2, self.btn_s3,
                self.btn_roll1, self.btn_roll2, self.btn_roll3,
                self.btn_exp1, self.btn_exp2, self.btn_exp3]
        for btn in btns:
            try:
                btn.setEnabled(not running)
            except Exception:
                pass
        for w in (self.device_combo, self.s2_mode,
                  self.s3_instrument, self.s3_low, self.s3_high):
            try:
                w.setEnabled(not running)
            except Exception:
                pass
        self.btn_stop.setEnabled(running)

    def _update_transcribe_bar(self, pct: int, text: str) -> None:
        if hasattr(self, "transcribe_bar"):
            pct = max(0, min(100, int(pct)))
            self.transcribe_bar.setValue(pct)
            self.transcribe_bar.setFormat(text)

    def _tick_transcribe_progress(self) -> None:
        if self.worker is None or not self.worker.isRunning():
            return
        elapsed = max(0.0, time.time() - self._run_t0)
        if self._sep_active:
            # 分离占 3%~35%
            pct = min(35, max(self._run_stage_pct, 3 + int(0.32 * self._sep_pct)))
            self._run_stage_pct = pct
            self._update_transcribe_bar(
                pct,
                f"正在分离人声与伴奏… ({self._sep_pct}%) · 已用时 {elapsed:.1f}s")
            self.s1_status.setText(f"分离中… ({self._sep_pct}% · {elapsed:.1f}s)")
            return
        # 在步骤 ① 耗时推理期间，使用渐近曲线平滑推进进度条（不卡死，也不提前到 90%）
        if self._active_step in (0, 1) and self._run_stage_pct < 86:
            est = max(0.0, time.time() - (self._stage_t0 or self._run_t0))
            if est < 8.0 and self._run_stage_pct < 18:
                pct = max(self._run_stage_pct, min(18, 3 + int(est * 1.9)))
                stage = self._run_stage_text or "正在加载 AI 模型至显卡与初始化音频特征"
            else:
                # 8s 之后从 18% 渐近推进到 85%（时间常数 32s，兼容短歌与 4 分钟长歌）
                import math
                asym = 18 + int(67.0 * (1.0 - math.exp(-max(0.0, est - 8.0) / 32.0)))
                pct = max(self._run_stage_pct, min(85, asym))
                stage = self._run_stage_text or "正在执行步骤 ①：SheetSage2 音频智能转录"
            self._run_stage_pct = pct
        else:
            pct = self._run_stage_pct
            stage = self._run_stage_text or f"正在运行 {self._run_label}"

        self._update_transcribe_bar(pct, f"{stage} ({pct}%) · 已用时 {elapsed:.1f}s")
        # 同步在对应步骤的状态徽章上显示实时读秒
        if self._active_step == 1 or (self._active_step == 0 and pct < 88):
            self.s1_status.setText(f"运行中… ({pct}% · {elapsed:.1f}s)")
        elif self._active_step == 2 or (self._active_step == 0 and 88 <= pct < 95):
            self.s2_status.setText(f"运行中… ({pct}% · {elapsed:.1f}s)")
        elif self._active_step == 3 or (self._active_step == 0 and pct >= 95):
            self.s3_status.setText(f"运行中… ({pct}% · {elapsed:.1f}s)")

    def _run(self, fn, on_done, label: str, step_idx: int = 0,
             sep_active: bool = False) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.log("× 还有任务在跑，先等它结束或点「停止」。")
            return
        self._run_t0 = time.time()
        self._stage_t0 = self._run_t0
        self._run_label = label
        self._active_step = step_idx
        self._sep_pct = 0
        self._sep_active = bool(sep_active)
        if step_idx in (0, 1):
            self._run_stage_pct = 3
            self._run_stage_text = "正在启动 AI 音频转录管线"
        elif step_idx == 2:
            self._run_stage_pct = 30
            self._run_stage_text = "正在执行步骤 ②：声部对齐与单音合并"
        else:
            self._run_stage_pct = 50
            self._run_stage_text = "正在执行步骤 ③：音域修正与 MIDI 导出"
        self._update_transcribe_bar(self._run_stage_pct,
                                    f"{self._run_stage_text} ({self._run_stage_pct}%) · 已用时 0.0s")
        self._set_running(True)
        self._prog_timer.start()
        self.log(f"—————— {label} 开始 ——————")
        self.worker = Worker(fn, self)
        self.worker.log.connect(self._on_worker_log)
        self.worker.done.connect(lambda result: self._on_done(result, on_done, label))
        self.worker.failed.connect(lambda msg: self._on_failed(msg, label))
        self.worker.start()

    def _on_worker_log(self, chunk: str) -> None:
        text = chunk.rstrip("\n")
        if text.strip():
            self.log(text)
            for line in text.splitlines():
                s = line.strip()
                if not s:
                    continue
                if s.startswith("[分离进度]"):
                    m_sep = re.search(r"(\d{1,3})\s*%", s)
                    if m_sep:
                        self._sep_pct = min(100, max(0, int(m_sep.group(1))))
                        self._sep_active = True
                        self._run_stage_pct = max(
                            self._run_stage_pct, min(32, 3 + int(0.29 * self._sep_pct)))
                    continue
                if "[步骤1/3]" in s:
                    self._sep_active = False
                    self._stage_t0 = time.time()
                if "加载" in s or "loading" in s.lower() or "merge_lora" in s.lower():
                    self._run_stage_pct = max(self._run_stage_pct, 12)
                    self._run_stage_text = "正在加载 SheetSage2 与 MERT 模型权重"
                elif "transcribe" in s.lower() or "转录" in s:
                    self._run_stage_pct = max(self._run_stage_pct, 20)
                    self._run_stage_text = "正在执行步骤 ①：SheetSage2 音频智能转录"
                m_pct = re.search(r"(\d{1,3})\s*%", s)
                if m_pct and self._run_stage_pct < 86:
                    raw_p = min(100, max(0, int(m_pct.group(1))))
                    mapped = 18 + int(0.67 * raw_p)
                    self._run_stage_pct = max(self._run_stage_pct, min(85, mapped))
                if "[步骤1/3] 完成" in s:
                    self._run_stage_pct = max(self._run_stage_pct, 88)
                    self._run_stage_text = "步骤 ① 完成，正在执行步骤 ② 声部对齐合并"
                elif "[步骤2/3] 完成" in s:
                    self._run_stage_pct = max(self._run_stage_pct, 95)
                    self._run_stage_text = "步骤 ② 完成，正在执行步骤 ③ 音域修正导出"
                elif "[步骤3/3] 完成" in s:
                    self._run_stage_pct = 99
                    self._run_stage_text = "步骤 ③ 完成，正在整理输出文件"
            self._tick_transcribe_progress()

    def _on_done(self, result, on_done, label: str) -> None:
        self._prog_timer.stop()
        elapsed = max(0.1, time.time() - self._run_t0)
        self._set_running(False)
        self._update_transcribe_bar(100, f"✔ {label}已完成 (100%) · 总耗时 {elapsed:.1f}s")
        self.log(f"√ {label} 完成（耗时 {elapsed:.1f}s）")
        try:
            on_done(result)
        except Exception as exc:  # noqa: BLE001
            self.log(f"× 结果处理出错：{type(exc).__name__}: {exc}")

    def _on_failed(self, message: str, label: str) -> None:
        self._prog_timer.stop()
        elapsed = max(0.1, time.time() - self._run_t0)
        self._set_running(False)
        self._update_transcribe_bar(self._run_stage_pct,
                                    f"✖ {label}未完成（已用时 {elapsed:.1f}s）· 请查看下方日志")
        self.log(f"× {label} 失败：{message}")
        QMessageBox.warning(self, "运行失败",
                            f"{label} 失败：\n\n{message.splitlines()[0]}")

    def _stop(self) -> None:
        if pipeline.cancel_current():
            self.log("⏹ 已请求停止当前子进程…")
        else:
            self.log("⏹ 已请求停止（等待当前任务结束）。")

    # ------------------------------------------------------------- settings
    def _load_settings(self) -> None:
        data = _read_json(SETTINGS_PATH, {})
        s1 = data.get("step1") or {}
        s2 = data.get("step2") or {}
        s3 = data.get("step3") or {}
        # 兼容旧版 gui_settings.json 的顶层 audio / outdir
        legacy_audio = data.get("audio") or ""
        legacy_out = data.get("outdir") or ""
        self.s1_audio.setText(str(s1.get("audio") or legacy_audio))
        self.s1_out.setText("out")
        self.s2_vocal.setText(str(s2.get("vocal") or ""))
        self.s2_ins.setText(str(s2.get("instrumental") or ""))
        self.s2_chords.setText(str(s2.get("chords") or ""))
        self.s2_integrated.setText(str(s2.get("integrated") or ""))
        self.s2_out.setText(_out_display(s2.get("outdir") or legacy_out or "out"))
        self.s2_mode.setCurrentIndex(1 if str(s2.get("merge_mode")) == "velocity" else 0)
        self.s3_melody.setText(str(s3.get("melody") or ""))
        self.s3_chords.setText(str(s3.get("chords") or ""))
        self.s3_out.setText(_out_display(s3.get("outdir") or legacy_out or "out"))
        self.s3_name.setText(str(s3.get("name") or ""))
        self.s3_low.setValue(int(s3.get("low") or 40))
        self.s3_high.setValue(int(s3.get("high") or 88))
        try:
            _ver = int(data.get("version") or 0)
        except Exception:  # noqa: BLE001
            _ver = 0
        mode = ("electric" if _ver < 3
                else str(s3.get("instrument_mode") or "electric"))
        pos = self.s3_instrument.findData(mode)
        self.s3_instrument.setCurrentIndex(pos if pos >= 0 else 0)
        self.chk_vocal.setChecked(bool(s3.get("stem_vocal", True)))
        self.chk_inst.setChecked(bool(s3.get("stem_inst", True)))
        _sep = self.cmb_sep_model.findData(s3.get("sep_model") or "native")
        self.cmb_sep_model.setCurrentIndex(_sep if _sep >= 0 else 0)
        self.cmb_sep_model.setEnabled(self._stems() != "both")
        idx = {"auto": 0, "xpu": 1, "cuda": 2, "amd": 3, "rocm": 3, "cpu": 4}.get(
            str(data.get("device")), 0)
        self.device_combo.setCurrentIndex(idx)
        win = data.get("window") or {}
        if isinstance(win.get("size"), list) and len(win["size"]) == 2:
            try:
                avail = QApplication.desktop().availableGeometry(self)
                w = min(int(win["size"][0]), int(avail.width() * 0.92))
                h = min(int(win["size"][1]), int(avail.height() * 0.88))
                self.resize(max(760, w), max(520, h))
            except Exception:  # noqa: BLE001
                pass

    def _save_settings(self) -> None:
        data = {
            "version": 3,
            "device": self._device_key(),
            "step1": {"audio": self.s1_audio.text().strip(),
                      "outdir": _out_store(self.s1_out.text()),
                      "dtype": self._dtype_key()},
            "step2": {"vocal": self.s2_vocal.text().strip(),
                      "instrumental": self.s2_ins.text().strip(),
                      "chords": self.s2_chords.text().strip(),
                      "integrated": self.s2_integrated.text().strip(),
                      "outdir": _out_store(self.s2_out.text()),
                      "merge_mode": self._merge_key()},
            "step3": {"melody": self.s3_melody.text().strip(),
                      "chords": self.s3_chords.text().strip(),
                      "outdir": _out_store(self.s3_out.text()),
                      "name": self.s3_name.text().strip(),
                      "low": self.s3_low.value(), "high": self.s3_high.value(),
                      "instrument_mode": self.s3_instrument.currentData(),
                      "stem_vocal": self.chk_vocal.isChecked(),
                      "stem_inst": self.chk_inst.isChecked(),
                      "sep_model": self.cmb_sep_model.currentData()},
            "window": {"size": [self.width(), self.height()]},
        }
        try:
            SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            self.log(f"! 设置没保存成功：{exc}")

    def closeEvent(self, event) -> None:  # noqa: N802
        self._prog_timer.stop()
        if self.worker is not None and self.worker.isRunning():
            pipeline.cancel_current()
            self.worker.wait(3000)
        if self.probe is not None and self.probe.isRunning():
            self.probe.wait(20000)
        self._save_settings()
        event.accept()

    # ---------------------------------------------------------------- actions
    def _ensure_ai_or_prompt(self) -> bool:
        """AI 组件缺失时给出安装引导，而不是直接抛异常。"""
        try:
            import pipeline as PL
            ready = PL.ai_components_ready()
        except Exception:
            ready = True
        if ready:
            return True
        answer = QMessageBox.question(
            self, "尚未安装 AI 扒谱模型",
            "当前尚未安装 AI 音频扒谱模型组件（约 2.6 GB）。\n"
            "（注：演奏端导入 MIDI 与钢琴卷帘功能无需安装即可直接使用）\n\n"
            "是否立即打开「环境与显卡体检」进行一键自动安装？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer == QMessageBox.Yes:
            win = self.window()
            if hasattr(win, "show_env_check"):
                win.show_env_check()
        return False

    def _open_temp_sep(self):
        base = str(_resolve_out(self.s1_out.text()) or "out")
        path = os.path.join(base, "temp_sep")
        if not os.path.isdir(path):
            QMessageBox.information(
                self, "还没有分离音频",
                "先单选「人声主旋律」或「器乐旋律」跑一次扒谱，\n"
                "分离出的人声/器乐 WAV 会放在：\n%s" % path)
            return
        newest = ""
        try:
            wavs = [os.path.join(path, n) for n in os.listdir(path)
                    if n.lower().endswith((".wav", ".flac", ".mp3"))]
            if wavs:
                newest = max(wavs, key=os.path.getmtime)
        except Exception:  # noqa: BLE001
            newest = ""
        try:
            if newest:
                subprocess.Popen(["explorer", "/select,", os.path.normpath(newest)])
            else:
                os.startfile(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "打不开目录", str(exc))

    def _on_stem_toggled(self, _checked=None):
        if hasattr(self, "cmb_sep_model"):
            self.cmb_sep_model.setEnabled(self._stems() != "both")
        if not self.chk_vocal.isChecked() and not self.chk_inst.isChecked():
            other = (self.chk_inst if self.sender() is self.chk_vocal
                     else self.chk_vocal)
            other.blockSignals(True)
            other.setChecked(True)
            other.blockSignals(False)

    def _stems(self):
        v, i = self.chk_vocal.isChecked(), self.chk_inst.isChecked()
        if v and i:
            return "both"
        return "vocal" if v else "inst"

    def _run_all(self) -> None:
        if not self._ensure_ai_or_prompt():
            return
        audio = self.s1_audio.text().strip()
        out = str(_resolve_out(self.s1_out.text()))
        if not audio or not Path(audio).is_file():
            QMessageBox.information(self, "还差一步", "请先选择存在的原始音频文件。")
            return
        if not out:
            QMessageBox.information(self, "还差一步", "请先填输出目录。")
            return
        self.s2_out.setText(self.s1_out.text().strip() or "out")
        self.s3_out.setText(self.s1_out.text().strip() or "out")
        self._fill_step2()
        self._fill_step3()
        low, high = self.s3_low.value(), self.s3_high.value()
        if low >= high:
            QMessageBox.information(self, "音域不对", "最低音必须小于最高音。")
            return

        self.results = {1: None, 2: None, 3: None}
        for label in (self.s1_status, self.s2_status, self.s3_status):
            label.setText("运行中…")

        stems = self._stems()
        sep = self.cmb_sep_model.currentData() or "native"
        if stems == "both":
            sep = "native"
        will_sep = sep in ("htdemucs", "roformer")

        def job():
            return pipeline.run_all(audio, out, device=self._device_key(),
                                    dtype=self._dtype_key(), merge_mode=self._merge_key(),
                                    low=low, high=high,
                                    instrument_mode=self.s3_instrument.currentData(),
                                    stems=stems, sep_model=sep,
                                    final_name=self.s3_name.text().strip() or None)

        def done(summary):
            steps = summary.get("steps") or []
            for idx, result in enumerate(steps[:3], start=1):
                self.results[idx] = result
            self._update_status(1, steps[0] if len(steps) > 0 else {})
            self._update_status(2, steps[1] if len(steps) > 1 else {})
            self._update_status(3, steps[2] if len(steps) > 2 else {})
            self.log(f"最终成品：{summary.get('final_midi')}")
            self._fill_step2()
            self._fill_step3()
            final = summary.get("final_midi")
            if final:
                self.sig_all_done.emit(str(final))

        self._run(job, done, "一键全流程", step_idx=0, sep_active=will_sep)

    def _run_step1(self) -> None:
        if not self._ensure_ai_or_prompt():
            return
        audio = self.s1_audio.text().strip()
        out = str(_resolve_out(self.s1_out.text()))
        if not audio or not Path(audio).is_file():
            QMessageBox.information(self, "还差一步", "请先选择存在的原始音频文件。")
            return
        if not out:
            QMessageBox.information(self, "还差一步", "请先填输出目录。")
            return
        self.s1_status.setText("运行中…")

        def job():
            return pipeline.step1(audio, out, device=self._device_key(),
                                  dtype=self._dtype_key())

        def done(result):
            self.results[1] = result
            self._update_status(1, result)
            self._fill_step2()

        self._run(job, done, "步骤 ① 转录", step_idx=1)

    def _run_step2(self) -> None:
        out = str(_resolve_out(self.s2_out.text().strip() or self.s1_out.text()))
        if not out:
            QMessageBox.information(self, "还差一步", "请先填输出目录。")
            return
        self.s2_status.setText("运行中…")
        vocal = self.s2_vocal.text().strip() or None
        ins = self.s2_ins.text().strip() or None
        chords = self.s2_chords.text().strip() or None
        integrated = self.s2_integrated.text().strip() or None

        def job():
            return pipeline.step2(out, vocal=vocal, instrumental=ins,
                                  stems=self._stems(),
                                  chords=chords, integrated=integrated,
                                  merge_mode=self._merge_key())

        def done(result):
            self.results[2] = result
            self._update_status(2, result)
            self._fill_step3()

        self._run(job, done, "步骤 ② 合并", step_idx=2)

    def _run_step3(self) -> None:
        out = str(_resolve_out(self.s3_out.text().strip() or self.s2_out.text()))
        if not out:
            QMessageBox.information(self, "还差一步", "请先填输出目录。")
            return
        low, high = self.s3_low.value(), self.s3_high.value()
        if low >= high:
            QMessageBox.information(self, "音域不对", "最低音必须小于最高音。")
            return
        self.s3_status.setText("运行中…")
        melody = self.s3_melody.text().strip() or None
        chords = self.s3_chords.text().strip() or None
        name = self.s3_name.text().strip() or None
        audio = self.s1_audio.text().strip() or None

        def job():
            return pipeline.step3(melody, out, chords=chords, low=low, high=high,
                                  instrument_mode=self.s3_instrument.currentData(),
                                  final_name=name, source_audio=audio)

        def done(result):
            self.results[3] = result
            self._update_status(3, result)
            self.log(f"成品：{result['outputs']['final_midi']}")

        self._run(job, done, "步骤 ③ 导出", step_idx=3)

    def _update_status(self, step: int, result: dict) -> None:
        label = {1: self.s1_status, 2: self.s2_status, 3: self.s3_status}[step]
        label.setObjectName("statusBadge")
        counts = (result or {}).get("counts") or {}
        if step == 1:
            text = ("✔ 已完成 · 人声 %d 音 / 器乐 %d 音 / 和弦 %d 组"
                    % (counts.get('melody_vocal', 0),
                       counts.get('melody_instrumental', 0),
                       counts.get('chord_slices', counts.get('chords', 0))))
            label.setToolTip("推理设备：%s" % (result.get('device_label') or result.get('device') or '-'))
        elif step == 2:
            text = ("✔ 已完成 · 主旋律 %d 音 / 和弦 %d 组"
                    % (counts.get('merged', 0), counts.get('chord_slices', 0)))
        else:
            rng = result.get("pitch_range") or [None, None]
            text = ("✔ 已完成 · 音域 %s~%s（共 %d 音）"
                    % (rng[0], rng[1], counts.get('main_melody', 0)))
        label.setText(text)

    def open_piano_roll(self, step: int):
        """从对应步骤打开钢琴卷帘（模式 A：通用 MIDI 编辑）。"""
        path = self._step_midi_path(step)
        if not path:
            from PyQt5.QtWidgets import QFileDialog
            start = str(_resolve_out(
                self.s3_out.text() or self.s2_out.text() or self.s1_out.text() or "out"))
            path, _ = QFileDialog.getOpenFileName(self, "选择要微调的 MIDI", start,
                                                  "MIDI (*.mid *.midi)")
        if not path:
            return None
        try:
            pdir = str(Path(__file__).resolve().parent.parent / "player")
            if pdir not in sys.path:
                sys.path.insert(0, pdir)
            from piano_roll import PianoRollDialog
        except Exception as exc:  # noqa: BLE001
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.warning(self, "钢琴卷帘不可用", "载入 player/piano_roll.py 失败：%s" % exc)
            return None
        dlg = PianoRollDialog(path, role=None, parent=self)
        dlg.exec_()
        try:
            saved = getattr(dlg.model, "last_saved", None)
            if saved:
                self._after_roll_saved(step, saved)
        except Exception:
            pass
        return dlg

    def _after_roll_saved(self, step: int, saved_path: str) -> None:
        """卷帘保存覆盖中间/成品 MIDI 后，同步下游步骤与演奏台。"""
        saved = str(saved_path)
        final = None
        # 步骤②改过且步骤③跑过 → 立即重跑一次 step3（约 0.02 秒）同步成品
        if step == 2 and (self.results or {}).get(3):
            try:
                self.log("检测到步骤②MIDI 已更新，自动重跑步骤③同步成品…")
                out = _resolve_out(self.s3_out.text() or self.s2_out.text()
                                   or self.s1_out.text())
                res = pipeline.step3(saved, out,
                                     chords=self.s3_chords.text().strip() or None,
                                     low=self.s3_low.value(), high=self.s3_high.value(),
                                     instrument_mode=self.s3_instrument.currentData(),
                                     final_name=self.s3_name.text().strip() or None,
                                     source_audio=self.s1_audio.text().strip() or None)
                self.results[3] = res
                self._update_status(3, res)
                final = (res.get("outputs") or {}).get("final_midi")
                self.log("成品已同步：%s" % final)
            except Exception as exc:  # noqa: BLE001
                self.log("自动重跑步骤③失败：%s" % exc)
        elif step == 3:
            final = saved
        # 演奏台当前加载的正是这份成品时，自动刷新四角色缓存
        try:
            win = self.window()
            b = getattr(win, "b", None)
            target = final or ((self.results.get(3) or {}).get("outputs")
                               or {}).get("final_midi")
            if target and b is not None and hasattr(win, "send_to_player"):
                cur = os.path.abspath(str(getattr(b, "path", "") or ""))
                if cur and cur == os.path.abspath(str(target)):
                    win.send_to_player(str(target))
                    self.log("演奏台已同步最新修改：%s" % os.path.basename(str(target)))
        except Exception:
            pass

    def _step_midi_path(self, step: int):
        """步骤 1/2/3 的可用 MIDI：先看刚跑完的结果，再退回输出目录里的标准文件名。"""
        r = (self.results or {}).get(step) or {}
        out = r.get("outputs") or {}
        cands = []
        if step == 1:
            cands += [out.get("melody_vocal.mid"), out.get("melody_instrumental.mid")]
            raw = out.get("raw_dir")
            if raw:
                cands += [os.path.join(raw, "melody_vocal.mid"),
                          os.path.join(raw, "melody_instrumental.mid")]
        elif step == 2:
            cands += [out.get("main_melody.mid"),
                      os.path.join(str(_resolve_out(self.s2_out.text())), "main_melody.mid")]
        else:
            cands += [out.get("final_midi")]
        d = _resolve_out(self.s1_out.text() if step == 1 else
                         self.s2_out.text() if step == 2 else
                         self.s3_out.text())
        if d:
            names = (("melody_vocal.mid", "melody_instrumental.mid") if step == 1 else
                     ("main_melody.mid",) if step == 2 else ("final.mid",))
            cands += [os.path.join(str(d), n) for n in names]
        for c in cands:
            if c and os.path.isfile(str(c)):
                return str(c)
        return None

    def _fill_step2(self) -> None:
        out = _resolve_out(self.s1_out.text().strip() or self.s2_out.text() or "out")
        if not self.s2_out.text().strip():
            self.s2_out.setText(str(out))
        for edit, name in ((self.s2_vocal, "melody_vocal.mid"),
                           (self.s2_ins, "melody_instrumental.mid"),
                           (self.s2_chords, "chords.mid"),
                           (self.s2_integrated, "transcription.mid")):
            if (out / name).is_file():
                edit.setText(str(out / name))

    def _fill_step3(self) -> None:
        out = _resolve_out(self.s2_out.text().strip() or self.s3_out.text() or "out")
        if not self.s3_out.text().strip():
            self.s3_out.setText(str(out))
        for edit, name in ((self.s3_melody, "main_melody.mid"),
                           (self.s3_chords, "chords_merged.mid")):
            if (out / name).is_file():
                edit.setText(str(out / name))

    def _export(self, step: int) -> None:
        result = self.results.get(step)
        if not result:
            self.log(f"× 步骤 {step} 还没有产物，先跑一次。")
            return
        target = QFileDialog.getExistingDirectory(self, "导出到哪个文件夹", str(HERE))
        if not target:
            return
        outputs = result.get("outputs") or {}
        done = 0
        for value in outputs.values():
            path = Path(str(value))
            if path.is_file() and path.suffix.lower() in (".mid", ".midi", ".json"):
                try:
                    shutil.copy2(path, Path(target) / path.name)
                    done += 1
                except Exception as exc:  # noqa: BLE001
                    self.log(f"× 导出 {path.name} 失败：{exc}")
        self.log(f"√ 已导出 {done} 个文件到 {target}")

    def _open_out_dir(self) -> None:
        path = _resolve_out(self.s1_out.text() or "out")
        try:
            path.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(path))  # noqa: S606
            else:
                self.log(f"输出目录：{path}")
        except Exception as exc:  # noqa: BLE001
            self.log(f"× 打不开目录：{exc}")


def main() -> int:
    app = QApplication(sys.argv)
    try:
        from gui_theme import apply_theme
        apply_theme(app)
    except Exception:
        pass
    app.setFont(QFont("Microsoft YaHei UI", 9))
    win = MainWindow()
    if "--smoke" in sys.argv:
        # 只做无界面自检：窗口构造成功即可，不进入事件循环
        if win.probe is not None:
            win.probe.wait(20000)
        app.processEvents()
        print(f"GUI_SMOKE_OK title={win.windowTitle()} cards=3 "
              f"device={win.device_state.text()}")
        return 0
    win.show()
    return app.exec_()


if __name__ == "__main__":
    if sys.stdout is None or sys.stderr is None:
        try:
            _lg = Path(__file__).resolve().parent.parent / "logs"
            _lg.mkdir(exist_ok=True)
            _f = open(_lg / "gui_a_stdout.log", "a", encoding="utf-8", buffering=1)
            if sys.stdout is None:
                sys.stdout = _f
            if sys.stderr is None:
                sys.stderr = _f
        except Exception:
            pass
    raise SystemExit(main())
