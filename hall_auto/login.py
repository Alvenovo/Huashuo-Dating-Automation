from __future__ import annotations

import time

import win32gui
import win32process
from pywinauto.controls.uiawrapper import UIAWrapper
from pywinauto.uia_element_info import UIAElementInfo

from hall_auto.config import Config
from hall_auto.launch import LaunchError, _press_button, popup_text
from hall_auto.waiting import wait_until, wait_until_or_raise
from hall_auto.winapi import _hwnd_pid, _hwnd_visible, _top_hwnds

LOGIN_DIALOG_TIMEOUT_SEC = 15
LOGIN_SUBMIT_TIMEOUT_SEC = 25
CODE_RESEND_TIMEOUT_SEC = 65  # 短信网关冷却常为 60s，还原程紧接第一程再发时要等发送按钮回来
MICROSOFT_WINDOW_TIMEOUT_SEC = 15  # 点微软入口后内嵌登录窗出现
MICROSOFT_PAGE_TIMEOUT_SEC = 20  # WebView 里邮箱框出现（实测 ~1.7s，留余量）
MICROSOFT_SSO_TIMEOUT_SEC = 25  # 点磁贴/提交后回到大厅已登录态
MICROSOFT_OTP_SEND_TIMEOUT_SEC = 15  # 「我们向 <邮箱> 发送登录代码。」页上【发送验证码】按钮出现
MICROSOFT_OTP_PAGE_TIMEOUT_SEC = 30  # 点【发送验证码】后代码输入框出现（微软发信有网络往返）
MICROSOFT_OTP_SUBMIT_TIMEOUT_SEC = 12  # 填完码后提交按钮变可点（页面校验 + 重渲染，别抓一次快照就下结论）

# 「邮箱验证码页」的文本特征。**这一页必须排在 account_picker 之前判** ——
# 它的正文是「我们向 <邮箱> 发送登录代码。」，**含邮箱**（真值走 HALL_MS_USER，不落盘），
# 会被 `email in name` 那条误判成「账号选择器」：脚本去点一块纯文本（点不动），
# 然后死等已登录态 → 报「提交后未进入已登录态」，看着像登录失败，其实是认错了页面。
# 2026-09-22 真机截图实锤，见 reports 里那轮的 test_microsoft_login_sso/final.png。
_MICROSOFT_OTP_MARKERS = (
    "发送登录代码",
    "发送验证码",
    "输入代码",
    "登录代码",
    "send code",
    "enter code",
    "email code",
)

# 代码输入框 / 提交按钮的 automation id。取微软 login.microsoftonline.com 的
# 标准 OTC（one-time code）id；**留退路**是因为微软会改版，而这一页结构极简
# （只有一个输入框），退化规则比写死 id 更耐改。
_MS_OTP_INPUT_AIDS = ("idTxtBx_SAOTCC_OTC", "idTxtBx_SAOTCC_OTC2")
_MS_OTP_SUBMIT_AIDS = ("idSubmit_SAOTCC_Continue", "idSIButton9")
_MS_OTP_SUBMIT_NAMES = ("验证", "继续", "下一步", "提交", "Verify", "Continue", "Next")

# 提交按钮的**排除词**。上面 `_MS_OTP_SUBMIT_NAMES` 里有「验证」这种宽词，
# 而这一页同时挂着【发送验证码】—— 不排掉就会去点"重发"而不是"提交"：
# 按钮点下去了、看着像成功，实际码又发了一遍，人白等一个。
_MS_OTP_SUBMIT_AVOID = ("发送", "重新", "获取", "send", "resend", "get new")


def _is_microsoft_otp_page(name: str) -> bool:
    """这一条文本是不是「邮箱验证码」页的特征。大小写不敏感（微软文案中英混排）。"""
    low = name.lower()
    return any(marker in name or marker in low for marker in _MICROSOFT_OTP_MARKERS)


def _ms_dump_path() -> "Path":
    from pathlib import Path

    return Path(__file__).resolve().parent.parent / "reports" / "ms_ui_dump"


def dump_microsoft_tree(ms_win, reason: str) -> str:
    """把微软登录窗的控件树落盘，返回文件路径。

    **为什么必须留着**：微软会改版，写死的 aid 一旦失配，现场只有一句
    「点不到 XX」—— 有这棵树才能把 id 补回去，不用再让一线重跑一次抓。
    """
    from datetime import datetime

    lines = [f"# {reason}", f"# {datetime.now().isoformat(timespec='seconds')}"]
    for node in _ms_nodes(ms_win):
        try:
            info = node.element_info
            lines.append(
                f"{info.control_type or '?':14} aid={info.automation_id or '':32} "
                f"name={popup_text(info.name or '')[:120]}"
            )
        except Exception:
            continue
    target = _ms_dump_path() / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines), encoding="utf-8")
        return str(target)
    except OSError:
        return ""


def _windows(pid: int) -> list:
    """本进程的顶层窗口。用 Win32 EnumWindows 按 pid 过滤，比 UIA 枚举整个桌面再逐个读 process_id 快得多。"""
    hwnds: list[int] = []

    def _collect(hwnd, _):
        if win32process.GetWindowThreadProcessId(hwnd)[1] == pid:
            hwnds.append(hwnd)

    win32gui.EnumWindows(_collect, None)
    windows = []
    for h in hwnds:
        # 微软 SSO 登录完成时，内嵌的 WinForms 登录窗会关闭，句柄瞬间失效，
        # UIAElementInfo(h) 抛 COMError；跳过失效句柄，别让整轮枚举崩掉。
        try:
            windows.append(UIAWrapper(UIAElementInfo(h)))
        except Exception:
            continue
    return windows


def _nodes(pid: int, control_type: str | None = None):
    for win in _windows(pid):
        try:
            for node in win.descendants(control_type=control_type) if control_type else win.descendants():
                yield node
        except Exception:
            continue


def _by_aid(pid: int, control_type: str, aid: str):
    for node in _nodes(pid, control_type):
        try:
            if (node.element_info.automation_id or "") == aid:
                return node
        except Exception:
            continue
    return None


def _by_name(pid: int, control_type: str, keyword: str, exact: bool = False):
    for node in _nodes(pid, control_type):
        try:
            name = popup_text(node.element_info.name or node.window_text())
        except Exception:
            continue
        if name == keyword if exact else keyword in name:
            return node
    return None


def logged_in(pid: int) -> bool:
    node = _by_aid(pid, "Button", "UserInfoPart")
    return node is not None and "已登录" in popup_text(node.element_info.name)


def login_dialog_open(pid: int) -> bool:
    return _by_aid(pid, "Edit", "MobileInputBox") is not None


