# AIK Kernel Driver – Troubleshooting Guide

## Prerequisites Checklist

Before installing or using the kernel driver, ensure you have:

- [ ] **Visual Studio 2022** (Community or higher) with C++ Desktop workload
- [ ] **Windows Driver Kit (WDK)** matching your VS version  
  Download: https://learn.microsoft.com/en-us/windows-hardware/drivers/download-the-wdk
- [ ] **Administrator privileges** on the target machine
- [ ] **Test signing enabled** (for unsigned/test-signed drivers)
- [ ] **64-bit Windows 10/11** (the driver is built for x64)

---

## Step-by-Step Installation

### 1. Build the Driver

```
1. Open Visual Studio
2. File > Open > Project/Solution > driver_stub\AikKmdfIoctl\AikKmdfIoctl.vcxproj
   (If no .vcxproj exists, create a new KMDF project and replace source files)
3. Set Platform to x64, Configuration to Debug or Release
4. Build > Build Solution
5. Find the .sys file in x64\Debug\ or x64\Release\
```

### 2. Enable Test Signing

**IMPORTANT:** You must do this once, then reboot.

```powershell
# Run as Administrator
bcdedit /set testsigning on
# REBOOT YOUR MACHINE
shutdown /r /t 0
```

After reboot, you'll see a "Test Mode" watermark on the desktop. This is normal.

### 3. Install the Driver

```powershell
# Run as Administrator
.\install_driver.ps1 -SysPath ".\driver_stub\x64\Debug\AikKmdfIoctl.sys"
```

Or manually:
```powershell
# Copy driver to System32
copy AikKmdfIoctl.sys C:\Windows\System32\drivers\aik_kmdf.sys

# Create the service
sc.exe create AikKmdfDriver type= kernel start= demand binPath= "C:\Windows\System32\drivers\aik_kmdf.sys" DisplayName= "AI Keyboard KMDF Driver"

# Start it
sc.exe start AikKmdfDriver
```

### 4. Verify It Works

```powershell
# Check service status
.\check_driver.ps1

# Or manually:
sc.exe query AikKmdfDriver

# Test from Python
python tools/driver_ping.py
```

### 5. Run the Agent in Kernel Mode

```bash
python main.py --goal "your task here" --kernel --verbose
```

---

## Common Errors and Solutions

### `[WinError 2] The system cannot find the file specified`

**Cause:** The driver device object (`\\.\AikKmdfIoctl`) doesn't exist.

**Solutions:**
1. **Driver not installed:** Run `.\install_driver.ps1`
2. **Driver not started:** Run `sc.exe start AikKmdfDriver`  
3. **Driver crashed on load:** Check Event Viewer (see below)
4. **Wrong service name:** Check with `sc.exe query AikKmdfDriver` or `sc.exe query AikKmdfIoctl`

### `[WinError 5] Access is denied`

**Cause:** Python is not running as Administrator.

**Solution:** Right-click your terminal → "Run as administrator", then retry.

Or use the `--elevate` flag:
```bash
python main.py --goal "..." --kernel --elevate
```

### `[WinError 1275] This driver has been blocked from loading`

**Cause:** Test signing is not enabled, or the driver is blocked by Secure Boot.

**Solutions:**
1. Enable test signing: `bcdedit /set testsigning on` → reboot
2. If Secure Boot is on, you may need to disable it in BIOS/UEFI
3. Sign the driver with a proper code signing certificate (for production)

### `sc.exe create` says "Access is denied"

**Cause:** Not running as Administrator.

**Solution:** Right-click PowerShell → "Run as administrator"

### `sc.exe start` fails with error 1275

**Cause:** Same as WinError 1275 above. Test signing not enabled.

### `sc.exe start` fails with error 2

**Cause:** The .sys file doesn't exist at the path specified in binPath.

**Solution:** Verify the file exists:
```powershell
Test-Path C:\Windows\System32\drivers\aik_kmdf.sys
```

### `sc.exe start` fails with error 1058

**Cause:** Service is disabled.

**Solution:**
```powershell
sc.exe config AikKmdfDriver start= demand
sc.exe start AikKmdfDriver
```

### Driver loads but PING fails

**Cause:** IOCTL codes might not match between driver and Python.

**Solution:** Verify that `Public.h` IOCTL definitions match the Python constants in `input_injector_kernel.py` and `driver_bridge.py`.

