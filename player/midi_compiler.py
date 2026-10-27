# -*- coding: utf-8 -*-
"""midi_compiler.py -- 《异键演奏》v1.1 编译器：多轨 .mid → 指法 → 硬件动作时间轴。

四步管线：
  ① 读歌      `load_song()`      解析 Track 0 的 `SOLO_MODE:` / `P2_MODE:` / `CHORD:` 标记，
                                 按轨名映射四角色音符序列。
  ② 分帧      `build_frames()`   起始时间差 ≤ 15ms 的音聚为同一发声帧。
  ③ 定指法    `compile_fret()`   品格模式：束搜索（beam_width=8）选品格/弦位；
              `compile_chord()`  和弦模式：柱式多弦同按 / 右键下扫 / 安全弦单拨；
              `_compile_hybrid_p2()` 2P 和弦 ↔ 品格 Z 键自适应混切编译。
  ④ 反向预调度 `preschedule()`   从每个音的 `T_on` 倒推翻页/选格，串上 18 项参数，
                                 输出带绝对时刻的硬件动作时间轴。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mido

import midi_adapter
import modes
import player_config
from midi_loader import (build_time_converter, normalize_chord_text,
                         read_flat_notes, read_chord_markers)

# ══════════════════════════════════════════════════════════════════════════
#  常量
# ══════════════════════════════════════════════════════════════════════════
OPEN_PITCH = {1: 40, 2: 45, 3: 50, 4: 55, 5: 59, 6: 64}   # 1=E2(最粗) … 6=E4(最细)
PITCH_TO_STRING = {v: k for k, v in OPEN_PITCH.items()}
MAX_FRET = 24
FRAME_GAP = 0.015          # 起始时间差 ≤ 15ms 算同一帧
BEAM_WIDTH = 8             # 束搜索每步保留 8 条路径
PAGE_ZERO = 4              # 品格模式第 5 页（0 品）的 0 起算页号

# 四角色定义
ROLE_SOLO = "solo"
ROLE_DUET_LEAD = "duet_lead"
ROLE_DUET_RHYTHM = "duet_rhythm"
ROLE_DUET_RHYTHM_FRET = "duet_rhythm_fret"
ROLES = (ROLE_SOLO, ROLE_DUET_LEAD, ROLE_DUET_RHYTHM, ROLE_DUET_RHYTHM_FRET)
ROLE_CN = {
    ROLE_SOLO: "1P 单人独奏（Solo 指弹）",
    ROLE_DUET_LEAD: "双人合奏 - 1P 主音",
    ROLE_DUET_RHYTHM: "双人合奏 - 2P 伴奏（柱式/扫弦）",
    ROLE_DUET_RHYTHM_FRET: "双人合奏 - 2P 伴奏（琶音/根音）",
}

TRACK_SOLO_MASTER = "SOLO_MASTER"
TRACK_DUET_LEAD = "DUET_P1_LEAD"
TRACK_DUET_RHYTHM = "DUET_P2_RHYTHM"
TRACK_DUET_P2_ARP = "DUET_P2_ARP"
TRACK_ALIASES = {
    TRACK_SOLO_MASTER: ("solo_master", "melody", "旋律", "主旋律", "lead", "vocal"),
    TRACK_DUET_LEAD: ("duet_p1_lead", "duet_lead", "melody", "旋律", "主旋律", "lead"),
    TRACK_DUET_RHYTHM: ("duet_p2_rhythm", "duet_rhythm", "chord", "chords",
                        "伴奏", "和弦", "accomp", "harmony"),
}

# ══════════════════════════════════════════════════════════════════════════
#  时间参数映射（与 player_config 键名对齐）
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
        return max(self.p12, 0.005)

    def key_hold(self):
        return max(self.p4, 0.005)

    def click_hold(self, kind=None):
        p = self.p2 if str(kind or "").lower() == "fret" else self.p7
        return max(p, 0.005)

    def strum_hold_short(self):
        return max(self.p9, 0.005)

    def repluck_gap(self):
        return max(self.p20, 0.005)

    def switch_span(self, steps, cross_page=False, need_click=True):
        """上一个音拨响 → 下一个音准时响，需要预留的真实时长。"""
        steps = int(steps or 0)
        press_delay = max(0.001, self.p1)
        if steps <= 0:
            # 同页换扇区：按下左键 1ms 后即可按弦；若连续换扇区则受上一次左键按住时长约束
            span = (self.p2 + press_delay + self.p17) if need_click else press_delay
            return round(span, 6)
        if steps == 1:
            span = self.p12 + self.p14 + self.p16 + self.p18
            return round(span, 6)
        span = (steps * self.p12 + (steps - 1) * self.p13
                + self.p14 + self.p16 + self.p18)
        return round(span, 6)

    def advance_need(self, steps, cross_page=False, need_click=True):
        """提前量 = switch_span + #1 + #22。"""
        return round(self.switch_span(steps, cross_page, need_click)
                     + self.p1 + self.p22, 6)

    def page_part(self, steps):
        if steps <= 0:
            return 0.0
        return round(steps * self.p12 + (steps - 1) * self.p13 + self.p14, 6)


STRINGS_SIMULTANEOUS = True
MIN_HOLD = 0.005
PAGE_SETTLE_MAX = 0.0
PAGE_TURN_BIAS = 0.068


# ══════════════════════════════════════════════════════════════════════════
#  ① 读歌
# ══════════════════════════════════════════════════════════════════════════
def _track_index_by_name(names, wanted, aliases=()):
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
    """Track 0 的 `P2_MODE:fret|chord` 文本事件 → [(秒, 模式)]，按时间排序。"""
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
                fret_strum_explicit = True
                fret_strum = up.split(":", 1)[1].strip().lower() in ("on", "true", "1", "yes")
            elif up.startswith("ANTI_REPEAT:"):
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
        "mode_markers": _read_p2_mode_markers(mid),
        "markers": read_chord_markers(path),
        "total_duration": round(mid.length, 3),
    }


