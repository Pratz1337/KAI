from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

import speech_recognition as sr
from dotenv import load_dotenv
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
        "--sarvam-mode",
        choices=["transcribe", "translate", "verbatim", "translit", "codemix"],
        default="transcribe",
        help="Sarvam output mode (as per quickstart docs)",
    )
    parser.add_argument(
        "--language-code",
        default="en-IN",
        help="Legacy fallback language code (used only if SDK rejects mode)",
    )
    parser.add_argument(
        "--run-command",
        action="store_true",
        help="Execute recognized text as a terminal command (with spoken phrase mapping)",
    )
    parser.add_argument(
        "--fallback-type",
        action="store_true",
        default=True,
        help="If text is not a valid command, type it in terminal instead of failing",
    )
    return parser.parse_args(argv)


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

    if args.provider == "google":
        text = recognizer.recognize_google(audio)
        return text.strip() or None

    if sarvam_client is None:
        raise RuntimeError("Sarvam client is not initialized.")

    wav_data = audio.get_wav_data()
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
            temp_file.write(wav_data)
            tmp_path = Path(temp_file.name)

        with tmp_path.open("rb") as file_handle:
            try:
                response = sarvam_client.speech_to_text.transcribe(
                    file=file_handle,
                    model=args.sarvam_model,
                    mode=args.sarvam_mode,
                )
            except TypeError:
                file_handle.seek(0)
                fallback_model = "saarika:v2.5" if args.sarvam_model == "saaras:v3" else args.sarvam_model
                response = sarvam_client.speech_to_text.transcribe(
                    file=file_handle,
                    model=fallback_model,
                    language_code=args.language_code,
                )

        text = _extract_sarvam_text(response)
        return text or None
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


def spoken_phrase_to_command(text: str) -> str | None:
    normalized = _normalize_spoken_text(text)
    lower = normalized.lower()

    direct_map = {
        "show files": "dir",
        "list files": "dir",
        "list file": "dir",
        "clear terminal": "cls",
        "clear": "cls",
        "who am i": "whoami",
        "python version": "python --version",
        "go back": "cd ..",
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
        if not target:
            return None

        target_lower = target.lower()
        app_map = {
            "spotify": "start spotify:",
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

        query = urllib.parse.quote_plus(target)
        return f'start "" "https://www.google.com/search?q={query}"'

    first_token = lower.split(" ", 1)[0]
    known_cmds = {
        "dir", "cd", "cls", "echo", "python", "pip", "git", "whoami",
        "ipconfig", "hostname", "start", "notepad", "code", "type",
    }
    if first_token in known_cmds:
        return normalized

    return None


def main(argv: list[str]) -> int:
    load_dotenv()
    args = parse_args(argv)
    recognizer = sr.Recognizer()
    keyboard = Controller()
    sarvam_client = None

    if args.provider == "sarvam":
        api_key = os.getenv("SARVAM_API_KEY", "").strip()
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

    print("Ready. Keep this terminal focused while typing is injected.")
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
                command = spoken_phrase_to_command(text)
                if command:
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