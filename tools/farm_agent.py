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
**密码绝不写进去**。凭据走节点本地 `farm_node.env`（**仓库根**，已 gitignore），格式：

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

## `--loop` 空闲时必须留痕（2026-09-21 修）

空闲分支原来**一个字都不打**：启动打印两行，然后 30 秒一次静默轮询，屏幕上再也不动。
首台真机 DESKTOP-DOHED68 就为此被当成"卡住 5 分钟不动"，实际它只是在正常空转。

**更糟的是它和真故障长得一样**：UNC 一断，`Path.is_file()` 会把 `OSError` 吞成 `False`，
`take_task` 一路走到"无任务" —— 节点在报平安，其实一个任务都取不到。
所以现在每次空闲轮询都打一行心跳（带时间戳 + 它在等哪个文件），
并额外探一次农场目录可达性；探不到就换成一段"节点活着但取不到任务"的排查指引。

`--once` 同理：农场不可达时**不能**打「无任务，退出」再返回 0（那是假绿），
改为返回 `3`。退出码约定：`0` 正常／`2` 任务被拒收／`3` 农场不可达。

## 常亮（节点无人值守的前提）

`main()` 一进来就 `keep_awake_for_process()`（`hall_auto/awake.py`）：agent 活着期间
不许息屏、不许睡眠。**不设的话**，空转等任务那几小时里机器会按默认电源方案睡过去，
轮询线程一起停摆 —— 表现与「共享盘断了」一模一样，是同一类误导。

这一层只覆盖 agent 自己的生命周期；**跑批开始前 / 两轮之间 / 跑批结束后**那三段空档
要靠机器级那层（`tools/set_keep_awake.ps1`，bootstrap 会顺手设上）。
两层分工见 `hall_auto/awake.py` 模块头。
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

from hall_auto.awake import keep_awake_for_process  # noqa: E402
from hall_auto.dpi import is_interactive_session, machine_profile, node_id  # noqa: E402
from hall_auto.env_pack import (  # noqa: E402
    NODE_ENV_EVIDENCE_NAME,
    NODE_ENV_FILENAME,
    build_suite_env,
    credential_status,
)
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


def prepare_farm(root: Path) -> str:
    """确保农场目录可用：建齐四个子目录，再确认能取任务。`""` = 可用，否则一句原因。

    **两件事为什么合成一件、而且每轮都做**：

    1. 共享盘不通时 `mkdir` 会抛 `OSError`。原来这发生在 `main` 启动时，
       `--loop` 刚起来就吐一段 Python traceback 退出，一线看到的是
       `PermissionError: [WinError 5] 拒绝访问` —— **看着像权限问题，实际多半是包源机关了**。
       改成"报告 + 继续轮询"之后，共享盘恢复了自己就好，不用守着机器重跑。
    2. 空闲轮询也必须探可达性：UNC 断掉时 `Path.is_file()` 会把 `OSError` 吞成 `False`，
       `take_task` 于是走到"无任务"那条路 —— 节点在报平安，实际一个任务都取不到。
       这和"真的没任务"从表面看**一模一样**（本项目反复踩的"静默失败"）。

    建目录是幂等的，代价是每轮 4 次 `mkdir`（已存在就返回），可以忽略。
    """
    try:
        ensure_dirs(root)
    except OSError as exc:
        return f"连不上 / 建不出 {root}（{exc}）"
    tasks_dir = root / "tasks"
    if not tasks_dir.is_dir():
        return f"{tasks_dir} 不存在"
    return ""


def farm_hint(root: Path) -> str:
    """取不到任务时那段排查指引。`--once` 起不来和 `--loop` 空转共用一份，别写两遍。"""
    return (
        "         ⚠ 节点进程活着，但一个任务都取不到。逐条查：\n"
        "           ① 包源机（共享盘宿主）是不是关了 —— 它是唯一的包来源；\n"
        f"           ② 这台机器上手工连一次农场共享（见手册步骤 4）：net use {root}\n"
        "           ③ 农场共享名 / 机器名写错了（IP 会变，一律用机器名）。"
    )


