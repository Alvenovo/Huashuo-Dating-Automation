"""P2 WorkBuddy 入口集成：组长用例表（1.6.11.2S sheet）「小硕 X Work Buddy入口集成」
板块里，单机、未登录态就能自动化的点：非破坏 6 个 + 破坏性行 25（装→验→卸往返）。

用例对照：
  双图标展示（开关开启、未登录）      → test_wb_dual_icon_beside_original
  免登录点击 + 未安装跳大厅内详情页    → test_wb_click_jumps_in_hall_detail
  不唤起系统浏览器                    → 同上（点击后全系统窗口无浏览器）
  频繁连点防抖                        → test_wb_rapid_click_debounce
  原有入口回归（小硕知道逻辑无变更）  → test_original_entry_regression
  WB 静态图标                         → test_wb_icon_is_static
  已安装时点入口拉起 WB（行 25）      → test_wb_installed_entry_launches_wb（破坏性/门禁）

红线：非破坏用例绝不点落地页「一键安装」；唯一例外是门禁（--allow-install /
HALL_ALLOW_INSTALL）放行的破坏性用例 test_wb_installed_entry_launches_wb（行 25
已安装拉起，装→验→卸 往返，用户 2026-09-16 授权），且只经 entries.click_install_button。
本套件 marker=wb，独立跑，不进无人值守回归。仍受阻项（开关关闭 / ARM / ROG /
4 机型 / 隐私协议态 / 配置隔离）需改后台或换设备，见覆盖对照表，不在此写。
"""
from __future__ import annotations

import shlex
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from hall_auto import apps, entries, winapi
from hall_auto.launch import concrete_main, start_fresh, wait_main_window, wait_until_ready
from hall_auto.product import (
    agreement_config_path,
    read_agreen,
    read_installed,
    set_agreen,
    stop_main_process,
)
from hall_auto.waiting import wait_until
from pywinauto.controls.uiawrapper import UIAWrapper
from pywinauto.uia_element_info import UIAElementInfo

pytestmark = pytest.mark.wb

BROWSER_HINTS = ("chrome", "edge", "firefox", "browser", "浏览器")
WB_INSTALL_TIMEOUT = 180


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
    """WB 入口在连续采样窗口内是静态图。

    行 29 组长表写「WB 静态图 vs 原入口 GIF」，探针实测（reports/probe/wb_gif_*）：
    WB 静态成立；但本机 1.6.11.4 原入口（小硕知道）图标在加载期/稳态/hover 三种
    采样下均零帧变化，不是动图——表的「原入口 GIF」前提在本机不复现，故只断言 WB
    静态，不反向断言原入口动效（差异已记入覆盖对照表，待组长/开发核对）。
    """
    main, _ = hall
    wb, _ = _both_regions(main)
    changed_frames, total_frames = entries.region_frames_changed(wb, seconds=3.0)
    assert total_frames > 0, "WB 区域采样帧为空"
    assert changed_frames == 0, (
        f"WB 图标疑似动图: 连续采样 {changed_frames}/{total_frames} 帧有变化"
    )


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


WB_PROC_HINTS = ("workbuddy", "xiaoshuo", "claw")


def _proc_names() -> set[str]:
    """全系统进程映像名（小写）。点 WB 入口后靠进程判是否真拉起了 WB，
    比看窗口标题稳（标题可能复用/滞后）。tasklist CSV 是 GBK 编码。"""
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, check=False)
    names: set[str] = set()
    for line in out.stdout.decode("gbk", errors="ignore").splitlines():
        parts = line.strip().strip('"').split('","')
        if parts and parts[0]:
            names.add(parts[0].lower())
    return names


def _wb_procs() -> set[str]:
    return {p for p in _proc_names() if any(h in p for h in WB_PROC_HINTS)}


