"""多机跑批：套件定义、分片、并发预算。

## 为什么需要这个模块

20+ 台机器跑同一套用例，不是"复制 20 份 pytest 命令"就完事，有两类硬约束：

1. **服务端压力**：真装真卸会打厂商 CDN + 大厅更新服务；登录会打账号服务。
   20 台同时打会被限流，产出的是假失败，不是缺陷。
2. **本机状态互斥**：装/卸、注册表、前台窗口都是全局机器状态，**同一台机上必须串行**。

所以按"能不能并行、要不要限流"把用例分成四类（见 `SUITES`），每类给出并发预算。

## 分片

同一类用例可以摊到多台机器（`shard`），分片口径统一走 pytest 的 `-k` / 节点 id 取模，
不用 pytest-xdist（它只做单机多进程，跨不了机器，而且这套用例有全局状态，同机多进程也会互踩）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 用例类 marker（与 pytest.ini 的 markers 一致）
MARKER_UNIT = "unit"
MARKER_INSTALL = "install"
MARKER_LAUNCH = "launch"
MARKER_APPS = "apps"
MARKER_LOGIN = "login"
MARKER_SETTINGS = "settings"
MARKER_SECURITY = "security"
MARKER_WB = "wb"
MARKER_MANUAL = "manual"
MARKER_DESTRUCTIVE = "destructive"


@dataclass(frozen=True)
class Suite:
    """一个可独立跑批的套件。

    name        套件名（命令行用）
    paths       传给 pytest 的路径
    marker      -m 表达式（不含分片）
    parallel    并发策略：
                  full     全并行，不碰服务端
                  limited  跨机限流，最多 max_concurrent 台同时跑
                  serial   全局串行（同一时刻只允许 1 台）
    max_concurrent  limited 时的并发上限；serial 恒为 1
    needs       运行前提（写给人看，缺则整套 skip / 报错）
    farm_safe   是否能进无人值守农场。manual（人在环）永远 False。
    """

    name: str
    paths: tuple[str, ...]
    marker: str
    parallel: str = "full"
    max_concurrent: int = 0
    needs: str = ""
    farm_safe: bool = True

    def concurrency(self) -> int:
        """本套件允许的最大并发机器数。"""
        if self.parallel == "serial":
            return 1
        if self.parallel == "limited":
            return max(1, self.max_concurrent)
        return 0  # full = 不限


# 并发预算的来源与理由写在每条 needs/note 里，改之前先回去看实际被限流的记录。
SUITES: dict[str, Suite] = {
    "unit": Suite(
        name="unit",
        paths=("tests/unit",),
        marker=MARKER_UNIT,
        parallel="full",
        needs="无",
    ),
    "install": Suite(
        name="install",
        paths=("tests/install",),
        marker=MARKER_INSTALL,
        parallel="limited",
        # 会真装/卸大厅 + 打更新服务；20 台同时装同一个版本对服务端是 20 倍压力
        max_concurrent=5,
        needs="管理员权限 + HALL_ALLOW_INSTALL=1 + installer_dir 下有安装包",
    ),
    "launch": Suite(
        name="launch",
        paths=("tests/launch/test_p0_launch.py", "tests/launch/test_p0_search.py"),
        marker=MARKER_LAUNCH,
        parallel="full",
        needs="本机已装大厅",
    ),
    "apps-detect": Suite(
        name="apps-detect",
        paths=("tests/launch/test_p1_apps.py",),
        marker="apps and not destructive",
        parallel="full",
        needs="本机已装大厅；不需要管理员",
    ),
    "apps-lifecycle": Suite(
        name="apps-lifecycle",
        paths=("tests/launch/test_p1_apps.py",),
        marker="apps and destructive",
        parallel="limited",
        # 真装真卸，走厂商 CDN 下载 165MB 级安装包；并发太高会被限流且拖慢每台
        max_concurrent=5,
        needs="管理员权限（HallAutoP1 计划任务或管理员终端）+ HALL_ALLOW_INSTALL=1",
    ),
    "login": Suite(
        name="login",
        paths=("tests/launch/test_p1_login.py", "tests/launch/test_p1_register.py"),
        # `and not manual` 不能省：test_sms_login_manual / test_forgot_password_reset_manual
        # **同时带 login + manual 两个 marker**，光写 "login" 会把它们选进来。
        # 唯一守卫是 `sys.stdin.isatty()`，而 farm_agent 起 pytest 时没重定向 stdin
        # （只重定向了 stdout/stderr）→ 子进程 isatty 为真 → 不 skip →
        # 真发短信 + `input()` 永久阻塞 → 那台机器再也取不到下一个任务。
        # pytest.ini 里也明写「manual …无人值守套件一律排除」，这里对齐。
        marker="login and not manual",
        parallel="limited",
        # 已确认多机登录不互踢；限流是为了不打爆账号服务，不是防踢
        max_concurrent=5,
        needs="HALL_TEST_USER/HALL_TEST_PASSWORD（缺则密码用例自动 skip）；注册套件无需凭据",
    ),
    "login-manual": Suite(
        name="login-manual",
        paths=("tests/launch/test_p1_login.py",),
        marker=MARKER_MANUAL,
        parallel="serial",
        needs="人在环：需交互式终端 + HALL_TEST_USER/PASSWORD/NEW_PASSWORD；会真发短信、真改密码",
        farm_safe=False,  # 永不进无人值守农场
    ),
    "settings": Suite(
        name="settings",
        paths=("tests/launch/test_p1_theme.py",),
        marker=MARKER_SETTINGS,
        parallel="full",
        needs="本机已装大厅；会改外观档位，收尾自动还原",
    ),
    "security": Suite(
        name="security",
        paths=("tests/launch/test_p2_security.py",),
        marker=MARKER_SECURITY,
        parallel="full",
        needs="config.local.yaml 的 security.tools_dir 指向内部工具本地副本；缺则 skip",
    ),
    "wb": Suite(
        name="wb",
        paths=("tests/launch/test_p2_workbuddy.py",),
        marker=MARKER_WB,
        parallel="limited",
        # 非破坏 6 条只读点击；但会占屏且点 WB 入口，跨机限流避免同时打 WB 后台开关检查
        max_concurrent=5,
        needs="WB 后台开关已开、WB 未安装；破坏性行 25 另需管理员",
    ),
}


def get_suite(name: str) -> Suite:
    if name not in SUITES:
        raise KeyError(f"未知套件 {name!r}，可用：{', '.join(sorted(SUITES))}")
    return SUITES[name]


# 「全量档」：一次投完**无人值守能跑的全部**套件。
#
# 为什么要有它：`dispatch --suites` 要人肉打套件名，漏一个就表现为
# 「跑着跑着停了」—— 节点跑完手上的活就回轮询，看起来像卡死，实际是没人投。
# 全量档把「这次要跑什么」从人的记忆里拿出来，一条命令投完。
#
# 刻意**不含**这三个，理由不是省事：
#   install / apps-lifecycle  要管理员 + HALL_ALLOW_INSTALL=1，而 farm_agent 是非提权的
#                             （把 agent 整体提权会改掉 launch/login 的权限上下文）→ 投了必挂
#   login-manual              人在环，farm_safe=False，永不进无人值守农场
FULL_RUN_SUITES: tuple[str, ...] = (
    "unit",
    "launch",
    "apps-detect",
    "login",
    "settings",
    "security",
    "wb",
)

# --suites 的展开关键字。写成元组是故意的：两个词都认，但只在这一处定义，
# 免得 `all` 在 dispatch 里、`full` 在别处，两边漂移。
FULL_RUN_KEYWORDS: frozenset[str] = frozenset({"all", "full"})


def resolve_suite_names(raw: str) -> list[str]:
    """把命令行 `--suites` 的值展开成套件名列表。

    `all` / `full`（大小写不敏感）→ 全量档；其余按逗号切分、去空、**保序去重**。
    去重不能省：`--suites launch,launch` 会让节点把同一个套件跑两遍，
    而回执里两条同名记录看起来像「跑了两轮」，排查时会被带偏。
    """
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    names: list[str] = []
    for token in tokens:
        expanded = FULL_RUN_SUITES if token.lower() in FULL_RUN_KEYWORDS else (token,)
        for name in expanded:
            if name not in names:
                names.append(name)
    return names


def farm_suites() -> list[Suite]:
    """可进无人值守农场的套件（排除人在环）。"""
    return [s for s in SUITES.values() if s.farm_safe]


def build_command(suite: Suite, *, shard: int = 1, of: int = 1, extra: list[str] | None = None) -> list[str]:
    """拼出该套件的 pytest 参数（不含解释器路径）。

    分片用 marker 表达式 + `-k` 不好做精确切分，改用 pytest 的节点 id 取模 ——
    但 pytest 没有原生的"按用例序号取模"参数，所以分片走 `--shard-id/--shard-count`
    这两个自定义选项（在 tests/conftest.py 里实现）。
    """
    argv = [*suite.paths, "-m", suite.marker, "-v"]
    if of > 1:
        argv += [f"--shard-id={shard}", f"--shard-count={of}"]
    if extra:
        argv += extra
    return argv


@dataclass
class ShardPlan:
    """把一批套件摊到 N 台机器上的执行计划。"""

    total_nodes: int
    assignments: list[tuple[str, str, int, int]] = field(default_factory=list)
    # (node_id, suite_name, shard_id, shard_count)

    def to_dict(self) -> dict:
        return {
            "total_nodes": self.total_nodes,
            "assignments": [
                {"node": n, "suite": s, "shard_id": i, "shard_count": c}
                for n, s, i, c in self.assignments
            ],
        }


def plan_shards(suite_names: list[str], nodes: list[str]) -> ShardPlan:
    """把套件摊到节点上。

    规则（按服务端压力分级，不是简单轮转）：
      - full      ：每台机器都跑全套（不限并发），分片无意义 → 每台各自跑完整套件
      - limited   ：按 max_concurrent 把节点分组，组内分摊分片
      - serial    ：只派给第一个节点，全局唯一
    """
    if not nodes:
        raise ValueError("节点列表为空")
    plan = ShardPlan(total_nodes=len(nodes))
    for name in suite_names:
        suite = get_suite(name)
        if suite.parallel == "serial":
            plan.assignments.append((nodes[0], suite.name, 1, 1))
            continue
        if suite.parallel == "full":
            # 不限并发：每台机器跑完整套件，分片数是 1
            for node in nodes:
                plan.assignments.append((node, suite.name, 1, 1))
            continue
        # limited：把节点切成 n 个分片，并标出并发上限（执行器负责按 max_concurrent 节流）
        limit = min(suite.concurrency(), len(nodes))
        for idx, node in enumerate(nodes):
            shard = idx % limit + 1
            plan.assignments.append((node, suite.name, shard, limit))
    return plan
