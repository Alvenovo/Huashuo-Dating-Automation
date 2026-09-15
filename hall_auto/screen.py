from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

from PIL import Image, ImageStat

try:
    # 物理像素坐标要跟 UIA 的 rectangle() 对齐，必须 Per-Monitor Aware
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except OSError:
    pass


def grab_virtual_screen() -> tuple[Image.Image, tuple[int, int]]:
    """抓整个虚拟屏，返回 PIL 图和虚拟屏原点；坐标是物理像素，跟 UIA 的 rectangle() 同一套。"""
    u32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    vx = u32.GetSystemMetrics(76)
    vy = u32.GetSystemMetrics(77)
    vw = u32.GetSystemMetrics(78)
    vh = u32.GetSystemMetrics(79)
    hdc_screen = u32.GetDC(None)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    bmp = gdi32.CreateCompatibleBitmap(hdc_screen, vw, vh)
    old = gdi32.SelectObject(hdc_mem, bmp)
    gdi32.BitBlt(hdc_mem, 0, 0, vw, vh, hdc_screen, vx, vy, 0x00CC0020)

    class BIH(ctypes.Structure):
        _fields_ = [
            ("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
            ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
            ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
        ]

    bih = BIH()
    bih.biSize = ctypes.sizeof(BIH)
    bih.biWidth, bih.biHeight = vw, -vh
    bih.biPlanes, bih.biBitCount, bih.biCompression = 1, 32, 0
    buf = ctypes.create_string_buffer(vw * vh * 4)
    gdi32.SelectObject(hdc_mem, old)
    gdi32.GetDIBits(hdc_mem, bmp, 0, vh, buf, ctypes.byref(bih), 0)
    img = Image.frombuffer("RGB", (vw, vh), buf, "raw", "BGRX", 0, 1)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(hdc_mem)
    u32.ReleaseDC(None, hdc_screen)
    return img, (vx, vy)


def count_red_pixels(img: Image.Image) -> int:
    """数红字像素。大厅的错误提示是红字灰底，灰提示的红像素实测为 0。"""
    px = img.convert("RGB").load()
    total = 0
    for y in range(img.height):
        for x in range(img.width):
            r, g, b = px[x, y]
            if r > 150 and g < 110 and b < 110:
                total += 1
    return total


def mean_luminance(img: Image.Image) -> float:
    return ImageStat.Stat(img.convert("L")).mean[0]
