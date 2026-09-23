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
import importlib.util
import json
import types
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


# ---------------- setup 阶段失败（pytest 的 ERROR）必须进记录 ----------------


def _load_conftest():
    """按**路径**加载 `tests/conftest.py`。

    不写 `import conftest`：跑 pytest 时它可能以 `conftest` / `tests.conftest`
    两种名字待在 `sys.modules` 里，按名字取不可靠。按路径加载才稳定。
    """
    spec = importlib.util.spec_from_file_location(
        "_hall_conftest_guard", REPO_ROOT / "tests" / "conftest.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeReport:
    """只带 hook 用到的那几个字段的假 report。"""

    def __init__(self, when, *, failed=False, skipped=False, longrepr="", duration=0.1):
        self.when = when
        self.failed = failed
        self.skipped = skipped
        self.longrepr = longrepr
        self.duration = duration


class _FakeItem:
    def __init__(self, nodeid, evidence):
        self.nodeid = nodeid
        self.config = types.SimpleNamespace(_evidence=evidence)
        self.function = types.SimpleNamespace(__doc__="用例说明")

    def iter_markers(self):
        return []

    def get_closest_marker(self, name):
        """conftest 的 `_wants_screenshot` 用它判「是不是 unit 用例」。

        假 item 没有 marker → 返回 None → 按「要抓屏」处理，
        和这些用例原来的预期一致。
        """
        return next((m for m in self.iter_markers() if getattr(m, "name", "") == name), None)


def _feed_report(conftest_mod, item, report):
    """驱动 hookwrapper 版的 `pytest_runtest_makereport`：走到 yield，再把假 report 交回去。"""
    gen = conftest_mod.pytest_runtest_makereport(item, None)
    next(gen)

    class _Outcome:
        def get_result(self):
            return report

    try:
        gen.send(_Outcome())
    except StopIteration:
        pass


def test_setup_error_is_recorded_so_it_cannot_be_silently_dropped(tmp_path, fake_capture):
    """**这条锁的是 2026-09-22 的假绿根因。**

    `pytest_runtest_makereport` 原先只认 `when == "call"`，外加 `when == "setup"` 的
    **skip** —— `setup` 阶段失败（= pytest 的 ERROR，fixture 崩了、用例一行都没执行）
    **没有分支，直接被丢弃**。

    真机实测后果：节点上 188 条用例因 `%TEMP%\\pytest-of-admin` 拒绝访问而在 setup 就崩，
    `summary.json` 却写 `total 273 / passed 273 / failed 0`；而
    `farm_control.aggregate` 按 nodeid 合并跨轮结果、**新那轮没记录就保留老记录**，
    于是矩阵里那一格挂着更早一轮的 ✓ —— 整行看着全绿。

    所以这里断言三件事：进 records、进 summary 的 `errors`、**不混进 failed**。
    """
    s, run = _session(tmp_path, ev.MODE_FAILURE)
    item = _FakeItem(NODEID_BAD, s)
    _feed_report(
        _load_conftest(), item,
        _FakeReport("setup", failed=True, longrepr="PermissionError: [WinError 5] 拒绝访问。"),
    )

    assert [r.outcome for r in s.records] == [ev.OUTCOME_ERROR], (
        "setup 阶段失败没进记录 —— 汇总报告那一格会留着上一轮的旧结果，假绿又回来了"
    )

    report = s.finish()
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    assert summary["total"] == 1 and summary["errors"] == 1, "error 没进 summary —— 汇总层还是看不见它"
    assert summary["failed"] == 0, (
        "error 不该混进失败列：看报告的人要能区分「跑挂了」和「根本没跑起来」"
    )
    assert (_case_dir(run, NODEID_BAD) / "final.png").is_file(), (
        "error 也要留图 —— 临时目录被拒这类现场只能靠截图复现"
    )
    html = report.read_text(encoding="utf-8")
    assert "错误" in html, "报告里要能一眼看到「错误」这一档，不能只有通过/失败/跳过"


def test_setup_skip_is_still_recorded_as_skipped(tmp_path, fake_capture):
    """补 error 分支别把原有的 setup-skip 分支挤掉（缺凭据/缺夹具靠它才看得见）。"""
    s, _ = _session(tmp_path, ev.MODE_FAILURE)
    item = _FakeItem(NODEID_OK, s)
    _feed_report(_load_conftest(), item, _FakeReport("setup", skipped=True, longrepr="Skipped: 缺凭据"))

    assert [r.outcome for r in s.records] == [ev.OUTCOME_SKIPPED]
    assert "缺凭据" in s.records[0].assertion


def test_call_phase_result_is_unchanged(tmp_path, fake_capture):
    """call 阶段的行为不能被这次改动带偏（通过/失败仍照旧）。"""
    s, _ = _session(tmp_path, ev.MODE_FAILURE)
    item = _FakeItem(NODEID_OK, s)
    _feed_report(_load_conftest(), item, _FakeReport("call", failed=False))

    assert [r.outcome for r in s.records] == [ev.OUTCOME_PASSED]


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


def test_evidence_default_is_all():
    """2026-09-23 定调：默认 `all`（成功失败都留证）。

    **2026-09-22 曾定成 `failure-only`**（`reports/` 堆到 3.8 万张 PNG），
    2026-09-23 用户要求改回 `all` —— 理由是排查时**通过用例的现场图也有用**
    （"这一步界面长什么样" 只能从通过那刻的图看，失败图只有崩掉那一刻）。

    ⚠️ **代价要知道**：每台机器每轮全量档约 650 张图（`unit` 618 条也各一张），
    比 `failure-only` 多约 150-200MB/轮。盘紧张时用 `--evidence=failure-only` 压回去。

    锁住是为了**别被静默改回去**：这个值决定每台机器每轮跑批往盘上写多少东西，
    改它该是一次有意识的决定，不是顺手。
    """
    assert _conftest_evidence_default() == ev.MODE_ALL
    assert ev.MODE_ALL == "all", "命令行取值变了，手册和脚本都在用字面量"


def test_runbook_states_the_same_default_as_conftest():
    """手册写的默认值必须跟 conftest 一致 —— 不然一线按手册加开关会加反。"""
    default = _conftest_evidence_default()
    text = (REPO_ROOT / "项目知识库" / "运行手册.md").read_text(encoding="utf-8")

    assert f"`--evidence={default}`（默认）" in text, (
        f"运行手册里没写「`--evidence={default}`（默认）」—— "
        "默认值改了但手册没跟上，一线会以为要显式加 failure-only，或者反过来以为默认是全留"
    )


# ---------------- 失败文本必须留下**根因**（不能只截头）----------------
#
# 2026-09-23 真机踩出来的：原来是 `text[:800]`。pytest 的 `longrepr` 是完整调用链，
# 顺序是「用例源码 → `_ _ _` 逐层往下 → **真正的异常类型与消息在最后**」。
# 截前 800 字符 = 留下了我们本来就有的用例源码、丢掉了唯一有用的异常行。
#
# 后果很具体：`apps-lifecycle` 的 `test_fixture_install_then_uninstall` 失败，
# 报告里那段文字停在 `> matched = install_fixture(` 就没了 ——
# 异常是什么、为什么，一个字都看不到，只能去节点翻 `reports\_elev\elevated_run.log`。


class _LongRepr:
    """只带 `longrepr` 的假 report（`_assertion_text` 只用这一个字段）。"""

    def __init__(self, text: str):
        self.longrepr = text


def test_assertion_text_keeps_the_exception_at_the_tail():
    """**核心回归锁**：超长时尾巴必须留 —— 根因就在尾巴上。"""
    conftest_mod = _load_conftest()
    traceback_text = (
        "用例自己的源码（几十行，本来就有）\n" * 60
        + "E   RuntimeError: 钉住安装包不存在：C:/.../fixtures/xxx.exe"
    )
    assert len(traceback_text) > conftest_mod.ASSERTION_HEAD + conftest_mod.ASSERTION_TAIL, (
        "这条用例要造一段**真的超长**的文本，否则走不到截断分支，等于没测"
    )
    out = conftest_mod._assertion_text(_LongRepr(traceback_text))

    assert "RuntimeError: 钉住安装包不存在" in out, (
        "截断把根因丢了 —— 报告里只剩用例源码，等于没有报错"
    )
    assert "省略" in out, "要显式标出中间被省略了多少，别让人以为原文就这么短"
    assert len(out) < len(traceback_text), "超长时要真的截，不能原样返回（报告会爆）"


def test_assertion_text_keeps_short_text_intact():
    """短文本原样返回 —— 别为了统一格式给正常失败加省略号。"""
    conftest_mod = _load_conftest()
    short = "assert False"
    assert conftest_mod._assertion_text(_LongRepr(short)) == short


def test_assertion_text_handles_empty_longrepr():
    conftest_mod = _load_conftest()
    assert conftest_mod._assertion_text(_LongRepr("")) == ""


# ---------------- --evidence=all 下 unit 不抓屏（2026-09-23）----------------
#
# `--evidence=all` 一开，**每一条**用例都抓一次屏。而 `tests/unit` 有 600+ 条
# **纯逻辑**用例，它们不碰 UI —— 抓下来的是**桌面**，一条都没有排查价值，
# 代价却是实测 `pytest tests/unit` 从 **16s → 149s**（633 次抓屏）。
# 单测的结论在 `summary.json` / `report.html` 里一条都不少，所以省掉的不是信息。


def test_unit_cases_do_not_take_screenshots():
    """**核心**：`unit` 用例不抓屏。这条红了**别改断言**，去看 `_wants_screenshot`。"""
    conftest_mod = _load_conftest()

    class _Item:
        def __init__(self, unit: bool):
            self._unit = unit

        def get_closest_marker(self, name):
            return types.SimpleNamespace(name=name) if (self._unit and name == "unit") else None

    assert conftest_mod._wants_screenshot(_Item(unit=True)) is False, (
        "unit 用例抓的是桌面，没价值还慢 10 倍"
    )
    assert conftest_mod._wants_screenshot(_Item(unit=False)) is True, (
        "UI 套件要抓 —— `--evidence=all` 的意图就是「通过用例也留现场图」"
    )


def test_capture_false_writes_no_evidence_dir(tmp_path, fake_capture):
    """`capture=False` **连目录都不建**（不只是不抓屏），但**结论仍要记进 summary**。

    守卫这条是为了别被"顺手改成只跳过抓屏" —— 那会留下 600+ 个**空目录**，
    回传时白拷一堆目录项。
    """
    s, run = _session(tmp_path, ev.MODE_ALL)
    rec = s.record(NODEID_BAD, ev.OUTCOME_FAILED, 1.0, "boom", capture=False)

    assert not _case_dir(run, NODEID_BAD).exists(), "capture=False 不该建证据目录"
    assert fake_capture == [], "capture=False 连抓屏都不该尝试"
    assert rec.outcome == ev.OUTCOME_FAILED, "结论仍要记进 summary（省的是图，不是信息）"
    assert s.records == [rec]

