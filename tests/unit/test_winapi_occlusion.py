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
    """只实现 occlusion_hint / ensure_window_shown 用到的那几个调用。"""

    def __init__(self, *, at: dict | None = None, iconic: bool = False):
        self.at = dict(at or {})      # (x, y) -> hwnd；缺的点返回 0（= 那儿没窗口）
        self.iconic = iconic
        self.points: list[tuple[int, int]] = []
        self.shown: list[tuple[int, int]] = []      # ShowWindow 的调用记录

    def WindowFromPoint(self, point):            # noqa: N802 - Win32 命名
        self.points.append((point.x, point.y))
        return self.at.get((point.x, point.y), 0)

    def GetAncestor(self, hwnd, _flag):          # noqa: N802
        return hwnd

    def IsIconic(self, _hwnd):                   # noqa: N802
        return self.iconic

    def ShowWindow(self, hwnd, cmd):             # noqa: N802
        self.shown.append((hwnd, cmd))
        return True


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


def test_no_top_level_window_at_all_is_reported(monkeypatch):
    """**连顶层窗都没有** → 报「进程可能已退出」，不能返回空。

    返回空会让报错看起来像产品缺陷（2026-09-23 真机就是这么被藏住真因的）。
    """
    monkeypatch.setattr(winapi, "_top_hwnds", lambda: [])
    hint = winapi.occlusion_hint(HALL_PID)
    assert "退出" in hint, f"该说清是「找不到窗口」：{hint!r}"


def test_hidden_hall_window_is_reported(desk, monkeypatch):
    """**主窗被隐藏**（缩到托盘 / 被 hide）也要报 —— 这是真机实际发生的那种。

    当时只判了「被别的窗口挡住」，于是提示返回空、`test_sync_list_logged_in`
    报「登录后同步页仍没刷出列表」看着像产品缺陷，其实**窗口根本不在屏幕上**
    （失败截图里只有桌面 + 终端）。**UIA 照样找得到隐藏的窗口**，
    所以启动阶段不会报错，只会在依赖页面的断言处空等。
    """
    monkeypatch.setattr(winapi, "_hwnd_visible", lambda h: False)
    _use(monkeypatch, _FakeUser32())

    hint = winapi.occlusion_hint(HALL_PID)
    assert "不可见" in hint, f"没说清是「不可见」：{hint!r}"
    assert "WebView2" in hint, "要说明机制，否则一线会去查产品"


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


# ---------------- ensure_window_shown：把"窗口不在屏幕上"直接修掉 ----------------
#
# 这是 `test_sync_list_logged_in` 那条真机失败的**正面修法**：
# UIA 找得到隐藏 / 最小化的窗口，所以 `wait_main_window` 会"成功"、测试继续跑，
# 而 WebView2 不渲染 → 依赖页面的断言空等 → 报成「列表没刷出来」。
# 所以启动阶段就要**确保窗口真的可见**，而不是等断言失败后去猜。


def test_ensure_window_shown_does_nothing_when_fine(desk, monkeypatch):
    """正常窗口**一个动作都不做** —— 不抢焦点、不改正常路径行为。"""
    fake = _FakeUser32()
    _use(monkeypatch, fake)
    assert winapi.ensure_window_shown(100) == ""
    assert fake.shown == [], "正常窗口不该被动过"


def test_ensure_window_shown_restores_a_minimized_window(desk, monkeypatch):
    fake = _FakeUser32(iconic=True)
    _use(monkeypatch, fake)
    note = winapi.ensure_window_shown(100)
    assert "最小化" in note, f"要说清修了什么（不静默）：{note!r}"
    assert fake.shown == [(100, winapi.SW_RESTORE)], fake.shown


def test_ensure_window_shown_shows_a_hidden_window(desk, monkeypatch):
    """隐藏（缩到托盘）用 SW_SHOW，不是 SW_RESTORE —— 两者语义不同，别混用。"""
    monkeypatch.setattr(winapi, "_hwnd_visible", lambda h: False)
    fake = _FakeUser32()
    _use(monkeypatch, fake)
    note = winapi.ensure_window_shown(100)
    assert "不可见" in note, f"要说清修了什么（不静默）：{note!r}"
    assert fake.shown == [(100, winapi.SW_SHOW)], fake.shown


def test_ensure_window_shown_tolerates_no_hwnd(desk, monkeypatch):
    """拿不到 hwnd 时不能抛 —— 它跑在启动路径上，抛了整批一条都不跑。"""
    fake = _FakeUser32()
    _use(monkeypatch, fake)
    assert winapi.ensure_window_shown(0) == ""
    assert fake.shown == []


# ---------------- window_state_facts：诊断说"没问题"时也要给依据 ----------------
#
# 2026-09-23 真机：`occlusion_hint` 返回空（结论 = "窗口可见且在最上"），
# 而失败截图里**大厅不在屏幕上** —— 结论和截图矛盾，两边都不足以定性。
# **只报结论、不报依据**就会卡在这儿。所以再加一条「原始事实」。


def test_window_state_facts_reports_the_numbers(desk, monkeypatch):
    """**核心**：把 hwnd / rect / visible / iconic / 采样命中都打出来。"""
    _use(monkeypatch, _FakeUser32(at={
        (50, 50): 100, (25, 25): 100, (75, 75): 100, (75, 25): 100, (25, 75): 100,
    }))
    facts = winapi.window_state_facts(HALL_PID)
    assert "rect=(0, 0, 100, 100)" in facts, facts
    assert "visible=True" in facts and "iconic=False" in facts, facts
    assert "采样命中大厅自己 5/5" in facts, f"要把「采样命中」这个关键数字报出来：{facts!r}"


def test_window_state_facts_marks_samples_that_hit_nothing(desk, monkeypatch):
    """采样点**什么都没碰到**（窗口被挪到屏幕外 / rect 不可信）必须和「大厅在最上」区分开。

    原来两种都表现为"遮挡列表为空"，没法区分 —— 这正是 2026-09-23 卡住的地方。
    """
    _use(monkeypatch, _FakeUser32())          # WindowFromPoint 全返回 0
    facts = winapi.window_state_facts(HALL_PID)
    assert "采样命中大厅自己 0/5" in facts, (
        f"0/5 时不能只说「遮挡=无」—— 那会被读成「没被挡」，实际是「没采到」：{facts!r}"
    )


def test_window_state_facts_reports_no_window_at_all(monkeypatch):
    monkeypatch.setattr(winapi, "_top_hwnds", lambda: [])
    assert "没有任何顶层窗" in winapi.window_state_facts(HALL_PID)


def test_window_state_facts_names_the_occluders(desk, monkeypatch):
    """真被挡时，依据里也要带上遮挡者名字（结论和依据对得上）。"""
    _use(monkeypatch, _FakeUser32(at={(50, 50): 300}))
    desk[1][300] = 777
    monkeypatch.setattr(winapi, "_hwnd_text", lambda h: "微信")
    assert "微信" in winapi.window_state_facts(HALL_PID)
