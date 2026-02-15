"""Python bridge to the AIK kernel-mode keyboard driver.

Communicates with the driver via IOCTL (DeviceIoControl).
Falls back gracefully when the driver is not loaded.
"""

from __future__ import annotations

import ctypes
import logging
import struct
from ctypes import wintypes

log = logging.getLogger("aik.driver")

# ── Win32 constants ──────────────────────────────────────────────────────────
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

FILE_DEVICE_UNKNOWN = 0x00000022
METHOD_BUFFERED = 0
FILE_ANY_ACCESS = 0

AIK_IOCTL_INDEX = 0x800


def _ctl_code(dev: int, func: int, method: int, access: int) -> int:
    return (dev << 16) | (access << 14) | (func << 2) | method


IOCTL_AIK_PING = _ctl_code(FILE_DEVICE_UNKNOWN, AIK_IOCTL_INDEX + 0, METHOD_BUFFERED, FILE_ANY_ACCESS)
IOCTL_AIK_ECHO = _ctl_code(FILE_DEVICE_UNKNOWN, AIK_IOCTL_INDEX + 1, METHOD_BUFFERED, FILE_ANY_ACCESS)
IOCTL_AIK_INJECT_KEYS = _ctl_code(FILE_DEVICE_UNKNOWN, AIK_IOCTL_INDEX + 2, METHOD_BUFFERED, FILE_ANY_ACCESS)

# Key-event flags  (match KEYBOARD_INPUT_DATA.Flags)
KEY_MAKE = 0x0000
KEY_BREAK = 0x0001
KEY_E0 = 0x0002
KEY_E1 = 0x0004

# ── Win32 function signatures ────────────────────────────────────────────────
CreateFileW = kernel32.CreateFileW
CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
CreateFileW.restype = wintypes.HANDLE

DeviceIoControl = kernel32.DeviceIoControl
DeviceIoControl.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
    wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
]
DeviceIoControl.restype = wintypes.BOOL

CloseHandle = kernel32.CloseHandle
CloseHandle.argtypes = [wintypes.HANDLE]
CloseHandle.restype = wintypes.BOOL


