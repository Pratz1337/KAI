#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Installs the AIK Kernel-Mode Driver (AikKmdfIoctl).

.DESCRIPTION
    - Checks for Administrator privileges
    - Verifies test signing is enabled
    - Copies .sys to System32\drivers\
    - Creates and starts the kernel driver service
    - Validates the device object is accessible

.PARAMETER SysPath
    Path to the compiled AikKmdfIoctl.sys driver file.
    If not provided, searches common build output locations.

.EXAMPLE
    .\install_driver.ps1 -SysPath ".\driver_stub\x64\Debug\AikKmdfIoctl.sys"
#>

param(
    [Parameter(Mandatory=$false)]
    [string]$SysPath
)

$ErrorActionPreference = "Stop"
$ServiceName   = "AikKmdfDriver"
$DisplayName   = "AI Keyboard KMDF Driver"
$DriverDest    = "$env:SystemRoot\System32\drivers\aik_kmdf.sys"

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

function Write-Banner {
    param([string]$Text)
    $line = "=" * 60
    Write-Host ""
    Write-Host $line -ForegroundColor Cyan
    Write-Host "  $Text" -ForegroundColor Cyan
    Write-Host $line -ForegroundColor Cyan
    Write-Host ""
}

function Write-Step {
    param([string]$Text, [string]$Status = "INFO")
    $color = switch ($Status) {
        "OK"      { "Green" }
        "WARN"    { "Yellow" }
        "ERROR"   { "Red" }
        "INFO"    { "White" }
        default   { "White" }
    }
    $icon = switch ($Status) {
        "OK"      { "[OK]   " }
        "WARN"    { "[WARN] " }
        "ERROR"   { "[FAIL] " }
        "INFO"    { "[INFO] " }
        default   { "       " }
    }
    Write-Host "$icon $Text" -ForegroundColor $color
}

# ─────────────────────────────────────────────────────────────
# 1. Verify Administrator
# ─────────────────────────────────────────────────────────────

Write-Banner "AIK Kernel Driver Installer"

$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Step "This script MUST be run as Administrator." "ERROR"
    Write-Step "Right-click PowerShell -> 'Run as administrator', then retry." "INFO"
    exit 1
}
Write-Step "Running as Administrator" "OK"


# ─────────────────────────────────────────────────────────────
# 2. Check Test Signing
# ─────────────────────────────────────────────────────────────

Write-Host ""
Write-Step "Checking test signing status..." "INFO"

try {
    $bcdOutput = & bcdedit /enum "{current}" 2>&1 | Out-String
    if ($bcdOutput -match "testsigning\s+Yes") {
        Write-Step "Test signing is ENABLED" "OK"
    } else {
        Write-Step "Test signing is NOT enabled (or could not be detected)." "WARN"
        Write-Host ""
        Write-Host "  Unsigned/test-signed drivers require test signing mode." -ForegroundColor Yellow
        Write-Host "  To enable it, run this in an admin PowerShell and then REBOOT:" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "    bcdedit /set testsigning on" -ForegroundColor White
        Write-Host ""
        $yn = Read-Host "  Continue anyway? (y/N)"
        if ($yn -notmatch "^[Yy]") {
            Write-Step "Aborted. Enable test signing first, then retry." "ERROR"
            exit 2
        }
    }
} catch {
    Write-Step "Could not query bcdedit: $_" "WARN"
}


# ─────────────────────────────────────────────────────────────
# 3. Locate the .sys file
# ─────────────────────────────────────────────────────────────

Write-Host ""
Write-Step "Locating driver binary..." "INFO"

if (-not $SysPath) {
    # Common build output paths to search
    $searchPaths = @(
        ".\driver_stub\x64\Debug\AikKmdfIoctl\AikKmdfIoctl.sys",
        ".\driver_stub\x64\Release\AikKmdfIoctl\AikKmdfIoctl.sys",
        ".\driver_stub\x64\Debug\AikKmdfIoctl.sys",
        ".\driver_stub\x64\Release\AikKmdfIoctl.sys",
        ".\driver_stub\ARM64\Debug\AikKmdfIoctl.sys",
        ".\driver_stub\ARM64\Release\AikKmdfIoctl.sys",
        ".\AikKmdfIoctl.sys",
        ".\aik_kmdf.sys"
    )
    foreach ($p in $searchPaths) {
        if (Test-Path $p) {
            $SysPath = $p
            break
        }
    }
}

if (-not $SysPath -or -not (Test-Path $SysPath)) {
    Write-Step "Driver .sys file not found!" "ERROR"
    Write-Host ""
    Write-Host "  Please specify the path to your compiled driver:" -ForegroundColor Yellow
    Write-Host "    .\install_driver.ps1 -SysPath 'path\to\AikKmdfIoctl.sys'" -ForegroundColor White
    Write-Host ""
    Write-Host "  Build the driver first:" -ForegroundColor Yellow
    Write-Host "    1. Open driver_stub\AikKmdfIoctl in Visual Studio" -ForegroundColor White
    Write-Host "    2. Build -> Build Solution (x64/Debug or Release)" -ForegroundColor White
    Write-Host "    3. The .sys file will be in x64\Debug\ or x64\Release\" -ForegroundColor White
    exit 3
}

$SysPath = Resolve-Path $SysPath
Write-Step "Found driver: $SysPath" "OK"


# ─────────────────────────────────────────────────────────────
# 4. Stop & remove existing service (if any)
# ─────────────────────────────────────────────────────────────

