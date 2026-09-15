from __future__ import annotations

import time

import pytest

from hall_auto.launch import popup_text, start_fresh, wait_main_window, wait_until_ready
from hall_auto.login import _nodes, logout
from hall_auto.product import stop_main_process
from hall_auto.register import (
    click_register,
    fill_register,
    open_register_page,
    register_controls,
    register_enabled,
    register_open,
    set_agree,
    tip_red_flags,
)

# 公开示例号段 + 假验证码；全程不点 CodeSendBtn，服务端不可能放行注册
FAKE_PHONE = "13800138000"
FAKE_CODE = "000000"
VALID_PASSWORD = "Abcdef123"

# (密码, 确认密码, 期望红提示)：第一行是密码格式提示，第二行是确认密码提示
INVALID_CASES = [
    pytest.param("ab1", "ab1", (True, True), id="short-3"),
    pytest.param("12345678", "12345678", (True, True), id="digits-only"),
    pytest.param("abc中文123", "abc中文123", (True, True), id="chinese"),
    pytest.param("A1" * 20, "A1" * 20, (True, True), id="long-40"),
    pytest.param("", "", (True, True), id="empty-both"),
    pytest.param(VALID_PASSWORD, "Abcdef124", (False, True), id="mismatch"),
    pytest.param(VALID_PASSWORD, "", (False, True), id="empty-confirm"),
]


@pytest.fixture(scope="module")
def register_pid(cfg):
    app = start_fresh(cfg)
    main = wait_main_window(app, cfg)
    wait_until_ready(cfg, main)
    pid = main.element_info.process_id
    logout(pid)
    open_register_page(pid)
    yield pid
    stop_main_process(cfg.timeouts.process_stop_sec)


@pytest.mark.login
def test_register_controls_present(register_pid):
    controls = register_controls(register_pid)
    missing = [key for key, ok in controls.items() if not ok]
    assert not missing, f"注册页缺控件: {missing}（全部: {controls}）"


@pytest.mark.login
def test_register_submit_gate(register_pid):
    fill_register(register_pid, FAKE_PHONE, FAKE_CODE, VALID_PASSWORD, VALID_PASSWORD)
    set_agree(register_pid, False)
    assert not register_enabled(register_pid), "未勾协议时注册按钮应禁用"
    set_agree(register_pid, True)
    assert register_enabled(register_pid), "勾协议且四项都合法时注册按钮应可用"
    fill_register(register_pid, FAKE_PHONE, FAKE_CODE, "", "")
    assert not register_enabled(register_pid), "密码为空时注册按钮应禁用"
    assert tip_red_flags(register_pid) == (True, True), "空密码应两行提示都变红"


@pytest.mark.login
@pytest.mark.parametrize(("password", "confirm", "expected_tips"), INVALID_CASES)
def test_register_invalid_password_blocks_submit(register_pid, password, confirm, expected_tips):
    fill_register(register_pid, FAKE_PHONE, FAKE_CODE, password, confirm)
    set_agree(register_pid, True)
    assert not register_enabled(register_pid), f"非法密码 {password!r}/{confirm!r} 应禁用注册按钮"
    assert tip_red_flags(register_pid) == expected_tips, (
        f"密码 {password!r}/确认 {confirm!r} 的红提示应为 {expected_tips}"
    )


@pytest.mark.login
def test_register_valid_password_keeps_tips_gray(register_pid):
    fill_register(register_pid, FAKE_PHONE, FAKE_CODE, VALID_PASSWORD, VALID_PASSWORD)
    set_agree(register_pid, True)
    assert register_enabled(register_pid)
    assert tip_red_flags(register_pid) == (False, False), "合法密码不应有红提示"


@pytest.mark.login
@pytest.mark.xfail(
    strict=False,
    reason="疑似缺陷：验证码未发送/错误时点「注册并登录」无任何反馈（无 toast、无红字、窗口不关），待与开发确认",
)
def test_register_wrong_code_shows_feedback(register_pid):
    fill_register(register_pid, FAKE_PHONE, FAKE_CODE, VALID_PASSWORD, VALID_PASSWORD)
    set_agree(register_pid, True)
    before = {
        (n.element_info.control_type, n.element_info.automation_id, popup_text(n.element_info.name or ""))
        for n in _nodes(register_pid)
        if popup_text(n.element_info.name or "")
    }
    assert click_register(register_pid)
    time.sleep(5)
    feedback = []
    if not register_open(register_pid):
        feedback.append("注册窗口关闭")
    now = {
        (n.element_info.control_type, n.element_info.automation_id, popup_text(n.element_info.name or ""))
        for n in _nodes(register_pid)
        if popup_text(n.element_info.name or "")
    }
    feedback += [name for _, _, name in (now - before)]
    assert feedback, "点提交后应出现错误反馈（toast/红字/关窗任一）"
