"""entries 的纯逻辑单测：入口标签过滤（把首页轮播 WB 磁贴挡在合并热区外）。"""
from __future__ import annotations

from hall_auto.entries import _is_entry_label


def test_bottom_left_entry_labels_pass():
    for name in ("小硕知道", "小硕 x", "WorkBuddy", "  WorkBuddy  "):
        assert _is_entry_label(name), name


def test_home_carousel_wb_tile_rejected():
    # 首页轮播里的 WB 推广磁贴：名字长且带状态后缀，混进来会把热区撑成大半个页面
    for name in (
        "小硕 × WorkBuddy22的3已选择",
        "安装应用：小硕 × WorkBuddy",
        "打开应用：小硕 × WorkBuddy",
        "更新应用：小硕 × WorkBuddy",
    ):
        assert not _is_entry_label(name), name


def test_long_description_rejected():
    assert not _is_entry_label("华硕专属联名AI办公工具 WorkBuddy 桌面AI智能体")


def test_empty_rejected():
    assert not _is_entry_label("")
    assert not _is_entry_label("   ")