def load_song(path, enable_arpeggio=None, arpeggio_pattern=None,
              enable_chord_rhythm=None, timing=None, force_recalc=False,
              chord_strum_mode=None):
    """读整首歌：元信息 + 每轨音符。"""
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
    midi_adapter.adapt_song(meta, notes, enable_arpeggio=enable_arpeggio,
                            arpeggio_pattern=arpeggio_pattern,
                            enable_chord_rhythm=enable_chord_rhythm,
                            timing=timing, force_recalc=force_recalc,
                            chord_strum_mode=chord_strum_mode)
    return meta


def pick_track(song, role):
    """角色 → 读哪条轨。返回 (轨号, 说明文字)。"""
    idx = song["track_index"]
    if role in (ROLE_DUET_RHYTHM, ROLE_DUET_RHYTHM_FRET) \
            and (song.get("adapter") or {}).get("no_chord_track") \
            and idx.get(TRACK_DUET_RHYTHM) is None:
        return None, "本曲没有和弦轨（no_chord_track=True），2P 无事件"
    p2 = song.get("p2")
    if p2 and role in (ROLE_DUET_RHYTHM, ROLE_DUET_RHYTHM_FRET) \
            and idx.get(TRACK_DUET_RHYTHM) == p2.get("track"):
        want = "arp" if role == ROLE_DUET_RHYTHM_FRET else "chord"
        song["notes_by_track"][p2["track"]] = p2["notes"].get(want) or []
        # 关键修复：角色 3（arp）在和弦段同样需要完整的和弦切片标记，不能设为空列表，
        # 否则 compile_chord 找不到和弦名会对每颗单音琶音瞎猜和弦导致高频乱点轮盘！
        mks = (p2.get("markers") or {})
        song["markers"] = list(mks.get(want) or mks.get("chord") or [])
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
    """角色 → 锁定/起始的模式。"""
    if role == ROLE_DUET_LEAD:
        return "fret"
    if role in (ROLE_DUET_RHYTHM, ROLE_DUET_RHYTHM_FRET):
        return "chord"
    return "chord" if song.get("solo_mode") == "chord" else "fret"


# ══════════════════════════════════════════════════════════════════════════
#  ② 分帧
# ══════════════════════════════════════════════════════════════════════════
def build_frames(notes, gap=FRAME_GAP):
    """按起始时间差 ≤ gap 聚成帧；同帧同音高只留一个。"""
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
    return PITCH_TO_STRING.get(int(pitch) - int(fret))


def fret_candidates_for_pitch(pitch, max_fret=MAX_FRET):
    out = []
    for s, op in OPEN_PITCH.items():
        f = int(pitch) - op
        if 0 <= f <= max_fret:
            out.append((f, s))
    return out


def _assign_group(notes, fret):
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
    """品格模式：束搜索选出每帧用哪个品格、哪根弦、谁提前。"""
    t = timing or Timing()
    log = log or (lambda m: None)
    if not frames:
        return [], {"frames": 0, "notes": 0, "voices": 0}

    cands = [_frame_candidates(f, t, onbeat_rule, max_fret) for f in frames]

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
                    if "string" not in e:
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
    d = float(dur or 0.0)
    if t.p5 <= 0 or d < t.p19:
        return round(max(0.005, t.p4), 4)
    return round(max(0.005, min(d, t.p5)), 4)


def _frame_candidates(frame, t, onbeat_rule, max_fret):
    notes = frame["notes"]
    if not notes:
        return []
    playable = [n for n in notes if fret_candidates_for_pitch(n["pitch"], max_fret)]
    dropped = [n for n in notes if n not in playable]
    if not playable:
        return [{"groups": [], "bad": [n["pitch"] for n in dropped],
                 "dropped": len(dropped)}]

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

    if onbeat_rule == "highest":
        cands = [c for c in cover if any(n["pitch"] == top_pitch for n, _s in c[1])]
    else:
        cands = cover[:4]

    out = []
    for (f_on, ok_on, bad_on) in cands:
        groups = []
        drop = list(dropped)
        if bad_on:
            best = None
            p_on = _page_of(f_on)
            for f in range(0, max_fret + 1):
                ok_adv, still = _assign_group(bad_on, f)
                if not ok_adv:
                    continue
                lowest = min(n["pitch"] for n, _s in ok_adv)
                steps, _k = page_delta(p_on, _page_of(f))
                score = (-len(ok_adv), int(steps), lowest, f)
                if best is None or score < best[0]:
                    best = (score, f, ok_adv, still)
            if best:
                _sc, f_adv, ok_adv, still = best
                groups.append({"fret": f_adv, "pairs": ok_adv, "advanced": True})
                drop.extend(still)
            else:
                drop.extend(bad_on)
        groups.append({"fret": f_on, "pairs": ok_on, "advanced": False})

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
    cs = fret_candidates_for_pitch(pitch, max_fret)
    return min(f for f, _s in cs) if cs else 0


def _fallback_candidates(frame, max_fret):
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
    groups = cand.get("groups") or []
    if not groups:
        return 1e6
    cost = 0.0
    if prev_fret is not None and prev_t is not None:
        f0 = groups[0]["fret"]
        pa, pb = _page_of(prev_fret), _page_of(f0)
        steps, _k = page_delta(pa, pb)
        need = (t.advance_need(steps, pa != pb, need_click=(pb != PAGE_ZERO))
                if prev_fret != f0 else t.p1)
        cost += need + PAGE_TURN_BIAS * int(steps)
        if (t_on - prev_t) < need - 1e-9:
            cost += 1000.0
    for g in groups:
        cost += abs(float(g.get("t_off") or 0.0))
        if g.get("advanced") and g.get("pairs"):
            low = min(g["pairs"], key=lambda pr: pr[0]["pitch"])
            cost += 0.0005 * int(low[1])
    for g in groups:
        for (_n, s) in g["pairs"]:
            if busy[s] - t_on > 0.15:
                cost += 50.0
    return cost


