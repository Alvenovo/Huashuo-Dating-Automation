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

**`env` 只放非敏感项**（白名单见 `hall_auto/env_pack.py`：`HALL_ALLOW_INSTALL` /
`HALL_NODE_ID` / `HALL_FARM_ROOT` / `HALL_PACKAGE_SHARE` …）。任务文件躺在共享盘上，
**密码绝不写进去**。凭据走节点本地 `tools/farm_node.env`（已 gitignore），格式：

    HALL_TEST_USER=13800000000
    HALL_TEST_PASSWORD=xxxx
    HALL_MS_USER=someone@outlook.com
    HALL_SHARE_USER=hallshare
    HALL_SHARE_PASSWORD=xxxx

三档优先级：节点进程已有 env > 任务文件 env > 节点本地凭据文件。
不装配这一步的话，密码登录 / SSO 用例在节点机上必然静默 skip（报告上一片黄看不出根因）。

## 重复任务保护

`done/` 里已有同 `task_id` 的回执 → 拒绝执行（防止控制机重复 dispatch 把
`install` / `apps-lifecycle` 这类真装卸套件又跑一遍）。
任务里 `suites` 为空 → 拒收且**不写回执**（写"成功"回执会让 aggregate 假绿）。

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
from hall_auto.env_pack import NODE_ENV_FILENAME, build_suite_env, credential_status  # noqa: E402
from hall_auto.evidence import EVIDENCE_ROOT  # noqa: E402
from hall_auto.suites import build_command, get_suite  # noqa: E402

POLL_SECONDS = 30

# 节点本地凭据文件：仓库根下，已在 .gitignore，不进 git、不上共享盘。
NODE_ENV_PATH = REPO_ROOT / NODE_ENV_FILENAME


