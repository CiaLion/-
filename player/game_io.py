# -*- coding: utf-8 -*-
"""game_io.py -- 游戏输入输出适配层。

把 `input_engine` 的裸函数、轮盘几何、截图能力包成**一个对象**，
让演奏器（performer.py）只跟这一个对象打交道。

三条硬约束：
* **窗口位置绝不缓存**。实测游戏窗口会在两次运行之间移动，缓存一次就全点歪。
  所以每次动作都重新 `metrics()`，绝不保存坐标。
* 轮盘几何（圆心/半径比例）**每次动作前从设置里刷新**，随时标定都能立刻生效。
* 截图是"从桌面 DC 抓可见像素"，所以**游戏必须在最前面**，抓之前先 focus + 等一下。

客户区比例一律用 `player_config.json` 里存的值（可标定）。
"""
import ctypes
from ctypes import wintypes
import time

import input_engine as ie
import player_config

# 轮盘几何的默认值（chord_map.py 里那份实测值），player_config 会覆盖它
DEFAULT_WHEEL_CENTER_X_RATIO = 0.6937
DEFAULT_WHEEL_CENTER_Y_RATIO = 0.4378
DEFAULT_WHEEL_RADIUS_RATIO = 0.2957

# 扇区角度：0=12点、1=2点、2=4点、3=6点、4=8点、5=10点（0° = 3 点钟方向，顺时针）
SECTOR_ANGLES = [270, 330, 30, 90, 150, 210]

# 点击落在半径的哪个位置。**必须与 chord_map.SECTOR_CLICK_FRACTION 一致**。
# 0.62 是实测值：粉色高亮带大约在 0.45~0.85 R，和弦名印在 0.68 R 附近，
# 点 0.62 R 稳稳落在亮带里（用户反馈 0.72 R 太靠外）。
SECTOR_CLICK_FRACTION = 0.62

# 扫弦时指针停哪儿：轮盘外的空白处。指针停在轮盘上会被当成"选扇区"。
STRUM_PARK = (0.10, 0.86)

