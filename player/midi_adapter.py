# -*- coding: utf-8 -*-
"""midi_adapter.py -- 《异键演奏》v1.1 外部 MIDI → 演奏契约 song 结构转换器。

四件事（全部"物理秒直驱"，只丢事件、绝不改时间戳）：
  ① 分轨八度归位：整轨 -12（存在 >88 且中位数 >72 时）→ 孤立八度跳音 -12
     → 单音循环归位（越界丢弃）；
  ② 多轨合流：没有 SOLO_MASTER / DUET_P1_LEAD 轨名时，把非鼓、非 Chords* 的轨
     按绝对秒合并，主旋律优先；
  ③ 单人 Solo：在 ② 之上再叠"品格伴奏音"（多候选品格同品双音/三音 + 句间短颗粒琶音）；
  ④ 2P 伴奏：从 Chords* 轨切和弦切片（原始 start_sec），生成角色 3（纯拨弦琶音/根音）
     与角色 4（多弦同按柱式抓弦/扫弦）序列及 Z 键混切标记，装进虚拟轨交给编译器；
     编译前断言 1P / 2P 的事件基准 t0 一致（都 = 原始 start_sec）。
"""
import bisect
import os
import re
import statistics

import guitar_plan as GP
import modes                    # 品格页号（`modes.locate("fret", fret)`）的唯一出处

# ── 常量 ──────────────────────────────────────────────────────────────────
MIN_NOTE, MAX_NOTE = 40, 88          # 品格模式可弹音域
SHIFTS = (-24, -12, 0, 12, 24)       # 整轨候选平移（`pick_shift` 老工具，保留）
DRUM_CH = 9
GAP_SLICE = 0.030                    # 和弦切片：起始差 ≤30ms
CHORD_FILTER = {49, 56, 70, 71, 72, 73}   # 缺口音高
CHORD_MIN, CHORD_MAX = 40, 74        # 2P 和弦模式音域
HYBRID_MIN_SEG = 0.80                # 2P 混切：失配段最短时长（够切模式 + 选品格）
HYBRID_GAP_MERGE = 2.00              # 2P 混切：两段之间 < 此时长的和弦段并入混切段（防 Z 键抖动）
SOLO_DOUBLE_GAP = 0.05               # 单人 Solo：同品格双音的最小时间间隔（物理下限）
# 2P 混切：段边界预留"按 Z 切模式 + 翻页点扇区"的时间，防止切模式撞上一段尾音
P2_SWITCH_RESERVE = 0.30

# 下面这些数字只决定丢不丢，绝不参与生成/移动时刻。
CHORD_THROTTLE = 0.45                # 2P 和弦：距上一个保留事件 <0.45s 的切片丢掉
FRET_MIN_GAP = 0.15                  # 2P 品格：相邻保留事件 <0.15s 丢后一个
SOLO_FRAME = 0.015                   # 单人 Solo：≤15ms 算"同一帧"
SOLO_CROSS_GAP = 0.125               # 单人 Solo：跨 1 品双音要求前后 ≥125ms
SOLO_PAGE_GAP = 0.100                # 单人 Solo：同页伴奏音要求前后空隙 ≥100ms
SOLO_TURN_GAP = 0.250                # 单人 Solo：必须翻页的伴奏音前后各 ≥250ms
SOLO_RAISE_MAX = 64                  # 跨页时允许提八度的上限
ISOLATED_JUMP = 10                   # 孤立八度跳音：比前后都高 ≥10 个半音
ISOLATED_DUR = 0.35                  # 且时值 <0.35 秒
WHOLE_SHIFT_MEDIAN = 72              # 整轨降八度：中位数门槛

TRACK_SOLO_MASTER = "SOLO_MASTER"
TRACK_DUET_LEAD = "DUET_P1_LEAD"
TRACK_DUET_RHYTHM = "DUET_P2_RHYTHM"
# v1.1：角色 3（2P 琶音/根音，纯拨弦）的独立轨，另存时一并写出
TRACK_DUET_P2_ARP = "DUET_P2_ARP"

ACCEPT_SCORE = 2.2       # 超过这个分数算"没匹配上"
PAGE_COST = 1.1          # 跨一页的惩罚（同页便宜、跨页贵）
PAGE_COUNT = len(GP.CHORD_PAGES)


# ── 轨名识别 ──────────────────────────────────────────────────────────────
def _tokens(name):
    return [t for t in re.split(r"[^0-9a-z]+", (name or "").lower()) if t]


def is_chords_track(name):
    """Chords1~5 / chords / 和弦 / 伴奏 / accomp 之类。"""
    low = (name or "").lower()
    toks = _tokens(name)
    return (low.startswith("chord") or "chord" in toks or "chords" in toks
            or "和弦" in low or "伴奏" in low
            or any(t in ("accomp", "harmony", "pad", "comp") for t in toks))


def is_melody_track(name):
    """Main Melody / Vocal / Ins / Melody / Lead 之类。"""
    low = (name or "").lower()
    if "melody" in low or "vocal" in low or "lead" in low \
            or "旋律" in low or "主旋律" in low:
        return True
    return "ins" in _tokens(name)


# ── ① 八度归位 ────────────────────────────────────────────────────────────
def pick_shift(pitches):
    """（老工具，保留）选越界音最少的平移；并列时 |shift| 最小；再并列取负。"""
    best = None
    for sh in SHIFTS:
        out = sum(1 for p in pitches if p + sh < MIN_NOTE or p + sh > MAX_NOTE)
        key = (out, abs(sh), sh)
        if best is None or key < best[0]:
            best = (key, sh)
    return best[1]


def normalize_track(notes, melody=False):
    """整轨音域归位（40~88），只归位、不按拍点对齐。"""
    if not notes:
        return [], {"shift": 0, "rescued": 0, "dropped": 0, "isolated": 0,
                    "median": 0, "max": 0, "min": 0, "in_range": True}
    raw = [int(n["pitch"]) for n in notes]
    med = statistics.median(raw)
    top = max(raw)
    low = min(raw)

    # 整轨已经 100% 落在 [40,88] 内 → 原样返回，不做任何移调/孤立滤波
    if low >= MIN_NOTE and top <= MAX_NOTE:
        return ([dict(n) for n in notes],
                {"shift": 0, "rescued": 0, "dropped": 0, "isolated": 0,
                 "min": low, "median": med, "max": top, "in_range": True})

    # ① 整轨降八度
    shift = -12 if (top > MAX_NOTE and med > WHOLE_SHIFT_MEDIAN) else 0
    work = []
    for n in notes:
        d = dict(n)
        d["pitch"] = int(n["pitch"]) + shift
        work.append(d)

    # ② 孤立八度跳音（只看前后紧邻音）
    isolated = 0
    if melody and len(work) >= 3:
        work.sort(key=lambda n: (float(n["t"]), int(n["pitch"])))
        for i in range(1, len(work) - 1):
            cur = work[i]
            if float(cur.get("dur") or 0.0) >= ISOLATED_DUR:
                continue
            p = int(cur["pitch"])
            if (p > MAX_NOTE
                    and p - int(work[i - 1]["pitch"]) >= ISOLATED_JUMP
                    and p - int(work[i + 1]["pitch"]) >= ISOLATED_JUMP):
                cur["pitch"] = p - 12
                isolated += 1
        work.sort(key=lambda n: (float(n["t"]), int(n["pitch"])))

    # ③ 单音循环归位
    keep, rescued, dropped = [], 0, 0
    for d in work:
        p = int(d["pitch"])
        moved = False
        while p > MAX_NOTE:
            p -= 12
            moved = True
        while p < MIN_NOTE:
            p += 12
            moved = True
        if p < MIN_NOTE or p > MAX_NOTE:
            dropped += 1
            continue
        if moved:
            rescued += 1
        d["pitch"] = p
        keep.append(d)
    return keep, {"shift": shift, "rescued": rescued, "dropped": dropped,
                  "isolated": isolated, "min": min(raw), "median": med, "max": top}


# ── ② 多轨合流（只保留主旋律轨）──────────────────────────────────────────
def _merge_notes(by_track, mel_idx):
    merged = []
    for i in mel_idx:
        for n in (by_track.get(i) or []):
            d = dict(n)
            d["src"] = "melody"
            merged.append(d)
    merged.sort(key=lambda n: (float(n["t"]), int(n["pitch"])))
    return merged, {"melody": len(merged)}


