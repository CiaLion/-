# -*- coding: utf-8 -*-
"""gui_b.py -- 《异键演奏》界面（MIDI 编译器 + 高精度播放器）。

界面功能：
  ① 选歌         选一份 .mid（测试曲默认从项目里的 test_midis\\ 取）
  ② 选角色       单人独奏 / 双人 1P 主音（品格）/ 双人 2P 伴奏（和弦）
  ③ 照着摆游戏   大字提示"按 Z 切到哪个模式、按 T 翻到第几页"
  ④ 开始演奏     F8 开始/停止、F12 紧急停；可勾"整 5 秒同步启动"
另有可折叠的「高级模式」区：按 player_config.TIMING_INFO
自动列出全部时间参数，可保存到 player_config.json 或一键恢复默认。
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Qt 平台插件装在含中文的项目路径下时，Qt 自身解析会失败；
# 显式用环境变量指向本 venv 的插件目录（必须在 import PyQt5 之前设置）。
_QT_PLUGINS = os.path.join(sys.prefix, "Lib", "site-packages", "PyQt5", "Qt5", "plugins")
if os.path.isdir(_QT_PLUGINS) and not os.environ.get("QT_PLUGIN_PATH"):
    os.environ["QT_PLUGIN_PATH"] = _QT_PLUGINS

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox,
                             QDialog, QDoubleSpinBox, QFileDialog, QFrame,
                             QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                             QMainWindow, QMessageBox, QPlainTextEdit,
                             QProgressBar, QPushButton, QRadioButton, QScrollArea,
                             QVBoxLayout, QWidget)

import input_engine as ie
import midi_compiler as MC
import player_config as PC
import recorder as REC
import timeline_runner as TR
from game_io import GameIO

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
TEST_MIDI_DIR = os.path.join(PROJECT_DIR, "assets", "test_midis")

ROLE_ORDER = (MC.ROLE_SOLO, MC.ROLE_DUET_LEAD, MC.ROLE_DUET_RHYTHM,
              MC.ROLE_DUET_RHYTHM_FRET)
ROLE_TITLE = {
    MC.ROLE_SOLO: "单人独奏 (Solo)",
    MC.ROLE_DUET_LEAD: "双人合奏 · 1P 主音",
    MC.ROLE_DUET_RHYTHM: "双人合奏 · 2P 和弦",
    MC.ROLE_DUET_RHYTHM_FRET: "双人合奏 · 2P 品格",
}
ROLE_TIP = {
    MC.ROLE_SOLO: "旋律与伴奏智能合流",
    MC.ROLE_DUET_LEAD: "品格模式：纯主旋律",
    MC.ROLE_DUET_RHYTHM: "和弦模式：24 和弦右键下扫",
    MC.ROLE_DUET_RHYTHM_FRET: "品格模式：和弦内音顺拨琶音",
}

# 18 项参数全部真实生效，不再有 25/30/35ms 的界面下限。
def _timing_dep_text(key):
    """每一项"受谁影响 / 是否独立"的一句话说明（参数面板第 4 列）。"""
    return {
        "min_action_interval": "全局基准：进入换格/翻页预算",
        "fret_click_hold": "点格按住时长",
        "pluck_hold": "数字键按住时长",
        "sustain_max": "游戏侧约 4.05s 到顶",
        "select_click_hold": "和弦选格按住时长",
        "strum_park_wait": "扫弦前挪开指针后等待",
        "strum_click_hold": "扫弦按住时长",
        "strum_hold": "0 = 点一下；>0 = 按住延音",
        "page_key_hold": "翻页键按住时长",
        "page_turn_gap": "进入翻页预算",
        "after_page_key_release": "翻页键松开后根音生效延迟",
        "settle_after_page_turn": "直接生效",
        "settle_same_page": "直接生效",
        "settle_page_turn": "直接生效",
        "sustain_min": "判断门槛，不是等待",
        "repluck_gap": "同弦重拨最小间隔",
        "start_delay": "只加在时间轴开头",
        "preschedule_lead": "额外提前量",
    }.get(key, "直接生效")

# ══════════════════════════════════════════════════════════════════════════
#  配色表（只在这里改颜色）
# ══════════════════════════════════════════════════════════════════════════
C_BG = "#13151a"          # 窗口底色
C_CARD = "#1b1f27"        # 卡片
C_CARD2 = "#12141a"       # 卡片里的深一档
C_LINE = "#2b313d"        # 描边
C_TEXT = "#e6ebf2"        # 主文字
C_TEXT2 = "#9aa4b2"       # 次要文字
C_ACCENT = "#e84393"      # 强调（粉）
C_GOLD = "#ffd166"        # 指引（金）
C_LOG_BG = "#0e1014"      # 日志底色
C_LOG_TEXT = "#c3ccd9"    # 日志文字

QSS = """
QWidget { background-color: %(bg)s; color: %(text)s;
          font-family: "Microsoft YaHei"; font-size: 13px; }

QLabel { background: transparent; color: %(text)s; }
QLabel#title { font-size: 20px; font-weight: bold; color: #ffffff; }
QLabel#sub   { font-size: 12px; color: %(text2)s; }
QLabel#section { font-size: 12px; font-weight: bold; color: %(text2)s; }

QFrame#card  { background-color: %(card)s; border: 1px solid %(line)s;
               border-radius: 10px; }
QFrame#guide { background-color: #16211a; border: 1px solid #2c4a35;
               border-radius: 10px; }
QLabel#guide_text { font-size: 15px; color: %(gold)s; font-weight: bold;
                    line-height: 160%%; }

QPushButton { background-color: #262b36; border: 1px solid #3a4150;
              border-radius: 7px; color: %(text)s; padding: 7px 16px; }
