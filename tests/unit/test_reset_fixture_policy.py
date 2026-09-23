"""锁 reset_fixture 的「需要提权」报错口径。

2026-09-23 真机：非管理员跑（或机器上有东西要卸）时**裸抛 traceback**
（`OSError: [WinError 740] 请求的操作需要提升`），而它的 docstring 早就写着
「必须在管理员终端里跑」—— **写了约束却没检查，等于没写**。
一线看到裸 traceback 只能猜，而正确动作（换管理员窗口）一句话就能说清。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

pytestmark = pytest.mark.unit


def _load_reset_fixture():
    spec = importlib.util.spec_from_file_location(
        "_hall_reset_fixture_guard", REPO_ROOT / "tools" / "reset_fixture.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _elevation_error() -> OSError:
    exc = OSError(22, "请求的操作需要提升。")
    exc.winerror = 740
    return exc


def test_needs_elevation_is_detected(monkeypatch):
    """守卫判定本身：740 要认出来，别的 WinError 不许误判（否则真错误会被吞掉）。"""
    mod = _load_reset_fixture()
    assert mod._is_needs_elevation(_elevation_error()) is True

    other = OSError(5, "拒绝访问。")
    other.winerror = 5
    assert mod._is_needs_elevation(other) is False, "WinError 5 不是 740，别混"
    assert mod._is_needs_elevation(OSError(22, "没有 winerror 属性")) is False


def test_main_turns_elevation_failure_into_a_clear_message(monkeypatch, capsys):
    """**核心**：740 要变成「换管理员窗口」的一句人话 + 退出码 2，**不是裸 traceback**。"""
    mod = _load_reset_fixture()

    def _boom():
        raise _elevation_error()

    monkeypatch.setattr(mod, "kill_leftover_installers", _boom)
    rc = mod.main()

    err = capsys.readouterr().err
    assert rc == 2, f"退出码该是 2（和 run_p1_apps.ps1 的约定一致）：{rc}"
    assert "管理员" in err, f"要告诉人换管理员窗口：{err!r}"
    assert "提权段" in err, "还要给出「让跑批自动做」这条路（零人工那条）"


def test_main_does_not_swallow_other_oserrors(monkeypatch):
    """反向锁：别的 OSError 必须**照旧抛出去** —— 否则真故障会被伪装成"权限问题"。"""
    mod = _load_reset_fixture()

    def _boom():
        raise OSError(2, "文件不存在")

    monkeypatch.setattr(mod, "kill_leftover_installers", _boom)
    with pytest.raises(OSError):
        mod.main()
