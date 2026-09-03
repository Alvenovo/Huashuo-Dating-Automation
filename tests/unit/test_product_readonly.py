from __future__ import annotations

import pytest

from hall_auto.product import (
    CRITICAL_PROCESS_NAMES,
    HELPER_PROCESS_NAMES,
    read_installed,
)
from hall_auto.version import versions_equal


@pytest.mark.unit
def test_appstoreserver_is_helper_not_critical():
    assert "AppStoreServer" in HELPER_PROCESS_NAMES
    assert "AppStoreServer" not in CRITICAL_PROCESS_NAMES
    assert "AsusMemberCenter" in CRITICAL_PROCESS_NAMES


@pytest.mark.unit
def test_read_installed_if_present():
    info = read_installed()
    if info is None:
        pytest.skip("本机未安装华硕大厅")
    assert info.display_name
    assert info.display_version
    assert info.exe_path.name == "AsusMemberCenter.exe"
    if info.file_version:
        assert versions_equal(info.file_version, info.display_version)
