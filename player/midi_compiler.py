# -*- coding: utf-8 -*-
"""midi_compiler.py -- 《异键演奏》的**编译器**：4 轨 .mid → 指法 → 硬件动作时间轴。

════════════════════════════════════════════════════════════════════════════
四件事，一件比一件底层：

  ① 读歌      `load_song()`      Track 0 的 `SOLO_MODE:` / `CHORD:` 标记，
                                 按轨名找 SOLO_MASTER / DUET_P1_LEAD /
                                 DUET_P2_RHYTHM，取出每轨音符。
  ② 分帧      `build_frames()`   起始时间差 ≤ 15ms 的音算"同时响"。
  ③ 定指法    `compile_fret()`   品格模式：束搜索（beam_width=8）选品格/弦位；
              `compile_chord()`  和弦模式：≥3 音走扫弦，单音/分解走数字键。
  ④ 反向倒推  `preschedule()`    从每个音的 `T_on` 倒推翻页/选格，串上 18 项参数，
                                 输出**硬件动作时间轴**（每条动作带绝对时刻）。

════════════════════════════════════════════════════════════════════════════
三条贯穿全片的做法：

  * **绝对时刻，不是等待**：时间轴里每个动作都写"第几秒做"，执行器只负责卡点。
    翻页/选格/拨弦的耗时是**算**出来的，误差只剩卡点精度（1ms 时钟 + 自旋）。
  * **换格链从 `T_on` 往回排**（计划 1.4）；链子撞到上一个音就整条往后平移，
    并记一笔"这个音晚了多少"——绝不偷偷吞音。
  * **参数一律现读** `player_config.json`（18 项），不写死常量。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mido

import midi_adapter             # 外部 MIDI → 旧契约 song 结构（八度归位/合流/2P）
import modes                    # 24 和弦指型 / 品格表（全项目唯一出处）
import player_config            # 18 项时间参数
from midi_loader import (build_time_converter, normalize_chord_text,
                         read_flat_notes, read_chord_markers)

# ══════════════════════════════════════════════════════════════════════════
#  常量
# ══════════════════════════════════════════════════════════════════════════
OPEN_PITCH = {1: 40, 2: 45, 3: 50, 4: 55, 5: 59, 6: 64}   # 1=E2(最粗) … 6=E4(最细)
PITCH_TO_STRING = {v: k for k, v in OPEN_PITCH.items()}
MAX_FRET = 24
FRAME_GAP = 0.015          # 计划 1.2：起始时间差 ≤ 15ms 算同一帧
BEAM_WIDTH = 8             # 计划 1.2：束搜索每步保留 8 条路径
PAGE_ZERO = 4              # 品格模式第 5 页（0 品）的 0 起算页号

# 角色（计划 1.1）
ROLE_SOLO = "solo"
ROLE_DUET_LEAD = "duet_lead"
ROLE_DUET_RHYTHM = "duet_rhythm"
ROLE_DUET_RHYTHM_FRET = "duet_rhythm_fret"        # 2P 伴奏，但锁品格模式
ROLES = (ROLE_SOLO, ROLE_DUET_LEAD, ROLE_DUET_RHYTHM, ROLE_DUET_RHYTHM_FRET)
ROLE_CN = {ROLE_SOLO: "单人独奏", ROLE_DUET_LEAD: "双人合奏 - 1P 主音（品格）",
           ROLE_DUET_RHYTHM: "双人合奏 - 2P 伴奏（和弦）",
           ROLE_DUET_RHYTHM_FRET: "双人合奏 - 2P 伴奏（品格）"}

TRACK_SOLO_MASTER = "SOLO_MASTER"
TRACK_DUET_LEAD = "DUET_P1_LEAD"
TRACK_DUET_RHYTHM = "DUET_P2_RHYTHM"
# 老格式兼容：本机现有扒谱器产物是 Melody / Chords，没有 A 的 4 轨契约
TRACK_ALIASES = {
    TRACK_SOLO_MASTER: ("solo_master", "melody", "旋律", "主旋律", "lead", "vocal"),
    TRACK_DUET_LEAD: ("duet_p1_lead", "duet_lead", "melody", "旋律", "主旋律", "lead"),
    TRACK_DUET_RHYTHM: ("duet_p2_rhythm", "duet_rhythm", "chord", "chords",
                        "伴奏", "和弦", "accomp", "harmony"),
}

# ══════════════════════════════════════════════════════════════════════════
#  19 项时间参数（计划书编号 #1~#19 ↔ player_config 的键名）
# ══════════════════════════════════════════════════════════════════════════
PARAM_KEYS = {
    1: "min_action_interval",
    2: "fret_click_hold",
    4: "pluck_hold",
    5: "sustain_max",
    6: "select_pre_wait",
    7: "select_click_hold",
    8: "strum_park_wait",
    9: "strum_click_hold",
    10: "strum_hold",
    11: "arpeggio_gap",
    12: "page_key_hold",
    13: "page_turn_gap",
    14: "after_page_key_release",
    15: "after_wheel_click_release",
    16: "settle_after_page_turn",
    17: "settle_same_page",
    18: "settle_page_turn",
    19: "sustain_min",
    20: "repluck_gap",
    21: "start_delay",
    22: "preschedule_lead",
}

class Timing(object):
    """18 项参数的只读快照（构造时从 `player_config` 现读，缺项退默认）。"""

    def __init__(self, values=None):
        src = dict(player_config.load_timing())
        if values:
            src.update(values)
        for n, key in PARAM_KEYS.items():
            setattr(self, "p%d" % n, float(src.get(key, player_config.TIMING_DEFAULTS[key])))
        self.raw = src

    def as_dict(self):
        return {("p%d" % n): getattr(self, "p%d" % n) for n in PARAM_KEYS}

    def page_key_hold(self):
        """翻页键实际按住时长 = #12（只留 5ms 安全底线）。"""
        return max(self.p12, 0.005)

    def key_hold(self):
        """数字键拨弦的按住时长 = #4（只留 5ms 安全底线）。"""
        return max(self.p4, 0.005)

    def click_hold(self, kind=None):
        """点扇区按住时长 = #2（品格）或 #7（和弦），5ms 安全底线。"""
        p = self.p2 if str(kind or "").lower() == "fret" else self.p7
        return max(p, 0.005)

    def strum_hold_short(self):
        """扫弦点一下按住时长 = #9，5ms 安全底线。"""
        return max(self.p9, 0.005)

    def repluck_gap(self):
        """同一根弦两次按下最小间隔 = #20，5ms 安全底线。"""
        return max(self.p20, 0.005)

    def switch_span(self, steps, cross_page=False, need_click=True):
        """上一个音拨响 → 下一个音准时响，需要预留的真实时长。"""
        steps = int(steps or 0)
        if steps <= 0:
            span = self.p1 + self.p17
            if need_click:
                span += self.p2
            return round(span, 6)
        if steps == 1:
            span = self.p12 + self.p14 + self.p16
            if need_click:
                span += self.p2 + self.p18
            return round(span, 6)
        span = (steps * self.p12 + (steps - 1) * self.p13
                + self.p14 + self.p16)
        if need_click:
            span += self.p2 + self.p18
        return round(span, 6)

    def advance_need(self, steps, cross_page=False, need_click=True):
        """提前量 = switch_span + #1 + #22。"""
        return round(self.switch_span(steps, cross_page, need_click)
                     + self.p1 + self.p22, 6)

    def page_part(self, steps):
        """翻 steps 页本身要多久（按键 + 连按间隔 + 松开后的根音延迟 #14）。"""
        if steps <= 0:
            return 0.0
        return round(steps * self.p12 + (steps - 1) * self.p13 + self.p14, 6)

# 同一帧（同品格）的多个音：**同时按下**（游戏实测"6 根弦之间无密度限制"）。
# 若将来发现同时按会吞音，把它改成 False，就会按 #11 琶音间隔依次按下。
STRINGS_SIMULTANEOUS = True

#  ① **只有翻页是"松开判定"**：翻页键**松开后 35 毫秒**根音才变；
#  ② **其余全是"按下判定"**：拨弦/点扇区都是按下那一刻就生效，0 延迟；
#  ③ 任何按键**按下后要保持 25 毫秒**游戏才捕捉得到（短了会被吞）；
#  ④ 同一根弦要再拨：两次按下之间至少隔 **25 毫秒**，否则游戏不认；
#  ⑤ 鼠标左右键扫弦要按住 **30 毫秒**（比按键更"重"，需要更长）。
# 不再写死 25ms/30ms/35ms 的下限；只保留 5ms 的物理安全底线。
MIN_HOLD = 0.005
# 有余量时，要不要把翻页再往前挪（默认 0 = 不动；见文件后面的说明）
PAGE_SETTLE_MAX = 0.0

# ══════════════════════════════════════════════════════════════════════════
#  ① 读歌
# ══════════════════════════════════════════════════════════════════════════
def _track_index_by_name(names, wanted, aliases=()):
    """按轨名找轨号：先精确、再包含（别名）。找不到返回 None。"""
    def norm(x):
        return (x or "").strip().lower()

    w = norm(wanted)
    for i, name in enumerate(names):
        if norm(name) == w:
            return i
    for key in [w] + [norm(a) for a in aliases]:
        for i, name in enumerate(names):
            if key and key in norm(name):
                return i
    return None

