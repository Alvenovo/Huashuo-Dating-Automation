from __future__ import annotations

import time

from hall_auto.launch import LaunchError, _press_button
from hall_auto.login import _by_aid, _by_name, open_login_dialog
from hall_auto.screen import count_red_pixels, grab_virtual_screen

REGISTER_OPEN_TIMEOUT_SEC = 10
RED_PIXEL_THRESHOLD = 50
# 格式提示的 AutomationProperties.Name 被钉死在设计期文案上，UIA 读不到变红后的语义，只能截屏数红像素
TIP_AIDS = ("PasswordInputTip", "RePasswordInputTip")

FIELD_AIDS = {
    "phone": ("Edit", "MobileInput"),
    "code": ("Edit", "CodeInput"),
    "password": ("Edit", "PasswordSecInput"),
    "confirm": ("Edit", "RePasswordSecInput"),
}


def register_open(pid: int) -> bool:
    return _by_name(pid, "Window", "注册窗口") is not None


def open_register_page(pid: int) -> None:
    open_login_dialog(pid)
    entry = _by_name(pid, "Button", "注册账号") or _by_name(pid, "Text", "注册账号")
    if entry is None or not _press_button(entry):
        raise LaunchError("注册：点不开注册页入口")
    deadline = time.time() + REGISTER_OPEN_TIMEOUT_SEC
    while time.time() < deadline:
        if _by_aid(pid, "Button", "RegisterBtn") is not None:
            return
        time.sleep(1)
    raise LaunchError("注册：注册页没出来（找不到 RegisterBtn）")


def register_controls(pid: int) -> dict[str, bool]:
    wanted = dict(FIELD_AIDS)
    wanted["agree"] = ("CheckBox", "AgreeCheck")
    wanted["submit"] = ("Button", "RegisterBtn")
    wanted["send_code"] = ("Button", "CodeSendBtn")
    return {key: _by_aid(pid, ctype, aid) is not None for key, (ctype, aid) in wanted.items()}


def fill_register(pid: int, phone: str, code: str, password: str, confirm: str) -> None:
    for key, value in (("phone", phone), ("code", code), ("password", password), ("confirm", confirm)):
        ctype, aid = FIELD_AIDS[key]
        node = _by_aid(pid, ctype, aid)
        if node is None:
            raise LaunchError(f"注册：找不到输入框 {aid}")
        node.set_edit_text(value)
        time.sleep(0.3)


def _agree_state(pid: int) -> str | None:
    box = _by_aid(pid, "CheckBox", "AgreeCheck")
    if box is None:
        return None
    try:
        return str(box.get_toggle_state())
    except Exception:
        return None


def set_agree(pid: int, agree: bool) -> None:
    box = _by_aid(pid, "CheckBox", "AgreeCheck")
    if box is None:
        raise LaunchError("注册：找不到协议复选框 AgreeCheck")
    for _ in range(3):
        if (_agree_state(pid) == "1") == agree:
            return
        _press_button(box)
        time.sleep(0.5)
    raise LaunchError(f"注册：协议复选框切不到 {agree}")


def register_enabled(pid: int) -> bool:
    btn = _by_aid(pid, "Button", "RegisterBtn")
    if btn is None:
        raise LaunchError("注册：找不到 RegisterBtn")
    return bool(btn.is_enabled())


def click_register(pid: int) -> bool:
    btn = _by_aid(pid, "Button", "RegisterBtn")
    if btn is None:
        raise LaunchError("注册：找不到 RegisterBtn")
    return _press_button(btn)


def tip_red_flags(pid: int) -> tuple[bool, bool]:
    """(密码格式提示红, 确认密码提示红)。灰=0 红像素、红字约九百起，阈值 50 足够分开。"""
    img, (vx, vy) = grab_virtual_screen()
    flags: list[bool] = []
    for aid in TIP_AIDS:
        node = _by_aid(pid, "Text", aid)
        if node is None:
            raise LaunchError(f"注册：找不到提示节点 {aid}")
        rect = node.rectangle()
        crop = img.crop((rect.left - vx, rect.top - vy, rect.right - vx, rect.bottom - vy))
        flags.append(count_red_pixels(crop) > RED_PIXEL_THRESHOLD)
    return flags[0], flags[1]
