from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class ActionExecutionRecord:
    step: int
    action: dict
    success: bool
    duration_ms: int
    error: str | None
    timestamp_utc: str


@dataclass
class StepMemory:
    step: int
    observed: str
    planned_actions: list[dict]
    executed_actions: list[ActionExecutionRecord]
    success: bool
    timestamp_utc: str
    screenshot_png: bytes | None = None


@dataclass
class ProgressChecklist:
    tasks: list[str] = field(default_factory=list)
    completed: set[str] = field(default_factory=set)

    def render(self) -> str:
        if not self.tasks:
            return "(no checklist inferred yet)"
        lines: list[str] = []
        for item in self.tasks:
            mark = "☑" if item in self.completed else "☐"
            lines.append(f"{mark} {item}")
        return "\n".join(lines)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _action_signature(action: dict) -> str:
    action_type = str(action.get("type", "")).lower()
    if action_type == "type_text":
        text = str(action.get("text", "")).strip().lower()
        return f"type_text:{text}"
    if action_type == "key_press":
        key = str(action.get("key", "")).strip().lower()
        return f"key_press:{key}"
    if action_type == "hotkey":
        keys = action.get("keys", [])
        if not isinstance(keys, list):
            keys = []
        normalized = "+".join(sorted(str(k).strip().lower() for k in keys))
        return f"hotkey:{normalized}"
    if action_type == "wait_ms":
        return "wait_ms"
    if action_type == "stop":
        return "stop"
    return json.dumps(action, sort_keys=True)


def _contains_any(text: str, needles: list[str]) -> bool:
    lowered = text.lower()
    return any(n in lowered for n in needles)


