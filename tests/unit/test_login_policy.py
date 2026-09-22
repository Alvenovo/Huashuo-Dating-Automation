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

EMAIL = "someone@outlook.com"  # 假值：真邮箱只走 HALL_MS_USER，不进仓库


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


# ---------- 提交验证码：不许"抓一次快照就下结论" ----------
#
# 2026-09-22 真机报「填了验证码但点不到提交按钮」。三个坑：
#   ① 按钮要等页面校验完才变可点 → 抓一次就放弃，太早；
#   ② `set_edit_text` 走 UIA ValuePattern，webview 的 React 收不到 input 事件，
#      它自己的 state 还是空 → 按钮一直 disabled；
#   ③ 候选词里的「验证」会撞上【发送验证码】→ 点下去是**重发**，人白等一个码。


def _clicks(nodes, *, press_returns: bool = True):
    """跑一轮 `_press_submit_once`，返回 (是否点到, 被点到的 (aid, name) 列表)。"""
    clicked: list[tuple[str, str]] = []

    def _press(node):
        info = node.element_info
        clicked.append((info.automation_id, info.name))
        return press_returns

    with (
        mock.patch.object(login, "_ms_nodes", return_value=nodes),
        mock.patch.object(login, "_press_button", side_effect=_press),
    ):
        return login._press_submit_once(object(), []), clicked


@pytest.mark.unit
def test_submit_never_clicks_the_resend_button():
    """**关键保护**：候选词「验证」会匹配到【发送验证码】。

    点错的表现是「看着成功、实际重发」—— 比报错更难查：码又发了一遍，
    人拿着旧码一直提交失败，而日志里一切正常。
    """
    ok, clicked = _clicks([_Node(name="发送验证码"), _Node(name="下一步")])
    assert ok is True
    assert clicked == [("", "下一步")], clicked


@pytest.mark.unit
def test_submit_prefers_the_standard_aid():
    """有标准 aid 就别靠中文文案猜 —— 文案会变，aid 相对稳。"""
    ok, clicked = _clicks([_Node(name="下一步"), _Node(aid="idSIButton9")])
    assert ok is True
    assert clicked == [("idSIButton9", "")], clicked


@pytest.mark.unit
def test_submit_retries_instead_of_giving_up_on_the_first_snapshot():
    """按钮要等页面校验完才可点 —— 一次快照抓不到就放弃，正是 09-22 那个报错。"""
    calls = {"n": 0}

    def _nodes(_win):
        calls["n"] += 1
        return [] if calls["n"] < 3 else [_Node(name="下一步")]

    clock = {"t": 0.0}

    def _now():
        clock["t"] += 1.0
        return clock["t"]

    with (
        mock.patch.object(login, "_ms_nodes", side_effect=_nodes),
        mock.patch.object(login, "_press_button", return_value=True),
        mock.patch.object(login.time, "sleep"),
        mock.patch.object(login.time, "time", side_effect=_now),
    ):
        assert login._wait_submit(object(), []) is True
    assert calls["n"] >= 3, "只在第一轮找过一次就没再找"


@pytest.mark.unit
def test_submit_error_says_whether_anything_was_found():
    """报错要能一眼分清「压根没找到」和「找到了点不动」—— 下一步排查方向完全相反。"""
    with (
        mock.patch.object(login, "wait_microsoft_code_input", return_value=mock.Mock()),
        mock.patch.object(login, "_ms_nodes", return_value=[_Node(name="发送验证码")]),
        mock.patch.object(login, "_press_button", return_value=False),
        mock.patch.object(login, "_bring_to_front", return_value=False),
        mock.patch.object(login, "dump_microsoft_tree", return_value=""),
        mock.patch.object(login.time, "sleep"),
        mock.patch.object(login, "MICROSOFT_OTP_SUBMIT_TIMEOUT_SEC", 0),
    ):
        with pytest.raises(login.LaunchError) as exc:
            login.submit_microsoft_code(object(), "123456")
    assert "一个都没找到" in str(exc.value)


@pytest.mark.unit
def test_submit_does_not_type_keys_when_the_window_is_not_in_front():
    """**关键保护**：补真键盘前必须确认窗口在前台。

    确认漏了就会往**别人的窗口**里打字 —— 刚被 `focus_console()` 提到最前的
    终端就是最可能的受害者（用户正等着在那儿输码）。
    """
    typed: list[str] = []
    box = mock.Mock()
    box.type_keys.side_effect = lambda *a, **k: typed.append(str(a))

    with (
        mock.patch.object(login, "wait_microsoft_code_input", return_value=box),
        mock.patch.object(login, "_ms_nodes", return_value=[_Node(name="发送验证码")]),
        mock.patch.object(login, "_press_button", return_value=False),
        mock.patch.object(login, "_bring_to_front", return_value=False),
        mock.patch.object(login, "dump_microsoft_tree", return_value=""),
        mock.patch.object(login.time, "sleep"),
        mock.patch.object(login, "MICROSOFT_OTP_SUBMIT_TIMEOUT_SEC", 0),
    ):
        with pytest.raises(login.LaunchError):
            login.submit_microsoft_code(object(), "123456")
    assert typed == [], f"窗口不在前台还敲了键盘：{typed}"
