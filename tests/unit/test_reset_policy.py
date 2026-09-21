"""锁跑批前复位策略（installer.reset_before_run + tools/reset_machine.py 的预演）。

组长 2026-09-18 要求：「不论装没装对应大厅，都先检测，有检测到就先删掉，重新下载」。
这段逻辑决定每台机器的起点是否干净 —— 起点不干净，跑出来的结论全是噪声。

**但卸载是不可逆的**，所以这段逻辑还多两道保护闸，下面的用例把它们钉死：

1. **顺序**：先只读确认包能拿到，再卸。反过来的话包取不到就停在
   「大厅被卸掉且装不回来」—— 对跑批是最坏结果。
2. **闸门**：没显式开卸载闸（`allow_uninstall=True` / `HALL_ALLOW_INSTALL=1`）就只报告不卸。
   办公机上误跑一次就少一个大厅，而 pytest 的 `--allow-install` 管不到这里。
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from hall_auto import installer
from hall_auto.config import load_config


@pytest.fixture
def cfg(tmp_path):
    with mock.patch.dict("os.environ", {"HALL_INSTALLER_DIR": str(tmp_path)}, clear=False):
        yield load_config()


def _fake_installed():
    return mock.Mock(
        display_name="华硕大厅",
        display_version="1.6.8.17",
        uninstall_string='"C:/Program Files (x86)/ASUS/x/uninst.exe"',
        install_dir=Path("C:/Program Files (x86)/ASUS/ASUS Member Center"),
    )


def _precheck_ok():
    """预检通过：假装每个包都能拿到（不真碰文件系统）。"""
    return mock.patch.object(installer, "check_packages_available", return_value=(True, []))


def test_reset_uninstalls_when_gate_open(cfg):
    """开了卸载闸 + 包拿得到 -> 先卸载，再备包。"""
    with _precheck_ok(), \
         mock.patch.object(installer, "read_installed", return_value=_fake_installed()), \
         mock.patch.object(installer, "stop_product") as stop, \
         mock.patch.object(installer, "_run_nsis") as run, \
         mock.patch.object(installer, "_wait_until") as wait, \
         mock.patch.object(installer, "configured_filenames", return_value=[]):
        report = installer.reset_before_run(cfg, allow_uninstall=True)
    stop.assert_called_once()
    run.assert_called_once()
    wait.assert_called_once()  # 等卸载完成，但不真等
    steps = [s["step"] for s in report["steps"]]
    assert steps == ["detect", "uninstall"]
    assert report["steps"][0]["found"] is True
    assert report["steps"][0]["display_version"] == "1.6.8.17"


def test_reset_refuses_uninstall_when_gate_closed(cfg):
    """**关键保护**：检测到已装但没开闸 -> 只报告，绝不卸载。

    办公机上误跑一次 `reset_machine.py` 就少一个大厅。默认必须不卸。
    """
    with _precheck_ok(), \
         mock.patch.object(installer, "read_installed", return_value=_fake_installed()), \
         mock.patch.object(installer, "stop_product") as stop, \
         mock.patch.object(installer, "_run_nsis") as run, \
         mock.patch.object(installer, "configured_filenames", return_value=[]):
        report = installer.reset_before_run(cfg, allow_uninstall=False)
    run.assert_not_called()
    stop.assert_not_called()
    uninstall_step = report["steps"][-1]
    assert uninstall_step["step"] == "uninstall"
    assert uninstall_step["skipped"] is True
    assert "未开卸载闸" in uninstall_step["detail"]
    # 没卸就不该算失败：机器保持原样是预期行为，不是错误
    assert report["ok"] is True


def test_reset_blocked_when_packages_unavailable(cfg):
    """**关键保护**：包拿不到 -> 抛 ResetBlocked，**大厅保持不动**。

    这是"先确认能装回来，再卸"的落地。没有这道闸的旧行为会先把大厅卸掉，
    然后取包失败，机器停在卸了装不回来的状态。
    """
    with mock.patch.object(
        installer, "check_packages_available",
        return_value=(False, [{"filename": "missing.exe", "available": False, "source": "none"}]),
    ), mock.patch.object(installer, "read_installed", return_value=_fake_installed()), \
         mock.patch.object(installer, "_run_nsis") as run:
        with pytest.raises(installer.ResetBlocked, match="拒绝卸载"):
            installer.reset_before_run(cfg, allow_uninstall=True)
    run.assert_not_called(), "包拿不到时绝不能动已装的厅"


def test_reset_skips_uninstall_when_clean(cfg):
    """没装就跳过卸载，不白跑一遍。"""
    with _precheck_ok(), \
         mock.patch.object(installer, "read_installed", return_value=None), \
         mock.patch.object(installer, "_run_nsis") as run, \
         mock.patch.object(installer, "configured_filenames", return_value=[]):
        report = installer.reset_before_run(cfg, allow_uninstall=True)
    run.assert_not_called()
    assert report["steps"][0]["found"] is False


def test_reset_fetches_packages(cfg):
    """卸载之后要把配置声明的包备齐。"""
    from hall_auto.fetch import FetchResult

    with _precheck_ok(), \
         mock.patch.object(installer, "read_installed", return_value=None), \
         mock.patch.object(installer, "configured_filenames", return_value=["a.exe", "b.exe"]), \
         mock.patch.object(
             installer, "ensure_package",
             side_effect=lambda c, n, **kw: FetchResult(
                 path=Path(n), filename=n, source="download", sha256="ab" * 32, bytes=1024, url="https://x"
             ),
         ):
        report = installer.reset_before_run(cfg)
    assert [p["filename"] for p in report["packages"]] == ["a.exe", "b.exe"]
    assert all(p["source"] == "download" for p in report["packages"])
    assert report["ok"] is True


def test_reset_records_package_failure_without_raising(cfg):
    """单个包实际获取失败要记进报告、ok=False，但**不抛异常**打断别的包。

    与上面的 ResetBlocked 区分：那个是"预检就确定拿不到，别卸"，
    这个是"预检过了但真取时还是失败了"—— 此时不再拦，但要如实标记不 ok。
    """
    with _precheck_ok(), \
         mock.patch.object(installer, "read_installed", return_value=None), \
         mock.patch.object(installer, "configured_filenames", return_value=["bad.exe"]), \
         mock.patch.object(installer, "ensure_package", side_effect=RuntimeError("网络不通")):
        report = installer.reset_before_run(cfg)
    assert report["packages"][0]["source"] == "failed"
    assert "网络不通" in report["packages"][0]["error"]
    assert report["ok"] is False


def _stub_cfg(installer_dir, *, names=("ghost.exe",), shares=()):
    """造一个只够 check_packages_available 用的最小配置桩。

    不真读 config.local.yaml —— 本机那份里配着**真实可达的共享盘**，
    会让"只剩下载这一条路"的断言失真。
    """
    return mock.Mock(
        installer_dir=Path(installer_dir),
        baseline_setup=names[0] if names else "",
        latest_setup="",
        update_fixture=mock.Mock(package="", sha256=""),
        raw={"package_share": {"dirs": list(shares)}},
    )


def test_precheck_does_not_treat_guessed_cdn_as_available(tmp_path):
    """**关键保护**：本地/缓存/共享盘都没有、且 `download.url_template` 为空时，
    预检必须判**不可得**。

    默认 CDN 模板只是猜测（组长两次确认没有官方地址，那些 URL 实际全是 404）。
    把它当"能拿到"，就会在卸完大厅后 404 装不回来。
    """
    bogus = tmp_path / "empty-installer-dir"
    cfg = _stub_cfg(bogus, names=("ghost.exe",), shares=())
    with mock.patch.object(installer, "configured_filenames", return_value=["ghost.exe"]):
        ok, results = installer.check_packages_available(cfg, allow_download=True)
    packs = [r for r in results if r.get("filename") == "ghost.exe"]
    assert packs, "至少要探测配置声明的包"
    assert packs[0]["available"] is False, "地址未配置时不该判为可得"
    assert ok is False


def test_precheck_accepts_share_hit(tmp_path):
    """共享盘里有这个包 -> 判可得。这是没下载地址时的主路，不能被上面的保护误伤。"""
    share = tmp_path / "share"
    share.mkdir()
    (share / "real.exe").write_bytes(b"x")
    cfg = _stub_cfg(tmp_path / "empty", names=("real.exe",), shares=(str(share),))
    with mock.patch.object(installer, "configured_filenames", return_value=["real.exe"]):
        ok, results = installer.check_packages_available(cfg, allow_download=False)
    packs = [r for r in results if r.get("filename") == "real.exe"]
    assert packs[0]["available"] is True
    assert packs[0]["source"] == "share"
    assert ok is True


def test_precheck_rejects_create_of_installer_dir(tmp_path):
    """**假绿防护**：预检不该把配置里那个（可能来自别人机器的）目录建出来。

    旧实现在 reset_machine.py 里 mkdir 空目录，导致"包不存在"被藏起来、报告还是绿。
    """
    bogus = tmp_path / "not-created-yet" / "华硕大厅"
    cfg = _stub_cfg(bogus, names=("x.exe",), shares=())
    with mock.patch.object(installer, "configured_filenames", return_value=["x.exe"]):
        installer.check_packages_available(cfg, allow_download=False)
    assert not bogus.exists(), "预检只读，不该创建目录"


def test_resolve_setup_respects_env_disable(cfg):
    """HALL_ALLOW_PACKAGE_DOWNLOAD=0 时禁止下载，只找本地（离线验收用）。"""
    with mock.patch.dict("os.environ", {"HALL_ALLOW_PACKAGE_DOWNLOAD": "0"}), \
         mock.patch.object(installer, "ensure_package") as ensure:
        installer.resolve_setup(cfg, "x.exe")
    assert ensure.call_args.kwargs["allow_download"] is False


def test_resolve_setup_converts_error_type(cfg):
    """底层取包异常统一转成 InstallError，调用方只需 catch 一种。"""
    with mock.patch.object(installer, "ensure_package", side_effect=RuntimeError("下载失败")):
        with pytest.raises(installer.InstallError, match="取安装包失败"):
            installer.resolve_setup(cfg, "x.exe")


def test_reset_allowed_reads_env_gate():
    """卸载闸与环境变量同口径：只有 HALL_ALLOW_INSTALL=1 才算开。"""
    with mock.patch.dict("os.environ", {}, clear=False):
        import os

        os.environ.pop("HALL_ALLOW_INSTALL", None)
        assert installer.reset_allowed() is False
        os.environ["HALL_ALLOW_INSTALL"] = "1"
        assert installer.reset_allowed() is True
        os.environ["HALL_ALLOW_INSTALL"] = "0"
        assert installer.reset_allowed() is False


# ---------------- 钉住包的位置：必须认 _cache ----------------
#
# 2026-09-21 查出的真 bug：新机器上钉住包不是人拷来的，是 bootstrap 从共享盘/官网
# 取回来的，落点在 `installer_dir/_cache/fixtures/`。而 `reset_fixture.py` 读的是
# `cfg.update_package_path`，早先只认 `installer_dir/fixtures/` —— 于是离线新机上
# 它会报「钉住安装包不存在」，P1-A 更新用例的起点建不起来
# （表现为该用例失败，看着像产品问题，实际是包放错了位置）。
# 与 `security_package_path` 是同一类洞，那次修了安全包、这次漏了夹具包。


def _fixture_cfg(tmp_path, package: str):
    import yaml

    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text(yaml.safe_dump({
        "installer_dir": str(tmp_path / "installer"),
        "update_fixture": {"name": "7-Zip", "package": package},
    }, allow_unicode=True), encoding="utf-8")
    with mock.patch.dict("os.environ", {"HALL_CONFIG": str(cfg_file)}, clear=False):
        os.environ.pop("HALL_PACKAGE_CACHE", None)
        return load_config(cfg_file)


def test_update_package_path_prefers_direct_location(tmp_path):
    """直路径有 -> 就用它（既有跑法不能改坏）。"""
    cfg = _fixture_cfg(tmp_path, "fixtures/7z2602-x64.exe")
    direct = tmp_path / "installer" / "fixtures" / "7z2602-x64.exe"
    direct.parent.mkdir(parents=True)
    direct.write_bytes(b"x")
    assert cfg.update_package_path == direct


def test_update_package_path_falls_back_to_cache(tmp_path):
    """**回归锁**：只有 `_cache/fixtures/` 有也要认出来。"""
    cfg = _fixture_cfg(tmp_path, "fixtures/7z2602-x64.exe")
    cached = tmp_path / "installer" / "_cache" / "fixtures" / "7z2602-x64.exe"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"x")
    assert cfg.update_package_path == cached


def test_update_package_path_returns_direct_when_nowhere(tmp_path):
    """两边都没有 -> 返回直路径（让调用方报错时报出"应该放哪"，便于补救）。"""
    cfg = _fixture_cfg(tmp_path, "fixtures/7z2602-x64.exe")
    assert cfg.update_package_path == tmp_path / "installer" / "fixtures" / "7z2602-x64.exe"


def test_update_package_path_respects_absolute_path(tmp_path):
    """配的是绝对路径 -> 原样返回，不拼接 installer_dir。"""
    absolute = tmp_path / "elsewhere" / "7z.exe"
    cfg = _fixture_cfg(tmp_path, str(absolute))
    assert cfg.update_package_path == absolute
