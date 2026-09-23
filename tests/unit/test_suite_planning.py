"""多机跑批：套件定义 / 分片规划 / 坐标矩阵 的纯逻辑单测。

这些规则是防止「20 台同时打服务端产出假失败」的关键参数，锁死它们避免被改坏。
"""

from __future__ import annotations

import os
import time

import pytest

from hall_auto import suites
from hall_auto.suites import SUITES, farm_suites, get_suite, plan_shards


@pytest.mark.unit
def test_all_suites_have_unique_names():
    for key, suite in SUITES.items():
        assert suite.name == key, key


@pytest.mark.unit
def test_manual_suite_never_farm_safe():
    """人在环用例（会真发短信、真改密码）永远不能进无人值守农场。"""
    assert get_suite("login-manual").farm_safe is False
    assert "login-manual" not in {s.name for s in farm_suites()}


@pytest.mark.unit
def test_login_suite_marker_excludes_manual_tests():
    """`login` 套件的 marker 必须排除 manual —— 漏了会把农场节点卡死、还真发短信。

    `test_sms_login_manual` / `test_forgot_password_reset_manual` **同时带
    `login` + `manual` 两个 marker**，光写 `"login"` 就会被选进来。
    唯一守卫是 `sys.stdin.isatty()`，而 `farm_agent` 起 pytest 时**只重定向了
    stdout/stderr、没重定向 stdin** → 交互式终端下子进程 isatty 为真 → 不 skip →
    `input()` 永久阻塞 + 真发短信，那台机器再也取不到下一个任务。

    2026-09-22 实测：`-m login` 收集 17 条，其中 2 条是人在环。
    `pytest.ini` 里也明写「manual …无人值守套件一律排除」，这条守卫让两边对齐。
    """
    marker = get_suite("login").marker
    assert "not manual" in marker, (
        f"login 套件的 marker 是 {marker!r} —— 没排除 manual。"
        "投进农场会让节点卡在等短信验证码的 input() 上，整台机器再也取不到任务。"
    )


@pytest.mark.unit
def test_login_suite_still_covers_the_plain_login_cases():
    """排除 manual 别把普通登录用例也排掉了 —— 那是这个套件存在的理由。"""
    marker = get_suite("login").marker
    assert "login" in marker.replace("not manual", ""), f"marker 里没有 login：{marker!r}"
    assert "tests/launch/test_p1_login.py" in get_suite("login").paths


@pytest.mark.unit
def test_server_pressure_suites_are_limited():
    """打服务端 / 厂商 CDN 的套件必须限流；不限并发会被限流产出假失败。"""
    for name in ("install", "apps-lifecycle", "login", "wb"):
        suite = get_suite(name)
        assert suite.parallel == "limited", name
        assert suite.concurrency() >= 1, name
        assert suite.max_concurrent <= 8, f"{name} 并发上限过高，会打爆服务端"


@pytest.mark.unit
def test_readonly_suites_are_full_parallel():
    """只读套件不碰服务端，应全并行（concurrency() 返回 0 表示不限）。"""
    for name in ("unit", "launch", "apps-detect", "settings", "security"):
        assert get_suite(name).parallel == "full", name
        assert get_suite(name).concurrency() == 0, name


@pytest.mark.unit
def test_serial_suite_concurrency_is_one():
    assert get_suite("login-manual").concurrency() == 1


@pytest.mark.unit
def test_get_suite_rejects_unknown_name():
    with pytest.raises(KeyError):
        get_suite("no-such-suite")


@pytest.mark.unit
def test_build_command_single_shard_omits_shard_args():
    argv = suites.build_command(get_suite("launch"))
    assert not any(a.startswith("--shard") for a in argv)


@pytest.mark.unit
def test_build_command_multi_shard_includes_shard_args():
    argv = suites.build_command(get_suite("launch"), shard=2, of=5)
    assert "--shard-id=2" in argv
    assert "--shard-count=5" in argv


