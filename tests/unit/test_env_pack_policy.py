"""锁节点侧环境变量装配策略（hall_auto/env_pack.py）。

这段逻辑决定**节点机上的用例能不能真跑**：密码登录 / SSO / 改密码这些套件靠环境变量拿凭据，
装配错了就在报告上表现为一片 skip 黄 —— 看不出是"机器没配"还是"用例真跳过"。

最关键的一条：**密码绝不从共享盘上的任务文件读**（共享盘全组可读）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hall_auto import env_pack

pytestmark = pytest.mark.unit


def test_parse_env_text_basic():
    text = """
    # 注释行
    HALL_TEST_USER=13800000000

    HALL_TEST_PASSWORD = secret123
    """
    parsed = env_pack.parse_env_text(text)
    assert parsed["HALL_TEST_USER"] == "13800000000"
    assert parsed["HALL_TEST_PASSWORD"] == "secret123", "等号两侧空白要去掉"


def test_parse_env_text_last_wins():
    """重复键后者覆盖前者 —— 便于文件末尾临时覆盖。"""
    parsed = env_pack.parse_env_text("K=1\nK=2\n")
    assert parsed["K"] == "2"


def test_parse_env_text_ignores_garbage():
    """没有等号的行、空 key 都忽略，不抛异常 —— 手写的凭据文件难免有杂物。"""
    parsed = env_pack.parse_env_text("这是句废话\n=novalue\nOK=fine\n")
    assert parsed == {"OK": "fine"}


def test_load_node_env_missing_file_is_empty(tmp_path):
    """文件不存在返回空字典，不报错 —— 只跑只读套件的机器不需要凭据文件。"""
    assert env_pack.load_node_env(tmp_path / "nope.env") == {}


def test_task_env_accepts_allowlisted():
    accepted, rejected = env_pack.sanitize_task_env({
        "HALL_ALLOW_INSTALL": "1",
        "HALL_NODE_ID": "R01",
    })
    assert accepted == {"HALL_ALLOW_INSTALL": "1", "HALL_NODE_ID": "R01"}
    assert rejected == []


def test_task_env_rejects_secrets():
    """**红线**：任务文件躺在共享盘上，密码写进去等于公开。必须拒收。"""
    accepted, rejected = env_pack.sanitize_task_env({
        "HALL_TEST_PASSWORD": "oops",
        "HALL_SHARE_PASSWORD": "oops",
        "HALL_TEST_USER": "13800000000",
    })
    assert accepted == {}, "敏感项一个都不许从任务文件进"
    assert set(rejected) == {"HALL_TEST_PASSWORD", "HALL_SHARE_PASSWORD", "HALL_TEST_USER"}


def test_task_env_rejects_unknown_keys():
    accepted, rejected = env_pack.sanitize_task_env({"PATH": "/evil", "HALL_ALLOW_INSTALL": "0"})
    assert accepted == {"HALL_ALLOW_INSTALL": "0"}
    assert "PATH" in rejected, "白名单外的一律忽略，不许改 PATH 这类"


def test_task_env_non_dict_is_ignored():
    accepted, rejected = env_pack.sanitize_task_env("字符串不是映射")
    assert accepted == {} and rejected == []


def test_build_env_priority_process_beats_file_beats_task(tmp_path, monkeypatch):
    """优先级：节点进程已有 env > 任务文件 env > 节点本地凭据文件。

    进程已有值最高，是为了不覆盖人在那台机器上显式设的值（调试时想换账号复现）。
    """
    env_file = tmp_path / "farm_node.env"
    env_file.write_text("HALL_NODE_ID=from-file\nHALL_TEST_USER=fileuser\n", encoding="utf-8")

    monkeypatch.setenv("HALL_NODE_ID", "from-process")

    env, notes = env_pack.build_suite_env(
        task_env={"HALL_NODE_ID": "from-task"},
        node_env_path=env_file,
    )
    assert env["HALL_NODE_ID"] == "from-process", "进程环境优先"
    assert env["HALL_TEST_USER"] == "fileuser", "凭据来自节点本地文件"
    assert any("凭据文件已加载" in n for n in notes)


def test_build_env_task_overrides_file_when_no_process_value(tmp_path, monkeypatch):
    """进程没设时，任务文件（非敏感）应盖过本地文件里的同名项。"""
    monkeypatch.delenv("HALL_ALLOW_INSTALL", raising=False)
    env_file = tmp_path / "farm_node.env"
    env_file.write_text("HALL_ALLOW_INSTALL=0\n", encoding="utf-8")
    env, _ = env_pack.build_suite_env(
        task_env={"HALL_ALLOW_INSTALL": "1"}, node_env_path=env_file
    )
    assert env["HALL_ALLOW_INSTALL"] == "1"


def test_build_env_sets_utf8_defaults(tmp_path):
    env, _ = env_pack.build_suite_env(node_env_path=tmp_path / "none.env")
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_build_env_reports_rejected_notes(tmp_path):
    _, notes = env_pack.build_suite_env(
        task_env={"HALL_TEST_PASSWORD": "x"}, node_env_path=tmp_path / "none.env"
    )
    assert any("忽略不白名单项" in n for n in notes), "拒收的项要显式说出来，别悄悄吞"


def test_credential_status_detects_missing():
    """凭据状态要能如实反映缺什么 —— 报告靠它区分 skip 根因。"""
    status = env_pack.credential_status({})
    assert status == {
        "password_login": False,
        "microsoft_sso": False,
        "change_password": False,
        "share_creds": False,
    }


def test_credential_status_detects_present():
    status = env_pack.credential_status({
        "HALL_TEST_USER": "u",
        "HALL_TEST_PASSWORD": "p",
        "HALL_MS_USER": "a@b.com",
        "HALL_SHARE_USER": "hallshare",
        "HALL_SHARE_PASSWORD": "s",
    })
    assert status["password_login"] is True
    assert status["microsoft_sso"] is True
    assert status["share_creds"] is True
    assert status["change_password"] is False, "改密码用的临时密码没给，如实报 False"


def test_credential_status_needs_both_user_and_password():
    """只有用户名不算配好 —— 缺密码时登录用例照样 skip，报 True 会误导。"""
    status = env_pack.credential_status({"HALL_TEST_USER": "u"})
    assert status["password_login"] is False
