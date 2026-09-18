from __future__ import annotations

import os
import shutil

import pytest

from hall_auto.awake import keep_awake_for_process, session_lost, session_lost_detail
from hall_auto.config import load_config
from hall_auto.dpi import is_interactive_session, machine_profile, node_id
from hall_auto.evidence import (
    MODE_ALL,
    MODE_FAILURE,
    OUTCOME_FAILED,
    OUTCOME_PASSED,
    OUTCOME_SKIPPED,
    EvidenceSession,
    new_run_dir,
    prune_old_runs,
)


def pytest_addoption(parser):
    parser.addoption(
        "--allow-install",
        action="store_true",
        default=False,
        help="允许卸载/安装华硕大厅（P0-01/02）。也可设环境变量 HALL_ALLOW_INSTALL=1",
    )
    parser.addoption(
        "--evidence",
        choices=[MODE_ALL, MODE_FAILURE],
        default=MODE_ALL,
        help="证据采集策略：all=成功失败都留证（默认）；failure-only=只留失败",
    )
    parser.addoption(
        "--evidence-keep-days",
        type=int,
        default=30,
        help="证据保留天数，跑批时自动删除更早的 run 目录；0=不清理",
    )
    parser.addoption(
        "--skip-env-check",
        action="store_true",
        default=False,
        help="跳过跑批前的环境预检（交互式会话）。仅调试用，正式跑批不要加。",
    )
    # 多机分片：把用例按序号取模摊到多台机器上。不用 pytest-xdist —— 它只做单机多进程，
    # 跨不了机器，而这套用例有全局机器状态（装/卸、注册表、前台窗口），同机多进程也会互踩。
    parser.addoption(
        "--shard-id",
        type=int,
        default=1,
        help="本节点承担的分片序号（从 1 开始），配合 --shard-count 使用",
    )
    parser.addoption(
        "--shard-count",
        type=int,
        default=1,
        help="总的分片数；>1 时本节点只跑属于自己分片的用例",
    )


def pytest_report_header(config):
    """跑批前打印本机环境画像，多机汇总时能直接看出是哪台、什么环境跑的。"""
    profile = machine_profile()
    lines = [
        f"节点: {profile['node']}  系统: {profile['os']}  Python {profile['python']}({profile['python_bits']}位)",
        f"显示: {profile['screen']}  缩放: {profile['scale_percent']}%  显示器数: {profile['monitors']}  DPI感知: {profile['dpi_awareness']}",
    ]
    if not is_interactive_session():
        lines.append("会话检查: 阻断 — 非交互式会话（锁屏/息屏/RDP 断开），抓屏与点击会失效")
    return lines


def _allow_install(config) -> bool:
    return bool(config.getoption("--allow-install")) or os.environ.get("HALL_ALLOW_INSTALL") == "1"


def _shard_selection(config) -> tuple[int, int]:
    shard = max(1, config.getoption("--shard-id"))
    count = max(1, config.getoption("--shard-count"))
    return min(shard, count), count


def pytest_collection_modifyitems(config, items):
    # 分片：按「收集顺序序号」取模。收集顺序在同样的 pytest 版本 + 同样的路径下是稳定的，
    # 所以多台机器上用同一份参数能切出互不重叠、且合起来不重不漏的集合。
    shard, count = _shard_selection(config)
    if count > 1:
        kept, dropped = [], []
        for idx, item in enumerate(items):
            (kept if idx % count == shard - 1 else dropped).append(item)
        if dropped:
            items[:] = kept
        print(f"\n[分片] 本节点跑分片 {shard}/{count}：{len(kept)} 条用例（其余 {len(dropped)} 条归别的分片）")

    if _allow_install(config):
        return
    skip = pytest.mark.skip(reason="破坏性安装未开启：加 --allow-install 或 HALL_ALLOW_INSTALL=1")
    for item in items:
        if item.get_closest_marker("destructive"):
            item.add_marker(skip)


