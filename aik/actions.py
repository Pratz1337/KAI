from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


class ActionParseError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedPlan:
    actions: list[dict[str, Any]]


ALLOWED_ACTION_TYPES = {"type_text", "key_press", "hotkey", "wait_ms", "stop"}


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

    return ParsedPlan(actions=parsed)


def _normalize_action(a: dict[str, Any], idx: int) -> dict[str, Any]:
    t = a["type"]

    if t == "type_text":
        text = a.get("text")
        if not isinstance(text, str):
            raise ActionParseError(f'Action #{idx} type_text requires string field "text".')
        return {"type": t, "text": text}

    if t == "key_press":
        key = a.get("key")
        if not isinstance(key, str):
            raise ActionParseError(f'Action #{idx} key_press requires string field "key".')
        return {"type": t, "key": key.strip().lower()}

    if t == "hotkey":
        keys = a.get("keys")
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            raise ActionParseError(f'Action #{idx} hotkey requires string array field "keys".')
        norm = [k.strip().lower() for k in keys if k.strip()]
        if not norm:
            raise ActionParseError(f"Action #{idx} hotkey needs at least 1 key.")
        return {"type": t, "keys": norm}

    if t == "wait_ms":
        ms = a.get("ms")
        if not isinstance(ms, (int, float)):
            raise ActionParseError(f'Action #{idx} wait_ms requires numeric field "ms".')
        ms_i = int(ms)
        if ms_i < 0 or ms_i > 60_000:
            raise ActionParseError(f"Action #{idx} wait_ms.ms out of range (0..60000).")
        return {"type": t, "ms": ms_i}

    if t == "stop":
        reason = a.get("reason", "")
        if reason is None:
            reason = ""
        if not isinstance(reason, str):
            reason = str(reason)
        return {"type": t, "reason": reason}

    raise ActionParseError(f"Internal: unhandled action type {t!r}")


def _strip_code_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        # Remove first fence line, and last fence if present.
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    return s


def _loads_first_json_object(text: str) -> Any:
    s = _strip_code_fences(text)
    s = s.strip()
    if not s:
        raise ActionParseError("Empty model response.")

    # Fast path.
    try:
        return json.loads(s)
    except Exception:
        pass

    # Common failure mode: model prints extra text around the JSON.
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ActionParseError("Could not find a JSON object in the model response.")

    candidate = s[start : end + 1]
    try:
        return json.loads(candidate)
    except Exception as e:
        raise ActionParseError(f"Failed to parse JSON: {e}") from e

