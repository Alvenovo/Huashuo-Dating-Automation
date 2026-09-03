from __future__ import annotations

import pytest

from hall_auto.launch import LaunchError, launch_until_ready
from hall_auto.product import read_installed, stop_main_process


pytestmark = [pytest.mark.launch, pytest.mark.smoke]


@pytest.fixture
def installed():
    info = read_installed()
    if info is None or not info.exe_path.is_file():
        pytest.skip("本机未安装华硕大厅")
    return info


def test_p0_03_launch_window(cfg, installed):
    result = launch_until_ready(cfg)
    assert cfg.display_name_contains in result.title
    for aid in cfg.launch.required_auto_ids:
        assert aid in result.structure_ids


def test_p0_04_ready_cold_starts(cfg, installed):
    seen: list[tuple[str, ...]] = []
    errors: list[str] = []
    try:
        for index in range(cfg.launch.cold_starts):
            try:
                result = launch_until_ready(cfg)
            except LaunchError as exc:
                errors.append(f"cold start {index + 1}: {exc}")
                continue
            assert cfg.display_name_contains in result.title
            seen.append(result.structure_ids)
        if errors:
            pytest.fail(" ; ".join(errors))
        assert len(seen) == cfg.launch.cold_starts
        assert len(set(seen)) == 1, f"三次启动结构不一致: {seen}"
    finally:
        stop_main_process(cfg.timeouts.process_stop_sec)