@pytest.fixture(scope="session", autouse=True)
def _env_precheck(request):
    """跑批前环境预检：非交互式会话直接中止；跑批期间保持屏幕常亮并巡检交互态。

    非交互式（锁屏 / 息屏 / RDP 断开）会让抓屏拿黑屏、点击无落点，跑下去全是假失败 ——
    直接中止比产出 20 份垃圾报告强。

    **屏幕常亮**：组长 2026-09-18 确认测试机无人碰屏，靠脚本自己保证不息屏。
    用 SetThreadExecutionState（进程级），不动系统电源计划。**常亮持续到进程退出**
    （不在 fixture teardown 时恢复：teardown 到进程退出之间那一段足够长套件中途息屏）。

    **中途丢失交互态也中止**：开跑时是交互态不代表跑完还是 —— RDP 断连 / 远程锁屏
    会让后半程全变假失败。巡检线程发现翻转就标记，每个用例结束时查一次，
    标记了就中止并写清原因（见 `_session_guard`）。

    **缩放不做门禁**：脚本走 Per-Monitor Aware 物理像素坐标（本机真实 150% 下全套跑通），
    非 100% 不影响正确性，只在报告页头记录环境画像供多机分组。
    """
    if request.config.getoption("--skip-env-check"):
        yield
        return
    if not is_interactive_session():
        pytest.exit(
            "非交互式会话（锁屏/息屏/RDP 断开），抓屏与点击会失效。"
            "请保持屏幕解锁常亮、在本地交互会话里跑；确认无碍可加 --skip-env-check 跳过预检。",
            returncode=3,
        )
    started = keep_awake_for_process()
    if not started:
        print("\n[电源] 屏幕常亮设置未生效（SetThreadExecutionState 调用失败），跑批期间屏幕可能息屏")
    yield
    # 常亮与巡检线程随进程退出由 atexit 收尾，这里不恢复（理由见 awake.py 模块头）
    detail = session_lost_detail()
    if detail:
        print(f"\n[会话] ⚠ {detail}")


@pytest.fixture(autouse=True)
def _session_guard(request):
    """跑批中途丢失交互态 → 立刻中止，别继续产出不可信的结果。

    只有开跑时的预检是**不够**的：长套件跑几十分钟，中途 RDP 断开 / 锁屏后
    抓屏变黑、点击无落点，后面所有用例都会以「元素找不到」失败 ——
    这类假失败会污染整份兼容性结论，比直接中止更糟。
    """
    if request.config.getoption("--skip-env-check"):
        yield
        return
    yield
    if session_lost():
        pytest.exit(
            f"跑批中途 {session_lost_detail()}",
            returncode=3,
        )


@pytest.fixture(scope="session")
def cfg():
    return load_config()


@pytest.fixture(scope="session")
def node() -> str:
    """本机节点标识，多机汇总时用于区分是哪台机器跑的。"""
    return node_id()


@pytest.fixture(scope="session")
def machine() -> dict:
    """本机环境画像（OS / 缩放 / 分辨率 / Python 位数）。断言里的兼容性差异可以引用它。"""
    return machine_profile()


@pytest.fixture(scope="session")
def allow_install(request) -> bool:
    return _allow_install(request.config)


def _assertion_text(report) -> str:
    text = str(report.longrepr or "")
    return text[:800]


# 门禁 marker → 受阻类别，只作前缀标签；具体原因与解锁条件用 skip 现场写的原文（作者写得比这里更精确）。
# 新增门禁 marker 时往这里加一行，漏加就只显示原文、不带类别前缀。
SKIP_GATE_CATEGORY = [
    ("manual", "需人工在环"),
    ("destructive", "环境受阻"),
    ("login", "环境受阻"),
    ("security", "环境受阻"),
    ("wb", "环境受阻"),
]


def _raw_skip_reason(report) -> str:
    lr = report.longrepr
    if isinstance(lr, tuple) and len(lr) == 3:
        text = str(lr[2])
    else:
        crash = getattr(lr, "reprcrash", None)
        text = str(crash.message if crash is not None else lr)
    return text.removeprefix("Skipped: ").strip()


def _skip_label(item, report) -> str:
    markers = {m.name for m in item.iter_markers()}
    raw = _raw_skip_reason(report)
    for name, category in SKIP_GATE_CATEGORY:
        if name in markers:
            return f"[{category}] {raw}"
    return raw


def _case_title(item) -> str:
    doc = getattr(item.function, "__doc__", None) or ""
    return doc.strip().splitlines()[0].strip() if doc.strip() else ""


def pytest_configure(config):
    config._evidence = EvidenceSession(new_run_dir(), config.getoption("--evidence"))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    session: EvidenceSession = item.config._evidence
    if report.when == "call":
        result = OUTCOME_FAILED if report.failed else (OUTCOME_SKIPPED if report.skipped else OUTCOME_PASSED)
        if report.failed:
            detail = _assertion_text(report)
        elif report.skipped:
            detail = _skip_label(item, report)
        else:
            detail = ""
        # 在 call 阶段结束、fixture 还没 teardown 时截图，才能拍到应用运行中的终态
        session.record(item.nodeid, result, report.duration, detail, title=_case_title(item))
    elif report.when == "setup" and report.skipped:
        session.record(item.nodeid, OUTCOME_SKIPPED, report.duration, _skip_label(item, report), title=_case_title(item))


def pytest_sessionfinish(session, exitstatus):
    ev: EvidenceSession = session.config._evidence
    if not ev.records:
        shutil.rmtree(ev.run_dir, ignore_errors=True)
        return
    report_path = ev.finish()
    removed = prune_old_runs(keep_days=session.config.getoption("--evidence-keep-days"))
    print(f"\n[证据] 汇总报告: {report_path}")
    for d in removed:
        print(f"[证据] 已清理过期 run: {d.name}")
