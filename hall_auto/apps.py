"""P1-A 应用生命周期。

实测「我的」页面结构：页签按钮 `UpTabBtn`/`UnTabBtn`/`NoTabBtn`，列表 `UpAppListBox`/`UnAppListBox`，
条目是 `ListItem`，条目内动作按钮 `UpBtn`（更新）/`UnstallBtn`（卸载）。同步页未登录时没有列表，
只有提示文案加 `LoginBtn`。

卸载列表里会出现微信、WPS、Python3 这类日常软件，所以执行类动作只允许打在配置的夹具应用上。
"""

from __future__ import annotations

import ctypes
import dataclasses
import re
import time
import winreg
from collections.abc import Iterable
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from hall_auto.config import Config
from hall_auto.launch import HOME_TAB_NAME, LaunchError, _press_button, popup_text
from hall_auto.product import is_admin
from hall_auto.search import open_detail_until_ready, search_hits
from hall_auto.waiting import wait_until, wait_until_or_raise
from hall_auto.winapi import (
    SMTO_ABORTIFHUNG,
    _child_hwnds,
    _hwnd_alive,
    _hwnd_class,
    _hwnd_pid,
    _hwnd_rect,
    _hwnd_text,
    _top_hwnds,
    _user32,
)

MINE_TAB_NAME = "我的"
MINE_AUTO_ID = "Mine"
TAB_AIDS = {"update": "UpTabBtn", "uninstall": "UnTabBtn", "sync": "NoTabBtn"}
LIST_AIDS = {"update": "UpAppListBox", "uninstall": "UnAppListBox"}
ITEM_ACTION_AIDS = {"update": "UpBtn", "uninstall": "UnstallBtn"}
SYNC_LOGIN_HINT = "登录后查看同账号在其他电脑已安装应用"
MINE_TIMEOUT_SEC = 20
LIST_TIMEOUT_SEC = 30


@dataclass(frozen=True)
class AppListState:
    tab: str
    loaded: bool
    needs_login: bool
    items: tuple[str, ...]


def _by_aid(main, control_type: str, aid: str):
    try:
        nodes = main.descendants(control_type=control_type)
    except Exception:
        return None
    for node in nodes:
        try:
            if (node.element_info.automation_id or "") == aid:
                return node
        except Exception:
            continue
    return None


def _by_name(main, control_type: str, name: str, exact: bool = False):
    try:
        nodes = main.descendants(control_type=control_type)
    except Exception:
        return None
    for node in nodes:
        try:
            text = popup_text(node.element_info.name or node.window_text())
        except Exception:
            continue
        if (text == name) if exact else (name in text):
            return node
    return None


def open_mine(main, timeout_sec: int = MINE_TIMEOUT_SEC) -> None:
    """进「我的」页面。侧栏条目是 ListItem，不是 Button。"""
    if _by_aid(main, "Custom", MINE_AUTO_ID) is not None:
        return
    tab = _by_name(main, "ListItem", MINE_TAB_NAME, exact=True)
    if tab is None or not _press_button(tab):
        raise LaunchError("P1-A：点不开侧栏「我的」")
    wait_until_or_raise(
        lambda: _by_aid(main, "Custom", MINE_AUTO_ID) is not None,
        "P1-A：「我的」页面没出现（找不到 aid=Mine）",
        timeout_sec=timeout_sec,
        interval=0.5,
    )


def switch_app_tab(main, tab: str) -> None:
    aid = TAB_AIDS[tab]
    button = _by_aid(main, "Button", aid)
    if button is None or not _press_button(button):
        raise LaunchError(f"P1-A：点不动页签 {tab}（aid={aid}）")
    list_aid = LIST_AIDS.get(tab)
    if list_aid:
        wait_until(lambda: _by_aid(main, "List", list_aid) is not None)
    else:
        # 同步页没有已知容器，等更新/卸载的容器消失即认为旧页签内容已让位，
        # 否则 _synced_item_names 会把上个页签的 ListItem 误当同步页数据。
        wait_until(lambda: all(_by_aid(main, "List", aid_) is None for aid_ in LIST_AIDS.values()))


