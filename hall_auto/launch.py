from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pywinauto import Application
from pywinauto.controls.uiawrapper import UIAWrapper
from pywinauto.uia_element_info import UIAElementInfo

from hall_auto.config import Config, LaunchSettings, REPO_ROOT
from hall_auto.product import EXE_NAME, read_installed, stop_main_process
from hall_auto.winapi import _hwnd_pid, _hwnd_text, _hwnd_visible, _top_hwnds

REPORTS_DIR = REPO_ROOT / "reports" / "launch"

HOME_TAB_NAME = "推荐"


class LaunchError(RuntimeError):
    def __init__(self, message: str, evidence: Path | None = None):
        super().__init__(message)
        self.evidence = evidence


@dataclass(frozen=True)
class LaunchResult:
    title: str
    structure_ids: tuple[str, ...]
    evidence: Path | None = None


def popup_text(name: str) -> str:
    return (name or "").strip()


def is_whitelisted(name: str, settings: LaunchSettings) -> bool:
    text = popup_text(name)
    return any(key and key in text for key in settings.popup_whitelist_keywords)


def prefer_dismiss(name: str, settings: LaunchSettings) -> bool:
    text = popup_text(name)
    return any(key and key in text for key in settings.update_or_card_keywords)


def _save_screenshot(wrapper, stem: str) -> Path | None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{stem}.png"
    try:
        # capture_as_image 抓的是屏幕像素，不置顶会拍到挡在前面的窗口
        wrapper.set_focus()
        time.sleep(0.5)
    except Exception:
        pass
    try:
        wrapper.capture_as_image().save(path)
        return path
    except Exception:
        return None


@dataclass(frozen=True)
class Overlay:
    name: str
    node: object
    first_run: bool = False


def _wrap_hwnd(hwnd: int):
    try:
        return UIAWrapper(UIAElementInfo(hwnd))
    except Exception:
        return None


def _overlay_windows(main, settings: LaunchSettings) -> list[Overlay]:
    """大厅自己弹的覆盖层：主窗口下的子 Window + 同进程的其他顶层窗。

    顶层窗用 Win32 EnumWindows 按 pid 过滤（0.00s 级），只把命中的 hwnd 包成
    UIA 节点；Desktop().windows() 全系统 UIA 枚举实测 1s 左右，就绪循环每轮
    要跑多次，用不起。first_run 只对非白名单顶层窗算 —— 首跑协议窗是独立
    顶层窗，子 Window 用不着走那次昂贵的 Button 子树遍历。
    """
    found: list[Overlay] = []
    seen = set()
    try:
        main_handle = main.handle
    except Exception:
        main_handle = None

    def _add(name: str, node, top_level: bool) -> None:
        try:
            handle = node.handle
        except Exception:
            handle = id(node)
        if handle in seen or (main_handle is not None and handle == main_handle):
            return
        if not name:
            return
        seen.add(handle)
        first_run = (
            top_level
            and not is_whitelisted(name, settings)
            and _looks_like_first_run(node)
        )
        found.append(Overlay(name=name, node=node, first_run=first_run))

    try:
        nodes = main.descendants(control_type="Window")
    except Exception:
        nodes = []
    for node in nodes:
        try:
            name = popup_text(node.element_info.name or node.window_text())
        except Exception:
            continue
        _add(name, node, top_level=False)

    try:
        pid = main.element_info.process_id
    except Exception:
        pid = None
    if pid:
        for hwnd in _top_hwnds():
            # EnumWindows 连隐藏的消息窗（Default IME 之类）一起给，UIA 的
            # Desktop 枚举不会，必须自己过滤，否则全被当成未知弹窗
            if not _hwnd_visible(hwnd) or _hwnd_pid(hwnd) != pid:
                continue
            if main_handle is not None and hwnd == main_handle:
                continue
            node = _wrap_hwnd(hwnd)
            if node is None:
                continue
            name = popup_text(_hwnd_text(hwnd))
            if not name:
                try:
                    name = popup_text(node.element_info.name)
                except Exception:
                    continue
            _add(name, node, top_level=True)
    return found


def click_first_run_agree(main, settings: LaunchSettings, present: set[str]) -> bool:
    """首次协议画在主窗口上时，没有子 Window，直接点「同意」。更新框有 VersionLabel 则不点同意。"""
    if settings.required_auto_ids[0] in present:
        return False
    if "VersionLabel" in present or "ChangeLog" in present:
        return False
    return _click_named_button(main, settings.accept_buttons)


