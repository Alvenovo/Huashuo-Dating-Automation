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
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.WindowFromPoint.argtypes = [wintypes.POINT]
_user32.WindowFromPoint.restype = wintypes.HWND
_user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetAncestor.restype = wintypes.HWND
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


# ---------------------------------------------------------------- 遮挡判定
#
# 2026-09-23 真机：两条用例（同步页列表 / 微软登录）同时超时，截图**一模一样** ——
# 微信在前台、大厅一个像素都没露。大厅主内容区是 **WebView2**，窗口被完全遮挡时
# Chromium 会**暂停渲染**，依赖页面的断言永远等不到；而 Win32 那层（按钮、标题）
# 照样读得到 —— 现象是「**能读、但页面是空的**」，报告上看着像产品缺陷。
#
# 人得逐张看 PNG 截图才能定性。这里把「谁挡着」变成失败信息里的一行。

GA_ROOT = 2


def _main_window_hwnd(pid: int) -> int:
    """该进程**面积最大的可见顶层窗**（大厅主窗）。没有则 0。

    取最大面积而不是第一个：大厅会同时存在若干顶层窗（隐藏的、工具窗），
    主窗是最大的那个。
    """
    best, best_area = 0, 0
    for hwnd in _top_hwnds():
        if _hwnd_pid(hwnd) != pid or not _hwnd_visible(hwnd):
            continue
        left, top, right, bottom = _hwnd_rect(hwnd)
        area = max(0, right - left) * max(0, bottom - top)
        if area > best_area:
            best, best_area = hwnd, area
    return best


def occluders_of(hwnd: int, samples: int = 5) -> list[str]:
    """挡在这个窗口前面的、**属于别的进程**的窗口标题（去重保序）。空 = 没被挡。

    ⚠️ **必须按 pid 过滤**：大厅自己的登录弹窗 / 绑定弹窗都是**独立顶层窗**，
    它们盖在主窗上时 `WindowFromPoint` 也会指到它们。不过滤的话，
    「弹窗正常打开」会被误报成「大厅被遮挡」—— 那正是这些用例的常规状态。
    """
    left, top, right, bottom = _hwnd_rect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return []
    points = [(left + width // 2, top + height // 2)]
    if samples >= 3:
        points += [
            (left + width // 4, top + height // 4),
            (right - width // 4, bottom - height // 4),
        ]
    if samples >= 5:
        points += [
            (right - width // 4, top + height // 4),
            (left + width // 4, bottom - height // 4),
        ]
    pid = _hwnd_pid(hwnd)
    out: list[str] = []
    for x, y in points:
        at = int(_user32.WindowFromPoint(wintypes.POINT(x, y)) or 0)
        if not at:
            continue
        root = int(_user32.GetAncestor(at, GA_ROOT) or at)
        if root == hwnd or _hwnd_pid(root) == pid:
            continue
        title = _hwnd_text(root) or _hwnd_class(root) or f"hwnd={root}"
        if title not in out:
            out.append(title)
    return out


def occlusion_hint(pid: int) -> str:
    """大厅被别的窗口挡住 / 最小化时，给一句**可执行**的提示；没事返回空串。

    **不抛异常、不改变任何行为** —— 只在已经失败时提供排查方向，调用方拼进报错里即可。
    没被挡就返回空串，不会给报错加噪音。
    """
    hwnd = _main_window_hwnd(pid)
    if not hwnd:
        return ""
    if _user32.IsIconic(hwnd):
        return (
            "大厅窗口处于**最小化**状态 —— WebView2 不渲染，"
            "依赖页面的断言会一直等不到。请把窗口还原后再跑。"
        )
    names = occluders_of(hwnd)
    if not names:
        return ""
    listed = "、".join(f"「{n}」" for n in names[:4])
    return (
        f"大厅被别的窗口挡住了：{listed}。"
        "大厅主内容区是 WebView2，**被遮挡时 Chromium 会暂停渲染**，"
        "依赖页面的断言会一直等不到（而按钮 / 标题这类 Win32 控件照样读得到，"
        "所以表现为「能读、但页面是空的」）。"
        "处理：把这些窗口最小化或关掉，再重跑。"
    )