def _item_names(container) -> list[str]:
    try:
        nodes = container.descendants(control_type="ListItem")
    except Exception:
        return []
    names: list[str] = []
    for node in nodes:
        try:
            name = popup_text(node.element_info.name or node.window_text())
        except Exception:
            continue
        if name and name not in names:
            names.append(name)
    return names


def app_list_state(main, tab: str, timeout_sec: int = LIST_TIMEOUT_SEC) -> AppListState:
    """读一个页签的列表状态。

    容器出现即算刷新成功（条目为空是「本来没有」，不算失败）；同步页未登录时没有容器，
    以登录提示文案为准。容器和提示都没有 → 判未刷新，交给调用方报错。
    """
    switch_app_tab(main, tab)
    list_aid = LIST_AIDS.get(tab)
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if list_aid:
            container = _by_aid(main, "List", list_aid)
            if container is not None:
                return AppListState(tab=tab, loaded=True, needs_login=False, items=tuple(_item_names(container)))
        else:
            names = _synced_item_names(main)
            if names is not None:
                return AppListState(tab=tab, loaded=True, needs_login=False, items=names)
            if _by_name(main, "Text", SYNC_LOGIN_HINT) is not None:
                return AppListState(tab=tab, loaded=True, needs_login=True, items=())
        time.sleep(0.5)
    return AppListState(tab=tab, loaded=False, needs_login=False, items=())


def _synced_item_names(main) -> list[str] | None:
    """同步页登录后的列表容器 aid 未知，只在「我的页面」容器里收 ListItem，避免把左侧分类栏算进来。"""
    container = _by_aid(main, "Custom", MINE_AUTO_ID)
    if container is None:
        return None
    names: list[str] = []
    try:
        nodes = container.descendants(control_type="ListItem")
    except Exception:
        return None
    for node in nodes:
        name = _safe_name(node)
        if name and name not in names:
            names.append(name)
    return names or None


def _safe_descendants(main, control_type: str) -> list:
    try:
        return main.descendants(control_type=control_type)
    except Exception:
        return []


def _safe_name(node) -> str:
    try:
        return popup_text(node.element_info.name or node.window_text())
    except Exception:
        return ""


def read_mine_lists(main) -> dict[str, AppListState]:
    open_mine(main)
    return {tab: app_list_state(main, tab) for tab in ("update", "uninstall", "sync")}


UNINSTALL_ROOTS = (
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
)


def installed_display_names() -> set[str]:
    """系统卸载注册表里的 DisplayName 集合，用来判夹具应用装没装，不看截图。"""
    names: set[str] = set()
    for hive, root in UNINSTALL_ROOTS:
        try:
            with winreg.OpenKey(hive, root) as key:
                count = winreg.QueryInfoKey(key)[0]
                for i in range(count):
                    try:
                        sub = winreg.EnumKey(key, i)
                        with winreg.OpenKey(key, sub) as item:
                            value, _ = winreg.QueryValueEx(item, "DisplayName")
                    except OSError:
                        continue
                    text = popup_text(str(value or ""))
                    if text:
                        names.add(text)
        except OSError:
            continue
    return names


def match_display_name(app_name: str, names: Iterable[str]) -> str | None:
    """精确命中优先，其次包含。商店名和注册表 DisplayName 常有版本号/后缀差异。"""
    target = popup_text(app_name).lower()
    if not target:
        return None
    listed = list(names)
    for name in listed:
        if popup_text(name).lower() == target:
            return name
    for name in listed:
        if target in popup_text(name).lower():
            return name
    return None


def installed_match(app_name: str) -> str | None:
    """商店里的应用名和注册表 DisplayName 不一定完全相等（常带版本号/后缀），先精确再包含。"""
    return match_display_name(app_name, installed_display_names())


def is_app_installed(app_name: str) -> bool:
    return installed_match(app_name) is not None