def _press_button(btn) -> bool:
    """优先 UIA Invoke：不依赖鼠标坐标和窗口层级。click_input 在大厅被遮挡时会点到遮挡窗口上。"""
    try:
        btn.invoke()
        return True
    except Exception:
        pass
    try:
        btn.set_focus()
        btn.click_input()
        return True
    except Exception:
        return False


def matches_button(aid: str, name: str, labels: tuple[str, ...]) -> bool:
    if aid in ("CancelBtn", "closebtn"):
        return any(x in ("取消", "关闭") for x in labels)
    if aid == "ConfirmBtn":
        return any(x in ("同意", "确定") for x in labels)
    return any(label and label in name for label in labels)


def _click_named_button(overlay, labels: tuple[str, ...]) -> bool:
    try:
        buttons = overlay.descendants(control_type="Button")
    except Exception:
        return False
    for btn in buttons:
        try:
            aid = btn.element_info.automation_id or ""
            name = popup_text(btn.element_info.name or btn.window_text())
        except Exception:
            continue
        if matches_button(aid, name, labels) and _press_button(btn):
            return True
    return False


def _looks_like_first_run(overlay) -> bool:
    labels = []
    try:
        buttons = overlay.descendants(control_type="Button")
    except Exception:
        return False
    for btn in buttons:
        try:
            labels.append(popup_text(btn.element_info.name or btn.window_text()))
        except Exception:
            continue
    return any("同意" == x or x.endswith("同意") for x in labels) and any(
        "取消" in x for x in labels
    )


def dismiss_whitelist_popups(overlays, settings: LaunchSettings) -> None:
    for overlay in overlays:
        name = overlay.name
        first_run = overlay.first_run
        if not (is_whitelisted(name, settings) or first_run):
            continue
        if prefer_dismiss(name, settings) and not first_run:
            labels = settings.dismiss_buttons
        else:
            labels = settings.accept_buttons
        if not _click_named_button(overlay.node, labels) and labels != settings.dismiss_buttons:
            _click_named_button(overlay.node, settings.dismiss_buttons)
        time.sleep(1)


def is_unknown(overlay: Overlay, settings: LaunchSettings) -> bool:
    return not (is_whitelisted(overlay.name, settings) or overlay.first_run)


def _back_if_subpage(main) -> bool:
    """大厅会恢复上次的页面，点回首页。实测重装后首跑落在设置/关于页（有
    BackBtn），应用详情页连 BackBtn 都没有，所以三级兜底：aid=BackBtn →
    名叫「返回」的按钮 → 侧边栏「推荐」页签。"""
    home = None
    try:
        buttons = main.descendants(control_type="Button")
    except Exception:
        buttons = []
    for node in buttons:
        try:
            aid = node.element_info.automation_id or ""
            if aid == "BackBtn":
                return _press_button(node)
            name = popup_text(node.element_info.name or node.window_text())
            if name == "返回":
                return _press_button(node)
        except Exception:
            continue
    try:
        items = main.descendants(control_type="ListItem")
    except Exception:
        items = []
    for node in items:
        try:
            name = popup_text(node.element_info.name or node.window_text())
        except Exception:
            continue
        if name == HOME_TAB_NAME:
            home = node
            break
    return home is not None and _press_button(home)


def _structure(main, settings: LaunchSettings) -> tuple[set[str], bool]:
    """一次全树遍历同时收 AutomationId 集合和 webview 就绪标志。

    descendants() 全树遍历实测 1.4s 一次，就绪循环每轮原来要走两次
    （查 id 一次、查 webview 一次），合成一次省一半。
    """
    aids: set[str] = set()
    webview = False
    try:
        descendants = main.descendants()
    except Exception:
        return aids, webview
    for node in descendants:
        try:
            info = node.element_info
            aid = info.automation_id
            if aid:
                aids.add(aid)
            if not webview:
                name = popup_text(info.name)
                if name and any(key in name for key in settings.webview_ready_names if key):
                    webview = True
        except Exception:
            continue
    return aids, webview


def connect_app(timeout_sec: int) -> Application:
    deadline = time.time() + timeout_sec
    last = None
    while time.time() < deadline:
        try:
            return Application(backend="uia").connect(path=EXE_NAME, timeout=3)
        except Exception as exc:
            last = exc
            time.sleep(1)
    raise LaunchError(f"未能连接到 {EXE_NAME}: {last}")


