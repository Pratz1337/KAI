"""Parse and validate the JSON action plan returned by the VLM.

Supports keyboard actions (type_text, key_press, hotkey, wait_ms)
AND mouse actions (mouse_click, mouse_scroll).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


class ActionParseError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedPlan:
    actions: list[dict[str, Any]]
    meta: dict[str, Any] | None = None


ALLOWED_ACTION_TYPES = {
    "type_text",
    "key_press",
    "hotkey",
    "wait_ms",
    "stop",
    "ask_user",
    # mouse
    "mouse_click",
    "mouse_scroll",
}


def parse_plan(text: str) -> ParsedPlan:
    obj = _loads_first_json_object(text)
    if not isinstance(obj, dict):
        raise ActionParseError("Top-level JSON must be an object.")

    actions = obj.get("actions")
    if not isinstance(actions, list):
        raise ActionParseError('JSON must contain an "actions" array.')

    parsed: list[dict[str, Any]] = []
    for i, a in enumerate(actions):
        if not isinstance(a, dict):
            raise ActionParseError(f"Action #{i} must be an object.")
        if not isinstance(a.get("type"), str):
            raise ActionParseError(f'Action #{i} missing string field "type".')
        t = str(a["type"]).strip().lower()
        if t not in ALLOWED_ACTION_TYPES:
            raise ActionParseError(f"Action #{i} has unsupported type: {t!r}")
        parsed.append(_normalize_action({**a, "type": t}, i))

    meta = obj.get("meta")
    if meta is not None and not isinstance(meta, dict):
        meta = {"value": meta}

    return ParsedPlan(actions=parsed, meta=meta)


# ── per-type normalizers ─────────────────────────────────────────────────────

def _normalize_action(a: dict[str, Any], idx: int) -> dict[str, Any]:
    t = a["type"]

    if t == "type_text":
        text = a.get("text")
        if not isinstance(text, str):
            raise ActionParseError(f'Action #{idx} type_text requires string "text".')
        return {"type": t, "text": text}

    if t == "key_press":
        key = a.get("key")
        if not isinstance(key, str):
            raise ActionParseError(f'Action #{idx} key_press requires string "key".')
        return {"type": t, "key": key.strip().lower()}

    if t == "hotkey":
        keys = a.get("keys")
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            raise ActionParseError(f'Action #{idx} hotkey requires string array "keys".')
        norm = [k.strip().lower() for k in keys if k.strip()]
        if not norm:
            raise ActionParseError(f"Action #{idx} hotkey needs at least 1 key.")
        return {"type": t, "keys": norm}

    if t == "wait_ms":
        ms = a.get("ms")
        if not isinstance(ms, (int, float)):
            raise ActionParseError(f'Action #{idx} wait_ms requires numeric "ms".')
        ms_i = int(ms)
        if ms_i < 0 or ms_i > 60_000:
            raise ActionParseError(f"Action #{idx} wait_ms.ms out of range (0..60000).")
        return {"type": t, "ms": ms_i}

    if t == "stop":
        reason = a.get("reason", "")
        if reason is None:
            reason = ""
        return {"type": t, "reason": str(reason)}

    if t == "ask_user":
        question = a.get("question")
        options = a.get("options")
        if not isinstance(question, str) or not question.strip():
            raise ActionParseError(f'Action #{idx} ask_user requires non-empty "question".')
        if not isinstance(options, list) or not all(isinstance(o, str) and o.strip() for o in options):
            raise ActionParseError(f'Action #{idx} ask_user requires string array "options".')
        opts = [o.strip() for o in options][:6]
        if not opts:
            raise ActionParseError(f"Action #{idx} ask_user requires at least 1 option.")
        return {"type": t, "question": question.strip(), "options": opts}

    # ── mouse actions ────────────────────────────────────────────────────

    if t == "mouse_click":
        x = _coerce_coord(a.get("x"), idx, "x")
        y = _coerce_coord(a.get("y"), idx, "y")
        button = str(a.get("button", "left")).strip().lower()
        if button not in ("left", "right", "middle"):
            button = "left"
        clicks = int(a.get("clicks", 1))
        clicks = max(1, min(clicks, 3))
        return {"type": t, "x": x, "y": y, "button": button, "clicks": clicks}

    if t == "mouse_scroll":
        x = _coerce_coord(a.get("x"), idx, "x")
        y = _coerce_coord(a.get("y"), idx, "y")
        direction = str(a.get("direction", "down")).strip().lower()
        if direction not in ("up", "down"):
            direction = "down"
        clicks = int(a.get("clicks", 3))
        clicks = max(1, min(clicks, 20))
        return {"type": t, "x": x, "y": y, "direction": direction, "clicks": clicks}

    raise ActionParseError(f"Internal: unhandled action type {t!r}")


# ── coordinate helper ────────────────────────────────────────────────────────

def _coerce_coord(val: Any, idx: int, name: str) -> int:
    """Accept int, float, or string pixel coordinate and return an int."""
    if val is None:
        raise ActionParseError(f'Action #{idx} requires "{name}" coordinate.')
    try:
        v = float(val)
    except (TypeError, ValueError):
        raise ActionParseError(f'Action #{idx} "{name}" must be numeric (got {val!r}).')
    # Clamp negatives to 0
    if v < 0:
        v = 0
    return int(round(v))


# ── JSON extraction helpers ──────────────────────────────────────────────────

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
        raise ActionParseError("Empty model response.")

    # Fast path
    try:
        return json.loads(s)
    except Exception:
        pass

    # Extract first { … } block
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ActionParseError("Could not find a JSON object in the model response.")

    candidate = s[start: end + 1]
    try:
        return json.loads(candidate)
    except Exception as e:
        raise ActionParseError(f"Failed to parse JSON: {e}") from e

