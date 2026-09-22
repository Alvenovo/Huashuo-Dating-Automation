"""锁微软登录状态机的**判定顺序**（hall_auto.login.microsoft_state）。

为什么值得一条专门的守卫：`email_otp` 与 `account_picker` 的先后顺序**就是正确性本身**。
「我们向 <邮箱> 发送登录代码。」这一页的正文**含邮箱**，先判 `account_picker`
就会把验证码页认成账号选择器 —— 脚本去点一块纯文本（点不动），然后死等已登录态，
最后报「微软登录：提交后未进入已登录态」。

**这个错报与真正的登录失败同形**：2026-09-22 真机整轮都在这上面绕
（那轮证据里的 `test_microsoft_login_sso/final.png` 是实锤：屏幕上明明是
「发送登录代码 + 【发送验证码】」页）。顺序被谁「顺手整理」成一个 `any()` 就会复发。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from hall_auto import login

pytestmark = pytest.mark.unit

EMAIL = "3330859445@qq.com"


class _Node:
    """只提供 microsoft_state 真正读到的三个字段。"""

    def __init__(self, name: str = "", aid: str = ""):
        self.element_info = SimpleNamespace(name=name, automation_id=aid)


def _state(names: list[str], email: str = EMAIL, *, aid: str = "") -> str:
    nodes = [_Node(name=n) for n in names]
    if aid:
        nodes.append(_Node(aid=aid))
    with (
        mock.patch.object(login, "logged_in", return_value=False),
        mock.patch.object(login, "_ms_nodes", return_value=nodes),
    ):
        return login.microsoft_state(1, object(), email)


# ---------- 判定顺序：本文件存在的理由 ----------


@pytest.mark.unit
def test_email_otp_page_is_not_mistaken_for_account_picker():
    """**关键保护**：验证码页含邮箱，顺序写反就变成 account_picker。"""
    page = ["登录你的 Microsoft 帐户", EMAIL, f"我们向 {EMAIL} 发送登录代码。"]
    assert _state(page) == "email_otp"


@pytest.mark.unit
def test_account_picker_still_detected_without_otp_text():
    """改了顺序不能把真·账号选择器一起改坏：只有邮箱、没有验证码文案时仍要认出来。"""
    assert _state(["选择一个帐户", EMAIL]) == "account_picker"


@pytest.mark.unit
def test_bare_email_is_not_an_otp_page():
    """光有邮箱不算验证码页 —— 否则账号选择器永远判不到。"""
    assert login._is_microsoft_otp_page(EMAIL) is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    ["发送登录代码", "发送验证码", "输入代码", "Send code", "ENTER CODE", "email code"],
)
def test_otp_markers_recognised(text):
    assert login._is_microsoft_otp_page(text) is True


# ---------- 别的分支不能被顺序改动带坏 ----------


@pytest.mark.unit
def test_password_page_wins_over_everything():
    """i0118（要密码）优先级最高：出现就一定是密码页。"""
    assert _state([EMAIL, "输入密码"], aid="i0118") == "password"


@pytest.mark.unit
def test_mfa_page_still_detected():
    assert _state(["验证你的身份", EMAIL]) == "mfa"


@pytest.mark.unit
def test_logged_in_wins_first():
    with (
        mock.patch.object(login, "logged_in", return_value=True),
        mock.patch.object(login, "_ms_nodes", return_value=[_Node(name="发送登录代码")]),
    ):
        assert login.microsoft_state(1, object(), EMAIL) == "logged_in"


@pytest.mark.unit
def test_unknown_page_is_waiting():
    assert _state(["正在加载…"]) == "waiting"


@pytest.mark.unit
def test_no_email_configured_never_returns_account_picker():
    """没配邮箱时不能因为空串 `in` 任何字符串都成立而误判（`"" in "x"` 为真）。"""
    assert _state(["选择一个帐户", EMAIL], email="") == "waiting"
