from __future__ import annotations

import pytest

from hall_auto.launch import main_window, start_fresh, wait_until_ready
from hall_auto.product import stop_main_process
from hall_auto.search import open_detail_until_ready, search_hits


pytestmark = [pytest.mark.launch, pytest.mark.smoke]


@pytest.fixture(scope="module")
def ready_main(cfg):
    app = start_fresh(cfg)
    main = main_window(app, cfg.display_name_contains, cfg.timeouts.launch_sec)
    wait_until_ready(cfg, main)
    try:
        yield main
    finally:
        stop_main_process(cfg.timeouts.process_stop_sec)


@pytest.fixture(scope="module")
def hits(cfg, ready_main):
    return search_hits(cfg, ready_main)


def test_p0_06_search_lists_keyword_titles(cfg, hits):
    assert len(hits) >= cfg.search_min_hits, (
        f"搜索 {cfg.search_keyword!r} 命中标题 {len(hits)} 条，少于要求 {cfg.search_min_hits} 条"
    )


def test_p0_07_detail_shows_name_and_primary_button(cfg, ready_main, hits):
    if len(hits) < cfg.search_min_hits:
        pytest.skip("P0-06 未命中，按失败策略不做详情")
    detail = open_detail_until_ready(cfg, ready_main, hits[0])
    assert detail.title == hits[0]
    assert detail.primary_action
