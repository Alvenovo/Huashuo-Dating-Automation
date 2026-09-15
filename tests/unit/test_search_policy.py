from __future__ import annotations

import pytest

from hall_auto.search import filter_hits, is_primary_action, title_from_action


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("安装 腾讯QQ", "腾讯QQ"),
        ("打开 QQ浏览器", "QQ浏览器"),
        ("更新 QQ影音", "QQ影音"),
        ("安装应用：小硕 × WorkBuddy", "小硕 × WorkBuddy"),
        ("打开应用：我是大侠", "我是大侠"),
        ("  安装  穿越火线  ", "穿越火线"),
        ("安装 ", None),
        ("返回", None),
        ("更多软件8款", None),
        ("", None),
        ("立即下载 QQ", None),
    ],
)
def test_title_from_action(name, expected):
    assert title_from_action(name) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("一键安装", True),
        ("安装", True),
        ("打开", True),
        ("更新", True),
        ("下载", True),
        ("立即下载", True),
        (" 安装 ", True),
        # 导航栏与结果页按钮都不是详情页主按钮
        ("下载管理", False),
        ("设置", False),
        ("返回", False),
        ("安装 腾讯QQ", False),
        ("", False),
    ],
)
def test_is_primary_action(name, expected):
    assert is_primary_action(name) is expected


@pytest.mark.unit
def test_filter_hits_is_case_insensitive_and_deduped():
    titles = ["腾讯QQ", "QQ浏览器", "QQ影音", "微信", "腾讯QQ"]
    assert filter_hits(titles, "qq") == ["腾讯QQ", "QQ浏览器", "QQ影音"]
    assert filter_hits(titles, "QQ") == ["腾讯QQ", "QQ浏览器", "QQ影音"]
    assert filter_hits(titles, "微信") == ["微信"]
    assert filter_hits(titles, "不存在的词") == []
    assert filter_hits(titles, "") == []
    assert filter_hits([], "qq") == []
