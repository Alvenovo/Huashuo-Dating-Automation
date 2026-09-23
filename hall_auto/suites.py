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

import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

# **不 import hall_auto.config**：它顶层 `import yaml`，而本模块会被
# `tools/bootstrap_machine.py` 间接导入 —— 那台机器上 PyYAML 还没装。
# 同一条约束见 hall_auto/elevation.py 与 tests/unit/test_bootstrap_policy.py。
REPO_ROOT = Path(__file__).resolve().parents[1]

# pytest 的临时根目录落点。**不能让它用系统默认的 `%LOCALAPPDATA%\Temp\pytest-of-<user>`** ——
# 见 `pytest_basetemp` 的 docstring。
PYTEST_TMP_REL = "reports/_pytest_tmp"
PYTEST_TMP_ROOT = REPO_ROOT / PYTEST_TMP_REL

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
    interactive 需要真人守着的交互式终端。会给 pytest 加 `-s`（**不加必挂**，
                理由见 `build_command`），调用方也不该重定向它的 stdout。
                必然 `farm_safe=False` —— 两者互斥，有守卫锁着。
    needs_elevation
                必须**管理员**权限。农场 agent 是非提权的（整体提权会改掉
                launch/login 的权限上下文），所以这类套件由 agent 走
                `hall_auto/elevation.py` 的提权通道：写请求文件 → 触发计划任务
                `HallAutoP1`（bootstrap 第 11 步建好）→ 提权侧跑 → 回结果。
                **仍然 farm_safe**：它进农场，只是换了个身份执行。
    reset_fixture
                跑之前先执行 `tools/reset_fixture.py` 把夹具压回起点。
                上一次装剩的会让装卸用例**直接 skip**（不报错），报告上一片黄
                而人以为"跑过了"。原来写死在 `run_p1_apps.ps1` 里，
                现在跟着套件定义走，提权通道照做。
    """

    name: str
    paths: tuple[str, ...]
    marker: str
    parallel: str = "full"
    max_concurrent: int = 0
    needs: str = ""
    farm_safe: bool = True
    interactive: bool = False
    needs_elevation: bool = False
    reset_fixture: bool = False

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
        needs="管理员权限（提权通道 HallAutoP1）+ HALL_ALLOW_INSTALL=1 + installer_dir 下有安装包",
        needs_elevation=True,
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
        needs="管理员权限（提权通道 HallAutoP1）+ HALL_ALLOW_INSTALL=1",
        needs_elevation=True,
        # 上一次装剩的夹具会让装卸用例直接 skip（不报错）—— 必须先从注册表静默卸掉。
        reset_fixture=True,
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
        needs=(
            "人在环：需交互式终端 + HALL_TEST_USER/PASSWORD/NEW_PASSWORD；"
            "会真发短信、真改密码、登录后可能真绑手机号（HALL_BIND_PHONE，缺省回退 HALL_TEST_USER）"
        ),
        farm_safe=False,  # 永不进无人值守农场
        # 2026-09-22 真机踩出：不加 `-s` 时这三条**永远 skip**，整套人在环等于不存在。
        # 详见 build_command 里的实测记录。
        interactive=True,
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


# 「全量档」：一次投完**所有**套件（除人在环）。
#
# 为什么要有它：`dispatch --suites` 要人肉打套件名，漏一个就表现为
# 「跑着跑着停了」—— 节点跑完手上的活就回轮询，看起来像卡死，实际是没人投。
# 全量档把「这次要跑什么」从人的记忆里拿出来，一条命令投完。
#
# 唯一不含的是 `login-manual`：人在环，`farm_safe=False`，永不进无人值守农场。
#
# ⚠️ **顺序有意义，不是随便排的**：`install` 会**卸载并重装大厅本体**，
# 跑完之后登录态、主题档位全部重置 —— 所以它必须排在最后。
# 顺序靠 `resolve_suite_names` 保序来维持，改这里的位置等于改执行顺序。
# 守卫：tests/unit/test_suite_planning.py::test_elevated_suites_run_last。
#
# 历史：2026-09-23 之前这里只有 7 个，`install` / `apps-lifecycle` 被排除在外，
# 理由是「farm_agent 非提权 → 投了必挂」。那个结论过时了 —— 提权通道
# `HallAutoP1` 早就建好（bootstrap 第 11 步），只是没人接上。现在它们走
# `hall_auto/elevation.py` 的 broker，跑批不用再切管理员窗口。
FULL_RUN_SUITES: tuple[str, ...] = (
    "unit",
    "launch",
    "apps-detect",
    "login",
    "settings",
    "security",
    "wb",
    "apps-lifecycle",
    "install",
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


def elevated_suites() -> list[Suite]:
    """需要管理员、必须走提权通道执行的套件。"""
    return [s for s in SUITES.values() if s.needs_elevation]


def execution_order(names: list[str]) -> list[str]:
    """套件的执行顺序：**非提权段先跑，提权段后跑**，组内保持原顺序（稳定排序）。

    **为什么要强制，而不是「投任务的人按顺序写」**：`install` 会卸载并重装大厅本体，
    跑完之后登录态和主题档位全被重置。任务里同时有 `install` 和 `launch`/`login` 时，
    谁先谁后直接决定结果对不对。

    全量档自己已经排好了（见 `FULL_RUN_SUITES` 上面的说明），但
    `--suites login,install` 这种手写组合必须也被纠正 —— 靠人记得是不可靠的。
    """
    return sorted(names, key=_elevation_rank)


def _elevation_rank(name: str) -> int:
    """排序键：提权套件排后面。未知套件名按非提权处理。

    **未知名字不能在这里炸**：抛 KeyError 会让整批套件一个都不跑（排序发生在
    第一个套件之前），而真错应该在执行到它时报出来，那时前面已经跑完的还有结果。
    """
    try:
        return 1 if get_suite(name).needs_elevation else 0
    except KeyError:
        return 0


def build_command(suite: Suite, *, shard: int = 1, of: int = 1, extra: list[str] | None = None,
                  basetemp: str | None = None) -> list[str]:
    """拼出该套件的 pytest 参数（不含解释器路径）。

    分片用 marker 表达式 + `-k` 不好做精确切分，改用 pytest 的节点 id 取模 ——
    但 pytest 没有原生的"按用例序号取模"参数，所以分片走 `--shard-id/--shard-count`
    这两个自定义选项（在 tests/conftest.py 里实现）。

    `basetemp` 是 `pytest_basetemp()` 的产物（**相对路径**，两个调用方的 cwd 都是仓库根）。
    传了就加 `--basetemp=`，pytest 的 `tmp_path` 就落在那儿，不碰系统临时目录。
    """
    argv = [*suite.paths, "-m", suite.marker, "-v"]
    if suite.interactive:
        # `-s` 是**必需项，不是可选优化**。2026-09-22 真机实测（`--local login-manual`）：
        #
        #   pytest 默认捕获会把 `sys.stdin` 换成 `DontReadFromInput`，于是测试体里的
        #   `sys.stdin.isatty()` **恒为 False** —— 三条人在环用例一句不落地全 skip，
        #   而 `farm_agent` 照打「退出码 0」，看着像「跑过了、只是没到条件」，
        #   实际是**这套功能从没被执行过**。
        #   就算跳过守卫，`input()` 也会抛
        #   `OSError: pytest: reading from stdin while output is captured! Consider using -s.`
        #
        # 实测对照（同一台机、同一条探针用例）：
        #   默认捕获 → type(sys.stdin)=DontReadFromInput，isatty()=False，input() 抛 OSError
        #   加 `-s`   → type(sys.stdin)=TextIOWrapper，    isatty()=True， input() 正常读
        #
        # 所以这条**不能靠"人记得加 -s"**：必须由套件定义自己带上，
        # 否则 `farm_agent --local login-manual` 这条路永远是假的。
        # 守卫：tests/unit/test_suite_planning.py。
        argv.append("-s")
    if of > 1:
        argv += [f"--shard-id={shard}", f"--shard-count={of}"]
    # 关掉 pytest 缓存插件：它会往**仓库根**写 `.pytest_cache/`，而管理员跑过一次之后
    # 那个目录归 `BUILTIN\Administrators`，之后普通权限的每一轮都会打一堆
    # `PytestCacheWarning: could not create cache path ... [WinError 5] 拒绝访问`
    # （2026-09-23 真机日志里就有）。我们**一条都不用** `--lf/--ff/cache` fixture，
    # 关掉零代价。守卫：tests/unit/test_suite_planning.py。
    argv += ["-p", "no:cacheprovider"]
    # `HALL_EVIDENCE=failure-only` 把证据量压回去（默认 `all`，见 tests/conftest.py）。
    #
    # 放在**这里**而不是调用方：普通段（`farm_agent.run_suite`）和提权段
    # （`run_suite_elevated`）都经过本函数，一处就够 —— 分散到两处必然漏一个，
    # 而漏了的那段会静默用默认值（提权段的证据是唯一能复现真装真卸现场的东西）。
    #
    # 为什么不放 `pytest.ini` / 环境变量直通：`farm_agent` 已经把 `farm_node.env`
    # 合进子进程环境，`pytest.ini` 里写死又改不了单次运行。这里读进程环境最省事。
    evidence_mode = os.environ.get("HALL_EVIDENCE", "").strip()
    if evidence_mode:
        argv.append(f"--evidence={evidence_mode}")
    if basetemp:
        argv.append(f"--basetemp={basetemp}")
    if extra:
        argv += extra
    return argv


def pytest_basetemp(task_id: str, suite: str, shard_id: int = 1) -> str:
    """给这次 pytest 进程一个**专属**的临时根目录（相对仓库根），返回相对路径字符串。

    ## 为什么必须这么干（2026-09-23 真机踩出来的）

    pytest 默认把临时根放在 `%LOCALAPPDATA%\\Temp\\pytest-of-<user>`，**按用户名复用**。
    而 bootstrap 第 12 步的自检是**用管理员**跑的，于是那个目录由提权进程创建 ——
    Windows 把**提权进程创建的对象的 owner 记成 `BUILTIN\\Administrators`**，
    ACL 也只给 Administrators 全控。

    之后普通权限跑 `tests/unit` 时，进程令牌里的 Administrators 是 **deny-only**
    （UAC 过滤令牌的标准行为）→ 在 `pytest-of-<user>` 里建子目录被拒 → `tmp_path`
    fixture 在 setup 阶段直接 `PermissionError: [WinError 5]`。
    真机后果：**238 / 599 条用例 ERROR**（凡是用了 `tmp_path` 的全崩），
    而报告上看着像"代码烂了"，实际是环境被自己的 bootstrap 污染了。

    **不是"清一次就好"** —— 每台新机跑 bootstrap 都会重新污染一遍。所以做法是
    **根本不用那个共享目录**：每次运行一个专属 basetemp，谁建的谁用，永不复用。

    名字里带 `pid` + 时间戳：pytest 对 `--basetemp` 会先 `rm_rf` 再 `mkdir`，
    而复用的路径若归另一个身份所有（管理员跑过的那批），`rm_rf` 本身就失败并**抛异常**。
    每次换新名字就绕开了这一步。

    **父目录要自己建**：pytest 的 `--basetemp` 只做 `mkdir()`（不带 `parents=True`），
    父目录不存在会直接 `FileNotFoundError`，表现是「凡是用了 `tmp_path` 的用例全 ERROR」——
    和这次要修的症状**长得一模一样**，很容易查错方向（2026-09-23 实测踩到）。
    """
    try:
        PYTEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    except OSError:
        # 建不出来时**不在这里炸** —— 让 pytest 自己在套件日志里报，那才是能定位的地方。
        pass
    stamp = time.strftime("%Y%m%d_%H%M%S")
    raw = f"{task_id}_{suite}_s{shard_id}_p{os.getpid()}_{stamp}"
    safe = re.sub(r"[^0-9A-Za-z_.\-]", "_", raw)[:90]
    # 连续的点也洗掉：`task/../evil` 只洗分隔符会留下字面的 `..`（不是路径上跳，
    # 但看着像，且守卫没法用一句 `".." not in rel` 表达）。`re.sub` 一次搞定。
    safe = re.sub(r"\.{2,}", "_", safe)
    return f"{PYTEST_TMP_REL}/{safe}"


def prune_pytest_tmp(keep_days: int = 2) -> int:
    """清掉过期的 basetemp 目录。返回删掉的个数。

    **尽力而为，绝不抛异常**：目录可能归另一个身份所有（管理员跑过的那批），
    普通权限删不掉。删不掉就留着，反正不影响正确性 —— 只是占点磁盘。
    """
    if keep_days <= 0 or not PYTEST_TMP_ROOT.is_dir():
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    try:
        entries = list(PYTEST_TMP_ROOT.iterdir())
    except OSError:
        return 0
    for path in entries:
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    return removed


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
