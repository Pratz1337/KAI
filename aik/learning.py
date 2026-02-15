"""Learning graph for AIK agent — stores successful/failed patterns locally."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class LearningGraph:
    """Persistent storage for what the agent has learned across sessions."""

    path: str
    data: dict[str, Any]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: str) -> "LearningGraph":
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except (FileNotFoundError, json.JSONDecodeError):
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

    # ------------------------------------------------------------------
    # Record outcomes
    # ------------------------------------------------------------------

    def record_success(
        self,
        *,
        app: str,
        goal: str,
        actions: list[dict],
        note: str = "",
    ) -> None:
        """Record a successful action sequence."""
        bucket = self.data.setdefault("successes", [])
        bucket.append(
            {
                "app": _norm(app),
                "goal": goal.strip(),
                "actions": actions[-12:],
                "note": note,
                "ts": int(time.time()),
            }
        )
        if len(bucket) > 150:
            del bucket[: len(bucket) - 150]
        self.save()

    def record_failure(
        self,
        *,
        app: str,
        goal: str,
        action: dict,
        reason: str = "",
    ) -> None:
        """Record a single failed action so we avoid it next time."""
        bucket = self.data.setdefault("failures", [])
        bucket.append(
            {
                "app": _norm(app),
                "goal": goal.strip(),
                "action": action,
                "reason": reason,
                "ts": int(time.time()),
            }
        )
        if len(bucket) > 200:
            del bucket[: len(bucket) - 200]
        self.save()

    def add_tip(self, *, app: str, tip: str) -> None:
        """Store a learned tip for an app (deduplicated)."""
        tips = self.data.setdefault("tips", {})
        key = _norm(app) or "general"
        app_tips = tips.setdefault(key, [])
        if tip not in app_tips:
            app_tips.append(tip)
            if len(app_tips) > 30:
                del app_tips[: len(app_tips) - 30]
            self.save()

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_tips(self, *, app: str, goal: str) -> list[str]:
        """Return tips relevant to the current context."""
        result: list[str] = []
        tips = self.data.get("tips", {})
        key = _norm(app)

        if key and key in tips:
            result.extend(tips[key])
        if "general" in tips:
            result.extend(tips["general"])

        # Mine successful sequences for notes
        for seq in self.data.get("successes", []):
            note = seq.get("note", "")
            if note and _goal_overlaps(seq.get("goal", ""), goal):
                result.append(f"Past success: {note}")

        # Deduplicate, keep order
        seen: set[str] = set()
        deduped: list[str] = []
        for t in result:
            if t not in seen:
                seen.add(t)
                deduped.append(t)
        return deduped[:12]

    def get_recent_failures(self, *, app: str, goal: str) -> list[dict]:
        """Return recently failed actions so the model can avoid them."""
        failures = self.data.get("failures", [])
        key = _norm(app)
        relevant: list[dict] = []
        for f in failures:
            if f.get("app", "") == key or _goal_overlaps(f.get("goal", ""), goal):
                relevant.append(
                    {"action": f.get("action", {}), "reason": f.get("reason", "")}
                )
        return relevant[-6:]

    def get_successful_patterns(self, *, app: str, goal: str) -> list[dict]:
        """Return action patterns that worked before for similar goals."""
        successes = self.data.get("successes", [])
        relevant: list[dict] = []
        for s in successes:
            if _goal_overlaps(s.get("goal", ""), goal):
                relevant.append(
                    {"actions": s.get("actions", []), "note": s.get("note", "")}
                )
        return relevant[-3:]


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_STOP_WORDS = frozenset(
    {"a", "an", "the", "to", "in", "on", "for", "and", "or", "is", "it", "of", "my", "i", "me", "do"}
)


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _goal_overlaps(goal1: str, goal2: str) -> bool:
    """True if two goals share enough meaningful keywords."""
    w1 = set(goal1.lower().split()) - _STOP_WORDS
    w2 = set(goal2.lower().split()) - _STOP_WORDS
    if not w1 or not w2:
        return False
    overlap = w1 & w2
    return len(overlap) >= 2 or (len(overlap) / max(len(w1), len(w2))) > 0.4
