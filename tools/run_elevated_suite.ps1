# Elevated-suite executor wrapper. This is the ACTION of the HallAutoP1 scheduled task.
#
# Why this file exists at all (and why it is not the logic):
#   A scheduled task's action field is a FIXED string - it cannot carry command line
#   arguments. So the parameters travel through a request file instead
#   (reports\_elev\request.json), and all the real work lives in tools\elevated_runner.py
#   where it can be unit tested. This wrapper only locates the repo and launches it.
#
# ASCII only on purpose: PowerShell 5.1 misreads BOM-less UTF-8 as ANSI and collapses lines.
# Do NOT add a UTF-8 BOM here - this file is meant to stay plain ASCII.
#
# The task is created /RL HIGHEST, so this runs with an administrator token.
# Triggered with a standard (filtered) token - that is the whole point: 0 manual steps.
#   MSYS_NO_PATHCONV=1 schtasks /Run /TN HallAutoP1
$repo = Split-Path -Parent $PSScriptRoot
$log = "$repo\reports\_elev\wrapper.log"
New-Item -ItemType Directory -Force -Path "$repo\reports\_elev" | Out-Null

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$py = "$repo\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

# cmd redirection keeps python's UTF-8 bytes intact; PowerShell >> would re-encode them.
cmd /c "`"$py`" -X utf8 `"$repo\tools\elevated_runner.py`" >> `"$log`" 2>&1"
exit $LASTEXITCODE
