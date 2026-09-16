from __future__ import annotations

from hall_auto.launch import LaunchError, _press_button
from hall_auto.login import _by_aid
from hall_auto.screen import grab_virtual_screen, mean_luminance
from hall_auto.waiting import wait_until, wait_until_or_raise

SETTINGS_OPEN_TIMEOUT_SEC = 15
THEME_APPLY_TIMEOUT_SEC = 10
# 三档都是 RadioButton，且名字会被同名 ToolTip 抢走，所以只认 AutomationId
THEME_AIDS = {"浅色": "LightMode", "深色": "DarkMode", "跟随系统": "AuroMode"}
# 实测主窗口平均亮度：浅色 227~255、深色 41~53，差五倍以上
DARK_LUMINANCE_MAX = 128.0


def open_settings(main) -> int:
    """主窗口点设置按钮进设置页，返回 pid。"""
    pid = main.element_info.process_id
    btn = _by_aid(pid, "Button", "SetBtn")
    if btn is None or not _press_button(btn):
        raise LaunchError("设置页：点不到设置按钮 SetBtn")
    wait_until_or_raise(
        lambda: bool(theme_nodes(pid)),
        "设置页：主题三档没出现（设置页没打开？）",
        timeout_sec=SETTINGS_OPEN_TIMEOUT_SEC,
        interval=0.5,
    )
    return pid


def ensure_settings(main) -> int:
    """大厅会记住上次的页面，重启后可能已经在设置页，所以先看在不在再决定要不要点。"""
    pid = main.element_info.process_id
    if theme_nodes(pid):
        return pid
    return open_settings(main)


def theme_nodes(pid: int) -> dict[str, object]:
    nodes = {}
    for label, aid in THEME_AIDS.items():
        node = _by_aid(pid, "RadioButton", aid)
        if node is not None:
            nodes[label] = node
    return nodes


def theme_selected(pid: int) -> str | None:
    for label, node in theme_nodes(pid).items():
        try:
            if node.is_selected():
                return label
        except Exception:
            continue
    return None


def _select_radio(node) -> bool:
    """先走 SelectionItem 模式（不依赖坐标）；点偏了会改到隔壁档位，所以坐标点击只当兜底。"""
    try:
        node.select()
        return True
    except Exception:
        return _press_button(node)


def set_theme(pid: int, label: str) -> None:
    node = theme_nodes(pid).get(label)
    if node is None:
        raise LaunchError(f"主题：找不到 {label} 单选钮")
    if not _select_radio(node):
        raise LaunchError(f"主题：点不动 {label}")
    if not wait_until(lambda: theme_selected(pid) == label, timeout_sec=THEME_APPLY_TIMEOUT_SEC):
        raise LaunchError(f"主题：点了 {label} 但选中状态没变（当前 {theme_selected(pid)}）")


def main_luminance(main) -> float:
    rect = main.rectangle()
    img, (vx, vy) = grab_virtual_screen()
    return mean_luminance(img.crop((rect.left - vx, rect.top - vy, rect.right - vx, rect.bottom - vy)))


def looks_dark(main) -> bool:
    return main_luminance(main) < DARK_LUMINANCE_MAX
