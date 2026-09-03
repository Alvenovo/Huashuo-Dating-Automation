from __future__ import annotations

from pathlib import Path

import pytest

from hall_auto.config import load_config
from hall_auto.version import expected_version_from_setup_name, versions_equal


@pytest.mark.unit
@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("myappstore_1.6.8.17S_Setup.exe", "1.6.8.17"),
        ("myappstore_1.6.10.7S_Setup.exe", "1.6.10.7"),
        ("myappstore_1.6.7.3S_Setup.exe", "1.6.7.3"),
        ("myappstore_1.6.8.7S_Setup.exe", "1.6.8.7"),
        ("myappstore_1.6.9.7S_Setup.exe", "1.6.9.7"),
        ("myappstore_1.6.9.12S_Setup.exe", "1.6.9.12"),
        ("myappstore_1.6.8.17_Setup.exe", "1.6.8.17"),
        ("MYAPPSTORE_1.6.8.17s_SETUP.EXE", "1.6.8.17"),
        (r"C:\Users\admin\Desktop\Test\华硕大厅\myappstore_1.6.8.17S_Setup.exe", "1.6.8.17"),
    ],
)
def test_expected_version_from_setup_name(filename, expected):
    assert expected_version_from_setup_name(filename) == expected


@pytest.mark.unit
def test_reject_bad_setup_name():
    with pytest.raises(ValueError):
        expected_version_from_setup_name("Setup.exe")


@pytest.mark.unit
def test_versions_equal_strips():
    assert versions_equal("1.6.8.17", "1.6.8.17")
    assert versions_equal(" 1.6.8.17 ", "1.6.8.17")
    assert not versions_equal("1.6.8.17S", "1.6.8.17")
    assert not versions_equal(None, "1.6.8.17")


@pytest.mark.unit
def test_load_config_default_installer_dir():
    cfg = load_config()
    assert cfg.installer_dir == Path("C:/Users/admin/Desktop/Test/华硕大厅")
    assert expected_version_from_setup_name(cfg.baseline_setup) == "1.6.8.17"
    assert expected_version_from_setup_name(cfg.latest_setup) == "1.6.10.7"
