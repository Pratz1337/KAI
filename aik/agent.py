from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .actions import ActionParseError, ParsedPlan, parse_plan
from .anthropic_client import AnthropicClient
from .capture import ScreenCapturer, Screenshot
from .failure_detector import detect_failure
from .input_injector import InputInjector
from .kill_switch import KillSwitch
from .prompt import PromptContext, SYSTEM_PROMPT, build_user_prompt
from .recovery_strategies import suggest_recovery
from .session_memory import SessionMemory, StepRecord
from .window_context import ForegroundWindow, get_foreground_window

log = logging.getLogger("aik.agent")


@dataclass(frozen=True)
class AgentConfig:
    goal: str
    max_steps: int = 40
    loop_interval_s: float = 0.8
    max_tokens: int = 700
    temperature: float = 0.2
    monitor_index: int = 1
    screenshot_max_width: int | None = 1280
    dry_run: bool = False
    inter_key_delay_s: float = 0.01
    kernel_mode: bool = False
    driver_path: str = r"\\.\AikKmdfIoctl"

    # Goal verification (prevents false-positive "stop")
    verify_goal_on_stop: bool = True
    verify_confidence_threshold: float = 0.8
    max_stop_verification_failures: int = 3
    verification_delay_s: float = 0.4

    # Stage-based decomposition for complex goals
    use_goal_decomposition: bool = True

    # Lightweight failure detection (screen unchanged / unexpected window switch)
    detect_failures: bool = True

    # Detailed per-step logging
    verbose_logging: bool = True

    # Multi-turn conversation history size (only used if use_multiturn=True)
    max_conversation_turns: int = 60
    
    # Whether to feed full multi-turn history into the model. Recommended: False.
    # You already inject structured `session_history` every step; multi-turn tends to
    # duplicate context and can cause prompt bloat/amnesia.
    use_multiturn: bool = False

    # Micro-verification of each step (vision-based verification vs expected_outcome)
    verify_steps: bool = True
    micro_verify_confidence_threshold: float = 0.65


@dataclass
class AgentState:
    recent_actions: list[dict] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)
    stop_verification_failures: int = 0
    consecutive_parse_failures: int = 0
    completed_stages: list[str] = field(default_factory=list)
    current_stage_index: int = 0
    total_actions_executed: int = 0
    total_failures_detected: int = 0


