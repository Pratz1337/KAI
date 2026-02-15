from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

import mss
import mss.tools


@dataclass(frozen=True)
class Screenshot:
    png: bytes
    width: int
    height: int
    monitor_index: int
    monitor: dict[str, Any]


class ScreenCapturer:
    def __init__(self, monitor_index: int = 1, max_width: int | None = 1280):
        # mss monitors are 1-based; 1 is "primary"
        self._monitor_index = monitor_index
        self._max_width = max_width
        self._sct = mss.mss()

    def capture(self) -> Screenshot:
        monitors = self._sct.monitors
        if self._monitor_index < 1 or self._monitor_index >= len(monitors):
            raise ValueError(
                f"Invalid monitor_index={self._monitor_index}. "
                f"Valid range is 1..{len(monitors) - 1}."
            )

        mon = dict(monitors[self._monitor_index])
        # mss uses monitors[0] as the virtual screen bounding box.
        vmon = monitors[0] if monitors else None
        if isinstance(vmon, dict):
            mon["__virtual_screen_left"] = int(vmon.get("left", 0))
            mon["__virtual_screen_top"] = int(vmon.get("top", 0))
            mon["__virtual_screen_width"] = int(vmon.get("width", 0))
            mon["__virtual_screen_height"] = int(vmon.get("height", 0))
        shot = self._sct.grab(mon)
        png = mss.tools.to_png(shot.rgb, shot.size)

        width, height = shot.size
        if self._max_width is not None and width > self._max_width:
            png, width, height = _downscale_png(png, self._max_width, width, height)

        return Screenshot(
            png=png,
            width=width,
            height=height,
            monitor_index=self._monitor_index,
            monitor=mon,
        )


def _downscale_png(png: bytes, max_width: int, width: int, height: int) -> tuple[bytes, int, int]:
    """
    Downscale with Pillow if available. If Pillow isn't present, return original.
    """
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return png, width, height

    img = Image.open(io.BytesIO(png))
    if img.width <= max_width:
        return png, img.width, img.height

    new_h = max(1, int(img.height * (max_width / img.width)))
    img = img.resize((max_width, new_h), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue(), img.width, img.height
