"""
Kernel-mode keyboard injector.

Communicates with the AikKmdfIoctl driver via DeviceIoControl to inject
raw scancodes through an optional kernel driver.

Falls back to user-mode SendInput when the driver is not loaded,
but only after exhausting all auto-recovery options (service start, etc.).
"""
from __future__ import annotations

import ctypes
import logging
import struct
import time
from ctypes import wintypes

log = logging.getLogger("aik.input_injector_kernel")

# ---------------------------------------------------------------------------
# Win32 handles
# ---------------------------------------------------------------------------
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

CreateFileW = kernel32.CreateFileW
CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
    wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
CreateFileW.restype = wintypes.HANDLE

DeviceIoControl = kernel32.DeviceIoControl
DeviceIoControl.argtypes = [
    wintypes.HANDLE, wintypes.DWORD,
    wintypes.LPVOID, wintypes.DWORD,
    wintypes.LPVOID, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
]
DeviceIoControl.restype = wintypes.BOOL

CloseHandle = kernel32.CloseHandle
CloseHandle.argtypes = [wintypes.HANDLE]
CloseHandle.restype = wintypes.BOOL


def _ctl_code(device_type: int, function: int, method: int, access: int) -> int:
    return (device_type << 16) | (access << 14) | (function << 2) | method


AIK_IOCTL_INDEX = 0x800
IOCTL_AIK_PING = _ctl_code(FILE_DEVICE_UNKNOWN, AIK_IOCTL_INDEX + 0, METHOD_BUFFERED, FILE_ANY_ACCESS)
IOCTL_AIK_INJECT_KEY = _ctl_code(FILE_DEVICE_UNKNOWN, AIK_IOCTL_INDEX + 2, METHOD_BUFFERED, FILE_ANY_ACCESS)

AIK_MAX_SCANCODES = 32
DEVICE_PATH = r"\\.\AikKmdfIoctl"

# Flags matching driver's Public.h
KEY_MAKE  = 0
KEY_BREAK = 1
KEY_E0    = 2

# ---------------------------------------------------------------------------
# PS/2 Set-1 scancode table.  VK -> (scancode, is_extended)
# This maps the same VK codes used by the user-mode injector to PS/2
# scancodes that the driver injects into the keyboard class.
# ---------------------------------------------------------------------------
_VK_TO_SCAN: dict[int, tuple[int, bool]] = {
    # letters A-Z  (0x41 - 0x5A)
    0x41: (0x1E, False),  # A
    0x42: (0x30, False),  # B
    0x43: (0x2E, False),  # C
    0x44: (0x20, False),  # D
    0x45: (0x12, False),  # E
    0x46: (0x21, False),  # F
    0x47: (0x22, False),  # G
    0x48: (0x23, False),  # H
    0x49: (0x17, False),  # I
    0x4A: (0x24, False),  # J
    0x4B: (0x25, False),  # K
    0x4C: (0x26, False),  # L
    0x4D: (0x32, False),  # M
    0x4E: (0x31, False),  # N
    0x4F: (0x18, False),  # O
    0x50: (0x19, False),  # P
    0x51: (0x10, False),  # Q
    0x52: (0x13, False),  # R
    0x53: (0x1F, False),  # S
    0x54: (0x14, False),  # T
    0x55: (0x16, False),  # U
    0x56: (0x2F, False),  # V
    0x57: (0x11, False),  # W
    0x58: (0x2D, False),  # X
    0x59: (0x15, False),  # Y
    0x5A: (0x2C, False),  # Z
    # digits 0-9  (0x30 - 0x39)
    0x30: (0x0B, False),  # 0
    0x31: (0x02, False),  # 1
    0x32: (0x03, False),  # 2
    0x33: (0x04, False),  # 3
    0x34: (0x05, False),  # 4
    0x35: (0x06, False),  # 5
    0x36: (0x07, False),  # 6
    0x37: (0x08, False),  # 7
    0x38: (0x09, False),  # 8
    0x39: (0x0A, False),  # 9
    # function keys F1-F12
    0x70: (0x3B, False),  # F1
    0x71: (0x3C, False),  # F2
    0x72: (0x3D, False),  # F3
    0x73: (0x3E, False),  # F4
    0x74: (0x3F, False),  # F5
    0x75: (0x40, False),  # F6
    0x76: (0x41, False),  # F7
    0x77: (0x42, False),  # F8
    0x78: (0x43, False),  # F9
    0x79: (0x44, False),  # F10
    0x7A: (0x57, False),  # F11
    0x7B: (0x58, False),  # F12
    # modifiers
    0x10: (0x2A, False),  # SHIFT (left)
    0x11: (0x1D, False),  # CTRL  (left)
    0x12: (0x38, False),  # ALT   (left)
    0x5B: (0x5B, True),   # LWIN  (E0 5B)
    0x5C: (0x5C, True),   # RWIN  (E0 5C)
    # navigation — all extended (E0 prefix)
    0x0D: (0x1C, False),  # ENTER
    0x1B: (0x01, False),  # ESC
    0x09: (0x0F, False),  # TAB
    0x08: (0x0E, False),  # BACKSPACE
    0x20: (0x39, False),  # SPACE
    0x14: (0x3A, False),  # CAPSLOCK
    0x2E: (0x53, True),   # DELETE   (E0 53)
    0x2D: (0x52, True),   # INSERT   (E0 52)
    0x24: (0x47, True),   # HOME     (E0 47)
    0x23: (0x4F, True),   # END      (E0 4F)
    0x21: (0x49, True),   # PAGE UP  (E0 49)
    0x22: (0x51, True),   # PAGE DN  (E0 51)
    0x25: (0x4B, True),   # LEFT     (E0 4B)
    0x26: (0x48, True),   # UP       (E0 48)
    0x27: (0x4D, True),   # RIGHT    (E0 4D)
    0x28: (0x50, True),   # DOWN     (E0 50)
    0x13: (0x45, False),  # PAUSE
    0x2C: (0x37, True),   # PRINT SCREEN (E0 37)
    # punctuation / OEM
    0xC0: (0x29, False),  # ` / ~  (grave / tilde)
    0xBD: (0x0C, False),  # - / _
    0xBB: (0x0D, False),  # = / +
    0xDC: (0x2B, False),  # \ / |
    0xBA: (0x27, False),  # ; / :
    0xDE: (0x28, False),  # ' / "
    0xBC: (0x33, False),  # , / <
    0xBE: (0x34, False),  # . / >
    0xBF: (0x35, False),  # / / ?
    0xDB: (0x1A, False),  # [ / {
    0xDD: (0x1B, False),  # ] / }
}


