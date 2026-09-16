"""P2 WorkBuddy 入口集成：组长用例表（1.6.11.2S sheet）「小硕 X Work Buddy入口集成」
板块里，单机、未登录态、非破坏就能自动化的 6 个点。

用例对照：
  双图标展示（开关开启、未登录）      → test_wb_dual_icon_beside_original
  免登录点击 + 未安装跳大厅内详情页    → test_wb_click_jumps_in_hall_detail
  不唤起系统浏览器                    → 同上（点击后全系统窗口无浏览器）
  频繁连点防抖                        → test_wb_rapid_click_debounce
  原有入口回归（小硕知道逻辑无变更）  → test_original_entry_regression
  WB 静态图标                         → test_wb_icon_is_static

红线：绝不点落地页「一键安装」（会真装 WorkBuddy）；本套件 marker=wb，独立跑，
不进无人值守回归。受阻项（开关关闭 / ARM / ROG / 4 机型 / 隐私协议态 / 配置隔离 /
已安装拉起）需改后台或换设备，见覆盖对照表，不在此写。
"""
from __future__ import annotations

import time

import pytest

from hall_auto import entries
from hall_auto.launch import concrete_main, start_fresh, wait_main_window, wait_until_ready
from hall_auto.product import read_installed, stop_main_process

pytestmark = pytest.mark.wb

BROWSER_HINTS = ("chrome", "edge", "firefox", "browser", "浏览器")


@pytest.fixture(scope="module")
def hall(cfg):
    info = read_installed()
    if info is None or not info.exe_path.is_file():
        pytest.skip("本机未安装华硕大厅")
    app = start_fresh(cfg)
    spec = wait_main_window(app, cfg)
    wait_until_ready(cfg, spec)
    pid = spec.process_id()
    # 点进 WB 详情页后主窗口 UIA Name 会变（同登录后变「华硕应用商店」），按标题懒解析的
    # WindowSpecification 会 ElementNotFoundError；换成按 Win32 标题+pid 绑定的具体 wrapper，
    # Win32 标题始终是「华硕大厅」，跨页面稳定。
    main = concrete_main(pid, cfg.display_name_contains)
    try:
        yield main, pid
    finally:
        stop_main_process(cfg.timeouts.process_stop_sec)


def _both_regions(main):
    regions = entries.locate_entries(main)
    assert "wb" in regions and "original" in regions, f"左下角入口未找全: {sorted(regions)}"
    return regions["wb"], regions["original"]


def _new_browser_windows(before: list[str], after: list[str]) -> list[str]:
    gained = [t for t in after if t not in before]
    return [t for t in gained if any(h in t.lower() for h in BROWSER_HINTS)]


def _focus(main) -> None:
    try:
        main.set_focus()
        time.sleep(0.5)
    except Exception:
        pass


def _click_wb_until_detail(main, tries: int = 3) -> str:
    """点 WB 入口直到落进详情页（以「一键安装」为标志）。

    点击是真实鼠标事件，偶发失手会停在首页（首页轮播里也有 WB 磁贴，光看
    「WorkBuddy」在不在分不出有没有跳），所以每次点前先抢焦点、点完验标志，
    没到就返回首页重点，重试耗尽返回最后一次落地文本让断言报错。
    """
    text = ""
    for _ in range(tries):
        wb, _ = _both_regions(main)
        _focus(main)
        entries.click_entry(wb)
        time.sleep(entries.CLICK_SETTLE_SEC)
        text = entries.landing_text(main)
        if entries.INSTALL_BUTTON_TEXT in text:
            return text
        entries.back_home(main)
        time.sleep(entries.HOME_SETTLE_SEC)
    return text


def test_wb_dual_icon_beside_original(hall):
    main, _ = hall
    wb, original = _both_regions(main)
    # 原有入口在左、WB 图标叠加在旁（右），两个点击热区相互独立不重叠
    assert wb.left >= original.left, f"WB({wb.left}) 应在原有入口({original.left}) 右侧"
    assert not (wb.left < original.right and original.left < wb.right), (
        f"两入口热区重叠: 原({original.left},{original.right}) WB({wb.left},{wb.right})"
    )


def test_wb_click_jumps_in_hall_detail(hall):
    main, pid = hall
    before = entries.all_window_titles()
    try:
        # 未安装 → 跳大厅内置应用市场的 WB 详情页，不唤起系统浏览器
        text = _click_wb_until_detail(main)
        browsers = _new_browser_windows(before, entries.all_window_titles())
        assert not browsers, f"点击 WB 疑似唤起系统浏览器: {browsers}"
        assert entries.INSTALL_BUTTON_TEXT in text, (
            f"点 WB 未落进大厅内详情页（未见「一键安装」）；大厅窗口={entries.process_window_titles(pid)}"
        )
    finally:
        entries.back_home(main)


def test_wb_rapid_click_debounce(hall):
    main, pid = hall
    wb, _ = _both_regions(main)
    before = entries.all_window_titles()
    try:
        _focus(main)
        for _ in range(5):
            entries.click_entry(wb)
            time.sleep(0.15)
        time.sleep(entries.CLICK_SETTLE_SEC)
        browsers = _new_browser_windows(before, entries.all_window_titles())
        assert not browsers, f"连点 5 次唤起多个/外部窗口: {browsers}"
        # 防连点：短时只响应首次，大厅名下仍是单一主窗，没叠出多个详情页/多次拉起
        titles = entries.process_window_titles(pid)
        assert len(titles) <= 1, f"连点后大厅名下窗口不止一个: {titles}"
    finally:
        entries.back_home(main)


def test_wb_icon_is_static(hall):
    main, _ = hall
    wb, _ = _both_regions(main)
    changed, total = entries.region_pixel_change(wb)
    ratio = changed / total if total else 0.0
    assert total > 0, "WB 区域截取为空"
    assert ratio < 0.02, f"WB 图标疑似动图: 变化像素 {changed}/{total} ({ratio:.3%})"


def test_original_entry_regression(hall):
    main, pid = hall
    _, original = _both_regions(main)
    try:
        _focus(main)
        entries.click_entry(original)
        time.sleep(entries.CLICK_SETTLE_SEC)
        # 未登录点原有入口（小硕知道）→ 拉起登录窗，与上线前逻辑一致
        titles = entries.process_window_titles(pid)
        assert any(entries.LOGIN_WINDOW_KEYWORD in t for t in titles), (
            f"点原有入口未拉起登录窗: {titles}"
        )
    finally:
        entries.close_window_by_keyword(pid, entries.LOGIN_WINDOW_KEYWORD)
        time.sleep(entries.HOME_SETTLE_SEC)
        entries.back_home(main)
