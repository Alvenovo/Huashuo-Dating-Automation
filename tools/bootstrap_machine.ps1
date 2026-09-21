# One-shot machine bootstrap wrapper.
#
# 编码：本文件**必须**存成 UTF-8 with BOM（utf-8-sig）。
# PowerShell 5.1 会把**无 BOM** 的 UTF-8 当 ANSI 读 —— 中文变乱码，甚至把行吞掉。
# 所以这里以前全写 ASCII。现在为了给一线**中文**报错改成带 BOM；
# 如果你用编辑器改过本文件，**务必确认 BOM 还在**（VS Code 右下角应显示 "UTF-8 with BOM"）。
#
# 为什么有下面那段 Python 前置检查（2026-09-21 加）：
# 原来这里直接 `& python`。新机器上没装 Python、或者装了但没勾「Add Python to PATH」时，
# PowerShell 只抛一句英文的 `The term 'python' is not recognized as the name of a cmdlet...`。
# 一线看不懂，更看不出根因是「PATH 没配」。而 Python 恰恰是**唯一需要人工装的第 0 层**，
# 是新手最容易卡住的一步 —— 这里的报错必须说人话。
# （同类教训：盘满、pip 源不通、路径不对，报错都长得像「共享盘坏了」。）
#
# Runs tools\bootstrap_machine.py with the repo's venv python (falls back to system python
# when the venv does not exist yet, which is the normal case on a fresh machine).
#
# Usage (elevation recommended; steps 4, 11 and the 7-Zip install need admin):
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\bootstrap_machine.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\bootstrap_machine.ps1 -InstallerDir "D:\Test\hall"
param(
    [string]$InstallerDir = "",
    [string]$NodeId = "",
    [switch]$SkipVenv,
    [switch]$SkipSevenZip,
    [switch]$SkipSecurityTools,
    [switch]$SkipSchtask,
    [switch]$SkipSelftest,
    [switch]$SkipShare,
    [switch]$SkipFarm,
    [switch]$NoDownload
)

$repo = Split-Path -Parent $PSScriptRoot

# 找 Python。顺序：仓库 venv > PATH 上的 python > py 启动器。
# 返回 @{ Exe = <可执行文件>; Arg = @(<前缀参数>); Note = <要提醒的话> } 或 $null。
function Resolve-PythonForBootstrap {
    $venvPy = "$repo\.venv\Scripts\python.exe"
    if (Test-Path $venvPy) {
        return @{ Exe = $venvPy; Arg = @(); Note = "" }
    }

    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        # Windows 自带一个「应用执行别名」桩（在 WindowsApps 下）。没装 Python 时敲 python
        # 会弹微软商店、退出码 9009。它**看着像装了**，最容易被误判成「已经装好了」。
        if ($cmd.Source -like "*\WindowsApps\*") {
            return @{ Exe = $null; Arg = @(); Note = "store-stub" }
        }
        return @{ Exe = $cmd.Source; Arg = @(); Note = "" }
    }

    # python.org 的安装包会装一个 py 启动器，它常常在 PATH 里而 python 不在。
    # 能用就先顶上 —— 总比直接失败强，但必须提醒：手册的自检会通不过。
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        return @{
            Exe  = $launcher.Source
            Arg  = @("-3")
            Note = "python 不在 PATH 里，本次改用 py 启动器顶上"
        }
    }
    return $null
}

function Write-PythonMissingHelp([string]$Reason) {
    Write-Host ""
    Write-Host "============================================================"
    Write-Host "[FAIL] 这台机器上找不到可用的 Python"
    Write-Host "============================================================"
    Write-Host ""
    if ($Reason -eq "store-stub") {
        Write-Host "根因：PATH 上的 python 是 Windows 的「应用执行别名」桩，不是真的 Python。"
        Write-Host "      敲 python 会弹微软商店 —— 看着像装了，其实没装。"
        Write-Host "      处理：设置 → 应用 → 高级应用设置 → 应用执行别名，关掉 python.exe / python3.exe"
    }
    else {
        Write-Host "根因：这台机器上没有 Python，或者装了但**没勾「Add Python to PATH」**。"
    }
    Write-Host ""
    Write-Host "Python 是整个流程里**唯一需要人工装**的东西（脚本自己装不了自己）。"
    Write-Host "装 Python 3.x **64 位**（32 位不行，pywinauto 访问不了 64 位大厅进程）："
    Write-Host "    https://www.python.org/downloads/windows/"
    Write-Host ""
    Write-Host "安装时**务必勾上「Add Python to PATH」**（安装向导第一屏最下面那个复选框）。"
    Write-Host "漏勾就会像现在这样：装是装了，命令行里却敲不出 python。"
    Write-Host ""
    Write-Host "装完之后："
    Write-Host "  1. **开一个新的 PowerShell 窗口**（旧窗口的环境变量不会自动更新）"
    Write-Host "  2. 敲 python --version，能打印版本号才算过"
    Write-Host "  3. 重跑本脚本"
    Write-Host ""
    Write-Host "详细步骤见《项目知识库\测试机操作手册.md》第一部分 1.2。"
    Write-Host ""
}

$found = Resolve-PythonForBootstrap
if (-not $found -or -not $found.Exe) {
    $reason = if ($found) { $found.Note } else { "" }
    Write-PythonMissingHelp $reason
    exit 1
}

if ($found.Note) {
    Write-Host "[提醒] $($found.Note)"
    Write-Host "        建议补上 PATH（安装向导里勾「Add Python to PATH」），否则手册步骤 1 的自检会失败。"
    Write-Host ""
}

$py = $found.Exe
$pyArg = $found.Arg

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$argv = @("$repo\tools\bootstrap_machine.py")
if ($InstallerDir) { $argv += @("--installer-dir", $InstallerDir) }
if ($NodeId) { $argv += @("--node-id", $NodeId) }
if ($SkipVenv) { $argv += "--skip-venv" }
if ($SkipSevenZip) { $argv += "--skip-seven-zip" }
if ($SkipSecurityTools) { $argv += "--skip-security-tools" }
if ($SkipSchtask) { $argv += "--skip-schtask" }
if ($SkipSelftest) { $argv += "--skip-selftest" }
if ($SkipShare) { $argv += "--skip-share" }
if ($SkipFarm) { $argv += "--skip-farm" }
if ($NoDownload) { $argv += "--no-download" }

Write-Host "bootstrap: repo=$repo python=$py"
& $py @pyArg -X utf8 @argv
exit $LASTEXITCODE
