# -*- coding: utf-8 -*-
"""
guitar_plan.py -- the guitar (chord-wheel) side of the auto player.

Why this module exists
----------------------
The piano side can play any melody. The guitar side cannot: it is a chord
instrument wired to a fixed vocabulary.

What the instrument physically is
---------------------------------
* Six strings tuned E2 A2 D3 G3 B3 E4 (MIDI 40 45 50 55 59 64). String 1 is the
  thickest / lowest, which is how the game's own fret screens number them.
* 24 chords in total, six per wheel page.
* Selecting a chord is a mouse click on a wheel sector; changing page is R / T.
* The chord decides which strings may sound and at which fret, so any single
  string press has ONE fixed pitch for a given chord.
* Sound stops when the chord changes, so sustain depends on staying put.

What that means for arranging
-----------------------------
A song can only be played if its harmony can be expressed with those 24 chords.
Melody notes are only available if they happen to sit on the current chord's
strings. Chord changes cost 150-300 ms, which rules out one-chord-per-beat
progressions at fast tempos.

This module therefore does three things:
  1. describes every chord's string fingerings (the physical model),
  2. maps a song's chord symbols onto the 24 available chords,
  3. renders that into a timed plan of "select chord / strum / pluck" actions
     which the player executes.
"""
import math

# Open-string pitches, string 1 = thickest.
# and C4 and the user picked C4 -- but a C chord holds that string at the first fret, so
# with C4 open the chord would sound C#4, which is not a note a C chord can contain. The
# two claims ("open string is C4" and "the C chord shape is right") cannot both be true,
# and the first test could not tell C4 from C#4 (they are only 100 cents apart, the same
# distance as C4 from B3). Left at B3 until a test with two half-step-apart references
# settles it. Details: 吉他模式-音高验证结论.md
STRING_OPEN_NOTES = {1: 40, 2: 45, 3: 50, 4: 55, 5: 59, 6: 64}
STRING_COUNT = 6

NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
NOTE_TO_PC = {n: i for i, n in enumerate(NOTE_NAMES)}
NOTE_TO_PC.update({'Db': 1, 'Eb': 3, 'Gb': 6, 'Ab': 8, 'Bb': 10,
                   'C#': 1, 'D#': 3, 'F#': 6, 'G#': 8, 'A#': 10})