def uninstall_entry(app_hint: str) -> dict[str, str] | None:
    """按 DisplayName 包含关系读卸载注册表条目，更新夹具的断言和复位都用它。

    商店目录名（「7-Zip（64位）」）和注册表 DisplayName（「7-Zip 26.02 (x64)」）不是一回事，
    所以更新夹具单独配 registry_hint，不复用 match_display_name。
    """
    target = popup_text(app_hint).lower()
    if not target:
        return None
    for hive, root in UNINSTALL_ROOTS:
        try:
            with winreg.OpenKey(hive, root) as key:
                count = winreg.QueryInfoKey(key)[0]
                for i in range(count):
                    try:
                        sub = winreg.EnumKey(key, i)
                        with winreg.OpenKey(key, sub) as item:
                            def get(name: str) -> str:
                                try:
                                    return str(winreg.QueryValueEx(item, name)[0])
                                except OSError:
                                    return ""

                            name = get("DisplayName")
                            if not name or target not in name.lower():
                                continue
                            return {
                                "key": sub,
                                "name": name,
                                "version": get("DisplayVersion"),
                                "uninstall": get("UninstallString"),
                                "quiet": get("QuietUninstallString"),
                            }
                    except OSError:
                        continue
        except OSError:
            continue
    return None


def find_item_button(main, app_name: str, action_aid: str):
    """在列表条目里找动作按钮。只认名字完全相同的条目，避免误打日常软件。"""
    for node in _safe_descendants(main, "ListItem"):
        if _safe_name(node) != popup_text(app_name):
            continue
        try:
            buttons = node.descendants(control_type="Button")
        except Exception:
            continue
        for button in buttons:
            try:
                if (button.element_info.automation_id or "") == action_aid:
                    return button
            except Exception:
                continue
    return None


INSTALL_ACTIONS = ("一键安装", "安装", "立即下载", "下载")

STORE_DOWNLOAD_DIR = Path(r"C:\AsusMCenterDownload")
WIZARD_BUTTONS = (
    "同意并安装", "立即安装", "一键安装", "开始安装", "安装", "卸载", "下一步", "完成", "关闭",
    # 7-Zip 官方 NSIS 是英文界面（实测更新流程弹「7-Zip 26.03 (x64) Setup」）；
    # 放在中文后面，中文向导同时出现两种文字时仍优先中文。
    "Install", "Finish", "Close",
)
NEED_ADMIN_HINT = (
    "P1-A：夹具应用要写 Program Files 和 HKLM 卸载表，必须有管理员令牌，"
    "非提权跑到安装那一步一定失败（向导本身非提权是能读能点的）。"
    "请用提权方式跑 run_p1_apps.ps1：管理员终端粘贴一行，或用已建好的计划任务触发。"
)


def _process_image(pid) -> str:
    """进程映像路径。pid 为空、或目标进程提权而本进程没提权时读不到，返回空串。"""
    if not pid:
        return ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


WIZARD_TITLE_HINTS = ("安装", "卸载", "所需空间", "Setup", "Install", "Uninstall")
HALL_EXE_PREFIX = "asusmembercenter"
# NSIS 卸载器把自己解包到 %TEMP%\~nsuA.tmp\Au_.exe 再拉起，映像不在下载目录，
# 光按下载目录认会漏掉它（标题没有关键词的中间页也靠这条兜住）。
NSIS_TEMP_IMAGE = re.compile(r"[\\/]~ns[^\\/]*\.tmp[\\/]Au_\.exe$", re.IGNORECASE)
# NSIS 安装器的对话框类。绿色应用（Win截图）的映像同样落在下载目录里，
# 光按映像认会把它误当向导，所以还要求窗口是个真对话框。
DIALOG_CLASSES = ("#32770",)
# 自绘主按钮没有文字，只能按面积认：网易云的「立即安装」是 360x60=21600，
# 右上角关闭叉 36x36=1296，协议链接最大 144x25=3600，都远在门槛之下。
CTA_MIN_AREA = 12000

BM_CLICK = 0x00F5
MOUSE_LEFTDOWN = 0x0002
MOUSE_LEFTUP = 0x0004


_ACCELERATOR = re.compile(r"\(&\w\)")


def button_label(text: str) -> str:
    """去掉 Win32 的快捷键标记：'关闭(&L)' -> '关闭'。"""
    return popup_text(_ACCELERATOR.sub("", text)).replace("&", "").replace(" ", "")


