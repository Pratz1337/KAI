"""
Session memory for the AIK agent.

Maintains a structured history of everything the agent has done in the current
session, so the LLM never loses context about past actions and their outcomes.

The key insight: the old code sent only the last 10 raw action dicts, with no
model reasoning, no outcomes, no timestamps. The LLM had no way to know what it
had already accomplished. This module fixes that by building a rich, structured
conversation history that is included in every API call.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("aik.session_memory")


@dataclass
class StepRecord:
    """One complete agent planning step: what the model saw, thought, and did."""

    step_number: int
    timestamp: float = field(default_factory=time.time)

    # What the model saw
    window_title: str = ""
    process_path: str | None = None

    # What the model said (its full JSON plan + any reasoning)
    model_response_text: str = ""
    # Parsed fields from the model plan (useful for downstream verification)
    reasoning: str = ""
    expected_outcome: str = ""

    # Parsed actions that were actually executed
    actions_executed: list[dict[str, Any]] = field(default_factory=list)

    # Outcome summary (success, failure signals, verification results)
    outcome: str = ""            # "success", "failure", "partial", "stop_requested"
    outcome_details: str = ""    # Human-readable details
    failure_signals: dict[str, Any] | None = None
    verification_result: dict[str, Any] | None = None

    def to_summary(self) -> dict[str, Any]:
        """Compact summary for inclusion in the prompt."""
        summary: dict[str, Any] = {
            "step": self.step_number,
            "window": self.window_title,
        }
        if self.expected_outcome:
            # Keep small to avoid prompt bloat.
            eo = self.expected_outcome.strip()
            if len(eo) > 220:
                eo = eo[:217] + "..."
            summary["expected_outcome"] = eo
        if self.actions_executed:
            summary["actions"] = [
                _compact_action(a) for a in self.actions_executed
            ]
        if self.outcome:
            summary["outcome"] = self.outcome
        if self.outcome_details:
            summary["details"] = self.outcome_details
        if self.failure_signals:
            summary["failure"] = self.failure_signals
        if self.verification_result:
            summary["verification"] = self.verification_result
        return summary


@dataclass
class ConversationTurn:
    """One assistant turn in the multi-turn conversation."""

    role: str  # "user" or "assistant"
    text: str
    step_number: int


class SessionMemory:
    """
    Accumulates the full history of agent actions, model responses, and
    outcomes for the current session.

    Provides two views of history:
    1. `get_step_summaries()` — compact summaries for prompt injection
    2. `get_conversation_history()` — full multi-turn messages for the API
    """

    def __init__(self, *, max_conversation_turns: int = 60, max_summary_steps: int = 30) -> None:
        self._steps: list[StepRecord] = []
        self._conversation: list[ConversationTurn] = []
        self._max_conversation_turns = max_conversation_turns
        self._max_summary_steps = max_summary_steps

    @property
    def step_count(self) -> int:
        return len(self._steps)

    @property
    def steps(self) -> list[StepRecord]:
        return list(self._steps)

    def begin_step(
        self,
        step_number: int,
        window_title: str,
        process_path: str | None,
    ) -> StepRecord:
        """Start recording a new planning step."""
        record = StepRecord(
            step_number=step_number,
            window_title=window_title,
            process_path=process_path,
        )
        self._steps.append(record)
        return record

    def record_model_response(self, record: StepRecord, response_text: str) -> None:
        """Save the model's raw response text for this step."""
        record.model_response_text = response_text

    def record_plan_fields(self, record: StepRecord, *, reasoning: str, expected_outcome: str) -> None:
        """Save parsed fields from the model plan for this step."""
        record.reasoning = reasoning or ""
        record.expected_outcome = expected_outcome or ""

    def record_actions(self, record: StepRecord, actions: list[dict[str, Any]]) -> None:
        """Save the actions that were parsed and executed."""
        record.actions_executed = list(actions)

    def record_outcome(
        self,
        record: StepRecord,
        outcome: str,
        details: str = "",
        failure_signals: dict[str, Any] | None = None,
        verification_result: dict[str, Any] | None = None,
    ) -> None:
        """Record what happened after execution."""
        record.outcome = outcome
        record.outcome_details = details
        record.failure_signals = failure_signals
        record.verification_result = verification_result

    def add_conversation_turn(self, role: str, text: str, step_number: int) -> None:
        """Add a turn to the conversation history for multi-turn API calls."""
        self._conversation.append(ConversationTurn(role=role, text=text, step_number=step_number))
        # Trim old turns (keep the most recent ones)
        if len(self._conversation) > self._max_conversation_turns:
            self._conversation = self._conversation[-self._max_conversation_turns:]

    def get_step_summaries(self) -> list[dict[str, Any]]:
        """
        Get compact summaries of recent steps for prompt injection.
        Includes enough detail for the LLM to understand what was done and what worked.
        """
        # Exclude the current in-progress step record (created before we have a model response).
        complete = [s for s in self._steps if not _is_incomplete_step(s)]
        steps = complete[-self._max_summary_steps:]
        return [s.to_summary() for s in steps]

    def get_conversation_messages(self) -> list[dict[str, Any]]:
        """
        Build the multi-turn messages array for the Anthropic API.
        Returns list of {"role": "user"|"assistant", "content": "..."} dicts.
        """
        messages: list[dict[str, Any]] = []
        for turn in self._conversation[-self._max_conversation_turns:]:
            messages.append({"role": turn.role, "content": turn.text})
        return messages

    def get_completed_actions_summary(self) -> str:
        """
        Build a human-readable summary of everything accomplished so far.
        This is the key anti-amnesia mechanism: it tells the LLM exactly what
        has already been done so it doesn't repeat work.
        """
        if not self._steps:
            return "No actions taken yet."

        lines: list[str] = []
        for record in self._steps:
            if _is_incomplete_step(record):
                continue
            action_strs = [_compact_action_str(a) for a in record.actions_executed]
            actions_text = ", ".join(action_strs) if action_strs else "no actions"
            outcome_text = record.outcome or "unknown"
            if record.outcome_details:
                outcome_text += f" ({record.outcome_details})"
            lines.append(
                f"Step {record.step_number} [{record.window_title}]: "
                f"{actions_text} → {outcome_text}"
            )

        return "\n".join(lines)

    def get_milestone_summary(self) -> list[str]:
        """
        Extract key milestones (successful steps, stage completions) for
        a high-level progress report.
        """
        milestones: list[str] = []
        for record in self._steps:
            if _is_incomplete_step(record):
                continue
            if record.outcome in ("success", "stage_complete"):
                action_strs = [_compact_action_str(a) for a in record.actions_executed]
                milestones.append(
                    f"Step {record.step_number}: {', '.join(action_strs)}"
                )
        return milestones


