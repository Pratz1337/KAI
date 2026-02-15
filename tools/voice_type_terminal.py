from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

import httpx
import speech_recognition as sr
from dotenv import find_dotenv, load_dotenv
from pynput.keyboard import Controller


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Listen to microphone input and type recognized text into the focused terminal"
    )
    parser.add_argument(
        "--continuous",
        action="store_true",
        help="Keep listening and typing until interrupted",
    )
    parser.add_argument(
        "--phrase-time-limit",
        type=float,
        default=7.0,
        help="Max seconds per captured phrase",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=8.0,
        help="Seconds to wait for speech before retry",
    )
    parser.add_argument(
        "--enter",
        action="store_true",
        help="Press Enter after typing recognized text",
    )
    parser.add_argument(
        "--countdown",
        type=float,
        default=1.0,
        help="Seconds to wait before typing (lets you re-focus terminal)",
    )
    parser.add_argument(
        "--language",
        default="en-IN",
        help="Primary language code (e.g. en-IN, hi-IN, ta-IN)",
    )
    parser.add_argument(
        "--languages",
        default="",
        help="Comma-separated language codes for multilingual recognition priority order",
    )
    parser.add_argument(
        "--provider",
        choices=["sarvam", "google"],
        default="sarvam",
        help="Speech-to-text provider",
    )
    parser.add_argument(
        "--sarvam-model",
        default="saaras:v3",
        help="Sarvam speech model id",
    )
    parser.add_argument(
        "--sarvam-api-key",
        default="",
        help="Sarvam API key (overrides SARVAM_API_KEY env/.env). Prefer setting env var instead of passing on CLI.",
    )
    parser.add_argument(
        "--sarvam-mode",
        choices=["transcribe", "translate", "verbatim", "translit", "codemix"],
        default="transcribe",
        help="Sarvam output mode (as per quickstart docs)",
    )
    parser.add_argument(
        "--language-code",
        default="en-IN",
        help="Legacy fallback language code (used only if SDK rejects mode and no language list is set)",
    )
    parser.add_argument(
        "--run-command",
        action="store_true",
        help="Execute recognized text as a terminal command (with spoken phrase mapping)",
    )
    parser.add_argument(
        "--delegate-to-agent",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Delegate complex multi-step spoken tasks to main.py --goal",
    )
    parser.add_argument(
        "--ai-command-map",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use LLM fallback to convert natural speech into safe terminal commands",
    )
    parser.add_argument(
        "--ai-model",
        default=os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
        help="Anthropic model used for natural-language command mapping",
    )
    parser.add_argument(
        "--fallback-type",
        action="store_true",
        default=True,
        help="If text is not a valid command, type it in terminal instead of failing",
    )
    parser.add_argument(
        "--delegate-unknown",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="If no command mapping matches, delegate imperative speech to main.py --goal instead of typing it",
    )
    return parser.parse_args(argv)


def resolve_language_codes(args: argparse.Namespace) -> list[str]:
    if args.languages.strip():
        codes = [part.strip() for part in args.languages.split(",") if part.strip()]
    else:
        codes = [args.language.strip()] if args.language.strip() else []

    if not codes and args.language_code.strip():
        codes = [args.language_code.strip()]

    deduped: list[str] = []
    for code in codes:
        if code and code not in deduped:
            deduped.append(code)
    return deduped or ["en-IN"]