def is_installer_dialog(hwnd: int, app_name: str = "") -> bool:
    """认厂商安装/卸载向导的对话框。

    两条判据取或：进程映像落在商店下载目录（或 NSIS 的临时解包目录）且窗口类是
    标准对话框；或标题是「应用名 + 安装卸载类关键词」。第二条不能省 —— 有的安装器
    会把自己解包到别处再拉起，映像路径就不在下载目录了。大厅自己的窗口一律排除。
    """
    if not _user32.IsWindowVisible(hwnd):
        return False
    title = popup_text(_hwnd_text(hwnd))
    if app_name and app_name in title and any(hint in title for hint in WIZARD_TITLE_HINTS):
        return True
    image = _process_image(_hwnd_pid(hwnd))
    if not image or Path(image).name.lower().startswith(HALL_EXE_PREFIX):
        return False
    if Path(image).parent != STORE_DOWNLOAD_DIR and not NSIS_TEMP_IMAGE.search(image):
        return False
    return _hwnd_class(hwnd) in DIALOG_CLASSES


def installer_dialogs(app_name: str = "") -> list[int]:
    return [hwnd for hwnd in _top_hwnds() if is_installer_dialog(hwnd, app_name)]


def installer_pids(app_name: str = "") -> set[int]:
    """厂商安装器进程的 pid。

    残留的安装器非提权杀不掉（taskkill 报拒绝访问），复位脚本要提权按这份清单清理。
    """
    return {_hwnd_pid(hwnd) for hwnd in installer_dialogs(app_name)}


@dataclass(frozen=True)
class WizardButton:
    hwnd: int
    text: str
    rect: tuple[int, int, int, int]
    visible: bool
    enabled: bool

    @property
    def area(self) -> int:
        return max(0, self.rect[2] - self.rect[0]) * max(0, self.rect[3] - self.rect[1])

    def describe(self) -> str:
        label = button_label(self.text)
        if label:
            return f"{label}@{self.rect}"
        kind = "自绘主按钮" if self.area >= CTA_MIN_AREA else "无文字小按钮"
        return f"{kind}@{self.rect}"


def wizard_buttons(hwnd: int) -> list[WizardButton]:
    """对话框里所有 Button 子控件，按面积从大到小。"""
    buttons: list[WizardButton] = []
    for child in _child_hwnds(hwnd):
        if _hwnd_class(child) != "Button":
            continue
        buttons.append(
            WizardButton(
                hwnd=child,
                text=popup_text(_hwnd_text(child)),
                rect=_hwnd_rect(child),
                visible=bool(_user32.IsWindowVisible(child)),
                enabled=bool(_user32.IsWindowEnabled(child)),
            )
        )
    buttons.sort(key=lambda b: b.area, reverse=True)
    return buttons


def pick_wizard_label(names: Iterable[str]) -> str | None:
    """按 WIZARD_BUTTONS 的顺序挑一个精确命中的按钮名；没命中返回 None（绝不点取消或捆绑推广）。"""
    present = {popup_text(name) for name in names}
    for label in WIZARD_BUTTONS:
        if label in present:
            return label
    return None


def pick_wizard_button(buttons: Iterable[WizardButton], drawn_ok: bool = True) -> WizardButton | None:
    """挑该点的那个按钮，认不出就返回 None（宁可不点，绝不点错）。

    顺序：可见且文字命中白名单 → 唯一一块可见的自绘主按钮 → 隐藏但文字命中白名单。

    最后一条是给换肤卸载器留的。「网易云音乐 卸载」页把真按钮画成两块同样大的
    自绘块，左「再想想」右「狠心卸载」，位置大小全一样，光看几何分不出谁是谁
    （左边那个是取消，猜错就把卸载撤了）。NSIS 自己的「卸载(&U)」还在树里，只是
    被皮肤藏起来了，BM_CLICK 对隐藏按钮照样有效，所以按文字点它。
    自绘块多于一块时一律不猜 —— 没有文字就没有可靠依据。
    """
    usable = [b for b in buttons if b.enabled and b.area > 0]
    labelled = [b for b in usable if b.visible and b.text]
    label = pick_wizard_label(button_label(b.text) for b in labelled)
    if label:
        return next(b for b in labelled if button_label(b.text) == label)
    drawn = [b for b in usable if b.visible and not b.text and b.area >= CTA_MIN_AREA]
    if drawn_ok and len(drawn) == 1:
        return drawn[0]
    hidden = [b for b in usable if not b.visible and b.text]
    label = pick_wizard_label(button_label(b.text) for b in hidden)
    if label:
        return next(b for b in hidden if button_label(b.text) == label)
    return None


