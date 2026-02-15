# KMDF Driver Stub (IOCTL + Debug Logging Only)

This folder contains a minimal **KMDF** driver skeleton that:

- Creates a named device accessible from user-mode (`\\\\.\\AikKmdfIoctl`)
- Implements a small IOCTL interface (`PING`, `ECHO`)
- Logs to the kernel debugger (e.g., DebugView / WinDbg)

It is meant to help you validate your user-mode control plane (ctypes `CreateFileW` + `DeviceIoControl`) against a real
kernel driver, without implementing any input injection.

## Build notes

- Create a new **KMDF Driver** project in Visual Studio (WDK installed).
- Replace the generated source files with the files in `AikKmdfIoctl/`.
- Ensure the project is configured for the target OS version and matches your machine architecture (x64).

## Files

- `AikKmdfIoctl/Public.h`: IOCTL codes shared with user-mode.
- `AikKmdfIoctl/Driver.c`: `DriverEntry` + `EvtDeviceAdd`.
- `AikKmdfIoctl/Device.c`: Device creation and symbolic link.
- `AikKmdfIoctl/Queue.c`: IOCTL handlers.