@pytest.mark.unit
def test_plan_shards_full_parallel_covers_every_node():
    """不限并发的套件，每台机器都跑完整一套（分片数 1）。"""
    nodes = ["R01", "R02", "R03"]
    plan = plan_shards(["launch"], nodes)
    assert [(n, s, i, c) for n, s, i, c in plan.assignments] == [
        ("R01", "launch", 1, 1),
        ("R02", "launch", 1, 1),
        ("R03", "launch", 1, 1),
    ]


@pytest.mark.unit
def test_plan_shards_limited_respects_concurrency_and_rotates():
    """限流套件：分片数 = 并发上限，节点按取模轮转，不重叠且合起来覆盖全集。"""
    nodes = [f"R{i:02d}" for i in range(1, 9)]  # 8 台
    plan = plan_shards(["apps-lifecycle"], nodes)
    assigned = [(n, i, c) for n, _, i, c in plan.assignments]
    limit = get_suite("apps-lifecycle").concurrency()
    assert all(c == limit for _, _, c in assigned)
    assert [i for _, i, _ in assigned] == [1, 2, 3, 4, 5, 1, 2, 3]  # 8 台轮转 5 片
    assert max(i for _, i, _ in assigned) == limit


@pytest.mark.unit
def test_plan_shards_limited_never_exceeds_node_count():
    """节点数少于并发上限时，分片数收敛到节点数，不产生空分片。"""
    nodes = ["R01", "R02"]
    plan = plan_shards(["login"], nodes)
    assert all(c == 2 for _, _, _, c in plan.assignments)
    assert {i for _, _, i, _ in plan.assignments} == {1, 2}


@pytest.mark.unit
def test_plan_shards_serial_goes_to_first_node_only():
    plan = plan_shards(["login-manual"], ["R01", "R02", "R03"])
    assert len(plan.assignments) == 1
    assert plan.assignments[0][0] == "R01"


@pytest.mark.unit
def test_plan_shards_rejects_empty_nodes():
    with pytest.raises(ValueError):
        plan_shards(["launch"], [])


@pytest.mark.unit
def test_plan_shards_multiple_suites_all_assigned():
    plan = plan_shards(["launch", "security", "login"], ["R01", "R02"])
    assigned_suites = {s for _, s, _, _ in plan.assignments}
    assert assigned_suites == {"launch", "security", "login"}


@pytest.mark.unit
def test_plan_shards_dict_shape():
    plan = plan_shards(["login-manual"], ["R01"])
    payload = plan.to_dict()
    assert payload["total_nodes"] == 1
    assert payload["assignments"][0]["suite"] == "login-manual"


# ---------- 全量档（dispatch --suites all）----------

# 全量档**刻意排除**的套件。2026-09-23 起只剩人在环这一个 ——
# `install` / `apps-lifecycle` 曾经也在这儿，理由是「farm_agent 非提权 → 投了必挂」，
# 现在它们走提权通道（`hall_auto/elevation.py`，计划任务 `HallAutoP1`）执行，
# 仍然 farm_safe。改这个名单前先读 `hall_auto/suites.py` 里 FULL_RUN_SUITES 上面的理由。
FULL_RUN_EXCLUDED = {"login-manual"}


@pytest.mark.unit
@pytest.mark.parametrize("keyword", ["all", "ALL", "full", "Full"])
def test_full_run_keywords_expand_to_the_full_run_set(keyword):
    """`--suites all` 是「一条命令投完」的入口，大小写都得认。"""
    assert suites.resolve_suite_names(keyword) == list(suites.FULL_RUN_SUITES)


@pytest.mark.unit
def test_full_run_never_contains_the_manual_suite():
    """**关键保护**：全量档混进人在环套件，会让「一条命令跑完」变成「一条命令卡死」——
    节点无人应答却问了「要不要参与人在环」，永久 `input()` 阻塞。

    这条红了**别改断言**，去改 `FULL_RUN_SUITES`。
    """
    assert "login-manual" not in suites.FULL_RUN_SUITES
    for name in suites.FULL_RUN_SUITES:
        assert get_suite(name).farm_safe, name
        assert not get_suite(name).interactive, f"{name} 是人在环，不该进全量档"


