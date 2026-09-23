"""一条命令跑完整批：投任务 → 等结果 → 出报告。

## 为什么要有它

`farm_control.py` 的三个子命令是**分开**的，一线要自己串：

    dispatch → status → status → status → … → aggregate

中间那段"反复敲 status 看有没有跑完"全靠人盯，而且**最要命的一种情况它不报错**：
任务投出去了、节点没取走，`status` 只显示「待执行 1 个」永远不动 ——
在控制机上**看不出**是"节点 agent 没跑"还是"共享盘断了"还是"任务名对不上"。

2026-09-23 真踩过：用户按清单先敲了 `--once`（取一次就退出）再投任务，
任务躺在 `tasks\\` 里没人取，控制机毫无提示。本脚本把这种状态**当场喊出来**。

## 用法

    # 冒烟：一个套件、几十秒，只验「节点真的会自己取任务跑起来」
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\run_farm.py ^
        --nodes DESKTOP-DOHED68 --suites launch

    # 冒烟过了 → 一条命令跑全部（含真装真卸，节点侧零人工）
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\run_farm.py ^
        --nodes DESKTOP-DOHED68 --suites all

    # 多台
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\run_farm.py ^
        --nodes R01,R02,R03 --suites all --timeout 14400

    # 只投不等（等价于原来的 dispatch）
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\run_farm.py --nodes R01 --suites launch --no-wait

`--suites all` = 全量档 9 个，**含 `install`（卸载重装大厅本体）和 `apps-lifecycle`
（夹具真装真卸）** —— 这两个要管理员，但走的是 bootstrap 建好的提权计划任务
`HallAutoP1`，节点侧零人工（见 `hall_auto/elevation.py`）。唯一不跑的是
`login-manual`（要真人收验证码），它在节点终端上单独问、单独跑。

**它不替代节点侧的 `farm_agent.py --loop`。** 节点上那个窗口没开，本脚本会在
90 秒后明确告诉你「任务没被取走」，然后继续等（或超时退出）—— 这是刻意的：
"投出去就不管"比"当场告诉你没人取"危险得多。

## 退出码

`0` 全部节点交了回执 ／ `1` 超时（有节点没交）／ `2` 投前自检没过或投任务失败 ／ `130` 人按了 Ctrl+C
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from hall_auto.suites import get_suite, resolve_suite_names  # noqa: E402

# 投出去多久还没被取走就告警。3 个轮询周期（agent 的 POLL_SECONDS = 30），
# 足够覆盖"节点刚好在轮询间隔里"的正常情况，又不至于让人干等。
STUCK_WARN_SECONDS = 90

DEFAULT_INTERVAL = 30
# 默认超时按**全量档**给：9 个套件里 `apps-lifecycle` 要下 165MB 级安装包、
# `install` 要卸载+重装大厅，单套件十几分钟是常态。按老口径（1 小时）给的话，
# 全量档跑到一半就被判超时，看着像"卡住"。
DEFAULT_TIMEOUT = 4 * 3600


def _load_farm_control():
    """按文件路径加载 `tools/farm_control.py`，复用它已经写好的 dispatch / aggregate。

    不复制粘贴那两段逻辑：它们带并发预算、人在环拦截、全量档展开这些容易漏的规则，
    复制一份迟早两边漂移。
    """
    path = TOOLS_DIR / "farm_control.py"
    spec = importlib.util.spec_from_file_location("farm_control", path)
    if not spec or not spec.loader:
        raise SystemExit(f"加载不了 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["farm_control"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- 节点状态

def _receipt_path(root: Path, node: str, task_id: str) -> Path:
    """节点跑完写的回执。**这就是"这台机器交活了"的唯一判据。**"""
    return root / "done" / f"{node}_{task_id}.json"


def _pending_path(root: Path, node: str) -> Path:
    return root / "tasks" / f"{node}.json"


def _running_path(root: Path, node: str) -> Path:
    return root / "tasks" / f"{node}.json.running"


def node_state(root: Path, node: str, task_id: str) -> str:
    """这台节点此刻在哪一态。

    顺序不能换：回执优先 —— 跑完之后 `.running` 已经被收掉了，
    但万一收尾时崩了，**回执在就说明活交过了**，不该判成"执行中"。
    """
    if _receipt_path(root, node, task_id).is_file():
        return "done"
    if _running_path(root, node).is_file():
        return "running"
    if _pending_path(root, node).is_file():
        return "pending"
    # 既没回执、也没任务文件：任务被删了 / 被重投覆盖了 / agent 拒收了。
    # 不能当"没事" —— 它永远不会自己变好。
    return "missing"


_STATE_TEXT = {
    "done": "已交回执",
    "running": "已取走，正在跑",
    "pending": "还没被取走",
    "missing": "任务文件不在了（被删/被重投覆盖/agent 拒收）",
}


def _tally(states: dict[str, str]) -> tuple[int, int, int]:
    pending = sum(1 for s in states.values() if s == "pending")
    running = sum(1 for s in states.values() if s == "running")
    done = sum(1 for s in states.values() if s == "done")
    return pending, running, done


def _print_progress(nodes: list[str], states: dict[str, str],
                    prev: dict[str, str] | None) -> None:
    """打一行总账 + **只打状态变了的**节点明细。

    明细必须去重：轮询默认 30 秒一次，跑一小时就是 120 行「XXX 还没被取走」——
    刷屏之后人就不看了，等于没打。
    """
    pending, running, done = _tally(states)
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] 待执行 {pending} / 执行中 {running} / 已完成 {done}", flush=True)
    for node in nodes:
        state = states[node]
        if prev is not None and prev.get(node) == state:
            continue
        print(f"  {node:20} {_STATE_TEXT[state]}", flush=True)


def _stuck_hint(root: Path, nodes: list[str], states: dict[str, str]) -> str:
    """有人一直没取走任务时，给一段**能照着做**的排查。"""
    stuck = [n for n in nodes if states[n] == "pending"]
    if not stuck:
        return ""
    sample = stuck[0]
    lines = [
        f"⚠ 已等 {STUCK_WARN_SECONDS} 秒，这些节点还没取走任务：{', '.join(stuck)}",
        "  任务文件就在那儿躺着，节点侧不会有任何报错 —— 问题一定在节点那边：",
        f"    ① 节点上 `farm_agent.py --loop` 没在跑（最常见：敲成了 `--once`，取一次就退出了）",
        f"    ② 它连不到农场目录 —— 在节点上验：`dir {root / 'tasks'}`",
        f"    ③ 节点名对不上 —— 它等的是 `tasks\\<自己的名字>.json`，不是 `{sample}.json`",
        "  它自己也会说：心跳里写「等待任务文件 …」，对一下名字；",
        "  连不上时会打「⚠ 节点进程活着，但一个任务都取不到」。",
    ]
    return "\n".join(lines)


def _timeout_report(root: Path, nodes: list[str], states: dict[str, str],
                    elapsed: float) -> None:
    waited = f"{elapsed:.0f} 秒" if elapsed < 120 else f"{elapsed / 60:.1f} 分钟"
    print(f"\n[超时] 已等 {waited}，还有节点没交回执：", flush=True)
    for node in nodes:
        if states[node] != "done":
            print(f"  {node:20} {_STATE_TEXT[states[node]]}", flush=True)
    print("\n按状态对症：", flush=True)
    if any(states[n] == "pending" for n in nodes):
        print("  · 还没被取走 → 节点 agent 没跑 / 连不到农场目录（见上面的排查）", flush=True)
    if any(states[n] == "running" for n in nodes):
        print(f"  · 已取走但没回执 → 还在跑，或跑挂了。看 {root / 'logs'} 下那台的日志；", flush=True)
        print("    残留的 `.running` 会挡住下一个任务，重跑 agent 时会自动清。", flush=True)
    if any(states[n] == "missing" for n in nodes):
        print("  · 任务文件不见了 → 有人手工删了、或控制机又投了一轮把它覆盖了。", flush=True)
    print("\n人工看进度：farm_control.py status", flush=True)


# ---------------------------------------------------------------- 投前自检

def preflight(root: Path, nodes: list[str]) -> tuple[list[str], list[str]]:
    """投任务前的自检。返回 (必须停下的问题, 提醒)。

    **为什么要在投之前查**：任务投出去没人取时，原来的表现是控制机干等一小时才超时，
    期间只有一行「待执行 1 个」。而下面这几类问题在**投之前**就能看出来，
    发现时间从一小时变成一秒：

      - 农场目录连不上 / 建不出来（包源机关了、代理没关、共享名写错）
      - 目标节点上还压着**上一轮没被取走**的任务 —— 本次投递会把它覆盖掉
      - 有残留的 `.running` 占位 —— 它会挡住新任务（agent 重跑时会自动清，但要说一声）

    注意 `Path.is_dir()` 在 UNC 上会把 `OSError` 吞成 `False`（本项目实测过），
    所以判可达性用「真去 mkdir 一次」，不是 `is_dir()`。
    """
    errors: list[str] = []
    warnings: list[str] = []
    for sub in ("tasks", "done", "results", "logs"):
        try:
            (root / sub).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            errors.append(f"农场目录不可用：{root / sub}（{exc}）")
            return errors, warnings

    for node in nodes:
        running = root / "tasks" / f"{node}.json.running"
        if running.is_file():
            warnings.append(
                f"{node}：有残留的 `.running` 占位 —— 上一轮没正常收尾。"
                "节点重跑 agent 时会自动清，没清掉它取不到新任务。"
            )
        pending = root / "tasks" / f"{node}.json"
        if pending.is_file():
            warnings.append(
                f"{node}：还压着一条**没被取走**的任务（{pending.name}），本次投递会把它覆盖。"
            )
    return errors, warnings


def destructive_notice(suite_names: list[str]) -> str:
    """提权/破坏性套件的醒目告警。**空串 = 本次没有**。

    全量档现在包含真装真卸（`install` 会卸载并重装大厅本体），
    这事必须在按下回车之前说清楚 —— 它是跑批里唯一会改动被测产品的动作。
    """
    elevated = [n for n in suite_names if get_suite(n).needs_elevation]
    if not elevated:
        return ""
    lines = [
        "⚠ 本次含真装真卸套件（走提权通道 HallAutoP1，节点侧零人工）：",
    ]
    for name in elevated:
        what = {
            "install": "卸载并重装**华硕大厅本体** —— 跑完登录态/主题档位全部重置",
            "apps-lifecycle": "真装真卸夹具应用（走厂商 CDN，165MB 级安装包）",
        }.get(name, get_suite(name).needs)
        lines.append(f"    · {name}：{what}")
    lines.append("  ⚠ **只能在测试机上跑。** 办公机上投这一批会把大厅卸掉重装。")
    lines.append("  它们排在只读套件之后执行（install 最后），不会打断前面的结果。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="一条命令跑完整批：投任务 → 等结果 → 出报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--nodes", required=True, help="逗号分隔的节点标识（必须和节点心跳里的名字一致）")
    parser.add_argument("--suites", required=True,
                        help="逗号分隔的套件名；写 all = 全量档（9 个，含真装真卸）。"
                             "单个套件可用：unit/launch/apps-detect/…")
    parser.add_argument("--task-id", default="", help="不填则用当前时间生成")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help=f"轮询间隔秒数（默认 {DEFAULT_INTERVAL}）")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"总超时秒数（默认 {DEFAULT_TIMEOUT}）；到点没交齐就退出，码 1")
    parser.add_argument("--no-wait", action="store_true",
                        help="只投任务不等待（等价于原来的 dispatch）")
    parser.add_argument("--no-aggregate", action="store_true",
                        help="跑完后不自动汇总")
    parser.add_argument("--allow-manual", action="store_true",
                        help="允许投人在环套件（login-manual）。默认拒绝——它会让节点卡在等输入。")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    nodes = [n.strip() for n in args.nodes.split(",") if n.strip()]
    if not nodes:
        raise SystemExit("--nodes 不能为空")
    if args.interval < 1:
        raise SystemExit("--interval 至少 1 秒")

    fc = _load_farm_control()

    task_id = args.task_id.strip() or f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    print(f"=== 跑批 {task_id} ===", flush=True)
    print(f"节点：{', '.join(nodes)}", flush=True)
    print(f"套件：{args.suites}", flush=True)
    print(flush=True)

    root = fc.farm_root()

    print("[1/4] 投前自检", flush=True)
    errors, warnings = preflight(root, nodes)
    for line in warnings:
        print(f"  ⚠ {line}", flush=True)
    if errors:
        for line in errors:
            print(f"  ✗ {line}", file=sys.stderr, flush=True)
        print("\n自检没过，不投任务。", file=sys.stderr, flush=True)
        return 2
    print(f"  ✓ 农场目录可用：{root}", flush=True)
    notice = destructive_notice(resolve_suite_names(args.suites))
    if notice:
        print("\n" + notice + "\n", flush=True)

    print("[2/4] 投任务", flush=True)
    dispatch_args = argparse.Namespace(
        nodes=args.nodes,
        suites=args.suites,
        task_id=task_id,
        allow_manual=args.allow_manual,
    )
    try:
        rc = fc.cmd_dispatch(dispatch_args)
    except SystemExit as exc:
        # dispatch 的参数校验（人在环拦截、套件名写错）走 SystemExit，原样透传。
        if exc.code not in (0, None):
            print(f"\n投任务失败（退出码 {exc.code}）。", file=sys.stderr, flush=True)
        return 2 if exc.code not in (0, None) else 0
    if rc != 0:
        return 2

    if args.no_wait:
        print("\n[--no-wait] 投完即退。看进度：farm_control.py status", flush=True)
        return 0

    started = time.monotonic()
    print(f"\n[3/4] 等结果（每 {args.interval} 秒查一次，Ctrl+C 可中断）", flush=True)

    stuck_warned = False
    prev_states: dict[str, str] | None = None
    while True:
        states = {n: node_state(root, n, task_id) for n in nodes}
        _print_progress(nodes, states, prev_states)
        prev_states = states

        if all(s == "done" for s in states.values()):
            print("\n全部节点已交回执。", flush=True)
            break

        elapsed = time.monotonic() - started
        if not stuck_warned and elapsed >= STUCK_WARN_SECONDS:
            hint = _stuck_hint(root, nodes, states)
            if hint:
                print("\n" + hint + "\n", flush=True)
                stuck_warned = True

        if elapsed >= args.timeout:
            _timeout_report(root, nodes, states, elapsed)
            return 1

        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n\n中断。任务还在农场里，节点会继续跑完并写回执。", flush=True)
            print("看进度：farm_control.py status", flush=True)
            return 130

    if args.no_aggregate:
        print("\n[--no-aggregate] 跳过汇总。", flush=True)
        return 0

    print("\n[4/4] 出报告", flush=True)
    try:
        fc.cmd_aggregate(argparse.Namespace())
    except SystemExit as exc:
        print(f"汇总失败：{exc}", file=sys.stderr, flush=True)
        return 1

    # 全自动部分到此结束。人在环**不在这个脚本里**：它要真人收验证码，
    # 只能在节点那台机器的终端上问（agent 跑完自动段后会问一句）。
    # 这里只负责告诉人「去哪儿、怎么并进报告」，不替人做决定。
    if task_covers_full_run_names(resolve_suite_names(args.suites)):
        print(
            "\n自动段全部跑完。人在环（微软邮箱码 / 短信码 / 忘记密码往返）"
            "在**节点那台机器的终端**上：\n"
            "  跑完自动段后 agent 会问一句「是否现在参与人在环？」—— 回「能」就开始收码。\n"
            "  错过/跳过了要补跑：在节点上敲 "
            ".\\.venv\\Scripts\\python.exe -X utf8 tools\\farm_agent.py --local login-manual\n"
            "  跑完在控制机重跑一次 aggregate，把人在环结果并进上面那份报告。",
            flush=True,
        )
    return 0


def task_covers_full_run_names(suite_names: list[str]) -> bool:
    """这批套件是不是覆盖了全量档的自动段 —— 是的话才提人在环。

    判据与节点侧 `farm_agent.task_covers_full_run` 同源（都对着 `FULL_RUN_SUITES`），
    写死套件名的话，以后往全量档里加套件，两边就会各说各的。
    """
    from hall_auto.suites import FULL_RUN_SUITES

    return set(FULL_RUN_SUITES) <= set(suite_names)


if __name__ == "__main__":
    raise SystemExit(main())
