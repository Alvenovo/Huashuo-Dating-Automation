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

import ast
import importlib.util
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

pytestmark = pytest.mark.unit


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

    with _no_share_wheelhouse(bm), \
         mock.patch.object(bm, "_deps_probe", return_value=(True, "ok")), \
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

    with _no_share_wheelhouse(bm), \
         mock.patch.object(bm, "_deps_probe", side_effect=probe), \
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
    with _no_share_wheelhouse(bm), \
         mock.patch.object(bm, "_deps_probe", return_value=(False, "缺 pytest")), \
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
    xml = (
        f"<Command>{ps}</Command>"
        f"<Arguments>-File {bm.REPO_ROOT}/tools/run_elevated_suite.ps1</Arguments>"
    )
    with mock.patch.object(bm, "_task_action", return_value=xml), \
         mock.patch.object(bm, "is_admin", return_value=True), \
         mock.patch.object(bm.subprocess, "run", return_value=_fake_run()) as run:
        st = bm.step_schtask(False)

    assert st.ok
    assert any("跳过" in line for line in st.detail)
    assert not any("Create" in str(c) for c in run.call_args_list), "不应执行 Create"


def test_schtask_migrates_from_the_old_p1_apps_action(bm):
    """**关键保护**：指向旧动作 `run_p1_apps.ps1` 的任务必须重建。

    这是 2026-09-23 改造留下的唯一「例外重建」场景：老的 HallAutoP1 只会跑夹具装卸，
    新代码触发它时它还是去跑老脚本 —— 表现是「提权段一直没结果」，而节点侧看着只是慢。
    不重建的话，新加进全量档的大厅装卸（`install`）永远跑不到，且不会报错。
    """
    xml = (
        f"<Command>powershell</Command>"
        f"<Arguments>-File {bm.REPO_ROOT}/run_p1_apps.ps1</Arguments>"
    )
    with mock.patch.object(bm, "_task_action", return_value=xml), \
         mock.patch.object(bm, "is_admin", return_value=True), \
         mock.patch.object(bm.subprocess, "run", return_value=_fake_run()) as run:
        st = bm.step_schtask(False)

    assert st.ok
    assert any("旧动作" in line for line in st.detail), "要写明为什么破例重建"
    created = [str(c) for c in run.call_args_list if "Create" in str(c)]
    assert created, "指向旧动作时必须重建"
    assert "run_elevated_suite.ps1" in created[0], "重建后要指向新的提权执行器"


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


@pytest.fixture(autouse=True)
def _no_real_download():
    """单元测试一律不许真联网。

    踩过的坑：`step_seven_zip` 现在**无条件**确保钉住包在手，
    于是没造夹具文件的用例会真去 `https://www.7-zip.org/` 下载 ——
    单测变成联网测试，在没外网的机器上直接卡住（实测挂满 2 分钟超时）。
    这里把 `_download` 默认打成"一调就炸"，需要下载路径的用例自己 patch 掉它。
    """
    with mock.patch("hall_auto.fetch._download", side_effect=RuntimeError("单元测试不应真的下载")):
        yield


@pytest.fixture(autouse=True)
def _no_real_share():
    """单元测试同样不许真去探测共享盘。

    与 `_no_real_download` 同一个理由，但后果更隐蔽：钉住包的取用链现在是
    「本地 → 缓存 → 共享盘 → 下载」，而本机 `config.yaml` 配的是**真实共享盘地址**。
    不挡住的话：
      - 每个相关用例都真去连那台机器（探测失败要等 5s 超时，实测把单测拖到 3 分钟以上）；
      - 在共享盘可用的机器上，本该"本地没有 → 去下载"的用例会真从共享盘拷回一个包，
        于是它**再也不走下载分支**，断言集体失配（假红）；
      - 反之在共享盘不可达的机器上又是另一套结果 —— 同一份代码在两种机器上跑出不同结论。

    需要共享盘路径的用例自己 `mock.patch("hall_auto.fetch.share_dirs", ...)`，
    显式 patch 会覆盖这里的默认值。
    """
    with mock.patch("hall_auto.fetch.share_dirs", return_value=[]):
        yield


@pytest.fixture(autouse=True)
def _no_real_pip_probe():
    """单元测试也不许真去连 pip 源。

    `step_venv` 在装依赖前会探一次 pip 源（好让没外网的新机几秒内失败）。
    不挡住的话，单测的结论会取决于**跑测试这台机器有没有外网** ——
    有外网时"应触发重装"的用例正常，没外网时它们会卡在 `_pip_net_ok` 的失败分支上假红。

    挡在 `socket.create_connection` 这一层，而不是把 `bm._pip_net_ok` 整个换成 Mock ——
    后者会让**直接测 `_pip_net_ok` 自己的用例**测到 Mock 上去（实测踩过）。
    需要特定结果的用例自己 patch 内层，`mock.patch` 后进先出，会覆盖这里。
    """
    with mock.patch("socket.create_connection"):
        yield


def _no_share_wheelhouse(bm):
    """单测里不真去找共享盘上的 wheelhouse。

    `step_venv` 现在会去共享盘找**离线 wheelhouse**（无外网机器装依赖靠它），
    而这一步在第 2 步、比第 7 步「共享盘连接」还早，所以 `_resolve_wheelhouse`
    自己会先 `net use` 一次。不挡住的话：结论取决于跑测试这台机器连不连得上那个
    共享盘，而且网络盘 `is_dir()` 断网时卡 20s+。

    **为什么不做成 autouse**：`step_share_login` 的用例要的就是真实的
    `_share_connect`（它们 mock 的是更内层的 `hall_auto.fetch.share_reachable`
    和 `subprocess.run`）。autouse 把它一起挡掉，那几个用例就再也测不到东西了
    —— 实测踩过。所以这里显式声明，哪个用例要真连自己不加就行。
    """
    return mock.patch.object(bm, "_wheelhouse_share_dirs", return_value=[])


def _place_fixture(bm, tmp_path: Path) -> Path:
    """造一个"已经在本地、且摘要对得上"的钉住包，并让常量认它。

    返回夹具路径。用它就不用碰网络，也不会被 sha256 校验拦下。
    """
    fixture = tmp_path / "fixtures" / "7z2602-x64.exe"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_bytes(b"x")
    return fixture


def test_seven_zip_skips_install_when_already_present(bm, tmp_path):
    """已装 -> 跳过安装，**不执行任何安装动作**（夹具仍会被确保在手）。"""
    exe = tmp_path / "7z.exe"
    exe.write_bytes(b"already here")
    fixture = _place_fixture(bm, tmp_path)
    with mock.patch.object(bm, "SEVEN_ZIP_EXE", exe), \
         mock.patch.object(bm, "SEVEN_ZIP_FIXTURE_SHA256", bm.sha256_of(fixture)), \
         mock.patch.object(bm.subprocess, "run") as run:
        st = bm.step_seven_zip(tmp_path, allow_download=True)

    assert st.ok
    assert any("已就位" in line for line in st.detail)
    assert not run.called, "已装就不该再调安装器"


def test_seven_zip_fixture_fetched_even_when_zip_present(bm, tmp_path):
    """**已装 7-Zip 也要确保夹具包在手** —— 它是 P1-A 更新用例的起点包。

    这是真 bug 的回归锁：早先只在"7-Zip 没装"时才去取夹具包，
    于是已经装了 7-Zip 的机器永远拿不到它，
    `test_update_fixture_via_hall` 因"钉住安装包不存在"直接失败。
    """
    exe = tmp_path / "7z.exe"
    exe.write_bytes(b"already here")

    def _fake_download(url, dest, timeout_sec, retries):
        assert url == bm.SEVEN_ZIP_URL
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"fetched")

    with mock.patch.object(bm, "SEVEN_ZIP_EXE", exe), \
         mock.patch.object(bm, "SEVEN_ZIP_FIXTURE_SHA256", "x"), \
         mock.patch("hall_auto.fetch._download", side_effect=_fake_download) as dl, \
         mock.patch.object(bm.subprocess, "run") as run:
        st = bm.step_seven_zip(tmp_path, allow_download=True)

    assert st.ok
    assert dl.called, "7-Zip 已装也必须去取夹具包"
    assert (tmp_path / "fixtures" / "7z2602-x64.exe").is_file()
    assert not run.called, "已装就不该调安装器"


def test_seven_zip_installs_from_local_fixture(bm, tmp_path):
    """未装 + 本地有钉住包 -> 静默安装，并确认 7z.exe 出现。"""
    exe = tmp_path / "prog" / "7z.exe"          # 目标位置（一开始不存在）
    fixture = _place_fixture(bm, tmp_path)
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
    fixture = _place_fixture(bm, tmp_path)
    with mock.patch.object(bm, "SEVEN_ZIP_EXE", exe), \
         mock.patch.object(bm, "SEVEN_ZIP_FIXTURE_SHA256", bm.sha256_of(fixture)), \
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
    """本地没有 + 允许下载 -> 从官方源取钉住包，校验通过后拿它装。"""
    exe = tmp_path / "prog" / "7z.exe"
    fixture = tmp_path / "fixtures" / "7z2602-x64.exe"
    payload = b"downloaded-7z"
    digest = hashlib.sha256(payload).hexdigest()

    def _fake_download(url, dest, timeout_sec, retries):
        assert url == bm.SEVEN_ZIP_URL
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(payload)

    with mock.patch.object(bm, "SEVEN_ZIP_EXE", exe), \
         mock.patch.object(bm, "SEVEN_ZIP_FIXTURE_SHA256", digest), \
         mock.patch.object(bm, "is_admin", return_value=True), \
         mock.patch("hall_auto.fetch._download", side_effect=_fake_download) as dl, \
         mock.patch.object(bm.subprocess, "run", side_effect=_installer_that_creates(exe)) as run:
        st = bm.step_seven_zip(tmp_path, allow_download=True)

    assert dl.called, "本地没有就该去官方源取"
    assert fixture.is_file()
    assert any("已取到钉住包" in line for line in st.detail)
    assert run.called, "取到包后应拿去装"
    assert exe.is_file()


def test_seven_zip_downloaded_fixture_with_bad_sha_is_rejected(bm, tmp_path):
    """下回来的包摘要不符 -> 不用它装（防半截包/被替换的包）。"""
    exe = tmp_path / "prog" / "7z.exe"

    def _fake_download(url, dest, timeout_sec, retries):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"tampered")

    with mock.patch.object(bm, "SEVEN_ZIP_EXE", exe), \
         mock.patch.object(bm, "SEVEN_ZIP_FIXTURE_SHA256", "0" * 64), \
         mock.patch.object(bm, "is_admin", return_value=True), \
         mock.patch("hall_auto.fetch._download", side_effect=_fake_download), \
         mock.patch.object(bm.subprocess, "run") as run:
        st = bm.step_seven_zip(tmp_path, allow_download=True)

    assert st.ok is True
    assert not run.called, "摘要不符的包不能拿去装"
    assert any("sha256 不符" in line for line in st.detail)


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


# ---------------- 共享盘连接：农场共享也要连 ----------------
#
# 早先只连 package_share.dirs（安装包共享），农场共享 HALL_FARM_ROOT 靠
# "同一台服务器已经认证过了"这个**隐含前提**兜着。
# 隐含前提会在换机器 / 换服务器 / 加 --skip-share 时突然不成立，
# 表现是节点报"任务取不到"，而控制机那边看任务文件明明躺在 tasks/ 里。
# 所以农场 UNC 要显式连一次。


def test_unc_target_trims_to_host_and_share(bm):
    """UNC 裁成 net use 要的前两段；本地路径返回空串。"""
    assert bm._unc_target("//192.168.0.4/hall-farm") == "\\\\192.168.0.4\\hall-farm"
    assert bm._unc_target("\\\\192.168.0.4\\hall-farm\\tasks") == "\\\\192.168.0.4\\hall-farm"
    assert bm._unc_target("C:/hall-farm") == ""
    assert bm._unc_target("relative/path") == ""


