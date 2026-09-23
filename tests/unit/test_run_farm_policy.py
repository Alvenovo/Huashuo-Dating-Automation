"""锁 `tools/run_farm.py` 的完成判据与「投了没人取」告警。

2026-09-23 的教训：控制机把任务投出去、节点没取走时，原来的 `farm_control.py status`
只说「待执行 1 个」，**一个字都不提示** —— 用户以为"清单走完就该自动跑"，
实际任务在共享盘上躺着，而节点侧也零报错。本脚本的职责就是把这种状态当场喊出来。

**最容易写成假绿的两条判据**（下面各有守卫钉着）：
1. 「`tasks/` 里没有这个节点的文件 = 完成」→ 任务被删了 / 被覆盖了也判完成。
2. 「`done/` 里有这个节点的文件 = 完成」→ **上一轮**的回执会让新任务立刻判完成。
所以完成判据必须是 `done/<node>_<task_id>.json` —— **带 task_id**。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import run_farm  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def root(tmp_path):
    for sub in ("tasks", "done", "results", "logs"):
        (tmp_path / sub).mkdir()
    return tmp_path


# --------------------------------------------------------------- 完成判据

def test_receipt_must_carry_task_id(root):
    """**核心判据**：回执文件名带 task_id。

    只认 `<node>.json` 的话，同一台机器跑第二轮时，第一轮的回执会让新任务
    **在投出去的一瞬间就判完成** —— 报告里是绿的，实际一条用例都没跑。
    """
    (root / "done" / "R01.json").write_text("{}", encoding="utf-8")
    assert run_farm.node_state(root, "R01", "T_NEW") != "done"


def test_old_round_receipt_does_not_count(root):
    """上一轮的回执不算这一轮。"""
    (root / "done" / "R01_T_OLD.json").write_text("{}", encoding="utf-8")
    (root / "tasks" / "R01.json").write_text("{}", encoding="utf-8")
    assert run_farm.node_state(root, "R01", "T_NEW") == "pending"


def test_receipt_wins_over_leftover_running(root):
    """回执在 = 这活交过了，**即使 `.running` 收尾时没删掉**。

    收尾崩掉会留下 `.running`。若判据是"有 `.running` 就算在跑"，
    这台机器会被永远算成"执行中"，超时报告也会给错方向。
    """
    (root / "done" / "R01_T1.json").write_text("{}", encoding="utf-8")
    (root / "tasks" / "R01.json.running").write_text("{}", encoding="utf-8")
    assert run_farm.node_state(root, "R01", "T1") == "done"


def test_running_beats_pending(root):
    """取走时 `.json` 已改名成 `.json.running`，两态不会同时出现 —— 但要防实现写反。"""
    (root / "tasks" / "R01.json.running").write_text("{}", encoding="utf-8")
    assert run_farm.node_state(root, "R01", "T1") == "running"


def test_pending_while_task_file_sits_there(root):
    (root / "tasks" / "R01.json").write_text("{}", encoding="utf-8")
    assert run_farm.node_state(root, "R01", "T1") == "pending"


def test_missing_when_task_file_vanished(root):
    """三样都没有 = 任务文件不见了。

    **不能当成"没事"**：它不会自己变好。常见成因是有人手工删了、
    控制机又投了一轮把同名文件覆盖了、或 agent 拒收（任务里 suites 为空时不写回执）。
    """
    assert run_farm.node_state(root, "R01", "T1") == "missing"


def test_state_does_not_leak_across_nodes(root):
    """另一台机器的回执不能算这台完成。"""
    (root / "done" / "R02_T1.json").write_text("{}", encoding="utf-8")
    assert run_farm.node_state(root, "R01", "T1") == "missing"


# ------------------------------------------------------------------ 告警

def test_stuck_hint_fires_and_names_the_real_causes(root):
    """任务一直没被取走时要给**能照着做**的排查，而不是一句"等超时"。"""
    states = {"R01": "pending"}
    hint = run_farm._stuck_hint(root, ["R01"], states)
    assert hint, "有节点卡在 pending，却没给出任何提示"
    assert "--once" in hint, "没点出最常见的成因：敲成 --once，取一次就退出了"
    assert "--loop" in hint
    assert "R01" in hint


def test_stuck_hint_is_silent_when_nobody_is_stuck(root):
    """没人卡在 pending 就不该报警 —— 否则真出问题时人也当噪音忽略。"""
    assert run_farm._stuck_hint(root, ["R01"], {"R01": "running"}) == ""
    assert run_farm._stuck_hint(root, ["R01"], {"R01": "done"}) == ""


def test_stuck_hint_only_names_the_stuck_nodes(root):
    hint = run_farm._stuck_hint(root, ["R01", "R02"], {"R01": "done", "R02": "pending"})
    assert "R02" in hint
    assert "R01" not in hint.split("：", 1)[-1].split("\n", 1)[0]


# ------------------------------------------------------------------ 进度

def test_progress_prints_detail_only_on_change(root, capsys):
    """明细只在**状态变化**时打。

    默认 30 秒一轮，跑一小时就是 120 行「XXX 还没被取走」—— 刷屏之后人就不看了。
    """
    run_farm._print_progress(["R01"], {"R01": "pending"}, None)
    first = capsys.readouterr().out
    assert "还没被取走" in first

    run_farm._print_progress(["R01"], {"R01": "pending"}, {"R01": "pending"})
    assert "还没被取走" not in capsys.readouterr().out

    run_farm._print_progress(["R01"], {"R01": "running"}, {"R01": "pending"})
    assert "已取走" in capsys.readouterr().out


def test_tally_counts_every_state():
    pending, running, done = run_farm._tally(
        {"A": "pending", "B": "running", "C": "done", "D": "done", "E": "missing"}
    )
    assert (pending, running, done) == (1, 1, 2)


# ------------------------------------------------------------ main 的退出码

class _FakeFC:
    """假的 farm_control：只写一个任务文件，不碰真农场。"""

    def __init__(self, root: Path):
        self._root = root
        self.aggregate_called = False

    def cmd_dispatch(self, args) -> int:
        (self._root / "tasks").mkdir(parents=True, exist_ok=True)
        (self._root / "done").mkdir(parents=True, exist_ok=True)
        for node in [n.strip() for n in args.nodes.split(",") if n.strip()]:
            (self._root / "tasks" / f"{node}.json").write_text("{}", encoding="utf-8")
            print(f"{node}: 1 个套件 -> {node}.json")
        return 0

    def farm_root(self) -> Path:
        return self._root

    def cmd_aggregate(self, args) -> int:
        self.aggregate_called = True
        return 0


@pytest.fixture
def fake_fc(root, monkeypatch):
    fc = _FakeFC(root)
    monkeypatch.setattr(run_farm, "_load_farm_control", lambda: fc)
    return fc


def test_timeout_returns_1(fake_fc):
    """节点一直不取 → 超时退出，**退出码 1**（不是 0）。"""
    rc = run_farm.main(["--nodes", "R01", "--suites", "launch",
                        "--interval", "1", "--timeout", "2", "--no-aggregate"])
    assert rc == 1


def test_all_done_returns_0_and_aggregates(fake_fc, root):
    """全部交回执 → 0，并自动汇总。"""
    (root / "tasks" / "R01.json").unlink(missing_ok=True)
    (root / "done" / "R01_T1.json").write_text("{}", encoding="utf-8")
    rc = run_farm.main(["--nodes", "R01", "--suites", "launch", "--task-id", "T1",
                        "--interval", "1", "--timeout", "5"])
    assert rc == 0
    assert fake_fc.aggregate_called, "全部跑完却没自动出报告"


def test_no_wait_returns_immediately(fake_fc, root):
    """`--no-wait` 只投不等 —— 等价于原来的 dispatch。"""
    rc = run_farm.main(["--nodes", "R01", "--suites", "launch", "--no-wait"])
    assert rc == 0
    assert (root / "tasks" / "R01.json").is_file()
    assert not fake_fc.aggregate_called


def test_stuck_warning_is_printed_while_waiting(fake_fc, monkeypatch, capsys):
    """**这条是本脚本存在的理由**：卡住时要当场喊出来。

    把告警阈值压到 0，确认它真的会打 —— 阈值 90 秒在测试里等不起，
    但"等不起所以不测"正是这类告警最容易悄悄失效的方式。
    """
    monkeypatch.setattr(run_farm, "STUCK_WARN_SECONDS", 0)
    run_farm.main(["--nodes", "R01", "--suites", "launch",
                   "--interval", "1", "--timeout", "2", "--no-aggregate"])
    out = capsys.readouterr().out
    assert "--once" in out, "节点没取走任务，却没提示最常见的成因"
    assert "还没被取走" in out


def test_empty_nodes_rejected(fake_fc):
    with pytest.raises(SystemExit):
        run_farm.main(["--nodes", " , ", "--suites", "launch"])


def test_bad_interval_rejected(fake_fc):
    with pytest.raises(SystemExit):
        run_farm.main(["--nodes", "R01", "--suites", "launch", "--interval", "0"])


# ------------------------------------------------------------ 投前自检
#
# 2026-09-23 加：任务投出去没人取时，原来的表现是**干等一小时才超时**，
# 期间只有一行「待执行 1 个」。而下面这几类问题在投之前就能看出来 ——
# 发现时间从一小时变成一秒。


def test_preflight_ok_on_a_healthy_farm(root):
    errors, warnings = run_farm.preflight(root, ["R01"])
    assert errors == [] and warnings == []


def test_preflight_creates_the_farm_dirs(tmp_path):
    """节点第一次跑不必手工建目录（幂等）。"""
    errors, _warnings = run_farm.preflight(tmp_path / "fresh", ["R01"])
    assert errors == []
    assert (tmp_path / "fresh" / "tasks").is_dir()


def test_preflight_reports_an_unusable_farm_root(tmp_path):
    """**关键**：农场目录建不出来时要**拦住投递**，不是投出去再等超时。

    包源机关了 / 代理没关 / 共享名写错，都表现成这一条。
    `Path.is_dir()` 在 UNC 上会把 OSError 吞成 False（本项目实测过），
    所以判可达性是「真去 mkdir 一次」。
    """
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")   # 拿普通文件当父目录，mkdir 必抛 OSError
    errors, _warnings = run_farm.preflight(blocker / "farm", ["R01"])
    assert errors, "农场不可用却没报错"
    assert "农场目录不可用" in errors[0]


def test_preflight_warns_about_a_leftover_running_placeholder(root):
    """残留 `.running` 会挡住新任务 —— 投之前先说一声。"""
    (root / "tasks" / "R01.json.running").write_text("{}", encoding="utf-8")
    errors, warnings = run_farm.preflight(root, ["R01"])
    assert errors == []
    assert any(".running" in w for w in warnings)


def test_preflight_warns_when_a_previous_task_was_never_taken(root):
    """上一轮的任务还压着 → 本次投递会把它覆盖掉。这事不能悄悄发生。"""
    (root / "tasks" / "R01.json").write_text("{}", encoding="utf-8")
    _errors, warnings = run_farm.preflight(root, ["R01"])
    assert any("没被取走" in w for w in warnings)


def test_preflight_fails_before_dispatch(fake_fc, tmp_path, monkeypatch):
    """自检没过时**不投任务**，退出码 2。"""
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(fake_fc, "farm_root", lambda: blocker / "farm")
    rc = run_farm.main(["--nodes", "R01", "--suites", "launch", "--no-wait"])
    assert rc == 2


# ---------------------------------------------------- 真装真卸的醒目告警

def test_destructive_notice_names_what_gets_installed(root):
    """全量档现在含真装真卸，**按下回车之前必须说清楚**。

    `install` 会卸载并重装大厅本体 —— 这是跑批里唯一会改动被测产品的动作，
    一句话不说就投出去，人会在机器被重装到一半时才发现。
    """
    notice = run_farm.destructive_notice(["unit", "install", "apps-lifecycle"])
    assert "install" in notice and "apps-lifecycle" in notice
    assert "华硕大厅本体" in notice
    assert "只能在测试机上跑" in notice


def test_destructive_notice_is_silent_for_readonly_batches(root):
    """只读批次不该看到这个告警 —— 否则真出问题时人也当噪音忽略。"""
    assert run_farm.destructive_notice(["unit", "launch", "login"]) == ""


def test_destructive_notice_covers_the_whole_full_run():
    """全量档必须触发告警。往全量档里加了提权套件而这里没跟上，就是漏报。"""
    from hall_auto.suites import FULL_RUN_SUITES

    assert run_farm.destructive_notice(list(FULL_RUN_SUITES)), (
        "全量档含真装真卸，却没打出告警"
    )


def test_manual_hint_only_for_a_full_run_batch():
    """人在环的提示只对**覆盖全量档**的批次打。

    判据与节点侧同源（都对着 `FULL_RUN_SUITES`）—— 写死套件名的话，
    以后往全量档里加套件，两边会各说各的。
    """
    from hall_auto.suites import FULL_RUN_SUITES

    assert run_farm.task_covers_full_run_names(list(FULL_RUN_SUITES))
    assert not run_farm.task_covers_full_run_names(["launch"])
    assert not run_farm.task_covers_full_run_names(list(FULL_RUN_SUITES)[:-1])


def test_full_run_reports_the_manual_phase_location(fake_fc, root, capsys):
    """全量档跑完，控制机要告诉人「人在环在哪儿跑、跑完怎么并进报告」。"""
    (root / "tasks" / "R01.json").unlink(missing_ok=True)
    (root / "done" / "R01_T1.json").write_text("{}", encoding="utf-8")
    rc = run_farm.main(["--nodes", "R01", "--suites", "all", "--task-id", "T1",
                        "--interval", "1", "--timeout", "5"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "login-manual" in out, "没告诉人人在环怎么跑"
    assert "aggregate" in out, "没告诉人跑完怎么并进报告"