### Agent falls back to user-mode even with `--kernel`

**Cause:** Driver connection failed, but `fallback=True` (default).

**Solution:** Check the log output for the specific error. The Python code now provides detailed error messages explaining exactly what went wrong.

---

## How to Check if the Driver is Loaded

### Method 1: Service Control Manager
```powershell
sc.exe query AikKmdfDriver
# Look for STATE: RUNNING
```

### Method 2: Device Manager
1. Open Device Manager (Win+X → Device Manager)
2. View → Show hidden devices
3. Look for "AI Keyboard KMDF Driver" or "AikKmdfIoctl"

### Method 3: DebugView (for driver debug output)
1. Download [DebugView](https://learn.microsoft.com/en-us/sysinternals/downloads/debugview) from Sysinternals
2. Run DebugView as Administrator
3. Enable: Capture → Capture Kernel
4. Start the driver — you should see "AikKmdf: DriverEntry" messages
5. Run `python tools/driver_ping.py` — you should see PING/PONG messages

### Method 4: Event Viewer (for driver errors)
1. Open Event Viewer (Win+R → `eventvwr.msc`)
2. Navigate to: Windows Logs → System
3. Look for events from source "Service Control Manager" with your service name
4. Error events will explain why the driver failed to load

### Method 5: PowerShell Check Script
```powershell
.\check_driver.ps1
```

---

## Debugging Driver Issues

### Enable Verbose Driver Logging

The driver uses `KdPrintEx` for debug output. To capture it:

1. **DebugView** (easiest):
   - Run as Admin
   - Capture → Capture Kernel ✓
   - Watch for `AikKmdf:` prefixed messages

2. **WinDbg** (advanced):
   - Attach WinDbg as a kernel debugger
   - Use `!drvobj AikKmdfIoctl` to inspect the driver object
   - Use `!devobj \Device\AikKmdfIoctl` to inspect the device

### Common Debug Scenarios

**Driver loads but device not created:**
```
Check DebugView for errors in EvtDeviceAdd or AikCreateDevice
The symbolic link creation might be failing
```

**IOCTL returns error:**
```
Check that IOCTL codes match between Public.h and Python
Use DebugView to see which IOCTL code the driver receives
Verify buffer sizes match between Python and driver
```

**Scancodes injected but no effect:**
```
The keyboard class service callback might not be hooked
Check that the driver finds the keyboard device stack
Verify the scancode values are correct (PS/2 Set 1)
```

---

## Uninstalling

To completely remove the driver:

```powershell
# Using the uninstall script
.\uninstall_driver.ps1

# Or manually:
sc.exe stop AikKmdfDriver
sc.exe delete AikKmdfDriver
del C:\Windows\System32\drivers\aik_kmdf.sys

# Optionally disable test signing:
bcdedit /set testsigning off
# Reboot
```

---

## Architecture Overview

```
┌─────────────────────────────────────────────────┐
│  Python Agent (main.py --kernel)                │
│  ┌─────────────────────────────────────────┐    │
│  │ KernelInputInjector                     │    │
│  │  • Checks service status               │    │
│  │  • Auto-starts if stopped              │    │
│  │  • Opens \\.\AikKmdfIoctl handle       │    │
│  │  • Sends IOCTL_AIK_INJECT_KEY          │    │
│  │  • Falls back to SendInput on failure  │    │
│  └──────────────┬──────────────────────────┘    │
│                 │ DeviceIoControl                │
├─────────────────┼───────────────────────────────┤
│  KERNEL MODE    │                               │
│  ┌──────────────▼──────────────────────────┐    │
│  │ AikKmdfIoctl.sys (KMDF Driver)         │    │
│  │  • Creates \\Device\\AikKmdfIoctl      │    │
│  │  • Handles PING, ECHO, INJECT_KEY      │    │
│  │  • Injects via keyboard class callback │    │
│  └──────────────┬──────────────────────────┘    │
│                 │                               │
│  ┌──────────────▼──────────────────────────┐    │
│  │ Keyboard Class Service (kbdclass.sys)   │    │
│  │  • Receives scancodes as if from HW    │    │
│  │  • Bypasses UIPI / UAC / Secure Desk   │    │
│  └─────────────────────────────────────────┘    │
└─────────────────────────────────────────────────┘
```
