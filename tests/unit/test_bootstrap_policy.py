"""锁环境铺设的「先检测、缺什么才装什么」策略（tools/bootstrap_machine.py）。

组长 2026-09-18 要求：「不是直接装，应该先检测有没有对应环境，如果有的就不用装了，
没有的再装」。

这条为什么重要：20 台机器环境不一样，有的可能已经有 Python / 大厅 / 7-Zip。
如果每次都无脑重装，一是白等（pip 走网络要几十秒），
二是**有副作用**——计划任务被重建会重置触发时间，
正好在跑批时重建会把任务状态抹掉。

所以这里把三个判定钉死：
  1. 依赖齐 + requirements 没变  -> 跳过 pip
  2. requirements 变了            -> 重装
  3. 标记说装过但实测缺包          -> 重装（**不能只信标记**）
  4. 计划任务已存在且指向本仓库脚本 -> 跳过，不执行 Create
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_bootstrap():
    """按路径加载 tools/bootstrap_machine.py。

    tools/ 不是包（没有 __init__.py），不能直接 import，所以用 spec 加载。
    """
    path = REPO_ROOT / "tools" / "bootstrap_machine.py"
    spec = importlib.util.spec_from_file_location("bootstrap_machine_under_test", path)
    assert spec and spec.loader, f"加载失败：{path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bm():
    return _load_bootstrap()


def _fake_run(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------- 依赖检测 ----------------


def test_req_digest_changes_with_content(bm, tmp_path):
    """摘要必须随内容变化 —— 否则改了 requirements 也认不出来。"""
    req = tmp_path / "requirements.txt"
    req.write_text("pytest>=8\n", encoding="utf-8")
    first = bm._req_digest(req)
    req.write_text("pytest>=8\npywinauto>=0.6.8\n", encoding="utf-8")
    assert first != bm._req_digest(req)
    assert len(first) == 64


def test_req_digest_missing_file_is_empty_string(bm, tmp_path):
    """文件不存在不该抛错，返回空串（调用方据此跳过检测）。"""
    assert bm._req_digest(tmp_path / "nope.txt") == ""


def test_deps_probe_detects_missing_module(bm):
    """探测到缺包时必须返回 False —— 这是"不能只信标记"的基础。"""
    py = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    if not py.is_file():
        pytest.skip("本机没有 .venv")
    ok, why = bm._deps_probe(py)
    assert ok is True, f"本机依赖应齐全，实际：{why}"


def test_deps_probe_bad_interpreter_is_false_not_crash(bm, tmp_path):
    """解释器起不来时必须返回 False，不能抛异常把 bootstrap 带崩。"""
    bogus = tmp_path / "not-a-python.exe"
    bogus.write_text("not an exe", encoding="utf-8")
    ok, why = bm._deps_probe(bogus)
    assert ok is False
    assert why  # 有说明，不是空


# ---------------- venv / 依赖步骤 ----------------


def test_venv_skips_pip_when_marker_matches_and_deps_ok(bm):
    """标记一致 + 依赖实测齐全 -> 不装（核心：重跑不该碰网络）。

    只断言「没有 install 动作」，不断言「没有 pip 字样」——
    `_log_installed` 会跑 `pip list` 报版本，那不算安装。
    """
    py = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    req = REPO_ROOT / "requirements.txt"
    if not (py.is_file() and req.is_file()):
        pytest.skip("本机没有 .venv")

    with mock.patch.object(bm, "_deps_probe", return_value=(True, "关键依赖均可 import")), \
         mock.patch.object(bm, "_req_digest", return_value="a" * 64), \
         mock.patch.object(Path, "exists", return_value=True), \
         mock.patch.object(Path, "read_text", return_value="a" * 64), \
         mock.patch.object(bm.subprocess, "run", return_value=_fake_run()) as run:
        st = bm.step_venv(False)

    assert st.ok
    assert any("跳过安装" in line for line in st.detail)
    installs = [c for c in run.call_args_list if "install" in str(c)]
    assert not installs, f"不应执行 pip install，实际调用：{installs}"


def test_venv_reinstalls_when_requirements_changed(bm):
    """requirements 变了（标记哈希不匹配）-> 必须重装。"""
    py = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    if not py.is_file():
        pytest.skip("本机没有 .venv")

    with mock.patch.object(bm, "_deps_probe", return_value=(True, "ok")), \
         mock.patch.object(bm, "_req_digest", return_value="a" * 64), \
         mock.patch.object(Path, "exists", return_value=True), \
         mock.patch.object(Path, "read_text", return_value="b" * 64), \
         mock.patch.object(Path, "write_text"), \
         mock.patch.object(bm.subprocess, "run", return_value=_fake_run()) as run:
        st = bm.step_venv(False)

    assert st.ok
    assert any("有变动" in line for line in st.detail)
    assert any("pip" in str(c) or "install" in str(c) for c in run.call_args_list), "应执行 pip"


def test_venv_reinstalls_when_marker_lies(bm):
    """**核心防线**：标记说装过，但实测缺包 -> 重装。

    这条防的是「标记被残留、venv 被人为清理、pip 半途失败但标记已写」。
    只信标记文件的设计会在这里放行一个坏环境。
    """
    py = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    if not py.is_file():
        pytest.skip("本机没有 .venv")

    calls = {"n": 0}

    def probe(_py):
        calls["n"] += 1
        if calls["n"] == 1:
            return False, "缺 pywinauto"  # 第一次（跳过前校验）报缺
        return True, "关键依赖均可 import"

    with mock.patch.object(bm, "_deps_probe", side_effect=probe), \
         mock.patch.object(Path, "exists", return_value=True), \
         mock.patch.object(Path, "read_text", return_value="c" * 64), \
         mock.patch.object(Path, "write_text"), \
         mock.patch.object(bm, "_req_digest", return_value="c" * 64), \
         mock.patch.object(bm.subprocess, "run", return_value=_fake_run()):
        st = bm.step_venv(False)

    assert st.ok
    assert any("实测不齐" in line for line in st.detail), st.detail
    assert any("装依赖" in line for line in st.detail), "应触发重装"
    assert calls["n"] >= 2, "装完必须复核一次"


def test_venv_fails_but_does_not_mark_when_deps_still_broken(bm):
    """pip 跑完依赖仍不齐 -> 报错，且**不写标记**（避免下轮误判为就绪）。"""
    py = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    if not py.is_file():
        pytest.skip("本机没有 .venv")

    writes: list = []
    with mock.patch.object(bm, "_deps_probe", return_value=(False, "缺 pytest")), \
         mock.patch.object(bm, "_req_digest", return_value="d" * 64), \
         mock.patch.object(Path, "exists", return_value=False), \
         mock.patch.object(Path, "write_text", side_effect=lambda *a, **k: writes.append(a)), \
         mock.patch.object(bm.subprocess, "run", return_value=_fake_run()):
        st = bm.step_venv(False)

    assert st.ok is False
    assert any("仍不齐" in line for line in st.detail)
    assert not writes, f"依赖不齐时不该写标记，实际写了：{writes}"


# ---------------- 计划任务 ----------------


def test_schtask_skips_when_existing_points_to_this_repo(bm):
    """任务已存在且指向本仓库脚本 -> 跳过，且**不执行 Create**。

    不重建的理由：覆盖会重置触发时间。跑批途中重建会把任务状态抹掉。
    """
    ps = "power" + "shell"
    xml = f"<Command>{ps}</Command><Arguments>-File {bm.REPO_ROOT}/run_p1_apps.ps1</Arguments>"
    with mock.patch.object(bm, "_task_action", return_value=xml), \
         mock.patch.object(bm, "is_admin", return_value=True), \
         mock.patch.object(bm.subprocess, "run", return_value=_fake_run()) as run:
        st = bm.step_schtask(False)

    assert st.ok
    assert any("跳过" in line for line in st.detail)
    assert not any("Create" in str(c) for c in run.call_args_list), "不应执行 Create"


def test_schtask_recreates_when_missing(bm):
    """任务不存在 -> 建。"""
    with mock.patch.object(bm, "_task_action", return_value=""), \
         mock.patch.object(bm, "is_admin", return_value=True), \
         mock.patch.object(bm.subprocess, "run", return_value=_fake_run()) as run:
        st = bm.step_schtask(False)

    assert st.ok
    assert any("Create" in str(c) for c in run.call_args_list)


def test_schtask_recreates_when_pointing_elsewhere(bm):
    """任务存在但指向别的仓库 -> 覆盖重建（否则跑的是别人的脚本）。"""
    xml = "<Command>x</Command><Arguments>-File D:/other/repo/run_p1_apps.ps1</Arguments>"
    with mock.patch.object(bm, "_task_action", return_value=xml), \
         mock.patch.object(bm, "is_admin", return_value=True), \
         mock.patch.object(bm.subprocess, "run", return_value=_fake_run()) as run:
        st = bm.step_schtask(False)

    assert st.ok
    assert any("别的脚本" in line for line in st.detail)
    assert any("Create" in str(c) for c in run.call_args_list)


def test_schtask_needs_admin(bm):
    """非管理员要明确报错，别静默失败让人以为建好了。"""
    with mock.patch.object(bm, "is_admin", return_value=False):
        st = bm.step_schtask(False)
    assert st.ok is False
    assert any("管理员" in line for line in st.detail)


def test_task_action_returns_empty_when_query_fails(bm):
    """查询失败（任务不存在 / 权限不足）返回空串，不抛异常。"""
    with mock.patch.object(bm.subprocess, "run", return_value=_fake_run(returncode=1, stderr="找不到")):
        assert bm._task_action("NoSuchTask") == ""


# ---------------- 7-Zip 自动安装 ----------------
#
# 这条策略的由来：原实现只在环境自检里"检测"7-Zip，缺了就让测试人员自己去官网下载。
# 但钉住包本来就在 installer_dir/fixtures/7z2602-x64.exe（reset_fixture.py 早就用它静默装了），
# 包在手边还让人跑一趟官网没有道理。改成缺了自动装。
#
# 三条边界必须钉死：
#   1. 已装 -> 一个安装动作都不许有（幂等，不能每台机器白装一遍）
#   2. 非管理员 -> 不调安装器（会弹 UAC 把脚本卡死），且**不算失败**
#   3. 装不上 -> **不算失败**（只影响 P2 套件，不能把整台机器的铺设判死）


def _installer_that_creates(target: Path):
    """假的静默安装器：被调用时"装上"7z.exe，模拟真实安装效果。"""

    def _run(argv, **kwargs):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake 7z.exe")
        return _fake_run()

    return _run


def test_seven_zip_skips_install_when_already_present(bm, tmp_path):
    """已装 -> 跳过，**不执行任何安装动作**。"""
    exe = tmp_path / "7z.exe"
    exe.write_bytes(b"already here")
    with mock.patch.object(bm, "SEVEN_ZIP_EXE", exe), \
         mock.patch.object(bm.subprocess, "run") as run:
        st = bm.step_seven_zip(tmp_path, allow_download=True)

    assert st.ok
    assert any("已就位" in line for line in st.detail)
    assert not run.called, "已装就不该再调安装器"


def test_seven_zip_installs_from_local_fixture(bm, tmp_path):
    """未装 + 本地有钉住包 -> 静默安装，并确认 7z.exe 出现。"""
    exe = tmp_path / "prog" / "7z.exe"          # 目标位置（一开始不存在）
    fixture = tmp_path / "fixtures" / "7z2602-x64.exe"
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(b"x")
    real_sha = bm.SEVEN_ZIP_FIXTURE_SHA256
    with mock.patch.object(bm, "SEVEN_ZIP_EXE", exe), \
         mock.patch.object(bm, "SEVEN_ZIP_FIXTURE_SHA256", bm.sha256_of(fixture)), \
         mock.patch.object(bm, "is_admin", return_value=True), \
         mock.patch.object(bm.subprocess, "run", side_effect=_installer_that_creates(exe)) as run:
        st = bm.step_seven_zip(tmp_path, allow_download=True)

    assert st.ok
    assert exe.is_file(), "装完 7z.exe 应存在"
    assert run.called
    argv = run.call_args_list[0][0][0]
    assert argv[0].endswith("7z2602-x64.exe") and argv[1] == "/S", f"应走静默安装：{argv}"
    assert bm.SEVEN_ZIP_FIXTURE_SHA256 == real_sha  # 别把常量改了忘了还原


def test_seven_zip_needs_admin_and_does_not_fail(bm, tmp_path):
    """非管理员 -> 不调安装器（否则弹 UAC 卡死），且**不算失败**。"""
    exe = tmp_path / "prog" / "7z.exe"
    fixture = tmp_path / "fixtures" / "7z2602-x64.exe"
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(b"x")
    with mock.patch.object(bm, "SEVEN_ZIP_EXE", exe), \
         mock.patch.object(bm, "is_admin", return_value=False), \
         mock.patch.object(bm.subprocess, "run") as run:
        st = bm.step_seven_zip(tmp_path, allow_download=True)

    assert st.ok is True, "只影响 P2，不该把整台机器判死"
    assert not run.called, "非管理员不能调安装器"
    assert any("管理员" in line for line in st.detail)


def test_seven_zip_no_download_skips_missing_fixture(bm, tmp_path):
    """钉住包不在本地 + --no-download -> 不下载、不失败。"""
    exe = tmp_path / "prog" / "7z.exe"
    with mock.patch.object(bm, "SEVEN_ZIP_EXE", exe), \
         mock.patch.object(bm, "is_admin", return_value=True), \
         mock.patch.object(bm.subprocess, "run") as run:
        st = bm.step_seven_zip(tmp_path, allow_download=False)

    assert st.ok is True
    assert not run.called
    assert any("--no-download" in line for line in st.detail)


def test_seven_zip_downloads_fixture_when_missing(bm, tmp_path):
    """本地没有 + 允许下载 -> 从官方源取钉住包再装。"""
    exe = tmp_path / "prog" / "7z.exe"
    fixture = tmp_path / "fixtures" / "7z2602-x64.exe"

    def _fake_download(url, dest, timeout_sec, retries):
        assert url == bm.SEVEN_ZIP_URL
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"x")

    with mock.patch.object(bm, "SEVEN_ZIP_EXE", exe), \
         mock.patch.object(bm, "SEVEN_ZIP_FIXTURE_SHA256", "deadbeef"), \
         mock.patch.object(bm, "is_admin", return_value=True), \
         mock.patch("hall_auto.fetch._download", side_effect=_fake_download) as dl, \
         mock.patch.object(bm.subprocess, "run", side_effect=_installer_that_creates(exe)):
        st = bm.step_seven_zip(tmp_path, allow_download=True)

    assert dl.called, "本地没有就该去官方源取"
    assert fixture.is_file()
    assert any("官方源" in line for line in st.detail)


def test_seven_zip_rejects_fixture_with_wrong_sha(bm, tmp_path):
    """钉住包被换过（sha256 不符）-> 不用它安装，且不失败。"""
    exe = tmp_path / "prog" / "7z.exe"
    fixture = tmp_path / "fixtures" / "7z2602-x64.exe"
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(b"tampered")
    with mock.patch.object(bm, "SEVEN_ZIP_EXE", exe), \
         mock.patch.object(bm, "is_admin", return_value=True), \
         mock.patch.object(bm.subprocess, "run") as run:
        st = bm.step_seven_zip(tmp_path, allow_download=False)

    assert st.ok is True
    assert not run.called, "摘要不符的包不能拿去装"
    assert any("sha256 不符" in line for line in st.detail)


def test_seven_zip_timeout_does_not_fail(bm, tmp_path):
    """安装器跑了但 7z.exe 一直没出现 -> 只提示，不算失败。"""
    exe = tmp_path / "prog" / "7z.exe"
    fixture = tmp_path / "fixtures" / "7z2602-x64.exe"
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(b"x")
    # 第一次 time() 算 deadline，之后直接越过，让等待循环只走一轮。
    with mock.patch.object(bm, "SEVEN_ZIP_EXE", exe), \
         mock.patch.object(bm, "SEVEN_ZIP_FIXTURE_SHA256", bm.sha256_of(fixture)), \
         mock.patch.object(bm, "is_admin", return_value=True), \
         mock.patch.object(bm.subprocess, "run", return_value=_fake_run()), \
         mock.patch.object(bm.time, "time", side_effect=[0, 10_000]), \
         mock.patch.object(bm.time, "sleep"):
        st = bm.step_seven_zip(tmp_path, allow_download=False)

    assert st.ok is True, "装不上只影响 P2，不该把整台机器判死"
    assert any("未见" in line for line in st.detail)


def test_seven_zip_skip_flag(bm, tmp_path):
    """--skip-seven-zip 直接跳过，不检测也不装。"""
    with mock.patch.object(bm.subprocess, "run") as run:
        st = bm.step_seven_zip(tmp_path, allow_download=True, skip=True)
    assert st.ok
    assert not run.called
    assert any("--skip-seven-zip" in line for line in st.detail)
