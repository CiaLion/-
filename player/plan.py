# -*- coding: utf-8 -*-
"""plan.py -- 演奏计划的数据结构、读写、校验、以及"起始设置指引"。

计划 = **一整首曲子的演奏脚本**：什么时候点哪个扇区、拨哪根弦、扫哪个方向。
两个模式共用一套格式，只有 `kind` 和事件种类不同。

设计要点：
* `time` = 该动作**开始的时刻**（不是完成时刻），演奏器按 `target = t0 + time / speed`
  绝对调度，不累积漂移。生成器已经把"机械耗时"提前扣掉了（例如按弦动作提前 0.035 秒发），
  所以声音落在拍点上。
* **速度不进计划**：计划里存的是乐曲本身的秒数，倍数由运行时统一除 —— 中途改速度不用重算。
* 开头留 `lead` 秒，留出切回游戏窗口的时间。
* **演奏期不做识别**：起始页按 `start.steps` 摆好，之后只靠计数翻页。
"""
import io
import json
import os

# ── 品格模式的 5 页（实测：轮盘 6 个扇区 = 6 个品格；5 页覆盖 0~24 品）────────
FRET_PAGES = {
    1: {"range": "1~6 品", "wheel_hint": "轮盘左上角应是 I，顺时针 I~VI"},
    2: {"range": "7~12 品", "wheel_hint": "轮盘左上角应是 VII，顺时针 VII~XII"},
    3: {"range": "13~18 品", "wheel_hint": "轮盘左上角应是 XIII，顺时针 XIII~XVIII"},
    4: {"range": "19~24 品", "wheel_hint": "轮盘左上角应是 XIX，顺时针 XIX~XXIV"},
    5: {"range": "0 品（空弦）", "wheel_hint": "轮盘上应该全是 Zero"},
}

# 品格模式的音域：1 弦最粗 E2(40) … 6 弦最细 E4(64)，最高 24 品
STRING_OPEN = {1: 40, 2: 45, 3: 50, 4: 55, 5: 59, 6: 64}
MAX_FRET = 24

PLAN_VERSION = 2

# ── 品格 <-> 页/扇区 的**唯一换算处**────────────────────────────────────
def page_of_fret(fret):
    """品格数 -> 第几页（1..5）。0 品在第 5 页（Zero）。"""
    fret = int(fret)
    if fret == 0:
        return 5
    return (fret - 1) // 6 + 1

def sector_of_fret(fret):
    """品格数 -> 扇区号（0=12点 … 5=10点）。"""
    fret = int(fret)
    if fret == 0:
        return 0
    return (fret - 1) % 6

def fret_of_page_sector(page, sector):
    """第几页 + 扇区 -> 品格数（第 5 页恒为 0 品）。"""
    page, sector = int(page), int(sector) % 6
    if page == 5:
        return 0
    return (page - 1) * 6 + sector + 1

def fret_candidates(midi, max_fret=MAX_FRET):
    """这个音高在哪些 (品格, 弦) 上能弹出来。"""
    out = []
    for s, opening in STRING_OPEN.items():
        f = int(midi) - opening
        if 0 <= f <= max_fret:
            out.append((f, s))
    return out

# ── 时间基准：全局 20 毫秒 ────────────────────────────────────────────────
#
#   * 所有"等多久 / 隔多久"的参数，基准值一律 0.020 秒（20 毫秒）；
#   * **唯一例外**：翻页键松开后 25 毫秒（`after_page_key_release`）；
#   * **6 根弦之间无密度限制**。
#
# 下面这组 `LIMIT_*` 是**基准值**，不是限制值。
BASE_MS = 0.020                 # 全局时间基准（20 毫秒）
BASE_MS_PAGE_TURN = 0.025       # 翻页这一条例外：25 毫秒

