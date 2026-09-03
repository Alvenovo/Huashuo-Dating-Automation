from __future__ import annotations

import pytest

from hall_auto.launch import is_whitelisted, prefer_dismiss
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
