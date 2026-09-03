from __future__ import annotations

import os

import pytest

from hall_auto.config import load_config


def pytest_addoption(parser):
    parser.addoption(
        "--allow-install",
        action="store_true",
        default=False,
        help="允许卸载/安装华硕大厅（P0-01/02）。也可设环境变量 HALL_ALLOW_INSTALL=1",
    )


def _allow_install(config) -> bool:
    return bool(config.getoption("--allow-install")) or os.environ.get("HALL_ALLOW_INSTALL") == "1"


def pytest_collection_modifyitems(config, items):
    if _allow_install(config):
        return
    skip = pytest.mark.skip(reason="破坏性安装未开启：加 --allow-install 或 HALL_ALLOW_INSTALL=1")
    for item in items:
        if item.get_closest_marker("destructive"):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def cfg():
    return load_config()


@pytest.fixture(scope="session")
def allow_install(request) -> bool:
    return _allow_install(request.config)
