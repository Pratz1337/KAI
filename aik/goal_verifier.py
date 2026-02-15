from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from .anthropic_client import AnthropicClient

log = logging.getLogger("aik.goal_verifier")


VERIFY_SYSTEM_PROMPT = """\
You are a strict verifier for a Windows desktop automation goal.

You will be given:
- The original user goal (text)
- The current active window title + process path (may be empty)
- A screenshot of the user's screen

Decide whether the goal is actually achieved in the screenshot.

Return ONLY valid JSON (no markdown, no code fences, no extra text) with this schema:
{
  "goal_achieved": true|false,
  "confidence": 0.0-1.0,
  "evidence": "what you see that supports the decision",
  "missing": "what is missing / what would need to be visible to say it's achieved"
}

Rules:
- Be conservative. If you are not sure from the screenshot, set goal_achieved=false with low confidence.
- Do not assume actions succeeded unless the UI clearly shows the result.
"""


@dataclass(frozen=True)
class GoalVerificationResult:
    verified: bool
    confidence: float
    reason: str
    evidence: str = ""
    missing: str = ""
    raw: dict[str, Any] | None = None
    model_text: str | None = None


class GoalVerifier:
    def __init__(
        self,
        anthropic: AnthropicClient,
        *,
        max_tokens: int = 300,
        temperature: float = 0.0,
    ) -> None:
        self._anthropic = anthropic
        self._max_tokens = max_tokens
        self._temperature = temperature

    def verify(
        self,
        *,
        goal: str,
        screenshot_png: bytes,
        window_title: str = "",
        process_path: str | None = None,
        step: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> GoalVerificationResult:
        payload: dict[str, Any] = {
            "original_goal": goal,
            "active_window_title": window_title or "",
            "active_process_path": process_path,
        }
        if step is not None:
            payload["step"] = int(step)
        if extra:
            payload["extra"] = extra

        user_text = (
            "Verify whether the goal has been achieved.\n"
            "Context:\n"
            f"{json.dumps(payload, ensure_ascii=True)}"
        )

        try:
            resp = self._anthropic.create_message(
                system=VERIFY_SYSTEM_PROMPT,
                user_text=user_text,
                image_png=screenshot_png,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )
        except Exception as e:
            log.exception("Goal verification API call failed: %s", e)
            return GoalVerificationResult(
                verified=False,
                confidence=0.0,
                reason=f"verification_error: {e}",
            )

        try:
            obj = _loads_first_json_object(resp.text)
        except Exception as e:
            return GoalVerificationResult(
                verified=False,
                confidence=0.0,
                reason=f"verification_parse_error: {e}",
                raw=resp.raw if isinstance(resp.raw, dict) else None,
                model_text=resp.text,
            )

        if not isinstance(obj, dict):
            return GoalVerificationResult(
                verified=False,
                confidence=0.0,
                reason="verification_invalid_json: expected object",
                raw={"parsed": obj},
                model_text=resp.text,
            )

        achieved = bool(obj.get("goal_achieved", False))
        confidence = _coerce_float(obj.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))
        evidence = _coerce_str(obj.get("evidence", ""))
        missing = _coerce_str(obj.get("missing", ""))
        reason = evidence if achieved else (missing or evidence or "goal not achieved")

        return GoalVerificationResult(
            verified=achieved,
            confidence=confidence,
            reason=reason,
            evidence=evidence,
            missing=missing,
            raw=obj,
            model_text=resp.text,
        )


def _coerce_float(v: Any) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def _coerce_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    return str(v).strip()


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

