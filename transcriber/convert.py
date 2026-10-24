# -*- coding: utf-8 -*-
"""《异键演奏》秒级数据转换层。

职责：
  * 定义统一的音符 / 轨道数据结构（全部使用绝对秒）；
  * MIDI -> 秒级 JSON，JSON -> MIDI；
  * 供 pipeline.py 与 GUI 直接 import。

依赖：pretty_midi、mido（均为纯 Python，主 Python 已安装）。
输出 MIDI 统一 480 tpb、120 BPM 占位，保证各步骤写入同一物理秒基准。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

import mido
import pretty_midi

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
DEFAULT_TPB = 480
DEFAULT_BPM = 120.0
JSON_VERSION = 1
_SEC_ROUND = 6        # JSON 里秒保留 6 位（微秒级），远高于 MIDI 分辨率
_EPS = 1e-6


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class Note:
    """一个绝对秒坐标下的音符。"""

    pitch: int
    start_sec: float
    end_sec: float
    velocity: int = 100

    def __post_init__(self) -> None:
        self.pitch = int(self.pitch)
        self.start_sec = float(self.start_sec)
        self.end_sec = float(self.end_sec)
        self.velocity = int(self.velocity)
        if self.pitch < 0 or self.pitch > 127:
            raise ValueError(f"pitch 超出 0~127: {self.pitch}")
        if self.end_sec < self.start_sec:
            # 容错：坏数据若 end < start，视为极短音，避免负时长
            self.start_sec, self.end_sec = self.end_sec, self.start_sec
        self.velocity = max(1, min(127, self.velocity))

    @property
    def duration(self) -> float:
        return self.end_sec - self.start_sec

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pitch": int(self.pitch),
            "start_sec": round(float(self.start_sec), _SEC_ROUND),
            "end_sec": round(float(self.end_sec), _SEC_ROUND),
            "velocity": int(self.velocity),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Note":
        return cls(
            pitch=d["pitch"],
            start_sec=d.get("start_sec", d.get("start", 0.0)),
            end_sec=d.get("end_sec", d.get("end", 0.0)),
            velocity=d.get("velocity", 100),
        )

    def copy(self) -> "Note":
        return replace(self)


@dataclass
class TrackNotes:
    """一条轨道（保留原始轨名，便于 GUI 展示 / 下游按名取轨）。"""

    name: str
    notes: List[Note] = field(default_factory=list)
    is_drum: bool = False
    program: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name or "",
            "is_drum": bool(self.is_drum),
            "program": int(self.program),
            "note_count": len(self.notes),
            "notes": [n.to_dict() for n in self.notes],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any], fallback_name: str = "Track") -> "TrackNotes":
        raw_notes = d.get("notes", [])
        notes: List[Note] = []
        for item in raw_notes:
            if isinstance(item, Note):
                notes.append(item)
            elif isinstance(item, dict):
                try:
                    notes.append(Note.from_dict(item))
                except (KeyError, TypeError, ValueError):
                    continue
            elif isinstance(item, (list, tuple)) and len(item) >= 4:
                # 兼容 [pitch, start, end, velocity] 简写
                notes.append(Note(int(item[0]), float(item[1]), float(item[2]), int(item[3])))
        notes.sort(key=lambda n: (n.start_sec, n.end_sec, n.pitch))
        return cls(
            name=str(d.get("name") or fallback_name),
            notes=notes,
            is_drum=bool(d.get("is_drum", False)),
            program=int(d.get("program", 0) or 0),
        )

    def copy(self) -> "TrackNotes":
        return TrackNotes(self.name, [n.copy() for n in self.notes], self.is_drum, self.program)


@dataclass
class ChordSlice:
    """和弦切片：同一时刻（起音相差 <=30ms）的一组和弦音。

    `chords.json` 就存这个结构的列表；`end_sec` 是片内最大结束时间。
    """

    start_sec: float
    end_sec: float
    pitches: List[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.start_sec = float(self.start_sec)
        self.end_sec = float(self.end_sec)
        self.pitches = sorted({int(p) for p in self.pitches if 0 <= int(p) <= 127})
        if self.end_sec < self.start_sec:
            self.start_sec, self.end_sec = self.end_sec, self.start_sec

    def to_dict(self) -> Dict[str, Any]:
        return {
            "start_sec": round(float(self.start_sec), _SEC_ROUND),
            "end_sec": round(float(self.end_sec), _SEC_ROUND),
            "pitches": [int(p) for p in self.pitches],
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ChordSlice":
        return cls(
            start_sec=d.get("start_sec", d.get("start", 0.0)),
            end_sec=d.get("end_sec", d.get("end", 0.0)),
            pitches=list(d.get("pitches", [])),
        )

    @property
    def duration(self) -> float:
        return self.end_sec - self.start_sec


# ---------------------------------------------------------------------------
# v3：SheetSage2 原生 `chord.lab` / `beat.lab` 纯 Python 直读
# ---------------------------------------------------------------------------
# 音名 → 音级（与 Harte 语法一致，大小写敏感度由调用方归一）
NOTE_TO_PC: Dict[str, int] = {
    "C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4, "F": 5,
    "F#": 6, "Gb": 6, "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10,
    "B": 11, "Cb": 11, "E#": 5, "B#": 0, "Fb": 4,
}

# 品质 → 相对根音的半音音程表（含四音扩展色彩：6 / 7 / maj7 / sus4）
QUALITY_INTERVALS: Dict[str, Tuple[int, ...]] = {
    "maj": (0, 4, 7),
    "min": (0, 3, 7),
    "dim": (0, 3, 6),
    "aug": (0, 4, 8),
    "sus4": (0, 5, 7),
    "sus2": (0, 2, 7),
    "7": (0, 4, 7, 10),
    "maj7": (0, 4, 7, 11),
    "min7": (0, 3, 7, 10),
    "minmaj7": (0, 3, 7, 11),
    "dim7": (0, 3, 6, 9),
    "hdim7": (0, 3, 6, 10),
    "maj6": (0, 4, 7, 9),
    "min6": (0, 3, 7, 9),
    "9": (0, 4, 7, 10, 2),
    "maj9": (0, 4, 7, 11, 2),
    "min9": (0, 3, 7, 10, 2),
}

# 别名（SheetSage2 偶发写法 / 人工改写）→ 上表的标准品质名
QUALITY_ALIASES: Dict[str, str] = {
    "": "maj", "M": "maj", "major": "maj",
    "m": "min", "minor": "min", "-": "min",
    "dom7": "7", "m7": "min7", "M7": "maj7", "ma7": "maj7",
    "min/maj7": "minmaj7", "mmaj7": "minmaj7", "mM7": "minmaj7",
    "ø7": "hdim7", "half-dim7": "hdim7", "m7b5": "hdim7",
    "o7": "dim7", "add9": "maj", "5": "maj", "6": "maj6", "m6": "min6",
}

# 斜杠转位度数 → 相对根音的半音数
DEGREE_TO_SEMITONES: Dict[str, int] = {
    "1": 0, "b2": 1, "2": 2, "#2": 3, "b3": 3, "3": 4, "4": 5, "#4": 6,
    "b5": 6, "5": 7, "#5": 8, "b6": 8, "6": 9, "bb7": 9, "b7": 10, "7": 11,
}

CHORD_LAB_SKIP = {"N", "X", "?"}
CHORD_LAB_REARTICULATE_SEC = 2.5      # 超过这个时长的和弦在小节线内重触发


def parse_chord_label(label: str) -> Optional[Tuple[int, Tuple[int, ...], int]]:
    """Harte 标签 → `(root_pc, intervals, bass_pc)`；无法解析返回 None。

    支持 `<Root>:<Quality>[/<BassInterval>]`，例如 `C#:min`、`E:maj6`、
    `C#:min7/b3`、`A:maj/2`。`N / X / ?` 一律跳过。
    """
    text = (label or "").strip()
    if not text or text in CHORD_LAB_SKIP:
        return None
    text = text.replace("♯", "#").replace("♭", "b")

    bass_tok = None
    if "/" in text:
        text, bass_tok = text.split("/", 1)
        bass_tok = bass_tok.strip()
        text = text.strip()

    if ":" in text:
        root_txt, quality_txt = text.split(":", 1)
    else:
        root_txt, quality_txt = text, ""
    root_txt = root_txt.strip()
    quality_txt = quality_txt.strip()

    root_pc = NOTE_TO_PC.get(root_txt)
    if root_pc is None and root_txt:
        root_pc = NOTE_TO_PC.get(root_txt[0].upper() + root_txt[1:])
    if root_pc is None:
        return None

    key = quality_txt.lower()
    intervals = QUALITY_INTERVALS.get(key)
    if intervals is None:
        alias = QUALITY_ALIASES.get(quality_txt) or QUALITY_ALIASES.get(key)
        intervals = QUALITY_INTERVALS.get(alias) if alias else None
    if intervals is None:
        # 未知品质：按大三和弦兜底，绝不因为标签怪异丢掉整片和弦
        intervals = QUALITY_INTERVALS["maj"]

    bass_pc = root_pc
    if bass_tok:
        semis = DEGREE_TO_SEMITONES.get(bass_tok.lower())
        if semis is None:
            named = NOTE_TO_PC.get(bass_tok) or NOTE_TO_PC.get(
                bass_tok[:1].upper() + bass_tok[1:])
            bass_pc = named if named is not None else root_pc
        else:
            bass_pc = (root_pc + semis) % 12
    return root_pc, tuple(intervals), bass_pc


def chord_label_to_pitches(label: str) -> List[int]:
    """标签 → 三层音高布局（与 `midi_sheetsage2.chord_pitches` 完全一致）。

    * Bass 层 `36..47`：`36 + bass_pc`（真实低音，含斜杠转位低音）；
    * Root 层 `48..59`：`48 + root_pc`（和弦本位根音，供下游识别本名）；
    * Upper 层：`60 + (root_pc + interval) % 12`，含 6 / 7 / maj7 / sus4 色彩音。
    """
    got = parse_chord_label(label)
    if got is None:
        return []
    root_pc, intervals, bass_pc = got
    pitches = [36 + int(bass_pc), 48 + int(root_pc)]
    upper = {60 + (int(root_pc) + int(iv)) % 12
             for iv in intervals if int(iv) % 12 != 0}
    pitches.extend(sorted(upper))
    return sorted(set(int(p) for p in pitches))


def _read_beat_downbeats(beat_path: Optional[Union[str, Path]]) -> List[float]:
    """读 `beat.lab`（每行 `time beat_num [num] [den]`）里的小节线时刻。"""
    if not beat_path:
        return []
    path = Path(beat_path)
    if not path.is_file():
        return []
    out: List[float] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                when = float(parts[0])
                beat_no = int(float(parts[1]))
            except ValueError:
                continue
            if beat_no == 1:
                out.append(round(when, 6))
    except OSError:
        return []
    out.sort()
    return out


def parse_chord_lab_to_slices(
    lab_path: Union[str, Path],
    beat_path: Optional[Union[str, Path]] = None,
) -> List[ChordSlice]:
    """SheetSage2 `chord.lab` → `List[ChordSlice]`（纯 Python，不依赖 GPU / mir_eval）。

    这样即使不重跑推理、只用现成的 `out/sheetsage2/chord.lab` 跑 step2 / step3，
    也能 100% 拿到四音扩展和弦（`maj6 / min7 / maj7`）与斜杠转位低音（`/2, /b3`）。

    跨小节长和弦（时长 > 2.5s）内部按 `beat.lab` 的 `beat_num == 1`（强拍小节线）
    切分重触发，与 `midi_sheetsage2.build_playback` 的 downbeats 行为一致。
    """
    path = Path(lab_path)
    if not path.is_file():
        raise FileNotFoundError(f"找不到 chord.lab: {path}")
    downbeats = _read_beat_downbeats(beat_path)

    slices: List[ChordSlice] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            start = float(parts[0])
            end = float(parts[1])
        except ValueError:
            continue
        label = parts[2].strip()
        pitches = chord_label_to_pitches(label)
        if not pitches:
            continue
        if end <= start:
            continue
        if (end - start) > CHORD_LAB_REARTICULATE_SEC and downbeats:
            cuts = [start] + [d for d in downbeats if start + _EPS < d < end - _EPS] + [end]
        else:
            cuts = [start, end]
        for a, b in zip(cuts, cuts[1:]):
            if b - a <= _EPS:
                continue
            slices.append(ChordSlice(a, b, list(pitches)))
    slices.sort(key=lambda s: (s.start_sec, s.end_sec))
    return slices


def notes_to_chord_slices(
    notes: Iterable[Note],
    *,
    onset_tolerance: float = 0.03,
) -> List[ChordSlice]:
    """把和弦音符聚成切片：以每片第一个起音为锚，起音差 <=30ms 归入同片。

    同一片内音高去重、升序；`end_sec` 取片内最大结束时间。
    """
    ordered = sorted(
        (n for n in notes if n.end_sec > n.start_sec + 1e-9),
        key=lambda n: (n.start_sec, n.pitch, n.end_sec),
    )
    slices: List[ChordSlice] = []
    for note in ordered:
        if not slices or note.start_sec - slices[-1].start_sec > onset_tolerance + 1e-9:
            slices.append(ChordSlice(note.start_sec, note.end_sec, [note.pitch]))
        else:
            current = slices[-1]
            current.end_sec = max(current.end_sec, note.end_sec)
            if note.pitch not in current.pitches:
                current.pitches.append(note.pitch)
                current.pitches.sort()
    return slices


def chord_slices_to_notes(slices: Iterable[ChordSlice]) -> List[Note]:
    """把切片展开成可写 MIDI 的音符（每个音高一个音，力度按 100）。"""
    notes: List[Note] = []
    for sl in slices:
        end = max(sl.end_sec, sl.start_sec + 1e-3)
        for pitch in sl.pitches:
            notes.append(Note(pitch, sl.start_sec, end, 100))
    notes.sort(key=lambda n: (n.start_sec, n.end_sec, n.pitch))
    return notes


def chord_slices_to_track(slices: Iterable[ChordSlice], name: str = "Chords") -> TrackNotes:
    return TrackNotes(name=name, notes=chord_slices_to_notes(slices), is_drum=False, program=0)


def count_chord_slice_pitches(slices: Iterable[ChordSlice]) -> int:
    return sum(len(sl.pitches) for sl in slices)


def save_chords_json(path: Union[str, Path], slices: Sequence[ChordSlice]) -> Path:
    """写出和弦切片 JSON（顶层就是列表，便于 GUI 直接读）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump([s.to_dict() for s in slices], fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path


def load_chords_json(path: Union[str, Path]) -> List[ChordSlice]:
    """读取和弦切片 JSON；兼容旧版 tracks JSON 与直接给 MIDI 的情况。"""
    path = Path(path)
    if path.suffix.lower() in (".mid", ".midi"):
        tracks = select_tracks_by_keyword(read_midi_tracks(path), "chord")
        return notes_to_chord_slices(flatten_notes(tracks))

    with open(path, "r", encoding="utf-8-sig") as fh:
        data = json.load(fh)

    if isinstance(data, list):
        if not data:
            return []
        first = data[0]
        if isinstance(first, dict) and "pitches" in first:
            return [ChordSlice.from_dict(item) for item in data if isinstance(item, dict)]
        if isinstance(first, dict) and ("pitch" in first or "start_sec" in first):
            return notes_to_chord_slices([Note.from_dict(item) for item in data
                                          if isinstance(item, dict)])
        raise ValueError(f"无法识别的和弦列表格式: {path}")

    if isinstance(data, dict) and isinstance(data.get("slices"), list):
        return [ChordSlice.from_dict(item) for item in data["slices"] if isinstance(item, dict)]

    # 兼容旧版 {"tracks":[...]} 或单轨 {"notes":[...]}
    tracks = load_tracks_json(path)
    chord_tracks = [t for t in tracks if "chord" in t.name.strip().lower()]
    if not chord_tracks:
        chord_tracks = [t for t in tracks if not t.is_drum]
    return notes_to_chord_slices(flatten_notes(chord_tracks))



# ---------------------------------------------------------------------------
# 秒级 JSON
# ---------------------------------------------------------------------------
def save_tracks_json(
    path: Union[str, Path],
    tracks: Sequence[TrackNotes],
    *,
    source: Optional[str] = None,
    kind: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    tempo_bpm: Optional[float] = None,
) -> Path:
    """把轨道写成统一秒级 JSON。

    结构::

        {
          "version": 1,
          "kind": "melody_vocal",
          "source": "clip30.wav",
          "tpb": 480, "tempo_bpm": 120.0,
          "tracks": [
            {"name": "Vocal", "is_drum": false, "program": 0,
             "notes": [{"pitch": 72, "start_sec": 1.23, "end_sec": 1.75, "velocity": 90}]}
          ]
        }
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any] = {
        "version": JSON_VERSION,
        "kind": kind or "",
        "source": source or "",
        "tpb": DEFAULT_TPB,
        "tempo_bpm": float(tempo_bpm) if tempo_bpm else DEFAULT_BPM,
        "tracks": [t.to_dict() for t in tracks],
    }
    if extra:
        payload.update(extra)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path


def load_tracks_json(path: Union[str, Path]) -> List[TrackNotes]:
    """读取秒级 JSON，兼容几种宽松格式（裸列表 / 单轨 dict / 标准 tracks）。"""
    path = Path(path)
    with open(path, "r", encoding="utf-8-sig") as fh:
        data = json.load(fh)

    if isinstance(data, list):
        return [TrackNotes.from_dict({"name": path.stem, "notes": data}, path.stem)]

    if not isinstance(data, dict):
        raise ValueError(f"无法识别的 JSON 结构: {path}")

    if isinstance(data.get("tracks"), list):
        tracks: List[TrackNotes] = []
        for i, item in enumerate(data["tracks"]):
            if isinstance(item, dict):
                tracks.append(TrackNotes.from_dict(item, f"Track {i + 1}"))
            elif isinstance(item, list):
                tracks.append(TrackNotes.from_dict({"name": f"Track {i + 1}", "notes": item},
                                                   f"Track {i + 1}"))
        return tracks

    # 单轨：{"name": ..., "notes": [...]} 或 {"notes": [...]}
    if isinstance(data.get("notes"), list):
        return [TrackNotes.from_dict(data, data.get("name") or path.stem)]

    # 兼容 {"Vocal": {...}, "Ins": {...}} 这种按轨名分组的写法
    tracks = []
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(value.get("notes"), list):
            item = dict(value)
            item.setdefault("name", key)
            tracks.append(TrackNotes.from_dict(item, key))
        elif isinstance(value, list):
            try:
                tracks.append(TrackNotes.from_dict({"name": key, "notes": value}, key))
            except Exception:
                continue
    if tracks:
        return tracks
    raise ValueError(f"JSON 里找不到音符数据: {path}")


# ---------------------------------------------------------------------------
# MIDI 读写
# ---------------------------------------------------------------------------
def read_midi_tracks(path: Union[str, Path]) -> List[TrackNotes]:
    """用 pretty_midi 读取 MIDI，全部转成绝对秒；保留轨名与鼓轨标记。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"MIDI 不存在: {path}")
    pm = pretty_midi.PrettyMIDI(str(path))
    tracks: List[TrackNotes] = []
    for idx, inst in enumerate(pm.instruments):
        notes = [Note(n.pitch, n.start, n.end, n.velocity) for n in inst.notes]
        notes = [n for n in notes if n.end_sec > n.start_sec + 1e-9]
        notes.sort(key=lambda n: (n.start_sec, n.end_sec, n.pitch))
        name = (inst.name or "").strip()
        if not name:
            name = "Drums" if inst.is_drum else f"Track {idx + 1}"
        tracks.append(TrackNotes(name=name, notes=notes, is_drum=bool(inst.is_drum),
                                 program=int(inst.program)))
    return tracks


