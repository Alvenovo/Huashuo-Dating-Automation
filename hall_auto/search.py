"""P0-06 搜索 / P0-07 详情。

结果页同时挂着第三方广告位，其节点坐标落在 MainWeb 可视区之外，所以标题一律只从
可视区内的「安装/打开/更新 X」主按钮上取，不按整页文本匹配。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from hall_auto.config import Config
from hall_auto.launch import LaunchError, popup_text
from hall_auto.waiting import wait_until_or_raise

SEARCH_BOX_AUTO_ID = "SearchBarInput"
ACTION_PREFIXES = ("安装应用：", "打开应用：", "更新应用：", "安装 ", "打开 ", "更新 ")


def title_from_action(name: str) -> str | None:
    """从主按钮名称取出应用名；不是主按钮则返回 None。"""
    text = popup_text(name)
    for prefix in ACTION_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):].strip() or None
    return None


def filter_hits(titles: list[str], keyword: str) -> list[str]:
    kw = popup_text(keyword).lower()
    if not kw:
        return []
    hits: list[str] = []
    for title in titles:
        if kw in popup_text(title).lower() and title not in hits:
            hits.append(title)
    return hits


def _center_in(node, box) -> bool:
    try:
        rect = node.rectangle()
    except Exception:
        return False
    cx = (rect.left + rect.right) / 2
    cy = (rect.top + rect.bottom) / 2
    return box.left <= cx <= box.right and box.top <= cy <= box.bottom


def viewport(main):
    """主窗口矩形即可视区；结果页挂的广告节点坐标落在窗口之外。"""
    try:
        return main.rectangle()
    except Exception as exc:
        raise LaunchError(f"读不到主窗口矩形: {exc}") from exc


def _buttons(main) -> list:
    try:
        return main.descendants(control_type="Button")
    except Exception:
        return []


def visible_titles(main) -> list[str]:
    box = viewport(main)
    titles: list[str] = []
    for node in _buttons(main):
        if not _center_in(node, box):
            continue
        try:
            title = title_from_action(node.element_info.name)
        except Exception:
            continue
        if title and title not in titles:
            titles.append(title)
    return titles


def _search_box(main):
    try:
        return main.child_window(auto_id=SEARCH_BOX_AUTO_ID, control_type="Edit").wrapper_object()
    except Exception as exc:
        raise LaunchError(f"找不到搜索框 {SEARCH_BOX_AUTO_ID}: {exc}") from exc


def _edit_value(node) -> str | None:
    try:
        return node.get_value()
    except Exception:
        return None


def submit_keyword(cfg: Config, main) -> None:
    box = _search_box(main)
    keyword = cfg.search_keyword
    box.set_edit_text(keyword)
    if _edit_value(box) is not None:
        wait_until_or_raise(
            lambda: _edit_value(box) == keyword,
            f"搜索框内容没变成 {keyword!r}",
            timeout_sec=5,
            interval=0.1,
        )
    else:
        time.sleep(0.3)
    box.type_keys("{ENTER}", set_foreground=True)


def search_hits(cfg: Config, main) -> list[str]:
    """提交关键词并等结果渲染，返回命中的应用名列表（可能少于 search_min_hits）。"""
    submit_keyword(cfg, main)
    deadline = time.time() + cfg.timeouts.ready_sec
    hits: list[str] = []
    while time.time() < deadline:
        hits = filter_hits(visible_titles(main), cfg.search_keyword)
        if len(hits) >= cfg.search_min_hits:
            return hits
        time.sleep(0.5)
    return hits


@dataclass(frozen=True)
class Detail:
    title: str
    primary_action: str


DETAIL_PRIMARY_BUTTONS = ("一键安装", "安装", "打开", "更新", "立即下载", "下载")


def is_primary_action(name: str) -> bool:
    """详情页主按钮。精确匹配：前缀匹配会把导航栏「下载管理」误判成主按钮。"""
    return popup_text(name) in DETAIL_PRIMARY_BUTTONS


def visible_texts(main) -> list[str]:
    box = viewport(main)
    texts: list[str] = []
    for node in main.descendants(control_type="Text"):
        if not _center_in(node, box):
            continue
        try:
            texts.append(popup_text(node.element_info.name))
        except Exception:
            continue
    return texts


def open_detail(main, title: str) -> None:
    """点结果卡片打开详情。只点标题文本，不点「安装 X」按钮——那会触发真实下载。"""
    box = viewport(main)
    for node in main.descendants(control_type="Text"):
        try:
            if popup_text(node.element_info.name) != title:
                continue
        except Exception:
            continue
        if not _center_in(node, box):
            continue
        node.set_focus()
        node.click_input()
        return
    raise LaunchError(f"结果页里找不到可点击的标题「{title}」")


def detail_state(main, title: str) -> Detail | None:
    """详情页要同时看得到应用名称和主操作按钮，缺一即未就绪。"""
    box = viewport(main)
    action = None
    for node in _buttons(main):
        try:
            name = popup_text(node.element_info.name)
        except Exception:
            continue
        if is_primary_action(name) and _center_in(node, box):
            action = name
            break
    if action is None or title not in visible_texts(main):
        return None
    return Detail(title=title, primary_action=action)


def open_detail_until_ready(cfg: Config, main, title: str) -> Detail:
    open_detail(main, title)
    deadline = time.time() + cfg.timeouts.ready_sec
    while time.time() < deadline:
        state = detail_state(main, title)
        if state is not None:
            return state
        time.sleep(0.5)
    raise LaunchError(f"点进「{title}」后详情页没出现主按钮")
