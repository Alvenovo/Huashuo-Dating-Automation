<#
.SYNOPSIS
    把测试机改成「常亮」：永不睡眠 / 永不关显示器 / 关掉屏保。**可回滚**。

.DESCRIPTION
    为什么还需要这个脚本 —— `hall_auto/awake.py` 已经在做常亮了，但两者管的
    **不是同一段时间**，缺一不可：

      · `awake.py`（进程级，pytest 跑批期间）：`SetThreadExecutionState`，
        **只在那个 python 进程活着时生效**，进程一退就自动失效。它管不了：
          - 跑批**开始之前**（人还没敲命令、机器在桌上闲着）
          - 两轮任务**之间**（节点 agent 空转等任务，可能空等几小时）
          - 跑批**结束之后**（机器要留到第二天接着跑）
      · 本脚本（机器级，持久）：改**当前电源方案**的超时值，改完一直有效、重启不丢。
        管的正是上面那三段空档。

    ⚠️ 为什么这个空档要紧：无人值守的节点机按 Windows 默认电源方案
    （显示器 10 分钟关、30 分钟睡眠）会**自己睡过去**。睡着的机器连 agent 的轮询
    线程一起停摆 —— 控制机看不到回执，现场表现是「投了任务没反应」，
    而它与「共享盘断了」「agent 崩了」长得一模一样，排查方向完全是错的。
    组长 2026-09-18 已确认测试机无人碰屏，允许设「永不睡眠 / 不锁屏」。

    改的是什么（每一项都可回滚）：
      powercfg：显示器关闭 / 睡眠 / 休眠 三个超时，交流(AC)与电池(DC) 都设为 0 = 永不
      合盖动作（笔记本）：设为「不采取任何操作」（要管理员；-SkipLid 可跳过）
      HKCU：屏保关掉（ScreenSaveActive=0），且唤醒时不要求重新登录（ScreenSaverIsSecure=0）

    ⚠️ 本脚本**不碰**「组策略下发的屏保 / 电源策略」—— 策略会把它这里的设置顶回去。
    脚本会检测到并提示，但不会去改策略（那要 IT 动域策略，不是脚本该干的事）。

.PARAMETER Check
    只读：打印当前设置 + 结论，**一个字节都不改**。
    退出码 0 = 已经是常亮；1 = 会息屏/会睡眠（需要跑一次本脚本）。

.PARAMETER Restore
    回滚：按备份文件把超时值与屏保还原成**改之前**的样子。

.PARAMETER DryRun
    只打印打算改什么，不真改。

.PARAMETER SkipLid
    不动合盖动作（该项需要管理员，且有的机型没有这个设置项）。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\set_keep_awake.ps1 -Check
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\set_keep_awake.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\set_keep_awake.ps1 -Restore

.NOTES
    退出码：0 = 目标状态已达成（已设好 / 本来就对 / 还原成功 / -DryRun 走完）；
            1 = 有设置没生效（明细在 stdout）；
            2 = 需要管理员（提权后重跑）；
            3 = 取不到电源信息（powercfg 不可用，或输出解析不了）。

    备份：%LOCALAPPDATA%\hall-keep-awake\power_backup.json（**本机专属，不进仓库**）。
    **这个文件别删** —— 删了就只能手工把电源方案调回默认，没人记得住原值。
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$Restore,
    [switch]$DryRun,
    [switch]$SkipLid
)

$ErrorActionPreference = 'Stop'

function Write-Line([string]$Text) { Write-Output $Text }

# 电源设置项的 GUID：这些是 Windows 固定值，与系统语言无关（所以比匹配中文标签可靠）。
$SUB_SLEEP    = '238c9fa8-0aad-41ed-83f4-97be242c8f20'
$ID_STANDBY   = '29f6c1db-86da-48c5-9fdb-f2b67b1f44da'   # 在此时间后睡眠
$ID_HIBERNATE = '9d7815a6-7ee4-497e-8888-515a05f02364'   # 在此时间后休眠
$SUB_VIDEO    = '7516b95f-f776-4464-8c53-06167f40cc99'
$ID_VIDEOIDLE = '3c0bc021-c8a8-4e07-a973-6b14cbcb2b7e'   # 在此时间后关闭显示器
$SUB_BUTTONS  = '4f971e89-eebd-4455-a8de-9e59040e7347'
$ID_LID       = '5ca83367-6e45-459f-a27b-476b1d01c936'   # 合上盖子时

