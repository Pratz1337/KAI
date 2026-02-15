from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .actions import ActionParseError, ParsedPlan, parse_plan
from .anthropic_client import AnthropicClient
from .capture import ScreenCapturer
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

    def run(self) -> None:
        self._kill.start()

        log.info("Starting agent. dry_run=%s max_steps=%d", self._cfg.dry_run, self._cfg.max_steps)
        log.info("Kill switch: Ctrl+Alt+Backspace")

        for step in range(1, self._cfg.max_steps + 1):
            if self._kill.triggered:
                log.warning("Kill switch triggered. Stopping.")
                return

            shot = self._capturer.capture()
            fg = get_foreground_window()

            ctx = PromptContext(
                goal=self._cfg.goal,
                window_title=fg.title,
                process_path=fg.process_path,
                step=step,
                recent_actions=self._state.recent_actions,
            )
            user_prompt = build_user_prompt(ctx)

            try:
                resp = self._anthropic.create_message(
                    system=SYSTEM_PROMPT,
                    user_text=user_prompt,
                    image_png=shot.png,
                    max_tokens=self._cfg.max_tokens,
                    temperature=self._cfg.temperature,
                )
                plan = parse_plan(resp.text)
            except ActionParseError as e:
                log.error("Model returned invalid JSON plan: %s", e)
                log.debug("Raw model text:\n%s", getattr(resp, "text", ""))
                return
            except Exception as e:
                log.exception("Failed to get plan: %s", e)
                return

            if self._execute_plan(plan):
                return

            if self._cfg.loop_interval_s:
                time.sleep(self._cfg.loop_interval_s)

        log.warning("Max steps reached (%d). Stopping.", self._cfg.max_steps)

    def _execute_plan(self, plan: ParsedPlan) -> bool:
        actions = plan.actions[:6]
        log.info("Plan: %s", actions)

        for a in actions:
            if self._kill.triggered:
                log.warning("Kill switch triggered mid-plan. Stopping.")
                return True

            self._state.recent_actions.append(a)

            t = a["type"]
            if t == "stop":
                log.info("Stop: %s", a.get("reason", ""))
                return True

            if self._cfg.dry_run:
                continue

            if t == "type_text":
                self._injector.type_text(a["text"])
            elif t == "key_press":
                self._injector.key_press(a["key"])
            elif t == "hotkey":
                self._injector.hotkey(a["keys"])
            elif t == "wait_ms":
                time.sleep(a["ms"] / 1000.0)

        return False