def _read_p2_mode_markers(mid):
    """Track 0 的 `P2_MODE:fret|chord` 文本事件 → [(秒, 模式)]，按时间排序。

    写法与 `SOLO_MODE:` 一致（大小写不敏感，只认 fret / chord）。
    """
    out = []
    if not mid.tracks:
        return out
    conv = build_time_converter(mid)
    tick = 0
    for msg in mid.tracks[0]:
        tick += msg.time
        if msg.type != "text":
            continue
        up = (msg.text or "").strip().upper()
        if not up.startswith("P2_MODE:"):
            continue
        v = up.split(":", 1)[1].strip().lower()
        if v in ("fret", "chord"):
            out.append((round(conv(tick), 6), v))
    out.sort(key=lambda x: x[0])
    return out

def read_meta(path):
    """读 Track 0 的元信息 + 各轨名 + 和弦标记。"""
    mid = mido.MidiFile(path)
    names = []
    for track in mid.tracks:
        got = ""
        for msg in track:
            if msg.type == "track_name":
                got = msg.name
                break
        names.append(got)

    solo_mode, duet_available, fret_strum, anti_repeat = None, None, False, True
    fret_strum_explicit = False
    for track in mid.tracks:
        for msg in track:
            if msg.type != "text":
                continue
            up = (msg.text or "").strip().upper()
            if up.startswith("SOLO_MODE:"):
                v = up.split(":", 1)[1].strip().lower()
                if v in ("fret", "chord"):
                    solo_mode = v
            elif up.startswith("DUET_AVAILABLE:"):
                duet_available = up.split(":", 1)[1].strip().lower() in ("true", "1", "yes")
            elif up.startswith("FRET_STRUM:"):
                # ★ 测试专用开关：让**品格模式**也发鼠标扫弦（默认不开，正常歌曲不受影响）。
                #   开启后，一帧里"同一品格上有 ≥3 个音"会被弹成一记扫弦，
                #   而不是六根数字键同时按下。方向照旧由力度决定。
                fret_strum_explicit = True
                fret_strum = up.split(":", 1)[1].strip().lower() in ("on", "true", "1", "yes")
            elif up.startswith("ANTI_REPEAT:"):
                # ★ 测试专用开关（`ANTI_REPEAT:off`）：关掉"防同弦连打"的换弦规则。
                #   复测 MIDI 里一个音只隔 0.4 秒，那套规则会按"音高走向"把音挪到别的弦上，
                #   测出来就不是我们要测的那根弦了。**正常歌曲不开这个开关。**
                anti_repeat = up.split(":", 1)[1].strip().lower() not in ("off", "false", "0", "no")

    return {
        "path": path,
        "track_names": names,
        "track_index": {
            TRACK_SOLO_MASTER: _track_index_by_name(names, TRACK_SOLO_MASTER,
                                                    TRACK_ALIASES[TRACK_SOLO_MASTER]),
            TRACK_DUET_LEAD: _track_index_by_name(names, TRACK_DUET_LEAD,
                                                  TRACK_ALIASES[TRACK_DUET_LEAD]),
            TRACK_DUET_RHYTHM: _track_index_by_name(names, TRACK_DUET_RHYTHM,
                                                    TRACK_ALIASES[TRACK_DUET_RHYTHM]),
        },
        "solo_mode": solo_mode,
        "duet_available": duet_available,
        "fret_strum": fret_strum,
        "fret_strum_explicit": fret_strum_explicit,
        "anti_repeat": anti_repeat,
        "mode_markers": _read_p2_mode_markers(mid),   # P2_MODE:fret|chord（没有就是空）
        "markers": read_chord_markers(path),   # 认得 `CHORD:G`，也认得裸写的 `G`
        "total_duration": round(mid.length, 3),
    }

def load_song(path, enable_arpeggio=None, arpeggio_pattern=None,
              enable_chord_rhythm=None, timing=None, force_recalc=False):
    """读整首歌：元信息 + 每轨音符（`{"t","dur","pitch","vel","track"}`）。"""
    if enable_arpeggio is None:
        try:
            import player_config as PC
            enable_arpeggio = PC.load().get("enable_arpeggio", True)
        except Exception:
            enable_arpeggio = True
    if arpeggio_pattern is None:
        try:
            import player_config as PC
            arpeggio_pattern = PC.load().get("arpeggio_pattern", "pingpong")
        except Exception:
            arpeggio_pattern = "pingpong"
    if enable_chord_rhythm is None:
        try:
            import player_config as PC
            enable_chord_rhythm = bool(PC.load().get("enable_chord_rhythm", True))
        except Exception:
            enable_chord_rhythm = True
    meta = read_meta(path)
    notes, info = read_flat_notes(path)
    by_track = {}
    for n in notes:
        by_track.setdefault(n.track_idx, []).append({
            "t": round(float(n.start), 6),
            "dur": round(float(n.length), 6),
            "pitch": int(n.pitch),
            "vel": int(n.velocity),
            "track": int(n.track_idx),
        })
    for lst in by_track.values():
        lst.sort(key=lambda x: (x["t"], x["pitch"]))
    meta["notes_by_track"] = by_track
    meta["note_count"] = len(notes)
    meta["bpm"] = info.get("bpm")
    meta["tempo_map"] = info.get("tempo_map") or [(0.0, float(info.get("bpm") or 120.0))]
    # 外部 MIDI 的统一适配：分轨八度归位 → 多轨合流 → 2P 虚拟轨
    midi_adapter.adapt_song(meta, notes, enable_arpeggio=enable_arpeggio,
                            arpeggio_pattern=arpeggio_pattern,
                            enable_chord_rhythm=enable_chord_rhythm,
                            timing=timing, force_recalc=force_recalc)
    return meta

def pick_track(song, role):
    """角色 → 读哪条轨（计划 1.1）。返回 (轨号, 说明文字)。"""
    idx = song["track_index"]
    # 2P 角色：本曲没有和弦轨时返回 None（由 compile_file 返回空事件），
    # 绝不能退到“取音符最多的轨”把主旋律当和弦弹。
    if role in (ROLE_DUET_RHYTHM, ROLE_DUET_RHYTHM_FRET) \
            and (song.get("adapter") or {}).get("no_chord_track") \
            and idx.get(TRACK_DUET_RHYTHM) is None:
        return None, "本曲没有和弦轨（no_chord_track=True），2P 无事件"
    # 2P 虚拟轨有两套内容：和弦模式用块，品格模式用单音线
    p2 = song.get("p2")
    if p2 and role in (ROLE_DUET_RHYTHM, ROLE_DUET_RHYTHM_FRET) \
            and idx.get(TRACK_DUET_RHYTHM) == p2.get("track"):
        want = "fret" if role == ROLE_DUET_RHYTHM_FRET else "chord"
        song["notes_by_track"][p2["track"]] = p2["notes"][want]
        song["markers"] = p2["markers"][want]
    name = {ROLE_SOLO: TRACK_SOLO_MASTER,
            ROLE_DUET_LEAD: TRACK_DUET_LEAD,
            ROLE_DUET_RHYTHM: TRACK_DUET_RHYTHM,
            ROLE_DUET_RHYTHM_FRET: TRACK_DUET_RHYTHM}[role]
    got = idx.get(name)
    if got is not None and song["notes_by_track"].get(got):
        return got, "按轨名「%s」" % song["track_names"][got]
    for guess in (1, 2, 3):
        if song["notes_by_track"].get(guess):
            return guess, "轨名对不上，按轨号 %d 兜底" % guess
    if song["notes_by_track"]:
        best = max(song["notes_by_track"].items(), key=lambda kv: len(kv[1]))
        return best[0], "轨名对不上，取音符最多的轨 %d" % best[0]
    return None, "这份 .mid 里没有音符"

def role_kind(song, role):
    """角色 → 锁定的模式（计划 1.1：全程锁定，绝不中途切换）。"""
    if role in (ROLE_DUET_LEAD, ROLE_DUET_RHYTHM_FRET):
        return "fret"
    if role == ROLE_DUET_RHYTHM:
        return "chord"
    return "chord" if song.get("solo_mode") == "chord" else "fret"

# ══════════════════════════════════════════════════════════════════════════
#  ② 分帧
# ══════════════════════════════════════════════════════════════════════════
def build_frames(notes, gap=FRAME_GAP):
    """按"起始时间差 ≤ gap"聚成帧；同帧同音高只留一个（一根弦发一个音）。"""
    if not notes:
        return []
    order = sorted(notes, key=lambda n: (n["t"], -(n.get("vel") or 0)))
    frames = []
    for n in order:
        if frames and (n["t"] - frames[-1]["t"]) <= gap + 1e-9:
            frames[-1]["notes"].append(n)
        else:
            frames.append({"t": n["t"], "notes": [n]})
    out = []
    for f in frames:
        seen = {}
        for n in f["notes"]:
            p = n["pitch"]
            if p not in seen or (n.get("dur") or 0) > (seen[p].get("dur") or 0):
                seen[p] = n
        out.append({"t": f["t"], "notes": [seen[p] for p in sorted(seen)]})
    return out

# ══════════════════════════════════════════════════════════════════════════
#  ③-A 品格模式：束搜索
# ══════════════════════════════════════════════════════════════════════════
def fret_string(pitch, fret):
    """这个音高在品格 `fret` 上落在哪根弦；按不出来返回 None。"""
    return PITCH_TO_STRING.get(int(pitch) - int(fret))