def _fret_stats(frames, events, t):
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
CHORD_NAMES = [c.value for pg in modes.get("chord").pages for c in pg.cells]


def chord_at(t_on, markers, fallback=None):
    got = fallback
    for (mt, name) in markers or []:
        if mt <= t_on + 1e-6:
            got = name
        else:
            break
    return got


def chord_sounding_strings(name):
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
    import guitar_plan as _GP
    sh = modes.shape("chord", name)
    if sh is None:
        return []
    pcs = _GP.chord_pitch_class_set(name)
    out = []
    for s in _GP.pluck_safe_strings(name):
        p = _GP.pluck_pitch(name, s)
        if p is not None and (int(p) % 12) in pcs:
            out.append((s, p))
    if not out:
        for s in _GP.pluck_safe_strings(name):
            p = _GP.pluck_pitch(name, s)
            if p is not None:
                out.append((s, p))
    out.sort(key=lambda x: x[1])
    return out


def _prev_string_pitch(name, string):
    if not name:
        return None
    try:
        import guitar_plan as _GP
        return _GP.pluck_pitch(name, int(string))
    except Exception:
        return None


def _pick_string_anti_repeat(free, pitch, t, last, name=None, n_notes=1):
    if not free:
        return None
    cands = list(free)
    if last is not None:
        p_t, p_pitch, p_s = last
        if (t - float(p_t)) < 0.50 and int(pitch) != int(p_pitch):
            others = [sp for sp in cands if int(sp[0]) != int(p_s)]
            if others:
                cands = others
            up = int(pitch) > int(p_pitch)
            prev_p = _prev_string_pitch(name, p_s)
            if prev_p is not None:
                if up:
                    same = [sp for sp in cands if int(sp[1]) > int(prev_p)]
                else:
                    same = [sp for sp in cands if int(sp[1]) < int(prev_p)]
            else:
                if up:
                    same = [sp for sp in cands if int(sp[0]) > int(p_s)]
                else:
                    same = [sp for sp in cands if int(sp[0]) < int(p_s)]
            if same:
                cands = same
    return min(cands, key=lambda sp: (abs(sp[1] - int(pitch)), sp[0]))