def _extract_sarvam_text(response: object) -> str:
    if response is None:
        return ""
    if isinstance(response, str):
        return response.strip()
    if isinstance(response, dict):
        for key in ("transcript", "text", "output_text"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    for key in ("transcript", "text", "output_text"):
        value = getattr(response, key, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _sarvam_transcribe_with_kwargs(sarvam_client: object, file_handle: object, **kwargs: object) -> object:
    return sarvam_client.speech_to_text.transcribe(file=file_handle, **kwargs)


def _try_sarvam_transcribe(
    sarvam_client: object,
    file_handle: object,
    model: str,
    mode: str,
    language_code: str,
) -> object:
    attempts = (
        {"model": model, "mode": mode, "language_code": language_code},
        {"model": model, "mode": mode},
        {"model": model, "language_code": language_code},
    )
    last_error: Exception | None = None
    for kwargs in attempts:
        try:
            file_handle.seek(0)
            return _sarvam_transcribe_with_kwargs(sarvam_client, file_handle, **kwargs)
        except TypeError as exc:
            last_error = exc

    fallback_model = "saarika:v2.5" if model == "saaras:v3" else model
    fallback_attempts = (
        {"model": fallback_model, "language_code": language_code},
        {"model": fallback_model, "mode": mode},
    )
    for kwargs in fallback_attempts:
        try:
            file_handle.seek(0)
            return _sarvam_transcribe_with_kwargs(sarvam_client, file_handle, **kwargs)
        except TypeError as exc:
            last_error = exc

    if last_error is not None:
        raise last_error
    raise RuntimeError("No valid Sarvam transcription method found.")


def recognize_once(
    recognizer: sr.Recognizer,
    mic: sr.Microphone,
    args: argparse.Namespace,
    sarvam_client: object | None,
) -> str | None:
    with mic as source:
        print("Listening... speak now")
        audio = recognizer.listen(
            source,
            timeout=args.timeout,
            phrase_time_limit=args.phrase_time_limit,
        )

    language_codes = resolve_language_codes(args)

    if args.provider == "google":
        for code in language_codes:
            try:
                text = recognizer.recognize_google(audio, language=code)
                text = text.strip()
                if text:
                    return text
            except sr.UnknownValueError:
                continue
        raise sr.UnknownValueError()

    if sarvam_client is None:
        raise RuntimeError("Sarvam client is not initialized.")

    wav_data = audio.get_wav_data()
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
            temp_file.write(wav_data)
            tmp_path = Path(temp_file.name)

        with tmp_path.open("rb") as file_handle:
            for code in language_codes:
                response = _try_sarvam_transcribe(
                    sarvam_client=sarvam_client,
                    file_handle=file_handle,
                    model=args.sarvam_model,
                    mode=args.sarvam_mode,
                    language_code=code,
                )
                text = _extract_sarvam_text(response)
                if text:
                    return text
        return None
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def type_to_terminal(text: str, keyboard: Controller, with_enter: bool) -> None:
    keyboard.type(text)
    if with_enter:
        keyboard.press("\n")
        keyboard.release("\n")


def run_terminal_command(command: str) -> int:
    completed = subprocess.run(command, shell=True)
    return completed.returncode


def is_safe_command(command: str) -> bool:
    stripped = command.strip().lower()
    if not stripped:
        return False

    dangerous_tokens = (
        "del ",
        " erase ",
        "rmdir",
        "rd ",
        "format",
        "shutdown",
        "restart-computer",
        "stop-computer",
        "remove-item",
        "reg delete",
        "diskpart",
        "bcdedit",
    )
    if any(token in f" {stripped} " for token in dangerous_tokens):
        return False

    if any(op in command for op in ("&&", "||", ";")):
        return False

    return True


def _normalize_spoken_text(text: str) -> str:
    text = text.strip()
    text = re.sub(r"\s+", " ", text)
    return text.rstrip(".?!").strip()


def _extract_song_query(lower_text: str) -> str | None:
    match = re.search(r"(?:play(?: the)?(?: song)?\s+)(.+)$", lower_text)
    if not match:
        return None
    query = match.group(1).strip(" .?!")
    fillers = {
        "right now",
        "now",
        "please",
    }
    for filler in fillers:
        query = query.replace(filler, " ")
    query = re.sub(r"\s+", " ", query).strip()
    return query or None


def _is_complex_multistep_intent(lower_text: str) -> bool:
    markers = (
        " and then ",
        " then ",
        " after that ",
        " save ",
        " email ",
        " send ",
        "type in",
        "write in",
        "fill ",
        "aur phir",
        "phir",
    )
    wrapped = f" {lower_text} "
    return any(marker in wrapped for marker in markers)


def _build_agent_delegate_command(goal_text: str) -> str | None:
    main_path = Path(__file__).resolve().parents[1] / "main.py"
    if not main_path.exists():
        return None
    escaped_goal = goal_text.replace('"', '\\"')
    return f'python "{main_path}" --goal "{escaped_goal}"'


def spoken_phrase_to_command(text: str, delegate_to_agent: bool = True) -> str | None:
    normalized = _normalize_spoken_text(text)
    lower = normalized.lower()

    if delegate_to_agent and _is_complex_multistep_intent(lower):
        delegated = _build_agent_delegate_command(normalized)
        if delegated:
            return delegated

    direct_map = {
        "show files": "dir",
        "list files": "dir",
        "list file": "dir",
        "files dikhao": "dir",
        "folder dikhao": "dir",
        "clear terminal": "cls",
        "clear": "cls",
        "terminal saaf karo": "cls",
        "who am i": "whoami",
        "mai kaun hu": "whoami",
        "python version": "python --version",
        "go back": "cd ..",
        "piche jao": "cd ..",
        "peeche jao": "cd ..",
    }
    if lower in direct_map:
        return direct_map[lower]

    if "open spotify" in lower or "start spotify" in lower:
        song_query = _extract_song_query(lower)
        if song_query:
            encoded_query = urllib.parse.quote_plus(song_query)
            return f'start "" "https://open.spotify.com/search/{encoded_query}"'
        return "start spotify:"

    open_match = re.search(r"(?:^|\b)(?:open|start)\s+(.+)$", normalized, flags=re.IGNORECASE)
    if open_match:
        target = open_match.group(1).strip()
        target = re.split(r"\b(?:and then|then|after that|aur phir|phir)\b|,", target, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        if not target:
            return None

        target_lower = target.lower()
        app_map = {
            "spotify": "start spotify:",
            "excel": "start excel",
            "excel file": "start excel",
            "notepad": "start notepad",
            "chrome": "start chrome",
            "calculator": "start calc",
            "cmd": "start cmd",
            "powershell": "start powershell",
            "explorer": "start explorer",
        }
        if target_lower in app_map:
            return app_map[target_lower]

        if target_lower.startswith("http://") or target_lower.startswith("https://"):
            return f'start "" "{target}"'

        if "." in target_lower and " " not in target_lower:
            return f'start "" "https://{target}"'

        if len(target.split()) <= 3:
            return f'start "" "{target}"'
        return None

    first_token = lower.split(" ", 1)[0]
    known_cmds = {
        "dir", "cd", "cls", "echo", "python", "pip", "git", "whoami",
        "ipconfig", "hostname", "start", "notepad", "code", "type",
    }
    if first_token in known_cmds:
        return normalized

    return None


def ai_spoken_phrase_to_command(text: str, anthropic_api_key: str, model: str) -> str | None:
    if not anthropic_api_key.strip():
        return None

    system_prompt = (
        "You convert spoken Hinglish/Hindi/English intent into ONE safe Windows terminal command. "
        "Return strict JSON only: {\"command\": string, \"execute\": boolean}. "
        "Use execute=false if request is conversational, unclear, or unsafe. "
        "Never output destructive commands. Prefer simple commands like dir, cd .., cls, whoami, ipconfig, "
        "python --version, start <app>, or start URL search."
    )

    user_prompt = (
        f"Spoken text: {text}\n"
        "If intent is app opening, use start command. "
        "If no actionable command exists, set execute=false."
    )

    payload = {
        "model": model,
        "max_tokens": 120,
        "temperature": 0,
        "system": system_prompt,
        "messages": [
            {"role": "user", "content": user_prompt},
        ],
    }
    headers = {
        "x-api-key": anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    with httpx.Client(timeout=20.0) as client:
        response = client.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

    content = data.get("content", [])
    text_parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text_value = item.get("text", "")
            if isinstance(text_value, str):
                text_parts.append(text_value)

    raw = "\n".join(text_parts).strip()
    if not raw:
        return None

    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        return None

    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None

    if not isinstance(obj, dict):
        return None

    execute = bool(obj.get("execute", False))
    command = obj.get("command", "")
    if not execute or not isinstance(command, str):
        return None

    command = command.strip()
    if not is_safe_command(command):
        return None

    return command


def main(argv: list[str]) -> int:
    load_dotenv(find_dotenv(usecwd=True))
    args = parse_args(argv)
    recognizer = sr.Recognizer()
    keyboard = Controller()
    sarvam_client = None
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()

    if args.provider == "sarvam":
        api_key = (args.sarvam_api_key or "").strip() or os.getenv("SARVAM_API_KEY", "").strip()
        if not api_key:
            print(
                "Missing SARVAM_API_KEY. Set it in environment or .env, or run with --provider google.",
                file=sys.stderr,
            )
            return 2
        try:
            from sarvamai import SarvamAI

            sarvam_client = SarvamAI(api_subscription_key=api_key)
        except Exception as exc:
            print(f"Failed to initialize Sarvam SDK: {exc}", file=sys.stderr)
            return 2

    try:
        mic = sr.Microphone()
    except Exception as exc:
        print(
            "Microphone init failed. Install and verify audio input device."
            f" Details: {exc}",
            file=sys.stderr,
        )
        return 2

    with mic as source:
        print("Calibrating ambient noise...")
        recognizer.adjust_for_ambient_noise(source, duration=0.8)

    language_codes = resolve_language_codes(args)
    print("Ready. Keep this terminal focused while typing is injected.")
    print(f"Languages: {', '.join(language_codes)}")
    if args.run_command:
        ai_status = "enabled" if args.ai_command_map and anthropic_api_key else "disabled"
        print(f"AI command mapping: {ai_status}")
    print("Press Ctrl+C to stop.")

    try:
        while True:
            try:
                text = recognize_once(recognizer, mic, args, sarvam_client)
            except sr.WaitTimeoutError:
                print("No speech detected in time window. Retrying...")
                if args.continuous:
                    continue
                return 1
            except sr.UnknownValueError:
                print("Could not understand audio. Try again.")
                if args.continuous:
                    continue
                return 1
            except sr.RequestError as exc:
                print(f"Speech API error: {exc}", file=sys.stderr)
                return 3
            except Exception as exc:
                print(f"Transcription failed: {exc}", file=sys.stderr)
                if args.continuous:
                    continue
                return 3

            if not text:
                print("Recognized empty text.")
                if args.continuous:
                    continue
                return 1

            print(f"Recognized: {text}")
            if args.countdown > 0:
                time.sleep(args.countdown)

            if args.run_command:
                command = spoken_phrase_to_command(text, delegate_to_agent=args.delegate_to_agent)
                if not command and args.ai_command_map and anthropic_api_key:
                    try:
                        command = ai_spoken_phrase_to_command(text, anthropic_api_key, args.ai_model)
                    except Exception as exc:
                        print(f"AI command mapping failed: {exc}")
                if command:
                    if "main.py\" --goal" in command or command.endswith("main.py --goal"):
                        print("Detected complex task; delegating to AI agent goal execution.")
                    print(f"Executing command: {command}")
                    exit_code = run_terminal_command(command)
                    print(f"Command exit code: {exit_code}")
                elif args.fallback_type:
                    print("No command mapping found; typing recognized text in terminal.")
                    type_to_terminal(text, keyboard, args.enter)
                else:
                    print("No command mapping found.")
            else:
                type_to_terminal(text, keyboard, args.enter)

            if not args.continuous:
                return 0
    except KeyboardInterrupt:
        print("\nStopped by user.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))