def fret_candidates_for_pitch(pitch, max_fret=MAX_FRET):
    """一个音高能按出来的所有 (品格, 弦)。0 品（空弦）也在里面。"""
    out = []
    for s, op in OPEN_PITCH.items():
        f = int(pitch) - op
        if 0 <= f <= max_fret:
            out.append((f, s))
    return out

def _assign_group(notes, fret):
    """在品格 `fret` 上把能按出的音分配到弦位。

    同一品格下"音高 → 弦"是唯一的（六根空弦音高互不相同），所以不用做匹配；
    撞弦只可能来自同音高（已在分帧时去重）。
    """
    ok, used, bad = [], set(), []
    for n in notes:
        s = fret_string(n["pitch"], fret)
        if s is None or s in used:
            bad.append(n)
        else:
            used.add(s)
            ok.append((n, s))
    return ok, bad

def _page_of(fret):
    return modes.locate("fret", int(fret))[0]

def _sector_of(fret):
    return modes.locate("fret", int(fret))[1]

def page_delta(p1, p2, pages=5):
    """5 页循环里从 p1 翻到 p2 的最短步数与按键。返回 (steps, 'T'|'R'|None)。"""
    steps = (int(p2) - int(p1)) % pages
    if steps == 0:
        return 0, None
    if steps <= pages // 2:
        return steps, "T"
    return pages - steps, "R"

def _switch(steps, cross):
    return steps, cross

def compile_fret(frames, timing=None, beam_width=BEAM_WIDTH, strum_chords=False,
                 onbeat_rule="highest", max_fret=MAX_FRET, log=None):
    """品格模式：束搜索选出"每帧用哪个品格、哪根弦、谁提前"。

    返回 `(events, stats)`；事件是**语义层**发声事件：
        {"t","dur","kind":"pluck","fret","page","sector","string","pitch",
         "vel","hold","advanced"}

    `onbeat_rule`：一帧跨品时"谁留在正拍"（见 `_frame_candidates`）：
        "coverage" = 覆盖音符数最多的品格留正拍（默认，问题清单 1A）；
        "highest"  = 最高音留正拍（1B）。
    """
    t = timing or Timing()
    log = log or (lambda m: None)
    if not frames:
        return [], {"frames": 0, "notes": 0, "voices": 0}

    cands = [_frame_candidates(f, t, onbeat_rule, max_fret) for f in frames]

    # 束： (代价, 上一个品格, 各弦预计松开时刻, 事件)
    beams = [(0.0, None, (0.0,) * 7, [])]
    for i, frame in enumerate(frames):
        t_on = frame["t"]
        prev_t = frames[i - 1]["t"] if i > 0 else None
        nxt = []
        for (cost, prev_fret, busy, evs) in beams:
            for cand in cands[i]:
                add = _cand_cost(prev_fret, cand, busy, t_on, prev_t, t)
                new_busy = list(busy)
                batch = _cand_events(cand, t_on, t, strum_chords=strum_chords)
                for e in batch:
                    if "string" not in e:      # 扫弦事件没有单根弦，不占用 busy 表
                        continue
                    rel = e["t"] + (e.get("hold") or 0.0)
                    if rel > new_busy[e["string"]]:
                        new_busy[e["string"]] = rel
                nxt.append((cost + add, cand["groups"][-1]["fret"],
                            tuple(new_busy), evs + batch))
        if not nxt:
            nxt = [(cost, prev_fret, busy, evs) for (cost, prev_fret, busy, evs) in beams]
        nxt.sort(key=lambda x: x[0])
        beams = nxt[:max(1, int(beam_width))]

    best = min(beams, key=lambda x: x[0])
    events = sorted(best[3], key=lambda e: (e["t"], e.get("string", 0)))
    return events, _fret_stats(frames, events, t)

def _cand_events(cand, t_on, t, strum_chords=False):
    """一个候选 → 发声事件（组内同弦号同时按；提前的组按 t_off 挪到前面）。

    `strum_chords=True`（只在 Track 0 写了 `FRET_STRUM:on` 的测试文件里生效）：
    正拍那一组如果**同一品格上有 ≥3 个音**，就弹成一记**鼠标扫弦**（而不是六根数字键
    同时按下），方向照旧由平均力度决定（≥85 上扫/左键厚，<85 下扫/右键脆）。
    """
    out = []
    for g in cand["groups"]:
        base = t_on + g["t_off"]
        page, sec = _page_of(g["fret"]), _sector_of(g["fret"])
        if strum_chords and len(g["pairs"]) >= 3 and not g.get("advanced"):
            vel = sum(n.get("vel") or 80 for n, _s in g["pairs"]) / float(len(g["pairs"]))
            out.append({
                "t": round(base, 6),
                "dur": round(max(float(n["dur"]) for n, _s in g["pairs"]), 6),
                "kind": "strum",
                "direction": "U" if vel >= 85 else "D",
                "fret": int(g["fret"]),
                "page": int(page),
                "sector": int(sec),
                "vel": int(round(vel)),
                "hold": None,
                "advanced": False,
                "order": 0,
            })
            continue
        for k, (n, s) in enumerate(g["pairs"]):
            out.append({
                "t": round(base, 6),
                "dur": round(float(n["dur"]), 6),
                "kind": "pluck",
                "fret": int(g["fret"]),
                "page": int(page),
                "sector": int(sec),
                "string": int(s),
                "pitch": int(n["pitch"]),
                "vel": int(n.get("vel") or 80),
                "hold": _hold_for(n["dur"], t),
                "advanced": bool(g["advanced"]),
                "order": k,
            })
    out.sort(key=lambda e: (e["t"], e.get("string", 0)))
    return out

def _hold_for(dur, t):
    """一个音按多久（计划 1.5.1 + 用户实测口径）。

    * 短音：按 **25 毫秒**就够（用户实测"按下 25ms 松手游戏才捕捉得到"；
      旧代码用的 0.12 秒是"怕 30fps 吞帧"的推测，实测没必要，而且白占弦）；
    * 长音（时值 ≥ #19 且 #5 > 0）：按住 min(时值, #5)，最少也是 25 毫秒。
    """
    d = float(dur or 0.0)
    if t.p5 <= 0 or d < t.p19:
        return round(max(0.005, t.p4), 4)
    return round(max(0.005, min(d, t.p5)), 4)

def _frame_candidates(frame, t, onbeat_rule, max_fret):
    """一帧的全部候选指法（规则来源：指法规划规范 `1B 2A`）。

    候选 = 一串"分组"：**最后一个分组留在正拍**，前面的分组按换格耗时
    **往前挪**（`t_off` 为负）。分组内部是同品格、不同弦（1~6 个音同时按）。

    ★ **1B 口径**：
      * 正拍格 = **最高音（主旋律）所在的品格**；该品格上还能按出的音一起齐按；
      * 其余异品的低音/伴奏音**只允许提前一组**（单帧最多换 1 次格）；
      * 提前那一组选"覆盖剩余音数最多"的品格（平局选含最低音的），
        仍落在第三个品格上的中间内音**直接丢弃**（不丢就得多换一次格，主旋律会抢拍）。

    ★ **2A 口径**：弦号分区（低音走 1~3 弦、高音走 4~6 弦）只是**软偏好**，
      在 `_cand_cost` 里作为很小的加分项；硬约束只有"同一帧/锁音要不同弦"。
    """
    notes = frame["notes"]
    if not notes:
        return []
    playable = [n for n in notes if fret_candidates_for_pitch(n["pitch"], max_fret)]
    dropped = [n for n in notes if n not in playable]
    if not playable:
        return [{"groups": [], "bad": [n["pitch"] for n in dropped],
                 "dropped": len(dropped)}]

    # 每个品格能覆盖多少音
    cover = []
    for f in range(0, max_fret + 1):
        ok, bad = _assign_group(playable, f)
        if ok:
            cover.append((f, ok, bad))
    if not cover:
        return _fallback_candidates(frame, max_fret)
    top_pitch = max(n["pitch"] for n in playable)

    def rank(item):
        f, ok, bad = item
        has_top = 1 if any(n["pitch"] == top_pitch for n, _s in ok) else 0
        if onbeat_rule == "highest":
            return (-has_top, -len(ok), f)
        return (-len(ok), -has_top, f)

    cover.sort(key=rank)

    # 1B：正拍格**只能**是"含最高音"的那些品格（一个音高最多 6 个弦位 → 最多 6 个候选）。
    # 不能只取前几个：否则会把"正好留在当前页"的更优品格漏掉，白白多翻一次页。
    if onbeat_rule == "highest":
        cands = [c for c in cover if any(n["pitch"] == top_pitch for n, _s in c[1])]
    else:
        cands = cover[:4]

    out = []
    for (f_on, ok_on, bad_on) in cands:
        groups = []
        drop = list(dropped)             # 音域外、压根按不出来的
        if bad_on:
            # 只选 **一个** 提前格：覆盖剩余音最多 → 含最低音 → 品格号小
            best = None
            for f in range(0, max_fret + 1):
                ok_adv, still = _assign_group(bad_on, f)
                if not ok_adv:
                    continue
                lowest = min(n["pitch"] for n, _s in ok_adv)
                score = (-len(ok_adv), lowest, f)
                if best is None or score < best[0]:
                    best = (score, f, ok_adv, still)
            if best:
                _sc, f_adv, ok_adv, still = best
                groups.append({"fret": f_adv, "pairs": ok_adv, "advanced": True})
                drop.extend(still)       # 1B：掉在第三个品格上的音直接丢弃
            else:
                drop.extend(bad_on)
        groups.append({"fret": f_on, "pairs": ok_on, "advanced": False})

        # 从正拍往回算每组的时刻（相邻两个品格之间的换格耗时）
        offs = [0.0] * len(groups)
        for k in range(len(groups) - 2, -1, -1):
            f_from = groups[k]["fret"]
            f_to = groups[k + 1]["fret"]
            pa, pb = _page_of(f_from), _page_of(f_to)
            steps, _key = page_delta(pa, pb)
            offs[k] = offs[k + 1] - t.advance_need(steps, pa != pb,
                                                   need_click=(pb != PAGE_ZERO))
        for g, off in zip(groups, offs):
            g["t_off"] = round(off, 6)
        out.append({"groups": groups, "bad": [n["pitch"] for n in drop],
                    "dropped": len(drop)})
    return out

