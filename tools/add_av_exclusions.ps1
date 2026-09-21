<#
.SYNOPSIS
    给 Windows Defender 加/减本项目相关的排除项（跑批机器专用）。

.DESCRIPTION
    为什么需要它（2026-09-21 首台真机 DESKTOP-DOHED68 实测）：
    Defender 的实时保护 + ML 判定会把**进程刚写出来的小文件**判成
    `Trojan:Win32/Bearfoos.A!ml`（SeverityID 5）。此后**任何**读它都返回
    GetLastError=225 ERROR_VIRUS_INFECTED（Python 把它显示成
    `[Errno 22] Invalid argument`，看着像"参数写错了"），十几秒后文件被隔离删除。
    Defender 自己的记录里就躺着 pytest 的临时文件：
        file:_C:\...\Temp\pytest-of-*\...\share\fixtures\7z2602-x64.exe

    后果有两层，第二层才是要紧的：
      ① `tests/unit` 里"写个临时包再读回来"的 4 条用例会红，而失败项与代码质量无关；
      ② **从共享盘拷进 `_cache` 的安装包同样会被拦** —— 那才是真会挡住跑批的形态。
    所以第 12 步的自检重跑只能算"别让一线误判代码"，不是治本。

    ⚠️ 这是**安全策略变更**：加了排除项，这些目录/进程 Defender 不再实时查毒。
    因此本脚本刻意做窄：
      · 只加**本项目相关**的几条（仓库目录 / installer_dir / 本机 %TEMP% / python.exe 进程），
        不碰整个 C 盘、不动组策略、不关实时保护；
      · **幂等**：已有的不动，重复跑只会说"已在"；
      · **可回滚**：`-Remove` 只删本脚本会加的那几条，按字符串精确匹配；
      · **可预演**：`-DryRun` 只打印计划，不碰任何设置。
    **要不要在跑批机器上跑、跑哪几台，由人拍板。bootstrap 不会自动跑它。**

.PARAMETER InstallerDir
    安装包目录。不传则从 `<仓库>\config.local.yaml` 的 `installer_dir` 读；
    读不到就跳过这一条（其余照加），并在结论里说明。

.PARAMETER Remove
    反向操作：删掉本脚本会加的那几条排除项。

.PARAMETER DryRun
    只打印计划，不改任何设置。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\add_av_exclusions.ps1 -DryRun
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\add_av_exclusions.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\add_av_exclusions.ps1 -Remove

.EXAMPLE
    # 加完复跑探针验证（应当看不到 225）
    .venv\Scripts\python.exe -X utf8 tools\probe_av_quarantine.py

.NOTES
    退出码：0 = 目标状态已达成（加好 / 删好 / 本来就对）；
            1 = 有失败（明细在 stdout）；
            2 = 非管理员；
            3 = 本机取不到 Defender 配置（没装 Defender，或策略屏蔽了查询）。