def open_login_dialog(pid: int) -> None:
    if login_dialog_open(pid):
        return
    entry = _by_aid(pid, "Button", "UserInfoPart") or _by_name(pid, "Button", "点击登录")
    if entry is None or not _press_button(entry):
        raise LaunchError("登录：点不到登录入口 UserInfoPart")
    wait_until_or_raise(
        lambda: login_dialog_open(pid),
        "登录：弹窗没出现（找不到 MobileInputBox）",
        timeout_sec=LOGIN_DIALOG_TIMEOUT_SEC,
        interval=0.5,
    )


def switch_login_tab(pid: int, keyword: str, ready=None) -> None:
    """切登录弹窗页签。ready 传「切好了」的谓词（如目标页签特有控件出现），不传则不等。"""
    tab = (
        _by_name(pid, "TabItem", keyword)
        or _by_name(pid, "Text", keyword)
        or _by_name(pid, "Button", keyword)
    )
    if tab is None:
        raise LaunchError(f"登录：找不到页签 {keyword!r}")
    if not _press_button(tab):
        raise LaunchError(f"登录：点不动页签 {keyword!r}")
    if ready is not None:
        wait_until_or_raise(ready, f"登录：切到页签 {keyword!r} 后界面没就绪", timeout_sec=10)


def login_field_aids(pid: int) -> dict[str, str]:
    """登录弹窗里的关键控件，供断言用。"""
    found: dict[str, str] = {}
    for node in _nodes(pid):
        try:
            aid = node.element_info.automation_id or ""
            name = popup_text(node.element_info.name or node.window_text())
        except Exception:
            continue
        if aid in ("MobileInputBox", "PasswordSecInput", "CodeInput", "CodeSendBtn"):
            found[aid] = name
        elif name == "同意协议":
            found["agree"] = name
        elif name in ("登录", "忘记密码", "注册账号") or "微软账号登录" in name:
            found.setdefault(name, name)
    return found


def logout(pid: int) -> None:
    """已登录时点用户区会开独立菜单窗（标题「华硕应用商店」），内有退出登录。

    **先收掉可能还开着的「绑定手机号」模态弹窗**（2026-09-22 补）：
    那个弹窗挡在主界面前面，用户区点不动 → 这里会抛「退出登录：点不开用户菜单」，
    看着像登出功能坏了，实际是被模态窗挡着。
    **不在这里兜的话，只有人在环那两条会处理它** —— 无人值守的 SSO 那条
    （`test_microsoft_login_sso`）登进去就弹、弹完直接走 finally 里的 logout，
    报的是登出失败，**与真缺陷同形**。
    放在 `logout()` 里而不是逐条用例加，是因为它被每条登录用例开头结尾都调用，
    一处改完全都盖住（这也是 `close_bind_dialog` 那段注释说的「模态窗连累后面每一条」）。
    """
    if dismiss_bind_dialog(pid):
        print("（退出登录前先关掉了残留的「绑定手机号」弹窗 —— 它挡着用户区）")
    if not logged_in(pid):
        return
    entry = _by_aid(pid, "Button", "UserInfoPart")
    if entry is None or not _press_button(entry):
        raise LaunchError("退出登录：点不开用户菜单")
    deadline = time.time() + LOGIN_DIALOG_TIMEOUT_SEC
    while time.time() < deadline:
        button = _by_name(pid, "Button", "退出登录", exact=True)
        if button is not None:
            _press_button(button)
        time.sleep(0.5)
        if not logged_in(pid):
            return
    raise LaunchError("退出登录：点完仍是已登录态")


def _agree_toggled(node) -> bool:
    try:
        return node.get_toggle_state() == 1
    except Exception:
        return False


def _check_agree(pid: int) -> None:
    agree = _by_name(pid, "CheckBox", "同意协议")
    if agree is None or _agree_toggled(agree):
        return
    _press_button(agree)
    # 读不到勾选状态（控件不支持 TogglePattern）时退化成短固定等待，不阻塞登录
    if not wait_until(lambda: _agree_toggled(agree), timeout_sec=2, interval=0.2):
        time.sleep(0.3)


def request_sms_code(pid: int, user: str) -> None:
    """填手机号、勾协议、点「发送验证码」。

    点完 CodeSendBtn 节点会从控件树里消失（倒计时态不暴露按钮，2026-09-16 实测），
    所以发送成功与否不能按按钮签名断言，以手机收到短信为准。
    """
    open_login_dialog(pid)
    switch_login_tab(
        pid,
        "短信验证码登录",
        ready=lambda: _by_aid(pid, "Edit", "CodeInput") is not None,
    )
    phone = _by_aid(pid, "Edit", "MobileInputBox")
    if phone is None:
        raise LaunchError("登录：找不到手机号输入框")
    phone.set_edit_text(user)
    _check_agree(pid)
    send = _by_aid(pid, "Button", "CodeSendBtn")
    if send is None or not _press_button(send):
        raise LaunchError("登录：点不到「发送验证码」")


def login_with_sms_code(pid: int, code: str) -> str:
    box = _by_aid(pid, "Edit", "CodeInput")
    if box is None:
        raise LaunchError("登录：找不到验证码输入框")
    box.set_edit_text(code)
    submit = _by_name(pid, "Button", "登录", exact=True)
    if submit is None or not _press_button(submit):
        raise LaunchError("登录：点不到登录按钮")
    deadline = time.time() + LOGIN_SUBMIT_TIMEOUT_SEC
    while time.time() < deadline:
        if logged_in(pid):
            return popup_text(_by_aid(pid, "Button", "UserInfoPart").element_info.name)
        time.sleep(0.5)
    raise LaunchError("登录：提交验证码后未进入已登录态")


def open_forgot_password_page(pid: int) -> None:
    """从登录弹窗点「忘记密码」，登录窗口原地换成忘记密码页（客户端内 WPF 页）。

    幂等：已在忘记密码页（MobileInput 在）就直接返回，方便往返用例的还原程
    不管上一程把登录窗口留在什么状态都能重新导航到位。
    入口必须 Button + 全名精确匹配：密码提示文案「…可点击忘记密码进行重置」
    也含这四个字，模糊匹配会点到 Text 上毫无反应（2026-09-16 实测踩过）。
    """
    if _by_aid(pid, "Edit", "MobileInput") is not None:
        return
    open_login_dialog(pid)
    entry = _by_name(pid, "Button", "忘记密码", exact=True)
    if entry is None or not _press_button(entry):
        raise LaunchError("登录：点不到「忘记密码」")
    # 忘记密码页手机号框 aid=MobileInput（登录弹窗是 MobileInputBox，注册页才是 MobileInput）
    wait_until_or_raise(
        lambda: _by_aid(pid, "Edit", "MobileInput") is not None,
        "登录：忘记密码页没出现（找不到 MobileInput）",
        timeout_sec=LOGIN_DIALOG_TIMEOUT_SEC,
        interval=0.5,
    )