def _safe_track_name(name: str, index: int) -> str:
    """mido 的 meta 文本按 latin-1 编码；非 ASCII 轨名降级为 Track N。"""
    text = (name or "").strip() or f"Track {index + 1}"
    try:
        text.encode("latin-1")
        return text
    except UnicodeEncodeError:
        return f"Track {index + 1}"


def write_midi(
    path: Union[str, Path],
    tracks: Sequence[TrackNotes],
    *,
    ticks_per_beat: int = DEFAULT_TPB,
    bpm: float = DEFAULT_BPM,
    tempo_bpm: Optional[float] = None,
    text_events: Optional[Sequence[tuple]] = None,
) -> Path:
    """把轨道写成 MIDI（默认 480 tpb；bpm 为真实速度，未给时 120 占位）。

    `text_events`：`[(秒, 文本)]`，写进 Track 0 的 Meta Text 事件。v3.1 用它承载
    `P2_MODE:chord` / `P2_MODE:fret` 混切标记，另存后重新加载即可复原混切序列。
    """
    if tempo_bpm:
        bpm = tempo_bpm
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    mid = mido.MidiFile(ticks_per_beat=int(ticks_per_beat))
    us_per_beat = int(round(60_000_000 / float(bpm)))
    tick_per_sec = float(ticks_per_beat) * float(bpm) / 60.0
    first_track = True

    def sec_to_tick(sec: float) -> int:
        return int(round(max(0.0, float(sec)) * tick_per_sec))

    for t_idx, track in enumerate(tracks):
        mt = mido.MidiTrack()
        mid.tracks.append(mt)
        events: List[tuple] = []

        def add(base_tick: int, order: int, msg: mido.Message) -> None:
            events.append((max(0, int(base_tick)), order, msg))

        add(0, 0, mido.MetaMessage("track_name", name=_safe_track_name(track.name, t_idx), time=0))
        if first_track:
            # 只在第一条轨写一次速度/拍号，避免多出无名指挥轨
            add(0, 1, mido.MetaMessage("set_tempo", tempo=us_per_beat, time=0))
            add(0, 2, mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
            first_track = False
        program = max(0, min(127, int(track.program)))
        if program:
            add(0, 3, mido.Message("program_change", program=program, channel=0, time=0))

        if t_idx == 0 and text_events:
            for (when, text) in text_events:
                try:
                    add(sec_to_tick(float(when)), 2,
                        mido.MetaMessage("text", text=str(text), time=0))
                except Exception:
                    continue

        for note in track.notes:
            if note.end_sec <= note.start_sec:
                continue
            t_on = sec_to_tick(note.start_sec)
            t_off = max(sec_to_tick(note.end_sec), t_on + 1)
            vel = max(1, min(127, int(note.velocity)))
            add(t_on, 6, mido.Message("note_on", note=int(note.pitch), velocity=vel, channel=0, time=0))
            add(t_off, 4, mido.Message("note_off", note=int(note.pitch), velocity=0, channel=0, time=0))

        events.sort(key=lambda e: (e[0], e[1]))
        last_tick = 0
        for tick, _order, msg in events:
            msg.time = max(0, tick - last_tick)
            mt.append(msg)
            last_tick = tick
        mt.append(mido.MetaMessage("end_of_track", time=0))

    if not mid.tracks:
        mid.tracks.append(mido.MidiTrack())
        mid.tracks[0].append(mido.MetaMessage("end_of_track", time=0))

    mid.save(str(path))
    return path


# ---------------------------------------------------------------------------
# 轨道选择 / 通用读取
# ---------------------------------------------------------------------------
def select_track(
    tracks: Sequence[TrackNotes],
    preferred: Sequence[str],
    *,
    fallback_all: bool = True,
    require: bool = False,
) -> TrackNotes:
    """按优先轨名取一条轨；找不到时退回第一条非鼓轨 / 合并所有非鼓轨。"""
    lowered = [p.lower() for p in preferred]
    for want in lowered:
        for t in tracks:
            if t.name.strip().lower() == want:
                return t.copy()
    for want in lowered:
        for t in tracks:
            if want in t.name.strip().lower():
                return t.copy()
    if not fallback_all:
        # 明确要求“按轨名取”：取不到就返回空轨，绝不拿别的轨冒充
        if require and not tracks:
            raise ValueError(f"MIDI 里没有任何轨道（期望 {preferred}）")
        return TrackNotes(name=preferred[0], notes=[])

    non_drum = [t for t in tracks if not t.is_drum]
    if non_drum:
        if len(non_drum) > 1:
            merged: List[Note] = []
            for t in non_drum:
                merged.extend(t.copy().notes)
            merged.sort(key=lambda n: (n.start_sec, n.end_sec, n.pitch))
            return TrackNotes(name=preferred[0], notes=merged, is_drum=False)
        return non_drum[0].copy()
    if require and not tracks:
        raise ValueError(f"MIDI 里没有任何轨道（期望 {preferred}）")
    return TrackNotes(name=preferred[0], notes=[])


def select_tracks_by_keyword(
    tracks: Sequence[TrackNotes], keyword: str, *, fallback_all: bool = True
) -> List[TrackNotes]:
    hits = [t.copy() for t in tracks if keyword.lower() in t.name.strip().lower()]
    if hits:
        return hits
    if not fallback_all:
        return []
    return [t.copy() for t in tracks if not t.is_drum]


def read_notes_any(path: Union[str, Path]) -> List[TrackNotes]:
    """按扩展名读取 JSON 或 MIDI。"""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        return load_tracks_json(path)
    if suffix in (".mid", ".midi"):
        return read_midi_tracks(path)
    raise ValueError(f"不支持的输入格式: {path}（只支持 .json/.mid/.midi）")


def count_notes(tracks: Sequence[TrackNotes]) -> int:
    return sum(len(t.notes) for t in tracks)


def flatten_notes(tracks: Sequence[TrackNotes]) -> List[Note]:
    out: List[Note] = []
    for t in tracks:
        out.extend(n.copy() for n in t.notes)
    out.sort(key=lambda n: (n.start_sec, n.end_sec, n.pitch))
    return out


def apply_program(track: TrackNotes, program: int) -> TrackNotes:
    track.program = max(0, min(127, int(program)))
    return track


def assert_monophonic(notes: Sequence[Note], *, eps: float = 1e-6) -> None:
    """断言同一时刻严格只有 1 个音（相邻音只允许首尾相接）。

    这是主旋律的硬约束；合并模块与步骤5入口都会调用。
    """
    ordered = sorted(notes, key=lambda n: (n.start_sec, n.end_sec, n.pitch))
    for prev, cur in zip(ordered, ordered[1:]):
        if cur.start_sec < prev.end_sec - eps:
            raise AssertionError(
                "主旋律出现重叠音："
                f"pitch {prev.pitch} [{prev.start_sec:.3f},{prev.end_sec:.3f}] 与 "
                f"pitch {cur.pitch} [{cur.start_sec:.3f},{cur.end_sec:.3f}]"
            )


def monophonic_violations(notes: Sequence[Note], *, eps: float = 1e-6) -> List[Tuple[int, int]]:
    """返回重叠音符对的下标（自测 / GUI 校验用；空列表即严格单音）。"""
    ordered = sorted(notes, key=lambda n: (n.start_sec, n.end_sec, n.pitch))
    pairs: List[Tuple[int, int]] = []
    for i, (prev, cur) in enumerate(zip(ordered, ordered[1:])):
        if cur.start_sec < prev.end_sec - eps:
            pairs.append((i, i + 1))
    return pairs
