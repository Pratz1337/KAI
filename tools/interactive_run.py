from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys

from dotenv import load_dotenv


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Interactive terminal launcher for AIK")
    p.add_argument("--python", default=sys.executable, help="Python executable to use")
    p.add_argument("--max-steps", type=int, default=20)
    p.add_argument("--interval", type=float, default=0.6)
    p.add_argument("--monitor", type=int, default=1)
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument("--model", default=os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"))
    p.add_argument("--log-level", default=os.getenv("AIK_LOG_LEVEL", "INFO"))
    p.add_argument("--overlay", action="store_true", help="Show overlay (default: on)")
    p.add_argument("--no-overlay", action="store_true", help="Disable overlay")
    p.add_argument("--memory", default=os.getenv("AIK_MEMORY_PATH", ".aik_memory.json"), help="Path to local agent memory JSON")
    p.add_argument("--learning", default=os.getenv("AIK_LEARNING_PATH", ".aik_learning.json"), help="Path to learning graph JSON")
    p.add_argument("--no-driver", action="store_true", help="Disable kernel-driver injection")
    p.add_argument("--dry-run-start", action="store_true", help="Start in dry-run mode (default is live)")
    p.add_argument("--live", action="store_true", help="Force live typing mode")
    return p.parse_args(argv)


def build_command(args: argparse.Namespace, goal: str, dry_run: bool) -> list[str]:
    cmd = [
        args.python,
        "main.py",
        "--goal",
        goal,
        "--max-steps",
        str(args.max_steps),
        "--interval",
        str(args.interval),
        "--monitor",
        str(args.monitor),
        "--max-tokens",
        str(args.max_tokens),
        "--temperature",
        str(args.temperature),
        "--model",
        args.model,
        "--log-level",
        args.log_level,
        "--memory",
        args.memory,
        "--learning",
        args.learning,
    ]
    if dry_run:
        cmd.append("--dry-run")
    if args.no_overlay:
        cmd.append("--no-overlay")
    else:
        cmd.append("--overlay")
    if args.no_driver:
        cmd.append("--no-driver")
    return cmd


def main(argv: list[str]) -> int:
    load_dotenv()
    args = parse_args(argv)

    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        print("Missing ANTHROPIC_API_KEY in environment/.env", file=sys.stderr)
        return 2

    dry_run = args.dry_run_start
    if args.live:
        dry_run = False

    def mode_name() -> str:
        return "dry-run" if dry_run else "live"

    print("AIK interactive launcher")
    print("Commands: /dry, /live, /status, /quit")
    print(f"Current mode: {mode_name()} (default: live)")

    while True:
        try:
            goal = input(f"\nGoal [{mode_name()}]> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            return 0

        if not goal:
            continue

        if goal.lower() in {"/quit", "quit", "exit"}:
            return 0
        if goal.lower() == "/dry":
            dry_run = True
            print("Mode set to dry-run")
            continue
        if goal.lower() == "/live":
            dry_run = False
            print("Mode set to live")
            continue
        if goal.lower() == "/status":
            print(f"Current mode: {mode_name()}")
            continue

        cmd = build_command(args, goal, dry_run)
        print(f"Mode: {mode_name()}")
        print("Running:", " ".join(shlex.quote(c) for c in cmd))
        proc = subprocess.run(cmd, check=False)
        print(f"Exit code: {proc.returncode}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