def request_forgot_sms(pid: int, user: str) -> None:
    """忘记密码页填手机号并点「发送验证码」。页面没有协议勾选框。

    点完 CodeSendBtn 从控件树消失（与登录短信页同一套倒计时实现），
    以消失作为已触发发送的信号，最终以手机收到短信为准。
    注意别拿 SetBtn 当页面判据：主窗口导航栏的「设置」按钮 aid 也是 SetBtn。
    """
    phone = _by_aid(pid, "Edit", "MobileInput")
    if phone is None:
        raise LaunchError("忘记密码：找不到手机号输入框")
    phone.set_edit_text(user)
    # 还原程紧接第一程再发时可能还在冷却（CodeSendBtn 未回控件树），先等它出现再点
    if not wait_until(
        lambda: _by_aid(pid, "Button", "CodeSendBtn") is not None,
        timeout_sec=CODE_RESEND_TIMEOUT_SEC,
        interval=0.5,
    ):
        raise LaunchError("忘记密码：等不到「发送验证码」按钮（可能仍在冷却）")
    send = _by_aid(pid, "Button", "CodeSendBtn")
    if not _press_button(send):
        raise LaunchError("忘记密码：点不动「发送验证码」")
    if not wait_until(lambda: _by_aid(pid, "Button", "CodeSendBtn") is None, timeout_sec=8, interval=0.5):
        # 2026-09-23 真机踩出：这条报错原来只说「可能没触发」，把**最常见的真因**漏掉了。
        # 短信验证码是**服务端限流**的：短时间内给同一个号发太多次，服务端拒绝发送，
        # 界面上通常只闪一下提示（截图里抓不到），按钮留在原位不动 ——
        # 表现与"点击没落上"完全一样。一线看到「可能没触发」会去查控件、查 UIA，
        # 方向全错。所以这里把限流写在第一条，并给出可执行动作。
        raise LaunchError(
            "忘记密码：点发送后 CodeSendBtn 没消失，可能没触发。"
            "**先按「短信发送超限」排查**：短时间内给同一个号发太多次时，服务端会拒绝发送，"
            "按钮留在原位不动、提示只闪一下（截图抓不到）。"
            "处理：① 换一个没被限流的测试号；② 等冷却（通常十几分钟到次日）再跑；"
            "③ 本条用例要**连发 2 个码**（重置 + 还原），是最容易撞上超限的一条。"
            "若确实刚发过码且号也没超限，再按控件没落点去查。"
        )


def submit_forgot_reset(pid: int, code: str, new_password: str) -> dict:
    """忘记密码页填验证码 + 新密码 + 确认密码，点「提交」完成重置。

    手机号和发送已由 request_forgot_sms 做好。提交按钮 aid=SetBtn 和主窗口导航栏
    「设置」按钮同 aid，必须按 name「提交」精确挑，别拿 aid 撞（会点到设置）。
    提交后页面行为未知（可能自动登录、可能关窗回登录页），以「忘记密码页关闭
    （MobileInput 消失）或进入已登录态」为成功信号；都不发生说明验证码/密码被拒。
    调用方拿到成功信号后仍应 logout 再用新密码登录一次，才算真验到重置生效。
    返回 {"page_closed": bool, "logged_in": bool}，标明命中了哪个信号，方便定位。
    """
    code_box = _by_aid(pid, "Edit", "CodeInput")
    secret = _by_aid(pid, "Edit", "PasswordSecInput")
    confirm = _by_aid(pid, "Edit", "RePasswordSecInput")
    if code_box is None or secret is None or confirm is None:
        raise LaunchError("忘记密码：找不到验证码/新密码/确认密码输入框")
    code_box.set_edit_text(code)
    secret.set_edit_text(new_password)
    confirm.set_edit_text(new_password)
    submit = _by_name(pid, "Button", "提交", exact=True)
    if submit is None or not _press_button(submit):
        raise LaunchError("忘记密码：点不到「提交」按钮")
    deadline = time.time() + LOGIN_SUBMIT_TIMEOUT_SEC
    page_closed = False
    loggedin = False
    while time.time() < deadline:
        page_closed = _by_aid(pid, "Edit", "MobileInput") is None
        loggedin = logged_in(pid)
        if page_closed or loggedin:
            return {"page_closed": page_closed, "logged_in": loggedin}
        time.sleep(0.5)
    raise LaunchError(
        f"忘记密码：提交后页面没关也没登录（page_closed={page_closed}, logged_in={loggedin}），可能验证码错或新密码不合法"
    )


def login_with_password(cfg: Config, pid: int) -> str:
    user, password = cfg.test_account()
    if not user or not password:
        raise LaunchError("登录：缺凭据，设置 HALL_TEST_USER / HALL_TEST_PASSWORD")
    return login_with_password_value(pid, user, password)


def login_with_password_value(pid: int, user: str, password: str) -> str:
    """账号密码页登录，凭据显式传入（改密码往返用例要拿新密码登一次验证）。"""
    open_login_dialog(pid)
    switch_login_tab(
        pid,
        "账号密码登录",
        ready=lambda: _by_aid(pid, "Edit", "PasswordSecInput") is not None,
    )
    phone = _by_aid(pid, "Edit", "MobileInputBox")
    secret = _by_aid(pid, "Edit", "PasswordSecInput")
    if phone is None or secret is None:
        raise LaunchError("登录：找不到手机号/密码输入框")
    phone.set_edit_text(user)
    secret.set_edit_text(password)
    _check_agree(pid)
    submit = _by_name(pid, "Button", "登录", exact=True)
    if submit is None or not _press_button(submit):
        raise LaunchError("登录：点不到登录按钮")
    deadline = time.time() + LOGIN_SUBMIT_TIMEOUT_SEC
    while time.time() < deadline:
        if logged_in(pid):
            return popup_text(_by_aid(pid, "Button", "UserInfoPart").element_info.name)
        time.sleep(0.5)
    raise LaunchError("登录：提交后未进入已登录态")


def _descendants(win) -> list:
    """任意 UIA 根节点的后代；元素失效/树在变时返回空表。

    **不抛**是刻意的：调用方大多是"轮询直到出现"的循环，偶发 COMError 应该
    等价于"这一轮没找到"，而不是把整轮用例崩掉。
    """
    try:
        return list(win.descendants())
    except Exception:
        return []


def _names_of(win) -> list[str]:
    """根节点下所有节点的 name（清洗过）。给"靠文案认窗口/认弹窗"的场合用。"""
    out: list[str] = []
    for node in _descendants(win):
        try:
            out.append(popup_text(node.element_info.name or ""))
        except Exception:
            continue
    return out


