"""环境事实探测：WebView2 运行时、微软登录会话。

## 为什么要单独一个模块

`dpi.py` 管的是**显示**相关（DPI / 缩放 / 分辨率 / 监视器数），是本机跑得对不对的前提；
这里管的是**兼容性分组**相关的两项事实 —— 组长定的验证目标是「版本放量前的兼容性验证」，
报告要按环境维度分组说「哪些环境有问题」，而下面这两项恰恰是最容易把
**环境缺失误判成产品缺陷**的维度：

- **WebView2 运行时缺失或过旧** → 大厅首页永远不到就绪（`MainWeb` / `专题页` 判据失效），
  启动类用例全挂。看起来像产品问题，实际是这台机器缺运行时。
- **没有缓存的微软登录会话** → SSO 免密用例必然 skip（那台机器上没有可免密的账号）。

不把这两项采出来，20 台混在一起的汇总表里，你无法区分
「这台机器环境有问题」和「这个版本有缺陷」—— 结论会被污染。

## 单一权威实现

`tools/collect_machine.py` 原来自己抄了一份 WebView2 / 大厅安装探测，`dpi.machine_profile()`
又完全不采这些字段，两份口径迟早漂移。现在探测实现只在这里，两边都调这里。

## 只读

本模块**只读注册表，不改任何东西**，不装运行时、不建会话，纯观测。
"""

from __future__ import annotations

import os

# WebView2 运行时的 EdgeUpdate 产品码。三个位置都查：
# 系统级 32 位视图、系统级 64 位视图、当前用户级（用户级安装是合法形态）。
WEBVIEW2_PRODUCT_CODE = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

WEBVIEW2_KEYS = (
    (r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\\" + WEBVIEW2_PRODUCT_CODE, "hklm32"),
    (r"SOFTWARE\Microsoft\EdgeUpdate\Clients\\" + WEBVIEW2_PRODUCT_CODE, "hklm64"),
)

HALL_UNINSTALL_KEYS = (
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\AsusMemberCenter",
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\AsusMemberCenter",
)


def _read_reg(rel_path: str, value_name: str, *, current_user: bool = False) -> str:
    """读注册表一个值；任何失败（键不存在 / 无权限 / 非 Windows）都返回空串。"""
    try:
        import winreg
    except ImportError:
        return ""
    hive = winreg.HKEY_CURRENT_USER if current_user else winreg.HKEY_LOCAL_MACHINE
    try:
        with winreg.OpenKey(hive, rel_path) as key:
            value, _ = winreg.QueryValueEx(key, value_name)
    except OSError:
        return ""
    return str(value).strip()


def webview2_version() -> str:
    """WebView2 运行时版本；没装返回空串。

    大厅首页就是 WebView2 承载的，缺了或过旧会让首页初始化非常慢甚至失败。
    """
    for rel_path, _tag in WEBVIEW2_KEYS:
        version = _read_reg(rel_path, "pv")
        if version:
            return version
    # 用户级安装
    return _read_reg(
        r"SOFTWARE\Microsoft\EdgeUpdate\Clients\\" + WEBVIEW2_PRODUCT_CODE, "pv", current_user=True
    )


def webview2_installed() -> bool:
    return bool(webview2_version())


def hall_installed_version() -> str:
    """注册表里已装大厅的 DisplayVersion；没装返回空串。

    与 `hall_auto.product.read_installed()` 的区别：那个读的是完整卸载信息（含 install_dir /
    exe 路径），给安装层用；这里只要一个版本字符串，给画像用，且**不 import product**
    （product 依赖 win32 相关模块，画像采集希望尽量轻、能在裸机上跑）。
    """
    for rel_path in HALL_UNINSTALL_KEYS:
        version = _read_reg(rel_path, "DisplayVersion")
        if version:
            return version
    return ""


def ms_login_session() -> str:
    """是否有缓存的微软登录会话（Token 缓存目录的条目数 > 0）。

    决定 SSO 免密用例在那台机器上是「能跑」还是「必然 skip」。
    只数条目，不读 Token 内容 —— 凭据类信息一律不采集、不进报告。

    返回 "yes" / "no" / "unknown"（探测失败时 unknown，不谎报 no）。
    """
    local = os.environ.get("LOCALAPPDATA") or ""
    if not local:
        return "unknown"
    from pathlib import Path

    candidates = (
        Path(local) / "Microsoft" / "OneAuth",
        Path(local) / "Microsoft" / "IdentityCache",
        Path(local) / "Microsoft" / "TokenBroker" / "Cache",
    )
    found_any = False
    for base in candidates:
        try:
            if not base.is_dir():
                continue
            found_any = True
            if any(base.iterdir()):
                return "yes"
        except OSError:
            continue
    # 连目录都没有 → 明确 no；目录存在但空 → 也是 no
    return "no" if found_any or os.environ.get("LOCALAPPDATA") else "unknown"


def ocr_has_chinese() -> str:
    """是否装了中文 OCR 语言包（`tools/ocr_text.ps1` 兜底路要用）。

    返回 "yes" / "no" / "unknown"。跑 PowerShell 探测，约 1~2 秒。
    """
    import subprocess

    try:
        proc = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]"
                "::AvailableRecognizerLanguages | ForEach-Object { $_.LanguageTag }",
            ],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    tags = [t.strip() for t in (proc.stdout or "").splitlines() if t.strip()]
    return "yes" if any(t.lower().startswith("zh") for t in tags) else "no"


def compat_facts() -> dict:
    """兼容性分组需要的补充事实。合并进 `dpi.machine_profile()`。

    刻意不复用 `product.read_installed()`：那个要 import win32 相关模块，
    而画像采集希望在**还没建 venv 的裸机**上也能跑（bootstrap 第 1 步就会调）。
    """
    return {
        "webview2": "yes" if webview2_installed() else "no",
        "webview2_version": webview2_version(),
        "hall_installed": "yes" if hall_installed_version() else "no",
        "hall_version": hall_installed_version(),
        "ms_session": ms_login_session(),
        "ocr_zh": ocr_has_chinese(),
    }
