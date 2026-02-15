# KMDF Driver – Kernel-Level Keyboard Injection

This folder contains the **KMDF** driver that provides kernel-level keyboard injection:

- Creates a named device accessible from user-mode (`\\\\.\\AikKmdfIoctl`)
- Implements IOCTLs: `PING`, `ECHO`, and **`INJECT_KEY`** (scancode injection)
- Injects scancodes via the keyboard class-service callback – bypasses UIPI, UAC, secure desktop
- Logs to the kernel debugger (DebugView / WinDbg)

## IOCTLs

| IOCTL | Description |
|---|---|
| `IOCTL_AIK_PING` | Returns "PONG" – driver health check |
| `IOCTL_AIK_ECHO` | Echoes input buffer back |
| `IOCTL_AIK_INJECT_KEY` | Accepts an `AIK_KEY_PACKET` of scancodes and injects them into the input stack |

## Build notes

1. Install **Visual Studio** with the **WDK** (Windows Driver Kit).
2. Create a new **KMDF Driver** project.
3. Replace the generated source files with the files in `AikKmdfIoctl/`.
4. Build for your target (x64 / Debug or Release).

## Deployment (Test Signing)

```powershell
# Enable test signing (run as Admin, then reboot)
bcdedit /set testsigning on

# Install and start the driver
python tools/driver_loader.py install --sys path\to\AikKmdfIoctl.sys
python tools/driver_loader.py start

# Run the agent in kernel mode
python main.py --goal "your task" --kernel
```

## Files

- `AikKmdfIoctl/Public.h`: IOCTL codes + `AIK_KEY_PACKET` / `AIK_SCANCODE` structs shared with user-mode.
- `AikKmdfIoctl/Driver.c`: `DriverEntry` + `EvtDeviceAdd`.
- `AikKmdfIoctl/Device.c`: Device creation and symbolic link.
- `AikKmdfIoctl/Queue.c`: IOCTL handlers including scancode injection.