# ── ③ 2P 和弦切片 ─────────────────────────────────────────────────────────
def chord_slices(flat_notes, chord_idx):
    """Chords* 轨按绝对秒聚成 [(t, end, {pitch}, [vel])]。"""
    ns = sorted([n for n in flat_notes if n.track_idx in chord_idx],
                key=lambda n: (n.start, n.pitch))
    out = []
    for n in ns:
        if out and (n.start - out[-1]["t"]) <= GAP_SLICE + 1e-9:
            out[-1]["pitches"].add(int(n.pitch))
            out[-1]["end"] = max(out[-1]["end"], n.start + n.length)
            out[-1]["vel"].append(int(n.velocity))
        else:
            out.append({"t": float(n.start), "end": float(n.start + n.length),
                        "pitches": {int(n.pitch)}, "vel": [int(n.velocity)]})
    return out


def _safe_strings(name):
    """单拨安全弦 = 实测单拨音高是本和弦构成音的弦。"""
    pcs = GP.chord_pitch_class_set(name)
    out = []
    for s in GP.chord_strings(name):
        p = GP.pluck_pitch(name, s)
        if p is not None and (p % 12) in pcs:
            out.append(s)
    return out


_LEGAL = None


def legal_chords():
    """24 个法定和弦全部参与匹配。"""
    global _LEGAL
    if _LEGAL is None:
        _LEGAL = []
        for name in GP.ALL_CHORDS:
            safe = _safe_strings(name)
            _LEGAL.append({"name": name, "pcs": GP.chord_pitch_class_set(name),
                           "page": GP.chord_page_sector(name)[0],
                           "safe": safe or list(GP.chord_strings(name))})
    return _LEGAL


def match_chord(pitches, prev=None):
    """音集 → (和弦名, 分数, 是否算匹配上)；带跨页惩罚。"""
    want = {int(p) % 12 for p in pitches}
    prev_page = GP.chord_page_sector(prev)[0] if prev else None
    best = None
    for c in legal_chords():
        steps = 0
        if prev_page is not None:
            steps = min((c["page"] - prev_page) % PAGE_COUNT,
                        (prev_page - c["page"]) % PAGE_COUNT)
        score = (len(want - c["pcs"]) + 0.6 * len(c["pcs"] - want)
                 + PAGE_COST * steps)
        if c["name"] == prev:
            score -= 0.15
        key = (round(score, 6), c["page"], c["name"])
        if best is None or key < best[0]:
            best = (key, c["name"], score)
    if best is None:
        return None, None, False
    return best[1], round(best[2], 4), best[2] <= ACCEPT_SCORE


def _nearest_safe_pitch(name, target):
    """这个和弦的安全弦里，离 target 最近的实测音高。"""
    best = None
    for s in (_safe_strings(name) or list(GP.chord_strings(name))):
        p = GP.pluck_pitch(name, s)
        if p is None:
            continue
        key = (abs(int(p) - int(target)), int(p))
        if best is None or key < best[0]:
            best = (key, int(p))
    return best[1] if best else int(target)


ARP_MIN_NOTE = 45  # A2
ARP_MAX_NOTE = 72  # C5
_BEST_FRET_CACHE = {}


def _bpm_at(tempo_map, t):
    bpm = 120.0
    for sec, val in tempo_map or []:
        if float(sec) <= float(t) + 1e-6:
            bpm = float(val)
        else:
            break
    return bpm if bpm > 0 else 120.0


def _beat_sec_for_slices(slices, tempo_map=None):
    """真实单拍时长：优先 tempo_map；单一 120 时用切片跨度反推。"""
    if tempo_map and not (len(tempo_map) == 1 and abs(float(tempo_map[0][1]) - 120.0) < 1e-3):
        return 60.0 / float(tempo_map[0][1])
    spans = [max(0.05, float(s["end"]) - float(s["t"])) for s in slices]
    if len(spans) < 3:
        return 0.50
    best, best_err = 0.50, None
    cand = 0.38
    while cand <= 0.721:
        err = sum(abs(D / cand - round(D / cand)) for D in spans)
        if best_err is None or err < best_err:
            best, best_err = cand, err
        cand = round(cand + 0.02, 4)
    return best


def _slice_grid(s, nxt, T):
    """返回 (D_grid, N_beats, b)。"""
    start, end = float(s["t"]), float(s["end"])
    D = end - start
    if nxt is not None:
        nstart = float(nxt["t"])
        if 0 < nstart - start <= 4.5 and nstart - end <= T:
            D = nstart - start
    N = max(1, int(round(D / T)))
    b = D / float(N)
    if b < 0.30:
        b *= 2.0
    elif b > 0.85:
        b /= 2.0
    return D, N, b