Write-Host ""
Write-Step "Checking for existing driver service..." "INFO"

$existingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existingService) {
    Write-Step "Service '$ServiceName' already exists (Status: $($existingService.Status))" "WARN"

    if ($existingService.Status -eq "Running") {
        Write-Step "Stopping existing service..." "INFO"
        & sc.exe stop $ServiceName 2>&1 | Out-Null
        Start-Sleep -Seconds 2
    }

    Write-Step "Removing existing service..." "INFO"
    & sc.exe delete $ServiceName 2>&1 | Out-Null
    Start-Sleep -Seconds 1
    Write-Step "Old service removed" "OK"
}


# ─────────────────────────────────────────────────────────────
# 5. Copy .sys to drivers folder
# ─────────────────────────────────────────────────────────────

Write-Host ""
Write-Step "Copying driver to $DriverDest ..." "INFO"

try {
    Copy-Item -Path $SysPath -Destination $DriverDest -Force
    Write-Step "Driver binary copied" "OK"
} catch {
    Write-Step "Failed to copy driver: $_" "ERROR"
    exit 4
}


# ─────────────────────────────────────────────────────────────
# 6. Create the kernel driver service
# ─────────────────────────────────────────────────────────────

Write-Host ""
Write-Step "Creating driver service..." "INFO"

$scCreate = & sc.exe create $ServiceName `
    type= kernel `
    start= demand `
    binPath= "$env:SystemRoot\System32\drivers\aik_kmdf.sys" `
    DisplayName= "$DisplayName" 2>&1 | Out-String

if ($LASTEXITCODE -eq 0 -or $scCreate -match "already exists") {
    Write-Step "Service '$ServiceName' created" "OK"
} else {
    Write-Step "sc.exe create failed: $scCreate" "ERROR"
    exit 5
}


# ─────────────────────────────────────────────────────────────
# 7. Start the driver service
# ─────────────────────────────────────────────────────────────

Write-Host ""
Write-Step "Starting driver service..." "INFO"

$scStart = & sc.exe start $ServiceName 2>&1 | Out-String
Start-Sleep -Seconds 2

if ($LASTEXITCODE -eq 0 -or $scStart -match "RUNNING") {
    Write-Step "Service '$ServiceName' is RUNNING" "OK"
} else {
    Write-Step "sc.exe start may have failed:" "WARN"
    Write-Host $scStart -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Common reasons:" -ForegroundColor Yellow
    Write-Host "    - Test signing not enabled (reboot after bcdedit /set testsigning on)" -ForegroundColor White
    Write-Host "    - Driver binary mismatch (wrong architecture, corrupted .sys)" -ForegroundColor White
    Write-Host "    - Missing dependency (WDF coinstaller not present)" -ForegroundColor White
    Write-Host ""
    Write-Host "  Check Event Viewer -> Windows Logs -> System for details." -ForegroundColor Yellow
}


# ─────────────────────────────────────────────────────────────
# 8. Verify the device object is accessible
# ─────────────────────────────────────────────────────────────

Write-Host ""
Write-Step "Verifying device object \\.\AikKmdfIoctl ..." "INFO"

$devicePath = "\\.\AikKmdfIoctl"

# Try querying the service to confirm it's running
$queryResult = & sc.exe query $ServiceName 2>&1 | Out-String
if ($queryResult -match "RUNNING") {
    Write-Step "Service is confirmed RUNNING" "OK"

    # Try opening the device from Python (quick check)
    $pyCheck = @"
import ctypes
from ctypes import wintypes
k32 = ctypes.WinDLL('kernel32', use_last_error=True)
h = k32.CreateFileW(r'\\.\AikKmdfIoctl', 0x80000000|0x40000000, 3, None, 3, 0x80, None)
if h == wintypes.HANDLE(-1).value:
    err = ctypes.get_last_error()
    print(f'FAIL:error={err}')
else:
    print('SUCCESS')
    k32.CloseHandle(h)
"@
    try {
        $result = & python -c $pyCheck 2>&1 | Out-String
        if ($result -match "SUCCESS") {
            Write-Step "Device handle opened successfully from Python!" "OK"
        } else {
            Write-Step "Device handle not accessible: $result" "WARN"
            Write-Host "  The service is running but the device object may not be created yet." -ForegroundColor Yellow
            Write-Host "  Try: python tools/driver_ping.py" -ForegroundColor Yellow
        }
    } catch {
        Write-Step "Could not run Python verification: $_" "WARN"
    }
} else {
    Write-Step "Service does not appear to be running" "WARN"
    Write-Host $queryResult
}


# ─────────────────────────────────────────────────────────────
# Final Summary
# ─────────────────────────────────────────────────────────────

Write-Host ""
Write-Banner "Installation Complete"

Write-Host "  Service Name:   $ServiceName" -ForegroundColor White
Write-Host "  Driver Binary:  $DriverDest" -ForegroundColor White
Write-Host "  Device Path:    $devicePath" -ForegroundColor White
Write-Host ""
Write-Host "  Next steps:" -ForegroundColor Green
Write-Host "    1. Run: python tools/driver_ping.py        (verify PING/PONG)" -ForegroundColor White
Write-Host "    2. Run: python main.py --goal '...' --kernel  (use kernel mode)" -ForegroundColor White
Write-Host "    3. To stop:  sc.exe stop $ServiceName" -ForegroundColor White
Write-Host "    4. To remove: .\uninstall_driver.ps1" -ForegroundColor White
Write-Host ""
