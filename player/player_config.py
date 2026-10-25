# -*- coding: utf-8 -*-
"""
player_config.py -- 异键演奏自动演奏器的持久化设置（含**可手动调整的延迟参数**）。

两件事住在这个文件里：
1. 轮盘几何 / 速度 / 模式；
2. **所有时间延迟** —— 每个参数都带
   **中文名 / 默认值 / 单位 / 允许范围 / 警告等级**，界面（`gui_v2.py` 的
   「⏱ 高级：延迟设置」）和 JSON 两条路都能改。

纯标准库、不依赖 Qt，所以任何地方都能 import。
"""
import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "player_config.json")

# Only these keys are persisted. Anything unknown found in the file is ignored,
# so an old config can never break a newer build.
# 轮盘几何的缺省值 = 标定并验证过的那一组（6/6 扇区全中），
# 这样万一配置文件丢了，也不会退回旧分辨率的测量值。
DEFAULTS = {
    "wheel_x_ratio": 0.6937,
    "wheel_y_ratio": 0.4378,
    "wheel_radius_ratio": 0.2957,
    "speed": 1.0,
    "mode": "fret",          # 只有 "fret"（品格）和 "chord"（和弦）
    "transpose": 0,
    "enable_arpeggio": True, # 开启指弹横按琶音（伴奏与独奏空隙展开为流水琶音）
    # 2P 和弦伴奏是否按真实单拍做整拍扫弦律动（关 = 每个切片只扫 1 次）
    "enable_chord_rhythm": True,
    "roll_volume": 35,       # 卷帘试听音量（0~100）
    "timing": {},            # 延迟参数（见 TIMING_INFO），空 = 全用默认
}

# ══════════════════════════════════════════════════════════════════════════
# 警告等级（界面上原样显示）
# ══════════════════════════════════════════════════════════════════════════
LEVEL_FLOOR = "⚠ 实测边界：改小可能发不出去"
LEVEL_MEASURED = "⚠ 实测值：改大改小都需重新试听确认"
LEVEL_FREE = "✅ 可自由调整：只影响快慢，不影响能否发出"

TIMING_PRESETS = {
    "30fps": {
        "min_action_interval": 0.001, "fret_click_hold": 0.034, "pluck_hold": 0.034,
        "sustain_max": 4.0, "select_click_hold": 0.034, "strum_park_wait": 0.001,
        "strum_click_hold": 0.034, "strum_hold": 0.0, "page_key_hold": 0.034,
        "page_turn_gap": 0.034, "after_page_key_release": 0.035,
        "settle_after_page_turn": 0.0, "settle_same_page": 0.0,
        "settle_page_turn": 0.0, "sustain_min": 0.30, "repluck_gap": 0.034,
        "start_delay": 3.0, "preschedule_lead": 0.0,
    },
    "60fps": {
        "min_action_interval": 0.001, "fret_click_hold": 0.017, "pluck_hold": 0.017,
        "sustain_max": 4.0, "select_click_hold": 0.017, "strum_park_wait": 0.001,
        "strum_click_hold": 0.017, "strum_hold": 0.0, "page_key_hold": 0.017,
        "page_turn_gap": 0.017, "after_page_key_release": 0.018,
        "settle_after_page_turn": 0.0, "settle_same_page": 0.0,
        "settle_page_turn": 0.0, "sustain_min": 0.30, "repluck_gap": 0.017,
        "start_delay": 3.0, "preschedule_lead": 0.0,
    },
    "90fps": {
        "min_action_interval": 0.001, "fret_click_hold": 0.012, "pluck_hold": 0.012,
        "sustain_max": 4.0, "select_click_hold": 0.012, "strum_park_wait": 0.001,
        "strum_click_hold": 0.012, "strum_hold": 0.0, "page_key_hold": 0.012,
        "page_turn_gap": 0.012, "after_page_key_release": 0.013,
        "settle_after_page_turn": 0.0, "settle_same_page": 0.0,
        "settle_page_turn": 0.0, "sustain_min": 0.30, "repluck_gap": 0.012,
        "start_delay": 3.0, "preschedule_lead": 0.0,
    },
}
DEFAULT_TIMING_PRESET = "30fps"

