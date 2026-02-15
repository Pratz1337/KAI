"""Screen-edge border overlay — shows a coloured glow frame while the agent is active.

Displays a thin purple/yellow border around the entire screen with a floating
"KAI Agent in action..." label.  The border is click-through so it never
interferes with automation or steals focus from the agent.

Usage::

    border = ScreenBorder()
    border.start()          # shows border
    border.stop()           # hides + destroys
"""

from __future__ import annotations

import ctypes
import logging
import threading

log = logging.getLogger("aik.screen_border")

# Win32 extended-window-style flags for click-through
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
GWL_EXSTYLE = -20

_user32 = ctypes.windll.user32
_GetWindowLongW = _user32.GetWindowLongW
_SetWindowLongW = _user32.SetWindowLongW


def _make_click_through(hwnd: int) -> None:
    """Make a window click-through so it never steals focus."""
    try:
        style = _GetWindowLongW(hwnd, GWL_EXSTYLE)
        _SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW)
    except Exception:
        pass


class ScreenBorder:
    """Thin coloured border overlay around the entire screen."""

    BORDER_PX = 4
    COLOUR = "#7c3aed"  # purple
    LABEL_BG = "#7c3aed"
    LABEL_FG = "#ffffff"

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._hide_event = threading.Event()
        self._show_event = threading.Event()
        self._root = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        t = threading.Thread(target=self._run, name="screen-border", daemon=True)
        self._thread = t
        t.start()

    def stop(self) -> None:
        self._stop_event.set()

    def hide_for_capture(self) -> None:
        """Temporarily hide so it won't appear in screenshots."""
        self._hide_event.set()
        import time; time.sleep(0.08)

    def show_after_capture(self) -> None:
        """Re-show after screenshot."""
        self._show_event.set()

    def _run(self) -> None:
        try:
            import tkinter as tk
        except Exception:
            return

        root = tk.Tk()
        self._root = root
        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.85)

        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        b = self.BORDER_PX

        root.geometry(f"{sw}x{sh}+0+0")
        root.configure(bg="")

        canvas = tk.Canvas(root, width=sw, height=sh, highlightthickness=0, bg="black")
        canvas.pack(fill="both", expand=True)

        # Draw four border rectangles (top, bottom, left, right) to form a frame
        canvas.create_rectangle(0, 0, sw, b, fill=self.COLOUR, outline="")       # top
        canvas.create_rectangle(0, sh - b, sw, sh, fill=self.COLOUR, outline="")  # bottom
        canvas.create_rectangle(0, 0, b, sh, fill=self.COLOUR, outline="")       # left
        canvas.create_rectangle(sw - b, 0, sw, sh, fill=self.COLOUR, outline="")  # right

        # Make the centre transparent
        root.wm_attributes("-transparentcolor", "black")

        # Label
        label = tk.Label(
            root, text="  KAI Agent in action...  ",
            font=("Segoe UI", 9, "bold"),
            bg=self.LABEL_BG, fg=self.LABEL_FG,
            padx=10, pady=3,
        )
        label.place(x=sw // 2 - 100, y=b + 4)

        root.update_idletasks()
        root.deiconify()

        # Make click-through
        try:
            hwnd = root.winfo_id()
            parent = ctypes.windll.user32.GetParent(hwnd)
            _make_click_through(parent if parent else hwnd)
        except Exception:
            pass

        def poll():
            if self._stop_event.is_set():
                root.destroy()
                return
            if self._hide_event.is_set():
                self._hide_event.clear()
                root.withdraw()
            if self._show_event.is_set():
                self._show_event.clear()
                root.deiconify()
            root.after(200, poll)

        root.after(200, poll)
        root.mainloop()