@pytest.mark.unit
def test_full_run_includes_the_elevation_suites():
    """**反向锁**：`install` / `apps-lifecycle` 必须在全量档里。

    它们不在的话，跑批又要退回「先跑全量档、再开管理员窗口补跑」那三段式 ——
    正是 2026-09-23 这次改造要消灭的东西。它们能进是因为走提权通道执行，
    不是靠把 agent 整体提权（那会改掉 launch/login 的权限上下文）。
    """
    for name in ("install", "apps-lifecycle"):
        assert get_suite(name).needs_elevation, f"{name} 没标 needs_elevation"
        assert name in suites.FULL_RUN_SUITES, f"{name} 不在全量档里"


@pytest.mark.unit
def test_elevation_suites_run_last():
    """**顺序即正确性**：`install` 会卸载并重装大厅本体，跑完登录态/主题档位全重置。

    它排在只读套件前面的话，`launch` / `login` / `settings` 全部跑在刚重装的机器上，
    结果对不对没人说得清。所以全量档里提权段必须在最后，且 `install` 是最后一个。
    """
    names = list(suites.FULL_RUN_SUITES)
    ranks = [1 if get_suite(n).needs_elevation else 0 for n in names]
    assert ranks == sorted(ranks), f"提权套件没排在最后：{names}"
    assert names[-1] == "install", f"最后一个必须是 install（它会重装大厅）：{names[-1]!r}"


@pytest.mark.unit
def test_execution_order_puts_elevated_suites_last():
    """手写组合（`--suites login,install`）也要被纠正成「先只读、后提权」。

    靠"投任务的人记得按顺序写套件名"是不可靠的：写反了不会报错，
    只会让 login 跑在一台刚被重装过大厅的机器上。
    """
    assert suites.execution_order(["install", "login", "launch"]) == ["login", "launch", "install"]
    assert suites.execution_order(["apps-lifecycle", "unit"]) == ["unit", "apps-lifecycle"]
    # 组内保序（稳定排序）：不能顺手把只读套件的相对顺序也打乱
    assert suites.execution_order(["wb", "unit", "launch"]) == ["wb", "unit", "launch"]


@pytest.mark.unit
def test_execution_order_tolerates_unknown_suite_names():
    """未知套件名不能在这里抛异常。

    排序发生在**第一个套件之前**，抛了就是整批一条都不跑；
    真错应该在执行到它时报出来，那时前面跑完的还有结果。
    """
    assert suites.execution_order(["no-such-suite", "install"]) == ["no-such-suite", "install"]


@pytest.mark.unit
def test_apps_lifecycle_resets_fixtures_before_running():
    """夹具复位必须跟着套件定义走。

    上一次装剩的夹具会让装卸用例**直接 skip**（不报错），报告上一片黄而人以为"跑过了"。
    原来这条顺序写死在 `run_p1_apps.ps1` 里，换成提权通道后就没人做了 ——
    所以它必须是套件自己的属性，提权执行器照着做。
    """
    assert get_suite("apps-lifecycle").reset_fixture is True
    # 大厅自身装卸不该顺带复位夹具：它验的是安装包，夹具在不在与它无关
    assert get_suite("install").reset_fixture is False


@pytest.mark.unit
def test_every_runnable_suite_is_either_in_full_run_or_documented_excluded():
    """反向锁：以后新增了能无人值守跑的套件，忘了加进全量档，
    现场就又是「跑着跑着停了」—— 那正是全量档要消灭的症状。
    """
    for name, suite in SUITES.items():
        if not suite.farm_safe:
            continue
        assert name in suites.FULL_RUN_SUITES or name in FULL_RUN_EXCLUDED, (
            f"套件 {name} 既不在全量档、也不在排除名单里 —— 二选一，别悬着"
        )