def midi_name(n):
    return "%s%d" % (NOTE_NAMES[n % 12], (n // 12) - 1)

# Fret per string, 1..6. **现在不再有 -1（没有哑音弦）**。
#
#   而且同一格上 1→6 弦音高严格递增。原来那 34 个 `-1` 是照"真实吉他按法"
#   抄的（比如 C 和弦第 1 弦现实中不弹），在游戏里是错的 —— 它导致和弦模式弹
#   分解单音时只能反复用同一根低音弦，听起来像单音噪音。
#   现在把 34 个弦位都补成"该和弦的和弦内音 + 保证递增"的最小品格。
#
#     → 音高 42(F#2) 45(A2) 50(D3) 57(A3) 62(D4) 69(A4)。
#     第 2~6 弦全是纯正的 D/A 五度和弦内音（消灭了原来那个 A#2 的小二度擦音），
#     第 1 弦 42 是 D 大三和弦的三音，四音协和且仍满足 1→6 严格递增。
#   补齐/校验依据：fill_chord_shapes（已校验）。
CHORD_SHAPES = {
    # page 1 - open triads
    "G":     {1: 3, 2: 2, 3: 0, 4: 0, 5: 0, 6: 3},
    "Am":    {1: 0, 2: 0, 3: 2, 4: 2, 5: 1, 6: 0},
    "Bm":    {1: 2, 2: 2, 3: 4, 4: 4, 5: 3, 6: 2},
    "C":     {1: 0, 2: 3, 3: 2, 4: 0, 5: 1, 6: 0},
    "D":     {1: 2, 2: 0, 3: 0, 4: 2, 5: 3, 6: 2},
    "Em":    {1: 0, 2: 2, 3: 2, 4: 0, 5: 0, 6: 0},

    # page 2 - sevenths and ninths
    "Cmaj7": {1: 0, 2: 3, 3: 2, 4: 0, 5: 0, 6: 0},
    "C7":    {1: 0, 2: 3, 3: 2, 4: 3, 5: 1, 6: 0},
    "Cm7":   {1: 3, 2: 3, 3: 5, 4: 3, 5: 4, 6: 3},
    "G7":    {1: 3, 2: 2, 3: 0, 4: 0, 5: 0, 6: 1},
    "C9":    {1: 0, 2: 3, 3: 2, 4: 3, 5: 3, 6: 3},
    "Cm9":   {1: 3, 2: 3, 3: 5, 4: 3, 5: 3, 6: 3},

    # page 3 - power chords and add9 shapes
    "C5":    {1: 3, 2: 3, 3: 5, 4: 5, 5: 8, 6: 8},
    "D5":    {1: 2, 2: 0, 3: 0, 4: 2, 5: 3, 6: 5},
    "G5":    {1: 3, 2: 5, 3: 5, 4: 7, 5: 8, 6: 10},
    "A5":    {1: 0, 2: 0, 3: 2, 4: 2, 5: 5, 6: 5},
    "Cmaj9": {1: 0, 2: 3, 3: 2, 4: 0, 5: 0, 6: 2},
    "Cadd9": {1: 0, 2: 3, 3: 2, 4: 0, 5: 3, 6: 3},

    # page 4 - inversions and minor shapes
    "G/B":   {1: 3, 2: 2, 3: 0, 4: 0, 5: 0, 6: 3},
    "Am7/G": {1: 3, 2: 0, 3: 2, 4: 0, 5: 1, 6: 0},
    "D/F#":  {1: 2, 2: 0, 3: 0, 4: 2, 5: 3, 6: 2},
    "F/A":   {1: 1, 2: 0, 3: 3, 4: 2, 5: 1, 6: 1},
    "A":     {1: 0, 2: 0, 3: 2, 4: 2, 5: 2, 6: 0},
    "Dm":    {1: 1, 2: 0, 3: 0, 4: 2, 5: 3, 6: 1},
}

# The whole vocabulary, in wheel order.
CHORD_PAGES = [
    ["G", "Am", "Bm", "C", "D", "Em"],
    ["Cmaj7", "C7", "Cm7", "G7", "C9", "Cm9"],
    ["C5", "D5", "G5", "A5", "Cmaj9", "Cadd9"],
    ["G/B", "Am7/G", "D/F#", "F/A", "A", "Dm"],
]
ALL_CHORDS = [c for page in CHORD_PAGES for c in page]

# Wheel sectors: 12, 2, 4, 6, 8, 10 o'clock.
SECTOR_ANGLES = [270, 330, 30, 90, 150, 210]

def chord_page_sector(name):
    """(page index, sector index) for a chord, or None."""
    for p, page in enumerate(CHORD_PAGES):
        if name in page:
            return p, page.index(name)
    return None

def chord_strings(name):
    """Strings that sound for a chord (1 = thickest)."""
    shape = CHORD_SHAPES.get(name)
    if not shape:
        return list(range(1, STRING_COUNT + 1))
    return [s for s in range(1, STRING_COUNT + 1) if shape.get(s, -1) >= 0]

def chord_pitch_of_string(name, string):
    """The MIDI pitch a string sounds when this chord is selected (None if muted)."""
    shape = CHORD_SHAPES.get(name)
    if not shape:
        return None
    fret = shape.get(string, -1)
    if fret is None or fret < 0:
        return None
    return STRING_OPEN_NOTES[string] + fret

def best_string_for_pitch(name, pitch):
    """
    Which string of this chord sounds closest to `pitch`.

    Returns (string, pitch_offset) or (None, None) when the chord cannot produce it.
    """
    best, best_off = None, None
    for s in chord_strings(name):
        p = pluck_pitch(name, s)          # ★ 单拨走实测（见 PLUCK_PITCHES）
        if p is None:
            continue
        off = abs(p - pitch)
        if best_off is None or off < best_off:
            best, best_off = s, off
    return best, best_off

def chord_pitches(name):
    """Every pitch this chord can sound, lowest first."""
    out = []
    for s in chord_strings(name):
        p = chord_pitch_of_string(name, s)
        if p is not None:
            out.append((p, s))
    out.sort()
    return out

# ══════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════
# 来历：基准录音里和弦模式 144 个单音有 30 条对不上。为了排除"叠音干扰"，
#     ① 22:43  4 和弦（G/C/Em/C5），每根弦隔 2 秒，失真音色；
#     ② 23:00  24 和弦，每根弦 0.4 秒，清音；
#     ③ 23:18  24 和弦，每根弦 1 秒，清音。
#   ②和③两次独立录音有 **128/144 条测出来完全一样** → 不是随机噪声。
#   再用"起音差谱"（把起音前的频谱减掉，只留新弹的这个音）复核：
#   D 弦5 得到干净的 D#4 谐波列（310/620/930/1240Hz）、Cm7 弦6 得到 G#4
#   （415/830/1244/1656Hz），说明**这些"差 1 个半音"是游戏真的这么响**，
#   不是测量串音。
#
# 口径：①扫弦仍然用 `CHORD_SHAPES`（扫弦 48/48 实测是对的，没动过）；
#   ②**单拨按这张实测表**，24 个和弦 × 6 根弦**全部列出**（与指型相同的弦
#     也照写），方便逐条核对、也避免"改了指型却忘了单拨表"。
#     表里与指型不同的那些弦，来历见上面的录音/差谱复核说明。
#     唯一一处例外：G5 弦6 的 YIN 测到 G2（低 31 个半音，明显是八度误判），
#     改用差谱复核得到的 D5(74)（与指型相同）。
#   ③ 顺序 = `CHORD_PAGES` 的轮盘顺序。
PLUCK_PITCHES = {
    # 第 1 页
    "G":     [43, 47, 50, 55, 59, 67],  # G2, B2, D3, G3, B3, G4
    "Am":    [40, 46, 52, 57, 59, 64],  # E2, A#2, E3, A3, B3, E4
    "Bm":    [42, 48, 54, 59, 61, 66],  # F#2, C3, F#3, B3, C#4, F#4
    "C":     [40, 53, 52, 55, 60, 64],  # E2, F3, E3, G3, C4, E4
    "D":     [40, 45, 50, 57, 63, 66],  # E2, A2, D3, A3, D#4, F#4
    "Em":    [40, 45, 50, 57, 61, 64],  # E2, A2, D3, A3, C#4, E4
    # 第 2 页
    "Cmaj7": [40, 53, 52, 55, 59, 64],  # E2, F3, E3, G3, B3, E4
    "C7":    [40, 53, 52, 58, 60, 64],  # E2, F3, E3, A#3, C4, E4
    "Cm7":   [44, 53, 60, 58, 63, 68],  # G#2, F3, C4, A#3, D#4, G#4
    "G7":    [43, 47, 50, 55, 59, 65],  # G2, B2, D3, G3, B3, F4
    "C9":    [44, 53, 52, 58, 63, 68],  # G#2, F3, E3, A#3, D#4, G#4
    "Cm9":   [44, 53, 51, 58, 63, 68],  # G#2, F3, D#3, A#3, D#4, G#4
    # 第 3 页
    "C5":    [53, 60, 65, 59, 64, 53],  # F3, C4, F4, B3, E4, F3
    "D5":    [42, 45, 50, 57, 63, 66],  # F#2, A2, D3, A3, D#4, F#4
    "G5":    [43, 50, 55, 59, 67, 43],  # G2, D3, G3, B3, G4, G2
    "A5":    [41, 45, 52, 57, 64, 69],  # F2, A2, E3, A3, E4, A4
    "Cmaj9": [53, 52, 55, 59, 62, 64],  # F3, E3, G3, B3, D4, E4
    "Cadd9": [53, 52, 55, 63, 68, 53],  # F3, E3, G3, D#4, G#4, F3
    # 第 4 页
    "G/B":   [43, 47, 50, 55, 59, 67],  # G2, B2, D3, G3, B3, G4
    "Am7/G": [43, 45, 52, 57, 60, 64],  # G2, A2, E3, A3, C4, E4
    "D/F#":  [42, 42, 50, 57, 63, 66],  # F#2, F#2, D3, A3, D#4, F#4
    "F/A":   [41, 45, 53, 57, 60, 65],  # F2, A2, F3, A3, C4, F4
    "A":     [41, 45, 52, 57, 62, 64],  # F2, A2, E3, A3, D4, E4
    "Dm":    [44, 45, 50, 57, 63, 65],  # G#2, A2, D3, A3, D#4, F4
}

def pluck_pitch(name, string):
    """这个和弦上按数字键 `string`，游戏**实际**发出的音高（有实测用实测）。"""
    d = PLUCK_PITCHES.get((name or "").strip()) if isinstance(name, str) else None
    if d:
        try:
            idx = int(string)
            if isinstance(d, dict):
                p = d.get(idx)
            else:                       # 人工实测表：列表按 1~6 弦排列
                p = d[idx - 1] if 0 <= idx - 1 < len(d) else None
        except Exception:
            p = None
        if p is not None:
            return int(p)
    return chord_pitch_of_string(name, string)

def pluck_pitches(name):
    """[(弦号, 单拨实测音高)]，按音高由低到高。"""
    out = []
    for s in chord_strings(name):
        p = pluck_pitch(name, s)
        if p is not None:
            out.append((s, p))
    out.sort(key=lambda x: x[1])
    return out

def parse_chord_symbol(text):
    """
    Turn 'Cmaj7', 'Am7/G', 'D/F#', 'F#m', 'Bb' into (root_pc, quality, bass_pc).

    Only the vocabulary the game actually has is produced; anything fancier maps to
    the nearest available quality, which is the whole point of the adapter.
    """
    if not text:
        return None
    raw = text.strip().replace('♯', '#').replace('♭', 'b')
    raw = raw.split(':')[-1].strip()              # some MIDI files write "Chord:Am"
    if not raw:
        return None

    bass_pc = None
    if '/' in raw:
        raw, bass = raw.split('/', 1)
        bass = bass.strip()
        if bass and bass[0].isalpha():
            name = bass[0].upper() + bass[1:]
            if name[:2] in NOTE_TO_PC:
                bass_pc = NOTE_TO_PC[name[:2]]
            elif name[:1] in NOTE_TO_PC:
                bass_pc = NOTE_TO_PC[name[:1]]

    raw = raw.strip()
    if not raw or not raw[0].isalpha():
        return None
    root = raw[0].upper()
    rest = raw[1:]
    if rest[:1] in ('#', 'b'):
        root += rest[0]
        rest = rest[1:]
    if root not in NOTE_TO_PC:
        return None
    root_pc = NOTE_TO_PC[root]

    low = rest.lower()
    if low.startswith('maj7') or low.startswith('m7') and low.startswith('maj'):
        quality = 'maj7'
    elif low.startswith('maj9'):
        quality = 'maj9'
    elif low.startswith('add9'):
        quality = 'add9'
    elif low.startswith('m9'):
        quality = 'm9'
    elif low.startswith('m7'):
        quality = 'm7'
    elif low.startswith('m'):
        quality = 'm'
    elif low.startswith('7'):
        quality = '7'
    elif low.startswith('9'):
        quality = '9'
    elif low.startswith('5'):
        quality = '5'
    else:
        quality = 'maj'
    return root_pc, quality, bass_pc

def chord_pitch_classes(name):
    """Pitch classes a game chord contains, derived from its fingering."""
    pcs = set()
    for s in chord_strings(name):
        p = chord_pitch_of_string(name, s)
        if p is not None:
            pcs.add(p % 12)
    return pcs

def chord_bass_pitch_class(name):
    pcs = chord_pitches(name)
    return pcs[0][0] % 12 if pcs else None

# 来历：单拨走的是实测音高（PLUCK_PITCHES），有些弦实测出来的音**不是和弦内音**，
# 而且与某个和弦内音只差 1 个半音（比如 Am 的 2 弦实测 A#2，撞 Am 的 5 弦音 A2）。
# 这种弦一拨就是刺耳的撞音，所以单拨时直接熔断（不用它）。
def chord_pitch_class_set(name):
    """这个和弦的**理论**构成音级集合（由指型表 CHORD_SHAPES 导出）。"""
    return chord_pitch_classes(name)

def _semitone_clash(pc, pcs):
    """pc 不是和弦内音，且与某个和弦内音只差 1 个半音 → 是撞音。"""
    if not pcs or pc in pcs:
        return False
    return any((pc - x) % 12 in (1, 11) for x in pcs)

def _safe_by(name, strings, pcs):
    out = []
    for s in strings:
        p = pluck_pitch(name, s)
        if p is None or not _semitone_clash(p % 12, pcs):
            out.append(s)
    return out

def pluck_safe_strings(name):
    """单拨可用弦号：去掉小二度地雷弦。

    兜底：过滤后不足 3 根时，先退到「只躲根音」，仍不足 3 根就全部可用
    （宁可让它响，也不能把和弦弹成残缺）。
    """
    strings = chord_strings(name)
    safe = _safe_by(name, strings, chord_pitch_class_set(name))
    if len(safe) >= 3:
        return safe
    root = _root_of(name)
    if root is not None:
        safe = _safe_by(name, strings, {root})
        if len(safe) >= 3:
            return safe
    return list(strings)

def pluck_safe_pitches(name):
    """[(弦号, 实测音高)]，只含安全弦，按音高由低到高。"""
    ok = set(pluck_safe_strings(name))
    return [(s, p) for (s, p) in pluck_pitches(name) if s in ok]

_QUALITY_ORDER = ['maj', 'm', '7', 'maj7', 'm7', '9', 'm9', 'maj9', 'add9', '5']

def _quality_of(name):
    root, quality, bass = parse_chord_symbol(name) or (None, None, None)
    return quality

def _root_of(name):
    root, quality, bass = parse_chord_symbol(name) or (None, None, None)
    return root

def chord_distance(symbol, game_chord, prefer_simple=True):
    """
    How badly does `game_chord` stand in for `symbol`? Lower is better.

    Score = missing pitch classes + extra pitch classes + wrong root/bass penalties.
    Quality similarity is a tie-breaker so that Am7 does not map to C when Am exists.
    """
    parsed = parse_chord_symbol(symbol)
    if not parsed:
        return None
    root_pc, quality, bass_pc = parsed

    want = set()
    if quality == 'maj':
        want = {root_pc, (root_pc + 4) % 12, (root_pc + 7) % 12}
    elif quality == 'm':
        want = {root_pc, (root_pc + 3) % 12, (root_pc + 7) % 12}
    elif quality == '7':
        want = {root_pc, (root_pc + 4) % 12, (root_pc + 7) % 12, (root_pc + 10) % 12}
    elif quality == 'maj7':
        want = {root_pc, (root_pc + 4) % 12, (root_pc + 7) % 12, (root_pc + 11) % 12}
    elif quality == 'm7':
        want = {root_pc, (root_pc + 3) % 12, (root_pc + 7) % 12, (root_pc + 10) % 12}
    elif quality == '9':
        want = {root_pc, (root_pc + 4) % 12, (root_pc + 7) % 12, (root_pc + 10) % 12,
                (root_pc + 2) % 12}
    elif quality == 'm9':
        want = {root_pc, (root_pc + 3) % 12, (root_pc + 7) % 12, (root_pc + 10) % 12,
                (root_pc + 2) % 12}
    elif quality == 'maj9':
        want = {root_pc, (root_pc + 4) % 12, (root_pc + 7) % 12, (root_pc + 11) % 12,
                (root_pc + 2) % 12}
    elif quality == 'add9':
        want = {root_pc, (root_pc + 4) % 12, (root_pc + 7) % 12, (root_pc + 2) % 12}
    elif quality == '5':
        want = {root_pc, (root_pc + 7) % 12}

    have = chord_pitch_classes(game_chord)
    missing = len(want - have)
    extra = len(have - want) * 0.6

    penalty = missing + extra
    # root must match unless the symbol itself is an inversion
    if game_chord == "G/B":
        grow = _root_of("G/B")
    else:
        grow = _root_of(game_chord)
    if grow != root_pc:
        penalty += 2.0
    if bass_pc is not None and chord_bass_pitch_class(game_chord) != bass_pc:
        penalty += 1.0

    # prefer the plainest chord that fits, but an exact name match always wins
    if symbol.strip().lower() == game_chord.strip().lower():
        penalty -= 1.2
    if prefer_simple:
        gq = _quality_of(game_chord)
        try:
            penalty += 0.15 * _QUALITY_ORDER.index(gq)
        except ValueError:
            pass
    return penalty

def map_chord(symbol, allowed=None):
    """Best game chord for a symbol -> (name, score, alternatives)."""
    allowed = allowed or ALL_CHORDS
    scored = []
    for c in allowed:
        d = chord_distance(symbol, c)
        if d is not None:
            scored.append((d, c))
    if not scored:
        return None, None, []
    scored.sort()
    return scored[0][1], scored[0][0], scored[:3]

STRUM = "strum"
PLUCK = "pluck"
SELECT = "select"
VERIFY = "verify"

# Measured in the live game (see the latency test):
#   same wheel page  -> 150-200 ms from click to the game showing the new chord
#   one page turn    -> ~550 ms
# The planner schedules a change this long BEFORE the beat it belongs to.
CHANGE_COST_SAME_PAGE = 0.25
CHANGE_COST_PAGE_TURN = 0.60

# How long the player is actually BUSY making the change, measured in the live run
# log: selecting a chord inside one page took 0.25 s, one that needed a page turn
# took 0.70 s. This is NOT the same as the lead time above. Strum scheduled inside
# this window cannot play on time; they fire the moment the change returns and
# collide with the new chord's first strum, which is heard as a short burst.
CHANGE_BLOCK_SAME_PAGE = 0.25
CHANGE_BLOCK_PAGE_TURN = 0.70

SECTOR_CLICK_FRACTION = 0.62
# Where along a sector's centre line the pointer is placed. The pink highlight covers
# roughly 0.45-0.85 R and the chord name is printed at ~0.68 R, so clicking at 0.62 R
# lands solidly inside the lit band instead of on its outer lip (the user reported the
# click looked off-centre even when the correct chord was selected).

# Usable strumming patterns, expressed in beats; D = down, U = up, R = rest (a stroke that
# is deliberately NOT played).
#
# The quarter pattern is what every earlier recording used, and the user's verdict on it was
# blunt: "the rhythm is monotonous". It is -- one downstroke on every beat for three minutes
# is a metronome, not an accompaniment. Real strumming patterns get their character from
# what they leave out, so the patterns below are the standard figures taught for 4/4
# accompaniment, with the rest built in:
#
#   folk_ballad      D . D U . U D U    the classic "下 下上 空 上下" figure
#   campfire         D . D U . U D U with a lighter second half
#   arpeggio         a bass note plus a slow roll, for the verses
PATTERNS = {
    "down_only":     [("D", 1.0)],
    "quarter":       [("D", 1.0), ("D", 1.0), ("D", 1.0), ("D", 1.0)],
    "folk":          [("D", 0.5), ("D", 0.5), ("D", 0.5), ("U", 0.5),
                      ("U", 0.5), ("D", 0.5), ("U", 0.5)],
    "ballad":        [("D", 1.0), ("D", 0.5), ("U", 0.5), ("U", 0.5), ("D", 0.5)],
    "half_note":     [("D", 2.0)],
    "whole_note":    [("D", 4.0)],

    # D . D U . U D U : the standard beginner-to-intermediate 4/4 figure. The two rests are
    # what stop it sounding like a machine.
    "folk_ballad":   [("D", 1.0), ("R", 0.5), ("D", 0.5), ("U", 0.5), ("R", 0.5),
                      ("U", 0.5), ("D", 0.5), ("U", 0.5)],
    # the same idea, but the second half is a single stroke: calmer, good under a melody
    "campfire":      [("D", 1.0), ("R", 0.5), ("D", 0.5), ("U", 0.5), ("R", 0.5),
                      ("D", 1.0)],
    # a slow roll: bass, then three light strokes, leaving air between them
    "arpeggio":      [("D", 1.0), ("R", 0.5), ("D", 0.5), ("U", 1.0), ("R", 0.5),
                      ("D", 0.5)],
    # 长度正好 2 拍，所以每个和弦都能完整跑一遍，节奏不会因为换和弦而被打断。
    # 对照：4.5 拍的 campfire 套在 2 拍的和弦上，听起来节奏乱。
    "cell_du":       [("D", 1.0), ("U", 1.0)],
    "cell_d":        [("D", 2.0)],
}

class Action(object):
    __slots__ = ("time", "kind", "chord", "string", "direction", "beat", "dropped")

    def __init__(self, time, kind, chord=None, string=None, direction=None, beat=0.0):
        self.time = time
        self.kind = kind
        self.chord = chord
        self.string = string
        self.direction = direction
        self.beat = beat
        self.dropped = False

    def __repr__(self):
        # Every kind gets its own branch. The verify action used to fall through
        # to the pluck branch and printed as "PLUCK <chord> string None", which
        # looked exactly like a broken pluck with a missing string number and
        # sent two readers chasing a bug that was not there.
        if self.kind == SELECT:
            return "t=%.3f SELECT %s" % (self.time, self.chord)
        if self.kind == STRUM:
            return "t=%.3f STRUM %s (%s)" % (self.time, self.direction, self.chord)
        if self.kind == PLUCK:
            return "t=%.3f PLUCK %s string %s" % (self.time, self.chord, self.string)
        if self.kind == VERIFY:
            return "t=%.3f VERIFY %s" % (self.time, self.chord)
        return "t=%.3f %s %s" % (self.time, self.kind, self.chord)

def make_plan(progression, bpm=100.0, pattern="folk", start_time=0.0,
              melody_notes=None, change_cost=None, verify=True,
              change_cost_same_page=None, change_cost_page_turn=None,
              block_same_page=None, block_page_turn=None):
    """
    Turn a chord progression into a timed list of actions.

    progression: list of (start_seconds, chord_symbol, duration_seconds)
    melody_notes: optional list of (time, midi_pitch) played as plucks inside the
                  current chord when the pitch sits on one of its strings.
    change_cost: seconds to schedule a change early; defaults to the measured values
                 (0.25 s inside a page, 0.60 s when the wheel has to turn).
    change_cost_same_page / change_cost_page_turn: same thing, but set per case.
                 Passing both is the preferred way: a page turn genuinely costs
                 more than stepping to a neighbouring sector, and using one number
                 for both either makes same-page changes rush or page turns late.
    block_same_page / block_page_turn: how long the player is actually BUSY
                 selecting a chord (measured live: 0.25 s inside a page, 0.70 s
                 across a page turn). Strum that fall in that busy window cannot be
                 played when they were meant to; they fire the instant the change
                 returns and collide with the new chord's first strum, which is
                 heard as a burst. Defaults to the lead time, but the two are not
                 the same thing: the lead time is how early we start, the block is
                 how long we are stuck. Leaving them equal lets the last strum of
                 the old chord escape the filter whenever the block outlasts the
                 lead, which is exactly what happened on a real run.
    verify:      emit a VERIFY action right after each selection, so the player can
                 confirm the wheel really moved (a single key press and a screenshot).
    """
    actions = []
    beat = 60.0 / max(1.0, bpm)
    seq = PATTERNS.get(pattern) or PATTERNS["quarter"]

    if change_cost is not None:
        cost_same = cost_turn = float(change_cost)
    else:
        cost_same = (CHANGE_COST_SAME_PAGE if change_cost_same_page is None
                     else float(change_cost_same_page))
        cost_turn = (CHANGE_COST_PAGE_TURN if change_cost_page_turn is None
                     else float(change_cost_page_turn))
    block_same = (CHANGE_BLOCK_SAME_PAGE if block_same_page is None
                  else float(block_same_page))
    block_turn = (CHANGE_BLOCK_PAGE_TURN if block_page_turn is None
                  else float(block_page_turn))

    current = None
    current_page = None
    change_windows = []          # (busy_from, busy_until) for every chord change
    for (t0, symbol, dur) in progression:
        mapped, score, alts = map_chord(symbol)
        if not mapped:
            continue
        if mapped != current:
            loc = chord_page_sector(mapped)
            page = loc[0] if loc else 0
            needs_turn = (current_page is not None and page != current_page)
            cost = cost_turn if needs_turn else cost_same
            block = block_turn if needs_turn else block_same
            when = max(start_time, t0 - cost)
            # Selecting a chord blocks the player for `block` seconds. A strum
            # scheduled inside that window cannot happen when it was meant to: it
            # would be fired the instant the change returns, on top of the new
            # chord's own strums, and the ear hears one short burst after every
            # change. Those strums are marked below and dropped by the caller.
            change_windows.append((when, when + block))
            actions.append(Action(when, SELECT, chord=mapped))
            if verify:
                actions.append(Action(when, VERIFY, chord=mapped))
            current = mapped
            current_page = page
        # Strum through the chord's duration. The pattern CYCLES until the chord ends:
        # the earlier version stopped after a single pass, so a 4-beat pattern on a
        # 4-beat chord emitted ONE strum and every song sounded like a single thud
        # (this is why up-strums never appeared to make any difference).
        pos = t0
        i = 0
        end = t0 + dur
        while pos < end - 1e-6:
            direction, beats = seq[i % len(seq)]
            length = beats * beat
            if pos + length > end + 1e-6:
                # the tail is shorter than the next stroke: a half stroke would land
                # off the beat, so skip it instead of clipping it
                break
            strum = Action(pos, STRUM, chord=mapped, direction=direction,
                           beat=pos / beat)
            for (busy_from, busy_until) in change_windows:
                if busy_from - 1e-9 <= pos < busy_until - 1e-9:
                    strum.dropped = True
                    break
            actions.append(strum)
            pos += length
            i += 1

    # melody plucks inside whatever chord is current at that moment
    if melody_notes:
        for item in melody_notes:
            # tolerate both (time, pitch) and (time, pitch, extra) shapes
            try:
                t, pitch = float(item[0]), int(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            chord = None
            for a in actions:
                if a.kind == SELECT and a.time <= t:
                    chord = a.chord
            if not chord:
                continue
            s, off = best_string_for_pitch(chord, pitch)
            if s and off is not None and off <= 2:
                actions.append(Action(t, PLUCK, chord=chord, string=int(s)))

    # drop malformed actions rather than letting the player press a "None" key
    actions = [a for a in actions
               if a.kind != PLUCK or (a.string is not None and 1 <= int(a.string) <= 6)]
    actions.sort(key=lambda a: (a.time, 0 if a.kind == SELECT else 1))
    return actions

def plan_stats(actions):
    sel = [a for a in actions if a.kind == SELECT]
    strums = [a for a in actions if a.kind == STRUM]
    plucks = [a for a in actions if a.kind == PLUCK]
    ups = sum(1 for a in strums if a.direction == 'U')
    downs = len(strums) - ups
    changes = 0
    for a, b in zip(sel, sel[1:]):
        if a.chord != b.chord:
            changes += 1
    gaps = [round(b.time - a.time, 3) for a, b in zip(sel, sel[1:])]
    return {
        "selects": len(sel), "strums": len(strums), "plucks": len(plucks),
        "down_strums": downs, "up_strums": ups,
        "changes": changes,
        "min_change_gap": min(gaps) if gaps else None,
        "too_fast": sum(1 for g in gaps if g < 0.30),
    }

def change_gaps(actions):
    """Time between consecutive chord changes, in seconds (for warnings)."""
    sel = [a for a in actions if a.kind == SELECT]
    return [round(b.time - a.time, 3) for a, b in zip(sel, sel[1:])]

def exclusive_note(symbol, game_chord):
    """
    True when the game chord is exactly the chord asked for (same name).

    Used to make a perfect match always win a tie: G and G/B used to score the same
    for the symbol 'G', and the tie-break could silently pick the inversion.
    """
    return symbol.strip().lower() == game_chord.strip().lower()
