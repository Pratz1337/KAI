# AIK (AI Keyboard) - Vision-Based Keyboard Automation

A Windows-based AI agent that uses **Claude Vision (Haiku 4.5)** to understand your screen and perform keyboard-only automation tasks. The agent captures screenshots, analyzes them with AI, and executes keyboard actions to accomplish your goals.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    User Space (Python Agent)                 │
├─────────────────────────────────────────────────────────────┤
│  User Goal Input → Agent Controller (main.py)               │
│         ↓                                                    │
│    AI Logic Loop                                             │
│    ├── Window Manager (pywin32) ← Context                   │
│    ├── Vision Module (mss/PIL) ← Capture                    │
│    └── LLM Client (Anthropic) → Action Plan                 │
│         ↓                                                    │
│    Driver Interface (ctypes) → IOCTL (Scancodes)            │
└─────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────┐
│                  Kernel Space (Ring 0)                       │
├─────────────────────────────────────────────────────────────┤
│    Kernel Keyboard Filter Driver (KMDF)                      │
│    └── Inject → Windows Input Stack (kbdclass)              │
│                        ↓                                     │
│    Target Environment: Any App / System Prompts (UAC)       │
└─────────────────────────────────────────────────────────────┘
```

## Features

- **Vision-based AI**: Uses Claude Vision to understand screen content
- **Keyboard-only automation**: Executes type_text, key_press, hotkey actions
- **Kill switch**: Press `Ctrl+Alt+Backspace` to stop immediately
- **User-mode injection**: Works with most applications via SendInput
- **Kernel driver support** (optional): For bypassing UIPI restrictions

## Requirements

- Windows 10/11 (64-bit)
- Python 3.11+
- Anthropic API key with vision access

## Quick Start

### 1. Install dependencies

```powershell
pip install mss pywin32 pynput httpx pillow python-dotenv
```

Or use the requirements file:
```powershell
pip install -r requirements.txt
```

### 2. Configure API key

Edit `.env` file:
```
ANTHROPIC_API_KEY=your-api-key-here
ANTHROPIC_MODEL=claude-haiku-4-5-20251001
```

### 3. Run the agent

**Dry-run** (prints actions without executing):
```powershell
python main.py --goal "Open Notepad and type Hello World" --dry-run
```

**Live mode** (actually types):
```powershell
python main.py --goal "Type 'Hello World' and press Enter"
```

**Elevated mode** (type into admin apps):
```powershell
python main.py --elevate --goal "Type: Hello from elevated context"
```

Note: Elevation still cannot interact with the UAC secure desktop or login screen.

**Interactive terminal mode** (re-enter goals without retyping full command):
```powershell
python tools/interactive_run.py
```

## Command-line Options

| Option | Default | Description |
|--------|---------|-------------|
| `--goal` | (required) | What you want the agent to accomplish |
| `--dry-run` | False | Print actions without injecting keys |
| `--max-steps` | 40 | Maximum planning cycles |
| `--interval` | 0.8 | Seconds between planning cycles |
| `--monitor` | 1 | mss monitor index (1=primary) |
| `--screenshot-max-width` | 1280 | Downscale screenshots for API |
| `--model` | claude-haiku-4-5-20251001 | Anthropic model ID |
| `--log-level` | INFO | Logging verbosity |

## Action Schema

The AI returns JSON with keyboard actions:

```json
{
  "actions": [
    {"type": "type_text", "text": "Hello World"},
    {"type": "key_press", "key": "enter"},
    {"type": "hotkey", "keys": ["ctrl", "s"]},
    {"type": "wait_ms", "ms": 500},
    {"type": "stop", "reason": "Task completed"}
  ]
}
```

### Supported Actions

| Action | Fields | Description |
|--------|--------|-------------|
| `type_text` | `text` | Type a string |
| `key_press` | `key` | Press a single key (enter, tab, f1-f24, a-z, 0-9) |
| `hotkey` | `keys` | Press key combo (["ctrl", "c"]) |
| `wait_ms` | `ms` | Wait milliseconds (0-60000) |
| `stop` | `reason` | Stop the agent |

## Project Structure

```
├── main.py              # Entry point
├── aik/
│   ├── agent.py         # Main agent loop
│   ├── anthropic_client.py  # Claude API client
│   ├── capture.py       # Screen capture (mss)
│   ├── window_context.py    # Active window info (pywin32)
│   ├── input_injector.py    # User-mode key injection
│   ├── driver_bridge.py     # Kernel driver communication
│   ├── actions.py       # Action parsing
│   ├── prompt.py        # System prompts
│   └── kill_switch.py   # Emergency stop
├── driver_stub/         # KMDF driver source
│   └── AikKmdfIoctl/
├── tools/
│   └── driver_ping.py   # Driver test utility
└── requirements.txt
```

## Kernel Driver (Advanced)

The driver stub in `driver_stub/` provides kernel-level scancode injection that can bypass UIPI restrictions (type into UAC prompts, admin terminals, etc.).

### Building the Driver

1. Install [Windows Driver Kit (WDK)](https://docs.microsoft.com/en-us/windows-hardware/drivers/download-the-wdk)
2. Open `driver_stub/AikKmdfIoctl/` in Visual Studio
3. Build for your target (x64 Release)

### Loading the Driver (Test Mode)

```powershell
# Enable test signing (requires reboot)
bcdedit /set testsigning on

# Load driver
sc create AikKmdf type= kernel binPath= "C:\path\to\AikKmdfIoctl.sys"
sc start AikKmdf

# Test connectivity
python tools/driver_ping.py
```

### Driver IOCTLs

| IOCTL | Function |
|-------|----------|
| `IOCTL_AIK_PING` | Returns "PONG" |
| `IOCTL_AIK_ECHO` | Echoes input buffer |
| `IOCTL_AIK_INJECT_SCANCODE` | Inject single scancode |
| `IOCTL_AIK_INJECT_SCANCODES` | Inject scancode batch |

## Safety

- **Kill Switch**: `Ctrl+Alt+Backspace` stops the agent immediately
- **Dry Run**: Test with `--dry-run` before live execution
- **Max Steps**: Agent stops after 40 steps by default
- **No Mouse**: Intentionally keyboard-only to limit scope

## Troubleshooting

**"Missing ANTHROPIC_API_KEY"**
- Set the key in `.env` or environment variable

**Keys don't work in elevated apps**
- Run the Python script as Administrator
- Or use the kernel driver for UIPI bypass

**Driver won't load**
- Enable test signing: `bcdedit /set testsigning on`
- Check DebugView for kernel logs

## License

MIT

