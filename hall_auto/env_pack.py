"""节点侧运行时环境变量装配：把「任务文件」+「节点本地凭据文件」合成本轮进程环境。

## 要解决什么问题

多机跑批时节点机是**新机器、无人值守**，而好几套用例靠环境变量才能跑起来：

| 变量 | 谁需要 | 不给会怎样 |
| --- | --- | --- |
| `HALL_TEST_USER` / `HALL_TEST_PASSWORD` | `login` 套件 | 密码登录用例静默 skip |
| `HALL_MS_USER` | `login` 套件里的 SSO | 免密用例静默 skip |
| `HALL_TEST_NEW_PASSWORD` | `login-manual`（人在环） | 改密码往返跑不了 |
| `HALL_NODE_ID` | 全部 | 回退成主机名，改名后证据目录对不上 |
| `HALL_SHARE_USER` / `HALL_SHARE_PASSWORD` | 共享盘取包 | 只靠 `bootstrap` 时连过，`/persistent:yes` 挂着也能取 |

原来 `farm_agent` 是把节点进程的 `os.environ` 原样传给 pytest —— 那么凭据就必须
**在每台机器上由人先设好**。20 台机器逐台手工设，正是要消灭的动作，
而且漏一台在报告上表现为一片 skip 黄，看不出是"没配"还是"真跳过"。

## 三档优先级（高 → 低）

1. **节点进程已有的环境变量** —— 人在那台机器上显式设过，最高优先（也保住了
   "凭据只走环境变量"这条红线：设过就不被文件覆盖）
2. **任务文件里的 `env`** —— 控制机随任务下发。**只放非敏感项**
   （`HALL_ALLOW_INSTALL` / `HALL_NODE_ID` / `HALL_FARM_ROOT` / `HALL_PACKAGE_SHARE`），
   共享盘是全组可读的，**密码绝不往这里放**
3. **节点本地凭据文件** —— 仓库根下 `farm_node.env`（`KEY=VALUE`，**已在 .gitignore**）。
   凭据放机器本地、不落共享盘。bootstrap 会生成一份空模板，由人填。

## 为什么凭据不写进任务文件

任务文件躺在共享盘 `tasks/` 下，**共享盘是给所有测试机读的**，任何人能 `dir` 出来。
写进去等于把测试账号密码公开到一个网络位置上。所以按用途分开：
能公开的走任务文件，敏感的走机器本地。

## 凭据文件格式

    # tools/farm_node.env   （KEY=VALUE，允许 # 注释与空行，值不带引号）
    HALL_TEST_USER=13800000000
    HALL_TEST_PASSWORD=xxxx
    HALL_MS_USER=someone@outlook.com

不解析引号、不支持多行 —— 故意做简单，凭据文件里不应出现需要转义的值。
"""

from __future__ import annotations

import os
from pathlib import Path

# 节点本地凭据文件：仓库根下，已在 .gitignore。不放共享盘。
NODE_ENV_FILENAME = "farm_node.env"

# 随证据目录一起回传的「本轮凭据状态」文件名。
# 节点侧（tools/farm_agent.py）写、控制机侧（tools/farm_control.py）读，
# 两边必须同名 —— 汇总报告的「凭据」列靠它区分「这台没配凭据」和「用例本身在跳过」。
NODE_ENV_EVIDENCE_NAME = "node_env.json"

# 允许从任务文件下发的变量白名单。只有这些会被接受，其余一律忽略 ——
# 防止有人（或控制机的 bug）往共享盘的任务文件里塞密码。
TASK_ENV_ALLOWLIST = frozenset({
    "HALL_ALLOW_INSTALL",
    "HALL_ALLOW_PACKAGE_DOWNLOAD",
    "HALL_NODE_ID",
    "HALL_FARM_ROOT",
    "HALL_PACKAGE_SHARE",
    "HALL_PACKAGE_CACHE",
    "HALL_INSTALLER_DIR",
})

