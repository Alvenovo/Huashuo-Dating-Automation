"""屏幕常亮：跑批期间阻止息屏/睡眠。组长 2026-09-18 确认测试机无人碰屏，需脚本自己保证。

## 为什么用 API 而不是改电源计划

改电源计划（`powercfg /change`）是全局持久设置，需要管理员，且会改掉机器原有配置——
跑完还得还原，一旦脚本被强杀就留下烂摊子。

`SetThreadExecutionState` 是**进程级**的：进程活着就生效，进程退出自动失效，
不需要还原、不需要管理员。这是官方给多媒体播放器用的同一套机制。

## 用法

    with keep_awake():
        run_suites()

或手工控制：

    token = keep_awake_start()
    try:
        ...
    finally:
        keep_awake_stop(token)

`ES_CONTINUOUS` 让状态持续到下一次调用，不是"续一次 60 秒"那个老写法。
"""

from __future__ import annotations

import ctypes
from contextlib import contextmanager

# SetThreadExecutionState 的位标志
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001  # 阻止系统睡眠
ES_DISPLAY_REQUIRED = 0x00000002  # 阻止显示器关闭（息屏）


def keep_awake_start() -> bool:
    """开始阻止息屏与睡眠。返回是否调用成功。"""
    try:
        result = ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        )
        return bool(result)
    except (AttributeError, OSError):
        return False


def keep_awake_stop() -> bool:
    """恢复默认电源行为。"""
    try:
        result = ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        return bool(result)
    except (AttributeError, OSError):
        return False


@contextmanager
def keep_awake():
    """跑批期间保持屏幕常亮，退出时自动恢复。"""
    started = keep_awake_start()
    try:
        yield started
    finally:
        if started:
            keep_awake_stop()
