from __future__ import annotations

import pytest

from hall_auto.launch import start_fresh, wait_main_window, wait_until_ready
from hall_auto.login import (
    _by_aid,
    login_dialog_open,
    login_field_aids,
    login_with_password,
    logged_in,
    logout,
    open_login_dialog,
    switch_login_tab,
)
from hall_auto.product import stop_main_process


@pytest.fixture(scope="module")
def ready_pid(cfg):
    """登录弹窗/用户菜单会顶掉主窗口句柄，所以趁窗口健康时记下 pid，后面一律按进程找控件。"""
    app = start_fresh(cfg)
    main = wait_main_window(app, cfg)
    wait_until_ready(cfg, main)
    yield main.element_info.process_id
    stop_main_process(cfg.timeouts.process_stop_sec)


@pytest.mark.login
def test_login_dialog_controls(ready_pid):
    logout(ready_pid)
    open_login_dialog(ready_pid)
    fields = login_field_aids(ready_pid)
    assert "MobileInputBox" in fields, f"缺手机号输入框: {fields}"
    assert "PasswordSecInput" in fields, f"缺密码输入框: {fields}"
    assert "agree" in fields, f"缺同意协议复选框: {fields}"
    assert "登录" in fields, f"缺登录按钮: {fields}"
    assert "忘记密码" in fields and "注册账号" in fields, f"缺找回/注册入口: {fields}"
    switch_login_tab(
        ready_pid,
        "短信验证码登录",
        ready=lambda: _by_aid(ready_pid, "Edit", "CodeInput") is not None,
    )
    sms = login_field_aids(ready_pid)
    assert "CodeInput" in sms, f"短信页签缺验证码输入框: {sms}"
    assert "CodeSendBtn" in sms, f"短信页签缺发送验证码按钮: {sms}"
    assert "PasswordSecInput" not in sms, f"切到短信页签后密码框应消失: {sms}"


@pytest.mark.login
def test_password_login_success(cfg, ready_pid):
    user, password = cfg.test_account()
    if not (user and password):
        pytest.skip("缺凭据：设置 HALL_TEST_USER / HALL_TEST_PASSWORD")
    logout(ready_pid)
    name = login_with_password(cfg, ready_pid)
    assert logged_in(ready_pid)
    assert user[-5:] in name, f"已登录态应带手机号尾号: {name!r}"


@pytest.mark.login
def test_logout_returns_to_anonymous(ready_pid):
    if not logged_in(ready_pid):
        pytest.skip("当前未登录，先跑密码登录用例")
    logout(ready_pid)
    assert not logged_in(ready_pid)
    assert not login_dialog_open(ready_pid)
