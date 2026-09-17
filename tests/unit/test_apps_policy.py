from __future__ import annotations

import pytest

from hall_auto import apps
from hall_auto.apps import match_display_name, pick_wizard_label
from hall_auto.config import load_config


@pytest.mark.unit
@pytest.mark.parametrize(
    "app_name,names,expected",
    [
        ("网易云音乐", ["网易云音乐"], "网易云音乐"),
        ("网易云音乐", ["网易云音乐 3.1.40", "网易云音乐"], "网易云音乐"),
        ("网易云音乐", ["网易云音乐 3.1.40"], "网易云音乐 3.1.40"),
        ("Win截图", ["win截图 1.0"], "win截图 1.0"),
        ("网易云音乐", ["微信", "WPS Office"], None),
        ("", ["网易云音乐"], None),
    ],
)
def test_match_display_name(app_name, names, expected):
    assert match_display_name(app_name, names) == expected


@pytest.mark.unit
def test_match_display_name_never_picks_unrelated_prefix():
    """包含匹配只在没有精确命中时兜底，不能把「网易云音乐」匹到「云音乐播放器」之外的东西。"""
    assert match_display_name("网易云音乐", ["云音乐播放器"]) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "names,expected",
    [
        (["同意并安装", "取消"], "同意并安装"),
        (["立即安装", "安装"], "立即安装"),
        (["下一步", "取消"], "下一步"),
        (["完成"], "完成"),
        (["取消", "浏览"], None),
        (["安装捆绑推广"], None),
        ([], None),
    ],
)
def test_pick_wizard_label(names, expected):
    assert pick_wizard_label(names) == expected


@pytest.mark.unit
def test_fixture_apps_parsing(tmp_path):
    """夹具缺省必须是空串：没配夹具时执行类用例只能跳过，不能乱装。"""
    empty = tmp_path / "empty.yaml"
    empty.write_text("display_name_contains: 华硕大厅\n", encoding="utf-8")
    cfg = load_config(empty)
    assert (cfg.fixture_apps.install, cfg.fixture_apps.uninstall) == ("", "")

    filled = tmp_path / "filled.yaml"
    filled.write_text(
        'fixture_apps:\n  install: "网易云音乐"\n  uninstall: "网易云音乐"\n',
        encoding="utf-8",
    )
    cfg = load_config(filled)
    assert cfg.fixture_apps.install == "网易云音乐"
    assert cfg.fixture_apps.update == ""


@pytest.mark.unit
def test_installer_dialog_title_fallback(monkeypatch):
    """读不到进程映像时只认「应用名 + 安装类关键词」的标题，避免点到别的窗口。"""
    monkeypatch.setattr(apps._user32, "IsWindowVisible", lambda hwnd: True)
    monkeypatch.setattr(apps, "_process_image", lambda pid: "")
    titles = {
        1: "网易云音乐 安装",
        2: "网易云音乐 所需空间: 418.7MB",
        3: "微信",
        4: "网易云音乐",
        5: "网易云音乐 卸载",
    }
    monkeypatch.setattr(apps, "_hwnd_text", lambda hwnd: titles[hwnd])
    assert apps.is_installer_dialog(1, "网易云音乐")
    assert apps.is_installer_dialog(2, "网易云音乐")
    assert apps.is_installer_dialog(5, "网易云音乐")
    assert not apps.is_installer_dialog(1, "")
    assert not apps.is_installer_dialog(3, "网易云音乐")
    assert not apps.is_installer_dialog(4, "网易云音乐")