def _best_fret_for_pitch(pitch, max_fret=MAX_FRET):
    """落单的音挑哪个品格：优先"能按出来的最小品格"（低把位，少翻页）。"""
    cs = fret_candidates_for_pitch(pitch, max_fret)
    return min(f for f, _s in cs) if cs else 0

def _fallback_candidates(frame, max_fret):
    """兜底候选：每个音各挑一品格，绝不丢音。"""
    groups = []
    notes = sorted([n for n in frame["notes"]
                    if fret_candidates_for_pitch(n["pitch"], max_fret)],
                   key=lambda n: n["pitch"])
    for i, n in enumerate(notes):
        f = _best_fret_for_pitch(n["pitch"], max_fret)
        groups.append({"fret": f, "pairs": [(n, fret_string(n["pitch"], f))],
                       "advanced": i < len(notes) - 1, "t_off": 0.0})
    if not groups:
        return []
    return [{"groups": groups, "bad": [], "dropped": 0}]

def _cand_cost(prev_fret, cand, busy, t_on, prev_t, t):
    """候选的转移代价（计划 1.2 第 2 条）。

    代价单位 = **秒**（这样"换一格 0.055 秒"和"翻两页 0.17 秒"能直接比大小）：
      * 同格 0；同页换格 = 选格耗时；跨页 = 翻页耗时 + 选格耗时；
      * 帧间隔不够换格 → +1000（迫使算法在本页 30 个半音里找同音);
      * 候选弦上还有 > 0.15s 的延音 → +50（优先用空闲弦）。
    """
    groups = cand.get("groups") or []
    if not groups:
        return 1e6
    cost = 0.0
    # 帧与帧之间：从上一个品格换到"本帧第一个分组"的品格
    if prev_fret is not None and prev_t is not None:
        f0 = groups[0]["fret"]
        pa, pb = _page_of(prev_fret), _page_of(f0)
        steps, _k = page_delta(pa, pb)
        need = (t.advance_need(steps, pa != pb, need_click=(pb != PAGE_ZERO))
                if prev_fret != f0 else t.p1)
        cost += need
        if (t_on - prev_t) < need - 1e-9:
            cost += 1000.0
    # 帧内部：正拍之前的提前量已经写进 t_off，这里只累加实际耗时
    for g in groups:
        cost += abs(float(g.get("t_off") or 0.0))
        # 但只作为极小的加分项，绝不因此丢音或否定可行解。
        if g.get("advanced") and g.get("pairs"):
            low = min(g["pairs"], key=lambda pr: pr[0]["pitch"])
            cost += 0.0005 * int(low[1])
    # 同弦延音惩罚
    for g in groups:
        for (_n, s) in g["pairs"]:
            if busy[s] - t_on > 0.15:
                cost += 50.0
    return cost

def _fret_stats(frames, events, t):
    """品格编译统计。"""
    notes = sum(len(f["notes"]) for f in frames)
    advanced = sum(1 for e in events if e.get("advanced"))
    frets = [e["fret"] for e in events] or [0]
    switched, prev = 0, None
    for e in sorted(events, key=lambda x: x["t"]):
        if prev is not None and e["fret"] != prev:
            switched += 1
        prev = e["fret"]
    return {
        "frames": len(frames),
        "notes": notes,
        "voices": len(events),
        "advanced": advanced,
        "dropped": max(0, notes - len(events)),
        "switches": switched,
        "fret_min": min(frets),
        "fret_max": max(frets),
        "pages": sorted({e["page"] + 1 for e in events}),
        "start_page": (events[0]["page"] + 1) if events else 1,
        "start_sector": (events[0]["sector"] + 1) if events else 1,
        "sustains": sum(1 for e in events if (e.get("hold") or 0) > 0.005 + 1e-9),
    }

# ══════════════════════════════════════════════════════════════════════════
#  ③-B 和弦模式编译
# ══════════════════════════════════════════════════════════════════════════
# ★ 注意：`modes` 的 `Page` 实现了 `__getitem__`（取模返回格子），
#   所以 `for n in page` **永远不会结束**（要么用 `page.cells`，要么用 `page.labels()`）。
CHORD_NAMES = [c.value for pg in modes.get("chord").pages for c in pg.cells]

def chord_at(t_on, markers, fallback=None):
    """某时刻属于哪个和弦：取"最近一条不晚于它的标记"。"""
    got = fallback
    for (mt, name) in markers or []:
        if mt <= t_on + 1e-6:
            got = name
        else:
            break
    return got

def chord_sounding_strings(name):
    """和弦在轮盘上的发声弦，按音高由低到高：[(弦号, 音高)]（-1 的弦已剔除）。

    ★ 这是**扫弦**的音高（用鼠标扫出来的那一下）。**单拨不一定是这些音**——
      单拨请用 `chord_pluck_strings()`（2026-10-17 实测：两套音高不一样）。
    """
    sh = modes.shape("chord", name)
    if sh is None:
        return []
    out = []
    for s in range(1, 7):
        p = sh.pitch(s)
        if p is not None:
            out.append((s, p))
    out.sort(key=lambda x: x[1])
    return out

def chord_pluck_strings(name):
    """和弦在**单拨**（按数字键）时能用的实际音高：[(弦号, 音高)]，按音高由低到高。

    有实测就用实测（`guitar_plan.PLUCK_PITCHES`），没测过的和弦沿用指型；
    ★ 只返回**安全弦**（去掉小二度地雷弦，见 `guitar_plan.pluck_safe_strings`）。
    """
    import guitar_plan as _GP
    sh = modes.shape("chord", name)
    if sh is None:
        return []
    pcs = _GP.chord_pitch_class_set(name)
    out = []
    for s in _GP.pluck_safe_strings(name):
        p = _GP.pluck_pitch(name, s)
        if p is not None and (int(p) % 12) in pcs:   # 只用“实测音级属于和弦构成音”的干净弦
            out.append((s, p))
    if not out:                                      # 兜底：绝不把和弦弄哑
        for s in _GP.pluck_safe_strings(name):
            p = _GP.pluck_pitch(name, s)
            if p is not None:
                out.append((s, p))
    out.sort(key=lambda x: x[1])
    return out

def _prev_string_pitch(name, string):
    """上一根弦的**实测**音高（拿不到就返回 None）。"""
    if not name:
        return None
    try:
        import guitar_plan as _GP
        return _GP.pluck_pitch(name, int(string))
    except Exception:
        return None

def _pick_string_anti_repeat(free, pitch, t, last, name=None, n_notes=1):
    """在可用弦里挑一根（`free` = [(弦号, 该弦音高), ...]）。

    ★ 防同弦连打 / 六弦轮指（规范定义）：
      * 距上一音 **< 0.50 秒**且音高变了 → **不许再用上一根弦**；
      * 音高变高 → 优先挑**实测音高高于上一根弦**的候选，变低 → 优先低于的
        （★ 2026-10-18 改成按实测音高判断，不再看弦号大小：实测表里弦号大
         并不等于音高一定高）；
      * 顶到边界没有同方向的弦，就退到相邻那根（`cands` 不缩小，靠"最近音高"挑）。
    最后在剩下的弦里挑**和弦音高最近**的那根。
    """
    if not free:
        return None
    cands = list(free)
    if last is not None:
        p_t, p_pitch, p_s = last
        if (t - float(p_t)) < 0.50 and int(pitch) != int(p_pitch):
            others = [sp for sp in cands if int(sp[0]) != int(p_s)]
            if others:                       # 只要还有别的弦，就绝不重复用同一根
                cands = others
            up = int(pitch) > int(p_pitch)
            prev_p = _prev_string_pitch(name, p_s)
            if prev_p is not None:
                # 按实测音高判方向
                if up:
                    same = [sp for sp in cands if int(sp[1]) > int(prev_p)]
                else:
                    same = [sp for sp in cands if int(sp[1]) < int(prev_p)]
            else:
                # 拿不到上一根弦的实测音高：退回弦号比较（老行为）
                if up:
                    same = [sp for sp in cands if int(sp[0]) > int(p_s)]
                else:
                    same = [sp for sp in cands if int(sp[0]) < int(p_s)]
            if same:
                cands = same
    return min(cands, key=lambda sp: (abs(sp[1] - int(pitch)), sp[0]))

