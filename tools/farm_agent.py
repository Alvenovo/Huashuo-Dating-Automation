"""多机跑批：节点侧 agent（拉任务 → 跑套件 → 回传结果）。

## 设计选择（为什么不做自建调度器）

20 台、一台控制机、一个人维护。自建调度器 = 新引入一个服务要部署、监控、修 bug，
收益只有"更实时"。**共享盘投递一个 JSON 任务文件 + 计划任务轮询**同样能跑，代码量差一个数量级。
50+ 台或需要实时看板时再上调度器。

## 工作目录约定（`farm_root`，默认取环境变量 `HALL_FARM_ROOT`）

    farm_root/
      tasks/        控制机投放的任务文件  <node>.json
      done/         节点跑完把任务挪进来，附带结果摘要
      results/      节点回传的证据目录（整个 run 目录拷贝过来）  <node>/<run>/
      logs/         节点执行日志

## 任务文件格式（tasks/<node>.json）

    {
      "task_id": "R12_20260918_1400",
      "node": "R12",
      "suites": [{"name": "launch", "shard_id": 1, "shard_count": 1}],
      "env": {"HALL_ALLOW_INSTALL": "0"},
      "created_at": "2026-09-18T14:00:00"
    }

节点侧读到属于自己 node 的文件就执行；执行完把任务挪到 done/，结果拷到 results/<node>/。

## 用法（节点机）

    .\\.venv\\Scripts\\python.exe -X utf8 tools/farm_agent.py --once          # 有任务就跑一轮
    .\\.venv\\Scripts\\python.exe -X utf8 tools/farm_agent.py --loop         # 持续轮询
    .\\.venv\\Scripts\\python.exe -X utf8 tools/farm_agent.py --local launch # 不走共享盘，本机直接跑某套件
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from hall_auto.dpi import is_interactive_session, machine_profile, node_id  # noqa: E402
from hall_auto.evidence import EVIDENCE_ROOT  # noqa: E402
from hall_auto.suites import build_command, get_suite  # noqa: E402

POLL_SECONDS = 30


def farm_root() -> Path:
    raw = os.environ.get("HALL_FARM_ROOT", "").strip()
    if not raw:
        raise SystemExit(
            "未设 HALL_FARM_ROOT。它指向共享盘上的农场目录（含 tasks/ done/ results/ logs/）。\n"
            "例：$env:HALL_FARM_ROOT='\\\\SHARE\\qa\\hall-farm'"
        )
    return Path(raw)


def ensure_dirs(root: Path) -> None:
    for sub in ("tasks", "done", "results", "logs"):
        (root / sub).mkdir(parents=True, exist_ok=True)


def take_task(root: Path, node: str) -> dict | None:
    """取属于本节点的任务。取走即改名为 .running，避免同一台机器重复取。"""
    task_file = root / "tasks" / f"{node}.json"
    if not task_file.is_file():
        return None
    running = root / "tasks" / f"{node}.json.running"
    try:
        task_file.rename(running)
    except OSError:
        return None
    try:
        return json.loads(running.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run_suite(suite_name: str, shard_id: int, shard_count: int, log_path: Path) -> int:
    py = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    if not py.is_file():
        py = Path(sys.executable)
    suite = get_suite(suite_name)
    argv = [str(py), "-X", "utf8", "-m", "pytest", *build_command(suite, shard=shard_id, of=shard_count)]
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"\n===== {datetime.now().isoformat(timespec='seconds')} {' '.join(argv)} =====\n")
        fh.flush()
        proc = subprocess.run(argv, cwd=str(REPO_ROOT), env=env, stdout=fh, stderr=subprocess.STDOUT, check=False)
    return proc.returncode


def newest_run_dir() -> Path | None:
    """本轮新产生的证据目录（按修改时间取最新）。"""
    if not EVIDENCE_ROOT.is_dir():
        return None
    dirs = [d for d in EVIDENCE_ROOT.iterdir() if d.is_dir()]
    return max(dirs, key=lambda d: d.stat().st_mtime) if dirs else None


def main() -> int:
    parser = argparse.ArgumentParser(description="多机跑批节点侧 agent")
    parser.add_argument("--once", action="store_true", help="取一次任务就跑完退出")
    parser.add_argument("--loop", action="store_true", help="持续轮询共享盘")
    parser.add_argument("--local", metavar="SUITE", default="", help="不走共享盘，本机直接跑指定套件")
    parser.add_argument("--shard-id", type=int, default=1)
    parser.add_argument("--shard-count", type=int, default=1)
    args = parser.parse_args()

    node = node_id()

    if args.local:
        log = REPO_ROOT / "reports" / "farm" / f"local_{args.local}.log"
        rc = run_suite(args.local, args.shard_id, args.shard_count, log)
        print(f"套件 {args.local} 退出码 {rc}，日志 {log}")
        return rc

    root = farm_root()
    ensure_dirs(root)

    if not args.once and not args.loop:
        parser.error("需指定 --once / --loop / --local")

    print(f"节点 {node} 农场目录 {root}")
    while True:
        task = take_task(root, node)
        if task is None:
            if args.once:
                print("无任务，退出")
                return 0
            time.sleep(POLL_SECONDS)
            continue

        task_id = str(task.get("task_id") or datetime.now().strftime("%Y%m%d_%H%M%S"))
        print(f"取到任务 {task_id}")
        log = root / "logs" / f"{node}_{task_id}.log"
        results: list[dict] = []
        for entry in task.get("suites") or []:
            name = str(entry.get("name") or "")
            sid = int(entry.get("shard_id") or 1)
            scount = int(entry.get("shard_count") or 1)
            print(f"  跑 {name}（分片 {sid}/{scount}）")
            rc = run_suite(name, sid, scount, log)
            run_dir = newest_run_dir()
            results.append({"suite": name, "shard_id": sid, "shard_count": scount, "exit_code": rc,
                            "run_dir": run_dir.name if run_dir else None})
            if run_dir is not None:
                dest = root / "results" / node / run_dir.name
                try:
                    if dest.exists():
                        shutil.rmtree(dest, ignore_errors=True)
                    shutil.copytree(run_dir, dest)
                    print(f"  证据已回传 {dest}")
                except OSError as exc:
                    print(f"  证据回传失败：{exc}", file=sys.stderr)

        summary = {
            "task_id": task_id,
            "node": node,
            "profile": machine_profile(),
            "interactive": is_interactive_session(),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "results": results,
        }
        done_file = root / "done" / f"{node}_{task_id}.json"
        done_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        (root / "tasks" / f"{node}.json.running").unlink(missing_ok=True)
        print(f"任务完成，结果 {done_file}")

        if args.once:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