# 帧率预设的展示文案（标题, 推荐 BPM）
PRESET_INFO = {
    "30fps": ("30 帧", "最稳 · BPM≤120"),
    "60fps": ("60 帧", "均衡 · BPM120~170"),
    "90fps": ("90 帧", "高刷 · BPM≥170"),
}


# 时间参数完整表（界面只显示 TIMING_UI_ORDER 里的 6 项，18 个键全部保留）。
BASE_MS = 0.020                 # 全局时间基准：20 毫秒

# 界面展示的 6 项
TIMING_UI_ORDER = (
    "after_page_key_release",
    "pluck_hold",
    "min_action_interval",
    "sustain_min",
    "sustain_max",
    "start_delay",
)

# 完整 18 项（读写配置、范围钳制、命令行表格）
TIMING_INFO_ALL = [
    # key, 中文名, 默认值, 单位, 最小, 最大, 等级, 说明
    ("min_action_interval", "无需松开触发动作衔接延迟", 0.001, "秒", 0.0, 0.5,
     "✅ 默认统一 1 毫秒（0.001 秒）",
     "上一步不需要松开就能触发下一步时，两步之间隔这么久。"
     "例如左键点换扇区之后 1 毫秒就立刻按弦。"),
    ("fret_click_hold", "点轮盘格子的按住时长", 0.025, "秒", 0.0, 0.30,
     "⚠ 实测边界：按键保持 25 毫秒，改小可能发不出去",
     "换品格时「点一下扇区」按住 25 毫秒（实测下限；改小游戏可能整帧看不到）。"),
    ("pluck_hold", "按键与点选按住时长", 0.017, "秒", 0.0, 1.0,
     "⚠ 实测边界：帧时长 + 1ms（30帧 34ms / 60帧 17ms / 90帧 12ms）",
     "数字键拨弦和鼠标点格共用的按住时长。**保存这一项时会同步写回** "
     "page_key_hold / fret_click_hold / select_click_hold / strum_click_hold / "
     "page_turn_gap / repluck_gap，所以界面只留这一个入口就够了。"),
    ("sustain_max", "长按延音上限", 4.0, "秒", 0.0, 8.0,
     "✅ 可自由调整｜★ 设 0 会让延音完全失效；游戏侧约 4.05 秒到顶",
     "**这一条不是等待，是游戏侧的物理上限**：实测按住约 4.05 秒就到顶。"
     "设 0 会让延音**完全失效**（长音一个都按不住）；调大没有意义（游戏不给更长的音）。"),
    ("select_click_hold", "选格子点击的按住时长", 0.025, "秒", 0.0, 0.30,
     "⚠ 实测边界：按键保持 25 毫秒，改小可能点不中",
     "和弦模式选和弦那一下的鼠标按住时长（品格模式用上面「点轮盘格子」那一项）。"
     "**改小可能点不中**；这一项是**按住时长**（不是间隔）。"),
    ("strum_park_wait", "扫弦前把指针挪开的等待", 0.020, "秒", 0.0, 0.30,
     "✅ 全局基准：20 毫秒",
     "扫弦前指针要离开轮盘；这一条是挪完再等多久。"),
    ("strum_click_hold", "扫弦点击的按住时长", 0.030, "秒", 0.0, 1.0,
     "⚠ 实测边界：扫弦按住 30 毫秒，改小可能整帧看不到",
     "「右键下扫 / 左键上扫」按住的时长。鼠标键比键盘更「重」，实测要 30 毫秒。"),
    ("strum_hold", "扫弦按住不放的时长（延音）", 0.0, "秒", 0.0, 4.0,
     "✅ 可自由调整｜0 = 扫弦点一下不延音；大于 0 = 按住延音（实测会延音）",
     "0 = 扫弦就是点一下（不延音）。大于 0 = 「按下 → 等这么久 → 松开」。"
     "实测确认：**鼠标扫弦按住不放会延音**，品格模式和弦模式都一样。"),
    ("page_key_hold", "翻页键（T/R）按住时长", 0.025, "秒", 0.0, 0.30,
     "⚠ 实测边界：按键保持 25 毫秒",
     "翻页键按下到松开之间等 25 毫秒（实测下限，短了会被吞）。"),
    ("page_turn_gap", "连按翻页之间的间隔", 0.020, "秒", 0.0, 1.0,
     "✅ 全局基准：20 毫秒",
     "一次要翻好几页时，两次按键之间等 20 毫秒。"),
    ("after_page_key_release", "翻页 / Z 切模式松开后生效延迟", 0.018, "秒", 0.0, 0.5,
     "⚠ 实测边界：帧时长 + 2ms（30帧 35ms / 60帧 18ms / 90帧 13ms）",
     "翻页键（T/R）或 Z 键**松开之后**要等这么久，新的根音 / 模式才真正生效，"
     "这段时间里不能拨弦（实测值）。"),
    ("settle_after_page_turn", "翻页后等多久再演奏", 0.0, "秒", 0.0, 1.0,
     "✅ 可自由调整｜实测 0（翻页键松开 35ms 已经够）",
     "翻完最后一页之后、再发下一个动作之前额外等待。"
     "默认 0：翻页键松开后的 35 毫秒已经覆盖了根音切换。"),
    ("settle_same_page", "同页换格后等待（点格→拨弦额外间隔）", 0.0, "秒", 0.0, 1.0,
     "✅ 可自由调整｜实测 0（点下去声音就换好了）",
     "同页换格时，点格之后到拨弦之间的额外等待；调大 = 点格更早发生、拨弦时刻不变。"
     "实际按下→拨弦 = min_action_interval + 本项 + pluck_park_wait。"),
    ("settle_page_turn", "跨页换格子后等待", 0.0, "秒", 0.0, 1.0,
     "✅ 可自由调整｜实测 0",
     "换格子同时还要翻页时的额外等待。实测 0，理由同上。"),
    ("sustain_min", "长按延音判断门槛", 0.30, "秒", 0.0, 2.0,
     "✅ 不是等待，是判断门槛",
     "**这一条不是等待，是判断门槛**：时值短于它的音就点一下（长按反而拖拍）。"
     "只影响生成脚本时的判断。"),
    ("repluck_gap", "同一根弦再拨的最小间隔", 0.025, "秒", 0.0, 0.5,
     "⚠ 实测边界：25 毫秒，改小游戏不认",
     "同一根弦**两次按下之间**至少隔 25 毫秒（实测）。短于这个数，"
     "第二次拨弦会被游戏吞掉，所以编译器还会先补一个「松开」。"),
    ("start_delay", "开始演奏前切回游戏等待", 3.0, "秒", 0.0, 15.0,
     "✅ 可自由调整：留给你切回游戏",
     "编译时给整条时间轴统一加的开头留白。默认 3 秒，够切回游戏窗口。"),
    ("preschedule_lead", "预调度额外提前量", 0.0, "秒", 0.0, 0.5,
     "✅ 可自由调整：换格/翻页之外再多留的余量",
     "换格或翻页需要的时间之外，再额外提前这么多秒排动作。默认 0；"
     "电脑偏慢、成片迟到时可以加到 0.01~0.02。"),
]

