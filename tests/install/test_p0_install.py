from __future__ import annotations

import pytest

from hall_auto.about import read_about_version
from hall_auto.installer import install_baseline_clean, upgrade_to_latest
from hall_auto.product import is_admin, read_installed
from hall_auto.version import expected_version_from_setup_name, versions_equal


def _require_setup(cfg, path):
    if not cfg.installer_dir.is_dir():
        pytest.fail(f"安装包目录不存在: {cfg.installer_dir}")
    if not path.is_file():
        listing = sorted(p.name for p in cfg.installer_dir.glob("*.exe"))
        pytest.fail(f"找不到安装包 {path.name}，目录内: {listing}")


def _assert_product(info, expected_version: str, display_name_contains: str):
    assert info is not None, "安装后读不到卸载项"
    assert display_name_contains in info.display_name, (
        f"显示名应为包含 {display_name_contains!r}，实际 {info.display_name!r}"
    )
    assert versions_equal(info.display_version, expected_version), (
        f"DisplayVersion 期望 {expected_version}，实际 {info.display_version!r}"
    )
    assert versions_equal(info.file_version, expected_version), (
        f"FileVersion 期望 {expected_version}，实际 {info.file_version!r}"
    )


def _assert_about_version(cfg, expected_version: str):
    about = read_about_version(cfg)
    assert versions_equal(about, expected_version), (
        f"关于页版本 期望 {expected_version}，实际 {about!r}"
    )


@pytest.mark.install
@pytest.mark.destructive
def test_p0_01_install_baseline(cfg):
    """P0-01 干净安装基线包：装完注册表版本与「关于」页版本一致"""
    if not is_admin():
        pytest.fail("P0-01 需要管理员权限运行 pytest")
    _require_setup(cfg, cfg.baseline_path)
    expected = expected_version_from_setup_name(cfg.baseline_setup)
    info = install_baseline_clean(cfg)
    _assert_product(info, expected, cfg.display_name_contains)
    _assert_about_version(cfg, expected)


@pytest.mark.install
@pytest.mark.destructive
def test_p0_02_upgrade_latest(cfg):
    """P0-02 基线升级最新包：升级后注册表与「关于」页版本等于最新包"""
    if not is_admin():
        pytest.fail("P0-02 需要管理员权限运行 pytest")
    _require_setup(cfg, cfg.latest_path)
    expected = expected_version_from_setup_name(cfg.latest_setup)
    info = upgrade_to_latest(cfg)
    _assert_product(info, expected, cfg.display_name_contains)
    current = read_installed()
    _assert_product(current, expected, cfg.display_name_contains)
    _assert_about_version(cfg, expected)
