from __future__ import annotations

import time

import win32gui
import win32process
from pywinauto.controls.uiawrapper import UIAWrapper
from pywinauto.uia_element_info import UIAElementInfo

from hall_auto.config import Config
from hall_auto.launch import LaunchError, _press_button, popup_text
from hall_auto.waiting import wait_until, wait_until_or_raise

LOGIN_DIALOG_TIMEOUT_SEC = 15
LOGIN_SUBMIT_TIMEOUT_SEC = 25
CODE_RESEND_TIMEOUT_SEC = 65  # 短信网关冷却常为 60s，还原程紧接第一程再发时要等发送按钮回来


def _windows(pid: int) -> list:
    """本进程的顶层窗口。用 Win32 EnumWindows 按 pid 过滤，比 UIA 枚举整个桌面再逐个读 process_id 快得多。"""
    hwnds: list[int] = []

    def _collect(hwnd, _):
        if win32process.GetWindowThreadProcessId(hwnd)[1] == pid:
            hwnds.append(hwnd)

    win32gui.EnumWindows(_collect, None)
    return [UIAWrapper(UIAElementInfo(h)) for h in hwnds]


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


def _check_agree(pid: int) -> None:
    agree = _by_name(pid, "CheckBox", "同意协议")
    if agree is None or _agree_toggled(agree):
        return
    _press_button(agree)
    # 读不到勾选状态（控件不支持 TogglePattern）时退化成短固定等待，不阻塞登录
    if not wait_until(lambda: _agree_toggled(agree), timeout_sec=2, interval=0.2):
        time.sleep(0.3)


def request_sms_code(pid: int, user: str) -> None:
    """填手机号、勾协议、点「发送验证码」。

    点完 CodeSendBtn 节点会从控件树里消失（倒计时态不暴露按钮，2026-09-16 实测），
    所以发送成功与否不能按按钮签名断言，以手机收到短信为准。
    """
    open_login_dialog(pid)
    switch_login_tab(
        pid,
        "短信验证码登录",
        ready=lambda: _by_aid(pid, "Edit", "CodeInput") is not None,
    )
    phone = _by_aid(pid, "Edit", "MobileInputBox")
    if phone is None:
        raise LaunchError("登录：找不到手机号输入框")
    phone.set_edit_text(user)
    _check_agree(pid)
    send = _by_aid(pid, "Button", "CodeSendBtn")
    if send is None or not _press_button(send):
        raise LaunchError("登录：点不到「发送验证码」")


def login_with_sms_code(pid: int, code: str) -> str:
    box = _by_aid(pid, "Edit", "CodeInput")
    if box is None:
        raise LaunchError("登录：找不到验证码输入框")
    box.set_edit_text(code)
    submit = _by_name(pid, "Button", "登录", exact=True)
    if submit is None or not _press_button(submit):
        raise LaunchError("登录：点不到登录按钮")
    deadline = time.time() + LOGIN_SUBMIT_TIMEOUT_SEC
    while time.time() < deadline:
        if logged_in(pid):
            return popup_text(_by_aid(pid, "Button", "UserInfoPart").element_info.name)
        time.sleep(0.5)
    raise LaunchError("登录：提交验证码后未进入已登录态")


def open_forgot_password_page(pid: int) -> None:
    """从登录弹窗点「忘记密码」，登录窗口原地换成忘记密码页（客户端内 WPF 页）。

    幂等：已在忘记密码页（MobileInput 在）就直接返回，方便往返用例的还原程
    不管上一程把登录窗口留在什么状态都能重新导航到位。
    入口必须 Button + 全名精确匹配：密码提示文案「…可点击忘记密码进行重置」
    也含这四个字，模糊匹配会点到 Text 上毫无反应（2026-09-16 实测踩过）。
    """
    if _by_aid(pid, "Edit", "MobileInput") is not None:
        return
    open_login_dialog(pid)
    entry = _by_name(pid, "Button", "忘记密码", exact=True)
    if entry is None or not _press_button(entry):
        raise LaunchError("登录：点不到「忘记密码」")
    # 忘记密码页手机号框 aid=MobileInput（登录弹窗是 MobileInputBox，注册页才是 MobileInput）
    wait_until_or_raise(
        lambda: _by_aid(pid, "Edit", "MobileInput") is not None,
        "登录：忘记密码页没出现（找不到 MobileInput）",
        timeout_sec=LOGIN_DIALOG_TIMEOUT_SEC,
        interval=0.5,
    )