def main_window(app: Application, title_contains: str, timeout_sec: int):
    deadline = time.time() + timeout_sec
    last = None
    while time.time() < deadline:
        try:
            win = app.window(title_re=f".*{title_contains}.*", class_name="Window")
            win.wait("visible", timeout=3)
            return win
        except Exception as exc:
            last = exc
            time.sleep(1)
    raise LaunchError(f"未出现主窗口（标题含 {title_contains}）: {last}")


def dismiss_pre_main_popups(app: Application, settings: LaunchSettings) -> bool:
    """首跑协议/权限页是独立顶层窗口（实测叫「权限确认窗口」），点完同意主窗口才出现，所以等主窗口之前就要关它。

    枚举走 Win32 按 pid 过滤（等主窗口时这个循环每秒都在跑，UIA 全系统枚举用不起）。
    """
    clicked = False
    try:
        pid = app.process
    except Exception:
        return False
    for hwnd in _top_hwnds():
        if not _hwnd_visible(hwnd) or _hwnd_pid(hwnd) != pid:
            continue
        win = _wrap_hwnd(hwnd)
        if win is None:
            continue
        name = popup_text(_hwnd_text(hwnd))
        if not name:
            try:
                name = popup_text(win.element_info.name)
            except Exception:
                continue
        if not name:
            continue
        first_run = not is_whitelisted(name, settings) and _looks_like_first_run(win)
        if not (is_whitelisted(name, settings) or first_run):
            continue
        if prefer_dismiss(name, settings) and not first_run:
            labels = settings.dismiss_buttons
        else:
            labels = settings.accept_buttons
        if _click_named_button(win, labels):
            clicked = True
            time.sleep(0.5)
    return clicked


def wait_main_window(app: Application, cfg: Config):
    deadline = time.time() + cfg.timeouts.launch_sec
    last = None
    while time.time() < deadline:
        dismiss_pre_main_popups(app, cfg.launch)
        try:
            return main_window(app, cfg.display_name_contains, 3)
        except LaunchError as exc:
            last = exc
            time.sleep(1)
    raise LaunchError(f"未出现主窗口（标题含 {cfg.display_name_contains}）: {last}")


def start_fresh(cfg: Config) -> Application:
    info = read_installed()
    if info is None or not info.exe_path.is_file():
        raise LaunchError("未安装华硕大厅，无法启动")
    stop_main_process(cfg.timeouts.process_stop_sec)
    time.sleep(1)
    subprocess.Popen([str(info.exe_path)], cwd=str(info.exe_path.parent))
    return connect_app(cfg.timeouts.launch_sec)


def wait_until_ready(cfg: Config, main) -> LaunchResult:
    settings = cfg.launch
    deadline = time.time() + cfg.timeouts.ready_sec
    while time.time() < deadline:
        # 每轮只枚举一次覆盖层，白名单/未知判定复用同一份列表
        overlays = _overlay_windows(main, settings)
        dismiss_whitelist_popups(overlays, settings)
        unknown = [o.name for o in overlays if is_unknown(o, settings)]
        if unknown:
            evidence = _save_screenshot(main, "unknown_popup")
            raise LaunchError(
                f"未知弹窗: {unknown}；截图: {evidence}",
                evidence=evidence,
            )
        if overlays:
            time.sleep(0.5)
            continue
        present, webview = _structure(main, settings)
        click_first_run_agree(main, settings, present)
        missing = [aid for aid in settings.required_auto_ids if aid not in present]
        if missing and _back_if_subpage(main):
            time.sleep(0.5)
            continue
        if not missing and webview:
            return LaunchResult(
                title=popup_text(main.window_text()),
                structure_ids=tuple(settings.required_auto_ids),
            )
        time.sleep(0.5)
    evidence = _save_screenshot(main, "not_ready")
    present, web = _structure(main, settings)
    missing = [aid for aid in settings.required_auto_ids if aid not in present]
    leftover = [o.name for o in _overlay_windows(main, settings)]
    raise LaunchError(
        f"超时未就绪 missing={missing} webview={web} overlays={leftover}；截图: {evidence}",
        evidence=evidence,
    )


def launch_until_ready(cfg: Config) -> LaunchResult:
    app = start_fresh(cfg)
    main = wait_main_window(app, cfg)
    result = wait_until_ready(cfg, main)
    if cfg.display_name_contains not in result.title:
        raise LaunchError(f"主窗口标题不是 {cfg.display_name_contains!r}: {result.title!r}")
    return result
