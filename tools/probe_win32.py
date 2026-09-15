"""用纯 Win32 视角看安装器向导：控件的窗口文字到底读不读得到。

13:42 那次提权探针的结果是：网易云安装器的向导在 UIA 里散成 15 个「一个控件一个顶层窗口」，
class 是标准 Win32 的 Button/Edit/Static，UIA 名字几乎全空（只有两个 Static 有字），
所以「按控件名找按钮」和「枚举 descendants」两条路都是死的。

但 class 是标准 Win32 类，说明底层就是普通 HWND，那就绕开 UIA 直接问 Win32：
GetWindowText / WM_GETTEXT 能不能读出「立即安装」。读得出来就不用截图 + OCR，
点击驱动能简单一个数量级；读不出来才回退到 PW_RENDERFULLCONTENT 截图 + OCR。

只看不点：不发任何点击消息，dump 完把安装器杀掉。
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from hall_auto.apps import STORE_DOWNLOAD_DIR, _process_image  # noqa: E402
from hall_auto.product import is_admin  # noqa: E402
from probe_installer import GREEN_EXES, find_package  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "reports" / "probe"
REPORT = OUT_DIR / "win32_probe.txt"
SETTLE_SEC = 60

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumChildWindows.argtypes = [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.GetAncestor.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowEnabled.argtypes = [wintypes.HWND]
# GetWindowLongPtr 必须显式声明返回值：默认 c_int 会把 64 位的 style 位截掉
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND,
    ctypes.c_uint,
    wintypes.WPARAM,
    wintypes.LPARAM,
    ctypes.c_uint,
    ctypes.c_uint,
    ctypes.POINTER(ctypes.c_void_p),
]
user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.OpenProcess.argtypes = [ctypes.c_uint, wintypes.BOOL, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CHILD = 0x40000000
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_DISABLED = 0x08000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
GA_ROOT = 2
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
BM_GETCHECK = 0x00F0
SMTO_ABORTIFHUNG = 0x0002

_lines: list[str] = []


def say(*parts) -> None:
    line = " ".join(str(p) for p in parts)
    print(line, flush=True)
    _lines.append(line)
    REPORT.write_text("\n".join(_lines) + "\n", encoding="utf-8")


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    return buf.value if user32.GetClassNameW(hwnd, buf, 256) else "<err>"


def get_window_text(hwnd: int) -> str:
    """GetWindowTextW：读的是系统缓存的窗口文字，跨进程不会被阻塞。"""
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def send_gettext(hwnd: int) -> str:
    """WM_GETTEXT：让控件自己把文字交出来。自绘按钮的标题常常只有这条路能读到。

    系统会为跨进程的 WM_GETTEXT 自动编组缓冲区，所以这里传本地 buffer 是安全的。
    """
    result = ctypes.c_void_p()
    length = user32.SendMessageTimeoutW(
        hwnd, WM_GETTEXTLENGTH, 0, 0, SMTO_ABORTIFHUNG, 500, ctypes.byref(result)
    )
    if not length:
        return ""
    size = int(result.value or 0)
    if size <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(size + 1)
    got = user32.SendMessageTimeoutW(
        hwnd, WM_GETTEXT, size + 1, ctypes.addressof(buf),
        SMTO_ABORTIFHUNG, 500, ctypes.byref(result),
    )
    if not got:
        return ""
    return buf.value


def send_getcheck(hwnd: int) -> str:
    result = ctypes.c_void_p()
    if not user32.SendMessageTimeoutW(
        hwnd, BM_GETCHECK, 0, 0, SMTO_ABORTIFHUNG, 500, ctypes.byref(result)
    ):
        return "?"
    return {0: "未勾选", 1: "已勾选", 2: "不确定"}.get(int(result.value or 0), str(result.value))


def rect_of(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return (0, 0, 0, 0)
    return (rect.left, rect.top, rect.right, rect.bottom)


def style_flags(hwnd: int) -> str:
    style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
    exstyle = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    flags = []
    if style & WS_CHILD:
        flags.append("CHILD")
    if style & WS_POPUP:
        flags.append("POPUP")
    if style & WS_VISIBLE:
        flags.append("VISIBLE")
    if style & WS_DISABLED:
        flags.append("DISABLED")
    if exstyle & WS_EX_LAYERED:
        flags.append("EX_LAYERED")
    if exstyle & WS_EX_TRANSPARENT:
        flags.append("EX_TRANSPARENT")
    if exstyle & WS_EX_TOOLWINDOW:
        flags.append("EX_TOOLWINDOW")
    return "|".join(flags) or "-"


def enumerate_hwnds() -> list[int]:
    found: list[int] = []

    def callback(hwnd, _lparam):
        found.append(int(hwnd))
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


def installer_pids() -> set[int]:
    pids: set[int] = set()
    for pid in {window_pid(h) for h in enumerate_hwnds()}:
        if not pid:
            continue
        image = _process_image(pid)
        if not image or Path(image).parent != STORE_DOWNLOAD_DIR:
            continue
        if Path(image).name in GREEN_EXES:
            continue
        pids.add(pid)
    return pids


def describe(hwnd: int, index: int, label: str) -> tuple[str, tuple[int, int, int, int]] | None:
    box = rect_of(hwnd)
    text = get_window_text(hwnd)
    got = send_gettext(hwnd)
    extra = ""
    if class_name(hwnd) == "Button":
        extra = f" check={send_getcheck(hwnd)}"
    say(
        f"  [{index}] {label} hwnd={hwnd} class={class_name(hwnd)!r} "
        f"style={style_flags(hwnd)} rect={box} visible={bool(user32.IsWindowVisible(hwnd))} "
        f"enabled={bool(user32.IsWindowEnabled(hwnd))}{extra} "
        f"GetWindowText={text!r} WM_GETTEXT={got!r}"
    )
    for candidate in (got, text):
        if candidate.strip():
            return candidate.strip(), box
    return None


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    say(f"admin={is_admin()} time={time.strftime('%H:%M:%S')}")
    package = find_package()
    say(f"拉起安装包: {package}")
    process = subprocess.Popen([str(package)])  # noqa: S603 - 本机专用探针，路径来自商店下载目录
    say(f"launcher pid={process.pid}，等 {SETTLE_SEC}s 让向导出来")
    time.sleep(SETTLE_SEC)

    pids = installer_pids()
    say(f"下载目录映像（排除绿色应用）的进程: {sorted(pids)}")
    if not pids:
        say("没找到安装器进程：要么它自己提权跑到 UAC 安全桌面上去了，要么已经退出")
        say(f"VERDICT windows=0 labeled=0 labels=[]")
        return 0

    tops = [h for h in enumerate_hwnds() if window_pid(h) in pids]
    say(f"这些进程的顶层窗口 {len(tops)} 个")
    labeled: list[tuple[str, tuple[int, int, int, int]]] = []
    for index, hwnd in enumerate(tops):
        root = int(user32.GetAncestor(hwnd, GA_ROOT) or 0)
        parent = int(user32.GetParent(hwnd) or 0)
        found = describe(hwnd, index, f"root={root} parent={parent}")
        if found:
            labeled.append(found)

    # 顶层窗读不到字的话，再看看它们的子窗（真正的控件树可能挂在下面）
    children_total = 0
    for hwnd in tops:
        kids: list[int] = []

        def callback(child, _lparam):
            kids.append(int(child))
            return True

        user32.EnumChildWindows(hwnd, WNDENUMPROC(callback), 0)
        if not kids:
            continue
        children_total += len(kids)
        say(f"  hwnd={hwnd} 的子窗 {len(kids)} 个:")
        for index, kid in enumerate(kids[:40]):
            found = describe(kid, index, "child")
            if found:
                labeled.append(found)
    say(f"子窗合计 {children_total} 个")

    for text, box in labeled:
        say(f"MAP text={text!r} rect={box} center=({(box[0] + box[2]) // 2}, {(box[1] + box[3]) // 2})")
    say(f"VERDICT windows={len(tops)} children={children_total} labeled={len(labeled)} "
        f"labels={[text for text, _ in labeled]}")
    if labeled:
        say("VERDICT 结论: Win32 能读到控件文字 -> 不用截图 OCR，直接按文字找 HWND 点击")
    elif tops:
        say("VERDICT 结论: Win32 也读不到文字 -> 回退 PW_RENDERFULLCONTENT 截图 + OCR")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        for pid in sorted(installer_pids()):
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True, check=False)
            say(f"已杀掉安装器 pid={pid}")
