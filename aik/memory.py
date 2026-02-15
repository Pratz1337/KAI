from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class Memory:
    path: str
    data: dict[str, Any]

    @classmethod
    def load(cls, path: str) -> "Memory":
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except FileNotFoundError:
            data = {}
        except Exception:
            data = {}
        return cls(path=path, data=data)

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=True)
        os.replace(tmp, self.path)

    def remember_target(self, *, app: str, name: str, x: float, y: float, meta: dict[str, Any] | None = None) -> None:
        app = (app or "").strip().lower() or "unknown"
        name = (name or "").strip().lower() or "target"
        targets = self.data.setdefault("targets", {})
        app_map = targets.setdefault(app, {})
        app_map[name] = {
            "x": float(x),
            "y": float(y),
            "meta": meta or {},
            "ts": int(time.time()),
        }
        self.save()

    def get_target(self, *, app: str, name: str) -> tuple[float, float] | None:
        app = (app or "").strip().lower() or "unknown"
        name = (name or "").strip().lower() or "target"
        try:
            t = self.data.get("targets", {}).get(app, {}).get(name)
            if not isinstance(t, dict):
                return None
            x = float(t.get("x"))
            y = float(t.get("y"))
            if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
                return x, y
        except Exception:
            return None
        return None

    def append_event(self, event: dict[str, Any]) -> None:
        events = self.data.setdefault("events", [])
        if not isinstance(events, list):
            events = []
            self.data["events"] = events
        events.append(event)
        # Keep bounded.
        if len(events) > 200:
            del events[: len(events) - 200]
        self.save()
