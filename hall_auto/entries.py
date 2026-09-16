"""左下角入口区（小硕知道 / 小硕 x WorkBuddy）的只读定位与点击。

WB 入口的文案被 UIA 拆成「小硕 x」+「WorkBuddy」两个文本节点（2026-09-16 实测），
按单节点识别会漏，所以把「含入口关键字但不含『小硕知道』」的节点聚成 WB 组，
热区取组内 rect 并集。原有入口「小硕知道」单独成组。

红线：本模块只做定位 + 单点/连点 + 像素差，绝不点落地页里的「一键安装」——
那会真装 WorkBuddy，属破坏性操作。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from pywinauto import mouse

from hall_auto.launch import _back_if_subpage
from hall_auto.screen import grab_virtual_screen
from hall_auto.winapi import (
    _close_window,
    _hwnd_pid,
    _hwnd_text,
    _hwnd_visible,
    _top_hwnds,
)

ENTRY_KEYS = ("小硕", "WorkBuddy", "workbuddy")
ORIGINAL_ENTRY = "小硕知道"
WB_LABEL = "小硕 x WorkBuddy"
INSTALL_BUTTON_TEXT = "一键安装"  # 红线：任何情况下都不点
LOGIN_WINDOW_KEYWORD = "登录"
HOME_SETTLE_SEC = 1.5
CLICK_SETTLE_SEC = 3.0


@dataclass(frozen=True)
class EntryRegion:
    name: str
    nodes: tuple
    left: int
    top: int
    right: int
    bottom: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2


def _is_entry_label(name: str) -> bool:
    """左下角入口标签是短名（小硕知道 / 小硕 x / WorkBuddy）；首页轮播里的 WB 推广
    磁贴名字又长又带「已选择 / 安装应用 / 打开应用」，会把合并热区撑成大半个页面，
    必须排除，否则坐标点会落到页面中间点不动入口。"""
    text = name.strip()
    if not text or len(text) > 20:
        return False
    return not any(noise in text for noise in ("已选择", "安装应用", "打开应用", "更新应用"))


def _entry_nodes(main) -> list:
    found = []
    for node in main.descendants():
        try:
            name = node.window_text() or ""
            rect = node.rectangle()
        except Exception:
            continue
        if any(key in name for key in ENTRY_KEYS) and _is_entry_label(name):
            found.append((node, name, rect))
    return found


def locate_entries(main) -> dict:
    """返回 {'original': EntryRegion, 'wb': EntryRegion}，缺哪组就不给哪个键。"""
    nodes = _entry_nodes(main)
    original = [(d, n, r) for d, n, r in nodes if ORIGINAL_ENTRY in n]
    wb = [(d, n, r) for d, n, r in nodes if ORIGINAL_ENTRY not in n]
    regions: dict[str, EntryRegion] = {}
    if original:
        rects = [r for _, _, r in original]
        regions["original"] = EntryRegion(
            ORIGINAL_ENTRY,
            tuple(d for d, _, _ in original),
            min(r.left for r in rects), min(r.top for r in rects),
            max(r.right for r in rects), max(r.bottom for r in rects),
        )
    if wb:
        rects = [r for _, _, r in wb]
        regions["wb"] = EntryRegion(
            WB_LABEL,
            tuple(d for d, _, _ in wb),
            min(r.left for r in rects), min(r.top for r in rects),
            max(r.right for r in rects), max(r.bottom for r in rects),
        )
    return regions


def click_entry(region: EntryRegion) -> None:
    """按合并热区中心坐标点，不用节点 click_input——WB 文案被拆成两个文本节点，
    树顺序不定，取 nodes[-1] 可能点到「小硕 x」而非「WorkBuddy」，坐标点更确定。"""
    mouse.click(button="left", coords=region.center)


def process_window_titles(pid: int) -> list:
    """某进程名下的可见顶层窗标题（大厅详情页画在同一窗里，不会多出来）。"""
    titles = []
    for hwnd in _top_hwnds():
        if _hwnd_pid(hwnd) == pid and _hwnd_visible(hwnd):
            text = _hwnd_text(hwnd)
            if text.strip():
                titles.append(text)
    return titles


def all_window_titles() -> list:
    """全系统可见顶层窗标题，用来判断点击有没有唤起大厅以外的窗口（如系统浏览器）。"""
    titles = []
    for hwnd in _top_hwnds():
        if _hwnd_visible(hwnd):
            text = _hwnd_text(hwnd)
            if text.strip():
                titles.append(text)
    return titles


def landing_text(main) -> str:
    parts = []
    for node in main.descendants():
        try:
            text = node.window_text()
        except Exception:
            continue
        if text:
            parts.append(text)
    return " ".join(parts)


def back_home(main) -> None:
    for _ in range(3):
        if not _back_if_subpage(main):
            return
        time.sleep(HOME_SETTLE_SEC)


def close_window_by_keyword(pid: int, keyword: str) -> bool:
    """关掉某进程名下标题含关键字的顶层窗（收尾关登录窗用），发 WM_CLOSE 不硬杀。"""
    closed = False
    for hwnd in _top_hwnds():
        if _hwnd_pid(hwnd) == pid and _hwnd_visible(hwnd) and keyword in _hwnd_text(hwnd):
            _close_window(hwnd)
            closed = True
    return closed


def region_pixel_change(region: EntryRegion, gap: float = 0.6, threshold: int = 24) -> tuple:
    """区域两帧像素差，返回 (变化像素数, 区域总像素数)。静态图应为 0，GIF 动图 > 0。"""
    img1, origin = grab_virtual_screen()
    time.sleep(gap)
    img2, _ = grab_virtual_screen()
    box = (
        region.left - origin[0], region.top - origin[1],
        region.right - origin[0], region.bottom - origin[1],
    )
    c1 = img1.crop(box).convert("L")
    c2 = img2.crop(box).convert("L")
    p1, p2 = c1.load(), c2.load()
    changed = sum(
        1 for y in range(c1.height) for x in range(c1.width)
        if abs(p1[x, y] - p2[x, y]) > threshold
    )
    return changed, c1.width * c1.height
