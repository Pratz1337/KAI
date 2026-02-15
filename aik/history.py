from __future__ import annotations

import base64
import json
import os
import uuid
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


def _new_session_id() -> str:
    # Timestamp prefix keeps sessions lexicographically sortable and human-readable.
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{uuid.uuid4().hex[:8]}"


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
    def __init__(self, goal: str, *, keep_recent_steps: int = 6, history_path: str | None = None) -> None:
        self.goal = goal
        self.keep_recent_steps = max(2, keep_recent_steps)
        self.history_path = history_path

        # Each run gets its own session record in the history file so history isn't overwritten.
        self.session_id = _new_session_id()
        self.started_at_utc = _utc_now_iso()
        self._previous_sessions: list[dict] = self._load_previous_sessions()

        self._task_message = self._build_initial_task_message(goal)
        self._steps: list[StepMemory] = []
        self._progress = ProgressChecklist(tasks=self._infer_subtasks(goal))

        # Create the file early so the session exists even if we crash before step 1.
        self.save()

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
        self.save()

    def save(self) -> None:
        if not self.history_path:
            return
        
        try:
            # We don't save the full screenshots to the JSON history (too large), 
            # just the text metadata.
            session = {
                "session_id": self.session_id,
                "goal": self.goal,
                "started_at_utc": self.started_at_utc,
                "updated_at_utc": _utc_now_iso(),
                "steps": [
                    {
                        "step": s.step,
                        "observed": s.observed,
                        "planned_actions": s.planned_actions,
                        "executed_actions": [
                            {
                                "action": r.action,
                                "success": r.success,
                                "error": r.error,
                                "duration_ms": r.duration_ms,
                                "timestamp": r.timestamp_utc,
                            }
                            for r in s.executed_actions
                        ],
                        "success": s.success,
                        "timestamp": s.timestamp_utc,
                    }
                    for s in self._steps
                ],
                "progress": {
                    "tasks": self._progress.tasks,
                    "completed": list(self._progress.completed),
                }
            }

            data = {
                "version": 2,
                "sessions": [*self._previous_sessions, session],
            }
            
            os.makedirs(os.path.dirname(self.history_path) or ".", exist_ok=True)
            tmp = self.history_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.history_path)
        except Exception:
            # Soft failure for history saving
            pass

    def _load_previous_sessions(self) -> list[dict]:
        """Load prior sessions from history_path (supports legacy single-session format)."""
        path = self.history_path
        if not path or not os.path.exists(path):
            return []

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            # Don't destroy a possibly useful history file; move it aside and start fresh.
            try:
                ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                os.replace(path, f"{path}.corrupt.{ts}")
            except Exception:
                pass
            return []

        # New multi-session format.
        if isinstance(data, dict) and isinstance(data.get("sessions"), list):
            sessions = [s for s in data.get("sessions", []) if isinstance(s, dict)]
            return sessions

        # Legacy single-session format (older AIK versions).
        if isinstance(data, dict) and ("goal" in data) and isinstance(data.get("steps"), list):
            legacy_ts = str(data.get("timestamp_utc") or _utc_now_iso())
            legacy = {
                "session_id": f"legacy-{legacy_ts}",
                "goal": str(data.get("goal") or ""),
                "started_at_utc": legacy_ts,
                "updated_at_utc": legacy_ts,
                "steps": data.get("steps", []),
                "progress": data.get("progress", {}),
                "legacy_format": True,
            }
            return [legacy]

        return []

    def _update_progress(self, memory: StepMemory) -> None:
        joined = " ".join(
            [memory.observed]
            + [json.dumps(a.action, ensure_ascii=False) for a in memory.executed_actions]
        ).lower()
        mapping = {
            "Open Excel": ["excel", "start excel"],
            "Create document content": ["type_text", "typed", "cell", "workbook"],
            "Save document": ["ctrl+s", "save", "saved"],
            "Close Excel": ["alt+f4", "close excel", "closed excel"],
            "Open browser": ["chrome", "firefox", "edge", "browser", "ctrl+l"],
            "Navigate to Gmail": ["gmail", "mail.google.com"],
            "Compose email": ["compose", "new message"],
            "Attach document": ["attach", "attachment", "paperclip"],
            "Send email": ["send", "sent"],
            "Open required application": ["start", "open", "launched"],
            "Complete core task steps": ["type", "fill", "write", "entered"],
            "Finalize and confirm completion": ["save", "done", "completed", "stop"],
        }
        for task in self._progress.tasks:
            hints = mapping.get(task, [])
            if any(h in joined for h in hints):
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
            action_names = [str(a.get("type", "")) for a in step.planned_actions]
            action_preview = ", ".join(action_names[:4]) if action_names else "none"
            status = "success" if step.success else "partial/failure"
            lines.append(
                f"- Step {step.step}: observed '{step.observed[:120]}', actions [{action_preview}], status={status}."
            )
        lines.append("Checklist so far:\n" + self._progress.render())
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
            "instruction": (
                "Review all prior memory and checklist. Do not repeat already-completed actions. "
                "Return only the NEXT small set of actions (max 6). Return stop when complete."
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
