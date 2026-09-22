"""把终端窗口抢到前台 —— 人在环收码时不用手动 Alt+Tab 找回来。

## 为什么需要

人在环用例（`login-manual`）的节奏是「脚本点发送验证码 → 人去收码 → 回终端输码」。
脚本操作大厅时会把大厅窗口顶到前台，**终端被压在后面** —— 每收一个码都要切一次窗口。
点完「发送验证码」自动把终端提到最前，人只管收码、输码。

## 为什么不能直接 SetForegroundWindow

Windows 有**前台锁**：不是当前前台进程的进程调 `SetForegroundWindow`，
系统只让任务栏图标闪一下，窗口不会真的到前面。标准绕法是 `AttachThreadInput` ——
把自己的线程临时挂到「当前前台窗口」的输入线程上，借它的前台资格，调完再摘掉。
本模块就是这么做的（`_force_foreground`）。

## 找终端窗口的三条路（按可靠性排序）

1. `GetConsoleWindow()` —— 经典 conhost 直接给句柄。
   ⚠️ Windows Terminal 下它返回的是**隐藏的伪控制台窗**，所以必须先判 `IsWindowVisible`。
2. `GetConsoleTitleW()` + `FindWindowW()` —— Windows Terminal 的窗口标题就是
   当前标签页标题，通常等于控制台标题；conhost 同理。这条同时覆盖两者。
3. 沿**父进程链**往上走，`EnumWindows` 找那些 pid 名下的可见顶层窗 ——
   前两条都失效时的兜底（终端被包在 VS Code / 别的壳里时就是这种情况）。

三条都失败返回 `False`。**调用方不该因此失败** —— 这只是省一次 Alt+Tab 的便利，
不是用例的前置条件，也不是任何断言的一部分。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

__all__ = ["focus_console"]

_SW_RESTORE = 9
_TH32CS_SNAPPROCESS = 0x00000002

# 爬到这些进程就停：它们之上不再是"终端"，而且名下挂着桌面/任务栏窗口。
_SHELL_EXES = frozenset({"explorer.exe"})


def _user32():
    return ctypes.WinDLL("user32", use_last_error=True)


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _console_hwnd() -> int:
    """经典 conhost 的窗口句柄；Windows Terminal 下拿到的是隐藏窗，判可见性后丢弃。"""
    hwnd = _kernel32().GetConsoleWindow()
    if hwnd and _user32().IsWindowVisible(hwnd):
        return int(hwnd)
    return 0


def _console_title_hwnd() -> int:
    """按控制台标题找顶层窗 —— 这条同时覆盖 conhost 与 Windows Terminal。"""
    buf = ctypes.create_unicode_buffer(512)
    if not _kernel32().GetConsoleTitleW(buf, 512):
        return 0
    title = buf.value.strip()
    if not title:
        return 0
    return int(_user32().FindWindowW(None, title) or 0)


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _process_table() -> dict[int, tuple[int, str]]:
    """`{pid: (父 pid, exe 名)}` —— 一次快照全拿，避免每爬一层都重扫。

    拿不到就返回空表（调用方据此停住，不会死循环）。
    """
    k = _kernel32()
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    k.CloseHandle.argtypes = [wintypes.HANDLE]

    snap = k.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        return {}
    table: dict[int, tuple[int, str]] = {}
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not k.Process32FirstW(snap, ctypes.byref(entry)):
            return {}
        while True:
            table[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), entry.szExeFile)
            if not k.Process32NextW(snap, ctypes.byref(entry)):
                break
    finally:
        k.CloseHandle(snap)
    return table


def _ancestor_pids() -> list[int]:
    """本进程 → 父 → 祖父 …，**遇到桌面壳就停**。最多 8 层，防环。

    遇到 `explorer.exe` 必须停：它再往上是 `services.exe` 那类，而且 explorer
    名下有桌面/任务栏窗口 —— 接着爬只会找到"把桌面提到前台"那种错窗口。
    """
    table = _process_table()
    chain: list[int] = []
    pid = int(_kernel32().GetCurrentProcessId())
    for _ in range(8):
        entry = table.get(pid)
        if entry is None:
            break
        parent = entry[0]
        if not parent or parent == pid or parent in chain:
            break
        if table.get(parent, (0, ""))[1].lower() in _SHELL_EXES:
            break
        chain.append(parent)
        pid = parent
    return chain


# 桌面 / 任务栏这些"壳窗口"必须排除。`explorer.exe` 也在祖先进程链里，
# 不排掉就会把「Program Manager」（桌面）提到前台 —— 效果是**把所有窗口最小化**，
# 比不聚焦还糟。
_SHELL_WINDOW_CLASSES = frozenset(
    {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "SysShadow"}
)


def _window_class(hwnd: int) -> str:
    u = _user32()
    u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    buf = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _ancestor_window() -> int:
    """祖先进程名下的第一个可见顶层窗，**从最近的祖先开始找**。

    顺序不能反：链尾是 `explorer.exe`，它名下的桌面窗口也是"可见顶层窗"，
    先扫到它就会把桌面提到前台。近的那个才是终端。
    """
    u = _user32()
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    for pid in _ancestor_pids():
        found: list[int] = []

        def _cb(hwnd, _lparam, _pid=pid, _found=found):
            if not u.IsWindowVisible(hwnd):
                return True
            own = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(own))
            if own.value != _pid:
                return True
            if _window_class(hwnd) in _SHELL_WINDOW_CLASSES:
                return True
            if not u.GetWindowTextLengthW(hwnd):
                return True  # 没标题的多半是隐藏宿主窗，不是终端
            _found.append(int(hwnd))
            return False

        u.EnumWindows(proc(_cb), 0)
        if found:
            return found[0]
    return 0


def _force_foreground(hwnd: int) -> bool:
    """把 hwnd 提到最前，绕开 Windows 的前台锁。

    做法：把自己的线程挂到当前前台窗口的输入线程上（`AttachThreadInput`），
    这样系统认为"是前台自己在切"，`SetForegroundWindow` 才会真的生效；
    调完立刻摘掉，免得把两个线程的输入队列长期绑在一起。
    """
    u = _user32()
    k = _kernel32()
    u.GetForegroundWindow.restype = wintypes.HWND
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.SetForegroundWindow.restype = wintypes.BOOL
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.BringWindowToTop.argtypes = [wintypes.HWND]
    u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    k.GetCurrentThreadId.restype = wintypes.DWORD

    if int(u.GetForegroundWindow() or 0) == hwnd:
        return True

    cur_tid = k.GetCurrentThreadId()
    fg = int(u.GetForegroundWindow() or 0)
    attached: list[int] = []
    for target in {int(u.GetWindowThreadProcessId(fg, None)) if fg else 0,
                   int(u.GetWindowThreadProcessId(hwnd, None))}:
        if target and target != cur_tid and u.AttachThreadInput(cur_tid, target, True):
            attached.append(target)
    try:
        u.ShowWindow(hwnd, _SW_RESTORE)
        u.BringWindowToTop(hwnd)
        if u.SetForegroundWindow(hwnd):
            return True
        # 前台锁没绕过去时兜一下：SetFocus 改不了 Z 序，但至少让键盘有落点
        return int(u.GetForegroundWindow() or 0) == hwnd
    finally:
        for tid in attached:
            u.AttachThreadInput(cur_tid, tid, False)


def focus_console() -> bool:
    """把跑测试的终端提到最前。成功返回 True；**任何情况下都不抛异常**。

    不抛是刻意的：这是"省一次 Alt+Tab"的便利，不是用例前置条件。
    真拿不到窗口（终端类型没覆盖到、被别的进程抢走）就静默返回 False，
    人照旧手动切一下即可，用例该过还得过。
    """
    if sys.platform != "win32":
        return False
    for finder in (_console_hwnd, _console_title_hwnd, _ancestor_window):
        try:
            hwnd = finder()
        except Exception:
            hwnd = 0
        if not hwnd:
            continue
        try:
            if _force_foreground(hwnd):
                return True
        except Exception:
            continue
    return False