LIMIT_FRET_PLUCK = BASE_MS          # 换成品格 + 拨弦
LIMIT_PLUCK = BASE_MS               # 拨一根弦
LIMIT_STRUM = BASE_MS               # 扫弦
LIMIT_CHORD_SELECT = BASE_MS        # 只换格子（不扫）
LIMIT_PAGE_TURN = BASE_MS_PAGE_TURN  # 翻页
LIMIT_NOTES_PER_SEC = 1.0 / BASE_MS  # = 50 个/秒（按 20 毫秒基准）
SUSTAIN_MAX_MEASURED = 4.05  # 长按延音：按住超过约 4.05 秒，再按也不会更长（实测台阶）

# ── 品格模式"扫弦"（= 按弦号依次拨 6 根）────────────────────────────────
# 默认值 = **出厂默认**；真正算账时走 `timing_now()`，也就是 `player_config.json` 里
# 当前设置的值。见 `fret_strum_cost()`。
FRET_STRUM_STRINGS = 6
FRET_STRUM_GAP_FLOOR = BASE_MS              # 20 毫秒基准
FRET_STRUM_GAP = BASE_MS                    # 20 毫秒基准
PLUCK_HOLD_DEFAULT = 0.12                   # 按住时长（不是间隔）：让游戏能看清这一帧
FRET_STRUM_COST = (FRET_STRUM_STRINGS - 1) * FRET_STRUM_GAP + PLUCK_HOLD_DEFAULT

# 出厂默认的"时间账单"里的可配置项（读不到 player_config 时用这一份兜底）
_FALLBACK_TIMING = {
    "min_action_interval": BASE_MS,
    "arpeggio_gap": BASE_MS,
    "fret_strum_gap": BASE_MS,            # 老名字，等价
    "pluck_hold": PLUCK_HOLD_DEFAULT,
    "pluck_park_wait": BASE_MS,
    "strum_park_wait": BASE_MS,
    "strum_click_hold": 0.05,
    "strum_hold": 0.0,
    "fret_click_hold": BASE_MS,
    "select_click_hold": BASE_MS,
    "select_pre_wait": BASE_MS,
    "page_key_hold": BASE_MS,
    "page_turn_gap": BASE_MS,
    "after_page_key_release": BASE_MS_PAGE_TURN,
    "after_wheel_click_release": 0.015,
    "settle_after_page_turn": BASE_MS,
    "sustain_max": 4.0,
}

def arpeggio_gap(timing=None):
    """**琶音**（依次拨弦）时每根弦之间的间隔（现读配置）。"""
    t = timing or timing_now()
    return float(t.get("arpeggio_gap", t.get("fret_strum_gap", FRET_STRUM_GAP)))

def timing_now():
    """
    **当前生效的延迟参数**（从 `player_config.json` 读；读不到就用出厂默认）。

    密度自检（`density_report`）按这里的值**现算**成本。
    成本若写死在常量里，参数改了而自检仍按旧值算，就会得出"游戏弹得动"的错误结论
    （例：每根弦间隔由 0.020 秒调到 0.5 秒时，6 弦扫弦成本由 0.22 秒变成 2.62 秒）。
    现读之后，参数改到哪，账就跟到哪。
    """
    out = dict(_FALLBACK_TIMING)
    try:
        import player_config
        got = player_config.rename_legacy_keys(player_config.load_timing())
        for k in out:
            if k in got:
                out[k] = float(got[k])
        # 老名字 `fret_strum_gap` 若还在用，同步到新名字
        if "arpeggio_gap" not in got and "fret_strum_gap" in got:
            out["arpeggio_gap"] = float(got["fret_strum_gap"])
    except Exception:
        pass
    return out

def fret_strum_cost(strings=FRET_STRUM_STRINGS, timing=None):
    """
    **琶音**（依次拨弦）弹完一次**要花多少秒**（真实成本，跟着配置走）。

    = (弦数 − 1) × 每根之间的间隔 + 最后一根的按住时长。

    这**不是扫弦**的耗时。扫弦是鼠标点一下（见 `strum_cost`）。

    琶音间隔设为 0 时，算式结果也是 0；但**游戏侧的物理账不能当 0**（6 根弦 = 6 次拨弦），
    所以这里给一个地板值「每次拨弦至少 `LIMIT_PLUCK`」—— 自检采用这个值。
    """
    t = timing or timing_now()
    gap = arpeggio_gap(timing=t)
    hold = float(t.get("pluck_hold", PLUCK_HOLD_DEFAULT))
    n = max(1, int(strings))
    want = (n - 1) * gap + hold
    floor = n * LIMIT_PLUCK                 # 20 毫秒基准 × 拨弦次数
    return round(max(want, floor), 4)