$BackupDir  = Join-Path $env:LOCALAPPDATA 'hall-keep-awake'
$BackupFile = Join-Path $BackupDir 'power_backup.json'
$DesktopKey = 'HKCU:\Control Panel\Desktop'
$PolicyKeys = @(
    'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Control Panel\Desktop',
    'HKCU:\SOFTWARE\Policies\Microsoft\Windows\Control Panel\Desktop'
)

function Test-Admin {
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        return (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)
    }
    catch { return $false }
}

function Format-Timeout([int64]$Seconds) {
    if ($Seconds -le 0) { return '永不' }
    return ('{0} 分钟' -f [math]::Round($Seconds / 60, 0))
}

function To-Minutes([int64]$Seconds) {
    if ($Seconds -le 0) { return 0 }
    # powercfg /change 只吃分钟。原始值若不是整分钟，向上取整 ——
    # 宁可还原得比原来久一点，也不要因为截断变成"0 = 永不"（那是改不回来的方向）。
    return [int][math]::Ceiling($Seconds / 60)
}

function Get-Timeout([string]$Sub, [string]$Id) {
    <#  返回 @{ Ac = <秒>; Dc = <秒> }；取不到返回 $null。

        输出是**本地化**的（中文系统吐中文标签），所以这里不匹配任何文字，
        只抓 `0x` 十六进制索引。

        ⚠️ **必须取末尾两个，不能取前两个**（2026-09-21 实跑抓出来的坑，严重）：
        `/query` 在「当前 AC/DC 索引」之前先打一段取值范围，里面同样是 8 位十六进制：

            最小可能的设置: 0x00000000          ← 第 1 个
            最大可能的设置: 0xffffffff          ← 第 2 个
            可能的设置增量: 0x00000001
            可能的设置单位: 秒
            当前交流电源设置索引: 0x00000258     ← 真正要的（倒数第 2 个）
            当前直流电源设置索引: 0x00000078     ← 真正要的（倒数第 1 个）

        取前两个的话，**任何机器**都会读成「AC=0（永不）/ DC=0xffffffff」，
        于是 `-Check` 在一台十分钟后就睡的机器上照样报 `[OK] 已常亮`，
        bootstrap 第 13 步因此一个设置都不改 —— 假绿，而且假得看不出来。
        取值范围那三个数字永远排在 AC/DC 之前，所以**末尾两个**才是当前索引；
        枚举型设置（如合盖动作）没有取值范围段，末尾两个同样是 AC/DC，两种情况都成立。
    #>
    $raw = ''
    try { $raw = (& powercfg /query SCHEME_CURRENT $Sub $Id 2>&1 | Out-String) }
    catch { return $null }
    # 命令本身失败时不要拿残缺输出硬猜（猜出来的值一样会被当成"读到了"）
    if ($LASTEXITCODE -ne 0) { return $null }
    $m = [regex]::Matches($raw, '0x([0-9a-fA-F]{8})')
    if ($m.Count -lt 2) { return $null }
    return @{
        Ac = [int64][Convert]::ToInt64($m[$m.Count - 2].Groups[1].Value, 16)
        Dc = [int64][Convert]::ToInt64($m[$m.Count - 1].Groups[1].Value, 16)
    }
}

function Get-RegValue([string]$Path, [string]$Name) {
    try { return [string](Get-ItemProperty -Path $Path -Name $Name -ErrorAction Stop).$Name }
    catch { return '' }
}

function Test-ScreenSaverOff {
    return ((Get-RegValue $DesktopKey 'ScreenSaveActive') -eq '0')
}

function Test-PolicyOverrides {
    <#  组策略下发的屏保/电源策略会把这里的设置顶回去。
        只检测 + 提示，不改策略 —— 那是 IT 的域策略，脚本没资格动。 #>
    $hits = @()
    foreach ($key in $PolicyKeys) {
        if (Test-Path $key) {
            foreach ($name in @('ScreenSaveActive', 'ScreenSaveTimeOut', 'ScreenSaverIsSecure')) {
                $v = Get-RegValue $key $name
                if ($v -ne '') { $hits += ("{0} → {1}={2}" -f $key, $name, $v) }
            }
        }
    }
    return $hits
}

$script:failures = @()

