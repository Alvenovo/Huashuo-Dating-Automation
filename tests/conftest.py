from __future__ import annotations

import os
import shutil

import pytest

from hall_auto.config import load_config
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


def _allow_install(config) -> bool:
    return bool(config.getoption("--allow-install")) or os.environ.get("HALL_ALLOW_INSTALL") == "1"


def pytest_collection_modifyitems(config, items):
    if _allow_install(config):
        return
    skip = pytest.mark.skip(reason="破坏性安装未开启：加 --allow-install 或 HALL_ALLOW_INSTALL=1")
    for item in items:
        if item.get_closest_marker("destructive"):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def cfg():
    return load_config()


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
