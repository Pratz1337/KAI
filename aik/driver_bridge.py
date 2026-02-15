"""
Kernel Driver Bridge for AIK

Provides Python interface to communicate with the AikKmdfIoctl kernel driver
for low-level scancode injection that bypasses UIPI restrictions.

Usage:
    bridge = DriverBridge()
    if bridge.connect():
        bridge.inject_scancode(0x1E, is_down=True)   # 'A' key down
        bridge.inject_scancode(0x1E, is_down=False)  # 'A' key up
        bridge.disconnect()
"""

from __future__ import annotations

import ctypes
import logging
import struct
from ctypes import wintypes
from typing import List, Tuple

log = logging.getLogger("aik.driver_bridge")

# Windows API constants
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

# IOCTL constants (must match Public.h)
FILE_DEVICE_UNKNOWN = 0x00000022
METHOD_BUFFERED = 0
FILE_ANY_ACCESS = 0

AIK_IOCTL_INDEX = 0x800

# Scancode flags (must match Public.h)
AIK_KEY_DOWN = 0x00
AIK_KEY_UP = 0x01
AIK_KEY_EXTENDED = 0x02

AIK_MAX_SCANCODES = 64


def _ctl_code(device_type: int, function: int, method: int, access: int) -> int:
    """Generate Windows IOCTL code."""
    return (device_type << 16) | (access << 14) | (function << 2) | method


# IOCTL codes
IOCTL_AIK_PING = _ctl_code(FILE_DEVICE_UNKNOWN, AIK_IOCTL_INDEX + 0, METHOD_BUFFERED, FILE_ANY_ACCESS)
IOCTL_AIK_ECHO = _ctl_code(FILE_DEVICE_UNKNOWN, AIK_IOCTL_INDEX + 1, METHOD_BUFFERED, FILE_ANY_ACCESS)
IOCTL_AIK_INJECT_SCANCODE = _ctl_code(FILE_DEVICE_UNKNOWN, AIK_IOCTL_INDEX + 2, METHOD_BUFFERED, FILE_ANY_ACCESS)
IOCTL_AIK_INJECT_SCANCODES = _ctl_code(FILE_DEVICE_UNKNOWN, AIK_IOCTL_INDEX + 3, METHOD_BUFFERED, FILE_ANY_ACCESS)

# Default device path
DEFAULT_DEVICE_PATH = r"\\.\AikKmdfIoctl"

# Setup kernel32 functions
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CreateFileW = kernel32.CreateFileW
CreateFileW.argtypes = [
    wintypes.LPCWSTR,  # lpFileName
    wintypes.DWORD,    # dwDesiredAccess
    wintypes.DWORD,    # dwShareMode
    wintypes.LPVOID,   # lpSecurityAttributes
    wintypes.DWORD,    # dwCreationDisposition
    wintypes.DWORD,    # dwFlagsAndAttributes
    wintypes.HANDLE,   # hTemplateFile
]
CreateFileW.restype = wintypes.HANDLE

DeviceIoControl = kernel32.DeviceIoControl
DeviceIoControl.argtypes = [
    wintypes.HANDLE,   # hDevice
    wintypes.DWORD,    # dwIoControlCode
    wintypes.LPVOID,   # lpInBuffer
    wintypes.DWORD,    # nInBufferSize
    wintypes.LPVOID,   # lpOutBuffer
    wintypes.DWORD,    # nOutBufferSize
    ctypes.POINTER(wintypes.DWORD),  # lpBytesReturned
    wintypes.LPVOID,   # lpOverlapped
]
DeviceIoControl.restype = wintypes.BOOL

CloseHandle = kernel32.CloseHandle
CloseHandle.argtypes = [wintypes.HANDLE]
CloseHandle.restype = wintypes.BOOL


class ScancodeInput(ctypes.Structure):
    """Matches AIK_SCANCODE_INPUT structure in driver."""
    _pack_ = 1
    _fields_ = [
        ("ScanCode", wintypes.USHORT),
        ("Flags", ctypes.c_ubyte),
    ]


class ScancodeBatch(ctypes.Structure):
    """Matches AIK_SCANCODE_BATCH structure in driver."""
    _pack_ = 1
    _fields_ = [
        ("Count", wintypes.ULONG),
        ("Scancodes", ScancodeInput * AIK_MAX_SCANCODES),
    ]


