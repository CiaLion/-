# -*- coding: utf-8 -*-
"""modes.py -- 两种模式的**唯一差别表**。

## 这个模块负责什么

"模式"不再散落在 `performer.py` / `app_core.py` 的 `if mode == 'fret' ... else ...`
分支上。两种模式**只由本模块的 `MODES` 表描述**；
执行层（`performer.py` / `app_core.py`）不认模式，只认"形状"。

## 统一模型：一切都是"形状"（Shape）

一个"形状" = **6 根弦各自的品格偏移**，`-1` 表示这根弦不发声。

    品格模式：Shape(品格 n)  → offsets = {1:n, 2:n, 3:n, 4:n, 5:n, 6:n}
    和弦模式：Shape(和弦 C)  → offsets = CHORD_SHAPES["C"] = {1:-1, 2:3, 3:2, 4:0, 5:1, 6:0}

两种模式用的是**同一个求音高公式**（不写死任何"音高查表"）：

    音高 = STRING_OPEN[string] + offsets[string]

所以：
* 品格 n 上的第 s 弦 = `STRING_OPEN[s] + n`
* 和弦 C 上的第 s 弦 = `STRING_OPEN[s] + CHORD_SHAPES["C"][s]`

—— 同一条公式，两种模式都适用。

## 模式描述表只描述三件事

1. **共几页**（品格 5 页 / 和弦 4 页）；
2. **每页 6 格是什么形状 + 格子上印什么字**（品格：I…XXIV、最后一页 Zero；和弦：24 个和弦名）；
3. **扫弦底层走哪条路**（两种模式都是鼠标左/右键；"按弦号依次拨"是**琶音**，不是扫弦）。

弦号约定（**全项目一致，实测确认**）：
`1 = 最粗 E2(40) … 6 = 最细 E4(64)`，也就是**弦号越大音越高**。
"1-6 从低到高"指的是**演奏顺序下标**（1→6 音高递增）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plan as P                     # noqa: E402  （品格页/扇区换算的唯一出处）
import chord_map as CM               # noqa: E402  （和弦页/扇区的唯一出处）
import guitar_plan as GP             # noqa: E402  （和弦指型的唯一出处）

# ── 弦号 → 空弦音高（全项目唯一出处，plan.py / chord_map.py / guitar_plan.py 同值）──
# 1 弦最粗 E2(40)，6 弦最细 E4(64)。**已实测确认**。
STRING_OPEN = {1: 40, 2: 45, 3: 50, 4: 55, 5: 59, 6: 64}
STRING_COUNT = 6
MAX_FRET = P.MAX_FRET               # 24

# 扫弦底层走哪条路。
# **鼠标左键=上扫、右键=下扫，两种模式都有效**，不是和弦模式独占。
# 两种模式的 `strum_impl` 完全一样 —— 唯一区别只剩页数和格子上的字。
STRUM_MOUSE = "mouse_click"          # 鼠标左键（上扫）/ 右键（下扫）—— **两种模式都走这条**
STRUM_SEQUENCE = "sequential_pluck"  # 按弦号依次拨（**琶音**，不是扫弦）

# 下扫 = 低音弦 → 高音弦（弦号 1→6）；上扫 = 反序。
# 这个顺序是**琶音**（依次拨弦）用的；鼠标扫弦的方向由游戏自己决定，跟这个列表无关。
DOWN_ORDER = [1, 2, 3, 4, 5, 6]
UP_ORDER = [6, 5, 4, 3, 2, 1]

def strum_order(direction):
    """**琶音**时弦号的先后顺序。'D'=由低到高（1→6），'U'=由高到低（6→1）。"""
    return list(DOWN_ORDER if str(direction).upper() != "U" else UP_ORDER)

# ── 罗马数字（品格格子上的字）────────────────────────────────────────────
_ROMAN_UNITS = ["", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX"]

def roman(n):
    """1..24 → I .. XXIV（轮盘品格格子上印的字）。"""
    n = int(n)
    if n <= 0:
        return "Zero"
    if n < 10:
        return _ROMAN_UNITS[n]
    if n < 20:
        return "X" + _ROMAN_UNITS[n - 10]
    if n == 20:
        return "XX"
    return "XX" + _ROMAN_UNITS[n - 20]

def midi_name(n):
    """MIDI 号 → 音名（例如 40 → E2）。用于文档和日志，跟 guitar_plan 同口径。"""
    return GP.midi_name(int(n))

# ══════════════════════════════════════════════════════════════════════ Shape
class Shape(object):
    """
    一个"形状"：6 根弦各自的品格偏移。

    `offsets[s]` = 第 s 根弦按在第几品；`-1` = 这根弦不发声。
    **品格**和**和弦**都用这一个类表示 —— 这就是"两模式底层通用"的那一点。
    """

    __slots__ = ("kind", "value", "label", "offsets")

    def __init__(self, kind, value, label, offsets):
        self.kind = kind            # 'fret' | 'chord'
        self.value = value          # 品格数（int）或和弦名（str）
        self.label = label          # 轮盘格子上印的字（'XII' / 'Cmaj7'）
        self.offsets = dict(offsets)

    def pitch(self, string):
        """第 string 根弦在这个形状下发什么音；不发声返回 None。"""
        try:
            s = int(string)
        except Exception:
            return None
        off = self.offsets.get(s)
        if off is None or int(off) < 0:
            return None
        return STRING_OPEN[s] + int(off)

    def pitch_table(self):
        """{弦号: MIDI 或 None}，键固定 1..6（**从低到高**）。"""
        return dict((s, self.pitch(s)) for s in range(1, STRING_COUNT + 1))

    def sounding_strings(self):
        """会发声的弦号，**从小到大**（= 从低音到高音）。"""
        return [s for s in range(1, STRING_COUNT + 1) if self.pitch(s) is not None]

    def muted_strings(self):
        return [s for s in range(1, STRING_COUNT + 1) if self.pitch(s) is None]

    def pitches(self):
        """[(MIDI, 弦号), ...]，按音高从低到高。"""
        out = [(self.pitch(s), s) for s in range(1, STRING_COUNT + 1)]
        return sorted([(p, s) for (p, s) in out if p is not None])

    def closest_string(self, midi):
        """这个形状上离 midi 最近的那根弦 → (弦号, 音高, 差几个半音) 或 (None, None, None)。"""
        best = None
        for (p, s) in self.pitches():
            off = abs(p - int(midi))
            if best is None or off < best[2]:
                best = (s, p, off)
        return best or (None, None, None)

    def is_muted(self, string):
        return self.pitch(string) is None

    def describe(self):
        """一行中文：'XII 品：1弦G#3 2弦C#4 …'（不发声的弦写「不发声」）。"""
        bits = []
        for s in range(1, STRING_COUNT + 1):
            p = self.pitch(s)
            bits.append("%d弦%s" % (s, midi_name(p) if p is not None else "不发声"))
        return "%s：%s" % (self.label, " ".join(bits))

    def __repr__(self):
        return "<Shape %s %s %s>" % (self.kind, self.value, self.offsets)

def fret_shape(fret):
    """品格 n → 形状（6 根弦**全体偏移同一个数**）。0 品 = 全空弦。"""
    f = int(fret)
    if f < 0 or f > MAX_FRET:
        raise ValueError("品格超出范围 0~%d：%r" % (MAX_FRET, fret))
    return Shape("fret", f, roman(f) if f else "Zero",
                 dict((s, f) for s in range(1, STRING_COUNT + 1)))

def chord_shape(name):
    """和弦名 → 形状（6 根弦各自的偏移，`-1` = 不发声）。"""
    n = str(name).strip()
    shape = GP.CHORD_SHAPES.get(n)
    if shape is None:
        return None
    return Shape("chord", n, n, shape)

# ══════════════════════════════════════════════════════════════════════ Mode
class Page(object):
    """轮盘上的一页：6 个格子，每格一个形状。"""

    __slots__ = ("index", "cells", "range_cn")

    def __init__(self, index, cells, range_cn=""):
        self.index = index            # 0 起算
        self.cells = list(cells)      # 长度 6
        self.range_cn = range_cn      # 人话描述（"1~6 品" / "G Am Bm C D Em"）

    def __len__(self):
        return len(self.cells)

    def __getitem__(self, i):
        return self.cells[int(i) % max(1, len(self.cells))]

    def labels(self):
        return [c.label for c in self.cells]

    def label_text(self):
        return " ".join(self.labels())

class Mode(object):
    """
    一种演奏模式 = **页数 + 每格的字 + 扫弦走哪条路**。就这三样，没别的。

    执行层（performer / app_core）只要拿到 `Mode`，就不再需要知道"现在是哪个模式"。
    """

    __slots__ = ("key", "name_cn", "pages", "strum_impl", "select_hint", "wheel_hint")

    def __init__(self, key, name_cn, pages, strum_impl, select_hint="", wheel_hint=""):
        self.key = key
        self.name_cn = name_cn
        self.pages = list(pages)
        self.strum_impl = strum_impl
        self.select_hint = select_hint     # "点扇区" 这件事在本模式里叫什么
        self.wheel_hint = wheel_hint

    @property
    def page_count(self):
        return len(self.pages)

    def page(self, index):
        if not self.pages:
            return None
        return self.pages[int(index) % self.page_count]

    def shape_at(self, page_index, sector):
        """第 page_index 页（0 起算）第 sector 格的形状。"""
        pg = self.page(page_index)
        if pg is None:
            return None
        return pg[int(sector) % 6]

    def locate(self, value):
        """形状的值 → (页号 0 起算, 扇区号 0..5)；找不到返回 None。"""
        for pg in self.pages:
            for i, c in enumerate(pg.cells):
                if c.value == value:
                    return (pg.index, i)
        return None

    def page_of(self, value):
        loc = self.locate(value)
        return loc[0] if loc else None

    def sector_of(self, value):
        loc = self.locate(value)
        return loc[1] if loc else None

    def label_of(self, value):
        loc = self.locate(value)
        return self.shape_at(*loc).label if loc else None

    def turn(self, cur_page, want_page):
        """
        从 cur_page 走到 want_page 要按什么键、按几下（**走最近方向**）。
        返回 (按键 'T'/'R', 次数) 或 None（已经在那一页）。
        """
        if cur_page is None or want_page is None:
            return None
        n = self.page_count
        steps = (int(want_page) - int(cur_page)) % n
        if steps == 0:
            return None
        if steps <= n // 2:
            return ("T", steps)
        return ("R", n - steps)

    def values(self):
        """本模式所有格子上的值（品格 0..24 / 24 个和弦名）。"""
        out = []
        for pg in self.pages:
            for c in pg.cells:
                out.append(c.value)
        return out

    def page_range_cn(self, index):
        pg = self.page(index)
        return pg.range_cn if pg else ""

    def describe(self):
        lines = ["【%s模式】共 %d 页，每页 6 格；扫弦底层走「%s」"
                 % (self.name_cn, self.page_count, self.strum_impl)]
        for pg in self.pages:
            lines.append("  第 %d 页（%s）：%s"
                         % (pg.index + 1, pg.range_cn, pg.label_text()))
        return "\n".join(lines)

# ── 建表：品格 5 页 ─────────────────────────────────────────────────────
def _build_fret_pages():
    pages = []
    # 第 1~4 页：每页 6 个品格（1~6 / 7~12 / 13~18 / 19~24）
    for p in range(4):
        cells = [fret_shape(p * 6 + i + 1) for i in range(6)]
        pages.append(Page(p, cells, "%d~%d 品" % (p * 6 + 1, p * 6 + 6)))
    # 第 5 页：全是 0 品（空弦）—— 轮盘上全是 Zero
    cells = [fret_shape(0) for _ in range(6)]
    pages.append(Page(4, cells, "0 品（全空弦）"))
    return pages

# ── 建表：和弦 4 页 ─────────────────────────────────────────────────────
def _build_chord_pages():
    pages = []
    for p, names in enumerate(CM.CHORD_PAGES):
        cells = []
        for name in names:
            sh = chord_shape(name)
            if sh is None:              # 指型表里没有 → 空形状（不发声），但格子还在
                sh = Shape("chord", name, name, dict((s, -1) for s in range(1, 7)))
            cells.append(sh)
        pages.append(Page(p, cells, " ".join(names)))
    return pages

FRET_MODE = Mode(
    "fret", "品格", _build_fret_pages(), STRUM_MOUSE,
    select_hint="点第几品（换品格）",
    wheel_hint="第 1~4 页依次是 I~VI / VII~XII / XIII~XVIII / XIX~XXIV，第 5 页全是 Zero")
CHORD_MODE = Mode(
    "chord", "和弦", _build_chord_pages(), STRUM_MOUSE,
    select_hint="点第几个和弦（换和弦）",
    wheel_hint="4 页共 24 个和弦")

MODES = {"fret": FRET_MODE, "chord": CHORD_MODE}

# 事件的 kind → 模式（计划里的 `kind` 就是模式名，两者是同一个东西）
KIND_TO_MODE = dict(MODES)

# ══════════════════════════════════════════════════════════════════════ 对外函数
def get(kind):
    """'fret' / 'chord' → Mode；非法输入抛 ValueError（不静默退回）。"""
    if kind not in MODES:
        raise ValueError("没有这种模式：%r（只有 'fret' 和 'chord'）" % (kind,))
    return MODES[kind]

def kind_of_plan(plan):
    """计划 dict → 模式名。"""
    return (plan or {}).get("kind")

def shape(kind, value):
    """(模式, 值) → Shape 或 None（和弦名不认识时）。"""
    if kind == "fret":
        return fret_shape(value)
    if kind == "chord":
        return chord_shape(value)
    raise ValueError("没有这种模式：%r" % (kind,))

def locate(kind, value):
    """(模式, 值) → (页号 0 起算, 扇区号) 或 None。"""
    return get(kind).locate(value)

def pitch_of(kind, value, string):
    """
    两种模式共用的**求音高入口**：
        音高 = STRING_OPEN[string] + 该形状在第 string 弦的偏移
    不发声返回 None。
    """
    sh = shape(kind, value)
    if sh is None:
        return None
    return sh.pitch(string)

def pitch_table(kind, value):
    """{弦号 1..6: MIDI 或 None}（从低到高）。"""
    sh = shape(kind, value)
    return sh.pitch_table() if sh else dict((s, None) for s in range(1, 7))

def page_count(kind):
    return get(kind).page_count

def strum_strings(kind, value, direction):
    """
    **琶音**（依次拨弦）时，这个形状按 direction 的**弦号演奏顺序**（以及每根弦的音高）。

    返回 [(弦号, MIDI), ...]；不发声的弦被跳过。

    ★ 品格模式：6 根弦全发声（偏移全一样），所以就是 1→6 或 6→1。
    ★ 和弦模式：按指型，`-1` 的弦跳过（例如 D 和弦跳过 1、2 弦）。

    ⚠ 这**不是扫弦**：扫弦是鼠标点一下（见 `Mode.strum_impl`），
      这里是"一根一根拨出来"，用来做琶音/分解和弦。
    """
    sh = shape(kind, value)
    out = []
    for s in strum_order(direction):
        if sh is None:
            break
        p = sh.pitch(s)
        if p is not None:
            out.append((s, p))
    return out

# ── 自查：模块自己把表念一遍（跑 `python modes.py` 就能看）──────────────
def summary_text():
    lines = []
    for key in ("fret", "chord"):
        m = get(key)
        lines.append(m.describe())
        lines.append("")
    lines.append("音高公式：音高 = STRING_OPEN[弦号] + 该格子的偏移；STRING_OPEN = %s"
                 % STRING_OPEN)
    return "\n".join(lines)

if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    print(summary_text())
