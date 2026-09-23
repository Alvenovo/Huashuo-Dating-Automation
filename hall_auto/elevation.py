"""提权通道：让**非提权**的农场 agent 跑得动「需要管理员」的套件。

## 要解决什么问题

`install`（大厅自身装卸）和 `apps-lifecycle`（夹具真装真卸）必须提权：前者写
`Program Files` 和 HKLM 卸载表，后者的厂商卸载器本身就是提权进程（实测
`TokenElevation=1`），非提权点不动它。

而农场 agent 是**非提权**常驻进程 —— 这不是将就，是刻意：整体提权会改掉
`launch` / `login` 那批用例的权限上下文（见《运行手册》UIPI 边界那一节）。
于是原来的结论是「这两类套件不进农场」，代价是跑批被切成三段，人要开管理员窗口手敲。

**但提权通道早就建好了**：bootstrap 第 11 步建了计划任务 `HallAutoP1`（`/RL HIGHEST`），
**标准（过滤）令牌就能启动它**，已端到端验过 0 人工。缺的只是把它接进任务流。

## 参数为什么走文件

计划任务的「操作」字段是**一段固定字符串**，不能传命令行参数 —— 这条坎在
`学习库/07-用例与断言策略/03-标记体系与门禁.md` 里记着，也是当初把提权段做成
「手工开管理员窗口」的直接原因。

解法是标准的提权 broker：非提权侧写请求文件 → 触发固定动作 → 提权侧读请求执行 → 写结果文件。

    reports/_elev/request.json    非提权侧写（原子写：先 .tmp 再 replace）
    reports/_elev/result.json     提权侧写
    reports/_elev/elevated_run.log  提权侧的 pytest 输出

**同机器、同用户**，所以两边读写同一个目录没有 ACL 问题（提权进程读得到用户的文件，
非提权进程也读得到提权进程写的文件）。

## request_id 是唯一判据

结果文件按 `request_id` 匹配，不按时间戳、不按「文件存不存在」——
连续投两轮时，上一轮的结果文件还在原地，只认「文件在」会把旧结果当成新一轮的
（本项目反复踩的「错认上一轮」那一类假绿）。所以：

- 写请求前先删掉旧的 `result.json`；
- 提权侧拿到请求先看结果文件里的 `request_id` 是否已经是它，是就直接退出（连点两次任务不重复跑）。

守卫：`tests/unit/test_elevation.py`。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# **不 import hall_auto.config**：它顶层 `import yaml`，而本模块会被
# `tools/bootstrap_machine.py` 导入 —— 那台机器上 PyYAML 还没装（装依赖正是它的第 2 步）。
# 走 config 的话，新机上 `python tools/bootstrap_machine.py` 会在打印第一行之前就崩，
# 报错指向"环境没铺好"而实际是"铺环境的脚本自己起不来"。
# 同一条教训见 tests/unit/test_bootstrap_policy.py::test_bootstrap_module_imports_without_pyyaml。
REPO_ROOT = Path(__file__).resolve().parents[1]

# 计划任务名。**唯一定义处** —— bootstrap 建它、farm_agent 触发它、文档写它，
# 三处漂移的表现是「任务触发了但什么都没跑」，且节点侧一句报错都没有。
ELEVATION_TASK_NAME = "HallAutoP1"

# 计划任务的动作脚本（相对仓库根）。任务动作字符串是固定的，所以参数只能走文件。
ELEVATION_SCRIPT_REL = "tools/run_elevated_suite.ps1"
ELEVATION_SCRIPT = REPO_ROOT / ELEVATION_SCRIPT_REL

REQUEST_DIR = REPO_ROOT / "reports" / "_elev"
REQUEST_NAME = "request.json"
RESULT_NAME = "result.json"
LOG_NAME = "elevated_run.log"

# 提权段只允许这些 pytest 参数通过。请求文件躺在仓库里、又能被提权进程执行，
# 白名单是防「文件被塞了别的东西」的第二道闸（第一道是仓库本身的写权限）。
_ALLOWED_FLAGS = frozenset({"-v", "-s", "-q", "--tb=short", "-p", "no:cacheprovider"})
_ALLOWED_PREFIXES = ("--shard-id=", "--shard-count=")


def request_path(root: Path | None = None) -> Path:
    return (root or REQUEST_DIR) / REQUEST_NAME


def result_path(root: Path | None = None) -> Path:
    return (root or REQUEST_DIR) / RESULT_NAME


def log_path(root: Path | None = None) -> Path:
    return (root or REQUEST_DIR) / LOG_NAME


def is_elevated() -> bool:
    """当前进程是否提权。

    抽成函数是为了**只有一个判据**：`farm_agent` 要用它决定「直接跑」还是「走 broker」，
    提权执行器也要用它确认自己真的提权了（计划任务被改成非提权时，早报错比跑一半挂掉好）。
    """
    if os.name != "nt":  # pragma: no cover - 本项目只在 Windows 跑
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # pragma: no cover - 拿不到就按非提权处理（保守）
        return False


def new_request_id(task_id: str, suite: str, shard_id: int) -> str:
    """请求标识。**必须能在一毫秒内连发多次都不重复**。

    ⚠️ 不要用 `datetime.now().strftime('%f')`：Windows 上 `datetime.now()` 的分辨率
    约 15.6 毫秒（不是微秒），连续两次调用会拿到**同一个值**。而一个任务里
    几个提权套件是连着投的 —— 撞 id 的后果是第二个套件的请求被提权侧判成
    「已经跑过」直接跳过，**不报错**，回执里还写着成功。
    `time.perf_counter_ns()` 走 QPC，分辨率足够；前面挂日期只是为了排查时看得懂。
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{task_id}_{suite}_s{shard_id}_{stamp}_{time.perf_counter_ns()}"