class ConversationHistory:
    def __init__(self, goal: str, *, keep_recent_steps: int = 10) -> None:
        self.goal = goal
        self.keep_recent_steps = max(2, keep_recent_steps)
        self._task_message = self._build_initial_task_message(goal)
        self._steps: list[StepMemory] = []
        self._progress = ProgressChecklist(tasks=self._infer_subtasks(goal))
        # Persistent log of ALL actions ever executed (never trimmed by keep_recent_steps)
        self._action_log: list[str] = []

    @property
    def steps(self) -> list[StepMemory]:
        return self._steps

    @property
    def progress(self) -> ProgressChecklist:
        return self._progress

    @staticmethod
    def _build_initial_task_message(goal: str) -> str:
        return (
            "Initial Task Description:\n"
            f"- User goal: {goal}\n"
            "- Never repeat actions already completed unless the UI clearly indicates retry is required.\n"
            "- Use prior step memory to choose the next logical action.\n"
            "- When the goal is complete, return a single stop action with reason."
        )

    @staticmethod
    def _infer_subtasks(goal: str) -> list[str]:
        goal_l = goal.lower()
        tasks: list[str] = []
        if _contains_any(goal_l, ["excel", "spreadsheet", "workbook"]):
            tasks.extend([
                "Open Excel",
                "Create document content",
                "Save document",
                "Close Excel",
            ])
        if _contains_any(goal_l, ["gmail", "email", "mail"]):
            tasks.extend([
                "Open browser",
                "Navigate to Gmail",
                "Compose email",
                "Attach document",
                "Send email",
            ])
        if not tasks:
            tasks = [
                "Open required application",
                "Complete core task steps",
                "Finalize and confirm completion",
            ]
        # de-dupe preserving order
        result: list[str] = []
        seen: set[str] = set()
        for item in tasks:
            if item not in seen:
                result.append(item)
                seen.add(item)
        return result

    def find_recent_duplicate(self, action: dict, *, last_n_steps: int = 3) -> tuple[int, str] | None:
        signature = _action_signature(action)
        if signature in {"wait_ms", "stop"}:
            return None

        recent_steps = self._steps[-max(1, last_n_steps) :]
        for step in reversed(recent_steps):
            for rec in reversed(step.executed_actions):
                if _action_signature(rec.action) == signature and rec.success:
                    return (step.step, signature)
        return None

    def check_duplicate_action(self, action: dict, *, last_n_steps: int = 3) -> str | None:
        found = self.find_recent_duplicate(action, last_n_steps=last_n_steps)
        if not found:
            return None
        duplicate_step, signature = found
        return f"Potential repeat detected: action '{signature}' already succeeded in Step {duplicate_step}."

    def build_messages_for_decision(
        self,
        *,
        step: int,
        screenshot_png: bytes,
        active_window_title: str,
        active_process_path: str | None,
        user_text: str | None = None,
    ) -> list[dict]:
        messages: list[dict] = [
            {
                "role": "user",
                "content": [{"type": "text", "text": self._task_message}],
            }
        ]

        summary_text = self._build_old_steps_summary()
        if summary_text:
            messages.append(
                {
                    "role": "user",
                    "content": [{"type": "text", "text": summary_text}],
                }
            )

        recent = self._steps[-self.keep_recent_steps :]
        for memory in recent:
            content: list[dict] = [
                {
                    "type": "text",
                    "text": self._render_step_user_memory(memory),
                }
            ]
            if memory.screenshot_png:
                content.append(self._image_block(memory.screenshot_png))
            messages.append({"role": "user", "content": content})
            messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"actions": memory.planned_actions}, ensure_ascii=True),
                        }
                    ],
                }
            )

        current_context_text = self._build_current_context_message(
            step=step,
            active_window_title=active_window_title,
            active_process_path=active_process_path,
        )
        if user_text:
            current_context_text = current_context_text + "\n\nUser prompt/context:\n" + user_text
        current_content = [
            {"type": "text", "text": current_context_text},
            self._image_block(screenshot_png),
        ]
        messages.append({"role": "user", "content": current_content})
        return messages

    def append_step(
        self,
        *,
        step: int,
        observed: str,
        planned_actions: list[dict],
        executed_actions: list[ActionExecutionRecord],
        success: bool,
        screenshot_png: bytes,
    ) -> None:
        memory = StepMemory(
            step=step,
            observed=observed,
            planned_actions=planned_actions,
            executed_actions=executed_actions,
            success=success,
            timestamp_utc=_utc_now_iso(),
            screenshot_png=screenshot_png,
        )
        self._steps.append(memory)
        self._update_progress(memory)

        # Append to persistent action log (never trimmed)
        for rec in executed_actions:
            self._action_log.append(self._summarize_action_record(rec, step))

    @staticmethod
    def _summarize_action_record(rec: ActionExecutionRecord, step: int) -> str:
        """One-line human-readable summary of an executed action."""
        a = rec.action
        t = str(a.get("type", ""))
        ok = "✓" if rec.success else "✗"
        if t == "type_text":
            detail = f'typed "{str(a.get("text", ""))[:50]}"'
        elif t == "key_press":
            detail = f'pressed {a.get("key")}'
        elif t == "hotkey":
            detail = f'hotkey {a.get("keys")}'
        elif t == "mouse_click":
            detail = f'clicked ({a.get("x")},{a.get("y")}) {a.get("button", "left")}'
        elif t == "mouse_scroll":
            detail = f'scrolled {a.get("direction")} at ({a.get("x")},{a.get("y")})'
        elif t == "stop":
            detail = f'stop: {str(a.get("reason", ""))[:50]}'
        elif t == "wait_ms":
            detail = f'waited {a.get("ms")}ms'
        elif t == "ask_user":
            detail = f'asked: {str(a.get("question", ""))[:40]}'
        else:
            detail = t
        return f"Step {step}: {ok} {detail}"

    def _update_progress(self, memory: StepMemory) -> None:
        joined = " ".join(
            [memory.observed]
            + [json.dumps(a.action, ensure_ascii=False) for a in memory.executed_actions]
        ).lower()
        mapping = {
            "Open Excel": ["excel", "start excel", "excel.exe"],
            "Create document content": ["type_text", "typed", "cell", "workbook", "entered data"],
            "Save document": ["ctrl+s", "save", "saved", "ctrl\"+\"s"],
            "Close Excel": ["alt+f4", "close excel", "closed excel"],
            "Open browser": ["chrome", "firefox", "edge", "browser", "ctrl+l", "chrome.exe"],
            "Navigate to Gmail": ["gmail", "mail.google.com", "inbox"],
            "Compose email": ["compose", "new message", "compose button"],
            "Attach document": ["attach", "attachment", "paperclip", "attach file"],
            "Send email": ["send", "sent", "message sent"],
            "Open required application": ["start", "open", "launched", "running"],
            "Complete core task steps": ["type", "fill", "write", "entered", "typed"],
            "Finalize and confirm completion": ["save", "done", "completed", "stop", "verified"],
        }
        for task in self._progress.tasks:
            hints = mapping.get(task, [])
            if any(h in joined for h in hints):
                self._progress.completed.add(task)

    def update_checklist_from_vlm(self, progress_text: str) -> None:
        """Allow the VLM's progress field to mark checklist items complete."""
        if not progress_text:
            return
        pl = progress_text.lower()
        for task in self._progress.tasks:
            if task.lower() in pl or any(
                word in pl for word in task.lower().split() if len(word) > 3
            ):
                self._progress.completed.add(task)

    def _render_step_user_memory(self, memory: StepMemory) -> str:
        executed = [
            {
                "action": rec.action,
                "success": rec.success,
                "duration_ms": rec.duration_ms,
                "error": rec.error,
                "timestamp_utc": rec.timestamp_utc,
            }
            for rec in memory.executed_actions
        ]
        payload = {
            "step": memory.step,
            "timestamp_utc": memory.timestamp_utc,
            "observed": memory.observed,
            "planned_actions": memory.planned_actions,
            "executed_actions": executed,
            "step_success": memory.success,
        }
        return "Step memory:\n" + json.dumps(payload, ensure_ascii=True)

    def _build_old_steps_summary(self) -> str:
        if len(self._steps) <= self.keep_recent_steps:
            return ""
        old = self._steps[: -self.keep_recent_steps]
        lines = [
            f"Summary of Steps 1-{old[-1].step} (screenshots omitted to save tokens):",
        ]
        for step in old:
            action_details: list[str] = []
            for rec in step.executed_actions:
                action_details.append(self._summarize_action_record(rec, step.step))
            detail_str = "; ".join(action_details) if action_details else "no actions"
            status = "success" if step.success else "partial/failure"
            lines.append(
                f"- Step {step.step} [{status}]: {detail_str}"
            )
        lines.append("\nChecklist so far:\n" + self._progress.render())
        return "\n".join(lines)

    def _build_current_context_message(
        self,
        *,
        step: int,
        active_window_title: str,
        active_process_path: str | None,
    ) -> str:
        context = {
            "step": step,
            "active_window_title": active_window_title,
            "active_process_path": active_process_path,
            "checklist": self._progress.render(),
            "completed_actions_history": self._action_log[-40:],
            "instruction": (
                "CRITICAL: Review the completed_actions_history list above. "
                "Every action listed there has ALREADY been executed. "
                "DO NOT repeat any action that already succeeded (marked ✓). "
                "Only return the NEXT small set of actions (max 6) that move "
                "toward the goal. Return stop when the goal is visually confirmed complete."
            ),
            "completion_rule": (
                "Only return stop if the CURRENT screenshot provides clear evidence the goal is complete. "
                "If the goal mentions verification (exists/open/show), perform verification steps first."
            ),
        }
        return "Current step input:\n" + json.dumps(context, ensure_ascii=True)

    @staticmethod
    def _image_block(image_png: bytes) -> dict:
        b64 = base64.b64encode(image_png).decode("ascii")
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": b64,
            },
        }
