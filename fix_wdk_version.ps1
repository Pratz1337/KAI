#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Fix WDK version mismatch: VS 18 looks for 18.0 DLLs but WDK only has 17.0.
    Creates copies of the 17.0 build task DLLs with 18.0 names.
#>

$binDir = "C:\Program Files (x86)\Windows Kits\10\build\10.0.26100.0\bin"

$copies = @(
    @{ Src = "Microsoft.DriverKit.Build.Tasks.17.0.dll";                    Dst = "Microsoft.DriverKit.Build.Tasks.18.0.dll" },
    @{ Src = "Microsoft.DriverKit.Build.Tasks.PackageVerifier.17.0.dll";    Dst = "Microsoft.DriverKit.Build.Tasks.PackageVerifier.18.0.dll" }
)

foreach ($c in $copies) {
    $srcPath = Join-Path $binDir $c.Src
    $dstPath = Join-Path $binDir $c.Dst

    if (-not (Test-Path $srcPath)) {
        Write-Host "[SKIP]  Source not found: $($c.Src)" -ForegroundColor Yellow
        continue
    }

    if (Test-Path $dstPath) {
        Write-Host "[OK]    Already exists: $($c.Dst)" -ForegroundColor Green
        continue
    }

    Copy-Item $srcPath $dstPath -Force
    Write-Host "[DONE]  Copied $($c.Src) -> $($c.Dst)" -ForegroundColor Green
}

Write-Host ""
Write-Host "Verification:" -ForegroundColor Cyan
Get-ChildItem $binDir -Filter "Microsoft.DriverKit.Build.Tasks*" | ForEach-Object {
    Write-Host "  $($_.Name)  ($($_.Length) bytes)"
}

Write-Host ""
Write-Host "You can now build the driver:" -ForegroundColor Green
Write-Host '  cd driver_stub'
Write-Host '  & "C:\Program Files\Microsoft Visual Studio\18\Community\MSBuild\Current\Bin\MSBuild.exe" AikKmdfIoctl\AikKmdfIoctl.vcxproj /p:Configuration=Debug /p:Platform=x64'
