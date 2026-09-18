"""锁农场任务取用策略（tools/farm_agent.take_task）。

两个坑都是"重复/空跑"导致的：
- 控制机重复 dispatch 同一个 task_id → 节点把 `install` / `apps-lifecycle` 真装卸套件
  又跑一遍，白等十几分钟还搅乱机器状态。
- 任务里 suites 为空 → 旧实现照样写一份"成功"回执，aggregate 把该节点算成已完成（假绿）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import farm_agent  # noqa: E402


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
