from __future__ import annotations

from dataclasses import dataclass

import win32api
import win32con
import win32gui
import win32process


@dataclass(frozen=True)
class WindowMatch:
    hwnd: int
    title: str
    pid: int
    exe: str | None


def focus_first_window(exe_substr: str) -> bool:
    exe_substr = (exe_substr or "").lower().strip()
    if not exe_substr:
        return False

    matches: list[WindowMatch] = []

    def enum_cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd) or ""
        if not title.strip():
            return
        try:
            _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return
        exe = None
        if pid:
            try:
                hproc = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                try:
                    exe = win32process.GetModuleFileNameEx(hproc, 0)
                finally:
                    win32api.CloseHandle(hproc)
            except Exception:
                exe = None
        if exe and exe_substr in exe.lower():
            matches.append(WindowMatch(hwnd=hwnd, title=title, pid=pid, exe=exe))

    win32gui.EnumWindows(enum_cb, None)

    if not matches:
        return False

    hwnd = matches[0].hwnd
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    except Exception:
        pass
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        # Fallback: try alt-tab style activate
        try:
            win32gui.BringWindowToTop(hwnd)
        except Exception:
            return False
    return True


def focus_app_for_goal(goal: str) -> bool:
    g = (goal or "").lower()
    # Cheap keyword mapping.
    if "chrome" in g or "gmail" in g:
        return focus_first_window("chrome.exe")
    if "spotify" in g:
        return focus_first_window("spotify.exe")
    if "whatsapp" in g:
        return focus_first_window("whatsapp.exe")
    if "paint" in g or "mspaint" in g:
        return focus_first_window("mspaint")
    if "notepad" in g:
        return focus_first_window("notepad")
    return False
