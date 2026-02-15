from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080

FILE_DEVICE_UNKNOWN = 0x00000022
METHOD_BUFFERED = 0
FILE_ANY_ACCESS = 0


def ctl_code(device_type: int, function: int, method: int, access: int) -> int:
    return (device_type << 16) | (access << 14) | (function << 2) | method


AIK_IOCTL_INDEX = 0x800
IOCTL_AIK_PING = ctl_code(FILE_DEVICE_UNKNOWN, AIK_IOCTL_INDEX + 0, METHOD_BUFFERED, FILE_ANY_ACCESS)
IOCTL_AIK_ECHO = ctl_code(FILE_DEVICE_UNKNOWN, AIK_IOCTL_INDEX + 1, METHOD_BUFFERED, FILE_ANY_ACCESS)


CreateFileW = kernel32.CreateFileW
CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
CreateFileW.restype = wintypes.HANDLE

DeviceIoControl = kernel32.DeviceIoControl
DeviceIoControl.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPVOID,
]
DeviceIoControl.restype = wintypes.BOOL

CloseHandle = kernel32.CloseHandle
CloseHandle.argtypes = [wintypes.HANDLE]
CloseHandle.restype = wintypes.BOOL


INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


def open_device(path: str) -> wintypes.HANDLE:
    h = CreateFileW(
        path,
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if h == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    return h


def ioctl(h: wintypes.HANDLE, code: int, in_data: bytes = b"", out_size: int = 256) -> bytes:
    in_buf = (ctypes.c_ubyte * max(1, len(in_data)))()
    for i, b in enumerate(in_data):
        in_buf[i] = b

    out_buf = (ctypes.c_ubyte * out_size)()
    returned = wintypes.DWORD(0)
    ok = DeviceIoControl(
        h,
        code,
        ctypes.byref(in_buf),
        len(in_data),
        ctypes.byref(out_buf),
        out_size,
        ctypes.byref(returned),
        None,
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return bytes(out_buf[: returned.value])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default=r"\\.\AikKmdfIoctl")
    args = ap.parse_args()

    h = open_device(args.path)
    try:
        pong = ioctl(h, IOCTL_AIK_PING, b"", 64)
        print("PING ->", pong.decode("ascii", errors="replace").rstrip("\x00"))

        msg = b"hello from usermode"
        echo = ioctl(h, IOCTL_AIK_ECHO, msg, 256)
        print("ECHO ->", echo.decode("ascii", errors="replace"))
    finally:
        CloseHandle(h)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

