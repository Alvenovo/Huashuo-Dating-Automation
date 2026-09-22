"""锁控制机侧策略（tools/farm_control.py）：人在环门禁 + 汇总报告的凭据列。

两件事都直接决定测试人员能不能按手册得出结论：

1. **人在环套件不能进农场**。手册明确承诺 `login-manual`「永不进农场」，但早先
   `dispatch` 只打印一句 `⚠ 人在环，不应进农场`，照样把任务写出去 ——
   节点上它会卡在 `input()` 等短信验证码，agent 一直不返回，那台机器再也取不到新任务，
   而控制机这边只看到"执行中"。

2. **汇总报告的「凭据」列必须真有数据**。手册第四部分让测试人员靠这一列区分
   「这台机器没配凭据」和「凭据配了、是用例本身在跳过」。早先 `cmd_aggregate`
   只读 `results/*/summary.json`，而凭据状态只写在 `done/` 回执里，
   `nodes_data` 从未写入 `credentials` 键 → 那一列**恒为 ✗**，
   一线会拿着全红的凭据列去反复折腾 `farm_node.env`。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import farm_control  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def farm(tmp_path, monkeypatch):
    for sub in ("tasks", "done", "results", "logs"):
        (tmp_path / sub).mkdir()
    monkeypatch.setenv("HALL_FARM_ROOT", str(tmp_path))
    return tmp_path


def _dispatch(nodes: str, suites: str, **kw):
    return farm_control.cmd_dispatch(
        argparse.Namespace(nodes=nodes, suites=suites, task_id=kw.pop("task_id", "T1"), **kw)
    )


# ---------------- 人在环套件门禁 ----------------


def test_dispatch_rejects_manual_suite(farm):
    """**关键门禁**：`login-manual` 默认不许投进农场，且一个任务文件都不该产生。"""
    with pytest.raises(SystemExit) as exc:
        _dispatch("R01", "login-manual")
    assert "人在环" in str(exc.value)
    assert not list((farm / "tasks").iterdir()), "拒投就不该留下任务文件"


def test_dispatch_manual_rejection_mentions_how_to_run_it(farm):
    """拒绝信息要给出正确跑法（在交互式终端 --local），否则一线只会换个参数硬试。"""
    with pytest.raises(SystemExit) as exc:
        _dispatch("R01", "login-manual")
    assert "--local" in str(exc.value)


def test_dispatch_allows_manual_with_explicit_flag(farm):
    """显式 `--allow-manual`（例如你正远程盯着那台机器）才放行。"""
    assert _dispatch("R01", "login-manual", allow_manual=True) == 0
    payload = json.loads((farm / "tasks" / "R01.json").read_text(encoding="utf-8"))
    assert payload["suites"] == [{"name": "login-manual", "shard_id": 1, "shard_count": 1}]


def test_dispatch_rejects_manual_mixed_with_normal_suites(farm):
    """混着投也要整体拒掉，不能"只投能跑的那部分" —— 那样一线以为 login-manual 也跑了。"""
    with pytest.raises(SystemExit):
        _dispatch("R01", "launch,login-manual")
    assert not list((farm / "tasks").iterdir())


def test_dispatch_normal_suites_still_work(farm):
    assert _dispatch("R01,R02", "launch,apps-detect") == 0
    assert (farm / "tasks" / "R01.json").is_file()
    assert (farm / "tasks" / "R02.json").is_file()


def test_dispatch_marks_destructive_suites(farm):
    """真装卸套件要在任务里显式带上 `HALL_ALLOW_INSTALL=1`，便于审计"谁开的闸"。"""
    _dispatch("R01", "install")
    payload = json.loads((farm / "tasks" / "R01.json").read_text(encoding="utf-8"))
    assert payload["env"]["HALL_ALLOW_INSTALL"] == "1"


def test_dispatch_unknown_suite_fails_fast(farm):
    """套件名写错要当场报错，别投出去才发现。"""
    with pytest.raises(KeyError):
        _dispatch("R01", "no-such-suite")


# ---------------- 汇总报告的凭据列 ----------------


def _put_run(farm: Path, node: str, run: str, *, node_env: dict | None = None) -> Path:
    run_dir = farm / "results" / node / run
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps({
            "run": run, "node": node,
            "profile": {"os": "Windows 11", "screen": "2560x1600", "scale_percent": 150},
            "total": 1, "passed": 1, "failed": 0, "skipped": 0, "duration_s": 1.0,
            "cases": [{
                "module": "launch", "case": "test_a", "title": "启动", "outcome": "passed",
                "nodeid": "tests/launch/test_p0_launch.py::test_a", "duration_s": 0.5,
                "timestamp": "2026-09-21T10:00:00", "assertion": "", "screenshot": None,
            }],
        }, ensure_ascii=False), encoding="utf-8",
    )
    if node_env is not None:
        (run_dir / farm_control.NODE_ENV_EVIDENCE_NAME).write_text(
            json.dumps(node_env, ensure_ascii=False), encoding="utf-8"
        )
    return run_dir


def _render(farm: Path) -> str:
    farm_control.cmd_aggregate(argparse.Namespace())
    return (farm / "aggregate_report.html").read_text(encoding="utf-8")


def test_aggregate_reads_credentials_from_evidence(farm):
    """**核心回归锁**：证据目录里的 `node_env.json` 必须被读出来，凭据列渲染成 ✓。"""
    _put_run(farm, "R01", "R01_2026-09-21_1000", node_env={
        "node": "R01", "credentials": {"password_login": True, "microsoft_sso": True, "share_creds": True},
    })
    html = _render(farm)
    assert html.count("#1a7f37\">✓") == 3, "三个凭据格都该是 ✓"
    assert "#c62828\">✗" not in html, "配了凭据就不该出现 ✗"


def test_aggregate_shows_cross_when_credentials_absent(farm):
    """没配凭据的节点该显示 ✗ —— 这一列的意义就是区分根因，不能两边都一个样。"""
    _put_run(farm, "R01", "R01_2026-09-21_1000", node_env={
        "node": "R01", "credentials": {"password_login": False, "microsoft_sso": False, "share_creds": False},
    })
    html = _render(farm)
    assert html.count("#c62828\">✗") == 3
    assert "#1a7f37\">✓" not in html


def test_aggregate_falls_back_to_done_receipts(farm):
    """证据里没有 `node_env.json`（早先版本节点跑的结果）-> 用 `done/` 回执兜底。"""
    _put_run(farm, "R01", "R01_2026-09-21_1000")
    (farm / "done" / "R01_T1.json").write_text(json.dumps({
        "task_id": "T1", "node": "R01",
        "credentials": {"password_login": True, "microsoft_sso": False, "share_creds": True},
    }, ensure_ascii=False), encoding="utf-8")
    html = _render(farm)
    assert html.count("#1a7f37\">✓") == 2, "回执里的凭据要能补上"
    assert html.count("#c62828\">✗") == 1


def test_aggregate_warns_when_credentials_unknown(farm, capsys):
    """两边都拿不到凭据状态时要**明确警告**，别让全 ✗ 被当成"没配凭据"的结论。"""
    _put_run(farm, "R01", "R01_2026-09-21_1000")
    _render(farm)
    out = capsys.readouterr().out
    assert "没拿到凭据状态" in out
    assert "R01" in out


def test_aggregate_requires_results(farm):
    with pytest.raises(SystemExit):
        farm_control.cmd_aggregate(argparse.Namespace())
