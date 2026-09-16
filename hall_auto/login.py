from __future__ import annotations

import time

from pywinauto import Desktop

from hall_auto.config import Config
from hall_auto.launch import LaunchError, _press_button, popup_text
from hall_auto.waiting import wait_until, wait_until_or_raise

LOGIN_DIALOG_TIMEOUT_SEC = 15
LOGIN_SUBMIT_TIMEOUT_SEC = 25


def _windows(pid: int) -> list:
    found = []
    for win in Desktop(backend="uia").windows():
        try:
            if win.element_info.process_id == pid:
                found.append(win)
        except Exception:
            continue
    return found


def _nodes(pid: int, control_type: str | None = None):
    for win in _windows(pid):
        try:
            for node in win.descendants(control_type=control_type) if control_type else win.descendants():
                yield node
        except Exception:
            continue


def _by_aid(pid: int, control_type: str, aid: str):
    for node in _nodes(pid, control_type):
        try:
            if (node.element_info.automation_id or "") == aid:
                return node
        except Exception:
            continue
    return None


def _by_name(pid: int, control_type: str, keyword: str, exact: bool = False):
    for node in _nodes(pid, control_type):
        try:
            name = popup_text(node.element_info.name or node.window_text())
        except Exception:
            continue
        if name == keyword if exact else keyword in name:
            return node
    return None


def logged_in(pid: int) -> bool:
    node = _by_aid(pid, "Button", "UserInfoPart")
    return node is not None and "已登录" in popup_text(node.element_info.name)


def login_dialog_open(pid: int) -> bool:
    return _by_aid(pid, "Edit", "MobileInputBox") is not None


def open_login_dialog(pid: int) -> None:
    if login_dialog_open(pid):
        return
    entry = _by_aid(pid, "Button", "UserInfoPart") or _by_name(pid, "Button", "点击登录")
    if entry is None or not _press_button(entry):
        raise LaunchError("登录：点不到登录入口 UserInfoPart")
    wait_until_or_raise(
        lambda: login_dialog_open(pid),
        "登录：弹窗没出现（找不到 MobileInputBox）",
        timeout_sec=LOGIN_DIALOG_TIMEOUT_SEC,
        interval=0.5,
    )


def switch_login_tab(pid: int, keyword: str, ready=None) -> None:
    """切登录弹窗页签。ready 传「切好了」的谓词（如目标页签特有控件出现），不传则不等。"""
    tab = (
        _by_name(pid, "TabItem", keyword)
        or _by_name(pid, "Text", keyword)
        or _by_name(pid, "Button", keyword)
    )
    if tab is None:
        raise LaunchError(f"登录：找不到页签 {keyword!r}")
    if not _press_button(tab):
        raise LaunchError(f"登录：点不动页签 {keyword!r}")
    if ready is not None:
        wait_until_or_raise(ready, f"登录：切到页签 {keyword!r} 后界面没就绪", timeout_sec=10)


def login_field_aids(pid: int) -> dict[str, str]:
    """登录弹窗里的关键控件，供断言用。"""
    found: dict[str, str] = {}
    for node in _nodes(pid):
        try:
            aid = node.element_info.automation_id or ""
            name = popup_text(node.element_info.name or node.window_text())
        except Exception:
            continue
        if aid in ("MobileInputBox", "PasswordSecInput", "CodeInput", "CodeSendBtn"):
            found[aid] = name
        elif name == "同意协议":
            found["agree"] = name
        elif name in ("登录", "忘记密码", "注册账号") or "微软账号登录" in name:
            found.setdefault(name, name)
    return found


def logout(pid: int) -> None:
    """已登录时点用户区会开独立菜单窗（标题「华硕应用商店」），内有退出登录。"""
    if not logged_in(pid):
        return
    entry = _by_aid(pid, "Button", "UserInfoPart")
    if entry is None or not _press_button(entry):
        raise LaunchError("退出登录：点不开用户菜单")
    deadline = time.time() + LOGIN_DIALOG_TIMEOUT_SEC
    while time.time() < deadline:
        button = _by_name(pid, "Button", "退出登录", exact=True)
        if button is not None:
            _press_button(button)
        time.sleep(0.5)
        if not logged_in(pid):
            return
    raise LaunchError("退出登录：点完仍是已登录态")


def _agree_toggled(node) -> bool:
    try:
        return node.get_toggle_state() == 1
    except Exception:
        return False


def login_with_password(cfg: Config, pid: int) -> str:
    user, password = cfg.test_account()
    if not user or not password:
        raise LaunchError("登录：缺凭据，设置 HALL_TEST_USER / HALL_TEST_PASSWORD")
    open_login_dialog(pid)
    switch_login_tab(
        pid,
        "账号密码登录",
        ready=lambda: _by_aid(pid, "Edit", "PasswordSecInput") is not None,
    )
    phone = _by_aid(pid, "Edit", "MobileInputBox")
    secret = _by_aid(pid, "Edit", "PasswordSecInput")
    if phone is None or secret is None:
        raise LaunchError("登录：找不到手机号/密码输入框")
    phone.set_edit_text(user)
    secret.set_edit_text(password)
    agree = _by_name(pid, "CheckBox", "同意协议")
    if agree is not None:
        _press_button(agree)
        # 读不到勾选状态（控件不支持 TogglePattern）时退化成短固定等待，不阻塞登录
        if not wait_until(lambda: _agree_toggled(agree), timeout_sec=2, interval=0.2):
            time.sleep(0.3)
    submit = _by_name(pid, "Button", "登录", exact=True)
    if submit is None or not _press_button(submit):
        raise LaunchError("登录：点不到登录按钮")
    deadline = time.time() + LOGIN_SUBMIT_TIMEOUT_SEC
    while time.time() < deadline:
        if logged_in(pid):
            return popup_text(_by_aid(pid, "Button", "UserInfoPart").element_info.name)
        time.sleep(0.5)
    raise LaunchError("登录：提交后未进入已登录态")
