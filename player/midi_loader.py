# -*- coding: utf-8 -*-
"""midi_loader.py -- 读 MIDI（**不含任何钢琴内容**）。

跟"游戏钢琴 36 键"有关的东西（PIANO_KEY_MAP、fold_to_piano_range、36 键音域压缩）
都不在本文件里 —— 品格模式覆盖 **E2~E6（MIDI 40~88，五个八度）**，普通 MIDI 直接就能弹。

本文件负责：
* 按 track 解析音符、算时值、忽略打击乐通道（channel 9），否则鼓点会混进旋律；
* 内嵌和弦标记（Guitar Pro 导出的 marker / lyrics / text）—— 和弦模式的现成答案；
* 每轨独立收集音符，供"自动挑主旋律轨"使用。
"""
import os
import re
from collections import Counter

import mido

NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

# 品格模式的覆盖音域：1 弦空弦 E2(40) ~ 6 弦 24 品 E6(88)
GUITAR_MIN_NOTE = 40
GUITAR_MAX_NOTE = 88

def midi_note_to_name(note_num):
    note_num = int(round(note_num))
    return "%s%d" % (NOTE_NAMES[note_num % 12], note_num // 12 - 1)

def build_time_converter(mid):
    """
    造一个"绝对 tick → 秒"的换算函数。

    **为什么必须自己算**：mido 迭代一整首 `MidiFile` 时会把 tick 换成秒（内部按 tempo map 做），
    但**逐轨迭代 `MidiTrack` 时给的是 tick**。这个项目要按轨挑主旋律，所以必须自己换算。
    不换算的后果：首音会变成 31920"秒"，其实是 960 tick/拍下的 31920 tick = 24.63 秒。
    """
    tpb = mid.ticks_per_beat or 480

    # 1) 全局 tempo map：mido 的 merge_tracks 就是这么做的（任何轨里的 set_tempo 都全局生效）
    changes = []
    for track in mid.tracks:
        tick = 0
        for msg in track:
            tick += msg.time
            if msg.type == 'set_tempo':
                changes.append((tick, msg.tempo))
    changes.sort()
    if not changes or changes[0][0] > 0:
        changes.insert(0, (0, 500000))       # 缺省 120 BPM

    # 2) 预先算好每一段 tempo 的起点（tick, 秒），避免每条消息都从头积分
    segs = []
    sec = 0.0
    prev_tick = 0
    tempo = changes[0][1]
    for (t, tp) in changes:
        if t > prev_tick:
            segs.append((prev_tick, t, sec, tempo))
            sec += mido.tick2second(t - prev_tick, tpb, tempo)
            prev_tick = t
        tempo = tp
    segs.append((prev_tick, float('inf'), sec, tempo))

    def conv(tick):
        for (a, b, s0, tp) in segs:
            if a <= tick < b:
                return s0 + mido.tick2second(tick - a, tpb, tp)
        return sec

    return conv

def fold_into_guitar_range(note):
    """超出 E2~E6 的音按八度折回来（保住音名，只换八度）。"""
    note = int(round(note))
    while note < GUITAR_MIN_NOTE:
        note += 12
    while note > GUITAR_MAX_NOTE:
        note -= 12
    return note

class NoteEvent(object):
    def __init__(self, time_sec, note, velocity, duration=0.1, channel=0, track_idx=0):
        self.time_sec = float(time_sec)
        self.note = int(note)
        self.velocity = int(velocity)
        self.duration = float(duration)
        self.channel = int(channel)
        self.track_idx = int(track_idx)

    def __repr__(self):
        return "NoteEvent(t=%.3f, %s, dur=%.3f)" % (
            self.time_sec, midi_note_to_name(self.note), self.duration)

class FlatNote(object):
    """扁平音符（给和弦识别用）。字段名与 `chord_recognizer` 的调用方约定一致，
    另外给几个更好记的别名。"""
    __slots__ = ("start", "pitch", "velocity", "length", "channel", "track_idx")

    def __init__(self, start, pitch, velocity, length, channel=0, track_idx=0):
        self.start = float(start)
        self.pitch = int(pitch)
        self.velocity = int(velocity)
        self.length = float(length)
        self.channel = int(channel)
        self.track_idx = int(track_idx)

    # 别名
    @property
    def time_sec(self):
        return self.start

    @property
    def note(self):
        return self.pitch

    @property
    def duration(self):
        return self.length

    def __repr__(self):
        return "FlatNote(t=%.3f, %s, len=%.3f)" % (
            self.start, midi_note_to_name(self.pitch), self.length)

def read_flat_notes(path, drop_drums=True, tracks=None):
    """
    读成扁平音符表（**唯一的一份 MIDI 读取器**）。

    返回 (notes, meta)；notes 按开始时间排序，时间是**秒**。
    tracks: 只要这几条轨（轨号列表）；None = 全部。
    全项目**只有这一份** MIDI 读取器。
    """
    mid = mido.MidiFile(path)
    conv = build_time_converter(mid)
    notes = []
    tempos = []
    tempo_events = []

    for track_idx, track in enumerate(mid.tracks):
        if tracks is not None and track_idx not in tracks:
            continue
        active = {}
        tick = 0
        for msg in track:
            tick += msg.time
            if msg.type == 'set_tempo':
                bpm_value = mido.tempo2bpm(msg.tempo)
                tempos.append(bpm_value)
                tempo_events.append((round(conv(tick), 6), round(float(bpm_value), 4)))
                continue
            if drop_drums and getattr(msg, 'channel', 0) == 9:
                continue
            if msg.type == 'note_on' and msg.velocity > 0:
                key = (msg.channel, msg.note)
                active.setdefault(key, []).append((tick, msg.velocity))
            elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                key = (msg.channel, msg.note)
                if active.get(key):
                    start_tick, vel = active[key].pop(0)
                    notes.append(FlatNote(conv(start_tick), msg.note, vel,
                                          max(0.02, conv(tick) - conv(start_tick)),
                                          msg.channel, track_idx))
        for (channel, pitch), pending in active.items():
            for start_tick, vel in pending:
                notes.append(FlatNote(conv(start_tick), pitch, vel, 0.25, channel, track_idx))

    notes.sort(key=lambda n: (n.start, n.pitch))
    tempo_events.sort()
    if not tempo_events:
        tempo_events = [(0.0, 120.0)]
    meta = {
        "bpm": round(tempos[0], 1) if tempos else 120.0,
        "tempo_map": tempo_events,
        "ticks_per_beat": mid.ticks_per_beat,
        "tracks": len(mid.tracks),
        "length": mid.length,
        "note_count": len(notes),
    }
    return notes, meta

# ══════════════════════════════════════════════════════════════════════════════
#  适配本软件的 MIDI 格式 —— 读取与分析
#
#  格式要求就是本节这几条；体检器在 `a_selfcheck.py`。
#  一份合格的歌 = **一个 .mid 文件**，里面：
#    * 一条**主旋律轨**（单音，名字含 Melody/旋律/Sing/Vocal…）→ 品格模式
#    * 一条**伴奏轨**（和弦音，名字含 Chords/伴奏/Accomp…）→ 和弦模式
#    * **和弦标记**（marker / 歌词，如 `G`、`Am7`、`G/B`）→ 和弦走向
#  有了伴奏轨的真实起音时刻，和弦模式就**不需要预设节奏型**了 —— 节奏跟着文件走。
# ══════════════════════════════════════════════════════════════════════════════

# 轨名线索：谁像伴奏、谁像主旋律（与 melody.py 的分类保持一致，避免两处打架）
ACCOMP_NAME_WORDS = ("chord", "accomp", "harmony", "pad", "comp", "guitar",
                     "和弦", "伴奏", "和声", "背景")
MELODY_NAME_WORDS = ("melody", "vocal", "sing", "voice", "lead", "solo",
                     "主旋律", "旋律", "人声", "唱")

# 和弦符号：根音 + 可选后缀 + 可选转位低音。只认**整段就是这个和弦**的字符串，
# 所以 "Chorus in G" 这种散文不会被误判（它不以和弦形状结束）。
# 根音大小写都收（转谱软件常写小调 `am`、`g`），后面统一把根音抬成大写。
_CHORD_RE = re.compile(
    r"^[A-Ga-g][#b]?"
    r"(?:maj|min|dim|aug|sus|add|m|M|\+|-|\d|no|°|ø|/)*"
    r"(?:/[A-Ga-g][#b]?)?$")

def normalize_chord_text(txt):
    """
    把 MIDI 里的一条标记文本变成和弦符号；不是和弦就返回 None。

    认得的写法（都可以，软件内部统一成大写根音的标准写法）：
        `G`  `Am`  `Bm`  `C`  `D`  `Em`  `Cmaj7`  `Cadd9`  `G7`  `G/B`
        `Chord: G`   `[G]`   `G:maj7`   `Gmaj`
    """
    s = (txt or "").strip()
    if not s:
        return None
    # 去掉常见前缀/括号
    for pre in ("chord:", "chord ", "和弦:", "和弦：", "acordes:", "akord:"):
        if s.lower().startswith(pre):
            s = s[len(pre):].strip()
    s = s.strip("[](){} \t")
    # `G:maj7` → `Gmaj7`
    s = s.replace(":", "")
    if not s or len(s) > 12:
        return None
    if not _CHORD_RE.match(s):
        return None
    # 统一根音大写：g → G，bb → Bb
    if len(s) >= 2 and s[1] in ("#", "b"):
        s = s[0].upper() + s[1] + s[2:]
    else:
        s = s[0].upper() + s[1:]
    # `Gmaj` / `Gmin` → 标准后缀
    s = re.sub(r"^([A-G][#b]?)maj$", r"\1", s)
    s = re.sub(r"^([A-G][#b]?)min$", r"\1m", s)
    return s

def read_chord_markers(path, verbose=False):
    """
    读出 MIDI 里的**和弦标记时间轴**。

    返回 [(时间秒, 和弦符号), ...]，按时间排序；没有就返回 []。
    只认 marker 与 lyrics（`text` 常被转谱软件塞进歌名、版权、歌词，误判率高，
    所以默认不看 text；`verbose=True` 时也扫 text，给校验器用）。
    """
    mid = mido.MidiFile(path)
    conv = build_time_converter(mid)
    out = []
    types = ("marker", "lyrics") + (("text",) if verbose else ())
    for track in mid.tracks:
        tick = 0
        for msg in track:
            tick += msg.time
            if msg.type not in types:
                continue
            sym = normalize_chord_text(getattr(msg, "text", ""))
            if sym:
                out.append((round(conv(tick), 4), sym))
    out.sort(key=lambda x: x[0])
    # 同一时刻重复写同一个和弦：去重（转谱软件常重复写一遍）
    dedup = []
    for t, s in out:
        if dedup and abs(dedup[-1][0] - t) < 1e-6 and dedup[-1][1] == s:
            continue
        dedup.append((t, s))
    return dedup

def read_track_onsets(path, track_index, cluster_gap=0.06):
    """
    读一条轨的**扫弦起音簇**（和弦模式的节奏来源）。

    返回 [{"time","direction","count","spread"}]：
      * `time`      = 这一下扫弦的第一个音的时刻（秒）
      * `direction` = `"D"` 下扫 / `"U"` 上扫 / `"D"`（信息不足时的默认）
      * `spread`    = 簇内最早音与最晚音的差 —— 转谱软件若写成了"错开的扫弦"，
                      它就能反推出方向；若写成了完全对齐（spread≈0），就一律下扫。
    """
    mid = mido.MidiFile(path)
    conv = build_time_converter(mid)
    if not (0 <= track_index < len(mid.tracks)):
        return []
    raw = []
    tick = 0
    for msg in mid.tracks[track_index]:
        tick += msg.time
        if msg.type == "note_on" and msg.velocity > 0 and getattr(msg, "channel", 0) != 9:
            raw.append((conv(tick), int(msg.note)))
    raw.sort()
    clusters = []
    for t, n in raw:
        if clusters and (t - clusters[-1][-1][0]) < cluster_gap:
            clusters[-1].append((t, n))
        else:
            clusters.append([(t, n)])
    out = []
    for cl in clusters:
        t0, spread = cl[0][0], cl[-1][0] - cl[0][0]
        if len(cl) > 1 and spread >= 0.02:
            direction = "D" if cl[0][1] < cl[-1][1] else "U"
        else:
            direction = "D"          # 完全对齐（量化过）：没有方向信息，一律下扫
        out.append({"time": round(t0, 4), "direction": direction,
                    "count": len(cl), "spread": round(spread, 4),
                    "pitches": [n for _t, n in cl]})
    return out

def detect_timing_problems(path, min_span=8.0, max_count=8, spread_ratio=0.2):
    """
    找**速度/时间轴异常**（转谱软件最容易犯的错）。

    实测：《稻香》原曲那份 MIDI 把 **169 个速度事件全塞在最后 3.33 秒**里，
    速度从 81 一路塌到 64 BPM；前面 198.5 秒其实是恒定 81 BPM。
    也就是说：那 3 秒的曲线是假的，而任何"按文件的时间轴算秒数"的用法都会在那一段开始漂。

    判据（两条，命中一条就报）：
      * **成群**：≥`max_count` 个速度事件挤在 `min_span` 秒以内；
      * **极不平均**：绝大多数速度事件只占了整首时长的 `spread_ratio` 以下
        （正常音乐的速度变化是均匀散开的；一条 3 秒的"假曲线"会明显偏在一边）。

    返回 [中文警告字符串, ...]。
    """
    mid = mido.MidiFile(path)
    conv = build_time_converter(mid)
    evs = []
    for track in mid.tracks:
        tick = 0
        for msg in track:
            tick += msg.time
            if msg.type == "set_tempo":
                evs.append((round(conv(tick), 4), round(mido.tempo2bpm(msg.tempo), 2)))
    evs.sort()
    if len(evs) <= 1:
        return []
    warns = []
    span = evs[-1][0] - evs[0][0]
    n = len(evs)

    # ① 成群：一小段时间里塞了一大把速度事件
    best = []
    i = 0
    while i < n:
        j = i
        while j + 1 < n and evs[j + 1][0] - evs[i][0] <= min_span:
            j += 1
        if (j - i + 1) > len(best):
            best = evs[i:j + 1]
        i = max(j, i) + 1
    if len(best) >= max_count:
        lo = min(b for _t, b in best)
        hi = max(b for _t, b in best)
        warns.append(
            "速度表异常：有 %d 个速度事件挤在 %.2f 秒内（%.1f~%.1f BPM）—— "
            "这多半是转谱软件写的假曲线，请让扒谱程序输出**恒定速度**"
            % (len(best), best[-1][0] - best[0][0], lo, hi))
        return warns            # 群的形状已经说清楚了，不必再叠一条

    # ② 极不平均：绝大多数速度事件挤在整首的一小块里
    if n >= 4 and span > 1.0:
        core = evs[1:-1] or evs
        core_span = core[-1][0] - core[0][0]
        if core_span <= span * spread_ratio:
            warns.append(
                "速度表异常：%d 个速度事件里有 %d 个只分布在整首的 %.1f%% 时长里"
                "（%.1f~%.1f BPM）—— 时间轴会在那一段开始漂，建议改成恒定速度"
                % (n, len(core), 100.0 * core_span / span,
                   min(b for _t, b in evs), max(b for _t, b in evs)))

    # ③ 速度整体跨度超过一倍
    bpms = [b for _t, b in evs]
    if max(bpms) > 2.0 * min(bpms):
        warns.append("速度表跨度太大：%.1f~%.1f BPM，请确认这是有意的" % (min(bpms), max(bpms)))
    return warns

def analyze_format(path):
    """
    判断这份 MIDI **能不能直接用**，并指认主旋律轨 / 伴奏轨 / 和弦标记。

    返回 dict：
        ok           : 有和弦标记、且能算出和弦模式（品格模式另有判断）
        has_markers  : 和弦标记条数
        accomp_track : (轨号, 轨名) 或 None
        melody_track : (轨号, 轨名) 或 None
        onsets       : 伴奏轨的起音簇数
        issues       : 阻碍演奏的问题（中文）
        notes        : 提示性的话（中文）
    """
    mid = mido.MidiFile(path)
    track_names = [(t.name or "").strip() for t in mid.tracks]

    # 每轨音符数（忽略鼓）
    counts = [0] * len(mid.tracks)
    simultaneous = [0] * len(mid.tracks)
    for ti, track in enumerate(mid.tracks):
        active = 0
        mx = 0
        for msg in track:
            if getattr(msg, "channel", 0) == 9:
                continue
            if msg.type == "note_on" and msg.velocity > 0:
                counts[ti] += 1
                active += 1
                mx = max(mx, active)
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                active = max(0, active - 1)
        simultaneous[ti] = mx

    markers = read_chord_markers(path)
    issues, notices = [], []

    # ── 伴奏轨：优先看名字，其次看"多音同时响"这条物理特征 ──────────────
    accomp = None
    for ti, name in enumerate(track_names):
        low = name.lower()
        if counts[ti] and any(w in low for w in ACCOMP_NAME_WORDS):
            accomp = (ti, name or ("音轨 %d" % (ti + 1)))
            break
    if accomp is None:
        cands = [(counts[ti], ti) for ti in range(len(mid.tracks))
                 if counts[ti] and simultaneous[ti] >= 3 and (ti, track_names[ti]) != accomp]
        if cands:
            cnt, ti = max(cands)
            accomp = (ti, track_names[ti] or ("音轨 %d" % (ti + 1)))
            notices.append("伴奏轨是靠「同时响 3 个音以上」猜出来的（轨名里没有 Chords/伴奏 字样），"
                           "建议按规范给轨起名")

    # ── 主旋律轨：名字优先，其次"单音 + 音符数最多" ─────────────────────
    melody = None
    for ti, name in enumerate(track_names):
        low = name.lower()
        if counts[ti] and any(w in low for w in MELODY_NAME_WORDS):
            melody = (ti, name or ("音轨 %d" % (ti + 1)))
            break
    if melody is None:
        mono = [(counts[ti], ti) for ti in range(len(mid.tracks))
                if counts[ti] and simultaneous[ti] <= 2 and ti != (accomp[0] if accomp else -1)]
        if mono:
            cnt, ti = max(mono)
            melody = (ti, track_names[ti] or ("音轨 %d" % (ti + 1)))

    onsets = read_track_onsets(path, accomp[0]) if accomp else []

    # ── 结论 ────────────────────────────────────────────────────────────
    if not markers:
        notices.append("这份 MIDI 里没有和弦标记：和弦模式会退回「自动识别」（软件猜的，可能不准），"
                       "品格模式不受影响")
    if accomp is None:
        notices.append("找不到伴奏轨：和弦模式只能靠自动识别，或者走旁边的 .progression.json")
    if melody is None:
        issues.append("找不到主旋律轨：品格模式不可用")
    if accomp and not onsets:
        issues.append("伴奏轨里没有音符")
    issues.extend(detect_timing_problems(path))

    return {
        "path": path,
        "ok": bool(markers) and accomp is not None and melody is not None,
        "has_markers": len(markers),
        "markers": markers,
        "accomp_track": accomp,
        "melody_track": melody,
        "onsets": onsets,
        "track_names": track_names,
        "issues": issues,
        "notes": notices,
    }

class MidiSong(object):
    def __init__(self, file_path):
        self.file_path = file_path
        self.file_name = os.path.basename(file_path)
        self.total_duration = 0.0
        self.events = []            # 所有音符事件（按时间排序）
        self.raw_notes = []         # 只留音高（给统计用）
        self.track_names = []
        self.bpm = 120.0
        self.chord_markers = []     # [(时间, 和弦名)]，来自 MIDI 内嵌标记
        self.notes_by_track = {}    # {轨道号: [NoteEvent]}
        self._load()
    def _load(self):
        mid = mido.MidiFile(self.file_path)
        self.total_duration = float(mid.length) if mid.length and mid.length > 0 else 1.0

        for track in mid.tracks:
            for msg in track:
                if msg.type == 'set_tempo':
                    self.bpm = round(mido.tempo2bpm(msg.tempo), 1)
                    break

        try:
            from chord_map import get_chord_location
        except Exception:
            get_chord_location = None

        conv = build_time_converter(mid)          # 绝对 tick → 秒（逐轨迭代拿到的是 tick！）

        # 必须**按轨道分别迭代**：mido 整体迭代时不告诉你消息属于哪条轨。
        for track_idx, track in enumerate(mid.tracks):
            name = track.name.strip() if track.name else "音轨 %d" % (track_idx + 1)
            self.track_names.append(name)
            self.notes_by_track[track_idx] = []

            active = {}          # (channel, note) -> (start_time, velocity)
            tick = 0

            for msg in track:
                tick += msg.time
                current_time = conv(tick)

                if msg.type in ('marker', 'lyrics', 'text'):
                    txt = (getattr(msg, 'text', '') or '').strip()
                    if txt and get_chord_location:
                        clean = txt.replace('Chord:', '').replace('[', '').replace(']', '').strip()
                        if get_chord_location(clean):
                            self.chord_markers.append((current_time, clean))

                # 打击乐通道（channel 10 = index 9）直接忽略，否则鼓点混进旋律
                if getattr(msg, 'channel', None) == 9:
                    continue

                if msg.type == 'note_on' and msg.velocity > 0:
                    key = (msg.channel, msg.note)
                    if key in active:           # 同一个音没关又按下：把上一次收尾
                        start_t, vel = active.pop(key)
                        self._add(track_idx, start_t, msg.note, vel, current_time)
                    active[key] = (current_time, msg.velocity)
                    self.raw_notes.append(msg.note)
                elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                    key = (msg.channel, msg.note)
                    if key in active:
                        start_t, vel = active.pop(key)
                        self._add(track_idx, start_t, msg.note, vel, current_time)

            for (channel, note), (start_t, vel) in active.items():
                self._add(track_idx, start_t, note, vel, current_time)

        self.chord_markers.sort(key=lambda x: x[0])
        self.events.sort(key=lambda e: e.time_sec)
        for k in self.notes_by_track:
            self.notes_by_track[k].sort(key=lambda e: e.time_sec)

    def _add(self, track_idx, start_t, note, vel, end_t):
        dur = max(0.08, end_t - start_t)
        ev = NoteEvent(start_t, note, vel, dur, 0, track_idx)
        self.events.append(ev)
        self.notes_by_track.setdefault(track_idx, []).append(ev)

    def track_summary(self):
        """每条轨道：音符数、音域、平均音高。挑主旋律轨要用。"""
        out = []
        for idx in sorted(self.notes_by_track):
            notes = self.notes_by_track[idx]
            if not notes:
                out.append({"index": idx, "name": self.track_names[idx] if idx < len(self.track_names) else "",
                            "notes": 0, "min": None, "max": None, "avg": None})
                continue
            vals = [n.note for n in notes]
            out.append({"index": idx,
                        "name": self.track_names[idx] if idx < len(self.track_names) else "",
                        "notes": len(vals), "min": min(vals), "max": max(vals),
                        "avg": sum(vals) / float(len(vals))})
        return out

    def in_range_ratio(self, shift=0):
        if not self.raw_notes:
            return 0.0
        ok = sum(1 for n in self.raw_notes if GUITAR_MIN_NOTE <= (n + shift) <= GUITAR_MAX_NOTE)
        return ok / float(len(self.raw_notes))

    def optimal_octave_shift(self):
        """只按八度整体移位，保住调性和音程；挑覆盖率最高的那个。"""
        best_shift, best = 0, self.in_range_ratio(0)
        for shift in (-24, -12, 12, 24):
            r = self.in_range_ratio(shift)
            if r > best:
                best, best_shift = r, shift
        return best_shift

    def most_common_notes(self, n=5):
        c = Counter(self.raw_notes)
        return c.most_common(n)
