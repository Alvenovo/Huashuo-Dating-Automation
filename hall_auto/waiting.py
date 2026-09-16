"""统一的条件轮询等待：条件满足立即返回，替代点完操作后的固定 sleep。

轮询间隔要远小于超时上限，页面通常几百毫秒就绪，谓词本身（抓控件树）也有开销，
间隔取 0.3s 足够密。谓词抛异常按「还没就绪」处理——页面切换中途 UIA 读到一半的树
是常态，不该让整个等待炸掉。
"""

from __future__ import annotations

import time

from hall_auto.launch import LaunchError


def wait_until(predicate, timeout_sec: float = 10.0, interval: float = 0.3) -> bool:
    """轮询到谓词为真返回 True；超时返回 False，由调用方决定怎么收场。"""
    deadline = time.time() + timeout_sec
    while True:
        try:
            if predicate():
                return True
        except Exception:
            pass
        remaining = deadline - time.time()
        if remaining <= 0:
            return False
        time.sleep(min(interval, remaining))


def wait_until_or_raise(
    predicate,
    message: str,
    timeout_sec: float = 10.0,
    interval: float = 0.3,
    error: type[Exception] = LaunchError,
) -> None:
    """轮询到谓词为真即返回；超时抛错，报错里写清等的是什么条件。"""
    if not wait_until(predicate, timeout_sec=timeout_sec, interval=interval):
        raise error(f"{message}；等了 {timeout_sec:g}s 仍未满足")