def compile_chord(frames, markers, timing=None, log=None, min_strum_notes=3,
                  anti_repeat=True):
    """和弦模式：齐奏块走鼠标扫弦，单音/分解走数字键（计划 1.3）。

    `anti_repeat=False`：关掉"防同弦连打"的换弦规则（只有 `ANTI_REPEAT:off` 的
    测试 MIDI 会这么用；正常歌曲保持 True）。

    返回 `(events, stats)`；事件：
        {"t","dur","kind":"strum"|"pluck","chord","page","sector",
         "direction"(strum) / "string"(pluck),"vel","hold","order"}
    """
    t = timing or Timing()
    log = log or (lambda m: None)
    if not frames:
        return [], {"frames": 0, "notes": 0, "strums": 0, "plucks": 0}

    events, cur, guessed = [], None, 0
    last_pluck = None      # 上一个音：(时刻, 音高, 用过的弦号) —— 防同弦连打用
    for frame in frames:
        name = chord_at(frame["t"], markers, fallback=cur)
        if name is None:
            name = _guess_chord(frame["notes"])
            guessed += 1
        if name is None:
            continue
        loc = modes.locate("chord", name)
        if not loc:
            continue
        cur = name
        page, sec = loc
        notes = frame["notes"]
        base = {"t": round(frame["t"], 6),
                "dur": round(max(n["dur"] for n in notes), 6),
                "chord": name, "page": int(page), "sector": int(sec)}
        if len(notes) >= min_strum_notes:
            vel = sum(n.get("vel") or 80 for n in notes) / float(len(notes))
            ev = dict(base)
            # ★ 用户真机听感要求——和弦伴奏一律用鼠标**右键下扫**。
            #   （原逻辑按力度 >=85 走左键上扫，已废除。）
            ev.update({"kind": "strum", "direction": "D",
                       "vel": int(round(vel)), "hold": None})
            events.append(ev)
        else:
            strings = chord_pluck_strings(name)   # ★ 单拨用实测音高，不是扫弦的音高
            if not strings:
                continue
            # 按音高排序，逐个配弦；带"防同弦连打"规则（见下）
            ordered = sorted(notes, key=lambda n: n["pitch"])
            free = list(strings)
            pairs = []
            for n in ordered:
                if not free:
                    break
                pick = _pick_string_anti_repeat(free, n["pitch"], frame["t"],
                                                last_pluck if anti_repeat else None,
                                                name, len(ordered))
                free.remove(pick)
                pairs.append((n, pick))
            # 记住本帧最后一个音（供下一帧的"防同弦连打"用）
            if pairs:
                last_pluck = (frame["t"], int(pairs[-1][0]["pitch"]), int(pairs[-1][1][0]))
            for k, (n, (s, _p)) in enumerate(pairs):
                ev = dict(base)
                ev.update({"t": round(frame["t"], 6), "kind": "pluck",
                           "string": int(s), "pitch": int(n["pitch"]),
                           "vel": int(n.get("vel") or 80),
                           "dur": round(float(n["dur"]), 6), "hold": None,
                           "order": k, "total": len(pairs)})
                events.append(ev)
    events.sort(key=lambda e: (e["t"], e.get("order", 0)))
    stats = {
        "frames": len(frames),
        "notes": sum(len(f["notes"]) for f in frames),
        "voices": len(events),
        "strums": sum(1 for e in events if e["kind"] == "strum"),
        "plucks": sum(1 for e in events if e["kind"] == "pluck"),
        "chords_used": len({e["chord"] for e in events}),
        "pages": sorted({e["page"] + 1 for e in events}),
        "start_page": (events[0]["page"] + 1) if events else 1,
        "start_sector": (events[0]["sector"] + 1) if events else 1,
        "guessed_chords": guessed,
        "dropped": 0,
    }
    log("和弦编译：%d 帧 → 扫弦 %d 次 / 拨弦 %d 次（%d 个和弦）"
        % (stats["frames"], stats["strums"], stats["plucks"], stats["chords_used"]))
    return events, stats

def _guess_chord(notes):
    """没有和弦标记时按音高类猜一个轮盘上有的和弦（只有老格式兜底才会用到）。"""
    pcs = {int(n["pitch"]) % 12 for n in notes}
    if not pcs:
        return None
    best, best_d = None, None
    for name in CHORD_NAMES:
        own = {p % 12 for _s, p in chord_sounding_strings(name)}
        if not own:
            continue
        d = len(pcs ^ own)
        if best_d is None or d < best_d:
            best, best_d = name, d
    return best

# ══════════════════════════════════════════════════════════════════════════
#  ④ 反向预调度：语义事件 → 硬件动作时间轴
# ══════════════════════════════════════════════════════════════════════════
def _hw(t, op, **kw):
    ev = {"t": round(float(t), 6), "op": op}
    ev.update(kw)
    return ev

def mode_switch_events(markers, start_mode):
    """`P2_MODE:` 标记 → 状态机里的"模式切换"事件（只在模式真的变了时才发）。

    *(秒, 'fret'|'chord') 列表 → [{"t","kind":"mode","mode",...}]，按时间排序。
    没有 `P2_MODE:` 标记时返回空列表，行为与旧版完全一致。
    """
    out = []
    cur = str(start_mode or "").lower()
    for (mt, mode) in markers or []:
        m = str(mode or "").lower()
        if m not in ("fret", "chord") or m == cur:
            continue
        cur = m
        out.append({"t": round(float(mt), 6), "kind": "mode", "mode": m, "order": -1})
    return out

