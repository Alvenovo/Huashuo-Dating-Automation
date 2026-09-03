from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pywinauto import Application
from pywinauto.base_wrapper import ElementNotEnabled

from hall_auto.config import Config, LaunchSettings, REPO_ROOT
from hall_auto.product import EXE_NAME, read_installed, stop_main_process

REPORTS_DIR = REPO_ROOT / "reports" / "launch"


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
        wrapper.capture_as_image().save(path)
        return path
    except Exception:
        return None


def _overlay_windows(main) -> list:
    found = []
    try:
        nodes = main.descendants(control_type="Window")
    except Exception:
        return found
    for node in nodes:
        try:
            name = popup_text(node.element_info.name or node.window_text())
        except Exception:
            continue
        if name:
            found.append((name, node))
    return found


def _click_named_button(overlay, labels: tuple[str, ...]) -> bool:
    try:
        buttons = overlay.descendants(control_type="Button")
    except Exception:
        return False
    lowered = labels
    for btn in buttons:
        try:
            aid = btn.element_info.automation_id or ""
            name = popup_text(btn.element_info.name or btn.window_text())
        except Exception:
            continue
        if aid in ("CancelBtn", "closebtn") and any(x in ("取消", "关闭") for x in lowered):
            try:
                btn.click_input()
                return True
            except (ElementNotEnabled, Exception):
                continue
        if aid == "ConfirmBtn" and any(x in ("同意", "确定") for x in lowered):
            try:
                btn.click_input()
                return True
            except (ElementNotEnabled, Exception):
                continue
        if any(label and label in name for label in lowered):
            try:
                btn.click_input()
                return True
            except (ElementNotEnabled, Exception):
                continue
    return False


def dismiss_whitelist_popups(main, settings: LaunchSettings) -> None:
    for name, overlay in _overlay_windows(main):
        if not is_whitelisted(name, settings):
            continue
        labels = settings.dismiss_buttons if prefer_dismiss(name, settings) else settings.accept_buttons
        if not _click_named_button(overlay, labels):
            _click_named_button(overlay, settings.dismiss_buttons)
        time.sleep(1)


def unknown_popups(main, settings: LaunchSettings) -> list[str]:
    names = []
    for name, _overlay in _overlay_windows(main):
        if not is_whitelisted(name, settings):
            names.append(name)
    return names


def _auto_ids_present(main) -> set[str]:
    found: set[str] = set()
    try:
        descendants = main.descendants()
    except Exception:
        return found
    for node in descendants:
        try:
            aid = node.element_info.automation_id
        except Exception:
            continue
        if aid:
            found.add(aid)
    return found


def _webview_ready(main, settings: LaunchSettings) -> bool:
    try:
        descendants = main.descendants()
    except Exception:
        return False
    for node in descendants:
        try:
            name = popup_text(node.element_info.name)
        except Exception:
            continue
        if any(key and key in name for key in settings.webview_ready_names):
            return True
    return False


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
    last_unknown: list[str] = []
    while time.time() < deadline:
        dismiss_whitelist_popups(main, settings)
        last_unknown = unknown_popups(main, settings)
        if last_unknown:
            evidence = _save_screenshot(main, "unknown_popup")
            raise LaunchError(
                f"未知弹窗: {last_unknown}；截图: {evidence}",
                evidence=evidence,
            )
        if _overlay_windows(main):
            time.sleep(1)
            continue
        present = _auto_ids_present(main)
        missing = [aid for aid in settings.required_auto_ids if aid not in present]
        if not missing and _webview_ready(main, settings):
            return LaunchResult(
                title=popup_text(main.window_text()),
                structure_ids=tuple(settings.required_auto_ids),
            )
        time.sleep(1)
    evidence = _save_screenshot(main, "not_ready")
    present = _auto_ids_present(main)
    missing = [aid for aid in settings.required_auto_ids if aid not in present]
    web = _webview_ready(main, settings)
    leftover = [name for name, _ in _overlay_windows(main)]
    raise LaunchError(
        f"超时未就绪 missing={missing} webview={web} overlays={leftover}；截图: {evidence}",
        evidence=evidence,
    )


def launch_until_ready(cfg: Config) -> LaunchResult:
    app = start_fresh(cfg)
    main = main_window(app, cfg.display_name_contains, cfg.timeouts.launch_sec)
    result = wait_until_ready(cfg, main)
    if cfg.display_name_contains not in result.title:
        raise LaunchError(f"主窗口标题不是 {cfg.display_name_contains!r}: {result.title!r}")
    return result