class DriverBridge:
    """
    Python bridge to AIK kernel driver for scancode injection.
    
    The driver must be loaded and running for this to work.
    See driver_stub/README.md for build instructions.
    """

    def __init__(self, device_path: str = DEFAULT_DEVICE_PATH):
        self._device_path = device_path
        self._handle: wintypes.HANDLE | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected and self._handle is not None

    def connect(self) -> bool:
        """
        Open handle to the kernel driver.
        Returns True on success, False if driver not available.
        """
        if self._connected:
            return True

        try:
            h = CreateFileW(
                self._device_path,
                GENERIC_READ | GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL,
                None,
            )
            if h == INVALID_HANDLE_VALUE:
                err = ctypes.get_last_error()
                log.warning("Failed to open driver at %s: error %d", self._device_path, err)
                return False

            self._handle = h
            self._connected = True
            log.info("Connected to driver at %s", self._device_path)
            return True

        except Exception as e:
            log.exception("Exception connecting to driver: %s", e)
            return False

    def disconnect(self) -> None:
        """Close the driver handle."""
        if self._handle is not None:
            CloseHandle(self._handle)
            self._handle = None
        self._connected = False
        log.info("Disconnected from driver")

    def ping(self) -> str | None:
        """
        Send PING to driver, expect 'PONG' response.
        Returns response string or None on failure.
        """
        if not self.connected:
            return None

        try:
            out_buf = (ctypes.c_ubyte * 256)()
            returned = wintypes.DWORD(0)

            ok = DeviceIoControl(
                self._handle,
                IOCTL_AIK_PING,
                None,
                0,
                ctypes.byref(out_buf),
                256,
                ctypes.byref(returned),
                None,
            )

            if not ok:
                err = ctypes.get_last_error()
                log.error("PING failed: error %d", err)
                return None

            return bytes(out_buf[:returned.value]).decode("utf-8", errors="replace").rstrip("\x00")

        except Exception as e:
            log.exception("Exception during PING: %s", e)
            return None

    def inject_scancode(self, scancode: int, *, is_down: bool = True, extended: bool = False) -> bool:
        """
        Inject a single scancode via the kernel driver.
        
        Args:
            scancode: Hardware scancode (e.g., 0x1E for 'A')
            is_down: True for key press, False for key release
            extended: True for extended keys (arrows, numpad, etc.)
        
        Returns:
            True on success, False on failure
        """
        if not self.connected:
            log.warning("Cannot inject: not connected to driver")
            return False

        try:
            flags = AIK_KEY_DOWN if is_down else AIK_KEY_UP
            if extended:
                flags |= AIK_KEY_EXTENDED

            sc_input = ScancodeInput(ScanCode=scancode, Flags=flags)
            returned = wintypes.DWORD(0)

            ok = DeviceIoControl(
                self._handle,
                IOCTL_AIK_INJECT_SCANCODE,
                ctypes.byref(sc_input),
                ctypes.sizeof(sc_input),
                None,
                0,
                ctypes.byref(returned),
                None,
            )

            if not ok:
                err = ctypes.get_last_error()
                log.error("inject_scancode failed: error %d", err)
                return False

            return True

        except Exception as e:
            log.exception("Exception during inject_scancode: %s", e)
            return False

    def inject_scancodes(self, scancodes: List[Tuple[int, bool, bool]]) -> bool:
        """
        Inject multiple scancodes in a batch via the kernel driver.
        
        Args:
            scancodes: List of (scancode, is_down, extended) tuples
        
        Returns:
            True on success, False on failure
        """
        if not self.connected:
            log.warning("Cannot inject: not connected to driver")
            return False

        if not scancodes:
            return True

        if len(scancodes) > AIK_MAX_SCANCODES:
            log.error("Too many scancodes: %d (max %d)", len(scancodes), AIK_MAX_SCANCODES)
            return False

        try:
            batch = ScancodeBatch()
            batch.Count = len(scancodes)

            for i, (sc, is_down, extended) in enumerate(scancodes):
                flags = AIK_KEY_DOWN if is_down else AIK_KEY_UP
                if extended:
                    flags |= AIK_KEY_EXTENDED
                batch.Scancodes[i].ScanCode = sc
                batch.Scancodes[i].Flags = flags

            returned = wintypes.DWORD(0)

            ok = DeviceIoControl(
                self._handle,
                IOCTL_AIK_INJECT_SCANCODES,
                ctypes.byref(batch),
                ctypes.sizeof(batch),
                None,
                0,
                ctypes.byref(returned),
                None,
            )

            if not ok:
                err = ctypes.get_last_error()
                log.error("inject_scancodes failed: error %d", err)
                return False

            return True

        except Exception as e:
            log.exception("Exception during inject_scancodes: %s", e)
            return False

    def type_key(self, scancode: int, *, extended: bool = False) -> bool:
        """
        Convenience: press and release a key.
        """
        return (
            self.inject_scancode(scancode, is_down=True, extended=extended) and
            self.inject_scancode(scancode, is_down=False, extended=extended)
        )

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False


