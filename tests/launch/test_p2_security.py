"""P2 安全验证：组长用例表（1.6.11.2S sheet）「安全验证」板块的四个固定项。

用例对照：数字签名 → test_package_all_signed；数字签名-个人空间盘 →
test_personal_storage_all_signed；控制面板信息检查 → test_control_panel_info；
签章检查报告 → test_signcheck_report_no_ecc。

不改机器状态：只解包/镜像到 reports/_secwork 工作目录，不点大厅、不装卸任何东西。
逆向拦截 / 升级验证 / 清单对比几条依赖服务端升级包或缺材料，见覆盖对照表，不在这里写。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hall_auto import security
from hall_auto.config import load_config
from hall_auto.product import read_installed

pytestmark = pytest.mark.security


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def extracted_package(cfg):
    if not cfg.security.configured:
        pytest.skip("security.tools_dir 未配置（内部工具不进仓库）")
    dest = security.work_root(cfg) / f"extract_{cfg.security_package_path.stem}"
    if not (dest.exists() and any(dest.iterdir())):
        security.extract_package(cfg, dest)
    return dest


def test_package_all_signed(cfg, extracted_package):
    result = security.run_checkappv(extracted_package, cfg)
    assert result.unsigned_count == 0, f"安装包存在未签名文件: {result.unsigned_files}"


def test_personal_storage_all_signed(cfg):
    if not cfg.security.configured:
        pytest.skip("security.tools_dir 未配置（内部工具不进仓库）")
    mirror = security.work_root(cfg) / "ps_mirror"
    count = security.mirror_signable_files(Path(cfg.security.personal_storage_dir), mirror)
    assert count > 0, f"个人空间盘目录没有可检文件: {cfg.security.personal_storage_dir}"
    result = security.run_checkappv(mirror, cfg)
    assert result.unsigned_count == 0, f"个人空间盘存在未签名文件: {result.unsigned_files}"


def test_control_panel_info(cfg):
    entries = security.control_panel_entries()
    hall = security.hall_entries(entries, cfg.display_name_contains)
    assert hall, "控制面板里没有大厅条目"
    publisher = security.expected_publisher()
    wrong = [f"{e['key']}={e['Publisher']!r}" for e in hall if (e.get("Publisher") or "") != publisher]
    assert not wrong, f"发布者不是{publisher}: {wrong}"
    installed = read_installed()
    if installed is not None:
        versions = {e.get("DisplayVersion") for e in hall}
        assert installed.display_version in versions, f"控制面板版本 {versions} 与已装 {installed.display_version} 不一致"
    space_disk = [e["key"] for e in entries if "个人空间盘" in (e.get("DisplayName") or "")]
    assert not space_disk, f"控制面板不应出现个人空间盘信息: {space_disk}"


def test_signcheck_report_no_ecc(cfg, extracted_package):
    report = security.run_signcheck(extracted_package, cfg)
    bad = security.signcheck_ecc_rows(report)
    assert not bad, f"签章检查报告存在 ECC/ECDSA: {bad}"
