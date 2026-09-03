Set-Location $PSScriptRoot
Write-Host "Launch smoke P0-03/04. Does not reinstall. Closes and restarts ASUS Hall 3 times."
python -m pytest tests/launch -m "launch" -v
