# -*- coding: utf-8 -*-
"""timeline_runner.py -- 《异键演奏》**执行器**：照着时间轴一条一条卡点发出去。

════════════════════════════════════════════════════════════════════════════
它只管三件事，别的事一概不做：

  ① **把 Windows 的时钟精度调到 1 毫秒**（`timeBeginPeriod(1)`）。
     不调的话 `time.sleep` 的粒度是 15.6 毫秒，快歌必然糊。
  ② **绝对时刻卡点**：`target = t0 + 事件时刻 / 速度`，每次都从同一个零点算，
     所以**不会累积漂移**。离得远就 `sleep`，剩不到 2 毫秒就自旋空转。
  ③ **所有动作都是"按一下就返回"**：不在这里 sleep。翻页键的按住时长、拨弦的
     按住时长、扫弦的按住时长，**都是时间轴里另外排好的松开动作**。
     —— 这是整套东西能精确到毫秒的前提。

════════════════════════════════════════════════════════════════════════════
安全底线（任何一条出问题都不许把按键留在游戏里）：

  * 停止 / 报错 / 跑完 → `release_all()` 松开所有按住的键和鼠标；
  * 每一轮循环都检查"停止"标记，最多 1 毫秒就响应；
  * 游戏丢了前台 → 立即松键并暂停（绝不强抢焦点）。
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FOCUS_CHECK_INTERVAL = 0.04     # 每 40ms 看一次前台窗口（保证失焦 <50ms 内松键）
SPIN_WINDOW = 0.003             # 剩 3 毫秒以内开始自旋
SLEEP_MARGIN = 0.002            # sleep 少睡 2 毫秒，免得睡过头

class WinTimer(object):
    """把 Windows 定时器精度提到 1 毫秒（进入 with 时开，退出时还原）。"""

    def __init__(self, ms=1):
        self.ms = int(ms)
        self._opened = False

    def __enter__(self):
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(self.ms)
            self._opened = True
        except Exception:
            self._opened = False
        return self

    def __exit__(self, *exc):
        if self._opened:
            try:
                import ctypes
                ctypes.windll.winmm.timeEndPeriod(self.ms)
            except Exception:
                pass
        return False

def wait_until(target, stop_flag=None):
    """等到 `target`（perf_counter 的绝对时刻）。返回 True = 等到了，
    False = 中途被叫停。剩余 3 毫秒内改用自旋，保证误差 < 1 毫秒。"""
    while True:
        if stop_flag is not None and stop_flag():
            return False
        rem = target - time.perf_counter()
        if rem <= 0:
            return True
        if rem > SPIN_WINDOW:
            time.sleep(rem - SLEEP_MARGIN)
        # 剩下的两毫秒：自旋，不 sleep（sleep 的粒度到不了这么细）

def wait_next_5s(stop_flag=None, log=None):
    """等到"系统时间刚好是整 5 秒"的那一刻（双人同步启动用，计划 2.3）。

    两台机器各自等自己的整 5 秒，就能做到毫秒级同步，不需要联网。
    返回 perf_counter 的时刻；被叫停返回 None。
    """
    now = time.time()
    target_wall = (int(now / 5.0) + 1) * 5.0
    if target_wall - now > 4.9:          # 刚好卡在边界上：别等整整 5 秒
        target_wall -= 5.0
    if log:
        log("等待整 5 秒同步点（还有 %.2f 秒）……" % (target_wall - time.time()))
    while True:
        if stop_flag is not None and stop_flag():
            return None
        rem = target_wall - time.time()
        if rem <= 0:
            break
        if rem > 0.05:
            time.sleep(min(rem - 0.02, 0.2))
        # 最后 50 毫秒自旋，靠真实时间判定
    return time.perf_counter()

def reschedule_for_speed(timeline, speed, mechanical_max=0.06):
    """按倍速缩放时间轴，但**不压缩**硬件按住时长。

    直接 t/speed 会把 page_key_hold(28ms)、after_page_key_release(35ms)
    一起除以倍速，1.5x 下只剩 18ms，低于游戏帧采样就会吞键。
    这里只缩放“乐谱间隔”：按下→松开之间原本 <=60ms 的机械间隙（拨弦/翻页/
    点击）保持恒定物理毫秒数；更长的时值（延音）按倍速正常压缩。
    """
    speed = max(0.1, float(speed or 1.0))
    if abs(speed - 1.0) <= 1e-9:
        return dict(timeline or {})
    src = list((timeline or {}).get("events") or [])
    out = []
    queues = {}
    last_t = 0.0
    for ev in src:
        item = dict(ev)
        op = item.get("op")
        orig = float(item.get("t") or 0.0)
        t = orig / speed
        fam = target = None
        if op in ("key_down", "mouse_down"):
            fam = "key" if op == "key_down" else "mouse"
            target = item.get("key") if fam == "key" else item.get("btn")
        elif op in ("key_up", "mouse_up"):
            fam = "key" if op == "key_up" else "mouse"
            target = item.get("key") if fam == "key" else item.get("btn")
            q = queues.get((fam, target))
            if q:
                down_orig, down_out = q.pop(0)
                gap = orig - down_orig
                if 0.0 < gap <= mechanical_max:
                    t = max(t, down_out + gap)
        item["t"] = round(max(t, last_t), 6)
        last_t = item["t"]
        if op in ("key_down", "mouse_down"):
            # 记录“真正发出”的时刻，供配对松开时保持机械间隔
            queues.setdefault((fam, target), []).append([orig, last_t])
        out.append(item)
    result = dict(timeline or {})
    result["events"] = out
    result["speed"] = speed
    return result


class TimelineRunner(object):
    """照着编译好的时间轴演奏。

    参数
    ----
    io      : `game_io.GameIO`（真演奏）或任何有同名方法的假对象（离线自检）
    log     : 收一行文字的日志回调
    """

    def __init__(self, io, log=None, on_progress=None,
                 is_game_foreground=None, on_focus_lost=None,
                 on_pause_state_changed=None):
        self.io = io
        self._log = log or (lambda m: None)
        self._on_progress = on_progress
        self._is_game_foreground = is_game_foreground
        self._on_focus_lost = on_focus_lost
        # 任何原因引起的暂停/恢复都通知外部（录音器据此同步暂停）
        self._on_pause_state_changed = on_pause_state_changed
        self.stats = {}
        self.paused = False
        self._held_keys = set()
        self._held_mouse = set()

    def log(self, msg):
        self._log(msg)

    def _press_key(self, key):
        key = str(key)
        if key.isdigit():
            self.io.pluck_key_down(key)
        else:
            self.io.tap_key_down(key)
        self._held_keys.add(key)

    def _release_key(self, key):
        key = str(key)
        if key not in self._held_keys:
            return
        self._held_keys.discard(key)
        try:
            if key.isdigit():
                self.io.pluck_key_up(key)
            else:
                self.io.tap_key_up(key)
        except Exception:
            pass

    def _press_mouse(self, btn):
        self.io.click_down(btn)
        self._held_mouse.add(btn)

    def _release_mouse(self, btn):
        if btn not in self._held_mouse:
            return
        self._held_mouse.discard(btn)
        try:
            self.io.click_up(btn)
        except Exception:
            pass

    def pause(self, reason="手动暂停"):
        """立即松键并进入暂停（可被 F8 恢复）。"""
        self.release_all(reason)
        self.paused = True
        if self._on_focus_lost is not None:
            try:
                self._on_focus_lost()
            except Exception:
                pass
        self._notify_pause(True, reason)

    def _notify_pause(self, paused, reason):
        """把暂停状态广播给外部（录音器/界面）。**绝不因为回调出错而影响演奏。**"""
        fn = self._on_pause_state_changed
        if fn is None:
            return
        try:
            fn(bool(paused), str(reason or ""))
        except Exception:
            pass

    def resume(self):
        """恢复播放前先检查前台：不在游戏里就拒绝，绝不抢焦点。"""
        if self._is_game_foreground is not None:
            try:
                if not bool(self._is_game_foreground()):
                    return False
            except Exception:
                return False
        self.paused = False
        self._notify_pause(False, "继续")
        return True

    def _alt_or_win_down(self):
        """用户是否正按着 Alt / 左 Win / 右 Win。"""
        fn = getattr(self.io, "is_key_down", None)
        if fn is None:
            return False
        for vk in (0x12, 0x5B, 0x5C):
            try:
                if fn(vk):
                    return True
            except Exception:
                pass
        return False

    def release_all(self, reason=""):
        """松开所有按住的键和鼠标键（停止/报错/结束都要调）。"""
        n = len(self._held_keys) + len(self._held_mouse)
        for k in list(self._held_keys):
            self._release_key(k)
        for b in list(self._held_mouse):
            self._release_mouse(b)
        if n and reason:
            self.log("已松开 %d 个按住的通道（%s）" % (n, reason))
        return n

    def run(self, timeline, speed=1.0, sync_start=False, stop_flag=None,
            pause_flag=None, focus_guard=True, fast=False):
        """演奏一份时间轴。返回统计 dict。

        `stop_flag()`  返回 True = 停下来（F12）
        `pause_flag()` 返回 True = 暂停（暂停不吃时间，恢复后接着走）
        `fast=True`    不卡点（离线自检用，几秒跑完整首）
        """
        events = list((timeline or {}).get("events") or [])
        if not events:
            self.log("时间轴是空的，没有可演奏的东西")
            return {"events": 0}
        speed = max(0.1, float(speed or 1.0))
        if abs(speed - 1.0) > 1e-6:
            # 只压缩乐谱间隔；按住/翻页等硬件耗时保持原始物理毫秒数
            timeline = reschedule_for_speed(timeline, speed)
            events = list(timeline.get("events") or [])
            speed = 1.0

        if focus_guard and hasattr(self.io, "has_window"):
            if not self.io.has_window():
                self.log("⚠ 找不到游戏窗口，先启动《异环》")
                return {"events": 0, "error": "没有游戏窗口"}
            self.io.focus()
            time.sleep(1.0)          # 实测：切换动画期间点击会点错
        game_hwnd = None
        if focus_guard and hasattr(self.io, "find_window"):
            try:
                game_hwnd = self.io.find_window()
            except Exception:
                game_hwnd = None
        fg_func = self._is_game_foreground
        if fg_func is None and game_hwnd is not None:
            def fg_func():
                try:
                    return self.io.foreground_window() == game_hwnd
                except Exception:
                    return False
        if fg_func is not None:
            self._is_game_foreground = fg_func

        self._focus_lost_count = 0
        watch_stop = threading.Event()

        def _watch_focus():
            while not watch_stop.is_set():
                try:
                    ok = bool(fg_func())
                except Exception:
                    ok = False
                if not ok and not self.paused:
                    self.pause("检测到切出游戏")
                    self._focus_lost_count += 1
                    self.log("⚠️ 检测到切出游戏窗口，已自动释放全部按键并暂停"
                             "（切回游戏后按 F8 继续）")
                time.sleep(FOCUS_CHECK_INTERVAL)

        if focus_guard and fg_func is not None:
            threading.Thread(target=_watch_focus, daemon=True).start()

        self.stats = {"events": len(events), "late": 0, "max_late": 0.0,
                      "late_list": [], "elapsed": 0.0, "speed": speed,
                      "sync_start": bool(sync_start)}
        late_count, max_late, late_list = 0, 0.0, []
        last_focus = 0.0
        lost_focus = 0
        paused_total = 0.0

        with WinTimer(1):
            if sync_start and not fast:
                base = wait_next_5s(stop_flag, self.log)
                if base is None:
                    self.release_all("被停止")
                    return self.stats
            else:
                base = time.perf_counter()

            idx = 0
            try:
                while idx < len(events):
                    if stop_flag is not None and stop_flag():
                        break
                    # 暂停：不吃时间
                    if pause_flag is not None and pause_flag():
                        p0 = time.perf_counter()
                        while pause_flag() and not (stop_flag and stop_flag()):
                            time.sleep(0.01)
                        paused_total += time.perf_counter() - p0
                        base += time.perf_counter() - p0

                    ev = events[idx]
                    target = base + float(ev["t"]) / speed
                    if not fast:
                        if not wait_until(target, stop_flag):
                            break

                    now = time.perf_counter()
                    if now - target > 0.002:
                        late_count += 1
                        d = now - target
                        if len(late_list) < 50:
                            late_list.append(round(d, 4))
                        if d > max_late:
                            max_late = d

                    # 任何动作发出前最后一刻：Alt/Win 按住或已切出 → 当场不发
                    if focus_guard and (self._alt_or_win_down()
                                        or (fg_func is not None and not bool(fg_func()))):
                        self.pause("检测到 Alt/Win 或切出游戏")
                        lost_focus += 1
                        idx += 1
                        continue

                    try:
                        self._do(ev)
                    except Exception as exc:
                        self.log("动作失败（%s）：%s" % (ev.get("op"), exc))

                    idx += 1
                    if self._on_progress and (idx % 10 == 0 or idx == len(events)):
                        try:
                            self._on_progress(min(now - base, float(ev["t"]) / speed),
                                              float(events[-1]["t"]) / speed, idx)
                        except Exception:
                            pass
            finally:
                watch_stop.set()
                self.release_all("演奏结束")

        lost_focus += int(getattr(self, "_focus_lost_count", 0))
        self.stats.update({"late": late_count, "max_late": round(max_late, 4),
                           "late_list": late_list,
                           "elapsed": round(time.perf_counter() - base, 3),
                           "lost_focus": lost_focus, "paused": round(paused_total, 3)})
        if late_count:
            self.log("演奏结束：%d 条动作迟到（最多 %.0f 毫秒）" % (late_count, max_late * 1000))
        else:
            self.log("演奏结束：%d 条动作全部准点" % len(events))
        return self.stats

    def _do(self, ev):
        """执行一条硬件动作（**立即返回**，不 sleep）。"""
        op = ev.get("op")
        if op == "move_sector":
            m = self.io.metrics()
            if not m:
                return
            p = self.io.sector_point(int(ev["sector"]), m)
            if p:
                self.io.move(p[0], p[1])
        elif op == "move_park":
            m = self.io.metrics()
            if not m:
                return
            p = self.io.park_point(m)
            if p:
                self.io.move(p[0], p[1])
        elif op == "mouse_down":
            self._press_mouse(ev.get("btn", "left"))
        elif op == "mouse_up":
            self._release_mouse(ev.get("btn", "left"))
        elif op == "key_down":
            self._press_key(ev.get("key"))
        elif op == "key_up":
            self._release_key(ev.get("key"))
        else:
            self.log("时间轴里有个不认识的动件：%r（已跳过）" % (op,))

# ══════════════════════════════════════════════════════════════════════════
#  命令行：不碰游戏，只把时间轴"空跑"一遍（看动作序列对不对）
# ══════════════════════════════════════════════════════════════════════════
class _DryIO(object):
    """假 io：把动作记下来，不真的发键鼠。"""

    def __init__(self):
        self.calls = []

    def metrics(self):
        return (0, 0, 1280, 720)

    def sector_point(self, sector, metrics=None):
        return (100 + 10 * int(sector), 200)

    def park_point(self, metrics=None):
        return (128, 619)

    def move(self, x, y):
        self.calls.append(("move", x, y))

    def pluck_key_down(self, s):
        self.calls.append(("key_down", str(s)))

    def pluck_key_up(self, s):
        self.calls.append(("key_up", str(s)))

    def tap_key_down(self, k):
        self.calls.append(("key_down", str(k)))

    def tap_key_up(self, k):
        self.calls.append(("key_up", str(k)))

    def click_down(self, btn):
        self.calls.append(("mouse_down", btn))

    def click_up(self, btn):
        self.calls.append(("mouse_up", btn))

def main():
    import json
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        print("用法：python timeline_runner.py <时间轴.json> [--dry]")
        return 2
    path = args[0]
    dry = "--dry" in args
    with open(path, encoding="utf-8") as f:
        tl = json.load(f)
    io = _DryIO() if dry else __import__("game_io").GameIO()
    runner = TimelineRunner(io, log=print)
    st = runner.run(tl, speed=1.0, sync_start=False, fast=dry)
    print("动作 %d 条，迟到 %d 条，最大迟到 %.1f 毫秒"
          % (st.get("events", 0), st.get("late", 0), (st.get("max_late") or 0) * 1000))
    if dry:
        print("前 12 个动作：%s" % (io.calls[:12],))
    return 0

if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.exit(main())
