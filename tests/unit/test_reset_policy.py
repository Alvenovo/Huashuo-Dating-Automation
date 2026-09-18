"""锁跑批前复位策略（installer.reset_before_run + tools/reset_machine.py 的预演）。

组长 2026-09-18 要求：「不论装没装对应大厅，都先检测，有检测到就先删掉，重新下载」。
这段逻辑决定每台机器的起点是否干净 —— 起点不干净，跑出来的结论全是噪声。
"""

from __future__ import annotations

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


def test_reset_uninstalls_when_installed(cfg):
    """检测到已装 -> 先卸载，再备包。"""
    with mock.patch.object(installer, "read_installed", return_value=_fake_installed()), \
         mock.patch.object(installer, "stop_product") as stop, \
         mock.patch.object(installer, "_run_nsis") as run, \
         mock.patch.object(installer, "_wait_until") as wait, \
         mock.patch.object(installer, "configured_filenames", return_value=[]):
        report = installer.reset_before_run(cfg)
    stop.assert_called_once()
    run.assert_called_once()
    wait.assert_called_once()  # 等卸载完成，但不真等
    steps = [s["step"] for s in report["steps"]]
    assert steps == ["detect", "uninstall"]
    assert report["steps"][0]["found"] is True
    assert report["steps"][0]["display_version"] == "1.6.8.17"


def test_reset_skips_uninstall_when_clean(cfg):
    """没装就跳过卸载，不白跑一遍。"""
    with mock.patch.object(installer, "read_installed", return_value=None), \
         mock.patch.object(installer, "_run_nsis") as run, \
         mock.patch.object(installer, "configured_filenames", return_value=[]):
        report = installer.reset_before_run(cfg)
    run.assert_not_called()
    assert report["steps"][0]["found"] is False


def test_reset_fetches_packages(cfg):
    """卸载之后要把配置声明的包备齐。"""
    from hall_auto.fetch import FetchResult

    with mock.patch.object(installer, "read_installed", return_value=None), \
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
    """单个包拿不到不该炸掉复位 —— 别的包还是要备齐，失败记进报告。"""
    with mock.patch.object(installer, "read_installed", return_value=None), \
         mock.patch.object(installer, "configured_filenames", return_value=["bad.exe"]), \
         mock.patch.object(installer, "ensure_package", side_effect=RuntimeError("网络不通")):
        report = installer.reset_before_run(cfg)
    assert report["packages"][0]["source"] == "failed"
    assert "网络不通" in report["packages"][0]["error"]
    assert report["ok"] is False


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
