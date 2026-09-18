from __future__ import annotations

import pytest

from hall_auto.about import read_about_version
from hall_auto.installer import (
    InstallError,
    install_baseline_clean,
    resolve_setup_full,
    upgrade_to_latest,
)
from hall_auto.product import is_admin, read_installed
from hall_auto.version import expected_version_from_setup_name, versions_equal


def _require_setup(cfg, filename: str):
    """确保安装包在手：本地优先，缺失则下载。返回 (路径, 获取结果)。

    以前是「找不到就 fail」，多机跑批时机器上没有人拷包，改成能自己拿。
    """
    if not cfg.installer_dir.is_dir():
        cfg.installer_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = resolve_setup_full(cfg, filename)
    except InstallError as exc:
        pytest.fail(str(exc))
    assert result.path.is_file(), f"取到的包不存在: {result.path}"
    return result.path, result


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
    _, fetched = _require_setup(cfg, cfg.baseline_setup)
    assert fetched.source in ("local", "cache", "download"), fetched.source
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
    _, fetched = _require_setup(cfg, cfg.latest_setup)
    assert fetched.source in ("local", "cache", "download"), fetched.source
    expected = expected_version_from_setup_name(cfg.latest_setup)
    info = upgrade_to_latest(cfg)
    _assert_product(info, expected, cfg.display_name_contains)
    current = read_installed()
    _assert_product(current, expected, cfg.display_name_contains)
    _assert_about_version(cfg, expected)
