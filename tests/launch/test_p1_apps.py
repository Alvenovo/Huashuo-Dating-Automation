from __future__ import annotations

import time

import pytest

from hall_auto.apps import (
    SYNC_ITEM_PLACEHOLDER,
    _by_aid,
    _leave_mine,
    app_list_state,
    dismiss_vendor_dialogs,
    drive_vendor_installer,
    install_fixture,
    installed_match,
    is_app_installed,
    open_mine,
    read_mine_lists,
    uninstall_entry,
    uninstall_fixture,
    update_item_button,
    wizard_snapshot,
    wizard_target_version,
)
from hall_auto.launch import (
    LaunchError,
    _press_button,
    concrete_main,
    start_fresh,
    wait_main_window,
    wait_until_ready,
)
from hall_auto.login import logged_in, login_with_password, logout
from hall_auto.product import _file_version, is_admin, stop_main_process
from hall_auto.version import four_segment
from hall_auto.winapi import occlusion_hint


@pytest.fixture(scope="module")
def ready_main(cfg):
    app = start_fresh(cfg)
    main = wait_main_window(app, cfg)
    wait_until_ready(cfg, main)
    yield main
    stop_main_process(cfg.timeouts.process_stop_sec)


@pytest.mark.apps
def test_mine_lists_refresh(ready_main):
    """三个页签都要能刷出数据；空要能区分「本来没有」和「接口失败」。"""
    states = read_mine_lists(ready_main)
    for tab, state in states.items():
        assert state.loaded, f"{tab} 列表没刷出来：容器和空态提示都没找到"

    uninstall = states["uninstall"].items
    matched = [name for name in uninstall if installed_match(name)]
    assert matched or not uninstall, (
        f"卸载列表和系统注册表对不上，怀疑接口失败而不是本来没有: {uninstall}"
    )


@pytest.mark.apps
@pytest.mark.destructive
def test_fixture_install_then_uninstall(cfg, ready_main):
    """P1-A 夹具真装真卸：大厅装完注册表有条目、卸完条目消失"""
    app_name = cfg.fixture_apps.install or cfg.fixture_apps.uninstall
    if not app_name:
        pytest.skip("没配夹具应用：config.local.yaml 的 fixture_apps.install")
    if is_app_installed(app_name):
        pytest.skip(f"{app_name} 已在机器上，先手工卸掉再跑真装真卸")

    matched = install_fixture(cfg, ready_main, app_name)
    assert matched, f"装完注册表里找不到 {app_name}"
    assert is_app_installed(app_name)

    uninstall_fixture(cfg, ready_main, cfg.fixture_apps.uninstall or app_name)
    assert not is_app_installed(app_name), f"卸完 {app_name} 还在注册表里"