def _ms_nodes(ms_win):
    return _descendants(ms_win)


def _ms_by_aid(ms_win, aid: str):
    for node in _ms_nodes(ms_win):
        try:
            if (node.element_info.automation_id or "") == aid:
                return node
        except Exception:
            continue
    return None


def _wait_microsoft_window(pid: int, before: set[int]):
    """等本进程新增的、内嵌 webBrowser 面板的顶层窗（微软登录窗）。"""
    deadline = time.time() + MICROSOFT_WINDOW_TIMEOUT_SEC
    while time.time() < deadline:
        for h in _top_hwnds():
            if not _hwnd_visible(h) or h in before or _hwnd_pid(h) != pid:
                continue
            try:
                win = UIAWrapper(UIAElementInfo(h))
            except Exception:
                continue
            if _ms_by_aid(win, "webBrowser") is not None:
                return win
        time.sleep(0.5)
    raise LaunchError("微软登录：内嵌登录窗没出现")


def open_microsoft_login(pid: int):
    """点登录弹窗的微软入口，返回内嵌的微软登录窗（UIAWrapper）。

    微软登录不是外部 msedge 进程，而是大厅进程内的 WinForms 窗（内嵌 webBrowser 面板），
    所以按「本进程新增的、含 webBrowser 面板的顶层窗」定位。
    不勾协议点微软入口无反应（与密码/短信同一道闸），先 _check_agree。
    """
    open_login_dialog(pid)
    _check_agree(pid)
    before = {h for h in _top_hwnds() if _hwnd_visible(h)}
    entry = _by_name(pid, "Image", "微软") or _by_name(pid, None, "微软账号登录")
    if entry is None or not _press_button(entry):
        raise LaunchError("微软登录：点不到微软入口")
    return _wait_microsoft_window(pid, before)


def close_microsoft_login(pid: int) -> bool:
    """关掉可能还开着的微软登录窗（内嵌 `webBrowser` 面板的那个顶层窗）。

    为什么需要：人在环用例中途挂掉时，这个窗会**留在屏幕上**。
    它是大厅进程里的独立顶层窗，后面用例去操作大厅登录弹窗时会撞上它 ——
    2026-09-22 实测：微软那条挂在提交按钮上，紧跟着的短信那条就报
    「切到页签 '短信验证码登录' 后界面没就绪」，看着像短信的毛病，其实是被连累的。
    `logout()` 兜不住这种情况：它只在"已登录"时才动作。

    关不掉返回 False，**不抛** —— 收尾失败不该盖掉用例本身那个真正的失败。
    """
    closed = False
    for h in _top_hwnds():
        if not _hwnd_visible(h) or _hwnd_pid(h) != pid:
            continue
        try:
            win = UIAWrapper(UIAElementInfo(h))
        except Exception:
            continue
        if _ms_by_aid(win, "webBrowser") is None:
            continue
        try:
            win32gui.PostMessage(h, 0x0010, 0, 0)  # WM_CLOSE
            closed = True
        except Exception:
            continue
    return closed


def submit_microsoft_email(ms_win, email: str) -> None:
    """微软登录窗填邮箱（i0116）并点「下一步」（idSIButton9）。"""
    box = None
    deadline = time.time() + MICROSOFT_PAGE_TIMEOUT_SEC
    while time.time() < deadline and box is None:
        box = _ms_by_aid(ms_win, "i0116")
        time.sleep(0.4)
    if box is None:
        raise LaunchError("微软登录：等不到邮箱输入框 i0116")
    box.set_edit_text(email)
    nxt = _ms_by_aid(ms_win, "idSIButton9")
    if nxt is None or not _press_button(nxt):
        raise LaunchError("微软登录：点不到「下一步」")


def microsoft_state(pid: int, ms_win, email: str) -> str:
    """点「下一步」后的一次快照分支。

    返回 account_picker(本机有缓存 MS 会话，免密点磁贴) / password(要密码 i0118) /
    mfa(要二次验证) / email_otp(要邮箱验证码) / logged_in(已回大厅) / waiting(还在跳转)。

    **判定顺序不能改**：email_otp 必须排在 account_picker 之前。这一页的正文含邮箱，
    先判 account_picker 会把「发送登录代码」页认成账号选择器，脚本去点纯文本、
    然后死等已登录态 —— 报出来的是「提交后未进入已登录态」，与「账号选择器点不动」
    同形，方向全错（2026-09-22 真机踩过）。
    """
    if logged_in(pid):
        return "logged_in"
    names: list[str] = []
    for node in _ms_nodes(ms_win):
        try:
            name = popup_text(node.element_info.name or "")
            aid = node.element_info.automation_id or ""
        except Exception:
            continue
        if aid == "i0118":
            return "password"
        if "验证你的身份" in name or "verify your identity" in name.lower():
            return "mfa"
        names.append(name)
    # 先判验证码页，再判账号选择器 —— 顺序即正确性，别合并成一个 any()。
    # 守卫：tests/unit/test_login_policy.py（注入法验过能红）。
    if any(_is_microsoft_otp_page(n) for n in names):
        return "email_otp"
    if email and any(email in n for n in names):
        return "account_picker"
    return "waiting"


def _ms_by_name(ms_win, keyword: str, exact: bool = False):
    for node in _ms_nodes(ms_win):
        try:
            name = popup_text(node.element_info.name or "")
        except Exception:
            continue
        if (name == keyword) if exact else (keyword in name):
            return node
    return None


def _ms_otp_send_candidates(ms_win) -> list:
    """「发送验证码」的候选节点，**Button 优先，Text 垫底**。

    为什么分先后：webview 里一个按钮常常是 `Button(名=发送验证码)` 里再套一层
    `Text(名=发送验证码)`。命中里面那个 Text 时 `invoke()` 必然失败，
    `click_input()` 又只认矩形 —— 点了跟没点一样，**而且不报错**。
    外层那个 Button 才是能 invoke 的。

    先一次遍历拿全部节点、再在内存里挑（同 `_press_submit_once`）：
    每个候选各扫一遍整棵树的话，webview 上一轮就是好几秒。
    """
    buttons: list = []
    others: list = []
    for node in _ms_nodes(ms_win):
        try:
            name = popup_text(node.element_info.name or "")
            ctype = node.element_info.control_type or ""
        except Exception:
            continue
        if "发送验证码" in name or "send code" in name.lower():
            (buttons if ctype == "Button" else others).append(node)
    return buttons + others


