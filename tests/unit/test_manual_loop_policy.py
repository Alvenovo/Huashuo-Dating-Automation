"""锁「人在环」的交互便利设施。

两件事：
1. `hall_auto.console.focus_console()` —— 点完「发送验证码」把终端抢到前台，
   人不用手动 Alt+Tab。**它只是便利，绝不能成为用例的前置条件或断言的一部分。**
2. 人在环用例里**只许通过 `_ask_code()` 读 stdin** —— 那一处负责"先切窗口再问人"。

为什么要专门锁第 2 条（2026-09-22 用户明确要求「点完发送验证码自动跳到终端」）：
散着写 4 处 `input()` 的话，下次加第 5 个码就会漏掉切窗口，
而漏了**不报错**，只是人得自己 Alt+Tab —— 这类"静默降级"最不容易被发现。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from hall_auto import console

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
MANUAL_TESTS = REPO_ROOT / "tests" / "launch" / "test_p1_login.py"


# ---------- focus_console：拿不到窗口是常态，绝不能抛 ----------


@pytest.mark.unit
def test_focus_console_returns_a_bool_and_never_raises():
    assert isinstance(console.focus_console(), bool)


@pytest.mark.unit
def test_focus_console_swallows_finder_errors(monkeypatch):
    """**关键保护**：三条找窗口的路全炸时也要安静返回 False。

    终端类型没覆盖到是**正常情况**（用户可能跑在 VS Code 集成终端里），
    这时候抛异常会把「省一次 Alt+Tab」的便利变成用例失败 —— 完全不成比例。
    """

    def _boom():
        raise OSError("拿不到窗口")

    monkeypatch.setattr(console, "_console_hwnd", _boom)
    monkeypatch.setattr(console, "_console_title_hwnd", _boom)
    monkeypatch.setattr(console, "_ancestor_window", _boom)
    assert console.focus_console() is False


@pytest.mark.unit
def test_ancestor_chain_is_bounded():
    """爬父进程链必须封顶 —— 环或异常表都会让它转不出来。"""
    assert len(console._ancestor_pids()) <= 8


@pytest.mark.unit
def test_desktop_shell_windows_are_never_focused():
    """**关键保护**：桌面/任务栏被提到前台 = **所有窗口最小化**，比不聚焦还糟。

    `explorer.exe` 就在祖先进程链上，不排掉就会聚焦到「Program Manager」。
    """
    assert {"Progman", "WorkerW", "Shell_TrayWnd"} <= console._SHELL_WINDOW_CLASSES
    assert "explorer.exe" in console._SHELL_EXES


# ---------- 人在环用例：只许通过 _ask_code 读 stdin ----------


def _manual_tree() -> ast.Module:
    return ast.parse(MANUAL_TESTS.read_text(encoding="utf-8"))


def _calls(node: ast.AST, func_name: str) -> list[ast.Call]:
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and getattr(sub.func, "id", None) == func_name:
            out.append(sub)
    return out


@pytest.mark.unit
def test_ask_code_is_the_only_place_that_reads_stdin():
    """**关键保护**：除了 `_ask_code` 自己，不许别处直接 `input()`。

    绕过去 = 少了"先把终端提到前台"，人得自己切窗口 —— 而且不报错，纯静默降级。
    """
    tree = _manual_tree()
    helper = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_ask_code"),
        None,
    )
    assert helper is not None, "`_ask_code` 不见了 —— 切窗口那步也跟着没了"
    allowed = {id(call) for call in _calls(helper, "input")}
    assert allowed, "`_ask_code` 里没有 input()，那它就不是那个唯一入口了"

    offenders = [
        call.lineno
        for call in _calls(tree, "input")
        if id(call) not in allowed
    ]
    assert not offenders, f"这些行绕过了 _ask_code 直接 input()：{offenders}"


@pytest.mark.unit
def test_ask_code_focuses_the_terminal_before_reading():
    """顺序不能反：**先切窗口，再问人**。反了等于没做（提示语出现时窗口还是后台）。"""
    helper = next(
        n
        for n in ast.walk(_manual_tree())
        if isinstance(n, ast.FunctionDef) and n.name == "_ask_code"
    )
    focus_lines = [c.lineno for c in _calls(helper, "focus_console")]
    input_lines = [c.lineno for c in _calls(helper, "input")]
    assert focus_lines and input_lines
    assert min(focus_lines) < min(input_lines), "_ask_code 里 focus_console 没有排在 input 前面"
