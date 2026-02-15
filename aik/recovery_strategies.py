from __future__ import annotations

import logging
from dataclasses import dataclass

from .failure_detector import FailureSignals

log = logging.getLogger("aik.recovery_strategies")


@dataclass(frozen=True)
class RecoveryAdvice:
    note: str
    severity: str = "info"  # "info", "warning", "critical"
    suggested_actions: list[str] | None = None


def suggest_recovery(signals: FailureSignals) -> RecoveryAdvice | None:
    """
    Convert low-level failure signals into actionable recovery advice.

    Returns a RecoveryAdvice that gets fed back into the LLM planning step
    as an observation, so the model can adjust its strategy.

    This module intentionally does not perform any "automatic" recovery keystrokes.
    Instead, it feeds the observation back into the next LLM planning step.
    """

    # Error dialog takes highest priority
    if signals.error_dialog_suspected:
        return RecoveryAdvice(
            note=(
                "Observation: An error dialog, UAC prompt, or warning may have appeared. "
                "The current window title suggests an error or security prompt. "
                "Try pressing Escape or Enter to dismiss it, or Alt+Tab to return "
                "to the target application."
            ),
            severity="critical",
            suggested_actions=[
                "Press Escape to dismiss dialog",
                "Press Enter to accept default",
                "Alt+Tab to return to target app",
            ],
        )

    # Screen unchanged — the action probably did nothing
    if signals.screen_unchanged:
        return RecoveryAdvice(
            note=(
                "Observation: The screen looks unchanged after the last keys. "
                "The shortcut may not have worked, or the target window may not "
                "be focused. Try an alternative navigation path. "
                "For example, if Ctrl+N didn't work, try File menu > New instead."
            ),
            severity="warning",
            suggested_actions=[
                "Try alternative keyboard shortcut",
                "Use menu navigation (Alt+F, then menu item letter)",
                "Click on the target window first (Alt+Tab)",
                "Try Win+R for Run dialog if launching apps",
            ],
        )

    # Window changed unexpectedly
    if signals.window_process_changed:
        return RecoveryAdvice(
            note=(
                "Observation: A different application came to the foreground "
                "after the last keys. This may mean the wrong app opened, or "
                "another app stole focus. Use Alt+Tab to return to the intended "
                "application, or close the unexpected window with Alt+F4."
            ),
            severity="warning",
            suggested_actions=[
                "Alt+Tab to return to intended app",
                "Alt+F4 to close unexpected window",
                "Verify correct app is focused before retrying",
            ],
        )

    if signals.window_title_changed:
        return RecoveryAdvice(
            note=(
                "Observation: The window title changed after the last action. "
                "This could be normal (e.g., a dialog opened) or unexpected. "
                "Verify the current state matches your expectations before proceeding."
            ),
            severity="info",
        )

    return None
