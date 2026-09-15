from __future__ import annotations

import pytest

from hall_auto.launch import start_fresh, wait_main_window, wait_until_ready
from hall_auto.product import stop_main_process
from hall_auto.settings import (
    DARK_LUMINANCE_MAX,
    THEME_AIDS,
    ensure_settings,
    main_luminance,
    open_settings,
    set_theme,
    theme_nodes,
    theme_selected,
)


class Session:
    """重启后 main/pid 会换，用例改这里的引用，收尾还原才拿得到活的句柄。"""

    def __init__(self, main):
        self.main = main
        self.pid = main.element_info.process_id
        self.original: str | None = None

    def restart(self, cfg) -> None:
        stop_main_process(cfg.timeouts.process_stop_sec)
        app = start_fresh(cfg)
        self.main = wait_main_window(app, cfg)
        wait_until_ready(cfg, self.main)
        self.pid = open_settings(self.main)


@pytest.fixture(scope="module")
def session(cfg):
    app = start_fresh(cfg)
    main = wait_main_window(app, cfg)
    wait_until_ready(cfg, main)
    holder = Session(main)
    holder.pid = open_settings(main)
    holder.original = theme_selected(holder.pid)
    yield holder
    try:
        if holder.original:
            set_theme(ensure_settings(holder.main), holder.original)
    finally:
        stop_main_process(cfg.timeouts.process_stop_sec)


@pytest.mark.settings
def test_theme_controls_present(session):
    nodes = theme_nodes(session.pid)
    assert set(nodes) == set(THEME_AIDS), f"主题三档没找齐：{sorted(nodes)}"
    assert theme_selected(session.pid) in THEME_AIDS


@pytest.mark.settings
def test_theme_switch_applies_and_survives_restart(cfg, session):
    set_theme(session.pid, "深色")
    assert theme_selected(session.pid) == "深色"
    dark = main_luminance(session.main)
    assert dark < DARK_LUMINANCE_MAX, f"选了深色但主窗口还是亮的：亮度 {dark:.1f}"

    session.restart(cfg)
    assert theme_selected(session.pid) == "深色", "重启后主题没保持住"
    assert main_luminance(session.main) < DARK_LUMINANCE_MAX, "重启后外观没跟着回到深色"

    set_theme(session.pid, "浅色")
    light = main_luminance(session.main)
    assert theme_selected(session.pid) == "浅色"
    assert light > DARK_LUMINANCE_MAX, f"选了浅色但主窗口还是暗的：亮度 {light:.1f}"
    assert light - dark > 50, f"深浅色外观差别太小，怀疑没真换肤：浅 {light:.1f} / 深 {dark:.1f}"
