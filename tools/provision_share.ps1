# Provision the SMB shares the multi-machine farm needs. MUST run ELEVATED.
# ASCII only on purpose: PowerShell 5.1 misreads BOM-less UTF-8 as ANSI and collapses lines.
#
# WHY THIS SCRIPT EXISTS
#   Creating an SMB share is a kernel-level privileged operation. A non-elevated process
#   gets "System error 5. Access denied" and there is no way around it from code --
#   UAC's consent prompt is drawn on the secure desktop, which a non-elevated process
#   cannot reach. So share creation can never be fully automated; it needs ONE human
#   press of the elevation button. This script makes that one press cover everything:
#   idempotent, re-runnable, and it verifies its own work at the end.
#
# TWO SHARES, TWO DIFFERENT JOBS
#   hall-packages : READ-only for nodes. Holds the myappstore setup exes.
#   hall-farm     : CHANGE for nodes. This is the control channel -- nodes rename tasks
#                   out of tasks/, write receipts to done/, push evidence to results/
#                   and logs. Read-only here would make every node fail silently.
#
# Usage (Administrator PowerShell):
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\provision_share.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\provision_share.ps1 -SkipPackages
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\provision_share.ps1 -ShareUser "hallshare" -SharePassword "xxx"
#
# Exit codes: 0 = ok, 2 = not elevated, 1 = something failed.
param(
    [string]$PackagesRoot = "C:\hall-packages",
    [string]$FarmRoot = "C:\hall-farm",
    [string]$PackagesShare = "hall-packages",
    [string]$FarmShare = "hall-farm",
    [string]$ShareUser = "hallshare",
    [string]$SharePassword = "",
    [switch]$SkipPackages,
    [switch]$SkipFirewall,
    [switch]$SkipVerify
)

# Cmdlets that quietly return nothing (Get-SmbShare / Get-LocalUser / Get-NetFirewallRule)
# are the normal "does this exist?" probe, so their non-terminating errors must not abort
# the run. Real failures are caught and reported explicitly at each call site instead.
$ErrorActionPreference = "Continue"

# ---- elevation gate: fail fast and loud, before touching anything ----
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Host "NOT ELEVATED: creating an SMB share needs an administrator token." -ForegroundColor Red
    Write-Host "Open PowerShell as Administrator, then run this script again."
    Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File tools\provision_share.ps1"
    # Write-Host output is not capturable by the caller; emit on the success stream too so
    # redirected logs (the normal automation path) actually contain the reason.
    Write-Output "RESULT: NOT_ELEVATED"
    exit 2
}

$ok = $true
function Report($name, $pass, $detail) {
    # $script: is mandatory here -- a plain `$ok = $false` inside a function creates a
    # FUNCTION-LOCAL copy, silently discarding the failure. The script would then always
    # report OK and always exit 0, which is the worst possible outcome: a false green.
    # Any failure signal must be written to the script scope explicitly.
    if (-not $pass) { $script:ok = $false }
    $tag = if ($pass) { "OK  " } else { "FAIL" }
    Write-Host "[$tag] $name"
    if ($detail) { Write-Host "        $detail" }
}

Write-Host "=== Provision SMB shares for the hall automation farm ==="
Write-Host "host      : $env:COMPUTERNAME"
Write-Host "packages  : $PackagesShare -> $PackagesRoot"
Write-Host "farm      : $FarmShare -> $FarmRoot"
Write-Host "share user: $ShareUser"
Write-Host ""

# ---- helper: does a share already exist? ----
function Test-ShareExists($name) {
    # Get-SmbShare throws when the share is absent; -ErrorAction SilentlyContinue
    # keeps that quiet and the null check does the real work.
    $s = Get-SmbShare -Name $name -ErrorAction SilentlyContinue
    return ($null -ne $s)
}

# ---- helper: ensure the share user account exists (needed for network logon) ----
# A passwordless account CANNOT do a network logon on Windows -- the failure surfaces
# as "path not found", which is deeply misleading. Hence a dedicated account with a password.
function Ensure-ShareUser() {
    $exists = $null -ne (Get-LocalUser -Name $ShareUser -ErrorAction SilentlyContinue)
    if ($exists) {
        Report "share account $ShareUser" $true "already exists, left untouched"
        return $true
    }
    if (-not $SharePassword) {
        Report "share account $ShareUser" $false "does not exist and -SharePassword was not given. Create it manually: net user $ShareUser ""<password>"" /add"
        return $false
    }
    try {
        $sec = ConvertTo-SecureString $SharePassword -AsPlainText -Force
        New-LocalUser -Name $ShareUser -Password $sec -PasswordNeverExpires -Description "SMB read/change account for the hall automation farm" | Out-Null
        Report "share account $ShareUser" $true "created"
        return $true
    } catch {
        Report "share account $ShareUser" $false "create failed: $($_.Exception.Message)"
        return $false
    }
}

# ---- helper: create a share with an explicit access right for the share user ----
function Ensure-Share($name, $path, $access) {
    if (-not (Test-Path $path)) {
        New-Item -ItemType Directory -Force -Path $path | Out-Null
        Write-Host "        created directory $path"
    }

    if (Test-ShareExists $name) {
        Report "share $name" $true "already exists -> $path (left as is)"
    } else {
        try {
            # Grant Everyone Read at the SMB layer, then hand the real right to the share
            # user below. Everyone is only reachable after an authenticated network logon,
            # so this is not an anonymous-open door.
            New-SmbShare -Name $name -Path $path -ReadAccess "Everyone" -ErrorAction Stop | Out-Null
            Report "share $name" $true "created -> $path"
        } catch {
            Report "share $name" $false "create failed: $($_.Exception.Message)"
            return $false
        }
    }

    # Always (re)assert the share user's rights -- this is what makes the script idempotent
    # and self-healing when someone hand-edits permissions.
    try {
        Grant-SmbShareAccess -Name $name -AccountName $ShareUser -AccessRight $access -Force -ErrorAction Stop | Out-Null
        Report "$name -> $ShareUser = $access" $true ""
    } catch {
        Report "$name -> $ShareUser = $access" $false "$($_.Exception.Message)"
        return $false
    }
    return $true
}

