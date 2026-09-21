# 从共享盘静默安装 Git for Windows —— 「零依赖入口」：不依赖仓库、不依赖 Python、不依赖外网。
#
# 编码：本文件**必须**存成 UTF-8 with BOM（utf-8-sig）。理由同 bootstrap_machine.ps1 ——
# PowerShell 5.1 会把**无 BOM** 的 UTF-8 当 ANSI 读，中文报错变乱码。改完务必确认 BOM 还在。
#
# 为什么需要这个脚本（引导悖论）：
#   按《项目知识库/多机代码分发.md》，新测试机拿代码走 `git clone` 共享盘裸仓库。
#   但 bootstrap 要先有代码才能跑 —— 所以「装 Git」必须发生在拿到代码**之前**，
#   不能只做在 bootstrap 里。它就是这个不依赖任何东西的第 0 层入口。
#   复杂度与手册路线 B（一行 PowerShell 下 zip）相当，但**不需要外网、不需要令牌**。
#
# 用法（新测试机，普通 PowerShell 即可；安装程序自己会要管理员）：
#   & "\\LAPTOP-VS5F7HF4\hall-packages\repo\install_git.ps1"
#
# 退出码：
#   0  成功，或本来就装了（跳过）
#   2  找不到安装包（共享盘没连上）
#   3  安装包 SHA-256 校验失败
#   4  安装程序起不来 / 返回非 0
#   5  安装程序返回 0 但 git.exe 不在预期位置
#   9  未预期的异常