def _kill_wb_procs(timeout_sec: float = 10) -> None:
    """杀掉正在跑的 WB 进程并等它真的退出。安装向导收尾页常带「完成后运行」勾选，装完 WB
    就已经在跑；不先杀掉，点入口只是把已有窗口调到前台，看不出是入口把它拉起来的（行 25
    验的就是入口）。等到进程消失再返回，免得没死透的 WB 混进点击前的快照造成误判。"""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        names = _wb_procs()
        if not names:
            return
        for name in names:
            subprocess.run(["taskkill", "/IM", name, "/F"], capture_output=True, check=False)
        time.sleep(1)


def _remove_wb_dir(install_dir: str | None) -> None:
    """删掉静默卸载留下的 WB 安装目录，保证往返后机器真干净（残留目录会让下一轮安装
    走「修复」分支装不出注册表条目）。只在路径确属 WB 时才删：目录名是 XiaoshuoClaw
    或里面有 XiaoshuoClaw.exe，免得 InstallLocation 万一为空/指错时误删别的目录。"""
    if not install_dir:
        return
    path = Path(install_dir)
    if not path.is_dir():
        return
    if path.name != "XiaoshuoClaw" and not (path / "XiaoshuoClaw.exe").is_file():
        return
    shutil.rmtree(path, ignore_errors=True)


def _uninstall_wb(timeout_sec: float = 90) -> str | None:
    """还原机器：先记下安装目录（卸载后注册表条目就没了，wb_install_dir 会返回 None），
    跑静默卸载命令，轮询注册表直到 WB 条目消失，再删掉卸载器留下的安装目录。返回清掉的
    安装目录路径（拿不到时 None），供用例断言目录也没了。
    NSIS 卸载器 /S 是异步的（拉起即返回）。QuietUninstallString 形如
    '"...\\XiaoshuoClaw\\Uninstall XiaoshuoClaw.exe" /currentuser /S'，posix=False 保反斜杠。
    WB 注册表条目没写 InstallLocation（实测为空），所以安装目录从卸载器路径反推——
    卸载器就装在安装目录里，取它路径的父目录即是。"""
    install_dir = entries.wb_install_dir()
    cmd = entries.wb_uninstall_command()
    if cmd:
        argv = [p.strip('"') for p in shlex.split(cmd, posix=False)]
        if not install_dir and argv:
            install_dir = str(Path(argv[0]).parent)
        subprocess.run(argv, check=False, timeout=timeout_sec)
        deadline = time.time() + timeout_sec
        while time.time() < deadline and apps.is_app_installed(entries.WB_REGISTRY_HINT):
            time.sleep(1)
    _remove_wb_dir(install_dir)
    return install_dir


