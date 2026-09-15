"""厂商安装器探针：直接拉起下载目录里的安装包，把它向导窗能看到的东西全记下来。

为什么要这个：网易云音乐的安装器把向导做成了「一个控件一个无边框顶层窗口」
（class 就叫 Button/Edit/Static，UIA 名字全空），所以 descendants() 恒为 0；
这类分层窗口用普通 PrintWindow(flag=0) 和整屏裁剪都截成空白，只有带
PW_RENDERFULLCONTENT(flag=2) 才拿得到真实内容（本机已验证：distinct_colors 1 vs 6323）。
按控件名点击走不通，只能截到内容 → OCR 认字 → 按坐标点。

这个探针的产出就是那张「文字 → 窗口矩形」的表（结尾的 MAP 行），
点击驱动直接照着它写死这几款夹具应用的按钮。

只看不点：不点任何按钮，dump 完就把进程杀掉，机器上不会多装东西。
必须在管理员终端里跑（安装器提权，非提权既截不到也杀不掉）。
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# OCR 会认出 'Ø' 这类 cp936 编不出来的字符，不改编码整个探针会 UnicodeEncodeError 死在半路
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PIL import Image, ImageGrab  # noqa: E402
from pywinauto import Desktop  # noqa: E402

from hall_auto.apps import STORE_DOWNLOAD_DIR, _process_image, popup_text  # noqa: E402
from hall_auto.product import is_admin  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "reports" / "probe"
REPORT = OUT_DIR / "installer_probe.txt"
OCR = REPO / "tools" / "ocr_text.ps1"
SETTLE_SEC = 60

_lines: list[str] = []


def say(*parts) -> None:
    line = " ".join(str(p) for p in parts)
    print(line, flush=True)
    _lines.append(line)
    REPORT.write_text("\n".join(_lines) + "\n", encoding="utf-8")


# 绿色应用自己要求提权（WinError 740），拉它会弹 UAC，探针不碰
GREEN_EXES = ("WinScreenshot.exe",)


def find_package() -> Path:
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    packages = [
        p for p in STORE_DOWNLOAD_DIR.glob("*.exe") if p.name not in GREEN_EXES
    ]
    packages.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if not packages:
        raise SystemExit(f"{STORE_DOWNLOAD_DIR} 里没有可探的安装包")
    return packages[0]


def ocr(image: Path) -> list[str]:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(OCR), str(image), "-Boxes"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    words = [line for line in (result.stdout or "").splitlines() if line.strip()]
    if result.returncode != 0:
        words.append(f"<OCR 失败 rc={result.returncode} {result.stderr.strip()[:200]}>")
    return words


def window_class(hwnd: int) -> str:
    ctypes.windll.user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    buf = ctypes.create_unicode_buffer(256)
    if ctypes.windll.user32.GetClassNameW(hwnd, buf, 256):
        return buf.value
    return "<err>"


def integrity_level(pid: int) -> str:
    """进程完整性级别。medium=12288 high=16384 system=20480；读不到记 err。

    所有句柄类函数必须显式声明 restype/argtypes：ctypes 默认按 c_int 截断 64 位句柄，
    CloseHandle 拿到垃圾值会直接段错误。
    """
    kernel32 = ctypes.windll.kernel32
    advapi32 = ctypes.windll.advapi32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    advapi32.OpenProcessToken.restype = ctypes.c_bool
    advapi32.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.GetTokenInformation.restype = ctypes.c_bool
    advapi32.ConvertSidToStringSidW.restype = ctypes.c_bool
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    TOKEN_QUERY = 0x0008
    TokenIntegrityLevel = 25
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return "<err open>"
    token = ctypes.c_void_p()
    try:
        if not advapi32.OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
            return "<err token>"
        size = ctypes.c_uint32(0)
        advapi32.GetTokenInformation(token, TokenIntegrityLevel, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value or 64)
        if not advapi32.GetTokenInformation(
            token, TokenIntegrityLevel, buf, size.value or 64, ctypes.byref(size)
        ):
            return "<err info>"
        # TOKEN_MANDATORY_LABEL 的第一个字段是 PSID
        sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents
        text = ctypes.c_void_p()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            return "<err sid>"
        try:
            value = ctypes.cast(text, ctypes.c_wchar_p).value or ""
        finally:
            kernel32.LocalFree(text)
        level = int(value.rsplit("-", 1)[-1]) if value else -1
        return {8192: "low", 12288: "medium", 16384: "high", 20480: "system"}.get(level, str(level))
    except Exception as exc:  # noqa: BLE001 - 探针里任何一步失败都要记下来而不是崩
        return f"<err {exc!r}>"
    finally:
        if token:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(handle)


def is_cloaked(hwnd: int) -> str:
    ctypes.windll.dwmapi.DwmGetWindowAttribute.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    attr = ctypes.c_int(0)
    rc = ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(attr), 4)
    if rc != 0:
        return f"<err {rc}>"
    return "yes" if attr.value else "no"


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


PW_RENDERFULLCONTENT = 2
BI_RGB = 0


def capture_full_content(hwnd: int):
    """PrintWindow(PW_RENDERFULLCONTENT) 抓窗口真实像素，返回 (是否成功, PIL 图)。

    安装器的控件窗是分层窗，普通 PrintWindow 只拿到空白背景；这个 flag 让系统
    重绘完整内容（含 DirectX/WebView 层），是唯一能截到字的路子。
    """
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowDC.restype = ctypes.c_void_p
    user32.GetWindowDC.argtypes = [ctypes.c_void_p]
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi32.GetDIBits.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint,
    ]

    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError(f"GetWindowRect 失败 err={ctypes.get_last_error()}")
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise ValueError(f"矩形是空的 {width}x{height}")

    window_dc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    old_bitmap = gdi32.SelectObject(mem_dc, bitmap)
    try:
        ok = bool(user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT))
        # GetDIBits 要求位图没被选进任何 DC，先还原再取
        gdi32.SelectObject(mem_dc, old_bitmap)
        header = _BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height  # 负数 = 自上而下，和 PIL 的行序一致
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = BI_RGB
        buffer = ctypes.create_string_buffer(width * height * 4)
        lines = gdi32.GetDIBits(
            mem_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0
        )
        if not lines:
            raise OSError("GetDIBits 返回 0 行")
        image = Image.frombuffer("RGB", (width, height), buffer, "raw", "BGRX", 0, 1)
        return ok, image
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, window_dc)


def distinct_colors(image) -> int:
    """图里有几种颜色。1 = 纯空白（截图失败的典型症状），几百上千 = 真截到内容了。"""
    return len(image.convert("RGB").getcolors(maxcolors=1_000_000) or [])


def shoot(win, pid: int, index: int, full):
    """截一个顶层窗口，认字，返回 (窗口里的文字, 屏幕矩形) 或 None。"""
    try:
        rect = win.rectangle()
        box = (rect.left, rect.top, rect.right, rect.bottom)
    except Exception as exc:
        say(f"  [{index}] 读不到矩形: {exc!r}")
        return None
    try:
        visible = win.is_visible()
    except Exception:
        visible = "?"
    try:
        controls = len(win.descendants())
    except Exception as exc:
        controls = f"err {exc!r}"
    cls = window_class(win.handle)
    cloaked = is_cloaked(win.handle)
    level = integrity_level(pid)
    say(
        f"  [{index}] title={popup_text(win.window_text() or '')!r} class={cls!r} rect={box} "
        f"visible={visible} cloaked={cloaked} integrity={level} "
        f"descendants(control-view)={controls}"
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        say(f"  [{index}] 矩形是空的，不截图")
        return None

    stem = OUT_DIR / f"installer_w{index}_pid{pid}"
    shots: list[tuple[str, Path, object]] = []
    try:
        ok, image = capture_full_content(win.handle)
        png = Path(f"{stem}_renderfull.png")
        image.save(png)
        colors = distinct_colors(image)
        say(
            f"  [{index}] PrintWindow(RENDERFULLCONTENT) ok={ok} -> {png.name} "
            f"size={image.size} distinct_colors={colors}"
        )
        shots.append(("renderfull", png, image))
    except Exception as exc:
        say(f"  [{index}] PrintWindow(RENDERFULLCONTENT) 失败: {exc!r}")
    try:
        crop = full.crop(box)
        png = Path(f"{stem}_screen.png")
        crop.save(png)
        colors = distinct_colors(crop)
        say(
            f"  [{index}] 整屏裁剪 -> {png.name} size={crop.size} distinct_colors={colors}"
        )
        shots.append(("screen", png, crop))
    except Exception as exc:
        say(f"  [{index}] 整屏裁剪失败: {exc!r}")

    texts: list[str] = []
    for suffix, png, image in shots:
        if distinct_colors(image) <= 1:
            say(f"  [{index}] {suffix} 是纯空白（distinct_colors<=1），跳过 OCR")
            continue
        words = ocr(png)
        say(f"  [{index}] OCR({suffix}) {len(words)} 词:")
        for word in words[:40]:
            say(f"        {word}")
        if suffix == "renderfull" and not texts:
            texts = [word.split("\t", 1)[0] for word in words if "\t" in word]
    text = " ".join(text for text in texts if text).strip()
    if text:
        return text, box
    return None


def installer_pids() -> set[int]:
    """安装包会自己提权重拉子进程，真向导的 pid 和我拉起的不一样，只能按映像路径认。"""
    pids: set[int] = set()
    try:
        windows = Desktop(backend="uia").windows()
    except Exception:
        return pids
    for win in windows:
        try:
            pid = win.element_info.process_id
        except Exception:
            continue
        if not pid:
            continue
        image = _process_image(pid)
        if not image or Path(image).parent != STORE_DOWNLOAD_DIR:
            continue
        # 绿色应用也放在下载目录，跑着的时候会被误认成安装器（还会被结尾的 taskkill 打死）
        if Path(image).name in GREEN_EXES:
            continue
        pids.add(int(pid))
    return pids


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    say(f"admin={is_admin()} time={time.strftime('%H:%M:%S')}")
    package = find_package()
    say(f"拉起安装包: {package}")
    process = subprocess.Popen([str(package)])  # noqa: S603 - 本机专用探针，路径来自商店下载目录
    say(f"launcher pid={process.pid}，等 {SETTLE_SEC}s 让向导出来（真身可能是提权子进程）")
    time.sleep(SETTLE_SEC)

    full_path = OUT_DIR / "installer_full.png"
    full = ImageGrab.grab(all_screens=True)
    full.save(full_path)
    say(f"整屏截图 -> {full_path.name} size={full.size}")

    try:
        all_windows = Desktop(backend="uia").windows()
    except Exception as exc:
        say(f"枚举窗口失败: {exc!r}")
        all_windows = []
    say(f"全量顶层窗口 {len(all_windows)} 个:")
    for win in all_windows:
        try:
            pid = win.element_info.process_id
            title = popup_text(win.window_text() or "")
            cls = window_class(win.handle)
            image = Path(_process_image(pid) or "").name
        except Exception:
            continue
        say(f"    pid={pid} class={cls!r} image={image!r} title={title!r}")

    pids = installer_pids() or {process.pid}
    say(f"下载目录映像的进程: {sorted(pids)}")
    try:
        windows = Desktop(backend="uia").windows()
    except Exception as exc:
        say(f"枚举窗口失败: {exc!r}")
        windows = []
    mine = []
    for win in windows:
        try:
            if win.element_info.process_id in pids:
                mine.append(win)
        except Exception:
            continue
    say(f"这些进程的顶层窗口 {len(mine)} 个")
    labeled: list[tuple[str, tuple[int, int, int, int]]] = []
    for index, win in enumerate(mine):
        found = shoot(win, sorted(pids)[0], index, full)
        if found:
            labeled.append(found)

    if not mine:
        say("没找到窗口，直接对整屏 OCR")
        for word in ocr(full_path)[:80]:
            say(f"        {word}")
    for text, box in labeled:
        say(f"MAP text={text!r} rect={box} center=({(box[0] + box[2]) // 2}, {(box[1] + box[3]) // 2})")
    say(
        f"VERDICT windows={len(mine)} labeled={len(labeled)} "
        f"labels={[text for text, _ in labeled]}"
    )
    if mine and not labeled:
        say("VERDICT 结论: 窗口截到了但一个字都认不出 -> 截图或 OCR 这条路还不通")
    elif labeled:
        say("VERDICT 结论: 能拿到「文字 → 矩形」表 -> 点击驱动照 MAP 行写死按钮")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        # 只看不点：探针结束必须把安装器杀掉，别留在机器上等人点
        for pid in sorted(installer_pids()):
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True, check=False)
            say(f"已杀掉安装器 pid={pid}")
