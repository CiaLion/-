# -*- coding: utf-8 -*-
"""midi_adapter.py -- 外部 MIDI → 旧契约 song 结构（只转换，不做编译）。

四件事（★ 2026-10-18 起全部"物理秒直驱"，只丢事件、绝不改时间戳）：
  ① 分轨八度归位：整轨 -12（存在 >88 且中位数 >72 时）→ 孤立八度跳音 -12
     → 单音循环归位（越界丢弃）；
  ② 多轨合流：没有 SOLO_MASTER / DUET_P1_LEAD 轨名时，把非鼓、非 Chords* 的轨
     按绝对秒合并，主旋律优先，装饰音挡路就丢；
  ③ 单人 Solo：在 ② 之上再叠"品格伴奏音"（同一帧同品格 / 跨 1 品的双音规则）；
  ④ 2P 伴奏：从 Chords* 轨切和弦切片（原始 start_sec），生成「和弦 / 品格」两套
     序列，装进 DUET_P2_RHYTHM 虚拟轨，交给 compile_chord / compile_fret；
     编译前还会断言 1P / 2P 的事件基准 t0 一致（都 = 原始 start_sec）。
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

#   下面这些数字只决定**丢不丢**，绝不参与生成/移动时刻。
CHORD_THROTTLE = 0.45                # 2P 和弦：距上一个保留事件 <0.45s 的切片丢掉（0.5s 律动可用）
FRET_MIN_GAP = 0.15                  # 2P 品格：相邻保留事件 <0.15s 丢后一个
SOLO_FRAME = 0.015                   # 单人 Solo：≤15ms 算"同一帧"
SOLO_CROSS_GAP = 0.125               # 单人 Solo：跨 1 品双音要求前后 ≥125ms
SOLO_PAGE_GAP = 0.100                # 单人 Solo：同页伴奏音要求前后空隙 ≥100ms
SOLO_TURN_GAP = 0.250                # 单人 Solo：必须翻页的伴奏音前后各 ≥250ms
SOLO_RAISE_MAX = 64                  # 跨页时允许提八度的上限（再高就不提）
ISOLATED_JUMP = 10                   # 孤立八度跳音：比前后都高 ≥10 个半音
ISOLATED_DUR = 0.35                  # 且时值 <0.35 秒
WHOLE_SHIFT_MEDIAN = 72              # 整轨降八度：中位数门槛

TRACK_SOLO_MASTER = "SOLO_MASTER"
TRACK_DUET_LEAD = "DUET_P1_LEAD"
TRACK_DUET_RHYTHM = "DUET_P2_RHYTHM"

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
    """（老工具，保留）选越界音最少的平移；并列时 |shift| 最小；再并列取负。

    ★ 2026-10-18 起 `normalize_track` 不再用它——整轨平移改成"存在 >88 且中位数
      >72 才整轨 -12"的明确规则。留着是给外部脚本/老数据兜底用。
    """
    best = None
    for sh in SHIFTS:
        out = sum(1 for p in pitches if p + sh < MIN_NOTE or p + sh > MAX_NOTE)
        key = (out, abs(sh), sh)
        if best is None or key < best[0]:
            best = (key, sh)
    return best[1]

def normalize_track(notes, melody=False):
    """整轨音域归位（40~88），**只归位、不按拍点对齐**。

    规则（2026-10-18 替换旧规则）：
      ① 整轨降八度：存在 >88 的音 **且** 音高中位数 >72 → 整轨 -12；
      ② 孤立八度跳音滤波（只对主旋律/器乐轨）：比前后紧邻音都高 ≥10 个半音、
         且时值 <0.35 秒 → 该音 -12（倍频噪点，比如突兀的 A6=93）；
      ③ 单音循环归位：while >88 → -12；while <40 → +12；
         循环完仍不在 40~88 → 丢弃并计数。

    返回 `(音符, 统计)`；统计里 `median/max` 是**原始**轨的中位数/最高音，
    方便报告里核对"为什么降/没降"。
    """
    if not notes:
        return [], {"shift": 0, "rescued": 0, "dropped": 0, "isolated": 0,
                    "median": 0, "max": 0, "min": 0, "in_range": True}
    raw = [int(n["pitch"]) for n in notes]
    med = statistics.median(raw)
    top = max(raw)
    low = min(raw)

    # ★ 治本：整轨已经 100% 落在 [40,88] 内 → 原样返回，不做任何移调/孤立滤波
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

    # ② 孤立八度跳音（只看"前后紧邻音"，所以先按时间排好）
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

# ── ② 多轨合流（只保留主旋律轨；装饰音历史遗留一律忽略）──────────────
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
    """单拨安全弦 = 实测单拨音高**是本和弦构成音**的弦。

    C5 这种和弦实测单拨全是 F/B/E，一根和弦音都拨不出来 → 0 根 → 排除。
    """
    pcs = GP.chord_pitch_class_set(name)
    out = []
    for s in GP.chord_strings(name):
        p = GP.pluck_pitch(name, s)
        if p is not None and (p % 12) in pcs:
            out.append(s)
    return out

_LEGAL = None

def legal_chords():
    """24 个法定和弦**全部**参与匹配（不再因单拨安全弦不足而剔除）。

    ★ 单拨安全弦（`_safe_strings`）只用于给「不足 3 音的切片」挑一根最近的弦；
      C5 / Cadd9 的单拨实测音本来就偏离指型，但扫弦（≥3 音）是正常的，
      需求要求 24 个和弦都能匹配。单拨撞音仍由
      `guitar_plan.pluck_safe_strings` 的熔断规则兜底（不足 3 根就回退不熔断）。
    """
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
            score -= 0.15                      # 轻微粘住上一个，避免来回跳
        key = (round(score, 6), c["page"], c["name"])
        if best is None or key < best[0]:
            best = (key, c["name"], score)
    if best is None:
        return None, None, False
    return best[1], round(best[2], 4), best[2] <= ACCEPT_SCORE

def _nearest_safe_pitch(name, target):
    """这个和弦的安全弦里，离 target 最近的实测音高。

    没有安全弦时（C5 / Cadd9 这类）回退到全部弦，绝不因为熔断把和弦弄哑。
    """
    best = None
    for s in (_safe_strings(name) or list(GP.chord_strings(name))):
        p = GP.pluck_pitch(name, s)
        if p is None:
            continue
        key = (abs(int(p) - int(target)), int(p))
        if best is None or key < best[0]:
            best = (key, int(p))
    return best[1] if best else int(target)

ARP_MIN_NOTE = 45  # A2 (避开 40~44 极低音发闷轰头)
ARP_MAX_NOTE = 72  # C5 (避开 >76 高把位衰减)
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
             tempo_map=None, enable_chord_rhythm=True, timing=None):
    """Chords* → 「和弦模式」块 + 「品格模式」单音线/琶音线。

    ★ 和弦模式：用切片**完整音级** S 匹配 24 和弦；长切片按 0.5s 扫弦律动。
    ★ `enable_chord_rhythm=False`：不做整拍细分，每个切片起点只扫 1 次。
    ★ 品格模式：琶音开 = 严格从低到高顺拨第 1 页舒适区（45~68）；
                 琶音关 = 每个切片一颗 [45,56] 根音。
    """
    slices = chord_slices(flat_notes, chord_idx)
    if not slices:
        return None
    if timing is None:
        import midi_compiler as _MC
        timing = _MC.Timing()
    chord_notes, markers, fret_notes = [], [], []
    stats = {"slices": len(slices), "usable": 0, "empty": 0,
             "fallback": 0, "degrade": 0, "throttle": 0,
             "chord_notes": 0, "fret_kept": 0, "fret_dropped": 0,
             "fret_collide": 0, "chords": {}}
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

    def _pick_name(S, bass_pc):
        name, _score, ok = match_chord(S, prev)
        if ok and name:
            return name, False
        for cand in POWER:                       # 优先含低音音级的强力和弦
            if bass_pc in GP.chord_pitch_class_set(cand):
                return cand, False
        cands = [c for c in legal_chords() if bass_pc in c["pcs"]]
        if cands:
            cands.sort(key=lambda c: (len(S - c["pcs"]), c["page"]))
            return cands[0]["name"], False
        return "C5", True                        # 退化单拨根音（用强力和弦占位，避免空名丢帧）

    for i, s in enumerate(slices):
        t = float(s["t"])
        span = max(0.05, float(s["end"]) - t)
        nxt = slices[i + 1] if i + 1 < len(slices) else None
        D_grid, N_beats, b = _slice_grid(s, nxt, T)
        s["beat_sec"] = b
        s["grid_len"] = D_grid
        s["n_beats"] = N_beats
        vel = int(round(sum(s["vel"]) / float(len(s["vel"]))))
        raw = [int(p) for p in s["pitches"]]
        S = {p % 12 for p in raw}                # 完整音级，绝不在提取阶段删音
        bass_pc = min(raw) % 12
        name, single = _pick_name(S, bass_pc)
        s["name"] = name
        s["pcs"] = S
        s["bass_pc"] = bass_pc
        if name is None:
            stats["degrade"] += 1
        else:
            stats["usable"] += 1
            prev = name
            stats["chords"][name] = stats["chords"].get(name, 0) + 1

        # ── 2P 和弦模式：按切片整拍网格扫弦 ───────────────────────────
        if name is not None and not single:
            beats = []
            if enable_chord_rhythm and N_beats >= 2 and D_grid >= 0.90:
                for k in range(N_beats):
                    bt = t + k * b
                    if bt <= float(s["end"]) - 0.12 + 1e-9:
                        beats.append(bt)
            if not beats:
                beats = [t]
            # 开启律动：单拍内相邻扫弦最多 0.32s（允许 0.5s 律动）；
            # 关闭律动：每个切片起点只扫 1 次，只保留旧的 0.45s 节流保护。
            grid_throttle = min(0.32, b * 0.75) if enable_chord_rhythm else CHORD_THROTTLE
            for k, bt in enumerate(beats):
                if last_t is not None and (bt - last_t) < grid_throttle - 1e-9:
                    stats["throttle"] += 1
                    continue
                last_t = bt
                # 所有扫弦音簇统一力度 75（配套右键下扫）
                level = 75
                ps = sorted(p for p in range(40, 75) if p % 12 in S)
                if not ps:
                    ps = [_nearest_safe_pitch(name, 45 + bass_pc)]
                ps = ps[:6]
                for p in ps:
                    chord_notes.append({"t": bt, "dur": min(span, 0.35),
                                        "pitch": int(p), "vel": level, "track": -1,
                                        "is_head": (k == 0)})
                markers.append((bt, name))
                stats["chord_notes"] += len(ps)
        else:
            bp = _bass_in_range(bass_pc, 40, 74) or int(min(raw))
            if last_t is None or (t - last_t) >= CHORD_THROTTLE - 1e-9:
                last_t = t
                chord_notes.append({"t": t, "dur": span, "pitch": int(bp),
                                    "vel": vel, "track": -1})
                markers.append((t, name or "C5"))
                stats["chord_notes"] += 1

        # ── 2P 品格模式 ────────────────────────────────────────────────
        if enable_arpeggio:
            bass = _bass_in_range(bass_pc, 45, 56)
            if bass is None:
                bass = _bass_in_range(bass_pc, 40, 68)
            # 45/46 只能落在第 0 页，会让琶音反复翻页；同音级上移八度进第 1 页
            if bass is not None and bass < 47 and bass + 12 <= 68:
                bass += 12
            if bass is None:
                stats["fret_dropped"] += 1
                continue
            if span < 0.30:                       # 短切片只留起点的低音
                notes_seq = [bass]
                step_list = [(t, bass)]
            else:
                # 空弦音会落到“Zero 页”，琶音里要避开，否则整段来回翻页
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

    if not chord_notes and not fret_notes:
        return None
    return {
        "notes": {"chord": chord_notes, "fret": fret_notes},
        "markers": {"chord": markers, "fret": []},
        "stats": stats,
        "slices": slices,
    }

# ── 单人 Solo：主旋律+装饰音 再叠上"品格伴奏音"（同一物理秒轴）────────────
def _fret_options(pitch, max_fret=24):
    """这个音高能按出来的 [(品格, 弦)]；空 = 按不出来。"""
    p = int(pitch)
    out = []
    for s, op in GP.STRING_OPEN_NOTES.items():
        f = p - int(op)
        if 0 <= f <= max_fret:
            out.append((f, int(s)))
    return out

def _compiled_note_frets(base, timing=None):
    """用编译器给只有主旋律+装饰音的线定把位，取出每个音实际用的品格 fret。"""
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
    """t 时刻正在响的和弦切片（允许 1ms 边界容差）。

    和弦切片起点来自未取整的浮点秒，而主旋律音 `t` 统一 round 到 6 位小数，
    两者在换和弦的瞬间会差零点几微秒；不留容差就会把"新小节第一拍"算到旧和弦上。
    """
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
    """把任意 BPM 的单拍折算到人类体感的四分音符脉搏 [0.44, 0.88] 秒。

    例：193.55 BPM 的 0.31s 单拍 → 0.62s 体感四分音符；
    琶音步长恒为半拍（0.22~0.44s 黄金区间），通吃 60~200 BPM，不做单曲过拟合。
    """
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
    """在固定品格 `fret` 上，找出音级属于 `pcs` 的低音候选（升序）。

    `below`：只收严格低于该音高的音（同刻齐拨时用来避开主旋律自己）。
    """
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


def merge_solo_accomp(merged, accomp, enable_arpeggio=True, arpeggio_pattern="pingpong", slices=None,
                      timing=None):
    """通用「主旋律锚定 + 归一化恒定脉搏」Solo 伴奏合流。

    ★ 铁律：翻页完全自由——只要换页预算够、主旋律不晚出（late==0），
      绝不为了压低 turns 而限制伴奏音所在品格页。
    ★ 所有伴奏音的 `pitch % 12` 必须属于当前和弦切片，绝不产生和弦外音。

    琶音关（根音模式）：每个和弦切片垫 1 颗 [45,56] 根音；
      优先找主旋律音同刻齐拨（同品格，0 错拍），其次找足够宽的主旋律空隙补根音。
    琶音开（指弹模式）：密集唱段只垫同刻同品格低音；长延音/停顿段以主旋律音起点
      为时间零点、按恒定 arp_step 半拍顺拨，尾巴在下一句人声前 >=0.16s 收音。
    """
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

    # 主旋律按 15ms 聚帧（同一帧视为同一个起音点）
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
    last_acc = [None]          # 上一颗伴奏音的起点（用列表便于闭包写）

    def spaced(t, q):
        return last_acc[0] is None or (float(t) - float(last_acc[0])) >= q * 0.85 - 1e-9

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
                # ── 密集唱段：中间绝不塞任何异拍琶音，只允许同刻同品格低音 ──
                if not spaced(t, q):
                    stats["drop_dense"] += 1
                    continue
                cands = _sl_same_fret_lows(m_pitch, f_m, S, 45, 68, below=m_pitch - 2)
                if not cands:
                    stats["drop_dense"] += 1
                    continue
                bp = max(cands)                       # 最靠近主旋律的低音，听感最厚
                kept.append({"t": t, "dur": m_dur, "pitch": int(bp),
                             "vel": max(45, min(95, int(mnote.get("vel") or 70))),
                             "track": -1, "fret": int(f_m), "src": "same_fret_low",
                             "pcs": sorted(S)})
                stats["bass_doublestop"] += 1
                stats["kept"] += 1
                last_acc[0] = t
                continue

            # ── 长延音 / 句间停顿：t_i 同刻垫低音 + 恒定 arp_step 顺拨 ──
            t_limit = t_next - max(0.75 * step, timing.advance_need(1, True, True) + 0.03)
            cands = _sl_same_fret_lows(m_pitch, f_m, S, 45, 68, below=m_pitch - 2)
            if cands and spaced(t, q):
                bp = max(cands)
                bass_dur = round(min(m_dur, step * 0.85, 0.25), 4)
                kept.append({"t": t, "dur": bass_dur, "pitch": int(bp),
                             "vel": max(45, min(95, int(mnote.get("vel") or 70))),
                             "track": -1, "fret": int(f_m), "src": "same_fret_low",
                             "pcs": sorted(S)})
                stats["bass_doublestop"] += 1
                stats["kept"] += 1
                last_acc[0] = t
            k = 1
            while k <= 64:
                tk = round(t + k * step, 6)
                if tk > t_limit + 1e-9:
                    break
                if round(tk, 6) in mel_pitches:        # 绝不和主旋律抢同一刻
                    k += 1
                    continue
                sl_k = _sl_active(slices, tk) or sl
                S_k = set(sl_k.get("pcs") or set()) or S
                hi = min(68, m_pitch - 2)
                cycle = [p for p in range(45, hi + 1) if (p % 12) in S_k]
                if cycle:
                    p = cycle[(k - 1) % len(cycle)]
                    # 空隙琶音恢复短促颗粒，绝不再延到下一句主旋律前
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
        # ── 根音模式：每个和弦切片一颗饱满低音，优先同刻齐拨，允许自由跨页 ──
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
                # 切片末尾 20ms 内的音属于"下一小节的和弦"（MIDI 边界常差零点几毫秒），
                # 在这里垫音会变成上一小节的音级 → 跑调；交给下一个切片再找锚点。
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
                # 规则 B：切片起点落在宽敞空隙里 → 补一颗根音，延到下一颗主旋律前 25ms
                k = bisect.bisect_right(starts, t_sl)
                d_prev = (t_sl - starts[k - 1]) if k > 0 else 9.9
                d_next = (starts[k] - t_sl) if k < len(starts) else 9.9
                need = max(0.22, 0.75 * step)
                if (d_prev >= need - 1e-9 and d_next >= need - 1e-9
                        and spaced(t_sl, q)):
                    dur = min(d_next - 0.025, e_sl - t_sl) if k < len(starts)                         else (e_sl - t_sl)
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
    # 去重：同一时刻同一音高只留一颗
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
    """编译前断言：1P 与 2P 的事件基准 `t0` 一致（都直接落在原始 start_sec 上）。

    做法：把 .mid 里每个音符的原始 start_sec 排成表，逐个事件查"离它最近的
    原始起点"；偏差必须 <1ms。任何事件被拍点量化/整体平移/四舍五入过，
    这里都会直接抛 AssertionError 报出来。
    """
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
               enable_chord_rhythm=None, timing=None, force_recalc=False):
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
    info = {"shifts": [], "merged": None, "p2": None, "t0": None, "chords": None}
    names = song.get("track_names") or []
    by_track = song.get("notes_by_track") or {}

    chans = {}
    for n in flat_notes:
        chans.setdefault(n.track_idx, set()).add(n.channel)

    # 已经带完整角色契约轨的文件（例如本软件「另存为新 MIDI」的产物）：
    # 直接采用文件里的四条角色轨，不再做八度归位与自动合流——否则用户
    # 在卷帘里的手工微调会被覆盖。
    exact_names = {(n or "").strip().upper() for n in names}
    # force_recalc：用户主动改了琶音/扫弦开关或点了恢复默认，此时忽略文件里
    # 静态保存的角色轨，按最新开关从 Main Melody / Chords 重新生成。
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
        if is_melody_track(name):        # 其它轨（含装饰音轨）一律忽略
            mel_idx.append(idx)

    if force_recalc:
        # 只按原始旋律轨重算，忽略另存文件里的角色轨，避免旋律重复叠加
        role_tracks = (TRACK_SOLO_MASTER, TRACK_DUET_LEAD, TRACK_DUET_RHYTHM)
        mel_idx = [i for i in mel_idx
                   if (names[i] if i < len(names) else "").strip().upper()
                   not in role_tracks]

    # ①-b 本文件没有和弦轨就按“没有”处理：绝不跨文件偷别的曲子。
    chord_flat = flat_notes
    audit_notes = flat_notes
    chord_src = {"file": os.path.basename(song.get("path") or ""),
                 "path": song.get("path") or "",
                 "tracks": len(chord_idx), "fallback": False, "names": [],
                 "no_chord_track": not chord_idx}
    info["chords"] = chord_src
    info["no_chord_track"] = not chord_idx

    # ① 八度归位（Chords* 与鼓轨跳过；主旋律轨额外做"孤立八度跳音"滤波）
    for idx in ([] if trust_roles else mel_idx):
        keep, st = normalize_track(by_track.get(idx) or [], melody=True)
        by_track[idx] = keep
        st["name"] = names[idx] if idx < len(names) else ("轨%d" % idx)
        st["track"] = idx
        info["shifts"].append(st)

    # ② 先算 2P：Solo 要把它的"品格伴奏音"叠进来，所以得排在合流之前。
    #    没有**精确** DUET_P2_RHYTHM 轨名、有 Chords* 轨、且文件里没有老式和弦标记。
    exact = exact_names
    p2 = None
    if chord_idx and (force_recalc or TRACK_DUET_RHYTHM not in exact)             and not (song.get("markers") or []):
        p2 = build_p2(chord_flat, chord_idx, enable_arpeggio=enable_arpeggio,
                      arpeggio_pattern=arpeggio_pattern,
                      tempo_map=song.get("tempo_map"),
                      enable_chord_rhythm=enable_chord_rhythm,
                      timing=timing)
    elif trust_roles and chord_idx and not (song.get("markers") or []):
        # 已带角色轨：和弦模式沿用文件里的块，品格线按当前开关重新生成
        built = build_p2(chord_flat, chord_idx, enable_arpeggio=enable_arpeggio,
                         arpeggio_pattern=arpeggio_pattern,
                         tempo_map=song.get("tempo_map"),
                         enable_chord_rhythm=enable_chord_rhythm,
                         timing=timing)
        if built:
            stored = by_track.get((song.get("track_index") or {}).get(TRACK_DUET_RHYTHM))
            if stored:
                built["notes"]["chord"] = [dict(n) for n in stored]
            p2 = built

    # ③ 合流：没有旧契约轨名（按**精确轨名**判，别名不算）才做
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
            _add_track(song, TRACK_SOLO_MASTER, solo)      # 单人 = 1P + 品格伴奏
            _add_track(song, TRACK_DUET_LEAD, merged)      # 1P = 只有主旋律+装饰音
            info["merged"] = mst
            info["merged"]["tracks"] = [names[i] if i < len(names) else str(i)
                                        for i in mel_idx]

    # ④ 挂 2P 虚拟轨（和弦用块、品格用单音线，两份内容同一个物理秒轴）
    if p2:
        stored_idx = (song.get("track_index") or {}).get(TRACK_DUET_RHYTHM)
        if trust_roles and stored_idx is not None:
            idx = stored_idx
        else:
            idx = _add_track(song, TRACK_DUET_RHYTHM, p2["notes"]["chord"])
        p2["track"] = idx
        if not (trust_roles and stored_idx is not None):
            p2["notes"]["chord"] = song["notes_by_track"][idx]
        for role in ("chord", "fret"):
            fixed = []
            for n in p2["notes"][role]:
                d = dict(n)
                d["track"] = idx
                fixed.append(d)
            p2["notes"][role] = fixed
        song["p2"] = p2
        info["p2"] = p2["stats"]

    # ⑤ 编译前断言：1P / 2P 的事件基准 t0 必须一致（都 = 原始 start_sec）
    _audit_t0(song, audit_notes, info)

    # 走新路径时：品格模式禁用鼠标扫弦（旧文件明确写了 FRET_STRUM:on 的照旧）
    if info["merged"] or info["p2"]:
        if not song.get("fret_strum_explicit"):
            song["fret_strum"] = False
        song["adapter_merged"] = True

    song["adapter"] = info
    return info

# ── 报告 ──────────────────────────────────────────────────────────────────
def report_lines(info):
    """给 `report_text` 用的几行人话。"""
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