QPushButton:hover { background-color: #303745; border-color: #4d566a; }
QPushButton:pressed { background-color: #1e222b; }
QPushButton:disabled { background-color: #1a1d24; color: #5a6273;
                       border-color: #262b34; }

QPushButton#play {
    background-color: #c22f75; border: 1px solid #ef6ba6; color: #ffffff;
    font-size: 15px; font-weight: bold; padding: 9px 22px; border-radius: 8px;
}
QPushButton#play:hover { background-color: #d84289; }
QPushButton#play:checked { background-color: #3a2530; border-color: #a33a63;
                           color: #ff9cc4; }

QRadioButton, QCheckBox { background: transparent; color: %(text)s;
                          spacing: 8px; padding: 3px 0px; }
QRadioButton::indicator, QCheckBox::indicator { width: 15px; height: 15px; }
QRadioButton::indicator:unchecked, QCheckBox::indicator:unchecked {
    border: 1px solid #5a6273; background: #0f1116; border-radius: 8px; }
QRadioButton::indicator:checked, QCheckBox::indicator:checked {
    border: 1px solid %(accent)s; background: %(accent)s; border-radius: 8px; }
QCheckBox::indicator:unchecked { border-radius: 3px; }
QCheckBox::indicator:checked { border-radius: 3px; }

QPlainTextEdit#log {
    background-color: %(logbg)s; color: %(logtext)s;
    border: 1px solid %(line)s; border-radius: 8px;
    font-family: "Consolas", "Microsoft YaHei", monospace; font-size: 12px;
    padding: 8px; selection-background-color: %(accent)s;
    selection-color: #ffffff;
}

QDoubleSpinBox { background-color: #1a1e26; border: 1px solid #3a4150;
                 border-radius: 6px; color: #ffffff; padding: 4px 8px;
                 min-height: 18px; }
QDoubleSpinBox::up-button {
    subcontrol-origin: border; subcontrol-position: top right;
    width: 16px; background-color: #2a303c;
    border-left: 1px solid #3a4150; border-top-right-radius: 6px; }
QDoubleSpinBox::down-button {
    subcontrol-origin: border; subcontrol-position: bottom right;
    width: 16px; background-color: #2a303c;
    border-left: 1px solid #3a4150; border-bottom-right-radius: 6px; }
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {
    background-color: #363d4b; }
QDoubleSpinBox::up-arrow {
    width: 0px; height: 0px;
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-bottom: 5px solid #c3ccd9; }
QDoubleSpinBox::down-arrow {
    width: 0px; height: 0px;
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid #c3ccd9; }

QComboBox { background-color: #1a1e26; border: 1px solid #3a4150;
            border-radius: 6px; color: #ffffff; padding: 4px 8px; }
QComboBox:hover { border-color: #4d566a; }
QComboBox QAbstractItemView { background-color: #1b1f27; color: #e6ebf2;
                              border: 1px solid #2b313d;
                              selection-background-color: #e84393; }

QProgressBar { background-color: #14161c; border: 1px solid %(line)s;
               border-radius: 6px; height: 12px; text-align: center;
               color: transparent; }
QProgressBar::chunk { background-color: %(accent)s; border-radius: 5px; }

QScrollBar:vertical { background: #14161c; width: 10px; border-radius: 5px; }
QScrollBar::handle:vertical { background: #39404e; min-height: 24px;
                              border-radius: 5px; }
QScrollBar::handle:vertical:hover { background: #4b5468; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0px; }

QGroupBox { border: 1px solid %(line)s; border-radius: 10px;
            margin-top: 10px; padding: 10px 12px 8px 12px;
            color: %(text2)s; font-weight: bold; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px;
                   color: %(text2)s; }
QScrollArea { border: none; background: transparent; }
QWidget#adv_inner { background: transparent; }

QToolTip { background-color: #1b1f27; color: %(text)s;
           border: 1px solid %(line)s; }
""" % {"bg": C_BG, "card": C_CARD, "card2": C_CARD2, "line": C_LINE,
       "text": C_TEXT, "text2": C_TEXT2, "accent": C_ACCENT, "gold": C_GOLD,
       "logbg": C_LOG_BG, "logtext": C_LOG_TEXT}

class CompileWorker(threading.Thread):
    """后台编译（一首歌通常 0.2~1 秒，放后台是为了界面不卡）。"""

    def __init__(self, path, role, done, fail, enable_arpeggio=True,
                 arpeggio_pattern="pingpong", enable_chord_rhythm=True):
        super(CompileWorker, self).__init__(daemon=True)
        self.path, self.role = path, role
        self.done, self.fail = done, fail
        self.enable_arpeggio = enable_arpeggio
        self.arpeggio_pattern = arpeggio_pattern
        self.enable_chord_rhythm = enable_chord_rhythm

    def run(self):
        try:
            # 任务 1：本曲没有和弦轨时，2P 角色直接返回空事件，不偷别的曲子、
            # 也不让 compile_file 抛异常把界面打断。
            if self.role in (MC.ROLE_DUET_RHYTHM, MC.ROLE_DUET_RHYTHM_FRET):
                meta = MC.read_meta(self.path)
                has_chords = any(MC.midi_adapter.is_chords_track(n)
                                 for n in (meta.get("track_names") or []))
                if not has_chords:
                    self.done(self._empty_timeline())
                    return
            tl = MC.compile_file(self.path, role=self.role,
                                 enable_arpeggio=self.enable_arpeggio,
                                 arpeggio_pattern=self.arpeggio_pattern,
                                 enable_chord_rhythm=self.enable_chord_rhythm)
            self.done(tl)
        except Exception as exc:
            self.fail(exc)

    def _empty_timeline(self):
        """没有和弦轨时的空时间轴：0 事件 + no_chord_track 标记。"""
        return {
            "version": 1, "role": self.role,
            "role_cn": MC.ROLE_CN.get(self.role, self.role),
            "kind": "chord", "source": os.path.abspath(self.path),
            "start": {"page": 1, "sector": 1, "mode_cn": "和弦", "wheel": ""},
            "stats": {"frames": 0, "notes": 0, "voices": 0, "events": 0,
                      "turns": 0, "clicks": 0, "plucks": 0, "strums": 0,
                      "switches": 0, "late": 0, "max_late": 0.0,
                      "duration": 0.0, "hw_events": 0, "no_chord_track": True},
            "compile_stats": {}, "hw_stats": {"events": 0, "turns": 0, "late": 0,
                                              "max_late": 0.0, "hw_events": 0,
                                              "duration": 0.0},
            "adapter": {"no_chord_track": True,
                        "chords": {"tracks": 0, "no_chord_track": True}},
            "timing": {}, "events": [],
        }

class PlayWorker(threading.Thread):
    """后台演奏（可选：同时在后台录下系统输出的声音）。"""

    def __init__(self, timeline, speed, sync, log, progress, done, rec=None):
        super(PlayWorker, self).__init__(daemon=True)
        self.timeline, self.speed, self.sync = timeline, speed, sync
        self.log, self.progress, self.done = log, progress, done
        self.rec = rec
        self.stop_requested = False
        self.paused = False
        self.runner = None

    def run(self):
        io = GameIO()
        self.runner = TR.TimelineRunner(io, log=self.log,
                                        on_progress=lambda a, b, i: self.progress(a, b),
                                        on_focus_lost=self._focus_lost,
                                        on_pause_state_changed=self._pause_state_changed)
        # 录音与演奏**同时开始**；录不到也绝不影响演奏
        if self.rec is not None:
            try:
                self.rec.start()
            except Exception as exc:
                self.log("! 录音没能开始：%s（演奏照常）" % exc)
                self.rec = None
        stats = {}
        try:
            stats = self.runner.run(
                self.timeline, speed=self.speed, sync_start=self.sync,
                stop_flag=lambda: self.stop_requested,
                pause_flag=lambda: self.paused)
        finally:
            if self.rec is not None:
                # ★ 手动中断（F8 再按一次 / F12）→ 录音作废；正常弹完 → 保留
                path = self.rec.stop(keep=not self.stop_requested)
                if path:
                    self.log("录音已保存：%s（%.1f 秒）" % (path, self.rec.recorded))
                elif self.stop_requested:
                    self.log("演奏被手动中断 → 本次录音已作废删除")
        self.done(stats)

    def pause(self, reason="手动暂停"):
        self.paused = True
        # 录音暂停统一由 runner 的 on_pause_state_changed 回调处理
        if self.runner is not None:
            try:
                self.runner.pause(reason)
            except Exception:
                pass
        elif self.rec is not None:          # 演奏线程还没起来时的兜底
            try:
                self.rec.pause()
            except Exception:
                pass

    def _pause_state_changed(self, paused, reason):
        """runner 的暂停/恢复广播：任何原因（F8/失焦/Alt/Win）都同步录音器。"""
        if self.rec is None:
            return
        try:
            if paused:
                self.rec.pause()
            else:
                self.rec.resume()
        except Exception:
            pass

    def _focus_lost(self):
        self.paused = True

    def resume(self):
        """F8 继续：必须先切回游戏；不在游戏里就拒绝，绝不抢焦点。"""
        if self.runner is not None and not self.runner.resume():
            self.log("! 请先点击切回游戏窗口后再按 F8 继续（程序不会抢焦点）")
            return False
        self.paused = False
        return True

    def stop(self):
        self.stop_requested = True

class TimingConfigDialog(QDialog):
    """18 项底层时间参数的独立配置弹窗（920×680）。

    顶部提供 UE5 30/60/90 帧三档预设；参数表与实时换格预算在弹窗内，
    切换预设后立即写盘、重编主界面并刷新已打开的钢琴卷帘。
    """

    def __init__(self, owner, parent=None):
        super(TimingConfigDialog, self).__init__(parent or owner)
        self.owner = owner
        self.setObjectName("timingDialog")
        self.setWindowTitle("高级时间参数配置")
        self.setModal(False)
        self.resize(920, 680)
        self.setMinimumSize(820, 560)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        preset_row = QHBoxLayout()
        preset_row.setSpacing(8)
        for key, text, tip in (
                ("30fps", "30 帧稳健预设", "单帧 33.3ms，防低帧率吞键/吞翻页"),
                ("60fps", "60 帧标准预设（默认推荐）", "单帧 16.7ms，默认均衡档"),
                ("90fps", "90 帧极速预设", "单帧 11.1ms，适合高刷机器")):
            btn = QPushButton(text)
            btn.setToolTip(tip)
            btn.setMinimumHeight(34)
            btn.clicked.connect(lambda _=False, k=key: owner.apply_timing_preset(k))
            preset_row.addWidget(btn)
        preset_row.addStretch(1)
        lay.addLayout(preset_row)

        self.adv_box = owner.adv_box
        lay.addWidget(self.adv_box, 1)
        self.adv_box.setVisible(True)

        # 底部只保留「保存当前修改」+「关闭」，预设按钮统一放在顶部
        foot = QHBoxLayout()
        foot.setSpacing(8)
        foot.addStretch(1)
        self.btn_save = QPushButton("保存当前修改")
        self.btn_save.setObjectName("primary")
        self.btn_save.setMinimumHeight(34)
        self.btn_save.setToolTip("把表格里的参数写盘，并立即重编主界面、刷新已打开的钢琴卷帘")
        self.btn_save.clicked.connect(owner.save_timing)
        foot.addWidget(self.btn_save)
        self.btn_close = QPushButton("关闭")
        self.btn_close.setMinimumHeight(34)
        self.btn_close.clicked.connect(self.close)
        foot.addWidget(self.btn_close)
        lay.addLayout(foot)

    def closeEvent(self, ev):
        self.adv_box.setVisible(False)
        super(TimingConfigDialog, self).closeEvent(ev)


class MainWindow(QMainWindow):
    sig_compiled = pyqtSignal(object)
    sig_failed = pyqtSignal(object)
    sig_log = pyqtSignal(str)
    sig_progress = pyqtSignal(float, float)
    sig_finished = pyqtSignal(object)
    sig_devices = pyqtSignal(object)      # 声卡设备列表（后台线程查好再回来填）

    def __init__(self):
        super(MainWindow, self).__init__()
        self.setWindowTitle("《异键演奏》")
        self.resize(960, 780)
        self.setAcceptDrops(True)
        self.timelines = {}
        self.path = None
        self.custom_role_notes = {}
        self.player = None
        self.io = GameIO()
        self.role = MC.ROLE_SOLO
        self._hot_last = {}
        self.timing_spins = {}
        self._timing_dlg = None

        self.sig_compiled.connect(self._on_compiled)
        self.sig_failed.connect(self._on_failed)
        self.sig_log.connect(self._append_log)
        self.sig_progress.connect(self._on_progress)
        self.sig_finished.connect(self._on_finished)
        self.sig_devices.connect(self._on_devices)

        self._build_ui()
        self._update_switch_hints(self.role)
        self._hotkeys()

    def _build_ui(self):
        """紧凑三卡片控制台（曲目/角色 → 准备+主控 → 录音/参数/日志）。"""
        root = QWidget()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(8)

        # 卡片 1：曲目加载与四角色编配
        c1 = QFrame()
        c1.setObjectName("card")
        a = QVBoxLayout(c1)
        a.setContentsMargins(12, 10, 12, 10)
        a.setSpacing(6)
        row = QHBoxLayout()
        self.btn_open = QPushButton("打开 MIDI 乐谱…")
        self.btn_open.clicked.connect(self.open_file)
        row.addWidget(self.btn_open)
        self.btn_testmidi = QPushButton("选择测试 MIDI")
        self.btn_testmidi.setToolTip("默认列出项目里的 test_midis\\ 文件夹")
        self.btn_testmidi.clicked.connect(self.open_test_midi)
        row.addWidget(self.btn_testmidi)
        self.lbl_file = QLabel("（还没选歌）")
        self.lbl_file.setObjectName("sub")
        row.addWidget(self.lbl_file, 1)
        self.btn_roll = QPushButton("打开钢琴卷帘微调")
        self.btn_roll.setToolTip("打开当前角色的转译序列；改完可直接重编并更新演奏台")
        self.btn_roll.clicked.connect(self.open_piano_roll)
        row.addWidget(self.btn_roll)
        a.addLayout(row)

        self.radios = {}
        # 四个角色按钮各自放在独立小容器里，Qt 的"同父级自动互斥"会失效，
        # 必须显式放进同一个 QButtonGroup，保证任何时刻只有 1 个角色被选中。
        self.role_group = QButtonGroup(self)
        self.role_group.setExclusive(True)
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)
        for i, r in enumerate(ROLE_ORDER):
            cell = QWidget()
            cv = QVBoxLayout(cell)
            cv.setContentsMargins(0, 0, 0, 0)
            cv.setSpacing(1)
            rb = QRadioButton(ROLE_TITLE[r])
            rb.setObjectName("roleRadio")
            rb.setChecked(r == self.role)
            rb.toggled.connect(lambda on, rr=r: on and self.choose_role(rr))
            self.role_group.addButton(rb, i)
            cv.addWidget(rb)
            desc = QLabel(ROLE_TIP[r])
            desc.setObjectName("roleDesc")
            desc.setContentsMargins(24, 0, 0, 0)
            desc.setCursor(Qt.PointingHandCursor)
            desc.mousePressEvent = lambda _ev, box=rb: box.setChecked(True)
            cv.addWidget(desc)
            grid.addWidget(cell, i // 2, i % 2)
            self.radios[r] = rb
        a.addLayout(grid)

        row_arp = QHBoxLayout()
        self.chk_arpeggio = QCheckBox("开启和弦内音琶音")
        self.chk_arpeggio.setChecked(bool(PC.load().get("enable_arpeggio", True)))
        self.chk_arpeggio.setToolTip("2P 品格从低到高顺拨 / Solo 长空隙点缀；关闭则只垫根音")
        self.chk_arpeggio.toggled.connect(self._on_arpeggio_toggled)
        row_arp.addWidget(self.chk_arpeggio)
        self.chk_chord_rhythm = QCheckBox("开启和弦扫弦律动")
        self.chk_chord_rhythm.setChecked(bool(PC.load().get("enable_chord_rhythm", True)))
        self.chk_chord_rhythm.setToolTip("开：按真实单拍扫弦；关：每切片只扫 1 次")
        self.chk_chord_rhythm.toggled.connect(self._on_chord_rhythm_toggled)
        row_arp.addWidget(self.chk_chord_rhythm)
        self.cmb_pattern = QComboBox()
        self.cmb_pattern.addItem("固定从低到高（新版，无可选走向）", "ascending")
        self.cmb_pattern.setVisible(False)
        row_arp.addWidget(self.cmb_pattern)
        row_arp.addStretch(1)
        a.addLayout(row_arp)
        v.addWidget(c1)

        # 卡片 2：游戏内三步准备 + 演奏主控
        c2 = QFrame()
        c2.setObjectName("card")
        b = QVBoxLayout(c2)
        b.setContentsMargins(12, 10, 12, 10)
        b.setSpacing(6)
        b.addWidget(self._section("① 演奏前游戏内准备步骤"))
        self.lbl_guide = QLabel("（选完歌，这里会写出三步）")
        self.lbl_guide.setObjectName("guide_text")
        self.lbl_guide.setWordWrap(True)
        self.lbl_guide.setTextInteractionFlags(Qt.TextSelectableByMouse)
        b.addWidget(self.lbl_guide)
        row2 = QHBoxLayout()
        self.btn_play = QPushButton("▶ 开始演奏（F8）")
        self.btn_play.setObjectName("playBtn")
        self.btn_play.setMinimumHeight(44)
        self.btn_play.setCheckable(True)
        self.btn_play.clicked.connect(self.toggle_play)
        row2.addWidget(self.btn_play)
        spin_lab = QLabel("播放倍速")
        spin_lab.setObjectName("sub")
        row2.addWidget(spin_lab)
        self.spin_speed = QDoubleSpinBox()
        self.spin_speed.setRange(0.5, 1.5)
        self.spin_speed.setSingleStep(0.05)
        self.spin_speed.setValue(1.0)
        self.spin_speed.setFixedWidth(84)
        row2.addWidget(self.spin_speed)
        self.chk_sync = QCheckBox("整 5 秒双人同步")
        row2.addWidget(self.chk_sync)
        row2.addStretch(1)
        self.btn_calibrate = QPushButton("指示圈校准轮盘")
        self.btn_calibrate.clicked.connect(self._auto_calibrate)
        row2.addWidget(self.btn_calibrate)
        self.btn_diag = QPushButton("4 步硬件自检")
        self.btn_diag.clicked.connect(self._open_diag)
        row2.addWidget(self.btn_diag)
        b.addLayout(row2)
        v.addWidget(c2)

        # 卡片 3：内录 / 高级时间参数 / 日志
        c3 = QFrame()
        c3.setObjectName("card")
        c = QVBoxLayout(c3)
        c.setContentsMargins(12, 10, 12, 10)
        c.setSpacing(6)
        row3 = QHBoxLayout()
        self.chk_rec = QCheckBox("同步内录系统声音")
        self.chk_rec.setChecked(False)
        row3.addWidget(self.chk_rec)
        rec_lab = QLabel("声卡：")
        rec_lab.setObjectName("sub")
        row3.addWidget(rec_lab)
        self.cmb_rec = QComboBox()
        self.cmb_rec.addItem("（正在查找声卡…）")
        row3.addWidget(self.cmb_rec, 1)
        c.addLayout(row3)
        self._load_devices()
        self._build_advanced(c)
        self.bar = QProgressBar()
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        c.addWidget(self.bar)
        c.addWidget(self._section("运行日志"))
        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        c.addWidget(self.log, 1)
        v.addWidget(c3, 1)

    def _build_ui_legacy(self):
        root = QWidget()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(10)

        title = QLabel("《异键演奏》· 吉他编译器")
        title.setObjectName("title")
        v.addWidget(title)

        sub = QLabel("选一份 .mid → 选角色 → 照下面的大字把游戏摆好 → 按 F8")
        sub.setObjectName("sub")
        v.addWidget(sub)

        # ① 选歌
        row = QHBoxLayout()
        self.btn_open = QPushButton("打开 MIDI 乐谱文件…")
        self.btn_open.clicked.connect(self.open_file)
        row.addWidget(self.btn_open)
        self.btn_testmidi = QPushButton("选择测试MIDI")
        self.btn_testmidi.setToolTip("默认列出项目里的 test_midis\\ 文件夹；也可以浏览其它文件夹")
        self.btn_testmidi.clicked.connect(self.open_test_midi)
        row.addWidget(self.btn_testmidi)
        self.lbl_file = QLabel("（还没选歌）")
        self.lbl_file.setObjectName("sub")
        row.addWidget(self.lbl_file, 1)
        v.addLayout(row)

        # ② 角色
        box = QFrame()
        box.setObjectName("card")
        bv = QVBoxLayout(box)
        bv.setContentsMargins(14, 12, 14, 12)
        bv.setSpacing(4)
        bv.addWidget(self._section("① 这台电脑负责哪个角色（整首曲子不会中途换）"))
        self.radios = {}
        for r in ROLE_ORDER:
            rb = QRadioButton("%s —— %s" % (MC.ROLE_CN[r], ROLE_TIP[r]))
            rb.setChecked(r == self.role)
            rb.toggled.connect(lambda on, rr=r: on and self.choose_role(rr))
            bv.addWidget(rb)
            self.radios[r] = rb
        bv.addSpacing(6)
        row_arp = QHBoxLayout()
        row_arp.setSpacing(8)
        self.chk_arpeggio = QCheckBox("开启和弦内音琶音（2P 品格从低到高顺拨 / Solo 长空隙点缀）")
        self.chk_arpeggio.setChecked(bool(PC.load().get("enable_arpeggio", True)))
        self.chk_arpeggio.setToolTip("新版固定“从低到高顺拨”：只弹当前和弦构成音，全部落在第 1 页 45~68，不跑调、几乎不翻页；关闭则只垫根音")
        self.chk_arpeggio.toggled.connect(self._on_arpeggio_toggled)
        row_arp.addWidget(self.chk_arpeggio)

        lbl_pat = QLabel("走向：")
        lbl_pat.setObjectName("sub")
        row_arp.addWidget(lbl_pat)

        self.cmb_pattern = QComboBox()
        self.cmb_pattern.addItem("固定从低到高（新版，无可选走向）", "ascending")
        self.cmb_pattern.setEnabled(False)
        row_arp.addWidget(self.cmb_pattern)

        self.chk_chord_rhythm = QCheckBox("开启和弦扫弦律动（2P和弦）")
        self.chk_chord_rhythm.setChecked(bool(PC.load().get("enable_chord_rhythm", True)))
        self.chk_chord_rhythm.setToolTip(
            "开：长和弦切片按真实单拍（b = 网格/拍数）做整拍强弱扫弦；\n"
            "关：每个和弦切片只在起点扫 1 次（保留 0.45s 相邻扫弦节流）")
        self.chk_chord_rhythm.toggled.connect(self._on_chord_rhythm_toggled)
        row_arp.addWidget(self.chk_chord_rhythm)
        row_arp.addStretch(1)
        bv.addLayout(row_arp)
        v.addWidget(box)

        # ③ 摆游戏
        guide = QFrame()
        guide.setObjectName("guide")
        gv = QVBoxLayout(guide)
        gv.setContentsMargins(14, 12, 14, 12)
        gv.addWidget(self._section("② 演奏前游戏内准备步骤"))
        self.lbl_guide = QLabel("（选完歌，这里会写出三步）")
        self.lbl_guide.setObjectName("guide_text")
        self.lbl_guide.setWordWrap(True)
        self.lbl_guide.setTextInteractionFlags(Qt.TextSelectableByMouse)
        gv.addWidget(self.lbl_guide)
        v.addWidget(guide)

        # ④ 播放控制
        row2 = QHBoxLayout()
        self.btn_play = QPushButton("开始演奏（F8）")
        self.btn_play.setObjectName("play")
        self.btn_play.setMinimumHeight(42)
        self.btn_play.setCheckable(True)
        self.btn_play.clicked.connect(self.toggle_play)
        row2.addWidget(self.btn_play)
        self.chk_sync = QCheckBox("整 5 秒同步启动（两台一起按，毫秒级对齐）")
        row2.addWidget(self.chk_sync)
        self.btn_roll = QPushButton("钢琴卷帘微调")
        self.btn_roll.setToolTip("打开当前角色的转译序列；改完可直接重编并更新演奏台")
        self.btn_roll.clicked.connect(self.open_piano_roll)
        row2.addWidget(self.btn_roll)
        row2.addStretch(1)
        speed_lab = QLabel("速度")
        speed_lab.setObjectName("sub")
        row2.addWidget(speed_lab)
        self.spin_speed = QDoubleSpinBox()
        self.spin_speed.setRange(0.5, 1.5)
        self.spin_speed.setSingleStep(0.05)
        self.spin_speed.setValue(1.0)
        self.spin_speed.setFixedWidth(84)
        row2.addWidget(self.spin_speed)
        v.addLayout(row2)

        # ④-1 轮盘自动校准 + 硬件通道诊断
        row_diag = QHBoxLayout()
        self.btn_calibrate = QPushButton("自动校准轮盘坐标")
        self.btn_calibrate.clicked.connect(self._auto_calibrate)
        row_diag.addWidget(self.btn_calibrate)
        self.btn_diag = QPushButton("硬件通道自检")
        self.btn_diag.clicked.connect(self._open_diag)
        row_diag.addWidget(self.btn_diag)
        row_diag.addStretch(1)
        v.addLayout(row_diag)

        # ④-2 录音（临时功能：演奏同时把系统输出的声音录下来）
        row3 = QHBoxLayout()
        self.chk_rec = QCheckBox("录音（演奏同时录下电脑放出来的声音）")
        self.chk_rec.setChecked(False)
        row3.addWidget(self.chk_rec)
        rec_lab = QLabel("录哪个设备")
        rec_lab.setObjectName("sub")
        row3.addWidget(rec_lab)
        self.cmb_rec = QComboBox()
        self.cmb_rec.setMinimumWidth(280)
        self.cmb_rec.addItem("（正在查找声卡…）")
        row3.addWidget(self.cmb_rec, 1)
        v.addLayout(row3)
        # ★ 声卡设备**只能在后台线程里查**：soundcard 会把当前线程的 COM 变成
        #   多线程套间（MTA），而 Qt 的系统文件对话框要单线程套间（STA）。
        self._load_devices()

        # ④-3 高级模式（可折叠，不弹窗）
        self._build_advanced(v)

        self.bar = QProgressBar()
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        v.addWidget(self.bar)

        # ⑤ 日志
        v.addWidget(self._section("③ 运行日志"))
        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        v.addWidget(self.log, 1)

        tip = QLabel("F8 = 开始 / 停止　　F12 = 紧急停　　"
                     "（游戏窗口在最前面时按键才进得去）\n"
                     "提示：本程序以管理员运行，Windows 会禁止从资源管理器拖文件进来——"
                     "请用上面的「打开 MIDI 乐谱文件」或「选择测试MIDI」按钮。")
        tip.setObjectName("sub")
        tip.setWordWrap(True)
        v.addWidget(tip)

    def _section(self, text):
        lab = QLabel(text)
        lab.setObjectName("section")
        return lab

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        for u in ev.mimeData().urls():
            p = u.toLocalFile()
            if p.lower().endswith((".mid", ".midi")):
                self.load(p)
                return

    def open_file(self):
        # ★ 用 **Qt 自己的**文件对话框（不用 Windows 系统对话框）：
        #   系统对话框依赖 COM 单线程套间；而录音库会把线程变成多线程套间，
        #   Qt 自己画的对框不依赖 COM，稳。
        self._append_log("打开文件对话框……")
        dlg = QFileDialog(self, "选一份 MIDI", "", "MIDI 文件 (*.mid *.midi)")
        dlg.setOption(QFileDialog.DontUseNativeDialog, True)
        dlg.setFileMode(QFileDialog.ExistingFile)
        if dlg.exec_():
            files = dlg.selectedFiles()
            if files:
                self.load(files[0])

    def open_test_midi(self):
        """「选择测试MIDI」：对话框从项目里的 test_midis\\ 开始，也可以浏览别处。"""
        try:
            os.makedirs(TEST_MIDI_DIR, exist_ok=True)
        except Exception as exc:
            self._append_log("! 建不了 test_midis 文件夹：%s" % exc)
        self._append_log("选择测试MIDI……（默认目录 %s）" % TEST_MIDI_DIR)
        # ★ 和「打开 MIDI」一样用 Qt 自己的对话框：系统对话框依赖 COM 单线程套间，
        dlg = QFileDialog(self, "选择测试MIDI", TEST_MIDI_DIR, "MIDI 文件 (*.mid)")
        dlg.setOption(QFileDialog.DontUseNativeDialog, True)
        dlg.setFileMode(QFileDialog.ExistingFile)
        dlg.setDirectory(TEST_MIDI_DIR)
        if dlg.exec_():
            files = dlg.selectedFiles()
            if files:
                self.load(files[0])

    def _build_advanced(self, v):
        """「高级时间参数」按钮行；18 项参数表放进独立弹窗 TimingConfigDialog。"""
        row = QHBoxLayout()
        self.btn_adv = QPushButton("⚙ 高级时间参数 (18项)…")
        self.btn_adv.setToolTip("打开独立弹窗逐项调整 18 项底层时间参数；保存后下次编译/演奏生效")
        self.btn_adv.clicked.connect(self._open_timing_dialog)
        row.addWidget(self.btn_adv)
        hint = QLabel("支持 30帧 / 60帧(默认) / 90帧 预设切换与 18 项参数自由微调")
        hint.setObjectName("sub")
        row.addWidget(hint)
        row.addStretch(1)
        v.addLayout(row)

        self.adv_box = QGroupBox("")
        av = QVBoxLayout(self.adv_box)
        av.setContentsMargins(8, 8, 8, 8)
        av.setSpacing(6)

        self._timing_hint_base = (
            "点击上方预设可按游戏帧率一键配置；也可手动微调单项参数，"
            "点击「保存当前修改」后立即生效。")
        self._timing_dirty = False
        self.lbl_timing_hint = QLabel(self._timing_hint_base)
        self.lbl_timing_hint.setObjectName("sub")
        self.lbl_timing_hint.setWordWrap(True)
        av.addWidget(self.lbl_timing_hint)

        inner = QWidget()
        inner.setObjectName("adv_inner")
        grid = QGridLayout(inner)
        grid.setContentsMargins(2, 2, 2, 2)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(5)
        for col, text in enumerate(("参数名称", "当前值", "实际生效",
                                    "允许范围 / 依赖")):
            lab = QLabel(text)
            lab.setObjectName("section")
            grid.addWidget(lab, 0, col)

        current = PC.load_timing()
        self.timing_effects = {}
        for i, (key, cn, dflt, unit, lo, hi, level, note) in enumerate(PC.TIMING_INFO):
            lab = QLabel(cn)
            lab.setToolTip("参数 key：%s\n%s\n%s" % (key, level, note))
            grid.addWidget(lab, i + 1, 0)

            spin = QDoubleSpinBox()
            spin.setDecimals(3)
            spin.setRange(float(lo), float(hi))
            spin.setSingleStep(0.005)
            spin.setSuffix(" " + unit)
            spin.setMinimumWidth(110)
            spin.setToolTip("%s\n允许范围 %s ~ %s %s\n%s"
                            % (cn, lo, hi, unit, note))
            try:
                spin.setValue(float(current.get(key, dflt)))
            except (TypeError, ValueError):
                spin.setValue(float(dflt))
            spin.valueChanged.connect(self._on_timing_spin_changed)
            self.timing_spins[key] = spin
            grid.addWidget(spin, i + 1, 1)

            eff = QLabel("-")
            eff.setObjectName("sub")
            eff.setMinimumWidth(130)
            self.timing_effects[key] = eff
            grid.addWidget(eff, i + 1, 2)

            rng = QLabel("%s ~ %s %s ｜ %s" % (lo, hi, unit, _timing_dep_text(key)))
            rng.setObjectName("sub")
            rng.setWordWrap(True)
            rng.setToolTip(_timing_dep_text(key))
            grid.addWidget(rng, i + 1, 3)
            grid.setRowMinimumHeight(i + 1, 30)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(2, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        scroll.setMinimumHeight(300)
        av.addWidget(scroll, 1)

        self.lbl_timing_budget = QLabel("")
        self.lbl_timing_budget.setObjectName("sub")
        self.lbl_timing_budget.setWordWrap(True)
        av.addWidget(self.lbl_timing_budget)
        self._update_timing_effects()

        self.adv_box.setVisible(False)
        try:
            inner.adjustSize()
        except Exception:
            pass

    def _open_timing_dialog(self):
        """打开独立的高级时间参数弹窗（不挤占主界面日志）。"""
        dlg = self._timing_dlg
        if dlg is None or not isinstance(dlg, QDialog):
            dlg = TimingConfigDialog(self, self)
            self._timing_dlg = dlg
        self.adv_box.setVisible(True)
        dlg.show()
        try:
            dlg.layout().activate()
        except Exception:
            pass
        dlg.raise_()
        dlg.activateWindow()
        return dlg

    def _timing_values(self):
        return dict((k, float(sp.value())) for k, sp in self.timing_spins.items())

    def _on_timing_spin_changed(self, _v=None):
        self._timing_dirty = True
        try:
            self.lbl_timing_hint.setText("● 有未保存修改：点「保存当前修改」后才生效"
                                         "（会自动重新编译）\n" + self._timing_hint_base)
        except Exception:
            pass
        self._update_timing_effects()

    def _update_timing_effects(self):
        """把每项的真实生效值算出来显示（与 midi_compiler.Timing 同口径）。"""
        try:
            vals = self._timing_values()
            t = MC.Timing(vals)
        except Exception:
            return
        for key, lbl in self.timing_effects.items():
            v = float(vals.get(key, 0.0) or 0.0)
            if key == "min_action_interval":
                txt = "全局基准（只进预算）"
            elif key == "strum_hold":
                txt = "点一下（扫弦按住）" if v <= 1e-9 else "%.0f ms" % (v * 1000)
            elif key == "strum_click_hold" and float(vals.get("strum_hold") or 0) > 1e-9:
                txt = "由「扫弦按住延音」决定"
            elif key == "sustain_max":
                txt = "%.1f s" % v
            elif key == "sustain_min":
                txt = "%.0f ms（门槛）" % (v * 1000)
            else:
                txt = "%.0f ms" % (v * 1000)
                if v < 0.014 - 1e-9:
                    txt += "（直接生效，需高帧率）"
            lbl.setText(txt)
        try:
            s0 = t.switch_span(0, False, True)
            s1 = t.switch_span(1, True, True)
            s2 = t.switch_span(2, True, True)
            self.lbl_timing_budget.setText(
                "换格/翻页预算（按当前 18 项实时计算）：\n"
                "同页换格 %.0f ms = 最小间隔 + 点格按住 + 同页等待\n"
                "翻 1 页 %.0f ms = 翻页键按住 + 松开后根音延迟 + 翻页后等待 + 点格按住 + 跨页等待\n"
                "翻 2 页 %.0f ms = 2×翻页按住 + 连按间隔 + 松开后根音延迟 + 翻页后等待 + 点格按住 + 跨页等待"
                % (s0 * 1000, s1 * 1000, s2 * 1000))
        except Exception:
            pass

    def apply_timing_preset(self, name):
        """一键套用 UE5 30/60/90 帧预设：填表 → 写盘 → 重编 → 刷新卷帘。"""
        vals = dict(PC.TIMING_PRESETS.get(str(name)) or {})
        if not vals:
            return False
        for k, sp in self.timing_spins.items():
            if k in vals:
                sp.setValue(float(vals[k]))
        ok = PC.save_timing(vals, merge=False)
        if ok:
            self._timing_dirty = False
            try:
                self.lbl_timing_hint.setText(self._timing_hint_base)
            except Exception:
                pass
            self._append_log("已切换到 %s 预设（%d 项已写盘）" % (name, len(vals)))
            if self.path:
                self._compile()
            self._refresh_active_roll()
        else:
            self._append_log("× 预设写盘失败，请检查文件权限")
        self._update_timing_effects()
        return ok

    def _refresh_active_roll(self):
        dlg = getattr(self, "_active_roll_dlg", None)
        if dlg is not None and getattr(dlg, "isVisible", lambda: False)():
            try:
                dlg.on_timing_updated()
            except Exception as exc:  # noqa: BLE001
                self._append_log("× 卷帘刷新失败：%s" % exc)

    def save_timing(self):
        """把当前界面上的全部时间参数写进 player_config.json 的 timing 段。"""
        vals = self._timing_values()
        ok = PC.save_timing(vals)
        n = len(vals)
        if ok:
            self._timing_dirty = False
            try:
                self.lbl_timing_hint.setText(self._timing_hint_base)
            except Exception:
                pass
            self._append_log("已保存 %d 项参数（立即重编并刷新卷帘）" % n)
            if self.path:
                self._compile()
            self._refresh_active_roll()
        else:
            self._append_log("× 保存失败（player_config.json 没写进去），请检查文件权限")
        self._update_timing_effects()
        return ok

    def reset_timing(self):
        """全部恢复成 TIMING_DEFAULTS，并立刻写盘。"""
        d = PC.timing_defaults()
        ok = PC.save_timing(d, merge=False)
        for k, sp in self.timing_spins.items():
            if k in d:
                sp.setValue(float(d[k]))
        if ok:
            self._timing_dirty = False
            try:
                self.lbl_timing_hint.setText(self._timing_hint_base)
            except Exception:
                pass
            self._append_log("已恢复默认 60 帧预设（%d 项，已写盘）" % len(d))
            if self.path:
                self._compile()
            self._refresh_active_roll()
        else:
            self._append_log("× 恢复默认时写盘失败，请检查文件权限")
        self._update_timing_effects()
        return ok

    def _load_devices(self):
        """在后台线程里查声卡设备，查完再回界面填下拉框。"""
        def work():
            try:
                names = REC.list_speakers()
                dflt = REC.default_speaker_name()
            except Exception:
                names, dflt = [], None
            try:
                self.sig_devices.emit((names, dflt))
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _on_devices(self, payload):
        names, dflt = payload
        try:
            self.cmb_rec.clear()
            self.cmb_rec.addItems(names or ["（没找到输出设备）"])
            if dflt and dflt in (names or []):
                self.cmb_rec.setCurrentText(dflt)
        except Exception:
            pass

    def load(self, path):
        self.path = path
        self.timelines = {}
        self.custom_role_notes = {}
        self.lbl_file.setText(os.path.basename(path))
        self._append_log("载入：%s" % path)
        self._compile()

    def choose_role(self, role):
        self.role = role
        self._update_switch_hints(role)
        if self.path:
            self._compile()

    def _update_switch_hints(self, role):
        """按当前角色提示两个开关是否当前生效。"""
        if role == MC.ROLE_DUET_RHYTHM:
            self.chk_arpeggio.setToolTip("2P 和弦角色不琶音；琶音开关只影响 Solo / 2P 品格")
            self.chk_chord_rhythm.setToolTip("当前生效：2P 和弦按真实单拍扫弦，全为右键下扫")
        elif role == MC.ROLE_DUET_RHYTHM_FRET:
            self.chk_arpeggio.setToolTip("当前生效：2P 品格从低到高顺拨/只垫根音")
            self.chk_chord_rhythm.setToolTip("2P 品格角色不走和弦扫弦，本开关不生效")
        else:
            self.chk_arpeggio.setToolTip("当前生效：Solo 伴奏铺琶音/根音")
            self.chk_chord_rhythm.setToolTip("单人与 1P 主音不走 2P 和弦扫弦，本开关不生效")

    def _on_arpeggio_toggled(self, checked):
        PC.save({"enable_arpeggio": bool(checked)})
        self.timelines = {}
        self._append_log("和弦内音琶音已%s" % ("开启（从低到高顺拨）" if checked else "关闭（只垫根音）"))
        if self.path:
            self._compile()

    def _on_chord_rhythm_toggled(self, checked):
        PC.save({"enable_chord_rhythm": bool(checked)})
        self.timelines = {}
        self._append_log("2P 和弦扫弦律动已%s" % ("开启（按真实单拍强弱扫弦）" if checked
                                                   else "关闭（每个切片只扫 1 次）"))
        if self.path:
            self._compile()

    def _on_pattern_changed(self, idx):
        # 新版固定从低到高顺拨，不再提供多种走向；保留回调兼容旧配置
        PC.save({"arpeggio_pattern": "ascending"})

    def _compile(self):
        if not self.path:
            return
        self.lbl_guide.setText("正在编译……")

        # 回调要包一层 try：用户可能在编译还没跑完时就关掉窗口，
        # 那时 `self` 已经被 Qt 销毁，直接 emit 会在后台线程里抛异常（虽然不影响使用，
        # 但会在日志/控制台留下难看的红字）。包起来就干净了。
        def ok(tl):
            try:
                self.sig_compiled.emit(tl)
            except Exception:
                pass

        def bad(exc):
            try:
                self.sig_failed.emit(exc)
            except Exception:
                pass

        enable_arpeggio = self.chk_arpeggio.isChecked() if hasattr(self, "chk_arpeggio") else True
        arpeggio_pattern = (self.cmb_pattern.currentData() if hasattr(self, "cmb_pattern") else None) or "pingpong"
        enable_chord_rhythm = (self.chk_chord_rhythm.isChecked()
                               if hasattr(self, "chk_chord_rhythm") else True)
        w = CompileWorker(self.path, self.role, ok, bad,
                          enable_arpeggio=enable_arpeggio,
                          arpeggio_pattern=arpeggio_pattern,
                          enable_chord_rhythm=enable_chord_rhythm)
        w.start()

    def _on_compiled(self, tl):
        self.timelines[tl["role"]] = tl
        self._append_log(MC.report_text(tl).replace("\n", " ｜ "))
        self.refresh_guide(tl)

    def refresh_guide(self, tl=None):
        """把"按 Z / 按 T·R / 按 F8"三步提示刷新成当前角色的真实起始页。"""
        tl = tl or self.timelines.get(self.role)
        if not tl:
            self.lbl_guide.setText("（选完歌，这里会写出三步）")
            return
        st = tl.get("start") or {}
        steps = ["① 游戏里按 Z 键，切到【%s模式】" % (st.get("mode_cn") or "品格"),
                 "② 按 T / R 翻到【第 %s 页】（%s）" % (st.get("page") or 1, st.get("wheel") or ""),
                 "③ 切回游戏窗口，按 F8 开始"]
        self.lbl_guide.setText("\n".join(steps))

    def sync_external(self, path, timelines=None):
        """外部（主界面一键送入）编译完成后，把演奏页的路径/缓存/大字提示一次同步好。"""
        self.path = str(path)
        try:
            self.lbl_file.setText(os.path.basename(self.path))
        except Exception:
            pass
        if timelines:
            self.timelines.update(dict(timelines))
        tl = self.timelines.get(self.role) or next(iter(self.timelines.values()), None)
        self.refresh_guide(tl)
        st = (tl or {}).get("stats") or {}
        if st:
            self._append_log("当前角色统计：%d 发声 / %d 翻页 / 晚出 %d（最大 %.0f ms）"
                             % (int(st.get("voices") or 0), int(st.get("turns") or 0),
                                int(st.get("late") or 0),
                                float(st.get("max_late") or 0) * 1000))

    def apply_custom_timeline(self, tl, role_notes=None):
        """卷帘「应用微调并更新演奏台」回调：替换时间轴，并记住各角色微调。"""
        if not tl:
            return False
        if role_notes:
            self.custom_role_notes = {k: [dict(n) for n in v]
                                      for k, v in role_notes.items()}
        self.timelines[tl.get("role") or self.role] = tl
        self._append_log("已应用卷帘微调：%s（%d 条动作，晚出 %d）"
                         % (tl.get("role_cn"), len(tl.get("events") or []),
                            (tl.get("hw_stats") or {}).get("late", 0)))
        self.refresh_guide(tl)
        return True

    def on_roll_flags_changed(self, enable_arpeggio, enable_chord_rhythm):
        """卷帘内切换琶音 / 扫弦律动后，演奏页同步重编译。"""
        try:
            PC.save({"enable_arpeggio": bool(enable_arpeggio),
                     "enable_chord_rhythm": bool(enable_chord_rhythm)})
        except Exception:
            pass
        self.timelines = {}
        self._append_log("卷帘内切换：琶音=%s，扫弦律动=%s"
                         % ("开" if enable_arpeggio else "关",
                            "开" if enable_chord_rhythm else "关"))
        if self.path:
            self._compile()

    def open_piano_roll(self):
        """打开钢琴卷帘：演奏页直接进当前角色的转译序列模式。"""
        if not self.path:
            QMessageBox.information(self, "还没选歌", "先选一份 MIDI，再打开钢琴卷帘。")
            return None
        try:
            from piano_roll import PianoRollDialog
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "钢琴卷帘不可用", "载入 player/piano_roll.py 失败：%s" % exc)
            return None
        dlg = PianoRollDialog(self.path, role=self.role, parent=self,
                              on_apply=self.apply_custom_timeline)
        # 上一次「应用微调」的结果要延续，否则重开卷帘会退回磁盘原文件
        try:
            if self.custom_role_notes:
                dlg.model.role_notes = {k: [dict(n) for n in v]
                                        for k, v in self.custom_role_notes.items()}
                if self.role in dlg.model.role_notes:
                    dlg.model.notes = [dict(n) for n in dlg.model.role_notes[self.role]]
                    dlg.model.compile()
                    dlg._fill_tracks()
                    dlg._refresh_buttons()
                    dlg.canvas.viewport().update()
        except Exception:
            pass
        self._active_roll_dlg = dlg
        dlg.setModal(False)
        dlg.finished.connect(lambda _r: setattr(self, "_active_roll_dlg", None))
        dlg.show()
        dlg.raise_()
        return dlg

    def _on_failed(self, exc):
        self.lbl_guide.setText("编译失败：%s" % exc)
        self._append_log("× 编译失败：%s" % exc)

    def _auto_calibrate(self):
        try:
            from gui_main import WheelOverlayDialog
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "校准不可用", "请从《异键演奏》主界面打开校准：%s" % exc)
            return
        dlg = WheelOverlayDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            cfg = dlg.compute_ratios()
            self._append_log("√ 指示圈已锁定：x=%.4f y=%.4f r=%.4f"
                             % (cfg["wheel_x_ratio"], cfg["wheel_y_ratio"],
                                cfg["wheel_radius_ratio"]))
            QMessageBox.information(self, "轮盘校准完成",
                                    "x=%.4f  y=%.4f  r=%.4f\n已写入 player_config.json"
                                    % (cfg["wheel_x_ratio"], cfg["wheel_y_ratio"],
                                       cfg["wheel_radius_ratio"]))

    def _open_diag(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("硬件通道自检（点击后 2 秒内切回游戏）")
        lay = QVBoxLayout(dlg)
        tip = QLabel("点任意一项后，2 秒倒计时结束会在游戏里执行。请先进入吉他界面。")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        status = QLabel("就绪")
        for n, text in (
                (1, "测试 1：拨弦通道（VK 数字键 1~6）"),
                (2, "测试 2：轮盘定位（鼠标移到 6 个扇区）"),
                (3, "测试 3：选格 + 拨弦链"),
                (4, "测试 4：翻页通道（T/R 扫描码）")):
            btn = QPushButton(text)
            btn.clicked.connect(lambda _=False, nn=n: self._diag_countdown(nn, status))
            lay.addWidget(btn)
        lay.addWidget(status)
        self._diag_dlg = dlg
        dlg.show()

    def _diag_countdown(self, n, status):
        status.setText("2 秒后执行……请切回游戏")
        QTimer.singleShot(2000, lambda: self._diag_exec(n, status))

    def _diag_exec(self, n, status):
        status.setText("执行中……")
        self.sig_log.emit("硬件自检 %d 开始" % n)

        def job():
            io = GameIO()
            try:
                if not io.has_window():
                    self.sig_log.emit("! 没找到游戏窗口")
                    return
                if n == 1:
                    for s in "123456":
                        io.pluck_key(s)
                        time.sleep(0.25)
                elif n == 2:
                    m = io.metrics()
                    for sector in range(1, 7):
                        io.move(*io.sector_point(sector, m))
                        time.sleep(0.30)
                    io.move(*io.park_point(m))
                elif n == 3:
                    m = io.metrics()
                    x, y = io.sector_point(1, m)
                    io.move(x, y)
                    io.click("left", hold=0.025)
                    time.sleep(0.02)
                    io.move(*io.park_point(m))
                    io.pluck_key("6")
                elif n == 4:
                    m = io.metrics()
                    io.tap("T", hold=0.025)
                    time.sleep(0.035)
                    x, y = io.sector_point(2, m)
                    io.move(x, y)
                    io.click("left", hold=0.025)
                    io.pluck_key("6")
                    time.sleep(0.05)
                    io.tap("R", hold=0.025)
                self.sig_log.emit("√ 硬件自检 %d 完成（所有按键已释放）" % n)
            except Exception as exc:  # noqa: BLE001
                self.sig_log.emit("! 硬件自检 %d 失败：%s" % (n, exc))
            finally:
                try:
                    for btn in ("left", "right"):
                        io.click_up(btn)
                except Exception:
                    pass

        threading.Thread(target=job, daemon=True).start()

    def toggle_play(self):
        if self.player and self.player.is_alive():
            if self.player.paused:
                if self.player.resume():
                    self._append_log("▶ 已继续演奏")
                    self.btn_play.setText("暂停（F8）")
                return
            self.player.pause()
            self._append_log("⏸ 已暂停（F12 紧急停止）")
            self.btn_play.setText("继续（F8）")
            return
        tl = self.timelines.get(self.role)
        if not tl:
            self.btn_play.setChecked(False)
            QMessageBox.information(self, "还没准备好", "先选一份 MIDI，等它编译完。")
            return
        if not self.io.has_window():
            self.btn_play.setChecked(False)
            QMessageBox.warning(self, "没找到游戏", "先把《异环》打开，进到吉他在手的状态。")
            return
        self.bar.setValue(0)
        self.btn_play.setText("暂停（F8）")
        self.btn_play.setChecked(True)

        rec = None
        if self.chk_rec.isChecked():
            try:
                path = REC.make_path(os.path.join(os.path.dirname(
                    os.path.abspath(__file__)), "录音"),
                    os.path.basename(self.path or ""), MC.ROLE_CN.get(self.role, ""))
                rec = REC.LoopbackRecorder(path, log=lambda m: self.sig_log.emit(m),
                                           device_name=self.cmb_rec.currentText() or None)
                self._rec_path = path
            except Exception as exc:
                self._append_log("! 录音初始化失败：%s（演奏照常）" % exc)
                rec = None
        else:
            self._rec_path = None

        self.player = PlayWorker(
            tl, float(self.spin_speed.value()), bool(self.chk_sync.isChecked()),
            lambda m: self.sig_log.emit(m),
            lambda a, b: self.sig_progress.emit(a, b),
            lambda st: self.sig_finished.emit(st), rec=rec)
        self.player.start()

    def _on_progress(self, a, b):
        if b > 0:
            self.bar.setValue(int(max(0.0, min(1.0, a / b)) * 100))

    def _on_finished(self, stats):
        self.btn_play.setText("开始演奏（F8）")
        self.btn_play.setChecked(False)
        if stats:
            self._append_log("本次：%d 条动作，迟到 %d 条（最多 %.0f 毫秒）"
                             % (stats.get("events", 0), stats.get("late", 0),
                                (stats.get("max_late") or 0) * 1000))

    def _append_log(self, msg):
        self.log.appendPlainText(time.strftime("%H:%M:%S ") + str(msg))

    def _hotkeys(self):
        """F8 开始/停止、F12 紧急停（全局热键，不依赖窗口焦点）。"""
        timer = QTimer(self)
        timer.timeout.connect(self._poll_hotkeys)
        timer.start(30)

    def _poll_hotkeys(self):
        for name, vk in (("F8", ie.VK_F8), ("F12", ie.VK_F12)):
            down = ie.is_key_down(vk)
            if down and not self._hot_last.get(name):
                if name == "F8":
                    self.toggle_play()
                else:
                    if self.player and self.player.is_alive():
                        self.player.stop()
                        self._append_log("F12 紧急停止")
            self._hot_last[name] = down

    def closeEvent(self, ev):
        if self.player and self.player.is_alive():
            self.player.stop()
            time.sleep(0.3)
        ev.accept()

def main():
    app = QApplication(sys.argv)
    try:
        from gui_theme import apply_theme
        apply_theme(app)
    except Exception:
        pass
    w = MainWindow()
    if "--smoke" in sys.argv:
        # 冒烟模式：构造主窗口，进事件循环 1 秒后正常退出
        w.show()
        QTimer.singleShot(1000, app.quit)
        rc = app.exec_()
        print("GUI_SMOKE_OK title=%s" % w.windowTitle())
        return 0 if rc == 0 else rc
    w.show()
    return app.exec_()

if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.exit(main())
