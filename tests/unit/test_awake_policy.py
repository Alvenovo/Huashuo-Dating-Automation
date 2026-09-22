"""锁屏幕常亮策略（hall_auto/awake.py）。

组长 2026-09-18 确认测试机无人碰屏，脚本必须自己保证不息屏 —— 息屏后抓屏全黑，
点击无落点，产出的是假失败。这里钉住 API 用法与还原行为、巡检中止策略。

## 还原为什么在 atexit

pytest 的 session fixture teardown 之后到进程真正退出之间还有一段
（报告生成、证据落盘、agent 回传）。原来在 teardown 就还原，长套件中间那一空档
足够息屏。改成：常亮持续到进程退出，由 `atexit` 统一还原。
"""

from __future__ import annotations

from unittest import mock

import pytest

from hall_auto import awake

pytestmark = pytest.mark.unit


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


def test_atexit_handler_restores():
    """atexit 处理器必须真调还原（这是唯一的还原点）。"""
    with mock.patch.object(awake.ctypes.windll.kernel32, "SetThreadExecutionState", return_value=1) as call:
        awake._restore_on_exit()
    assert call.call_count == 1
    assert call.call_args.args[0] == awake.ES_CONTINUOUS


def test_start_failure_is_reported_not_raised():
    """API 不可用时返回 False 而不是抛异常 —— 常亮失败不该让整个跑批挂掉。"""
    with mock.patch.object(
        awake.ctypes.windll.kernel32, "SetThreadExecutionState", side_effect=OSError("no such api")
    ):
        assert awake.keep_awake_start() is False


def test_session_lost_starts_clean_and_records():
    """交互态丢失标记：初始为未丢失，标记一次后变已丢失，且带得出原因文本。"""
    awake._session_lost_at = None
    assert awake.session_lost() is False
    assert awake.session_lost_detail() == ""

    awake.mark_session_lost()
    assert awake.session_lost() is True
    assert "非交互式会话" in awake.session_lost_detail()

    awake._session_lost_at = None  # 清理，别污染其他用例


def test_mark_session_lost_keeps_first_timestamp():
    """重复标记只保留第一次的时间 —— 报告里要的是"什么时候开始不可信"，不是最后一次探测。"""
    awake._session_lost_at = None
    awake.mark_session_lost()
    first = awake._session_lost_at
    awake.mark_session_lost()
    assert awake._session_lost_at == first, "后一次标记不该覆盖首次时间"
    awake._session_lost_at = None


def test_keep_awake_for_process_is_idempotent():
    """重复调用只装配一次 —— fixture 可能被多次触发，不能每次起一个巡检线程。

    这里让线程体立刻退出（`_watch_stop` 已置位），避免留下后台线程。
    关键是**不能把 `_watch_stop` 整个 mock 掉** —— 那样每次都得到新 mock，
    测的就不是幂等性了。
    """
    awake._watch_thread = None
    awake._watch_stop = awake.threading.Event()
    awake._watch_stop.set()  # 线程体 wait() 立刻返回 True，线程随即结束
    awake._watch_armed = False
    try:
        with mock.patch.object(awake.ctypes.windll.kernel32, "SetThreadExecutionState", return_value=1), \
             mock.patch.object(awake.atexit, "register"):
            assert awake.keep_awake_for_process() is True
            first_thread = awake._watch_thread
            with mock.patch.object(awake.threading, "Thread") as thread_ctor:
                assert awake.keep_awake_for_process() is True
                thread_ctor.assert_not_called(), "第二次调用不该再建线程"
            assert awake._watch_thread is first_thread
    finally:
        awake._watch_thread = None
        awake._watch_stop = awake.threading.Event()
        awake._watch_armed = False