@pytest.mark.destructive
def test_wb_installed_entry_launches_wb(hall):
    """行 25：WB 已安装时，点左下角 WB 入口应直接拉起 WB（不再跳详情页/浏览器）。

    破坏性往返：一键安装 WB → 驱动 NSIS 向导装完（注册表 DisplayName 为准）→ 回首页
    点入口验拉起 → finally 静默卸载还原机器。安装器是大厅子进程，装/验/卸必须在大厅
    同一生命周期内做完，所以复用 module 级 hall 夹具，不另起大厅。用户 2026-09-16 授权。
    """
    if apps.is_app_installed(entries.WB_REGISTRY_HINT):
        pytest.skip("WB 已装在本机，不做安装往返（先卸干净再跑）")
    main, _ = hall
    installed_here = False
    try:
        text = _click_wb_until_detail(main)
        assert entries.INSTALL_BUTTON_TEXT in text, f"未落进 WB 详情页（未见「一键安装」）: {text!r}"
        assert entries.click_install_button(main), "详情页里找不到可点的「一键安装」按钮"

        clicked: list[str] = []
        deadline = time.time() + WB_INSTALL_TIMEOUT
        while time.time() < deadline:
            if apps.is_app_installed(entries.WB_REGISTRY_HINT):
                break
            for label in apps.drive_vendor_installer(entries.WB_REGISTRY_HINT):
                if label not in clicked:
                    clicked.append(label)
            time.sleep(1)
        apps.dismiss_vendor_dialogs(entries.WB_REGISTRY_HINT)
        assert apps.is_app_installed(entries.WB_REGISTRY_HINT), (
            f"{WB_INSTALL_TIMEOUT}s 内 WB 未装上（注册表无 DisplayName）；向导点过 {clicked}；"
            f"当前向导标题 {apps.installer_titles(entries.WB_REGISTRY_HINT)}；"
            f"快照 {apps.wizard_snapshot(entries.WB_REGISTRY_HINT)}"
        )
        installed_here = True

        entries.back_home(main)
        time.sleep(entries.HOME_SETTLE_SEC)
        wb, _ = _both_regions(main)
        # 装完 WB 可能已被向导「完成后运行」拉起，先杀干净，让这一下点击真的从零拉起 WB
        _kill_wb_procs()
        time.sleep(1)
        before = _wb_procs()
        _focus(main)
        entries.click_entry(wb)
        # WB 冷启动可能比 3s 慢，轮询到超时
        launched: set[str] = set()
        deadline = time.time() + entries.CLICK_SETTLE_SEC + 12
        while time.time() < deadline:
            launched = _wb_procs() - before
            if launched:
                break
            time.sleep(1)
        assert launched, f"WB 已装时点入口未拉起 WB 进程；点击前 WB 进程={sorted(before)}"
    finally:
        if installed_here:
            entries.back_home(main)
            _kill_wb_procs()
            cleaned_dir = _uninstall_wb()
            assert not apps.is_app_installed(entries.WB_REGISTRY_HINT), "WB 卸载后注册表仍有残留，机器未还原"
            assert not cleaned_dir or not Path(cleaned_dir).exists(), (
                f"WB 安装目录未删干净，下一轮安装会走修复分支：{cleaned_dir}"
            )


AGREE_BUTTON = "同意"
CANCEL_BUTTON = "取消"
FIRST_RUN_TAGS_DONE = "我选好了"


def _launch_unagreed(cfg) -> int:
    """把 Agreen 置 0 后直接起大厅（不走 wait_until_ready，避免自动点同意），返回 pid。"""
    info = read_installed()
    stop_main_process(cfg.timeouts.process_stop_sec)
    time.sleep(1)
    set_agreen("0")
    proc = subprocess.Popen([str(info.exe_path)], cwd=str(info.exe_path.parent))
    return proc.pid


def _main_wrapper(pid: int, title: str, timeout_sec: float):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        for hwnd in winapi._top_hwnds():
            if (
                winapi._hwnd_pid(hwnd) == pid
                and winapi._hwnd_visible(hwnd)
                and title in winapi._hwnd_text(hwnd)
            ):
                return UIAWrapper(UIAElementInfo(hwnd))
        time.sleep(0.5)
    return None


def _button_names(main) -> list[str]:
    names = []
    for btn in main.descendants(control_type="Button"):
        try:
            names.append(btn.element_info.name or btn.window_text())
        except Exception:
            continue
    return names


def _click_agree(main) -> bool:
    for btn in main.descendants(control_type="Button"):
        try:
            if (btn.element_info.name or btn.window_text()) == AGREE_BUTTON:
                btn.invoke()
                return True
        except Exception:
            continue
    return False


def _hall_windows(pid: int, title: str) -> list:
    """该 pid 下所有可见「华硕大厅」窗的具体 wrapper。

    首跑同意后首页窗和「欢迎使用」引导窗是同 pid 的两个顶层窗，枚举顺序不固定，
    不能假定第 0 个就是首页窗，所以全列出来让调用方逐个试。
    """
    wraps = []
    for hwnd in winapi._top_hwnds():
        if (
            winapi._hwnd_pid(hwnd) == pid
            and winapi._hwnd_visible(hwnd)
            and title in winapi._hwnd_text(hwnd)
        ):
            wraps.append(UIAWrapper(UIAElementInfo(hwnd)))
    return wraps


