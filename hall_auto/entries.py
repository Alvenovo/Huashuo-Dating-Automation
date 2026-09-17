"""左下角入口区（小硕知道 / 小硕 x WorkBuddy）的只读定位与点击。

WB 入口的文案被 UIA 拆成「小硕 x」+「WorkBuddy」两个文本节点（2026-09-16 实测），
按单节点识别会漏，所以把「含入口关键字但不含『小硕知道』」的节点聚成 WB 组，
热区取组内 rect 并集。原有入口「小硕知道」单独成组。

红线：非破坏套件只做定位 + 单点/连点 + 像素差，绝不点落地页里的「一键安装」。
唯一例外是门禁（--allow-install / HALL_ALLOW_INSTALL）放行的破坏性行 25 用例
（装→验拉起→卸 往返），且只能走本模块的 click_install_button 这一个收口函数，
便于审计"谁真装了 WorkBuddy"。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from pywinauto import mouse

from hall_auto.apps import uninstall_entry
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
INSTALL_BUTTON_TEXT = "一键安装"  # 仅破坏性行 25 用例经 click_install_button 点，非破坏套件禁碰
WB_REGISTRY_HINT = "WorkBuddy"  # 注册表 DisplayName 含此串即视为 WB 已装（HKCU，按用户装）
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


def region_frames_changed(region: EntryRegion, seconds: float = 3.0, interval: float = 0.5, threshold: int = 24) -> tuple:
    """区域在 [0, seconds] 窗口内每 interval 采一帧、逐帧比上一帧，返回 (有变化的帧数, 总帧数)。

    静态图全程 0 帧变化；GIF 动图至少若干帧 >0。连续窗口采样比单次两帧稳：单次 gap
    若正好撞上 GIF 帧间隔的整数倍会假阴性，连续采样不依赖某个特定 gap。
    """
    img, origin = grab_virtual_screen()
    box = (
        region.left - origin[0], region.top - origin[1],
        region.right - origin[0], region.bottom - origin[1],
    )
    prev = img.crop(box).convert("L")
    changed_frames = 0
    total_frames = 0
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(interval)
        img, _ = grab_virtual_screen()
        cur = img.crop(box).convert("L")
        p1, p2 = prev.load(), cur.load()
        n = sum(
            1 for y in range(cur.height) for x in range(cur.width)
            if abs(p1[x, y] - p2[x, y]) > threshold
        )
        total_frames += 1
        if n:
            changed_frames += 1
        prev = cur
    return changed_frames, total_frames


def click_install_button(main, timeout_sec: float = 15) -> bool:
    """破坏性行 25 用例专用：等 WB 详情页「一键安装」出现并按热区中心点它。

    非破坏套件禁止调用（点了会真装 WorkBuddy）。坐标点而非节点 click_input，
    与 click_entry 同理：详情页是 WebView，节点点击不稳。
    """
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        for node in main.descendants():
            try:
                text = (node.window_text() or "").strip()
                rect = node.rectangle()
            except Exception:
                continue
            if INSTALL_BUTTON_TEXT in text and rect.width() > 0 and rect.height() > 0:
                mouse.click(button="left", coords=((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2))
                return True
        time.sleep(0.5)
    return False


def wb_uninstall_command() -> str | None:
    """WB 注册表条目的静默卸载串（优先 QuietUninstallString），供行 25 用例收尾还原。"""
    entry = uninstall_entry(WB_REGISTRY_HINT)
    if not entry:
        return None
    return entry.get("quiet") or entry.get("uninstall") or None


def wb_install_dir() -> str | None:
    """WB 注册表条目的 InstallLocation（按用户装，形如 ...\\Programs\\XiaoshuoClaw）。

    静默卸载只删注册表条目、常把安装目录的文件留下；残留目录会让下一轮安装走「修复」
    分支、装不出注册表条目（实测 180s 超时）。行 25 用例收尾要拿这个路径把目录也删掉，
    保证往返后机器真干净。未装时返回 None。
    """
    entry = uninstall_entry(WB_REGISTRY_HINT)
    if not entry:
        return None
    return entry.get("location") or None
