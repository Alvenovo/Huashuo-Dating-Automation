"""多机跑批：套件定义 / 分片规划 / 坐标矩阵 的纯逻辑单测。

这些规则是防止「20 台同时打服务端产出假失败」的关键参数，锁死它们避免被改坏。
"""

from __future__ import annotations

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