# 兼容：配置文件里可能还叫 `fret_strum_gap`，plan.py 自己会把它当
# `arpeggio_gap` 的同义词读；这两个键已不在界面上，但旧配置仍可正常读。
TIMING_ALIASES = {}

TIMING_DEFAULTS = {}
TIMING_META = {}
for (_k, _cn, _d, _u, _lo, _hi, _lv, _note) in TIMING_INFO_ALL:
    TIMING_DEFAULTS[_k] = _d
    TIMING_META[_k] = {"key": _k, "cn": _cn, "default": _d, "unit": _u,
                       "min": _lo, "max": _hi, "level": _lv, "note": _note}

# 从界面参数表移除、但编译核心仍会 `.get()` 读取的 3 个旧键：
# 只保留默认值，保证 `Timing()` / `plan.py` 不 KeyError，且输出与旧版一致。
LEGACY_TIMING_DEFAULTS = {
    # Kept only so old modules doing Timing().pluck_park_wait do not crash.
    "pluck_park_wait": 0.0,
    "select_pre_wait": 0.020,
    "arpeggio_gap": 0.020,
    "after_wheel_click_release": 0.015,
}
TIMING_DEFAULTS.update(LEGACY_TIMING_DEFAULTS)
TIMING_DEFAULTS.update(TIMING_PRESETS[DEFAULT_TIMING_PRESET])
for _k, _meta in TIMING_META.items():
    if _k in TIMING_DEFAULTS:
        _meta["default"] = TIMING_DEFAULTS[_k]