# 别名：语义与 `fret_strum_cost` 完全相同
arpeggio_cost = fret_strum_cost

def turn_cost(timing=None):
    """
    翻一页**实际要占多久**（软件侧）= 按键按住 + 连按间隔 + 翻后等待。

    结果不会低于 `LIMIT_PAGE_TURN`（= 时间基准里翻页那一条）。
    """
    t = timing or timing_now()
    c = (float(t.get("page_key_hold", 0.05))
         + float(t.get("page_turn_gap", 0.15))
         + float(t.get("settle_after_page_turn", 0.05)))
    return round(max(LIMIT_PAGE_TURN, c), 4)

def pluck_cost(timing=None):
    """拨一根弦（短音）实际要占多久 = 挪指针 + 按住。"""
    t = timing or timing_now()
    return round(float(t.get("pluck_park_wait", 0.05))
                 + float(t.get("pluck_hold", PLUCK_HOLD_DEFAULT)), 4)

def strum_cost(timing=None):
    """和弦模式扫弦（点一下）实际要占多久 = 挪指针 + 点击按住。"""
    t = timing or timing_now()
    return round(float(t.get("strum_park_wait", 0.03))
                 + float(t.get("strum_click_hold", 0.05)), 4)

def _prev_cost(kind, timing=None):
    """
    事件 op → 这个动作"要花多久"（密度检查用）。

    ★ 游戏侧的基准值（`LIMIT_*`）和软件侧的等待（可配置）**取大的那个**：
    等待调大 → 账跟着变大；等待调小 → 也不会低于游戏的物理基准。
    """
    t = timing or timing_now()
    fs = fret_strum_cost(timing=t)
    turn = turn_cost(timing=t)
    pl = max(LIMIT_PLUCK, pluck_cost(timing=t))
    st = max(LIMIT_STRUM, strum_cost(timing=t))
    sel = (max(LIMIT_CHORD_SELECT, float(t.get("fret_click_hold", 0.01)))
           + float(t.get("select_pre_wait", 0.0)))
    if kind == "fret":
        return {"fret_pluck": max(LIMIT_FRET_PLUCK, pl),
                "pluck": pl,
                "chord_pluck": pl,
                "shape_pluck": pl,
                # 扫弦 = 鼠标点一下，**两种模式一样快**
                "strum": st,
                "select_strum": st + sel,
                # 琶音 = 依次拨弦，**两种模式一样的硬账**（不是扫弦）
                "arpeggio": max(fs, LIMIT_PLUCK),
                "fret_strum": max(fs, LIMIT_PLUCK),   # 别名，语义 = 琶音
                "select": sel,
                "turn": turn}
    return {"select_strum": st + sel,
            "strum": st,
            "select": sel,
            "turn": turn,
            # 和弦模式也能拨单根弦（实测会响）
            "chord_pluck": pl,
            "shape_pluck": pl,
            "arpeggio": max(fs, LIMIT_PLUCK),
            "fret_strum": max(fs, LIMIT_PLUCK)}