def microsoft_send_code(ms_win) -> None:
    """在「我们向 <邮箱> 发送登录代码。」页点【发送验证码】，**并确认真的发出去了**。

    成功判据是「代码输入框出现」—— 这是页面上唯一与"码已发出"一一对应的可观测变化
    （按钮本身点完会消失/变倒计时态，`request_sms_code` 早就不拿按钮状态当判据了）。

    **为什么必须验**：webview 上 `invoke()` / `click_input()` 都可能**静默失败**，
    不验的话现场表现是「脚本压根没点发送，却直接回来问我要码」——
    人只能自己去点一下，而且看不出哪里不对（2026-09-22 新机实测就是这个）。
    等不到代码框就落盘控件树再抛错，报错里带上所有候选节点，
    别让人凭一句「点不到」猜。
    """
    # 点之前代码框就在 → 这一页可能已经发过码，验证不了"这次点生效"，不硬判。
    already = microsoft_code_input(ms_win) is not None

    deadline = time.time() + MICROSOFT_OTP_SEND_TIMEOUT_SEC
    seen: list[str] = []
    clicked = False
    # **do-while**：先试一次再判超时。写成 `while time.time() < deadline` 的话，
    # 超时给 0 时一轮都不跑（测试里就是这么撞出来的），而且"第一轮"本来就该跑。
    while not clicked:
        for node in _ms_otp_send_candidates(ms_win):
            try:
                info = node.element_info
                seen.append(f"{info.control_type or '?'} aid={info.automation_id or '-'}")
            except Exception:
                seen.append("? aid=-")
            # `invoke()` 不依赖前台，但退化的 `click_input()` 依赖 ——
            # 窗口在后面时点击会落到盖住它的那个窗口上（大厅被遮挡时实测就是这样）。
            # 复用 `_bring_to_front`（它会**确认**真拿到了前台，不只是调一下 set_focus）。
            _bring_to_front(ms_win)
            if _press_button(node):
                clicked = True
                break
        if clicked or time.time() >= deadline:
            break
        time.sleep(0.5)

    if not clicked:
        dumped = dump_microsoft_tree(ms_win, "等不到可点的【发送验证码】")
        raise LaunchError(
            f"微软登录：等不到可点的【发送验证码】按钮（{MICROSOFT_OTP_SEND_TIMEOUT_SEC}s）"
            + (f"，找到过 {len(seen)} 个同名节点：{seen}" if seen else "，整棵树里没有这个名字")
            + (f"，控件树已落盘：{dumped}" if dumped else "")
        )

    if already:
        return

    if (
        wait_microsoft_code_input(
            ms_win,
            timeout_sec=MICROSOFT_OTP_PAGE_TIMEOUT_SEC,
            required=False,  # 超时要自己拼更准的报错（"点了没生效"≠"没找到按钮"）
        )
        is not None
    ):
        return

    dumped = dump_microsoft_tree(ms_win, "点了【发送验证码】但代码输入框没出现")
    raise LaunchError(
        "微软登录：点了【发送验证码】，但代码输入框没出现"
        f"（{MICROSOFT_OTP_PAGE_TIMEOUT_SEC}s）—— 多半是点没生效（webview 静默失败），"
        "人也没收到码，先别去邮箱等"
        + (f"，控件树已落盘：{dumped}" if dumped else "")
    )


def microsoft_code_input(ms_win):
    """当前代码输入框；找不到返回 None。

    先按标准 aid 找，再退化到「窗里唯一的 Edit」—— 这一页结构极简，
    退化规则比写死 id 更耐微软改版。
    """
    for aid in _MS_OTP_INPUT_AIDS:
        node = _ms_by_aid(ms_win, aid)
        if node is not None:
            return node
    edits = []
    for node in _ms_nodes(ms_win):
        try:
            if (node.element_info.control_type or "") == "Edit":
                edits.append(node)
        except Exception:
            continue
    return edits[0] if len(edits) == 1 else None


def wait_microsoft_code_input(
    ms_win,
    timeout_sec: float | None = None,
    *,
    required: bool = True,
):
    """等代码输入框。超时行为由 `required` 决定，**因为两个调用方的需求正好相反**：

    - `submit_microsoft_code`：**必须**有框才填得进去，超时是硬失败 → `required=True`（默认）。
    - `microsoft_send_code`：把"框出现"当**发送成功**的证据，超时要自己拼一句更准的报错
      （"点了但没生效"和"压根没找到按钮"是两回事）→ `required=False`，超时返回 None。
    """
    if timeout_sec is None:
        timeout_sec = MICROSOFT_OTP_PAGE_TIMEOUT_SEC
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        node = microsoft_code_input(ms_win)
        if node is not None:
            return node
        time.sleep(0.5)
    if not required:
        return None
    dumped = dump_microsoft_tree(ms_win, "等不到验证码输入框（aid 可能被微软改版）")
    raise LaunchError(
        "微软登录：点【发送验证码】后等不到代码输入框"
        + (f"，控件树已落盘：{dumped}" if dumped else "")
    )


def _press_submit_once(ms_win, seen: list[str]) -> bool:
    """找一轮提交按钮并点它；这一轮没成返回 False。

    **先一次遍历拿全部控件，再在内存里挑** —— 原来每个候选各调一次
    `_ms_by_aid` / `_ms_by_name`，那俩内部都是一次全树遍历；
    9 个候选 = 9 次遍历，webview 上一轮就是好几秒，轮询两下就把超时耗光了。

    `seen` 记下**找到过**的候选：报错时能一眼分清"压根没找到"和"找到了点不动" ——
    这两种的下一步排查方向完全相反。
    """
    snapshot: list[tuple[str, str, object]] = []
    for node in _ms_nodes(ms_win):
        try:
            info = node.element_info
            snapshot.append((info.automation_id or "", popup_text(info.name or ""), node))
        except Exception:
            continue

    for aid in _MS_OTP_SUBMIT_AIDS:
        for node_aid, _text, node in snapshot:
            if node_aid != aid:
                continue
            if aid not in seen:
                seen.append(aid)
            if _press_button(node):
                return True

    for keyword in _MS_OTP_SUBMIT_NAMES:
        for _aid, text, node in snapshot:
            if keyword not in text:
                continue
            low = text.lower()
            if any(bad in text or bad in low for bad in _MS_OTP_SUBMIT_AVOID):
                continue  # 这是【发送验证码】那类，点下去是重发，不是提交
            if keyword not in seen:
                seen.append(keyword)
            if _press_button(node):
                return True
    return False


def _wait_submit(ms_win, seen: list[str]) -> bool:
    deadline = time.time() + MICROSOFT_OTP_SUBMIT_TIMEOUT_SEC
    while time.time() < deadline:
        if _press_submit_once(ms_win, seen):
            return True
        time.sleep(1.0)
    return False