TIMING_KEYS = [row[0] for row in TIMING_INFO_ALL]      # 全部 18 个键（读写配置用）

# 界面只展示这 6 项（按 TIMING_UI_ORDER 的顺序）
TIMING_INFO = [row for row in TIMING_INFO_ALL if row[0] in TIMING_UI_ORDER]
TIMING_INFO.sort(key=lambda _row: TIMING_UI_ORDER.index(_row[0]))
TIMING_UI_KEYS = [row[0] for row in TIMING_INFO]

# 「按键与点选按住时长」在底层是 6 个键；界面上只留 pluck_hold 一个入口，
# 保存时把它的值同步写回下面这 6 项（各自按自己的允许范围钳制）。
PLUCK_MIRROR_KEYS = ("page_key_hold", "fret_click_hold", "select_click_hold",
                     "strum_click_hold", "page_turn_gap", "repluck_gap")


def expand_pluck_hold(values):
    """把界面上的 pluck_hold 同步铺开到 6 个底层键（不覆盖显式给出的键）。"""
    out = dict(values or {})
    if "pluck_hold" in out:
        v = out["pluck_hold"]
        for k in PLUCK_MIRROR_KEYS:
            out.setdefault(k, v)
    return out

# ── 「全部设为 0」时**保持不动**的项 ─────────────────────────────────────
# ① 翻页这一组（不参与归零）：
#   · page_key_hold             —— 翻页键按住时长
#   · page_turn_gap             —— 连按翻页之间的间隔
#   · after_page_key_release    —— 翻页键**松开**后多久根音才变（25ms）
#   · settle_after_page_turn    —— 翻页后等多久再演奏
ZERO_KEEP = ("page_key_hold", "page_turn_gap",
             "after_page_key_release",
             "settle_after_page_turn")

# ② **根本不是"等待"**的两条，归零会把功能弄坏，所以也不动：
#   · sustain_max —— 长按延音上限。设 0 = **长音一个都按不住**（延音全废）。
#   · sustain_min —— "多短的音就不长按"的判断门槛，不是等待。
NON_DELAY_KEYS = ("sustain_max", "sustain_min")

# 「恢复默认」= 回到 `TIMING_DEFAULTS` 全部（其中就包含 `MEASURED_KEYS` 那 8 个数）。
MEASURED_KEYS = ("fret_click_hold", "pluck_hold",
                 "select_click_hold", "strum_park_wait", "strum_click_hold")

def _clamp(key, value):
    """把用户写的值拉到允许范围里（写错一个量级也不至于把演奏搞垮）。"""
    meta = TIMING_META[key]
    try:
        v = float(value)
    except (TypeError, ValueError):
        return meta["default"]
    if v != v or v in (float("inf"), float("-inf")):     # NaN / inf
        return meta["default"]
    return max(meta["min"], min(meta["max"], v))

def timing_defaults():
    return dict(TIMING_DEFAULTS)

def clamp_timing(key, value):
    """公开版的范围钳制：任何"从外面塞进来"的值都先过这一道。"""
    if key not in TIMING_META:
        return value
    return _clamp(key, value)

def load_timing():
    """
    读延迟参数。**任何缺失/写坏的项都退回默认值**，绝不抛异常 ——
    配置文件坏掉不能让演奏器起不来。

    别名（`TIMING_ALIASES`）会被当成正式名字读，所以旧配置文件不会失效。
    """
    out = dict(TIMING_DEFAULTS)
    raw = None
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        raw = None
    if isinstance(raw, dict):
        got = raw.get("timing")
        if isinstance(got, dict):
            for k in TIMING_KEYS:
                if k in got:
                    out[k] = _clamp(k, got[k])
            for old, new in TIMING_ALIASES.items():          # 别名 → 正式名
                if old in got and new not in got:
                    out[new] = _clamp(new, got[old])
    return out

def save_timing(values, merge=True):
    """
    只写延迟参数（其余设置原样保留）。返回 True/False，**不抛异常**。
    merge=False 时先把整份 timing 清空重置为默认再写。
    """
    cur = load()
    t = dict(cur.get("timing") or {}) if merge else {}
    values = expand_pluck_hold(values)
    for k in TIMING_KEYS:
        if k in (values or {}):
            t[k] = _clamp(k, values[k])
    cur["timing"] = t
    return _write(cur)

