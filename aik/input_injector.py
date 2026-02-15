from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .driver_bridge import DriverBridge

log = logging.getLogger("aik.input_injector")

INPUT_KEYBOARD = 1

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

MAPVK_VK_TO_VSC = 0

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


class InputInjector:
    """
    Input injection with optional kernel driver support.
    
    By default uses user-mode SendInput API. If a DriverBridge is provided
    and connected, can use an optional kernel driver for lower-level injection.
    """
    
    def __init__(
        self, 
        *, 
        inter_key_delay_s: float = 0.01,
        driver_bridge: "DriverBridge | None" = None,
        prefer_driver: bool = False,
    ) -> None:
        self._delay = inter_key_delay_s
        self._driver = driver_bridge
        self._prefer_driver = prefer_driver
        
        if self._driver and self._prefer_driver:
            log.info("InputInjector configured with kernel driver preference")

    @property
    def using_driver(self) -> bool:
        """Returns True if driver injection is available and preferred."""
        return self._prefer_driver and self._driver is not None and self._driver.connected

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
    "grave": 0xC0,
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