def _bring_to_front(win) -> bool:
    """把窗口提到前台，并**确认它真拿到了前台**。

    确认这一步不能省：后面要往输入框敲真键盘，敲错窗口就是往别人窗口里打字 ——
    刚被 `focus_console()` 提到最前的终端就是最可能的受害者。
    """
    try:
        win.set_focus()
    except Exception:
        pass
    try:
        return win32gui.GetForegroundWindow() == int(win.handle)
    except Exception:
        return False


def submit_microsoft_code(ms_win, code: str) -> None:
    """填邮箱收到的验证码并提交。

    2026-09-22 真机报「点不到提交按钮」，根因是**只抓一次快照就下结论**。
    三个坑叠在一起：

      ① 按钮要等页面校验完才变可点 —— 抓一次就放弃，太早。
      ② `set_edit_text` 走 UIA ValuePattern，webview 里的 React 输入框
         **收不到 input 事件**，它自己的 state 还是空 → 按钮一直 disabled。
         现场表现就是「按钮明明在，就是点不动」。真键盘事件走另一条路，React 认。
      ③ 提交按钮的候选里有「验证」这种宽词，会撞上【发送验证码】——
         点下去看着成功，实际是重发，人白等一个码（已用 `_MS_OTP_SUBMIT_AVOID` 排掉）。

    所以流程：填 → **轮询**等可点 → 还不行补真键盘再轮询 → 最后兜一手回车。
    """
    box = wait_microsoft_code_input(ms_win)
    box.set_edit_text(code)
    seen: list[str] = []

    if _wait_submit(ms_win, seen):
        return

    # 走到这说明「填了但点不动」，最可能是 webview 没收到输入事件。
    # ⚠️ 补键盘前必须确认窗口真在前台，否则键盘会敲到别的窗口上。
    if _bring_to_front(ms_win):
        try:
            box.set_edit_text("")
            box.type_keys("^a")  # 全选，免得和 set_edit_text 填的值拼在一起
            box.type_keys(code, with_spaces=True)
        except Exception:
            pass
        if _wait_submit(ms_win, seen):
            return
        try:
            box.type_keys("{ENTER}")  # 不少 OTP 页回车即提交
            return
        except Exception:
            pass

    dumped = dump_microsoft_tree(ms_win, "找不到代码提交按钮")
    raise LaunchError(
        "微软登录：填了验证码但点不到提交按钮"
        f"（找到过的候选：{'、'.join(seen) if seen else '一个都没找到'}）"
        + (f"，控件树已落盘：{dumped}" if dumped else "")
    )


def wait_microsoft_state(pid: int, ms_win, email: str, timeout_sec: int = 15) -> str:
    deadline = time.time() + timeout_sec
    state = "waiting"
    while time.time() < deadline:
        state = microsoft_state(pid, ms_win, email)
        if state != "waiting":
            return state
        time.sleep(0.5)
    return state


def microsoft_pick_account(ms_win, email: str) -> None:
    """账号选择器里点缓存账号磁贴（名字含邮箱的那块），走 SSO 免密。"""
    tile = None
    deadline = time.time() + MICROSOFT_WINDOW_TIMEOUT_SEC
    while time.time() < deadline and tile is None:
        for node in _ms_nodes(ms_win):
            try:
                name = popup_text(node.element_info.name or "")
            except Exception:
                continue
            if email and email in name:
                tile = node
                break
        time.sleep(0.5)
    if tile is None:
        raise LaunchError("微软登录：没找到缓存账号磁贴")
    if not _press_button(tile):
        tile.click_input()


def wait_microsoft_logged_in(pid: int) -> str:
    """SSO/提交后等回大厅已登录态，返回用户区文案。"""
    deadline = time.time() + MICROSOFT_SSO_TIMEOUT_SEC
    while time.time() < deadline:
        if logged_in(pid):
            return popup_text(_by_aid(pid, "Button", "UserInfoPart").element_info.name)
        time.sleep(0.5)
    raise LaunchError("微软登录：提交后未进入已登录态")


# ---------------------------------------------------------------------------
# 登录后「绑定手机号」弹窗（2026-09-22 新机实测新增）
# ---------------------------------------------------------------------------
#
# 现象：在**没绑过手机号**的机器/账号上，登录成功后会弹一个绑定手机号的弹窗，
# 挡在主界面前面。它和登录弹窗一样是「大厅自己弹的覆盖层」。
#
# 为什么放在登录流程里、而不是单开一条用例：**它就是登录流程的一部分**。
# 不处理它，后面所有步骤都会被这个模态窗卡住；而且它必须在 `logout()` **之前**
# 处理 —— 模态窗不关，「点用户区 → 退出登录」根本点不动。
#
# ⚠️ **控件定位目前是按大厅既有弹窗的命名习惯推的**（登录弹窗 `MobileInputBox` /
# 忘记密码页 `MobileInput` / 短信页 `CodeInput` + `CodeSendBtn`），
# **2026-09-22 还没在真机上探过壳**。所以这里刻意做了三件事：
#   ① 每个控件都按「先 aid 全等、再名字包含」两轮找；
#   ② 找不到就 `dump_bind_tree()` 落盘**整进程的控件树**再抛错 ——
#      拿到那棵树就能把 aid 补准，不用让一线重跑一次抓；
#   ③ 任何失败路径都尽力**关掉弹窗**，绝不把它留在屏幕上连累后面的用例。
# 探壳补进 `项目知识库/探壳结论.md` 之后，这段注释就可以降级成一句指路。

BIND_DIALOG_TIMEOUT_SEC = 8  # 登录成功后等弹窗出现；没弹 = 已经绑过，不是失败
BIND_CONTROL_TIMEOUT_SEC = 10  # 弹窗已出现，等里面某个控件变成可交互
BIND_SUBMIT_TIMEOUT_SEC = 20  # 点「确定」后等弹窗消失（提交要过网络）

# 认「这就是绑定手机号弹窗」的文案特征。**必须带"绑定"二字** ——
# 只认"手机号"会误命中登录弹窗（那上面也写着手机号）。
_BIND_MARKERS = ("绑定手机号", "绑定手机", "手机号绑定", "完善手机号", "绑定账号")

