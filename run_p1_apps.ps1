# P1-A app lifecycle suite (list detection + fixture install/uninstall).
# MUST run ELEVATED: the fixture app installs into C:\Program Files and writes the HKLM
# uninstall table, and that needs an admin token. The vendor wizard itself is readable and
# clickable without elevation (it is NSIS with a standard #32770 dialog); only the install
# step is privileged.
# ASCII only on purpose: PowerShell 5.1 misreads BOM-less UTF-8 as ANSI and collapses lines.
$repo = $PSScriptRoot
$log = "$repo\reports\p1_apps_run.log"
New-Item -ItemType Directory -Force -Path "$repo\reports" | Out-Null

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
"wrapper start admin=$admin time=$(Get-Date -Format o)" | Out-File -FilePath $log -Encoding utf8

if (-not $admin) {
    "NOT ADMIN: the fixture install writes C:\Program Files and the HKLM uninstall table." | Out-File -FilePath $log -Append -Encoding utf8
    Write-Host "Need elevation: open PowerShell as Administrator, then run this script again."
    exit 2
}

$env:HALL_ALLOW_INSTALL = "1"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
Set-Location $repo

# The install/uninstall case needs a clean start: a fixture app left over from the
# previous run makes it skip. reset_fixture.py silently uninstalls it via the registry.
cmd /c "`"$repo\.venv\Scripts\python.exe`" -u tools\reset_fixture.py >> `"$log`" 2>&1"
"reset exit=$LASTEXITCODE" | Out-File -FilePath $log -Append -Encoding utf8

# cmd redirection keeps python's UTF-8 bytes intact; PowerShell >> would re-encode them.
cmd /c "`"$repo\.venv\Scripts\python.exe`" -u -m pytest tests/launch/test_p1_apps.py -m apps -v >> `"$log`" 2>&1"
"pytest exit=$LASTEXITCODE" | Out-File -FilePath $log -Append -Encoding utf8
Write-Host "Done. Log: $log"