@pytest.mark.unit
def test_resolve_suite_names_keeps_order_and_dedupes():
    """保序 + 去重。去重不能省：`launch,launch` 会让节点把同一套件跑两遍，
    回执里两条同名记录看起来像「跑了两轮」，排查时会被带偏。
    """
    assert suites.resolve_suite_names("settings,launch,settings") == ["settings", "launch"]


@pytest.mark.unit
def test_resolve_suite_names_ignores_blank_tokens():
    assert suites.resolve_suite_names(" launch , , apps-detect ") == ["launch", "apps-detect"]


@pytest.mark.unit
def test_resolve_suite_names_can_mix_keyword_and_names():
    """`all,install` 这种混写：关键字展开后仍保序，且不重复。"""
    names = suites.resolve_suite_names("all,unit")
    assert names == list(suites.FULL_RUN_SUITES)


@pytest.mark.unit
def test_interactive_suite_gets_dash_s():
    """**关键保护**：人在环套件的 pytest 命令必须带 `-s`。

    2026-09-22 真机踩出：pytest 默认捕获会把 `sys.stdin` 换成 `DontReadFromInput`，
    于是测试体里的 `sys.stdin.isatty()` **恒为 False** —— 三条人在环用例全 skip，
    而 `farm_agent` 照打「退出码 0」，看着像「跑过了、只是没到条件」，
    **实际是这套功能从没执行过**。就算绕过那个守卫，`input()` 也会抛
    `OSError: pytest: reading from stdin while output is captured! Consider using -s.`

    这条红了**别改断言**，去改 `hall_auto/suites.py` 的 `build_command`。
    """
    argv = suites.build_command(get_suite("login-manual"))
    assert "-s" in argv, f"人在环套件没带 -s，整套会静默全 skip：{argv}"


@pytest.mark.unit
def test_only_interactive_suites_get_dash_s():
    """反向锁：`-s` 只给人在环套件。

    无人值守套件带上 `-s`，pytest 就不再捕获它的输出，证据目录里的输出段会变空 ——
    不致命，但等于白白多出「这轮为什么没输出」一个排查方向。
    """
    for name, suite in SUITES.items():
        has_dash_s = "-s" in suites.build_command(suite)
        assert has_dash_s is suite.interactive, (
            f"{name}: 命令里 -s={has_dash_s}，但 interactive={suite.interactive} —— 两边对不上"
        )


@pytest.mark.unit
def test_interactive_suites_are_never_farm_safe():
    """人在环 ⇒ 绝不进无人值守农场。两个标记互斥。

    写反了的后果是农场卡死：节点无人应答却问了「要不要参与人在环」，
    或者反过来 —— 人在环套件被当成无人值守投出去，节点在 `input()` 上永久阻塞。
    """
    for name, suite in SUITES.items():
        if suite.interactive:
            assert not suite.farm_safe, f"{name} 标了 interactive 却还是 farm_safe"


@pytest.mark.unit
def test_login_manual_keeps_its_interactive_flag():
    """`login-manual` 的 interactive 位别被顺手删掉 —— 删了 `-s` 就没了，整套静默失效。"""
    assert get_suite("login-manual").interactive is True


# ---------- pytest 的临时目录 / 缓存隔离（2026-09-23：238 条 ERROR 的根因）----------
#
# bootstrap 第 12 步的自检是**管理员**跑的，而 pytest 默认把临时根放在
# `%LOCALAPPDATA%\Temp\pytest-of-<user>`、按用户名复用。提权进程建出来的目录
# owner 是 `BUILTIN\Administrators`，之后**普通权限**（UAC 过滤令牌里 Administrators
# 是 deny-only）写不进去 → `tmp_path` fixture 在 setup 阶段 `PermissionError`，
# 凡是用了 `tmp_path` 的用例全崩。真机后果：新机第一次跑农场，`unit` 套件
# **238 / 599 条 ERROR**，报告上看着像"代码烂了"。
#
# 修法不是"事后清目录"（每台新机 bootstrap 都会重新污染），而是**根本不用那个共享目录**。