def density_report(plan):
    """
    这份计划**游戏能不能弹得动**？返回中文问题清单（空 = 没问题）。

    生成完之后必须自查一遍，不能把不可能的密度直接塞进去。

    成本**现读 `player_config`**（见 `timing_now()`）：延迟参数一改，这里的账立刻跟着变。

    判据分两类：
      * 前面是**发声动作**（fret_pluck/pluck/chord_pluck/select*/strum/fret_strum）→ 按实际耗时卡；
      * 前面是**翻页** → 只要求把那一页翻完（`turn_cost()`）。

    翻页（`turn`）**不产生声音**，实测 0.12 秒一档也认；
    但它常被排在"它要服务的那个音"之前，因而**夹在两个发声动作中间**——
    若用"发声动作的耗时"去要求它，会误报"来不及"。
    """
    problems = []
    if not plan:
        return problems
    evs = plan.get("events") or []
    kind = plan.get("kind")
    t = timing_now()
    cost = _prev_cost(kind, timing=t)
    turn = turn_cost(timing=t)
    too_tight = []
    for i in range(len(evs) - 1):
        gap = float(evs[i + 1]["time"]) - float(evs[i]["time"])
        prev_op = evs[i].get("op")
        need = cost.get(prev_op, 0.0)
        # 关键：**后面那个是翻页时，不要把"发声动作的耗时"算在它头上** ——
        # 翻页是提前排的，它只要能把键发出去就行。
        if evs[i + 1].get("op") == "turn":
            need = min(need, turn)
        if gap < need - 1e-6:
            too_tight.append((round(float(evs[i]["time"]), 3),
                              prev_op, round(gap, 3), round(need, 3)))
    if too_tight:
        tightest = min(too_tight, key=lambda x: x[2])
        problems.append(
            "有 %d 处相邻动作来不及（最紧的一处：第 %.2f 秒的「%s」之后只隔 %.3f 秒，"
            "而这个动作要 %.3f 秒）—— 游戏里这一段会糊或迟到"
            % (len(too_tight), tightest[0], tightest[1], tightest[2], tightest[3]))
    # 音符密度
    if evs:
        span = float(evs[-1]["time"]) - float(evs[0]["time"])
        if span > 1.0:
            rate = len([e for e in evs
                        if e.get("op") in ("fret_pluck", "pluck", "chord_pluck")]) / span
            if rate > LIMIT_NOTES_PER_SEC * 1.2:
                problems.append("音符密度 %.1f 个/秒，超过游戏上限（≈%.1f 个/秒）—— 会有音发不出来"
                                % (rate, LIMIT_NOTES_PER_SEC))
    return problems

def density_summary(plan):
    """给界面/日志用的一行人话（不管有没有问题都给一句）。"""
    if not plan:
        return "（还没生成脚本）"
    evs = plan.get("events") or []
    if not evs:
        return "（脚本是空的）"
    cost = _prev_cost(plan.get("kind"))
    margins = []
    for i in range(len(evs) - 1):
        gap = float(evs[i + 1]["time"]) - float(evs[i]["time"])
        need = cost.get(evs[i].get("op"), 0.0)
        if need > 0:
            margins.append(gap - need)
    if not margins:
        return "这份脚本没有密集动作，游戏弹得动"
    worst = min(margins)
    if worst < 0:
        return "⚠ 最紧的一处还差 %.0f 毫秒（会糊/迟到）" % (-worst * 1000)
    return "节奏余量：最紧的一处还有 %.0f 毫秒，游戏弹得动" % (worst * 1000)

# ── 计划对象 ────────────────────────────────────────────────────────────
def make_plan(kind, source="", duration=0.0, speed=1.0, start=None, stats=None, events=None):
    """造一份计划（普通 dict，方便直接 json.dump）。"""
    if kind not in ("fret", "chord"):
        raise ValueError("kind 只能是 'fret' 或 'chord'，收到 %r" % (kind,))
    return {
        "version": PLAN_VERSION,
        "kind": kind,
        "source": source,
        "duration": float(duration),
        "speed": float(speed),
        "start": start or {},
        "stats": stats or {},
        "events": list(events or []),
    }

def make_start(kind, page, extra_steps=None):
    """造"起始设置指引"。照这几步把游戏摆好，演奏期就不需要识别了。"""
    page = int(page)
    if kind == "fret":
        info = FRET_PAGES.get(page, {})
        mode_cn = "品格"
        wheel_hint = info.get("wheel_hint", "")
        steps = [
            "游戏里按 Z 键，切到「品格」模式",
            "按 T 键翻到第 %d 页（%s）" % (page, info.get("range", "")),
            "切回游戏窗口，按 F8 开始演奏",
        ]
    else:
        mode_cn = "和弦"
        try:
            from chord_map import CHORD_PAGES
            names = " ".join(CHORD_PAGES[page - 1]) if 1 <= page <= len(CHORD_PAGES) else ""
        except Exception:
            names = ""
        wheel_hint = ("第 %d 页是 %s" % (page, names)) if names else ("第 %d 页" % page)
        steps = [
            "游戏里按 Z 键，切到「和弦」模式",
            "按 T 键翻到第 %d 页（%s）" % (page, names or wheel_hint),
            "切回游戏窗口，按 F8 开始演奏",
        ]
    if extra_steps:
        steps.extend(extra_steps)
    return {"mode_cn": mode_cn, "page": page, "wheel_hint": wheel_hint, "steps": steps}

