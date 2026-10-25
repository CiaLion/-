# -*- coding: utf-8 -*-
"""piano_roll.py -- 《异键演奏》v1.1 绑定游戏物理规则的钢琴卷帘编辑器。

★ 单一物理真相源：
  指法角标（弦/品/页）、同帧异品丢音、换品/翻页晚出，全部来自
  `midi_compiler.compile_fret / compile_chord / preschedule` 的真实输出，
  本文件不写任何平行的指法分配或延迟公式。

两种模式：
  · 模式 A（通用 MIDI）：打开 A 端任意中间/成品 MIDI，改音高/时值；
    `<40` / `>88` 画深红禁区，「一键归位」直接调用 pipeline.fix_pitch_range。
  · 模式 B（四角色转译序列）：加载 `adapt_song` 之后的角色序列，
    松开鼠标即跑 `compile_fret/compile_chord + preschedule`，把真实结果画回音符；
    「应用微调」走 `midi_compiler.compile_custom_role` 直通重编。

试听：numpy 合成拨弦包络 → 内存 WAV → QMediaPlayer / winsound，
      不 import soundcard，零 COM 冲突。
"""
from __future__ import annotations

import bisect
import io
import json
import math
import os
import sys
import time
import wave
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE, os.path.join(_ROOT, "transcriber")):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

# 中文项目路径与便携 runtime 下 Qt 平台插件定位：优先项目内 .venv，次选 sys.prefix
for _cand_plug in (
    Path(_ROOT) / ".venv" / "Lib" / "site-packages" / "PyQt5" / "Qt5" / "plugins",
    Path(sys.prefix) / "Lib" / "site-packages" / "PyQt5" / "Qt5" / "plugins",
):
    if _cand_plug.is_dir():
        os.environ.setdefault("QT_PLUGIN_PATH", str(_cand_plug))
        os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", str(_cand_plug / "platforms"))
        break

from PyQt5.QtCore import (QEvent, QPoint, QRect, Qt, QTimer, QUrl,  # noqa: E402
                          pyqtSignal)
from PyQt5.QtGui import (QColor, QFont, QKeySequence, QPainter,  # noqa: E402
                         QPen, QPolygon)
from PyQt5.QtWidgets import (QAbstractScrollArea, QApplication, QCheckBox,  # noqa: E402
                             QComboBox, QDialog, QFileDialog, QHBoxLayout,
                             QFrame, QLabel, QMessageBox, QPushButton,
                             QScrollArea, QShortcut, QSizePolicy, QSlider,
                             QVBoxLayout, QWidget)

import midi_compiler as MC  # noqa: E402
import guitar_plan as GP  # noqa: E402
import player_config as PC  # noqa: E402
import convert as CV  # noqa: E402

PITCH_MIN = 40
PITCH_MAX = 88
DISPLAY_LO = 36
DISPLAY_HI = 96
KEY_W = 64
RULER_H = 28
MIN_DUR = MC.KEY_HOLD if hasattr(MC, "KEY_HOLD") else 0.025
ALL_CHORDS = list(getattr(GP, "ALL_CHORDS", []) or [])

PLAY_ROLE_LABELS = {
    "solo": "1P 单人独奏（Solo 指弹）",
    "duet_lead": "双人合奏 · 1P 主音",
    "duet_rhythm_fret": "双人合奏 · 2P 伴奏（琶音/根音）",
    "duet_rhythm": "双人合奏 · 2P 伴奏（柱式/扫弦）",
}


def _out_dir():
    """文件对话框的默认目录（项目 out/ 存在就用它）。"""
    d = os.path.join(_ROOT, "out")
    return d if os.path.isdir(d) else _ROOT


def _clamp_pitch(p):
    return max(PITCH_MIN, min(PITCH_MAX, int(round(float(p)))))


def _all_notes():
    return list(range(DISPLAY_LO, DISPLAY_HI + 1))


