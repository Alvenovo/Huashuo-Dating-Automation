"""锁农场任务取用策略（tools/farm_agent.take_task）。

两个坑都是"重复/空跑"导致的：
- 控制机重复 dispatch 同一个 task_id → 节点把 `install` / `apps-lifecycle` 真装卸套件
  又跑一遍，白等十几分钟还搅乱机器状态。
- 任务里 suites 为空 → 旧实现照样写一份"成功"回执，aggregate 把该节点算成已完成（假绿）。
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import farm_agent  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def root(tmp_path):
    for sub in ("tasks", "done", "results", "logs"):
        (tmp_path / sub).mkdir()
    return tmp_path


def _write_task(root: Path, node: str, payload: dict) -> None:
    (root / "tasks" / f"{node}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _task(task_id: str = "T1", suites=None) -> dict:
    return {
        "task_id": task_id,
        "node": "R01",
        "suites": suites if suites is not None else [{"name": "launch"}],
        "created_at": "2026-09-18T14:00:00",
    }


def _full_run_task(task_id: str = "T1") -> dict:
    """全量档任务 —— 默认只有它跑完才该问人在环（见 `farm_agent.task_covers_full_run`）。"""
    return _task(task_id, suites=[{"name": n} for n in farm_agent.FULL_RUN_SUITES])


def test_take_task_reaps_stale_running_placeholder(root):
    """**关键保护**：上一轮崩掉留下的 `.running` 会永久挡住新任务。

    取任务是把 `<node>.json` 改名成 `<node>.json.running`。节点中途挂了（断电/被结束）
    就会留下占位文件，之后控制机投的新任务**永远接管不了**，
    表现为"投了任务但节点一直说无任务"，从表面极难排查。
    """
    # 上一轮的残留占位（内容是一个已经跑完的 task）
    stale = root / "tasks" / "R01.json.running"
    stale.write_text(json.dumps(_task(task_id="T_OLD"), ensure_ascii=False), encoding="utf-8")
    (root / "done" / "R01_T_OLD.json").write_text("{}", encoding="utf-8")

    # 控制机投了新任务
    _write_task(root, "R01", _task(task_id="T_NEW"))

    task, note = farm_agent.take_task(root, "R01")
    assert task is not None, "残留占位不该挡住新任务"
    assert task["task_id"] == "T_NEW"
    assert "清理上一轮残留" in note, "要显式说明清理过，便于发现上次没正常收尾"


def test_take_task_clears_placeholder_on_duplicate_reject(root):
    """因重复而拒收时，也要把占位收掉 —— 否则它挡住下一个新任务。"""
    _write_task(root, "R01", _task(task_id="T_DUP"))
    (root / "done" / "R01_T_DUP.json").write_text("{}", encoding="utf-8")

    task, reject = farm_agent.take_task(root, "R01")
    assert task is None and "拒绝重复执行" in reject
    assert not (root / "tasks" / "R01.json.running").exists(), "拒收后不该留占位"


def test_take_task_clears_placeholder_on_empty_suites(root):
    """空套件拒收同样要收掉占位。"""
    _write_task(root, "R01", _task(task_id="T_E", suites=[]))
    task, reject = farm_agent.take_task(root, "R01")
    assert task is None and "suites 为空" in reject
    assert not (root / "tasks" / "R01.json.running").exists()


def test_take_task_reaps_unreadable_placeholder(root):
    """占位文件内容损坏也要能清掉（否则永久卡死），说明里标注读不出。"""
    (root / "tasks" / "R01.json.running").write_text("{坏掉的内容", encoding="utf-8")
    _write_task(root, "R01", _task(task_id="T_AFTER_CORRUPT"))

    task, note = farm_agent.take_task(root, "R01")
    assert task is not None
    assert task["task_id"] == "T_AFTER_CORRUPT"


def test_take_task_returns_none_when_no_file(root):
    task, reject = farm_agent.take_task(root, "R01")
    assert task is None and reject == ""


def test_take_task_takes_and_marks_running(root):
    _write_task(root, "R01", _task())
    task, reject = farm_agent.take_task(root, "R01")
    assert task is not None and reject == ""
    assert task["task_id"] == "T1"
    assert (root / "tasks" / "R01.json.running").is_file(), "取走要改名成 .running"


def test_take_task_rejects_duplicate_task_id(root):
    """**关键保护**：done/ 里已有同 task_id 的回执 → 拒绝再跑。

    没有这道闸，控制机重复 dispatch 会让 install / apps-lifecycle 这种真装卸套件
    在测试机上再跑一遍。
    """
    _write_task(root, "R01", _task(task_id="T_DUP"))
    (root / "done" / "R01_T_DUP.json").write_text("{}", encoding="utf-8")

    task, reject = farm_agent.take_task(root, "R01")
    assert task is None
    assert "拒绝重复执行" in reject
    assert "T_DUP" in reject


def test_take_task_rejects_empty_suites_without_receipt(root):
    """**假绿防护**：suites 为空 → 拒收，且**不写回执**。

    写一份"成功但什么都没跑"的回执，会让控制机的 aggregate 把该节点算成已完成，
    把控制机的 bug 掩盖掉。
    """
    _write_task(root, "R01", _task(task_id="T_EMPTY", suites=[]))
    task, reject = farm_agent.take_task(root, "R01")
    assert task is None
    assert "suites 为空" in reject
    assert not list((root / "done").iterdir()), "拒收时不该产生任何回执"


def test_take_task_rejects_malformed_json(root):
    (root / "tasks" / "R01.json").write_text("{不是合法 JSON", encoding="utf-8")
    task, reject = farm_agent.take_task(root, "R01")
    assert task is None
    assert "JSON" in reject


def test_take_task_allows_retake_when_no_receipt(root):
    """没有回执时允许重取 —— 上一轮可能中途崩了，重试是正常路径。"""
    _write_task(root, "R01", _task(task_id="T_RETRY"))
    task, _ = farm_agent.take_task(root, "R01")
    assert task is not None
    # 模拟节点崩了：.running 留着但没回执
    task2, reject2 = farm_agent.take_task(root, "R01")
    assert task2 is None and reject2 == "", "没有 .json 了，取不到但不算拒收"


def test_take_task_missing_task_id_still_runs(root):
    """旧格式任务（没有 task_id）不该被去重逻辑误杀 —— 没有 id 就无从比对回执。"""
    payload = {"node": "R01", "suites": [{"name": "launch"}]}
    _write_task(root, "R01", payload)
    task, reject = farm_agent.take_task(root, "R01")
    assert task is not None and reject == ""


# ---------------- 农场目录来源：环境变量 > config.local.yaml ----------------
#
# 节点机不设 `HALL_FARM_ROOT` 时 farm_agent 直接退出、**一个任务都取不到**。
# 以前每个新开的窗口都得重设一次，忘了就是"节点在跑但永远说无任务"，极难排查。
# 现在 bootstrap 会把值写进 config.local.yaml，farm_agent 自动读，环境变量只作临时覆盖。


def test_farm_root_prefers_env_over_config(monkeypatch):
    """环境变量优先（临时切到别的农场做验证）。"""
    monkeypatch.setenv("HALL_FARM_ROOT", r"\\env-host\hall-farm")
    fake = mock.Mock(farm_root=r"\\config-host\hall-farm")
    with mock.patch("hall_auto.config.load_config", return_value=fake):
        assert farm_agent.farm_root() == Path(r"\\env-host\hall-farm")


def test_farm_root_falls_back_to_config(monkeypatch):
    """环境变量没设 -> 读 config.local.yaml 的 farm_root（bootstrap 写的常驻值）。"""
    monkeypatch.delenv("HALL_FARM_ROOT", raising=False)
    fake = mock.Mock(farm_root=r"\\config-host\hall-farm")
    with mock.patch("hall_auto.config.load_config", return_value=fake):
        assert farm_agent.farm_root() == Path(r"\\config-host\hall-farm")


def test_farm_root_blank_env_falls_back_to_config(monkeypatch):
    """环境变量设成空串（`$env:X=""` 很常见）也要回落配置，不能当"已设置"。"""
    monkeypatch.setenv("HALL_FARM_ROOT", "   ")
    fake = mock.Mock(farm_root=r"\\config-host\hall-farm")
    with mock.patch("hall_auto.config.load_config", return_value=fake):
        assert farm_agent.farm_root() == Path(r"\\config-host\hall-farm")


def test_farm_root_exits_when_nothing_set(monkeypatch):
    """两边都没有 -> **必须硬退出**。

    给个默认值（比如当前目录）会让节点假装在跑、实际一个任务都取不到，
    从表面完全看不出根因，比直接报错糟得多。
    """
    monkeypatch.delenv("HALL_FARM_ROOT", raising=False)
    fake = mock.Mock(farm_root="")
    with mock.patch("hall_auto.config.load_config", return_value=fake):
        with pytest.raises(SystemExit) as exc:
            farm_agent.farm_root()
    assert "HALL_FARM_ROOT" in str(exc.value)


def test_farm_root_survives_config_read_error(monkeypatch):
    """读配置炸了也不能崩栈 —— 退化成"两边都没有"的正常报错。"""
    monkeypatch.delenv("HALL_FARM_ROOT", raising=False)
    with mock.patch("hall_auto.config.load_config", side_effect=RuntimeError("配置坏了")):
        with pytest.raises(SystemExit):
            farm_agent.farm_root()


# ---------------- 证据回传：不许错认上一轮的目录 ----------------
#
# 回传逻辑是"跑完取证据根目录下最新的那个 run"。若本轮**没产出**证据目录
# （用例收集失败 / pytest 起不来 / 一条都没收集到），"最新"就是**上一轮的旧目录** ——
# 于是报告里那台机器挂着一份别的运行的结果。这比"没有证据"危险得多：
# 结论看着有、实际张冠李戴。
#
# 解法是跑之前先给证据根目录拍快照，只认新出现的。


@pytest.fixture
def evidence_root(tmp_path, monkeypatch):
    root = tmp_path / "evidence"
    root.mkdir()
    monkeypatch.setattr(farm_agent, "EVIDENCE_ROOT", root)
    return root


def test_evidence_snapshot_empty_when_root_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(farm_agent, "EVIDENCE_ROOT", tmp_path / "nope")
    assert farm_agent.evidence_snapshot() == set()


def test_newest_run_dir_returns_none_when_nothing_new(evidence_root):
    """**核心回归锁**：没有新目录时返回 None，绝不回落旧目录。"""
    (evidence_root / "OLD_2026-09-01_0000").mkdir()
    snapshot = farm_agent.evidence_snapshot()
    assert snapshot == {"OLD_2026-09-01_0000"}
    assert farm_agent.newest_run_dir(exclude=snapshot) is None


def test_newest_run_dir_picks_the_new_one(evidence_root):
    """有新目录时只认新的那个（旧的不参与比较）。"""
    old = evidence_root / "OLD_2026-09-01_0000"
    old.mkdir()
    snapshot = farm_agent.evidence_snapshot()
    new = evidence_root / "NEW_2026-09-21_1000"
    new.mkdir()
    assert farm_agent.newest_run_dir(exclude=snapshot) == new


def test_newest_run_dir_none_without_exclude_still_works(evidence_root):
    """不传 exclude 时保持旧行为（兼容调用方），返回最新目录。"""
    (evidence_root / "ONLY_2026-09-21_1000").mkdir()
    assert farm_agent.newest_run_dir() is not None


# ---------------- 凭据状态随证据回传 ----------------
#
# 控制机汇总时**只读 results/ 下的证据目录**，不读 done/ 回执。
# 凭据状态只写回执的话，汇总报告的「凭据」列恒为 ✗，
# 而手册让一线正是靠这一列区分"没配凭据"和"用例本身在跳过"。


def test_write_node_env_lands_in_run_dir(tmp_path):
    run_dir = tmp_path / "R01_2026-09-21_1000"
    run_dir.mkdir()
    creds = {"password_login": True, "microsoft_sso": False, "change_password": False, "share_creds": True}
    farm_agent._write_node_env(run_dir, "R01", "T1", "login", creds)

    payload = json.loads(
        (run_dir / farm_agent.NODE_ENV_EVIDENCE_NAME).read_text(encoding="utf-8")
    )
    assert payload["credentials"] == creds
    assert payload["node"] == "R01" and payload["task_id"] == "T1" and payload["suite"] == "login"
    assert "profile" in payload, "环境画像也一起带上，汇总时不必另找"


def test_write_node_env_does_not_crash_on_bad_path(tmp_path):
    """写不进去只警告，不能让整轮任务失败（证据已经跑出来了，别丢）。"""
    farm_agent._write_node_env(tmp_path / "no" / "such" / "dir", "R01", "T1", "login", {})


def test_evidence_name_matches_control_side():
    """节点写、控制机读的是同一个文件名，两处常量必须同源。"""
    from hall_auto.env_pack import NODE_ENV_EVIDENCE_NAME

    assert farm_agent.NODE_ENV_EVIDENCE_NAME == NODE_ENV_EVIDENCE_NAME


# ---------------- 空闲轮询不许静默（"--loop 卡住了"这个假象） ----------------
#
# 2026-09-21 首台真机 DESKTOP-DOHED68：`--loop` 启动后打印两行，然后 5 分钟没动静，
# 现场判定为"卡死"。实际空闲分支一个字都不打 —— 30 秒一次静默轮询，机器好得很。
#
# 更要命的是它和真故障**长得一样**：UNC 一断，`Path.is_file()` 把 OSError 吞成 False，
# `take_task` 走到"无任务"，节点在报平安而其实一个任务都取不到。
# 所以：① 每次空闲轮询必须留痕；② 农场探不到时必须吼出来，不能报"无任务"。


def test_idle_line_never_blank(root):
    """**核心回归锁**：空闲那一行不许是空串 —— 空白就是这次的 bug 本身。"""
    assert farm_agent.idle_line("R01", root, 1, "").strip()
    assert farm_agent.idle_line("R01", root, 7, "取不到农场").strip()


def test_idle_line_shows_which_file_it_waits_for(root):
    """要打出来它在等哪个文件 —— 节点名对不上是"一直无任务"的头号原因。"""
    line = farm_agent.idle_line("R01", root, 1, "")
    assert "无任务" in line
    assert "R01.json" in line


def test_idle_line_loud_when_farm_unreachable(root):
    """农场不可达时不能只说"无任务"，要给出排查方向。"""
    line = farm_agent.idle_line("R01", root, 3, "连不上 \\\\host\\hall-farm（系统错误 67）")
    assert "连不上" in line
    assert "一个任务都取不到" in line


def test_prepare_farm_ok_when_dirs_exist(root):
    assert farm_agent.prepare_farm(root) == ""


def test_prepare_farm_creates_missing_dirs(tmp_path):
    """目录不在就建出来（幂等）—— 节点第一次跑不必手工建。"""
    assert farm_agent.prepare_farm(tmp_path / "fresh") == ""
    assert (tmp_path / "fresh" / "tasks").is_dir()


def test_prepare_farm_reports_oserror_without_raising(tmp_path):
    """**关键**：共享盘不通时 `mkdir` 抛 `OSError`，必须变成一句原因，不许冒出 traceback。

    原来这个 `mkdir` 发生在 `main` 启动时 → `--loop` 刚起来就崩，
    一线看到的是 `PermissionError: [WinError 5] 拒绝访问` —— 看着像权限问题，
    实际多半是包源机关了。实测（2026-09-21）：指向不存在的共享名就是这个栈。
    """
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")   # 拿普通文件当父目录，mkdir 必抛 OSError
    assert farm_agent.prepare_farm(blocker / "farm") != ""


def test_loop_prints_heartbeat_while_idle(root, monkeypatch, capsys):
    """端到端锁：`--loop` 空转 3 轮，屏幕上必须有 3 行心跳。

    只测 `idle_line` 不够 —— 真正咬人的是"函数写对了但 main 里没调用"。
    """
    monkeypatch.setattr(farm_agent, "farm_root", lambda: root)
    monkeypatch.setattr(farm_agent, "node_id", lambda: "R01")

    ticks = {"n": 0}

    def fake_sleep(_seconds):
        ticks["n"] += 1
        if ticks["n"] >= 3:
            raise KeyboardInterrupt  # 用中断跳出无限循环，模拟人按 Ctrl+C

    monkeypatch.setattr(farm_agent.time, "sleep", fake_sleep)
    monkeypatch.setattr(sys, "argv", ["farm_agent.py", "--loop"])

    with pytest.raises(KeyboardInterrupt):
        farm_agent.main()

    out = capsys.readouterr().out
    assert out.count("轮询 #") == 3, "每轮空闲都要留痕，不许静默空转"
    assert "R01.json" in out, "要打出来它在等哪个任务文件"


def test_once_does_not_fake_green_when_farm_unreachable(tmp_path, monkeypatch, capsys):
    """**假绿防护**：农场不可达时 `--once` 不许打「无任务，退出」再返回 0。

    那样一线会以为"链路通了、只是没人投任务"，而真相是包源机可能已经关了 ——
    和"空套件写成功回执"是同一类错误。
    """
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(farm_agent, "farm_root", lambda: blocker / "farm")
    monkeypatch.setattr(farm_agent, "node_id", lambda: "R01")
    monkeypatch.setattr(sys, "argv", ["farm_agent.py", "--once"])

    assert farm_agent.main() == 3
    captured = capsys.readouterr()
    assert "无任务，退出" not in captured.out
    assert "农场不可达" in captured.err


def test_once_still_clean_when_farm_ok_and_no_task(root, monkeypatch, capsys):
    """正常空农场保持原行为：`无任务，退出` + 返回 0（这是链路通了的证明）。"""
    monkeypatch.setattr(farm_agent, "farm_root", lambda: root)
    monkeypatch.setattr(farm_agent, "node_id", lambda: "R01")
    monkeypatch.setattr(sys, "argv", ["farm_agent.py", "--once"])

    assert farm_agent.main() == 0
    assert "无任务，退出" in capsys.readouterr().out


# ---------- 人在环门禁 ----------
#
# 需求是「自动套件跑完后在终端问一句能不能参与人在环，回『能』就依次收验证码」。
# 这里锁的是**不能问的场合**：问了没人答，这个常驻 agent 会永久卡在 `input()`，
# 而控制机只看到「执行中」—— 与节点死机同形，是最坏的一类挂起。


class _Tty(io.StringIO):
    """够用的假终端：有 write/flush，且 isatty 为真。"""

    def isatty(self) -> bool:
        return True


class _NotTty(io.StringIO):
    def isatty(self) -> bool:
        return False


def _pretend_terminal(monkeypatch, *, stdin: bool = True, stdout: bool = True) -> None:
    """把 stdin / stdout 换成假终端。

    ⚠️ **必须在测试函数体里调，不能做成 fixture**：pytest 的全局捕获会在 setup 之后、
    call 之前把 `sys.stdout` 换成它自己的对象，fixture 里设的值会被冲掉 ——
    症状是「单跑能过、整跑就挂」（本轮真的踩了一次）。测试体里设的值才活到断言那一刻。
    """
    monkeypatch.setattr(farm_agent.sys, "stdin", _Tty() if stdin else _NotTty())
    monkeypatch.setattr(farm_agent.sys, "stdout", _Tty() if stdout else _NotTty())


def test_manual_prompt_silent_when_stdin_is_not_a_terminal(monkeypatch):
    """**关键保护**：stdin 不是终端（计划任务 / 管道）→ 连问都不许问。

    判据是 `isatty()` 而不是「有没有加 --no-manual」：默认就该是安全的，
    忘了加参数的无人值守节点不能因为漏一个开关就把自己锁死。
    """
    _pretend_terminal(monkeypatch, stdin=False)
    asked: list = []
    monkeypatch.setattr("builtins.input", lambda *a: asked.append(a) or "能")

    assert farm_agent.ask_manual_participation() is False
    assert asked == [], "stdin 不是终端时连 input() 都不该调"


def test_manual_prompt_silent_when_stdout_is_redirected(monkeypatch):
    """stdout 被重定向（`... --loop > log.txt`）→ 提示语到不了人眼前，也不能问。

    这种场合人只看到 agent 不动了，与 stdin 那条是**同一种挂起、不同原因**，
    所以两个都必须是终端的判据，缺一不可。
    """
    _pretend_terminal(monkeypatch, stdout=False)
    asked: list = []
    monkeypatch.setattr("builtins.input", lambda *a: asked.append(a) or "能")

    assert farm_agent.ask_manual_participation() is False
    assert asked == [], "stdout 被重定向时连 input() 都不该调"


def test_manual_prompt_accepts_the_documented_answer(monkeypatch):
    """需求原话是「我回复能」—— 中文「能」必须被认，不能只认 y/yes。"""
    _pretend_terminal(monkeypatch)
    for answer in ("能", "可以", "好", "是", "行", "y", "YES", "ok"):
        monkeypatch.setattr("builtins.input", lambda *a, _ans=answer: _ans)
        assert farm_agent.ask_manual_participation() is True, answer


def test_manual_prompt_treats_empty_and_other_replies_as_no(monkeypatch):
    """直接回车 / 别的回复 = 跳过，不能当成同意（会真发短信、真改密码）。"""
    _pretend_terminal(monkeypatch)
    for answer in ("", "   ", "不", "no", "n", "算了吧"):
        monkeypatch.setattr("builtins.input", lambda *a, _ans=answer: _ans)
        assert farm_agent.ask_manual_participation() is False, answer


def test_manual_prompt_survives_closed_stdin(monkeypatch):
    """`isatty()` 为真 ≠ 有人在 —— 本项目已踩过这个坑（farm_agent 起 pytest 时
    只重定向 stdout/stderr，子进程 `isatty()` 为真，于是真发短信 + `input()` 永久阻塞）。
    stdin 关着时必须当成不参与，不能抛也不能挂。
    """
    _pretend_terminal(monkeypatch)

    def _closed(*_args):
        raise EOFError

    monkeypatch.setattr("builtins.input", _closed)
    assert farm_agent.ask_manual_participation() is False


def test_manual_suite_matches_the_one_dispatch_refuses_to_send():
    """人在环阶段跑的套件必须**正好**是 `dispatch` 拒投的那一个。

    两边漂移的后果很具体：要么控制机投得进来（节点卡死），
    要么人在环阶段跑的不是同一批用例（收的码对不上用例）。
    """
    from hall_auto.suites import get_suite

    suite = get_suite(farm_agent.MANUAL_SUITE)
    assert suite.farm_safe is False
    assert farm_agent.MANUAL_SUITE == "login-manual"
    # 还必须是 interactive —— 人在环这条链路上它管两件事：`run_suite` 不重定向 stdout，
    # 以及 `build_command` 给 pytest 带 `-s`。少了 `-s`，pytest 默认捕获会让
    # `sys.stdin.isatty()` 恒 False，三条用例全 skip 却照报「退出码 0」，
    # **整套静默失效**（2026-09-22 真机踩过）。
    assert suite.interactive is True


def _boom_if_called(*_args, **_kwargs):
    raise AssertionError("这个场景不该问人在环")


def test_manual_phase_is_skipped_with_no_manual_flag(root, monkeypatch):
    """`--no-manual` 下，就算人真在终端前也不问（计划任务脚本化调用要能显式关掉）。"""
    _write_task(root, "R01", _task())
    monkeypatch.setattr(farm_agent, "farm_root", lambda: root)
    monkeypatch.setattr(farm_agent, "node_id", lambda: "R01")
    monkeypatch.setattr(sys, "argv", ["farm_agent.py", "--once", "--no-manual"])
    monkeypatch.setattr(farm_agent, "run_suite", lambda *a, **k: (0, {}))
    # 证据快照/新目录发现都指向真实 reports/evidence —— 不拦掉会真去 copytree 一整个目录
    monkeypatch.setattr(farm_agent, "evidence_snapshot", lambda: set())
    monkeypatch.setattr(farm_agent, "newest_run_dir", lambda exclude=None: None)
    # 人就在终端前（门禁放行）—— 此时唯一能拦住提问的只有 --no-manual 本身
    monkeypatch.setattr(farm_agent, "_manual_capable", lambda: True)
    monkeypatch.setattr(farm_agent, "ask_manual_participation", _boom_if_called)

    assert farm_agent.main() == 0


def test_manual_phase_runs_when_user_says_yes(root, monkeypatch):
    """回「能」→ 人在环套件要被真的执行一次，且**标 interactive**。

    标 interactive 是回执的一部分：汇总时要能区分「无人值守跑的」和
    「真人守着收码跑的」，否则要人输验证码的用例和纯自动用例在报告里长得一样。

    任务造的是**全量档** —— 默认只有全量档跑完才问人在环（`task_covers_full_run`）。
    """
    _write_task(root, "R01", _full_run_task())
    monkeypatch.setattr(farm_agent, "farm_root", lambda: root)
    monkeypatch.setattr(farm_agent, "node_id", lambda: "R01")
    monkeypatch.setattr(sys, "argv", ["farm_agent.py", "--once"])
    monkeypatch.setattr(farm_agent, "evidence_snapshot", lambda: set())
    monkeypatch.setattr(farm_agent, "newest_run_dir", lambda exclude=None: None)

    calls: list[tuple] = []

    def _fake_run_suite(name, sid, scount, log, *, task_env=None, interactive=False):
        calls.append((name, interactive))
        return 0, {}

    monkeypatch.setattr(farm_agent, "run_suite", _fake_run_suite)
    monkeypatch.setattr(farm_agent, "ask_manual_participation", lambda: True)

    assert farm_agent.main() == 0
    assert ("launch", False) in calls, "任务里的自动套件要先跑"
    assert (farm_agent.MANUAL_SUITE, True) in calls, "回「能」后人在环套件要以交互模式跑"
    receipt = json.loads((root / "done" / "R01_T1.json").read_text(encoding="utf-8"))
    manual = [r for r in receipt["results"] if r["suite"] == farm_agent.MANUAL_SUITE]
    assert manual and manual[0].get("interactive") is True


def test_manual_prompt_is_skipped_for_a_smoke_task(root, monkeypatch):
    """冒烟任务跑完**不许问**人在环 —— 2026-09-23 真机挂起的回归守卫。

    冒烟的全部意义是「几十秒验链路通」。弹一个要人回答的问句，人不答就永久
    `input()` 阻塞，而控制机侧看到的是「执行中」不动 —— **与节点死机同形**，
    现场只能干等（真机：4 passed / 36.9s 就跑完，之后两分多钟毫无动静）。
    """
    _write_task(root, "R01", _task(suites=[{"name": "launch"}]))
    monkeypatch.setattr(farm_agent, "farm_root", lambda: root)
    monkeypatch.setattr(farm_agent, "node_id", lambda: "R01")
    monkeypatch.setattr(sys, "argv", ["farm_agent.py", "--once"])
    monkeypatch.setattr(farm_agent, "run_suite", lambda *a, **k: (0, {}))
    monkeypatch.setattr(farm_agent, "evidence_snapshot", lambda: set())
    monkeypatch.setattr(farm_agent, "newest_run_dir", lambda exclude=None: None)
    # 人就在终端前（门禁放行）—— 此时唯一能拦住提问的只有「任务不是全量档」本身
    monkeypatch.setattr(farm_agent, "_manual_capable", lambda: True)
    monkeypatch.setattr(farm_agent, "ask_manual_participation", _boom_if_called)

    assert farm_agent.main() == 0
    assert (root / "done" / "R01_T1.json").is_file(), "冒烟任务也必须照常写回执、清 .running"


def test_ask_manual_flag_forces_the_prompt_on_a_smoke_task(root, monkeypatch):
    """`--ask-manual` 能把「只对全量档问」那道门打开 —— 手工调试要在冒烟后跑人在环。"""
    _write_task(root, "R01", _task(suites=[{"name": "launch"}]))
    monkeypatch.setattr(farm_agent, "farm_root", lambda: root)
    monkeypatch.setattr(farm_agent, "node_id", lambda: "R01")
    monkeypatch.setattr(sys, "argv", ["farm_agent.py", "--once", "--ask-manual"])
    monkeypatch.setattr(farm_agent, "evidence_snapshot", lambda: set())
    monkeypatch.setattr(farm_agent, "newest_run_dir", lambda exclude=None: None)

    calls: list[tuple] = []

    def _fake_run_suite(name, sid, scount, log, *, task_env=None, interactive=False):
        calls.append((name, interactive))
        return 0, {}

    monkeypatch.setattr(farm_agent, "run_suite", _fake_run_suite)
    monkeypatch.setattr(farm_agent, "ask_manual_participation", lambda: True)

    assert farm_agent.main() == 0
    assert (farm_agent.MANUAL_SUITE, True) in calls


def test_task_covers_full_run_needs_the_whole_set():
    """判据是「**覆盖**全量档」，不是「任务里有几个套件」。少一个都不算。"""
    full = [{"name": n} for n in farm_agent.FULL_RUN_SUITES]
    assert farm_agent.task_covers_full_run({"suites": full})
    assert farm_agent.task_covers_full_run({"suites": full + [{"name": "extra"}]})
    assert not farm_agent.task_covers_full_run({"suites": [{"name": "launch"}]})
    assert not farm_agent.task_covers_full_run({"suites": full[:-1]}), "少一个都不算全量档"
    assert not farm_agent.task_covers_full_run({"suites": []})
    assert not farm_agent.task_covers_full_run({}), "任务缺 suites 键不能炸"


# ---------- 人在环提示语的**内容** ----------
#
# 提示语是人在环**唯一的预告**：人照着它准备手机和邮箱。
# 它写错一个字，现场就是「码发了、人却没在等」，而终端上看着一切正常。
# 2026-09-22 用户指出两处：① 微软那条**不只**要邮箱码，登录成功后还会弹
# 「绑定手机号」再要 1 个短信码；② 报码顺序和实际执行顺序是反的。

# 每条用例「主要收哪种码」的期望关键词。**不是**穷举，是防「标签写串行」：
# 微软那条如果被写成只要短信，人就不会去开邮箱。
_EXPECTED_CODE_KIND = {
    "test_microsoft_login_code_manual": "邮箱",
    "test_sms_login_manual": "短信",
    "test_forgot_password_reset_manual": "密码",
}


def _manual_test_order_in_source() -> list[str]:
    """按**文件顺序**取出 `tests/launch/test_p1_login.py` 里 `-m manual` 的用例名。

    **为什么要读源码而不是写死**：提示语的顺序必须等于 pytest 的实际执行顺序，
    而 pytest 不改文件顺序。写死的话，以后谁调整了用例位置，
    提示语会静默地和实际顺序对不上 —— 正是本轮修的那个 bug 的成因。
    """
    import ast

    src = (REPO_ROOT / "tests" / "launch" / "test_p1_login.py").read_text(encoding="utf-8")
    names: list[str] = []
    for node in ast.parse(src).body:
        if not isinstance(node, ast.FunctionDef):
            continue
        marks = {
            dec.attr
            for dec in node.decorator_list
            if isinstance(dec, ast.Attribute) and isinstance(dec.value, ast.Attribute)
        }
        if {"login", "manual"} <= marks:
            names.append(node.name)
    return names


def _prompt_text(monkeypatch) -> str:
    """把提示语整段抓下来（回空串 = 不参与，不触发任何后续动作）。

    ⚠️ **不能用 `capsys`**：`_pretend_terminal` 把 `sys.stdout` 换成了自己的
    `_Tty`，提示语全写进那个 StringIO，pytest 的捕获里一个字都没有
    （本轮真踩了一次：断言报 `'绑定手机号' in ''`）。所以直接读那个 sink。
    """
    sink = _Tty()
    monkeypatch.setattr(farm_agent.sys, "stdin", _Tty())
    monkeypatch.setattr(farm_agent.sys, "stdout", sink)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    assert farm_agent.ask_manual_participation() is False
    return sink.getvalue()


def test_manual_prompt_order_matches_the_test_file(monkeypatch):
    """报码顺序必须**等于** `test_p1_login.py` 里 `-m manual` 用例的文件顺序。

    原来提示语把「短信登录」写在「微软登录」前面，实际跑的是微软先 ——
    人按提示先掏手机，第一条要的却是邮箱码。
    """
    out = _prompt_text(monkeypatch)

    declared = [name for name, _label in farm_agent.MANUAL_SEQUENCE]
    assert declared == _manual_test_order_in_source(), (
        "MANUAL_SEQUENCE 的顺序与 test_p1_login.py 里 -m manual 用例的文件顺序不一致："
        f"提示语={declared}"
    )

    # 顺序断言之外还要锁「标签没写串行」：微软那条必须提邮箱，不是只提短信。
    for name, label in farm_agent.MANUAL_SEQUENCE:
        assert _EXPECTED_CODE_KIND[name] in label, f"{name} 的标签没提它主要收哪种码：{label}"

    # 提示语必须真按这个顺序打出来（不是常量躺着没人用）
    positions = [out.index(label) for _name, label in farm_agent.MANUAL_SEQUENCE]
    assert positions == sorted(positions), "提示语没按 MANUAL_SEQUENCE 的顺序打印"
    assert len(positions) == len(farm_agent.MANUAL_SEQUENCE)


def test_manual_prompt_warns_about_the_bind_sms_code(monkeypatch):
    """**关键保护**：提示语必须写明「登录成功后可能弹绑定手机号、还要再给 1 个短信码」。

    用户 2026-09-22 指出：微软那条**不只**要邮箱码 —— 邮箱验证通过、登录成功后，
    没绑过号的账号会弹「绑定手机号」，脚本自动填号 + 自动点「获取验证码」，
    然后**还要人再给 1 个短信码**。提示语漏掉它，人只准备了邮箱、手机不在手边，
    就卡在那个弹窗上（模态窗，还挡着后面每条用例）。
    """
    out = _prompt_text(monkeypatch)

    assert "绑定手机号" in out, "提示语没提「绑定手机号」弹窗"
    assert "短信码" in out, "提示语没提要额外再给 1 个短信码"
    # 「会弹窗」但不说要码等于没说 —— 断言这两件事在同一句里
    warning = "".join(farm_agent._MANUAL_BIND_WARNING)
    assert "绑定手机号" in warning and "短信码" in warning
    for line in farm_agent._MANUAL_BIND_WARNING:
        assert line in out, f"绑定提示没打进终端：{line!r}"


def test_manual_prompt_says_which_codes_need_a_phone(monkeypatch):
    """手机必须在手边 —— 否则人只拿邮箱来，第 2 条开始就废了。

    这一条防的是「把绑定码从提示里删掉」这类回退：只要还要求手机，
    就必须有那句提醒；哪天真的不需要手机了，这条会红，提醒改文档而不是静默。
    """
    out = _prompt_text(monkeypatch)
    assert "手机" in out

