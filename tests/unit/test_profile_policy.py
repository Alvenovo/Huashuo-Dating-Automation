"""锁环境画像的兼容性字段（hall_auto/profile.py）。

组长定的验证目标是「版本放量前的兼容性验证」，报告要按环境维度分组说
「哪些环境有问题」。WebView2 与微软会话是其中最容易**把环境缺失误判成产品缺陷**
的两项 —— 缺 WebView2 时首页永远不到就绪，看起来像产品 bug，实际是这台机器缺运行时。
"""

from __future__ import annotations

from unittest import mock

from hall_auto import profile


def test_webview2_version_reads_pv():
    with mock.patch.object(profile, "_read_reg", return_value="153.0.4234.32"):
        assert profile.webview2_version() == "153.0.4234.32"
        assert profile.webview2_installed() is True


def test_webview2_missing_returns_empty():
    with mock.patch.object(profile, "_read_reg", return_value=""):
        assert profile.webview2_version() == ""
        assert profile.webview2_installed() is False


def test_webview2_falls_back_to_user_hive():
    """用户级安装是合法形态（HKCU）。前两个 HKLM 位置都没有时要去查 HKCU。"""
    calls: list[bool] = []

    def _fake(rel_path, value_name, *, current_user=False):
        calls.append(current_user)
        return "1.2.3" if current_user else ""

    with mock.patch.object(profile, "_read_reg", side_effect=_fake):
        assert profile.webview2_version() == "1.2.3"
    assert calls[-1] is True, "最后一步该查 HKCU"


def test_hall_installed_version():
    with mock.patch.object(profile, "_read_reg", return_value="1.6.11.4"):
        assert profile.hall_installed_version() == "1.6.11.4"


def test_reg_read_swallows_oserror():
    """注册表键不存在/无权限要返回空串，不能抛 —— 画像采集失败不能拖垮跑批。"""
    import winreg

    with mock.patch.object(winreg, "OpenKey", side_effect=OSError("拒绝访问")):
        assert profile._read_reg(r"SOFTWARE\\Nope", "pv") == ""


def test_ms_session_no_localappdata_is_unknown():
    """拿不到 LOCALAPPDATA 时返回 unknown，**不谎报 no**（unknown 与 no 结论不同）。"""
    with mock.patch.dict("os.environ", {}, clear=True):
        assert profile.ms_login_session() == "unknown"


def test_ms_session_detects_entries(tmp_path):
    oneauth = tmp_path / "Microsoft" / "OneAuth"
    oneauth.mkdir(parents=True)
    (oneauth / "some-cache-file").write_text("x", encoding="utf-8")
    with mock.patch.dict("os.environ", {"LOCALAPPDATA": str(tmp_path)}):
        assert profile.ms_login_session() == "yes"


def test_ms_session_empty_dirs_is_no(tmp_path):
    (tmp_path / "Microsoft" / "OneAuth").mkdir(parents=True)
    (tmp_path / "Microsoft" / "IdentityCache").mkdir(parents=True)
    with mock.patch.dict("os.environ", {"LOCALAPPDATA": str(tmp_path)}):
        assert profile.ms_login_session() == "no"


def test_compat_facts_shape():
    """兼容性事实必须齐这些键 —— 报告的"节点环境"表按这些列取值。"""
    with mock.patch.object(profile, "webview2_version", return_value="1.2.3"), \
         mock.patch.object(profile, "hall_installed_version", return_value="1.6.11.4"), \
         mock.patch.object(profile, "ms_login_session", return_value="yes"), \
         mock.patch.object(profile, "ocr_has_chinese", return_value="yes"):
        facts = profile.compat_facts()
    assert facts == {
        "webview2": "yes",
        "webview2_version": "1.2.3",
        "hall_installed": "yes",
        "hall_version": "1.6.11.4",
        "ms_session": "yes",
        "ocr_zh": "yes",
    }


def test_machine_profile_includes_compat_facts():
    """machine_profile() 必须带兼容性字段 —— 这是它过去缺的那两块，导致无法按环境分组。"""
    from hall_auto import dpi

    prof = dpi.machine_profile()
    for key in ("webview2", "webview2_version", "hall_installed", "ms_session", "ocr_zh"):
        assert key in prof, f"画像缺字段 {key}"


def test_machine_profile_survives_compat_failure():
    """兼容性探测炸了也不能让 profile 挂 —— 只是退化成 unknown，其余字段照常返回。"""
    from hall_auto import dpi

    with mock.patch("hall_auto.profile.compat_facts", side_effect=RuntimeError("注册表被锁")):
        prof = dpi.machine_profile()
    assert prof["webview2"] == "unknown"
    assert prof["ms_session"] == "unknown"
    assert prof["os"], "基础字段仍要有"
    assert prof["screen"], "基础字段仍要有"