@pytest.mark.unit
def test_installer_dialog_by_nsis_temp_image(monkeypatch):
    """厂商卸载器把自己解包到 %TEMP%\\~nsuA.tmp\\Au_.exe，映像不在下载目录也要认得。

    进度页/完成页的标题可能不带关键词，光靠标题会漏。
    """
    monkeypatch.setattr(apps._user32, "IsWindowVisible", lambda hwnd: True)
    monkeypatch.setattr(apps, "_hwnd_text", lambda hwnd: "")
    images = {
        40: r"C:\Users\admin\AppData\Local\Temp\~nsuA.tmp\Au_.exe",
        50: r"C:\Users\admin\AppData\Local\Temp\netease\CloudMusic.exe",
    }
    monkeypatch.setattr(apps, "_process_image", lambda pid: images[pid])
    monkeypatch.setattr(apps, "_hwnd_pid", lambda hwnd: hwnd)
    monkeypatch.setattr(apps, "_hwnd_class", lambda hwnd: "#32770")
    assert apps.is_installer_dialog(40, "网易云音乐")
    assert not apps.is_installer_dialog(50, "网易云音乐")


@pytest.mark.unit
def test_installer_dialog_by_image_and_hall_excluded(monkeypatch):
    """映像在商店下载目录且是标准对话框才认；大厅自己的窗口一律排除，免得点到商店界面。"""
    monkeypatch.setattr(apps._user32, "IsWindowVisible", lambda hwnd: True)
    monkeypatch.setattr(apps, "_hwnd_text", lambda hwnd: "")
    images = {
        10: r"C:\AsusMCenterDownload\NeteaseCloudMusic_Setup.exe",
        20: r"C:\Program Files (x86)\ASUS\ASUS Member Center\AsusMemberCenter.exe",
        30: r"C:\AsusMCenterDownload\WinScreenshot.exe",
    }
    pids = {10: 10, 20: 20, 30: 30}
    classes = {10: "#32770", 20: "HwndWrapper[AsusMemberCenter.exe;;x]", 30: "zzbase_widget_window"}
    monkeypatch.setattr(apps, "_process_image", lambda pid: images.get(pid, ""))
    monkeypatch.setattr(apps, "_hwnd_pid", lambda hwnd: pids[hwnd])
    monkeypatch.setattr(apps, "_hwnd_class", lambda hwnd: classes[hwnd])
    assert apps.is_installer_dialog(10, "网易云音乐")
    assert not apps.is_installer_dialog(20, "网易云音乐")
    # 绿色应用的映像同样在下载目录，但它不是安装对话框，认错了会点到它的界面
    assert not apps.is_installer_dialog(30, "网易云音乐")


# 2026-09-15 从网易云音乐 NSIS 安装器第一页实抓的按钮表
NETEASE_PAGE1 = (
    apps.WizardButton(27854912, "", (1072, 801, 1432, 861), True, True),  # 自绘主按钮 360x60
    apps.WizardButton(25428776, "", (1617, 441, 1653, 477), True, True),  # 右上角关闭叉 36x36
    apps.WizardButton(985740, "关闭(&L)", (1416, 1014, 1554, 1056), False, True),
    apps.WizardButton(1117118, "取消(&C)", (1573, 1014, 1711, 1056), False, False),
    apps.WizardButton(985708, "< 上一步(&P)", (-9178, -9589, -9178, -9589), False, False),
)


@pytest.mark.unit
def test_pick_wizard_button_finds_owner_drawn_cta():
    """主按钮没有文字，只能按面积认；关闭叉同样没文字，必须靠面积门槛排除。"""
    picked = apps.pick_wizard_button(NETEASE_PAGE1)
    assert picked is not None and picked.hwnd == 27854912


@pytest.mark.unit
def test_pick_wizard_button_never_cancels():
    """就算取消/上一步变成可见可用，也绝不能点 —— 点了这一轮夹具就白跑了。"""
    live = tuple(
        apps.WizardButton(b.hwnd, b.text, b.rect, True, True) for b in NETEASE_PAGE1[3:]
    )
    assert [b.text for b in live] == ["取消(&C)", "< 上一步(&P)"]
    assert apps.pick_wizard_button(live) is None