def idle_line(node: str, root: Path, poll_index: int, problem: str) -> str:
    """空闲轮询要打印的那一行。**永不返回空串**。

    留痕的理由见模块 docstring「`--loop` 空闲时必须留痕」：
    没有它，现场无法区分"在正常等任务"和"UNC 卡死"，只能靠猜。
    """
    stamp = datetime.now().strftime("%H:%M:%S")
    if problem:
        return f"[{stamp}] 轮询 #{poll_index}：{problem}\n{farm_hint(root)}"
    return (
        f"[{stamp}] 轮询 #{poll_index}：无任务，{POLL_SECONDS} 秒后再查"
        f"（等 {root / 'tasks' / (node + '.json')}）"
    )


def run_suite(
    suite_name: str,
    shard_id: int,
    shard_count: int,
    log_path: Path,
    *,
    task_env: dict[str, str] | None = None,
    interactive: bool = False,
) -> tuple[int, dict[str, bool]]:
    """跑一个套件。返回 (退出码, 本轮凭据状态)。

    凭据状态一并返回，是因为调用方写回执时要用它 —— 再算一遍 `build_suite_env`
    没有意义（同参数必得同结果），还会多读一次节点凭据文件。

    `interactive=True` 用于人在环套件：**stdout 不能重定向**。这些用例靠 `input()`
    的提示语告诉人「现在去收哪个码」，重定向进日志文件的话提示语到不了终端，
    人会干等 —— 而日志里看着一切正常，最难查的一类故障。
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
    header = (
        f"\n===== {datetime.now().isoformat(timespec='seconds')} {' '.join(argv)} =====\n"
        + "".join(f"[env] {note}\n" for note in notes)
        + "[env] 凭据：密码登录={password_login} 微软SSO={microsoft_sso} "
        "改密码={change_password} 共享盘={share_creds}\n".format(**creds)
    )
    if interactive:
        # 先把「这段没落日志」记进日志，免得以后有人翻日志以为用例没跑。
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(header)
            fh.write("[env] 人在环：本段输出直接打到终端（提示语要让人看见），故不落本日志；")
            fh.write("用例结果仍在证据目录的 summary.json / report.html 里\n")
            fh.flush()
        proc = subprocess.run(argv, cwd=str(REPO_ROOT), env=env, check=False)
    else:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(header)
            fh.flush()
            proc = subprocess.run(
                argv, cwd=str(REPO_ROOT), env=env, stdout=fh, stderr=subprocess.STDOUT, check=False
            )
    return proc.returncode, creds


# 人在环套件：只能跑在「有真人守着的交互式终端」里，且**永不进农场任务文件**。
# 与 suites.py 的 farm_safe=False 是同一条约束的两侧，改一边要改另一边。
MANUAL_SUITE = "login-manual"

# 认「能」的回复。中文单字必须显式列出来：`input().lower()` 对「能」没影响，
# 但只认 y/yes 的话，按需求回「能」的人会被当成拒绝。
_MANUAL_YES = frozenset({"能", "可以", "好", "是", "行", "y", "yes", "ok", "1"})


def _manual_capable() -> bool:
    """能不能问人在环 —— 人既看得见提示（stdout 是终端）又答得了（stdin 是终端）。

    单独抽出来是为了**只有一个判据**：调用处再写一遍 `isatty()` 的话，
    迟早出现「问了但人不该看见」或「该问却没问」的半边修。
    守卫：tests/unit/test_farm_agent_policy.py（注入法验过能红）。
    """
    return sys.stdin.isatty() and sys.stdout.isatty()


def ask_manual_participation() -> bool:
    """问一句要不要现在参与人在环。**不是「人能看见又能回答」的环境就不问，直接 False。**

    判据要**两个都成立**，缺一不可：
      - `stdin.isatty()`：能回答。无人值守节点（计划任务）里 stdin 不是控制台，
        问了没人答 → 这个常驻 agent 会永久卡在 `input()` 上，
        而控制机只看到「执行中」，与节点死机同形，是最坏的一类挂起。
      - `stdout.isatty()`：能看见。输出被重定向进文件时，提示语到不了人眼前，
        人只看到 agent 不动了 —— 同一种挂起，只是原因不同。

    ⚠️ **`isatty()` 为真 ≠ 有人在**：本项目已踩过一次（bootstrap 第 7 步的探针
    先用当前身份建了隐式会话那类问题之外，`farm_agent` 起 pytest 时只重定向
    stdout/stderr、不重定向 stdin，子进程 `isatty()` 为真 → 真发短信 + `input()`
    永久阻塞）。所以这里还兜一层 `EOFError`：stdin 关着就当成不参与。
    """
    if not _manual_capable():
        return False
    print("\n" + "=" * 62, flush=True)
    print("自动套件已跑完。是否现在参与人在环？", flush=True)
    print("  参与后会在本终端依次向你要这几个验证码：", flush=True)
    print("    1) 短信登录        —— 1 个手机短信验证码（会真发短信）", flush=True)
    print("    2) 微软登录        —— 1 个邮箱验证码（发到 HALL_MS_USER 那个邮箱）", flush=True)
    print("    3) 忘记密码往返    —— 2 个手机短信验证码（会真把测试号密码改两次再改回）", flush=True)
    print("=" * 62, flush=True)
    try:
        answer = input("输入「能」开始（直接回车跳过）: ").strip().lower()
    except EOFError:
        return False
    return answer in _MANUAL_YES


def evidence_snapshot() -> set[str]:
    """跑套件**之前**给证据根目录拍个快照，用于事后判定"这一轮新增了哪个目录"。"""
    if not EVIDENCE_ROOT.is_dir():
        return set()
    return {d.name for d in EVIDENCE_ROOT.iterdir() if d.is_dir()}


def newest_run_dir(exclude: set[str] | None = None) -> Path | None:
    """本轮新产生的证据目录（按修改时间取最新）。

    `exclude` 传跑之前的快照。**不加这个过滤会张冠李戴**：套件没产出证据目录时
    （收集失败 / 进程起不来 / 一条用例都没收到），这里会返回**上一轮的旧目录**，
    调用方把它当本轮证据回传 —— 报告里那台机器就挂着一份别的运行的结果，
    比"没有证据"危险得多。
    """
    if not EVIDENCE_ROOT.is_dir():
        return None
    dirs = [
        d for d in EVIDENCE_ROOT.iterdir()
        if d.is_dir() and (exclude is None or d.name not in exclude)
    ]
    return max(dirs, key=lambda d: d.stat().st_mtime) if dirs else None


def _write_node_env(run_dir: Path, node: str, task_id: str, suite: str, creds: dict[str, bool]) -> None:
    """把本轮凭据状态写进证据目录，随证据一起回传。

    **为什么必须落到证据里**：控制机汇总（`farm_control.aggregate`）只读 `results/`
    下的证据目录，不读 `done/` 回执。凭据状态只写回执的话，汇总报告的「凭据」列
    永远是 ✗ —— 而手册让测试人员正是靠这一列区分「这台机器没配凭据」和
    「凭据配了、是用例本身在跳过」。写错一处，一线就会去反复折腾 `farm_node.env`。
    """
    try:
        (run_dir / NODE_ENV_EVIDENCE_NAME).write_text(
            json.dumps(
                {
                    "node": node,
                    "task_id": task_id,
                    "suite": suite,
                    "credentials": creds,
                    "profile": machine_profile(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"  凭据状态写入证据失败（汇总报告凭据列会不准）：{exc}", file=sys.stderr)


def execute_suite(
    name: str,
    shard_id: int,
    shard_count: int,
    *,
    root: Path,
    node: str,
    task_id: str,
    log: Path,
    task_env: dict[str, str],
    interactive: bool = False,
) -> tuple[dict, dict[str, bool]]:
    """跑一个套件并把证据回传农场。返回 (回执条目, 本轮凭据状态)。

    抽出来是因为**人在环阶段要走同一套**：拍证据快照 → 跑 → 认本轮新目录 →
    写凭据 → 回传。复制一份的话，两边的「认错上一轮目录」这类坑迟早只修一边。
    """
    print(f"  跑 {name}（分片 {shard_id}/{shard_count}）", flush=True)
    before = evidence_snapshot()   # 先拍快照，避免没产出证据时错认上一轮的目录
    rc, creds = run_suite(name, shard_id, shard_count, log, task_env=task_env, interactive=interactive)
    run_dir = newest_run_dir(exclude=before)
    entry: dict = {
        "suite": name,
        "shard_id": shard_id,
        "shard_count": shard_count,
        "exit_code": rc,
        "run_dir": run_dir.name if run_dir else None,
    }
    if interactive:
        # 标出来：汇总时能区分「无人值守跑的」和「真人守着收码跑的」，
        # 否则一条要人输验证码的用例和一条纯自动用例在报告里长得一样。
        entry["interactive"] = True
    if run_dir is None:
        print(f"  {name} 本轮没产出证据目录（rc={rc}），不猜测、不回传", file=sys.stderr, flush=True)
        return entry, creds
    _write_node_env(run_dir, node, task_id, name, creds)
    dest = root / "results" / node / run_dir.name
    try:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(run_dir, dest)
        print(f"  证据已回传 {dest}", flush=True)
    except OSError as exc:
        print(f"  证据回传失败：{exc}", file=sys.stderr, flush=True)
    return entry, creds


def main() -> int:
    parser = argparse.ArgumentParser(description="多机跑批节点侧 agent")
    parser.add_argument("--once", action="store_true", help="取一次任务就跑完退出")
    parser.add_argument("--loop", action="store_true", help="持续轮询共享盘")
    parser.add_argument("--local", metavar="SUITE", default="", help="不走共享盘，本机直接跑指定套件")
    parser.add_argument("--shard-id", type=int, default=1)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument(
        "--no-manual",
        action="store_true",
        help="跑完自动套件后不再问「要不要参与人在环」。计划任务/无人值守场景加它。",
    )
    args = parser.parse_args()

    # 节点机无人值守：agent 空转等任务时可能一等等几小时，机器若按默认电源方案
    # （显示器 10 分钟关、睡眠 30 分钟）睡过去，**这个轮询线程会一起停摆** ——
    # 控制机看不到回执，现场表现与「共享盘断了」「agent 崩了」完全同形，排查方向全错。
    # 这里用进程级常亮兜住 agent 自己的生命周期（零权限、退出自动失效）；
    # 机器级那层（改电源方案，覆盖 agent 没在跑的空档）见 tools/set_keep_awake.ps1。
    if not keep_awake_for_process():
        print(
            "[提示] 常亮设置未生效（SetThreadExecutionState 调用失败）——"
            "本机若会息屏/睡眠，agent 空转期间可能停摆",
            file=sys.stderr, flush=True,
        )

    node = node_id()

    if args.local:
        log = REPO_ROOT / "reports" / "farm" / f"local_{args.local}.log"
        # 人在环套件用 --local 跑时**必须交互**：它的全部意义就是让人在终端输验证码，
        # 输出被重定向进日志的话提示语到不了终端，人只能干等。
        # 判据取套件自己的 `interactive`（不是硬写 login-manual，也不是反推 farm_safe）：
        # 以后再加人在环套件只要在 SUITES 里标上就行。
        # ⚠️ 光这里不重定向**还不够** —— pytest 自己那层捕获要套件定义带的 `-s` 才关得掉，
        # 否则 `sys.stdin.isatty()` 恒 False，三条用例全 skip 还打「退出码 0」。
        # 2026-09-22 真机踩过，见 hall_auto/suites.py 的 build_command。
        rc, _creds = run_suite(
            args.local,
            args.shard_id,
            args.shard_count,
            log,
            interactive=get_suite(args.local).interactive,
        )
        print(f"套件 {args.local} 退出码 {rc}，日志 {log}")
        return rc

    root = farm_root()

    if not args.once and not args.loop:
        parser.error("需指定 --once / --loop / --local")

    print(f"节点 {node} 农场目录 {root}", flush=True)
    # 把"它在等哪个文件"直接打出来：节点名和控制机 `dispatch --nodes` 对不上
    # 是「一直无任务」的头号原因，写出来一眼就能核对，不用去翻 config / 环境变量。
    print(f"等待任务文件 {root / 'tasks' / f'{node}.json'}", flush=True)
    # flush 不能省：`--loop` 常驻，输出被重定向到文件/管道时是块缓冲的，
    # 不 flush 就攒在缓冲区里 —— 又变回"看起来卡住"。
    poll_index = 0
    while True:
        problem = prepare_farm(root)
        task, reject = take_task(root, node)
        if task is None:
            if reject:
                print(f"拒收任务：{reject}", file=sys.stderr, flush=True)
                if args.once:
                    return 2
                time.sleep(POLL_SECONDS)
                continue
            if args.once:
                if problem:
                    # 农场不可达 ≠ 无任务。打「无任务，退出」+ 返回 0 是**假绿**：
                    # 一线会以为链路通了、只是没人投任务，实际包源机可能已经关了。
                    print(f"农场不可达，没取到任务：{problem}", file=sys.stderr, flush=True)
                    print(farm_hint(root), file=sys.stderr, flush=True)
                    return 3
                print("无任务，退出", flush=True)
                return 0
            poll_index += 1
            print(idle_line(node, root, poll_index, problem), flush=True)
            time.sleep(POLL_SECONDS)
            continue

        if reject:
            # 非拒收的说明（如清理了残留占位）——打出来便于发现"上次没正常收尾"
            print(f"[提示] {reject}", flush=True)

        task_id = str(task.get("task_id") or datetime.now().strftime("%Y%m%d_%H%M%S"))
        task_env = task.get("env") if isinstance(task.get("env"), dict) else {}
        print(f"取到任务 {task_id}", flush=True)
        log = root / "logs" / f"{node}_{task_id}.log"
        results: list[dict] = []
        creds: dict[str, bool] = {}
        for entry in task.get("suites") or []:
            name = str(entry.get("name") or "")
            sid = int(entry.get("shard_id") or 1)
            scount = int(entry.get("shard_count") or 1)
            item, creds = execute_suite(
                name, sid, scount, root=root, node=node, task_id=task_id, log=log, task_env=task_env
            )
            results.append(item)

        # 人在环阶段：**等自动套件全跑完再问**，不夹在中间 ——
        # 夹在中间的话，一条要人输码的用例会把整批自动套件卡在后面，
        # 而控制机那边只看到「执行中」，与节点死机同形。
        if not args.no_manual:
            if ask_manual_participation():
                print("  开始人在环，按提示输入验证码。", flush=True)
                item, manual_creds = execute_suite(
                    MANUAL_SUITE, 1, 1, root=root, node=node, task_id=task_id, log=log,
                    task_env=task_env, interactive=True,
                )
                results.append(item)
                if manual_creds:
                    creds = manual_creds
            elif _manual_capable():
                # 人明确跳过了：把补跑命令打出来，别让他再去翻手册。
                print(
                    "  跳过人在环。要补跑：.\\.venv\\Scripts\\python.exe -X utf8 "
                    f"tools\\farm_agent.py --local {MANUAL_SUITE}",
                    flush=True,
                )

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
        print(f"任务完成，结果 {done_file}", flush=True)

        if args.once:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