class GameIO(object):
    """游戏窗口的键鼠/截图/几何接口。演奏器只认这个对象。"""

    def __init__(self, settings=None):
        self._settings = settings or player_config.load()
        self.refresh_settings()

    def refresh_settings(self):
        """重新读一次日志里的标定值（用户可能刚拖过悬浮窗）。"""
        s = self._settings or {}
        self.wheel_x_ratio = float(s.get("wheel_x_ratio", DEFAULT_WHEEL_CENTER_X_RATIO))
        self.wheel_y_ratio = float(s.get("wheel_y_ratio", DEFAULT_WHEEL_CENTER_Y_RATIO))
        self.wheel_radius_ratio = float(s.get("wheel_radius_ratio", DEFAULT_WHEEL_RADIUS_RATIO))

    def reload_from_disk(self):
        self._settings = player_config.load()
        self.refresh_settings()

    def set_wheel(self, cx_ratio, cy_ratio, r_ratio):
        self.wheel_x_ratio = float(cx_ratio)
        self.wheel_y_ratio = float(cy_ratio)
        self.wheel_radius_ratio = float(r_ratio)

    def find_window(self):
        return ie.find_game_window()

    def metrics(self):
        """(screen_x, screen_y, client_w, client_h)；没窗口就 None。每次现读。"""
        hwnd = ie.find_game_window()
        if not hwnd:
            return None
        return ie.get_window_metrics(hwnd)

    def focus(self):
        hwnd = ie.find_game_window()
        if not hwnd:
            return False
        return ie.focus_game_window(hwnd)

    def has_window(self):
        return bool(ie.find_game_window())

    def foreground_window(self):
        """当前前台窗口句柄（焦点守卫用）。"""
        try:
            return ctypes.windll.user32.GetForegroundWindow()
        except Exception:
            return None

    def move(self, x, y):
        ie.mouse_move_screen(int(x), int(y))

    def click(self, button='left', hold=0.02):
        ie.mouse_click(button, hold)

    def click_down(self, button='left'):
        """**只按下鼠标**（扫弦延音的开始）。实测确认按住会延音。"""
        return ie.mouse_down(button)

    def click_up(self, button='left'):
        """松开鼠标（结束扫弦延音）。"""
        return ie.mouse_up(button)

    def tap(self, key, hold=0.05):
        ie.tap_key(key, hold)

    def tap_key_down(self, key):
        """**只按下**翻页键（T/R），不 sleep。

        高精度时间轴（timeline_runner）用：翻页键的"按住多久"由时间轴里另一条
        松开动作负责，执行器本身不能在这里睡觉，否则后面全串味。
        返回 True/False（键名不认识返回 False）。
        """
        sc = ie.SCANCODES.get(str(key).upper())
        if sc is None:
            return False
        ie.press_scancode(sc)
        return True

    def tap_key_up(self, key):
        """**只松开**翻页键（T/R），与 `tap_key_down` 配套。"""
        sc = ie.SCANCODES.get(str(key).upper())
        if sc is None:
            return False
        ie.release_scancode(sc)
        return True

    def pluck_key(self, string, hold=0.12):
        """拨弦走**虚拟键码**路径（scancode 那条路游戏不认，见 input_engine 注释）。

        hold = 按住多久。**长按会延音**（实测：最长约 4 秒、带渐弱），
        所以长音就靠这里按住不放 —— 见 `Performer.pluck(..., hold=…)`。
        """
        return ie.tap_number_key(int(string), float(hold))

    def pluck_key_down(self, string):
        """只按下数字键（长按延音的开始）。"""
        return ie.number_key_down(int(string))

    def pluck_key_up(self, string):
        """松开数字键（结束延音）。"""
        return ie.number_key_up(int(string))

    def is_key_down(self, vk):
        return ie.is_key_down(vk)

    def wheel_center(self, metrics=None):
        m = metrics or self.metrics()
        if not m:
            return None
        sx, sy, cw, ch = m
        return (sx + cw * self.wheel_x_ratio, sy + ch * self.wheel_y_ratio,
                ch * self.wheel_radius_ratio)

    def sector_point(self, sector, metrics=None):
        """第 sector 个扇区的点击点（屏幕坐标）。"""
        c = self.wheel_center(metrics)
        if not c:
            return None
        cx, cy, r = c
        import math
        ang = math.radians(SECTOR_ANGLES[int(sector) % 6])
        return (cx + r * SECTOR_CLICK_FRACTION * math.cos(ang),
                cy + r * SECTOR_CLICK_FRACTION * math.sin(ang))

    def park_point(self, metrics=None):
        m = metrics or self.metrics()
        if not m:
            return None
        sx, sy, cw, ch = m
        return (sx + cw * STRUM_PARK[0], sy + ch * STRUM_PARK[1])

    def screenshot(self):
        """客户区截图（PIL Image）。游戏不在最前面时抓到的是别的窗口。"""
        try:
            from PIL import Image

            class BMIH(ctypes.Structure):
                _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                            ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                            ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                            ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                            ("biClrImportant", wintypes.DWORD)]

            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            hwnd = ie.find_game_window()
            if not hwnd:
                return None
            rect = wintypes.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(rect))
            w, h = rect.right, rect.bottom
            if w <= 0 or h <= 0:
                return None
            pt = wintypes.POINT(0, 0)
            user32.ClientToScreen(hwnd, ctypes.byref(pt))
            hdesk = user32.GetDesktopWindow()
            hdc = user32.GetDC(hdesk)
            memdc = gdi32.CreateCompatibleDC(hdc)
            bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
            gdi32.SelectObject(memdc, bmp)
            gdi32.BitBlt(memdc, 0, 0, w, h, hdc, pt.x, pt.y, 0x00CC0020)
            bi = BMIH()
            bi.biSize = ctypes.sizeof(BMIH)
            bi.biWidth = w
            bi.biHeight = -h
            bi.biPlanes = 1
            bi.biBitCount = 32
            buf = ctypes.create_string_buffer(w * h * 4)
            gdi32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(memdc)
            user32.ReleaseDC(hdesk, hdc)
            return Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
        except Exception:
            return None

    def pink_sector(self, img=None):
        """
        看轮盘上哪个扇区是粉色高亮（回读点击有没有生效，与音高无关）。

        返回扇区号或 None。**注意**：品格模式和弦模式的画面只有字不一样，
        所以这个函数**不能用来判模式**，只能用来确认"点击落到哪个扇区"。
        """
        import math
        img = img if img is not None else self.screenshot()
        if img is None:
            return None
        m = self.metrics()
        if not m:
            return None
        sx, sy, cw, ch = m
        c = self.wheel_center(m)
        if not c:
            return None
        cx, cy, r = c
        px = img.load()
        best_i, best_v = None, 0.0
        for i, ang in enumerate(SECTOR_ANGLES):
            t = math.radians(ang)
            # 图是客户区，屏幕坐标要减掉客户区原点
            tx = cx - sx + r * 0.72 * math.cos(t)
            ty = cy - sy + r * 0.72 * math.sin(t)
            hits = tot = 0
            for dy in range(-24, 25, 4):
                for dx in range(-24, 25, 4):
                    x, y = int(tx + dx), int(ty + dy)
                    if 0 <= x < img.width and 0 <= y < img.height:
                        rr, gg, bb = px[x, y]
                        tot += 1
                        if rr > 120 and bb > 60 and (rr - gg) > 50:
                            hits += 1
            v = hits / max(1, tot)
            if v > best_v:
                best_i, best_v = i, v
        return best_i if best_v > 0.05 else None

    def sleep(self, sec):
        time.sleep(sec)

    def is_admin(self):
        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False