# aid 取自大厅既有弹窗的命名习惯，名字关键字是 aid 失配时的退路。
_BIND_PHONE_AIDS = ("MobileInput", "MobileInputBox", "PhoneInput", "BindMobileInput", "TelInput")
_BIND_CODE_AIDS = ("CodeInput", "CodeInputBox", "SmsCodeInput", "VerifyCodeInput")
_BIND_SEND_AIDS = ("CodeSendBtn", "SendBtn", "GetCodeBtn", "CodeSendButton")
_BIND_SEND_NAMES = ("获取验证码", "发送验证码", "获取短信验证码", "获取校验码")
_BIND_CONFIRM_AIDS = ("ConfirmBtn", "SetBtn", "BindBtn", "OkBtn", "SubmitBtn")
# ⚠️ 确认按钮的**名字候选里刻意不放"绑定"**：弹窗标题就是「绑定手机号」，
# 按名字搜会把标题那块 Text 当成按钮 —— 点下去什么都没发生，而脚本以为提交过了。
_BIND_CONFIRM_NAMES = ("确定", "确认", "提交", "完成")
_BIND_CANCEL_AIDS = ("CancelBtn", "closebtn")
_BIND_CANCEL_NAMES = ("取消", "关闭", "以后再说", "暂不", "跳过")


def _bind_dump_path():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent / "reports" / "bind_ui_dump"


def dump_bind_tree(pid: int, reason: str) -> str:
    """把**本进程所有顶层窗**的控件树落盘（含 control_type / aid / name / 矩形）。

    比 `dump_microsoft_tree` 宽：绑定弹窗可能是主窗口下的子 Window，
    也可能是独立顶层窗，全落下来才能一次看清它挂在哪、控件叫什么。
    """
    from datetime import datetime

    lines = [
        f"# {reason}",
        f"# {datetime.now().isoformat(timespec='seconds')}",
        f"# pid={pid}",
    ]
    for h in _top_hwnds():
        if _hwnd_pid(h) != pid:
            continue
        try:
            win = UIAWrapper(UIAElementInfo(h))
        except Exception:
            continue
        try:
            title = win.window_text()
        except Exception:
            title = ""
        lines.append(f"===== hwnd={h} visible={_hwnd_visible(h)} title={title!r} =====")
        for node in _descendants(win):
            try:
                info = node.element_info
                try:
                    rect = node.rectangle()
                    box = f"({rect.left},{rect.top},{rect.width()}x{rect.height()})"
                except Exception:
                    box = "-"
                lines.append(
                    f"{info.control_type or '?':14} aid={info.automation_id or '':30} "
                    f"name={popup_text(info.name or '')[:100]!r:104} {box}"
                )
            except Exception:
                continue

    target = _bind_dump_path() / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines), encoding="utf-8")
        return str(target)
    except OSError:
        return ""


def _bind_snapshot(scope) -> list[tuple[str, str, str, object]]:
    """(aid, control_type, name, node) 四元组快照。**一次遍历拿全**，
    后面的候选匹配全在内存里做 —— 每个候选各扫一遍整棵树的话，
    这个弹窗上轮询几轮就把超时耗光了。"""
    out: list[tuple[str, str, str, object]] = []
    for node in _descendants(scope):
        try:
            info = node.element_info
            out.append(
                (
                    info.automation_id or "",
                    info.control_type or "",
                    popup_text(info.name or ""),
                    node,
                )
            )
        except Exception:
            continue
    return out


def _bind_find(scope, aids: tuple[str, ...], names: tuple[str, ...], control_types=None):
    """弹窗里找一个控件：**先 aid 全等、再名字包含**；都找不到返回 None。"""
    snapshot = _bind_snapshot(scope)

    def _ok(ctype: str) -> bool:
        return control_types is None or ctype in control_types

    for aid in aids:
        for node_aid, ctype, _name, node in snapshot:
            if node_aid == aid and _ok(ctype):
                return node
    for keyword in names:
        for _aid, ctype, name, node in snapshot:
            if keyword in name and _ok(ctype):
                return node
    return None


def _bind_find_button(scope, aids: tuple[str, ...], names: tuple[str, ...]):
    """按钮类控件：**先只认 `Button`，再放开任意类型**。

    大厅的按钮常常是 `Button` 里套一层 `Text`；但两种都可能出现，
    所以先严格后宽松。宽松那轮是退路，不放在前面 —— 否则会先把
    同名的一行说明文字当成按钮点下去。
    """
    return _bind_find(scope, aids, names, ("Button",)) or _bind_find(scope, aids, names, None)


def bind_phone_field(scope):
    return _bind_find(scope, _BIND_PHONE_AIDS, (), ("Edit",))


def bind_code_field(scope):
    return _bind_find(scope, _BIND_CODE_AIDS, (), ("Edit",))


def bind_send_button(scope):
    return _bind_find_button(scope, _BIND_SEND_AIDS, _BIND_SEND_NAMES)


def bind_confirm_button(scope):
    return _bind_find_button(scope, _BIND_CONFIRM_AIDS, _BIND_CONFIRM_NAMES)


def bind_cancel_button(scope):
    return _bind_find_button(scope, _BIND_CANCEL_AIDS, _BIND_CANCEL_NAMES)


def _bind_signature(scope) -> bool:
    """这个窗口现在看起来是不是「绑定手机号弹窗」。

    要求**同时**满足：有绑定文案 + 有一个手机号框或发送按钮。
    只看文案会误命中主窗口里别处的字样（弹窗是主窗口子窗时尤其危险）。
    """
    if not any(m in n for n in _names_of(scope) for m in _BIND_MARKERS):
        return False
    return bind_phone_field(scope) is not None or bind_send_button(scope) is not None


def find_bind_dialog(pid: int, timeout_sec: float = BIND_DIALOG_TIMEOUT_SEC):
    """等「绑定手机号」弹窗出现；没出现返回 None。

    **没弹不是失败** —— 已经绑过手机号的账号不会再弹，那是正常情况。
    """
    deadline = time.time() + timeout_sec
    while True:
        for h in _top_hwnds():
            if not _hwnd_visible(h) or _hwnd_pid(h) != pid:
                continue
            try:
                scope = UIAWrapper(UIAElementInfo(h))
            except Exception:
                continue
            if _bind_signature(scope):
                return scope
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


