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
    p.add_argument(
        "--elevate",
        action="store_true",
        help="Relaunch as Administrator via UAC prompt if not already elevated.",
    )
    p.add_argument(
        "--require-admin",
        action="store_true",
        help="Exit with an error if not running as Administrator.",
    )
    p.add_argument("--max-steps", type=int, default=40)
    p.add_argument("--interval", type=float, default=1.5, help="Seconds between planning cycles.")
    p.add_argument("--monitor", type=int, default=1, help="mss monitor index (1=primary).")
    p.add_argument("--screenshot-max-width", type=int, default=1280)
    p.add_argument("--max-tokens", type=int, default=700)
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument("--model", default=os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20240620"))
    p.add_argument("--base-url", default=os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com"))
    p.add_argument("--anthropic-version", default=os.getenv("ANTHROPIC_VERSION", "2023-06-01"))
    p.add_argument(
        "--min-api-interval",
        type=float,
        default=float(os.getenv("AIK_MIN_API_INTERVAL", "1.5")),
        help="Minimum seconds between Anthropic API requests (helps avoid rate limits).",
    )
    p.add_argument(
        "--api-retries",
        type=int,
        default=int(os.getenv("AIK_API_RETRIES", "4")),
        help="Retries for transient Anthropic API errors (429/529/5xx).",
    )
    p.add_argument("--log-level", default=os.getenv("AIK_LOG_LEVEL", "INFO"))
    p.add_argument(
        "--kernel",
        action="store_true",
        help="Use kernel-mode driver for keystroke injection (bypasses UIPI/UAC).",
    )
    p.add_argument(
        "--driver-path",
        default=os.getenv("AIK_DRIVER_PATH", r"\\.\AikKmdfIoctl"),
        help="Device path for the kernel driver.",
    )
    p.add_argument(
        "--no-verify",
        action="store_true",
        help="Disable vision-based goal verification (not recommended; may cause false 'success').",
    )
    p.add_argument(
        "--verify-threshold",
        type=float,
        default=0.8,
        help="Confidence threshold (0..1) required to accept a verified stop.",
    )
    p.add_argument(
        "--no-decompose",
        action="store_true",
        help="Disable automatic goal decomposition into verifiable stages.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Enable detailed per-step logging with action descriptions and post-action diagnostics.",
    )
    p.add_argument(
        "--history-limit",
        type=int,
        default=60,
        help="Max number of conversation turns to keep in context (default: 60).",
    )
    p.add_argument(
        "--no-verify-steps",
        action="store_true",
        help="Disable micro-verification of every action (verification is ON by default).",
    )
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    load_dotenv()
    args = parse_args(argv)
    setup_logging(args.log_level)

    if args.elevate or args.require_admin:
        from aik.elevation import is_admin, relaunch_as_admin

        if not is_admin():
            if args.require_admin and not args.elevate:
                print(
                    "This command must be run as Administrator.\n"
                    "Re-run your terminal as Admin, or pass --elevate to trigger a UAC prompt.",
                    file=sys.stderr,
                )
                return 3

            try:
                relaunch_as_admin(argv=sys.argv)
            except Exception as e:
                print(f"Failed to elevate: {e}", file=sys.stderr)
                return 4
            return 0

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("Missing ANTHROPIC_API_KEY (set it in env or .env).", file=sys.stderr)
        return 2

    client = AnthropicClient(
        api_key=api_key,
        model=args.model,
        base_url=args.base_url,
        anthropic_version=args.anthropic_version,
        max_retries=args.api_retries,
        min_interval_s=float(args.min_api_interval),
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
        kernel_mode=args.kernel,
        driver_path=args.driver_path,
        verify_goal_on_stop=not args.no_verify,
        verify_confidence_threshold=float(args.verify_threshold),
        use_goal_decomposition=not args.no_decompose,
        verbose_logging=args.verbose,
        max_conversation_turns=args.history_limit,
        verify_steps=not args.no_verify_steps,
    )

    agent = KeyboardVisionAgent(cfg, anthropic=client, kill_switch=KillSwitch())
    agent.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