def compile_chord(frames, markers, timing=None, log=None, min_strum_notes=3,
                  anti_repeat=True, allow_strum=True):
    """和弦模式：柱式多弦同按 / 鼠标右键下扫 / 数字键安全弦单拨。"""
    t = timing or Timing()
    log = log or (lambda m: None)
    if not frames:
        return [], {"frames": 0, "notes": 0, "strums": 0, "plucks": 0}

    events, cur, guessed = [], None, 0
    last_pluck = None
    for frame in frames:
        notes = frame["notes"]
        name = chord_at(frame["t"], markers, fallback=None)
        if name is None:
            for n in notes:
                if n.get("chord"):
                    name = n.get("chord")
                    break
        if name is None:
            name = cur
        if name is None:
            name = _guess_chord(notes)
            guessed += 1
        if name is None:
            continue
        loc = modes.locate("chord", name)
        if not loc:
            continue
        cur = name
        page, sec = loc
        base = {"t": round(frame["t"], 6),
                "dur": round(max(n["dur"] for n in notes), 6),
                "chord": name, "page": int(page), "sector": int(sec)}
        if allow_strum and len(notes) >= min_strum_notes:
            vel = sum(n.get("vel") or 80 for n in notes) / float(len(notes))
            ev = dict(base)
            ev.update({"kind": "strum", "direction": "D",
                       "vel": int(round(vel)), "hold": None})
            events.append(ev)
        else:
            strings = chord_pluck_strings(name)
            if not strings:
                continue
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
    """`P2_MODE:` 标记 → 状态机里的模式切换事件。"""
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
    """把发声事件编译成硬件动作时间轴。"""
    t = timing or Timing()
    log = log or (lambda m: None)
    click_gap = 0.0
    if not events:
        return [], {"events": 0, "turns": 0, "clicks": 0, "plucks": 0,
                    "strums": 0, "switches": 0, "late": 0, "max_late": 0.0,
                    "duration": 0.0, "late_times": []}

    hw = []
    cur_page = int(start_page) - 1
    cur_sec = None
    cur_kind = kind
    prev_action = None
    prev_kbd = None
    prev_mouse = None
    prev_sound_t = None
    group_key = None
    group_shift = 0.0
    last_key_down = {}
    late = []
    late_detail = []
    late_times_raw = []
    turns = clicks = plucks = strums = switches = 0
    last_sound = 0.0

    for ev in sorted(events, key=lambda e: (e["t"], e.get("order", 0))):
        # ── 模式切换（Z 键）──────────────────────────────────────────────
        if ev.get("kind") == "mode":
            hold = t.page_key_hold()
            release_wait = max(t.p1, t.p14 + t.p16)
            t_up = float(ev["t"]) - release_wait
            t_down = t_up - hold
            if prev_action is not None and t_down < prev_action + t.p1 - 1e-9:
                shift = (prev_action + t.p1) - t_down
                t_down += shift
                t_up += shift
            hw.append(_hw(t_down, "key_down", key="Z"))
            hw.append(_hw(t_up, "key_up", key="Z"))
            prev_action = t_up
            prev_kbd = max(prev_kbd or t_up, t_up)
            cur_kind = str(ev.get("mode") or cur_kind)
            # 按 Z 切模式后页码回到第 1 页（0），但轮盘扇区索引跨模式继承；
            # 显式保留扇区，严禁重置为 None 导致对已选中扇区重复点左键触发扫弦。
            cur_page, cur_sec = 0, cur_sec
            group_key, group_shift = None, 0.0
            switches += 1
            continue

        page, sec = int(ev["page"]), int(ev["sector"])
        is_strum = ev["kind"] == "strum"
        park = t.p8 if is_strum else 0.0
        t_on = float(ev["t"])

        same_group = (group_key is not None and abs(t_on - group_key) < 1e-6)
        if same_group:
            t_on += group_shift
        else:
            group_key, group_shift = t_on, 0.0
        t_ready = t_on - park

        # 第 5 页（0 品）六格都是 Zero：不用点扇区
        need_sector = not (cur_kind == "fret" and page == PAGE_ZERO)
        # 显式禁止同页同扇区重复点格（避免在已选中格子上再点左键触发扫弦）
        same_cell = (page == cur_page) and (sec == cur_sec)
        need_switch = (page != cur_page) or (need_sector and sec != cur_sec)
        if same_cell and need_sector:
            need_switch = False

        chain = []
        pg_events, wheel_events = [], []
        if same_group:
            pass
        elif need_switch:
            cross = page != cur_page
            steps, key = page_delta(cur_page, page) if cross else (0, None)
            hold = t.page_key_hold()

            # ① 翻页（松开判定）
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

            # ② 点扇区（按下即触发：在拨弦前 1ms 按下左键即可立刻按弦，按住满帧时长+1ms后松开）
            if need_sector and sec != cur_sec:
                hold_click = t.click_hold(cur_kind)
                press_delay = max(0.001, t.p1)
                t_click = t_ready - press_delay - t.p17 - click_gap
                wheel_events.append((t_click - 0.001, "move_sector",
                                     {"page": page, "sector": sec}))
                wheel_events.append((t_click, "mouse_down", {"btn": "left"}))
                wheel_events.append((t_click + hold_click, "mouse_up", {"btn": "left"}))
                clicks += 1
            # 关键修复：访问第 5 页（PAGE_ZERO，need_sector=False）时未点选扇区，
            # 游戏内高亮扇区保持原样，因此仅在 need_sector 为真时更新 cur_sec！
            cur_page = page
            if need_sector:
                cur_sec = sec

            if pg_events:
                earliest = min(x[0] for x in pg_events)
                floor = (prev_action + t.p1) if prev_action is not None else None
                room = (earliest - floor) if floor is not None else 0.0
                if room > 0:
                    bp = min(room, PAGE_SETTLE_MAX)
                    pg_events = [(tt - bp, o, kw) for (tt, o, kw) in pg_events]
            chain = pg_events + wheel_events
            if is_strum:
                chain.append((t_ready, "move_park", {}))
        else:
            if is_strum:
                chain.append((t_ready, "move_park", {}))

        # ── 碰撞钳制 ──────────────────────────────────────────────────
        if chain:
            press = [c for c in chain if c[1] not in ("move_sector", "move_park")]
            earliest = min(c[0] for c in (press or chain))
            kinds = {("kbd" if c[1].startswith("key_") else "mouse") for c in press}
            ref = prev_action
            if kinds == {"mouse"} and prev_mouse is not None:
                ref = prev_mouse
            if ref is not None and earliest < ref + t.p1 - 1e-9:
                shift = (ref + t.p1) - earliest
                chain = [(ct + shift, op, kw) for (ct, op, kw) in chain]
                t_on += shift
                t_ready += shift
                group_shift += shift
                late.append(shift)
                late_times_raw.append(round(float(ev["t"]), 6))
                if detail:
                    late_detail.append({"t": round(float(ev["t"]), 6),
                                        "pitch": ev.get("pitch"),
                                        "chord": ev.get("chord"),
                                        "shift": round(shift, 6),
                                        "why": "switch"})
            chain.sort(key=lambda x: x[0])
            for (ct, op, kw) in chain:
                hw.append(_hw(ct, op, **kw))
                if op != "mouse_up":
                    prev_action = max(prev_action or ct, ct)
                if op.startswith("key_"):
                    prev_kbd = max(prev_kbd or ct, ct)
                elif op.startswith("mouse_"):
                    prev_mouse = max(prev_mouse or ct, ct)

        # ── 发声动作本体 ─────────────────────────────────────────────
        if is_strum:
            btn = "right" if ev.get("direction") == "D" else "left"
            hold = t.p10 if t.p10 > 1e-9 else t.strum_hold_short()
            hw.append(_hw(t_on, "mouse_down", btn=btn, sound=True))
            hw.append(_hw(t_on + hold, "mouse_up", btn=btn))
            prev_mouse = max(prev_mouse or t_on, t_on)
            strums += 1
        else:
            key = str(int(ev["string"]))
            hold = ev.get("hold")
            if hold is None:
                hold = _hold_for(ev.get("dur") or 0.0, t)
            last = last_key_down.get(key)
            if last is not None and (t_on - last) < t.repluck_gap() - 1e-9:
                shift = (last + t.repluck_gap()) - t_on
                t_on += shift
                late.append(shift)
                late_times_raw.append(round(float(ev["t"]), 6))
                if detail:
                    late_detail.append({"t": round(float(ev["t"]), 6),
                                        "pitch": ev.get("pitch"),
                                        "chord": ev.get("chord"),
                                        "shift": round(shift, 6),
                                        "why": "repluck"})
            last_key_down[key] = t_on
            hw.append(_hw(t_on, "key_down", key=key, sound=True))
            hw.append(_hw(t_on + float(hold), "key_up", key=key))
            prev_kbd = max(prev_kbd or t_on, t_on)
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
             "duration": round(last_sound, 3), "hw_events": len(hw),
             "late_times": sorted(set(late_times_raw))}
    if detail:
        stats["late_detail"] = late_detail
    if late:
        log("⚠ 有 %d 个音为了让换格链完整，被迫晚了（最多 %.0f 毫秒）"
            % (len(late), max(late) * 1000))
    return hw, stats


def _op_order(op):
    return {"key_up": 0, "mouse_up": 0, "move_park": 1, "move_sector": 1,
            "key_down": 2, "mouse_down": 2}.get(op, 3)