def _vk_to_scancode(vk: int) -> tuple[int, bool]:
    """Return (ps2_scancode, is_extended) for a VK code."""
    entry = _VK_TO_SCAN.get(vk)
    if entry is not None:
        return entry
    raise ValueError(f"No scancode mapping for VK 0x{vk:02X}")


# ---------------------------------------------------------------------------
# Key-name -> VK
# ---------------------------------------------------------------------------
_VK: dict[str, int] = {
    "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "space": 0x20,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "insert": 0x2D, "capslock": 0x14,
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C,
    "pause": 0x13, "printscreen": 0x2C,
    "grave": 0xC0, "`": 0xC0, "~": 0xC0, "backtick": 0xC0, "tilde": 0xC0,
    "minus": 0xBD, "-": 0xBD,
    "equal": 0xBB, "=": 0xBB,
    "backslash": 0xDC, "\\": 0xDC,
    "semicolon": 0xBA, ";": 0xBA,
    "quote": 0xDE, "'": 0xDE,
    "comma": 0xBC, ",": 0xBC,
    "period": 0xBE, ".": 0xBE,
    "slash": 0xBF, "/": 0xBF,
    "[": 0xDB, "]": 0xDD,
}


def _vk_from_key_name(key: str) -> int:
    k = (key or "").strip().lower()
    if not k:
        raise ValueError("Empty key name")
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
    except KeyError as exc:
        raise ValueError(f"Unsupported key name: {key!r}") from exc


# ---------------------------------------------------------------------------
# Packet builder - packs an AIK_KEY_PACKET for DeviceIoControl
# ---------------------------------------------------------------------------
def _build_key_packet(scancodes: list[tuple[int, int]]) -> bytes:
    """
    Build an AIK_KEY_PACKET binary struct.

    Each entry in `scancodes` is (makecode, flags).
    Layout (pack=1):
        ULONG  Count
        AIK_SCANCODE[Count]   -  each is (USHORT MakeCode, USHORT Flags)
    Padded out to AIK_MAX_SCANCODES entries.
    """
    count = len(scancodes)
    if count > AIK_MAX_SCANCODES:
        raise ValueError(f"Too many scancodes ({count} > {AIK_MAX_SCANCODES})")

    buf = struct.pack("<I", count)
    for mc, flags in scancodes:
        buf += struct.pack("<HH", mc, flags)
    # Pad remaining slots to fixed size.
    for _ in range(AIK_MAX_SCANCODES - count):
        buf += struct.pack("<HH", 0, 0)
    return buf


