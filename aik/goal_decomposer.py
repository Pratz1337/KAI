from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from .anthropic_client import AnthropicClient

log = logging.getLogger("aik.goal_decomposer")


DECOMPOSE_SYSTEM_PROMPT = """\
You decompose a Windows desktop automation goal into a short sequence of verifiable stages.

Return ONLY valid JSON (no markdown, no code fences, no extra text) with this schema:
{
  "stages": [
    {
      "name": "short stage name",
      "description": "what should be true when this stage is complete",
      "verify": "how to verify this stage from a screenshot (signals to look for)"
    }
  ]
}

Rules:
- Prefer 3-8 stages.
- Each stage must be visually verifiable from a screenshot and/or window title.
- Keep stages concrete and UI-focused.
"""


@dataclass(frozen=True)
class GoalStage:
    name: str
    description: str
    verify: str


@dataclass(frozen=True)
class GoalDecomposition:
    stages: list[GoalStage]
    raw: dict[str, Any] | None = None
    model_text: str | None = None


class GoalDecomposer:
    def __init__(
        self,
        anthropic: AnthropicClient,
        *,
        max_tokens: int = 500,
        temperature: float = 0.2,
    ) -> None:
        self._anthropic = anthropic
        self._max_tokens = max_tokens
        self._temperature = temperature

    def decompose(self, goal: str, *, max_stages: int = 8) -> GoalDecomposition:
        user_text = (
            "Decompose this goal into verifiable stages.\n"
            f"Goal: {goal!r}\n"
            f"Max stages: {int(max_stages)}"
        )
        try:
            resp = self._anthropic.create_message(
                system=DECOMPOSE_SYSTEM_PROMPT,
                user_text=user_text,
                image_png=None,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )
        except Exception as e:
            log.exception("Goal decomposition API call failed: %s", e)
            return GoalDecomposition(stages=[], raw=None, model_text=None)

        try:
            obj = _loads_first_json_object(resp.text)
        except Exception:
            return GoalDecomposition(stages=[], raw=resp.raw, model_text=resp.text)

        if not isinstance(obj, dict):
            return GoalDecomposition(stages=[], raw={"parsed": obj}, model_text=resp.text)

        stages_in = obj.get("stages", [])
        if not isinstance(stages_in, list):
            return GoalDecomposition(stages=[], raw=obj, model_text=resp.text)

        stages: list[GoalStage] = []
        for item in stages_in[:max(1, int(max_stages))]:
            if not isinstance(item, dict):
                continue
            name = _coerce_str(item.get("name", "")).strip()
            desc = _coerce_str(item.get("description", "")).strip()
            verify = _coerce_str(item.get("verify", "")).strip()
            if not name or not (desc or verify):
                continue
            stages.append(GoalStage(name=name, description=desc, verify=verify))

        return GoalDecomposition(stages=stages, raw=obj, model_text=resp.text)


def _coerce_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return str(v)


def _strip_code_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    return s


def _loads_first_json_object(text: str) -> Any:
    s = _strip_code_fences(text).strip()
    if not s:
        raise ValueError("Empty model response")
    try:
        return json.loads(s)
    except Exception:
        pass
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Could not find JSON object in model response")
    return json.loads(s[start : end + 1])

