import math

# Wheel geometry, measured from in-game screenshots (client 1280x720).
# The wheel centre sits at 67.3% of the client width and 51.1% of its height;
# the outer disc radius is 24.6% of the client height.
# The user can still fine-tune these with the on-screen calibration overlay, and
# the tuned values are remembered in player_config.json.
WHEEL_CENTER_X_RATIO = 0.6727
WHEEL_CENTER_Y_RATIO = 0.5111
WHEEL_RADIUS_RATIO = 0.2460   # distance from centre out to the chord ring

# How far out along a sector we click, as a fraction of the radius.
# The pink highlight covers roughly 0.45-0.85 R and the chord name is printed at
# ~0.68 R, so 0.62 R lands solidly inside the lit band instead of on its outer
# lip (the user reported clicks that looked too far out at 0.72 R).
# MUST stay equal to guitar_plan.SECTOR_CLICK_FRACTION: that is the value the
# real player uses (guitar_player.py), and if the two ever drift apart the
# calibration overlay's test-click stops matching what the song actually does.
SECTOR_CLICK_FRACTION = 0.62

# 6 radial sectors in degrees (0 deg = 3 o'clock, clockwise)
# Top: -90° (or 270°)
# Top-Right: -30° (or 330°)
# Bottom-Right: +30°
# Bottom: +90°
# Bottom-Left: +150°
# Top-Left: +210°
SECTOR_ANGLES = [
    270, # 0: Top (12 o'clock)
    330, # 1: Top-Right (2 o'clock)
    30,  # 2: Bottom-Right (4 o'clock)
    90,  # 3: Bottom (6 o'clock)
    150, # 4: Bottom-Left (8 o'clock)
    210  # 5: Top-Left (10 o'clock)
]

# 4 pages of chord presets from actual in-game screenshots.
#   这张表是全项目的"权威表"：plan.py 的起始指引、performer 的选和弦、
#   planner_chord 的走向映射都依赖它。**改任何一个名字都要先让用户再核一遍** ——
#   错一格 = 弹那首歌时会点错和弦（用户耳朵很尖，会听出来）。
CHORD_PAGES = [
    # Page 1 (Dot 1): Basic Triads
    ["G", "Am", "Bm", "C", "D", "Em"],
    
    # Page 2 (Dot 2): 7th and 9th Chords
    ["Cmaj7", "C7", "Cm7", "G7", "C9", "Cm9"],
    
    # Page 3 (Dot 3): Power chords & Extended chords
    ["C5", "D5", "G5", "A5", "Cmaj9", "Cadd9"],
    
    # Page 4 (Dot 4): Inverted & slash chords
    ["G/B", "Am7/G", "D/F#", "F/A", "A", "Dm"]
]

DEFAULT_CHORD_PAGES = CHORD_PAGES

# Standard guitar tuning, string 1 = thickest (lowest) ... string 6 = thinnest.
# Confirmed by the user: in 异环's guitar mode the number keys run from the thick
# string up to the thin one.
STRING_OPEN_NOTES = {
    1: 40,   # E2 - thickest
    2: 45,   # A2
    3: 50,   # D3
    4: 55,   # G3
    5: 59,   # B3
    6: 64,   # E4 - thinnest
}

# Which strings actually sound for each chord.
#
# 这张表**直接从 guitar_plan.CHORD_SHAPES 派生**（-1 = 该弦不发声），
# 保证和弦音高只有**一个出处**，不会出现"两张表打架"。
#
# 音高口径：`guitar_plan.CHORD_SHAPES` 是演奏器真正读的那张（实测与真机录音对得上）；
# `chord_map.CHORD_STRINGS` 供练习曲生成使用。
# 要改"哪根弦响"，**改 CHORD_SHAPES**，这里自动跟着变。
def _derive_chord_strings():
    try:
        import guitar_plan as _GP
    except Exception:
        return None
    out = {}
    for _name, _shape in getattr(_GP, "CHORD_SHAPES", {}).items():
        if not isinstance(_shape, dict):
            continue
        out[_name] = [s for s in range(1, 7) if _shape.get(s, -1) >= 0]
    return out or None

CHORD_STRINGS = _derive_chord_strings() or {
    # 兜底（万一 guitar_plan 读不到，至少是乐理自洽的一份）
    "G": [1, 2, 3, 4, 5, 6],
    "Am": [2, 3, 4, 5, 6],
    "Bm": [2, 3, 4, 5, 6],
    "C": [2, 3, 4, 5, 6],
    "D": [3, 4, 5, 6],
    "Em": [1, 2, 3, 4, 5, 6],
    "G7": [1, 2, 3, 4, 5, 6],
    "Am7/G": [1, 2, 3, 4, 5, 6],
    "D/F#": [1, 3, 4, 5, 6],
}

def get_chord_strings(chord_name):
    """Strings that sound for a chord; falls back to all six."""
    if not chord_name:
        return list(STRING_OPEN_NOTES)
    return CHORD_STRINGS.get(chord_name.strip(), list(STRING_OPEN_NOTES))

def nearest_string(note):
    """
    Map a MIDI note to the guitar string that would play it.

    Nothing is folded here: an open string or a note within six frets of one is
    mapped to that string, so the pluck keeps its register instead of being
    squashed into one octave.
    """
    best_string, best_cost = 1, None
    for s, opening in STRING_OPEN_NOTES.items():
        if note < opening:
            cost = (opening - note) * 2.0      # below the open string: penalise hard
        else:
            fret = note - opening
            cost = fret if fret <= 6 else 6 + (fret - 6) * 1.5
        if best_cost is None or cost < best_cost:
            best_cost, best_string = cost, s
    return best_string

def get_chord_location(chord_name, pages=None):
    """
    Find which (page_index, sector_index) a chord belongs to.
    Returns: (page_index, sector_index) or None
    """
    pages = pages or CHORD_PAGES
    name_clean = chord_name.strip()
    for p_idx, page in enumerate(pages):
        for s_idx, ch in enumerate(page):
            if ch.lower() == name_clean.lower():
                return p_idx, s_idx
    return None

def calculate_sector_screen_pos(screen_x0, screen_y0, client_w, client_h, sector_index):
    """
    Calculate screen (X, Y) coordinate to click a specific chord sector.
    """
    center_x = screen_x0 + client_w * WHEEL_CENTER_X_RATIO
    center_y = screen_y0 + client_h * WHEEL_CENTER_Y_RATIO
    radius = client_h * WHEEL_RADIUS_RATIO
    
    deg = SECTOR_ANGLES[sector_index % 6]
    rad = math.radians(deg)
    
    target_x = center_x + radius * SECTOR_CLICK_FRACTION * math.cos(rad)
    target_y = center_y + radius * SECTOR_CLICK_FRACTION * math.sin(rad)
    
    return int(target_x), int(target_y)
