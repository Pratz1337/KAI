from __future__ import annotations

import queue
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class OverlayState:
    goal: str
    step: int
    max_steps: int
    mode: str
    progress: str
    estimated_total_steps: int | None = None
    last_action: str | None = None
    # Checklist data for the glass overlay progress tracker
    checklist_tasks: tuple[str, ...] = ()
    checklist_completed: frozenset[str] = frozenset()


class Overlay:
    def __init__(self) -> None:
        self._q: queue.Queue[OverlayState | None] = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return

        t = threading.Thread(target=self._run, name="aik-overlay", daemon=True)
        self._thread = t
        t.start()

    def stop(self) -> None:
        self._q.put(None)

    def update(self, state: OverlayState) -> None:
        self._q.put(state)

    def _run(self) -> None:
        try:
            import tkinter as tk
        except Exception:
            return

        root = tk.Tk()
        root.title("AIK")
        root.attributes("-topmost", True)
        root.resizable(False, False)
        # Small, non-intrusive overlay.
        root.geometry("360x140+20+20")

        lbl = tk.Label(root, text="AIK", justify="left", anchor="nw", font=("Segoe UI", 10))
        lbl.pack(fill="both", expand=True, padx=10, pady=10)

        state: OverlayState | None = None

        def render() -> None:
            nonlocal state
            if state is None:
                return
            est = "?" if not state.estimated_total_steps else str(state.estimated_total_steps)
            last = state.last_action or ""
            text = (
                f"Goal: {state.goal}\n"
                f"Mode: {state.mode}\n"
                f"Step: {state.step}/{state.max_steps} (est: {est})\n"
                f"Progress: {state.progress}\n"
            )
            if last:
                text += f"Last: {last}\n"
            lbl.config(text=text)

        def poll() -> None:
            nonlocal state
            try:
                while True:
                    item = self._q.get_nowait()
                    if item is None:
                        root.destroy()
                        return
                    state = item
            except queue.Empty:
                pass
            render()
            root.after(150, poll)

        root.after(150, poll)
        root.mainloop()
