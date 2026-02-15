from __future__ import annotations

import ctypes
import time
from ctypes import wintypes


INPUT_KEYBOARD = 1
INPUT_MOUSE = 0

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

MAPVK_VK_TO_VSC = 0

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_SendInput = _user32.SendInput
_SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
_SendInput.restype = wintypes.UINT

_MapVirtualKeyW = _user32.MapVirtualKeyW
_MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
_MapVirtualKeyW.restype = wintypes.UINT

_GetSystemMetrics = _user32.GetSystemMetrics
_GetSystemMetrics.argtypes = (ctypes.c_int,)
_GetSystemMetrics.restype = ctypes.c_int

SM_CXSCREEN = 0
SM_CYSCREEN = 1

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

_GetCursorPos = _user32.GetCursorPos
_GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
_GetCursorPos.restype = wintypes.BOOL


class InputInjector:
    def __init__(self, *, inter_key_delay_s: float = 0.01) -> None:
        self._delay = inter_key_delay_s

    def type_text(self, text: str) -> None:
        for ch in text:
            if ch == "\r":
                continue
            if ch == "\n":
                self.key_press("enter")
                continue
            if ch == "\t":
                self.key_press("tab")
                continue
            if ch == "\b":
                self.key_press("backspace")
                continue
            self._send_unicode(ch)
            if self._delay:
                time.sleep(self._delay)

    def key_press(self, key: str) -> None:
        vk = _vk_from_key_name(key)
        self._send_vk(vk, is_down=True)
        self._send_vk(vk, is_down=False)
        if self._delay:
            time.sleep(self._delay)

    def hotkey(self, keys: list[str]) -> None:
        if not keys:
            return
        if len(keys) == 1:
            self.key_press(keys[0])
            return

        vks = [_vk_from_key_name(k) for k in keys]
        mods, main = vks[:-1], vks[-1]

        for vk in mods:
            self._send_vk(vk, is_down=True)
        self._send_vk(main, is_down=True)
        self._send_vk(main, is_down=False)
        for vk in reversed(mods):
            self._send_vk(vk, is_down=False)
        if self._delay:
            time.sleep(self._delay)

    def mouse_move_normalized(self, x: float, y: float) -> None:
        # Absolute mouse expects 0..65535.
        x = min(1.0, max(0.0, float(x)))
        y = min(1.0, max(0.0, float(y)))
        abs_x = int(x * 65535)
        abs_y = int(y * 65535)
        mi = MOUSEINPUT(
            dx=abs_x,
            dy=abs_y,
            mouseData=0,
            dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
            time=0,
            dwExtraInfo=0,
        )
        inp = INPUT(type=INPUT_MOUSE, mi=mi)
        self._send_inputs([inp])
        if self._delay:
            time.sleep(self._delay)

    def mouse_move_smooth(self, x: float, y: float, *, steps: int = 12, step_delay_s: float = 0.004) -> None:
        """Move cursor smoothly to normalized (virtual-desktop) coords."""
        steps = max(1, min(int(steps), 60))

        # Current cursor position in normalized virtual desktop coordinates.
        cur = wintypes.POINT()
        if _GetCursorPos(ctypes.byref(cur)):
            # Convert pixels -> normalized virtual desktop.
            vx = _GetSystemMetrics(SM_XVIRTUALSCREEN)
            vy = _GetSystemMetrics(SM_YVIRTUALSCREEN)
            vw = _GetSystemMetrics(SM_CXVIRTUALSCREEN)
            vh = _GetSystemMetrics(SM_CYVIRTUALSCREEN)
            if vw > 0 and vh > 0:
                cx = (float(cur.x) - float(vx)) / float(vw)
                cy = (float(cur.y) - float(vy)) / float(vh)
            else:
                # Fallback to primary screen.
                w = _GetSystemMetrics(SM_CXSCREEN)
                h = _GetSystemMetrics(SM_CYSCREEN)
                if w > 0 and h > 0:
                    cx = float(cur.x) / float(w)
                    cy = float(cur.y) / float(h)
                else:
                    cx, cy = x, y
        else:
            cx, cy = x, y

        tx = min(1.0, max(0.0, float(x)))
        ty = min(1.0, max(0.0, float(y)))

        for i in range(1, steps + 1):
            nx = cx + (tx - cx) * (i / steps)
            ny = cy + (ty - cy) * (i / steps)
            self.mouse_move_normalized(nx, ny)
            if step_delay_s:
                time.sleep(step_delay_s)

    def mouse_button_down(self, button: str = "left") -> None:
        b = (button or "left").strip().lower()
        if b == "left":
            flag = MOUSEEVENTF_LEFTDOWN
        elif b == "right":
            flag = MOUSEEVENTF_RIGHTDOWN
        elif b == "middle":
            flag = MOUSEEVENTF_MIDDLEDOWN
        else:
            raise ValueError(f"Unsupported mouse button: {button!r}")
        self._send_inputs([INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=flag, time=0, dwExtraInfo=0))])

    def mouse_button_up(self, button: str = "left") -> None:
        b = (button or "left").strip().lower()
        if b == "left":
            flag = MOUSEEVENTF_LEFTUP
        elif b == "right":
            flag = MOUSEEVENTF_RIGHTUP
        elif b == "middle":
            flag = MOUSEEVENTF_MIDDLEUP
        else:
            raise ValueError(f"Unsupported mouse button: {button!r}")
        self._send_inputs([INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=flag, time=0, dwExtraInfo=0))])

    def mouse_drag_normalized(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        *,
        button: str = "left",
        steps: int = 24,
    ) -> None:
        """Drag from (x1,y1) to (x2,y2) in normalized virtual-desktop coords."""
        self.mouse_move_smooth(x1, y1, steps=max(6, steps // 2))
        time.sleep(0.01)
        self.mouse_button_down(button)
        time.sleep(0.01)
        self.mouse_move_smooth(x2, y2, steps=steps)
        time.sleep(0.01)
        self.mouse_button_up(button)

    def mouse_click(self, button: str = "left", *, clicks: int = 1) -> None:
        b = (button or "left").strip().lower()
        if b == "left":
            down, up = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
        elif b == "right":
            down, up = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
        elif b == "middle":
            down, up = MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP
        else:
            raise ValueError(f"Unsupported mouse button: {button!r}")

        clicks = max(1, min(int(clicks), 3))
        for _ in range(clicks):
            self._send_inputs(
                [
                    INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=down, time=0, dwExtraInfo=0)),
                    INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=up, time=0, dwExtraInfo=0)),
                ]
            )
            if self._delay:
                time.sleep(self._delay)

    def mouse_scroll(self, delta: int) -> None:
        d = int(delta)
        mi = MOUSEINPUT(dx=0, dy=0, mouseData=d, dwFlags=MOUSEEVENTF_WHEEL, time=0, dwExtraInfo=0)
        self._send_inputs([INPUT(type=INPUT_MOUSE, mi=mi)])
        if self._delay:
            time.sleep(self._delay)

    def _send_unicode(self, ch: str) -> None:
        code = ord(ch)
        down = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(wVk=0, wScan=code, dwFlags=KEYEVENTF_UNICODE, time=0, dwExtraInfo=0))
        up = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(wVk=0, wScan=code, dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, time=0, dwExtraInfo=0))
        self._send_inputs([down, up])

    def _send_vk(self, vk: int, *, is_down: bool) -> None:
        flags = 0
        if not is_down:
            flags |= KEYEVENTF_KEYUP
        if vk in _EXTENDED_VK:
            flags |= KEYEVENTF_EXTENDEDKEY
        scan = _MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
        inp = INPUT(
            type=INPUT_KEYBOARD,
            ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0),
        )
        self._send_inputs([inp])

    def _send_inputs(self, inputs: list[INPUT]) -> None:
        n = len(inputs)
        arr = (INPUT * n)(*inputs)
        sent = _SendInput(n, arr, ctypes.sizeof(INPUT))
        if sent != n:
            err = ctypes.get_last_error()
            raise OSError(f"SendInput sent {sent}/{n} inputs (GetLastError={err}).")


def _vk_from_key_name(key: str) -> int:
    k = (key or "").strip().lower()
    if not k:
        raise ValueError("Empty key name")

    # Single alnum maps directly for A-Z / 0-9.
    if len(k) == 1 and k.isalpha():
        return ord(k.upper())
    if len(k) == 1 and k.isdigit():
        return ord(k)

    if k.startswith("f") and k[1:].isdigit():
        n = int(k[1:])
        if 1 <= n <= 24:
            return 0x70 + (n - 1)

    try:
        return _VK[k]
    except KeyError as e:
        raise ValueError(f"Unsupported key name: {key!r}") from e


_VK = {
    "enter": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "delete": 0x2E,
    "space": 0x20,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "insert": 0x2D,
    "capslock": 0x14,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B,  # left win
    "lwin": 0x5B,
    "rwin": 0x5C,
    "pause": 0x13,
    "printscreen": 0x2C,
}

_EXTENDED_VK = {
    0x21,  # page up
    0x22,  # page down
    0x23,  # end
    0x24,  # home
    0x25,  # left
    0x26,  # up
    0x27,  # right
    0x28,  # down
    0x2D,  # insert
    0x2E,  # delete
    0x5B,  # lwin
    0x5C,  # rwin
}