# ---- helper: NTFS permissions ----
# Share-level rights alone are not enough; the filesystem ACL must also allow the write.
# Granting on the folder (not recursively) is enough for creating children.
function Grant-Ntfs($path, $access) {
    try {
        $acl = Get-Acl $path
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $ShareUser, $access, "ContainerInherit,ObjectInherit", "None", "Allow")
        $acl.SetAccessRule($rule)
        Set-Acl -Path $path -AclObject $acl
        Report "NTFS $path -> $ShareUser = $access" $true ""
        return $true
    } catch {
        Report "NTFS $path -> $ShareUser = $access" $false "$($_.Exception.Message)"
        return $false
    }
}

# ---- helper: firewall rule for SMB (TCP 445), idempotent ----
function Ensure-Firewall() {
    $ruleName = "HallShare-SMB"
    $existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    if ($existing) {
        Report "firewall $ruleName" $true "already present (left as is)"
        return $true
    }
    try {
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP `
            -LocalPort 445 -Action Allow -Profile Private,Public -ErrorAction Stop | Out-Null
        Report "firewall $ruleName" $true "created (TCP 445 inbound, Private+Public)"
        Write-Host "        NOTE: 445 open on a public network is exposure. Remove after the run:"
        Write-Host "              Remove-NetFirewallRule -DisplayName ""$ruleName"""
        return $true
    } catch {
        Report "firewall $ruleName" $false "$($_.Exception.Message)"
        return $false
    }
}

# ---- helper: create the farm subdirectories ----
function Ensure-FarmDirs() {
    $created = @()
    foreach ($sub in @("tasks", "done", "results", "logs")) {
        $target = Join-Path $FarmRoot $sub
        if (-not (Test-Path $target)) {
            New-Item -ItemType Directory -Force -Path $target | Out-Null
            $created += $sub
        }
    }
    if ($created.Count -gt 0) {
        Report "farm subdirectories" $true "created: $($created -join ', ')"
    } else {
        Report "farm subdirectories" $true "tasks/done/results/logs all present"
    }
    return $true
}

# ---- run ----
# Everything below is wrapped so that a stray terminating error still produces a
# readable RESULT line on the success stream -- automation that silently dies is worse
# than automation that fails loudly.
$result = "OK"
try {

    # The share account must exist BEFORE we grant it rights. Missing account with no
    # password supplied is an actionable user error, not a crash.
    if (-not (Ensure-ShareUser)) {
        $result = "NO_SHARE_ACCOUNT"
        $ok = $false
    } else {

        if (-not $SkipPackages) {
            if (-not (Ensure-Share $PackagesShare $PackagesRoot "Read")) { $ok = $false }
            if (-not (Grant-Ntfs $PackagesRoot "Read")) { $ok = $false }
        }

        # hall-farm is the control channel: nodes must WRITE here, so Change not Read.
        if (-not (Ensure-Share $FarmShare $FarmRoot "Change")) { $ok = $false }
        if (-not (Grant-Ntfs $FarmRoot "Modify")) { $ok = $false }
        if (-not (Ensure-FarmDirs)) { $ok = $false }

        if (-not $SkipFirewall) {
            if (-not (Ensure-Firewall)) { $ok = $false }
        }

        # ---- verify: reach the shares over the network, the way a node will ----
        if (-not $SkipVerify) {
            Write-Host ""
            Write-Host "=== verification (network path, as a node sees it) ==="
            $uncPackages = "\\$env:COMPUTERNAME\$PackagesShare"
            $uncFarm = "\\$env:COMPUTERNAME\$FarmShare"

            foreach ($unc in @($uncPackages, $uncFarm)) {
                if ($unc -eq $uncPackages -and $SkipPackages) { continue }
                $reachable = $false
                try { $reachable = Test-Path $unc } catch { $reachable = $false }
                if ($reachable) {
                    Report "reachable $unc" $true ""
                } else {
                    Report "reachable $unc" $false "not reachable from this session. A node must run: net use $unc /user:$ShareUser ""<password>"" /persistent:yes"
                    $ok = $false
                }
            }
        }
    }
} catch {
    $result = "EXCEPTION"
    Write-Host "[FAIL] unhandled error: $($_.Exception.Message)" -ForegroundColor Red
    $ok = $false
}

# ---- summary (machine-readable line first, for the automation log) ----
Write-Output "RESULT: $result"
Write-Host ""
if ($ok) {
    Write-Host "Shares are ready." -ForegroundColor Green
} else {
    Write-Host "Finished with failures -- see the FAIL lines above." -ForegroundColor Yellow
}
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1) Point HALL_FARM_ROOT at the farm share on the control machine and every node:"
Write-Host "       `$env:HALL_FARM_ROOT = ""\\$env:COMPUTERNAME\$FarmShare"""
Write-Host "  2) Keep this machine powered on -- it is both the package source and the farm root."
Write-Host "  3) On each node, bootstrap handles the rest:"
Write-Host "       `$env:HALL_SHARE_USER=""$ShareUser""; `$env:HALL_SHARE_PASSWORD=""<password>"""
Write-Host "       powershell -NoProfile -ExecutionPolicy Bypass -File tools\bootstrap_machine.ps1"

if ($ok) { exit 0 } else { exit 1 }
