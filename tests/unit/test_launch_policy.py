from __future__ import annotations

import pytest

from hall_auto.launch import is_whitelisted, matches_button, prefer_dismiss
from hall_auto.config import load_config


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    ["启动卡片窗口", "更新对话框", "用户协议", "欢迎使用"],
)
def test_whitelist_known_popups(name):
    settings = load_config().launch
    assert is_whitelisted(name, settings)


@pytest.mark.unit
def test_unknown_popup_not_whitelisted():
    settings = load_config().launch
    assert not is_whitelisted("致命错误", settings)
    assert not is_whitelisted("", settings)


@pytest.mark.unit
def test_update_and_card_prefer_dismiss():
    settings = load_config().launch
    assert prefer_dismiss("启动卡片窗口", settings)
    assert prefer_dismiss("更新对话框", settings)
    assert not prefer_dismiss("用户协议", settings)
    assert not prefer_dismiss("欢迎使用 华硕大厅", settings)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("aid", "name", "labels", "expected"),
    [
        ("", "关闭", ("关闭", "取消"), True),
        ("", "我选好了", ("关闭", "取消"), False),
        ("", "我知道了", ("我知道了",), True),
        ("closebtn", "", ("关闭",), True),
        ("closebtn", "", ("同意",), False),
        ("ConfirmBtn", "", ("同意", "确定"), True),
        # aid 命中时不再按名称兜底，避免把确认按钮当成关闭按钮点掉
        ("ConfirmBtn", "关闭", ("关闭",), False),
        ("", "关闭", (), False),
        ("", "我已阅读并同意", ("同意",), True),
    ],
)
def test_matches_button(aid, name, labels, expected):
    assert matches_button(aid, name, labels) is expected
