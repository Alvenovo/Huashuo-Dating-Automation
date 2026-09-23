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
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
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
SW_SHOW = 5
SW_RESTORE = 9


def _main_window_hwnd(pid: int, *, visible_only: bool = True) -> int:
    """该进程**面积最大的顶层窗**（大厅主窗）。没有则 0。

    取最大面积而不是第一个：大厅会同时存在若干顶层窗（隐藏的、工具窗），
    主窗是最大的那个。

    `visible_only=False` 用来在「找不到可见窗口」时**继续往下查**：
    窗口可能是被隐藏了（缩到托盘），那种状态下 WebView2 不渲染，
    报错要说清是哪一种，别只报一句"找不到"。
    """
    best, best_area = 0, 0
    for hwnd in _top_hwnds():
        if _hwnd_pid(hwnd) != pid:
            continue
        if visible_only and not _hwnd_visible(hwnd):
            continue
        left, top, right, bottom = _hwnd_rect(hwnd)
        area = max(0, right - left) * max(0, bottom - top)
        if area > best_area:
            best, best_area = hwnd, area
    return best


def ensure_window_shown(hwnd: int) -> str:
    """确保窗口**真的可见**；返回「修了什么」（本来就正常则返回空串）。

    ## 为什么要它（2026-09-23 真机）

    大厅主内容区是 **WebView2**，**窗口不可见时 Chromium 不渲染** ——
    依赖页面的断言（列表刷出来 / 进已登录态）永远等不到。
    而 **UIA 照样找得到隐藏 / 最小化的窗口**，所以 `wait_main_window` 会"成功"、
    测试继续往下跑，最后报成「列表没刷出来」这种**看起来像产品缺陷**的错。

    真机表现：`test_sync_list_logged_in` 等 44s 后报「列表没刷出来」，
    失败截图里**大厅整个不在屏幕上**（只有桌面 + 终端）。

    **只在真的不正常时才动手**（正常时返回空串、不碰窗口）——
    不抢焦点、不改正常路径的行为。
    """
    if not hwnd:
        return ""
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, SW_RESTORE)
        return "大厅窗口原来是最小化，已还原"
    if not _hwnd_visible(hwnd):
        _user32.ShowWindow(hwnd, SW_SHOW)
        return "大厅窗口原来不可见（被隐藏 / 缩到托盘），已显示"
    return ""


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
    """大厅「看不见 / 被挡住」时给一句**可执行**的提示；一切正常返回空串。

    覆盖四种状态（**后两种是 2026-09-23 真机补上的** —— 当时只判了遮挡，
    而实际发生的是「窗口根本不在屏幕上」，于是提示返回空、把真因藏了）：

    | 状态 | 提示 |
    | --- | --- |
    | 主窗可见且在最上 | 空串（不打扰） |
    | 主窗被别的进程的窗口盖住 | 报出是哪个窗口 |
    | 主窗**最小化** | 说清是最小化 |
    | 主窗**不可见**（隐藏 / 缩到托盘） | 说清是不可见 |
    | **找不到任何顶层窗** | 说可能是进程已退出 |

    **不抛异常、不改变任何行为** —— 只在已经失败时提供排查方向。
    """
    hwnd = _main_window_hwnd(pid)
    if not hwnd:
        # 没有**可见**的顶层窗。往下细分，别只报一句"找不到"——
        # 2026-09-23 真机就栽在这儿：主窗被隐藏了，提示返回空，于是
        # `test_sync_list_logged_in` 报「列表没刷出来」看着像产品缺陷。
        hidden = _main_window_hwnd(pid, visible_only=False)
        if not hidden:
            return (
                "找不到大厅的任何顶层窗口 —— 进程可能已经退出。"
                "依赖页面的断言在那种状态下只会等超时。"
            )
        if _user32.IsIconic(hidden):
            return (
                "大厅窗口处于**最小化**状态 —— WebView2 不渲染，"
                "依赖页面的断言会一直等不到。请把窗口还原后再跑。"
            )
        return (
            "大厅窗口**不可见**（被隐藏 / 缩到托盘）—— WebView2 不渲染，"
            "依赖页面的断言会一直等不到（而 UIA 照样找得到控件，"
            "所以启动阶段不会报错，只会在断言处超时）。"
            "请把窗口显示出来后再跑。"
        )
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
