<#
.SYNOPSIS
    共享盘 ACL 自检 —— 找出 C:\hall-packages 下"节点账号读不到"的文件。

.DESCRIPTION
    为什么需要它（2026-09-21 真机实测踩过，别再重踩）：
    共享盘裸仓库的 pack 文件若是从本地目录硬链接 / 搬过来的，会连**本地目录的 ACL** 一起带过来
    —— 只剩 Administrators / SYSTEM / <本机用户>，**没有 hallshare**。
    共享宿主自己就是管理员，读得毫无问题；节点以 hallshare 连过来却读不了，
    而 git 的报错是：
        fatal: failed to copy file to '<桌面上的目标路径>': Permission denied
    **指向桌面上的目标文件**，完全看不出真因在共享盘那一侧。

    这就是「本机跑得过 ≠ 新机跑得过」的又一例 —— 所以**发新机之前必须跑这个**。

.PARAMETER Root
    共享盘根目录，默认 C:\hall-packages。

.PARAMETER Account
    节点连接共享盘用的账号，默认 hallshare。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\check_share_acl.ps1

.NOTES
    退出码：0 = 全部可读；1 = 有文件缺 ACE（明细打到 stdout）；2 = 路径不存在。
#>
[CmdletBinding()]
param(
    [string]$Root    = 'C:\hall-packages',
    [string]$Account = 'hallshare'
)

if (-not (Test-Path -LiteralPath $Root)) {
    Write-Output "[share-acl] 路径不存在：$Root"
    exit 2
}

$missing = New-Object System.Collections.Generic.List[string]
$total = 0

Get-ChildItem -LiteralPath $Root -Recurse -Force -File -ErrorAction SilentlyContinue | ForEach-Object {
    $total++
    try {
        $acl = Get-Acl -LiteralPath $_.FullName -ErrorAction Stop
        $has = $false
        foreach ($ace in $acl.Access) {
            if ($ace.IdentityReference.Value -like "*$Account*") { $has = $true; break }
        }
        if (-not $has) { $missing.Add($_.FullName) }
    } catch {
        $missing.Add("(ACL 读取失败) " + $_.FullName)
    }
}

Write-Output "[share-acl] 扫描 $total 个文件；账号 '$Account' 读不到的：$($missing.Count)"

if ($missing.Count -eq 0) {
    Write-Output "[share-acl] 全部可读。"
    exit 0
}

Write-Output ""
Write-Output "缺 ACE 的文件："
foreach ($p in $missing) { Write-Output "  $p" }
Write-Output ""
Write-Output "修法（让子项重新继承父目录的权限，父目录上本来就有 hallshare 授权）："
Write-Output "  icacls `"$Root\repo`" /reset /T /C"
Write-Output "  icacls `"$Root\wheelhouse`" /reset /T /C"
Write-Output "  icacls `"$Root\hall-farm`" /reset /T /C"
Write-Output ""
Write-Output "修完复跑本脚本，必须看到「读不到的：0」。"
Write-Output ""
Write-Output "⚠️ 千万别对 `"$Root`" 本身跑 /reset —— 那会把根目录上那条**显式的** hallshare 授权"
Write-Output "   一并抹掉（变成继承 C:\ 的权限），节点从此整个共享盘都进不去。"
Write-Output ""
Write-Output "为什么会有这种文件：裸仓库若是用 `git clone --bare`（默认走硬链接）建的，"
Write-Output "pack 文件会硬链接到本地仓库的 inode，ACL 也跟着是本地那套。"
Write-Output "要建新裸仓库请用 `git clone --bare --no-hardlinks`，建完跑一遍本脚本。"
exit 1
