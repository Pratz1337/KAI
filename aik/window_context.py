from __future__ import annotations

from dataclasses import dataclass

import win32api
import win32con
import win32gui
import win32process


@dataclass(frozen=True)
class ForegroundWindow:
    hwnd: int
    title: str
    pid: int
    process_path: str | None


def get_foreground_window() -> ForegroundWindow:
    hwnd = win32gui.GetForegroundWindow()
    title = ""
    pid = 0
    process_path: str | None = None

    try:
        title = win32gui.GetWindowText(hwnd) or ""
    except Exception:
        title = ""

    try:
        _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
    except Exception:
        pid = 0

    if pid:
        try:
            # QueryLimitedInformation is enough to read image name for many processes.
            hproc = win32api.OpenProcess(
                win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            try:
                process_path = win32process.GetModuleFileNameEx(hproc, 0)
            finally:
                win32api.CloseHandle(hproc)
        except Exception:
            process_path = None

    return ForegroundWindow(hwnd=hwnd, title=title, pid=pid, process_path=process_path)

