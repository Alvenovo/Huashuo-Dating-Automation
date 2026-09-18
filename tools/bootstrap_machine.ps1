# One-shot machine bootstrap wrapper. ASCII only on purpose:
# PowerShell 5.1 misreads BOM-less UTF-8 as ANSI and collapses lines.
#
# Runs tools\bootstrap_machine.py with the repo's venv python (falls back to system python
# when the venv does not exist yet, which is the normal case on a fresh machine).
#
# Usage (elevation recommended; step 6 needs admin):
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\bootstrap_machine.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\bootstrap_machine.ps1 -InstallerDir "D:\Test\华硕大厅"
param(
    [string]$InstallerDir = "",
    [string]$NodeId = "",
    [switch]$SkipVenv,
    [switch]$SkipSchtask,
    [switch]$SkipSelftest,
    [switch]$SkipShare,
    [switch]$SkipFarm,
    [switch]$NoDownload
)

$repo = Split-Path -Parent $PSScriptRoot
$venvPy = "$repo\.venv\Scripts\python.exe"
$py = if (Test-Path $venvPy) { $venvPy } else { "python" }

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$argv = @("$repo\tools\bootstrap_machine.py")
if ($InstallerDir) { $argv += @("--installer-dir", $InstallerDir) }
if ($NodeId) { $argv += @("--node-id", $NodeId) }
if ($SkipVenv) { $argv += "--skip-venv" }
if ($SkipSchtask) { $argv += "--skip-schtask" }
if ($SkipSelftest) { $argv += "--skip-selftest" }
if ($SkipShare) { $argv += "--skip-share" }
if ($SkipFarm) { $argv += "--skip-farm" }
if ($NoDownload) { $argv += "--no-download" }

Write-Host "bootstrap: repo=$repo python=$py"
& $py -X utf8 @argv
exit $LASTEXITCODE