def start_hint(plan):
    """给界面用的起始指引（多行文本 + 结构化字段）。"""
    start = (plan or {}).get("start") or {}
    steps = list(start.get("steps") or [])
    if not steps:
        steps = ["按 F8 开始演奏"]
    text = "开始前请在游戏里这样设置：\n" + "\n".join(
        "%d. %s" % (i + 1, s) for i, s in enumerate(steps))
    return {
        "mode_cn": start.get("mode_cn", ""),
        "page": start.get("page"),
        "wheel_hint": start.get("wheel_hint", ""),
        "steps": steps,
        "text": text,
    }

def summary_line(plan):
    """一行摘要，界面上的小字用（例如"433 个音 · 换品格 168 次 · 全在第 2 页 · 3 分 35 秒"）。"""
    if not plan:
        return "（还没生成脚本）"
    st = plan.get("stats") or {}
    dur = float(plan.get("duration") or 0.0)
    mm, ss = int(dur // 60), int(dur % 60)
    kind = plan.get("kind")
    if kind == "fret":
        parts = ["%d 个音" % st.get("notes", 0)]
        if st.get("fret_changes") is not None:
            parts.append("换品格 %d 次" % st["fret_changes"])
        if st.get("cross_page"):
            parts.append("跨页 %d 次" % st["cross_page"])
        elif st.get("cross_page") == 0:
            parts.append("不用跨页")
        if st.get("fret_min") is not None and st.get("fret_max") is not None:
            parts.append("%d~%d 品" % (st["fret_min"], st["fret_max"]))
    else:
        parts = ["%d 个和弦" % st.get("chords", 0),
                 "和弦变化 %d 次" % st.get("changes", 0),
                 "扫弦 %d 次" % st.get("strums", 0)]
    parts.append("%d 分 %02d 秒" % (mm, ss))
    return " · ".join(parts)

# ── 读写 ────────────────────────────────────────────────────────────────
def save_plan(plan, path):
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)
    return path

def load_plan(path):
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)