def _click_hwnd(hwnd: int) -> None:
    result = ctypes.c_void_p()
    _user32.SendMessageTimeoutW(
        hwnd, BM_CLICK, 0, 0, SMTO_ABORTIFHUNG, 3000, ctypes.byref(result)
    )


def _mouse_click(rect: tuple[int, int, int, int]) -> None:
    _user32.SetCursorPos((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
    time.sleep(0.3)
    _user32.mouse_event(MOUSE_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.1)
    _user32.mouse_event(MOUSE_LEFTUP, 0, 0, 0, 0)


def _button_signature(hwnd: int) -> tuple:
    return tuple((b.text, b.rect, b.visible, b.enabled) for b in wizard_buttons(hwnd))


def drive_vendor_installer(app_name: str = "", drawn_ok: bool = True) -> list[str]:
    """点厂商向导里该点的按钮，返回点过的按钮描述。

    自绘主按钮可能不响应 BM_CLICK，所以点完对比一次按钮签名，
    没变化就再用真鼠标点同一个位置。
    """
    clicked: list[str] = []
    for hwnd in installer_dialogs(app_name):
        target = pick_wizard_button(wizard_buttons(hwnd), drawn_ok=drawn_ok)
        if target is None:
            continue
        before = _button_signature(hwnd)
        _click_hwnd(target.hwnd)

        def _advanced() -> bool:
            return not _hwnd_alive(hwnd) or _button_signature(hwnd) != before

        wait_until(_advanced, timeout_sec=2)
        # 鼠标兜底只对可见按钮有意义：隐藏按钮的 rect 是 NSIS 默认布局的位置，
        # 跟皮肤实际画出来的东西对不上，往那儿点是乱点。
        if target.visible and _hwnd_alive(hwnd) and _button_signature(hwnd) == before:
            _mouse_click(target.rect)
            wait_until(_advanced, timeout_sec=2)
        clicked.append(target.describe())
    return clicked


def dismiss_vendor_dialogs(app_name: str = "", rounds: int = 5) -> list[str]:
    """把厂商向导点到没有对话框剩下，返回点过的按钮描述。

    装完/卸完向导常常还挂在屏幕上：一是挡住大厅，二是下一轮 install_fixture 的
    残留前置检查会当成没收拾干净直接拒跑。
    只点有文字的按钮（关闭/完成）—— 收尾页那块自绘大按钮常是「立即体验」，
    点下去会把刚卸掉的应用又拉起来，污染机器状态。
    """
    clicked: list[str] = []
    for _ in range(rounds):
        if not installer_dialogs(app_name):
            break
        step = drive_vendor_installer(app_name, drawn_ok=False)
        clicked.extend(step)
        if not step:
            break
        wait_until(lambda: not installer_dialogs(app_name), timeout_sec=3, interval=0.5)
    return clicked


def wizard_snapshot(app_name: str = "") -> list[str]:
    """当前认出的厂商向导：标题 + 按钮清单。安装超时时带进报错，省一轮排查。"""
    snapshot: list[str] = []
    for hwnd in installer_dialogs(app_name):
        title = popup_text(_hwnd_text(hwnd))
        buttons = [b.describe() for b in wizard_buttons(hwnd)][:20]
        snapshot.append(f"hwnd={hwnd} title={title!r} rect={_hwnd_rect(hwnd)} buttons={buttons}")
    return snapshot


_VERSION_IN_TEXT = re.compile(r"\d+\.\d+(?:\.\d+)*")


def installer_titles(app_name: str = "") -> list[str]:
    return [popup_text(_hwnd_text(hwnd)) for hwnd in installer_dialogs(app_name)]


def wizard_target_version(app_name: str = "") -> str | None:
    """厂商向导标题里声明的目标版本，当「目录版本」的 oracle，避免把版本号写死。

    实测 7-Zip 更新弹「7-Zip 26.03 (x64) Setup」，标题里的点分数字就是目标版本。
    """
    for title in installer_titles(app_name):
        match = _VERSION_IN_TEXT.search(title)
        if match:
            return match.group(0)
    return None


WIZARD_DUMP = Path("reports/probe/wizard_dump.txt")
WIZARD_TIMELINE = Path("reports/probe/wizard_timeline.txt")


def dump_wizard_windows(app_name: str = "", path: Path = WIZARD_DUMP, append: bool = False) -> Path:
    """把当前所有顶层窗口 + 疑似安装向导的按钮清单落盘。

    安装超时时用：光看认出来的对话框不够，真向导可能没被判据认出来，
    得把全量窗口摊开看。append=True 时往时间线追加，轮询期间周期留证。
    """
    lines: list[str] = []
    if append:
        lines.append(f"===== {time.strftime('%H:%M:%S')} =====")
    dialogs = set(installer_dialogs(app_name))
    for hwnd in _top_hwnds():
        pid = _hwnd_pid(hwnd)
        image = _process_image(pid)
        title = popup_text(_hwnd_text(hwnd))
        lines.append(
            f"window hwnd={hwnd} pid={pid} class={_hwnd_class(hwnd)!r} "
            f"title={title!r} image={Path(image).name if image else ''!r}"
        )
        if hwnd not in dialogs:
            continue
        for button in wizard_buttons(hwnd)[:40]:
            lines.append(
                f"    Button hwnd={button.hwnd} text={button.text!r} rect={button.rect} "
                f"visible={button.visible} enabled={button.enabled}"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    if append:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def install_fixture(cfg: Config, main, app_name: str, timeout_sec: int | None = None) -> str:
    """从商店装夹具应用，装没装成只看注册表 DisplayName，不看截图。"""
    if not app_name:
        raise LaunchError("P1-A：没配安装夹具应用")
    if not is_admin():
        raise LaunchError(NEED_ADMIN_HINT)
    if installer_dialogs(app_name):
        raise LaunchError(
            f"P1-A：上一轮的厂商安装向导还挂着（{wizard_snapshot(app_name)}），先关掉再跑。"
            "残留进程可能连 taskkill 都拒绝访问，在管理员终端里跑 tools\\reset_fixture.py 收干净"
        )
    if is_app_installed(app_name):
        raise LaunchError(f"P1-A：{app_name} 已经在机器上，先卸掉再当安装夹具")
    probe = dataclasses.replace(cfg, search_keyword=app_name, search_min_hits=1)
    hits = search_hits(probe, main)
    if app_name not in hits:
        raise LaunchError(f"P1-A：商店里搜不到夹具应用 {app_name}，命中 {hits}")
    detail = open_detail_until_ready(probe, main, app_name)
    if detail.primary_action not in INSTALL_ACTIONS:
        raise LaunchError(f"P1-A：{app_name} 详情页主按钮是 {detail.primary_action!r}，不是安装类")
    button = _by_name(main, "Button", detail.primary_action, exact=True)
    if button is None or not _press_button(button):
        raise LaunchError(f"P1-A：点不动 {app_name} 详情页的「{detail.primary_action}」")
    WIZARD_TIMELINE.parent.mkdir(parents=True, exist_ok=True)
    WIZARD_TIMELINE.write_text("", encoding="utf-8")
    deadline = time.time() + (timeout_sec or cfg.timeouts.install_sec)
    wizard_clicks: list[str] = []
    next_trace = time.time()
    while time.time() < deadline:
        matched = installed_match(app_name)
        if matched:
            dismiss_vendor_dialogs(app_name)
            return matched
        for label in drive_vendor_installer(app_name):
            if label not in wizard_clicks:
                wizard_clicks.append(label)
        if time.time() >= next_trace:
            dump_wizard_windows(app_name, path=WIZARD_TIMELINE, append=True)
            next_trace = time.time() + 30
        time.sleep(1)
    dump = dump_wizard_windows(app_name)
    raise LaunchError(
        f"P1-A：{app_name} 安装超时，注册表里没出现；向导点过 {wizard_clicks}；"
        f"当前向导 {wizard_snapshot(app_name)}；全量窗口已落盘 {dump}，过程时间线 {WIZARD_TIMELINE}"
    )


def _leave_mine(main) -> bool:
    """退回首页，好让下次 open_mine 真的重进一次页面（open_mine 看到 aid=Mine 就直接返回）。"""
    home = _by_name(main, "ListItem", HOME_TAB_NAME, exact=True)
    if home is None or not _press_button(home):
        return False
    return wait_until(lambda: _by_aid(main, "Custom", MINE_AUTO_ID) is None, timeout_sec=5)


def _item_action_button(main, app_name: str, tab: str, timeout_sec: int = 40):
    """找列表里某条的动作按钮。

    列表是进页面时拉的，装完/更新完当场可能还是旧的：先原地等，再退出「我的」重进逼它重拉。
    """
    action_aid = ITEM_ACTION_AIDS[tab]
    for attempt in (1, 2):
        open_mine(main)
        switch_app_tab(main, tab)
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            button = find_item_button(main, app_name, action_aid)
            if button is not None:
                return button
            time.sleep(1)
        if attempt == 1 and not _leave_mine(main):
            break
    return None


def uninstall_item_button(main, app_name: str, timeout_sec: int = 40):
    return _item_action_button(main, app_name, "uninstall", timeout_sec)


def update_item_button(main, app_name: str, timeout_sec: int = 40):
    return _item_action_button(main, app_name, "update", timeout_sec)


def uninstall_fixture(cfg: Config, main, app_name: str, timeout_sec: int | None = None) -> None:
    """从「我的 → 卸载应用」卸夹具应用，断言走注册表。"""
    if not app_name:
        raise LaunchError("P1-A：没配卸载夹具应用")
    if not is_app_installed(app_name):
        raise LaunchError(f"P1-A：{app_name} 不在系统卸载列表里，不能当卸载夹具")
    button = uninstall_item_button(main, app_name)
    if button is None or not _press_button(button):
        raise LaunchError(
            f"P1-A：卸载列表里找不到 {app_name} 的卸载按钮（退出「我的」重进也没刷出来）；"
            f"注册表里它确实装着 —— 列表不跟着系统状态刷新"
        )
    deadline = time.time() + (timeout_sec or cfg.timeouts.uninstall_sec)
    wizard_clicks: list[str] = []
    while time.time() < deadline:
        _confirm_uninstall(main)
        # 大厅点完「卸载」只是拉起厂商卸载器，真卸要在厂商对话框里再确认一次。
        # 只点有文字的按钮：确认页要的是被皮肤藏起来的「卸载(&U)」，而卸完那页
        # 只剩一块来历不明的自绘大按钮，点它可能把应用又拉起来。
        for label in drive_vendor_installer(app_name, drawn_ok=False):
            if label not in wizard_clicks:
                wizard_clicks.append(label)
        if not is_app_installed(app_name):
            dismiss_vendor_dialogs(app_name)
            return
        time.sleep(1)
    dump = dump_wizard_windows(app_name)
    raise LaunchError(
        f"P1-A：{app_name} 卸载超时，注册表里还在；厂商向导点过 {wizard_clicks}；"
        f"当前向导 {wizard_snapshot(app_name)}；全量窗口已落盘 {dump}"
    )


UNINSTALL_DIALOG_NAME = "卸载应用对话框"


def _confirm_uninstall(main) -> bool:
    """卸载确认弹窗是主窗口下的子 Window，按钮 aid=ConfirmBtn、name「卸载」。只在弹窗里点，避免误点页面上别的按钮。"""
    dialog = _by_name(main, "Window", UNINSTALL_DIALOG_NAME, exact=True)
    if dialog is None:
        return False
    try:
        buttons = dialog.descendants(control_type="Button")
    except Exception:
        return False
    for button in buttons:
        try:
            aid = button.element_info.automation_id or ""
            name = popup_text(button.element_info.name or "")
        except Exception:
            continue
        if aid == "ConfirmBtn" or name == "卸载":
            return _press_button(button)
    return False