class KeyboardVisionAgent:
    def __init__(
        self,
        cfg: AgentConfig,
        *,
        anthropic: AnthropicClient,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        self._cfg = cfg
        self._anthropic = anthropic
        self._kill = kill_switch or KillSwitch()
        self._capturer = ScreenCapturer(
            monitor_index=cfg.monitor_index, max_width=cfg.screenshot_max_width
        )

        self._goal_verifier = None
        if cfg.verify_goal_on_stop and not cfg.dry_run:
            from .goal_verifier import GoalVerifier

            self._goal_verifier = GoalVerifier(anthropic)

        self._goal_decomposer = None
        self._stages = []
        if cfg.use_goal_decomposition and not cfg.dry_run:
            try:
                from .goal_decomposer import GoalDecomposer
                self._goal_decomposer = GoalDecomposer(anthropic)
            except Exception as e:
                log.debug("Goal decomposer not available: %s", e)

        self._micro_verifier = None
        if cfg.verify_steps and not cfg.dry_run:
            from .micro_verifier import MicroVerifier
            self._micro_verifier = MicroVerifier(anthropic)
            log.info("Micro-verification enabled")

        if cfg.kernel_mode:
            from .input_injector_kernel import KernelInputInjector
            log.info("Using KERNEL-MODE injector (driver: %s)", cfg.driver_path)
            self._injector = KernelInputInjector(
                inter_key_delay_s=cfg.inter_key_delay_s,
                device_path=cfg.driver_path,
                fallback=True,
            )
        else:
            self._injector = InputInjector(inter_key_delay_s=cfg.inter_key_delay_s)

        self._state = AgentState()

        # ── NEW: Session memory for multi-turn conversation history ──
        self._memory = SessionMemory(
            max_conversation_turns=cfg.max_conversation_turns,
        )

    def run(self) -> None:
        self._kill.start()

        log.info("Starting agent. dry_run=%s max_steps=%d", self._cfg.dry_run, self._cfg.max_steps)
        log.info("Kill switch: Ctrl+Alt+Backspace")

        # Decompose goal into stages if enabled
        if self._goal_decomposer and self._cfg.use_goal_decomposition:
            self._decompose_goal()

        for step in range(1, self._cfg.max_steps + 1):
            if self._kill.triggered:
                log.warning("Kill switch triggered. Stopping.")
                self._log_session_summary(step, "kill_switch")
                return

            shot = self._capturer.capture()
            fg = get_foreground_window()

            # ── Begin recording this step ──
            step_record = self._memory.begin_step(
                step_number=step,
                window_title=fg.title,
                process_path=fg.process_path,
            )

            # Build context with stage awareness
            extra_observations = list(self._state.observations)
            if self._stages and self._state.current_stage_index < len(self._stages):
                current = self._stages[self._state.current_stage_index]
                extra_observations.append({
                    "type": "current_stage",
                    "stage_name": current.name,
                    "stage_description": current.description,
                    "stage_verify": current.verify,
                    "stages_completed": [s for s in self._state.completed_stages],
                    "stages_remaining": len(self._stages) - self._state.current_stage_index,
                })

            ctx = PromptContext(
                goal=self._cfg.goal,
                window_title=fg.title,
                process_path=fg.process_path,
                step=step,
                recent_actions=self._state.recent_actions,
                observations=extra_observations,
                session_history=self._memory.get_step_summaries(),
                completed_actions_summary=self._memory.get_completed_actions_summary(),
                milestones=self._memory.get_milestone_summary(),
            )
            user_prompt = build_user_prompt(ctx)

            # ── Build multi-turn messages ──
            # Get prior conversation turns and append the new user turn
            conversation_messages = self._memory.get_conversation_messages()
            conversation_messages.append({"role": "user", "content": user_prompt})

            try:
                resp = self._anthropic.create_message_multiturn(
                    system=SYSTEM_PROMPT,
                    messages=conversation_messages,
                    image_png=shot.png,
                    max_tokens=self._cfg.max_tokens,
                    temperature=self._cfg.temperature,
                )

                # ── Save the conversation turns into memory ──
                self._memory.add_conversation_turn("user", user_prompt, step)
                self._memory.add_conversation_turn("assistant", resp.text, step)
                self._memory.record_model_response(step_record, resp.text)

                plan = parse_plan(resp.text)
                self._state.consecutive_parse_failures = 0
            except ActionParseError as e:
                log.error("Model returned invalid JSON plan: %s", e)
                raw = (getattr(resp, "text", "") or "").strip()
                snippet = raw.replace("\r", "")[:800]
                if snippet:
                    log.error("Model response (truncated): %s", snippet)
                self._state.observations.append(
                    {
                        "type": "model_invalid_json",
                        "error": str(e),
                        "response_snippet": snippet,
                    }
                )
                # Still save conversation turns so context isn't lost
                self._memory.add_conversation_turn("user", user_prompt, step)
                self._memory.add_conversation_turn("assistant", raw, step)
                self._memory.record_outcome(step_record, "parse_error", str(e))

                self._state.consecutive_parse_failures += 1
                if self._state.consecutive_parse_failures >= 3:
                    log.error("Too many consecutive parse failures. Stopping.")
                    self._log_session_summary(step, "parse_failures")
                    return
                # Retry next loop iteration (new screenshot/context).
                if self._cfg.loop_interval_s:
                    time.sleep(self._cfg.loop_interval_s)
                continue
            except Exception as e:
                log.exception("Failed to get plan: %s", e)
                self._memory.record_outcome(step_record, "api_error", str(e))
                self._log_session_summary(step, "api_error")
                return

            # Record what actions the model planned
            self._memory.record_actions(step_record, plan.actions[:6])

            if self._execute_plan(plan, before_shot=shot, before_fg=fg, step=step, step_record=step_record):
                self._log_session_summary(step, "completed")
                return

            if self._cfg.loop_interval_s:
                time.sleep(self._cfg.loop_interval_s)

        log.warning("Max steps reached (%d). Stopping.", self._cfg.max_steps)
        self._log_session_summary(self._cfg.max_steps, "max_steps")

    def _decompose_goal(self) -> None:
        """Break the goal into verifiable stages."""
        if not self._goal_decomposer:
            return
        try:
            result = self._goal_decomposer.decompose(self._cfg.goal)
            if result.stages:
                self._stages = result.stages
                log.info("Goal decomposed into %d stages:", len(self._stages))
                for i, s in enumerate(self._stages):
                    log.info("  Stage %d: %s - %s", i + 1, s.name, s.description)
            else:
                log.debug("Goal decomposition returned no stages.")
        except Exception as e:
            log.debug("Goal decomposition failed: %s", e)

    def _execute_plan(
        self,
        plan: ParsedPlan,
        *,
        before_shot: Screenshot,
        before_fg: ForegroundWindow,
        step: int,
        step_record: StepRecord,
    ) -> bool:
        actions = plan.actions[:6]
        log.info("Step %d | Plan: %s", step, actions)

        stop_reason: str | None = None
        performed_input = False

        for a in actions:
            if self._kill.triggered:
                log.warning("Kill switch triggered mid-plan. Stopping.")
                self._memory.record_outcome(step_record, "kill_switch")
                return True

            self._state.recent_actions.append(a)

            t = a["type"]
            if t == "stop":
                stop_reason = str(a.get("reason", "") or "")
                break

            if self._cfg.dry_run:
                continue

            action_desc = self._describe_action(a)
            if self._cfg.verbose_logging:
                log.info("  Executing: %s", action_desc)

            if t == "type_text":
                performed_input = True
                self._injector.type_text(a["text"])
            elif t == "key_press":
                performed_input = True
                self._injector.key_press(a["key"])
            elif t == "hotkey":
                performed_input = True
                self._injector.hotkey(a["keys"])
            elif t == "wait_ms":
                time.sleep(a["ms"] / 1000.0)

            self._state.total_actions_executed += 1

        # Post-action checks
        if self._cfg.detect_failures and performed_input:
            after_shot = self._capturer.capture()
            after_fg = get_foreground_window()
            signals = detect_failure(
                before_shot=before_shot,
                after_shot=after_shot,
                before_fg=before_fg,
                after_fg=after_fg,
            )

            micro_res = None
            if self._micro_verifier and plan.expected_outcome:
                if self._cfg.verbose_logging:
                    log.info("  Micro-verifying outcome: %r", plan.expected_outcome)
                
                action_summary = ", ".join(self._describe_action(a) for a in actions)
                micro_res = self._micro_verifier.verify_step(
                    action_desc=action_summary,
                    expected_outcome=plan.expected_outcome,
                    before_png=before_shot.png,
                    after_png=after_shot.png,
                )
                if not micro_res.success:
                    log.warning("  Micro-verification FAILED: %s", micro_res.observation)
                    self._state.observations.append({
                        "type": "micro_verification_failed",
                        "expected": plan.expected_outcome,
                        "observation": micro_res.observation,
                        "correction": micro_res.correction,
                    })
                elif self._cfg.verbose_logging:
                    log.info("  Micro-verification PASSED")

            if self._cfg.verbose_logging:
                log.info(
                    "  Post-action check: similarity=%.3f unchanged=%s title_changed=%s "
                    "process_changed=%s error_dialog=%s semantc_fail=%s",
                    signals.screen_similarity,
                    signals.screen_unchanged,
                    signals.window_title_changed,
                    signals.window_process_changed,
                    signals.error_dialog_suspected,
                    (micro_res and not micro_res.success),
                )

            has_failure = (
                signals.screen_unchanged
                or signals.window_process_changed
                or signals.window_title_changed
                or signals.error_dialog_suspected
                or (micro_res and not micro_res.success)
            )

            if has_failure:
                self._state.total_failures_detected += 1
                advice = suggest_recovery(signals)
                failure_info = {
                    "screen_similarity": signals.screen_similarity,
                    "unchanged": signals.screen_unchanged,
                    "title_changed": signals.window_title_changed,
                    "process_changed": signals.window_process_changed,
                    "error_dialog": signals.error_dialog_suspected,
                    "micro_verification": micro_res.observation if micro_res else None,
                }
                if advice is not None:
                    self._state.observations.append(
                        {
                            "type": "failure_signal",
                            "details": signals.details,
                            "note": advice.note,
                            "severity": advice.severity,
                            "suggested_actions": advice.suggested_actions,
                        }
                    )
                    log.warning(
                        "  Failure detected [%s]: %s",
                        advice.severity, signals.details,
                    )
                
                details = signals.details
                if micro_res and not micro_res.success:
                    details += f"; VerifiedFail: {micro_res.observation}"
                
                self._memory.record_outcome(
                    step_record, "failure", details,
                    failure_signals=failure_info,
                )
            else:
                if self._cfg.verbose_logging:
                    log.info("  Action appears successful")
                
                detail = "screen changed as expected"
                if micro_res and micro_res.success:
                    detail += "; verified outcome"
                
                self._memory.record_outcome(step_record, "success", detail)
        elif not performed_input:
            # No input was performed (e.g., only wait_ms or stop)
            self._memory.record_outcome(step_record, "no_input", "no keyboard input performed")
        else:
            self._memory.record_outcome(step_record, "success")

        if stop_reason is None:
            return False

        # ──── STOP VERIFICATION ────
        if self._cfg.verbose_logging:
            log.info("  Model requested STOP: %s", stop_reason)
            log.info("  Verifying goal state from screenshot...")

        if self._cfg.dry_run or not self._cfg.verify_goal_on_stop:
            log.info("Stop (unverified): %s", stop_reason)
            self._memory.record_outcome(step_record, "stopped", stop_reason)
            return True

        if self._goal_verifier is None:
            log.warning("Stop requested but goal verifier is not available; continuing.")
            return False

        if self._cfg.verification_delay_s:
            time.sleep(self._cfg.verification_delay_s)

        verify_shot = self._capturer.capture()
        verify_fg = get_foreground_window()
        res = self._goal_verifier.verify(
            goal=self._cfg.goal,
            screenshot_png=verify_shot.png,
            window_title=verify_fg.title,
            process_path=verify_fg.process_path,
            step=step,
            extra={"model_stop_reason": stop_reason},
        )
        verification_info = {
            "goal_achieved": res.verified,
            "confidence": res.confidence,
            "evidence": res.evidence,
            "missing": res.missing,
        }
        self._state.observations.append(
            {
                "type": "goal_verification",
                "goal_achieved": res.verified,
                "confidence": res.confidence,
                "evidence": res.evidence,
                "missing": res.missing,
                "reason": res.reason,
                "model_stop_reason": stop_reason,
            }
        )

        if self._cfg.verbose_logging:
            log.info(
                "  Verification result: achieved=%s confidence=%.2f",
                res.verified, res.confidence,
            )
            if res.evidence:
                log.info("  Evidence: %s", res.evidence)
            if res.missing:
                log.info("  Missing: %s", res.missing)

        if res.verified and res.confidence >= self._cfg.verify_confidence_threshold:
            log.info(
                "Goal VERIFIED (confidence=%.2f). Stop: %s",
                res.confidence, stop_reason,
            )
            self._memory.record_outcome(
                step_record, "verified_complete", stop_reason,
                verification_result=verification_info,
            )
            return True

        # Goal NOT verified
        self._state.stop_verification_failures += 1
        log.warning(
            "Model requested stop, but goal NOT verified (confidence=%.2f): %s",
            res.confidence,
            res.reason,
        )
        log.warning(
            "  Verification failure %d/%d. Continuing with additional attempts...",
            self._state.stop_verification_failures,
            self._cfg.max_stop_verification_failures,
        )

        self._memory.record_outcome(
            step_record, "stop_rejected",
            f"Goal not verified (confidence={res.confidence:.2f}): {res.reason}",
            verification_result=verification_info,
        )

        if self._state.stop_verification_failures >= self._cfg.max_stop_verification_failures:
            log.error(
                "Stopping after %d failed stop verifications to avoid an infinite loop.",
                self._state.stop_verification_failures,
            )
            return True

        return False

    def _describe_action(self, action: dict) -> str:
        """Return a human-readable description of an action."""
        t = action.get("type", "?")
        if t == "type_text":
            text = action.get("text", "")
            preview = text[:30] + "..." if len(text) > 30 else text
            return f"type_text: {preview!r}"
        if t == "key_press":
            return f"key_press: {action.get('key', '?')}"
        if t == "hotkey":
            return f"hotkey: {'+'.join(action.get('keys', []))}"
        if t == "wait_ms":
            return f"wait: {action.get('ms', 0)}ms"
        if t == "stop":
            return f"stop: {action.get('reason', '')}"
        return f"unknown: {action}"

    def _log_session_summary(self, final_step: int, exit_reason: str) -> None:
        """Log a summary of the agent session."""
        log.info("=" * 60)
        log.info("SESSION SUMMARY")
        log.info("=" * 60)
        log.info("  Goal: %s", self._cfg.goal)
        log.info("  Exit reason: %s", exit_reason)
        log.info("  Steps completed: %d / %d", final_step, self._cfg.max_steps)
        log.info("  Actions executed: %d", self._state.total_actions_executed)
        log.info("  Failures detected: %d", self._state.total_failures_detected)
        log.info(
            "  Stop verification failures: %d / %d",
            self._state.stop_verification_failures,
            self._cfg.max_stop_verification_failures,
        )
        log.info("  Conversation turns in memory: %d", len(self._memory.get_conversation_messages()))
        if self._stages:
            log.info(
                "  Stages completed: %d / %d",
                len(self._state.completed_stages),
                len(self._stages),
            )
            for s in self._state.completed_stages:
                log.info("    ✓ %s", s)
            remaining = [
                s.name for s in self._stages[self._state.current_stage_index:]
            ]
            for s in remaining:
                log.info("    ✗ %s", s)
        log.info("=" * 60)

        # Log the full session history for debugging
        log.debug("Full session history:")
        for record in self._memory.steps:
            log.debug(
                "  Step %d [%s]: %s → %s",
                record.step_number,
                record.window_title,
                [self._describe_action(a) for a in record.actions_executed],
                record.outcome,
            )