# ── 校验：生成完自查一遍，别把坏计划交给演奏器 ──────────────────────────
def validate(plan):
    """返回问题列表（空 = 没问题）。"""
    problems = []
    if not isinstance(plan, dict):
        return ["计划不是字典"]
    try:
        from guitar_plan import CHORD_SHAPES as _SHAPES
    except Exception:
        _SHAPES = {}
    kind = plan.get("kind")
    if kind not in ("fret", "chord"):
        problems.append("kind 不合法：%r" % (kind,))
    events = plan.get("events")
    if not isinstance(events, list) or not events:
        problems.append("events 为空")
        return problems

    last_t = -1.0
    for i, e in enumerate(events):
        t = e.get("time")
        if not isinstance(t, (int, float)):
            problems.append("第 %d 个事件缺 time" % i)
            continue
        if t < last_t - 1e-6:
            problems.append("第 %d 个事件时间倒退（%.3f < %.3f）" % (i, t, last_t))
        last_t = max(last_t, t)
        op = e.get("op")
        if op not in ("fret_pluck", "pluck", "fret", "select", "select_strum",
                      "strum", "turn", "check",
                      # 两种模式都能做琶音（依次拨弦），所以多了这几个事件类型。
                      "chord_pluck", "arpeggio", "fret_strum", "shape", "shape_pluck"):
            problems.append("第 %d 个事件 op 不合法：%r" % (i, op))
            continue
        if op in ("fret_pluck", "fret"):
            s = e.get("sector")
            if not isinstance(s, int) or not (0 <= s <= 5):
                problems.append("第 %d 个事件 sector 不合法：%r" % (i, s))
        if op in ("fret_pluck", "pluck", "chord_pluck", "shape_pluck"):
            s = e.get("string")
            if not isinstance(s, int) or not (1 <= s <= 6):
                problems.append("第 %d 个事件 string 不合法：%r" % (i, s))
        if op == "fret_pluck":
            f = e.get("fret")
            if isinstance(f, int) and isinstance(e.get("sector"), int):
                if sector_of_fret(f) != e["sector"]:
                    problems.append("第 %d 个事件 fret=%s 与 sector=%s 不匹配（应为 %s）"
                                    % (i, f, e["sector"], sector_of_fret(f)))
        if op in ("select", "chord_pluck") and not e.get("chord"):
            problems.append("第 %d 个事件缺 chord" % i)
        # `select_strum` 两种模式通用：要么给 chord（和弦模式），要么给 fret（品格模式）
        if op == "select_strum" and not e.get("chord") and e.get("fret") is None:
            problems.append("第 %d 个事件既没有 chord 也没有 fret，不知道选哪个格子" % i)
        if op == "chord_pluck":
            c = e.get("chord")
            if c and _SHAPES and c not in _SHAPES:
                problems.append("第 %d 个事件的和弦不在轮盘上：%r" % (i, c))
        if op == "fret_strum":
            f = e.get("fret")
            if f is not None and (not isinstance(f, int) or not (0 <= f <= MAX_FRET)):
                problems.append("第 %d 个事件的 fret 不合法：%r（应 0~%d）" % (i, f, MAX_FRET))
            if f is None and not isinstance(e.get("sector"), int):
                problems.append("第 %d 个事件既没有 fret 也没有 sector，不知道弹哪个品格" % i)
        if op == "arpeggio":
            f = e.get("fret")
            if f is not None and (not isinstance(f, int) or not (0 <= f <= MAX_FRET)):
                problems.append("第 %d 个事件的 fret 不合法：%r（应 0~%d）" % (i, f, MAX_FRET))
            c = e.get("chord")
            if c and _SHAPES and c not in _SHAPES:
                problems.append("第 %d 个事件的和弦不在轮盘上：%r" % (i, c))
        if op in ("strum", "select_strum", "fret_strum", "arpeggio"):
            d = e.get("direction")
            # 'R' = 休止符（不该出现在计划里）；只允许下扫 D / 上扫 U
            if d is not None and d not in ("D", "U"):
                problems.append("第 %d 个事件扫弦方向不合法：%r（只能 D 或 U）" % (i, d))
        # 长按延音：写 null 表示"照老样子点一下"，写了就必须是个正数
        if op in ("fret_pluck", "pluck", "chord_pluck", "shape_pluck",
                  "strum", "select_strum", "fret_strum", "arpeggio"):
            h = e.get("hold")
            if h is not None and (not isinstance(h, (int, float)) or h <= 0):
                problems.append("第 %d 个事件的 hold 不合法：%r（要么不写，要么是正数秒）"
                                % (i, h))
        if op == "turn" and e.get("key") not in ("T", "R"):
            problems.append("第 %d 个事件的 turn key 不合法：%r" % (i, e.get("key")))
    if last_t <= 0:
        problems.append("所有事件时间都是 0，计划可能没生成好")
    return problems

def page_span(plan):
    """计划里用到的所有页（给界面显示"要不要中途翻页"）。"""
    if not plan:
        return []
    if plan.get("kind") == "fret":
        pages = {page_of_fret(e["fret"]) for e in plan.get("events", [])
                 if e.get("op") == "fret_pluck" and isinstance(e.get("fret"), int)}
    else:
        pages = set()
        try:
            from chord_map import get_chord_location
            for e in plan.get("events", []):
                if e.get("op") == "select" and e.get("chord"):
                    loc = get_chord_location(e["chord"])
                    if loc:
                        pages.add(loc[0] + 1)
        except Exception:
            pass
    return sorted(pages)