def build_p2(flat_notes, chord_idx, bpm=None, enable_arpeggio=True, arpeggio_pattern="pingpong",
             tempo_map=None, enable_chord_rhythm=True, timing=None, chord_strum_mode=False):
    """Chords* → 三条 2P 轨道 + 「混切」模式标记（v1.1 四角色架构）。"""
    slices = chord_slices(flat_notes, chord_idx)
    if not slices:
        return None
    if timing is None:
        import midi_compiler as _MC
        timing = _MC.Timing()
    chord_notes, markers, fret_notes, arp_notes = [], [], [], []
    strum_mode = bool(chord_strum_mode)
    stats = {"slices": len(slices), "usable": 0, "empty": 0,
             "fallback": 0, "degrade": 0, "throttle": 0,
             "chord_notes": 0, "fret_kept": 0, "fret_dropped": 0,
             "fret_collide": 0, "chords": {},
             "unmatched": 0, "single": 0, "hybrid_segments": 0,
             "hybrid_notes": 0, "hybrid_slices": 0,
             "arp_notes": 0, "pluck_chord": 0, "fret_stab": 0,
             "strum_mode": strum_mode}
    T = _beat_sec_for_slices(slices, tempo_map)
    prev = None
    last_t = None
    last_ft = None
    POWER = ("C5", "D5", "G5", "A5")

    def _bass_in_range(pc, lo=45, hi=56):
        for p in range(lo, hi + 1):
            if p % 12 == pc:
                return p
        for p in range(lo - 12, hi + 13):
            if p % 12 == pc and 40 <= p <= 68:
                return p
        return None

    def _fret_arp_seq(sl, t0, beat, pcs, bass_pc):
        """品格段（"fret"）的分解琶音序列 `[(时刻, 音高)]`；空 = 配不出低音。"""
        bass = _bass_in_range(bass_pc, 45, 56) or _bass_in_range(bass_pc, 40, 68)
        if bass is None:
            return []
        if bass < 47 and bass + 12 <= 68:
            bass += 12
        open_set = set(modes.STRING_OPEN.values())
        pool = sorted(p for p in range(bass, 69)
                      if (p % 12) in pcs and p not in open_set)
        if len(pool) < 3:
            pool = sorted(set(pool + [p + 12 for p in pool if p + 12 <= 68]))
        if not pool:
            pool = [bass]
        have = {p % 12 for p in pool}
        extra = []
        for pc in sorted(set(pcs) - have):
            for cand in range(max(int(bass), 60), 77):
                if cand % 12 == pc and cand not in open_set:
                    extra.append(cand)
                    break
        cycle = [pool[0]] + sorted(set(extra)) + list(pool[1:]) if extra else pool
        tail_cap = float(sl["end"])
        idx = slices.index(sl)
        if (idx + 1) >= len(slices) or (idx + 1) not in hybrid_idx:
            tail_cap = min(tail_cap, float(sl["end"]) - P2_SWITCH_RESERVE)
        limit = tail_cap - max(0.06, timing.advance_need(0, False, True))
        arp_step = beat / 2.0 if (beat / 2.0) >= 0.16 else beat
        span_avail = float(limit) - t0
        n_pcs = len({p % 12 for p in cycle})
        floor_step = max(0.16, timing.advance_need(0, False, True) + timing.p4)
        if n_pcs > 1 and span_avail > 0:
            need_step = span_avail / float(n_pcs - 1)
            if need_step < arp_step:
                arp_step = max(floor_step, need_step)
        seq, k, tt = [], 0, t0
        while tt <= limit + 1e-9 and k < 32:
            seq.append((tt, cycle[k % len(cycle)]))
            tt += arp_step
            k += 1
        if not seq:
            seq = [(t0, bass)]
        return seq

    prev_name_page = None

    def _pgap(page):
        if prev_name_page is None or page is None:
            return 0
        return abs(int(page) - int(prev_name_page))

    def _pick_name(chord_pcs, root_pc, bass_pc, prev_name=None):
        """音集 → (和弦名, 是否单拨退化)。"""
        # ① 24 和弦精确匹配
        name, _score, ok = match_chord(chord_pcs, prev_name)
        if ok and name:
            return name, False
        # ①-b 四音扩展和弦：某个 24 和弦音集完整包含于 chord_pcs 且含本位根音
        if len(chord_pcs) >= 4:
            covers = [c for c in legal_chords()
                      if not (c["pcs"] - chord_pcs) and root_pc in c["pcs"]]
            if covers:
                covers.sort(key=lambda c: (-len(c["pcs"]),
                                           _pgap(c["page"]),
                                           c["page"], c["name"]))
                return covers[0]["name"], False
        # ② 含本位根音的强力和弦 → 或缺失音最少（<=1）的合法和弦
        for cand in POWER:
            if root_pc in GP.chord_pitch_class_set(cand):
                return cand, False
        cands = [c for c in legal_chords() if root_pc in c["pcs"]]
        if cands:
            near = [c for c in cands if len(chord_pcs - c["pcs"]) <= 1]
            pool = near or cands
            pool.sort(key=lambda c: (len(chord_pcs - c["pcs"]),
                                     len(c["pcs"] - chord_pcs),
                                     _pgap(c["page"]),
                                     c["page"], c["name"]))
            return pool[0]["name"], False
        # ③ 单拨退化兜底：找实测单拨音里真的含 bass_pc / root_pc 的和弦（永不返回 C5）
        best = None
        for c in legal_chords():
            if c["name"] == "C5":
                continue
            safe = c.get("safe") or list(GP.chord_strings(c["name"]))
            pluck_pcs = set()
            for s_i in safe:
                pp = GP.pluck_pitch(c["name"], s_i)
                if pp is not None:
                    pluck_pcs.add(int(pp) % 12)
            has_bass = 0 if int(bass_pc) in pluck_pcs else 1
            has_root = 0 if int(root_pc) in pluck_pcs else 1
            if has_bass and has_root:
                continue
            key = (has_bass, has_root, _pgap(c["page"]),
                   c["page"], c["name"])
            if best is None or key < best[0]:
                best = (key, c["name"])
        if best is not None:
            return best[1], True
        # ④ 极端兜底：含 bass_pc 的任一和弦（依然不返回 C5）
        for c in legal_chords():
            if c["name"] != "C5" and int(bass_pc) in c["pcs"]:
                return c["name"], True
        return legal_chords()[0]["name"], True

    # ── 第一遍：高低音分层 → 音级分离 → 和弦匹配 ────────────────────────
    for i, s in enumerate(slices):
        t = float(s["t"])
        span = max(0.05, float(s["end"]) - t)
        nxt = slices[i + 1] if i + 1 < len(slices) else None
        D_grid, N_beats, b = _slice_grid(s, nxt, T)
        s["beat_sec"] = b
        s["grid_len"] = D_grid
        s["n_beats"] = N_beats
        raw = sorted(int(p) for p in s["pitches"])
        s["raw"] = raw
        if not raw:
            s["pcs"], s["chord_pcs"] = set(), set()
            s["bass_pc"], s["root_pc"] = 0, 0
            s["name"], s["single"], s["unmatched"] = None, True, True
            stats["empty"] += 1
            stats["unmatched"] += 1
            continue
        bass_pc = raw[0] % 12
        upper = [p for p in raw if p >= 48] or raw
        root_pc = upper[0] % 12
        S = {p % 12 for p in raw}
        chord_pcs = {p % 12 for p in upper}
        name, single = _pick_name(chord_pcs, root_pc, bass_pc, prev)
        s["pcs"] = S
        s["chord_pcs"] = chord_pcs
        s["bass_pc"] = bass_pc
        s["root_pc"] = root_pc
        s["name"] = name
        s["raw_label"] = _raw_label(root_pc, chord_pcs, bass_pc, name or "")
        s["single"] = bool(single)
        if single:
            stats["single"] += 1
        if name is None:
            stats["degrade"] += 1
            s["unmatched"] = True
        else:
            stats["usable"] += 1
            prev = name
            prev_name_page = GP.chord_page_sector(name)[0]
            stats["chords"][name] = stats["chords"].get(name, 0) + 1
            s["unmatched"] = bool(
                single or len(chord_pcs - GP.chord_pitch_class_set(name)) >= 2)
        if s["unmatched"]:
            stats["unmatched"] += 1

    # ── 失配段聚合 + 防抖门控 ─────────────────────────────────────────
    runs = []
    i = 0
    while i < len(slices):
        if slices[i].get("unmatched"):
            j = i
            while j + 1 < len(slices) and slices[j + 1].get("unmatched"):
                j += 1
            seg_start = float(slices[i]["t"])
            seg_end = float(slices[j]["end"])
            if (seg_end - seg_start) >= HYBRID_MIN_SEG - 1e-9:
                runs.append((i, j))
            i = j + 1
        else:
            i += 1

    merged = []
    for (a, b) in runs:
        if merged:
            prev_a, prev_b = merged[-1]
            gap_span = float(slices[a]["t"]) - float(slices[prev_b]["end"])
            if 0.0 <= gap_span < HYBRID_GAP_MERGE:
                merged[-1] = (prev_a, b)
                continue
        merged.append((a, b))
    runs = merged

    hybrid_idx = set()
    for (a, b) in runs:
        for k in range(a, b + 1):
            hybrid_idx.add(k)
    mode_markers = []
    for (a, b) in runs:
        mode_markers.append((round(float(slices[a]["t"]), 6), "fret"))
        mode_markers.append((round(float(slices[b]["end"]), 6), "chord"))
    stats["hybrid_segments"] = len(runs)
    stats["hybrid_slices"] = len(hybrid_idx)
    mode_markers.sort(key=lambda x: x[0])

    # ── 第二遍：发声内容生成 ───────────────────────────────────────────
    for i, s in enumerate(slices):
        t = float(s["t"])
        span = max(0.05, float(s["end"]) - t)
        b = s.get("beat_sec") or T
        N_beats = int(s.get("n_beats") or 1)
        D_grid = float(s.get("grid_len") or span)
        vel = int(round(sum(s["vel"]) / float(len(s["vel"])))) if s["vel"] else 80
        raw = s.get("raw") or []
        S = s.get("pcs") or set()
        name = s.get("name")
        single = bool(s.get("single"))
        bass_pc = s.get("bass_pc")
        if bass_pc is None:
            continue

        # 只要切片有匹配和弦名，就在切片起点登记一条和弦标记（确保角色 3 琶音模式在任何律动设置下都有完整和弦标记）
        if name is not None:
            markers.append((round(t, 6), name))

        # ── 混切段（"fret"）：精准品格分解琶音与同品柱式齐拨 ──
        if i in hybrid_idx:
            cap = float(s["end"])
            if (i + 1) >= len(slices) or (i + 1) not in hybrid_idx:
                cap = min(cap, float(s["end"]) - P2_SWITCH_RESERVE)
            beats_h = []
            if enable_chord_rhythm and N_beats >= 2 and D_grid >= 0.90:
                for k in range(N_beats):
                    bt = t + k * b
                    if bt <= cap - 0.12 + 1e-9:
                        beats_h.append(bt)
            if not beats_h:
                beats_h = [t]
            stab_dur = round(min(0.26, max(0.20, b * 0.75)), 4)
            plan = _fret_stab_plan(S, want=len(beats_h))
            if plan:
                for k, bt in enumerate(beats_h):
                    f_stab, stab_ps = plan[k % len(plan)]
                    for p in stab_ps:
                        chord_notes.append({"t": round(bt, 6), "dur": stab_dur,
                                            "pitch": int(p), "vel": 75, "track": -1,
                                            "fret": int(f_stab), "src": "fret_stab",
                                            "pcs": sorted(S), "is_head": (k == 0)})
                    stats["hybrid_notes"] += len(stab_ps)
                    stats["fret_stab"] += 1
            else:
                seq0 = _fret_arp_seq(s, t, b, S, bass_pc)
                if seq0:
                    chord_notes.append({"t": round(seq0[0][0], 6), "dur": stab_dur,
                                        "pitch": int(seq0[0][1]), "vel": vel,
                                        "track": -1, "src": "hybrid_fret",
                                        "pcs": sorted(S), "is_head": True})
                    stats["hybrid_notes"] += 1
            # ── 角色 3：分解琶音（单拨，音高全部来自切片真实音级）──────────
            seq = _fret_arp_seq(s, t, b, S, bass_pc)
            if not seq:
                stats["fret_dropped"] += 1
            elif not enable_arpeggio:
                seq = [(seq[0][0], _bass_in_range(bass_pc, 40, 74) or seq[0][1])]
            arp_dur_h = round(min(0.26, max(0.20, (b / 2.0) * 0.85)), 4)
            for k, (tt, p) in enumerate(seq):
                arp_notes.append({"t": round(tt, 6), "dur": arp_dur_h, "pitch": int(p),
                                  "vel": (min(95, vel + 5) if k == 0 else max(40, vel - 5)),
                                  "track": -1, "src": "hybrid_fret",
                                  "pcs": sorted(S), "is_head": (k == 0)})
                stats["hybrid_notes"] += 1
            continue

        # ── 2P 和弦段：整拍网格 ────────────────────────────────────────
        voice = _pluck_voicing(name, 4)
        stab_dur_c = round(min(0.26, max(0.20, b * 0.75)), 4)
        if name is not None and not single:
            beats = []
            seg_end_cap = float(s["end"])
            if (i + 1) < len(slices) and (i + 1) in hybrid_idx:
                seg_end_cap = min(seg_end_cap, float(slices[i + 1]["t"]) - P2_SWITCH_RESERVE)
            if enable_chord_rhythm and N_beats >= 2 and D_grid >= 0.90:
                for k in range(N_beats):
                    bt = t + k * b
                    if bt <= seg_end_cap - 0.12 + 1e-9:
                        beats.append(bt)
            if not beats:
                beats = [t]
            grid_throttle = min(0.32, b * 0.75) if enable_chord_rhythm else CHORD_THROTTLE
            for k, bt in enumerate(beats):
                if last_t is not None and (bt - last_t) < grid_throttle - 1e-9:
                    stats["throttle"] += 1
                    continue
                last_t = bt
                markers.append((round(bt, 6), name))
                if strum_mode:
                    ps = sorted(p for p in range(40, 75) if p % 12 in S)
                    if not ps:
                        ps = [_nearest_safe_pitch(name, 45 + bass_pc)]
                    ps = ps[:6]
                    for p in ps:
                        chord_notes.append({"t": bt, "dur": min(span, 0.35),
                                            "pitch": int(p), "vel": 75, "track": -1,
                                            "is_head": (k == 0)})
                    stats["chord_notes"] += len(ps)
                else:
                    for (sno, p) in voice:
                        chord_notes.append({"t": bt, "dur": stab_dur_c,
                                            "pitch": int(p), "vel": 75, "track": -1,
                                            "string": int(sno), "src": "pluck_chord",
                                            "is_head": (k == 0)})
                    stats["chord_notes"] += len(voice)
                    stats["pluck_chord"] += 1
        else:
            bp = _bass_in_range(bass_pc, 40, 74) or int(min(raw) if raw else 45)
            if last_t is None or (t - last_t) >= CHORD_THROTTLE - 1e-9:
                last_t = t
                markers.append((round(t, 6), name or "C5"))
                if voice:
                    for (sno, p) in voice:
                        chord_notes.append({"t": t, "dur": stab_dur_c, "pitch": int(p),
                                            "vel": vel, "track": -1,
                                            "string": int(sno), "src": "pluck_chord",
                                            "is_head": True})
                    stats["chord_notes"] += len(voice)
                    stats["pluck_chord"] += 1
                else:
                    chord_notes.append({"t": t, "dur": span, "pitch": int(bp),
                                        "vel": vel, "track": -1})
                    stats["chord_notes"] += 1

        # ── 角色 3：和弦模式「1~6 安全弦单拨」分解琶音（全程 0 鼠标）──────
        if voice:
            seg_end_cap = float(s["end"])
            if (i + 1) < len(slices) and (i + 1) in hybrid_idx:
                seg_end_cap = min(seg_end_cap, float(slices[i + 1]["t"]) - P2_SWITCH_RESERVE)
            limit = seg_end_cap - max(0.06, timing.advance_need(0, False, True))
            arp_step = b / 2.0 if (b / 2.0) >= 0.16 else b
            arp_dur_c = round(min(0.26, max(0.20, arp_step * 0.85)), 4)
            seq = []
            if not enable_arpeggio:
                p_bass = _bass_in_range(bass_pc, 40, 74) or voice[0][1]
                seq = [(t, int(p_bass))]
            else:
                k, tt = 0, t
                while tt <= limit + 1e-9 and k < 32:
                    seq.append((tt, voice[k % len(voice)][1]))
                    tt += arp_step
                    k += 1
                if not seq:
                    seq = [(t, voice[0][1])]
            for k, (tt, p) in enumerate(seq):
                arp_notes.append({"t": round(tt, 6), "dur": arp_dur_c, "pitch": int(p),
                                  "vel": (min(95, vel + 5) if k == 0 else max(40, vel - 5)),
                                  "track": -1, "chord": name, "src": "chord_arp",
                                  "pcs": sorted(S), "is_head": (k == 0)})
                stats["arp_notes"] += 1

        # ── 2P 品格模式（legacy 线，供 Solo 合流与 t0 校验复用）────────
        if enable_arpeggio:
            bass = _bass_in_range(bass_pc, 45, 56)
            if bass is None:
                bass = _bass_in_range(bass_pc, 40, 68)
            if bass is not None and bass < 47 and bass + 12 <= 68:
                bass += 12
            if bass is None:
                stats["fret_dropped"] += 1
                continue
            if span < 0.30:
                step_list = [(t, bass)]
            else:
                open_set = set(modes.STRING_OPEN.values())
                cycle = sorted(p for p in range(bass, 69)
                               if (p % 12) in S and p not in open_set)
                if len(cycle) < 3:
                    extra = [p + 12 for p in cycle if p + 12 <= 68]
                    cycle = sorted(set(cycle + extra))
                if not cycle:
                    cycle = [bass]
                step_list = []
                k = 0
                tt = t
                limit = float(s["end"]) - max(0.06, timing.advance_need(0, False, True))
                arp_step = b / 2.0 if (b / 2.0) >= 0.16 else b
                while tt <= limit + 1e-9 and k < 32:
                    step_list.append((tt, cycle[k % len(cycle)]))
                    tt += arp_step
                    k += 1
                if not step_list:
                    step_list = [(t, bass)]
            for k, (tt, p) in enumerate(step_list):
                if last_ft is not None and (tt - last_ft) < FRET_MIN_GAP - 1e-9:
                    stats["fret_collide"] += 1
                    continue
                last_ft = tt
                fret_notes.append({"t": tt, "dur": 0.20, "pitch": int(p),
                                   "vel": (vel + 5 if k == 0 else max(40, vel - 5)),
                                   "track": -1, "chord": name, "is_head": (k == 0),
                                   "src": "fret_arp", "pcs": sorted(S)})
                stats["fret_kept"] += 1
        else:
            bass = _bass_in_range(bass_pc, 45, 56) or _bass_in_range(bass_pc, 40, 68)
            if bass is None:
                stats["fret_dropped"] += 1
            elif last_ft is not None and (t - last_ft) < FRET_MIN_GAP - 1e-9:
                stats["fret_collide"] += 1
            else:
                last_ft = t
                fret_notes.append({"t": t, "dur": span, "pitch": int(bass),
                                   "vel": vel, "track": -1, "chord": name,
                                   "is_head": True, "src": "fret_root", "pcs": sorted(S)})
                stats["fret_kept"] += 1

    if not chord_notes and not fret_notes and not arp_notes:
        return None
    # 去重并按时间排序 markers
    uniq_markers = sorted(set(markers), key=lambda x: x[0])
    return {
        "notes": {"chord": chord_notes, "fret": fret_notes, "arp": arp_notes},
        "markers": {"chord": uniq_markers, "fret": [], "arp": list(uniq_markers)},
        "stats": stats,
        "slices": slices,
        "mode_markers": mode_markers,
    }


