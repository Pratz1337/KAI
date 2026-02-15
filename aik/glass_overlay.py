"""Glassmorphism overlay for KAI — prompt input with Sarvam AI voice + progress tracker.

Features
--------
* **Ctrl+Alt+Space** hotkey to toggle visibility
* Dark glassmorphism UI with Windows DWM acrylic blur (Win10 1803+/11)
* Single-line text input with placeholder + mic button for voice-to-text
* Real-time task progress tracker with animated checklist & progress bar
* Thread-safe queue-based communication with the agent loop
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .voice_input import VoiceRecognizer

log = logging.getLogger("aik.glass_overlay")


# ── Colour palette (GitHub-dark inspired glassmorphism) ──────────────────────

class _C:
    BG            = "#0d1117"
    SURFACE       = "#161b22"
    SURFACE_HI    = "#1c2333"
    BORDER        = "#30363d"
    ACCENT        = "#7c3aed"
    ACCENT_HOVER  = "#6d28d9"
    ACCENT_GLOW   = "#a78bfa"
    TEXT          = "#e6edf3"
    TEXT_DIM      = "#8b949e"
    TEXT_FAINT    = "#484f58"
    SUCCESS       = "#3fb950"
    WARNING       = "#d29922"
    ERROR         = "#f85149"
    ERROR_HOVER   = "#da3633"
    INPUT_BG      = "#0d1117"
    PROGRESS_BG   = "#21262d"
    PROGRESS_FILL = "#7c3aed"


# ── DWM acrylic blur (best-effort) ──────────────────────────────────────────

def _apply_acrylic_blur(hwnd: int) -> bool:
    """Enable DWM acrylic blur behind the HWND.  Falls back silently."""
    try:
        class ACCENT_POLICY(ctypes.Structure):
            _fields_ = [
                ("AccentState",   ctypes.c_uint),
                ("AccentFlags",   ctypes.c_uint),
                ("GradientColor", ctypes.c_uint),
                ("AnimationId",   ctypes.c_uint),
            ]

        class WINCOMPATTR(ctypes.Structure):
            _fields_ = [
                ("Attribute",  ctypes.c_uint),
                ("Data",       ctypes.POINTER(ACCENT_POLICY)),
                ("SizeOfData", ctypes.c_size_t),
            ]

        accent = ACCENT_POLICY()
        accent.AccentState = 4          # ACCENT_ENABLE_ACRYLICBLURBEHIND
        accent.AccentFlags = 2
        accent.GradientColor = 0xCC000000  # AABBGGRR — semi-transparent black

        data = WINCOMPATTR()
        data.Attribute = 19             # WCA_ACCENT_POLICY
        data.Data = ctypes.pointer(accent)
        data.SizeOfData = ctypes.sizeof(accent)

        return bool(
            ctypes.windll.user32.SetWindowCompositionAttribute(
                hwnd, ctypes.byref(data),
            )
        )
    except Exception:
        return False


# ── Glass Overlay ────────────────────────────────────────────────────────────

class GlassOverlay:
    """Glassmorphism overlay with prompt input, voice mic, and progress tracker.

    Drop-in replacement for the basic ``Overlay`` — same ``start/stop/update``
    API, plus ``wait_for_goal()`` for interactive prompt mode and
    ``mark_complete()`` for agent-finished signalling.
    """

    def __init__(
        self,
        voice: "VoiceRecognizer | None" = None,
        initial_goal: str = "",
    ) -> None:
        self._voice = voice
        self._initial_goal = initial_goal

        # Threading primitives
        self._q: queue.Queue = queue.Queue()
        self._voice_q: queue.Queue = queue.Queue()
        self._toggle_event = threading.Event()
        self._goal_ready = threading.Event()
        self._submitted_goal = ""
        self._stop_callback: object = None

        self._thread: threading.Thread | None = None
        self._is_recording = False

        # Widget references (populated in _build_ui)
        self._root = None
        self._entry = None
        self._entry_border = None
        self._mic_lbl = None
        self._launch_lbl = None
        self._stop_lbl = None
        self._task_frame = None
        self._task_widgets: list = []
        self._bar_canvas = None
        self._bar_width = 0
        self._step_lbl = None
        self._status_lbl = None
        self._placeholder_lbl = None
        self._in_running_mode = False
        self._cached_tasks: list[str] = []
        self._last_state = None

    # ── public API ───────────────────────────────────────────────────

    def start(self) -> None:
        """Launch the overlay in a daemon thread and register the hotkey."""
        if self._thread is not None:
            return
        t = threading.Thread(target=self._run, name="glass-overlay", daemon=True)
        self._thread = t
        t.start()
        self._start_hotkey_listener()

    def stop(self) -> None:
        self._q.put(None)

    def toggle(self) -> None:
        """Toggle visibility (safe to call from any thread)."""
        self._toggle_event.set()

    def update(self, state) -> None:
        """Accept an OverlayState (or compatible) object from the agent."""
        self._q.put(state)

    def set_stop_callback(self, cb) -> None:
        """Register a callable fired when the user clicks **Stop**."""
        self._stop_callback = cb

    def wait_for_goal(self) -> str:
        """Block until the user submits a goal.  Returns the goal text."""
        self._goal_ready.wait()
        return self._submitted_goal

    def mark_complete(self) -> None:
        """Signal the overlay that the agent has finished."""
        self._q.put("__IDLE__")

    def hide_for_capture(self) -> None:
        """Temporarily hide the overlay so it won't appear in screenshots."""
        self._q.put("__HIDE__")
        import time; time.sleep(0.08)  # give tkinter time to process

    def show_after_capture(self) -> None:
        """Re-show the overlay after the screenshot is taken."""
        self._q.put("__SHOW__")

    # ── hotkey listener ──────────────────────────────────────────────

    def _start_hotkey_listener(self) -> None:
        try:
            from pynput import keyboard  # type: ignore
            hk = keyboard.GlobalHotKeys({"<ctrl>+<alt>+<space>": self.toggle})
            hk.daemon = True
            hk.start()
        except Exception as exc:
            log.warning("Failed to register Ctrl+Alt+Space hotkey: %s", exc)

    # ── tkinter main loop ────────────────────────────────────────────

    def _run(self) -> None:
        try:
            import tkinter as tk
        except Exception:
            log.error("tkinter unavailable — overlay disabled")
            return

        root = tk.Tk()
        self._root = root
        root.withdraw()
        root.title("KAI")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.94)
        root.configure(bg=_C.BG)

        W, H = 460, 480
        sx, sy = root.winfo_screenwidth(), root.winfo_screenheight()
        # Position at bottom-right corner (with padding) so it doesn't
        # interfere with the agent's mouse actions in the main workspace.
        pad_x, pad_y = 16, 48  # taskbar clearance
        root.geometry(f"{W}x{H}+{sx - W - pad_x}+{sy - H - pad_y}")

        self._build_ui(root, tk)
        root.update_idletasks()

        # DWM acrylic blur (best-effort)
        try:
            hwnd = root.winfo_id()
            parent = ctypes.windll.user32.GetParent(hwnd)
            target = parent if parent else hwnd
            if _apply_acrylic_blur(target):
                root.attributes("-alpha", 0.88)
        except Exception:
            pass

        # If launched with a goal already, switch to running mode
        if self._initial_goal:
            self._entry.delete(0, "end")
            self._entry.insert(0, self._initial_goal)
            self._entry.config(fg=_C.TEXT)
            self._set_running_mode(root)

        root.deiconify()
        root.after(80, lambda: self._poll(root, tk))
        root.mainloop()

    # ── UI construction ──────────────────────────────────────────────

    def _build_ui(self, root, tk) -> None:
        _placeholder = "Type your goal or use mic…"
        self._placeholder_text = _placeholder

        # ─── Title bar ───
        title_bar = tk.Frame(root, bg=_C.SURFACE, height=44)
        title_bar.pack(fill="x", side="top")
        title_bar.pack_propagate(False)

        logo = tk.Label(
            title_bar, text="  K A I", font=("Segoe UI", 13, "bold"),
            bg=_C.SURFACE, fg=_C.ACCENT_GLOW, anchor="w",
        )
        logo.pack(side="left", padx=(10, 0), fill="y")

        sub = tk.Label(
            title_bar, text="Desktop Automation", font=("Segoe UI", 9),
            bg=_C.SURFACE, fg=_C.TEXT_DIM, anchor="w",
        )
        sub.pack(side="left", padx=(10, 0), fill="y")

        close_btn = tk.Label(
            title_bar, text=" ✕ ", font=("Segoe UI", 13),
            bg=_C.SURFACE, fg=_C.TEXT_DIM, cursor="hand2",
        )
        close_btn.pack(side="right", padx=(0, 8))
        close_btn.bind("<Button-1>", lambda e: self._hide(root))
        close_btn.bind("<Enter>", lambda e: close_btn.config(fg=_C.ERROR))
        close_btn.bind("<Leave>", lambda e: close_btn.config(fg=_C.TEXT_DIM))

        # Drag
        for w in (title_bar, logo, sub):
            w.bind("<ButtonPress-1>", self._start_drag)
            w.bind("<B1-Motion>", lambda e, r=root: self._do_drag(e, r))

        tk.Frame(root, bg=_C.BORDER, height=1).pack(fill="x")

        # ─── Prompt section ───
        prompt_frame = tk.Frame(root, bg=_C.BG)
        prompt_frame.pack(fill="x", padx=22, pady=(16, 0))

        tk.Label(
            prompt_frame, text="What would you like me to do?",
            font=("Segoe UI", 11), bg=_C.BG, fg=_C.TEXT, anchor="w",
        ).pack(fill="x", pady=(0, 10))

        # Input row
        input_row = tk.Frame(prompt_frame, bg=_C.BG)
        input_row.pack(fill="x", pady=(0, 14))

        self._entry_border = tk.Frame(input_row, bg=_C.BORDER, padx=1, pady=1)
        self._entry_border.pack(side="left", fill="x", expand=True, padx=(0, 8))

        self._entry = tk.Entry(
            self._entry_border,
            font=("Segoe UI", 11),
            bg=_C.INPUT_BG, fg=_C.TEXT_FAINT,
            insertbackground=_C.TEXT,
            relief="flat", highlightthickness=0,
            selectbackground=_C.ACCENT, selectforeground=_C.TEXT,
        )
        self._entry.pack(fill="x", ipady=8, padx=6)
        self._entry.insert(0, _placeholder)

        self._entry.bind("<FocusIn>", self._on_focus_in)
        self._entry.bind("<FocusOut>", self._on_focus_out)
        self._entry.bind("<Return>", lambda e: self._on_submit())

        # Focus-ring colour change
        self._entry.bind("<FocusIn>",
            lambda e: self._entry_border.config(bg=_C.ACCENT), add="+")
        self._entry.bind("<FocusOut>",
            lambda e: self._entry_border.config(bg=_C.BORDER), add="+")

        # Mic button
        mic_border = tk.Frame(input_row, bg=_C.BORDER, padx=1, pady=1)
        mic_border.pack(side="right")

        self._mic_lbl = tk.Label(
            mic_border, text=" ● ", font=("Segoe UI", 16),
            bg=_C.SURFACE, fg=_C.TEXT_DIM, cursor="hand2", padx=4, pady=2,
        )
        self._mic_lbl.pack()
        self._mic_lbl.bind("<Button-1>", lambda e: self._on_mic_click())
        self._mic_lbl.bind("<Enter>", lambda e: (
            self._mic_lbl.config(bg=_C.SURFACE_HI) if not self._is_recording else None
        ))
        self._mic_lbl.bind("<Leave>", lambda e: (
            self._mic_lbl.config(bg=_C.SURFACE) if not self._is_recording else None
        ))

        # Buttons row
        btn_row = tk.Frame(prompt_frame, bg=_C.BG)
        btn_row.pack(fill="x", pady=(0, 4))

        self._launch_lbl = tk.Label(
            btn_row, text="  ▶  Launch Agent  ",
            font=("Segoe UI", 11, "bold"),
            bg=_C.ACCENT, fg="#ffffff", cursor="hand2", pady=10,
        )
        self._launch_lbl.pack(side="left", expand=True)
        self._launch_lbl.bind("<Button-1>", lambda e: self._on_submit())
        self._launch_lbl.bind("<Enter>", lambda e: self._launch_lbl.config(bg=_C.ACCENT_HOVER))
        self._launch_lbl.bind("<Leave>", lambda e: self._launch_lbl.config(bg=_C.ACCENT))

        self._stop_lbl = tk.Label(
            btn_row, text="  ■  Stop  ",
            font=("Segoe UI", 10, "bold"),
            bg=_C.ERROR, fg="#ffffff", cursor="hand2", pady=8,
        )
        # Stop button hidden initially
        self._stop_lbl.bind("<Button-1>", lambda e: self._on_stop())
        self._stop_lbl.bind("<Enter>", lambda e: self._stop_lbl.config(bg=_C.ERROR_HOVER))
        self._stop_lbl.bind("<Leave>", lambda e: self._stop_lbl.config(bg=_C.ERROR))

        # ─── Separator ───
        tk.Frame(root, bg=_C.BORDER, height=1).pack(fill="x", padx=18, pady=(12, 0))

        # ─── Progress section ───
        prog = tk.Frame(root, bg=_C.BG)
        prog.pack(fill="both", expand=True, padx=22, pady=(10, 8))

        tk.Label(
            prog, text="Task Progress",
            font=("Segoe UI", 10, "bold"), bg=_C.BG, fg=_C.TEXT_DIM, anchor="w",
        ).pack(fill="x", pady=(0, 8))

        self._placeholder_lbl = tk.Label(
            prog, text="Launch a task to see progress here",
            font=("Segoe UI", 9), bg=_C.BG, fg=_C.TEXT_FAINT, anchor="w",
        )
        self._placeholder_lbl.pack(fill="x")

        self._task_frame = tk.Frame(prog, bg=_C.BG)
        self._task_frame.pack(fill="x", pady=(0, 10))

        # Progress bar
        bar_row = tk.Frame(prog, bg=_C.BG)
        bar_row.pack(fill="x", pady=(0, 6))

        self._bar_canvas = tk.Canvas(
            bar_row, height=8, bg=_C.PROGRESS_BG, highlightthickness=0,
        )
        self._bar_canvas.pack(side="left", fill="x", expand=True)
        self._bar_canvas.update_idletasks()
        self._bar_width = max(1, self._bar_canvas.winfo_width())

        self._step_lbl = tk.Label(
            bar_row, text="", font=("Segoe UI", 8),
            bg=_C.BG, fg=_C.TEXT_DIM, anchor="e",
        )
        self._step_lbl.pack(side="right", padx=(8, 0))

        self._status_lbl = tk.Label(
            prog, text="", font=("Segoe UI", 9),
            bg=_C.BG, fg=_C.TEXT_DIM, anchor="w",
        )
        self._status_lbl.pack(fill="x")

        # ─── Hotkey hint ───
        tk.Label(
            root,
            text="Ctrl+Alt+Space to toggle  ·  Ctrl+Alt+Backspace to kill agent",
            font=("Segoe UI", 7), bg=_C.BG, fg=_C.TEXT_FAINT,
        ).pack(side="bottom", pady=(0, 6))

    # ── Polling loop ─────────────────────────────────────────────────

    _lift_counter: int = 0

    def _poll(self, root, tk) -> None:
        # Process state queue
        try:
            while True:
                item = self._q.get_nowait()
                if item is None:
                    root.destroy()
                    return
                if isinstance(item, str):
                    if item == "__IDLE__":
                        self._set_idle_mode(root)
                    elif item == "__HIDE__":
                        self._hide(root)
                    elif item == "__SHOW__":
                        self._show(root)
                else:
                    self._last_state = item
                    if not self._in_running_mode:
                        self._set_running_mode(root)
                    self._update_progress_display(item)
        except queue.Empty:
            pass

        # Process voice results
        try:
            while True:
                tag, payload = self._voice_q.get_nowait()
                self._is_recording = False
                self._mic_lbl.config(fg=_C.TEXT_DIM, text=" ● ", bg=_C.SURFACE)
                if tag == "ok" and payload:
                    self._entry.delete(0, "end")
                    self._entry.insert(0, payload)
                    self._entry.config(fg=_C.TEXT)
                elif tag == "err":
                    log.warning("Voice recognition error: %s", payload)
        except queue.Empty:
            pass

        # Process toggle
        if self._toggle_event.is_set():
            self._toggle_event.clear()
            self._toggle_visibility(root)

        # Periodic re-lift: keep overlay on top even when other apps steal focus
        # (every ~3 seconds = 30 poll cycles at 100ms)
        self._lift_counter += 1
        if self._lift_counter % 30 == 0:
            try:
                if root.state() != "withdrawn":
                    root.lift()
                    root.attributes("-topmost", True)
            except Exception:
                pass

        root.after(100, lambda: self._poll(root, tk))

    # ── Event handlers ───────────────────────────────────────────────

    def _on_focus_in(self, event=None) -> None:
        if self._entry.get() == self._placeholder_text:
            self._entry.delete(0, "end")
            self._entry.config(fg=_C.TEXT)

    def _on_focus_out(self, event=None) -> None:
        if not self._entry.get().strip():
            self._entry.delete(0, "end")
            self._entry.insert(0, self._placeholder_text)
            self._entry.config(fg=_C.TEXT_FAINT)

    def _on_submit(self) -> None:
        if self._in_running_mode:
            return
        text = self._entry.get().strip()
        if not text or text == self._placeholder_text:
            return
        self._submitted_goal = text
        self._goal_ready.set()
        self._set_running_mode(self._root)

    def _on_mic_click(self) -> None:
        if self._is_recording or self._in_running_mode:
            return
        if self._voice is None or not self._voice.available:
            log.warning("Voice recognizer not available")
            return
        self._is_recording = True
        self._mic_lbl.config(fg=_C.ERROR, text=" ◉ ", bg=_C.SURFACE_HI)

        def _record():
            try:
                text = self._voice.recognize_once(timeout=8.0, phrase_time_limit=7.0)
                self._voice_q.put(("ok", text or ""))
            except Exception as exc:
                self._voice_q.put(("err", str(exc)))

        threading.Thread(target=_record, name="voice-rec", daemon=True).start()

    def _on_stop(self) -> None:
        if self._stop_callback:
            try:
                self._stop_callback()
            except Exception:
                pass

    # ── Mode switching ───────────────────────────────────────────────

    def _set_running_mode(self, root) -> None:
        if self._in_running_mode:
            return
        self._in_running_mode = True
        self._entry.config(state="disabled", disabledbackground=_C.SURFACE,
                           disabledforeground=_C.TEXT_DIM)
        self._mic_lbl.config(cursor="arrow")
        self._launch_lbl.pack_forget()
        self._stop_lbl.pack(side="left", expand=True)
        if self._placeholder_lbl.winfo_manager():
            self._placeholder_lbl.pack_forget()

    def _set_idle_mode(self, root) -> None:
        self._in_running_mode = False
        self._entry.config(state="normal", bg=_C.INPUT_BG, fg=_C.TEXT)
        self._mic_lbl.config(cursor="hand2")
        self._stop_lbl.pack_forget()
        self._launch_lbl.pack(side="left", expand=True)
        self._goal_ready.clear()
        # Reset progress display
        for w in self._task_widgets:
            w.destroy()
        self._task_widgets.clear()
        self._cached_tasks.clear()
        self._bar_canvas.delete("bar")
        self._step_lbl.config(text="")
        self._status_lbl.config(text="✓ Task completed", fg=_C.SUCCESS)

    # ── Progress display ─────────────────────────────────────────────

    def _update_progress_display(self, state) -> None:
        """Render an OverlayState (or compatible) into the progress section."""
        goal = getattr(state, "goal", "")
        step = getattr(state, "step", 0)
        max_steps = getattr(state, "max_steps", 60)
        progress = getattr(state, "progress", "")
        last_action = getattr(state, "last_action", "") or ""
        tasks = list(getattr(state, "checklist_tasks", ()) or ())
        completed = set(getattr(state, "checklist_completed", frozenset()) or frozenset())

        # Update task checklist
        if tasks:
            self._update_task_list(tasks, completed)

        # Update progress bar
        self._update_bar(step, max_steps)

        # Step label
        pct = int(step / max(1, max_steps) * 100) if max_steps else 0
        self._step_lbl.config(text=f"Step {step}/{max_steps}  ({pct}%)")

        # Status line
        parts: list[str] = []
        if progress:
            parts.append(progress[:60])
        if last_action:
            parts.append(f"Last: {last_action[:50]}")
        self._status_lbl.config(text="  ·  ".join(parts) if parts else "", fg=_C.TEXT_DIM)

    def _update_task_list(self, tasks: list[str], completed: set[str]) -> None:
        """Create or update the task checklist labels."""
        if tasks != self._cached_tasks:
            # Rebuild
            for w in self._task_widgets:
                w.destroy()
            self._task_widgets.clear()
            self._cached_tasks = list(tasks)

            import tkinter as tk
            for task_name in tasks:
                lbl = tk.Label(
                    self._task_frame, text="",
                    font=("Segoe UI", 9), bg=_C.BG, anchor="w", padx=4, pady=1,
                )
                lbl.pack(fill="x")
                self._task_widgets.append(lbl)

        # Update icons and colours
        for i, task_name in enumerate(tasks):
            if i >= len(self._task_widgets):
                break
            if task_name in completed:
                self._task_widgets[i].config(
                    text=f"  ✓   {task_name}", fg=_C.SUCCESS,
                )
            else:
                # Determine if this is the current in-progress task
                # (first uncompleted task)
                is_current = all(t in completed for t in tasks[:i])
                if is_current and task_name not in completed:
                    self._task_widgets[i].config(
                        text=f"  ▸   {task_name}", fg=_C.WARNING,
                    )
                else:
                    self._task_widgets[i].config(
                        text=f"  ○   {task_name}", fg=_C.TEXT_FAINT,
                    )

    def _update_bar(self, step: int, max_steps: int) -> None:
        """Redraw the canvas progress bar."""
        self._bar_canvas.delete("bar")
        # Re-measure in case of resize
        self._bar_width = max(1, self._bar_canvas.winfo_width())
        if max_steps <= 0:
            return
        pct = min(1.0, step / max_steps)
        fill_w = max(2, int(self._bar_width * pct))
        self._bar_canvas.create_rectangle(
            0, 0, fill_w, 8, fill=_C.PROGRESS_FILL, outline="", tags="bar",
        )

    # ── Drag ─────────────────────────────────────────────────────────

    _drag_x: int = 0
    _drag_y: int = 0

    def _start_drag(self, event) -> None:
        self._drag_x = event.x
        self._drag_y = event.y

    def _do_drag(self, event, root) -> None:
        x = root.winfo_x() + event.x - self._drag_x
        y = root.winfo_y() + event.y - self._drag_y
        root.geometry(f"+{x}+{y}")

    # ── Visibility ───────────────────────────────────────────────────

    def _hide(self, root) -> None:
        root.withdraw()

    def _show(self, root) -> None:
        root.deiconify()
        root.lift()

    def _toggle_visibility(self, root) -> None:
        try:
            if root.state() == "withdrawn":
                self._show(root)
            else:
                self._hide(root)
        except Exception:
            self._show(root)