@pytest.mark.apps
@pytest.mark.destructive
def test_update_fixture_via_hall(cfg, ready_main):
    """从钉住的老版本点大厅「更新」，版本要升到向导标题声明的目标版本。

    起点由 tools/reset_fixture.py 压回 pinned_version；目标版本运行时从厂商向导标题读，
    不写死版本号（7-Zip 实测弹英文 NSIS「7-Zip 26.03 (x64) Setup」）。
    """
    uf = cfg.update_fixture
    if not uf.configured:
        pytest.skip("没配更新夹具：config 的 update_fixture 块")
    if not is_admin():
        raise LaunchError(
            "P1-A：更新要写 Program Files 和 HKLM，必须提权跑；"
            "用 MSYS_NO_PATHCONV=1 schtasks /Run /TN HallAutoP1 或管理员终端跑 run_p1_apps.ps1"
        )
    entry = uninstall_entry(uf.registry_hint)
    if entry is None or entry["version"] != uf.pinned_version:
        pytest.skip(f"起点不是钉住版本 {uf.pinned_version}（当前 {entry}），先提权跑 tools/reset_fixture.py")

    button = update_item_button(ready_main, uf.name)
    assert button is not None, f"更新列表里找不到 {uf.name} 的更新按钮（退出「我的」重进也没刷出来）"
    assert _press_button(button), f"点不动 {uf.name} 的更新按钮"

    deadline = time.time() + cfg.timeouts.install_sec
    target = None
    updated = None
    while time.time() < deadline:
        target = target or wizard_target_version()
        drive_vendor_installer()
        entry = uninstall_entry(uf.registry_hint)
        if entry and entry["version"] != uf.pinned_version:
            updated = entry
            break
        time.sleep(1)
    dismiss_vendor_dialogs()
    assert updated is not None, (
        f"{uf.name} 更新超时，注册表版本没变；向导目标版本 {target}；当前向导 {wizard_snapshot()}"
    )
    assert target is not None, f"全程没见到厂商向导标题，拿不到目标版本；注册表 {updated}"
    assert updated["version"] == target, (
        f"注册表版本 {updated['version']} 和向导标题声明的目标 {target} 不一致"
    )
    file_version = _file_version(cfg.update_version_exe)
    assert file_version == four_segment(target), (
        f"{cfg.update_version_exe} 的 FileVersion {file_version} != 目标版本四段形式 {four_segment(target)}"
    )

    _leave_mine(ready_main)
    open_mine(ready_main)
    state = app_list_state(ready_main, "update")
    assert uf.name not in state.items, f"更新完 {uf.name} 还在更新列表里: {list(state.items)}"


@pytest.mark.apps
@pytest.mark.login
def _occlusion_suffix(pid: int) -> str:
    """把「大厅被谁挡住了」拼进失败信息；没被挡返回空串。

    2026-09-23 真机：这条断言超时 45s，而失败截图里**大厅一个像素都没露**（微信在前台）。
    大厅主内容区是 WebView2，**被遮挡时 Chromium 会暂停渲染** → 页面永远刷不出来，
    而 Win32 控件层照样读得到。原来只能靠人逐张看 PNG 截图才能定性，
    现在「谁挡着」直接写在报错里。
    """
    hint = occlusion_hint(pid)
    return f"\n  ⚠️ {hint}" if hint else ""


def test_sync_list_logged_in(cfg):
    """登录后同步列表能刷出「其他电脑已装应用」。只读断言：
    页底「全部安装」（InstallAllBtn）和条目安装会真装 QQ/网盘这类日常软件，绝不点。
    自带起停（start_fresh 会顶掉 ready_main 的进程），所以放在本模块最后。"""
    user, password = cfg.test_account()
    if not user or not password:
        pytest.skip("同步列表要登录：设 HALL_TEST_USER / HALL_TEST_PASSWORD")
    app = start_fresh(cfg)
    main = wait_main_window(app, cfg)
    wait_until_ready(cfg, main)
    pid = main.element_info.process_id
    try:
        if logged_in(pid):
            logout(pid)
        login_with_password(cfg, pid)
        # 登录后主窗口 UIA Name 变「华硕应用商店」，按标题懒解析的 spec 失效，按 hwnd 重包
        logged_main = concrete_main(pid, cfg.display_name_contains)
        open_mine(logged_main)
        state = app_list_state(logged_main, "sync")
        assert state.loaded and not state.needs_login, (
            "登录后同步页仍没刷出列表（NoAppListBox 没出现）"
            f"（loaded={state.loaded}, needs_login={state.needs_login}, items={len(state.items)}）"
            + _occlusion_suffix(pid)
        )
        assert state.items, "同步列表为空：该账号在其他电脑没有安装记录，或条目名没读出来"
        assert all(name != SYNC_ITEM_PLACEHOLDER for name in state.items), (
            f"条目名读到的还是占位内部类名: {list(state.items)}"
        )
        assert _by_aid(logged_main, "Button", "InstallAllBtn") is not None, (
            "同步页没找到「全部安装」按钮（只断言存在，不点）"
        )
    finally:
        try:
            logout(pid)
        except Exception:
            pass
        stop_main_process(cfg.timeouts.process_stop_sec)