# 音级 → 音名（`raw_label` 用，如 8 → G#）
_PC_NAME = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_QUAL_SUFFIX = {
    (0, 4, 7): "", (0, 3, 7): "m", (0, 3, 6): "dim", (0, 4, 8): "aug",
    (0, 5, 7): "sus4", (0, 2, 7): "sus2",
    (0, 4, 7, 10): "7", (0, 4, 7, 11): "maj7", (0, 3, 7, 10): "m7",
    (0, 3, 7, 11): "mMaj7", (0, 3, 6, 9): "dim7", (0, 3, 6, 10): "m7b5",
    (0, 4, 7, 9): "6", (0, 3, 7, 9): "m6",
    (0, 4, 7, 10, 2): "9", (0, 4, 7, 11, 2): "maj9", (0, 3, 7, 10, 2): "m9",
}


def _raw_label(root_pc, chord_pcs, bass_pc, fallback=""):
    """切片 → 人类可读的原始和弦名（`G#m7` / `A/B` / `Cmaj9`）。"""
    try:
        r = int(root_pc) % 12
        ivs = tuple(sorted({(int(p) - r) % 12 for p in (chord_pcs or set())}))
    except Exception:
        return str(fallback or "")
    suf = _QUAL_SUFFIX.get(ivs)
    if suf is None and bass_pc is not None:
        ivs2 = tuple(sorted({(int(p) - r) % 12 for p in (chord_pcs or set())
                             if int(p) % 12 != int(bass_pc) % 12}))
        if ivs2 in _QUAL_SUFFIX:
            suf = _QUAL_SUFFIX[ivs2]
    if suf is None:
        return str(fallback or (_PC_NAME[r] + "?"))
    txt = _PC_NAME[r] + suf
    if bass_pc is not None and int(bass_pc) % 12 != r:
        txt += "/" + _PC_NAME[int(bass_pc) % 12]
    return txt


