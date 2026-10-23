import ctypes
from ctypes import wintypes
import time

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

def make_dpi_aware():
    """
    Tell Windows this process knows about display scaling.

    Without this, on a 125%/150% scaled display GetClientRect/ClientToScreen return
    scaled numbers while SetCursorPos uses real pixels, so mouse clicks land off target.
    Must be the very first thing the program does.
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_AWARE
        return True
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
        return True
    except Exception:
        return False

# Hardware scancodes accepted by Unreal Engine
SCANCODES = {
    # Number row (High Octave in Piano Mode / Strings 1-6 in Guitar Mode)
    '1': 0x02, '2': 0x03, '3': 0x04, '4': 0x05, '5': 0x06,
    '6': 0x07, '7': 0x08, '8': 0x09, '9': 0x0A, '0': 0x0B,
    '-': 0x0C, '=': 0x0D,
    
    # QWERTY row (Mid Octave in Piano Mode / Page R & T in Guitar Mode)
    'Q': 0x10, 'W': 0x11, 'E': 0x12, 'R': 0x13, 'T': 0x14,
    'Y': 0x15, 'U': 0x16, 'I': 0x17, 'O': 0x18, 'P': 0x19,
    '[': 0x1A, ']': 0x1B,
    
    # ASDF row (Low Octave in Piano Mode)
    'A': 0x1E, 'S': 0x1F, 'D': 0x20, 'F': 0x21, 'G': 0x22,
    'H': 0x23, 'J': 0x24, 'K': 0x25, 'L': 0x26, ';': 0x27,
    "'": 0x28, 'Enter': 0x1C,
    
    # Function keys & Controls
    'Z': 0x2C, 'V': 0x2F, 'Space': 0x39, 'Esc': 0x01
}

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_ulonglong)
    ]

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_ulonglong)
    ]

class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD)
    ]

class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("ki", KEYBDINPUT),
        ("mi", MOUSEINPUT),
        ("hi", HARDWAREINPUT)
    ]

class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", _INPUT_UNION)
    ]

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_ABSOLUTE = 0x8000

# Virtual key codes for hotkeys
VK_F8 = 0x77
VK_F9 = 0x78
VK_F12 = 0x7B

def is_key_down(vk_code):
    """Check if a virtual key is currently pressed."""
    return bool(user32.GetAsyncKeyState(vk_code) & 0x8000)

def _send_key(scancode, up):
    """
    One keyboard event via SendInput, using the hardware scancode.

    dwExtraInfo is deliberately 0: the previous version passed the address of a
    temporary Python object that was already freed by the time Windows read it,
    which is undefined behaviour and can make a title/anti-cheat drop the key.
    """
    vk = user32.MapVirtualKeyW(scancode, 1)  # MAPVK_VSC_TO_VK
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.union.ki.wVk = vk
    inp.union.ki.wScan = scancode
    inp.union.ki.dwFlags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    inp.union.ki.time = 0
    inp.union.ki.dwExtraInfo = 0
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

def press_scancode(scancode):
    _send_key(scancode, False)

def release_scancode(scancode):
    _send_key(scancode, True)

def tap_key(key_str, duration=0.05):
    scancode = SCANCODES.get(key_str)
    if scancode:
        press_scancode(scancode)
        time.sleep(duration)
        release_scancode(scancode)

# Virtual-key codes for the number row. Sending the plain virtual key (without
# KEYEVENTF_SCANCODE) is a *different* input event than the scancode one: with
# that flag set, Windows ignores wVk entirely and delivers only the scan code.
# Page turns (R/T) work through the scan-code path, but the string plucks (1-6)
# do not, even though pressing those keys by hand works. So plucking uses this
# path instead.
VK_NUMBERS = {str(d): 0x30 + d for d in range(1, 10)}       # '1'..'9'
VK_ZERO = 0x30

def _send_vk(vk, up):
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.union.ki.wVk = vk
    inp.union.ki.wScan = 0
    inp.union.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
    inp.union.ki.time = 0
    inp.union.ki.dwExtraInfo = 0
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

def tap_number_key(digit, duration=0.12):
    """
    Press a number-row key by virtual key code.

    Returns True when the key was sent. A longer hold than the page-turn keys:
    the game seems to need the key down across a frame to register a pluck, and
    a 50 ms tap can fall between two frames at 30 fps.
    """
    vk = VK_NUMBERS.get(str(digit))
    if vk is None:
        return False
    _send_vk(vk, False)
    time.sleep(duration)
    _send_vk(vk, True)
    return True

def number_key_vk(digit):
    """数字键 1~6 的虚拟键码（拨弦用）；不合法返回 None。"""
    return VK_NUMBERS.get(str(digit))

def number_key_down(digit):
    """**只按下、不松开** —— 长按延音用（用户 2026-10-16 实测：游戏内长按有延音，
    能到 4 秒，带渐弱）。配套 `number_key_up`。"""
    vk = VK_NUMBERS.get(str(digit))
    if vk is None:
        return False
    _send_vk(vk, False)
    return True

def number_key_up(digit):
    """松开数字键（结束长按延音）。"""
    vk = VK_NUMBERS.get(str(digit))
    if vk is None:
        return False
    _send_vk(vk, True)
    return True

def mouse_move_screen(screen_x, screen_y):
    """Move cursor to absolute screen coordinates."""
    user32.SetCursorPos(int(screen_x), int(screen_y))

def _send_mouse(button, up):
    """一个鼠标按下/松开事件（SendInput）。button = 'left' / 'right'。"""
    if button == 'right':
        flag = MOUSEEVENTF_RIGHTUP if up else MOUSEEVENTF_RIGHTDOWN
    else:
        flag = MOUSEEVENTF_LEFTUP if up else MOUSEEVENTF_LEFTDOWN
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.union.mi.dwFlags = flag
    inp.union.mi.dwExtraInfo = 0
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

def mouse_down(button='left'):
    """
    **只按下、不松开** —— 扫弦延音用。

    用户 2026-10-16 在游戏里实测确认：和弦模式下鼠标左/右键扫弦，
    **按住不放会一直响（带渐弱）**。所以扫弦延音就是"按下 → 等 → 松开"。
    配套 `mouse_up`。
    """
    _send_mouse(button, False)
    return True

def mouse_up(button='left'):
    """松开鼠标键（结束扫弦延音）。"""
    _send_mouse(button, True)
    return True

def mouse_click(button='left', duration=0.03):
    """Send left or right mouse click（点一下）。"""
    _send_mouse(button, False)
    time.sleep(duration)
    _send_mouse(button, True)

class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

def find_game_window():
    """Find the 异环 (HTGame.exe) game window handle."""
    found_hwnd = None
    
    hdesk = user32.OpenInputDesktop(0, False, 0x01ff)
    if hdesk:
        user32.SetThreadDesktop(hdesk)
        
    CMPFUNC = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, wintypes.LPARAM)
    
    def enum_proc(hwnd, lParam):
        nonlocal found_hwnd
        if not user32.IsWindowVisible(hwnd):
            return 1
            
        cls_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls_buf, 256)
        cls_name = cls_buf.value
        
        # Unreal Engine game window
        if cls_name == "UnrealWindow":
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            title = buf.value
            
            # Check process name to be certain it's HTGame.exe
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            hProc = kernel32.OpenProcess(0x1000, False, pid.value) # PROCESS_QUERY_LIMITED_INFORMATION
            if hProc:
                path_buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                kernel32.QueryFullProcessImageNameW(hProc, 0, path_buf, ctypes.byref(size))
                kernel32.CloseHandle(hProc)
                proc_path = path_buf.value
                if "HTGame" in proc_path or "NTE" in proc_path or "异环" in title:
                    found_hwnd = hwnd
                    return 0
            elif "异环" in title:
                found_hwnd = hwnd
                return 0
        return 1

    user32.EnumDesktopWindows(hdesk if hdesk else 0, CMPFUNC(enum_proc), 0)
    return found_hwnd

def get_window_metrics(hwnd):
    """
    Get the client rectangle and its screen position.
    Returns: (screen_x, screen_y, client_width, client_height)
    """
    if not hwnd or not user32.IsWindow(hwnd):
        return None
    
    client_rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(client_rect))
    
    pt = POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    
    cw = client_rect.right - client_rect.left
    ch = client_rect.bottom - client_rect.top
    
    return pt.x, pt.y, cw, ch

def focus_game_window(hwnd):
    """Bring the game window to foreground."""
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9) # SW_RESTORE
    
    # Bypass Windows LockSetForegroundWindow restriction
    user32.keybd_event(0x12, 0, 0, 0) # Alt down
    user32.SetForegroundWindow(hwnd)
    user32.keybd_event(0x12, 0, 2, 0) # Alt up
    time.sleep(0.15)
    return True
