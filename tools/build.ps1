<#
.SYNOPSIS
    Local one-command build of winstaller.exe from winstaller.spec.

.DESCRIPTION
    Builds with the interpreter from .venv (or -Python), keeps PyInstaller's work and dist
    directories under -OutDir (default: build-out\ in the repo, ignored by git), then prints the
    exe size and the result of "winstaller.exe --smoke". Only --smoke is run: other modes ask
    for UAC or open a window.

.EXAMPLE
    .\tools\build.ps1
    .\tools\build.ps1 -OutDir $env:TEMP\winstaller-build -Clean
    .\tools\build.ps1 -Measure      # also runs tools\measure_exe.py (size + start-up times)
#>
[CmdletBinding()]
param(
    # Directory for PyInstaller output; the exe lands in <OutDir>\dist\winstaller.exe.
    [string]$OutDir,
    # Remove <OutDir> and PyInstaller's cache before building.
    [switch]$Clean,
    # Python interpreter with PyInstaller and the runtime dependencies installed.
    [string]$Python,
    # Run tools\measure_exe.py after a successful smoke test.
    [switch]$Measure
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot

if (-not $OutDir) { $OutDir = Join-Path $repo 'build-out' }
if (-not $Python) {
    $venvPython = Join-Path $repo '.venv\Scripts\python.exe'
    $Python = if (Test-Path $venvPython) { $venvPython } else { 'python' }
}
$OutDir = [System.IO.Path]::GetFullPath($OutDir)
$workPath = Join-Path $OutDir 'work'
$distPath = Join-Path $OutDir 'dist'
$exe = Join-Path $distPath 'winstaller.exe'

if ($Clean -and (Test-Path $OutDir)) {
    Write-Host "Removing $OutDir"
    Remove-Item -Recurse -Force $OutDir
}
New-Item -ItemType Directory -Force $OutDir | Out-Null

$pyArgs = @('-m', 'PyInstaller', (Join-Path $repo 'winstaller.spec'), '--noconfirm',
    '--workpath', $workPath, '--distpath', $distPath)
if ($Clean) { $pyArgs += '--clean' }

Write-Host "Build: $Python $($pyArgs -join ' ')"
$sw = [System.Diagnostics.Stopwatch]::StartNew()
Push-Location $repo
try {
    & $Python @pyArgs
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}
$sw.Stop()
if (-not (Test-Path $exe)) { throw "Missing $exe" }

$size = (Get-Item $exe).Length
Write-Host ''
Write-Host ("Exe:     {0}" -f $exe)
Write-Host ("Size:    {0:N2} MB ({1} B)" -f ($size / 1MB), $size)
Write-Host ("Time:    {0:N0} s" -f $sw.Elapsed.TotalSeconds)

# A windowed exe does not block PowerShell; piping its output makes the call wait and captures
# stdout (the onefile child inherits the piped handles).
$smoke = & $exe --smoke | Out-String
$code = $LASTEXITCODE
Write-Host ''
Write-Host "--smoke (exit code $code):"
Write-Host $smoke.TrimEnd()
if ($code -ne 0) { throw "winstaller.exe --smoke failed with exit code $code" }

if ($Measure) {
    & $Python (Join-Path $repo 'tools\measure_exe.py') $exe --json (Join-Path $OutDir 'measure.json')
    if ($LASTEXITCODE -ne 0) { throw "measure_exe.py failed with exit code $LASTEXITCODE" }
}