def _fret_stab_plan(pcs, want=1, max_n=4, lo=40, hi=88, fret_lo=1, fret_hi=24):
    """品格段柱式抓弦的品格计划：`[(品格, [音高...])] * want`。"""
    cands = []
    for f in range(int(fret_lo), int(fret_hi) + 1):
        ps = []
        for _s, op in GP.STRING_OPEN_NOTES.items():
            p = int(op) + f
            if p < int(lo) or p > int(hi):
                continue
            if (p % 12) in pcs:
                ps.append(int(p))
        if len(ps) < 2:
            continue
        ps = sorted(ps)[:int(max_n)]
        cands.append((int(f), ps, {p % 12 for p in ps}))
    if not cands:
        return []
    chosen = []
    remaining = set(pcs)
    while remaining and len(chosen) < 8:
        best = None
        for (f, ps, cs) in cands:
            gain = len(cs & remaining)
            if gain <= 0:
                continue
            key = (-gain, -len(ps), int(f))
            if best is None or key < best[0]:
                best = (key, f, ps, cs)
        if best is None:
            break
        chosen.append((best[1], best[2]))
        remaining -= best[3]
    if not chosen:
        return []
    n = max(1, int(want))
    return [chosen[k % len(chosen)] for k in range(n)]


def _pluck_voicing(name, max_n=4):
    """和弦的干净弦柱式声部：`[(弦号, 实测单拨音高)]`，按音高升序。"""
    nm = (name or "").strip()
    if not nm:
        return []
    try:
        import midi_compiler as _MC
        pairs = _MC.chord_pluck_strings(nm)
    except Exception:
        pairs = []
    if not pairs:
        return []
    pairs = [(int(s), int(p)) for (s, p) in pairs]
    if len(pairs) <= int(max_n):
        return pairs
    idx = [0]
    rest = list(range(1, len(pairs)))
    take = int(max_n) - 1
    if take > 0 and rest:
        step = len(rest) / float(take)
        for i in range(take):
            idx.append(rest[min(len(rest) - 1, int(round(i * step)))])
    return [pairs[i] for i in sorted(set(idx))][:int(max_n)]


# ── 单人 Solo：主旋律 再叠上"品格伴奏音"（同一物理秒轴）────────────
def _fret_options(pitch, max_fret=24):
    p = int(pitch)
    out = []
    for s, op in GP.STRING_OPEN_NOTES.items():
        f = p - int(op)
        if 0 <= f <= max_fret:
            out.append((f, int(s)))
    return out


def _compiled_note_frets(base, timing=None):
    """用编译器给只有主旋律的线定把位，取出每个音实际用的品格 fret。"""
    try:
        import midi_compiler as MC
        frames = MC.build_frames(base)
        events, _stats = MC.compile_fret(frames, timing=timing or MC.Timing())
    except Exception:
        return {}
    out = {}
    for e in events:
        if "pitch" in e and "fret" in e:
            out[(round(float(e["t"]), 4), int(e["pitch"]))] = int(e["fret"])
    return out


def _sl_active(slices, t):
    """t 时刻正在响的和弦切片（允许 1ms 边界容差）。"""
    if not slices:
        return None
    starts = [float(s["t"]) for s in slices]
    i = bisect.bisect_right(starts, float(t) + 1e-3) - 1
    if 0 <= i < len(slices) and float(slices[i]["t"]) - 1e-3 <= t <= float(slices[i]["end"]) + 0.02:
        return slices[i]
    return None


def _sl_bass(pc, lo=45, hi=56):
    for p in range(lo, hi + 1):
        if p % 12 == pc:
            return p
    for p in range(40, 69):
        if p % 12 == pc:
            return p
    return None


def _sl_fret(pitch):
    opts = _fret_options(pitch)
    return opts[0][0] if opts else None


def _sl_page(pitch):
    f = _sl_fret(pitch)
    if f is None:
        return None
    loc = modes.locate("fret", f)
    return loc[0] if loc else None


def _sl_norm_beat(slice_beat_sec):
    """把任意 BPM 的单拍折算到人类体感的四分音符脉搏 [0.44, 0.88] 秒。"""
    q = float(slice_beat_sec or 0.50)
    if q <= 0.0:
        q = 0.50
    n = 0
    while q < 0.44 and n < 8:
        q *= 2.0
        n += 1
    while q > 0.88 and n < 16:
        q /= 2.0
        n += 1
    return q, q / 2.0


def _sl_same_fret_lows(m_pitch, fret, pcs, lo=45, hi=58, below=None):
    """在固定品格 `fret` 上，找出音级属于 `pcs` 的低音候选（升序）。"""
    out = []
    if fret is None or not pcs:
        return out
    f = int(fret)
    for s in range(1, 7):
        p = int(GP.STRING_OPEN_NOTES[s]) + f
        if p < int(lo) or p > int(hi):
            continue
        if (p % 12) not in pcs:
            continue
        if below is not None and p >= int(below):
            continue
        if p == int(m_pitch):
            continue
        out.append(int(p))
    return sorted(set(out))


def _sl_frame_pitch(frame):
    return max(int(n["pitch"]) for n in frame)


def _sl_frame_dur(frame):
    return max(float(n.get("dur") or 0.0) for n in frame)


FRET_PAGE_COUNT = 5          # 品格模式 5 页循环（第 5 页 = 0 品）


def _fret_page(fret):
    loc = modes.locate("fret", int(fret))
    return loc[0] if loc else None


def _fret_steps(f_prev, f_next):
    pa, pb = _fret_page(f_prev), _fret_page(f_next)
    if pa is None or pb is None:
        return FRET_PAGE_COUNT
    d = (int(pb) - int(pa)) % FRET_PAGE_COUNT
    return min(d, FRET_PAGE_COUNT - d)


