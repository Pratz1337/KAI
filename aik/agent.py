"""Core agent loop — vision → VLM → action execution.

Restructured with:
* Mouse + keyboard actions
* Screenshot-change detection (stale screen → previous action failed)
* Progressive backtracking when stuck
* Learning graph integration (tips + failure avoidance)
* Dual-mode injection: kernel driver (if loaded) or user-mode SendInput
"""

from __future__ import annotations

import hashlib
import logging
import random
import sys
import time
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field

from .actions import ActionParseError, ParsedPlan, parse_plan
from .anthropic_client import AnthropicClient
from .capture import ScreenCapturer, Screenshot
from .driver_bridge import DriverBridge
from .input_injector import InputInjector
from .kill_switch import KillSwitch
from .learning import LearningGraph
from .history import ActionExecutionRecord, ConversationHistory
from .memory import Memory
from .overlay import Overlay, OverlayState
from .prompt import PromptContext, SYSTEM_PROMPT, build_user_prompt
from .window_context import get_foreground_window
from .app_focus import focus_app_for_goal

log = logging.getLogger("aik.agent")


# ── configuration ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AgentConfig:
    goal: str
    max_steps: int = 60
    loop_interval_s: float = 0.8
    max_tokens: int = 1024
    temperature: float = 0.2
    monitor_index: int = 1
    screenshot_max_width: int | None = 1280
    dry_run: bool = False
    inter_key_delay_s: float = 0.01
    memory_path: str = ".aik_memory.json"
    learning_path: str = ".aik_learning.json"
    use_driver: bool = True  # try kernel driver, fall back to SendInput


@dataclass
class AgentState:
    recent_actions: list[dict] = field(default_factory=list)
    human_notes: list[str] = field(default_factory=list)
    recent_plan_sigs: list[str] = field(default_factory=list)


# ── agent ────────────────────────────────────────────────────────────────────