@pytest.mark.unit
def test_pick_wizard_button_prefers_labelled_close():
    """收尾页同时有自绘大按钮和「关闭」时点关闭：大按钮常是「立即体验」，会把应用拉起来污染状态。"""
    buttons = (
        apps.WizardButton(1, "", (1072, 801, 1432, 861), True, True),
        apps.WizardButton(2, "关闭(&L)", (1416, 1014, 1554, 1056), True, True),
    )
    picked = apps.pick_wizard_button(buttons)
    assert picked is not None and picked.hwnd == 2


@pytest.mark.unit
def test_pick_wizard_button_ignores_bundle_promo():
    """有文字但不在白名单里的一律不碰，捆绑推广就长这样。"""
    buttons = (
        apps.WizardButton(1, "安装某某卫士", (900, 800, 1200, 840), True, True),
        apps.WizardButton(2, "取消(&C)", (1573, 1014, 1711, 1056), True, True),
    )
    assert apps.pick_wizard_button(buttons) is None


# 2026-09-15 从「网易云音乐 卸载」确认页实抓。两块自绘大按钮 OCR 出来是
# 左「再想想」右「狠心卸载」—— 尺寸位置全一样，几何上分不出谁是确认。
NETEASE_UNINSTALL = (
    apps.WizardButton(1903606, "", (1041, 674, 1401, 734), True, True),  # 狠心卸载（右）
    apps.WizardButton(4523964, "", (663, 674, 1023, 734), True, True),  # 再想想（左，是取消）
    apps.WizardButton(24970132, "", (942, 779, 1117, 804), True, True),
    apps.WizardButton(1969118, "", (851, 630, 926, 651), False, False),
    apps.WizardButton(7408192, "卸载(&U)", (926, 630, 1001, 651), False, True),
    apps.WizardButton(1510404, "取消(&C)", (1012, 630, 1087, 651), False, True),
    apps.WizardButton(2558362, "", (1397, 358, 1433, 394), True, True),  # 关闭叉
)


@pytest.mark.unit
def test_pick_wizard_button_uninstall_page_uses_hidden_label():
    """换肤卸载器只认被皮肤藏起来的 NSIS 文字按钮，BM_CLICK 对隐藏按钮有效（实跑验证过）。"""
    picked = apps.pick_wizard_button(NETEASE_UNINSTALL)
    assert picked is not None and picked.hwnd == 7408192
    assert not picked.visible


@pytest.mark.unit
def test_pick_wizard_button_never_guesses_between_drawn_ctas():
    """没有文字可依据时不猜：左边那块是「再想想」，猜错就把卸载取消了。"""
    drawn_only = tuple(b for b in NETEASE_UNINSTALL if not b.text)
    assert len([b for b in drawn_only if b.area >= apps.CTA_MIN_AREA]) == 2
    assert apps.pick_wizard_button(drawn_only) is None


@pytest.mark.unit
def test_pick_wizard_button_strips_nsis_nav_arrow():
    """WB 安装器下一页是「下一步(&N) >」带尾箭头；不剥掉就匹配不上白名单、drive 一个都不点。"""
    buttons = (
        apps.WizardButton(1, "下一步(&N) >", (1393, 983, 1506, 1015), True, True),
        apps.WizardButton(2, "取消(&C)", (1521, 983, 1634, 1015), True, True),
    )
    picked = apps.pick_wizard_button(buttons)
    assert picked is not None and picked.hwnd == 1
    assert apps.button_label(picked.text) == "下一步"
    assert apps.button_label("< 上一步(&P)") == "上一步"


@pytest.mark.unit
def test_pick_wizard_button_drawn_ok_off_skips_cta():
    """收尾关窗不点自绘大按钮（那块常是「立即体验」，点了会把应用拉起来），改点文字的「关闭」。"""
    picked = apps.pick_wizard_button(NETEASE_PAGE1, drawn_ok=False)
    assert picked is not None and picked.hwnd == 985740
    assert apps.button_label(picked.text) == "关闭"
    # 卸载页照样能点中隐藏的文字「卸载」，drawn_ok 不影响这一档
    picked = apps.pick_wizard_button(NETEASE_UNINSTALL, drawn_ok=False)
    assert picked is not None and picked.hwnd == 7408192
