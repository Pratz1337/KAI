<#
.SYNOPSIS
    Diagnoses the AIK Kernel Driver installation and status.

.DESCRIPTION
    Checks service registration, running state, test signing,
    and attempts to open the device handle from PowerShell/Python.

.EXAMPLE
    .\check_driver.ps1
#>

$ErrorActionPreference = "SilentlyContinue"
$ServiceName = "AikKmdfDriver"

function Write-Check {
    param([string]$Label, [string]$Status, [string]$Detail = "")
    $color = switch ($Status) {
        "PASS" { "Green" }
        "FAIL" { "Red" }
        "WARN" { "Yellow" }
        "INFO" { "Cyan" }
        default { "White" }
    }
    $icon = switch ($Status) {
        "PASS" { "[PASS]" }
        "FAIL" { "[FAIL]" }
        "WARN" { "[WARN]" }
        "INFO" { "[INFO]" }
        default { "[????]" }
    }
    Write-Host "$icon $Label" -ForegroundColor $color
    if ($Detail) {
        Write-Host "       $Detail" -ForegroundColor Gray
    }
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  AIK Kernel Driver Diagnostic Check" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""


# ─────────────────────────────────────────────────────────────
# 1. Running as Administrator?
# ─────────────────────────────────────────────────────────────

$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Check "Administrator privileges" "PASS"
} else {
    Write-Check "Administrator privileges" "WARN" "Some checks may not work without admin. Consider running as admin."
}


# ─────────────────────────────────────────────────────────────
# 2. Test Signing
# ─────────────────────────────────────────────────────────────

try {
    $bcdOutput = & bcdedit /enum "{current}" 2>&1 | Out-String
    if ($bcdOutput -match "testsigning\s+Yes") {
        Write-Check "Test signing" "PASS" "Enabled"
    } else {
        Write-Check "Test signing" "FAIL" "Not enabled. Run: bcdedit /set testsigning on (then reboot)"
    }
} catch {
    Write-Check "Test signing" "WARN" "Could not query bcdedit (need admin?)"
}


# ─────────────────────────────────────────────────────────────
# 3. Service Exists?
# ─────────────────────────────────────────────────────────────

Write-Host ""
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc) {
    Write-Check "Service '$ServiceName' exists" "PASS" "Status: $($svc.Status), StartType: $($svc.StartType)"
} else {
    # Also check the alternate name
    $svcAlt = Get-Service -Name "AikKmdfIoctl" -ErrorAction SilentlyContinue
    if ($svcAlt) {
        Write-Check "Service 'AikKmdfIoctl' exists (alternate name)" "PASS" "Status: $($svcAlt.Status)"
        $svc = $svcAlt
        $ServiceName = "AikKmdfIoctl"
    } else {
        Write-Check "Service '$ServiceName' exists" "FAIL" "Service not registered. Run install_driver.ps1"
    }
}


# ─────────────────────────────────────────────────────────────
# 4. Service Running?
# ─────────────────────────────────────────────────────────────

if ($svc) {
    if ($svc.Status -eq "Running") {
        Write-Check "Service running" "PASS"
    } else {
        Write-Check "Service running" "FAIL" "Service exists but status is '$($svc.Status)'. Run: sc.exe start $ServiceName"
    }
}


# ─────────────────────────────────────────────────────────────
# 5. sc.exe query (detailed)
# ─────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "--- sc.exe query $ServiceName ---" -ForegroundColor DarkGray
$queryOutput = & sc.exe query $ServiceName 2>&1 | Out-String
if ($queryOutput) {
    Write-Host $queryOutput -ForegroundColor Gray
} else {
    Write-Host "  (no output — service likely not registered)" -ForegroundColor Gray
}


# ─────────────────────────────────────────────────────────────
# 6. Driver binary exists in System32?
# ─────────────────────────────────────────────────────────────

$driverDest = "$env:SystemRoot\System32\drivers\aik_kmdf.sys"
if (Test-Path $driverDest) {
    $fi = Get-Item $driverDest
    Write-Check "Driver binary at $driverDest" "PASS" "Size: $($fi.Length) bytes, Modified: $($fi.LastWriteTime)"
} else {
    Write-Check "Driver binary at $driverDest" "FAIL" "File not found"
}


# ─────────────────────────────────────────────────────────────
# 7. Try opening device handle via Python
# ─────────────────────────────────────────────────────────────

Write-Host ""
$pyCheck = @"
import ctypes, sys
from ctypes import wintypes
k32 = ctypes.WinDLL('kernel32', use_last_error=True)
h = k32.CreateFileW(r'\\.\AikKmdfIoctl', 0x80000000|0x40000000, 3, None, 3, 0x80, None)
if h == wintypes.HANDLE(-1).value:
    err = ctypes.get_last_error()
    msgs = {2:'FILE_NOT_FOUND (driver device not created)',
            3:'PATH_NOT_FOUND',
            5:'ACCESS_DENIED (run as admin)',
            1275:'DRIVER_BLOCKED (check test signing)'}
    print(f'DEVICE_FAIL:error={err}:{msgs.get(err,"unknown")}')
    sys.exit(1)
else:
    print('DEVICE_OK')
    k32.CloseHandle(h)
"@

try {
    $result = & python -c $pyCheck 2>&1 | Out-String
    if ($result -match "DEVICE_OK") {
        Write-Check "Device handle \\.\AikKmdfIoctl" "PASS" "Opened successfully from Python"
    } elseif ($result -match "DEVICE_FAIL:error=(\d+):(.+)") {
        Write-Check "Device handle \\.\AikKmdfIoctl" "FAIL" "Win32 error $($Matches[1]): $($Matches[2])"
    } else {
        Write-Check "Device handle \\.\AikKmdfIoctl" "WARN" "Unexpected output: $result"
    }
} catch {
    Write-Check "Device handle test (Python)" "WARN" "Python not available or failed: $_"
}


# ─────────────────────────────────────────────────────────────
# 8. List all kernel drivers with "Aik" in the name
# ─────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "--- Kernel drivers matching '*Aik*' ---" -ForegroundColor DarkGray
$aikDrivers = Get-Service -DisplayName "*Aik*" -ErrorAction SilentlyContinue
$aikDrivers2 = Get-Service -Name "*Aik*" -ErrorAction SilentlyContinue
$all = @()
if ($aikDrivers)  { $all += $aikDrivers }
if ($aikDrivers2) { $all += $aikDrivers2 }
$all = $all | Sort-Object -Property Name -Unique

if ($all.Count -gt 0) {
    $all | Format-Table Name, DisplayName, Status, StartType -AutoSize | Out-String | Write-Host -ForegroundColor Gray
} else {
    Write-Host "  No services matching '*Aik*' found." -ForegroundColor Gray
}


# ─────────────────────────────────────────────────────────────
# 9. Summary
# ─────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Diagnostic Complete" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  If all checks PASS, run:" -ForegroundColor Green
Write-Host "    python main.py --goal '...' --kernel" -ForegroundColor White
Write-Host ""
Write-Host "  If service is missing:" -ForegroundColor Yellow
Write-Host "    .\install_driver.ps1 -SysPath 'path\to\AikKmdfIoctl.sys'" -ForegroundColor White
Write-Host ""
Write-Host "  If service exists but stopped:" -ForegroundColor Yellow
Write-Host "    sc.exe start AikKmdfDriver" -ForegroundColor White
Write-Host ""