def preschedule(events, kind, timing=None, start_page=1, log=None, detail=False):
    """把发声事件编译成硬件动作时间轴（计划 1.4）。

    输出每条动作都是**绝对时刻 + 单一原语**（执行器只管照着做）：
        move_sector / move_park / mouse_down / mouse_up / key_down / key_up

    同时做三件保护：
      * 碰撞钳制：链子撞到上一个音就整条往后平移，记 `late`；
      * 同弦重拨抬键保护（1.5.1）：同弦再拨前 max(#1, 35ms) 先松开；
      * 单弦/扫弦互斥锁（1.5.2）：按住鼠标扫弦时，下一个数字键/轮盘点击前先松。

    ★ `kind == "mode"` 的事件 = **按 Z 切模式**（2026-10-18 用户实测：Z 松开即生效、
      没有延迟，切完轮盘回到第 1 页）。所以松开后**不加** `after_page_key_release`，
      并且切换后把页码强制置为 1。
    """
    t = timing or Timing()
    log = log or (lambda m: None)
    click_gap = 0.0
    if not events:
        return [], {"events": 0, "turns": 0, "clicks": 0, "plucks": 0,
                    "strums": 0, "switches": 0, "late": 0, "max_late": 0.0,
                    "duration": 0.0}

    hw = []
    cur_page = int(start_page) - 1
    cur_sec = None
    cur_kind = kind           # 状态机"当前模式"（P2_MODE 会在中途改它）
    prev_action = None        # 上一个"动作"的时刻（#1 的参照）
    prev_sound_t = None       # 上一个发声动作的时刻
    group_key = None          # 当前这一帧的原始时刻（同帧多弦共享一次换格）
    group_shift = 0.0         # 这一帧被钳制后整体平移了多少
    last_key_down = {}        # 每根弦上一次"按下"的时刻（同弦重拨要隔 REPLUCK_GAP）
    late = []
    late_detail = []          # 仅 detail=True：逐音记录"谁被推后了多少"
    turns = clicks = plucks = strums = switches = 0
    last_sound = 0.0

    for ev in sorted(events, key=lambda e: (e["t"], e.get("order", 0))):
        # ── 模式切换（Z 键）：整条状态机里唯一会改"当前模式"的地方 ──────────
        if ev.get("kind") == "mode":
            hold = t.page_key_hold()             # 与翻页键同一个 #12
            # 松开即生效：只留 #3（拨弦前挪开指针）+ #1，不叠 35ms 缓冲
            t_up = float(ev["t"]) - t.p1
            t_down = t_up - hold
            if prev_action is not None and t_down < prev_action + t.p1 - 1e-9:
                shift = (prev_action + t.p1) - t_down
                t_down += shift
                t_up += shift
            hw.append(_hw(t_down, "key_down", key="Z"))
            hw.append(_hw(t_up, "key_up", key="Z"))
            prev_action = t_up
            cur_kind = str(ev.get("mode") or cur_kind)
            cur_page, cur_sec = 0, None          # 切完轮盘回到第 1 页
            group_key, group_shift = None, 0.0
            switches += 1
            continue
        page, sec = int(ev["page"]), int(ev["sector"])
        is_strum = ev["kind"] == "strum"
        park = t.p8 if is_strum else 0.0
        t_on = float(ev["t"])

        # 同一帧的多根弦：共享一次挪指针、一次换格、一次钳制（不重复做，也不互相钳制）
        same_group = (group_key is not None and abs(t_on - group_key) < 1e-6)
        if same_group:
            t_on += group_shift
        else:
            group_key, group_shift = t_on, 0.0
        t_ready = t_on - park

        # 第 5 页（0 品）六格都是 Zero：不用点扇区（计划 1.4）
        need_sector = not (cur_kind == "fret" and page == PAGE_ZERO)
        need_switch = (page != cur_page) or (need_sector and sec != cur_sec)

        chain = []   # (t, op, kw)
        pg_events, wheel_events = [], []
        if same_group:
            pass                     # 同一帧的后续弦：指针已经挪好，什么都不用做
        elif need_switch:
            cross = page != cur_page
            steps, key = page_delta(cur_page, page) if cross else (0, None)
            hold = t.page_key_hold()

            # ① 翻页（**松开判定**）：松开后 35ms 根音才变，
            #    所以"松开"要排在"最晚还能赶上的时刻"——拨弦前 #3 + 35ms。
            if steps > 0:
                t_up_last = (t_ready - t.p14 - t.p16)
                t_down_last = t_up_last - hold
                if steps == 2:
                    t_up_first = t_down_last - t.p13
                    t_down_first = t_up_first - hold
                    pg_events.append((t_down_first, "key_down", {"key": key}))
                    pg_events.append((t_up_first, "key_up", {"key": key}))
                pg_events.append((t_down_last, "key_down", {"key": key}))
                pg_events.append((t_up_last, "key_up", {"key": key}))
                turns += steps

            # ② 点扇区（**按下判定**）：按下那一刻根音就变；
            #    ★ 只在"格子号真的变了"时才点。**同一格连点第二下会被游戏当成扫弦**
            #      ——左键在已经选中的格子上再点一下 = 扫弦整个和弦，就是那个爆音。
            #      只翻页、格子号没变时，翻页本身已经把品格换好了，不需要再点（用户实测）。
            if need_sector and sec != cur_sec:
                hold_click = t.click_hold(cur_kind)
                if steps > 0:
                    t_click = t_up_last - t.p18 - hold_click
                else:
                    t_click = t_ready - t.p1 - t.p17 - hold_click - click_gap
                wheel_events.append((t_click - t.p1, "move_sector",
                                     {"page": page, "sector": sec}))
                wheel_events.append((t_click, "mouse_down", {"btn": "left"}))
                wheel_events.append((t_click + t.click_hold(cur_kind), "mouse_up", {"btn": "left"}))
                clicks += 1
            cur_page, cur_sec = page, sec

            # ── 有余量时把翻页提前（翻页不打断正在响的音，早翻只有好处）──
            if pg_events:
                earliest = min(x[0] for x in pg_events)
                floor = (prev_action + t.p1) if prev_action is not None else None
                room = (earliest - floor) if floor is not None else 0.0
                if room > 0:
                    bp = min(room, PAGE_SETTLE_MAX)
                    pg_events = [(tt - bp, o, kw) for (tt, o, kw) in pg_events]
            chain = pg_events + wheel_events
            # 只有鼠标扫弦才需要把指针挪到空白处；数字键拨弦完全不用挪指针。
            if is_strum:
                chain.append((t_ready, "move_park", {}))
        else:
            if is_strum:
                chain.append((t_ready, "move_park", {}))

        # ── 碰撞钳制（计划 1.4 第 4 条）──────────────────────────────
        if chain:
            earliest = min(c[0] for c in chain)
            if prev_action is not None and earliest < prev_action + t.p1 - 1e-9:
                shift = (prev_action + t.p1) - earliest
                chain = [(ct + shift, op, kw) for (ct, op, kw) in chain]
                t_on += shift
                t_ready += shift
                group_shift += shift
                late.append(shift)
                if detail:
                    late_detail.append({"t": round(float(ev["t"]), 6),
                                        "pitch": ev.get("pitch"),
                                        "chord": ev.get("chord"),
                                        "shift": round(shift, 6),
                                        "why": "switch"})
            # ★ 这里**不能**再"把挨太近的动作逐个推开"：
            #   硬按 #1 推开反而会把翻页和点格错开、把拨弦推晚。
            chain.sort(key=lambda x: x[0])
            for (ct, op, kw) in chain:
                hw.append(_hw(ct, op, **kw))
                prev_action = max(prev_action or ct, ct)

        # ── 发声动作本体 ─────────────────────────────────────────────
        if is_strum:
            btn = "right" if ev.get("direction") == "D" else "left"
            hold = t.p10 if t.p10 > 1e-9 else t.strum_hold_short()
            hw.append(_hw(t_on, "mouse_down", btn=btn, sound=True))
            hw.append(_hw(t_on + hold, "mouse_up", btn=btn))
            strums += 1
        else:
            key = str(int(ev["string"]))
            # 按住多久：品格模式在编译期就定好了；和弦模式（`hold` 为 None）在这里按
            # 「短音点一下 / 长音按住」的规矩现算（计划 1.5.1）。
            hold = ev.get("hold")
            if hold is None:
                hold = _hold_for(ev.get("dur") or 0.0, t)
            #   宁可把这个音往后挪十几毫秒（听不出来），也不要为了准点去切断上一个音、
            #   更不能让上一根弦"按下 0 毫秒又松开"。
            last = last_key_down.get(key)
            if last is not None and (t_on - last) < t.repluck_gap() - 1e-9:
                shift = (last + t.repluck_gap()) - t_on
                t_on += shift
                late.append(shift)
                if detail:
                    late_detail.append({"t": round(float(ev["t"]), 6),
                                        "pitch": ev.get("pitch"),
                                        "chord": ev.get("chord"),
                                        "shift": round(shift, 6),
                                        "why": "repluck"})
            last_key_down[key] = t_on
            hw.append(_hw(t_on, "key_down", key=key, sound=True))
            hw.append(_hw(t_on + float(hold), "key_up", key=key))
            plucks += 1
        prev_action = max(prev_action or t_on, t_on)
        prev_sound_t = t_on
        last_sound = max(last_sound, t_on)

    hw = _retrigger_guard(hw, t)
    hw = _interlock(hw, t)
    hw.sort(key=lambda e: (e["t"], _op_order(e["op"])))

    stats = {"events": len(events), "turns": turns, "clicks": clicks,
             "plucks": plucks, "strums": strums, "switches": switches,
             "late": len(late), "max_late": round(max(late), 4) if late else 0.0,
             "duration": round(last_sound, 3), "hw_events": len(hw)}
    if detail:
        stats["late_detail"] = late_detail
    if late:
        log("⚠ 有 %d 个音为了让换格链完整，被迫晚了（最多 %.0f 毫秒）"
            % (len(late), max(late) * 1000))
    return hw, stats

def _op_order(op):
    """同一时刻的动作顺序：先松开，再移动，再按下（避免"按住还没松就换键"）。"""
    return {"key_up": 0, "mouse_up": 0, "move_park": 1, "move_sector": 1,
            "key_down": 2, "mouse_down": 2}.get(op, 3)

def _retrigger_guard(hw, t):
    """同弦重拨抬键保护（计划 1.5.1）。

    第 s 弦要再按下时，若上一次的松开时刻晚于 `T_on - max(#1, #20)`，
    就把上一次的 `key_up` 提前到那里（留出 1 帧以上的抬键间隙）。
    """
    guard = max(t.p1, t.repluck_gap())
    downs = {}
    ups_by_key = {}
    for e in hw:
        if e["op"] == "key_down" and e["key"].isdigit():
            downs.setdefault(e["key"], []).append(e["t"])
        elif e["op"] == "key_up" and e["key"].isdigit():
            ups_by_key.setdefault(e["key"], []).append(e)
    for k, times in downs.items():
        times.sort()
        ups = sorted(ups_by_key.get(k, []), key=lambda x: x["t"])
        for i in range(1, len(times)):
            limit = times[i] - guard
            #   老写法只在上次松开 ≤ 这次按下 +1ms 的窗口里找，遇到**长延音**
            #   （上次松开比这次按下还晚，比如 0.43s 长音 0.43s 后又按同一根弦）
            #   就会整对漏掉 —— 结果同一根弦没松开就又按下去，游戏会吞音。
            if i - 1 < len(ups):
                u = ups[i - 1]
            else:
                # 数量对不上（理论上不会）：退回按时间窗找
                # ★ 容差 1 毫秒是必须的：两条事件是不同算式算出来的
                # （一个带开头留白、一个不带），实测会差出十几微秒。
                cand = [x for x in ups
                        if times[i - 1] - 1e-6 <= x["t"] <= times[i] + 1e-3]
                if not cand:
                    continue
                u = max(cand, key=lambda x: x["t"])
            if u["t"] > limit:
                floor = times[i - 1] + 0.005       # 只留 5ms 安全底线
                u["t"] = round(max(floor, limit), 6)
    return hw

def _interlock(hw, t):
    """单弦 / 扫弦互斥锁（计划 1.5.2）。

    按住鼠标扫弦期间，下一个数字键拨弦或轮盘点击之前至少提前 #1 松开鼠标。
    """
    holds = []      # (down_t, up_t, btn)
    down = None
    for e in sorted(hw, key=lambda x: x["t"]):
        if e["op"] == "mouse_down":
            down = e
        elif e["op"] == "mouse_up" and down:
            holds.append([down["t"], e["t"], down["btn"], e])
            down = None
    if not holds:
        return hw
    # 会跟扫弦抢"同一条输入路径"的动作：数字键按下、轮盘左键点击
    conflicts = [e for e in hw
                 if (e["op"] == "key_down" and e["key"].isdigit())
                 or (e["op"] == "mouse_down" and e["btn"] == "left")]
    for (d_t, u_t, btn, up_ev) in holds:
        if u_t - d_t < t.p1:          # 本来就是点一下，不是"按住延音"
            continue
        near = [c for c in conflicts if d_t < c["t"] < u_t + t.p1]
        if near:
            new_up = min(c["t"] for c in near) - t.p1
            if new_up > d_t:
                up_ev["t"] = round(new_up, 6)
    return hw