def _pick_best_same_fret_lows(m_pitch, f_default, prev_fret, pcs, bass_pc,
                              timing=None, t=None, prev_t=None, lo=45, hi=68,
                              allow_alt=True):
    """在全部候选品格里挑能同品格齐拨出和弦低音的那一个。"""
    if timing is None:
        import midi_compiler as _MC
        timing = _MC.Timing()
    below = int(m_pitch) - 2
    d_lows = _sl_same_fret_lows(m_pitch, f_default, pcs, lo, hi, below=below)
    if d_lows:
        same_page = (prev_fret is None) or (_fret_steps(prev_fret, f_default) == 0)
        has_bass = (bass_pc is not None
                    and any(int(p) % 12 == int(bass_pc) for p in d_lows))
        if has_bass or same_page:
            return f_default, d_lows

    if not allow_alt:
        return f_default, d_lows
    best = None
    steps_default = _fret_steps(prev_fret, f_default) if prev_fret is not None else 0
    for (f_alt, _s_alt) in _fret_options(m_pitch):
        if int(f_alt) == int(f_default):
            continue
        lows = _sl_same_fret_lows(m_pitch, f_alt, pcs, lo, hi, below=below)
        if not lows:
            continue
        steps = _fret_steps(prev_fret, f_alt) if prev_fret is not None else 0
        if steps > steps_default:
            continue
        if prev_t is not None and t is not None:
            need = timing.advance_need(steps, steps > 0, int(f_alt) != 0)
            if (float(t) - float(prev_t)) < need - 1e-9:
                continue
        has_bass = 0 if (bass_pc is not None
                         and any(int(p) % 12 == int(bass_pc) for p in lows)) else 1
        key = (int(steps), has_bass, int(f_alt))
        if best is None or key < best[0]:
            best = (key, int(f_alt), lows)
    if best is not None:
        return best[1], best[2]
    if d_lows:
        return f_default, d_lows
    return f_default, []


def _prune_accomp_to_doublestops(notes, timing, rounds=1):
    """落地校验：只保留真正落成同品格双音的伴奏音。"""
    try:
        import midi_compiler as MC
    except Exception:
        return notes, 0
    cur = list(notes)
    removed_total = 0
    for _ in range(max(0, int(rounds))):
        frames = MC.build_frames(cur)
        if not frames:
            break
        try:
            ev, _st = MC.compile_fret(frames, timing=timing)
            an = MC._analyze_notes(frames, ev, {}, "fret")
        except Exception:
            break
        fret_by = {}
        for a in an.get("notes") or []:
            fret_by[(round(float(a["t"]), 4), int(a["pitch"]))] = a.get("fret")
        drop = set()
        for f in frames:
            tk = round(float(f["t"]), 4)
            mel = [n for n in f["notes"] if n.get("src") == "melody"]
            acc = [n for n in f["notes"] if n.get("src") != "melody"]
            if not mel or not acc:
                continue
            top = max(mel, key=lambda n: int(n["pitch"]))
            m_fret = fret_by.get((tk, int(top["pitch"])))
            if m_fret is None:
                continue
            for n in acc:
                got = fret_by.get((tk, int(n["pitch"])))
                if got is None or int(got) != int(m_fret):
                    drop.add((tk, int(n["pitch"])))
        if not drop:
            break
        new = [n for n in cur
               if (round(float(n["t"]), 4), int(n["pitch"])) not in drop]
        if len(new) == len(cur):
            break
        removed_total += len(cur) - len(new)
        cur = new
    return cur, removed_total


def merge_solo_accomp(merged, accomp, enable_arpeggio=True, arpeggio_pattern="pingpong", slices=None,
                      timing=None):
    """通用「主旋律锚定 + 归一化恒定脉搏」Solo 伴奏合流。"""
    base = sorted(merged, key=lambda n: (float(n["t"]), int(n["pitch"])))
    stats = {"candidates": len(accomp or []), "kept": 0, "same_fret": 0,
             "cross_fret": 0, "root_gap": 0, "bass_doublestop": 0,
             "gap_arpeggio": 0, "drop_dense": 0, "drop_budget": 0,
             "drop_no_anchor": 0, "drop_dup": 0}
    if not base or not slices:
        return base, stats
    if timing is None:
        import midi_compiler as _MC
        timing = _MC.Timing()

    frets = _compiled_note_frets(base, timing)

    frames = []
    for n in base:
        if not frames or abs(float(n["t"]) - float(frames[-1][0]["t"])) > 0.015:
            frames.append([n])
        else:
            frames[-1].append(n)
    starts = [float(fr[0]["t"]) for fr in frames]
    mel_pitches = {round(float(n["t"]), 6) for n in base}

    def f_of(n):
        key = (round(float(n["t"]), 4), int(n["pitch"]))
        if key in frets:
            return frets[key]
        return _sl_fret(n["pitch"])

    kept = []
    last_acc = [None]
    prev_fret_state = [None]

    def spaced(t, q):
        return last_acc[0] is None or (float(t) - float(last_acc[0])) >= q * 0.85 - 1e-9

    def _choose_fret(m_pitch, f_default, sl, t, prev_t, allow_alt=True):
        return _pick_best_same_fret_lows(
            m_pitch, f_default, prev_fret_state[0],
            set(sl.get("pcs") or set()), sl.get("bass_pc"),
            timing=timing, t=t, prev_t=prev_t, lo=45, hi=68,
            allow_alt=allow_alt)

    if enable_arpeggio:
        for i, fr in enumerate(frames):
            t = float(fr[0]["t"])
            m_pitch = _sl_frame_pitch(fr)
            m_dur = _sl_frame_dur(fr)
            mnote = max(fr, key=lambda n: int(n["pitch"]))
            sl = _sl_active(slices, t)
            if sl is None:
                stats["drop_no_anchor"] += 1
                continue
            S = set(sl.get("pcs") or set())
            if not S:
                stats["drop_no_anchor"] += 1
                continue
            q, step = _sl_norm_beat(sl.get("beat_sec"))
            f_m = f_of(mnote)
            if i + 1 < len(frames):
                t_next = float(starts[i + 1])
            else:
                t_next = t + 4.0 * step
            gap = t_next - t

            if gap < 1.75 * step:
                if last_acc[0] is not None and (t - float(last_acc[0])) < SOLO_DOUBLE_GAP - 1e-9:
                    stats["drop_dense"] += 1
                    continue
                f_pick, cands = _choose_fret(m_pitch, f_m, sl, t,
                                             (float(starts[i - 1]) if i > 0 else None))
                if not cands:
                    stats["drop_dense"] += 1
                    continue
                bp = max(cands)
                kept.append({"t": t, "dur": m_dur, "pitch": int(bp),
                             "vel": max(45, min(95, int(mnote.get("vel") or 70))),
                             "track": -1, "fret": int(f_pick), "src": "same_fret_low",
                             "pcs": sorted(S)})
                if len(cands) >= 2 and int(cands[0]) != int(bp) and (
                        abs(t - float(sl["t"])) <= 0.06 or (t - float(last_acc[0] or -9.0)) >= q * 1.4):
                    kept.append({"t": t, "dur": m_dur, "pitch": int(cands[0]),
                                 "vel": max(48, min(92, int(mnote.get("vel") or 72))),
                                 "track": -1, "fret": int(f_pick), "src": "same_fret_low",
                                 "pcs": sorted(S)})
                    stats["kept"] += 1
                prev_fret_state[0] = int(f_pick)
                stats["bass_doublestop"] += 1
                stats["kept"] += 1
                last_acc[0] = t
                continue

            # ── 长延音 / 句间停顿：t_i 同刻垫低音 + 恒定 arp_step 顺拨 ──
            t_limit = t_next - max(0.75 * step, timing.advance_need(1, True, True) + 0.03)
            f_pick, cands = _choose_fret(m_pitch, f_m, sl, t,
                                         (float(starts[i - 1]) if i > 0 else None))
            if cands and spaced(t, q):
                bp = max(cands)
                bass_dur = round(min(m_dur, step * 0.85, 0.25), 4)
                kept.append({"t": t, "dur": bass_dur, "pitch": int(bp),
                             "vel": max(45, min(95, int(mnote.get("vel") or 70))),
                             "track": -1, "fret": int(f_pick), "src": "same_fret_low",
                             "pcs": sorted(S)})
                prev_fret_state[0] = int(f_pick)
                stats["bass_doublestop"] += 1
                stats["kept"] += 1
                last_acc[0] = t
            k = 1
            while k <= 64:
                tk = round(t + k * step, 6)
                if tk > t_limit + 1e-9:
                    break
                if round(tk, 6) in mel_pitches:
                    k += 1
                    continue
                sl_k = _sl_active(slices, tk) or sl
                S_k = set(sl_k.get("pcs") or set()) or S
                hi = min(68, m_pitch - 2)
                raw_pool = [p for p in range(45, hi + 1) if (p % 12) in S_k]
                b_pc = sl_k.get("bass_pc")
                if b_pc is not None and raw_pool:
                    roots = [p for p in raw_pool if (p % 12) == int(b_pc)]
                    others = [p for p in raw_pool if (p % 12) != int(b_pc)]
                    cycle = (roots[:1] + others + roots[1:]) if roots else raw_pool
                else:
                    cycle = raw_pool
                if cycle:
                    p = cycle[(k - 1) % len(cycle)]
                    dur = round(min(step * 0.70, 0.22), 4)
                    if dur >= 0.02:
                        kept.append({"t": tk, "dur": dur, "pitch": int(p),
                                     "vel": 58, "track": -1, "src": "gap_arpeggio",
                                     "pcs": sorted(S_k)})
                        stats["gap_arpeggio"] += 1
                        stats["kept"] += 1
                        last_acc[0] = tk
                k += 1
    else:
        # ── 根音模式：每个和弦切片一颗饱满低音 ──
        for sl in slices:
            t_sl = float(sl["t"])
            e_sl = float(sl["end"])
            S = set(sl.get("pcs") or set())
            pc = sl.get("bass_pc")
            if pc is None or not S:
                stats["drop_no_anchor"] += 1
                continue
            q, step = _sl_norm_beat(sl.get("beat_sec"))
            p_bass = _sl_bass(int(pc), 45, 56)
            if p_bass is None:
                stats["drop_no_anchor"] += 1
                continue
            lo_i = bisect.bisect_left(starts, t_sl - 1e-3)
            hi_i = bisect.bisect_right(starts, e_sl + 1e-6)
            anchor = None
            for j in range(lo_i, hi_i):
                t = float(starts[j])
                if t > e_sl - 0.02:
                    continue
                if not spaced(t, q):
                    continue
                fr = frames[j]
                m_pitch = _sl_frame_pitch(fr)
                m_dur = _sl_frame_dur(fr)
                mnote = max(fr, key=lambda n: int(n["pitch"]))
                f_m = f_of(mnote)
                cands = _sl_same_fret_lows(m_pitch, f_m, S, 45, 58)
                if cands:
                    bp = p_bass if p_bass in cands else min(
                        cands, key=lambda p: (abs(p - p_bass), -p))
                    if int(bp) != int(m_pitch):
                        anchor = {"t": t, "dur": m_dur, "pitch": int(bp),
                                  "vel": 62, "src": "root_same_fret",
                                  "fret": int(f_m), "pcs": sorted(S)}
                        stats["same_fret"] += 1
                        break
                d_prev = (t - starts[j - 1]) if j > 0 else 9.9
                root_gap = max(0.18, timing.advance_need(1, True, True) * 1.5 + timing.p4)
                if d_prev >= root_gap - 1e-9 and int(p_bass) != int(m_pitch):
                    anchor = {"t": t, "dur": m_dur, "pitch": int(p_bass),
                              "vel": 62, "src": "root_anchor", "pcs": sorted(S)}
                    stats["cross_fret"] += 1
                    break
            if anchor is None:
                k = bisect.bisect_right(starts, t_sl)
                d_prev = (t_sl - starts[k - 1]) if k > 0 else 9.9
                d_next = (starts[k] - t_sl) if k < len(starts) else 9.9
                need = max(0.22, 0.75 * step)
                if (d_prev >= need - 1e-9 and d_next >= need - 1e-9
                        and spaced(t_sl, q)):
                    dur = min(d_next - 0.025, e_sl - t_sl) if k < len(starts) else (e_sl - t_sl)
                    if dur >= 0.025:
                        anchor = {"t": t_sl, "dur": round(dur, 6),
                                  "pitch": int(p_bass), "vel": 60,
                                  "src": "root_gap", "pcs": sorted(S)}
                        stats["root_gap"] += 1
            if anchor is not None:
                kept.append(anchor)
                stats["kept"] += 1
                last_acc[0] = anchor["t"]

    if not kept:
        return base, stats
    seen = set()
    uniq = []
    for n in kept:
        key = (round(float(n["t"]), 6), int(n["pitch"]))
        if key in seen:
            stats["drop_dup"] += 1
            continue
        seen.add(key)
        uniq.append(n)
    stats["kept"] = len(uniq)
    out = list(base) + uniq
    out.sort(key=lambda n: (float(n["t"]), int(n["pitch"])))
    out, pruned = _prune_accomp_to_doublestops(out, timing, rounds=2)
    stats["pruned_no_doublestop"] = pruned
    return out, stats


