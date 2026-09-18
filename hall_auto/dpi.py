"""显示缩放（DPI）与环境画像。

## 结论先说（2026-09-18 实测订正）

**本套自动化不怕显示缩放，不需要把机器统一改成 100%。**

原因：`hall_auto/screen.py` 在 import 时就调 `SetProcessDpiAwarenessContext(-4)` 设成
Per-Monitor Aware，整套脚本因此工作在**真实物理像素**坐标系，与 UIA `rectangle()` 同一套。
本机真实缩放 150%（物理 2560x1600），P0/P1/P2 全部套件在此前提下实测跑通，
按钮面积判据（360x60 等）就是在物理像素下标定的。

所以下面这些是**错的方向，别再走**：
- 把 20 台机器的用户缩放统一改成 100%（抹平了变量，还测不出真实用户环境）
- 靠写 `HKCU\\Control Panel\\Desktop\\LogPixels` 改缩放（Win10 1703+ 缩放是每显示器存储的，
  本机实测该值根本不存在；写了也不生效，除非重启 explorer + 重新登录）

## 那这个模块还有什么用

三件事，都是**观测**而不是**修改**：
1. `machine_profile()` —— 采集环境画像（OS / 分辨率 / 缩放 / 显示器数 / Python 位数），
   多机汇总时按维度分组看结论。
2. `check_scale()` —— 报告当前缩放，写进报告页头。**不做门禁、不阻断跑批**：
   150% 也是能正常跑的（本机就是）。
3. `is_interactive_session()` —— 这个才是真正的门禁：锁屏 / 息屏 / RDP 断开后会话变非交互态，
   抓屏拿黑屏、点击无落点，**全套用例会批量失败**，必须拒绝开跑。

## 一个必须记住的读 DPI 的坑

**读任何 DPI / 屏幕尺寸前，必须先 `ensure_dpi_aware()`。**
实测同一台机器：DPI-unaware 进程读出来是 96 DPI / 1707x1067（被系统缩放虚拟化后的假值），
设成 Per-Monitor Aware 后才是真实的 144 DPI / 2560x1600。
不设就检测，会永远报「100%，符合预期」，把缩放事实整个漏掉。
本模块所有读数函数内部都已自带这一步。
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

# 100% 缩放对应 96 DPI，仅作换算基准
TARGET_LOGPIXELS = 96


def ensure_dpi_aware() -> str:
    """把本进程设成 Per-Monitor Aware，返回设置结果。

    **必须先做这一步再读任何 DPI/分辨率**，否则读到的是被系统缩放虚拟化后的值：
    实测本机（真实 150%）在 DPI-unaware 进程里读出来是 96 DPI / 1707x1067，
    设成 Per-Monitor Aware 后才读到真实的 144 DPI / 2560x1600。
    不设就检测，会永远报「100%，符合预期」，把缩放问题整个漏掉。

    与 hall_auto/screen.py 设的是同一个上下文（-4 = PER_MONITOR_AWARE_V2 兼容值），
    那边是为了截图坐标跟 UIA rectangle() 对齐；重复调用是幂等的。
    """
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return "per-monitor-aware"
    except (AttributeError, OSError):
        pass
    try:
        # Win8.1 回退：2 = PROCESS_PER_MONITOR_DPI_AWARE
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per-monitor-aware(shcore)"
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return "system-aware(fallback)"
    except (AttributeError, OSError):
        return "unaware"


def current_dpi() -> int:
    """当前**系统** DPI（物理值，100% = 96）。

    **读之前先保证进程是 DPI-aware**，否则拿到被缩放虚拟化后的假值。
    """
    ensure_dpi_aware()
    try:
        return int(ctypes.windll.user32.GetDpiForSystem())
    except (AttributeError, OSError):
        pass
    try:
        hdc = ctypes.windll.user32.GetDC(0)
        dpi = int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88))  # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, hdc)
        return dpi
    except OSError:
        return 0


def current_scale_percent() -> int:
    """当前缩放百分比，四舍五入到整数（96 -> 100，120 -> 125，144 -> 150）。"""
    dpi = current_dpi()
    return round(dpi / 96 * 100) if dpi else 0


def process_dpi_awareness() -> str:
    """本进程的 DPI 感知级别，用来确认截图坐标跟 UIA rectangle 是同一套。

    Per-Monitor Aware = 物理像素坐标，hall_auto/screen.py 在 import 时就设成这个。
    """
    try:
        ctx = ctypes.windll.user32.GetThreadDpiAwarenessContext()
        if ctypes.windll.user32.AreDpiAwarenessContextsEqual(ctx, ctypes.c_void_p(-4)):
            return "per-monitor-aware"
        if ctypes.windll.user32.AreDpiAwarenessContextsEqual(ctx, ctypes.c_void_p(-3)):
            return "per-monitor-aware(v2)"
        if ctypes.windll.user32.AreDpiAwarenessContextsEqual(ctx, ctypes.c_void_p(-2)):
            return "system-aware"
        return "unaware"
    except (AttributeError, OSError):
        return "unknown"


def check_scale(expected_percent: int | None = None) -> tuple[bool, str]:
    """报告当前缩放。返回 (是否是 100%, 说明文本)。

    **只观测，不改设置，也不做门禁。** 非 100% 不影响跑批正确性 —— 脚本用物理像素坐标，
    本机真实 150% 下全部套件实测跑通。这里返回的 ok 只表示「是否等于 100%」，
    给报告页头展示用；调用方不要拿它去阻断跑批。
    """
    actual = current_scale_percent()
    if actual == 100:
        return True, f"缩放 {actual}%（物理像素坐标，判据一致）"
    return False, (
        f"缩放 {actual}%。脚本走 Per-Monitor Aware 物理像素坐标，非 100% 不影响正确性；"
        f"仅作为环境事实记录，多机汇总时用于区分机型。"
    )


def machine_profile() -> dict:
    """采集一份本机环境画像，写进证据 summary / 机器清单，多机汇总时按维度分组用。"""
    return {
        "node": platform_node(),
        "os": _os_string(),
        "arch": os.environ.get("PROCESSOR_ARCHITECTURE", ""),
        "python": sys.version.split()[0],
        "python_bits": 64 if sys.maxsize > 2**32 else 32,
        "dpi": current_dpi(),
        "scale_percent": current_scale_percent(),
        "dpi_awareness": process_dpi_awareness(),
        "screen": _screen_size(),
        "monitors": _monitor_count(),
    }


def platform_node() -> str:
    import platform

    try:
        return platform.node() or ""
    except OSError:
        return ""


def _os_string() -> str:
    import platform

    return f"{platform.system()} {platform.release()} {platform.version()}"


def _screen_size() -> str:
    ensure_dpi_aware()
    u32 = ctypes.windll.user32
    return f"{u32.GetSystemMetrics(0)}x{u32.GetSystemMetrics(1)}"


def _monitor_count() -> int:
    ensure_dpi_aware()
    return int(ctypes.windll.user32.GetSystemMetrics(80))


def node_id() -> str:
    """本机节点标识。优先 HALL_NODE_ID 环境变量，否则用主机名，都取不到就 unknown-node。

    用途：多机并行时给证据目录 / 报告加前缀，避免 20 台回传撞名。
    """
    explicit = (os.environ.get("HALL_NODE_ID") or "").strip()
    if explicit:
        return explicit
    return platform_node().strip() or "unknown-node"


def is_interactive_session() -> bool:
    """当前是否交互式桌面会话。

    锁屏 / 息屏 / RDP 断开后会话变非交互态，抓屏拿到黑屏、点击无落点 → 全套用例批量失败。
    这是多机跑批最常见的环境坑，跑批前检查并拒绝在非交互态开跑，比跑完拿一堆假失败强。
    """
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    # 有前台窗口说明当前桌面可交互；锁屏时前台窗口属于 LogonUI
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return True
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return "LogonUI" not in buf.value


if __name__ == "__main__":
    import json

    profile = machine_profile()
    ok, msg = check_scale()
    profile["is_100_percent"] = ok
    profile["scale_message"] = msg
    profile["interactive"] = is_interactive_session()
    print(json.dumps(profile, ensure_ascii=False, indent=2))