# ══════════════════════════════════════════════════════════════════════════
#  顶层：一首歌 → 一份时间轴
# ══════════════════════════════════════════════════════════════════════════
def _empty_no_chord(path, role, t):
    """本曲没有和弦轨时的 2P 时间轴：0 事件 + no_chord_track 标记。"""
    return {
        "version": 1, "role": role, "role_cn": ROLE_CN.get(role, role),
        "kind": "chord", "source": os.path.abspath(path),
        "start": {"page": 1, "sector": 1, "mode_cn": "和弦", "wheel": ""},
        "stats": {"frames": 0, "notes": 0, "voices": 0, "events": 0, "turns": 0,
                  "clicks": 0, "plucks": 0, "strums": 0, "switches": 0,
                  "late": 0, "max_late": 0.0, "duration": 0.0, "hw_events": 0,
                  "no_chord_track": True},
        "compile_stats": {},
        "hw_stats": {"events": 0, "turns": 0, "late": 0, "max_late": 0.0,
                     "hw_events": 0, "duration": 0.0},
        "adapter": {"no_chord_track": True,
                    "chords": {"tracks": 0, "no_chord_track": True}},
        "timing": t.as_dict() if hasattr(t, "as_dict") else {},
        "events": [],
    }

def _analyze_notes(frames, sound, hstats, kind):
    """把真实编译结果回填到输入音符。

    * 品格模式：逐音返回真实分配的 `string/fret/page`、是否被丢弃、晚出多少秒；
    * 和弦模式：同帧 ≥3 音是扫弦（无单弦信息），<3 音是单拨（有弦号）。
    ★ 只做"结果归位"，不重算任何指法/延迟。
    """
    details = hstats.get("late_detail") or []
    out = []

    def _late_for(pitch, t_ev, chord=None):
        for d in details:
            if chord is not None:
                if d.get("chord") == chord and abs(float(d.get("t") or 0) - t_ev) <= 1e-5:
                    return float(d.get("shift") or 0.0)
                continue
            if int(d.get("pitch") or -1) != int(pitch):
                continue
            if abs(float(d.get("t") or 0) - t_ev) <= 1e-5:
                return float(d.get("shift") or 0.0)
        return 0.0

    if kind == "fret":
        pool = [dict(e) for e in sound]
        used = [False] * len(pool)
        for f in frames:
            for n in f["notes"]:
                hit = None
                for i, e in enumerate(pool):
                    if used[i] or int(e.get("pitch", -1)) != int(n["pitch"]):
                        continue
                    if e["t"] > f["t"] + 1e-6 or e["t"] < f["t"] - 0.20:
                        continue
                    hit = i
                    break
                if hit is None:
                    out.append({"t": round(float(n["t"]), 6),
                                "pitch": int(n["pitch"]), "dropped": True,
                                "late": 0.0})
                    continue
                used[hit] = True
                e = pool[hit]
                out.append({
                    "t": round(float(n["t"]), 6), "pitch": int(n["pitch"]),
                    "dropped": False, "late": round(_late_for(n["pitch"], e["t"]), 6),
                    "string": e.get("string"), "fret": e.get("fret"),
                    "page": e.get("page"), "advanced": bool(e.get("advanced")),
                })
    else:
        prev_strum_t = None
        for f in frames:
            evs = [e for e in sound if abs(float(e["t"]) - f["t"]) <= 1e-4]
            strum = next((e for e in evs if e.get("kind") == "strum"), None)
            dense = False
            if strum is not None:
                if prev_strum_t is not None and (strum["t"] - prev_strum_t) < 0.32 - 1e-9:
                    dense = True
                prev_strum_t = strum["t"]
            for n in f["notes"]:
                if strum is not None:
                    out.append({"t": round(float(n["t"]), 6),
                                "pitch": int(n["pitch"]), "dropped": False,
                                "late": round(_late_for(None, strum["t"],
                                                        chord=strum.get("chord")), 6),
                                "chord": strum.get("chord"), "dense": dense,
                                "string": None, "fret": None, "page": strum.get("page")})
                    continue
                hit = None
                for e in evs:
                    if e.get("kind") == "pluck" and int(e.get("pitch", -1)) == int(n["pitch"]):
                        hit = e
                        break
                if hit is None:
                    out.append({"t": round(float(n["t"]), 6),
                                "pitch": int(n["pitch"]), "dropped": True,
                                "late": 0.0})
                else:
                    out.append({"t": round(float(n["t"]), 6),
                                "pitch": int(n["pitch"]), "dropped": False,
                                "late": round(_late_for(n["pitch"], hit["t"]), 6),
                                "chord": hit.get("chord"), "string": hit.get("string"),
                                "fret": None, "page": hit.get("page")})
    return {"kind": kind, "notes": out,
            "dropped": sum(1 for a in out if a.get("dropped")),
            "late": sum(1 for a in out if (a.get("late") or 0.0) > 0.002)}


def _compile_notes_timeline(song, path, role, notes, markers, t, *, start_offset,
                            onbeat_rule="highest", log=None, want_analysis=False,
                            detail=False):
    """真实编译一段音符 → 时间轴 dict（`compile_file` 与卷帘直通重编共用）。"""
    log = log or (lambda m: None)
    kind = role_kind(song, role)
    frames = build_frames(notes)
    if kind == "fret":
        sound, cstats = compile_fret(frames, timing=t, onbeat_rule=onbeat_rule,
                                     strum_chords=bool(song.get("fret_strum")), log=log)
    else:
        sound, cstats = compile_chord(frames, markers or [], timing=t, log=log,
                                      anti_repeat=bool(song.get("anti_repeat", True)))
    if not sound:
        return None
    start_page = int(cstats.get("start_page") or 1)
    hw, hstats = preschedule(sound, kind, timing=t, start_page=start_page, log=log,
                             detail=detail)
    if kind == "fret":
        log("换格 %d 次，翻页 %d 步，晚出（late）%d 个音"
            % (cstats.get("switches", 0), hstats.get("turns", 0), hstats.get("late", 0)))
    else:
        log("选格 %d 次，翻页 %d 步，晚出（late）%d 个音"
            % (hstats.get("clicks", 0), hstats.get("turns", 0), hstats.get("late", 0)))

    for e in hw:                       # 开头留白：留给"切回游戏窗口"的时间
        e["t"] = round(float(e["t"]) + start_offset, 6)
    hstats["duration"] = round(float(hstats.get("duration") or 0.0) + start_offset, 3)
    if hw and hw[0]["t"] < 0:          # 极端情况：整体再往后挪，别出现负时刻
        shift = -hw[0]["t"]
        for e in hw:
            e["t"] = round(e["t"] + shift, 6)

    tl = {
        "version": 1,
        "role": role,
        "role_cn": ROLE_CN.get(role, role),
        "kind": kind,
        "source": os.path.abspath(path) if path else "",
        "start": {
            "page": start_page,
            "sector": int(cstats.get("start_sector") or 1),
            "mode_cn": "品格" if kind == "fret" else "和弦",
            "wheel": _wheel_hint(kind, start_page),
        },
        "stats": dict(list(cstats.items()) + list(hstats.items())),
        "compile_stats": cstats,
        "hw_stats": hstats,
        "adapter": song.get("adapter") or {},
        "timing": t.as_dict(),
        "events": hw,
    }
    if want_analysis:
        tl["analysis"] = _analyze_notes(frames, sound, hstats, kind)
    return tl


def compile_custom_role(song_or_path, role, custom_notes, timing=None, chord_slices=None,
                        log=None, start_offset=None, onbeat_rule="highest",
                        enable_arpeggio=None, enable_chord_rhythm=None):
    """钢琴卷帘直通重编：跳过 `adapt_song` 的自动重生成，直接编译用户改过的音符。

    `song_or_path`：路径（内部会正常 load_song 以拿到元信息/和弦标记）或已加载的 song。
    `custom_notes`：`[{"t","dur","pitch","vel"}]`，原样送入 `compile_fret/compile_chord`。
    `chord_slices`：和弦角色用，`[{"t","name"}]` 或 `[(t, name)]`。
    返回与 `compile_file` 同构的时间轴；额外带 `custom=True` 与 `analysis`。
    """
    log = log or (lambda m: None)
    t = timing or Timing()
    if start_offset is None:
        start_offset = float(t.p21)
    if isinstance(song_or_path, (str, bytes, os.PathLike)):
        path = os.fspath(song_or_path)
        song = load_song(path, enable_arpeggio=enable_arpeggio,
                         enable_chord_rhythm=enable_chord_rhythm, timing=t)
    else:
        song = song_or_path or {}
        path = song.get("path") or ""
    kind = role_kind(song, role)

    notes = []
    for n in custom_notes or []:
        try:
            notes.append({"t": round(max(0.0, float(n["t"])), 6),
                          "dur": round(max(0.001, float(n.get("dur") or 0.25)), 6),
                          "pitch": int(n["pitch"]),
                          "vel": int(n.get("vel") or 80),
                          "track": int(n.get("track") or 0)})
        except Exception:
            continue

    markers = []
    if kind == "chord":
        for s in (chord_slices or song.get("markers") or []):
            if isinstance(s, dict):
                markers.append((round(float(s.get("t") or 0.0), 6),
                                s.get("name") or s.get("chord")))
            elif isinstance(s, (list, tuple)) and len(s) >= 2:
                markers.append((round(float(s[0]), 6), s[1]))
        markers.sort(key=lambda x: x[0])
    else:
        markers = list(song.get("markers") or [])

    log("卷帘直通重编：角色 = %s，%d 个音（真实编译器）"
        % (ROLE_CN.get(role, role), len(notes)))
    tl = _compile_notes_timeline(song, path, role, notes, markers, t,
                                 start_offset=start_offset, onbeat_rule=onbeat_rule,
                                 log=log, want_analysis=True, detail=True)
    if tl is None:
        tl = {
            "version": 1, "role": role, "role_cn": ROLE_CN.get(role, role),
            "kind": kind, "source": os.path.abspath(path) if path else "",
            "start": {"page": 1, "sector": 1,
                      "mode_cn": "品格" if kind == "fret" else "和弦", "wheel": ""},
            "stats": {"frames": 0, "notes": len(notes), "voices": 0,
                      "events": 0, "turns": 0, "clicks": 0, "plucks": 0,
                      "strums": 0, "switches": 0, "late": 0, "max_late": 0.0,
                      "duration": 0.0, "hw_events": 0, "dropped": len(notes)},
            "compile_stats": {}, "hw_stats": {"events": 0, "turns": 0, "late": 0,
                                              "max_late": 0.0, "hw_events": 0,
                                              "duration": 0.0},
            "adapter": song.get("adapter") or {}, "timing": t.as_dict(),
            "events": [],
            "analysis": {"kind": kind, "dropped": len(notes), "late": 0,
                         "notes": [{"t": n["t"], "pitch": n["pitch"],
                                    "dropped": True, "late": 0.0} for n in notes]},
        }
    tl["custom"] = True
    return tl


