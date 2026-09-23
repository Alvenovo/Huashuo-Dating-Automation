"""锁大厅「被遮挡」判定（`hall_auto/winapi.py::occlusion_hint`）。

## 为什么要这个判定（2026-09-23 真机，代价是两条用例）

`test_sync_list_logged_in` 与 `test_microsoft_login_sso` **同时超时**（45s / 40s），
两张失败截图**一模一样**：微信在前台、**大厅一个像素都没露**。

机制：大厅主内容区是 **WebView2**。窗口被完全遮挡时 Chromium 会**暂停渲染**，
于是依赖页面的断言（「同步页列表刷出来了」「提交后进已登录态」）**永远等不到**；
而 Win32 那一层（按钮、标题）照样读得到 —— 现象是「**能读、但页面是空的**」，
报告上看着像产品缺陷。

原来只能靠人逐张打开 PNG 比对截图才能定性。这个判定把「谁挡着」直接写进失败信息。

⚠️ **最容易写错的一处**：大厅自己的登录 / 绑定弹窗是**独立顶层窗**，盖在主窗上是
**这些用例的常规状态**。判定必须**按 pid 过滤**，否则会把正常状态误报成遮挡 ——
报错里挂一句假提示比没有提示更糟（会把人带去查错方向）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from hall_auto import winapi  # noqa: E402

pytestmark = pytest.mark.unit

HALL_PID = 42


class _FakeUser32:
    """只实现 occlusion_hint 用到的那三个调用。"""

    def __init__(self, *, at: dict | None = None, iconic: bool = False):
        self.at = dict(at or {})      # (x, y) -> hwnd；缺的点返回 0（= 那儿没窗口）
        self.iconic = iconic
        self.points: list[tuple[int, int]] = []

    def WindowFromPoint(self, point):            # noqa: N802 - Win32 命名
        self.points.append((point.x, point.y))
        return self.at.get((point.x, point.y), 0)

    def GetAncestor(self, hwnd, _flag):          # noqa: N802
        return hwnd

    def IsIconic(self, _hwnd):                   # noqa: N802
        return self.iconic


@pytest.fixture
def desk(monkeypatch):
    """假桌面：主窗 hwnd=100（100×100，在 0,0）+ 小窗 hwnd=200（10×10），同一个大厅进程。"""
    rects = {100: (0, 0, 100, 100), 200: (0, 0, 10, 10)}
    pids = {100: HALL_PID, 200: HALL_PID}
    monkeypatch.setattr(winapi, "_top_hwnds", lambda: list(rects))
    monkeypatch.setattr(winapi, "_hwnd_rect", lambda h: rects[h])
    monkeypatch.setattr(winapi, "_hwnd_pid", lambda h: pids.get(h, 0))
    monkeypatch.setattr(winapi, "_hwnd_visible", lambda h: True)
    monkeypatch.setattr(winapi, "_hwnd_text", lambda h: f"win{h}")
    monkeypatch.setattr(winapi, "_hwnd_class", lambda h: "FakeClass")
    return rects, pids


def _use(monkeypatch, fake: _FakeUser32) -> None:
    monkeypatch.setattr(winapi, "_user32", fake)


# ---------------- 不遮挡时**不许**报（假提示比没提示更糟）----------------

def test_no_hint_when_nothing_covers_the_hall(desk, monkeypatch):
    _use(monkeypatch, _FakeUser32())            # 采样点全是「没窗口」
    assert winapi.occlusion_hint(HALL_PID) == ""


def test_own_dialogs_are_not_occluders(desk, monkeypatch):
    """**关键保护**：大厅自己的弹窗盖在主窗上是**常规状态**，不算遮挡。

    大厅的登录 / 绑定 / 忘记密码弹窗都是独立顶层窗。不按 pid 过滤的话，
    这些用例的正常过程会被挂上「大厅被别的窗口挡住了」——
    报错指错方向，比没有提示更糟。
    """
    _use(monkeypatch, _FakeUser32(at={(50, 50): 300}))
    desk[1][300] = HALL_PID                     # 同一个进程
    assert winapi.occlusion_hint(HALL_PID) == ""


def test_no_hall_window_at_all_returns_empty(monkeypatch):
    monkeypatch.setattr(winapi, "_top_hwnds", lambda: [])
    assert winapi.occlusion_hint(HALL_PID) == ""


def test_main_window_is_the_largest_visible_one(desk, monkeypatch):
    """主窗取**面积最大**的可见顶层窗。

    大厅同时存在若干顶层窗（隐藏的、工具窗）。取错了中心点会落在小窗上，
    于是把真正盖住主窗的窗口漏掉、或者把无关窗口误报成遮挡。
    """
    _use(monkeypatch, _FakeUser32(at={(50, 50): 999, (5, 5): 888}))
    desk[1][999] = HALL_PID                     # 主窗位置上是自己
    desk[1][888] = 777                          # 小窗位置上是别的进程
    assert winapi.occlusion_hint(HALL_PID) == "", (
        "取错了窗口（用了小窗），把别的进程窗口误报成遮挡"
    )


# ---------------- 真被挡时要报，而且要能指认是谁 ----------------

def test_hint_names_the_occluding_window(desk, monkeypatch):
    """**核心**：中心点被别的进程的窗口盖住 → 报出它的标题 + 机制 + 动作。"""
    _use(monkeypatch, _FakeUser32(at={(50, 50): 300}))
    desk[1][300] = 777
    monkeypatch.setattr(winapi, "_hwnd_text", lambda h: "微信")

    hint = winapi.occlusion_hint(HALL_PID)
    assert "微信" in hint, f"没指认是谁挡的：{hint!r}"
    assert "WebView2" in hint, "要说明机制（被遮挡时暂停渲染），否则一线会去查产品"
    assert "最小化" in hint or "关掉" in hint, "要给出可执行动作"


def test_minimized_hall_is_reported(desk, monkeypatch):
    """最小化也要报 —— 同样不渲染，但报错要说的是另一件事（还原窗口，不是关别人）。"""
    _use(monkeypatch, _FakeUser32(iconic=True))
    hint = winapi.occlusion_hint(HALL_PID)
    assert "最小化" in hint


def test_hint_lists_at_most_four_occluders(desk, monkeypatch):
    """挡的窗口很多时别把报错刷屏 —— 列前 4 个够定位了。"""
    at = {}
    for idx, (x, y) in enumerate([(50, 50), (25, 25), (75, 75), (75, 25), (25, 75)]):
        at[(x, y)] = 300 + idx
        desk[1][300 + idx] = 777 + idx
    _use(monkeypatch, _FakeUser32(at=at))
    monkeypatch.setattr(winapi, "_hwnd_text", lambda h: f"窗口{h}")

    hint = winapi.occlusion_hint(HALL_PID)
    for idx in range(4):
        assert f"窗口{300 + idx}" in hint, f"前 4 个都要列出来：{hint!r}"
    assert "窗口304" not in hint, f"第 5 个不该出现（会把报错刷屏）：{hint!r}"
    assert "窗口300" in hint, "第一个（中心点）那个必须报出来"
