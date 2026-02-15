from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

from aik.agent import AgentConfig, KeyboardVisionAgent
from aik.anthropic_client import AnthropicClient
from aik.kill_switch import KillSwitch
from aik.logging_setup import setup_logging
from aik.overlay import Overlay


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AIK: vision-based desktop automation agent")
    p.add_argument("--goal", required=True, help="What you want the agent to accomplish.")
    p.add_argument("--dry-run", action="store_true", help="Print actions but do not inject keys.")
    p.add_argument("--max-steps", type=int, default=60)
    p.add_argument("--interval", type=float, default=0.8, help="Seconds between planning cycles.")
    p.add_argument("--monitor", type=int, default=1, help="mss monitor index (1=primary).")
    p.add_argument("--screenshot-max-width", type=int, default=1280)
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument("--model", default=os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"))
    p.add_argument("--base-url", default=os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com"))
    p.add_argument("--anthropic-version", default=os.getenv("ANTHROPIC_VERSION", "2023-06-01"))
    p.add_argument("--log-level", default=os.getenv("AIK_LOG_LEVEL", "INFO"))
    p.add_argument("--overlay", action="store_true", help="Show small always-on-top progress overlay.")
    p.add_argument("--no-overlay", action="store_true", help="Disable overlay.")
    p.add_argument("--memory", default=os.getenv("AIK_MEMORY_PATH", ".aik_memory.json"), help="Path to local agent memory JSON.")
    p.add_argument("--learning", default=os.getenv("AIK_LEARNING_PATH", ".aik_learning.json"), help="Path to learning graph JSON.")
    p.add_argument("--no-driver", action="store_true", help="Disable kernel-driver injection (use SendInput only).")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    load_dotenv()
    args = parse_args(argv)
    setup_logging(args.log_level)

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("Missing ANTHROPIC_API_KEY (set it in env or .env).", file=sys.stderr)
        return 2

    client = AnthropicClient(
        api_key=api_key,
        model=args.model,
        base_url=args.base_url,
        anthropic_version=args.anthropic_version,
    )

    cfg = AgentConfig(
        goal=args.goal,
        dry_run=args.dry_run,
        max_steps=args.max_steps,
        loop_interval_s=args.interval,
        monitor_index=args.monitor,
        screenshot_max_width=args.screenshot_max_width,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        memory_path=args.memory,
        learning_path=args.learning,
        use_driver=not args.no_driver,
    )

    enable_overlay = bool(args.overlay) or (not bool(args.no_overlay))
    ov = Overlay() if enable_overlay else None

    agent = KeyboardVisionAgent(cfg, anthropic=client, kill_switch=KillSwitch(), overlay=ov)
    agent.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
