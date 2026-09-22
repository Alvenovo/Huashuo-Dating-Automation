"""锁证据采集策略（`hall_auto/evidence.py` + `tests/conftest.py` 的 `--evidence`）。

## 为什么单开一个文件

证据是**交付物**：`report.html` 是自包含单文件，手册里写着"可直发组长"；
多机汇总（`farm_control.aggregate`）只读证据目录，不读 `done/` 回执。
但 2026-09-22 查的时候，`EvidenceSession` **一条用例都没有** —— 两个模式都没测。
（`farm_agent` 侧的 `evidence_snapshot` / `newest_run_dir` 有测，但那是"怎么找到本轮目录"，
不是"录不录、录成什么"。）

## 默认值为什么要锁

2026-09-22 把默认从 `all` 改成 `failure-only`（跑批产物太多：`reports/` 下已堆了 3.8 万张 PNG）。
改默认之前必须先确认**没人在依赖"成功也留证"**，查下来只有一处关键依赖：

- `farm_control.aggregate` 读的是 `summary.json`，**不是** per-case 的 `result.json`；
  而 `finish()` 无条件写 `summary.json` -> 汇总不受影响。
- `farm_agent.newest_run_dir` 靠"证据根目录快照前后差"认本轮目录，
  所以**必须**保证 `new_run_dir()` 无论什么模式都会建目录 —— 否则套件全绿时找不到本轮目录，
  农场会**张冠李戴**回传上一轮的旧目录（比没有证据危险得多，见 `newest_run_dir` 的注释）。

这两条都是"改了默认也不会坏"的**前提**，所以都在这里锁住。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from PIL import Image

from hall_auto import evidence as ev

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

NODEID_OK = "tests/launch/test_p1_apps.py::test_list_renders"
NODEID_BAD = "tests/launch/test_p1_apps.py::test_install_flow"


@pytest.fixture
def fake_capture(monkeypatch):
    """替掉真抓屏 —— 单测不许碰屏幕，也要让"抓到了"这件事可断言。

    ⚠️ 这里**必须写出一张合法 PNG**，不能图省事写 `b"png"`：
    `conftest.pytest_runtest_makereport` 是在 **call 阶段结束、fixture 还没 teardown 时**
    截图的（它自己的注释就是这么写的），所以本 fixture 生效期间，**真 session 也会走这个假抓屏**。
    写个假文件的话，最后 `_render_html` 里 `Image.open()` 打不开，
    会把整个 session 的 `pytest_sessionfinish` 崩掉 —— 报错还指向真实证据目录，极难对上号。
    """
    calls: list[Path] = []

    def _fake(path: Path) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1, 1), (0, 0, 0)).save(path)
        calls.append(path)
        return True

    monkeypatch.setattr(ev, "capture_screen", _fake)
    return calls


def _session(tmp_path, mode):
    run = tmp_path / "run"
    run.mkdir()
    return ev.EvidenceSession(run, mode), run


def _case_dir(run: Path, nodeid: str) -> Path:
    return run / ev._safe_name(ev.module_of(nodeid)) / ev._safe_name(ev.case_of(nodeid))


# ---------------- 两个模式各录什么 ----------------


def test_failure_only_records_evidence_only_for_failed_cases(tmp_path, fake_capture):
    """`failure-only` 下：失败留原图 + result.json，通过/跳过**一个文件都不写**。"""
    s, run = _session(tmp_path, ev.MODE_FAILURE)

    s.record(NODEID_OK, ev.OUTCOME_PASSED, 1.0, "assert True")
    assert not _case_dir(run, NODEID_OK).exists(), "通过用例不该留证据目录"
    assert fake_capture == [], "通过用例连抓屏都不该做（省时间也是改默认的动机之一）"

    s.record(NODEID_BAD, ev.OUTCOME_FAILED, 2.0, "assert False")
    d = _case_dir(run, NODEID_BAD)
    assert (d / "final.png").is_file(), "失败用例必须留原图 —— 那是排查的唯一线索"
    rec = json.loads((d / "result.json").read_text(encoding="utf-8"))
    assert rec["outcome"] == ev.OUTCOME_FAILED
    assert rec["assertion"] == "assert False"
    assert rec["screenshot"], "result.json 里要指向那张图，否则报告嵌不出来"


def test_all_mode_records_evidence_for_every_outcome(tmp_path, fake_capture):
    """`all` 下：三种结果都留证（显式指定时要能拿回老行为）。"""
    s, run = _session(tmp_path, ev.MODE_ALL)

    for nodeid, outcome in (
        (NODEID_OK, ev.OUTCOME_PASSED),
        (NODEID_BAD, ev.OUTCOME_FAILED),
        ("tests/launch/test_p1_apps.py::test_needs_creds", ev.OUTCOME_SKIPPED),
    ):
        s.record(nodeid, outcome, 0.5, "x")
        assert (_case_dir(run, nodeid) / "result.json").is_file(), f"{outcome} 该留证"


# ---------------- 汇总读的是 summary.json，不是 per-case ----------------


def test_finish_always_writes_summary_and_report_listing_every_case(tmp_path, fake_capture):
    """`finish()` 必须无条件写 `summary.json` + `report.html`，且**列全**所有用例。

    这条是改默认的**前提**：控制机汇总只读 `summary.json`。
    如果 failure-only 下 `summary.json` 只列失败用例（或干脆不写），
    那多机汇总报告会凭空少掉一堆通过的用例 —— 看着像"那台机器没跑"，比报错还难查。
    """
    s, run = _session(tmp_path, ev.MODE_FAILURE)
    s.record(NODEID_OK, ev.OUTCOME_PASSED, 1.0, "assert True")
    s.record(NODEID_BAD, ev.OUTCOME_FAILED, 2.0, "assert False")
    report = s.finish()

    assert report == run / "report.html" and report.is_file()
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    assert summary["mode"] == ev.MODE_FAILURE
    assert summary["total"] == 2, "通过的那条也要在 total 里 —— 它确实跑过了"
    assert summary["passed"] == 1 and summary["failed"] == 1
    nodeids = {c["nodeid"] for c in summary["cases"]}
    assert nodeids == {NODEID_OK, NODEID_BAD}, "不抓图的用例也要留在 cases 里"
    assert [c for c in summary["cases"] if c["nodeid"] == NODEID_OK][0]["screenshot"] is None


# ---------------- 农场靠它认本轮目录：目录必须无条件建出来 ----------------


def test_new_run_dir_creates_the_directory_even_with_no_evidence(tmp_path):
    """**这条是 failure-only 能安全当默认的关键**。

    `farm_agent.newest_run_dir()` 用"跑之前的目录快照"排除上一轮 —— 本轮目录**必须存在**，
    它才认得出来。所以 `new_run_dir()` 无论什么模式都得建目录：
    套件全绿、一张图都没有，目录也要在。否则农场会回传**上一轮的旧目录**，
    报告里那台机器挂着一份别的运行的结果。
    """
    run = ev.new_run_dir(root=tmp_path, node="R01")

    assert run.is_dir(), "目录没建出来 —— 农场就认不出本轮"
    assert run.name.startswith("R01_"), "节点前缀是防多机回传撞名的，别丢"


def test_new_run_dir_does_not_collide_within_the_same_minute(tmp_path):
    """同一分钟起两次（重跑/多套件）不能撞名 —— 撞了就是两份结果互相覆盖。"""
    a = ev.new_run_dir(root=tmp_path, node="R01")
    b = ev.new_run_dir(root=tmp_path, node="R01")

    assert a != b and a.is_dir() and b.is_dir()


# ---------------- 默认值：代码与手册必须说同一句话 ----------------


def _conftest_evidence_default() -> str:
    """从 `tests/conftest.py` 的 `--evidence` 注册里读出 `default=` 的实际值。"""
    src = (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "addoption"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "--evidence"
        ):
            continue
        for kw in node.keywords:
            if kw.arg != "default":
                continue
            if isinstance(kw.value, ast.Constant):
                return kw.value.value
            # conftest 里写的是常量名（MODE_FAILURE），按 evidence 模块里的真值解析
            assert isinstance(kw.value, ast.Name), f"看不懂的 default 写法：{ast.dump(kw.value)}"
            return getattr(ev, kw.value.id)
    raise AssertionError("`tests/conftest.py` 里找不到 `--evidence` 的 default —— 守卫失效了")


def test_evidence_default_is_failure_only():
    """2026-09-22 定调：默认 `failure-only`（跑批产物太多，`reports/` 已堆 3.8 万张 PNG）。

    锁住是为了**别被静默改回去**：这个值决定每台机器每轮跑批往盘上写多少东西，
    改它该是一次有意识的决定，不是顺手。
    """
    assert _conftest_evidence_default() == ev.MODE_FAILURE
    assert ev.MODE_FAILURE == "failure-only", "命令行取值变了，手册和脚本都在用字面量"


def test_runbook_states_the_same_default_as_conftest():
    """手册写的默认值必须跟 conftest 一致 —— 不然一线按手册加开关会加反。"""
    default = _conftest_evidence_default()
    text = (REPO_ROOT / "项目知识库" / "运行手册.md").read_text(encoding="utf-8")

    assert f"`--evidence={default}`（默认）" in text, (
        f"运行手册里没写「`--evidence={default}`（默认）」—— "
        "默认值改了但手册没跟上，一线会以为要显式加 failure-only，或者反过来以为默认是全留"
    )
