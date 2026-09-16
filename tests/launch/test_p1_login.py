from __future__ import annotations

import sys

import pytest

from hall_auto.launch import start_fresh, wait_main_window, wait_until_ready
from hall_auto.login import (
    _by_aid,
    _by_name,
    login_dialog_open,
    login_field_aids,
    login_with_password,
    login_with_password_value,
    login_with_sms_code,
    logged_in,
    logout,
    open_forgot_password_page,
    open_login_dialog,
    request_forgot_sms,
    request_sms_code,
    submit_forgot_reset,
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


@pytest.mark.login
@pytest.mark.manual
def test_sms_login_manual(cfg, ready_pid):
    """短信登录人在环用例：脚本发验证码后停下，等人工回填手机收到的码。

    会真发短信。只在交互式终端跑（无人值守时 stdin 不是 tty，自动 skip，
    不会发了短信却没人回填）。手机号走 HALL_TEST_USER，不落盘。
    2026-09-16 已按此流程人工实跑通过（尾号 1935，回填后进入已登录态）。
    """
    user, _ = cfg.test_account()
    if not user:
        pytest.skip("缺手机号：设置 HALL_TEST_USER")
    if not sys.stdin.isatty():
        pytest.skip("非交互式终端：短信登录要人工回填验证码，请在自己终端跑 -m manual")
    logout(ready_pid)
    request_sms_code(ready_pid, user)
    code = input("输入手机收到的短信验证码（直接回车放弃）: ").strip()
    if not code:
        pytest.skip("人工放弃回填（验证码已发出）")
    name = login_with_sms_code(ready_pid, code)
    assert logged_in(ready_pid)
    assert user[-5:] in name, f"已登录态应带手机号尾号: {name!r}"
    logout(ready_pid)
    assert not logged_in(ready_pid)


@pytest.mark.login
@pytest.mark.manual
def test_forgot_password_reset_manual(cfg, ready_pid):
    """忘记密码往返用例：真改密码 + 重新登录验证 + 还原回原密码。

    会真把测试号密码改两次（OLD→NEW 验证，再 NEW→OLD 还原），账户最终回到原状，
    HALL_TEST_PASSWORD 不用改、用例可重复跑。需要人工转交 **两个** 短信验证码。
    三个密码值全走环境变量（HALL_TEST_USER / HALL_TEST_PASSWORD / HALL_TEST_NEW_PASSWORD），
    不落盘、不进对话、不进 git。和短信登录一样只在交互式终端跑（非 tty 自动 skip）。
    还原程放在 finally：只要第一次提交发出去了，哪怕新密码验证那步报错，也一定把
    密码压回 OLD（若本就是 OLD，再设一次无害），保证账户不会卡在 NEW 上。
    2026-09-16 先以「点到发送为止」探针验过页面结构和发送链路，本版把真正的重置闭上。
    """
    user, old_password = cfg.test_account()
    new_password = cfg.new_password()
    if not (user and old_password and new_password):
        pytest.skip("缺凭据：设置 HALL_TEST_USER / HALL_TEST_PASSWORD / HALL_TEST_NEW_PASSWORD")
    if not sys.stdin.isatty():
        pytest.skip("非交互式终端：改密码要人工回填两次验证码，请在自己终端跑 -m manual")

    # 第一程 OLD -> NEW
    logout(ready_pid)
    open_forgot_password_page(ready_pid)
    for aid in ("MobileInput", "CodeInput", "CodeSendBtn", "PasswordSecInput", "RePasswordSecInput"):
        assert _by_aid(ready_pid, "Edit", aid) is not None or _by_aid(ready_pid, "Button", aid) is not None, (
            f"忘记密码页缺控件 {aid}"
        )
    assert _by_name(ready_pid, "Text", "忘记密码", exact=True) is not None, "忘记密码页缺标题「忘记密码」"
    request_forgot_sms(ready_pid, user)
    code1 = input("【第1次·改成新密码】输入手机收到的重置验证码（回车放弃）: ").strip()
    if not code1:
        pytest.skip("人工放弃（验证码已发出，密码尚未修改）")
    submit_forgot_reset(ready_pid, code1, new_password)
    # 提交已发出，密码可能已变 NEW（也可能被拒仍是 OLD）；finally 一律压回 OLD
    try:
        logout(ready_pid)
        login_with_password_value(ready_pid, user, new_password)
        assert logged_in(ready_pid), "新密码登录后仍未进入已登录态，重置可能没生效"
    finally:
        # 第二程 NEW -> OLD 还原（幂等开页，不管上一程把窗口留在什么状态）
        logout(ready_pid)
        open_forgot_password_page(ready_pid)
        request_forgot_sms(ready_pid, user)
        code2 = input("【第2次·还原原密码】输入手机收到的重置验证码（回车放弃还原）: ").strip()
        if not code2:
            raise AssertionError(
                "还原中断：账户可能停在【新密码】，请用 HALL_TEST_NEW_PASSWORD 登录后手动改回原密码！"
            )
        submit_forgot_reset(ready_pid, code2, old_password)
        logout(ready_pid)
        login_with_password_value(ready_pid, user, old_password)
        assert logged_in(ready_pid), "原密码登录后仍未进入已登录态，还原可能没生效"
