# -*- coding: utf-8 -*-
"""multipart_studio.py -- 乐曲库：在线/本地多轨乐谱与转录人声对齐。"""
import glob
import gzip
import json
import math
import os
import re
import shutil
import sys
import threading
import time
import urllib.parse
import urllib.request
import wave

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
for _p in (ROOT, BASE, os.path.join(ROOT, "transcriber")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from PyQt5.QtCore import QPoint, QTimer, Qt, pyqtSignal  # noqa: E402
from PyQt5.QtGui import QColor, QKeySequence, QPainter, QPen, QPolygon  # noqa: E402
from PyQt5.QtWidgets import (QApplication, QComboBox, QDialog,  # noqa: E402
                             QDoubleSpinBox,
                             QFileDialog, QFrame, QHBoxLayout, QInputDialog,
                             QLabel, QLineEdit, QListWidget, QListWidgetItem,
                             QMessageBox, QPushButton, QScrollBar, QShortcut,
                             QSlider, QTabWidget, QVBoxLayout, QWidget)

import convert as CV  # noqa: E402
import midi_compiler as MC  # noqa: E402
import player_config as PC  # noqa: E402

OUT_DIR = os.path.join(ROOT, "out")
OPEN_PITCH = MC.OPEN_PITCH
DRUM_HINT = re.compile(r"\b(drum|drums|percussion|cymbals?)\b|\bdrum\s*kit\b|\btama\b", re.IGNORECASE)
DRUM_INSTRUMENT_ID = "1024"
CDN_HOSTS = ("dqsljvtekg760.cloudfront.net", "d3d3l6a6rcgkaf.cloudfront.net")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"


def _http(url, timeout=8):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": "https://www.songsterr.com/",
        "Accept": "application/json, */*", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        enc = resp.headers.get("Content-Encoding", "")
    if enc == "gzip" or data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def search_songs(pattern, timeout=5):
    url = "https://www.songsterr.com/api/songs?pattern=" + urllib.parse.quote(str(pattern))
    return json.loads(_http(url, timeout=timeout).decode("utf-8", "replace"))


def fetch_meta(song_id, timeout=8):
    return json.loads(_http("https://www.songsterr.com/api/meta/%d" % int(song_id),
                            timeout=timeout).decode("utf-8", "replace"))


def fetch_track(song_id, part_id=0, meta=None, timeout=12):
    """按官方路径取分轨 JSON：/{songId}/{revisionId}/{image}/{partId}.json。"""
    meta = meta or fetch_meta(song_id)
    rev, image = meta.get("revisionId"), meta.get("image")
    if not rev or not image:
        raise RuntimeError("曲谱元数据缺少 revisionId/image")
    last = None
    for host in CDN_HOSTS:
        url = "https://%s/%d/%s/%s/%d.json" % (host, int(song_id), rev, image, int(part_id))
        try:
            return json.loads(_http(url, timeout=timeout).decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise RuntimeError("曲谱分轨下载失败：%s" % last)


def _beat_beats(dur, default=1.0):
    if isinstance(dur, (list, tuple)) and len(dur) == 2:
        try:
            return 4.0 * float(dur[0]) / float(dur[1])
        except Exception:
            return default
    if isinstance(dur, (int, float)):
        return float(dur)
    return default


def track_json_to_notes(js, default_bpm=120.0):
    """Songsterr 分轨 JSON → 物理秒音符（含变调夹、特殊调弦与速度曲线）。"""
    js = js or {}
    tuning = [int(x) for x in (js.get("tuning") or []) if isinstance(x, (int, float))]
    capo = int(js.get("capo") or 0)
    tempo_map = sorted(
        (int(t.get("measure") or 0), float(t.get("bpm") or default_bpm))
        for t in ((js.get("automations") or {}).get("tempo") or []))

    def bpm_at(measure):
        bpm = float(default_bpm)
        for mi, val in tempo_map:
            if mi <= measure:
                bpm = val
            else:
                break
        return bpm

    notes, t_sec = [], 0.0
    last_on_string = {}
    for mi, m in enumerate(js.get("measures") or []):
        try:
            sig = m.get("signature") or [4, 4]
            bar = float(sig[0]) * 4.0 / float(sig[1])
        except Exception:
            bar = 4.0
        spb = 60.0 / max(20.0, bpm_at(mi))
        for voice in (m.get("voices") or [{"beats": m.get("beats") or []}]):
            pos = 0.0
            for b in (voice.get("beats") or []):
                dur_beat = _beat_beats(b.get("duration"))
                t0 = t_sec + pos * spb
                for n in (b.get("notes") or []):
                    if n.get("rest") or n.get("fret") is None:
                        continue
                    try:
                        string = int(n.get("string") or 0)
                        fret = int(n.get("fret") or 0)
                    except Exception:
                        continue
                    if 0 <= string < len(tuning):
                        pitch = int(tuning[string]) + capo + fret
                    else:
                        pitch = int(OPEN_PITCH.get(6 - string, 40)) + capo + fret
                    prev = last_on_string.get(string)
                    if n.get("tie") and prev is not None:
                        prev["dur"] = round(prev["dur"] + dur_beat * spb, 4)
                        continue
                    while pitch < 40:
                        pitch += 12
                    while pitch > 88:
                        pitch -= 12
                    note = {"t": round(t0, 4),
                            "dur": max(0.05, dur_beat * spb * 0.95),
                            "pitch": pitch, "string": 6 - string,
                            "fret": fret,
                            "vel": int(n.get("velocity") or 80)}
                    notes.append(note)
                    last_on_string[string] = note
                pos += dur_beat
        t_sec += bar * spb
    notes.sort(key=lambda n: (n["t"], n["pitch"]))
    return notes


def sanitize_fret_frame(notes, frame_ms=15.0, constraint_mode="guitar_fret",
                        prev_fret=None):
    """同帧内按品格可行性择优，剔除冲突音。"""
    if constraint_mode != "guitar_fret" or not notes:
        return list(notes), {"dropped": 0, "frames": 0}
    notes = sorted(notes, key=lambda n: (float(n["t"]), -int(n["pitch"])))
    kept, dropped, frames, remapped = [], 0, 0, 0
    cluster, cluster_t = [], None
    prev = prev_fret

    def flush(cl, prev_fret):
        nonlocal dropped, frames, remapped
        if not cl:
            return prev_fret
        frames += 1
        best, best_score, best_map = None, None, {}
        top = max(int(n["pitch"]) for n in cl)
        for fret in range(0, 25):
            used, assigned, mapping = set(), [], {}
            for n in sorted(cl, key=lambda x: -int(x["pitch"])):
                cand = [st for st, op in OPEN_PITCH.items()
                        if int(op) + fret == int(n["pitch"]) and st not in used]
                if cand:
                    used.add(cand[0])
                    assigned.append(n)
                    mapping[id(n)] = n
            for n in cl:
                if id(n) in mapping:
                    continue
                for st in sorted(OPEN_PITCH):
                    if st in used:
                        continue
                    if int(OPEN_PITCH[st]) + fret == int(n["pitch"]):
                        used.add(st)
                        mapping[id(n)] = dict(n, pitch=int(OPEN_PITCH[st]) + fret,
                                              string=st, fret=fret)
                        assigned.append(mapping[id(n)])
                        break
            if not assigned:
                continue
            steps = MC.page_delta(MC._page_of(prev_fret or 0), MC._page_of(fret))[0]
            covers_top = any(int(x["pitch"]) == top for x in assigned)
            score = (len(assigned) * 12.0 + (15.0 if covers_top else 0.0)
                     - steps * 6.0 - abs(fret - (prev_fret or 0)) * 0.4)
            if best_score is None or score > best_score:
                best, best_score, best_map = fret, score, mapping
        if best is None:
            kept.extend(cl)
            return prev_fret
        out = []
        for n in cl:
            got = best_map.get(id(n))
            if got is None:
                dropped += 1
                continue
            if isinstance(got, dict) and got is not n:
                remapped += 1
                out.append(got)
            else:
                out.append(n)
        kept.extend(out)
        return best

    for n in notes:
        t = float(n["t"]) * 1000.0
        if cluster_t is None or abs(t - cluster_t) <= frame_ms:
            cluster.append(n)
            cluster_t = t if cluster_t is None else cluster_t
            continue
        prev = flush(cluster, prev)
        cluster, cluster_t = [n], t
    flush(cluster, prev)
    return kept, {"dropped": dropped, "frames": frames, "remapped": remapped}


ASCII_TRACKS = {"人声轨": "Vocal", "主音吉他轨": "Lead Guitar",
                "节奏吉他轨": "Rhythm Guitar", "贝斯轨": "Bass",
                "键盘轨": "Keys", "全声部试听": "Full Mix"}
ASCII_BY_KEYWORD = (("vocal", "Vocal"), ("人声", "Vocal"),
                    ("lead", "Lead Guitar"), ("主音", "Lead Guitar"),
                    ("rhythm", "Rhythm Guitar"), ("节奏", "Rhythm Guitar"),
                    ("bass", "Bass"), ("贝斯", "Bass"),
                    ("piano", "Keys"), ("key", "Keys"), ("键盘", "Keys"))


def _ascii_name(prefix, index=0):
    text = str(prefix or "").strip()
    if text in ASCII_TRACKS:
        return ASCII_TRACKS[text]
    try:
        text.encode("latin-1")
        return text[:64] or ("Part %d" % (index + 1))
    except UnicodeEncodeError:
        pass
    low = text.lower()
    for key, name in ASCII_BY_KEYWORD:
        if key in low:
            return name
    return "Part %d" % (index + 1)


def _track_name(prefix, song):
    return "%s-%s" % (prefix, song)


# 旧版把轨名写成中文「人声轨」，存盘降级为 Track N
_VOCAL_LEGACY_NAME = re.compile(r"^Track \d+$")


def _is_vocal_name(name):
    text = str(name or "").strip()
    return "vocal" in text.lower() or "人声" in text


def _read_bpm(path):
    import mido
    try:
        for msg in mido.MidiFile(str(path)):
            if msg.type == "set_tempo":
                return round(60_000_000 / msg.tempo, 3)
    except Exception:
        pass
    return 120.0


def _safe_filename(name):
    cleaned = re.sub(r'[\\/:*?"<>|\r\n]+', "_", str(name or "").strip())
    return cleaned.strip(" ._") or "Track"


def _fold_notes(notes):
    out = []
    for n in notes or []:
        pitch = int(n["pitch"])
        while pitch < 40:
            pitch += 12
        while pitch > 88:
            pitch -= 12
        out.append(dict(n, pitch=pitch))
    return out


def write_parts(song, parts, out_root=OUT_DIR, mix_parts=None):
    song = _safe_filename(song)
    folder = os.path.join(out_root, song)
    os.makedirs(folder, exist_ok=True)
    made = []
    mix = []
    for i, (prefix, notes) in enumerate(parts):
        if not notes:
            continue
        tracks = [CV.TrackNotes(_ascii_name(prefix, i), notes)]
        path = os.path.join(folder, _track_name(prefix, song) + ".mid")
        CV.write_midi(path, tracks, text_events=[(0, "SOLO_MODE:fret")])
        made.append(path)
    for i, (prefix, notes) in enumerate(mix_parts if mix_parts is not None else parts):
        if notes:
            mix.append(CV.TrackNotes(_ascii_name(prefix, i), notes))
    if mix:
        path = os.path.join(folder, "全声部试听-%s.mid" % song)
        CV.write_midi(path, mix, text_events=[(0, "SOLO_MODE:fret")])
        made.append(path)
    return made


def read_midi_notes(path, stats=None):
    tracks = CV.read_midi_tracks(path)
    out, skipped = [], 0
    for t in tracks:
        if not t.notes:
            continue
        if getattr(t, "is_drum", False) or DRUM_HINT.search(str(t.name or "").lower()):
            skipped += 1
            continue
        out.append((t.name or "Track", [dict(pitch=int(n.pitch), t=float(n.start_sec),
                                             dur=max(0.05, float(n.end_sec) - float(n.start_sec)),
                                             vel=int(getattr(n, "velocity", 80)))
                                        for n in t.notes]))
    if stats is not None:
        stats["skipped_drums"] = skipped
    return out


def _fit_dialog(widget, width, height, min_w=680, min_h=480):
    try:
        avail = QApplication.desktop().availableGeometry(widget)
        width = min(int(width), int(avail.width() * 0.94))
        height = min(int(height), int(avail.height() * 0.90))
        min_w = min(int(min_w), int(avail.width() * 0.70))
        min_h = min(int(min_h), int(avail.height() * 0.65))
    except Exception:
        pass
    widget.resize(max(min_w, width), max(min_h, height))
    widget.setMinimumSize(min_w, min_h)
    return widget


class AlignCanvas(QWidget):
    offsetChanged = pyqtSignal(float)
    playheadChanged = pyqtSignal(float)
    viewChanged = pyqtSignal(float)

    RULER_H = 24

    def __init__(self, parent=None):
        super(AlignCanvas, self).__init__(parent)
        self.setMinimumHeight(200)
        self.ref, self.target = [], []
        self.offset = 0.0
        self.scale = 1.0
        self.pps = 90.0
        self.view_sec = 0.0
        self.playhead_sec = 0.0
        self.bpm = 120.0
        self.snap_div = 4
        self._drag = None
        self._drag_x = 0
        self._drag_shift = False
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.hbar = QScrollBar(Qt.Horizontal)
        self.hbar.valueChanged.connect(self._on_hbar)
        self._sync_hbar()

    def set_notes(self, ref, target):
        self.ref, self.target = list(ref or []), list(target or [])
        self._sync_hbar()
        self.update()

    def set_offset(self, value):
        self.offset = float(value)
        self._sync_hbar()
        self.update()

    def set_scale(self, value):
        self.scale = max(0.1, float(value))
        self._sync_hbar()
        self.update()

    def set_bpm(self, bpm):
        self.bpm = max(20.0, float(bpm))
        self.update()

    def set_snap(self, div):
        self.snap_div = int(div)
        self.update()

    def set_playhead(self, sec):
        self.playhead_sec = max(0.0, float(sec))
        self.playheadChanged.emit(self.playhead_sec)
        self.update()

    def set_view(self, sec, sync=True):
        self.view_sec = max(0.0, float(sec))
        if sync:
            self._sync_hbar()
        self.viewChanged.emit(self.view_sec)
        self.update()

    def visible_sec(self):
        return max(0.5, self.width() / max(1.0, self.pps))

    def _tmax(self):
        end = 10.0
        for n in self.ref:
            end = max(end, float(n["t"]) + float(n.get("dur") or 0))
        for n in self.target:
            end = max(end, n["t"] * self.scale + self.offset
                      + float(n.get("dur") or 0) * self.scale)
        return end + 1.0

    def _sync_hbar(self):
        span = max(0.0, self._tmax() - self.visible_sec())
        self.hbar.blockSignals(True)
        self.hbar.setRange(0, int(span * 10))
        self.hbar.setPageStep(int(self.visible_sec() * 10))
        self.hbar.setValue(int(self.view_sec * 10))
        self.hbar.blockSignals(False)

    def _on_hbar(self, value):
        self.set_view(value / 10.0, sync=False)

    def resizeEvent(self, ev):
        super(AlignCanvas, self).resizeEvent(ev)
        self._sync_hbar()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0f172a"))
        ruler = self.RULER_H
        p.fillRect(0, 0, self.width(), ruler, QColor("#1e293b"))
        t0, t1 = self.view_sec, self.view_sec + self.visible_sec()
        step = 1.0 if self.pps >= 40 else 5.0
        p.setPen(QPen(QColor("#475569"), 1))
        t = math.floor(t0 / step) * step
        while t <= t1:
            x = int((t - t0) * self.pps)
            if x >= 0:
                p.drawLine(x, ruler - 6, x, ruler)
            t += step
        p.setPen(QPen(QColor("#94a3b8"), 1))
        font = p.font()
        font.setPointSize(7)
        p.setFont(font)
        t = math.floor(t0 / step) * step
        while t <= t1:
            x = int((t - t0) * self.pps)
            if 0 <= x <= self.width() - 28:
                p.drawText(x + 2, 10, "%gs" % round(t, 1))
            t += step
        beat = 60.0 / self.bpm
        p.setPen(QPen(QColor("#334155"), 1, Qt.DotLine))
        t = math.floor(t0 / beat) * beat
        while t <= t1:
            x = int((t - t0) * self.pps)
            if x >= 0:
                p.drawLine(x, ruler, x, self.height())
            t += beat
        band = max(1, (self.height() - ruler) // 2)
        mid = ruler + band
        p.setPen(QPen(QColor("#334155"), 1))
        p.drawLine(0, mid, self.width(), mid)
        self._paint_band(p, self.ref, ruler, band, QColor("#94a3b8"), 0.0, 1.0)
        self._paint_band(p, self.target, mid, band, QColor("#3b82f6"), self.offset, self.scale)
        xh = int((self.playhead_sec - self.view_sec) * self.pps)
        if 0 <= xh <= self.width():
            p.setPen(QPen(QColor("#facc15"), 2))
            p.drawLine(xh, 0, xh, self.height())
            p.setBrush(QColor("#facc15"))
            p.drawPolygon(QPolygon([QPoint(xh - 5, 0), QPoint(xh + 5, 0), QPoint(xh, 8)]))
        p.end()

    def _paint_band(self, p, notes, top, height, color, shift, scale):
        if not notes:
            return
        pitches = [n["pitch"] for n in notes]
        lo, hi = min(pitches), max(pitches)
        span = max(1, hi - lo)
        p.setPen(QPen(color, 3))
        for n in notes:
            t = float(n["t"]) * scale + shift
            x = int((t - self.view_sec) * self.pps)
            if x < -20 or x > self.width() + 20:
                continue
            y = top + height - 8 - int((n["pitch"] - lo) / span * (height - 16))
            w = max(2, int(float(n.get("dur") or 0.1) * self.pps * scale))
            p.drawLine(x, y, x + w, y)

    def mousePressEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        x, y = ev.x(), ev.y()
        self._drag_shift = bool(ev.modifiers() & Qt.ShiftModifier)
        self._drag_x = x
        if y < self.RULER_H:
            self._drag = "playhead"
            self.set_playhead(self.view_sec + x / max(1.0, self.pps))
        elif y < self.RULER_H + (self.height() - self.RULER_H) // 2:
            self._drag = "view"
        else:
            self._drag = "offset"

    def mouseMoveEvent(self, ev):
        if self._drag is None:
            return
        x = ev.x()
        dx = x - self._drag_x
        self._drag_x = x
        if self._drag == "playhead":
            self.set_playhead(self.view_sec + x / max(1.0, self.pps))
        elif self._drag == "view":
            self.set_view(self.view_sec - dx / max(1.0, self.pps))
        else:
            self.offset += dx / max(1.0, self.pps)
            self.offsetChanged.emit(self.offset)
            self.update()

    def mouseReleaseEvent(self, _ev):
        if self._drag == "offset":
            self._apply_snap()
        self._drag = None

    def _apply_snap(self):
        if self.snap_div <= 0 or self._drag_shift or not self.target:
            return
        interval = (60.0 / self.bpm) * self.snap_div / 4.0
        first = min(n["t"] for n in self.target) * self.scale + self.offset
        snapped = round(first / interval) * interval
        self.offset += snapped - first
        self.offsetChanged.emit(self.offset)
        self.update()

    def wheelEvent(self, ev):
        delta = ev.angleDelta().y()
        if not delta:
            return
        anchor = self.view_sec + ev.x() / max(1.0, self.pps)
        self.pps = max(20.0, min(320.0, self.pps * (1.15 if delta > 0 else 1 / 1.15)))
        self.set_view(anchor - ev.x() / max(1.0, self.pps))


class MultipartStudioDialog(QDialog):
    sig_search_done = pyqtSignal(object, str)
    sig_meta_done = pyqtSignal(object, int, str)
    sig_dl_progress = pyqtSignal(int, int, str)

    def __init__(self, parent=None, on_done=None):
        super(MultipartStudioDialog, self).__init__(parent)
        self.on_done = on_done
        self.setWindowTitle("乐曲库")
        self.resize(1060, 720)
        _fit_dialog(self, 1060, 720, 680, 480)
        self.song_name = ""
        self.song_notes = []
        self.tracks = []
        self._align_ref = []
        self._align_tgt = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self.tabs.addTab(self._build_import_tab(), "乐谱导入")
        self.tabs.addTab(self._build_align_tab(), "人声对齐")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.sig_search_done.connect(self._on_search_done)
        self.sig_meta_done.connect(self._on_meta_done)
        self.sig_dl_progress.connect(self._on_dl_progress)
        self.fill_align_sources()

    def _on_tab_changed(self, _idx):
        self.stop_preview()
        self.fill_align_sources()

    def _build_import_tab(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        row = QHBoxLayout()
        self.edit_search = QLineEdit()
        self.edit_search.setPlaceholderText("输入歌名搜索 Songsterr…")
        row.addWidget(self.edit_search, 1)
        self.btn_search = QPushButton("搜索")
        self.btn_search.clicked.connect(self.do_search)
        row.addWidget(self.btn_search)
        self.btn_local = QPushButton("导入本地多轨 MIDI")
        self.btn_local.clicked.connect(self.import_local_midi)
        row.addWidget(self.btn_local)
        lay.addLayout(row)
        self.lbl_status = QLabel("可搜索在线曲谱，或直接导入本地多轨 MIDI。")
        self.lbl_status.setObjectName("sub")
        lay.addWidget(self.lbl_status)
        self.results = QListWidget()
        self.results.setMaximumHeight(150)
        self.results.itemDoubleClicked.connect(self.load_song)
        lay.addWidget(self.results)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("歌名"))
        self.edit_song = QLineEdit()
        row2.addWidget(self.edit_song, 1)
        lay.addLayout(row2)
        self.track_list = QListWidget()
        self.track_list.itemDoubleClicked.connect(self._rename_track)
        lay.addWidget(self.track_list, 1)
        self.lbl_hint = QLabel("双击轨道可改前缀；输出到 out/<歌名>/。")
        self.lbl_hint.setObjectName("sub")
        lay.addWidget(self.lbl_hint)
        row3 = QHBoxLayout()
        row3.addStretch(1)
        self.btn_generate = QPushButton("生成分轨并归档")
        self.btn_generate.clicked.connect(self.generate)
        row3.addWidget(self.btn_generate)
        lay.addLayout(row3)
        return page

    def _build_align_tab(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        row = QHBoxLayout()
        row.addWidget(QLabel("参考伴奏轨"))
        self.cmb_ref = QComboBox()
        self.cmb_ref.currentIndexChanged.connect(lambda _i: self.refresh_align())
        row.addWidget(self.cmb_ref, 1)
        self.btn_ref = QPushButton("浏览…")
        self.btn_ref.clicked.connect(lambda: self._pick_midi(self.cmb_ref))
        row.addWidget(self.btn_ref)
        lay.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("待对齐转录轨"))
        self.cmb_target = QComboBox()
        self.cmb_target.currentIndexChanged.connect(lambda _i: self.refresh_align())
        row.addWidget(self.cmb_target, 1)
        self.btn_target = QPushButton("浏览…")
        self.btn_target.clicked.connect(lambda: self._pick_midi(self.cmb_target))
        row.addWidget(self.btn_target)
        lay.addLayout(row)
        self.canvas = AlignCanvas()
        lay.addWidget(self.canvas, 1)
        lay.addWidget(self.canvas.hbar)
        row = QHBoxLayout()
        row.addWidget(QLabel("起点偏移(秒)"))
        self.spin_offset = QDoubleSpinBox()
        self.spin_offset.setRange(-60.0, 60.0)
        self.spin_offset.setSingleStep(0.02)
        self.spin_offset.valueChanged.connect(self.canvas.set_offset)
        self.canvas.offsetChanged.connect(self.spin_offset.setValue)
        row.addWidget(self.spin_offset)
        row.addWidget(QLabel("速度(%)"))
        self.spin_scale = QDoubleSpinBox()
        self.spin_scale.setRange(50.0, 150.0)
        self.spin_scale.setValue(100.0)
        self.spin_scale.valueChanged.connect(lambda v: self.canvas.set_scale(v / 100.0))
        row.addWidget(self.spin_scale)
        row.addWidget(QLabel("吸附"))
        self.cmb_snap = QComboBox()
        self.cmb_snap.addItem("自由 (无吸附)", 0)
        self.cmb_snap.addItem("1/4 拍", 1)
        self.cmb_snap.addItem("1/2 拍", 2)
        self.cmb_snap.addItem("1 拍", 4)
        self.cmb_snap.setCurrentIndex(1)
        self.cmb_snap.currentIndexChanged.connect(
            lambda _i: self.canvas.set_snap(self.cmb_snap.currentData()))
        row.addWidget(self.cmb_snap)
        self.canvas.set_snap(self.cmb_snap.currentData())
        self.btn_auto = QPushButton("自动对齐首音")
        self.btn_auto.clicked.connect(self.auto_align)
        row.addWidget(self.btn_auto)
        self.btn_preview = QPushButton("▶ 试听")
        self.btn_preview.clicked.connect(self._toggle_preview)
        row.addWidget(self.btn_preview)
        self.btn_stop = QPushButton("⏹ 停止")
        self.btn_stop.clicked.connect(self.stop_preview)
        row.addWidget(self.btn_stop)
        self.sld_vol = QSlider(Qt.Horizontal)
        self.sld_vol.setRange(0, 100)
        self.sld_vol.setValue(60)
        self.sld_vol.setFixedWidth(60)
        row.addWidget(self.sld_vol)
        row.addStretch(1)
        lay.addLayout(row)
        row2 = QHBoxLayout()
        row2.addStretch(1)
        self.btn_apply_align = QPushButton("确认对齐并覆盖保存")
        self.btn_apply_align.clicked.connect(self.apply_align)
        row2.addWidget(self.btn_apply_align)
        self.lbl_align_ok = QLabel("")
        self.lbl_align_ok.setStyleSheet("color: #16a34a; font-weight: 700;")
        row2.addWidget(self.lbl_align_ok)
        lay.addLayout(row2)
        self._space_key = QShortcut(QKeySequence(Qt.Key_Space), page)
        self._space_key.setContext(Qt.WidgetWithChildrenShortcut)
        self._space_key.activated.connect(self._toggle_preview)
        return page

    def _toggle_preview(self):
        timer = getattr(self, "_play_timer", None)
        if timer is not None and timer.isActive():
            timer.stop()
            try:
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
            self.btn_preview.setEnabled(True)
            self.btn_preview.setText("▶ 试听")
            return
        self.preview()

    def _pick_midi(self, combo):
        path, _ = QFileDialog.getOpenFileName(self, "选择 MIDI", OUT_DIR, "MIDI (*.mid *.midi)")
        if path:
            combo.clear()
            combo.addItem(os.path.basename(path), path)
            self.refresh_align()

    def do_search(self):
        pattern = self.edit_search.text().strip()
        if not pattern:
            return
        self.lbl_status.setText("搜索中…")

        def work():
            rows, err = [], ""
            try:
                rows = search_songs(pattern)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
            self.sig_search_done.emit(rows, err)

        threading.Thread(target=work, daemon=True).start()

    def _on_search_done(self, rows, err):
        if err:
            self.lbl_status.setText("离线或接口不可用：%s（可改用本地导入）" % err)
            return
        self.results.clear()
        for r in rows[:30]:
            item = QListWidgetItem("%s - %s" % (r.get("artist") or "", r.get("title") or ""))
            item.setData(Qt.UserRole, r.get("songId"))
            self.results.addItem(item)
        self.lbl_status.setText("找到 %d 首，双击载入。" % min(len(rows), 30))

    def load_song(self, item):
        song_id = item.data(Qt.UserRole)
        if not song_id:
            return
        self.lbl_status.setText("下载曲谱信息…")

        def work():
            meta, err = None, ""
            try:
                meta = fetch_meta(song_id)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
            self.sig_meta_done.emit(meta, int(song_id), err)

        threading.Thread(target=work, daemon=True).start()

    def _on_meta_done(self, meta, song_id, err):
        if err or not meta:
            self.lbl_status.setText("曲谱信息获取失败：%s" % (err or "未知错误"))
            return
        self.song_name = (meta.get("title") or "未命名").strip()
        self.edit_song.setText(self.song_name)
        self._meta = meta
        self._song_id = int(song_id)
        self.tracks = []
        for orig_idx, t in enumerate(meta.get("tracks") or []):
            name = t.get("name") or "Track"
            instrument = t.get("instrument") or ""
            if str(t.get("instrumentId") or "") == DRUM_INSTRUMENT_ID:
                continue
            if DRUM_HINT.search(("%s %s" % (name, instrument)).lower()):
                continue
            title = name or instrument or ("Track_%d" % (orig_idx + 1))
            self.tracks.append({"name": name, "instrument": instrument,
                                "part_id": orig_idx,
                                "prefix": _safe_filename(title),
                                "display": _safe_filename(title)})
        seen = {}
        for t in self.tracks:
            base = t["prefix"]
            seen[base] = seen.get(base, 0) + 1
            t["prefix"] = base if seen[base] == 1 else "%s %d" % (base, seen[base])
        self._fill_tracks()
        self.btn_generate.setEnabled(False)
        self.lbl_status.setText("%s · %d 轨，准备下载分轨…" % (
            self.song_name, len(self.tracks)))
        threading.Thread(target=self._download_all, daemon=True).start()

    def _download_all(self):
        total = max(1, len(self.tracks))
        for idx, t in enumerate(self.tracks):
            try:
                js = fetch_track(self._song_id, t.get("part_id", idx),
                                 meta=getattr(self, "_meta", None))
                if js.get("isDrums"):
                    t["notes"] = []
                    t["error"] = "打击乐轨已跳过"
                else:
                    t["notes"] = track_json_to_notes(js)
            except Exception as exc:  # noqa: BLE001
                t["notes"] = []
                t["error"] = str(exc)
            self.sig_dl_progress.emit(idx + 1, total, t.get("name") or "")

    def _on_dl_progress(self, done, total, name):
        self.lbl_status.setText("正在下载分轨 (%d/%d)… %s" % (done, total, name))
        if done >= total:
            self.btn_generate.setEnabled(True)
            self.lbl_status.setText("分轨已就绪：%d/%d，勾选后生成。" % (done, total))

    def _fill_tracks(self):
        self.track_list.clear()
        for i, t in enumerate(self.tracks):
            label = t.get("display") or t["prefix"]
            inst = t.get("instrument") or ""
            if inst and inst.lower() not in str(t.get("name") or "").lower():
                label = "%s  [%s]" % (label, inst)
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if i < 5 else Qt.Unchecked)
            item.setData(Qt.UserRole, i)
            self.track_list.addItem(item)

    def _rename_track(self, item):
        idx = item.data(Qt.UserRole)
        new, ok = QInputDialog.getText(self, "轨道前缀", "前缀：", text=self.tracks[idx]["prefix"])
        if ok and new.strip():
            clean = _safe_filename(new)
            self.tracks[idx]["prefix"] = clean
            self.tracks[idx]["display"] = clean
            self._fill_tracks()

    def import_local_midi(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入多轨 MIDI", OUT_DIR, "MIDI (*.mid *.midi)")
        if not path:
            return
        stats = {}
        try:
            tracks = read_midi_notes(path, stats)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "导入失败", str(exc))
            return
        if not tracks:
            QMessageBox.information(
                self, "没有可用的轨道",
                "这个 MIDI 里没有可当乐谱用的轨道（鼓轨已自动跳过，见下方说明）。")
            return
        self.song_name = os.path.splitext(os.path.basename(path))[0]
        self.edit_song.setText(self.song_name)
        self.tracks = []
        for i, (name, notes) in enumerate(tracks):
            clean = _safe_filename(name or ("Track_%d" % (i + 1)))
            self.tracks.append({"name": name, "prefix": clean, "display": clean,
                                "notes": notes, "local": True})
        self._fill_tracks()
        msg = "本地导入 %d 轨。" % len(self.tracks)
        if stats.get("skipped_drums"):
            msg = "本地导入 %d 轨（已跳过鼓轨 %d 条）。" % (len(self.tracks),
                                                   stats["skipped_drums"])
        self.lbl_status.setText(msg)

    def generate(self):
        song = _safe_filename(self.edit_song.text().strip() or self.song_name or "未命名")
        parts, mix_parts, total, dropped = [], [], 0, 0
        for i in range(self.track_list.count()):
            item = self.track_list.item(i)
            if item.checkState() != Qt.Checked:
                continue
            t = self.tracks[item.data(Qt.UserRole)]
            notes = _fold_notes(t.get("notes") or [])
            kept, st = sanitize_fret_frame(notes)
            dropped += st["dropped"]
            if kept:
                parts.append((t["prefix"], [CV.Note(pitch=n["pitch"], start_sec=n["t"],
                                                    end_sec=n["t"] + n["dur"],
                                                    velocity=n.get("vel", 80))
                                            for n in kept]))
                total += len(kept)
            if notes:
                mix_parts.append((t["prefix"], [CV.Note(pitch=n["pitch"], start_sec=n["t"],
                                                        end_sec=n["t"] + n["dur"],
                                                        velocity=n.get("vel", 80))
                                                for n in notes]))
        if not parts:
            QMessageBox.information(self, "没有可生成的分轨",
                                    "先载入曲谱或本地 MIDI，并勾选至少一条轨道。")
            return
        made = write_parts(song, parts, mix_parts=mix_parts)
        self.lbl_status.setText("已生成 %d 个文件到 out/%s/（清洗剔除 %d 音），"
                                "可在演奏页曲库查看。" % (len(made), song, dropped))
        if self.on_done:
            try:
                self.on_done()
            except Exception:
                pass

    def fill_align_sources(self):
        files = []
        for dirpath, _dirs, names in os.walk(OUT_DIR):
            for name in names:
                if name.lower().endswith((".mid", ".midi")):
                    files.append(os.path.join(dirpath, name))
        files.sort()
        current = (self.cmb_ref.currentData(), self.cmb_target.currentData())
        for combo in (self.cmb_ref, self.cmb_target):
            combo.blockSignals(True)
            combo.clear()
            for path in files[:80]:
                combo.addItem(os.path.relpath(path, OUT_DIR), path)
            combo.blockSignals(False)
        for combo, old in ((self.cmb_ref, current[0]), (self.cmb_target, current[1])):
            idx = combo.findData(old) if old else -1
            if idx >= 0:
                combo.setCurrentIndex(idx)
        for combo, key in ((self.cmb_ref, "全声部试听"), (self.cmb_target, "人声轨")):
            if combo.currentData() is None:
                for i in range(combo.count()):
                    if key in combo.itemText(i):
                        combo.setCurrentIndex(i)
                        break
        self.refresh_align()

    def refresh_align(self):
        ref_path = self.cmb_ref.currentData()
        tgt_path = self.cmb_target.currentData()
        ref, tgt = [], []
        if ref_path and os.path.isfile(str(ref_path)):
            ref = [n for _, ns in read_midi_notes(ref_path) for n in ns]
            self.canvas.set_bpm(_read_bpm(str(ref_path)))
        if tgt_path and os.path.isfile(str(tgt_path)):
            tgt = [n for _, ns in read_midi_notes(tgt_path) for n in ns]
        self.canvas.set_notes(ref, tgt)
        self._align_ref, self._align_tgt = ref, tgt

    def auto_align(self):
        if getattr(self, "_align_ref", None) and getattr(self, "_align_tgt", None):
            self.spin_offset.setValue(round(self._align_ref[0]["t"] - self._align_tgt[0]["t"], 2))

    def _scaled_notes(self):
        scale = self.spin_scale.value() / 100.0
        off = self.spin_offset.value()
        out = []
        for n in getattr(self, "_align_tgt", []):
            out.append(dict(n, t=n["t"] * scale + off, dur=n["dur"] * scale))
        return out

    def preview(self):
        self.refresh_align()
        start = max(0.0, float(self.canvas.playhead_sec))
        ref = [dict(n, t=float(n["t"]) - start) for n in getattr(self, "_align_ref", [])
               if float(n["t"]) >= start]
        tgt = [dict(n, t=float(n["t"]) - start) for n in self._scaled_notes()
               if float(n["t"]) >= start]
        notes = ref + tgt
        if not notes:
            return
        try:
            import piano_roll as PR
            data = PR.PianoRollModel.synthesize_wav(notes, 120.0)
            vol = self.sld_vol.value() / 100.0
            import audioop
            import io as _io
            with wave.open(_io.BytesIO(data)) as src:
                params = src.getparams()
                frames = src.readframes(src.getnframes())
            frames = audioop.mul(frames, params.sampwidth, vol)
            path = PC.tmp_wav_path("mp_align")
            with wave.open(path, "wb") as out:
                out.setparams(params)
                out.writeframes(frames)
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "试听失败", str(exc))
            return
        end = max(float(n["t"]) + float(n.get("dur") or 0) for n in notes)
        self._play_start_sec = start
        self._play_end_sec = start + end
        self._play_t0 = time.perf_counter()
        if getattr(self, "_play_timer", None) is None:
            self._play_timer = QTimer(self)
            self._play_timer.setInterval(30)
            self._play_timer.timeout.connect(self._tick_playhead)
        self._play_timer.start()
        self.btn_preview.setEnabled(True)
        self.btn_preview.setText("⏸ 暂停")

    def _tick_playhead(self):
        pos = self._play_start_sec + (time.perf_counter() - self._play_t0)
        if pos >= self._play_end_sec:
            self.stop_preview()
            return
        self.canvas.set_playhead(pos)
        vis = self.canvas.visible_sec()
        if pos > self.canvas.view_sec + vis * 0.9:
            self.canvas.set_view(pos - vis * 0.1)

    def stop_preview(self):
        timer = getattr(self, "_play_timer", None)
        if timer is not None:
            timer.stop()
        try:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass
        if getattr(self, "btn_preview", None) is not None:
            self.btn_preview.setEnabled(True)
            self.btn_preview.setText("▶ 试听")
        if getattr(self, "canvas", None) is not None and hasattr(self, "_play_start_sec"):
            self.canvas.set_playhead(self._play_start_sec)

    def closeEvent(self, ev):
        self.stop_preview()
        super(MultipartStudioDialog, self).closeEvent(ev)

    def reject(self):
        self.stop_preview()
        super(MultipartStudioDialog, self).reject()

    def apply_align(self):
        tgt_path = self.cmb_target.currentData()
        if not tgt_path or not os.path.isfile(str(tgt_path)):
            QMessageBox.information(self, "未选择待对齐轨", "先选择一条转录 MIDI。")
            return
        notes = self._scaled_notes()
        kept, st = sanitize_fret_frame(notes)
        if not kept:
            QMessageBox.warning(self, "对齐失败", "清洗后没有剩余音符。")
            return
        orig_bpm = _read_bpm(str(tgt_path))
        try:
            bak_dir = os.path.join(ROOT, "logs", "backup_align")
            os.makedirs(bak_dir, exist_ok=True)
            shutil.copy2(str(tgt_path),
                         os.path.join(bak_dir, os.path.basename(str(tgt_path)) + ".bak"))
        except Exception:
            pass
        try:
            CV.write_midi(str(tgt_path),
                          [CV.TrackNotes("Vocal",
                                         [CV.Note(pitch=n["pitch"], start_sec=n["t"],
                                                  end_sec=n["t"] + n["dur"],
                                                  velocity=n.get("vel", 80))
                                          for n in kept])],
                          bpm=orig_bpm,
                          text_events=[(0, "SOLO_MODE:fret")])
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        folder = os.path.dirname(str(tgt_path))
        tiles = glob.glob(os.path.join(folder, "全声部试听-*.mid"))
        if tiles:
            try:
                tracks = CV.read_midi_tracks(tiles[0])
                new_track = CV.TrackNotes("Vocal",
                                          [CV.Note(pitch=n["pitch"],
                                                   start_sec=n["t"],
                                                   end_sec=n["t"] + n["dur"],
                                                   velocity=n.get("vel", 80))
                                           for n in kept])
                # 人声轨只留一条
                strict = [i for i, tr in enumerate(tracks) if _is_vocal_name(tr.name)]
                legacy = [i for i, tr in enumerate(tracks)
                          if _VOCAL_LEGACY_NAME.match(str(tr.name or "").strip())]
                keep_at = strict[0] if strict else (legacy[0] if legacy else None)
                drop = set(legacy)
                if keep_at is not None:
                    drop.discard(keep_at)
                merged = []
                for i, tr in enumerate(tracks):
                    if i in drop:
                        continue
                    merged.append(new_track if i == keep_at else tr)
                if keep_at is None:
                    merged.append(new_track)
                CV.write_midi(tiles[0], merged, bpm=orig_bpm)
            except Exception:
                pass
        if self.on_done:
            try:
                self.on_done()
            except Exception:
                pass
        self.lbl_status.setText("已覆盖保存：%s（清洗剔除 %d 音）"
                                % (os.path.basename(str(tgt_path)), st["dropped"]))
        self.lbl_align_ok.setText("✔ 已保存")
        QTimer.singleShot(4000, lambda: self.lbl_align_ok.setText(""))
