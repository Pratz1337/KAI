from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

from .capture import Screenshot
from .window_context import ForegroundWindow

log = logging.getLogger("aik.failure_detector")


@dataclass(frozen=True)
class FailureSignals:
    screen_similarity: float
    screen_unchanged: bool
    window_title_changed: bool
    window_process_changed: bool
    error_dialog_suspected: bool = False
    details: str = ""


def detect_failure(
    *,
    before_shot: Screenshot,
    after_shot: Screenshot,
    before_fg: ForegroundWindow,
    after_fg: ForegroundWindow,
    unchanged_similarity_threshold: float = 0.985,
    expected_process: str | None = None,
) -> FailureSignals:
    """
    Detect common failure patterns after an action sequence.

    Checks:
    - Screen unchanged (action had no visible effect)
    - Window title changed unexpectedly
    - Window process changed (wrong app opened)
    - Error dialog suspected (heuristic based on window title keywords)
    """
    sim = screen_similarity(before_shot.png, after_shot.png)
    unchanged = sim >= unchanged_similarity_threshold

    title_changed = (before_fg.title or "") != (after_fg.title or "")
    proc_changed = (before_fg.process_path or "") != (after_fg.process_path or "")

    # Heuristic: detect error/UAC dialogs from window title
    error_dialog = _suspect_error_dialog(after_fg.title or "")

    # Check if we ended up in the wrong application
    wrong_app = False
    if expected_process and after_fg.process_path:
        wrong_app = (
            expected_process.lower() not in (after_fg.process_path or "").lower()
        )

    details_parts: list[str] = [f"screen_similarity={sim:.3f}"]
    if title_changed:
        details_parts.append(f"window_title_changed: {before_fg.title!r} -> {after_fg.title!r}")
    if proc_changed:
        details_parts.append(
            f"window_process_changed: {before_fg.process_path!r} -> {after_fg.process_path!r}"
        )
    if unchanged:
        details_parts.append("screen_unchanged=true (action may have had no effect)")
    if error_dialog:
        details_parts.append(f"error_dialog_suspected: title={after_fg.title!r}")
    if wrong_app:
        details_parts.append(f"wrong_app: expected={expected_process!r}, got={after_fg.process_path!r}")

    return FailureSignals(
        screen_similarity=sim,
        screen_unchanged=unchanged,
        window_title_changed=title_changed,
        window_process_changed=proc_changed,
        error_dialog_suspected=error_dialog,
        details="; ".join(details_parts),
    )


def _suspect_error_dialog(title: str) -> bool:
    """Heuristic: check if the foreground window title suggests an error or UAC dialog."""
    keywords = [
        "error", "warning", "denied", "access denied", "permission",
        "user account control", "uac", "not responding", "crash",
        "has stopped", "do you want", "are you sure",
    ]
    t = title.lower()
    return any(kw in t for kw in keywords)


def screen_similarity(png_a: bytes, png_b: bytes) -> float:
    """
    Return [0..1] similarity where 1.0 means identical-looking.

    Uses Pillow when available; falls back to byte equality.
    """
    if png_a == png_b:
        return 1.0

    try:
        from PIL import Image, ImageChops  # type: ignore
    except Exception:
        return 0.0

    try:
        img1 = Image.open(io.BytesIO(png_a)).convert("L").resize((64, 64))
        img2 = Image.open(io.BytesIO(png_b)).convert("L").resize((64, 64))
        diff = ImageChops.difference(img1, img2)
        # Mean absolute difference in [0..255]
        mean = sum(diff.getdata()) / (64 * 64)
        sim = 1.0 - (float(mean) / 255.0)
        return max(0.0, min(1.0, sim))
    except Exception as e:
        log.debug("screen_similarity failed, falling back to 0.0: %s", e)
        return 0.0
