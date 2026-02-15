from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

from aik.agent import AgentConfig, KeyboardVisionAgent
from aik.anthropic_client import AnthropicClient
from aik.kill_switch import KillSwitch
from aik.logging_setup import setup_logging


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AIK: vision-based keyboard automation (user-mode)")
    p.add_argument("--goal", required=True, help="What you want the agent to accomplish.")
    p.add_argument("--dry-run", action="store_true", help="Print actions but do not inject keys.")
    p.add_argument("--max-steps", type=int, default=40)
    p.add_argument("--interval", type=float, default=0.8, help="Seconds between planning cycles.")
    p.add_argument("--monitor", type=int, default=1, help="mss monitor index (1=primary).")
    p.add_argument("--screenshot-max-width", type=int, default=1280)
    p.add_argument("--max-tokens", type=int, default=700)
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument("--model", default=os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20240620"))
    p.add_argument("--base-url", default=os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com"))
    p.add_argument("--anthropic-version", default=os.getenv("ANTHROPIC_VERSION", "2023-06-01"))
    p.add_argument("--log-level", default=os.getenv("AIK_LOG_LEVEL", "INFO"))
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
    )

    agent = KeyboardVisionAgent(cfg, anthropic=client, kill_switch=KillSwitch())
    agent.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