def fill_bind_phone(scope, phone: str) -> None:
    """填手机号 → 点「获取验证码」。

    填号用 `set_edit_text` + 真键盘补一遍：大厅的输入框是自绘的，
    只改 ValuePattern 时界面上的 React/自绘层未必收到 input 事件，
    按钮可能一直停在不可点状态（微软那个 OTP 页就是这么坑的，见 `submit_microsoft_code`）。
    """
    box = None
    deadline = time.time() + BIND_CONTROL_TIMEOUT_SEC
    while time.time() < deadline and box is None:
        box = bind_phone_field(scope)
        if box is None:
            time.sleep(0.5)
    if box is None:
        dumped = dump_bind_tree(_scope_pid(scope), "找不到绑定弹窗的手机号输入框")
        raise LaunchError(
            "绑定手机号：找不到手机号输入框"
            + (f"，控件树已落盘：{dumped}" if dumped else "")
        )

    box.set_edit_text(phone)
    try:
        box.set_focus()
        box.type_keys("^a")
        box.type_keys(phone, with_spaces=True)
    except Exception:
        pass  # 退路失败不致命，set_edit_text 大概率已经进去了

    send = None
    deadline = time.time() + BIND_CONTROL_TIMEOUT_SEC
    while time.time() < deadline and send is None:
        send = bind_send_button(scope)
        if send is None:
            time.sleep(0.5)
    if send is None:
        dumped = dump_bind_tree(_scope_pid(scope), "找不到绑定弹窗的「获取验证码」按钮")
        raise LaunchError(
            "绑定手机号：找不到「获取验证码」按钮"
            + (f"，控件树已落盘：{dumped}" if dumped else "")
        )
    if not _press_button(send):
        dumped = dump_bind_tree(_scope_pid(scope), "「获取验证码」按钮点不动")
        raise LaunchError(
            "绑定手机号：「获取验证码」按钮点不动"
            + (f"，控件树已落盘：{dumped}" if dumped else "")
        )


def submit_bind_code(scope, code: str) -> None:
    """填短信验证码 → 点确定 → **等弹窗真的消失**才算成功。"""
    box = bind_code_field(scope)
    if box is None:
        dumped = dump_bind_tree(_scope_pid(scope), "找不到绑定弹窗的验证码输入框")
        raise LaunchError(
            "绑定手机号：找不到验证码输入框"
            + (f"，控件树已落盘：{dumped}" if dumped else "")
        )
    box.set_edit_text(code)
    try:
        box.set_focus()
        box.type_keys("^a")
        box.type_keys(code, with_spaces=True)
    except Exception:
        pass

    confirm = None
    deadline = time.time() + BIND_CONTROL_TIMEOUT_SEC
    while time.time() < deadline and confirm is None:
        confirm = bind_confirm_button(scope)
        if confirm is None:
            time.sleep(0.5)
    if confirm is None:
        dumped = dump_bind_tree(_scope_pid(scope), "找不到绑定弹窗的确定按钮")
        raise LaunchError(
            "绑定手机号：找不到确定/绑定按钮"
            + (f"，控件树已落盘：{dumped}" if dumped else "")
        )
    if not _press_button(confirm):
        dumped = dump_bind_tree(_scope_pid(scope), "绑定弹窗的确定按钮点不动")
        raise LaunchError(
            "绑定手机号：确定按钮点不动"
            + (f"，控件树已落盘：{dumped}" if dumped else "")
        )

    pid = _scope_pid(scope)
    deadline = time.time() + BIND_SUBMIT_TIMEOUT_SEC
    while time.time() < deadline:
        if find_bind_dialog(pid, timeout_sec=0) is None:
            return
        time.sleep(0.5)

    dumped = dump_bind_tree(pid, "点了确定但绑定弹窗没消失")
    raise LaunchError(
        "绑定手机号：点了确定，但弹窗没消失"
        f"（{BIND_SUBMIT_TIMEOUT_SEC}s）—— 验证码可能不对，或按钮没点生效"
        + (f"，控件树已落盘：{dumped}" if dumped else "")
    )


def _scope_pid(scope) -> int:
    """从 scope 窗口反查 pid。取不到返回 0（落盘/轮询会因此退化成空转，不抛）。"""
    try:
        return int(scope.element_info.process_id)
    except Exception:
        return 0


def close_bind_dialog(scope) -> bool:
    """尽力关掉绑定弹窗，**关不掉返回 False、不抛**。

    为什么要兜：模态窗留在屏幕上会把**后面每一条**用例都卡住
    （2026-09-22 微软登录窗就是这么连累短信那条的）。
    收尾失败不该盖掉用例本身那个真正的失败。
    """
    for finder in (bind_cancel_button,):
        try:
            btn = finder(scope)
        except Exception:
            btn = None
        if btn is not None and _press_button(btn):
            return True
    try:
        win32gui.PostMessage(scope.handle, 0x0010, 0, 0)  # WM_CLOSE
        return True
    except Exception:
        return False


def dismiss_bind_dialog(pid: int, timeout_sec: float = 0.0) -> bool:
    """弹了绑定手机号就关掉，**不绑**。返回是否真关掉了一个。

    **给无人值守的场合用**：那里没人能输短信码，`handle_bind_phone_popup` 那条
    「填号 → 发码 → 问人要码」的路走不通，只能把它关掉、别让它挡着后续用例。

    `timeout_sec` 默认 **0** = 只探一次、不等待。这里要的是「顺手清掉残留」，
    不是「等它出现」—— 等待会把每条登出用例都拖慢 `BIND_DIALOG_TIMEOUT_SEC`。
    """
    scope = find_bind_dialog(pid, timeout_sec=timeout_sec)
    if scope is None:
        return False
    return close_bind_dialog(scope)


def handle_bind_phone_popup(pid: int, phone: str, ask_code, timeout_sec: float = BIND_DIALOG_TIMEOUT_SEC) -> str:
    """登录后如果弹了「绑定手机号」，自动走完：填号 → 获取验证码 → 问人要码 → 确定。

    返回：
      `"absent"`  —— 没弹（账号已绑过）。**正常情况，不是失败。**
      `"bound"`   —— 绑好了。
      `"skipped"` —— 弹了但没绑成：没配手机号，或人回车放弃了。

    `ask_code` 是**回调**（测试里传 `_ask_code`），这里不 `input()` ——
    全项目只有测试文件那一处读 stdin，那一处负责「先切窗口再问人」，
    散着写会漏掉切窗口而且**不报错**（守卫：`tests/unit/test_manual_loop_policy.py`）。

    ⚠️ 号码走环境变量（`HALL_BIND_PHONE`，回退 `HALL_TEST_USER`），**不落盘、不进 git**。
    """
    scope = find_bind_dialog(pid, timeout_sec=timeout_sec)
    if scope is None:
        return "absent"

    if not phone:
        close_bind_dialog(scope)
        print(
            "⚠️ 弹出了绑定手机号弹窗，但没配号码（HALL_BIND_PHONE / HALL_TEST_USER 都是空）"
            "—— 已关掉弹窗，本次不绑。"
        )
        return "skipped"

    try:
        fill_bind_phone(scope, phone)
        code = ask_code(f"已向 {phone} 发送绑定验证码。输入手机收到的验证码（直接回车放弃绑定）: ")
        if not code:
            close_bind_dialog(scope)
            print("⚠️ 人工放弃绑定手机号（验证码已发出），已关掉弹窗。")
            return "skipped"
        submit_bind_code(scope, code)
        return "bound"
    except Exception:
        # 任何一步挂了都要把弹窗收掉 —— 否则后面每条用例都点不动。
        close_bind_dialog(scope)
        raise
