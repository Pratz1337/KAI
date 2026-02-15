# AIK context bundle

## Short answer: is kernel C code used right now?
No. The running agent is **Python user-mode only** (screen capture + Anthropic + Windows `SendInput`).

The `driver_stub/` C code is a **separate KMDF IOCTL stub** and is **not used** unless you manually build a `.sys`, enable test signing, load/start the driver, and then call it from a dedicated tool (e.g. `tools/driver_ping.py`). The agent loop does not talk to the driver.

## Current project state (Feb 15, 2026)
- ✅ Keyboard-only automation is the current execution mode (mouse/cursor actions intentionally removed to stabilize behavior).
- ✅ Vision + context loop works: screenshot + active window title + model JSON plan.
- ✅ Interactive terminal runner works: `tools/interactive_run.py` (runs `main.py` repeatedly).
- ✅ Rate limit handling: Anthropic client retries/backoff; agent loop also waits and continues on HTTP 429.
- ✅ Safety: kill switch via `Ctrl+Alt+Backspace`.
- ✅ Optional overlay: small always-on-top status window.
- ⚠️ Web-app tasks (Amazon/Gmail/WhatsApp Web) are inherently less reliable with keyboard-only constraints; success depends on the site/app supporting strong keyboard navigation.

## Files included in this bundle (what they do)

### Entry points
- `main.py`
  - CLI entrypoint.
  - Loads `.env`, parses args, builds `AgentConfig`, starts `KeyboardVisionAgent`.

- `tools/interactive_run.py`
  - Interactive loop (Goal> prompt).
  - Spawns `main.py` with consistent flags (model, overlay, memory, etc.).

### Core agent
- `aik/agent.py`
  - The main loop: capture screenshot, read active window, call Anthropic, parse plan, execute actions.
  - Handles:
    - repetition/stuck detection,
    - ask-user prompts,
    - stop verification (prevents hallucinated "done"),
    - 429 handling (sleep and continue).

- `aik/prompt.py`
  - `SYSTEM_PROMPT` + `PromptContext` + `build_user_prompt()`.
  - Defines the JSON schema the model must output.

- `aik/actions.py`
  - Parses and validates the model JSON.
  - **Currently allows only keyboard actions**: `type_text`, `key_press`, `hotkey`, `wait_ms`, `ask_user`, `stop`.

### OS integration
- `aik/input_injector.py`
  - Windows user-mode keyboard injection using `SendInput`.

- `aik/capture.py`
  - Screen capture using `mss` and optional downscaling with Pillow.

- `aik/window_context.py`
  - Reads foreground window title + process path via `pywin32`.

- `aik/kill_switch.py`
  - Global kill switch listener using `pynput`.

- `aik/app_focus.py`
  - Best-effort: if Chrome/Spotify/WhatsApp/etc is already open, bring it to the foreground.

### Cloud/model
- `aik/anthropic_client.py`
  - Minimal Messages API client using `httpx`.
  - Includes retry/backoff for transient failures / 429.

### UX / observability
- `aik/overlay.py`
  - Tiny always-on-top overlay showing goal, mode, steps, progress.

- `aik/logging_setup.py`
  - Logging configuration.

### Local persistence
- `aik/memory.py`
  - Stores small local history/events in `.aik_memory.json`.

### Environment / deps
- `requirements.txt`
  - Python dependencies.

- `.env.example`
  - Template env vars.

- `.env`
  - Local secrets/config (API key, model). **Do not share publicly.**

## What is NOT included (by design)
- `driver_stub/` and `.c` sources: not used by the agent runtime.
- Kernel-mode injection/bypass logic: out of scope for this agent.