function Set-Timeout([string]$Arg, [int]$Minutes) {
    if ($DryRun) {
        Write-Line ("    [计划] powercfg /change {0} {1}" -f $Arg, $Minutes)
        return $true
    }
    $out = (& powercfg /change $Arg $Minutes 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        Write-Line ("    [失败] powercfg /change {0} {1} -> rc={2} {3}" -f $Arg, $Minutes, $LASTEXITCODE, $out)
        return $false
    }
    Write-Line ("    [改] {0} = {1}" -f $Arg, $Minutes)
    return $true
}

$isAdmin = Test-Admin

Write-Line '=== 测试机常亮（永不睡眠 / 永不关显示器）==='
Write-Line ("管理员: {0}" -f $isAdmin)

$monitor = Get-Timeout $SUB_VIDEO $ID_VIDEOIDLE
$sleep   = Get-Timeout $SUB_SLEEP $ID_STANDBY
if ($null -eq $monitor -or $null -eq $sleep) {
    Write-Line '[FAIL] 取不到当前电源设置（powercfg 不可用，或输出解析不了）。'
    Write-Line '       先手工确认：powercfg /getactivescheme'
    exit 3
}
$hibernate = Get-Timeout $SUB_SLEEP $ID_HIBERNATE
$lid       = if ($SkipLid) { $null } else { Get-Timeout $SUB_BUTTONS $ID_LID }

Write-Line ("当前电源方案: {0}" -f (((& powercfg /getactivescheme 2>&1) | Out-String).Trim()))
Write-Line ("  关闭显示器 : AC {0} / DC {1}" -f (Format-Timeout $monitor.Ac), (Format-Timeout $monitor.Dc))
Write-Line ("  睡眠       : AC {0} / DC {1}" -f (Format-Timeout $sleep.Ac), (Format-Timeout $sleep.Dc))
if ($null -ne $hibernate) {
    Write-Line ("  休眠       : AC {0} / DC {1}" -f (Format-Timeout $hibernate.Ac), (Format-Timeout $hibernate.Dc))
}
if ($null -ne $lid) {
    Write-Line ("  合盖动作   : AC {0} / DC {1}（0 = 不采取任何操作）" -f $lid.Ac, $lid.Dc)
}
Write-Line ("  屏保       : {0}" -f $(if (Test-ScreenSaverOff) { '已关闭' } else { '开着或未设置' }))
$policy = Test-PolicyOverrides
if ($policy.Count -gt 0) {
    Write-Line '⚠️ 检测到组策略下发的屏保设置（本脚本改不动，会被它顶回去）：'
    foreach ($h in $policy) { Write-Line ("     {0}" -f $h) }
}
Write-Line ''

# ---------------- -Check：只读结论 ----------------
if ($Check) {
    # AC/DC 都要看：脚本两套都设成 0，只查 AC 的话，一台拔了电源（跑在电池上、
    # DC 仍是 10 分钟）的笔记本会被判成「已常亮」—— 又是"设了没查全"的假绿。
    $ok = ($monitor.Ac -eq 0) -and ($monitor.Dc -eq 0) -and
          ($sleep.Ac -eq 0) -and ($sleep.Dc -eq 0) -and (Test-ScreenSaverOff)
    if ($ok) {
        Write-Line '[OK] 本机已常亮：不会息屏、不会睡眠、屏保已关。'
        exit 0
    }
    Write-Line '[需要设置] 本机会息屏或睡眠 —— 无人值守跑批会在中途断掉（看着像 agent 卡死）。'
    Write-Line '           跑一次：powershell -NoProfile -ExecutionPolicy Bypass -File tools\set_keep_awake.ps1'
    exit 1
}