@pytest.mark.unit
def test_build_command_disables_pytest_cache():
    """关掉 pytest 缓存插件 —— 它会往仓库根写 `.pytest_cache/`，有同样的 ACL 污染问题。"""
    argv = suites.build_command(get_suite("launch"))
    assert "-p" in argv and "no:cacheprovider" in argv, f"没关掉 cacheprovider：{argv}"


@pytest.mark.unit
def test_build_command_uses_the_given_basetemp():
    argv = suites.build_command(get_suite("launch"), basetemp="reports/_pytest_tmp/X")
    assert "--basetemp=reports/_pytest_tmp/X" in argv


@pytest.mark.unit
def test_build_command_omits_basetemp_when_not_given():
    """不给就不加 —— 免得调用方以为"默认已经隔离了"。"""
    assert not any(a.startswith("--basetemp=") for a in suites.build_command(get_suite("launch")))


@pytest.mark.unit
def test_pytest_basetemp_is_relative_and_under_the_repo():
    """必须是**相对仓库根**的 `reports/` 路径。

    相对：两个调用方（`farm_agent.run_suite` / `elevated_runner`）的 cwd 都是仓库根，
    而提权侧的参数白名单**只放行 `reports/` 下的相对路径**（绝对路径等于让它写到任意位置）。
    """
    rel = suites.pytest_basetemp("task_1", "launch", 1)
    assert rel.startswith("reports/_pytest_tmp/"), rel
    assert ".." not in rel and ":" not in rel, f"不许出现盘符或上跳：{rel}"


@pytest.mark.unit
def test_pytest_basetemp_is_unique_per_process():
    """带 pid：pytest 对 `--basetemp` 会先 `rm_rf` 再 `mkdir`。

    路径复用、而归另一个身份所有时，**`rm_rf` 本身就失败** —— 每次换新名字才绕得开。
    """
    a = suites.pytest_basetemp("task_1", "launch", 1)
    b = suites.pytest_basetemp("task_1", "launch", 2)
    c = suites.pytest_basetemp("task_2", "launch", 1)
    assert len({a, b, c}) == 3


@pytest.mark.unit
def test_pytest_basetemp_sanitizes_the_name():
    """任务名 / 套件名会进路径，必须洗掉分隔符，别让它跑出目录。"""
    rel = suites.pytest_basetemp("task/../evil", "launch", 1)
    assert ".." not in rel and "/" not in rel.split("reports/_pytest_tmp/", 1)[1]


@pytest.mark.unit
def test_prune_pytest_tmp_removes_only_old_ones(tmp_path, monkeypatch):
    """过期的删掉、当次的留着。"""
    root = tmp_path / "tmp"
    root.mkdir()
    old, fresh = root / "old", root / "fresh"
    old.mkdir()
    fresh.mkdir()
    stale = time.time() - 10 * 86400
    os.utime(old, (stale, stale))

    monkeypatch.setattr(suites, "PYTEST_TMP_ROOT", root)
    assert suites.prune_pytest_tmp(keep_days=1) == 1
    assert not old.exists(), "过期目录该删"
    assert fresh.exists(), "当次目录不能删"


@pytest.mark.unit
def test_prune_pytest_tmp_never_raises(tmp_path, monkeypatch):
    """清理是**尽力而为**：目录可能归另一个身份所有（管理员跑过的那批），删不掉就留着。

    删不掉不影响正确性 —— 正因为 basetemp **不复用**，留着也只是占点磁盘。
    所以这里只要求"不抛异常"。
    """
    monkeypatch.setattr(suites, "PYTEST_TMP_ROOT", tmp_path / "nope")
    assert suites.prune_pytest_tmp() == 0          # 目录不存在

    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")       # 拿普通文件当目录根
    monkeypatch.setattr(suites, "PYTEST_TMP_ROOT", blocker)
    assert suites.prune_pytest_tmp() == 0           # 不能抛
