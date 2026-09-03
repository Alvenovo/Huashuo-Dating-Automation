Set-Location $PSScriptRoot
$env:HALL_ALLOW_INSTALL = "1"
Write-Host "Run this in Administrator PowerShell. It uninstalls and reinstalls ASUS Hall."
python -m pytest tests/install -m "install" -v