# ---------------- -Restore：回滚 ----------------
if ($Restore) {
    if (-not (Test-Path $BackupFile)) {
        Write-Line ("[FAIL] 没有备份文件：{0}" -f $BackupFile)
        Write-Line '       没法知道改之前是什么值，不能瞎猜（猜错方向可能是"改回永不"）。'
        Write-Line '       手工还原：powercfg /change monitor-timeout-ac <分钟> / standby-timeout-ac <分钟>'
        exit 1
    }
    $bk = Get-Content -Raw -Encoding UTF8 $BackupFile | ConvertFrom-Json
    Write-Line ("按备份还原（备份生成于 {0}，机器 {1}）" -f $bk.created_at, $bk.machine)
    if ($DryRun) { Write-Line '  （-DryRun：只打印，不改）' }

    $null = Set-Timeout 'monitor-timeout-ac'   (To-Minutes $bk.settings.monitor.ac)
    $null = Set-Timeout 'monitor-timeout-dc'   (To-Minutes $bk.settings.monitor.dc)
    $null = Set-Timeout 'standby-timeout-ac'   (To-Minutes $bk.settings.sleep.ac)
    $null = Set-Timeout 'standby-timeout-dc'   (To-Minutes $bk.settings.sleep.dc)
    if ($bk.settings.hibernate) {
        $null = Set-Timeout 'hibernate-timeout-ac' (To-Minutes $bk.settings.hibernate.ac)
        $null = Set-Timeout 'hibernate-timeout-dc' (To-Minutes $bk.settings.hibernate.dc)
    }
    if ($bk.settings.screensaver) {
        if ($DryRun) {
            Write-Line ("    [计划] 还原屏保 ScreenSaveActive={0} ScreenSaverIsSecure={1}" -f `
                $bk.settings.screensaver.ScreenSaveActive, $bk.settings.screensaver.ScreenSaverIsSecure)
        }
        else {
            Set-ItemProperty -Path $DesktopKey -Name 'ScreenSaveActive'    -Value $bk.settings.screensaver.ScreenSaveActive
            Set-ItemProperty -Path $DesktopKey -Name 'ScreenSaverIsSecure' -Value $bk.settings.screensaver.ScreenSaverIsSecure
            Write-Line '    [改] 屏保已还原'
        }
    }
    if ((-not $DryRun) -and $bk.settings.lid -and (-not $SkipLid)) {
        # 合盖动作只能用 /setacvalueindex，比 /change 多两步（设值 + 激活方案）
        $null = (& powercfg /setacvalueindex SCHEME_CURRENT $SUB_BUTTONS $ID_LID $bk.settings.lid.ac 2>&1)
        $null = (& powercfg /setdcvalueindex SCHEME_CURRENT $SUB_BUTTONS $ID_LID $bk.settings.lid.dc 2>&1)
        $null = (& powercfg /setactive SCHEME_CURRENT 2>&1)
        Write-Line '    [改] 合盖动作已还原'
    }
    Write-Line ''
    Write-Line '还原完成。备份文件保留在：' + $BackupFile
    exit 0
}

# ---------------- 默认：设置 ----------------
if ($DryRun) { Write-Line '（-DryRun：只打印计划，不改任何设置）' }

# 备份只在**第一次**写：第二次跑时值已经是 0，再写一份就把原值盖没了、再也回不去。
if ((Test-Path $BackupFile) -and (-not $DryRun)) {
    Write-Line ("备份已存在，沿用：{0}" -f $BackupFile)
}
else {
    if (-not $DryRun) {
        New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
        $payload = [ordered]@{
            created_at  = (Get-Date).ToString('s')
            machine     = $env:COMPUTERNAME
            scheme      = (((& powercfg /getactivescheme 2>&1) | Out-String).Trim())
            settings    = [ordered]@{
                monitor     = @{ ac = $monitor.Ac; dc = $monitor.Dc }
                sleep       = @{ ac = $sleep.Ac; dc = $sleep.Dc }
                hibernate   = @{ ac = $(if ($null -ne $hibernate) { $hibernate.Ac } else { $null });
                                 dc = $(if ($null -ne $hibernate) { $hibernate.Dc } else { $null }) }
                lid         = @{ ac = $(if ($null -ne $lid) { $lid.Ac } else { $null });
                                 dc = $(if ($null -ne $lid) { $lid.Dc } else { $null }) }
                screensaver = @{
                    ScreenSaveActive    = (Get-RegValue $DesktopKey 'ScreenSaveActive')
                    ScreenSaverIsSecure = (Get-RegValue $DesktopKey 'ScreenSaverIsSecure')
                }
            }
        }
        ($payload | ConvertTo-Json -Depth 5) | Set-Content -Encoding UTF8 -Path $BackupFile
        Write-Line ("原值已备份：{0}" -f $BackupFile)
    }
}

Write-Line '设置中：'
foreach ($arg in @('monitor-timeout-ac', 'monitor-timeout-dc', 'standby-timeout-ac', 'standby-timeout-dc')) {
    if (-not (Set-Timeout $arg 0)) { $script:failures += $arg }
}
# 休眠超时在"休眠被关掉"的机器上会报错，那属于正常（本来就不会休眠），只提示不判失败。
foreach ($arg in @('hibernate-timeout-ac', 'hibernate-timeout-dc')) {
    if (-not $DryRun) {
        $null = (& powercfg /change $arg 0 2>&1 | Out-String)
        if ($LASTEXITCODE -ne 0) { Write-Line ("    [跳过] {0}：本机不支持（休眠已关闭），不影响常亮" -f $arg) }
        else { Write-Line ("    [改] {0} = 0" -f $arg) }
    }
}

# 合盖：笔记本合盖默认睡眠，工位上一合就睡过去。需要管理员，失败只提示不判死。
if ((-not $SkipLid) -and ($null -ne $lid)) {
    if ($DryRun) {
        Write-Line '    [计划] powercfg /setacvalueindex … LIDACTION 0（合盖不操作）'
    }
    else {
        $null = (& powercfg /setacvalueindex SCHEME_CURRENT $SUB_BUTTONS $ID_LID 0 2>&1)
        $lidAc = $LASTEXITCODE
        $null = (& powercfg /setdcvalueindex SCHEME_CURRENT $SUB_BUTTONS $ID_LID 0 2>&1)
        $lidDc = $LASTEXITCODE
        $null = (& powercfg /setactive SCHEME_CURRENT 2>&1)
        if (($lidAc -eq 0) -and ($lidDc -eq 0)) {
            Write-Line '    [改] 合盖动作 = 不采取任何操作'
        }
        else {
            Write-Line ("    [提示] 合盖动作没改成（rc={0}/{1}）—— 该项需要管理员；" -f $lidAc, $lidDc)
            Write-Line '           不打算改可加 -SkipLid；笔记本跑批建议提权重跑一次。'
        }
    }
}

if ($DryRun) { Write-Line ''; Write-Line '（-DryRun 结束，未做任何修改）'; exit 0 }

# 屏保：HKCU，不需要管理员。有组策略时会被顶回去，所以提示但不判失败。
if ($policy.Count -gt 0) {
    Write-Line '    [提示] 有组策略屏保设置，本脚本关掉的屏保可能被策略顶回来（跑 -Check 复核）。'
}
Set-ItemProperty -Path $DesktopKey -Name 'ScreenSaveActive'    -Value '0'
Set-ItemProperty -Path $DesktopKey -Name 'ScreenSaverIsSecure' -Value '0'
Write-Line '    [改] 屏保已关闭（唤醒不要求重新登录）'
Write-Line ''

# 复核：改完必须回读一遍。只看命令 rc 会假绿 —— 组策略/别的东西可能把值顶回去。
$afterMonitor = Get-Timeout $SUB_VIDEO $ID_VIDEOIDLE
$afterSleep   = Get-Timeout $SUB_SLEEP $ID_STANDBY
Write-Line '复核（回读实际值）：'
Write-Line ("  关闭显示器 : AC {0} / DC {1}" -f (Format-Timeout $afterMonitor.Ac), (Format-Timeout $afterMonitor.Dc))
Write-Line ("  睡眠       : AC {0} / DC {1}" -f (Format-Timeout $afterSleep.Ac), (Format-Timeout $afterSleep.Dc))
Write-Line ("  屏保       : {0}" -f $(if (Test-ScreenSaverOff) { '已关闭' } else { '仍是开的（可能被策略顶回）' }))
Write-Line ''

$bad = @()
if ($afterMonitor.Ac -ne 0) { $bad += '关闭显示器(AC)' }
if ($afterMonitor.Dc -ne 0) { $bad += '关闭显示器(DC)' }
if ($afterSleep.Ac -ne 0)   { $bad += '睡眠(AC)' }
if ($afterSleep.Dc -ne 0)   { $bad += '睡眠(DC)' }
if (-not (Test-ScreenSaverOff)) { $bad += '屏保' }

if ($bad.Count -eq 0) {
    Write-Line '[OK] 本机已常亮：不会息屏、不会睡眠、屏保已关。'
    Write-Line ("还原：powershell -NoProfile -ExecutionPolicy Bypass -File tools\set_keep_awake.ps1 -Restore")
    exit 0
}

Write-Line ("[FAIL] 这几项没生效：{0}" -f ($bad -join '、'))
if ($script:failures.Count -gt 0) {
    Write-Line '       powercfg 命令本身失败了（上面有明细）。'
}
if (-not $isAdmin) {
    Write-Line '       当前不是管理员 —— 用管理员 PowerShell 重跑本脚本。'
    exit 2
}
Write-Line '       是管理员仍失败：多半是组策略/第三方电源管理软件在管这台机器，找 IT 处理。'
exit 1
