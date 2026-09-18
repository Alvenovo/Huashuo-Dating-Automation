"""锁屏幕常亮策略（hall_auto/awake.py）。

组长 2026-09-18 确认测试机无人碰屏，脚本必须自己保证不息屏 —— 息屏后抓屏全黑，
点击无落点，产出的是假失败。这里钉住 API 用法与还原行为。
"""

from __future__ import annotations

from unittest import mock

from hall_auto import awake


def test_start_sets_all_flags():
    """三个标志位必须一起传：CONTINUOUS 持续生效，SYSTEM_REQUIRED 防睡眠，DISPLAY_REQUIRED 防息屏。"""
    with mock.patch.object(awake.ctypes.windll.kernel32, "SetThreadExecutionState", return_value=1) as call:
        assert awake.keep_awake_start() is True
    flags = call.call_args.args[0]
    assert flags & awake.ES_CONTINUOUS
    assert flags & awake.ES_SYSTEM_REQUIRED
    assert flags & awake.ES_DISPLAY_REQUIRED


def test_stop_resets_to_continuous_only():
    """还原时只传 ES_CONTINUOUS，去掉要求位，让系统回到默认电源行为。"""
    with mock.patch.object(awake.ctypes.windll.kernel32, "SetThreadExecutionState", return_value=1) as call:
        assert awake.keep_awake_stop() is True
    assert call.call_args.args[0] == awake.ES_CONTINUOUS


def test_context_manager_restores():
    """上下文退出必须还原，否则脚本被强杀后机器一直不睡。"""
    with mock.patch.object(awake.ctypes.windll.kernel32, "SetThreadExecutionState", return_value=1) as call:
        with awake.keep_awake():
            assert call.call_count == 1
    assert call.call_count == 2
    assert call.call_args.args[0] == awake.ES_CONTINUOUS


def test_context_manager_restores_on_exception():
    with mock.patch.object(awake.ctypes.windll.kernel32, "SetThreadExecutionState", return_value=1) as call:
        try:
            with awake.keep_awake():
                raise RuntimeError("用例炸了")
        except RuntimeError:
            pass
    assert call.call_count == 2, "异常路径也必须还原"


def test_start_failure_is_reported_not_raised():
    """API 不可用时返回 False 而不是抛异常 —— 常亮失败不该让整个跑批挂掉。"""
    with mock.patch.object(
        awake.ctypes.windll.kernel32, "SetThreadExecutionState", side_effect=OSError("no such api")
    ):
        assert awake.keep_awake_start() is False


def test_context_manager_no_stop_when_start_failed():
    """启动就没成功，退出时不该再去调一次还原。"""
    with mock.patch.object(
        awake.ctypes.windll.kernel32, "SetThreadExecutionState", side_effect=OSError("no such api")
    ) as call:
        with awake.keep_awake() as started:
            assert started is False
    assert call.call_count == 1