# 只在节点本地凭据文件里认的变量（敏感项）。任务文件里出现也不接受。
NODE_ENV_SECRET_KEYS = frozenset({
    "HALL_TEST_USER",
    "HALL_TEST_PASSWORD",
    "HALL_TEST_NEW_PASSWORD",
    "HALL_MS_USER",
    "HALL_SHARE_USER",
    "HALL_SHARE_PASSWORD",
})


def parse_env_text(text: str) -> dict[str, str]:
    """解析 KEY=VALUE 文本。忽略注释与空行，值不做引号处理，首尾空白去掉。

    出现重复键时**后者覆盖前者**（跟 shell / dotenv 一致，便于文件末尾临时覆盖）。
    """
    out: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        out[key] = value.strip()
    return out


def load_node_env(env_path: Path) -> dict[str, str]:
    """读节点本地凭据文件。文件不存在或读不动 → 空字典（不是错误，可能这台机器只需只读套件）。"""
    try:
        return parse_env_text(env_path.read_text(encoding="utf-8"))
    except OSError:
        return {}


def sanitize_task_env(raw: object) -> tuple[dict[str, str], list[str]]:
    """从任务文件的 env 里挑出白名单内的项。

    返回 (接受的键值, 被拒的键名)。被拒的只是忽略，不报错、不中断执行 ——
    任务文件是控制机写的，节点不该因为多了一个不认识的键就罢工。
    """
    if not isinstance(raw, dict):
        return {}, []
    accepted: dict[str, str] = {}
    rejected: list[str] = []
    for key, value in raw.items():
        name = str(key).strip()
        if not name:
            continue
        if name in TASK_ENV_ALLOWLIST and name not in NODE_ENV_SECRET_KEYS:
            accepted[name] = str(value)
        else:
            rejected.append(name)
    return accepted, rejected


def build_suite_env(
    *,
    task_env: dict[str, str] | None = None,
    node_env_path: Path | None = None,
    base: dict[str, str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """合成跑套件用的环境。返回 (环境字典, 说明行列表)。

    优先级：节点进程已有 env（最高）> 任务文件 env > 节点本地凭据文件（最低）。

    **节点进程已有的环境变量优先，是为了不覆盖人在那台机器上显式设的值** ——
    比如调试时想用另一个账号复现，`set HALL_TEST_USER=...` 之后跑批就该用那个，
    不该被文件里的值盖掉。
    """
    env = dict(base if base is not None else os.environ)
    notes: list[str] = []

    accepted, rejected = sanitize_task_env(task_env or {})
    if rejected:
        notes.append(f"任务文件的 env 里忽略不白名单项：{', '.join(sorted(rejected))}")

    file_env: dict[str, str] = {}
    if node_env_path is not None:
        file_env = load_node_env(node_env_path)
        if file_env:
            notes.append(f"节点凭据文件已加载：{node_env_path.name}（{len(file_env)} 项）")
        elif node_env_path.is_file():
            notes.append(f"节点凭据文件存在但没有有效行：{node_env_path.name}")

    # 低优先级先写，高优先级后写（后写覆盖先写）
    for key, value in file_env.items():
        env[key] = value
    for key, value in accepted.items():
        env[key] = value
    # 已在进程环境里的值最后写回，保证最高优先级
    for key in set(file_env) | set(accepted):
        existing = os.environ.get(key)
        if existing:
            env[key] = existing

    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env, notes


def credential_status(env: dict[str, str]) -> dict[str, bool]:
    """本轮凭据齐不齐，用于日志与回执 —— 让报告能区分"没配凭据"和"用例真跳过"。"""
    return {
        "password_login": bool(env.get("HALL_TEST_USER") and env.get("HALL_TEST_PASSWORD")),
        "microsoft_sso": bool(env.get("HALL_MS_USER")),
        "change_password": bool(env.get("HALL_TEST_NEW_PASSWORD")),
        "share_creds": bool(env.get("HALL_SHARE_USER") and env.get("HALL_SHARE_PASSWORD")),
    }
