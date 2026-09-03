Set-Location $PSScriptRoot
Write-Host "P0-03/04 launch smoke. Does not reinstall. Restarts ASUS Hall 3 extra times."
$python = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }
& $python -m pytest tests/launch -m "launch" -v