# ── DriverBridge ─────────────────────────────────────────────────────────────
class DriverBridge:
    """Interface to the AIK kernel-mode keyboard driver."""

    DEVICE_PATH = r"\\.\AikKmdfIoctl"

    def __init__(self, device_path: str | None = None) -> None:
        self._path = device_path or self.DEVICE_PATH
        self._handle: int | None = None

    # ── lifecycle ──

    @property
    def is_open(self) -> bool:
        return self._handle is not None

    def open(self) -> bool:
        """Open a handle to the driver device.  Returns True on success."""
        if self._handle is not None:
            return True
        try:
            h = CreateFileW(
                self._path,
                GENERIC_READ | GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL,
                None,
            )
            if h == INVALID_HANDLE_VALUE:
                err = ctypes.get_last_error()
                log.debug("Cannot open driver %s (error %d)", self._path, err)
                return False
            self._handle = h
            log.info("Opened kernel driver at %s", self._path)
            return True
        except Exception as exc:
            log.debug("Failed to open driver: %s", exc)
            return False

    def close(self) -> None:
        if self._handle is not None:
            try:
                CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None

    # ── IOCTL helpers ──

    def ping(self) -> bool:
        """PING → PONG health-check."""
        if not self.is_open:
            return False
        try:
            result = self._ioctl(IOCTL_AIK_PING, b"", 64)
            return b"PONG" in result
        except Exception:
            return False

    def inject_scancodes(self, events: list[tuple[int, int]]) -> bool:
        """Send key events to the driver.

        *events* is a list of ``(scancode, flags)`` tuples.
        ``flags`` is a combination of ``KEY_MAKE``, ``KEY_BREAK``, ``KEY_E0``.
        """
        if not self.is_open or not events:
            return not events  # vacuously true when empty
        buf = b"".join(struct.pack("<HH", sc & 0xFFFF, fl & 0xFFFF) for sc, fl in events)
        try:
            self._ioctl(IOCTL_AIK_INJECT_KEYS, buf, 4)
            return True
        except Exception as exc:
            log.error("inject_scancodes failed: %s", exc)
            return False

    def inject_key_press(self, scancode: int, *, extended: bool = False) -> bool:
        """Press and release a single key."""
        flags_base = KEY_E0 if extended else 0
        return self.inject_scancodes([
            (scancode, flags_base | KEY_MAKE),
            (scancode, flags_base | KEY_BREAK),
        ])

    def inject_text(self, text: str) -> bool:
        """Type a string as scancode key-presses."""
        events: list[tuple[int, int]] = []
        for ch in text:
            entry = _CHAR_SC.get(ch)
            if entry is None:
                continue
            sc, shift = entry
            if shift:
                events.append((0x2A, KEY_MAKE))   # LShift down
            events.append((sc, KEY_MAKE))
            events.append((sc, KEY_BREAK))
            if shift:
                events.append((0x2A, KEY_BREAK))  # LShift up
        return self.inject_scancodes(events) if events else True

    # ── internal ──

    def _ioctl(self, code: int, in_data: bytes, out_size: int) -> bytes:
        in_buf = (ctypes.c_ubyte * max(1, len(in_data)))()
        for i, b in enumerate(in_data):
            in_buf[i] = b
        out_buf = (ctypes.c_ubyte * out_size)()
        returned = wintypes.DWORD(0)
        ok = DeviceIoControl(
            self._handle, code,
            ctypes.byref(in_buf), len(in_data),
            ctypes.byref(out_buf), out_size,
            ctypes.byref(returned), None,
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return bytes(out_buf[: returned.value])

    def __del__(self) -> None:
        self.close()

    # ── class-level probe ──

    @staticmethod
    def probe() -> bool:
        """Quick check: is the kernel driver loaded and responding?"""
        b = DriverBridge()
        try:
            if b.open():
                return b.ping()
        finally:
            b.close()
        return False


# ── Scan-code table ──────────────────────────────────────────────────────────
_CHAR_SC: dict[str, tuple[int, bool]] = {}


def _init() -> None:
    # Number row
    for i, c in enumerate("1234567890"):
        _CHAR_SC[c] = (0x02 + i, False)
    for i, c in enumerate("!@#$%^&*()"):
        _CHAR_SC[c] = (0x02 + i, True)

    # QWERTY rows
    for i, c in enumerate("qwertyuiop"):
        _CHAR_SC[c] = (0x10 + i, False)
        _CHAR_SC[c.upper()] = (0x10 + i, True)
    for i, c in enumerate("asdfghjkl"):
        _CHAR_SC[c] = (0x1E + i, False)
        _CHAR_SC[c.upper()] = (0x1E + i, True)
    for i, c in enumerate("zxcvbnm"):
        _CHAR_SC[c] = (0x2C + i, False)
        _CHAR_SC[c.upper()] = (0x2C + i, True)

    # Specials
    _CHAR_SC.update({
        " ":  (0x39, False), "\n": (0x1C, False), "\t": (0x0F, False),
        "-":  (0x0C, False), "=":  (0x0D, False), "_":  (0x0C, True),
        "+":  (0x0D, True),  "[":  (0x1A, False), "]":  (0x1B, False),
        "{":  (0x1A, True),  "}":  (0x1B, True),  "\\": (0x2B, False),
        "|":  (0x2B, True),  ";":  (0x27, False), "'":  (0x28, False),
        ":":  (0x27, True),  '"':  (0x28, True),  ",":  (0x33, False),
        ".":  (0x34, False), "/":  (0x35, False), "<":  (0x33, True),
        ">":  (0x34, True),  "?":  (0x35, True),  "`":  (0x29, False),
        "~":  (0x29, True),
    })


_init()