def request_forgot_sms(pid: int, user: str) -> None:
    """忘记密码页填手机号并点「发送验证码」。页面没有协议勾选框。

    点完 CodeSendBtn 从控件树消失（与登录短信页同一套倒计时实现），
    以消失作为已触发发送的信号，最终以手机收到短信为准。
    注意别拿 SetBtn 当页面判据：主窗口导航栏的「设置」按钮 aid 也是 SetBtn。
    """
    phone = _by_aid(pid, "Edit", "MobileInput")
    if phone is None:
        raise LaunchError("忘记密码：找不到手机号输入框")
    phone.set_edit_text(user)
    # 还原程紧接第一程再发时可能还在冷却（CodeSendBtn 未回控件树），先等它出现再点
    if not wait_until(
        lambda: _by_aid(pid, "Button", "CodeSendBtn") is not None,
        timeout_sec=CODE_RESEND_TIMEOUT_SEC,
        interval=0.5,
    ):
        raise LaunchError("忘记密码：等不到「发送验证码」按钮（可能仍在冷却）")
    send = _by_aid(pid, "Button", "CodeSendBtn")
    if not _press_button(send):
        raise LaunchError("忘记密码：点不动「发送验证码」")
    if not wait_until(lambda: _by_aid(pid, "Button", "CodeSendBtn") is None, timeout_sec=8, interval=0.5):
        raise LaunchError("忘记密码：点发送后 CodeSendBtn 没消失，可能没触发")


def submit_forgot_reset(pid: int, code: str, new_password: str) -> dict:
    """忘记密码页填验证码 + 新密码 + 确认密码，点「提交」完成重置。

    手机号和发送已由 request_forgot_sms 做好。提交按钮 aid=SetBtn 和主窗口导航栏
    「设置」按钮同 aid，必须按 name「提交」精确挑，别拿 aid 撞（会点到设置）。
    提交后页面行为未知（可能自动登录、可能关窗回登录页），以「忘记密码页关闭
    （MobileInput 消失）或进入已登录态」为成功信号；都不发生说明验证码/密码被拒。
    调用方拿到成功信号后仍应 logout 再用新密码登录一次，才算真验到重置生效。
    返回 {"page_closed": bool, "logged_in": bool}，标明命中了哪个信号，方便定位。
    """
    code_box = _by_aid(pid, "Edit", "CodeInput")
    secret = _by_aid(pid, "Edit", "PasswordSecInput")
    confirm = _by_aid(pid, "Edit", "RePasswordSecInput")
    if code_box is None or secret is None or confirm is None:
        raise LaunchError("忘记密码：找不到验证码/新密码/确认密码输入框")
    code_box.set_edit_text(code)
    secret.set_edit_text(new_password)
    confirm.set_edit_text(new_password)
    submit = _by_name(pid, "Button", "提交", exact=True)
    if submit is None or not _press_button(submit):
        raise LaunchError("忘记密码：点不到「提交」按钮")
    deadline = time.time() + LOGIN_SUBMIT_TIMEOUT_SEC
    page_closed = False
    loggedin = False
    while time.time() < deadline:
        page_closed = _by_aid(pid, "Edit", "MobileInput") is None
        loggedin = logged_in(pid)
        if page_closed or loggedin:
            return {"page_closed": page_closed, "logged_in": loggedin}
        time.sleep(0.5)
    raise LaunchError(
        f"忘记密码：提交后页面没关也没登录（page_closed={page_closed}, logged_in={loggedin}），可能验证码错或新密码不合法"
    )


def login_with_password(cfg: Config, pid: int) -> str:
    user, password = cfg.test_account()
    if not user or not password:
        raise LaunchError("登录：缺凭据，设置 HALL_TEST_USER / HALL_TEST_PASSWORD")
    return login_with_password_value(pid, user, password)


def login_with_password_value(pid: int, user: str, password: str) -> str:
    """账号密码页登录，凭据显式传入（改密码往返用例要拿新密码登一次验证）。"""
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
    _check_agree(pid)
    submit = _by_name(pid, "Button", "登录", exact=True)
    if submit is None or not _press_button(submit):
        raise LaunchError("登录：点不到登录按钮")
    deadline = time.time() + LOGIN_SUBMIT_TIMEOUT_SEC
    while time.time() < deadline:
        if logged_in(pid):
            return popup_text(_by_aid(pid, "Button", "UserInfoPart").element_info.name)
        time.sleep(0.5)
    raise LaunchError("登录：提交后未进入已登录态")
