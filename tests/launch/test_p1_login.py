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
    microsoft_pick_account,
    microsoft_send_code,
    open_forgot_password_page,
    open_login_dialog,
    open_microsoft_login,
    request_forgot_sms,
    request_sms_code,
    submit_forgot_reset,
    submit_microsoft_code,
    submit_microsoft_email,
    switch_login_tab,
    wait_microsoft_logged_in,
    wait_microsoft_state,
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
    """P1-B 登录弹窗控件图：手机号/密码/协议/登录/找回/注册齐全，短信页签切换后出验证码框"""
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
    """P1-B 账号密码登录：填手机号+密码勾选协议登录后进入已登录态，用户区带手机号尾号"""
    user, password = cfg.test_account()
    if not (user and password):
        pytest.skip("缺凭据：设置 HALL_TEST_USER / HALL_TEST_PASSWORD")
    logout(ready_pid)
    name = login_with_password(cfg, ready_pid)
    assert logged_in(ready_pid)
    assert user[-5:] in name, f"已登录态应带手机号尾号: {name!r}"


@pytest.mark.login
def test_logout_returns_to_anonymous(ready_pid):
    """P1-B 退出登录：已登录态点退出后回到未登录态，登录弹窗自动关闭"""
    if not logged_in(ready_pid):
        pytest.skip("当前未登录，先跑密码登录用例")
    logout(ready_pid)
    assert not logged_in(ready_pid)
    assert not login_dialog_open(ready_pid)


@pytest.mark.login
def test_microsoft_login_sso(cfg, ready_pid):
    """微软账号登录：填邮箱→下一步→点缓存账号磁贴走 SSO 免密→断言已登录→finally 还原登出。

    设计边界（主流程自动化设计.md）：微软账号测到系统/WebView 登录窗为止；
    出现密码页(i0118) / MFA / 邮箱验证码页则标记人工、不算脚本失败（skip）。
    本机有缓存的 Windows/MS 会话时，点「下一步」直接出账号选择器，可免密 SSO，
    所以这条在专用机上能无人值守跑通；无缓存会话的机器会落到 password / email_otp 分支 skip。
    邮箱走 HALL_MS_USER（或 config.local.yaml 的 accounts.microsoft.email），不进 git、不进对话。
    2026-09-16 探针实跑通过：SSO 后用户区=已登录用户 61935，logout 还原成未登录。
    2026-09-22 真机改走「发送登录代码到邮箱」→ 落 email_otp 分支 skip（见下）。
    """
    email = cfg.microsoft_account()
    if not email:
        pytest.skip("缺微软邮箱：设置 HALL_MS_USER")
    logout(ready_pid)
    ms_win = open_microsoft_login(ready_pid)
    submit_microsoft_email(ms_win, email)
    state = wait_microsoft_state(ready_pid, ms_win, email)
    if state == "password":
        pytest.skip("微软要密码(i0118)：本机无缓存 MS 会话，需人工在自己终端输入，标记人工")
    if state == "mfa":
        pytest.skip("微软要二次验证(MFA)：按设计标记人工，不算脚本失败")
    if state == "email_otp":
        # 2026-09-22 真机新增分支：微软不再直接出账号选择器，而是
        # 「我们向 <邮箱> 发送登录代码。」页（带【发送验证码】按钮），码发到邮箱。
        # 要人开邮箱取码 → 人在环，无人值守**不算失败**（早先误判成 account_picker，
        # 去点一块纯文本然后死等已登录态，报的是「提交后未进入已登录态」，方向全错）。
        # 收码流程在 test_microsoft_login_code_manual（-m manual）。
        pytest.skip(
            "微软要邮箱验证码：需人工开邮箱取码，标记人工；"
            "收码流程见 test_microsoft_login_code_manual（-m manual）"
        )
    if state == "waiting":
        pytest.fail("微软登录点「下一步」后没到任何已知分支（picker/password/mfa/email_otp）")
    try:
        if state == "account_picker":
            microsoft_pick_account(ms_win, email)
        name = wait_microsoft_logged_in(ready_pid)
        assert logged_in(ready_pid), "SSO 后仍未进入已登录态"
        assert "已登录" in name, f"已登录态用户区文案异常: {name!r}"
    finally:
        logout(ready_pid)
    assert not logged_in(ready_pid), "还原登出后应回到未登录态"


@pytest.mark.login
@pytest.mark.manual
def test_microsoft_login_code_manual(cfg, ready_pid):
    """微软登录·邮箱验证码人在环用例：点【发送验证码】→ 人工开邮箱取码回填 → 断言已登录 → 还原登出。

    2026-09-22 真机发现：这台机器点微软入口、填邮箱、点「下一步」后，
    **不再直接出账号选择器**，而是「我们向 <邮箱> 发送登录代码。」页
    （带【发送验证码】按钮），码发到 HALL_MS_USER 那个邮箱，需要真人去收。
    无人值守那条（test_microsoft_login_sso）遇到这一页只 skip，真收码走这条。

    会真发登录代码到邮箱。和短信登录同一个口径：只在交互式终端跑
    （非 tty 自动 skip），免得发了码却没人回填、把账号卡在半路。
    邮箱走 HALL_MS_USER，不落盘、不进 git。
    """
    email = cfg.microsoft_account()
    if not email:
        pytest.skip("缺微软邮箱：设置 HALL_MS_USER")
    if not sys.stdin.isatty():
        pytest.skip("非交互式终端：微软验证码要人工开邮箱取码，请在自己终端跑 -m manual")
    logout(ready_pid)
    ms_win = open_microsoft_login(ready_pid)
    submit_microsoft_email(ms_win, email)
    state = wait_microsoft_state(ready_pid, ms_win, email)
    if state == "account_picker":
        pytest.skip("这台机器走的是账号选择器免密分支（无验证码可收），请跑 test_microsoft_login_sso")
    if state != "email_otp":
        pytest.skip(f"微软没走邮箱验证码分支（当前 {state}）")
    microsoft_send_code(ms_win)
    code = input("微软已向配置的邮箱发送登录代码。输入邮箱收到的验证码（直接回车放弃）: ").strip()
    if not code:
        pytest.skip("人工放弃回填（登录代码已发出）")
    submit_microsoft_code(ms_win, code)
    try:
        name = wait_microsoft_logged_in(ready_pid)
        assert logged_in(ready_pid), "提交验证码后仍未进入已登录态"
        assert "已登录" in name, f"已登录态用户区文案异常: {name!r}"
    finally:
        logout(ready_pid)
    assert not logged_in(ready_pid), "还原登出后应回到未登录态"


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
    if new_password == old_password:
        pytest.skip("新密码与原密码相同，往返验证证明不了密码真被改过；换一个不同的 HALL_TEST_NEW_PASSWORD")
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
    # 提交一旦发出，密码就可能已变 NEW；放进 try，让 finally 的还原程兜住提交本身报错的情况
    try:
        submit_forgot_reset(ready_pid, code1, new_password)
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