def _patch_share_env(monkeypatch, farm="//192.168.0.4/hall-farm"):
    """造出"配了包共享 + 设了农场根 + 给了凭据"的最小环境。"""
    monkeypatch.setenv("HALL_SHARE_USER", "hallshare")
    monkeypatch.setenv("HALL_SHARE_PASSWORD", "pw")
    if farm:
        monkeypatch.setenv("HALL_FARM_ROOT", farm)
    else:
        monkeypatch.delenv("HALL_FARM_ROOT", raising=False)


def test_share_login_connects_farm_root_too(bm, monkeypatch):
    """HALL_FARM_ROOT 是 UNC -> 也要 net use 一次（不能只连包共享）。"""
    _patch_share_env(monkeypatch)
    fake_cfg = mock.Mock()
    probed: list[str] = []

    def _reachable(directory, timeout_sec=5):
        probed.append(str(directory))
        return False, "目录不存在或不是目录"   # 一开始都不可达 -> 触发 net use

    with mock.patch("hall_auto.config.load_config", return_value=fake_cfg), \
         mock.patch("hall_auto.fetch.share_dirs", return_value=[Path("//192.168.0.4/hall-packages")]), \
         mock.patch("hall_auto.fetch.share_reachable", side_effect=_reachable), \
         mock.patch.object(bm.subprocess, "run", return_value=_fake_run()) as run:
        st = bm.step_share_login(False)

    assert st.ok
    targets = [c[0][0][2] for c in run.call_args_list]
    assert "\\\\192.168.0.4\\hall-packages" in targets
    assert "\\\\192.168.0.4\\hall-farm" in targets, "农场共享必须显式连，不能靠隐含前提"


def test_share_login_skips_farm_when_not_unc(bm, monkeypatch):
    """HALL_FARM_ROOT 指向本地目录（单机 --local 场景）-> 不去 net use 它。"""
    _patch_share_env(monkeypatch, farm="C:/hall-farm")

    def _reachable(directory, timeout_sec=5):
        return False, "目录不存在或不是目录"

    with mock.patch("hall_auto.config.load_config", return_value=mock.Mock()), \
         mock.patch("hall_auto.fetch.share_dirs", return_value=[Path("//192.168.0.4/hall-packages")]), \
         mock.patch("hall_auto.fetch.share_reachable", side_effect=_reachable), \
         mock.patch.object(bm.subprocess, "run", return_value=_fake_run()) as run:
        st = bm.step_share_login(False)

    assert st.ok
    targets = [c[0][0][2] for c in run.call_args_list]
    assert "\\\\192.168.0.4\\hall-packages" in targets
    assert not any("hall-farm" in t for t in targets), "本地路径不该被当 UNC 连"


def test_share_login_no_creds_only_reports(bm, monkeypatch):
    """没给凭据 -> 只报告不可达，不调 net use、不失败。"""
    monkeypatch.delenv("HALL_SHARE_USER", raising=False)
    monkeypatch.delenv("HALL_SHARE_PASSWORD", raising=False)
    monkeypatch.setenv("HALL_FARM_ROOT", "//192.168.0.4/hall-farm")

    with mock.patch("hall_auto.config.load_config", return_value=mock.Mock()), \
         mock.patch("hall_auto.fetch.share_dirs", return_value=[Path("//192.168.0.4/hall-packages")]), \
         mock.patch("hall_auto.fetch.share_reachable", return_value=(False, "超时")), \
         mock.patch.object(bm.subprocess, "run") as run:
        st = bm.step_share_login(False)

    assert st.ok, "共享盘连不上不算铺设失败"
    assert not run.called, "没凭据就别去 net use"
    assert any("无法自动连接" in line for line in st.detail)


def test_share_login_skip_flag(bm):
    """--skip-share 一个网络动作都不做。"""
    with mock.patch.object(bm.subprocess, "run") as run:
        st = bm.step_share_login(True)
    assert st.ok
    assert not run.called
    assert any("--skip-share" in line for line in st.detail)


# ---------------- 2026-09-22 真机踩出的三个坑 ----------------
#
# 节点重跑 bootstrap 连续报 53 -> 1219 -> 1909，最后**整个脚本 traceback 退出**：
# 后面 4 步（安装包准备 / 提权计划任务 / unit 自检 / 屏幕常亮）一步没跑，
# **报告 JSON 也没写**。三个错里两个是环境（代理 TUN / 账号被锁），
# 一个是脚本自己的毛病 —— 下面钉的是"脚本不该再犯"的那部分。
#
# 关键事实（本机实测，别凭印象）：`pathlib` 的 `is_dir()` / `is_file()` **不是一律吞错**。
# 它只吞 ENOENT / ENOTDIR / EBADF / ELOOP（含映射成 ENOENT 的 WinError 53 / 67），
# **1909 账户锁定 / 1326 登录失败 / 5 拒绝访问 是 EACCES 类，会原样重抛**。
# 所以"UNC 上的存在性判断"必须 catch `OSError`，只包 `mkdir` 是不够的。


def _unc_is_dir_raises(err: OSError, monkeypatch) -> None:
    """让 **UNC 路径**的 `is_dir()` 抛指定异常（本地路径照常工作）。"""
    real = Path.is_dir

    def fake(self, *args, **kwargs):
        if str(self).replace("/", "\\").startswith("\\\\"):
            raise err
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_dir", fake)


def _winerror(code: int, text: str) -> OSError:
    err = OSError(13, f"[WinError {code}] {text}")
    err.winerror = code
    return err


def test_farm_root_survives_locked_account(bm, monkeypatch):
    """`is_dir()` 抛 1909（账户锁定）-> 干净 `[FAIL]`，**不许把整个脚本崩掉**。

    这是 2026-09-22 真机那次的直接原因：`step_farm_root` 只包了 `mkdir`，
    1909 从 `is_dir()` 冒泡出去 -> traceback 退出 -> 后面 4 步和报告全丢。
    """
    monkeypatch.setenv("HALL_FARM_ROOT", "//192.168.0.4/hall-farm")
    _unc_is_dir_raises(_winerror(1909, "引用的帐户当前已锁定"), monkeypatch)

    st = bm.step_farm_root(False)  # 不许抛

    assert st.ok is False, "建不出目录要判失败"
    assert bm._step_label(st) == "FAIL"
    assert any("1909" in line for line in st.detail)
    assert any("10 分钟" in line for line in st.detail), "要给出下一步（等锁定窗口过去）"


def test_farm_root_hints_proxy_for_winerror_53(bm, monkeypatch):
    """53（找不到网络路径）的提示要指向代理/TUN —— 真机那次的根因就是它。"""
    monkeypatch.setenv("HALL_FARM_ROOT", "//192.168.0.4/hall-farm")
    _unc_is_dir_raises(_winerror(53, "找不到网络路径"), monkeypatch)

    st = bm.step_farm_root(False)

    assert st.ok is False
    assert any("代理" in line for line in st.detail)


def test_share_connect_deletes_conflicting_session_then_retries(bm, monkeypatch):
    """`net use` 报 1219 -> 先按**同一 target** `/delete`，再重试一次。

    1219 的因果是脚本自己造的：本函数前面的探针用**当前 Windows 身份**直访 UNC，
    建了一条隐式会话；`net use` 只列显式映射、看不见它，
    于是换 `hallshare` 去连必被拒 —— "列表是空的"和"报 1219"可以同时成立。
    """
    _patch_share_env(monkeypatch, farm="")
    cmds: list[list[str]] = []
    state = {"use": 0, "deleted": False}

    def _reach(directory, timeout_sec=5):
        # 删掉冲突会话之前探不到，删完重连就通了 —— 和真机一致
        return (True, "") if state["deleted"] else (False, "不可达")

    def _run(cmd, **kwargs):
        cmds.append(list(cmd))
        if "/delete" in cmd:
            state["deleted"] = True
            return _fake_run(0)
        state["use"] += 1
        return _fake_run(2, stderr="发生系统错误 1219。") if state["use"] == 1 else _fake_run(0)

    log: list[str] = []
    with mock.patch("hall_auto.fetch.share_reachable", side_effect=_reach), \
         mock.patch.object(bm.subprocess, "run", side_effect=_run):
        left = bm._share_connect([Path("//192.168.0.4/hall-packages")], log.append)

    assert left == [], "删掉冲突会话后重试成功 -> 不算没连上"
    assert len(cmds) == 3, f"应该是 use -> delete -> use，实际：{cmds}"
    assert "/delete" in cmds[1] and cmds[1][2] == cmds[0][2], "删的必须是同一个 target"
    assert any("1219" in line for line in log)


def test_share_connect_explains_1219_when_still_failing(bm, monkeypatch):
    """删完还报 1219 -> 必须给出**下一步**，不是只报一句 `rc=2`。"""
    _patch_share_env(monkeypatch, farm="")
    log: list[str] = []
    with mock.patch("hall_auto.fetch.share_reachable", return_value=(False, "不可达")), \
         mock.patch.object(bm.subprocess, "run", return_value=_fake_run(2, stderr="发生系统错误 1219。")):
        left = bm._share_connect([Path("//192.168.0.4/hall-packages")], log.append)

    assert left, "没连上要如实返回给调用方"
    assert any("LanmanWorkstation" in line for line in log)


def test_share_login_warns_instead_of_fake_ok(bm, monkeypatch):
    """配了共享盘却连不上 -> `[WARN]`，**不是 `[OK]`**。

    真机那次两条共享都连接失败，第 7 步照样打 `[OK]` ——
    「不阻塞」是设计，「没问题」是结论，别用 `[OK]` 表示前者。
    """
    _patch_share_env(monkeypatch)
    with mock.patch("hall_auto.config.load_config", return_value=mock.Mock()), \
         mock.patch("hall_auto.fetch.share_dirs", return_value=[Path("//192.168.0.4/hall-packages")]), \
         mock.patch("hall_auto.fetch.share_reachable", return_value=(False, "超时")), \
         mock.patch.object(bm.subprocess, "run", return_value=_fake_run(2, stderr="发生系统错误 53。")):
        st = bm.step_share_login(False)

    assert st.ok is True, "共享盘连不上不算铺设失败（这台机器可能有本地包）"
    assert st.warned is True, "但必须让人看见 —— 否则一线以为共享盘没问题"
    assert bm._step_label(st) == "WARN"
    assert any(line.startswith("?? ") for line in st.detail)


def test_step_label_fail_beats_warn(bm):
    """一步既报警又失败时，`[FAIL]` 不能被 `[WARN]` 盖掉。"""
    st = bm.Step("x")
    st.warn("警告")
    st.fail("失败")
    assert bm._step_label(st) == "FAIL"