# ---------------------------------------------------------------------------
# KernelInputInjector
# ---------------------------------------------------------------------------
class KernelInputInjector:
    """
    Keyboard injector that sends scancodes through the AIK KMDF driver.

    The driver injects them directly into the keyboard class service callback,
    appearing as genuine hardware keystrokes to the OS.
    """

    def __init__(
        self,
        *,
        inter_key_delay_s: float = 0.01,
        device_path: str = DEVICE_PATH,
        fallback: bool = True,
    ) -> None:
        self._delay = inter_key_delay_s
        self._device_path = device_path
        self._handle: wintypes.HANDLE | None = None
        self._fallback = fallback
        self._user_mode: object | None = None  # lazy import
        self._connect()

    # ---- driver connection -------------------------------------------------

    def _connect(self) -> None:
        # ----- Phase 1: Check if driver service is installed & running -----
        svc_status = _check_driver_service()
        if svc_status == "not_installed":
            log.warning(
                "Driver service not installed. "
                "Run install_driver.ps1 as Administrator first."
            )
            if self._fallback:
                self._handle = None
                log.warning("Falling back to user-mode SendInput.")
                from .input_injector import InputInjector
                self._user_mode = InputInjector(inter_key_delay_s=self._delay)
                return
            raise RuntimeError(
                "Driver service 'AikKmdfDriver' not installed. "
                "Run: .\\install_driver.ps1 -SysPath <path-to-.sys>"
            )

        if svc_status == "stopped":
            log.info("Driver service exists but is stopped. Attempting auto-start...")
            started = _try_auto_start_service()
            if started:
                log.info("Driver service auto-started successfully.")
                import time as _time
                _time.sleep(1)  # give the driver a moment to create its device
            else:
                log.warning(
                    "Could not auto-start driver service. "
                    "Run 'sc.exe start AikKmdfDriver' as Administrator."
                )

        # ----- Phase 2: Try to open the device handle -----
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
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = h
            log.info("Connected to kernel driver at %s", self._device_path)

            # quick PING to validate the driver is alive
            self._ping()

        except OSError as e:
            self._handle = None
            winerr = getattr(e, "winerror", None)

            # Provide specific guidance per error code
            if winerr == 2:  # ERROR_FILE_NOT_FOUND
                log.error(
                    "Driver device not found at %s. Possible causes:\n"
                    "  1. Driver service not started -> sc.exe start AikKmdfDriver\n"
                    "  2. Driver .sys not installed  -> run install_driver.ps1\n"
                    "  3. Driver crashed on load     -> check Event Viewer > System",
                    self._device_path,
                )
            elif winerr == 5:  # ERROR_ACCESS_DENIED
                log.error(
                    "Access denied opening %s. Run Python as Administrator.",
                    self._device_path,
                )
            elif winerr == 1275:  # ERROR_DRIVER_BLOCKED
                log.error(
                    "Driver blocked by Windows. Ensure test signing is enabled:\n"
                    "  bcdedit /set testsigning on  (then reboot)"
                )
            else:
                log.error(
                    "Failed to connect to driver at %s: %s",
                    self._device_path, e,
                )

            if self._fallback:
                log.warning(
                    "%s Falling back to user-mode SendInput.",
                    _format_driver_connect_error(self._device_path, e),
                )
                from .input_injector import InputInjector
                self._user_mode = InputInjector(inter_key_delay_s=self._delay)
            else:
                raise

    def _ping(self) -> None:
        out_buf = (ctypes.c_ubyte * 64)()
        returned = wintypes.DWORD(0)
        in_buf = (ctypes.c_ubyte * 1)()
        ok = DeviceIoControl(
            self._handle, IOCTL_AIK_PING,
            ctypes.byref(in_buf), 0,
            ctypes.byref(out_buf), 64,
            ctypes.byref(returned), None,
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        resp = bytes(out_buf[:returned.value]).decode("ascii", errors="replace").rstrip("\x00")
        log.info("Driver PING -> %s", resp)

    def close(self) -> None:
        if self._handle is not None:
            CloseHandle(self._handle)
            self._handle = None

    # ---- IOCTL send --------------------------------------------------------

    def _send_scancodes(self, codes: list[tuple[int, int]]) -> None:
        """Send a list of (makecode, flags) to the driver in one IOCTL."""
        if self._user_mode is not None:
            # Delegation handled per-method below
            return

        if self._handle is None:
            raise RuntimeError("No driver connection and no fallback.")

        pkt = _build_key_packet(codes)
        in_buf = (ctypes.c_ubyte * len(pkt))(*pkt)
        out_buf = (ctypes.c_ubyte * 4)()
        returned = wintypes.DWORD(0)

        ok = DeviceIoControl(
            self._handle, IOCTL_AIK_INJECT_KEY,
            ctypes.byref(in_buf), len(pkt),
            ctypes.byref(out_buf), 4,
            ctypes.byref(returned), None,
        )
        if not ok:
            err = ctypes.get_last_error()
            raise OSError(f"IOCTL_AIK_INJECT_KEY failed (GetLastError={err})")

    # ---- public API (same interface as InputInjector) ----------------------

    def type_text(self, text: str) -> None:
        if self._user_mode is not None:
            self._user_mode.type_text(text)
            return

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
            self._type_char_kernel(ch)
            if self._delay:
                time.sleep(self._delay)

    def _type_char_kernel(self, ch: str) -> None:
        """Type a single character via kernel scancodes."""
        vk = _vk_from_char(ch)
        if vk is None:
            # Character not in our VK table - skip it with a warning.
            log.warning("Cannot map character %r to scancode; skipping.", ch)
            return
        scan, ext = _vk_to_scancode(vk)
        flags_down = KEY_MAKE | (KEY_E0 if ext else 0)
        flags_up   = KEY_BREAK | (KEY_E0 if ext else 0)

        # Check if Shift is needed (uppercase or shifted symbols).
        need_shift = _needs_shift(ch)
        codes: list[tuple[int, int]] = []
        if need_shift:
            s_scan, s_ext = _vk_to_scancode(0x10)  # SHIFT
            codes.append((s_scan, KEY_MAKE | (KEY_E0 if s_ext else 0)))
        codes.append((scan, flags_down))
        codes.append((scan, flags_up))
        if need_shift:
            s_scan, s_ext = _vk_to_scancode(0x10)
            codes.append((s_scan, KEY_BREAK | (KEY_E0 if s_ext else 0)))
        self._send_scancodes(codes)

    def key_press(self, key: str) -> None:
        if self._user_mode is not None:
            self._user_mode.key_press(key)
            return

        vk = _vk_from_key_name(key)
        scan, ext = _vk_to_scancode(vk)
        flags_down = KEY_MAKE | (KEY_E0 if ext else 0)
        flags_up   = KEY_BREAK | (KEY_E0 if ext else 0)
        self._send_scancodes([(scan, flags_down), (scan, flags_up)])
        if self._delay:
            time.sleep(self._delay)

    def hotkey(self, keys: list[str]) -> None:
        if self._user_mode is not None:
            self._user_mode.hotkey(keys)
            return

        if not keys:
            return
        if len(keys) == 1:
            self.key_press(keys[0])
            return

        vks = [_vk_from_key_name(k) for k in keys]
        mods, main_vk = vks[:-1], vks[-1]

        codes: list[tuple[int, int]] = []
        # Press modifiers
        for vk in mods:
            scan, ext = _vk_to_scancode(vk)
            codes.append((scan, KEY_MAKE | (KEY_E0 if ext else 0)))
        # Press + release main key
        scan, ext = _vk_to_scancode(main_vk)
        codes.append((scan, KEY_MAKE | (KEY_E0 if ext else 0)))
        codes.append((scan, KEY_BREAK | (KEY_E0 if ext else 0)))
        # Release modifiers in reverse
        for vk in reversed(mods):
            scan, ext = _vk_to_scancode(vk)
            codes.append((scan, KEY_BREAK | (KEY_E0 if ext else 0)))

        self._send_scancodes(codes)
        if self._delay:
            time.sleep(self._delay)


# ---------------------------------------------------------------------------
# Helpers for character -> VK mapping
# ---------------------------------------------------------------------------

# Characters that need Shift held.
_SHIFT_CHARS = set('ABCDEFGHIJKLMNOPQRSTUVWXYZ~!@#$%^&*()_+{}|:"<>?')

# Map from character -> VK code (unshifted base key).
_CHAR_TO_VK: dict[str, int] = {}
# lowercase letters
for _c in range(ord('a'), ord('z') + 1):
    _CHAR_TO_VK[chr(_c)] = _c - 32  # VK = uppercase ASCII
# uppercase letters (same VK, Shift added separately)
for _c in range(ord('A'), ord('Z') + 1):
    _CHAR_TO_VK[chr(_c)] = _c
# digits
for _c in range(ord('0'), ord('9') + 1):
    _CHAR_TO_VK[chr(_c)] = _c
# shifted digits -> same VK
_SHIFTED_DIGIT = {'!': 0x31, '@': 0x32, '#': 0x33, '$': 0x34, '%': 0x35,
                  '^': 0x36, '&': 0x37, '*': 0x38, '(': 0x39, ')': 0x30}
_CHAR_TO_VK.update(_SHIFTED_DIGIT)
# punctuation
_PUNCT = {
    '`': 0xC0, '~': 0xC0, '-': 0xBD, '_': 0xBD, '=': 0xBB, '+': 0xBB,
    '[': 0xDB, '{': 0xDB, ']': 0xDD, '}': 0xDD, '\\': 0xDC, '|': 0xDC,
    ';': 0xBA, ':': 0xBA, "'": 0xDE, '"': 0xDE, ',': 0xBC, '<': 0xBC,
    '.': 0xBE, '>': 0xBE, '/': 0xBF, '?': 0xBF, ' ': 0x20,
}
_CHAR_TO_VK.update(_PUNCT)


def _vk_from_char(ch: str) -> int | None:
    return _CHAR_TO_VK.get(ch)


def _needs_shift(ch: str) -> bool:
    return ch in _SHIFT_CHARS


def _format_driver_connect_error(device_path: str, exc: OSError) -> str:
    winerr = getattr(exc, "winerror", None)
    if winerr == 2:  # ERROR_FILE_NOT_FOUND
        return f"Kernel driver device not found at {device_path!r} ([WinError 2])."
    if winerr == 3:  # ERROR_PATH_NOT_FOUND
        return f"Kernel driver path not found: {device_path!r} ([WinError 3])."
    if winerr == 5:  # ERROR_ACCESS_DENIED
        return f"Access denied opening kernel driver device {device_path!r} ([WinError 5])."
    if winerr == 1275:  # ERROR_DRIVER_BLOCKED
        return "Kernel driver appears blocked by the OS ([WinError 1275])."
    return f"Kernel driver not available at {device_path!r} ({exc})."


# ---------------------------------------------------------------------------
#  Driver service management helpers
# ---------------------------------------------------------------------------

_SERVICE_NAMES = ["AikKmdfDriver", "AikKmdfIoctl"]


def _check_driver_service() -> str:
    """
    Query the Windows Service Control Manager for the driver service.

    Returns one of:
        "running"       – service exists and is running
        "stopped"       – service exists but is not running
        "not_installed" – service not registered
    """
    import subprocess

    for name in _SERVICE_NAMES:
        try:
            result = subprocess.run(
                ["sc.exe", "query", name],
                capture_output=True, text=True, timeout=10,
            )
            output = result.stdout + result.stderr
            if "RUNNING" in output:
                return "running"
            if "STOPPED" in output or "STATE" in output:
                return "stopped"
        except Exception:
            continue

    return "not_installed"


def _try_auto_start_service() -> bool:
    """Attempt to start the driver service.  Returns True on success."""
    import subprocess

    for name in _SERVICE_NAMES:
        try:
            result = subprocess.run(
                ["sc.exe", "start", name],
                capture_output=True, text=True, timeout=15,
            )
            combined = result.stdout + result.stderr
            if result.returncode == 0 or "RUNNING" in combined:
                log.info("Auto-started service '%s'.", name)
                return True
            if "Access is denied" in combined:
                log.warning(
                    "Cannot auto-start service '%s': access denied. "
                    "Run Python as Administrator, or start it manually:\n"
                    "  sc.exe start %s", name, name,
                )
                return False
        except Exception as e:
            log.debug("Failed to start service '%s': %s", name, e)

    return False

