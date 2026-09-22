"""探针：本机杀软会不会拦「刚写出来的文件」（2026-09-21 定位 Errno 22 用的那个）。

## 为什么留这个脚本

`tests/unit` 有 4 条共享盘拷贝用例偶发报 `OSError: [Errno 22] Invalid argument`，
失败点是读**本进程刚 `write_bytes` 写出来的**临时文件。
当时查了两轮都以为是"安全代理/过滤驱动"，因为 Python 给的线索是死的：

    OSError.errno == 22   ->  "Invalid argument"，看着像参数写错了
    OSError.winerror      ->  None

**真码在 Win32 层**：直接用 `CreateFileW` 复现一次，`GetLastError()` 给出的是
**225 = ERROR_VIRUS_INFECTED**（"文件含病毒，操作被阻止"）。Python 的 errno 表里
没有 225，才映射成 EINVAL。再查 Defender 自己的记录，拦的就是探针刚写的那些文件：

    Get-MpThreat -> 2147731250 = Trojan:Win32/Bearfoos.A!ml   SeverityID=5

约 14 秒后文件被隔离删除（再探变成 `GetLastError=2`）。

## 用法（只读，不碰仓库、不碰你的文件）

    .venv\\Scripts\\python.exe -X utf8 tools\\probe_av_quarantine.py

只在自己的 `%TEMP%` 下建几个临时文件，然后读它们。

（放在 `tools/` 而不是 `reports/probe/`：后者整个目录被 `.gitignore` 忽略，
clone 到新机器上那条命令会落空 —— 而文档正是叫新机器的人去跑它。）

## 怎么读结论

**只看 `[1]` 段**（下面 `Get-MpThreat*` 是历史账本，机器好了旧记录也照样躺着）：

- `[1]` 出现 `225(ERROR_VIRUS_INFECTED)@0.0s` -> **现在**会拦刚写出的文件，跟代码无关。
  顺序：① **先更新病毒库**（`Update-MpSignature`）→ 复探；② 仍是 225 才关实时保护 /
  加排除项。误报跟着**病毒库版本**走：2026-09-21 那批库误报，22:07 更新后自己就好了。
- `[1]` 全是 `0(OK)@0.0s` -> 本机现在没这个问题。**别关杀软**；用例真红了要去查代码。
- 根治办法（加 Defender 排除项）与完整链路见 `项目知识库/运行手册.md`「坑 6」。
  ⚠️ 加排除项是**安全策略变更**，要人拍板，别在跑批机器上随手加。
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
FILE_SHARE_ALL = 0x1 | 0x2 | 0x4

WINERROR_HINT = {
    0: "OK",
    2: "ERROR_FILE_NOT_FOUND —— 文件已经不在了（多半被隔离删除）",
    5: "ERROR_ACCESS_DENIED —— 瞬时句柄锁或真 ACL 问题",
    32: "ERROR_SHARING_VIOLATION —— 别的进程占着",
    87: "ERROR_INVALID_PARAMETER",
    225: "ERROR_VIRUS_INFECTED —— **杀软判定为病毒，读写全被拦**",
}


def _ps(script: str, tag: str) -> None:
    """跑一段 PowerShell 并打印。**走字节 + cp936 兜底**，不靠 text=True。"""
    proc = subprocess.run([PS, "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True)
    raw = (proc.stdout or b"") + (proc.stderr or b"")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp936", errors="replace")
    print(f"--- {tag} (rc={proc.returncode}) ---")
    print(text.strip()[:1200] or "(空)")


def win32_read_code(path: Path) -> int:
    """只读方式开一次，返回 Win32 错误码（0 = 成功）。"""
    handle = _k32.CreateFileW(str(path), GENERIC_READ, FILE_SHARE_ALL, None,
                              OPEN_EXISTING, 0, None)
    if handle in (-1, 0xFFFFFFFFFFFFFFFF):
        return ctypes.get_last_error()
    _k32.CloseHandle(handle)
    return 0


def probe_matrix(ext: str, wait_sec: float = 20.0, step: float = 1.0) -> set[int]:
    """新建一个文件，然后一直读到能读为止 —— 看它多久被判毒/删掉。

    **返回本次亲眼看到的错误码集合**，判读只认它。
    下面 `Get-MpThreat*` 那些是**历史账本**：机器早就好了，旧记录也照样躺着，
    拿它们当结论会把「现在没问题」误报成「本机杀软误杀」——2026-09-22 复测就踩了这个。
    """
    folder = Path(tempfile.mkdtemp(prefix="av-probe-"))
    path = folder / f"payload{ext}"
    path.write_bytes(b"from-share")  # 内容无关：纯文本照样中招
    start = time.monotonic()
    seen: dict[int, float] = {}
    while True:
        code = win32_read_code(path)
        seen.setdefault(code, round(time.monotonic() - start, 2))
        if code == 0 or time.monotonic() - start > wait_sec:
            break
        time.sleep(step)
    shown = ", ".join(f"{c}({WINERROR_HINT.get(c, '?')})@{t}s" for c, t in seen.items())
    print(f"  {ext or '(无扩展名)'}: {shown}")
    return set(seen)


def main() -> int:
    print("python:", sys.version.split()[0])
    print("管理员:", bool(ctypes.windll.shell32.IsUserAnAdmin()))
    print()
    print("[1] 新建文件后立刻读，最多等 20 秒（每 1 秒探一次）——**结论只看这一段**")
    live: set[int] = set()
    for ext in (".exe", ".txt"):
        live |= probe_matrix(ext)
    blocked = 225 in live
    print()
    _ps("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "Get-MpThreatDetection | Sort-Object InitialDetectionTime -Descending | "
        "Select-Object -First 5 InitialDetectionTime,"
        "@{n='res';e={$_.Resources -join ';'}} | Format-List | Out-String -Width 200",
        "Defender 历史拦截记录（**只是历史**，不代表现在还会拦）")
    _ps("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "Get-MpThreat | Select-Object ThreatID,ThreatName,SeverityID | "
        "Format-Table -AutoSize | Out-String -Width 200",
        "威胁名（2147731250 = Trojan:Win32/Bearfoos.A!ml 就是它）")
    print()
    if blocked:
        print("判读：**本机现在就会拦**（[1] 段亲眼看到 225）—— 见 运行手册「坑 6」。")
        print("     顺序：① 先更新病毒库（管理员 PowerShell `Update-MpSignature`，")
        print("             或安全中心「检查更新」）→ 复跑本探针；")
        print("           ② 更新后仍报 225，才关实时保护 / 加 Defender 排除项。")
        print("     别一上来就关杀软：误报是**病毒库**的问题，换库比关防护便宜。")
    else:
        print("判读：**本机现在不拦**（[1] 段全是 0(OK)）—— 什么都别动，直接往下跑。")
        print("     上面那些检出记录是**过去的**，躺着不代表现在还会拦 ——")
        print("     误报跟着病毒库版本走（2026-09-21 那批库误报，22:07 更新后不再复现）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
