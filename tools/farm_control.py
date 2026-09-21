"""多机跑批：控制机侧（投任务 / 收结果 / 汇总矩阵）。

配套 `tools/farm_agent.py`（节点侧）。控制机这边只做三件事，保持薄：

    dispatch  按套件并发预算生成任务文件，投到 farm_root/tasks/<node>.json
    status    看哪些节点还没交活
    aggregate 把 results/ 下各节点的 summary.json 汇总成一张「节点 × 用例」矩阵报告

## 用法

    # 投任务：把 launch / apps-detect / security 摊到 8 台机器
    .\\.venv\\Scripts\\python.exe -X utf8 tools/farm_control.py dispatch ^
        --nodes R01,R02,R03,R04,R05,R06,R07,R08 --suites launch,apps-detect,security

    # 看进度
    .\\.venv\\Scripts\\python.exe -X utf8 tools/farm_control.py status

    # 汇总
    .\\.venv\\Scripts\\python.exe -X utf8 tools/farm_control.py aggregate

需要 `HALL_FARM_ROOT` 指向共享盘农场目录。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from hall_auto.suites import SUITES, get_suite  # noqa: E402


def farm_root() -> Path:
    """农场目录：环境变量 > config.local.yaml > 硬退出。

    控制机通常就是办公机或包源机，也可能跑过 bootstrap 因此配置里有 farm_root。
    环境变量优先是为了临时切到别的农场做验证。
    """
    raw = os.environ.get("HALL_FARM_ROOT", "").strip()
    if not raw:
        try:
            from hall_auto.config import load_config

            raw = str(load_config().farm_root or "").strip()
        except Exception:
            raw = ""
    if not raw:
        raise SystemExit(
            "未设 HALL_FARM_ROOT，config.local.yaml 里也没有 farm_root。\n"
            "例：$env:HALL_FARM_ROOT='\\\\SHARE\\qa\\hall-farm'"
        )
    return Path(raw)


def cmd_dispatch(args) -> int:
    root = farm_root()
    nodes = [n.strip() for n in args.nodes.split(",") if n.strip()]
    suite_names = [s.strip() for s in args.suites.split(",") if s.strip()]
    if not nodes or not suite_names:
        raise SystemExit("--nodes 与 --suites 都不能为空")

    for name in suite_names:
        get_suite(name)  # 早报错，别投出去才发现名字写错

    task_id = args.task_id or f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    (root / "tasks").mkdir(parents=True, exist_ok=True)
    (root / "done").mkdir(parents=True, exist_ok=True)

    # 按并发预算把套件摊到节点上（plan_shards 的口径见 hall_auto/suites.py）
    per_node: dict[str, list[dict]] = {n: [] for n in nodes}
    for name in suite_names:
        suite = get_suite(name)
        if suite.parallel == "serial":
            target, sid, scount = nodes[0], 1, 1
        elif suite.parallel == "full":
            # 不限并发：每台跑完整套件
            for node in nodes:
                per_node[node].append({"name": name, "shard_id": 1, "shard_count": 1})
            continue
        else:
            limit = min(suite.concurrency(), len(nodes))
            for idx, node in enumerate(nodes):
                per_node[node].append({"name": name, "shard_id": idx % limit + 1, "shard_count": limit})
            continue
        per_node[target].append({"name": name, "shard_id": sid, "shard_count": scount})

    written = 0
    for node, suites in per_node.items():
        if not suites:
            continue
        payload = {
            "task_id": task_id,
            "node": node,
            "suites": suites,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        # 破坏性套件统一由节点侧环境变量控制，这里显式带上便于审计
        if any(s["name"] in ("install", "apps-lifecycle") for s in suites):
            payload["env"] = {"HALL_ALLOW_INSTALL": "1"}
        out = root / "tasks" / f"{node}.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written += 1
        print(f"{node}: {len(suites)} 个套件 -> {out.name}")

    print(f"\n任务 {task_id} 已投给 {written} 台节点（共 {len(nodes)} 台）。")
    _print_concurrency_notes(suite_names)
    _warn_missing_credentials(suite_names)
    return 0


# 这些套件靠凭据才能真跑；节点上没配 → 用例静默 skip（报告一片黄）。
# 控制机这边看不出来，所以投任务时就提醒一次，别等汇总时才发现。
_CRED_REQUIRED = {
    "login": "HALL_TEST_USER / HALL_TEST_PASSWORD / HALL_MS_USER",
    "login-manual": "HALL_TEST_USER / HALL_TEST_PASSWORD / HALL_TEST_NEW_PASSWORD",
}


def _warn_missing_credentials(suite_names: list[str]) -> None:
    """投了需要凭据的套件就提醒：凭据在**节点本地** farm_node.env，不是在这里设。"""
    needed = sorted({name for name in suite_names if name in _CRED_REQUIRED})
    if not needed:
        return
    print("\n⚠ 凭据提醒：")
    for name in needed:
        print(f"  {name} 需要节点本地 tools/farm_node.env 里有：{_CRED_REQUIRED[name]}")
    print("  （密码不落共享盘、不进任务文件；每台节点机各自的 farm_node.env 已 gitignore）")
    print("  没配的节点上，这些用例会静默 skip —— 汇总表里看 credentials 字段就能区分。")


def _print_concurrency_notes(suite_names: list[str]) -> None:
    """把并发预算摊开给人看：这是防服务端被打爆的关键参数，别悄悄生效。"""
    print("并发预算：")
    for name in suite_names:
        suite = get_suite(name)
        if suite.parallel == "full":
            note = "全并行（不碰服务端）"
        elif suite.parallel == "serial":
            note = "全局串行（同时只允许 1 台）"
        else:
            note = f"限流 ≤{suite.concurrency()} 台（打服务端 / 厂商 CDN）"
        flag = "" if suite.farm_safe else "  ⚠ 人在环，不应进农场"
        print(f"  {name:16} {note}{flag}")


def cmd_status(args) -> int:
    root = farm_root()
    tasks = sorted((root / "tasks").glob("*.json"))
    dones = sorted((root / "done").glob("*.json"))
    running = sorted((root / "tasks").glob("*.running"))
    print(f"待执行 {len(tasks)} 个，执行中 {len(running)} 个，已完成 {len(dones)} 个")
    for f in tasks:
        print(f"  待执行: {f.name}")
    for f in running:
        print(f"  执行中: {f.name}")
    for f in dones[-10:]:
        print(f"  已完成: {f.name}")
    return 0


def cmd_aggregate(args) -> int:
    root = farm_root()
    results_root = root / "results"
    if not results_root.is_dir():
        raise SystemExit(f"没有结果目录：{results_root}")

    nodes_data: dict[str, dict] = {}
    for node_dir in sorted(d for d in results_root.iterdir() if d.is_dir()):
        for run_dir in sorted(d for d in node_dir.iterdir() if d.is_dir()):
            summary_file = run_dir / "summary.json"
            if not summary_file.is_file():
                continue
            try:
                summary = json.loads(summary_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            # 同一节点多轮 result 合并：后面的覆盖前面的同名用例
            bucket = nodes_data.setdefault(node_dir.name, {"profile": summary.get("profile") or {},
                                                            "cases": {}, "runs": []})
            bucket["runs"].append(summary.get("run"))
            for case in summary.get("cases") or []:
                bucket["cases"][case["nodeid"]] = case

    if not nodes_data:
        raise SystemExit("没有可汇总的结果（results/ 下没有 summary.json）")

    out = root / "aggregate_report.html"
    out.write_text(_render_matrix(nodes_data), encoding="utf-8")
    print(f"汇总报告：{out}")
    print(f"覆盖节点 {len(nodes_data)} 台，用例 {len({c for v in nodes_data.values() for c in v['cases']})} 条")
    return 0


_OUTCOMES = ("passed", "failed", "skipped")
_SYMBOL = {"passed": "✓", "failed": "✗", "skipped": "–"}
_COLOR = {"passed": "#1a7f37", "failed": "#c62828", "skipped": "#9aa0a6"}


def _cred_cell(creds: dict, key: str) -> str:
    """凭据格：有=绿 ✓，没有=红 ✗。

    存在的意义是**区分 skip 的根因** —— 报告上一片黄时，看这列就知道
    是「这台机器没配凭据」还是「用例本身在跳过」。
    """
    ok = bool(creds.get(key))
    color = _COLOR["passed"] if ok else _COLOR["failed"]
    return f'<td style="color:{color}">{"✓" if ok else "✗"}</td>'


def _render_matrix(nodes_data: dict) -> str:
    """节点 × 用例矩阵：一眼看出「哪台机器挂在哪条用例」，那是兼容性结论的形态。"""
    all_cases = sorted({c for v in nodes_data.values() for c in v["cases"]})
    nodes = sorted(nodes_data)

    head = "".join(f"<th>{html.escape(n)}</th>" for n in nodes)
    rows = []
    for nodeid in all_cases:
        cells = []
        for node in nodes:
            case = nodes_data[node]["cases"].get(nodeid)
            if case is None:
                cells.append('<td class="na">·</td>')
                continue
            outcome = case.get("outcome", "")
            symbol = _SYMBOL.get(outcome, "?")
            color = _COLOR.get(outcome, "#000")
            title = html.escape((case.get("assertion") or "")[:200])
            cells.append(f'<td style="color:{color}" title="{title}">{symbol}</td>')
        short = nodeid.split("::")[-1]
        module = nodeid.split("::")[0]
        rows.append(f'<tr><td class="case"><div>{html.escape(short)}</div>'
                    f'<div class="mod">{html.escape(module)}</div></td>{"".join(cells)}</tr>')

    env_rows = []
    for node in nodes:
        profile = nodes_data[node].get("profile") or {}
        creds = nodes_data[node].get("credentials") or {}
        env_rows.append(
            "<tr><td>" + html.escape(node) + "</td>"
            + "".join(f"<td>{html.escape(str(profile.get(k, '')))}</td>"
                      for k in ("os", "screen", "scale_percent", "webview2", "webview2_version", "ms_session"))
            + "".join(_cred_cell(creds, k) for k in
                      ("password_login", "microsoft_sso", "share_creds"))
            + "</tr>"
        )

    totals = {n: {o: sum(1 for c in nodes_data[n]["cases"].values() if c.get("outcome") == o)
                  for o in _OUTCOMES} for n in nodes}

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>多机汇总 · 节点 × 用例矩阵</title>
<style>
 body{{margin:0;background:#f6f8fa;color:#1f2328;font-family:"Microsoft YaHei","Segoe UI",system-ui,sans-serif;font-size:13px;}}
 .wrap{{max-width:1400px;margin:0 auto;padding:24px 16px 64px;}}
 h1{{font-size:20px;margin:0 0 4px;}} h2{{font-size:15px;margin:28px 0 8px;}}
 .meta{{color:#656d76;margin-bottom:16px;}}
 table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #d0d7de;border-radius:8px;overflow:hidden;}}
 th,td{{border-bottom:1px solid #e4e8ec;padding:6px 8px;text-align:center;}}
 th{{background:#eef1f4;font-size:12px;color:#656d76;position:sticky;top:0;}}
 td.case{{text-align:left;min-width:260px;}} .mod{{color:#9aa0a6;font-size:11px;}}
 .na{{color:#c9ced3;}}
 .tbl-scroll{{overflow-x:auto;}}
</style></head><body><div class="wrap">
<h1>多机汇总 · 节点 × 用例矩阵</h1>
<div class="meta">生成于 {html.escape(datetime.now().isoformat(timespec='seconds'))} &nbsp;·&nbsp;
节点 {len(nodes)} 台 &nbsp;·&nbsp; 用例 {len(all_cases)} 条 &nbsp;·&nbsp;
✓ 通过 &nbsp; ✗ 失败 &nbsp; – 跳过 &nbsp; · 未跑到</div>

<h2>各节点统计</h2>
<div class="tbl-scroll"><table><thead><tr><th>节点</th><th>通过</th><th>失败</th><th>跳过</th></tr></thead><tbody>
{"".join(f'<tr><td>{html.escape(n)}</td><td style="color:{_COLOR["passed"]}">{totals[n]["passed"]}</td><td style="color:{_COLOR["failed"]}">{totals[n]["failed"]}</td><td>{totals[n]["skipped"]}</td></tr>' for n in nodes)}
</tbody></table></div>

<h2>节点环境</h2>
<div class="tbl-scroll"><table><thead><tr><th>节点</th><th>系统</th><th>分辨率</th><th>缩放</th><th>WebView2</th><th>WV2 版本</th><th>微软会话</th><th>密码凭据</th><th>SSO 凭据</th><th>共享盘</th></tr></thead><tbody>
{"".join(env_rows)}
</tbody></table></div>

<h2>节点 × 用例</h2>
<div class="tbl-scroll"><table><thead><tr><th>用例</th>{head}</tr></thead><tbody>
{"".join(rows)}
</tbody></table></div>
</div></body></html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="多机跑批控制机侧")
    sub = parser.add_subparsers(dest="command", required=True)

    p_dispatch = sub.add_parser("dispatch", help="生成并投放任务")
    p_dispatch.add_argument("--nodes", required=True, help="逗号分隔的节点标识")
    p_dispatch.add_argument("--suites", required=True, help=f"逗号分隔的套件名，可用：{', '.join(sorted(SUITES))}")
    p_dispatch.add_argument("--task-id", default="")
    p_dispatch.set_defaults(func=cmd_dispatch)

    p_status = sub.add_parser("status", help="看任务进度")
    p_status.set_defaults(func=cmd_status)

    p_agg = sub.add_parser("aggregate", help="汇总成节点×用例矩阵")
    p_agg.set_defaults(func=cmd_aggregate)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
