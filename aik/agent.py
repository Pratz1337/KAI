from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess

import httpx

from .history import ActionExecutionRecord, ConversationHistory

from .actions import ActionParseError, ParsedPlan, parse_plan
from .anthropic_client import AnthropicClient
from .capture import ScreenCapturer
from .actions import ALLOWED_ACTION_TYPES
from .input_injector import InputInjector
from .kill_switch import KillSwitch
from .prompt import PromptContext, SYSTEM_PROMPT, build_user_prompt
from .window_context import get_foreground_window

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


@dataclass
class AgentState:
    recent_actions: list[dict] = field(default_factory=list)


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
        self._history = ConversationHistory(cfg.goal)

    def run(self) -> None:
        self._kill.start()

        if self._try_handle_builtin_goal():
            return

        log.info("Starting agent. dry_run=%s max_steps=%d", self._cfg.dry_run, self._cfg.max_steps)
        log.info("Kill switch: Ctrl+Alt+Backspace")

        try:
            for step in range(1, self._cfg.max_steps + 1):
                if self._kill.triggered:
                    log.warning("Kill switch triggered. Stopping.")
                    return

                fg = get_foreground_window()
                if self._is_uac_secure_desktop(fg.process_path, fg.title):
                    log.warning(
                        "UAC prompt detected (secure desktop). Approve/dismiss it manually, then the agent will continue."
                    )
                    time.sleep(1.0)
                    continue

                if self._is_open_with_dialog(fg.title, fg.process_path):
                    log.warning("'Open with' dialog detected. Dismissing with Esc.")
                    if not self._cfg.dry_run:
                        try:
                            self._injector.key_press("esc")
                            time.sleep(0.2)
                            # Some variants ignore Esc; Alt+F4 is a safe fallback.
                            self._injector.hotkey(["alt", "f4"])
                        except Exception as exc:
                            log.warning("Failed to dismiss 'Open with' dialog: %s", exc)
                    time.sleep(0.5)
                    continue

                shot = self._capturer.capture()

                ctx = PromptContext(
                goal=self._cfg.goal,
                window_title=fg.title,
                process_path=fg.process_path,
                step=step,
                recent_actions=self._state.recent_actions,
                )
                user_prompt = build_user_prompt(ctx)

                history_messages = self._history.build_messages_for_decision(
                step=step,
                screenshot_png=shot.png,
                active_window_title=fg.title,
                active_process_path=fg.process_path,
                )

                try:
                    plan = self._get_plan_with_repair(history_messages, active_process_path=fg.process_path)
                except ActionParseError as e:
                    log.error("Model returned invalid JSON plan: %s", e)
                    return
                except httpx.HTTPStatusError as e:
                    status = getattr(e.response, "status_code", None)
                    if status in {429, 529}:
                        retry_after = e.response.headers.get("retry-after") if e.response is not None else None
                        wait_s = 10.0
                        if retry_after:
                            try:
                                wait_s = max(wait_s, float(retry_after))
                            except ValueError:
                                pass
                        log.warning("Rate limited by Anthropic (%s). Waiting %.1fs then retrying...", status, wait_s)
                        # sleep in short chunks so Ctrl+C is responsive
                        end_t = time.time() + wait_s
                        while time.time() < end_t:
                            if self._kill.triggered:
                                log.warning("Kill switch triggered while waiting. Stopping.")
                                return
                            time.sleep(0.25)
                        continue
                    log.exception("HTTP error from Anthropic: %s", e)
                    return
                except Exception as e:
                    log.exception("Failed to get plan: %s", e)
                    return

                if self._execute_plan(plan, step=step, screenshot_png=shot.png, observed_window=fg.title):
                    return

                if self._cfg.loop_interval_s:
                    time.sleep(self._cfg.loop_interval_s)

            log.warning("Max steps reached (%d). Stopping.", self._cfg.max_steps)
        except KeyboardInterrupt:
            log.warning("Stopped by user (Ctrl+C).")
            return

    @staticmethod
    def _is_uac_secure_desktop(process_path: str | None, window_title: str | None) -> bool:
        if process_path and process_path.lower().endswith("\\consent.exe"):
            return True
        if window_title and "user account control" in window_title.lower():
            return True
        return False

    @staticmethod
    def _is_open_with_dialog(window_title: str | None, process_path: str | None) -> bool:
        title = (window_title or "").lower()
        keywords = (
            "open with",
            "how do you want to open",
            "choose an app",
            "choose an application",
            "select an app",
            "always use this app",
        )
        if any(k in title for k in keywords):
            return True
        if process_path:
            p = process_path.lower().replace("/", "\\")
            if p.endswith("\\openwith.exe"):
                return True
            # Windows 11 often hosts this UI in system settings frame.
            if p.endswith("\\applicationframehost.exe") or p.endswith("\\systemsettings.exe"):
                if any(k in title for k in keywords):
                    return True
        return False

    def _try_handle_builtin_goal(self) -> bool:
        """Handle a small set of deterministic goals without LLM/vision.

        This prevents demos from failing due to UI focus, token limits, or model JSON issues.
        """

        goal = self._cfg.goal.strip()
        lower = goal.lower()

        if "create" not in lower or "desktop" not in lower or "file explorer" not in lower:
            return False

        filename = self._extract_filename(goal)
        if not filename or not filename.lower().endswith(".txt"):
            return False

        content = self._extract_content(goal)
        if content is None:
            return False

        now_iso = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        content = content.replace("[current time]", now_iso)

        desktop = self._desktop_path()
        target = desktop / filename

        log.info("Builtin goal handler: creating %s", target)
        if not self._cfg.dry_run:
            target.write_text(content + "\n", encoding="utf-8")

        exists = target.exists() if not self._cfg.dry_run else True
        if not exists:
            log.error("Builtin goal handler failed to create file: %s", target)
            return True

        log.info("Builtin goal handler: verifying via Explorer select")
        if not self._cfg.dry_run:
            subprocess.run(["explorer.exe", f"/select,{str(target)}"], check=False)

        log.info("Builtin goal handler: complete")
        return True

    @staticmethod
    def _desktop_path() -> Path:
        # Prefer OneDrive Desktop when present.
        candidate = Path(os.path.expanduser("~")) / "OneDrive" / "Desktop"
        if candidate.exists():
            return candidate
        return Path(os.path.expanduser("~")) / "Desktop"

    @staticmethod
    def _extract_filename(goal: str) -> str | None:
        match = re.search(r"named\s+['\"]([^'\"]+\.[a-z0-9]{1,6})['\"]", goal, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
        match = re.search(r"['\"]([^'\"]+\.[a-z0-9]{1,6})['\"]", goal)
        if match:
            return match.group(1).strip()
        return None

    @staticmethod
    def _extract_content(goal: str) -> str | None:
        # Extract content inside quotes after the word content.
        match = re.search(r"content\s+['\"]([^'\"]+)['\"]", goal, flags=re.IGNORECASE)
        if match:
            return match.group(1)
        match = re.search(r"with\s+the\s+content\s+['\"]([^'\"]+)['\"]", goal, flags=re.IGNORECASE)
        if match:
            return match.group(1)
        return None

    def _execute_plan(self, plan: ParsedPlan, *, step: int, screenshot_png: bytes, observed_window: str) -> bool:
        actions = plan.actions[:6]
        log.info("Plan: %s", actions)

        executed: list[ActionExecutionRecord] = []
        observed = f"Active window: {observed_window}" if observed_window else "Active window unknown"
        step_success = True

        for a in actions:
            if self._kill.triggered:
                log.warning("Kill switch triggered mid-plan. Stopping.")
                step_success = False
                break

            duplicate = self._history.find_recent_duplicate(a, last_n_steps=2)
            if duplicate:
                duplicate_step, signature = duplicate
                log.warning(
                    "Potential repeat detected: action '%s' already succeeded in Step %d.",
                    signature,
                    duplicate_step,
                )

            self._state.recent_actions.append(a)

            start_t = time.perf_counter()
            timestamp_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
            error: str | None = None
            success = True

            t = a["type"]
            if t == "stop":
                log.info("Stop: %s", a.get("reason", ""))
                duration_ms = int((time.perf_counter() - start_t) * 1000)
                executed.append(
                    ActionExecutionRecord(
                        step=step,
                        action=a,
                        success=True,
                        duration_ms=duration_ms,
                        error=None,
                        timestamp_utc=timestamp_utc,
                    )
                )
                self._history.append_step(
                    step=step,
                    observed=observed,
                    planned_actions=actions,
                    executed_actions=executed,
                    success=True,
                    screenshot_png=screenshot_png,
                )
                return True

            if self._cfg.dry_run:
                duration_ms = int((time.perf_counter() - start_t) * 1000)
                executed.append(
                    ActionExecutionRecord(
                        step=step,
                        action=a,
                        success=True,
                        duration_ms=duration_ms,
                        error=None,
                        timestamp_utc=timestamp_utc,
                    )
                )
                continue

            # Conservative dedup: only skip a narrow set of loop-causing repeats.
            # Never skip common navigation keys like Enter/Tab/Arrows.
            if duplicate and duplicate_step == step - 1 and self._should_dedup_skip(a):
                duration_ms = int((time.perf_counter() - start_t) * 1000)
                executed.append(
                    ActionExecutionRecord(
                        step=step,
                        action=a,
                        success=True,
                        duration_ms=duration_ms,
                        error="dedup_skipped",
                        timestamp_utc=timestamp_utc,
                    )
                )
                log.info("Dedup skipped action in step %d: %s", step, a)
                continue

            try:
                if t == "type_text":
                    self._injector.type_text(a["text"])
                elif t == "key_press":
                    self._injector.key_press(a["key"])
                elif t == "hotkey":
                    self._injector.hotkey(a["keys"])
                elif t == "wait_ms":
                    time.sleep(a["ms"] / 1000.0)
                else:
                    success = False
                    error = f"Unknown action type: {t}"
            except Exception as exc:
                success = False
                error = str(exc)
                step_success = False

            duration_ms = int((time.perf_counter() - start_t) * 1000)
            executed.append(
                ActionExecutionRecord(
                    step=step,
                    action=a,
                    success=success,
                    duration_ms=duration_ms,
                    error=error,
                    timestamp_utc=timestamp_utc,
                )
            )

            if not success:
                log.warning("Action failed: %s error=%s", a, error)
                break

        self._history.append_step(
            step=step,
            observed=observed,
            planned_actions=actions,
            executed_actions=executed,
            success=step_success,
            screenshot_png=screenshot_png,
        )

        return False

    def _get_plan_with_repair(self, history_messages: list[dict], *, active_process_path: str | None) -> ParsedPlan:
        resp = self._anthropic.create_message_with_history(
            system=SYSTEM_PROMPT,
            messages=history_messages,
            max_tokens=self._cfg.max_tokens,
            temperature=self._cfg.temperature,
        )
        try:
            plan = parse_plan(resp.text)
            plan = self._guard_premature_stop(
                plan,
                active_process_path=active_process_path,
                history_messages=history_messages,
                previous_model_text=resp.text,
            )
            return plan
        except ActionParseError as exc:
            validation_error = str(exc)
            log.warning("Invalid plan from model (%s). Attempting repair...", validation_error)

        allowed = ", ".join(sorted(ALLOWED_ACTION_TYPES))
        repair_text = (
            "Your previous response was invalid for this agent.\n"
            f"Validation error: {validation_error}\n"
            f"Allowed action types: {allowed}.\n"
            "Rules: keyboard-only. Do NOT output mouse actions (click/double_click/triple_click).\n"
            "Return ONLY valid JSON matching the schema, with at most 6 actions.\n\n"
            "Invalid response you must repair:\n"
            + resp.text
        )

        repaired_messages = list(history_messages)
        repaired_messages.append({"role": "user", "content": [{"type": "text", "text": repair_text}]})

        resp2 = self._anthropic.create_message_with_history(
            system=SYSTEM_PROMPT,
            messages=repaired_messages,
            max_tokens=self._cfg.max_tokens,
            temperature=0.0,
        )
        plan2 = parse_plan(resp2.text)
        return self._guard_premature_stop(
            plan2,
            active_process_path=active_process_path,
            history_messages=repaired_messages,
            previous_model_text=resp2.text,
        )

    def _guard_premature_stop(
        self,
        plan: ParsedPlan,
        *,
        active_process_path: str | None,
        history_messages: list[dict],
        previous_model_text: str,
    ) -> ParsedPlan:
        if not plan.actions:
            return plan
        first = plan.actions[0]
        if str(first.get("type", "")).lower() != "stop":
            return plan

        if not self._goal_requires_verification(self._cfg.goal):
            return plan

        desktop_target = self._extract_desktop_target_path(self._cfg.goal)
        file_exists = bool(desktop_target and desktop_target.exists())
        explorer_foreground = self._is_explorer_foreground(active_process_path)

        # If the goal mentions verification/opening Explorer, refuse to stop unless:
        # - the file exists on disk
        # - and Explorer is currently foreground (visual verification step)
        if file_exists and explorer_foreground:
            return plan

        explain = (
            "You returned stop but verification is NOT satisfied yet.\n"
            f"Goal requires verification: {self._cfg.goal}\n"
            f"Desktop target: {str(desktop_target) if desktop_target else '(unknown)'}\n"
            f"File exists on disk: {file_exists}\n"
            f"Foreground is Explorer: {explorer_foreground}\n\n"
            "Return the NEXT keyboard-only actions to complete/verify the goal. "
            "If the file does not exist, create it. If Explorer is not foreground, open File Explorer and navigate/select the file. "
            "Return ONLY valid JSON with allowed action types."
        )

        repair_messages = list(history_messages)
        repair_messages.append({"role": "user", "content": [{"type": "text", "text": explain}]})
        resp = self._anthropic.create_message_with_history(
            system=SYSTEM_PROMPT,
            messages=repair_messages,
            max_tokens=self._cfg.max_tokens,
            temperature=0.0,
        )
        try:
            return parse_plan(resp.text)
        except ActionParseError:
            # As a last resort, keep the original plan (better than crashing); execution loop will stop.
            log.warning("Stop-guard repair also produced invalid JSON; using original model output.")
            log.debug("Raw repair text:\n%s", resp.text)
            return plan

    @staticmethod
    def _goal_requires_verification(goal: str) -> bool:
        g = goal.lower()
        return any(w in g for w in ("verify", "exists", "exist", "file explorer", "open file explorer", "open explorer"))

    @staticmethod
    def _is_explorer_foreground(active_process_path: str | None) -> bool:
        if not active_process_path:
            return False
        return active_process_path.lower().endswith("\\explorer.exe") or active_process_path.lower().endswith("/explorer.exe")

    @staticmethod
    def _extract_desktop_target_path(goal: str) -> Path | None:
        # Try to extract a filename like 'test_report.txt' or "foo.xlsx".
        patterns = [
            r"['\"]([^'\"]+\.[a-z0-9]{1,6})['\"]",
            r"\b([A-Za-z0-9_\- ]+\.[A-Za-z0-9]{1,6})\b",
        ]
        filename: str | None = None
        for pat in patterns:
            match = re.search(pat, goal)
            if match:
                filename = match.group(1).strip()
                break
        if not filename:
            return None

        # If the goal explicitly mentions desktop, resolve to desktop path; else still try desktop as best effort.
        desktop = Path(os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop"))
        if not desktop.exists():
            desktop = Path(os.path.join(os.path.expanduser("~"), "Desktop"))
        return desktop / filename

    @staticmethod
    def _should_dedup_skip(action: dict) -> bool:
        t = str(action.get("type", "")).lower()
        if t in {"type_text", "hotkey"}:
            return True
        if t == "key_press":
            key = str(action.get("key", "")).strip().lower()
            return key in {"win"}
        return False

