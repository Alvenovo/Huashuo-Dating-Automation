Set-Location $PSScriptRoot
Write-Host "Use Administrator PowerShell. This uninstalls and reinstalls ASUS Hall."
$env:HALL_ALLOW_INSTALL = "1"
$python = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }
& $python -m pytest tests/install -m "install" -v