#>
[CmdletBinding()]
param(
    [string]$InstallerDir = '',
    [switch]$Remove,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot

function Write-Line([string]$Text) { Write-Output $Text }

# 本脚本会加的进程排除项（只有这一个：跑批全走 python.exe）
$Processes = @('python.exe')

# ---------------- 前置：管理员 ----------------
$isAdmin = $false
try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $isAdmin = (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}
catch { $isAdmin = $false }

if (-not $isAdmin) {
    Write-Line "[av-excl] 非管理员：改 Defender 排除项必须提权。"
    Write-Line "         用管理员 PowerShell 重跑本脚本："
    Write-Line "         powershell -NoProfile -ExecutionPolicy Bypass -File tools\add_av_exclusions.ps1"
    exit 2
}

# ---------------- 前置：本机有没有 Defender 可改 ----------------
try {
    $pref = Get-MpPreference -ErrorAction Stop
}
catch {
    Write-Line "[av-excl] 本机取不到 Windows Defender 配置：$($_.Exception.Message)"
    Write-Line "         可能没装 Defender、或组策略屏蔽了查询。"
    Write-Line "         本脚本只管 Defender；第三方杀软（如 McAfee）要在它自己的控制台里加。"
    exit 3
}

$havePaths = @()
if ($pref.ExclusionPath)    { $havePaths = @($pref.ExclusionPath) }
$haveProcs = @()
if ($pref.ExclusionProcess) { $haveProcs = @($pref.ExclusionProcess) }

# ---------------- 目标路径 ----------------
function Get-InstallerDirFromConfig {
    $cfg = Join-Path $repo 'config.local.yaml'
    if (-not (Test-Path -LiteralPath $cfg)) { return '' }
    foreach ($line in (Get-Content -LiteralPath $cfg -Encoding UTF8)) {
        if ($line -match '^\s*installer_dir\s*:\s*(.+?)\s*$') {
            $value = $matches[1].Trim().Trim("'").Trim('"')
            if ($value) { return $value }
        }
    }
    return ''
}

$wanted = New-Object System.Collections.ArrayList
[void]$wanted.Add($repo)

$installer = $InstallerDir
if (-not $installer) { $installer = Get-InstallerDirFromConfig }
if ($installer) { [void]$wanted.Add($installer) }
else { Write-Line "[av-excl] 没拿到 installer_dir（没传 -InstallerDir，config.local.yaml 里也没有）—— 这一条跳过。" }

if ($env:TEMP) { [void]$wanted.Add($env:TEMP) }

function Normalize([string]$Path) {
    return ($Path -replace '[\\/]+$', '').ToLowerInvariant()
}

function Test-HasPath([string]$Path) {
    $want = Normalize $Path
    foreach ($h in $havePaths) { if ((Normalize $h) -eq $want) { return $true } }
    return $false
}

# ---------------- 执行 ----------------
$mode = '加'
if ($Remove) { $mode = '删' }
Write-Line "[av-excl] 目标：$($wanted.Count) 条路径 + $($Processes.Count) 个进程；动作：$mode$(if ($DryRun) { '（预演，不改设置）' })"
Write-Line ""

$failed = 0
$changed = 0

foreach ($p in $wanted) {
    $present = Test-HasPath $p
    if (-not $Remove -and $present) {
        Write-Line "  [已在] $p"
        continue
    }
    if ($Remove -and -not $present) {
        Write-Line "  [本无] $p"
        continue
    }
    if ($DryRun) {
        if ($Remove) { Write-Line "  [计划] Remove-MpPreference -ExclusionPath `"$p`"" }
        else         { Write-Line "  [计划] Add-MpPreference    -ExclusionPath `"$p`"" }
        continue
    }
    try {
        if ($Remove) {
            Remove-MpPreference -ExclusionPath $p -ErrorAction Stop
            Write-Line "  [删]   $p"
        }
        else {
            Add-MpPreference -ExclusionPath $p -ErrorAction Stop
            Write-Line "  [加]   $p"
        }
        $changed++
    }
    catch {
        Write-Line "  [失败] $p -> $($_.Exception.Message)"
        $failed++
    }
}

foreach ($proc in $Processes) {
    $present = $false
    foreach ($h in $haveProcs) { if ($h.ToLowerInvariant() -eq $proc.ToLowerInvariant()) { $present = $true } }

    if (-not $Remove -and $present) {
        Write-Line "  [已在] 进程 $proc"
        continue
    }
    if ($Remove -and -not $present) {
        Write-Line "  [本无] 进程 $proc"
        continue
    }
    if ($DryRun) {
        if ($Remove) { Write-Line "  [计划] Remove-MpPreference -ExclusionProcess `"$proc`"" }
        else         { Write-Line "  [计划] Add-MpPreference    -ExclusionProcess `"$proc`"" }
        continue
    }
    try {
        if ($Remove) {
            Remove-MpPreference -ExclusionProcess $proc -ErrorAction Stop
            Write-Line "  [删]   进程 $proc"
        }
        else {
            Add-MpPreference -ExclusionProcess $proc -ErrorAction Stop
            Write-Line "  [加]   进程 $proc"
        }
        $changed++
    }
    catch {
        Write-Line "  [失败] 进程 $proc -> $($_.Exception.Message)"
        $failed++
    }
}

Write-Line ""
if ($DryRun) {
    Write-Line "[av-excl] 预演结束，什么都没改。去掉 -DryRun 才真的执行。"
    exit 0
}

Write-Line "[av-excl] 本次变更 $changed 条，失败 $failed 条。"

if ($Remove) {
    Write-Line "[av-excl] 已回滚本脚本加过的排除项（其它排除项一律没动）。"
    exit 0
}

Write-Line ""
Write-Line "验证（应当看不到 225 / ERROR_VIRUS_INFECTED）："
Write-Line "  .venv\Scripts\python.exe -X utf8 tools\probe_av_quarantine.py"
Write-Line ""
Write-Line "⚠️ 这是安全策略变更：上述目录与 python.exe 不再被实时查毒。"
Write-Line "   换机器、任务结束、或安全评审有意见时，用 -Remove 回滚。"
Write-Line "   本机若还装着第三方杀软（如 McAfee），要在它自己的控制台里另加。"

if ($failed -gt 0) { exit 1 }
exit 0
