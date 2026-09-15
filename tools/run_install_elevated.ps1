# Runs the install layer (P0-01/P0-02) and logs pytest output as raw UTF-8 bytes.
# ASCII only on purpose: PowerShell 5.1 misreads BOM-less UTF-8 as ANSI and collapses lines.
$repo = "C:\Users\admin\Desktop\Huashuo-Dating-Automation"
$log = "$repo\reports\install_run.log"
$trace = "$repo\reports\install_trace.log"
try {
    New-Item -ItemType Directory -Force -Path "$repo\reports" | Out-Null
    $env:HALL_ALLOW_INSTALL = "1"
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    "wrapper start admin=$admin cwd=$(Get-Location) time=$(Get-Date -Format o)" | Out-File -FilePath $log -Encoding utf8
    Set-Location $repo
    # cmd redirection keeps python's UTF-8 bytes intact; PowerShell >> would re-encode them.
    cmd /c "`"$repo\.venv\Scripts\python.exe`" -u -m pytest tests/install -m install -v >> `"$log`" 2>&1"
    "pytest exit=$LASTEXITCODE" | Out-File -FilePath $log -Append -Encoding utf8
} catch {
    "wrapper error: $_" | Out-File -FilePath $log -Append -Encoding utf8
}
[System.IO.File]::WriteAllText($trace, "script done " + (Get-Date -Format o))