# ── 入口：把适配结果写回 song 结构 ────────────────────────────────────────
def _add_track(song, name, notes):
    """在 song 末尾挂一条虚拟轨，返回轨号。"""
    idx = len(song["track_names"])
    song["track_names"].append(name)
    out = []
    for n in sorted(notes, key=lambda x: (x["t"], x["pitch"])):
        d = dict(n)
        d["track"] = idx
        out.append(d)
    song["notes_by_track"][idx] = out
    song["track_index"][name] = idx
    return idx


def _audit_t0(song, flat_notes, info, tol=1e-3):
    """编译前断言：1P 与 2P 的事件基准 `t0` 一致（都直接落在原始 start_sec 上）。"""
    raw = sorted({round(float(n.start), 6) for n in flat_notes})
    if not raw:
        return

    def worst(notes):
        w = 0.0
        for n in notes or []:
            t = round(float(n["t"]), 6)
            k = bisect.bisect_left(raw, t)
            best = abs(raw[k] - t) if k < len(raw) else 1e9
            if k > 0:
                best = min(best, abs(raw[k - 1] - t))
            if best > w:
                w = best
        return w

    ti = song.get("track_index") or {}
    lead = song["notes_by_track"].get(ti.get(TRACK_DUET_LEAD), [])
    solo = song["notes_by_track"].get(ti.get(TRACK_SOLO_MASTER), [])
    p2 = song.get("p2") or {}
    p2_notes = p2.get("notes") or {}
    solo_anchors = [n for n in solo if n.get("src") in ("melody", "bass_doublestop")]
    fret_anchors = [n for n in (p2_notes.get("fret") or []) if n.get("is_head", True)]
    chord_anchors = [n for n in (p2_notes.get("chord") or []) if n.get("is_head", True)]
    d1 = max(worst(lead), worst(solo_anchors if solo_anchors else solo))
    d2 = max(worst(chord_anchors), worst(fret_anchors if fret_anchors else p2_notes.get("fret")))
    info["t0"] = {"lead": round(d1, 6), "p2": round(d2, 6),
                  "lead_n": len(lead) + len(solo),
                  "p2_n": len(p2_notes.get("chord") or []) + len(p2_notes.get("fret") or [])}
    if d1 > tol or d2 > tol:
        raise AssertionError(
            "1P/2P 事件基准 t0 不一致：1P 最大偏差 %.6fs，2P 最大偏差 %.6fs"
            "（允许 %.0fms）" % (d1, d2, tol * 1000))