def _retrigger_guard(hw, t):
    """同弦重拨抬键保护。"""
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
            if i - 1 < len(ups):
                u = ups[i - 1]
            else:
                cand = [x for x in ups
                        if times[i - 1] - 1e-6 <= x["t"] <= times[i] + 1e-3]
                if not cand:
                    continue
                u = max(cand, key=lambda x: x["t"])
            if u["t"] > limit:
                floor = times[i - 1] + 0.005
                u["t"] = round(max(floor, limit), 6)
    return hw


def _interlock(hw, t):
    """单弦 / 扫弦互斥锁。"""
    holds = []
    down = None
    for e in sorted(hw, key=lambda x: x["t"]):
        if e["op"] == "mouse_down" and e.get("sound"):
            down = e
        elif e["op"] == "mouse_up" and down:
            holds.append([down["t"], e["t"], down["btn"], e])
            down = None
    if not holds:
        return hw
    conflicts = [e for e in hw
                 if (e["op"] == "key_down" and e["key"].isdigit())
                 or (e["op"] == "mouse_down" and e["btn"] == "left")]
    for (d_t, u_t, btn, up_ev) in holds:
        if u_t - d_t < t.p1:
            continue
        near = [c for c in conflicts if d_t < c["t"] < u_t + t.p1]
        if near:
            new_up = min(c["t"] for c in near) - t.p1
            if new_up > d_t:
                up_ev["t"] = round(new_up, 6)
    return hw


def reschedule_for_speed(timeline, speed=1.0):
    """倍速播放重排：只缩放音符/帧间隔，保持机械按键按住与翻页等待时长为恒定物理毫秒。"""
    spd = float(speed or 1.0)
    if abs(spd - 1.0) <= 1e-4 or not timeline:
        return timeline
    out = dict(timeline)
    evs = [dict(e) for e in (timeline.get("events") or [])]
    if not evs:
        return out
    t_cfg = Timing()
    min_key_hold = t_cfg.key_hold()
    min_page_hold = t_cfg.page_key_hold()
    min_click_hold = t_cfg.click_hold(timeline.get("kind"))
    for e in evs:
        e["t"] = round(float(e["t"]) / spd, 6)
    # 恢复因除以 speed 而被压缩的按下->松开脉冲宽度
    open_downs = {}
    for e in evs:
        op = e.get("op")
        if op == "key_down":
            open_downs[("k", e.get("key"))] = float(e["t"])
        elif op == "key_up":
            k = ("k", e.get("key"))
            if k in open_downs:
                dt = open_downs.pop(k)
                floor = min_page_hold if e.get("key") in ("T", "R", "Z") else min_key_hold
                if float(e["t"]) - dt < floor:
                    e["t"] = round(dt + floor, 6)
        elif op == "mouse_down":
            open_downs[("m", e.get("btn"))] = float(e["t"])
        elif op == "mouse_up":
            k = ("m", e.get("btn"))
            if k in open_downs:
                dt = open_downs.pop(k)
                if float(e["t"]) - dt < min_click_hold:
                    e["t"] = round(dt + min_click_hold, 6)
    evs.sort(key=lambda e: (e["t"], _op_order(e["op"])))
    out["events"] = evs
    return out


# ══════════════════════════════════════════════════════════════════════════
#  顶层：一首歌 → 一份时间轴
# ══════════════════════════════════════════════════════════════════════════
def _empty_no_chord(path, role, t):
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


def _analyze_notes(frames, sound, hstats, kind, mode_markers=None):
    """把真实编译结果回填到输入音符（逐帧按 mode_at(t) 分段校验）。"""
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

    pool = [dict(e) for e in sound]
    used = [False] * len(pool)

    def _match_fret(n, f):
        for i, e in enumerate(pool):
            if used[i] or int(e.get("pitch", -1)) != int(n["pitch"]):
                continue
            if e["t"] > f["t"] + 1e-6 or e["t"] < f["t"] - 0.20:
                continue
            used[i] = True
            return e
        return None

    prev_strum_t = None
    for f in frames:
        mode = _p2_mode_at(f["t"], mode_markers) if mode_markers else kind
        if mode == "fret":
            for n in f["notes"]:
                e = _match_fret(n, f)
                if e is None:
                    out.append({"t": round(float(n["t"]), 6),
                                "pitch": int(n["pitch"]), "dropped": True,
                                "late": 0.0, "mode": "fret"})
                    continue
                out.append({
                    "t": round(float(n["t"]), 6), "pitch": int(n["pitch"]),
                    "dropped": False, "late": round(_late_for(n["pitch"], e["t"]), 6),
                    "string": e.get("string"), "fret": e.get("fret"),
                    "page": e.get("page"), "advanced": bool(e.get("advanced")),
                    "mode": "fret",
                })
            continue
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
                            "string": None, "fret": None, "page": strum.get("page"),
                            "mode": "chord"})
                continue
            hit = None
            for e in evs:
                if e.get("kind") == "pluck" and int(e.get("pitch", -1)) == int(n["pitch"]):
                    hit = e
                    break
            if hit is None:
                out.append({"t": round(float(n["t"]), 6),
                            "pitch": int(n["pitch"]), "dropped": True,
                            "late": 0.0, "mode": "chord"})
            else:
                out.append({"t": round(float(n["t"]), 6),
                            "pitch": int(n["pitch"]), "dropped": False,
                            "late": round(_late_for(n["pitch"], hit["t"]), 6),
                            "chord": hit.get("chord"), "string": hit.get("string"),
                            "fret": None, "page": hit.get("page"),
                            "mode": "chord"})
    return {"kind": kind, "notes": out,
            "dropped": sum(1 for a in out if a.get("dropped")),
            "late": sum(1 for a in out if (a.get("late") or 0.0) > 0.002)}


def _count_doubled(events):
    cnt = {}
    for e in events or []:
        if e.get("kind") == "mode":
            continue
        k = round(float(e.get("t") or 0.0), 6)
        cnt[k] = cnt.get(k, 0) + 1
    return sum(1 for v in cnt.values() if v >= 2)


