from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

from aik.agent import AgentConfig, KeyboardVisionAgent
from aik.anthropic_client import AnthropicClient
from aik.glass_overlay import GlassOverlay
from aik.kill_switch import KillSwitch
from aik.logging_setup import setup_logging
from aik.voice_input import VoiceRecognizer


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AIK: vision-based desktop automation agent")
    p.add_argument("--goal", default="", help="What you want the agent to accomplish. If omitted, opens interactive prompt.")
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
    p.add_argument("--no-overlay", action="store_true", help="Disable the glass overlay.")
    p.add_argument("--overlay", action="store_true", help="(legacy, overlay is on by default)")
    p.add_argument("--basic-overlay", action="store_true", help="Use the basic text overlay instead of glass UI.")
    p.add_argument("--memory", default=os.getenv("AIK_MEMORY_PATH", ".aik_memory.json"), help="Path to local agent memory JSON.")
    p.add_argument("--learning", default=os.getenv("AIK_LEARNING_PATH", ".aik_learning.json"), help="Path to learning graph JSON.")
    p.add_argument("--no-driver", action="store_true", help="Disable kernel-driver injection (use SendInput only).")
    p.add_argument("--history-path", default=os.getenv("AIK_HISTORY_PATH", ".aik_history.json"), help="Path to session history JSON.")
    p.add_argument("--history-log-path", default=os.getenv("AIK_HISTORY_LOG_PATH", ".aik_history.jsonl"), help="Path to append-only JSONL step log.")
    p.add_argument("--no-border", action="store_true", help="Disable the purple screen border indicator.")
    p.add_argument("--voice-provider", choices=["sarvam", "google"], default="sarvam", help="Voice-to-text provider for mic.")
    p.add_argument("--voice-lang", default="en-IN", help="Comma-separated language codes for voice recognition.")
    return p.parse_args(argv)


def _build_voice(args: argparse.Namespace) -> VoiceRecognizer | None:
    """Create a VoiceRecognizer if credentials are available."""
    sarvam_key = os.getenv("SARVAM_API_KEY", "").strip()
    lang_codes = [c.strip() for c in args.voice_lang.split(",") if c.strip()] or ["en-IN"]
    try:
        vr = VoiceRecognizer(
            provider=args.voice_provider,
            sarvam_api_key=sarvam_key,
            language_codes=lang_codes,
        )
        if vr.available:
            return vr
    except Exception:
        pass
    return None


def main(argv: list[str]) -> int:
    load_dotenv()
    args = parse_args(argv)
    setup_logging(args.log_level)

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("Missing ANTHROPIC_API_KEY (set it in env or .env).", file=sys.stderr)
        return 2

    # Collect extra API keys for load balancing (ANTHROPIC_API_KEY_2, _3, …)
    extra_keys: list[str] = []
    for i in range(2, 20):
        k = os.getenv(f"ANTHROPIC_API_KEY_{i}", "").strip()
        if k:
            extra_keys.append(k)

    client = AnthropicClient(
        api_key=api_key,
        model=args.model,
        extra_api_keys=extra_keys,
        base_url=args.base_url,
        anthropic_version=args.anthropic_version,
    )

    ks = KillSwitch()
    voice = _build_voice(args)

    # Determine overlay type
    use_overlay = not args.no_overlay
    use_basic = args.basic_overlay

    goal = args.goal.strip() if args.goal else ""

    if use_overlay and not use_basic:
        # Glass overlay (default)
        ov = GlassOverlay(voice=voice, initial_goal=goal)
        ov.start()
        ov.set_stop_callback(lambda: ks._triggered.set())

        if not goal:
            # Interactive mode: wait for user to type/speak a goal
            print("Glass overlay opened. Enter a goal or use the mic button.")
            print("Press Ctrl+Alt+Space to toggle overlay visibility.")
            goal = ov.wait_for_goal()
            print(f"Goal received: {goal}")
    elif use_overlay and use_basic:
        from aik.overlay import Overlay
        ov = Overlay()
    else:
        ov = None

    if not goal:
        print("No goal provided. Use --goal or the glass overlay prompt.", file=sys.stderr)
        return 1

    cfg = AgentConfig(
        goal=goal,
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
        history_path=args.history_path,
        history_log_path=args.history_log_path,
        show_border=not args.no_border,
    )

    agent = KeyboardVisionAgent(cfg, anthropic=client, kill_switch=ks, overlay=ov)
    agent.run()

    # Signal overlay that agent is done
    if hasattr(ov, "mark_complete"):
        ov.mark_complete()

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
