from __future__ import annotations

import json
from dataclasses import dataclass, field


SYSTEM_PROMPT = """\
You are a Windows keyboard-only automation agent.

You will be given:
- A screenshot of the user's screen.
- The active window title and process (may be empty).
- A user goal.
- A COMPLETE HISTORY of every action you have already taken in this session and its outcome.

Your job is to propose the NEXT keyboard actions to move toward the goal.

CRITICAL RULES:
1. NEVER repeat actions that have already succeeded. Check the session history carefully.
2. If you already completed a sub-task (e.g. created a file, typed text, opened an app), do NOT redo it.
3. Use the session history to understand your current progress and decide only the NEXT step.
4. If the screenshot shows you are partway through, continue from where you left off — do NOT restart.
5. Keep each step small: at most 6 actions.
6. Prefer hotkeys (e.g. Alt+F, Ctrl+L) over long navigation when obvious.
7. Only return a stop action if the screenshot clearly shows the goal is achieved (be conservative).
8. If you think the goal might be achieved but you cannot verify it from the screenshot, do NOT stop.
9. Do NOT ask for permissions or attempt to bypass security prompts. Only act within normal desktop apps.

Output MUST be valid JSON only (no Markdown, no code fences, no extra text).

Schema:
{
  "reasoning": "Brief explanation of what you see on screen, what has been done so far, and what you plan to do next",
  "expected_outcome": "Visual description of what should happen after these actions (e.g. 'File menu appears', 'Text is inserted')",
  "actions": [
    {"type": "type_text", "text": "string"},
    {"type": "key_press", "key": "enter|tab|esc|backspace|delete|up|down|left|right|home|end|pageup|pagedown|space|f1..f24|a..z|0..9"},
    {"type": "hotkey", "keys": ["ctrl|alt|shift|win", "a..z|0..9|f1..f24|enter|tab|esc|space"]},
    {"type": "wait_ms", "ms": 0},
    {"type": "stop", "reason": "string"}
  ]
}
"""


@dataclass(frozen=True)
class PromptContext:
    goal: str
    window_title: str
    process_path: str | None
    step: int
    recent_actions: list[dict]
    observations: list[dict] = field(default_factory=list)
    # NEW: rich session history
    session_history: list[dict] = field(default_factory=list)
    completed_actions_summary: str = ""
    milestones: list[str] = field(default_factory=list)


def build_user_prompt(ctx: PromptContext) -> str:
    """
    Build the user prompt with complete session context.

    This is the key fix: we now include a rich session history so the LLM
    knows exactly what has been done, what worked, and what failed.
    """
    payload: dict = {
        "goal": ctx.goal,
        "active_window_title": ctx.window_title,
        "active_process_path": ctx.process_path,
        "current_step": ctx.step,
    }

    # Include milestones (high-level progress) if available
    if ctx.milestones:
        payload["milestones_completed"] = ctx.milestones

    # Include full session history (what was done and what happened)
    if ctx.session_history:
        payload["session_history"] = ctx.session_history
    else:
        # Fallback: old-style recent actions
        payload["recent_actions_executed"] = ctx.recent_actions[-10:]

    # Include observations (failures, verifications, stage info)
    if ctx.observations:
        payload["observations"] = ctx.observations[-10:]

    parts = [
        "Decide the NEXT keyboard actions only.",
        "IMPORTANT: Review the session history below. Do NOT repeat actions that already succeeded.",
        "Return JSON that matches the schema exactly.",
        "",
        f"Context:\n{json.dumps(payload, ensure_ascii=True)}",
    ]

    # Include human-readable progress summary
    if ctx.completed_actions_summary:
        parts.append("")
        parts.append(f"=== PROGRESS SO FAR ===\n{ctx.completed_actions_summary}")

    return "\n".join(parts)