def _p2_mode_at(t, markers, default="chord"):
    got = default
    for (mt, mode) in markers or []:
        if float(mt) <= float(t) + 1e-6:
            got = str(mode or got)
        else:
            break
    return got


def _compile_hybrid_p2(frames, chord_markers, mode_markers, timing=None, log=None,
                       anti_repeat=True, allow_strum=True, start_mode=None):
    """2P 伴奏「和弦模式 ↔ 品格模式」自适应混切。"""
    t = timing or Timing()
    log = log or (lambda m: None)
    if not frames:
        return [], {"frames": 0, "notes": 0, "voices": 0, "hybrid": False,
                    "switches": 0, "start_page": 1, "start_sector": 1,
                    "start_mode": str(start_mode or "chord"), "z_switches": [],
                    "doubled": 0}

    segs = []
    for f in frames:
        mode = _p2_mode_at(f["t"], mode_markers)
        if not segs or segs[-1]["mode"] != mode:
            segs.append({"mode": mode, "frames": [f]})
        else:
            segs[-1]["frames"].append(f)

    def _compile(seg_frames, mode):
        if not seg_frames:
            return [], {}
        if mode == "fret":
            return compile_fret(seg_frames, timing=t, log=log)
        return compile_chord(seg_frames, chord_markers or [], timing=t, log=log,
                             anti_repeat=anti_repeat, allow_strum=allow_strum)

    events = []
    if start_mode in ("chord", "fret"):
        cur_mode = str(start_mode)
    elif segs:
        cur_mode = segs[0]["mode"]
    else:
        cur_mode = "chord"
    first_mode = cur_mode
    prev_tail = None
    switches = 0
    z_switches = []
    notes = dropped = strums = plucks = 0
    fret_frames = chord_frames = 0
    used = set()
    start_page, start_sector = 1, 1
    first_sound = True
    est_page, est_sec = 0, None

    for seg in segs:
        want = seg["mode"]
        ev, st = _compile(seg["frames"], want)
        if not ev:
            continue
        if want != cur_mode:
            first = min(ev, key=lambda e: (float(e["t"]), e.get("order", 0)))
            page0 = int(first.get("page") or 0)
            sec0 = int(first.get("sector") or 0)
            steps, _k = page_delta(0, page0)
            need_sector = not (want == "fret" and page0 == PAGE_ZERO)
            need_click = bool(need_sector and (sec0 != est_sec))
            park = t.p8 if first.get("kind") == "strum" else 0.0
            if need_click:
                span = park + t.switch_span(steps, steps > 0, True)
            else:
                span = park + max(t.switch_span(steps, steps > 0, False),
                                  t.p14 + t.p1)
            switch_t = round(float(first["t"]) - span, 6)
            t_down = switch_t - max(t.p1, t.p14 + t.p16) - t.page_key_hold()
            floor = (prev_tail + t.p1) if prev_tail is not None else None
            if floor is None or t_down >= floor - 1e-9:
                events.append({"t": switch_t, "kind": "mode", "mode": want, "order": -1})
                cur_mode = want
                switches += 1
                z_switches.append(round(float(switch_t), 6))
            else:
                ev, st = _compile(seg["frames"], cur_mode)
                if not ev:
                    continue
        if first_sound and ev:
            first = min(ev, key=lambda e: (float(e["t"]), e.get("order", 0)))
            if cur_mode == "chord":
                start_page = int(first.get("page") or 0) + 1
                start_sector = int(first.get("sector") or 0) + 1
            first_sound = False
        events.extend(ev)
        notes += int(st.get("notes") or 0)
        dropped += int(st.get("dropped") or 0)
        strums += int(st.get("strums") or 0)
        plucks += int(st.get("plucks") or 0)
        if cur_mode == "fret":
            fret_frames += len(seg["frames"])
        else:
            chord_frames += len(seg["frames"])
        for e in ev:
            if e.get("chord"):
                used.add(e["chord"])
        tail = 0.0
        for e in sorted(ev, key=lambda x: (float(x["t"]), x.get("order", 0))):
            if e.get("kind") == "strum":
                h = t.p10 if t.p10 > 1e-9 else t.strum_hold_short()
            else:
                h = e.get("hold")
                if h is None:
                    h = _hold_for(e.get("dur") or 0.0, t)
            tail = max(tail, float(e["t"]) + float(h or 0.0))
            ep = int(e.get("page") or 0)
            es = int(e.get("sector") or 0)
            est_page = ep
            if not (cur_mode == "fret" and ep == PAGE_ZERO):
                est_sec = es
        prev_tail = tail if prev_tail is None else max(prev_tail, tail)

    events.sort(key=lambda e: (float(e["t"]), e.get("order", 0)))
    stats = {
        "frames": len(frames),
        "notes": notes,
        "voices": len(events),
        "dropped": dropped,
        "strums": strums,
        "plucks": plucks,
        "switches": switches,
        "chords_used": len(used),
        "pages": sorted({int(e.get("page", 0)) + 1 for e in events if "page" in e}),
        "start_page": start_page,
        "start_sector": start_sector,
        "hybrid": True,
        "fret_frames": fret_frames,
        "chord_frames": chord_frames,
        "start_mode": first_mode,
        "z_switches": z_switches,
        "doubled": _count_doubled(events),
    }
    log("2P 混切：和弦段 %d 帧 / 品格段 %d 帧，Z 切模式 %d 次"
        % (chord_frames, fret_frames, switches))
    return events, stats