def compile_file(path, role=ROLE_SOLO, timing=None, onbeat_rule="highest",
                 log=None, start_offset=None, enable_arpeggio=None, arpeggio_pattern=None,
                 enable_chord_rhythm=None, detail=False):
    """编译一首 .mid（返回可以直接 json.dump 的 dict）。

    `start_offset`：开头留白（秒），留给"切回游戏窗口"用；None = 读配置里的
                     「开始演奏前的等待」（默认 3 秒，与现有软件的 lead 一致）。
    `enable_arpeggio`：是否开启方案 B 横按分解琶音/指弹独奏（None 时从配置读取）。
    `arpeggio_pattern`：琶音走向 ("pingpong" 往返 / "ascending" 上行 / "pinch" 抓弦)。
    """
    log = log or (lambda m: None)
    t = timing or Timing()
    if start_offset is None:
        start_offset = float(t.p21)
    song = load_song(path, enable_arpeggio=enable_arpeggio,
                     arpeggio_pattern=arpeggio_pattern,
                     enable_chord_rhythm=enable_chord_rhythm, timing=t)
    kind = role_kind(song, role)

    track, how = pick_track(song, role)
    if track is None:
        if (song.get("adapter") or {}).get("no_chord_track") \
                and role in (ROLE_DUET_RHYTHM, ROLE_DUET_RHYTHM_FRET):
            log("角色 = %s：本曲没有和弦轨，返回空事件（no_chord_track=True）"
                % ROLE_CN.get(role, role))
            return _empty_no_chord(path, role, t)
        raise ValueError("这份 .mid 里没有可用的音符：%s" % how)
    notes = song["notes_by_track"].get(track, [])
    log("角色 = %s，锁定「%s」模式；%s，%d 个音"
        % (ROLE_CN.get(role, role), "品格" if kind == "fret" else "和弦", how, len(notes)))
    ad = song.get("adapter") or {}
    for line in midi_adapter.report_lines(ad):
        log(line)

    def _run(note_list):
        return _compile_notes_timeline(song, path, role, note_list,
                                       song.get("markers") or [], t,
                                       start_offset=start_offset,
                                       onbeat_rule=onbeat_rule, log=log,
                                       detail=detail)

    tl = _run(notes)
    if tl is None:
        raise ValueError("编译不出任何发声动作（音符可能全在音域之外）")
    # 30fps 等慢档下，若 Solo 伴奏把主旋律挤出晚出，优先牺牲伴奏保住主旋律 0 晚出。
    if role == ROLE_SOLO and int(tl.get("stats", {}).get("late") or 0) > 0:
        late_times = [float(d.get("t") or 0.0)
                      for d in (tl.get("stats", {}).get("late_detail") or [])]
        if not late_times:
            late_times = [float(e["t"]) for e in (tl.get("events") or [])
                          if e.get("sound")]
        def _is_melody(n):
            return bool(n.get("is_melody")) or n.get("src") == "melody"
        window = max(0.25, float(t.advance_need(1, True, True)))
        trimmed = [n for n in notes if _is_melody(n) or not any(
            abs(float(n.get("t") or 0.0) - lt) <= window for lt in late_times)]
        if len(trimmed) < len(notes):
            got = _run(trimmed)
            if got is not None and int(got.get("stats", {}).get("late") or 0) < int(tl["stats"]["late"]):
                tl = got
        if int(tl.get("stats", {}).get("late") or 0) > 0:
            melody_only = [n for n in notes if _is_melody(n)]
            got = _run(melody_only) if melody_only else None
            if got is not None:
                tl = got
    return tl

def _wheel_hint(kind, page):
    """界面/日志里的“第几页、轮盘上是什么”提示。"""
    if kind == "chord":
        try:
            names = modes.get("chord").pages[int(page) - 1].label_text()
        except Exception:
            names = ""
        return "和弦第 %d 页：%s" % (page, names)
    try:
        from plan import FRET_PAGES
        return FRET_PAGES.get(int(page), {}).get("wheel_hint", "")
    except Exception:
        return ""

def save_timeline(tl, path):
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tl, f, ensure_ascii=False, indent=1)
    return path

def report_text(tl):
    """一份给人看的报告（界面/日志用）。"""
    st = tl.get("stats") or {}
    cs = tl.get("compile_stats") or {}
    hw = tl.get("hw_stats") or {}
    lines = []
    for line in midi_adapter.report_lines(tl.get("adapter")):
        lines.append(line)
    lines.append("角色：%s　模式：%s" % (tl.get("role_cn"), tl.get("kind")))
    lines.append("起始：%s模式 第 %d 页　%s"
                 % (tl["start"]["mode_cn"], tl["start"]["page"], tl["start"]["wheel"]))
    if tl.get("kind") == "fret":
        lines.append("音符 %d / %d 帧　发声 %d　提前拨 %d　换格 %d 次　品格 %d~%d"
                     % (st.get("notes", 0), st.get("frames", 0), st.get("voices", 0),
                        st.get("advanced", 0), cs.get("switches", 0),
                        st.get("fret_min", 0), st.get("fret_max", 0)))
    else:
        lines.append("音符 %d / %d 帧　扫弦 %d、单拨 %d　用了 %d 个和弦%s"
                     % (st.get("notes", 0), st.get("frames", 0), st.get("strums", 0),
                        st.get("plucks", 0), st.get("chords_used", 0),
                        "（%d 帧没和弦标记，靠猜）" % st["guessed_chords"]
                        if st.get("guessed_chords") else ""))
    lines.append("硬件动作 %d 条：翻页 %d 步，点格 %d 次，按键 %d 次，扫弦 %d 次"
                 % (hw.get("hw_events", 0), hw.get("turns", 0), hw.get("clicks", 0),
                    hw.get("plucks", 0), hw.get("strums", 0)))
    if hw.get("switches"):
        lines.append("中途用 Z 切模式 %d 次（切完自动回第 1 页）"
                     % hw["switches"])
    if hw.get("late"):
        lines.append("⚠ 有 %d 个音为了让换格链完整，被迫晚了（最多 %.0f 毫秒）"
                     % (hw["late"], (hw.get("max_late") or 0) * 1000))
    lines.append("总时长 %.1f 秒" % (hw.get("duration", 0.0)))
    return "\n".join(lines)

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        print("÷python midi_compiler.py <.mid> "
              "[--role solo|duet_lead|duet_rhythm|duet_rhythm_fret] "
              "[--out ʱ.json] [--onbeat coverage|highest]")
        return 2
    path = args[0]
    role, out, rule = ROLE_SOLO, None, "highest"
    enable_arpeggio = None
    arpeggio_pattern = None
    i = 1
    while i < len(args):
        if args[i] == "--role" and i + 1 < len(args):
            role = args[i + 1]; i += 2
        elif args[i] == "--out" and i + 1 < len(args):
            out = args[i + 1]; i += 2
        elif args[i] == "--onbeat" and i + 1 < len(args):
            rule = args[i + 1]; i += 2
        elif (args[i] in ("--pattern", "--arpeggio-pattern")) and i + 1 < len(args):
            arpeggio_pattern = args[i + 1]; i += 2
        elif args[i] == "--no-arpeggio":
            enable_arpeggio = False; i += 1
        elif args[i] == "--arpeggio":
            enable_arpeggio = True; i += 1
        else:
            i += 1
    tl = compile_file(path, role=role, onbeat_rule=rule, log=print,
                      enable_arpeggio=enable_arpeggio, arpeggio_pattern=arpeggio_pattern)
    print(report_text(tl))
    if out is None:
        base, _ = os.path.splitext(path)
        out = "%s-timeline-%s.json" % (base, role)
    save_timeline(tl, out)
    print("写入：%s" % out)
    return 0

if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.exit(main())
