"""锁提权通道的协议（`hall_auto/elevation.py`）。

提权通道是「非提权 agent 跑管理员套件」的唯一路径。它坏掉的表现都很安静：

- **参数校验放松** → 提权跑的是管理员 pytest，参数来源必须可预期；
- **结果按「文件在不在」认领** → 上一轮的结果文件还在原地，新一轮直接读到旧结果（假绿）；
- **请求文件不是原子写** → 提权侧读到写了一半的 JSON，当成「没有请求」，
  表现是「触发了但什么都没跑」，节点侧一句报错都没有；
- **request_id 不带微秒** → 重跑同一任务时新请求被「已经跑过」挡掉。

这里逐条钉住。守卫不测 `schtasks` 本身（那是环境），只测我们自己写的逻辑。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from hall_auto import elevation  # noqa: E402

pytestmark = pytest.mark.unit


# ---------------- pytest 参数白名单 ----------------

def test_validate_accepts_a_normal_suite_command():
    """正常形态：套件路径 + `-m` 表达式 + `-v`（就是 `build_command` 的产物）。"""
    args, reject = elevation.validate_pytest_args(
        ["tests/install", "-m", "install", "-v"]
    )
    assert reject == ""
    assert args == ["tests/install", "-m", "install", "-v"]


def test_validate_accepts_shard_flags_and_dash_s():
    args, reject = elevation.validate_pytest_args(
        ["tests/launch/test_p1_apps.py", "-m", "apps and not destructive", "-v", "-s",
         "--shard-id=2", "--shard-count=5"]
    )
    assert reject == ""
    assert "--shard-id=2" in args


def test_validate_rejects_arbitrary_flags():
    """**关键保护**：请求文件躺在仓库里、又被提权进程执行，参数必须是可预期的。

    放开的话，往 request.json 里塞一个 `--rootdir` / `-c` / `-p` 之类的参数，
    提权跑的就不再是那套用例了。
    """
    for bad in (["-c", "evil.ini"], ["--rootdir", "C:/"], ["tests/install", "--co"]):
        args, reject = elevation.validate_pytest_args(bad)
        assert reject, f"该拒绝：{bad}"
        assert args == []


def test_validate_rejects_path_traversal_and_absolute_paths():
    for bad in (["tests/../../windows/system32", "-v"], ["C:/Windows/System32", "-v"]):
        _args, reject = elevation.validate_pytest_args(bad)
        assert reject, f"该拒绝：{bad}"


def test_validate_rejects_empty():
    for bad in ([], None, "tests/install"):
        _args, reject = elevation.validate_pytest_args(bad)
        assert reject


def test_validate_accepts_the_isolated_basetemp():
    """`--basetemp=reports/...` 要放行 —— 提权段的 pytest 也得有专属临时目录。

    不放行的话，提权段会去用共享的 `pytest-of-<user>`；管理员跑过一次之后，
    普通权限的 `tmp_path` 就全崩（2026-09-23 真机 238 条 ERROR）。
    """
    argv = [
        "tests/launch/test_p1_apps.py", "-m", "apps and destructive", "-v",
        "-p", "no:cacheprovider",
        "--basetemp=reports/_pytest_tmp/task_1_apps-lifecycle_s1_p123",
    ]
    args, reject = elevation.validate_pytest_args(argv)
    assert reject == ""
    assert "--basetemp=reports/_pytest_tmp/task_1_apps-lifecycle_s1_p123" in args


def test_validate_rejects_basetemp_outside_the_repo():
    """**关键保护**：放行 `--basetemp` 是因为它要带值，但值只许指向仓库自己的临时目录。

    放开绝对路径 / `..`，等于让提权段把临时目录写到机器上任意位置。
    """
    for bad in ("--basetemp=C:/Windows/Temp", "--basetemp=/tmp/x",
                "--basetemp=reports/../../evil", "--basetemp=reports\\..\\evil"):
        _args, reject = elevation.validate_pytest_args(["tests/install", "-v", bad])
        assert reject, f"该拒绝：{bad}"


# ---------------- 请求 / 结果文件 ----------------

def test_write_request_is_atomic_and_leaves_no_tmp(tmp_path):
    """原子写：先 `.tmp` 再 `replace`。

    直接 `write_text` 的话提权侧可能读到写了一半的 JSON，解析失败被当成
    「没有请求」—— 表现同样是「触发了但什么都没跑」。
    """
    elevation.write_request(
        task_id="T1", suite="install", shard_id=1, shard_count=1,
        pytest_args=["tests/install", "-m", "install", "-v"], root=tmp_path,
    )
    assert elevation.request_path(tmp_path).is_file()
    assert not list(tmp_path.glob("*.tmp")), "临时文件要清理干净"


def test_write_request_clears_the_previous_result(tmp_path):
    """写新请求时**必须清掉旧结果**。

    不清的话，提权侧刚被触发就能读到上一轮的结果文件 —— 就算按 request_id
    比对能挡住误判，「请求还没被处理」和「处理完了」之间的区分也多绕一层。
    """
    elevation.write_json(elevation.result_path(tmp_path), {"request_id": "OLD", "exit_code": 0})
    elevation.write_request(
        task_id="T1", suite="install", shard_id=1, shard_count=1,
        pytest_args=["tests/install", "-m", "install", "-v"], root=tmp_path,
    )
    assert not elevation.result_path(tmp_path).exists()


def test_read_result_for_ignores_a_stale_request_id(tmp_path):
    """**核心回归锁**：request_id 对不上 → 空字典，绝不把上一轮的结果当本轮。

    这一条防的就是本项目反复踩的「错认上一轮」：只判断「结果文件在不在」的话，
    上一轮的 `exit_code: 0` 会让新一轮直接判成功，而它其实一条都没跑。
    """
    elevation.write_json(
        elevation.result_path(tmp_path), {"request_id": "REQ-OLD", "exit_code": 0}
    )
    assert elevation.read_result_for("REQ-NEW", tmp_path) == {}
    assert elevation.read_result_for("REQ-OLD", tmp_path)["exit_code"] == 0


def test_new_request_id_is_unique_per_call():
    """同一秒内连投两个套件也要能区分开 —— 所以带微秒。

    不带的话，重跑同一任务时新请求会被提权侧的「已经跑过」判断挡掉，
    表现是「任务投了、提权段一直不执行」，且不报错。
    """
    ids = {elevation.new_request_id("T1", "install", 1) for _ in range(50)}
    assert len(ids) == 50


def test_new_request_id_encodes_what_it_is():
    """id 里带上任务 / 套件 / 分片 —— 排查时看一行就知道是哪个请求。"""
    rid = elevation.new_request_id("T1", "install", 2)
    assert rid.startswith("T1_install_s2_")


def test_read_json_tolerates_garbage(tmp_path):
    """读不动 / 不是字典 → 空字典，不抛异常（调用方按「没有」处理）。"""
    bad = tmp_path / "bad.json"
    bad.write_text("{不是合法 JSON", encoding="utf-8")
    assert elevation.read_json(bad) == {}
    assert elevation.read_json(tmp_path / "nope.json") == {}

    arr = tmp_path / "arr.json"
    arr.write_text("[1, 2, 3]", encoding="utf-8")
    assert elevation.read_json(arr) == {}


def test_write_json_round_trip(tmp_path):
    path = tmp_path / "x.json"
    elevation.write_json(path, {"k": "中文", "n": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"k": "中文", "n": 1}


# ---------------- 计划任务常量 ----------------

def test_task_name_and_script_are_the_single_definition():
    """任务名 / 动作脚本只在这一处定义。

    三处漂移（bootstrap 建 A、agent 触发 B、文档写 C）的表现是
    「任务触发了但什么都没跑」，节点侧一句报错都没有 —— 最难查的一类。
    """
    assert elevation.ELEVATION_TASK_NAME == "HallAutoP1"
    assert elevation.ELEVATION_SCRIPT.name == "run_elevated_suite.ps1"
    assert elevation.ELEVATION_SCRIPT.is_file(), "动作脚本必须真实存在，否则任务建了也跑不起来"


def test_trigger_task_reports_failure_instead_of_raising(monkeypatch):
    """`schtasks` 起不来时返回 (False, 说明)，不抛异常。

    抛异常会把整批套件打断在提权段；返回失败码能让 agent 继续跑后面的套件，
    回执里那条 `exit_code != 0` 就是现场结论。
    """
    def _boom(*_a, **_k):
        raise OSError("schtasks 不见了")

    monkeypatch.setattr(elevation.subprocess, "run", _boom)
    ok, note = elevation.trigger_task()
    assert ok is False
    assert "schtasks" in note


def test_trigger_task_passes_the_task_name_as_argv(monkeypatch):
    """用参数列表调用，**不走 shell**。

    Git Bash 会把 `/Run` 当路径转换（命令行版必须写 `MSYS_NO_PATHCONV=1`）；
    Python 传参数列表没有这个问题，这也是走 broker 比让人粘命令稳的地方。
    """
    seen: dict = {}

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["shell"] = kwargs.get("shell", False)
        return _Proc()

    monkeypatch.setattr(elevation.subprocess, "run", _fake_run)
    ok, _note = elevation.trigger_task()
    assert ok is True
    assert seen["argv"][:3] == ["schtasks", "/Run", "/TN"]
    assert seen["argv"][3] == elevation.ELEVATION_TASK_NAME
    assert not seen["shell"], "不能走 shell —— /Run 会被当路径转换"