def _compile_notes_timeline(song, path, role, notes, markers, t, *, start_offset,
                            onbeat_rule="highest", log=None, want_analysis=False,
                            detail=False):
    """真实编译一段音符 → 时间轴 dict。"""
    log = log or (lambda m: None)
    kind = role_kind(song, role)
    frames = build_frames(notes)
    mode_markers = song.get("mode_markers") or []
    hybrid = bool(mode_markers) and role in (ROLE_DUET_RHYTHM, ROLE_DUET_RHYTHM_FRET)
    start_kind = kind
    if hybrid:
        allow_strum = bool(song.get("chord_strum_mode")) and role == ROLE_DUET_RHYTHM
        sound, cstats = _compile_hybrid_p2(
            frames, markers or [], mode_markers, timing=t, log=log,
            anti_repeat=bool(song.get("anti_repeat", True)),
            allow_strum=allow_strum)
        start_kind = str(cstats.get("start_mode") or kind)
    elif kind == "fret":
        sound, cstats = compile_fret(frames, timing=t, onbeat_rule=onbeat_rule,
                                     strum_chords=bool(song.get("fret_strum")), log=log)
    else:
        sound, cstats = compile_chord(frames, markers or [], timing=t, log=log,
                                      anti_repeat=bool(song.get("anti_repeat", True)),
                                      allow_strum=bool(song.get("chord_strum_mode")))
    if not sound:
        return None
    start_page = int(cstats.get("start_page") or 1)
    hw, hstats = preschedule(sound, start_kind, timing=t, start_page=start_page, log=log,
                             detail=detail)
    if kind == "fret":
        log("换格 %d 次，翻页 %d 步，晚出（late）%d 个音"
            % (cstats.get("switches", 0), hstats.get("turns", 0), hstats.get("late", 0)))
    else:
        log("选格 %d 次，翻页 %d 步，晚出（late）%d 个音"
            % (hstats.get("clicks", 0), hstats.get("turns", 0), hstats.get("late", 0)))

    for e in hw:
        e["t"] = round(float(e["t"]) + start_offset, 6)
    hstats["duration"] = round(float(hstats.get("duration") or 0.0) + start_offset, 3)
    if hw and hw[0]["t"] < 0:
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
            "mode_cn": "品格" if start_kind == "fret" else "和弦",
            "wheel": _wheel_hint(start_kind, start_page),
        },
        "start_mode": start_kind,
        "stats": dict(list(cstats.items()) + list(hstats.items())),
        "compile_stats": cstats,
        "hw_stats": hstats,
        "adapter": song.get("adapter") or {},
        "timing": t.as_dict(),
        "events": hw,
    }
    if want_analysis:
        tl["analysis"] = _analyze_notes(frames, sound, hstats, start_kind,
                                        mode_markers if hybrid else None)
    return tl


def compile_custom_role(song_or_path, role, custom_notes, timing=None, chord_slices=None,
                        log=None, start_offset=None, onbeat_rule="highest",
                        enable_arpeggio=None, enable_chord_rhythm=None,
                        chord_strum_mode=None, mode_markers=None):
    """钢琴卷帘直通重编：跳过 `adapt_song` 的自动重生成，直接编译用户改过的音符。"""
    log = log or (lambda m: None)
    t = timing or Timing()
    if start_offset is None:
        start_offset = float(t.p21)
    if isinstance(song_or_path, (str, bytes, os.PathLike)):
        path = os.fspath(song_or_path)
        song = load_song(path, enable_arpeggio=enable_arpeggio,
                         enable_chord_rhythm=enable_chord_rhythm, timing=t,
                         chord_strum_mode=chord_strum_mode)
    else:
        song = song_or_path or {}
        path = song.get("path") or ""
    kind = role_kind(song, role)

    notes = []
    for n in custom_notes or []:
        try:
            d = {"t": round(max(0.0, float(n["t"])), 6),
                 "dur": round(max(0.001, float(n.get("dur") or 0.25)), 6),
                 "pitch": int(n["pitch"]),
                 "vel": int(n.get("vel") or 80),
                 "track": int(n.get("track") or 0)}
            if n.get("chord"):
                d["chord"] = n["chord"]
            if n.get("src"):
                d["src"] = n["src"]
            notes.append(d)
        except Exception:
            continue

    markers = []
    if kind == "chord":
        p2_mks = (((song.get("p2") or {}).get("markers") or {}).get("chord") or [])
        for s in (chord_slices or song.get("markers") or p2_mks):
            if isinstance(s, dict):
                markers.append((round(float(s.get("t") or 0.0), 6),
                                s.get("name") or s.get("chord")))
            elif isinstance(s, (list, tuple)) and len(s) >= 2:
                markers.append((round(float(s[0]), 6), s[1]))
        markers.sort(key=lambda x: x[0])
    else:
        markers = list(song.get("markers") or [])

    if mode_markers is not None:
        song["mode_markers"] = list(mode_markers)
    elif not song.get("mode_markers"):
        song["mode_markers"] = []
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
                 enable_chord_rhythm=None, detail=False, want_analysis=False,
                 chord_strum_mode=None, max_prune_ratio=0.0):
    """编译一首 .mid（返回可以直接 json.dump 的 dict）。"""
    log = log or (lambda m: None)
    t = timing or Timing()
    if start_offset is None:
        start_offset = float(t.p21)
    song = load_song(path, enable_arpeggio=enable_arpeggio,
                     arpeggio_pattern=arpeggio_pattern,
                     enable_chord_rhythm=enable_chord_rhythm, timing=t,
                     chord_strum_mode=chord_strum_mode)
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
                                       want_analysis=want_analysis,
                                       detail=detail)

    tl = _run(notes)
    if tl is None:
        raise ValueError("编译不出任何发声动作（音符可能全在音域之外）")

    def _late_times_of(tl_obj):
        sd = tl_obj.get("stats", {})
        got = [float(v) for v in (sd.get("late_times") or [])]
        if not got and sd.get("late_detail"):
            got = [float(d.get("t") or 0.0) for d in sd["late_detail"]]
        return got

    trimmed_accomp = 0
    prune_cap = int(max(0.0, min(0.15, float(max_prune_ratio or 0.0)))
                    * max(1, len(notes)))
    if prune_cap > 0 and role in (ROLE_DUET_RHYTHM, ROLE_DUET_RHYTHM_FRET):
        cur_notes = list(notes)
        for _round in range(3):
            if int(tl.get("stats", {}).get("late") or 0) <= 0:
                break
            lts = _late_times_of(tl)
            if not lts:
                break
            tol = max(0.25, float(t.advance_need(1, True, True)))
            trimmed = [n for n in cur_notes if not any(
                abs(float(n.get("t") or 0.0) - lt) <= tol for lt in lts)]
            removed = len(cur_notes) - len(trimmed)
            if removed <= 0 or trimmed_accomp + removed > prune_cap:
                break
            got = _run(trimmed)
            if got is None:
                break
            trimmed_accomp += removed
            cur_notes = trimmed
            tl = got
    if prune_cap > 0 and role == ROLE_SOLO and int(tl.get("stats", {}).get("late") or 0) > 0:
        def _is_melody(n):
            return bool(n.get("is_melody")) or n.get("src") == "melody"

        window = max(0.22, float(t.advance_need(1, True, True)))
        cur_notes = list(notes)
        for _round in range(3):
            if int(tl.get("stats", {}).get("late") or 0) <= 0:
                break
            lts = _late_times_of(tl)
            if not lts:
                break
            trimmed = [n for n in cur_notes if _is_melody(n) or not any(
                abs(float(n.get("t") or 0.0) - lt) <= window for lt in lts)]
            removed = len(cur_notes) - len(trimmed)
            if removed <= 0 or trimmed_accomp + removed > prune_cap:
                break
            got = _run(trimmed)
            if got is None or int(got.get("stats", {}).get("late") or 0) >= int(tl["stats"]["late"]):
                break
            trimmed_accomp += removed
            cur_notes = trimmed
            tl = got
    tl.setdefault("stats", {})["trimmed_accomp"] = trimmed_accomp
    if trimmed_accomp > 0:
        log("💡 提示：当前帧率预设下换格预算较紧，已自动精简 %d 颗密集伴奏音以防吞音/晚出"
            "（若游戏帧数充足，可在下方切换至「60 帧预设」或「90 帧预设」避免裁剪）"
            % trimmed_accomp)
    return tl


