"""通用 Win32 原语。

大厅和厂商安装器都要按进程枚举顶层窗口，UIA 的 Desktop().windows() 全系统枚举
实测要 1s 左右，Win32 EnumWindows + pid 过滤是 0.00s 级，所以统一走这里。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

_user32 = ctypes.windll.user32
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
_user32.EnumChildWindows.argtypes = [wintypes.HWND, _WNDENUMPROC, wintypes.LPARAM]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowEnabled.argtypes = [wintypes.HWND]
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
# SendMessageTimeoutW 必须显式声明返回值：ctypes 默认按 c_int 截断，64 位下会丢掉高位
_user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
_user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND,
    ctypes.c_uint,
    wintypes.WPARAM,
    wintypes.LPARAM,
    ctypes.c_uint,
    ctypes.c_uint,
    ctypes.POINTER(ctypes.c_void_p),
]

WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_CLOSE = 0x0010
SMTO_ABORTIFHUNG = 0x0002


def _top_hwnds() -> list[int]:
    found: list[int] = []

    def callback(hwnd, _lparam):
        found.append(int(hwnd))
        return True

    _user32.EnumWindows(_WNDENUMPROC(callback), 0)
    return found


def _child_hwnds(hwnd: int) -> list[int]:
    found: list[int] = []

    def callback(child, _lparam):
        found.append(int(child))
        return True

    _user32.EnumChildWindows(hwnd, _WNDENUMPROC(callback), 0)
    return found


def _hwnd_pid(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _hwnd_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    return buf.value if _user32.GetClassNameW(hwnd, buf, 256) else ""


def _hwnd_text(hwnd: int) -> str:
    """窗口文字。GetWindowTextW 读系统缓存，WM_GETTEXT 让控件自己交出来；

    自绘控件常常只有后者能读到，所以两个都试。
    """
    length = _user32.GetWindowTextLengthW(hwnd)
    if length > 0:
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value.strip():
            return buf.value
    result = ctypes.c_void_p()
    sent = _user32.SendMessageTimeoutW(
        hwnd, WM_GETTEXTLENGTH, 0, 0, SMTO_ABORTIFHUNG, 500, ctypes.byref(result)
    )
    size = int(result.value or 0)
    if not sent or size <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(size + 1)
    got = _user32.SendMessageTimeoutW(
        hwnd, WM_GETTEXT, size + 1, ctypes.addressof(buf), SMTO_ABORTIFHUNG, 500,
        ctypes.byref(result),
    )
    return buf.value if got else ""


def _hwnd_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return (0, 0, 0, 0)
    return (rect.left, rect.top, rect.right, rect.bottom)


def _hwnd_alive(hwnd: int) -> bool:
    return bool(_user32.IsWindowVisible(hwnd)) or _hwnd_class(hwnd) != ""


def _hwnd_visible(hwnd: int) -> bool:
    return bool(_user32.IsWindowVisible(hwnd))


def _close_window(hwnd: int) -> None:
    """给顶层窗发 WM_CLOSE，等同于点它的关闭按钮；不销毁别进程的窗口。"""
    _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