def _as_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ══════════════════════════════════════════════════════════════════════════
#  1. 模型：数据 + 真实编译器桥
# ══════════════════════════════════════════════════════════════════════════
class PianoRollModel(object):
    """卷帘数据层。所有"该弹第几弦/第几品/会不会晚出"都问 `midi_compiler`。"""

    MAX_UNDO = 50

    def __init__(self, path=None, role=None, song=None):
        self.path = str(path) if path else None
        self.role = role
        self.song = song
        self.kind = None
        self.mode = "role" if role else "midi"
        self.track_name = "Main Melody"
        self.tracks = []
        self.track_idx = 0
        self.notes = []
        self.markers = []
        self.slices = []
        # v1.1：2P 混切分段（[(秒, "chord"|"fret")]），卷帘按它分段校验/上色
        self.mode_markers = []
        self.bpm = 120.0
        self.beat_sec = 0.5
        self.timing = MC.Timing()
        self.tl = None
        self.analysis = {"kind": None, "notes": [], "dropped": 0, "late": 0}
        self.phys_enabled = False
        self.role_notes = {}
        self._role_undo = {}
        self._role_redo = {}
        self.last_saved = None
        self.force_recalc = False
        self._a_key = {}
        self._a_pitch = {}
        self._undo = []
        self._redo = []
        self._suspend_undo = 0
        if path:
            self.load(path, role=role)

    # ── 读 ──────────────────────────────────────────────────────────────
    def load(self, path, role=None, force_recalc=False):
        self.path = str(path)
        self.role = role
        self.mode = "role" if role else "midi"
        self._undo, self._redo = [], []
        self.force_recalc = bool(force_recalc)
        if role:
            self._load_role(force_recalc=force_recalc)
            self.role_notes[self.role] = [dict(n) for n in self.notes]
            self._role_undo[self.role] = []
            self._role_redo[self.role] = []
        else:
            self._load_midi()

    def _sync_role_mode_state(self):
        """按当前激活角色同步 `mode_markers`、`markers` 与每个切片的 `hybrid_mode`。

        关键修复：从 1P 角色切回已缓存的 2P 角色时，必须恢复 2P 的 `mode_markers`
        并刷新 `slices` 的 `hybrid_mode`，否则混切状态会丢失变为 0 次。
        """
        song = self.song or {}
        p2 = song.get("p2") or {}
        if self.role in (MC.ROLE_DUET_RHYTHM, MC.ROLE_DUET_RHYTHM_FRET):
            raw_mm = song.get("mode_markers") or p2.get("mode_markers") or []
            self.mode_markers = [(round(float(mt), 6), str(md)) for (mt, md) in raw_mm]
            chord_mks = (p2.get("markers") or {}).get("chord") or song.get("markers") or []
            self.markers = [tuple(x) for x in chord_mks]
        else:
            self.mode_markers = []
            self.markers = [tuple(x) for x in (song.get("markers") or [])]
        for s in self.slices:
            s["hybrid_mode"] = self._mode_at_markers(float(s.get("t") or 0.0))

    def _normalize_role_note_flags(self):
        for n in self.notes:
            n["t"] = round(_as_float(n.get("t")), 6)
            n["dur"] = round(max(MIN_DUR, _as_float(n.get("dur"), 0.25)), 6)
            n["pitch"] = int(n.get("pitch") or 60)
            n["vel"] = int(n.get("vel") or 80)
            if self.role == MC.ROLE_SOLO:
                n["is_melody"] = (n.get("src") == "melody") or not n.get("src")
            elif self.role in (MC.ROLE_DUET_RHYTHM, MC.ROLE_DUET_RHYTHM_FRET):
                n["is_melody"] = False
            else:
                n["is_melody"] = True

    def _load_role(self, force_recalc=False):
        if self.song is None or force_recalc or self.song.get("path") != self.path:
            self.song = MC.load_song(self.path, force_recalc=force_recalc)
        song = self.song
        self.kind = MC.role_kind(song, self.role)
        track, _how = MC.pick_track(song, self.role)
        self.track = track
        raw = song.get("notes_by_track", {}).get(track, []) if track is not None else []
        self.notes = [dict(n) for n in raw]
        self._normalize_role_note_flags()
        p2 = song.get("p2") or {}
        self.slices = []
        for s in (p2.get("slices") or []):
            try:
                t0 = round(float(s.get("t") or 0), 6)
                self.slices.append({
                    "t": t0,
                    "end": round(float(s.get("end") or 0), 6),
                    "name": s.get("name") or "",
                    "raw_label": (s.get("raw_label") or s.get("label") or s.get("name") or ""),
                    "hybrid_mode": "chord",
                    "beat_sec": float(s.get("beat_sec") or 0) or 0.5,
                })
            except Exception:
                continue
        self._sync_role_mode_state()
        self.bpm = _as_float((song.get("bpm") or 120.0), 120.0) or 120.0
        self.beat_sec = (self.slices[0]["beat_sec"] if self.slices else
                         (60.0 / self.bpm if self.bpm else 0.5))
        names = song.get("track_names") or []
        self.track_name = (names[track] if track is not None and track < len(names)
                           else ("Solo" if self.role == MC.ROLE_SOLO else "Main Melody"))
        self.compile()

    def _load_midi(self):
        self.tracks = [t for t in CV.read_midi_tracks(self.path)]
        if not self.tracks:
            raise ValueError("这份 MIDI 里没有轨道：%s" % self.path)
        self._select_track_data(0)
        try:
            meta = MC.read_meta(self.path)
            self.bpm = _as_float(meta.get("bpm"), 120.0) or 120.0
        except Exception:
            self.bpm = 120.0
        self.beat_sec = 60.0 / self.bpm if self.bpm else 0.5
        self.kind = None
        self.markers, self.slices, self.mode_markers = [], [], []
        self.tl, self.analysis = None, {"kind": None, "notes": [], "dropped": 0, "late": 0}

    def _select_track_data(self, idx):
        idx = max(0, min(len(self.tracks) - 1, int(idx)))
        self.track_idx = idx
        tr = self.tracks[idx]
        self.track_name = tr.name or "Track %d" % (idx + 1)
        self.notes = []
        for n in tr.notes:
            dur = max(MIN_DUR, float(n.end_sec) - float(n.start_sec))
            self.notes.append({"t": round(max(0.0, float(n.start_sec)), 6),
                               "dur": round(dur, 6), "pitch": int(n.pitch),
                               "vel": int(n.velocity), "is_melody": True})
        self._sort()

    def select_track(self, idx):
        if self.mode != "midi":
            return
        self._select_track_data(idx)
        self._undo, self._redo = [], []

    def reload_role(self):
        if self.mode == "role":
            self._load_role()

    # ── 角色切换 / 开关重建（模式 B）──────────────────────────────────
    def remember_role_notes(self):
        """把当前画布上的音符与撤销栈记到该角色名下。"""
        if self.mode == "role" and self.role:
            self.role_notes[self.role] = [dict(n) for n in self.notes]
            self._role_undo[self.role] = list(self._undo)
            self._role_redo[self.role] = list(self._redo)

    def switch_role(self, new_role):
        """在卷帘内切换演奏角色；完整恢复目标角色的音符、混切标记与撤销栈。"""
        if self.mode != "role" or not new_role or new_role == self.role:
            return False
        self.remember_role_notes()
        if new_role in self.role_notes:
            self.role = new_role
            self.kind = MC.role_kind(self.song or {}, new_role)
            self.notes = [dict(n) for n in self.role_notes[new_role]]
            self._normalize_role_note_flags()
            self._undo = list(self._role_undo.get(new_role) or [])
            self._redo = list(self._role_redo.get(new_role) or [])
            self._sync_role_mode_state()
            self.timing = MC.Timing()
            self.compile()
        else:
            self.role = new_role
            self._load_role(force_recalc=False)
            self.role_notes[self.role] = [dict(n) for n in self.notes]
            self._undo = []
            self._redo = []
        return True

    def roll_flags(self):
        """当前播放器用的琶音 / 扫弦律动开关。"""
        try:
            cfg = PC.load()
            return (bool(cfg.get("enable_arpeggio", True)),
                    bool(cfg.get("enable_chord_rhythm", True)))
        except Exception:
            return True, True

    def roll_strum_mode(self):
        """当前 2P 和弦段的发声方式：True=鼠标扫弦，False=多弦同按柱式抓弦。"""
        try:
            return bool(PC.load().get("chord_strum_mode", False))
        except Exception:
            return False

    def rebuild_for_strum_mode(self, on):
        """柱式 / 扫弦切换：写回配置并按新方式重算 2P 混切序列。"""
        self.remember_role_notes()
        melody = self.role_notes.get(MC.ROLE_DUET_LEAD)
        try:
            PC.save({"chord_strum_mode": bool(on)})
        except Exception:
            pass
        role = self.role
        self.role_notes = {}
        self.song = None
        self.load(self.path, role=role, force_recalc=True)
        if melody:
            self.role_notes[MC.ROLE_DUET_LEAD] = [dict(n) for n in melody]
            if role == MC.ROLE_DUET_LEAD:
                self.notes = [dict(n) for n in melody]
                self.compile()

    def rebuild_for_flags(self, enable_arpeggio, enable_chord_rhythm):
        """琶音 / 扫弦律动变化后，基于原曲重新生成 2P 与 Solo。"""
        self.remember_role_notes()
        melody = self.role_notes.get(MC.ROLE_DUET_LEAD)
        try:
            PC.save({"enable_arpeggio": bool(enable_arpeggio),
                     "enable_chord_rhythm": bool(enable_chord_rhythm)})
        except Exception:
            pass
        role = self.role
        self.role_notes = {}
        self.song = None
        self.load(self.path, role=role, force_recalc=True)
        if melody:
            self.role_notes[MC.ROLE_DUET_LEAD] = [dict(n) for n in melody]
            if role == MC.ROLE_DUET_LEAD:
                self.notes = [dict(n) for n in melody]
                self.compile()

    # ── 基础编辑（带撤销）──────────────────────────────────────────────
    def _snapshot(self):
        if self._suspend_undo:
            return
        self._undo.append([dict(n) for n in self.notes])
        if len(self._undo) > self.MAX_UNDO:
            self._undo.pop(0)
        self._redo = []

    def begin_batch(self):
        self._snapshot()
        self._suspend_undo += 1

    def end_batch(self):
        self._suspend_undo = max(0, self._suspend_undo - 1)

    def _sort(self):
        self.notes.sort(key=lambda n: (n["t"], n["pitch"]))

    def add_note(self, t, pitch, dur=None, vel=85):
        self._snapshot()
        dur = max(MIN_DUR, _as_float(dur, 0.25))
        is_mel = self.role not in (MC.ROLE_DUET_RHYTHM, MC.ROLE_DUET_RHYTHM_FRET)
        n = {"t": round(max(0.0, _as_float(t)), 6), "dur": round(dur, 6),
             "pitch": _clamp_pitch(pitch), "vel": max(1, min(127, int(vel))),
             "is_melody": is_mel}
        self.notes.append(n)
        return n

    def remove_notes(self, notes):
        if not notes:
            return 0
        self._snapshot()
        gone = set(id(n) for n in notes)
        before = len(self.notes)
        self.notes = [n for n in self.notes if id(n) not in gone]
        return before - len(self.notes)

    def move_notes(self, notes, dt, dp):
        for n in notes:
            n["t"] = round(max(0.0, n["t"] + dt), 6)
            n["pitch"] = _clamp_pitch(n["pitch"] + dp)

    def set_pitch(self, note, pitch):
        note["pitch"] = _clamp_pitch(pitch)

    def set_time(self, note, t, keep_end=False):
        end = note["t"] + note["dur"]
        note["t"] = round(max(0.0, float(t)), 6)
        if keep_end:
            note["dur"] = round(max(MIN_DUR, end - note["t"]), 6)
        note["dur"] = max(MIN_DUR, note["dur"])

    def resize(self, note, dur):
        note["dur"] = round(max(MIN_DUR, _as_float(dur, MIN_DUR)), 6)

    def undo(self):
        if not self._undo:
            return False
        self._redo.append([dict(n) for n in self.notes])
        self.notes = self._undo.pop()
        self.compile()
        return True

    def redo(self):
        if not self._redo:
            return False
        self._undo.append([dict(n) for n in self.notes])
        self.notes = self._redo.pop()
        self.compile()
        return True

    # ── 编译（唯一真相源）──────────────────────────────────────────────
    def compile(self):
        self.timing = MC.Timing()
        if self.tracks and 0 <= int(getattr(self, "track_idx", 0) or 0) < len(self.tracks):
            self.tracks[int(self.track_idx)].notes = self.to_convert_notes()
        if self.mode != "role" or not self.role:
            self.tl = None
            return None
        try:
            tl = MC.compile_custom_role(
                self.song, self.role, self.notes,
                timing=self.timing,
                chord_slices=self.markers,
                start_offset=0.0,
                mode_markers=self.mode_markers,
                chord_strum_mode=bool((self.song or {}).get("chord_strum_mode")),
            )
        except Exception as exc:  # noqa: BLE001
            self.tl = {"events": [], "stats": {}, "hw_stats": {},
                       "analysis": {"kind": self.kind, "notes": [], "dropped": 0,
                                    "late": 0, "error": str(exc)}}
            self._index_analysis(self.tl["analysis"])
            return self.tl
        self.tl = tl
        self._index_analysis(tl.get("analysis") or {})
        return tl

    def _index_analysis(self, analysis):
        self.analysis = analysis or {"kind": None, "notes": [], "dropped": 0, "late": 0}
        self._a_key = {}
        self._a_pitch = {}
        for a in self.analysis.get("notes") or []:
            key = (round(_as_float(a.get("t")), 2), int(a.get("pitch") or 0))
            self._a_key[key] = a
            self._a_pitch.setdefault(int(a.get("pitch") or 0), []).append(a)
        for lst in self._a_pitch.values():
            lst.sort(key=lambda x: _as_float(x.get("t")))

    def set_analysis(self, analysis):
        """外部（模式 A 的物理预览）写入分析结果。"""
        self._index_analysis(analysis or {"kind": None, "notes": [], "dropped": 0,
                                          "late": 0})

    def analysis_for(self, note):
        """查这颗音符的真实编译结果（不重算）。"""
        key = (round(note["t"], 2), int(note["pitch"]))
        hit = self._a_key.get(key)
        if hit is not None:
            return hit
        lst = self._a_pitch.get(int(note["pitch"])) or []
        if not lst:
            return None
        ts = [_as_float(a.get("t")) for a in lst]
        i = bisect.bisect_left(ts, note["t"])
        for j in (i - 1, i):
            if 0 <= j < len(lst) and abs(ts[j] - note["t"]) <= 0.02:
                return lst[j]
        return None

    def save_guard(self):
        """保存/应用前的硬拦截判定（不弹窗）。"""
        if self.mode == "role":
            try:
                self.compile()
            except Exception:
                pass
        bad = self.invalid_notes()
        return (not bad), bad

    def invalid_notes(self):
        """返回红色非法音符 [(note, reason), ...]。"""
        bad = []
        for n in self.notes:
            try:
                pitch = int(n["pitch"])
            except Exception:
                continue
            a = self.analysis_for(n)
            reason = None
            if pitch < 40 or pitch > 88:
                reason = "range"
            elif a is not None and a.get("dropped"):
                reason = "dropped"
            elif a is not None and a.get("mode") == "fret":
                reason = None
            elif self.mode_at(n["t"]) == "chord" and a is not None and a.get("string"):
                name = a.get("chord") or ""
                if name and int(a["string"]) in self.mine_strings(name):
                    reason = "mine"
            if reason:
                bad.append((n, reason))
        return sorted(bad, key=lambda x: (float(x[0].get("t") or 0), int(x[0].get("pitch") or 0)))

    def status_text(self):
        st = (self.tl or {}).get("stats") or {}
        n = len(self.notes)
        if self.mode != "role":
            return "音符：%d ｜ 音域：%s~%s ｜ 提示：<40 / >88 是深红禁区" % (
                n, min((x["pitch"] for x in self.notes), default="-"),
                max((x["pitch"] for x in self.notes), default="-"))
        dropped = int(st.get("dropped") or self.analysis.get("dropped") or 0)
        late = int(st.get("late") or self.analysis.get("late") or 0)
        max_late = float(st.get("max_late") or 0.0) * 1000.0
        z = int(st.get("switches") or 0)
        dbl = int(st.get("doubled") or 0)
        return ("总发声：%d ｜ 翻页步数：%d ｜ 丢弃音：%d ｜ 晚出音：%d（最大 %.0f ms）"
                " ｜ Z键混切: %d 次 ｜ 双音/柱式: %d 处"
                % (int(st.get("voices") or 0), int(st.get("turns") or 0),
                   dropped, late, max_late, z, dbl))

    # ── 和弦模式：24 和弦 / 地雷弦 / 扫弦 / 单拨 ────────────────────────
    def chord_names(self):
        return list(ALL_CHORDS)

    def valid_chord(self, name):
        return name in ALL_CHORDS

    def _mode_at_markers(self, t):
        if not self.mode_markers:
            return self.kind or "chord"
        got = "chord"
        for (mt, mode) in self.mode_markers:
            if float(mt) <= float(t) + 1e-6:
                got = str(mode or got)
            else:
                break
        return got

    def mode_at(self, t):
        """t 时刻 2P 处于哪种模式：`"chord"` 或 `"fret"`。"""
        return self._mode_at_markers(t)

    def hybrid_switch_times(self):
        """所有发生 Z 键切换的时刻（相邻两段模式不同处），供卷帘画 ⚡Z 标记。"""
        out = []
        prev = None
        for (mt, mode) in self.mode_markers or []:
            if prev is not None and str(mode) != str(prev):
                out.append(round(float(mt), 6))
            prev = mode
        return out

    def chord_at(self, t):
        got = None
        for (mt, name) in self.markers:
            if mt <= float(t) + 1e-6:
                got = name
            else:
                break
        return got

    def slice_at(self, t):
        for i, s in enumerate(self.slices):
            if s["t"] - 1e-6 <= t < s["end"] + 1e-6:
                return i
        return None

    def set_slice_chord(self, index, name):
        if not self.valid_chord(name):
            return False
        if not (0 <= index < len(self.slices)):
            return False
        self._snapshot()
        s = self.slices[index]
        s["name"] = name
        s["raw_label"] = name
        self._rewrite_slice_notes(s)
        for i, (mt, nm) in enumerate(self.markers):
            if abs(mt - s["t"]) <= 1e-6:
                self.markers[i] = (mt, name)
        return True

    def _rewrite_slice_notes(self, s):
        """把切片内的音符换成新和弦的音簇。"""
        t, end = s["t"], s["end"]
        keep = [n for n in self.notes if not (t - 1e-6 <= n["t"] < end - 1e-6)]
        pitches = [p for _s, p in MC.chord_pluck_strings(s["name"])][:4]
        if len(pitches) < 2:
            pitches = [p for _s, p in MC.chord_sounding_strings(s["name"])][:4]
        for p in pitches:
            keep.append({"t": round(t, 6), "dur": round(max(0.20, min(0.35, end - t)), 6),
                         "pitch": int(p), "vel": 85, "is_melody": False})
        self.notes = keep
        self._sort()

    def mine_strings(self, name):
        """被"小二度错音"锁死的弦号（guitar_plan.pluck_safe_strings）。"""
        safe = set(int(s) for s in GP.pluck_safe_strings(name))
        return [s for s in range(1, 7) if s not in safe]

    def clean_strings(self, name):
        return [s for s in range(1, 7) if s not in self.mine_strings(name)]

    def add_strum(self, t, up=True, name=None):
        """添加一次扫弦/柱式：当前和弦 ≥3 音同帧音簇。"""
        t = round(max(0.0, float(t)), 6)
        name = name or self.chord_at(t)
        if not self.valid_chord(name or ""):
            return None
        pairs = MC.chord_sounding_strings(name)
        pitches = [p for _s, p in pairs][:6]
        if len(pitches) < 3:
            pitches = [p for p in range(40, 76)
                       if p % 12 in GP.chord_pitch_class_set(name)][:6]
        if len(pitches) < 3:
            return None
        self.begin_batch()
        out = []
        for p in pitches:
            out.append(self.add_note(t, p, dur=0.30, vel=75))
        self.end_batch()
        return out

    def add_pluck(self, t, string, name=None):
        """添加一次 1~6 弦单拨；地雷弦直接拒绝。"""
        t = round(max(0.0, float(t)), 6)
        name = name or self.chord_at(t)
        string = int(string)
        if not self.valid_chord(name or ""):
            return None
        if string in self.mine_strings(name):
            return None
        p = GP.pluck_pitch(name, string)
        if p is None:
            return None
        self.begin_batch()
        n = self.add_note(t, int(p), dur=0.25, vel=85)
        self.end_batch()
        return n

    # ── 音域归位 ────────────────────────────────────────────────────────
    def fix_range(self, instrument_mode="acoustic"):
        import pipeline as PL
        notes = [CV.Note(int(n["pitch"]), float(n["t"]),
                         float(n["t"]) + float(n["dur"]), int(n["vel"]))
                 for n in self.notes]
        fixed, stats = PL.fix_pitch_range(notes, low=PITCH_MIN, high=PITCH_MAX,
                                          instrument_mode=instrument_mode)
        self._snapshot()
        fixed.sort(key=lambda x: (x.start_sec, x.pitch))
        self.notes = [{"t": round(float(n.start_sec), 6),
                       "dur": round(max(MIN_DUR, float(n.end_sec) - float(n.start_sec)), 6),
                       "pitch": _clamp_pitch(n.pitch), "vel": int(n.velocity),
                       "is_melody": True} for n in fixed]
        return stats

    def restore_auto(self):
        """恢复默认自动编配：重新走 load_song/adapt_song 的自动结果。"""
        if self.mode != "role":
            return False
        self.role_notes = {}
        self._role_undo = {}
        self._role_redo = {}
        self.song = None
        self.load(self.path, role=self.role, force_recalc=True)
        return True

    # ── 保存（同步同名 .json）───────────────────────────────────────────
    def to_convert_notes(self, notes=None):
        out = []
        for n in (notes if notes is not None else self.notes):
            out.append(CV.Note(int(n["pitch"]), float(n["t"]),
                               float(n["t"]) + float(n["dur"]), int(n["vel"])))
        return out

    def _song_notes(self, name):
        song = self.song or {}
        idx = (song.get("track_index") or {}).get(name)
        if idx is None:
            return []
        return [dict(n) for n in ((song.get("notes_by_track") or {}).get(idx) or [])]

    def _main_melody_notes(self):
        return [dict(n) for n in (self.role_notes.get(MC.ROLE_DUET_LEAD)
                                  or self._song_notes(MC.TRACK_DUET_LEAD)
                                  or self._song_notes(MC.TRACK_SOLO_MASTER))]

    def _chord_track_notes(self):
        """原曲 Chords 轨：多轨另存时原样写回，保证和声信息不丢。"""
        song = self.song or {}
        by_track = song.get("notes_by_track") or {}
        for i, name in enumerate(song.get("track_names") or []):
            if str(name).strip().lower() in ("chords", "chord", "chord_full", "和弦"):
                return [dict(n) for n in (by_track.get(i) or [])]
        return []

    def multitrack_export(self):
        """模式 B 另存：主旋律 + 和弦 + 已微调的角色轨，保证重新加载时四角色可用。"""
        self.remember_role_notes()
        out = []
        melody = self._main_melody_notes()
        if melody:
            out.append(CV.TrackNotes("Main Melody", self.to_convert_notes(melody)))
            out.append(CV.TrackNotes(MC.TRACK_DUET_LEAD, self.to_convert_notes(melody)))
        chords = self._chord_track_notes()
        if chords:
            out.append(CV.TrackNotes("Chords", self.to_convert_notes(chords)))
        p2_variants = ((self.song or {}).get("p2") or {}).get("notes") or {}
        for role, name in ((MC.ROLE_SOLO, MC.TRACK_SOLO_MASTER),
                           (MC.ROLE_DUET_RHYTHM, MC.TRACK_DUET_RHYTHM)):
            if any(tr.name == name for tr in out):
                continue
            if role == MC.ROLE_DUET_RHYTHM:
                notes = (self.role_notes.get(role) or p2_variants.get("chord")
                         or self._song_notes(name))
            else:
                notes = self.role_notes.get(role) or self._song_notes(name)
            if not notes:
                continue
            out.append(CV.TrackNotes(name, self.to_convert_notes(notes)))
        arp = (self.role_notes.get(MC.ROLE_DUET_RHYTHM_FRET)
               or p2_variants.get("arp") or [])
        if arp and not any(tr.name == MC.TRACK_DUET_P2_ARP for tr in out):
            out.append(CV.TrackNotes(MC.TRACK_DUET_P2_ARP,
                                     self.to_convert_notes(arp)))
        if not out:
            out = [CV.TrackNotes(self.track_name or "Main Melody",
                                 self.to_convert_notes())]
        return out

    def save(self, path=None, sync_json=True):
        path = str(path or self.path)
        if not path:
            raise ValueError("没有保存路径")
        if self.mode == "role" and self.song:
            tracks = self.multitrack_export()
        else:
            tracks = [CV.TrackNotes(self.track_name or "Main Melody", self.to_convert_notes())]
        text_events = []
        raw_mm = self.mode_markers or ((self.song or {}).get("mode_markers") or [])
        if raw_mm:
            text_events = [(float(mt), "P2_MODE:%s" % str(md))
                           for (mt, md) in raw_mm]
        CV.write_midi(path, tracks, bpm=float(self.bpm or 120.0),
                      text_events=text_events)
        jp = None
        if sync_json:
            jp = self.sync_json(path)
        self.path = path
        self.last_saved = path
        return {"midi": path, "json": jp}

    def sync_json(self, path=None):
        """把改动同步进同目录同名 `<stem>.json`（存在才写）。"""
        path = str(path or self.path)
        jp = str(Path(path).with_suffix(".json"))
        if not os.path.isfile(jp):
            return None
        try:
            with open(jp, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception:
            return None
        try:
            if isinstance(data, dict) and isinstance(data.get("tracks"), list):
                kind = data.get("kind") or ("main_melody" if "main" in Path(jp).stem else "")
                extra = dict((k, v) for k, v in data.items()
                             if k not in ("version", "kind", "source", "tpb",
                                          "tempo_bpm", "tracks"))
                CV.save_tracks_json(
                    jp,
                    [CV.TrackNotes(self.track_name or "Main Melody",
                                   self.to_convert_notes())],
                    kind=kind, source=data.get("source") or "",
                    extra=extra, tempo_bpm=float(self.bpm or 120.0))
                return jp
            if isinstance(data, list):
                CV.save_chords_json(jp, self.chord_slices())
                return jp
        except Exception:
            return None
        return None

    def chord_slices(self):
        """把当前音符聚成 ChordSlice 列表（供 chords.json 同步）。"""
        out = []
        src = self.slices or [{"t": m[0], "end": None, "name": m[1]}
                              for m in self.markers]
        if not src:
            notes = sorted(self.notes, key=lambda n: (n["t"], n["pitch"]))
            clusters = []
            for n in notes:
                if clusters and n["t"] - clusters[-1][0] <= 0.03:
                    clusters[-1][1].append(n)
                else:
                    clusters.append([n["t"], [n]])
            for t0, group in clusters:
                out.append(CV.ChordSlice(round(t0, 6),
                                         round(max(n["t"] + n["dur"] for n in group), 6),
                                         [int(n["pitch"]) for n in group]))
            return out
        for s in src:
            t = float(s["t"])
            end = s.get("end")
            if end is None:
                nxt = [x["t"] for x in src if float(x["t"]) > t + 1e-6]
                end = min(nxt) if nxt else (max((n["t"] + n["dur"] for n in self.notes),
                                                default=t + 0.5))
            pitches, vels = [], []
            for n in self.notes:
                if t - 1e-6 <= n["t"] < float(end) - 1e-6:
                    pitches.append(int(n["pitch"]))
                    vels.append(int(n["vel"]))
            if pitches:
                out.append(CV.ChordSlice(round(t, 6), round(float(end), 6), pitches))
        return out

    # ── 试听合成 ────────────────────────────────────────────────────────
    @staticmethod
    def synthesize_wav(notes, bpm=120.0, limit_sec=None):
        """numpy 合成拨弦包络 → 标准 44100Hz / 16bit / 单声道 WAV bytes。"""
        import numpy as np
        sr = 44100
        lo = 0.0
        hi = max((float(n["t"]) + max(MIN_DUR, float(n["dur"])) for n in notes),
                 default=0.5)
        total = max(0.25, hi + 0.35)
        if limit_sec:
            total = min(total, float(limit_sec))
        buf = np.zeros(int(sr * total) + 1, dtype=np.float32)
        for n in notes:
            pitch = int(n["pitch"])
            f0 = 440.0 * (2.0 ** ((pitch - 69) / 12.0))
            dur = max(0.09, min(float(n["dur"]), 4.0)) + 0.25
            cnt = min(int(sr * dur), len(buf))
            if cnt <= 0:
                continue
            tt = np.arange(cnt, dtype=np.float32) / sr
            env = np.exp(-tt * 5.5) * (1.0 - np.exp(-tt * 350.0))
            wave_ = (np.sin(2 * np.pi * f0 * tt)
                     + 0.45 * np.sin(4 * np.pi * f0 * tt)
                     + 0.18 * np.sin(6 * np.pi * f0 * tt))
            amp = max(0.05, min(1.0, float(n.get("vel", 80)) / 127.0))
            seg = wave_ * env * amp * 0.26
            st = int(sr * max(0.0, float(n["t"]) - lo))
            en = min(len(buf), st + cnt)
            if en > st:
                buf[st:en] += seg[:en - st]
        peak = float(np.max(np.abs(buf))) if len(buf) else 0.0
        if peak > 1e-6:
            buf = buf / peak * 0.9
        pcm = (np.clip(buf, -1.0, 1.0) * 32767.0).astype("<i2")
        bio = io.BytesIO()
        with wave.open(bio, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(pcm.tobytes())
        return bio.getvalue()


# ══════════════════════════════════════════════════════════════════════════
#  2. 画布
# ══════════════════════════════════════════════════════════════════════════
class PianoRollCanvas(QAbstractScrollArea):
    """卷帘视图：绘制、拖拽、缩放、吸附、选择。"""

    selectionChanged = pyqtSignal()
    statusChanged = pyqtSignal(str)
    editCommitted = pyqtSignal()
    noteClicked = pyqtSignal(object)
    seekRequested = pyqtSignal(float)
    playPauseRequested = pyqtSignal()
    scrubBegan = pyqtSignal()
    scrubEnded = pyqtSignal()

    def __init__(self, model, parent=None):
        super(PianoRollCanvas, self).__init__(parent)
        self.model = model
        self.pps = 180.0                 # 每秒像素
        self.row_h = 13.0
        self.grid_div = 4                # 每拍切 4 份 = 1/4 拍
        self.selection = []
        self.sel_slice = None
        self.cursor_t = 0.0
        self._drag = None
        self._slice_drag = None
        self._seek_drag = False
        self._seek_x = None
        self._rubber = None
        self.playhead = None
        self._follow_play = True
        self._pressed_pitch = None
        self._press_timer = QTimer(self)
        self._press_timer.setSingleShot(True)
        self._press_timer.setInterval(180)
        self._press_timer.timeout.connect(self._clear_pressed_pitch)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.viewport().installEventFilter(self)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.horizontalScrollBar().valueChanged.connect(self.viewport().update)
        self.verticalScrollBar().valueChanged.connect(self.viewport().update)
        self._fit()

    def _clear_pressed_pitch(self):
        self._pressed_pitch = None
        self.viewport().update()

    @staticmethod
    def _pitch_name(p):
        names = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
        return "%s%d" % (names[int(p) % 12], int(p) // 12 - 1)

    # ── 坐标 ───────────────────────────────────────────────────────────
    def _duration(self):
        return max(1.0, max((n["t"] + n["dur"] for n in self.model.notes), default=1.0))

    def _fit(self):
        self._update_scrollbars()

    def _update_scrollbars(self):
        vw = max(1, self.viewport().width())
        vh = max(1, self.viewport().height())
        hmax = max(0, int(self._duration() * self.pps) + 80 - (vw - KEY_W))
        self.horizontalScrollBar().setRange(0, hmax)
        self.horizontalScrollBar().setPageStep(max(1, vw - KEY_W))
        content = (DISPLAY_HI - DISPLAY_LO + 1) * self.row_h
        vmax = max(0, int(content) - (vh - RULER_H))
        self.verticalScrollBar().setRange(0, vmax)
        self.verticalScrollBar().setPageStep(max(1, vh - RULER_H))

    def resizeEvent(self, ev):
        super(PianoRollCanvas, self).resizeEvent(ev)
        self._update_scrollbars()

    def x_of(self, t):
        return KEY_W + float(t) * self.pps - self.horizontalScrollBar().value()

    def t_of(self, x):
        return (float(x) - KEY_W + self.horizontalScrollBar().value()) / self.pps

    def y_of(self, pitch):
        return RULER_H + (DISPLAY_HI - int(pitch)) * self.row_h \
            - self.verticalScrollBar().value()

    def pitch_of(self, y):
        return int(DISPLAY_HI - math.floor(
            (float(y) - RULER_H + self.verticalScrollBar().value()) / self.row_h))

    def note_rect(self, n):
        x = self.x_of(n["t"])
        w = max(6.0, n["dur"] * self.pps)
        y = self.y_of(n["pitch"])
        return QRect(int(round(x)), int(round(y)), int(round(w)),
                     max(3, int(round(self.row_h)) - 1))

    def snap(self, t):
        step = self.step_sec()
        if step <= 1e-9:
            return round(max(0.0, float(t)), 6)
        return round(round(float(t) / step) * step, 6)

    def step_sec(self):
        if self.grid_div <= 0:
            return 0.0
        return float(self.model.beat_sec or 0.5) / float(self.grid_div)

    # ── 绘制 ───────────────────────────────────────────────────────────
    def paintEvent(self, ev):
        p = QPainter(self.viewport())
        try:
            self._paint(p)
        finally:
            p.end()

    def _paint(self, p):
        w, h = self.viewport().width(), self.viewport().height()
        p.fillRect(0, 0, w, h, QColor("#0b0f16"))
        self._paint_rows(p, w, h)
        self._paint_grid(p, w, h)
        self._paint_slices(p, w)
        self._paint_notes(p, w, h)
        if self.playhead is not None:
            x = self.x_of(self.playhead)
            if KEY_W <= x <= w:
                p.setPen(QPen(QColor("#ffd866"), 2))
                p.drawLine(int(x), RULER_H, int(x), h)
        self._paint_ruler(p, w)
        self._paint_keyboard(p, h)
        if self._rubber is not None:
            p.setPen(QPen(QColor("#58a6ff"), 1, Qt.DashLine))
            p.setBrush(QColor(88, 166, 255, 40))
            p.drawRect(self._rubber)

    def _visible_pitches(self, h):
        top = DISPLAY_HI - int(self.verticalScrollBar().value() / self.row_h)
        return range(max(DISPLAY_LO, top), min(DISPLAY_HI, top - int(h / self.row_h) - 2), -1)

    def _paint_rows(self, p, w, h):
        black = {1, 3, 6, 8, 10}
        for pitch in self._visible_pitches(h):
            y = self.y_of(pitch)
            pc = pitch % 12
            if pitch < PITCH_MIN or pitch > PITCH_MAX:
                p.fillRect(KEY_W, int(y), max(0, w - KEY_W), int(self.row_h),
                           QColor("#2a0b10"))
                continue
            col = QColor("#0e1420") if pc in black else QColor("#141b28")
            if pitch % 12 == 0:
                col = QColor("#18202f")
            p.fillRect(KEY_W, int(y), max(0, w - KEY_W), int(self.row_h), col)

    def _paint_grid(self, p, w, h):
        beat = float(self.model.beat_sec or 0.5)
        if beat <= 1e-6:
            return
        t = 0.0
        dur = self._duration()
        while t <= dur + beat:
            x = self.x_of(t)
            if x < KEY_W:
                t += beat
                continue
            if x > w:
                break
            beat_idx = int(round(t / beat))
            if beat_idx % 4 == 0:
                p.setPen(QPen(QColor("#2b3d5c"), 1))
            else:
                p.setPen(QPen(QColor("#1b2434"), 1))
            p.drawLine(int(x), RULER_H, int(x), h)
            t += beat
        if self.grid_div > 1:
            s = beat / self.grid_div
            t = 0.0
            p.setPen(QPen(QColor("#131a26"), 1))
            while t <= dur:
                x = self.x_of(t)
                if KEY_W <= x <= w:
                    p.drawLine(int(x), RULER_H, int(x), h)
                t += s

    def _paint_ruler(self, p, w):
        """顶部节拍时刻表：小节号 + 拍刻度 + 可拖动的播放指针把手。"""
        p.fillRect(0, 0, w, RULER_H, QColor("#151c28"))
        beat = float(self.model.beat_sec or 0.5)
        if beat <= 1e-6:
            return
        p.setFont(QFont("Microsoft YaHei UI", 7))
        idx = 0
        t = 0.0
        last_label_x = -999
        dur = self._duration()
        while t <= dur + beat:
            x = self.x_of(t)
            if KEY_W <= x <= w:
                is_bar = (idx % 4 == 0)
                p.setPen(QPen(QColor("#4a5f86" if is_bar else "#26324a"), 1))
                p.drawLine(int(x), RULER_H - (11 if is_bar else 5), int(x), RULER_H)
                if is_bar and (x - last_label_x) >= 18:
                    p.setPen(QColor("#9fb3d1"))
                    p.drawText(QRect(int(x) + 2, 0, 52, RULER_H - 8),
                               Qt.AlignLeft | Qt.AlignVCenter,
                               "%d 小节" % (idx // 4 + 1))
                    p.setPen(QColor("#5f7292"))
                    p.drawText(QRect(int(x) + 2, 9, 52, RULER_H - 8),
                               Qt.AlignLeft | Qt.AlignVCenter, "%.1fs" % t)
                    last_label_x = x
            idx += 1
            t += beat
        if self.playhead is not None:
            x = self.x_of(self.playhead)
            if KEY_W - 6 <= x <= w + 6:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("#ffd866"))
                tri = QPolygon([QPoint(int(x) - 6, RULER_H - 2),
                                QPoint(int(x) + 6, RULER_H - 2),
                                QPoint(int(x), RULER_H - 12)])
                p.drawPolygon(tri)
                p.setPen(QPen(QColor("#ffd866"), 2))
                p.drawLine(int(x), 0, int(x), RULER_H - 2)
        if self._seek_drag and self._seek_x is not None:
            txt = "%.2f s" % (max(0.0, self.t_of(self._seek_x)))
            tw = 64
            bx = int(max(KEY_W, min(w - tw - 2, self._seek_x - tw / 2)))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#1f6feb"))
            p.drawRoundedRect(QRect(bx, 2, tw, 18), 4, 4)
            p.setPen(QColor("#ffffff"))
            p.setFont(QFont("Consolas", 8))
            p.drawText(QRect(bx, 2, tw, 18), Qt.AlignCenter, txt)

    def _paint_slices(self, p, w):
        if not self.model.slices:
            return
        bar_h = max(14, int(self.row_h) + 2)
        p.setFont(QFont("Microsoft YaHei UI", 8, QFont.Bold))
        # v1.1：将相邻且同模式、同和弦名的单拍切片合并为连续视觉块，
        # 避免每拍 1 个切片在缩小时因单拍窄于 34px 而导致整屏和弦名全部消失！
        runs = []
        for i, s in enumerate(self.model.slices):
            hybrid = str(s.get("hybrid_mode") or "chord") == "fret"
            disp = (s.get("raw_label") or s.get("name") or "") if hybrid \
                else (s.get("name") or s.get("raw_label") or "")
            sel = (self.sel_slice == i)
            if (runs and runs[-1]["hybrid"] == hybrid
                    and runs[-1]["disp"] == disp
                    and abs(float(s["t"]) - runs[-1]["end"]) <= 0.12):
                runs[-1]["end"] = float(s["end"])
                runs[-1]["sel"] = runs[-1]["sel"] or sel
            else:
                runs.append({"t": float(s["t"]), "end": float(s["end"]),
                             "hybrid": hybrid, "disp": disp, "sel": sel})

        for r in runs:
            x1 = self.x_of(r["t"])
            x2 = self.x_of(r["end"])
            if x2 < KEY_W or x1 > w:
                continue
            vx1 = int(max(KEY_W, x1))
            vx2 = int(min(w, x2))
            vw = max(1, vx2 - vx1)
            if r["hybrid"]:
                col = QColor("#155e4f") if r["sel"] else QColor("#0f3d33")
                txt_col = QColor("#6ee7b7")
                full_lbl = "[品格] %s" % r["disp"]
                short_lbl = r["disp"]
            else:
                col = QColor("#5a4218") if r["sel"] else QColor("#3b2a12")
                txt_col = QColor("#ffd866")
                full_lbl = "[和弦] %s" % r["disp"]
                short_lbl = r["disp"]
            p.fillRect(vx1, RULER_H, vw, bar_h, col)
            p.setPen(QPen(QColor("#1e293b"), 1))
            p.drawLine(vx1, RULER_H, vx1, RULER_H + bar_h)
            if vw >= 54:
                p.setPen(txt_col)
                p.drawText(QRect(vx1 + 4, RULER_H, vw - 6, bar_h),
                           Qt.AlignLeft | Qt.AlignVCenter, full_lbl)
            elif vw >= 16 and short_lbl:
                p.setPen(txt_col)
                p.drawText(QRect(vx1 + 2, RULER_H, vw - 3, bar_h),
                           Qt.AlignLeft | Qt.AlignVCenter, short_lbl)

        for t in (self.model.hybrid_switch_times() if self.model.mode_markers else []):
            x = self.x_of(t)
            if x < KEY_W or x > w:
                continue
            p.setPen(QPen(QColor("#ff5f56"), 2, Qt.DashLine))
            p.drawLine(int(x), RULER_H, int(x), int(RULER_H + bar_h))
            p.setPen(QColor("#ff8b84"))
            p.setFont(QFont("Microsoft YaHei UI", 7, QFont.Bold))
            p.drawText(QRect(int(x) + 2, RULER_H, 28, bar_h),
                       Qt.AlignLeft | Qt.AlignVCenter, "⚡Z")

    def _paint_notes(self, p, w, h):
        if not self.model.notes:
            return
        sel = set(id(n) for n in self.selection)
        p.setFont(QFont("Consolas", 7))
        for n in self.model.notes:
            r = self.note_rect(n)
            if r.right() < KEY_W or r.left() > w or r.bottom() < RULER_H or r.top() > h:
                continue
            a = (self.model.analysis_for(n)
                 if self.model.analysis.get("notes") else None)
            dropped = bool(a and a.get("dropped"))
            late = bool(a and (_as_float(a.get("late")) > 0.002))
            melody = bool(n.get("is_melody", True))
            mode = self.model.mode_at(n["t"]) if self.model.mode_markers else self.model.kind
            if mode == "chord":
                base = QColor("#2f81f7") if melody else QColor("#58a6ff")
            else:
                base = QColor("#2f81f7") if melody else QColor("#3fb950")
            if dropped:
                base = QColor("#da3633")
            p.setBrush(base)
            if id(n) in sel:
                p.setPen(QPen(QColor("#ffffff"), 2))
            elif r.width() <= 8:
                p.setPen(Qt.NoPen)
            else:
                p.setPen(QPen(QColor("#0b0f16"), 1))
            p.drawRoundedRect(r, 3, 3)

            if n["dur"] > float(self.model.timing.p5 or 4.0):
                maxx = self.x_of(n["t"] + float(self.model.timing.p5 or 4.0))
                p.setPen(QPen(QColor("#ffd866"), 1, Qt.DashLine))
                p.drawLine(int(maxx), r.top(), int(r.right()), r.top())
                p.drawLine(int(maxx), r.bottom(), int(r.right()), r.bottom())

            if dropped:
                p.setPen(QPen(QColor("#ff7b72"), 1))
                for k in range(r.left() - r.height(), r.right(), 7):
                    p.drawLine(k, r.bottom(), k + r.height(), r.top())
                if r.width() > 70:
                    p.setPen(QColor("#ffe3e0"))
                    p.drawText(r, Qt.AlignCenter, "× 同帧异品冲突（将被丢弃）")
                continue
            if late:
                p.setPen(QPen(QColor("#ffa657"), 2))
                p.drawRect(r.adjusted(1, 1, -1, -1))
                if r.width() > 52:
                    p.setPen(QColor("#ffe6c7"))
                    p.drawText(r, Qt.AlignCenter, "! 晚出 +%.0f ms" % (_as_float(a.get("late")) * 1000))
                continue
            if a and a.get("dense"):
                p.setPen(QPen(QColor("#ffa657"), 1, Qt.DashLine))
                p.drawRect(r.adjusted(1, 1, -1, -1))
                if r.width() > 60:
                    p.setPen(QColor("#ffe6c7"))
                    p.drawText(r, Qt.AlignCenter, "! 扫弦过密 <0.32s")

            if self.model.slices and mode != "chord":
                s = self.model.slice_at(n["t"])
                if s is not None:
                    nm = self.model.slices[s].get("name")
                    if nm and self.model.valid_chord(nm):
                        if (n["pitch"] % 12) not in GP.chord_pitch_class_set(nm):
                            p.setPen(QPen(QColor("#e3b341"), 1, Qt.DashLine))
                            p.drawRect(r.adjusted(0, 0, -1, -1))

            if a and a.get("string") is not None and r.width() > 40 and self.row_h >= 11:
                if a.get("fret") is not None:
                    txt = "S%dF%d·P%d" % (int(a["string"]), int(a.get("fret") or 0),
                                          int(a.get("page") or 0) + 1)
                else:
                    txt = "S%d·P%d" % (int(a["string"]), int(a.get("page") or 0) + 1)
                p.setPen(QColor("#0b0f16") if melody else QColor("#06210f"))
                p.drawText(r.adjusted(3, 0, -1, 0), Qt.AlignLeft | Qt.AlignVCenter, txt)

    def _paint_keyboard(self, p, h):
        p.fillRect(0, 0, KEY_W, h, QColor("#0d1117"))
        selected = [n for n in self.selection]
        hl = set()
        if len(selected) == 1 and self.model.mode == "role" \
                and self.model.kind == "fret":
            a = self.model.analysis_for(selected[0])
            if a and a.get("fret") is not None:
                f = int(a["fret"])
                hl = {40 + f, 45 + f, 50 + f, 55 + f, 59 + f, 64 + f}
        black = {1, 3, 6, 8, 10}
        p.setFont(QFont("Microsoft YaHei UI", 6))
        for pitch in self._visible_pitches(h):
            y = self.y_of(pitch)
            pc = pitch % 12
            pressed = (self._pressed_pitch == pitch)
            if pressed:
                p.fillRect(0, int(y), KEY_W - 1, int(self.row_h), QColor("#1f6feb"))
            else:
                p.fillRect(0, int(y), KEY_W - 4, int(self.row_h),
                           QColor("#242c3a") if pc in black else QColor("#dfe7f3"))
            p.setPen(QColor("#ffffff") if pressed else
                     (QColor("#0b0f16") if pc not in black else QColor("#c9d1d9")))
            if pressed:
                p.setFont(QFont("Microsoft YaHei UI", 7, QFont.Bold))
                p.drawText(QRect(2, int(y), KEY_W - 4, int(self.row_h)),
                           Qt.AlignLeft | Qt.AlignVCenter,
                           self._pitch_name(pitch))
                p.setFont(QFont("Microsoft YaHei UI", 6))
            elif pitch % 12 == 0:
                p.drawText(QRect(2, int(y), KEY_W - 8, int(self.row_h)),
                           Qt.AlignLeft | Qt.AlignVCenter,
                           "C%d" % (pitch // 12 - 1))
            if pitch in hl:
                p.setBrush(QColor("#3fb950"))
                p.setPen(Qt.NoPen)
                p.drawRect(KEY_W - 9, int(y + 2), 6, max(2, int(self.row_h) - 4))

    # ── 交互 ───────────────────────────────────────────────────────────
    def eventFilter(self, obj, ev):
        if obj is self.viewport() and ev.type() == QEvent.Wheel:
            self.wheelEvent(ev)
            return True
        return super(PianoRollCanvas, self).eventFilter(obj, ev)

    def _hit_note(self, pos):
        for n in reversed(self.model.notes):
            r = self.note_rect(n)
            if r.contains(pos):
                return n, (r.right() - pos.x()) <= 6
        return None, False

    def mousePressEvent(self, ev):
        x, y = ev.pos().x(), ev.pos().y()
        if ev.button() != Qt.LeftButton:
            return super(PianoRollCanvas, self).mousePressEvent(ev)
        if y < RULER_H:
            self.cursor_t = max(0.0, self.t_of(x))
            edge = None
            if self.model.slices:
                idx = self.model.slice_at(self.cursor_t)
                self.sel_slice = idx
                if idx is not None:
                    s = self.model.slices[idx]
                    x1, x2 = self.x_of(s["t"]), self.x_of(s["end"])
                    if abs(x - x1) <= 6:
                        edge = "start"
                    elif abs(x - x2) <= 6:
                        edge = "end"
                    if edge:
                        self._slice_drag = {"i": idx, "edge": edge,
                                            "t0": s["t"], "t1": s["end"]}
                self.selectionChanged.emit()
                self.viewport().update()
            if edge is None:
                self._seek_drag = True
                self._seek_x = x
                self.scrubBegan.emit()
                self.seekRequested.emit(self.cursor_t)
            return
        if x < KEY_W:
            if y >= RULER_H:
                pitch = _clamp_pitch(self.pitch_of(y))
                self._pressed_pitch = pitch
                self._press_timer.start()
                self.statusChanged.emit("♪ 试听 %s（MIDI %d）"
                                        % (self._pitch_name(pitch), pitch))
                self.viewport().update()
                self.noteClicked.emit({"t": 0.0, "dur": 0.15,
                                       "pitch": pitch, "vel": 100})
            return
        note, near_edge = self._hit_note(ev.pos())
        if note is not None:
            if ev.modifiers() & Qt.ControlModifier:
                if note in self.selection:
                    self.selection.remove(note)
                else:
                    self.selection.append(note)
            else:
                if note not in self.selection:
                    self.selection = [note]
            self.cursor_t = note["t"]
            self._drag = {"mode": "resize" if near_edge else "move",
                          "start": ev.pos(), "notes": list(self.selection),
                          "orig": [(n["t"], n["pitch"], n["dur"]) for n in self.selection],
                          "pushed": False}
            self.selectionChanged.emit()
            self.viewport().update()
            return
        if not (ev.modifiers() & Qt.ControlModifier):
            self.selection = []
        self.cursor_t = max(0.0, self.t_of(x))
        self._rubber = QRect(ev.pos(), ev.pos())
        self._drag = {"mode": "rubber", "start": ev.pos()}
        self.selectionChanged.emit()
        self.viewport().update()

    def mouseMoveEvent(self, ev):
        if self._seek_drag:
            self._seek_x = ev.pos().x()
            self.seekRequested.emit(max(0.0, self.t_of(ev.pos().x())))
            vw = self.viewport().width()
            if self._seek_x > vw - 30:
                self.horizontalScrollBar().setValue(
                    self.horizontalScrollBar().value() + 24)
            elif self._seek_x < KEY_W + 10:
                self.horizontalScrollBar().setValue(
                    max(0, self.horizontalScrollBar().value() - 24))
            self.viewport().update()
            return
        if self._slice_drag is not None:
            self._drag_slice_to(self._slice_drag, self.snap(self.t_of(ev.pos().x())))
            self.viewport().update()
            return
        if self._drag is None:
            self.viewport().setCursor(Qt.SplitHCursor if ev.pos().y() < RULER_H
                                      else Qt.ArrowCursor)
            return
        mode = self._drag["mode"]
        if mode == "rubber":
            self._rubber = QRect(self._drag["start"], ev.pos()).normalized()
            self.viewport().update()
            return
        dx = ev.pos().x() - self._drag["start"].x()
        dy = ev.pos().y() - self._drag["start"].y()
        if not self._drag["pushed"] and (abs(dx) > 2 or abs(dy) > 2):
            self.model.begin_batch()
            self._drag["pushed"] = True
        dt = self.x_to_dt(dx)
        if mode == "move":
            dp = int(round(-dy / self.row_h))
            for n, (t0, p0, _d0) in zip(self._drag["notes"], self._drag["orig"]):
                n["t"] = round(max(0.0, self.snap(t0 + dt)), 6)
                n["pitch"] = _clamp_pitch(p0 + dp)
        else:
            for n, (t0, _p0, d0) in zip(self._drag["notes"], self._drag["orig"]):
                end = self.snap(t0 + d0 + dt)
                n["dur"] = round(max(self.step_sec() or MIN_DUR, end - n["t"]), 6)
        self.viewport().update()
        self.statusChanged.emit(self._drag_text())

    def _drag_text(self):
        if not self.selection:
            return ""
        n = self.selection[0]
        return "拖动中：%d 音 ｜ 示例 t=%.3fs pitch=%d dur=%.3fs" % (
            len(self.selection), n["t"], n["pitch"], n["dur"])

    def x_to_dt(self, dx):
        if self.grid_div > 0:
            t = self.t_of(self._drag["start"].x() + dx)
            return self.snap(t) - self.snap(self.t_of(self._drag["start"].x()))
        return dx / self.pps

    def _drag_slice_to(self, st, t):
        i = st["i"]
        if not (0 <= i < len(self.model.slices)):
            return
        s = self.model.slices[i]
        t = max(0.0, float(t))
        if st["edge"] == "start":
            s["t"] = round(min(t, s["end"] - 0.05), 6)
            for k, (mt, nm) in enumerate(self.model.markers):
                if abs(mt - st["t0"]) <= 1e-6:
                    self.model.markers[k] = (s["t"], nm)
        else:
            s["end"] = round(max(t, s["t"] + 0.05), 6)

    def mouseReleaseEvent(self, ev):
        if self._seek_drag:
            self._seek_drag = False
            self._seek_x = None
            self.scrubEnded.emit()
            self.viewport().update()
            return
        if self._slice_drag is not None:
            self._slice_drag = None
            self.commit()
            return
        if self._drag is None:
            return
        mode = self._drag["mode"]
        if mode == "rubber" and self._rubber is not None:
            r = self._rubber
            picked = [n for n in self.model.notes if r.intersects(self.note_rect(n))]
            merged = list(self.selection)
            seen = {id(n) for n in merged}
            for n in picked:
                if id(n) not in seen:
                    merged.append(n)
                    seen.add(id(n))
            self.selection = merged
            self._rubber = None
            self.selectionChanged.emit()
            self.viewport().update()
        elif mode in ("move", "resize"):
            if self._drag.get("pushed"):
                self.model.end_batch()
                self.model._sort()
            elif len(self.selection) == 1:
                self.noteClicked.emit(self.selection[0])
            self.selection = [n for n in self.selection if n in self.model.notes]
            self.commit()
        self._drag = None

    def contextMenuEvent(self, ev):
        note, _edge = self._hit_note(ev.pos())
        if note is not None:
            if note not in self.selection:
                self.selection = [note]
            self.model.remove_notes(self.selection)
            self.selection = []
            self.commit()
            return
        self.cursor_t = max(0.0, self.snap(self.t_of(ev.pos().x())))
        n = self.model.add_note(self.cursor_t,
                                _clamp_pitch(self.pitch_of(ev.pos().y())),
                                dur=self.step_sec() or 0.25)
        self.selection = [n]
        self.commit()
        self.selectionChanged.emit()
        self.viewport().update()

    def mouseDoubleClickEvent(self, ev):
        if ev.pos().x() < KEY_W or ev.pos().y() < RULER_H:
            return
        t = max(0.0, self.snap(self.t_of(ev.pos().x())))
        pitch = _clamp_pitch(self.pitch_of(ev.pos().y()))
        n = self.model.add_note(t, pitch, dur=self.step_sec() or 0.25)
        self.selection = [n]
        self.cursor_t = t
        self.commit()
        self.selectionChanged.emit()
        self.viewport().update()

    def keyPressEvent(self, ev):
        key = ev.key()
        mods = ev.modifiers()
        if key == Qt.Key_Space:
            self.playPauseRequested.emit()
            ev.accept()
            return
        if key == Qt.Key_Delete or key == Qt.Key_Backspace:
            if self.selection:
                self.model.remove_notes(self.selection)
                self.selection = []
                self.commit()
            return
        if mods & Qt.ControlModifier and key == Qt.Key_Z:
            if self.model.undo():
                self.selection = []
                self.commit(keep_undo=True)
            return
        if (mods & Qt.ControlModifier) and (key == Qt.Key_Y
                                            or (key == Qt.Key_Z and mods & Qt.ShiftModifier)):
            if self.model.redo():
                self.selection = []
                self.commit(keep_undo=True)
            return
        if key in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down) and self.selection:
            step = self.step_sec() or MIN_DUR
            if key == Qt.Key_Left:
                self.model.move_notes(self.selection, -step, 0)
            elif key == Qt.Key_Right:
                self.model.move_notes(self.selection, step, 0)
            elif key == Qt.Key_Up:
                self.model.move_notes(self.selection, 0, 1)
            else:
                self.model.move_notes(self.selection, 0, -1)
            self.commit()
            return
        super(PianoRollCanvas, self).keyPressEvent(ev)

    def _zoom_time(self, factor, x):
        anchor = self.t_of(x)
        self.pps = max(18.0, min(900.0, self.pps * factor))
        self._update_scrollbars()
        newx = KEY_W + anchor * self.pps - self.horizontalScrollBar().value()
        self.horizontalScrollBar().setValue(
            int(self.horizontalScrollBar().value() + (newx - x)))
        self.statusChanged.emit("时间缩放：%.0f 像素/秒" % self.pps)
        self.viewport().update()

    def wheelEvent(self, ev):
        d = ev.angleDelta().y()
        dx = ev.angleDelta().x()
        mods = ev.modifiers()
        x = ev.pos().x()
        if (mods & Qt.ControlModifier) and (mods & Qt.AltModifier):
            self.row_h = max(7.0, min(30.0,
                                      self.row_h * (1.1 if d > 0 else 1 / 1.1)))
            self._update_scrollbars()
            self.viewport().update()
            return
        if (mods & Qt.AltModifier) or ((mods & Qt.ShiftModifier)
                                       and not (mods & Qt.ControlModifier)):
            self._zoom_time(1.15 if d > 0 else 1 / 1.15, x)
            return
        if d == 0 and dx != 0:
            self._zoom_time(1.15 if dx > 0 else 1 / 1.15, x)
            return
        if not d:
            return
        if mods & Qt.ControlModifier:
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - int(d / 120.0 * self.row_h * 3))
            return
        self.horizontalScrollBar().setValue(
            self.horizontalScrollBar().value() - int(d / 120.0 * self.pps * 0.35))
        ev.accept()

    def commit(self, keep_undo=False):
        if self.model.mode == "role":
            self.model.compile()
        self.statusChanged.emit(self.model.status_text())
        self.editCommitted.emit()
        self.viewport().update()

    def zoom_fit(self):
        self._update_scrollbars()
        cw = max(1, self.viewport().width() - KEY_W)
        self.pps = max(45.0, min(900.0, (cw - 60) / self._duration()))
        self._update_scrollbars()
        self.viewport().update()

    def center_on_notes(self):
        """把纵向滚动条对准音符的中位音高。"""
        if not self.model.notes:
            return
        pitches = sorted(int(n["pitch"]) for n in self.model.notes)
        med = pitches[len(pitches) // 2]
        vh = max(1, self.viewport().height() - RULER_H)
        content = (DISPLAY_HI - DISPLAY_LO + 1) * self.row_h
        want = (DISPLAY_HI - med) * self.row_h - vh / 2.0
        self.verticalScrollBar().setValue(
            int(max(0, min(content - vh, want))))
        self.viewport().update()


# ══════════════════════════════════════════════════════════════════════════
#  3. 对话框
# ══════════════════════════════════════════════════════════════════════════
class PianoRollDialog(QDialog):
    """卷帘主窗口：工具栏 + 画布 + 状态栏。"""

    def __init__(self, path=None, role=None, parent=None, on_apply=None):
        super(PianoRollDialog, self).__init__(parent)
        self.setWindowTitle("《异键演奏》钢琴卷帘微调")
        avail = QApplication.desktop().availableGeometry(self)
        self.resize(min(1280, int(avail.width() * 0.94)),
                    min(760, int(avail.height() * 0.88)))
        self.setMinimumSize(min(860, int(avail.width() * 0.70)),
                            min(520, int(avail.height() * 0.60)))
        self.on_apply = on_apply
        self._wav = None
        self._wav_path = None
        self._t0 = None
        self.player = None
        self._QMediaContent = None
        self._audio_path = None
        self._audio_dirty = True
        self._audio_duration = 0.0
        self._playing = False
        self._paused = False
        self._want_play = False
        self._pending_seek_ms = 0
        self._init_player()
        self.model = PianoRollModel(path, role=role)
        self.canvas = PianoRollCanvas(self.model, self)
        self.canvas.selectionChanged.connect(self._on_selection)
        self.canvas.statusChanged.connect(self._set_status)
        self.canvas.editCommitted.connect(self._refresh_buttons)
        self.canvas.editCommitted.connect(self._on_edit_committed)
        self.canvas.noteClicked.connect(self._preview_note)
        self.canvas.seekRequested.connect(self.seek_to)
        self.canvas.playPauseRequested.connect(self.toggle_play)
        self.canvas.scrubBegan.connect(self._begin_scrub)
        self.canvas.scrubEnded.connect(self._end_scrub)
        self._build_ui()
        self._space_sc = QShortcut(QKeySequence(Qt.Key_Space), self)
        self._space_sc.setContext(Qt.WindowShortcut)
        self._space_sc.activated.connect(self.toggle_play)
        self.canvas.setFocus()
        self._refresh_buttons()
        self._sync_play_button()
        self._set_status(self.model.status_text())
        self._role_mode = bool(role)

    def _init_player(self):
        try:
            from PyQt5.QtMultimedia import QMediaContent, QMediaPlayer
            self._QMediaContent = QMediaContent
            self.player = QMediaPlayer(self)
            vol = int(PC.load().get("roll_volume", 35))
            self.player.setVolume(max(0, min(100, vol)))
            self.player.mediaStatusChanged.connect(self._on_media_status)
        except Exception:
            self.player = None
            
    def _on_volume_changed(self, val):
        v = max(0, min(100, int(val)))
        if hasattr(self, "lbl_vol"):
            self.lbl_vol.setText("%d%%" % v)
        if self.player is not None:
            try:
                self.player.setVolume(v)
            except Exception:
                pass
        try:
            PC.save({"roll_volume": v})
        except Exception:
            pass

    # ── UI ─────────────────────────────────────────────────────────────
    def _btn(self, text, tip, slot, obj=None):
        b = QPushButton(text)
        b.setToolTip(tip)
        if obj:
            b.setObjectName(obj)
        b.clicked.connect(slot)
        return b

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        row = QHBoxLayout()
        self.btn_open = self._btn("打开 MIDI…", "打开任意阶段 MIDI", self.open_file)
        self.btn_save = self._btn("保存覆盖原 MIDI", "写回原文件，并同步同名 .json",
                                  self.save_over)
        self.btn_save_as = self._btn("另存为新 MIDI", "另存为；若同目录有同名 .json 也一并写",
                                     self.save_as)
        self.btn_undo = self._btn("↶ 撤销", "Ctrl+Z", self.undo)
        self.btn_redo = self._btn("↷ 重做", "Ctrl+Y", self.redo)
        for b in (self.btn_open, self.btn_save, self.btn_save_as, self.btn_undo, self.btn_redo):
            row.addWidget(b)
        row.addStretch(1)
        self.btn_fix = self._btn("归位 40~88", "调用 pipeline.fix_pitch_range 做音域归位",
                                 self.fix_range)
        self.cmb_inst = QComboBox()
        self.cmb_inst.addItem("木吉他", "acoustic")
        self.cmb_inst.addItem("电吉他", "electric")
        self.cmb_inst.setMinimumWidth(96)
        self.btn_restore = self._btn("恢复默认编配", "清除手动微调，重新走 adapt_song",
                                     self.restore_auto)
        self.btn_apply = self._btn("应用微调", "应用微调并更新演奏台：直通重编后把时间轴送回演奏页",
                                   self.apply_custom, obj="primary")
        for b in (self.btn_fix, self.cmb_inst, self.btn_restore, self.btn_apply):
            row.addWidget(b)

        tb_widget = QWidget()
        tb_lay = QVBoxLayout(tb_widget)
        tb_lay.setContentsMargins(0, 0, 0, 0)
        tb_lay.setSpacing(6)
        tb_lay.addLayout(row)

        row2 = QHBoxLayout()
        self.btn_play = self._btn("▶ 试听", "空格：播放/暂停；也可用底部滑块或顶部节拍表拖动", self.preview)
        self.btn_stop = self._btn("⏹ 停止", "停止试听", self.stop_preview)
        row2.addWidget(self.btn_play)
        row2.addWidget(self.btn_stop)
        row2.addWidget(QLabel("音量:"))
        self.sld_vol = QSlider(Qt.Horizontal)
        self.sld_vol.setRange(0, 100)
        _init_vol = int(PC.load().get("roll_volume", 35))
        self.sld_vol.setValue(max(0, min(100, _init_vol)))
        self.sld_vol.setFixedWidth(80)
        self.sld_vol.setToolTip("调节试听音量")
        self.lbl_vol = QLabel("%d%%" % self.sld_vol.value())
        self.lbl_vol.setMinimumWidth(34)
        self.sld_vol.valueChanged.connect(self._on_volume_changed)
        row2.addWidget(self.sld_vol)
        row2.addWidget(self.lbl_vol)
        self.lbl_mode = QLabel("")
        row2.addWidget(self.lbl_mode)
        self.cmb_track = QComboBox()
        self.cmb_track.setMinimumWidth(200)
        self.cmb_track.currentIndexChanged.connect(self._switch_track)
        self.lbl_track = QLabel("轨道：")
        row2.addWidget(self.lbl_track)
        row2.addWidget(self.cmb_track)
        row2.addWidget(QLabel("网格吸附："))
        self.cmb_grid = QComboBox()
        for text, div in (("不吸附", 0), ("1/2 拍", 2), ("1/4 拍", 4), ("1/8 拍", 8)):
            self.cmb_grid.addItem(text, div)
        self.cmb_grid.setCurrentIndex(2)
        self.cmb_grid.currentIndexChanged.connect(self._grid_changed)
        row2.addWidget(self.cmb_grid)
        self.chk_roll_arp = QCheckBox("和弦内音琶音")
        self.chk_roll_rhythm = QCheckBox("和弦扫弦律动")
        _arp, _rhythm = self.model.roll_flags()
        self.chk_roll_arp.setChecked(_arp)
        self.chk_roll_rhythm.setChecked(_rhythm)
        self.chk_roll_arp.toggled.connect(self._on_roll_flags_changed)
        self.chk_roll_rhythm.toggled.connect(self._on_roll_flags_changed)
        self.chk_strum_mode = QCheckBox("2P柱式改用鼠标扫弦")
        self.chk_strum_mode.setChecked(self.model.roll_strum_mode())
        self.chk_strum_mode.toggled.connect(self._on_strum_mode_changed)
        row2.addWidget(self.chk_roll_arp)
        row2.addWidget(self.chk_roll_rhythm)
        row2.addWidget(self.chk_strum_mode)
        self.chk_phys_preview = QCheckBox("物理指法与晚出预览（按1P主音）")
        self.chk_phys_preview.setToolTip(
            "模式 A 下勾选：按 1P 主音真实编译，显示 S弦F品·P页 角标、丢弃与晚出预警")
        self.chk_phys_preview.toggled.connect(self._toggle_phys_preview)
        row2.addWidget(self.chk_phys_preview)
        row2.addStretch(1)
        tb_lay.addLayout(row2)

        row_chord = QHBoxLayout()
        self.lbl_chord = QLabel("当前和弦：")
        self.cmb_chord = QComboBox()
        for name in ALL_CHORDS:
            self.cmb_chord.addItem(name, name)
        self.cmb_chord.setMinimumWidth(90)
        self.btn_chord = self._btn("应用到选中切片", "只允许 24 个游戏内和弦", self.apply_chord)
        self.btn_strum_u = self._btn("＋左键上扫(强)", "已停用：和弦伴奏一律右键下扫", self.add_strum)
        self.btn_strum_u.setVisible(False)
        self.btn_strum_d = self._btn("＋添加扫弦（右键下扫）", "生成 ≥3 音同帧簇，力度 75，右键下扫",
                                     lambda: self.add_strum(False))
        self.cmb_string = QComboBox()
        self.cmb_string.addItems([str(i) for i in range(1, 7)])
        self.btn_pluck = self._btn("＋单拨", "地雷弦会被锁死拒绝", self.add_pluck)
        for w in (self.lbl_chord, self.cmb_chord, self.btn_chord,
                  self.btn_strum_u, self.btn_strum_d, self.cmb_string, self.btn_pluck):
            row_chord.addWidget(w)
        row_chord.addStretch(1)
        tb_lay.addLayout(row_chord)

        tb_scroll = QScrollArea()
        tb_scroll.setFrameShape(QFrame.NoFrame)
        tb_scroll.setWidgetResizable(True)
        tb_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        tb_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        tb_scroll.setWidget(tb_widget)
        tb_scroll.setMinimumWidth(360)
        tb_scroll.setFixedHeight(tb_widget.sizeHint().height() + 18)
        root.addWidget(tb_scroll)

        root.addWidget(self.canvas, 1)
        self.canvas.setToolTip(
            "空格 = 播放/暂停｜滚轮 = 前后滑动｜Ctrl+滚轮 = 上下滑动｜"
            "Alt+滚轮（或 Shift+滚轮）= 时间缩放｜Ctrl+Alt+滚轮 = 行高缩放｜拖动顶部节拍表 = 定位播放头")

        row_play = QHBoxLayout()
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.setValue(0)
        self.slider.setToolTip("拖动定位播放位置（松手后从该处继续）")
        self.slider.sliderPressed.connect(self._on_slider_pressed)
        self.slider.sliderMoved.connect(self._on_slider_moved)
        self.slider.sliderReleased.connect(self._on_slider_released)
        self.lbl_time = QLabel("00:00.0 / 00:00.0")
        self.lbl_time.setObjectName("sub")
        self.lbl_time.setMinimumWidth(130)
        row_play.addWidget(self.slider, 1)
        row_play.addWidget(self.lbl_time)
        root.addLayout(row_play)

        status_row = QHBoxLayout()
        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("sub")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        status_row.addWidget(self.lbl_status, 1)
        self.lbl_legend = QLabel("")
        self.lbl_legend.setObjectName("sub")
        self.lbl_legend.setTextFormat(Qt.RichText)
        self.lbl_legend.setWordWrap(False)
        self.lbl_legend.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        status_row.addWidget(self.lbl_legend, 0)
        root.addLayout(status_row)

        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        QTimer.singleShot(0, self.canvas.center_on_notes)

        self._fill_tracks()
        self._chord_widgets_visible(self.model.kind == "chord")

    def _fill_tracks(self):
        self.cmb_track.blockSignals(True)
        self.cmb_track.clear()
        if self.model.mode == "midi":
            self.lbl_track.setText("轨道：")
            for i, tr in enumerate(self.model.tracks):
                self.cmb_track.addItem("%d. %s（%d 音）" % (i + 1, tr.name, len(tr.notes)), i)
            self.cmb_track.setCurrentIndex(self.model.track_idx)
            self.cmb_track.setEnabled(True)
        else:
            self.lbl_track.setText("演奏模式：")
            for role in (MC.ROLE_SOLO, MC.ROLE_DUET_LEAD,
                         MC.ROLE_DUET_RHYTHM_FRET, MC.ROLE_DUET_RHYTHM):
                self.cmb_track.addItem(PLAY_ROLE_LABELS[role], role)
            idx = self.cmb_track.findData(self.model.role)
            self.cmb_track.setCurrentIndex(max(0, idx))
            self.cmb_track.setEnabled(True)
        self.cmb_track.blockSignals(False)
        if self.model.mode == "role":
            role_cn = PLAY_ROLE_LABELS.get(self.model.role) or (self.model.tl or {}).get("role_cn") or ""
            mode_tag = "智能混切" if self.model.role in (MC.ROLE_DUET_RHYTHM, MC.ROLE_DUET_RHYTHM_FRET) else "品格"
            self.lbl_mode.setText("模式 B：%s（%s）" % (role_cn, mode_tag))
        else:
            self.lbl_mode.setText("模式 A：通用 MIDI（%s）" % os.path.basename(self.model.path or ""))
        self.btn_fix.setVisible(self.model.mode == "midi")
        self.cmb_inst.setVisible(self.model.mode == "midi")
        try:
            self.chk_phys_preview.setVisible(self.model.mode == "midi")
        except Exception:
            pass
        self.btn_restore.setVisible(self.model.mode == "role")
        self.btn_apply.setVisible(self.model.mode == "role")
        self._update_legend()
        self._update_switch_availability()
        for w in (self.chk_roll_arp, self.chk_roll_rhythm, self.chk_strum_mode):
            w.setVisible(self.model.mode == "role")

    def _chord_widgets_visible(self, on):
        show_bar = bool(on) and self.model.mode == "role"
        for w in (self.lbl_chord, self.cmb_chord, self.btn_chord,
                  self.cmb_string, self.btn_pluck):
            w.setVisible(show_bar)
        # 角色 3（2P 琶音/根音）全程纯拨弦 0 鼠标扫弦，仅在角色 4（柱式/扫弦）下显示添加扫弦按钮
        self.btn_strum_d.setVisible(show_bar and self.model.role == MC.ROLE_DUET_RHYTHM)
        self.btn_strum_u.setVisible(False)

    def _on_roll_flags_changed(self, _checked=None):
        """卷帘内切换琶音 / 扫弦律动：写回配置并实时重算 2P 与 Solo。"""
        if self.model.mode != "role":
            return
        arp = self.chk_roll_arp.isChecked()
        rhythm = self.chk_roll_rhythm.isChecked()
        if (arp, rhythm) == self.model.roll_flags():
            return
        self.model._snapshot()
        self.model.rebuild_for_flags(arp, rhythm)
        self.canvas.selection = []
        self._chord_widgets_visible(self.model.kind == "chord")
        self._fill_tracks()
        self._refresh_buttons()
        self.canvas.viewport().update()
        self._invalidate_audio_preview()
        self._notify_parent_flags(arp, rhythm)

    def _on_strum_mode_changed(self, _checked=None):
        """卷帘内切换"柱式 / 鼠标扫弦"：写回配置、重算 2P，并作废旧试听音频。"""
        if self.model.mode != "role":
            return
        on = self.chk_strum_mode.isChecked()
        if on == self.model.roll_strum_mode():
            return
        self.model._snapshot()
        self.model.rebuild_for_strum_mode(on)
        self.canvas.selection = []
        self._chord_widgets_visible(self.model.kind == "chord")
        self._fill_tracks()
        self._refresh_buttons()
        self.canvas.viewport().update()
        self._invalidate_audio_preview()
        par = self.parent()
        box = getattr(par, "chk_strum_mode", None)
        if box is not None:
            try:
                box.blockSignals(True)
                box.setChecked(bool(on))
                box.blockSignals(False)
            except Exception:
                pass

    def _notify_parent_flags(self, arp, rhythm):
        par = self.parent()
        for name, value in (("chk_arpeggio", bool(arp)),
                            ("chk_chord_rhythm", bool(rhythm))):
            box = getattr(par, name, None)
            if box is None:
                continue
            try:
                box.blockSignals(True)
                box.setChecked(value)
                box.blockSignals(False)
            except Exception:
                pass
        fn = getattr(par, "on_roll_flags_changed", None)
        if callable(fn):
            try:
                fn(arp, rhythm)
            except Exception:
                pass

    def _update_legend(self):
        """按当前角色刷新颜色图例（右下角常驻）。"""
        blue = "<span style='color:#58a6ff'>■</span>"
        green = "<span style='color:#3fb950'>■</span>"
        red = "<span style='color:#ff7b72'>■</span>"
        amber = "<span style='color:#ffa657'>□</span>"
        role = self.model.role if self.model.mode == "role" else None
        if role == MC.ROLE_SOLO:
            body = "%s蓝=主旋律  %s绿=伴奏  %s红=冲突不可弹  %s橙框=晚出" % (
                blue, green, red, amber)
        elif role in (MC.ROLE_DUET_RHYTHM, MC.ROLE_DUET_RHYTHM_FRET):
            body = "%s蓝=和弦模式  %s绿=品格模式(Z混切)  %s红=冲突不可弹  %s橙框=晚出" % (
                blue, green, red, amber)
        else:
            body = "%s蓝=主旋律  %s红=冲突不可弹  %s橙框=晚出" % (blue, red, amber)
        try:
            self.lbl_legend.setText("图例：" + body)
        except Exception:
            pass

    def _update_switch_availability(self):
        """按当前角色自动启用/置灰三个伴奏开关，并给置灰控件加明显半透明样式。"""
        role = self.model.role if self.model.mode == "role" else None
        arp_on = role in (MC.ROLE_SOLO, MC.ROLE_DUET_RHYTHM_FRET)
        rhy_on = role == MC.ROLE_DUET_RHYTHM
        str_on = role == MC.ROLE_DUET_RHYTHM
        specs = (
            (getattr(self, "chk_roll_arp", None), arp_on,
             "本角色有效：1P 独奏的句间点缀 / 2P 琶音的顺拨分解",
             "当前角色不使用「和弦内音琶音」（仅 1P 独奏与 2P 琶音/根音有效）"),
            (getattr(self, "chk_roll_rhythm", None), rhy_on,
             "本角色有效：2P 柱式/扫弦的整拍律动（和弦段与品格段同步生效）",
             "当前角色不使用「和弦扫弦律动」（仅 2P 柱式/扫弦有效）"),
            (getattr(self, "chk_strum_mode", None), str_on,
             "本角色有效：2P 柱式/扫弦的发声方式切换",
             "当前角色不使用「柱式/扫弦」开关（仅 2P 柱式/扫弦有效）"),
        )
        for w, on, tip_on, tip_off in specs:
            if w is None:
                continue
            try:
                w.setEnabled(bool(on))
                w.setStyleSheet("" if on else "color: #475569;")
                w.setToolTip(tip_on if on else tip_off)
            except Exception:
                pass

    def _refresh_buttons(self):
        self.btn_undo.setEnabled(bool(self.model._undo))
        self.btn_redo.setEnabled(bool(self.model._redo))
        role = getattr(self.model, "role", None)
        mode_a = role is None
        self.btn_open.setVisible(False)
        self.btn_save.setVisible(bool(mode_a))
        self.btn_save_as.setVisible(True)
        self._update_switch_availability()
        if self.model.kind == "chord":
            name = self.model.slice_at(self.canvas.cursor_t) if self.model.slices else None
            if name is not None:
                self.cmb_chord.setCurrentText(self.model.slices[name].get("name") or "C")
            self._update_mine_label()
        self._set_status(self.model.status_text())
        self.canvas.viewport().update()

    def _update_mine_label(self):
        name = self.cmb_chord.currentText()
        mine = self.model.mine_strings(name)
        self.cmb_string.setToolTip("地雷弦（禁止单拨）：%s" % ("、".join(map(str, mine)) or "无"))
        self.btn_pluck.setToolTip("地雷弦会被拒绝：%s" % ("、".join(map(str, mine)) or "无"))

    def _on_selection(self):
        self._refresh_buttons()
        if self.model.slices:
            idx = self.model.slice_at(self.canvas.cursor_t)
            if idx is not None:
                self.cmb_chord.setCurrentText(self.model.slices[idx].get("name") or "C")
        self._update_mine_label()

    def _set_status(self, text):
        self.lbl_status.setText(text)

    def _timing_text(self):
        try:
            t = MC.Timing()
            return ("时序：同页换格 %.0fms · 翻1页 %.0fms"
                    % (t.switch_span(0, False, True) * 1000,
                       t.switch_span(1, True, True) * 1000))
        except Exception:
            return ""

    def _check_can_save_or_apply(self, show_dialog=True):
        """红色非法音符硬拦截；返回 True 才允许保存/应用。"""
        if self.model.mode != "role" and not self.chk_phys_preview.isChecked():
            return True
        if self.model.mode == "role":
            _ok, bad = self.model.save_guard()
        else:
            if self.chk_phys_preview.isChecked():
                self._compile_phys()
            bad = self.model.invalid_notes()
        self.last_invalid = bad
        if not bad:
            return True
        first = float(bad[0][0].get("t") or 0.0)
        if show_dialog:
            QMessageBox.warning(
                self, "存在红色非法音符",
                "当前谱面存在 %d 颗红色冲突/不可演奏音符"
                "（最早位于 %.2f 秒），请先删除或移至安全音高后再保存！"
                % (len(bad), first))
            try:
                hb = self.canvas.horizontalScrollBar()
                hb.setValue(max(0, int(first * self.canvas.pps
                                        - self.canvas.viewport().width() / 2)))
            except Exception:
                pass
        return False

    def on_timing_updated(self):
        """高级时间参数保存/切换预设后，卷帘实时重算并刷新状态。"""
        self.model.compile()
        self.canvas.viewport().update()
        self._refresh_buttons()
        self._set_status(self.model.status_text() + " ｜ " + self._timing_text())

    def undo(self):
        if self.model.undo():
            self.canvas.selection = []
            self.canvas.editCommitted.emit()
            self.canvas.viewport().update()

    def redo(self):
        if self.model.redo():
            self.canvas.selection = []
            self.canvas.editCommitted.emit()
            self.canvas.viewport().update()

    def _grid_changed(self, _idx):
        self.canvas.grid_div = int(self.cmb_grid.currentData() or 0)

    def _switch_track(self, _idx):
        if self.model.mode == "role":
            role = self.cmb_track.currentData()
            if role and role != self.model.role and self.model.switch_role(role):
                self.canvas.selection = []
                self._chord_widgets_visible(self.model.kind == "chord")
                self._fill_tracks()
                self._refresh_buttons()
                self.canvas.center_on_notes()
                self.canvas.viewport().update()
                self._invalidate_audio_preview()
            return
        if self.model.mode != "midi":
            return
        self.model.select_track(int(self.cmb_track.currentData() or 0))
        self.canvas.selection = []
        self._refresh_buttons()

    # ── 动作 ───────────────────────────────────────────────────────────
    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "打开 MIDI", _out_dir(),
                                              "MIDI (*.mid *.midi)")
        if not path:
            return
        role = None
        if self.model.mode == "role":
            from PyQt5.QtWidgets import QInputDialog
            roles_order = (MC.ROLE_SOLO, MC.ROLE_DUET_LEAD,
                           MC.ROLE_DUET_RHYTHM_FRET, MC.ROLE_DUET_RHYTHM)
            items = [PLAY_ROLE_LABELS[r] for r in roles_order]
            cn, ok = QInputDialog.getItem(self, "选择角色", "按哪个角色打开？", items, 0, False)
            if not ok:
                return
            role = roles_order[items.index(cn)]
        try:
            self.model.song = None
            self.model.role_notes = {}
            self.model.load(path, role=role)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "打开失败", "这个文件打不开：\n%s\n\n%s"
                                % (path, exc))
            return
        self.canvas.selection = []
        self._fill_tracks()
        self._chord_widgets_visible(self.model.kind == "chord")
        if self.model.mode == "midi" and self.chk_phys_preview.isChecked():
            self.model.phys_enabled = True
            self._compile_phys()
        self.canvas.zoom_fit()
        self.canvas.center_on_notes()
        self._refresh_buttons()

    def save_over(self):
        if not self._check_can_save_or_apply():
            return
        if not self.model.path:
            return self.save_as()
        if QMessageBox.question(self, "保存覆盖",
                                "直接覆盖原文件？\n%s" % self.model.path) != QMessageBox.Yes:
            return
        self._do_save(self.model.path)

    def save_as(self, path=None):
        """另存为新 MIDI；`path` 为空时弹文件对话框。"""
        if not self._check_can_save_or_apply():
            return None
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "另存为 MIDI", _out_dir(),
                                                  "MIDI (*.mid)")
        if path:
            self._do_save(path)
            return path
        return None

    def _do_save(self, path):
        if not self._check_can_save_or_apply():
            return
        try:
            got = self.model.save(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        msg = "已保存：%s" % got["midi"]
        if got.get("json"):
            msg += "\n同名 JSON 已同步：%s" % got["json"]
        self._set_status(msg.replace("\n", " ｜ "))
        if self.parent() is not None and hasattr(self.parent(), "_append_log"):
            try:
                self.parent()._append_log(msg.replace("\n", " ｜ "))
            except Exception:
                pass
        # v1.2：另存到 out/ 之后，让演奏页左侧「本地 MIDI 曲库」自动刷新
        if self.parent() is not None and hasattr(self.parent(), "refresh_midi_list"):
            try:
                self.parent().refresh_midi_list()
            except Exception:
                pass

    def fix_range(self):
        try:
            mode = self.cmb_inst.currentData() or "acoustic"
            st = self.model.fix_range(instrument_mode=mode)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "归位失败", str(exc))
            return
        self.canvas.selection = []
        self.canvas.commit()
        self._set_status("已按%s规则归位：整轨 -%d 半音，孤立修正 %d，边界折断 %d"
                         % ("木吉他" if mode == "acoustic" else "电吉他",
                            st.get("global_down_octaves", 0) * 12,
                            st.get("isolated_fixed", 0),
                            st.get("boundary_folded", 0)))

    def restore_auto(self):
        if self.model.mode != "role":
            return
        if QMessageBox.question(self, "恢复自动编配",
                                "清除手动微调、恢复自动生成？") != QMessageBox.Yes:
            return
        self.model.restore_auto()
        par = self.parent()
        if par is not None and hasattr(par, "custom_role_notes"):
            try:
                par.custom_role_notes = {}
            except Exception:
                pass
        self.canvas.selection = []
        self.canvas.commit()
        self._refresh_buttons()
        self._invalidate_audio_preview()

    def apply_custom(self):
        if self.model.mode != "role":
            QMessageBox.information(self, "仅角色模式", "请从演奏页打开钢琴卷帘。")
            return
        if not self._check_can_save_or_apply():
            return
        tl = self.model.compile()
        if not tl:
            return
        if self.on_apply:
            self.model.remember_role_notes()
            try:
                self.on_apply(tl, dict(self.model.role_notes))
            except TypeError:
                self.on_apply(tl)
        QMessageBox.information(self, "已应用",
                                "已直通重编并更新演奏台：\n%d 条硬件动作"
                                % len(tl.get("events") or []))
        self._refresh_buttons()

    def apply_chord(self):
        idx = self.model.slice_at(self.canvas.cursor_t)
        if idx is None:
            QMessageBox.information(self, "没选切片", "先在顶部和弦切片栏点一个切片。")
            return
        name = self.cmb_chord.currentText()
        if not self.model.set_slice_chord(idx, name):
            QMessageBox.warning(self, "和弦不可用", "只能选游戏里存在的 24 个和弦。")
            return
        self.canvas.commit()

    def add_strum(self, up):
        t = self.canvas.cursor_t
        got = self.model.add_strum(t, up=up)
        if not got:
            QMessageBox.warning(self, "无法扫弦", "该时刻没有有效和弦（先选切片/改和弦）。")
            return
        self.canvas.selection = got
        self.canvas.commit()

    def add_pluck(self):
        t = self.canvas.cursor_t
        s = int(self.cmb_string.currentText())
        got = self.model.add_pluck(t, s)
        if got is None:
            QMessageBox.warning(self, "该弦被锁死",
                                "第 %d 弦是地雷弦（含小二度错音），不能单拨。" % s)
            return
        self.canvas.selection = [got]
        self.canvas.commit()

    # ── 试听 ───────────────────────────────────────────────────────────
    def _play_wav(self, data):
        import tempfile
        import winsound
        self._stop_wav()
        fd, path = tempfile.mkstemp(prefix="yjyz_", suffix=".wav")
        os.close(fd)
        try:
            with open(path, "wb") as f:
                f.write(data)
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            self._wav_path = path
            self._wav = data
        except Exception:
            try:
                os.remove(path)
            except Exception:
                pass
            raise
        return path

    def _stop_wav(self):
        import winsound
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass
        path, self._wav_path = self._wav_path, None
        if path:
            try:
                os.remove(path)
            except Exception:
                pass

    @staticmethod
    def _fmt_time(t):
        t = max(0.0, float(t or 0.0))
        return "%02d:%04.1f" % (int(t // 60), t % 60)

    def _notes_end(self):
        return max((float(n["t"]) + float(n["dur"]) for n in self.model.notes),
                   default=0.0)

    def _preview_note(self, note):
        try:
            name = PianoRollCanvas._pitch_name(int(note["pitch"]))
            if self._playing:
                self._set_status("♪ %s（MIDI %d）—— 正在整谱播放，单击试听已跳过"
                                 % (name, int(note["pitch"])))
                return
            one = dict(note)
            one["dur"] = 0.15
            one["t"] = 0.0
            data = PianoRollModel.synthesize_wav([one], self.model.bpm,
                                                 limit_sec=0.22)
            self._play_wav(data)
            self._set_status("♪ %s（MIDI %d）—— 150ms 试听"
                             % (name, int(note["pitch"])))
        except Exception:
            pass

    # ── 整谱播放（QMediaPlayer，可拖动进度）────────────────────────────
    def _cleanup_audio(self):
        try:
            if self.player is not None:
                self.player.stop()
                if self._QMediaContent is not None:
                    self.player.setMedia(self._QMediaContent())
        except Exception:
            pass
        path, self._audio_path = self._audio_path, None
        if path:
            try:
                os.remove(path)
            except Exception:
                pass

    def preview_wav_bytes(self):
        """当前画布音符对应的试听 WAV 字节流（不落盘）。"""
        return PianoRollModel.synthesize_wav(self.model.notes, self.model.bpm)

    def _build_audio(self):
        if self.player is None or self._QMediaContent is None:
            return False
        import tempfile
        data = self.preview_wav_bytes()
        self._cleanup_audio()
        try:
            if self.player is not None and self._QMediaContent is not None:
                self.player.setMedia(self._QMediaContent())
        except Exception:
            pass
        fd, path = tempfile.mkstemp(prefix="yjyz_full_", suffix=".wav")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(data)
        self._audio_path = path
        self._audio_duration = self._notes_end()
        self.player.setMedia(self._QMediaContent(QUrl.fromLocalFile(path)))
        self._audio_dirty = False
        self._update_progress(0.0)
        return True

    def _toggle_phys_preview(self, on):
        self.model.phys_enabled = bool(on) and self.model.mode == "midi"
        if self.model.phys_enabled:
            self._compile_phys()
        else:
            self.model.set_analysis(None)
            self._set_status(self.model.status_text())
        self.canvas.selection = []
        self.canvas.viewport().update()

    def _compile_phys(self):
        if self.model.mode != "midi" or not self.model.path:
            return
        try:
            tl = MC.compile_custom_role(self.model.path, MC.ROLE_DUET_LEAD,
                                        self.model.notes,
                                        timing=self.model.timing,
                                        start_offset=0.0)
            self.model.set_analysis(tl.get("analysis"))
            st = tl.get("stats") or {}
            self._set_status("物理预览（1P 主音）：发声 %d ｜ 丢弃 %d ｜ 晚出 %d"
                             % (int(st.get("voices") or 0),
                                int(st.get("dropped") or 0),
                                int(st.get("late") or 0)))
        except Exception as exc:  # noqa: BLE001
            self.model.set_analysis(None)
            self._set_status("物理预览失败：%s" % exc)
        self.canvas.viewport().update()

    def _on_media_status(self, status):
        try:
            from PyQt5.QtMultimedia import QMediaPlayer
            if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia) \
                    and self._want_play:
                self._apply_pending_play()
        except Exception:
            pass

    def _apply_pending_play(self):
        self._want_play = False
        try:
            self.player.setPosition(int(self._pending_seek_ms))
            self.player.play()
            self._playing = True
            self._paused = False
            self._timer.start()
            self._sync_play_button()
        except Exception as exc:  # noqa: BLE001
            self._set_status("试听失败：%s" % exc)
            self._playing = False

    def _begin_scrub(self):
        self._scrub_was_playing = self._playing
        if self._playing:
            self.pause_preview()
            self._set_status("拖动定位中…松手后继续播放")

    def _end_scrub(self):
        if getattr(self, "_scrub_was_playing", False):
            self._scrub_was_playing = False
            self.preview()

    def toggle_play(self):
        if self._playing:
            self.pause_preview()
        elif self._paused:
            self.preview()
        else:
            self.preview()

    def pause_preview(self):
        if self.player is not None and self._playing:
            try:
                self.player.pause()
                pos = self.player.position() / 1000.0
                self.canvas.playhead = pos
                self._update_progress(pos)
            except Exception:
                pass
            self._playing = False
            self._paused = True
            self._timer.stop()
            self._sync_play_button()
            self._set_status("已暂停（空格或 ▶ 继续）")
            self.canvas.viewport().update()
        else:
            self.stop_preview()

    def _sync_play_button(self):
        if not hasattr(self, "btn_play"):
            return
        if self._playing:
            self.btn_play.setText("⏸ 暂停（空格）")
        elif self._paused:
            self.btn_play.setText("▶ 继续（空格）")
        else:
            self.btn_play.setText("▶ 试听当前谱面")

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Space and ev.modifiers() == Qt.NoModifier:
            self.toggle_play()
            ev.accept()
            return
        super(PianoRollDialog, self).keyPressEvent(ev)

    def preview(self):
        if not self.model.notes:
            return
        if self._paused and self.player is not None and not self._audio_dirty:
            try:
                self.player.play()
                self._playing = True
                self._paused = False
                self._timer.start()
                self._sync_play_button()
                self._set_status("继续播放（空格暂停）")
                return
            except Exception:
                pass
        start = max(0.0, float(self.canvas.playhead or 0.0))
        if self.player is None:
            try:
                data = PianoRollModel.synthesize_wav(self.model.notes, self.model.bpm)
                self._play_wav(data)
                self._t0 = time.perf_counter()
                self._audio_duration = self._notes_end()
                self._play_from = start
                self._playing = True
                self._paused = False
                self._timer.start()
                self._sync_play_button()
                self._set_status("试听中（无进度拖动）…")
            except Exception as exc:  # noqa: BLE001
                self._set_status("试听失败：%s" % exc)
            return
        try:
            if self._audio_dirty or not self._audio_path \
                    or not os.path.isfile(self._audio_path):
                if not self._build_audio():
                    return
            dur = max(0.05, self._audio_duration or self._notes_end())
            if start >= dur - 0.10:
                start = 0.0
            start = min(start, max(0.0, dur - 0.05))
            self._pending_seek_ms = int(start * 1000)
            self._want_play = True
            from PyQt5.QtMultimedia import QMediaPlayer
            if self.player.mediaStatus() in (QMediaPlayer.LoadedMedia,
                                             QMediaPlayer.BufferedMedia):
                self._apply_pending_play()
            self._update_progress(start)
            self._set_status("试听中…（%d 音，%.1f 秒；拖底部滑块可跳转）"
                             % (len(self.model.notes), dur))
        except Exception as exc:  # noqa: BLE001
            self._set_status("试听失败：%s" % exc)
            self._playing = False

    def _invalidate_audio_preview(self):
        """角色或开关变化后作废旧试听音频，下次播放强制重新合成。"""
        try:
            self.stop_preview()
            self._cleanup_audio()
        except Exception:
            pass
        self._audio_dirty = True
        self._audio_duration = 0.0
        self._pending_seek_ms = 0
        self._want_play = False

    def stop_preview(self):
        self._playing = False
        self._paused = False
        self._want_play = False
        self._timer.stop()
        try:
            if self.player is not None:
                self.player.stop()
                if self._QMediaContent is not None:
                    self.player.setMedia(self._QMediaContent())
            else:
                self._stop_wav()
        except Exception:
            pass
        self._sync_play_button()
        self.canvas.viewport().update()

    def seek_to(self, t):
        dur = self._audio_duration or self._notes_end()
        t = max(0.0, min(float(t), max(0.05, dur)))
        self.canvas.playhead = t
        self._update_progress(t)
        if (self._playing or self._paused) and self.player is not None:
            try:
                self.player.setPosition(int(t * 1000))
            except Exception:
                pass
        self.canvas.viewport().update()

    def _update_progress(self, t):
        dur = self._audio_duration or self._notes_end()
        self.lbl_time.setText("%s / %s" % (self._fmt_time(t), self._fmt_time(dur)))
        if dur > 1e-6:
            val = int(max(0.0, min(1.0, t / dur)) * 1000)
            self.slider.blockSignals(True)
            self.slider.setValue(val)
            self.slider.blockSignals(False)

    def _on_slider_pressed(self):
        self._slider_was_playing = self._playing
        if self._playing:
            self.pause_preview()
            self._set_status("拖动进度中…松手后继续播放")

    def _on_slider_moved(self, value):
        dur = self._audio_duration or self._notes_end()
        t = dur * float(value) / 1000.0
        self.canvas.playhead = t
        self.lbl_time.setText("%s / %s" % (self._fmt_time(t), self._fmt_time(dur)))
        self.canvas.viewport().update()

    def _on_slider_released(self):
        dur = self._audio_duration or self._notes_end()
        t = dur * float(self.slider.value()) / 1000.0
        self.seek_to(t)
        if getattr(self, "_slider_was_playing", False) and self.player is not None \
                and not self._playing:
            self.preview()

    def _on_edit_committed(self):
        self._audio_dirty = True
        self._audio_duration = self._notes_end()
        if self._playing or self._paused:
            self.stop_preview()
            self._set_status("已停止试听：谱面已修改（点 ▶ 或空格从当前位置重听）")
        else:
            self._update_progress(self.canvas.playhead or 0.0)
        if self.model.mode == "midi" and self.model.phys_enabled:
            self._compile_phys()

    def _tick(self):
        try:
            if not self._playing:
                return
            if self.player is not None:
                from PyQt5.QtMultimedia import QMediaPlayer
                pos = self.player.position() / 1000.0
                if self.player.state() == QMediaPlayer.StoppedState \
                        and pos >= (self._audio_duration - 0.25):
                    self._playing = False
                    self._paused = False
                    self._timer.stop()
                    self._sync_play_button()
                t = max(0.0, min(pos, self._audio_duration or pos))
            else:
                t = float(getattr(self, "_play_from", 0.0)) + \
                    (time.perf_counter() - (self._t0 or time.perf_counter()))
                if t > (self._audio_duration or self._notes_end()) + 0.2:
                    self._playing = False
                    self._paused = False
                    self._timer.stop()
                    self._sync_play_button()
            self.canvas.playhead = t
            self._update_progress(t)
            self.canvas.viewport().update()
            self._follow_playhead()
        except Exception:
            pass

    def _follow_playhead(self):
        if not self._playing or self.canvas.playhead is None:
            return
        x = self.canvas.x_of(self.canvas.playhead)
        vw = self.canvas.viewport().width()
        if x > vw - 60:
            self.canvas.horizontalScrollBar().setValue(
                int(self.canvas.horizontalScrollBar().value() + (x - (vw - 60))))

    def closeEvent(self, ev):
        self.stop_preview()
        self._cleanup_audio()
        try:
            self._stop_wav()
        except Exception:
            pass
        super(PianoRollDialog, self).closeEvent(ev)


# ══════════════════════════════════════════════════════════════════════════
#  4. 命令行入口（调试/冒烟）
# ══════════════════════════════════════════════════════════════════════════
def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    app = QApplication(argv)
    try:
        from gui_main import apply_theme
        apply_theme(app)
    except Exception:
        pass
    path = None
    role = None
    rest = []
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--role" and i + 1 < len(argv):
            role = argv[i + 1]
            i += 2
            continue
        if not a.startswith("--"):
            rest.append(a)
        i += 1
    dflt = os.path.join(_ROOT, "assets", "test_midis", "sample_demo.mid")
    if not os.path.isfile(dflt):
        cand = sorted(Path(os.path.join(_ROOT, "assets", "test_midis")).glob("*.mid"))
        dflt = str(cand[0]) if cand else dflt
    path = rest[0] if rest else dflt
    dlg = PianoRollDialog(path, role=role)
    if "--smoke" in argv:
        dlg.show()
        QTimer.singleShot(1500, app.quit)
        rc = app.exec_()
        print("PIANO_ROLL_SMOKE_OK notes=%d" % len(dlg.model.notes))
        return 0 if rc == 0 else rc
    dlg.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())