# Common scancode mappings (Set 1 - standard PC keyboard)
SCANCODE_MAP = {
    # Letters (lowercase keys produce these scancodes)
    'a': 0x1E, 'b': 0x30, 'c': 0x2E, 'd': 0x20, 'e': 0x12,
    'f': 0x21, 'g': 0x22, 'h': 0x23, 'i': 0x17, 'j': 0x24,
    'k': 0x25, 'l': 0x26, 'm': 0x32, 'n': 0x31, 'o': 0x18,
    'p': 0x19, 'q': 0x10, 'r': 0x13, 's': 0x1F, 't': 0x14,
    'u': 0x16, 'v': 0x2F, 'w': 0x11, 'x': 0x2D, 'y': 0x15,
    'z': 0x2C,
    
    # Numbers (top row)
    '1': 0x02, '2': 0x03, '3': 0x04, '4': 0x05, '5': 0x06,
    '6': 0x07, '7': 0x08, '8': 0x09, '9': 0x0A, '0': 0x0B,
    
    # Special characters (without shift)
    '-': 0x0C, '=': 0x0D, '[': 0x1A, ']': 0x1B, '\\': 0x2B,
    ';': 0x27, "'": 0x28, '`': 0x29, ',': 0x33, '.': 0x34,
    '/': 0x35,
    
    # Control keys
    'space': 0x39, 'enter': 0x1C, 'tab': 0x0F, 'backspace': 0x0E,
    'escape': 0x01, 'esc': 0x01,
    
    # Modifiers
    'lshift': 0x2A, 'rshift': 0x36, 'shift': 0x2A,
    'lctrl': 0x1D, 'ctrl': 0x1D,
    'lalt': 0x38, 'alt': 0x38,
    'capslock': 0x3A,
    
    # Function keys
    'f1': 0x3B, 'f2': 0x3C, 'f3': 0x3D, 'f4': 0x3E,
    'f5': 0x3F, 'f6': 0x40, 'f7': 0x41, 'f8': 0x42,
    'f9': 0x43, 'f10': 0x44, 'f11': 0x57, 'f12': 0x58,
}

# Extended keys (require 0xE0 prefix in scancode set 1)
EXTENDED_KEYS = {
    'up': 0x48, 'down': 0x50, 'left': 0x4B, 'right': 0x4D,
    'home': 0x47, 'end': 0x4F, 'pageup': 0x49, 'pagedown': 0x51,
    'insert': 0x52, 'delete': 0x53,
    'rctrl': 0x1D, 'ralt': 0x38,  # Right modifiers are extended
    'lwin': 0x5B, 'rwin': 0x5C, 'win': 0x5B,
    'apps': 0x5D,  # Application/context menu key
    'printscreen': 0x37,
    'pause': 0x45,
}


def get_scancode(key: str) -> Tuple[int, bool]:
    """
    Get scancode and extended flag for a key name.
    
    Returns:
        (scancode, is_extended)
    """
    key = key.lower().strip()
    
    if key in SCANCODE_MAP:
        return (SCANCODE_MAP[key], False)
    
    if key in EXTENDED_KEYS:
        return (EXTENDED_KEYS[key], True)
    
    raise ValueError(f"Unknown key: {key}")


def text_to_scancodes(text: str) -> List[Tuple[int, bool, bool]]:
    """
    Convert text to a list of (scancode, is_down, extended) tuples.
    Handles basic text input (no shift handling for now).
    """
    result = []
    
    for ch in text:
        if ch == '\n':
            result.append((SCANCODE_MAP['enter'], True, False))
            result.append((SCANCODE_MAP['enter'], False, False))
        elif ch == '\t':
            result.append((SCANCODE_MAP['tab'], True, False))
            result.append((SCANCODE_MAP['tab'], False, False))
        elif ch == ' ':
            result.append((SCANCODE_MAP['space'], True, False))
            result.append((SCANCODE_MAP['space'], False, False))
        elif ch.lower() in SCANCODE_MAP:
            sc = SCANCODE_MAP[ch.lower()]
            needs_shift = ch.isupper() or ch in '!@#$%^&*()_+{}|:"<>?~'
            
            if needs_shift:
                result.append((SCANCODE_MAP['shift'], True, False))
            
            result.append((sc, True, False))
            result.append((sc, False, False))
            
            if needs_shift:
                result.append((SCANCODE_MAP['shift'], False, False))
    
    return result