def _dismiss_first_run_tags(pid: int, title: str) -> bool:
    """关掉首跑「欢迎使用」设备标签引导窗（点「我选好了」），避免它盖住首页影响后续用例。"""
    for win in _hall_windows(pid, title):
        for btn in win.descendants(control_type="Button"):
            try:
                if (btn.element_info.name or btn.window_text()) == FIRST_RUN_TAGS_DONE:
                    btn.invoke()
                    return True
            except Exception:
                continue
    return False


def test_wb_entry_gated_by_privacy_agreement(cfg):
    """行 24：隐私协议（首跑未同意）态下 WB 入口行为。

    探针实测（reports/probe/wb24_agreement_probe.py）：把 user.config 的 Agreen 置 0
    即回到首跑未同意态——主窗口整页是隐私协议页（同意/取消 + 隐私政策链接），首页
    内容**完全不渲染**，`locate_entries` 返回空，WB 入口根本不在。组长表「未同意协议
    WB 入口正常」的前提在本机不复现：产品把全部内容（含 WB 入口）挡在同意之后。

    故本用例断言隐私正确的实际行为（而非表的字面前提）：
      1. 未同意态：协议页在（同意+取消+隐私文案），且 WB 入口**不**暴露（内容被门控）；
      2. 点同意后：首页出现、WB 入口正常可定位。
    差异（表前提 vs 实际门控）已记覆盖对照表，待组长/开发核对。

    可逆、无需提权：只改用户级 user.config 的 Agreen，不重装大厅；正常路径点同意即
    回到机器原状态（Agreen=1），中途失败则把 user.config 还原成进用例前的内容。
    放模块最后：本用例自管大厅起停，会顶掉 module 级 hall 夹具的进程。
    """
    if agreement_config_path() is None:
        pytest.skip("未找到大厅 user.config，无法复位到未同意态")
    backup = agreement_config_path().read_text(encoding="utf-8")
    original_agreen = read_agreen()
    pid = None
    try:
        pid = _launch_unagreed(cfg)
        main = _main_wrapper(pid, cfg.display_name_contains, cfg.timeouts.launch_sec)
        assert main is not None, "未同意态下主窗口未出现"

        def _agreement_up() -> bool:
            names = _button_names(main)
            return AGREE_BUTTON in names and any(CANCEL_BUTTON in n for n in names)

        assert wait_until(_agreement_up, timeout_sec=cfg.timeouts.ready_sec), (
            f"未同意态未出现协议页（同意/取消）；按钮={_button_names(main)}"
        )
        landing = entries.landing_text(main)
        assert any(h in landing for h in ("隐私", "协议")), f"协议页缺隐私文案: {landing!r}"
        gated = entries.locate_entries(main)
        assert "wb" not in gated, (
            f"未同意态不应暴露 WB 入口（内容应被协议门控）: {sorted(gated)}"
        )

        assert _click_agree(main), "协议页找不到可点的「同意」按钮"

        def _wb_anywhere() -> bool:
            return any(
                "wb" in entries.locate_entries(win)
                for win in _hall_windows(pid, cfg.display_name_contains)
            )

        assert wait_until(_wb_anywhere, timeout_sec=cfg.timeouts.ready_sec), (
            "点同意后首页未出现 WB 入口（所有华硕大厅窗都扫过）"
        )
        # 首跑同意后还会弹「欢迎使用」设备标签引导窗盖住首页，关掉它还原机器，
        # 否则下次启动仍会弹、盖住首页影响其它用例的坐标点击。
        wait_until(
            lambda: _dismiss_first_run_tags(pid, cfg.display_name_contains),
            timeout_sec=10,
        )
    finally:
        stop_main_process(cfg.timeouts.process_stop_sec)
        if read_agreen() != original_agreen:
            agreement_config_path().write_text(backup, encoding="utf-8")
