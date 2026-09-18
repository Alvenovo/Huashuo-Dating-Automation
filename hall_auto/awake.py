"""屏幕常亮：跑批期间阻止息屏/睡眠。组长 2026-09-18 确认测试机无人碰屏，需脚本自己保证。

## 为什么用 API 而不是改电源计划

改电源计划（`powercfg /change`）是全局持久设置，需要管理员，且会改掉机器原有配置——
跑完还得还原，一旦脚本被强杀就留下烂摊子。

`SetThreadExecutionState` 是**进程级**的：进程活着就生效，进程退出自动失效，
不需要还原、不需要管理员。这是官方给多媒体播放器用的同一套机制。

## 用法

    with keep_awake():
        run_suites()

或（推荐，见下）由 conftest 的 session 级 fixture 调 `keep_awake_for_process()`，
**常亮持续到进程退出**，并在跑批期间巡检交互态；一旦发现 RDP 断连/锁屏就标记，
由 pytest 侧中止（否则后半程全是黑屏假失败）。

或手工控制：

    token = keep_awake_start()
    try:
        ...
    finally:
        keep_awake_stop(token)

`ES_CONTINUOUS` 让状态持续到下一次调用，不是"续一次 60 秒"那个老写法。

## 两个容易踩的坑

1. **别在 fixture teardown 时立刻恢复常亮**。pytest 的 session fixture 结束后到进程真正
   退出之间还有一段（报告生成、证据落盘、agent 回传），长套件中间那一空档足够息屏。
   所以恢复放在 `atexit`。
2. **`SetThreadExecutionState` 只管"不许息屏"，管不了 RDP 断连**。RDP 断连后会话变非交互态，
   抓屏黑屏、点击无落点 —— 这只能靠巡检发现并中止，不能靠常亮兜住。
"""

from __future__ import annotations

import atexit
import ctypes
import threading
import time
from contextlib import contextmanager

# SetThreadExecutionState 的位标志
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001  # 阻止系统睡眠
ES_DISPLAY_REQUIRED = 0x00000002  # 阻止显示器关闭（息屏）

# 巡检线程的续命/巡检间隔（秒）。1 秒级的检测足够及时发现 RDP 断连，
# 又不会让 CPU 忙起来。
_REFRESH_SEC = 5

# 交互态探测结果的缓存时长（秒）。巡检线程会调它，没必要每次都真枚举窗口。
_INTERACTIVE_CACHE_SEC = 1.0

_watch_thread: threading.Thread | None = None
_watch_stop = threading.Event()
# 是否已装配过常亮+巡检（显式标志，比看 thread.is_alive() 可靠：
# 线程可能刚启动、也可能因异常已退出，不能让这两种情况各起一个。
_watch_armed = False
_session_lock = threading.Lock()
_session_lost_at: float | None = None
_interactive_cache_at = 0.0
_interactive_cache_value: bool | None = None


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
    """跑批期间保持屏幕常亮，**整个进程生命周期内持续生效**。

    注意这里**不在退出时恢复**：`SetThreadExecutionState` 的作用域是调用它的线程，
    pytest 的 session fixture teardown 之后到进程真正退出之间，如果直接恢复，
    长套件（P0 装机那类几十分钟的）中间空档就会息屏。改用：
    - 开始时常亮一次
    - 期间由巡检线程每 `_REFRESH_SEC` 续一次（顺带兼作交互态巡检）
    - 进程退出时由 atexit 统一恢复

    进程被强杀时（kill -9 / 任务管理器结束）atexit 不跑，但**那也不需要担心** ——
    系统在调用进程终止时本来就自动清掉这些标志，这也是这个 API 相对改电源计划的优势。
    """
    started = keep_awake_start()
    try:
        yield started
    finally:
        # 刻意不从"一次调用一次恢复"的角度处理：真实恢复在 atexit。
        # 但如果是**嵌套/短命**用法（比如单跑一条用例），进程退出时 atexit 仍会兜住。
        pass


def keep_awake_for_process() -> bool:
    """常亮一直到进程退出（atexit 恢复）。配合巡检线程用。

    返回是否设置成功。重复调用幂等 —— 第二次开始复用已有巡检线程。
    """
    global _watch_stop, _watch_thread, _watch_armed

    # 不只看线程是否存活：线程可能刚启动/已结束，用显式标志位判断"是否已装配过"，
    # 避免竞态下起两个巡检线程。
    if _watch_armed:
        return True

    started = keep_awake_start()
    atexit.register(_restore_on_exit)

    def _watch() -> None:
        """定期续常亮，并记录交互态翻转（RDP 断连 / 锁屏）。"""
        while not _watch_stop.wait(_REFRESH_SEC):
            keep_awake_start()  # ES_CONTINUOUS 已是持续态，这里续一次防被别的进程冲刷
            if not is_interactive_session():
                mark_session_lost()

    _watch_thread = threading.Thread(target=_watch, name="hall-awake-watch", daemon=True)
    _watch_thread.start()
    _watch_armed = True
    return started


def _restore_on_exit() -> None:
    _watch_stop.set()
    keep_awake_stop()


def mark_session_lost() -> None:
    """记录「交互态丢失」（RDP 断连 / 锁屏 / 息屏）。跑批侧据此中止。"""
    global _session_lost_at
    with _session_lock:
        if _session_lost_at is None:
            _session_lost_at = time.time()


def session_lost() -> bool:
    """本轮是否发生过交互态丢失。"""
    with _session_lock:
        return _session_lost_at is not None


def session_lost_detail() -> str:
    """交互态丢失的原因描述，给报告用。没丢失返回空串。"""
    with _session_lock:
        if _session_lost_at is None:
            return ""
        return (
            f"跑批中途探测到非交互式会话（RDP 断连 / 锁屏 / 息屏），"
            f"发生于 {time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(_session_lost_at))}。"
            "此后抓屏会拿到黑屏、点击无落点，用例结果不可信。"
            "测试机请保持本地桌面解锁、不要用 RDP 连接（或断开前先停跑批）。"
        )


def is_interactive_session() -> bool:
    """当前是否交互式桌面会话。带缓存，避免高频调用时反复枚举窗口。

    判定沿用 dpi.py 的口径（有前台窗口 + 窗口有标题），这里只加一层短缓存：
    巡检线程每秒调一次，没必要每秒真枚举一次。
    """
    global _interactive_cache_at, _interactive_cache_value

    now = time.time()
    if _interactive_cache_value is not None and now - _interactive_cache_at < _INTERACTIVE_CACHE_SEC:
        return _interactive_cache_value

    from hall_auto.dpi import is_interactive_session as _probe

    value = _probe()
    _interactive_cache_value = value
    _interactive_cache_at = now
    return value