param(
    [switch]$Force,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'

# === 钉住的版本与校验和：改版本时这两处必须同时改 ===
# 校验和取自官方 release 说明：
#   github.com/git-for-windows/git/releases/tag/v2.55.0.windows.5
# 钉版本的理由同 7-Zip 夹具：无外网机器只能靠共享盘这一份，必须可复现、可校验。
$GitVersion      = '2.55.0.5'
$InstallerName   = "Git-$GitVersion-64-bit.exe"
$InstallerSha256 = 'D065A4E23C3D9A6B5073D609B5BE0830227EC3CA053C083BA385061DDFAF94C6'
$InstallerPath   = Join-Path $PSScriptRoot $InstallerName
# 用 API 取 Program Files，**不要用 `$env:ProgramFiles`**：后者在某些受限会话里是空的，
# 会让 Join-Path 抛一句「无法将参数绑定到参数"Path"，因为该参数是空值」——
# 完全看不出根因，而且发生在做任何事之前。（2026-09-21 沙箱验收实测踩到）
$ProgramFiles    = [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)
$DefaultGitExe   = Join-Path $ProgramFiles 'Git\cmd\git.exe'
$StagingDir      = $null

function Say([string]$Msg) {
    if (-not $Quiet) { Write-Host "[git-setup] $Msg" }
}

function Fail([int]$Code, [string]$Msg) {
    Write-Host ""
    Write-Host "[git-setup] 失败：$Msg" -ForegroundColor Red
    Write-Host ""
    exit $Code
}

try {
    Say "Git for Windows 安装器（钉住版本 $GitVersion）"

    # --- 1. 已经装了？----------------------------------------------------
    if (-not $Force) {
        $cmd = Get-Command git -ErrorAction SilentlyContinue
        if ($cmd) {
            Say "已装 Git（$(& $cmd.Source --version)），跳过。要强制重装加 -Force。"
            exit 0
        }
        # 装了但当前窗口的 PATH 还没刷新 —— 很常见，别白装一遍
        if (Test-Path $DefaultGitExe) {
            Say "默认位置已有 Git（$DefaultGitExe），但当前窗口 PATH 里没有。"
            Say "不用重装：**新开一个 PowerShell 窗口**再试。"
            exit 0
        }
    }

    # --- 2. 安装包在不在 --------------------------------------------------
    # 共享盘**没认证**时（新机器默认如此），Test-Path 会抛「拒绝访问」/「找不到网络路径」——
    # 那是没认证，不是包丢了。这里兜住，统一按 rc=2 报，并把那条 net use 直接给出来。
    # 刻意不让人去文件管理器里试：那儿会弹凭据框，输成别的账号会留下 1219 多重连接冲突。
    $pkgErr = ""
    try {
        $pkgThere = Test-Path -LiteralPath $InstallerPath
    } catch {
        $pkgThere = $false
        $pkgErr = $_.Exception.Message
    }
    if (-not $pkgThere) {
        Fail 2 ("找不到安装包：`n  $InstallerPath`n" +
                $(if ($pkgErr) { "读取时报错：$pkgErr`n" } else { "" }) +
                "最常见的原因：**共享盘没认证**（新机器默认连不上）。先在 PowerShell 里连一次：`n" +
                "  net use \\LAPTOP-VS5F7HF4\hall-packages /user:hallshare `"<共享盘密码>`" /persistent:yes`n" +
                "能 `dir \\LAPTOP-VS5F7HF4\hall-packages\repo` 列出文件，再重跑本脚本。`n" +
                "仍不行：确认包源机开着、能 ping 通、445 已放行。")
    }

    # --- 3. 先拷到本地再校验 ---------------------------------------------
    # 为什么不直接在共享盘上跑：Start-Process 的 UNC 路径有坑（工作目录不能是 UNC），
    # 而且从网络位置跑安装程序还会撞上 SmartScreen / 执行策略。拷到 %TEMP% 一次，麻烦全没。
    $StagingDir = Join-Path $env:TEMP ("git-setup-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $StagingDir -Force | Out-Null
    $localExe = Join-Path $StagingDir $InstallerName
    Say "拷贝安装包到本地临时目录 ..."
    Copy-Item -LiteralPath $InstallerPath -Destination $localExe -Force

    Say "校验 SHA-256 ..."
    $actual = (Get-FileHash -Algorithm SHA256 -Path $localExe).Hash
    if ($actual -ne $InstallerSha256) {
        Fail 3 ("安装包校验失败，**拒绝执行**。`n" +
                "  期望：$InstallerSha256`n" +
                "  实际：$actual`n" +
                "包被改过，或者下载/拷贝中途断了。从共享盘重新取一份。")
    }
    Say "校验通过。"

    # --- 4. 静默安装 ------------------------------------------------------
    # 不写 /COMPONENTS：默认组件已含「Git from the command line and also from 3rd-party
    # software」（即写进 PATH），正是我们要的。少写一个开关就少一个踩坑点。
    Say "开始静默安装（约 1 分钟，窗口无输出是正常的）..."
    $installArgs = @('/VERYSILENT', '/NORESTART', '/NOCANCEL', '/SP-', '/SUPPRESSMSGBOXES')
    try {
        $proc = Start-Process -FilePath $localExe -ArgumentList $installArgs -Wait -PassThru
    } catch {
        Fail 4 "安装程序起不来：$($_.Exception.Message)"
    }
    # Inno Setup 偶有「父进程先退、子进程接着装」的情况，ExitCode 会是 $null。
    # 所以这里只在**拿到非 0 退出码**时判失败，真正的裁决交给下面第 5 步的路径检查。
    if ($null -ne $proc.ExitCode -and $proc.ExitCode -ne 0) {
        Fail 4 ("安装程序返回退出码 $($proc.ExitCode)。`n" +
                "常见原因：没给管理员权限、被杀软拦下、磁盘满。")
    }

    # --- 5. 验证 ----------------------------------------------------------
    # 安装程序改了系统 PATH，但**当前窗口的 PATH 不会自动刷新**，
    # 所以这里用绝对路径验证，不去查 PATH。
    if (-not (Test-Path -LiteralPath $DefaultGitExe)) {
        Fail 5 ("安装程序返回 0，但 $DefaultGitExe 不存在。`n" +
                "装到别的位置去了，或者被杀软删了。手工确认 Git 装在哪。")
    }
    $ver = & $DefaultGitExe --version
    Say "完成：$ver"
    Say "当前窗口还没刷新 PATH，先这样用：& `"$DefaultGitExe`" --version"
    Say "**新开一个 PowerShell 窗口**，git 才直接在 PATH 上。"
    exit 0
}
finally {
    if ($StagingDir -and (Test-Path -LiteralPath $StagingDir)) {
        Remove-Item -LiteralPath $StagingDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