def reset_timing():
    """恢复所有延迟参数的默认值（= `TIMING_DEFAULTS`），返回默认值 dict。"""
    cur = load()
    cur["timing"] = {}
    _write(cur)
    return timing_defaults()

def zero_all_timing(keep=None):
    """
    **一键全部设为 0（最快）** —— 把除"翻页那一组"和"不是等待的那两条"以外的
    所有延迟设成 0，写盘。

    返回 (改成 0 的项 dict, 原样保留的项 dict)。

    **保持不动**的两类（见 `ZERO_KEEP` / `NON_DELAY_KEYS`）：
      · 翻页那一组（含 25ms / 15ms 两条"松开后多久根音才变"）；
      · `sustain_max`（设 0 = 长音一个都按不住）与 `sustain_min`（不是等待）。
    这里**不判断好坏**，只是照做；界面上会提示"发现漏音就点『恢复默认』"。
    """
    keep = tuple((ZERO_KEEP if keep is None else keep)) + tuple(NON_DELAY_KEYS)
    cur = load()
    t = dict(cur.get("timing") or {})
    zeroed, kept = {}, {}
    for k in TIMING_KEYS:
        now = t.get(k, TIMING_DEFAULTS[k])
        if k in keep:
            kept[k] = now
        else:
            t[k] = 0.0
            zeroed[k] = 0.0
    cur["timing"] = t
    _write(cur)
    return zeroed, kept

def restore_measured_timing():
    """
    **一键恢复默认（最稳）** —— 回到 `TIMING_DEFAULTS`，写盘。

    = 把所有项恢复成 `TIMING_DEFAULTS`（其中就包含 `MEASURED_KEYS` 那 8 个数）。
    返回恢复后的 dict。
    """
    cur = load()
    cur["timing"] = {}
    _write(cur)
    return timing_defaults()

def rename_legacy_keys(mapping):
    """把别名的键换成正式名（读到旧配置文件时用）。"""
    out = dict(mapping or {})
    for old, new in TIMING_ALIASES.items():
        if old in out and new not in out:
            out[new] = out.pop(old)
    return out

def _write(data):
    try:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
        return True
    except Exception:
        return False

# ══════════════════════════════════════════════════════════════════════════
# 通用设置（老接口，签名没变）
# ══════════════════════════════════════════════════════════════════════════
def load():
    """Return the stored settings, falling back to defaults for anything missing."""
    data = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for k in DEFAULTS:
                if k in raw and k != "timing":
                    data[k] = raw[k]
    except FileNotFoundError:
        pass
    except Exception:
        # A corrupt file must never stop the program from starting.
        pass
    data["timing"] = load_timing()
    return data

def save(settings):
    """Write the given subset of settings back to disk. Never raises."""
    data = load()
    for k in DEFAULTS:
        if k in settings:
            data[k] = settings[k]
    # timing 单独合并（不能整块覆盖，否则界面只改一项会把其余项清掉）
    timing = data.get("timing") or {}
    got = expand_pluck_hold((settings or {}).get("timing"))
    if isinstance(got, dict):
        for k in TIMING_KEYS:
            if k in got:
                timing[k] = _clamp(k, got[k])
        for old, new in TIMING_ALIASES.items():
            if old in got and new not in got:
                timing[new] = _clamp(new, got[old])
    data["timing"] = dict((k, v) for k, v in timing.items() if k in TIMING_META)
    return _write(data)

def timing_table_text():
    """给文档/命令行看的一张中文表。"""
    lines = ["%-28s %-9s %-6s %-16s %s" % ("中文名", "默认值", "单位", "允许范围", "警告等级")]
    for k in TIMING_KEYS:
        m = TIMING_META[k]
        lines.append("%-28s %-9s %-6s %-16s %s"
                     % (m["cn"], m["default"], m["unit"],
                        "%s ~ %s" % (m["min"], m["max"]), m["level"]))
    return "\n".join(lines)

if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    print("配置文件：%s" % CONFIG_PATH)
    print(timing_table_text())
    print()
    print("当前生效值：")
    for k, v in load_timing().items():
        print("  %-28s = %s" % (k, v))