def adapt_song(song, flat_notes, enable_arpeggio=True, arpeggio_pattern=None,
               enable_chord_rhythm=None, timing=None, force_recalc=False,
               chord_strum_mode=None):
    """读入后的统一适配：八度归位 → 合流（含 Solo 伴奏）→ 2P 虚拟轨 → t0 断言。"""
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
    if chord_strum_mode is None:
        chord_strum_mode = song.get("chord_strum_mode")
        if chord_strum_mode is None:
            try:
                import player_config as PC
                chord_strum_mode = bool(PC.load().get("chord_strum_mode", False))
            except Exception:
                chord_strum_mode = False
    song["chord_strum_mode"] = bool(chord_strum_mode)
    info = {"shifts": [], "merged": None, "p2": None, "t0": None, "chords": None}
    names = song.get("track_names") or []
    by_track = song.get("notes_by_track") or {}

    chans = {}
    for n in flat_notes:
        chans.setdefault(n.track_idx, set()).add(n.channel)

    exact_names = {(n or "").strip().upper() for n in names}
    trust_roles = (not force_recalc) and all(
        k in exact_names for k in (TRACK_SOLO_MASTER, TRACK_DUET_LEAD, TRACK_DUET_RHYTHM))

    chord_idx, mel_idx = set(), []
    for idx in sorted(by_track):
        name = names[idx] if idx < len(names) else ""
        if DRUM_CH in chans.get(idx, ()):
            continue
        if is_chords_track(name):
            chord_idx.add(idx)
            continue
        if is_melody_track(name):
            mel_idx.append(idx)

    if force_recalc:
        role_tracks = (TRACK_SOLO_MASTER, TRACK_DUET_LEAD, TRACK_DUET_RHYTHM, TRACK_DUET_P2_ARP)
        mel_idx = [i for i in mel_idx
                   if (names[i] if i < len(names) else "").strip().upper()
                   not in role_tracks]

    chord_flat = flat_notes
    audit_notes = flat_notes
    chord_src = {"file": os.path.basename(song.get("path") or ""),
                 "path": song.get("path") or "",
                 "tracks": len(chord_idx), "fallback": False, "names": [],
                 "no_chord_track": not chord_idx}
    info["chords"] = chord_src
    info["no_chord_track"] = not chord_idx

    # ① 八度归位
    for idx in ([] if trust_roles else mel_idx):
        keep, st = normalize_track(by_track.get(idx) or [], melody=True)
        by_track[idx] = keep
        st["name"] = names[idx] if idx < len(names) else ("轨%d" % idx)
        st["track"] = idx
        info["shifts"].append(st)

    # ② 先算 2P
    exact = exact_names
    p2 = None
    if chord_idx and (force_recalc or TRACK_DUET_RHYTHM not in exact) and not (song.get("markers") or []):
        p2 = build_p2(chord_flat, chord_idx, enable_arpeggio=enable_arpeggio,
                      arpeggio_pattern=arpeggio_pattern,
                      tempo_map=song.get("tempo_map"),
                      enable_chord_rhythm=enable_chord_rhythm,
                      timing=timing, chord_strum_mode=bool(song.get("chord_strum_mode")))
    elif trust_roles and chord_idx and not (song.get("markers") or []):
        built = build_p2(chord_flat, chord_idx, enable_arpeggio=enable_arpeggio,
                         arpeggio_pattern=arpeggio_pattern,
                         tempo_map=song.get("tempo_map"),
                         enable_chord_rhythm=enable_chord_rhythm,
                         timing=timing, chord_strum_mode=bool(song.get("chord_strum_mode")))
        if built:
            stored = by_track.get((song.get("track_index") or {}).get(TRACK_DUET_RHYTHM))
            if stored:
                built["notes"]["chord"] = [dict(n) for n in stored]
            arp_idx = None
            for _i, _nm in enumerate(names):
                if str(_nm or "").strip().upper() == TRACK_DUET_P2_ARP:
                    arp_idx = _i
                    break
            if arp_idx is not None and by_track.get(arp_idx):
                built["notes"]["arp"] = [dict(n) for n in by_track[arp_idx]]
            p2 = built

    # ③ 合流
    legacy = (not force_recalc) and ((TRACK_SOLO_MASTER in exact)
                                    or (TRACK_DUET_LEAD in exact))
    if not legacy and mel_idx:
        merged, mst = _merge_notes(by_track, mel_idx)
        if merged:
            solo = merged
            if p2 and p2["notes"].get("fret"):
                solo, sst = merge_solo_accomp(merged, p2["notes"]["fret"],
                                              enable_arpeggio=enable_arpeggio,
                                              arpeggio_pattern=arpeggio_pattern,
                                              slices=p2.get("slices"),
                                              timing=timing)
                mst["solo"] = sst
            _add_track(song, TRACK_SOLO_MASTER, solo)
            _add_track(song, TRACK_DUET_LEAD, merged)
            info["merged"] = mst
            info["merged"]["tracks"] = [names[i] if i < len(names) else str(i)
                                        for i in mel_idx]

    # ④ 挂 2P 虚拟轨
    if p2:
        stored_idx = (song.get("track_index") or {}).get(TRACK_DUET_RHYTHM)
        if trust_roles and stored_idx is not None:
            idx = stored_idx
        else:
            idx = _add_track(song, TRACK_DUET_RHYTHM, p2["notes"]["chord"])
        p2["track"] = idx
        if not (trust_roles and stored_idx is not None):
            p2["notes"]["chord"] = song["notes_by_track"][idx]
        for role_k in ("chord", "fret", "arp"):
            fixed = []
            for n in (p2["notes"].get(role_k) or []):
                d = dict(n)
                d["track"] = idx
                fixed.append(d)
            p2["notes"][role_k] = fixed
        song["p2"] = p2
        info["p2"] = p2["stats"]
        if force_recalc or not song.get("mode_markers"):
            song["mode_markers"] = list(p2.get("mode_markers") or [])
        info["mode_markers"] = list(song.get("mode_markers") or [])

    # ⑤ 编译前断言：1P / 2P 的事件基准 t0 必须一致
    _audit_t0(song, audit_notes, info)

    if info["merged"] or info["p2"]:
        if not song.get("fret_strum_explicit"):
            song["fret_strum"] = False
        song["adapter_merged"] = True

    song["adapter"] = info
    return info


# ── 报告 ──────────────────────────────────────────────────────────────────
def report_lines(info):
    """给 `report_text` 用的摘要行。"""
    info = info or {}
    out = []
    shifts = info.get("shifts") or []
    moved = ["%s %+d" % (s["name"], s["shift"]) for s in shifts if s.get("shift")]
    still = [s["name"] for s in shifts if not s.get("shift")]
    if moved:
        out.append("八度归位：%s（未动：%s）"
                   % ("、".join(moved), "、".join(still) if still else "无"))
    res = sum(s.get("rescued") or 0 for s in shifts)
    drop = sum(s.get("dropped") or 0 for s in shifts)
    iso = sum(s.get("isolated") or 0 for s in shifts)
    if res or drop or iso:
        out.append("八度兜底：单音循环归位 %d 个，孤立八度跳音 -12 %d 个，越界丢弃 %d 个"
                   % (res, iso, drop))
    m = info.get("merged")
    if m:
        out.append("合流：主旋律 %d 音（只取主旋律轨，装饰音轨忽略）" % m["melody"])
        s = m.get("solo")
        if s:
            out.append("单人 Solo 伴奏：候选 %d，保留 %d（同格根音 %d / 跨格齐拨 %d / "
                       "空隙补根 %d / 同格低音 %d / 恒定脉搏琶音 %d），让步丢弃 %d"
                       "（唱段密集 %d，空隙不足 %d，无锚点 %d）"
                       % (s.get("candidates", 0), s.get("kept", 0),
                          s.get("same_fret", 0), s.get("cross_fret", 0),
                          s.get("root_gap", 0), s.get("bass_doublestop", 0),
                          s.get("gap_arpeggio", 0),
                          s.get("drop_dense", 0) + s.get("drop_budget", 0)
                          + s.get("drop_no_anchor", 0),
                          s.get("drop_dense", 0), s.get("drop_budget", 0),
                          s.get("drop_no_anchor", 0)))
    c = info.get("chords")
    if c:
        where = ("同目录兜底 %s" % c.get("file")) if c.get("fallback") \
            else ("本文件 %s" % c.get("file"))
        out.append("和弦轨：%d 条，来自 %s%s"
                   % (c.get("tracks", 0), where,
                      "（%s）" % c["error"] if c.get("error") else ""))
    p = info.get("p2")
    if p:
        out.append("2P 切片 %d → 过滤后可用 %d（空 %d）；和弦：回退 %d / 退化 %d / "
                   "节流丢弃 %d，发声 %d；品格：保留 %d / 太挤 %d / 越界 %d"
                   % (p["slices"], p["usable"], p["empty"], p["fallback"],
                      p["degrade"], p["throttle"], p["chord_notes"],
                      p["fret_kept"], p["fret_collide"], p["fret_dropped"]))
    t0 = info.get("t0")
    if t0:
        out.append("事件基准 t0（原始秒）：1P 偏差 %.4fms / 2P 偏差 %.4fms → %s"
                   % (t0["lead"] * 1000.0, t0["p2"] * 1000.0,
                      "一致" if max(t0["lead"], t0["p2"]) < 1e-3 else "不一致"))
    return out