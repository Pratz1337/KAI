from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes


def is_admin() -> bool:
    """
    Returns True if the current process is running elevated (Administrator) on Windows.
    """
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin(*, argv: list[str] | None = None, cwd: str | None = None) -> None:
    """
    Relaunch the current Python entrypoint with UAC elevation (runas).

    Note: the elevated process will typically start in a new console/window.
    """
    if os.name != "nt":
        raise RuntimeError("Elevation is only supported on Windows.")

    if argv is None:
        argv = sys.argv
    if not argv:
        raise ValueError("argv must contain at least the script/executable name")

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ShellExecuteW = shell32.ShellExecuteW
    ShellExecuteW.argtypes = [
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_int,
    ]
    ShellExecuteW.restype = wintypes.HINSTANCE

    python_exe = sys.executable
    # Pass script + args as parameters to python.exe.
    params = subprocess.list2cmdline([os.path.abspath(argv[0]), *argv[1:]])

    rc = ShellExecuteW(
        None,
        "runas",
        python_exe,
        params,
        cwd or os.getcwd(),
        1,  # SW_SHOWNORMAL
    )
    # Per ShellExecuteW docs: >32 is success, <=32 is an error code.
    if rc <= 32:
        err = ctypes.get_last_error()
        raise ctypes.WinError(err)

