from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class KillSwitchConfig:
    enabled: bool = True
    # Ctrl+Alt+Backspace is widely available; Pause key isn't on all keyboards.
    ctrl_alt_backspace: bool = True


class KillSwitch:
    def __init__(self, cfg: KillSwitchConfig | None = None) -> None:
        self._cfg = cfg or KillSwitchConfig()
        self._triggered = threading.Event()
        self._listener = None

    @property
    def triggered(self) -> bool:
        return self._triggered.is_set()

    def start(self) -> None:
        if not self._cfg.enabled:
            return

        try:
            from pynput import keyboard  # type: ignore
        except Exception:
            # If pynput isn't available, we can't provide a global kill switch.
            return

        pressed = set()

        def key_attr(name: str):
            return getattr(keyboard.Key, name, None)

        ctrl_keys = {k for k in [key_attr("ctrl"), key_attr("ctrl_l"), key_attr("ctrl_r")] if k is not None}
        alt_keys = {k for k in [key_attr("alt"), key_attr("alt_l"), key_attr("alt_r"), key_attr("alt_gr")] if k is not None}
        backspace_key = key_attr("backspace")
        if backspace_key is None:
            return

        def has_any(s: set, options: set) -> bool:
            return any(k in s for k in options)

        def on_press(key):
            pressed.add(key)
            if self._cfg.ctrl_alt_backspace:
                if has_any(pressed, ctrl_keys) and has_any(pressed, alt_keys) and backspace_key in pressed:
                    self._triggered.set()
                    return False  # stop listener

        def on_release(key):
            pressed.discard(key)

        self._listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._listener.daemon = True
        self._listener.start()