def test_report_lists_warned_steps(bm, monkeypatch, tmp_path):
    """告警要进报告 JSON，且**不改退出码** —— 光在正文里一闪而过不够。"""
    monkeypatch.setattr(bm, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(bm, "is_admin", lambda: True)
    monkeypatch.setattr(bm, "machine_profile", lambda: {"os": "test"})

    st = bm.Step("共享盘连接")
    st.warn("两条都没连上")
    code = bm._write_report("NODE", tmp_path, [st])

    assert code == 0, "告警不该把退出码弄成 1"
    out = next((tmp_path / "reports" / "bootstrap").glob("*.json"))
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["warned"] == ["共享盘连接"]
    assert payload["steps"][0]["warned"] is True


def test_wheelhouse_probe_error_does_not_crash(bm, monkeypatch, tmp_path):
    """共享盘 wheelhouse 的 `is_dir()` 抛异常 -> 记一句、返回「没有」，不许崩。

    第 2 步跑在"还没装依赖"的时刻，这里崩掉等于整台机器铺不下去。
    """
    monkeypatch.delenv("HALL_PACKAGE_SHARE", raising=False)
    monkeypatch.setattr(bm, "REPO_ROOT", tmp_path)
    _unc_is_dir_raises(_winerror(1909, "引用的帐户当前已锁定"), monkeypatch)

    with mock.patch.object(bm, "_wheelhouse_share_dirs", return_value=[Path("//192.168.0.4/hall-packages")]), \
         mock.patch.object(bm, "_share_connect", return_value=[]), \
         mock.patch.object(bm, "_dir_reachable", return_value=True):
        wh, why = bm._resolve_wheelhouse(None, None)  # 不许抛

    assert wh is None
    assert "1909" in why


# ---------------- 内部安全工具（P2）：从共享盘自动备 ----------------
#
# 此前 `step_write_local_config` 把 `security.tools_dir` 指向
# `<installer_dir>/fixtures/security_tools`，但**从来没人往里放东西** ——
# 内部工具按红线不进公开仓库，只能人肉拷，于是每台新机器的 `security` 套件必然 skip。
# 共享盘不是 git 仓库，放它合规：包源机放一次，所有节点自动取。


def _security_tools_on_share(bm, share: Path, complete: bool = True) -> None:
    """在共享盘上摆一份内部安全工具（`fixtures/security_tools/`）。"""
    tools = share / bm.SECURITY_TOOLS_REL_DIR
    tools.mkdir(parents=True, exist_ok=True)
    for name in bm.SECURITY_TOOLS_REQUIRED:
        (tools / name).write_bytes(b"fake-tool")
    if complete:
        (tools / "SignAppsV.exe").write_bytes(b"extra")


def _patch_security_tools(bm, share: Path):
    """把「共享盘指向 tmp」和「load_config 返回假 cfg」一起打上。"""
    return (
        mock.patch("hall_auto.config.load_config", return_value=mock.Mock()),
        mock.patch("hall_auto.fetch.share_dirs", return_value=[share]),
        mock.patch("hall_auto.fetch.share_reachable", return_value=(True, "")),
    )


def test_security_tools_pulls_from_share(bm, tmp_path):
    """共享盘上有 -> 整目录拷到 `<installer_dir>/fixtures/security_tools`。"""
    share = tmp_path / "share"
    _security_tools_on_share(bm, share)
    installer = tmp_path / "installer"
    installer.mkdir()

    p1, p2, p3 = _patch_security_tools(bm, share)
    with p1, p2, p3:
        st = bm.step_security_tools(installer)

    dest = installer / "fixtures" / "security_tools"
    assert st.ok, st.detail
    assert (dest / "CheckAppV.exe").is_file()
    assert (dest / "SignCheck_v2.ps1").is_file()
    assert any("已从共享盘备好" in line for line in st.detail)


def test_security_tools_skips_when_already_present(bm, tmp_path):
    """本机已有完整工具 -> 一个字节都不拷（幂等，重跑 bootstrap 不该有副作用）。"""
    share = tmp_path / "share"
    _security_tools_on_share(bm, share)
    installer = tmp_path / "installer"
    dest = installer / "fixtures" / "security_tools"
    dest.mkdir(parents=True)
    for name in bm.SECURITY_TOOLS_REQUIRED:
        (dest / name).write_bytes(b"already-here")

    with mock.patch("hall_auto.fetch.copy_tree_from_share") as copy:
        st = bm.step_security_tools(installer)

    assert st.ok
    assert not copy.called, "已就位就不该再走拷贝路径"
    assert any("已就位" in line for line in st.detail)


def test_security_tools_absent_on_share_is_log_not_fail(bm, tmp_path):
    """共享盘上没有这份工具 -> **只提示不判失败**（只影响 P2，不该让整台机器铺设判死）。"""
    share = tmp_path / "share"
    share.mkdir()
    installer = tmp_path / "installer"
    installer.mkdir()

    p1, p2, p3 = _patch_security_tools(bm, share)
    with p1, p2, p3:
        st = bm.step_security_tools(installer)

    assert st.ok, "安全工具缺失不能算铺设失败"
    assert any("仅 P2 受影响" in line for line in st.detail)
    assert any("补救" in line for line in st.detail), "要告诉人怎么补"


def test_security_tools_partial_copy_is_log_not_fail(bm, tmp_path):
    """拷到了文件但缺关键文件 -> 同样只提示。半份工具比没有更危险（会误判成已配好）。"""
    share = tmp_path / "share"
    _security_tools_on_share(bm, share)
    # 只留一个非关键文件，关键文件删掉
    tools = share / bm.SECURITY_TOOLS_REL_DIR
    for name in bm.SECURITY_TOOLS_REQUIRED:
        (tools / name).unlink()
    (tools / "SignAppsV.exe").write_bytes(b"only-this")

    installer = tmp_path / "installer"
    installer.mkdir()

    p1, p2, p3 = _patch_security_tools(bm, share)
    with p1, p2, p3:
        st = bm.step_security_tools(installer)

    assert st.ok
    assert any("缺关键文件" in line for line in st.detail)


def test_security_tools_skip_flag(bm, tmp_path):
    """--skip-security-tools 直接跳过，不碰网络。"""
    with mock.patch("hall_auto.fetch.copy_tree_from_share") as copy:
        st = bm.step_security_tools(tmp_path, skip=True)
    assert st.ok
    assert not copy.called
    assert any("--skip-security-tools" in line for line in st.detail)


def test_security_tools_rel_dir_matches_local_layout(bm):
    """共享盘上的相对目录必须与 `step_write_local_config` 写进配置的落地路径同源。

    这两处一旦不一致，bootstrap 会把工具拷到一个 `security.tools_dir` 不认的地方，
    表现是"拷成功了但 P2 还是 skip"，比不拷更难查。
    """
    assert bm.SECURITY_TOOLS_REL_DIR == "fixtures/security_tools"
    assert bm.SECURITY_TOOLS_REQUIRED == ("CheckAppV.exe", "SignCheck_v2.ps1")


# ---------------- farm_root 落盘（免去每次开窗口重设环境变量）----------------
#
# 节点机不设 `HALL_FARM_ROOT` 时 farm_agent 直接退出、一个任务都取不到。
# 以前每个新窗口都得重设一次，忘了就是"节点在跑但没任务"。
# 现在 bootstrap 把它写进 config.local.yaml，farm_agent / farm_control 自动读。


def test_write_local_config_persists_farm_root(bm, tmp_path, monkeypatch):
    """设了 HALL_FARM_ROOT -> 要落进 config.local.yaml 的 farm_root。"""
    monkeypatch.setenv("HALL_FARM_ROOT", "//LAPTOP-VS5F7HF4/hall-farm")
    with mock.patch.object(bm, "REPO_ROOT", tmp_path):
        st = bm.step_write_local_config(tmp_path / "installer", "R01")

    assert st.ok, st.detail
    text = (tmp_path / "config.local.yaml").read_text(encoding="utf-8")
    assert "farm_root" in text
    assert "LAPTOP-VS5F7HF4/hall-farm" in text
    assert any("farm_root 已落盘" in line for line in st.detail)


def test_write_local_config_keeps_farm_root_when_env_absent(bm, tmp_path, monkeypatch):
    """**幂等保护**：本次没设环境变量时，不能把上一轮写进去的 farm_root 擦掉。"""
    monkeypatch.delenv("HALL_FARM_ROOT", raising=False)
    (tmp_path / "config.local.yaml").write_text(
        "farm_root: //LAPTOP-VS5F7HF4/hall-farm\n", encoding="utf-8"
    )
    with mock.patch.object(bm, "REPO_ROOT", tmp_path):
        st = bm.step_write_local_config(tmp_path / "installer", "R01")

    assert st.ok, st.detail
    text = (tmp_path / "config.local.yaml").read_text(encoding="utf-8")
    assert "//LAPTOP-VS5F7HF4/hall-farm" in text, "重跑 bootstrap 不该擦掉已落盘的农场目录"
    # 日志不许反过来说「文件里也没写 farm_root」—— 文件里明明有，这句是假话，
    # 会让人以为配置被擦了而白跑一趟。没设环境变量 ≠ 文件里没有，两条分支要分开报。
    assert any("沿用已有 farm_root" in line for line in st.detail), st.detail
    assert not any("也没写 farm_root" in line for line in st.detail), st.detail


# ---------------- 钉住包的取用链：本地 -> 缓存 -> 共享盘 -> 下载 ----------------
#
# 这条链是给**没有外网的测试机**用的：共享盘上摆一份钉住包，所有节点走局域网取。
# 早先只有「本地直路径 + 官网下载」两条路，于是离线机器上：
#   ① 7-Zip 装不上（手册却写着"7-Zip 你完全不用管"）；
#   ② 第 10 步把包取到 `_cache/fixtures/` 后，`reset_fixture.py` 找的是
#      `installer_dir/fixtures/` → 报「钉住安装包不存在」→ P1-A 更新用例起点建不起来。
# 所以取到后**必须落到规范位置**，这也是下面第一条用例锁的重点。


def _fixture_cfg(installer: Path):
    """给 `_fixture_config` 用的最小配置：只需要 installer_dir（决定缓存落点）。"""
    return mock.Mock(installer_dir=installer, raw={})


def test_seven_zip_fixture_comes_from_share_when_offline(bm, tmp_path):
    """**离线机器的关键路径**：本地没有 + 禁下载 + 共享盘有 -> 仍要拿到，并落到规范位置。"""
    share = tmp_path / "share"
    (share / "fixtures").mkdir(parents=True)
    payload = b"from-share"
    (share / "fixtures" / "7z2602-x64.exe").write_bytes(payload)
    installer = tmp_path / "installer"
    installer.mkdir()
    digest = hashlib.sha256(payload).hexdigest()

    with mock.patch.object(bm, "_fixture_config", return_value=_fixture_cfg(installer)), \
         mock.patch("hall_auto.fetch.share_dirs", return_value=[share]), \
         mock.patch("hall_auto.fetch.share_reachable", return_value=(True, "")), \
         mock.patch("hall_auto.fetch._expected_sha", return_value=""), \
         mock.patch.object(bm, "SEVEN_ZIP_FIXTURE_SHA256", digest):
        st = bm.Step("probe")
        got = bm._ensure_seven_zip_fixture(installer, allow_download=False, st=st)

    assert got is not None, st.detail
    assert any("共享盘" in line for line in st.detail), st.detail
    assert got == installer / "fixtures" / "7z2602-x64.exe", "必须落到 reset_fixture 找的那个位置"
    assert got.is_file()


def test_seven_zip_fixture_comes_from_cache_when_offline(bm, tmp_path):
    """本地直路径没有、但缓存里有（上一轮取过）-> 直接用它，不联网。"""
    installer = tmp_path / "installer"
    cache = installer / "_cache" / "fixtures"
    cache.mkdir(parents=True)
    payload = b"cached"
    (cache / "7z2602-x64.exe").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()

    with mock.patch.object(bm, "_fixture_config", return_value=_fixture_cfg(installer)), \
         mock.patch.object(bm, "SEVEN_ZIP_FIXTURE_SHA256", digest):
        st = bm.Step("probe")
        got = bm._ensure_seven_zip_fixture(installer, allow_download=False, st=st)

    assert got == installer / "fixtures" / "7z2602-x64.exe"
    assert got.is_file()
    assert any("缓存" in line for line in st.detail), st.detail


def test_fixture_config_pins_installer_dir(bm, tmp_path):
    """`_fixture_config` 必须把 installer_dir 钉成本次铺设的目录。

    `step_seven_zip` 在第 4 步跑，而写 `config.local.yaml` 是第 5 步 ——
    新机器此刻读到的还是仓库模板里的占位路径（`C:/Users/ASUS/...`）。
    不钉住的话，缓存查找和落点都会跑到别人的目录上。
    """
    cfg = bm._fixture_config(tmp_path)
    assert cfg.installer_dir == tmp_path


# ---------------- 第 12 步自检必须跑全量 ----------------


def test_selftest_runs_whole_unit_suite(bm):
    """自检**不能**按 marker 收窄。

    早先的命令是 `pytest tests/unit -m unit`，而 `tests/unit` 下 260 条里只有 127 条
    带 `unit` 标记 —— 漏掉的正好是 fetch / farm_agent / env_pack / reset / bootstrap
    这些**多机链路**模块。手册写的是"全绿说明环境 OK"，只跑一半属自检名不副实。
    """
    with mock.patch.object(bm.subprocess, "run", return_value=_fake_run()) as run:
        bm.step_selftest(False)

    argv = run.call_args_list[0][0][0]
    assert "tests/unit" in argv
    # `-m pytest` 那个 -m 是"跑模块"，是必须的；要禁的是**第二个** -m（marker 过滤）
    assert argv.count("-m") == 1, f"不该再按 marker 过滤：{argv}"
    assert argv[argv.index("-m") + 1] == "pytest"


# ---------------- 手册与代码的一致性（防漂移）----------------
#
# 这轮踩的坑就是"文档说 A、代码做 B"，而且两轮复核都只核对了名字对不对、
# 没核对新机器上跑不跑得通。把手册那张 13 步表和 main() 的调用顺序一起钉住。


_CODE_CALL_ORDER = [
    "env_check", "venv", "fixtures", "seven_zip", "write_local_config", "node_env_template",
    "share_login", "security_tools", "farm_root", "fetch_packages", "schtask", "selftest",
    "keep_awake",
]

_MANUAL_STEP_KEYWORDS = {
    1: "环境自检", 2: "虚拟环境", 3: "夹具", 4: "7-Zip", 5: "config.local.yaml",
    6: "farm_node.env", 7: "共享盘", 8: "安全工具", 9: "农场目录",
    10: "安装包", 11: "计划任务", 12: "单元测试", 13: "常亮",
}


def test_bootstrap_main_call_order_is_locked(bm):
    """`main()` 里 13 个步骤的调用顺序是手册那张表的依据，钉住它。"""
    import re

    src = (REPO_ROOT / "tools" / "bootstrap_machine.py").read_text(encoding="utf-8")
    main_src = src[src.index("def main()"):]
    calls = re.findall(r"run\(step_(\w+)\(", main_src)
    assert calls == _CODE_CALL_ORDER, f"步骤顺序变了，手册那张表要同步改：{calls}"


def test_manual_bootstrap_step_table_matches_code(bm):
    """《测试机操作手册》「脚本会依次做这 13 件事」那张表要与代码对得上。

    测试人员就是照着这张表判断"第几步红了该找谁"。表里写 13 步、代码跑 11 步，
    或者顺序错位，一线会按错误的编号去查故障（上一轮真发生过：故障表写第 10 步、
    实际是第 11 步）。
    """
    import re

    manual = REPO_ROOT / "项目知识库" / "测试机操作手册.md"
    if not manual.is_file():
        pytest.skip("手册不在本仓库（发布给测试同事的包里可能只带手册）")

    text = manual.read_text(encoding="utf-8")
    # 只取「脚本会依次做这 12 件事」那一节里的表 —— 「全流程一览」那张表在文档更前面，
    # 也是 `| 1 | ...` 开头，不切出区块会把两张表混在一起数。
    assert "脚本会依次做这 13 件事" in text, "手册里那张 13 步表的标题变了，本用例要跟着改"
    block = text.split("脚本会依次做这 13 件事", 1)[1].split("**预期结果**", 1)[0]
    rows = [(int(n), body) for n, body in re.findall(r"^\| (\d+) \| ([^|]*?) \|", block, re.M)
            if int(n) <= 13]
    assert len(rows) == 13, f"手册 13 步表取到 {len(rows)} 行：{rows}"
    wrong = [n for n, body in rows if _MANUAL_STEP_KEYWORDS[n] not in body]
    assert not wrong, f"这些行与代码对不上：{wrong}（行内容：{[b for n, b in rows if n in wrong]}）"
    assert len(_CODE_CALL_ORDER) == 13



# ---------------- 新机预检：磁盘空间 + pip 源可达性 ----------------
#
# 起因（2026-09-21，用户问「新测试机上可能很多前置条件都没有，需要先检查」）：
# 12 步里每一步都做到了「先检测、缺了才装」，但有**两样新机常缺的东西没人检查**：
#   1. 磁盘空间 —— 包缓存 ~800MB + 大厅本体 + 证据截图。盘满时会在拷到一半失败，
#      报错长得像「共享盘坏了」，一线会往完全错的方向查。
#   2. pip 源可达性 —— 第 2 步要联网装依赖，而手册只在**故障表**里事后提了一句。
#      没外网时 pip 自己重试好几轮（几分钟）才抛一段截断的 stderr。
# 下面把这两条钉死。


def test_free_gb_reports_existing_path(bm, tmp_path):
    """正常路径：返回剩余 GB（正数）。"""
    free, why = bm._free_gb(tmp_path)
    assert free > 0, f"应报出正数剩余空间，实际 {free}（{why}）"
    assert why == ""


def test_free_gb_walks_up_for_missing_path(bm, tmp_path):
    """installer_dir 还不存在时（新机首次铺设就是这种），向上找到存在的祖先，别报错。"""
    missing = tmp_path / "还没有" / "也不会有" / "华硕大厅"
    assert not missing.exists()
    free, why = bm._free_gb(missing)
    assert free > 0, f"路径不存在不该导致检测失败，实际 {free}（{why}）"


def test_free_gb_bad_path_is_negative_not_crash(bm, monkeypatch):
    """拿不到空间时返回负数 + 原因，调用方据此只提示不判失败。"""
    def boom(_path):
        raise OSError("盘符不存在")

    monkeypatch.setattr(bm.shutil, "disk_usage", boom)
    free, why = bm._free_gb(REPO_ROOT)
    assert free < 0
    assert "盘符不存在" in why


def _fake_env_check(bm, installer_dir, free_gb: float, config_node_id: str = ""):
    """跑一遍 step_env_check，把外部依赖全打成假的，只看磁盘那几行。"""
    with mock.patch.object(bm, "machine_profile", return_value={
        "os": "Windows 11", "python": "3.12.10", "python_bits": 64,
        "screen": "2560x1600", "scale_percent": 150, "monitors": 1,
        "dpi_awareness": "per-monitor",
    }), \
         mock.patch.object(bm, "is_interactive_session", return_value=True), \
         mock.patch.object(bm, "is_admin", return_value=True), \
         mock.patch.object(bm, "SEVEN_ZIP_EXE", REPO_ROOT / "不存在.exe"), \
         mock.patch.object(bm, "_free_gb", return_value=(free_gb, "")), \
         mock.patch.object(bm.subprocess, "run", return_value=_fake_run()):
        return bm.step_env_check("TESTNODE", installer_dir, config_node_id)


def test_env_check_warns_when_disk_is_tight(bm, tmp_path):
    """盘不够时要**明说**，并且点出"看着像共享盘坏了"这个误判方向。"""
    st = _fake_env_check(bm, tmp_path, free_gb=1.2)
    joined = "\n".join(st.detail)
    assert "低于建议的" in joined, joined
    assert "看着像共享盘坏了" in joined, joined


def test_env_check_reports_ok_when_disk_is_plenty(bm, tmp_path):
    st = _fake_env_check(bm, tmp_path, free_gb=50.0)
    assert any("够用" in line for line in st.detail), st.detail


def test_env_check_disk_failure_is_log_not_fail(bm, tmp_path):
    """拿不到磁盘信息只提示，不该把整台机器的铺设判死。"""
    with mock.patch.object(bm, "machine_profile", return_value={
        "os": "Windows 11", "python": "3.12.10", "python_bits": 64,
        "screen": "2560x1600", "scale_percent": 150, "monitors": 1,
        "dpi_awareness": "per-monitor",
    }), \
         mock.patch.object(bm, "is_interactive_session", return_value=True), \
         mock.patch.object(bm, "is_admin", return_value=True), \
         mock.patch.object(bm, "SEVEN_ZIP_EXE", REPO_ROOT / "不存在.exe"), \
         mock.patch.object(bm, "_free_gb", return_value=(-1.0, "OSError: 盘符不存在")), \
         mock.patch.object(bm.subprocess, "run", return_value=_fake_run()):
        st = bm.step_env_check("TESTNODE", tmp_path)
    assert any("磁盘空间检测跳过" in line for line in st.detail), st.detail


# ---------------- 「整包拷贝」带过来的老机器痕迹 ----------------
#
# 起因（2026-09-21）：用户问「git 可以不下吗」。可以 —— 但换的那条路（把整个目录拷到
# 新机器）会带上被 gitignore 的 `config.local.yaml`，而 `main()` 取 installer_dir 的
# 优先级是 `--installer-dir` > **已有 config** > 默认值。于是新机器继承了老机器的
# `C:/Users/<老用户名>/...`，取包必然失败，**而报错长得像"共享盘坏了"**。
# 只报警不改值：静默把 800MB 的落点换掉，比让它失败更吓人。


def test_foreign_profile_owner_detects_other_user(bm):
    """`C:/Users/<别人>/...` 要能认出来 —— 这是整包拷贝最典型的痕迹。"""
    assert bm._foreign_profile_owner(Path("C:/Users/asus/Desktop/Test/华硕大厅")) == "asus"


def test_foreign_profile_owner_ignores_public_and_current_user(bm):
    """`Public`（脚本自己的默认值）和当前用户自己的目录都不算"外来的"。"""
    assert bm._foreign_profile_owner(Path("C:/Users/Public/Desktop/Test/华硕大厅")) == ""
    assert bm._foreign_profile_owner(Path.home() / "Desktop" / "Test" / "华硕大厅") == ""


def test_foreign_profile_owner_ignores_non_profile_paths(bm):
    """别的盘、别的位置认不出来就别瞎报 —— 只认一眼能看出"不是本机"的形态。"""
    assert bm._foreign_profile_owner(Path("D:/Test/华硕大厅")) == ""
    assert bm._foreign_profile_owner(Path("C:/ProgramData/Test/华硕大厅")) == ""


def test_env_check_warns_on_foreign_installer_dir(bm):
    """核心：发现老机器痕迹要**说人话**，并给出两条明确处理办法。"""
    st = _fake_env_check(bm, Path("C:/Users/asus/Desktop/Test/华硕大厅"), free_gb=50.0)
    joined = "\n".join(st.detail)
    assert "installer_dir 指向别的用户目录（asus）" in joined, joined
    assert "-InstallerDir" in joined, joined
    assert "删掉 config.local.yaml" in joined, joined


def test_env_check_silent_on_normal_installer_dir(bm, tmp_path):
    """正常路径（当前用户 / Public）不该冒出这条警告 —— 狼来了就没人看了。"""
    st = _fake_env_check(bm, Path("C:/Users/Public/Desktop/Test/华硕大厅"), free_gb=50.0)
    assert not any("别的用户目录" in line for line in st.detail), st.detail


def test_stale_node_hint_flags_mismatch(bm):
    """config 里的 node_id 与本机节点名不一致时要点出来。"""
    hint = bm._stale_node_hint("LAPTOP-OLD", "R01")
    assert "LAPTOP-OLD" in hint and "R01" in hint, hint


def test_stale_node_hint_silent_when_matching_or_absent(bm):
    """一致、或压根没写，都不该提示。"""
    assert bm._stale_node_hint("R01", "R01") == ""
    assert bm._stale_node_hint("", "R01") == ""
    assert bm._stale_node_hint(None, "R01") == ""


def test_env_check_logs_stale_node_hint(bm, tmp_path):
    """这条提示要真的从 step_env_check 里出来，不能只是函数写好了没人调。"""
    st = _fake_env_check(bm, tmp_path, free_gb=50.0, config_node_id="LAPTOP-OLD")
    assert any("LAPTOP-OLD" in line for line in st.detail), st.detail


def test_pip_probe_target_prefers_index_url_over_proxy(bm, monkeypatch):
    """同时设了 `PIP_INDEX_URL` 和代理时，探的是 **pip 源**（pip 最终会去那儿）。"""
    monkeypatch.setenv("PIP_INDEX_URL", "http://mirror.corp.local:8080/simple")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp.local:3128")
    host, port, what = bm._pip_probe_target()
    assert (host, port) == ("mirror.corp.local", 8080), (host, port)
    assert "pip 源" in what


def test_pip_probe_target_uses_proxy_when_no_index_url(bm, monkeypatch):
    """**关键**：只配了代理（公司内网常见的出网方式）时，探代理而不是 pypi.org。

    裸 socket 不走代理，不认代理就会给出**假阴性** —— 把本来能装的机器判成装不了。
    """
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp.local:3128")
    host, port, what = bm._pip_probe_target()
    assert (host, port) == ("proxy.corp.local", 3128), (host, port)
    assert "代理" in what


def test_pip_probe_target_defaults_to_pypi(bm, monkeypatch):
    for key in ("PIP_INDEX_URL", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY",
                "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    host, port, what = bm._pip_probe_target()
    assert (host, port) == ("pypi.org", 443), (host, port)


def test_pip_net_ok_honours_pip_index_url(bm, monkeypatch):
    """有内网镜像时探的是镜像主机，不是 pypi.org。"""
    monkeypatch.setenv("PIP_INDEX_URL", "http://mirror.corp.local:8080/simple")
    with mock.patch("socket.create_connection") as conn:
        ok, why = bm._pip_net_ok(timeout_sec=1)
    assert ok is True
    assert conn.call_args[0][0] == ("mirror.corp.local", 8080), conn.call_args
    assert "mirror.corp.local:8080" in why


def test_pip_net_ok_false_when_unreachable(bm, monkeypatch):
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    with mock.patch("socket.create_connection", side_effect=OSError("连不上")):
        ok, why = bm._pip_net_ok(timeout_sec=1)
    assert ok is False
    assert "pypi.org:443" in why
    assert "连不上" in why


def test_venv_warns_when_pip_source_unreachable_but_still_tries(bm):
    """**回归锁**：探不到 pip 源要**立刻说出来**（附两条绕法），但**不拦路**。

    为什么不拦：探测有偏差（代理 / DNS / 防火墙策略），硬拦会把本来能装的机器判死。
    装不装得上最终由 pip 说了算 —— 所以必须看到它**仍然去跑了 pip install**。
    """
    with _no_share_wheelhouse(bm), \
         mock.patch.object(bm, "_deps_probe", return_value=(True, "关键依赖均可 import")), \
         mock.patch.object(bm, "_req_digest", return_value="a" * 64), \
         mock.patch.object(bm, "_pip_net_ok", return_value=(False, "pypi.org:443 连不上")), \
         mock.patch.object(Path, "is_file", return_value=False), \
         mock.patch.object(Path, "write_text"), \
         mock.patch.object(bm.subprocess, "run", return_value=_fake_run()) as run:
        st = bm.step_venv(False)

    joined = "\n".join(st.detail)
    assert "探不到 pip 源" in joined, joined
    assert "wheelhouse" in joined, "要给出「离线 wheelhouse」这条不碰网络的绕法"
    assert "index-url" in joined, "要给出内网镜像这条绕法"
    installs = [c for c in run.call_args_list if "install" in str(c)]
    assert installs, "探测失败不该拦路，仍要尝试 pip install"


def test_venv_offline_hint_on_pip_failure(bm):
    """pip 真失败了，报错里必须带两条绕法（这是操作员唯一能看到的地方）。"""
    def fake_run(argv, **kwargs):
        # 建 venv 要成功，装依赖才失败 —— 否则会停在"建 venv 失败"那一步，测不到 pip 分支
        if "install" in str(argv):
            return _fake_run(returncode=1, stderr="Could not find a version")
        return _fake_run()

    with _no_share_wheelhouse(bm), \
         mock.patch.object(bm, "_deps_probe", return_value=(True, "ok")), \
         mock.patch.object(bm, "_req_digest", return_value="a" * 64), \
         mock.patch.object(bm, "_pip_net_ok", return_value=(True, "可达")), \
         mock.patch.object(Path, "is_file", return_value=False), \
         mock.patch.object(bm.subprocess, "run", side_effect=fake_run):
        st = bm.step_venv(False)

    assert st.ok is False
    joined = "\n".join(st.detail)
    assert "pip install 失败" in joined, joined
    assert "wheelhouse" in joined and "index-url" in joined, joined
    assert "fetch_wheelhouse.py" in joined, "要告诉操作员离线包是哪个脚本产出的"


def test_offline_hint_does_not_recommend_copying_the_venv(bm):
    """**回归锁**：绕法里不许再推荐「整份拷 .venv + --skip-venv」。

    这条以前是推荐做法，2026-09-21 核出它是脆的：`pyvenv.cfg` 的 `home` 和
    `Scripts/*.exe` 里都写死了原机器的绝对路径，用户名或 Python 安装位置一变就起不来。
    留着它会让一线反复踩，所以现在只允许它作为**被劝退的**反面出现。
    """
    hint = bm.PIP_OFFLINE_HINT
    assert "wheelhouse" in hint and "index-url" in hint
    assert "别再用" in hint and ".venv" in hint, "要明确劝退「整份拷 .venv」，不能只是不提"



def test_venv_does_not_probe_pip_when_deps_already_ok(bm):
    """**核心防线**：已铺好的机器重跑 bootstrap **不该碰网络** —— 连探测都不该探。

    这是脚本自己写在文档里的承诺（「重跑本脚本不应该产生任何多余的安装动作，也不该碰网络」）。
    """
    py = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    req = REPO_ROOT / "requirements.txt"
    if not (py.is_file() and req.is_file()):
        pytest.skip("本机没有 .venv")

    probe = mock.Mock(return_value=(True, "不该被调用"))
    with mock.patch.object(bm, "_deps_probe", return_value=(True, "关键依赖均可 import")), \
         mock.patch.object(bm, "_req_digest", return_value="a" * 64), \
         mock.patch.object(bm, "_pip_net_ok", probe), \
         mock.patch.object(Path, "exists", return_value=True), \
         mock.patch.object(Path, "read_text", return_value="a" * 64), \
         mock.patch.object(bm.subprocess, "run", return_value=_fake_run()):
        st = bm.step_venv(False)

    assert st.ok
    assert any("跳过安装" in line for line in st.detail), st.detail
    probe.assert_not_called()


# ---------------- 无外网：离线 wheelhouse（2026-09-21） ----------------
#
# 起因（用户：「但是新测试机没有外网环境啊」）：
# 手册原来给无网机器的那条路是「整份拷 .venv + --skip-venv」。核了一下是**脆**的：
#   - `.venv/pyvenv.cfg` 的 `home` 指向原机器的 Python 安装路径；
#   - `.venv/Scripts/pip.exe` / `pytest.exe` 等启动器里写死了原 venv 的绝对路径。
# 用户名或 Python 安装位置一变就起不来，而报错看不出来是路径问题。
#
# 改成：在**有网**的机器上跑 `tools/fetch_wheelhouse.py` 产出一份 wheel 目录，
# 搬到无网机器上，bootstrap 用 `pip install --no-index --find-links <dir>` 离线装。
# wheel 与 Python 版本绑死（cp312 装不进 3.13），所以清单里记了版本，主动比对。
#
# 下面把这几条钉死。


def test_bootstrap_module_imports_without_pyyaml():
    """**回归锁 · 自举链条的第一环**：系统 Python 上没有 PyYAML 时，脚本必须仍能起来。

    2026-09-21 实测到的真问题：`hall_auto/__init__.py` 曾经
    `from hall_auto.config import load_config`，而 `config.py` 顶层 `import yaml`
    —— 于是 `import hall_auto.dpi` 这种跟 YAML 毫无关系的导入也要求装 PyYAML。
    结果全新机器上 `python tools/bootstrap_machine.py` 在**打印第一行之前**就
    `ModuleNotFoundError: No module named 'yaml'` 崩掉，而"装依赖"正是它的第 2 步。

    报错指向"环境没铺好"，实际是"铺环境的脚本自己起不来" —— 又一处报错指错方向。
    修法是 `hall_auto/__init__.py` 改惰性导出（PEP 562）。这条用例防它被改回去。
    """
    repo = str(REPO_ROOT)
    code = (
        "import sys; sys.modules['yaml'] = None\n"  # 之后任何 import yaml 都会 ImportError
        f"sys.path.insert(0, r'{repo}')\n"
        "import hall_auto.dpi, hall_auto.wheelhouse, hall_auto.env_pack\n"
        "import importlib.util, pathlib\n"
        f"p = pathlib.Path(r'{repo}') / 'tools' / 'bootstrap_machine.py'\n"
        "spec = importlib.util.spec_from_file_location('bm_probe', p)\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "print('OK', mod._venv_python().name)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"没有 PyYAML 时脚本起不来：\n{proc.stderr}"
    assert "OK" in proc.stdout, proc.stdout


def test_hall_auto_package_exports_stay_usable_after_lazy_rewrite():
    """惰性导出不能把 `from hall_auto import load_config` 这类用法弄坏。"""
    import hall_auto

    assert "load_config" in dir(hall_auto)
    assert "expected_version_from_setup_name" in dir(hall_auto)
    assert callable(hall_auto.load_config)
    assert callable(hall_auto.expected_version_from_setup_name)
    with pytest.raises(AttributeError):
        hall_auto.不存在的名字  # noqa: B018


def test_py_xy_reports_version_and_empty_on_broken(bm, tmp_path):
    """`_py_xy` 既回答"能不能跑"，也回答"是哪一代 Python"（决定 wheel 能不能用）。"""
    assert bm._py_xy(Path(sys.executable)) == f"{sys.version_info[0]}.{sys.version_info[1]}"
    assert bm._py_xy(tmp_path / "没有这个.exe") == ""
    assert bm._venv_python_ok(tmp_path / "没有这个.exe") is False


def test_running_under_detects_current_interpreter(bm):
    """换解释器重跑靠这个判定，判错了要么不换（假绿）、要么无限递归。"""
    assert bm._running_under(Path(sys.executable)) is True
    assert bm._running_under(Path(sys.executable).parent / "没有这个.exe") is False
    assert bm.REEXEC_ENV, "重入标记不能为空，否则防不住无限递归"


def test_dir_reachable_is_false_for_missing(bm, tmp_path):
    """极简探测版（自举第一遍用）：存在的目录 True，不存在的 False，且不抛异常。"""
    assert bm._dir_reachable(tmp_path) is True
    assert bm._dir_reachable(tmp_path / "没有") is False


def test_resolve_wheelhouse_prefers_explicit_arg(bm, tmp_path):
    """`--wheelhouse` 是人明确指的路，最优先。"""
    wh = tmp_path / "wh"
    wh.mkdir()
    got, why = bm._resolve_wheelhouse(wh, None)
    assert got == wh and why == ""


def test_resolve_wheelhouse_missing_explicit_dir_says_which_flag(bm, tmp_path):
    """指错了要报**是哪个开关**指错的，不能只说"没找到"。"""
    got, why = bm._resolve_wheelhouse(tmp_path / "没有", None)
    assert got is None
    assert "--wheelhouse" in why, why


def test_resolve_wheelhouse_finds_repo_local_dir_before_installer_dir(bm, tmp_path, monkeypatch):
    """「整包拷贝」路线（手册 1C）会把 wheelhouse 连同仓库一起带过来 —— 要优先用它。

    仓库根是**不需要读配置**就知道的，所以无网机器的第一遍也走得通；
    `installer_dir` 则可能来自读不了的 `config.local.yaml`。
    """
    repo = tmp_path / "repo"
    (repo / "wheelhouse").mkdir(parents=True)
    inst = tmp_path / "inst"
    (inst / "wheelhouse").mkdir(parents=True)
    monkeypatch.setattr(bm, "REPO_ROOT", repo)

    got, why = bm._resolve_wheelhouse(None, inst)
    assert got == repo / "wheelhouse", f"{got} / {why}"


def test_wheelhouse_share_dirs_puts_env_var_first(bm, monkeypatch):
    """`HALL_PACKAGE_SHARE` 必须排在配置里的共享盘之前，且**不读配置**。

    它是自举第一遍（系统 Python、没装依赖、import 不了 `hall_auto.config`）
    唯一能走通的路，所以顺序上也不能被配置压过去。
    """
    monkeypatch.setenv("HALL_PACKAGE_SHARE", "//host/hall-packages")
    with mock.patch("hall_auto.fetch.share_dirs", return_value=[Path("//other/hall-packages")]):
        dirs = bm._wheelhouse_share_dirs()
    assert dirs[0] == Path("//host/hall-packages"), dirs
    assert Path("//other/hall-packages") in dirs, dirs


def test_wheelhouse_share_dirs_survives_config_import_failure(bm, monkeypatch, tmp_path):
    """配置 import 不了（没装 PyYAML）时，仍然要能靠环境变量拿到共享盘。"""
    monkeypatch.setenv("HALL_PACKAGE_SHARE", "//host/hall-packages")
    # REPO_ROOT 指到空目录：本用例只验「环境变量这条路」，不让零依赖回退掺进来
    # （回退单独有守卫：test_wheelhouse_share_dirs_falls_back_to_config_without_pyyaml）
    monkeypatch.setattr(bm, "REPO_ROOT", tmp_path)
    monkeypatch.setitem(sys.modules, "hall_auto.config", None)  # 之后的 import 会 ImportError
    dirs = bm._wheelhouse_share_dirs()
    assert dirs == [Path("//host/hall-packages")], dirs


def test_wheelhouse_share_dirs_falls_back_to_config_without_pyyaml(bm, monkeypatch, tmp_path):
    """**无外网新机的命门**：没装 PyYAML 时，`package_share.dirs` 也必须读得到。

    2026-09-23 实测的硬阻断：原来这里只有「环境变量 + `hall_auto.config`」两条来源，
    而第 2 步跑在依赖还没装的时刻，`import hall_auto.config` 必抛（顶层 `import yaml`），
    异常被 `except Exception: pass` 吞掉 → 共享盘列表为空 → 共享盘上明明备好的
    `wheelhouse\\` 找不到 → 转在线 pip → 无外网的机器 `No matching distribution found`，
    bootstrap 当场停，报的却是「没配共享盘」（指向完全错误的方向）。
    """
    (tmp_path / "config.yaml").write_text(
        "installer_dir: \"C:/Users/ASUS/Desktop/华硕大厅\"\n"
        "package_share:\n"
        "  dirs:\n"
        "    - \"//LAPTOP-VS5F7HF4/hall-packages\"\n"
        "display_name_contains: \"华硕大厅\"\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bm, "REPO_ROOT", tmp_path)
    monkeypatch.delenv("HALL_PACKAGE_SHARE", raising=False)
    monkeypatch.setitem(sys.modules, "hall_auto.config", None)  # 模拟"系统 Python 没有 PyYAML"

    dirs = bm._wheelhouse_share_dirs()
    assert dirs == [Path("//LAPTOP-VS5F7HF4/hall-packages")], dirs


def test_share_dirs_from_config_text_ignores_commented_examples(bm):
    """注释必须剥干净：**行内尾注释不能粘进路径**，整行注释掉的备用地址也不能收。

    `config.yaml` 的真实写法就是 `- "//LAPTOP-VS5F7HF4/hall-packages"   # 机器名访问（推荐）`。
    不剥 `#` 的话整段注释会粘成路径的一部分 —— 节点去连一个不存在的地址，
    报的还是「找不到网络路径」，看着像共享盘没建（本项目反复踩的那类误导）。
    下面那段注释掉的 IP 备用地址同理：真收进来就会去连一个随 DHCP 变过的过期 IP。
    """
    text = (
        "package_share:\n"
        "  dirs:\n"
        "    - \"//LAPTOP-VS5F7HF4/hall-packages\"   # 机器名访问（推荐）\n"
        "  # 例（多共享盘做冗余，按顺序试）：\n"
        "  # dirs:\n"
        "  #   - \"//192.168.0.8/hall-packages\"     # 备用：本机当时 IP，会随 DHCP 变化\n"
        "display_name_contains: \"华硕大厅\"\n"
    )
    assert bm._share_dirs_from_config_text(text) == ["//LAPTOP-VS5F7HF4/hall-packages"]


def test_share_dirs_from_config_text_stops_at_other_top_level_lists(bm):
    """出了 `package_share` 块就停 —— 别把别的顶层列表项（如 `dismiss_buttons`）当共享盘。"""
    text = (
        "package_share:\n"
        "  dirs:\n"
        "    - \"//host/hall-packages\"\n"
        "launch:\n"
        "  dismiss_buttons:\n"
        "    - 关闭\n"
        "    - 稍后\n"
    )
    assert bm._share_dirs_from_config_text(text) == ["//host/hall-packages"]


def test_share_dirs_from_config_files_prefers_local_config(bm, monkeypatch, tmp_path):
    """本机 `config.local.yaml` 覆盖仓库模板 —— 与 `load_config()` 的覆盖语义一致。"""
    (tmp_path / "config.yaml").write_text(
        "package_share:\n  dirs:\n    - \"//repo-host/hall-packages\"\n", encoding="utf-8"
    )
    (tmp_path / "config.local.yaml").write_text(
        "package_share:\n  dirs:\n    - \"//local-host/hall-packages\"\n", encoding="utf-8"
    )
    monkeypatch.setattr(bm, "REPO_ROOT", tmp_path)
    assert bm._share_dirs_from_config_files() == ["//local-host/hall-packages"]


def test_real_repo_config_yaml_yields_the_share(bm):
    """真实仓库的 `config.yaml` 必须能被零依赖读法读出共享盘地址（新机就靠它）。"""
    dirs = bm._share_dirs_from_config_files()
    assert dirs, "config.yaml 里读不出 package_share.dirs —— 无外网新机第 2 步会挂"
    assert all(d.startswith("//") or ":" in d for d in dirs), dirs


def _write_wheelhouse(wh: Path, python_xy: str, req_sha: str) -> None:
    from hall_auto.wheelhouse import MANIFEST_NAME

    wh.mkdir(parents=True, exist_ok=True)
    (wh / "pyyaml-6.0-cp312-cp312-win_amd64.whl").write_bytes(b"x")
    (wh / MANIFEST_NAME).write_text(
        json.dumps(
            {
                "python_xy": python_xy,
                "requirements_sha256": req_sha,
                "wheel_count": 1,
                "wheels": ["pyyaml-6.0-cp312-cp312-win_amd64.whl"],
            }
        ),
        encoding="utf-8",
    )


def test_wheelhouse_usable_rejects_wheel_for_another_python(bm, tmp_path):
    """**核心防线**：为别的 Python 版本下的 wheel，必须当场判不能用并说清原因。

    不判的话 pip 只会抛 `No matching distribution found` 或一堆编译错误 ——
    一线看不出根因是"wheel 是为另一个 Python 版本下的"。
    """
    mine = f"{sys.version_info[0]}.{sys.version_info[1]}"
    other = "3.9" if mine != "3.9" else "3.8"
    wh = tmp_path / "wh"
    _write_wheelhouse(wh, other, "a" * 64)

    ok, why = bm._wheelhouse_usable(wh, Path(sys.executable), "a" * 64)
    assert ok is False
    assert other in why and mine in why, why
    assert "fetch_wheelhouse.py" in why, "要告诉人怎么重做一份"


def test_wheelhouse_usable_rejects_when_requirements_changed(bm, tmp_path):
    """requirements 改过，旧 wheelhouse 就不能再用了（缺新包会装到一半失败）。"""
    mine = f"{sys.version_info[0]}.{sys.version_info[1]}"
    wh = tmp_path / "wh"
    _write_wheelhouse(wh, mine, "a" * 64)

    ok, why = bm._wheelhouse_usable(wh, Path(sys.executable), "b" * 64)
    assert ok is False
    assert "requirements" in why, why


def test_wheelhouse_usable_accepts_matching_manifest(bm, tmp_path):
    """版本与 requirements 都对得上 -> 能用，且理由里带上 wheel 数量。"""
    mine = f"{sys.version_info[0]}.{sys.version_info[1]}"
    wh = tmp_path / "wh"
    _write_wheelhouse(wh, mine, "a" * 64)

    ok, why = bm._wheelhouse_usable(wh, Path(sys.executable), "a" * 64)
    assert ok is True, why
    assert "1" in why, why


def test_venv_prefers_offline_wheelhouse_over_network(bm, tmp_path):
    """**核心防线**：有可用的 wheelhouse 时，pip 必须走 `--no-index --find-links`。

    而且**不许**再探一次 pip 源 —— 无网机器上那一下纯属白等。
    离线优先不只是为了没网：它同时把版本锁死，5 台机器装出来的依赖完全一致。
    """
    wh = tmp_path / "wh"
    wh.mkdir()
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append([str(a) for a in argv] if isinstance(argv, (list, tuple)) else [str(argv)])
        return _fake_run()

    with _no_share_wheelhouse(bm), \
         mock.patch.object(bm, "_resolve_wheelhouse", return_value=(wh, "")), \
         mock.patch.object(bm, "_wheelhouse_usable", return_value=(True, "Python 3.12，7 个 wheel")), \
         mock.patch.object(bm, "_deps_probe", return_value=(True, "ok")), \
         mock.patch.object(bm, "_req_digest", return_value="a" * 64), \
         mock.patch.object(bm, "_pip_net_ok") as net, \
         mock.patch.object(Path, "is_file", return_value=False), \
         mock.patch.object(Path, "write_text"), \
         mock.patch.object(bm.subprocess, "run", side_effect=fake_run):
        st = bm.step_venv(False)

    assert st.ok, st.detail
    installs = [c for c in calls if "install" in " ".join(c)]
    assert installs, st.detail
    assert all("--no-index" in c and "--find-links" in c for c in installs), installs
    assert str(wh) in " ".join(installs[0]), installs[0]
    net.assert_not_called()


def test_venv_falls_back_online_when_offline_install_fails(bm, tmp_path):
    """离线装失败要**继续尝试在线** —— 不能因为"有 wheelhouse 但用不了"就比原来更差。"""
    wh = tmp_path / "wh"
    wh.mkdir()
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        argv_list = [str(a) for a in argv] if isinstance(argv, (list, tuple)) else [str(argv)]
        calls.append(argv_list)
        if "--no-index" in argv_list:
            return _fake_run(returncode=1, stderr="No matching distribution found")
        return _fake_run()

    with _no_share_wheelhouse(bm), \
         mock.patch.object(bm, "_resolve_wheelhouse", return_value=(wh, "")), \
         mock.patch.object(bm, "_wheelhouse_usable", return_value=(True, "Python 3.12，7 个 wheel")), \
         mock.patch.object(bm, "_deps_probe", return_value=(True, "ok")), \
         mock.patch.object(bm, "_req_digest", return_value="a" * 64), \
         mock.patch.object(bm, "_pip_net_ok", return_value=(True, "可达")) as net, \
         mock.patch.object(Path, "is_file", return_value=False), \
         mock.patch.object(Path, "write_text"), \
         mock.patch.object(bm.subprocess, "run", side_effect=fake_run):
        st = bm.step_venv(False)

    assert st.ok, st.detail
    installs = [c for c in calls if "install" in " ".join(c)]
    assert any("--no-index" in c for c in installs), "先要试离线"
    assert any("--no-index" not in c for c in installs), "离线失败后要继续试在线"
    net.assert_called()


def test_venv_reports_unusable_wheelhouse_instead_of_silently_going_online(bm, tmp_path):
    """有 wheelhouse 但用不了，要把**原因**说出来（版本不符？requirements 改过？）。

    静默回落到在线，在无网机器上就是"跑了半天然后失败"，人不知道是 wheel 不对。
    """
    wh = tmp_path / "wh"
    wh.mkdir()

    def fake_run(argv, **kwargs):
        argv_list = [str(a) for a in argv] if isinstance(argv, (list, tuple)) else [str(argv)]
        if "install" in argv_list:
            return _fake_run(returncode=1, stderr="连不上")
        return _fake_run()

    with _no_share_wheelhouse(bm), \
         mock.patch.object(bm, "_resolve_wheelhouse", return_value=(wh, "")), \
         mock.patch.object(bm, "_wheelhouse_usable", return_value=(False, "wheel 是给 Python 3.9 下的")), \
         mock.patch.object(bm, "_deps_probe", return_value=(True, "ok")), \
         mock.patch.object(bm, "_req_digest", return_value="a" * 64), \
         mock.patch.object(bm, "_pip_net_ok", return_value=(True, "可达")), \
         mock.patch.object(Path, "is_file", return_value=False), \
         mock.patch.object(Path, "write_text"), \
         mock.patch.object(bm.subprocess, "run", side_effect=fake_run):
        st = bm.step_venv(False)

    joined = "\n".join(st.detail)
    assert "用不了" in joined and "Python 3.9" in joined, joined


def test_skip_venv_probes_instead_of_trusting_existence(bm):
    """**回归锁**：`--skip-venv` 不再"文件在就跳过"，要实测依赖能用。

    原因：手册以前给无网机器的绕法是整份拷 `.venv`。那种 venv 文件全在但起不来
    （`pyvenv.cfg` 的 `home` 指向原机器）。旧逻辑会一路蒙到第 12 步自检才炸，
    报错还看不出根因。现在当场探、当场说，并给出离线 wheelhouse 这条出路。
    """
    with mock.patch.object(bm, "_venv_python_ok", return_value=True), \
         mock.patch.object(bm, "_deps_probe", return_value=(False, "缺 pywinauto")), \
         mock.patch.object(Path, "is_file", return_value=True):
        st = bm.step_venv(True)

    assert st.ok is False
    joined = "\n".join(st.detail)
    assert "--skip-venv 指定的 venv 不可用" in joined, joined
    assert "wheelhouse" in joined, "要给出离线 wheelhouse 这条出路"


def test_venv_rebuilds_copied_venv_that_cannot_start(bm):
    """拷来的 .venv 起不来 -> 当场本机重建，别拖到第 12 步才炸。"""
    cmds: list[list[str]] = []

    def fake_run(argv, **kwargs):
        cmds.append([str(a) for a in argv] if isinstance(argv, (list, tuple)) else [str(argv)])
        return _fake_run()

    with _no_share_wheelhouse(bm), \
         mock.patch.object(bm, "_venv_python_ok", return_value=False), \
         mock.patch.object(bm, "_deps_probe", return_value=(True, "ok")), \
         mock.patch.object(bm, "_req_digest", return_value="a" * 64), \
         mock.patch.object(Path, "is_file", return_value=True), \
         mock.patch.object(Path, "read_text", return_value="a" * 64), \
         mock.patch.object(bm.subprocess, "run", side_effect=fake_run):
        st = bm.step_venv(False)

    assert st.ok, st.detail
    assert any("本机重建" in line for line in st.detail), st.detail
    assert any("venv" in c for c in cmds), cmds


def test_main_reexecs_under_the_venv_interpreter(bm):
    """**核心防线**：main() 里必须有"换成 venv 解释器重跑"这一段。

    为什么必须有：本脚本 import 的 `hall_auto.config` 依赖 PyYAML。用**系统** Python
    跑的时候，第 5/7/8/10 步会各自"读配置失败"然后**静默跳过**（异常都被吞成一行日志），
    最后照样打印"全部通过" —— 也就是说**不换解释器，脚本会给一个假绿**。
    假绿比报错坏得多：人会以为环境好了，直到跑批才批量失败。
    """
    src = (REPO_ROOT / "tools" / "bootstrap_machine.py").read_text(encoding="utf-8")
    main_src = src[src.index("def main()"):]

    assert "REEXEC_ENV" in main_src, "main() 里要能看到换解释器的重入标记"
    assert "_venv_python()" in main_src, "要拿 venv 的解释器路径来比"
    assert "_running_under(" in main_src, "要判定当前是不是已经在 venv 里"


def test_dependency_failure_stops_before_the_rest_of_the_steps(bm):
    """依赖没铺好要**当场停**，不要继续跑后面几步。

    后面几步全都要 import 第三方包，继续跑只会刷一屏"读配置失败"的假错，
    把真正的原因（pip 装不上）埋掉 —— 又是一次"报错指向错误方向"。
    """
    src = (REPO_ROOT / "tools" / "bootstrap_machine.py").read_text(encoding="utf-8")
    main_src = src[src.index("def main()"):]

    guard = main_src.index("if not venv_step.ok:")
    first_other_step = main_src.index("run(step_fixtures(")
    assert guard < first_other_step, "依赖失败的拦截必须排在其余步骤之前"
    assert "aborted=True" in main_src[guard:first_other_step + 200]


def test_share_connect_is_shared_by_venv_and_share_login(bm):
    """第 2 步（取离线 wheel）和第 7 步（正式连盘）必须**共用一份**连接逻辑。

    各写一份的话，"认哪个环境变量、失败怎么报"会慢慢长歪 —— 这类分叉本项目踩过几次。
    """
    src = (REPO_ROOT / "tools" / "bootstrap_machine.py").read_text(encoding="utf-8")
    login = src[src.index("def step_share_login"):src.index("def step_fetch_packages")]
    assert "_share_connect(" in login, "step_share_login 要用公共件"
    # 真正的连接机制（拼 net use 命令、裁 UNC、起子进程）只该有一份。
    # 这里只查机制、不查字面 "net use" —— 注释里解释它是合法的。
    for mech in ("subprocess.run", "_unc_target("):
        assert mech not in login, f"连接细节（{mech}）不该在 step_share_login 里再抄一份"

    resolver = src[src.index("def _resolve_wheelhouse"):src.index("def _wheelhouse_usable")]
    assert "_share_connect(" in resolver, "取离线 wheel 时也要能连上共享盘"


def test_wheelhouse_dir_is_gitignored():
    """**`wheelhouse/` 必须在 .gitignore 里。**

    为什么是硬要求：`tools/fetch_wheelhouse.py` 的默认落点就是 `<仓库>/wheelhouse`，
    跑一次就是十几~上百 MB 的 `.whl`。不忽略的话，下一个 `git add -A` 就会把它提交进去
    —— 体积大到可能直接把 push 卡死，而且事后要改历史才清得掉。

    这类"跑一次就污染仓库"的产物必须靠 gitignore 挡，**不能靠人记得**。
    """
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    entries = {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert "wheelhouse/" in entries or "wheelhouse" in entries, (
        f"fetch_wheelhouse.py 的默认落点没被忽略：{sorted(entries)}"
    )


def test_wheelhouse_dir_name_is_consistent_across_three_places(bm):
    """产出目录名在三处必须一致：`fetch_wheelhouse.py` 的默认落点、
    `bootstrap` 的查找名、共享盘子目录名。

    不一致的后果很隐蔽：有网机器产出的东西，无网机器**找不到**，而且不报错 ——
    只是日志里一句"没有离线 wheelhouse"，然后转头去连 pip 源（无网机器上必然失败）。
    一线会以为是网络问题，实际是两边目录名对不上。
    """
    import importlib.util

    path = REPO_ROOT / "tools" / "fetch_wheelhouse.py"
    spec = importlib.util.spec_from_file_location("fetch_wheelhouse_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    produced = module.main.__globals__  # 只为拿 REPO_ROOT，不执行 main
    assert produced["REPO_ROOT"] == bm.REPO_ROOT, "两边算出的仓库根不一致"

    # 产出方默认写到 REPO_ROOT/"wheelhouse"（源码里的字面量），消费方按常量找
    src = path.read_text(encoding="utf-8")
    assert 'REPO_ROOT / "wheelhouse"' in src, "产出方的默认落点变了，要同步消费方与 .gitignore"
    assert bm.WHEELHOUSE_LOCAL_DIRNAME == "wheelhouse"
    assert bm.WHEELHOUSE_SHARE_SUBDIR == "wheelhouse"


# ---------------- 第 12 步自检：失败要说清是哪几条 ----------------


def _selftest_with(bm, monkeypatch, tmp_path, stdout: str, returncode: int = 1):
    """在 tmp_path 里假装跑一次自检，返回 (Step, 落盘目录)。"""
    fake_py = tmp_path / "python.exe"
    fake_py.write_bytes(b"")
    monkeypatch.setattr(bm, "REPO_ROOT", tmp_path)
    with mock.patch.object(bm, "_venv_python", return_value=fake_py), \
         mock.patch.object(bm.subprocess, "run",
                           return_value=_fake_run(returncode=returncode, stdout=stdout)):
        st = bm.step_selftest(False)
    return st, tmp_path / "reports" / "bootstrap"


def test_selftest_lists_every_failed_case(bm, tmp_path, monkeypatch):
    """**核心防线**：自检失败要把**每条**失败用例名点出来。

    原来只贴 stdout 的最后 3 行 —— 一次能红 20 条，一线只看得到最后 2 条，
    还得靠数数猜。手册写的是"全绿说明环境 OK"，红了却不知道哪条红，等于没报。
    """
    out = (
        "FAILED tests/unit/a.py::test_one - AssertionError: 甲\n"
        "FAILED tests/unit/b.py::test_two - AssertionError: 乙\n"
        "FAILED tests/unit/c.py::test_three - AssertionError: 丙\n"
        "3 failed, 10 passed in 2s\n"
    )
    st, _ = _selftest_with(bm, monkeypatch, tmp_path, out)

    joined = "\n".join(st.detail)
    for name in ("test_one", "test_two", "test_three"):
        assert name in joined, f"{name} 没被列出来：{joined}"
    assert st.ok is False
    assert "重跑单条" in joined, "要给出一条能直接复制的排查命令"


def test_selftest_folds_long_failure_lists(bm, tmp_path, monkeypatch):
    """失败太多时折起来，但必须**说清还有几条** —— 静默截断会让人以为只有 12 条。"""
    many = "".join(f"FAILED tests/unit/m.py::test_{i} - AssertionError: x\n" for i in range(20))
    st, _ = _selftest_with(bm, monkeypatch, tmp_path, many + "20 failed, 1 passed in 3s\n")

    joined = "\n".join(st.detail)
    listed = joined.count("FAILED tests/unit/m.py::test_")
    assert listed == bm.SELFTEST_MAX_LISTED, f"实际列了 {listed} 条"
    assert "还有 8 条" in joined, joined


def test_selftest_saves_full_output_next_to_the_report(bm, tmp_path, monkeypatch):
    """完整 pytest 输出要落盘，并在结论里给出路径。

    不然"红了但不知道为什么"：断言到底写了什么，屏幕上和报告里都找不到。
    """
    out = "FAILED tests/unit/x.py::test_a - AssertionError: 因为 X 所以红\n2 failed, 3 passed in 1s\n"
    st, out_dir = _selftest_with(bm, monkeypatch, tmp_path, out)

    logs = list(out_dir.glob("selftest_*.log"))
    assert len(logs) == 1, f"应该恰好落一份日志：{logs}"
    assert "因为 X" in logs[0].read_text(encoding="utf-8")
    assert "selftest_" in "\n".join(st.detail), "结论里要给日志路径"


def test_selftest_reports_collection_crash_without_failed_lines(bm, tmp_path, monkeypatch):
    """收集期就崩了（没有 FAILED 行却非零退出）时，尾部输出要原样贴出来。

    这种情况屏幕上本来什么都没有 —— 不贴的话一线只知道"红了"，连是导入错误
    还是语法错误都不知道。
    """
    out = "ImportError: cannot import name 'nope' from 'hall_auto'\nERROR: found no collectors\n"
    st, _ = _selftest_with(bm, monkeypatch, tmp_path, out)

    joined = "\n".join(st.detail)
    assert st.ok is False
    assert "收集期" in joined, joined
    assert "cannot import name 'nope'" in joined, joined


def test_selftest_green_run_writes_no_log(bm, tmp_path, monkeypatch):
    """全绿时**不落日志** —— 不然每次铺设都往 reports/ 里塞一份没用的文件。

    （这里也顺带保证单测不会往真仓库的 reports/ 写东西：mock 的 stdout 为空。）
    """
    st, out_dir = _selftest_with(bm, monkeypatch, tmp_path, "", returncode=0)
    assert st.ok is True
    assert not list(out_dir.glob("selftest_*.log"))


# ---------------- 子进程输出解码（2026-09-21 首台真机事故）----------------
#
# 真机 DESKTOP-DOHED68 跑 bootstrap 时，控制台刷出 3 个
# `Exception in thread Thread-2X (_readerthread): UnicodeDecodeError: 'utf-8' codec ...`
#
# 根因：包装脚本 `bootstrap_machine.ps1` 设了 `PYTHONUTF8=1`，
# 于是 `locale.getpreferredencoding(False)` 变成 utf-8；而 Windows 自带命令
# 在中文系统上吐的是 **ANSI(cp936)**（`net use` / `schtasks /Create`），
# `schtasks /Query /XML` 更是 **UTF-16LE**。
# `subprocess.run(..., text=True)` 按 utf-8 解 -> reader 线程死 -> `proc.stdout` 变 `None`
# -> **那一句报错永远看不到内容**。
#
# 最坑的是 `_task_action`：读不到 XML 就以为「任务不存在」，
# 于是每次重跑都走 `/Create /F` 把计划任务强制重建 —— 正好重置了触发时间，
# 而 `step_schtask` 的 docstring 明说这件事不许发生。
#
# 修法：统一走 `_run_text()`（先收字节、再自己解码），下面把这条钉死。


def test_decode_console_handles_gbk_and_utf16(bm):
    """真实世界里会遇到的四种形态都要解出来，且**不许抛**。"""
    assert bm._decode_console("已经是文本") == "已经是文本"
    assert bm._decode_console(b"plain ascii") == "plain ascii"
    # net use / schtasks /Create：ANSI(cp936)
    assert bm._decode_console("共享盘不可达".encode("cp936")) == "共享盘不可达"
    # schtasks /Query /XML：UTF-16LE 带 BOM
    assert "Task" in bm._decode_console('<?xml version="1.0"?><Task/>'.encode("utf-16"))
    assert bm._decode_console(None) == ""
    # 单测里 subprocess 常被 mock 成裸 Mock —— 那种情况下当作「没有输出」，
    # 不能让解码这一步把结论炸掉
    assert bm._decode_console(mock.Mock()) == ""


def test_run_text_decodes_real_gbk_child(bm):
    """真起一个吐 cp936 字节的子进程 —— 这正是 `text=True` 会崩的那种输出。"""
    code = "import sys;sys.stdout.buffer.write('共享盘 可达'.encode('cp936'))"
    proc = bm._run_text([sys.executable, "-c", code], check=False)
    assert proc.returncode == 0
    assert proc.stdout == "共享盘 可达", f"解出来是 {proc.stdout!r}"


def test_no_text_mode_subprocess_left(bm):
    """**回归锁**：本模块不许再用 `text=True`（会被 `PYTHONUTF8=1` 带偏）。

    新加子进程调用请走 `_run_text()`；反引号里的 `text=True` 是文档，不算。
    """
    import re

    src = (REPO_ROOT / "tools" / "bootstrap_machine.py").read_text(encoding="utf-8")
    offenders = []
    for i, ln in enumerate(src.splitlines(), 1):
        # 先把反引号包起来的片段（文档/注释里的写法）整段拿掉，再看剩下的是不是真代码
        if "text=True" in re.sub(r"`[^`]*`", "", ln):
            offenders.append(f"{i}: {ln.strip()}")
    assert not offenders, "这些行还在用 text=True：\n" + "\n".join(offenders)


# ---------------- 第 12 步自检：红的是不是"环境级 flake" ----------------


def _selftest_with_sequence(bm, monkeypatch, tmp_path, results):
    """按顺序给每次 `subprocess.run` 指定返回值，模拟「首跑红、重跑绿」。"""
    fake_py = tmp_path / "python.exe"
    fake_py.write_bytes(b"")
    monkeypatch.setattr(bm, "REPO_ROOT", tmp_path)
    seq = list(results)

    def fake_run(cmd, **kw):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    with mock.patch.object(bm, "_venv_python", return_value=fake_py), \
         mock.patch.object(bm.subprocess, "run", side_effect=fake_run) as run:
        st = bm.step_selftest(False)
    return st, run


def test_selftest_retries_failed_cases_in_a_fresh_process(bm, tmp_path, monkeypatch):
    """**环境级 flake**：首跑红、换进程重跑绿 -> 按通过计，但必须留下痕迹。

    首台真机就是这么红的（杀软把进程刚写的临时文件判毒，见 运行手册「坑 6」）。
    手册让一线「遇到时先重跑一遍」；这里把那一遍自动化 ——
    否则每台新机器都要人来问一次「这两条红是不是我代码坏了」。
    """
    red = ("FAILED tests/unit/a.py::test_one - OSError: [Errno 22] Invalid argument\n"
           "1 failed, 9 passed in 3s\n")
    green = "9 passed in 1s\n"
    st, run = _selftest_with_sequence(
        bm, monkeypatch, tmp_path,
        [_fake_run(returncode=1, stdout=red), _fake_run(returncode=0, stdout=green)],
    )

    assert run.call_count == 2, "应当只重跑一次"
    assert st.ok is True, st.detail
    joined = "\n".join(st.detail)
    assert "test_one" in joined, f"首跑红的用例名要留着，别静默：{joined}"
    assert "环境级 flake" in joined, joined
    assert "坑 6" in joined, "要指向排查入口"

    # 重跑只带失败的那一条，不是再跑一遍全量
    retry_argv = run.call_args_list[1][0][0]
    assert "tests/unit/a.py::test_one" in retry_argv
    assert "tests/unit" not in retry_argv

    # 两轮输出落在**同一份**日志里（一次铺设别散落成多份）
    logs = list((tmp_path / "reports" / "bootstrap").glob("selftest_*.log"))
    assert len(logs) == 1, f"不该散成多份：{logs}"
    text = logs[0].read_text(encoding="utf-8")
    assert "Errno 22" in text, "首跑输出要留着"
    assert "[重跑]" in text, "重跑那一轮也要留证据"


def test_selftest_still_red_after_retry_is_real_failure(bm, tmp_path, monkeypatch):
    """重跑还红 -> 不是偶发，仍判失败，并把「仍未过」的用例点名。

    防的是"加重试把真回归盖掉"：只有**重跑转绿**才当 flake。
    """
    red = "FAILED tests/unit/a.py::test_one - AssertionError: 真坏了\n1 failed, 9 passed in 3s\n"
    st, _ = _selftest_with_sequence(
        bm, monkeypatch, tmp_path, [_fake_run(returncode=1, stdout=red)],
    )

    assert st.ok is False
    joined = "\n".join(st.detail)
    assert "不是偶发" in joined, joined
    assert "仍未过：tests/unit/a.py::test_one" in joined, joined


def test_selftest_names_the_known_av_quarantine(bm, tmp_path, monkeypatch):
    """重跑也红、但红的是 `[Errno 22]` -> 明说"这是杀软误杀，别去改代码"。

    2026-09-21 首台真机就是踩在这个形态上：`Errno 22` 看起来像"共享盘拷贝的 bug"，
    实际是 Defender 把进程刚写出的临时文件判毒（`GetLastError=225`，见 运行手册 坑 6）。
    报错必须能指向**下一步该动哪儿**，否则一线只会去改 `fetch.py`。
    """
    red = ("FAILED tests/unit/a.py::test_one - OSError: [Errno 22] Invalid argument\n"
           "1 failed, 9 passed in 3s\n")
    st, _ = _selftest_with_sequence(
        bm, monkeypatch, tmp_path, [_fake_run(returncode=1, stdout=red)],
    )

    joined = "\n".join(st.detail)
    assert st.ok is False, "环境问题也是问题：这台机器现在确实跑不了这几条，不能判绿"
    assert "杀软误杀" in joined, joined
    assert "225" in joined and "坑 6" in joined, joined


def test_selftest_does_not_blame_av_for_other_failures(bm, tmp_path, monkeypatch):
    """普通断言失败不许挂到"杀软"头上 —— 误导比不报更坏。"""
    red = "FAILED tests/unit/a.py::test_one - AssertionError: 逻辑真的错了\n1 failed, 9 passed in 3s\n"
    st, _ = _selftest_with_sequence(
        bm, monkeypatch, tmp_path, [_fake_run(returncode=1, stdout=red)],
    )

    assert st.ok is False
    assert "杀软" not in "\n".join(st.detail)


# ---------------- main() 读的每个 args.X 都必须是注册过的命令行开关 ----------------
#
# 2026-09-22 复核「屏幕常亮」那批改动时真踩（不是假设）：
# `step_keep_awake` 接进了 `main()`、读的是 `args.skip_keep_awake`，
# 但 `--skip-keep-awake` **压根没注册进 argparse** —— 于是**每一次** bootstrap
# 都在最后一步 `AttributeError: 'Namespace' object has no attribute 'skip_keep_awake'`
# 崩掉，而且是在跑完前 12 步（十几分钟）之后才崩。
#
# 当时那条守卫写的是 `assert "--skip-keep-awake" in src`：docstring 里提一句就满足，
# **看着是绿的**。这就是本项目反复踩的"假守卫" —— 判据粗到能被文案满足的守卫等于没有守卫。
# 所以改成走 AST：只看 `main()` 里真的读了哪些 `args.<名字>`，逐个核对注册表。


def test_every_args_attribute_used_in_main_is_registered():
    """`main()` 里读的每个 `args.X`，argparse 里都要有对应的 `--x`。

    这一类错（漏注册一个 `add_argument`）不会在 import 时报错、不会在其它单测里露头，
    只在那一步真的跑到时才炸 —— 而它恰好排在最后。用 AST 钉死，别再靠人眼。
    """
    src = (REPO_ROOT / "tools" / "bootstrap_machine.py").read_text(encoding="utf-8")
    main_fn = next(
        (n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "main"),
        None,
    )
    assert main_fn is not None, "找不到 main() —— 本用例的前提是它存在"

    used = {
        node.attr
        for node in ast.walk(main_fn)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "args"
    }
    assert used, "main() 里一个 args.X 都没读到？八成是解析写错了"

    # `add_argument(` 之后可能换行才写参数名（本文件就是这么排的），所以用 `\s*` 跨行。
    registered = set(re.findall(r'add_argument\(\s*"--([a-z0-9-]+)"', src))

    missing = sorted(a for a in used if a.replace("_", "-") not in registered)
    assert not missing, (
        "main() 读了这些 `args.` 属性，但 argparse 里没注册对应开关 —— "
        "跑起来会在那一步 AttributeError 崩掉：\n  "
        + "\n  ".join(f"args.{a} -> 缺 --{a.replace('_', '-')}" for a in missing)
    )
