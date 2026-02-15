# AIK (AI Keyboard) - Vision-Based Keyboard Automation (User-Mode)

This repo contains a hackathon-friendly, Windows-only agent that:

- Captures the screen (screenshot).
- Reads the active window title/process.
- Sends the screenshot + context + your goal to Claude (Vision).
- Receives a strict JSON action plan (keyboard-only).
- Executes those keystrokes using **user-mode** Windows input injection (`SendInput`).

## Important limitation (by design)

This implementation **does not** include kernel-level keystroke injection or any mechanism to bypass Windows security
boundaries (UIPI/UAC secure desktop/login screen). Those capabilities are sensitive and are intentionally out of scope.

## Requirements

- Windows 10/11
- Python 3.11+
- An Anthropic API key with vision access

## Setup

1. Create a venv and install deps:

```powershell
python -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
```

2. Configure env vars (copy `.env.example` to `.env` and fill it, or set env vars directly):

- `ANTHROPIC_API_KEY`
- Optional: `ANTHROPIC_MODEL` (defaults to a reasonable value)

## Run

Dry-run (prints actions without typing):

```powershell
python main.py --goal "Open Notepad and type Hello World" --dry-run
```

Live typing:

```powershell
python main.py --goal "In the currently focused app, type: Hello World and press Enter"
```

Interactive terminal mode (re-enter goals without retyping full command):

```powershell
python tools/interactive_run.py
```

Starts in **live typing mode** by default. To start safely in dry-run mode:

```powershell
python tools/interactive_run.py --dry-run-start
```

Interactive commands:

- `/dry` -> dry-run mode (safe, prints plan only)
- `/live` -> live typing mode
- `/status` -> show current mode
- `/quit` -> exit

Kill switch:

- Press `Ctrl+Alt+Backspace` at any time to stop the agent immediately.

## Action schema (what Claude must return)

The model must output JSON like:

```json
{
  "actions": [
    {"type": "type_text", "text": "Hello"},
    {"type": "key_press", "key": "enter"},
    {"type": "hotkey", "keys": ["ctrl", "s"]},
    {"type": "wait_ms", "ms": 500},
    {"type": "stop", "reason": "Done"}
  ]
}
```

## Driver stub

See `driver_stub/` for a **non-injecting** KMDF IOCTL driver skeleton you can use to validate user-mode -> kernel
communication (ping/echo + debug logging). It is intentionally not a keyboard filter and does not inject input.

To build the driver stub, you need Visual Studio with C++ tools + Windows SDK + WDK component installed.