class KeyboardVisionAgent:
    def __init__(
        self,
        cfg: AgentConfig,
        *,
        anthropic: AnthropicClient,
        kill_switch: KillSwitch | None = None,
        overlay: Overlay | None = None,
    ) -> None:
        self._cfg = cfg
        self._anthropic = anthropic
        self._kill = kill_switch or KillSwitch()
        self._capturer = ScreenCapturer(
            monitor_index=cfg.monitor_index, max_width=cfg.screenshot_max_width,
        )
        self._injector = InputInjector(inter_key_delay_s=cfg.inter_key_delay_s)
        self._state = AgentState()
        self._overlay = overlay
        self._current_step = 0
        self._last_monitor: dict | None = None
        self._last_shot_width = 0
        self._last_shot_height = 0
        self._memory = Memory.load(cfg.memory_path)
        self._learning = LearningGraph.load(cfg.learning_path)
        self._history = ConversationHistory(cfg.goal)

        # Screenshot-change detection
        self._prev_screen_hash: str | None = None
        self._stale_count = 0       # consecutive steps where screen didn't change
        self._backtrack_level = 0   # progressive backtrack depth

        # Driver bridge (optional)
        self._driver: DriverBridge | None = None
        self._injection_mode = "user-mode"
        if cfg.use_driver:
            drv = DriverBridge()
            if drv.open() and drv.ping():
                self._driver = drv
                self._injection_mode = "kernel"
                log.info("Kernel driver available — using kernel-mode injection.")
            else:
                drv.close()
                log.info("Kernel driver not available — using user-mode SendInput.")

    # ── main loop ────────────────────────────────────────────────────────

    def run(self) -> None:
        self._kill.start()
        if self._overlay is not None:
            self._overlay.start()

        # Deterministic fast-path for common demo goals.
        if self._try_handle_builtin_goal():
            return

        log.info(
            "Agent starting. dry_run=%s  max_steps=%d  injection=%s",
            self._cfg.dry_run, self._cfg.max_steps, self._injection_mode,
        )
        log.info("Kill switch: Ctrl+Alt+Backspace")

        try:
            self._loop()
        finally:
            if self._driver is not None:
                self._driver.close()

    def _loop(self) -> None:
        for step in range(1, self._cfg.max_steps + 1):
            self._current_step = step

            # 0. Kill switch
            if self._kill.triggered:
                log.warning("Kill switch triggered. Stopping.")
                return

            # 1. Focus the target app (best-effort)
            try:
                focus_app_for_goal(self._cfg.goal)
            except Exception:
                pass

            # 2. Capture screenshot
            shot = self._capturer.capture()
            self._last_monitor = shot.monitor
            self._last_shot_width = shot.width
            self._last_shot_height = shot.height

            # 3. Detect whether screen changed since last step
            screen_changed = self._did_screen_change(shot)

            if not screen_changed and self._state.recent_actions:
                self._stale_count += 1
            else:
                self._stale_count = 0
                self._backtrack_level = 0  # reset backtrack on progress

            # 4. If stuck, try progressive backtracking
            if self._stale_count >= 2:
                self._progressive_backtrack()
                time.sleep(0.6)
                continue

            # 5. Read foreground window
            fg = get_foreground_window()

            # If Windows is showing UAC on the secure desktop, automation can't interact.
            if self._is_uac_secure_desktop(fg.process_path, fg.title):
                log.warning(
                    "UAC prompt detected (secure desktop). Approve/dismiss it manually, then the agent will continue."
                )
                time.sleep(1.0)
                continue

            # Handle Windows 'Open with / Choose an app' dialog so we don't get stuck.
            if self._is_open_with_dialog(fg.title, fg.process_path):
                log.warning("'Open with' dialog detected. Dismissing (Esc, then Alt+F4).")
                if not self._cfg.dry_run:
                    try:
                        self._injector.key_press("esc")
                        time.sleep(0.2)
                        self._injector.hotkey(["alt", "f4"])
                    except Exception as exc:
                        log.warning("Failed to dismiss 'Open with' dialog: %s", exc)
                time.sleep(0.6)
                continue

            # 6. Gather learning context
            app_name = _detect_app(fg.title, fg.process_path)
            tips = self._learning.get_tips(app=app_name, goal=self._cfg.goal)
            failed = self._learning.get_recent_failures(app=app_name, goal=self._cfg.goal)

            # 7. Build prompt
            ctx = PromptContext(
                goal=self._cfg.goal,
                window_title=fg.title,
                process_path=fg.process_path,
                step=step,
                recent_actions=self._state.recent_actions,
                screenshot_width=shot.width,
                screenshot_height=shot.height,
                human_notes=self._state.human_notes,
                learning_tips=tips,
                failed_actions=failed,
                screen_changed=screen_changed,
                injection_mode=self._injection_mode,
            )
            user_prompt = build_user_prompt(ctx)

            history_messages = self._history.build_messages_for_decision(
                step=step,
                screenshot_png=shot.png,
                active_window_title=fg.title,
                active_process_path=fg.process_path,
                user_text=user_prompt,
            )

            # 8. Call VLM (history-aware)
            plan = self._call_vlm(history_messages, shot, step)
            if plan is None:
                continue  # retry (rate-limit) or bail

            # 9. Update overlay
            self._update_overlay_plan(plan, step)

            # 10. Stuck detection via plan signature
            sig = _plan_signature(plan)
            self._state.recent_plan_sigs.append(sig)
            self._state.recent_plan_sigs = self._state.recent_plan_sigs[-5:]
            if (
                len(self._state.recent_plan_sigs) >= 2
                and len(set(self._state.recent_plan_sigs[-2:])) == 1
                and not screen_changed
            ):
                log.warning("Plan repetition + unchanged screen → stuck.")
                self._stale_count = 2  # trigger backtrack on next iteration
                # Record the repeated action as a failure for learning
                if plan.actions:
                    self._learning.record_failure(
                        app=app_name,
                        goal=self._cfg.goal,
                        action=plan.actions[0],
                        reason="Repeated same action with no screen change",
                    )
                continue

            # 11. Execute plan
            if self._execute_plan(plan, shot, active_window_title=fg.title, active_process_path=fg.process_path):
                # Record success
                self._learning.record_success(
                    app=app_name,
                    goal=self._cfg.goal,
                    actions=self._state.recent_actions[-12:],
                    note=f"Completed: {self._cfg.goal}",
                )
                return

            # 12. Save screenshot hash for next comparison
            self._prev_screen_hash = self._hash_screenshot(shot)

            # Sleep between cycles
            if self._cfg.loop_interval_s:
                time.sleep(self._cfg.loop_interval_s)

        log.warning("Max steps reached (%d). Stopping.", self._cfg.max_steps)
        self._update_overlay_simple("max steps reached")

    # ── VLM call ─────────────────────────────────────────────────────────

    def _call_vlm(self, history_messages: list[dict], shot: Screenshot, step: int) -> ParsedPlan | None:
        try:
            resp = self._anthropic.create_message_with_history(
                system=SYSTEM_PROMPT,
                messages=history_messages,
                max_tokens=self._cfg.max_tokens,
                temperature=self._cfg.temperature,
            )
            try:
                return parse_plan(resp.text)
            except ActionParseError as pe:
                # One repair attempt
                repair = (
                    f"Your previous response was INVALID: {pe}\n\n"
                    "Return corrected JSON only matching the schema.\n\n"
                    f"Original response:\n{resp.text or ''}"
                )
                repaired_messages = list(history_messages)
                repaired_messages.append({"role": "user", "content": [{"type": "text", "text": repair}]})
                resp2 = self._anthropic.create_message_with_history(
                    system=SYSTEM_PROMPT,
                    messages=repaired_messages,
                    max_tokens=self._cfg.max_tokens,
                    temperature=max(0.0, min(0.3, self._cfg.temperature)),
                )
                return parse_plan(resp2.text)

        except ActionParseError as e:
            log.error("Model returned invalid JSON plan: %s", e)
            return None
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 429:
                sleep_s = 8.0 + random.random() * 4.0
                log.warning("Rate limited (429). Sleeping %.1fs…", sleep_s)
                time.sleep(sleep_s)
                return None
            log.exception("VLM call failed: %s", e)
            return None

    # ── plan execution ───────────────────────────────────────────────────

    def _execute_plan(
        self,
        plan: ParsedPlan,
        shot: Screenshot,
        *,
        active_window_title: str,
        active_process_path: str | None,
    ) -> bool:
        """Execute actions.  Returns True if "stop" was reached."""
        actions = plan.actions[:6]
        log.info("Plan (%d actions): %s", len(actions), actions)

        step = self._current_step
        observed = ""
        if plan.meta and isinstance(plan.meta.get("observation"), str):
            observed = str(plan.meta.get("observation", "")).strip()
        if not observed:
            observed = f"Active window: {active_window_title}" if active_window_title else "Active window unknown"

        executed: list[ActionExecutionRecord] = []
        step_success = True
        stop_reached = False
        ask_user_break = False

        for a in actions:
            if self._kill.triggered:
                log.warning("Kill switch triggered mid-plan.")
                step_success = False
                break

            dup = self._history.check_duplicate_action(a, last_n_steps=3)
            if dup:
                log.warning("%s", dup)

            self._state.recent_actions.append(a)
            # Keep recent_actions bounded
            if len(self._state.recent_actions) > 30:
                self._state.recent_actions = self._state.recent_actions[-30:]

            t = a["type"]

            start_t = time.perf_counter()
            timestamp_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
            success = True
            error: str | None = None

            # ── stop ──
            if t == "stop":
                reason = a.get("reason", "")
                log.info("STOP: %s", reason)
                self._memory.append_event({"type": "stop", "reason": reason, "ts": int(time.time())})
                self._update_overlay_simple(f"done: {reason}")
                stop_reached = True
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
                break

            # ── ask_user ──
            if t == "ask_user":
                choice = _prompt_user_choice(a["question"], a["options"])
                self._state.human_notes.append(f"Q: {a['question']} → {choice}")
                self._memory.append_event({
                    "type": "ask_user", "question": a["question"],
                    "choice": choice, "ts": int(time.time()),
                })
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
                ask_user_break = True
                break  # re-capture after user input

            if self._cfg.dry_run:
                log.info("[dry-run] would execute: %s", a)
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

            try:
                # ── keyboard actions ──
                if t == "type_text":
                    self._do_type_text(a["text"])
                elif t == "key_press":
                    self._do_key_press(a["key"])
                elif t == "hotkey":
                    self._do_hotkey(a["keys"])
                elif t == "wait_ms":
                    time.sleep(a["ms"] / 1000.0)

                # ── mouse actions ──
                elif t == "mouse_click":
                    self._do_mouse_click(a, shot)
                elif t == "mouse_scroll":
                    self._do_mouse_scroll(a, shot)
            except Exception as exc:
                success = False
                error = str(exc)
                step_success = False
                log.warning("Action execution failed: %s error=%s", a, error)

            self._update_overlay_action(a)

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
                break

        # Persist step memory for next decision.
        try:
            self._history.append_step(
                step=step,
                observed=observed,
                planned_actions=actions,
                executed_actions=executed,
                success=step_success,
                screenshot_png=shot.png,
            )
        except Exception as exc:
            log.debug("Failed to append history step: %s", exc)

        if ask_user_break:
            return False

        return stop_reached

    # ── keyboard dispatch ────────────────────────────────────────────────

    def _do_type_text(self, text: str) -> None:
        if self._driver is not None:
            if not self._driver.inject_text(text):
                self._injector.type_text(text)  # fallback
        else:
            self._injector.type_text(text)

    def _do_key_press(self, key: str) -> None:
        # Kernel driver path would need scancode mapping; use user-mode for simplicity
        self._injector.key_press(key)

    def _do_hotkey(self, keys: list[str]) -> None:
        self._injector.hotkey(keys)

    # ── mouse dispatch ───────────────────────────────────────────────────

    def _do_mouse_click(self, action: dict, shot: Screenshot) -> None:
        sx, sy = int(action["x"]), int(action["y"])
        button = action.get("button", "left")
        clicks = action.get("clicks", 1)

        nx, ny = self._screenshot_to_virtual(sx, sy, shot)
        self._injector.mouse_move_smooth(nx, ny, steps=10, step_delay_s=0.003)
        time.sleep(0.03)
        self._injector.mouse_click(button, clicks=clicks)
        log.info("mouse_click (%d,%d) → virtual (%.4f,%.4f) button=%s clicks=%d",
                 sx, sy, nx, ny, button, clicks)

    def _do_mouse_scroll(self, action: dict, shot: Screenshot) -> None:
        sx, sy = int(action["x"]), int(action["y"])
        direction = action.get("direction", "down")
        scroll_clicks = action.get("clicks", 3)

        nx, ny = self._screenshot_to_virtual(sx, sy, shot)
        self._injector.mouse_move_smooth(nx, ny, steps=8, step_delay_s=0.003)
        time.sleep(0.02)
        delta = 120 * scroll_clicks * (1 if direction == "up" else -1)
        self._injector.mouse_scroll(delta)
        log.info("mouse_scroll (%d,%d) direction=%s clicks=%d", sx, sy, direction, scroll_clicks)

    def _screenshot_to_virtual(self, sx: int, sy: int, shot: Screenshot) -> tuple[float, float]:
        """Convert screenshot pixel coords → normalized virtual-desktop coords (0..1)."""
        mon = shot.monitor or {}
        mon_w = float(mon.get("width", shot.width))
        mon_h = float(mon.get("height", shot.height))
        shot_w = float(shot.width) or 1.0
        shot_h = float(shot.height) or 1.0

        # Scale screenshot pixel → actual monitor pixel
        scale_x = mon_w / shot_w
        scale_y = mon_h / shot_h
        actual_x = float(mon.get("left", 0)) + sx * scale_x
        actual_y = float(mon.get("top", 0)) + sy * scale_y

        # Convert to virtual-desktop normalized
        vs_left = float(mon.get("__virtual_screen_left", 0))
        vs_top = float(mon.get("__virtual_screen_top", 0))
        vs_w = float(mon.get("__virtual_screen_width", mon_w))
        vs_h = float(mon.get("__virtual_screen_height", mon_h))

        if vs_w <= 0:
            vs_w = mon_w
        if vs_h <= 0:
            vs_h = mon_h

        nx = (actual_x - vs_left) / vs_w
        ny = (actual_y - vs_top) / vs_h
        return max(0.0, min(1.0, nx)), max(0.0, min(1.0, ny))

    # ── screenshot change detection ──────────────────────────────────────

    @staticmethod
    def _hash_screenshot(shot: Screenshot) -> str:
        return hashlib.md5(shot.png).hexdigest()

    def _did_screen_change(self, shot: Screenshot) -> bool:
        if self._prev_screen_hash is None:
            self._prev_screen_hash = self._hash_screenshot(shot)
            return True
        h = self._hash_screenshot(shot)
        changed = h != self._prev_screen_hash
        self._prev_screen_hash = h
        return changed

    # ── progressive backtracking ─────────────────────────────────────────

    def _progressive_backtrack(self) -> None:
        """Escalating recovery when the agent is stuck."""
        self._backtrack_level += 1
        level = self._backtrack_level

        if level == 1:
            log.info("Backtrack L1: pressing Esc")
            self._injector.key_press("esc")
            self._state.human_notes.append("AUTO-BACKTRACK L1: pressed Esc")

        elif level == 2:
            log.info("Backtrack L2: click neutral area + Esc")
            self._injector.mouse_move_normalized(0.5, 0.5)
            time.sleep(0.02)
            self._injector.mouse_click("left")
            time.sleep(0.3)
            self._injector.key_press("esc")
            self._state.human_notes.append("AUTO-BACKTRACK L2: clicked center + Esc")

        elif level == 3:
            log.info("Backtrack L3: instruct model to change strategy")
            self._state.human_notes.append(
                "AUTO-BACKTRACK L3: Your previous actions had NO effect. "
                "You MUST try a COMPLETELY different approach now. "
                "If mouse clicks aren't working, use keyboard shortcuts. "
                "If keyboard isn't working, try clicking a different area. "
                "Consider scrolling to find the element if it's not visible."
            )
            self._stale_count = 0  # let model try again

        elif level == 4:
            log.info("Backtrack L4: Alt+Tab and back")
            self._injector.hotkey(["alt", "tab"])
            time.sleep(0.8)
            self._injector.hotkey(["alt", "tab"])
            self._state.human_notes.append("AUTO-BACKTRACK L4: Alt+Tab cycle")
            self._stale_count = 0

        else:
            log.info("Backtrack L5+: asking user for help")
            if _is_interactive():
                choice = _prompt_user_choice(
                    "Agent is stuck. What should it do?",
                    [
                        "Give a text hint",
                        "Press Esc and retry",
                        "Try keyboard shortcuts instead",
                        "Skip this and continue",
                    ],
                )
                if "hint" in choice.lower():
                    try:
                        note = input("Hint: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        note = ""
                    if note:
                        self._state.human_notes.append(f"USER HINT: {note}")
                        self._learning.add_tip(
                            app=_detect_app_from_goal(self._cfg.goal),
                            tip=note,
                        )
                elif "esc" in choice.lower():
                    self._injector.key_press("esc")
                elif "keyboard" in choice.lower():
                    self._state.human_notes.append(
                        "USER: use keyboard shortcuts instead of mouse"
                    )
                # Reset
                self._stale_count = 0
                self._backtrack_level = 0
            else:
                # Non-interactive: hard reset
                self._injector.key_press("esc")
                self._state.human_notes.append(
                    "AUTO-BACKTRACK: stuck with no human available. Pressed Esc."
                )
                self._stale_count = 0
                self._backtrack_level = 0

    # ── overlay helpers ──────────────────────────────────────────────────

    def _update_overlay_plan(self, plan: ParsedPlan, step: int) -> None:
        if self._overlay is None:
            return
        meta = plan.meta or {}
        progress_text = str(meta.get("progress", "planning"))[:120]
        # Let the VLM's progress text update the history checklist
        self._history.update_checklist_from_vlm(progress_text)
        self._overlay.update(OverlayState(
            goal=self._cfg.goal,
            step=step,
            max_steps=self._cfg.max_steps,
            mode=("dry-run" if self._cfg.dry_run else f"live/{self._injection_mode}"),
            progress=progress_text,
            estimated_total_steps=_int_or_none(meta.get("estimated_total_steps")),
            checklist_tasks=tuple(self._history.progress.tasks),
            checklist_completed=frozenset(self._history.progress.completed),
        ))

    # ── built-in deterministic goal handler ─────────────────────────────

    def _try_handle_builtin_goal(self) -> bool:
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
        match = re.search(r"content\s+['\"]([^'\"]+)['\"]", goal, flags=re.IGNORECASE)
        if match:
            return match.group(1)
        match = re.search(r"with\s+the\s+content\s+['\"]([^'\"]+)['\"]", goal, flags=re.IGNORECASE)
        if match:
            return match.group(1)
        return None

    # ── dialog detection ───────────────────────────────────────────────

    @staticmethod
    def _is_uac_secure_desktop(process_path: str | None, window_title: str | None) -> bool:
        if process_path and process_path.lower().replace("/", "\\").endswith("\\consent.exe"):
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
            if p.endswith("\\applicationframehost.exe") or p.endswith("\\systemsettings.exe"):
                if any(k in title for k in keywords):
                    return True
        return False

    def _update_overlay_action(self, action: dict) -> None:
        if self._overlay is None:
            return
        self._overlay.update(OverlayState(
            goal=self._cfg.goal,
            step=self._current_step,
            max_steps=self._cfg.max_steps,
            mode=("dry-run" if self._cfg.dry_run else f"live/{self._injection_mode}"),
            progress="executing",
            last_action=_action_summary(action),
            checklist_tasks=tuple(self._history.progress.tasks),
            checklist_completed=frozenset(self._history.progress.completed),
        ))

    def _update_overlay_simple(self, progress: str) -> None:
        if self._overlay is None:
            return
        self._overlay.update(OverlayState(
            goal=self._cfg.goal,
            step=self._current_step,
            max_steps=self._cfg.max_steps,
            mode=("dry-run" if self._cfg.dry_run else f"live/{self._injection_mode}"),
            progress=progress,
        ))


# ── utility functions ────────────────────────────────────────────────────────

def _plan_signature(plan: ParsedPlan) -> str:
    """Quick fingerprint of a plan for repetition detection."""
    parts: list[str] = []
    for a in plan.actions[:6]:
        t = a.get("type", "")
        key = (
            a.get("key")
            or a.get("text", "")[:20]
            or str(a.get("keys"))
            or f"{a.get('x')},{a.get('y')}"
        )
        parts.append(f"{t}:{key}")
    return "|".join(parts)


def _detect_app(title: str, process_path: str | None) -> str:
    """Guess which app is active from window title / process path."""
    t = (title or "").lower()
    p = (process_path or "").lower()
    if "chrome" in p or "chrome" in t:
        if "gmail" in t or "inbox" in t:
            return "gmail"
        return "chrome"
    if "spotify" in p or "spotify" in t:
        return "spotify"
    if "notepad" in p:
        return "notepad"
    if "explorer" in p:
        return "explorer"
    if "code" in p:
        return "vscode"
    return "unknown"


def _detect_app_from_goal(goal: str) -> str:
    g = (goal or "").lower()
    if "gmail" in g or "email" in g or "mail" in g:
        return "gmail"
    if "chrome" in g:
        return "chrome"
    if "spotify" in g:
        return "spotify"
    if "notepad" in g:
        return "notepad"
    return "general"


def _action_summary(action: dict) -> str:
    t = action.get("type", "")
    if t == "type_text":
        return f"type: {str(action.get('text', ''))[:40]}"
    if t == "key_press":
        return f"key: {action.get('key')}"
    if t == "hotkey":
        return f"hotkey: {action.get('keys')}"
    if t == "mouse_click":
        return f"click ({action.get('x')},{action.get('y')}) {action.get('button')}"
    if t == "mouse_scroll":
        return f"scroll ({action.get('x')},{action.get('y')}) {action.get('direction')}"
    if t == "wait_ms":
        return f"wait {action.get('ms')}ms"
    return t


def _int_or_none(v: object) -> int | None:
    try:
        if v is None:
            return None
        n = int(v)  # type: ignore[arg-type]
        return n if n > 0 else None
    except Exception:
        return None


def _is_interactive() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


def _prompt_user_choice(question: str, options: list[str]) -> str:
    print(f"\nAIK needs your choice:\n{question}")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    while True:
        try:
            s = input(f"Choose 1-{len(options)} (or type your own): ").strip()
        except (EOFError, KeyboardInterrupt):
            return options[0]
        if not s:
            return options[0]
        if s.isdigit():
            idx = int(s)
            if 1 <= idx <= len(options):
                return options[idx - 1]
        return s



