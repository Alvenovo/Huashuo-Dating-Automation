"""屏幕常亮：跑批期间阻止息屏/睡眠。组长 2026-09-18 确认测试机无人碰屏，需脚本自己保证。

## 两层常亮，管的不是同一段时间（缺一不可）

| 层 | 实现 | 生效范围 | 管的空档 |
| --- | --- | --- | --- |
| 进程级（本模块） | `SetThreadExecutionState` | 只在这个 python 进程活着时 | pytest 跑批期间 |
| 机器级（`tools/set_keep_awake.ps1`） | 改当前电源方案超时值 | 持久，重启不丢 | 跑批前 / 两轮任务之间 / 跑批后 |

**为什么两层都要**：本模块进程一退就自动失效，所以它管不了"节点 agent 空转等任务"
那几小时 —— 机器按默认电源方案（显示器 10 分钟、睡眠 30 分钟）会自己睡过去，
连 agent 的轮询线程一起停摆，控制机看不到回执，**现场表现与"共享盘断了"一模一样**。
无人值守的节点机必须靠机器级那层兜住；机器级那层要动系统设置，所以只在**节点机**上跑。

## 为什么这一层用 API 而不是改电源计划

改电源计划（`powercfg /change`）是全局持久设置，需要管理员，且会改掉机器原有配置——
跑完还得还原，一旦脚本被强杀就留下烂摊子。

`SetThreadExecutionState` 是**进程级**的：进程活着就生效，进程退出自动失效，
不需要还原、不需要管理员。这是官方给多媒体播放器用的同一套机制。

**跑批期间这一层更可靠**：它跟进程同生共死，不会因为"上一个人把电源方案改回去了"
而失效。机器级那层是给**没人跑 pytest 的时候**兜底的，两层互不替代。

## 用法

由 conftest 的 session 级 fixture 调 `keep_awake_for_process()`（推荐），
**常亮持续到进程退出**，并在跑批期间巡检交互态；一旦发现 RDP 断连/锁屏就标记，
由 pytest 侧中止（否则后半程全是黑屏假失败）。

或手工控制：

    keep_awake_start()
    try:
        ...
    finally:
        keep_awake_stop()

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

# SetThreadExecutionState 的位标志
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001  # 阻止系统睡眠
ES_DISPLAY_REQUIRED = 0x00000002  # 阻止显示器关闭（息屏）

# 巡检线程的续命/巡检间隔（秒）。秒级检测足够及时发现 RDP 断连，
# 又不会让 CPU 忙起来。
_REFRESH_SEC = 5

_watch_thread: threading.Thread | None = None
_watch_stop = threading.Event()
# 是否已装配过常亮+巡检（显式标志，比看 thread.is_alive() 可靠：
# 线程可能刚启动、也可能因异常已退出，不能让这两种情况各起一个。
_watch_armed = False
_session_lock = threading.Lock()
_session_lost_at: float | None = None


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
        from hall_auto.dpi import is_interactive_session

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