def write_json(path: Path, payload: dict) -> None:
    """原子写：先写 `.tmp` 再 `replace`。

    直接 `write_text` 的话，提权侧可能读到**写了一半**的 JSON ——
    解析失败被当成「没有请求」，表现同样是「触发了但什么都没跑」。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> dict:
    """读 JSON。不存在 / 读不动 / 不是字典 → 空字典（调用方按「没有」处理）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def validate_pytest_args(raw: object) -> tuple[list[str], str]:
    """校验请求里的 pytest 参数。返回 (参数列表, 拒绝原因)；原因非空即拒。

    允许的形态就三种：`tests/` 下的路径、白名单短参数、`--shard-*`。
    其余一律拒绝 —— 提权段跑的是管理员 pytest，参数来源必须是**可预期**的。
    """
    if not isinstance(raw, list) or not raw:
        return [], "pytest 参数为空"
    out: list[str] = []
    for item in raw:
        token = str(item)
        if token in _ALLOWED_FLAGS:
            out.append(token)
            continue
        if token.startswith(_ALLOWED_PREFIXES):
            out.append(token)
            continue
        if token.startswith("tests/") and ".." not in token:
            out.append(token)
            continue
        if token == "-m" or (out and out[-1] == "-m"):
            # `-m` 后面那个表达式。marker 表达式是自由文本，只能放行紧跟其后的一个 token。
            out.append(token)
            continue
        return [], f"不允许的 pytest 参数：{token!r}"
    return out, ""


def write_request(
    *,
    task_id: str,
    suite: str,
    shard_id: int,
    shard_count: int,
    pytest_args: list[str],
    reset_fixture: bool = False,
    env: dict[str, str] | None = None,
    root: Path | None = None,
) -> str:
    """写请求并**清掉上一轮的结果**。返回 request_id。

    清旧结果不能省：不清的话，提权侧刚被触发就能读到上一轮的结果文件，
    按 `request_id` 比对虽然能挡住误判，但会让「请求还没被处理」和
    「处理完了」之间的区分多绕一层。清掉最省事也最不容易错。
    """
    request_id = new_request_id(task_id, suite, shard_id)
    write_json(
        request_path(root),
        {
            "request_id": request_id,
            "task_id": task_id,
            "suite": suite,
            "shard_id": shard_id,
            "shard_count": shard_count,
            "reset_fixture": bool(reset_fixture),
            "pytest_args": [str(a) for a in pytest_args],
            "env": {str(k): str(v) for k, v in (env or {}).items()},
            "created_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    try:
        result_path(root).unlink()
    except OSError:
        pass
    return request_id


def read_request(root: Path | None = None) -> dict:
    """提权侧读请求。不存在 / 读不动 / 写了一半 → 空字典（调用方按「没有请求」处理）。"""
    return read_json(request_path(root))


def read_result_for(request_id: str, root: Path | None = None) -> dict:
    """取本次请求的结果。`request_id` 对不上 → 空字典（**不许**把上一轮的结果当本轮）。"""
    data = read_json(result_path(root))
    if data.get("request_id") != request_id:
        return {}
    return data


def trigger_task(task: str = ELEVATION_TASK_NAME) -> tuple[bool, str]:
    """用标准令牌启动提权计划任务。返回 (成功?, 说明)。

    ⚠️ **不要在这里加 `shell=True`**：Git Bash 会把 `/Run` 当路径转换，
    命令行版必须写 `MSYS_NO_PATHCONV=1`；Python 直接传参数列表就没有这个问题，
    这也是走 broker 比让人粘命令稳的地方之一。
    """
    try:
        proc = subprocess.run(
            ["schtasks", "/Run", "/TN", task],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        return False, f"启动不了 schtasks：{exc}"
    if proc.returncode != 0:
        detail = (proc.stdout or "") + (proc.stderr or "")
        return False, f"schtasks /Run 退出码 {proc.returncode}：{detail.strip()[:300]}"
    return True, "已触发"


def venv_python() -> Path:
    """仓库 venv 的解释器；不存在时回落到当前解释器（与 farm_agent.run_suite 同口径）。"""
    py = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    return py if py.is_file() else Path(sys.executable)