def _is_incomplete_step(record: StepRecord) -> bool:
    """
    A StepRecord is considered "incomplete" if we haven't yet stored a model
    response, actions, or an outcome. This happens because we create the record
    at the start of the loop before calling the model.
    """
    return (
        not record.model_response_text
        and not record.actions_executed
        and not record.outcome
        and not record.outcome_details
    )


def _compact_action(action: dict[str, Any]) -> str:
    """Compact string representation of an action for prompt injection."""
    t = action.get("type", "?")
    if t == "type_text":
        text = action.get("text", "")
        if len(text) > 40:
            text = text[:37] + "..."
        return f"type:{text!r}"
    if t == "key_press":
        return f"key:{action.get('key', '?')}"
    if t == "hotkey":
        return f"hotkey:{'+'.join(action.get('keys', []))}"
    if t == "wait_ms":
        return f"wait:{action.get('ms', 0)}ms"
    if t == "stop":
        return f"stop:{action.get('reason', '')}"
    return f"{t}:{action}"


def _compact_action_str(action: dict[str, Any]) -> str:
    """Even more compact string for milestone summaries."""
    t = action.get("type", "?")
    if t == "type_text":
        text = action.get("text", "")
        if len(text) > 30:
            text = text[:27] + "..."
        return f'typed "{text}"'
    if t == "key_press":
        return action.get("key", "?")
    if t == "hotkey":
        return "+".join(action.get("keys", []))
    if t == "wait_ms":
        return f"waited {action.get('ms', 0)}ms"
    if t == "stop":
        return f"stopped: {action.get('reason', '')}"
    return str(action)
