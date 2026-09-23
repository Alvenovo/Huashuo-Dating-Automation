"""提权执行器：被计划任务 `HallAutoP1` 以管理员身份拉起，跑一个「需要管理员」的套件。

## 为什么是这个形状

计划任务的动作字段是**固定字符串**，传不了参数，所以参数走文件：
`farm_agent` 写 `reports/_elev/request.json` → `schtasks /Run /TN HallAutoP1` →
本脚本读请求 → 跑 pytest → 写 `result.json`。协议的常量与校验都在
`hall_auto/elevation.py`，两边不会漂移。

**只做一件事**：按请求跑一次套件并把结果落盘。不做调度、不重试、不自己找活干 ——
那些是 `farm_agent` 的职责。职责单一的好处是：这个脚本出问题时，
「是提权没生效」还是「是任务没投对」一眼能分开。

## 幂等

任务被连点两次（人手动触发、或上一轮的 `/Run` 排队）时，第二次读到的是
**同一份请求**，而结果文件里的 `request_id` 已经是它 → 直接退出，不重复跑。
真装卸跑两遍会白等十几分钟，还会把机器状态搅乱。

## 非提权时会怎样

计划任务被改坏（`/RL HIGHEST` 掉了）时，本脚本以普通权限起来 —— 直接报错退出码 2，
**不静默降级**：真装真卸跑到一半失败留下的机器状态比「压根没跑」难收拾得多。

## 手工排查

    # 看最后一次提权段跑了什么、退出码多少
    type reports\\_elev\\result.json
    # 看 pytest 输出
    type reports\\_elev\\elevated_run.log
    # 看计划任务的动作指向
    schtasks /Query /TN HallAutoP1 /XML
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from hall_auto.elevation import (  # noqa: E402
    ELEVATION_TASK_NAME,
    is_elevated,
    log_path,
    read_json,
    read_request,
    result_path,
    validate_pytest_args,
    venv_python,
    write_json,
)

# 提权段统一打开的总开关。**只在这里打开**，不进节点进程环境 ——
# 让「会不会真装」只由「这个套件走了提权通道」决定，不由谁忘了设某个变量决定。
FORCED_ENV = {
    "HALL_ALLOW_INSTALL": "1",
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
}

# 请求里允许带过来的环境变量。任务文件的 env 走的是同一份白名单
# （hall_auto/env_pack.TASK_ENV_ALLOWLIST），这里再挡一层：请求文件在仓库里，
# 提权跑的是它，宁可多挡一次。
_ENV_ALLOWLIST = frozenset({
    "HALL_ALLOW_PACKAGE_DOWNLOAD",
    "HALL_NODE_ID",
    "HALL_FARM_ROOT",
    "HALL_PACKAGE_SHARE",
    "HALL_PACKAGE_CACHE",
    "HALL_INSTALLER_DIR",
})


def _finish(request_id: str, suite: str, exit_code: int, note: str = "", run_dir: str = "") -> int:
    write_json(
        result_path(),
        {
            "request_id": request_id,
            "suite": suite,
            "exit_code": exit_code,
            "note": note,
            "run_dir": run_dir,
            "elevated": is_elevated(),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    return exit_code


def main() -> int:
    request = read_request()
    request_id = str(request.get("request_id") or "")
    suite = str(request.get("suite") or "")
    if not request_id or not suite:
        print("没有待执行的提权请求（request.json 缺失或字段不全）—— 不是错误，直接退出。", flush=True)
        return 0

    # 幂等：同一份请求已经跑过就不重复跑（任务被连点两次会走到这里）
    done = read_json(result_path())
    if done.get("request_id") == request_id:
        print(f"请求 {request_id} 已经跑过（退出码 {done.get('exit_code')}），不重复执行。", flush=True)
        return 0

    if not is_elevated():
        # 计划任务被改坏时早报错。静默降级跑真装真卸，留下的机器状态比没跑难收拾。
        print(
            f"[FAIL] 提权执行器以**普通权限**起来了 —— 计划任务 {ELEVATION_TASK_NAME} "
            "的 /RL HIGHEST 掉了，或有人直接跑了本脚本。\n"
            "      真装真卸必须在管理员身份下跑。重建任务：用管理员重跑 "
            "tools\\bootstrap_machine.ps1（幂等，前面步骤会跳过）。",
            flush=True,
        )
        return _finish(request_id, suite, 2, note="非提权，拒绝执行")

    args, reject = validate_pytest_args(request.get("pytest_args"))
    if reject:
        print(f"[FAIL] {reject}", flush=True)
        return _finish(request_id, suite, 2, note=reject)

    env = dict(os.environ)
    for key, value in (request.get("env") or {}).items():
        if str(key) in _ENV_ALLOWLIST:
            env[str(key)] = str(value)
    env.update(FORCED_ENV)

    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    shard_id = int(request.get("shard_id") or 1)
    shard_count = int(request.get("shard_count") or 1)
    stamp = datetime.now().isoformat(timespec="seconds")

    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n===== {stamp} 提权段 {suite}（分片 {shard_id}/{shard_count}）=====\n")
        fh.write(f"[elev] request_id={request_id} admin=True cwd={REPO_ROOT}\n")
        fh.flush()

        if request.get("reset_fixture"):
            # 夹具复位必须先跑：上一次装剩的会让装卸用例**直接 skip**（不报错），
            # 报告上一片黄而人以为"跑过了"。run_p1_apps.ps1 原来也是这个顺序。
            reset_cmd = [str(venv_python()), "-u", "tools/reset_fixture.py"]
            fh.write(f"[elev] $ {' '.join(reset_cmd)}\n")
            fh.flush()
            reset = subprocess.run(
                reset_cmd, cwd=str(REPO_ROOT), env=env,
                stdout=fh, stderr=subprocess.STDOUT, check=False,
            )
            fh.write(f"[elev] reset exit={reset.returncode}\n")
            fh.flush()

        argv = [str(venv_python()), "-X", "utf8", "-m", "pytest", *args]
        fh.write(f"[elev] $ {' '.join(argv)}\n")
        fh.flush()
        proc = subprocess.run(
            argv, cwd=str(REPO_ROOT), env=env,
            stdout=fh, stderr=subprocess.STDOUT, check=False,
        )

    print(f"提权段 {suite} 退出码 {proc.returncode}，日志 {log}", flush=True)
    return _finish(request_id, suite, proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
