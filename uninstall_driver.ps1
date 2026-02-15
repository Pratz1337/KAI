#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Uninstalls the AIK Kernel-Mode Driver (AikKmdfDriver).

.DESCRIPTION
    - Stops the driver service if running
    - Deletes the service registration
    - Removes the .sys file from System32\drivers\
    - Cleans up registry entries

.EXAMPLE
    .\uninstall_driver.ps1
#>

$ErrorActionPreference = "Stop"
$ServiceName = "AikKmdfDriver"
$DriverDest  = "$env:SystemRoot\System32\drivers\aik_kmdf.sys"

function Write-Step {
    param([string]$Text, [string]$Status = "INFO")
    $color = switch ($Status) {
        "OK"    { "Green" }
        "WARN"  { "Yellow" }
        "ERROR" { "Red" }
        "INFO"  { "White" }
        default { "White" }
    }
    $icon = switch ($Status) {
        "OK"    { "[OK]   " }
        "WARN"  { "[WARN] " }
        "ERROR" { "[FAIL] " }
        "INFO"  { "[INFO] " }
        default { "       " }
    }
    Write-Host "$icon $Text" -ForegroundColor $color
}


Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  AIK Kernel Driver Uninstaller" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""


# ─────────────────────────────────────────────────────────────
# 1. Verify Administrator
# ─────────────────────────────────────────────────────────────

$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Step "This script MUST be run as Administrator." "ERROR"
    exit 1
}
Write-Step "Running as Administrator" "OK"


# ─────────────────────────────────────────────────────────────
# 2. Stop the service
# ─────────────────────────────────────────────────────────────

Write-Host ""
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc) {
    if ($svc.Status -eq "Running") {
        Write-Step "Stopping service '$ServiceName'..." "INFO"
        $stopResult = & sc.exe stop $ServiceName 2>&1 | Out-String
        Start-Sleep -Seconds 2

        $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
        if ($svc -and $svc.Status -eq "Stopped") {
            Write-Step "Service stopped" "OK"
        } else {
            Write-Step "Service may not have stopped cleanly: $stopResult" "WARN"
        }
    } else {
        Write-Step "Service is already stopped (status: $($svc.Status))" "OK"
    }
} else {
    Write-Step "Service '$ServiceName' not found (already removed?)" "WARN"

    # Check alternate name
    $svcAlt = Get-Service -Name "AikKmdfIoctl" -ErrorAction SilentlyContinue
    if ($svcAlt) {
        Write-Step "Found service under alternate name 'AikKmdfIoctl'" "WARN"
        $ServiceName = "AikKmdfIoctl"
        if ($svcAlt.Status -eq "Running") {
            & sc.exe stop $ServiceName 2>&1 | Out-Null
            Start-Sleep -Seconds 2
            Write-Step "Stopped 'AikKmdfIoctl'" "OK"
        }
    }
}


# ─────────────────────────────────────────────────────────────
# 3. Delete the service
# ─────────────────────────────────────────────────────────────

Write-Host ""
$exists = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($exists) {
    Write-Step "Deleting service '$ServiceName'..." "INFO"
    $deleteResult = & sc.exe delete $ServiceName 2>&1 | Out-String
    Start-Sleep -Seconds 1

    $stillExists = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($stillExists) {
        Write-Step "Service may still exist (pending reboot): $deleteResult" "WARN"
    } else {
        Write-Step "Service deleted" "OK"
    }
} else {
    Write-Step "Service '$ServiceName' does not exist (nothing to delete)" "OK"
}


# ─────────────────────────────────────────────────────────────
# 4. Remove the .sys file
# ─────────────────────────────────────────────────────────────

Write-Host ""
if (Test-Path $DriverDest) {
    Write-Step "Removing driver binary: $DriverDest" "INFO"
    try {
        Remove-Item -Path $DriverDest -Force
        Write-Step "Driver binary removed" "OK"
    } catch {
        Write-Step "Could not remove $DriverDest : $_" "WARN"
        Write-Host "       File may be in use. Try delete after reboot." -ForegroundColor Yellow
    }
} else {
    Write-Step "Driver binary not found at $DriverDest (already removed)" "OK"
}


# ─────────────────────────────────────────────────────────────
# 5. Clean up registry entries
# ─────────────────────────────────────────────────────────────

Write-Host ""
$regPath = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName"
if (Test-Path $regPath) {
    Write-Step "Cleaning up registry: $regPath" "INFO"
    try {
        Remove-Item -Path $regPath -Recurse -Force
        Write-Step "Registry entries removed" "OK"
    } catch {
        Write-Step "Could not clean registry (will be removed after reboot): $_" "WARN"
    }
} else {
    Write-Step "No registry entries found for '$ServiceName'" "OK"
}


# ─────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Uninstall Complete" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Driver service removed. If any steps showed warnings," -ForegroundColor White
Write-Host "  you may need to reboot for complete cleanup." -ForegroundColor White
Write-Host ""
Write-Host "  To reinstall later:" -ForegroundColor Green
Write-Host "    .\install_driver.ps1 -SysPath 'path\to\AikKmdfIoctl.sys'" -ForegroundColor White
Write-Host ""