def farm_root() -> Path:
    """农场目录：环境变量 > config.local.yaml > 硬退出。

    环境变量优先（临时切到别的农场），其次是 `config.local.yaml` 里的 `farm_root`
    —— bootstrap 会把 `HALL_FARM_ROOT` 的值写进去，这样**节点机不用每次开新窗口重设**。

    两边都没有时**必须硬退出**，不能给个默认值：静默用错目录的表现是
    "节点在跑但一个任务都取不到"，从表面完全看不出根因。
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
            "它指向共享盘上的农场目录（含 tasks/ done/ results/ logs/）。\n"
            "例：$env:HALL_FARM_ROOT='\\\\SHARE\\qa\\hall-farm'\n"
            '或在 config.local.yaml 写一行 farm_root: "//LAPTOP-VS5F7HF4/hall-farm"'
            "（bootstrap 跑过一次就会自动写）"
        )
    return Path(raw)


def ensure_dirs(root: Path) -> None:
    for sub in ("tasks", "done", "results", "logs"):
        (root / sub).mkdir(parents=True, exist_ok=True)


def _reap_stale_running(root: Path, node: str) -> str:
    """清掉上一轮残留的 `.json.running` 占位文件。

    **这是个真会咬人的坑**：取任务的做法是把 `tasks/<node>.json` 改名成
    `tasks/<node>.json.running`。如果节点在跑的过程中挂了（断电、被结束、agent 报错退出），
    `.running` 就留在那儿 —— 而它**挡住了后续所有新任务的接管**，
    表现是"控制机投了任务，节点一直说无任务"，极难从表面看出来。

    处理原则：走 `.done` 语义 —— 存在 `.running` 就说明上一轮没正常收尾。
    这里只清占位文件；是否正确执行过由 `done/` 里的 `task_id` 回执来判定
    （`take_task` 里的去重检查负责那件事），两者职责不重叠。
    """
    stale = root / "tasks" / f"{node}.json.running"
    if not stale.is_file():
        return ""
    try:
        data = json.loads(stale.read_text(encoding="utf-8"))
        task_id = str(data.get("task_id") or "（无 task_id）")
    except (OSError, ValueError):
        task_id = "（内容读不出）"
    try:
        stale.unlink()
    except OSError as exc:
        return f"残留 .running 清不掉（{exc}）—— 新任务无法接管，请手工删除 {stale}"
    return f"已清理上一轮残留的 .running 占位（task_id={task_id}，说明上次没正常收尾）"


def take_task(root: Path, node: str) -> tuple[dict | None, str]:
    """取属于本节点的任务。取走即改名为 .running，避免同一台机器重复取。

    返回 (任务字典或 None, 拒绝原因)。**只取不校验的话有两个坑**：

    1. 控制机重复 dispatch 同一个 task_id，节点会把 `install` / `apps-lifecycle`
       这类真装卸套件再跑一遍 —— 白等十几分钟，还把机器状态搅乱。
       所以先查 `done/` 有没有同 task_id 的回执。
    2. 上一轮崩掉留下的 `.running` 会永久挡住新任务（见 `_reap_stale_running`）。
    """
    note = _reap_stale_running(root, node)

    task_file = root / "tasks" / f"{node}.json"
    if not task_file.is_file():
        return None, ""
    running = root / "tasks" / f"{node}.json.running"
    try:
        task_file.rename(running)
    except OSError as exc:
        return None, f"接管任务失败（{exc}）"
    try:
        task = json.loads(running.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"任务文件读不动/不是合法 JSON：{exc}"

    task_id = str(task.get("task_id") or "")
    if task_id and (root / "done" / f"{node}_{task_id}.json").is_file():
        # 已有回执：把占位也收掉，否则它会挡住下一个新任务
        running.unlink(missing_ok=True)
        return None, f"该 task_id 已有回执，拒绝重复执行（done/{node}_{task_id}.json）"

    suites = task.get("suites") or []
    if not suites:
        # 空套件列表直接拒收，**不写回执** —— 写一份"成功但什么都没跑"的回执
        # 会让控制机的 aggregate 把该节点算成已完成，掩盖控制机的 bug。
        # 占位文件要收掉，免得下次任务进不来。
        running.unlink(missing_ok=True)
        return None, "任务里 suites 为空，拒收（不写回执，避免假绿）"

    if note:
        return task, note
    return task, ""


def run_suite(
    suite_name: str,
    shard_id: int,
    shard_count: int,
    log_path: Path,
    *,
    task_env: dict[str, str] | None = None,
) -> tuple[int, dict[str, bool]]:
    """跑一个套件。返回 (退出码, 本轮凭据状态)。

    凭据状态一并返回，是因为调用方写回执时要用它 —— 再算一遍 `build_suite_env`
    没有意义（同参数必得同结果），还会多读一次节点凭据文件。
    """
    py = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    if not py.is_file():
        py = Path(sys.executable)
    suite = get_suite(suite_name)
    argv = [str(py), "-X", "utf8", "-m", "pytest", *build_command(suite, shard=shard_id, of=shard_count)]
    # 环境变量：任务文件（非敏感）+ 节点本地凭据文件 合成，节点已有值优先。
    # 不这么做的话，密码登录 / SSO 套件在节点机上必然静默 skip。
    env, notes = build_suite_env(task_env=task_env, node_env_path=NODE_ENV_PATH)
    creds = credential_status(env)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"\n===== {datetime.now().isoformat(timespec='seconds')} {' '.join(argv)} =====\n")
        for note in notes:
            fh.write(f"[env] {note}\n")
        fh.write(
            "[env] 凭据：密码登录={password_login} 微软SSO={microsoft_sso} "
            "改密码={change_password} 共享盘={share_creds}\n".format(**creds)
        )
        fh.flush()
        proc = subprocess.run(argv, cwd=str(REPO_ROOT), env=env, stdout=fh, stderr=subprocess.STDOUT, check=False)
    return proc.returncode, creds


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
        rc, _creds = run_suite(args.local, args.shard_id, args.shard_count, log)
        print(f"套件 {args.local} 退出码 {rc}，日志 {log}")
        return rc

    root = farm_root()
    ensure_dirs(root)

    if not args.once and not args.loop:
        parser.error("需指定 --once / --loop / --local")

    print(f"节点 {node} 农场目录 {root}")
    while True:
        task, reject = take_task(root, node)
        if task is None:
            if reject:
                print(f"拒收任务：{reject}", file=sys.stderr)
                if args.once:
                    return 2
                time.sleep(POLL_SECONDS)
                continue
            if args.once:
                print("无任务，退出")
                return 0
            time.sleep(POLL_SECONDS)
            continue

        if reject:
            # 非拒收的说明（如清理了残留占位）——打出来便于发现"上次没正常收尾"
            print(f"[提示] {reject}")

        task_id = str(task.get("task_id") or datetime.now().strftime("%Y%m%d_%H%M%S"))
        task_env = task.get("env") if isinstance(task.get("env"), dict) else {}
        print(f"取到任务 {task_id}")
        log = root / "logs" / f"{node}_{task_id}.log"
        results: list[dict] = []
        creds: dict[str, bool] = {}
        for entry in task.get("suites") or []:
            name = str(entry.get("name") or "")
            sid = int(entry.get("shard_id") or 1)
            scount = int(entry.get("shard_count") or 1)
            print(f"  跑 {name}（分片 {sid}/{scount}）")
            rc, creds = run_suite(name, sid, scount, log, task_env=task_env)
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

        # 凭据状态写进回执：报告里能区分「这台没配凭据 → 一片 skip」和「用例真跳过」。
        # 不写的话，汇总表上只有一堆黄色，看不出根因是环境没铺好。
        # creds 直接取自最后一个套件的 run_suite 结果（同参数合成必然同结果，不重算）。
        summary = {
            "task_id": task_id,
            "node": node,
            "profile": machine_profile(),
            "interactive": is_interactive_session(),
            "credentials": creds,
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