def deduce_song_timing(path, role=ROLE_SOLO, max_prune_ratio=None, timing=None):
    """从 33ms 向 11ms 试编译，取第一个 late=0 的最大帧长。"""
    prune = 0.0 if max_prune_ratio is None else max(0.0, min(0.15, float(max_prune_ratio)))
    best = None
    tried = []
    for frame in (33, 28, 24, 20, 16, 14, 11):
        cand = Timing(player_config.build_timing_for_frame_ms(frame))
        try:
            tl = compile_file(path, role=role, timing=cand, max_prune_ratio=prune)
            late = int((tl.get("stats") or {}).get("late") or 0)
        except Exception:
            continue
        tried.append([frame, late])
        if best is None or late < best[1]:
            best = (frame, late)
        if late == 0:
            break
    frame_ms, late = best if best else (33, None)
    fps = max(30, min(90, int(round(1000.0 / frame_ms))))
    return {
        "frame_ms": float(frame_ms),
        "recommend_fps": fps,
        "timing": player_config.build_timing_for_frame_ms(frame_ms),
        "max_prune_ratio": prune,
        "late": late,
        "tried": tried,
    }


def _wheel_hint(kind, page):
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
    """生成给界面/日志看的编译统计文本。"""
    st = tl.get("stats") or {}
    cs = tl.get("compile_stats") or {}
    hw = tl.get("hw_stats") or {}
    lines = []
    for line in midi_adapter.report_lines(tl.get("adapter")):
        lines.append(line)
    lines.append("角色：%s 模式：%s" % (tl.get("role_cn"), tl.get("kind")))
    lines.append("起始：%s模式 第 %d 页 %s"
                 % (tl["start"]["mode_cn"], tl["start"]["page"], tl["start"]["wheel"]))
    if tl.get("kind") == "fret":
        lines.append("音符 %d / %d 帧 发声 %d 提前拨 %d 换格 %d 次 品格 %d~%d"
                     % (st.get("notes", 0), st.get("frames", 0), st.get("voices", 0),
                        st.get("advanced", 0), cs.get("switches", 0),
                        st.get("fret_min", 0), st.get("fret_max", 0)))
    else:
        lines.append("音符 %d / %d 帧 扫弦 %d、单拨 %d 用了 %d 个和弦%s"
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
    if st.get("trimmed_accomp"):
        lines.append("💡 当前预设自动精简伴奏 %d 颗（若游戏帧率稳定，可切换至「60 帧预设」或「90 帧预设」保留完整伴奏）"
                     % int(st["trimmed_accomp"]))
    if hw.get("late"):
        lines.append("⚠ 有 %d 个音为了让换格链完整，被迫晚了（最多 %.0f 毫秒）"
                     % (hw["late"], (hw.get("max_late") or 0) * 1000))
    lines.append("总时长 %.1f 秒" % (hw.get("duration", 0.0)))
    return "\n".join(lines)


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        print("用法: python midi_compiler.py <.mid> "
              "[--role solo|duet_lead|duet_rhythm|duet_rhythm_fret] "
              "[--out 时间轴.json] [--onbeat coverage|highest]")
        return 2
    path = args[0]
    role, out, rule = ROLE_SOLO, None, "highest"
    enable_arpeggio = None
    arpeggio_pattern = None
    i = 1
    while i < len(args):
        if args[i] == "--role" and i + 1 < len(args):
            role = args[i + 1]
            i += 2
        elif args[i] == "--out" and i + 1 < len(args):
            out = args[i + 1]
            i += 2
        elif args[i] == "--onbeat" and i + 1 < len(args):
            rule = args[i + 1]
            i += 2
        elif (args[i] in ("--pattern", "--arpeggio-pattern")) and i + 1 < len(args):
            arpeggio_pattern = args[i + 1]
            i += 2
        elif args[i] == "--no-arpeggio":
            enable_arpeggio = False
            i += 1
        elif args[i] == "--arpeggio":
            enable_arpeggio = True
            i += 1
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