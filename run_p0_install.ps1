# ============================================================================
# [已作废 · 2026-09-23] 别再手敲这条命令了。
#
# 装卸已经并进农场的「全量档」，提权由计划任务 HallAutoP1 自动完成，节点侧零人工：
#     控制机： .\.venv\Scripts\python.exe -X utf8 tools\run_farm.py --nodes <节点> --suites all
# 跑批清单里的「步骤十八」现在是「人在环（可选）」，不是本脚本。
#
# 本脚本**保留只作兜底**（提权通道坏了、或要单独复现某一条时用）。
# 保留它的理由：它是 09-17 端到端验过的那条路，在提权通道第一次真机验证通过之前，
# 它是唯一确认能跑通真装真卸的东西。
#
# 编码：本文件是 UTF-8 **with BOM**（为了给一线看中文提示）。
# PS 5.1 会把**无 BOM** 的 UTF-8 当 ANSI 读 —— 中文变乱码，甚至把行吞掉。
# 用编辑器改过本文件，务必确认 BOM 还在（VS Code 右下角应显示 "UTF-8 with BOM"）。
# 原来这里写的是 "ASCII only on purpose"，为了提示说人话改成带 BOM。
# ============================================================================
# 大厅自身装卸：原来也是手敲的，现在并进全量档（提权通道自动跑）。
# ============================================================================
Set-Location $PSScriptRoot
Write-Host "Use Administrator PowerShell. This uninstalls and reinstalls ASUS Hall."
$env:HALL_ALLOW_INSTALL = "1"
$python = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }
& $python -m pytest tests/install -m "install" -v

Write-Host ""
Write-Host "[已作废] 这是旧流程的步骤十八。跑批请用："
Write-Host "           .\.venv\Scripts\python.exe -X utf8 tools\run_farm.py --nodes <节点> --suites all"
Write-Host "         装卸已并进全量档，提权自动完成，不需要手敲本脚本。"
Write-Host "         本脚本只作兜底：提权通道坏了、或要单独复现某一条时用。"
