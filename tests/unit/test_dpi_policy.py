"""显示缩放 / 环境画像模块的纯逻辑单测。

不依赖真实 DPI 状态（跑批机器可能不是 100%），只验可判定的部分：
缩放换算、机器画像字段完整性、节点标识优先级、缩放检查文本分支。
"""

from __future__ import annotations

from unittest import mock

import pytest

from hall_auto import dpi


@pytest.mark.unit
def test_scale_percent_from_dpi():
    """96 DPI = 100%，120 = 125%，144 = 150%，192 = 200%（四舍五入）。"""
    cases = {96: 100, 120: 125, 144: 150, 192: 200, 0: 0}
    for raw, expected in cases.items():
        with mock.patch.object(dpi, "current_dpi", return_value=raw):
            assert dpi.current_scale_percent() == expected, raw


@pytest.mark.unit
def test_target_logpixels_is_100_percent():
    assert dpi.TARGET_LOGPIXELS == 96


@pytest.mark.unit
def test_machine_profile_has_required_fields():
    """多机汇总靠这些字段分组，少一个就没法按维度看结论。"""
    profile = dpi.machine_profile()
    for field in ("node", "os", "python", "python_bits", "dpi", "scale_percent", "screen", "monitors"):
        assert field in profile, field
    assert isinstance(profile["monitors"], int)
    assert isinstance(profile["python_bits"], int)
    assert profile["python_bits"] in (32, 64)


@pytest.mark.unit
def test_check_scale_does_not_gate_non_100_percent():
    """非 100% 是环境事实，不是失败：脚本走物理像素坐标，本机真实 150% 下全套跑通。

    这里返回 ok=False 只表示「不等于 100%」，调用方不得拿它阻断跑批。
    """
    with mock.patch.object(dpi, "current_scale_percent", return_value=150):
        ok, msg = dpi.check_scale()
    assert ok is False
    assert "150%" in msg
    # 措辞必须明确「不影响正确性」，否则后人又误以为要强制改 100%
    assert "不影响正确性" in msg
    assert "Per-Monitor" in msg


@pytest.mark.unit
def test_check_scale_has_no_write_side_effect():
    """本模块只观测不修改：不得再出现写注册表 / 重启 explorer 的路径。"""
    assert not hasattr(dpi, "set_scale_100"), "set_scale_100 已废弃：改注册表设缩放不可靠且方向错误"
    assert not hasattr(dpi, "_set_logpixels")


@pytest.mark.unit
def test_read_dpi_forces_awareness():
    """读 DPI 前必须先设 Per-Monitor Aware，否则读到被缩放虚拟化后的假值（实测 96 vs 真实 144）。"""
    with mock.patch.object(dpi, "ensure_dpi_aware") as ensure, \
         mock.patch.object(dpi.ctypes.windll.user32, "GetDpiForSystem", return_value=144):
        assert dpi.current_dpi() == 144
    ensure.assert_called_once()


@pytest.mark.unit
def test_node_id_prefers_env(monkeypatch):
    monkeypatch.setenv("HALL_NODE_ID", "TEST-PC-07")
    assert dpi.node_id() == "TEST-PC-07"


@pytest.mark.unit
def test_node_id_falls_back_to_hostname(monkeypatch):
    monkeypatch.delenv("HALL_NODE_ID", raising=False)
    with mock.patch.object(dpi, "platform_node", return_value="HOST-A"):
        assert dpi.node_id() == "HOST-A"


@pytest.mark.unit
def test_node_id_never_empty(monkeypatch):
    monkeypatch.delenv("HALL_NODE_ID", raising=False)
    with mock.patch.object(dpi, "platform_node", return_value=""):
        assert dpi.node_id() == "unknown-node"


@pytest.mark.unit
def test_process_dpi_awareness_returns_known_value():
    assert dpi.process_dpi_awareness() in (
        "per-monitor-aware",
        "per-monitor-aware(v2)",
        "system-aware",
        "unaware",
        "unknown",
    )


@pytest.mark.unit
def test_is_interactive_session_returns_bool():
    assert isinstance(dpi.is_interactive_session(), bool)
