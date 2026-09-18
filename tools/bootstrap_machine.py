"""每台机器的一次性环境铺设（bootstrap）。

用途：把一台裸的测试机变成「能跑本套 UI 自动化」的节点机。**幂等，可重复执行。**

## 核心原则：先检测、缺什么才装什么

20 台机器环境**不一样**，有的可能已经有 Python、有大厅、有 7-Zip。
所以每一步都先问「这个已经有了吗」，有了就跳过，没有才动手。
**重跑本脚本不应该产生任何多余的安装动作，也不该碰网络。**

各步骤的检测口径：

| 步骤 | 检测什么 | 已有则 |
| --- | --- | --- |
| 1 环境自检 | OS / 交互式会话 / Python 位数 / 7-Zip / OCR 语言包 | 只报告，不改 |
| 2 虚拟环境与依赖 | venv 在否；`requirements.txt` 哈希 + 关键包能否 import | 跳过 pip |
| 3 夹具校验 | 逐个查 SHA256 | 只报告 |
| 4 **7-Zip** | `C:/Program Files/7-Zip/7z.exe` 在否 | 跳过（不重复安装） |
| 5 生成本机配置 | 逐字段 setdefault，只覆盖机器绑定项 | 保留原有 |
| 6 生成节点凭据模板 | `farm_node.env` 在否 | 只报键名，绝不覆盖 |
| 7 连共享盘 | 先探测可达性 | 已达则不连 |
| 8 农场目录 | `tasks/ done/ results/ logs/` 齐否 | 齐则跳过 |
| 9 准备安装包 | 本地 → 缓存 → 共享盘 → 下载 | 命中即止 |
| 10 提权计划任务 | 查任务是否已存在且指向本仓库脚本 | 跳过（覆盖会重置触发时间） |
| 11 自检 | 跑 tests/unit | 可 `--skip-selftest` |

第 4 步为什么是**装**而不是**只检测**：`hall_auto/security.py` 的 P2 安全验证要用
`7z x` 解包待检安装包。这个钉住包本来就在 `installer_dir/fixtures/7z2602-x64.exe`
（`tools/reset_fixture.py` 早就用它静默装了），没理由再让人去官网手动下一趟。
装的是**钉住的 26.02 而非最新版** —— 它同时是 P1-A「更新夹具」的起点
（大厅目录里的 7-Zip 比它新，更新列表里才永远有它可更新）。
装不上**不算铺设失败**：只影响 P2，且 P2 还要内部安全工具才能跑。

第 2 步的标记文件是 `.venv/.deps_ok`：装成功后写入 requirements.txt 的 SHA256，
下次哈希一致且关键包都能 import 就跳过。标记只用于"快速跳过"，
**不能单独作为依据**——标记可能被手工删或 venv 被清理，
所以标记命中后仍会实际 import 校验一遍。

注：**不修改显示缩放**。脚本走 Per-Monitor Aware 物理像素坐标，非 100% 不影响正确性
（本机真实 150% 下 P0/P1/P2 全部套件实测跑通），缩放只作为环境事实记录进画像。

用法（管理员 PowerShell 或普通 PowerShell 均可，第 7 步需要管理员）：
    $env:HALL_SHARE_USER="hallshare"; $env:HALL_SHARE_PASSWORD="<密码>"
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\bootstrap_machine.py [选项]

选项：
    --installer-dir PATH   华硕大厅安装包目录（默认沿用现有 config.local.yaml 或内置默认）
    --node-id ID           本机节点标识（默认主机名）
    --skip-venv            不检查虚拟环境与依赖
    --skip-seven-zip       不检查/自动安装 7-Zip
    --skip-schtask         不检查/建计划任务
    --skip-selftest        不跑 tests/unit 自检
    --no-download          不下载安装包，只用本地已有的
    --skip-share           不尝试连接共享盘

产物：
    reports/bootstrap/bootstrap_<node>_<时间>.json   本次铺设结果（含环境画像）
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from shutil import which

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from hall_auto.dpi import (  # noqa: E402
    is_interactive_session,
    machine_profile,
    node_id,
)

REQUIRED_FILES = {
    # 相对 installer_dir 的相对路径 -> 说明
    "7z2602-x64.exe": "7-Zip 更新夹具包（fixtures/ 下，路径见下）",
}

# 7-Zip 安装位置（`hall_auto/config.py` 的 security.seven_zip 默认值，两处必须一致）。
SEVEN_ZIP_EXE = Path("C:/Program Files/7-Zip/7z.exe")

# 钉住的 7-Zip 26.02：官方包固定摘要（与 step_fixtures / config.yaml 钉的是同一个包）。
SEVEN_ZIP_FIXTURE_SHA256 = "6745fa76dc2ea031596d8678f6f6b99c3c1b435b4164a63485adbbc7b8d82ef0"
SEVEN_ZIP_URL = "https://www.7-zip.org/a/7z2602-x64.exe"

# 节点本地凭据文件的名字（仓库根下，已在 .gitignore）。
# 与 hall_auto/env_pack.NODE_ENV_FILENAME 是同一条约定，这里 import 过来免得抄错。
from hall_auto.env_pack import NODE_ENV_FILENAME  # noqa: E402

# bootstrap 时提示"哪些凭据还没配"用的建议清单（**只列键名，不涉及值**）。
SUGGESTED_NODE_ENV = (
    "HALL_TEST_USER",
    "HALL_TEST_PASSWORD",
    "HALL_MS_USER",
    "HALL_SHARE_USER",
    "HALL_SHARE_PASSWORD",
)

SELFTEST_MARKER = "unit"

# 依赖装好后写的标记文件，内容 = requirements.txt 的 SHA256。
# 下次 bootstrap 先比哈希，一致就直接跳过 pip（秒级）；不一致才走 pip + 校验。
DEPS_MARKER_NAME = ".deps_ok"

# 依赖齐不齐的快速校验项：import 名 -> 分发名（报错信息用）
DEPS_PROBE = (
    ("pytest", "pytest"),
    ("pywinauto", "pywinauto"),
    ("win32gui", "pywin32"),
    ("PIL", "Pillow"),
    ("yaml", "PyYAML"),
)


def _req_digest(req: Path) -> str:
    """requirements.txt 的内容摘要；文件不存在返回空串。"""
    try:
        return hashlib.sha256(req.read_bytes()).hexdigest()
    except OSError:
        return ""


def _deps_probe(py: Path) -> tuple[bool, str]:
    """在不装任何东西的前提下，确认关键依赖都能 import。

    返回 (是否齐全, 说明)。只起一个 Python 进程，约 1~2 秒。
    为什么不能只看标记文件：标记可能被手工删、venv 可能被人为清理过，
    标记只说明"上次装成功过"，不代表现在还在。
    """
    code = ";".join(f"import {mod}" for mod, _ in DEPS_PROBE)
    try:
        proc = subprocess.run(
            [str(py), "-c", code],
            capture_output=True, text=True, check=False, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"校验进程起不来：{exc}"
    if proc.returncode == 0:
        return True, "关键依赖均可 import"
    err = (proc.stderr or proc.stdout or "").strip().splitlines()
    tail = err[-1] if err else f"rc={proc.returncode}"
    missing = [dist for mod, dist in DEPS_PROBE if f"No module named '{mod}'" in (proc.stderr or "")]
    if missing:
        return False, f"缺 {', '.join(missing)}"
    return False, tail[:200]


class Step:
    """一步铺设结果。ok=False 不中断，最后统一汇总（尽量把问题一次报全）。"""

    def __init__(self, name: str):
        self.name = name
        self.ok = True
        self.detail: list[str] = []

    def log(self, text: str) -> None:
        self.detail.append(text)

    def fail(self, text: str) -> None:
        self.ok = False
        self.detail.append(f"!! {text}")

    def to_dict(self) -> dict:
        return {"step": self.name, "ok": self.ok, "detail": self.detail}


def say(text: str) -> None:
    print(text, flush=True)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_existing_local_config() -> dict:
    """读现有 config.local.yaml（有新机器是直接从别人那拷来的，可复用其 installer_dir）。"""
    try:
        import yaml

        path = REPO_ROOT / "config.local.yaml"
        if path.is_file():
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


# ---------------- 各步骤 ----------------


def step_env_check(node: str) -> Step:
    st = Step("环境自检")
    profile = machine_profile()
    st.log(f"节点 {node} / {profile['os']} / Python {profile['python']}({profile['python_bits']}位)")
    st.log(
        f"显示 {profile['screen']} 缩放 {profile['scale_percent']}% "
        f"显示器 {profile['monitors']} 个 DPI感知 {profile['dpi_awareness']}"
    )
    if profile["python_bits"] != 64:
        st.fail("Python 不是 64 位，pywinauto/comtypes 访问 64 位大厅进程会失败")
    if not is_interactive_session():
        st.fail("当前非交互式会话（锁屏/息屏/RDP 断开），跑批会批量失败；请保持本地解锁桌面")
    if not is_admin():
        st.log("当前非管理员：第 6 步建计划任务会失败，P0 安装层与 P1-A 真装真卸也跑不了")
    seven_zip = SEVEN_ZIP_EXE
    if seven_zip.is_file():
        st.log(f"7-Zip 就位：{seven_zip}")
    else:
        # 这里**不判 fail**：下一步「7-Zip」会尝试自动装。装不上也只影响 P2 套件，
        # 不该把整台机器的铺设判死（P2 还要内部安全工具，多数机器本来就不跑）。
        st.log(f"未装 7-Zip（{seven_zip}）—— 下一步会尝试自动安装")
    # 中文 OCR 语言包（tools/ocr_text.ps1 兜底路用到）
    try:
        proc = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]"
                "::AvailableRecognizerLanguages | ForEach-Object { $_.LanguageTag }",
            ],
            capture_output=True, text=True, timeout=60, check=False,
        )
        tags = [t.strip() for t in (proc.stdout or "").splitlines() if t.strip()]
        if any(t.lower().startswith("zh") for t in tags):
            st.log(f"OCR 语言包就位：{', '.join(tags)}")
        else:
            st.log(f"OCR 只有 {tags or '（无）'}，缺 zh-Hans-CN —— 仅当用到 OCR 兜底路才影响")
    except Exception as exc:
        st.log(f"OCR 语言包检测跳过：{exc}")
    return st


def step_venv(skip: bool) -> Step:
    """虚拟环境与依赖：**先检测、缺什么才装什么**。

    跳过条件（任一条满足即跳过 pip）：
      1. `--skip-venv` 且 venv 已就位 —— 显式要求跳过
      2. venv 在 + 标记文件哈希与 requirements.txt 一致 + 关键依赖都能 import
    第 2 条是默认路径：本机铺过一次后，重跑 bootstrap 不该再碰网络。
    """
    st = Step("虚拟环境与依赖")
    venv = REPO_ROOT / ".venv"
    py = venv / "Scripts" / "python.exe"
    req = REPO_ROOT / "requirements.txt"
    marker = venv / DEPS_MARKER_NAME

    if skip and py.is_file():
        st.log(f"按 --skip-venv 跳过（沿用 {py}）")
        return st

    # ---- 检测 1：venv 是否存在 ----
    if not py.is_file():
        base = sys.executable
        st.log(f"未检测到虚拟环境，创建：{base} -m venv .venv")
        proc = subprocess.run([base, "-m", "venv", str(venv)], capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            st.fail(f"建 venv 失败 rc={proc.returncode}: {(proc.stderr or '')[:300]}")
            return st
    else:
        st.log(f"虚拟环境已在：{py}")

    # ---- 检测 2：依赖是否齐全（先比哈希，再实际 import 校验）----
    want = _req_digest(req)
    if py.is_file() and want:
        recorded = ""
        try:
            recorded = marker.read_text(encoding="utf-8").strip()
        except OSError:
            recorded = ""
        if recorded == want:
            ok, why = _deps_probe(py)
            if ok:
                st.log(f"依赖已就绪（requirements.txt 未变 + {why}），跳过安装")
                _log_installed(py, st)
                return st
            st.log(f"标记说装过，但实测不齐（{why}）——重装")
        elif recorded:
            st.log("requirements.txt 有变动，重装依赖")
        else:
            st.log("无依赖标记（首次铺设或标记被清），按需安装")

    # ---- 需要装：pip 本身幂等，已装的包会跳过 ----
    st.log(f"装依赖：{req.name}")
    proc = subprocess.run(
        [str(py), "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", str(req)],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        st.fail(f"pip install 失败 rc={proc.returncode}: {(proc.stderr or '')[-400:]}")
        return st

    # ---- 装完复核，通过了才写标记（避免"装失败也被记为就绪"）----
    ok, why = _deps_probe(py)
    if not ok:
        st.fail(f"pip 已跑完但依赖仍不齐：{why}")
        return st
    try:
        marker.write_text(want, encoding="utf-8")
        st.log(f"依赖安装完成，已记录标记 {marker.name}（{why}）")
    except OSError as exc:
        st.log(f"依赖装好了，但写标记失败（下次会重装）：{exc}")
    _log_installed(py, st)
    return st


def _log_installed(py: Path, st: Step) -> None:
    """把关键包版本打进日志，便于多机汇总时比对环境是否一致。"""
    proc = subprocess.run([str(py), "-m", "pip", "list"], capture_output=True, text=True, check=False)
    for line in (proc.stdout or "").splitlines():
        if line.lower().startswith(("pytest ", "pywinauto ", "pywin32 ", "pillow ", "pyyaml ")):
            st.log(f"  {line.strip()}")


def step_fixtures(installer_dir: Path) -> Step:
    st = Step("夹具校验")
    st.log(f"installer_dir = {installer_dir}")
    if not installer_dir.is_dir():
        # 多机跑批时机器上没有人拷包，目录不存在不是错误 —— 下一步会自己下载
        st.log(f"安装包目录尚未创建：{installer_dir}（后续步骤会自动下载）")
        return st
    setups = sorted(installer_dir.glob("myappstore_*_Setup.exe"))
    if setups:
        st.log(f"大厅安装包 {len(setups)} 个：{', '.join(p.name for p in setups)}")
    else:
        st.log(f"{installer_dir} 下暂无大厅安装包 —— 后续步骤会按配置自动下载")
    seven_zip = installer_dir / "fixtures" / "7z2602-x64.exe"
    if seven_zip.is_file():
        digest = sha256_of(seven_zip)
        if digest == SEVEN_ZIP_FIXTURE_SHA256:
            st.log(f"7-Zip 更新夹具包 OK（sha256 {digest[:16]}…）")
        else:
            st.fail(
                f"7-Zip 夹具包 sha256 不符：期望 {SEVEN_ZIP_FIXTURE_SHA256[:16]}… "
                f"实际 {digest[:16]}…（本地包被换过）"
            )
    else:
        st.log(f"暂无 7-Zip 更新夹具包：{seven_zip}（下一步会按需从 {SEVEN_ZIP_URL} 取）")
    tools_dir = installer_dir / "fixtures" / "security_tools"
    wanted = ["CheckAppV.exe", "SignCheck_v2.ps1"]
    missing = [n for n in wanted if not (tools_dir / n).is_file()]
    if missing:
        st.log(f"内部安全工具缺 {missing}（{tools_dir}）—— 仅 P2 安全验证受影响，会 skip")
    else:
        st.log(f"内部安全工具就位：{tools_dir}")
    return st


def step_seven_zip(installer_dir: Path, allow_download: bool, skip: bool = False) -> Step:
    """确保 7-Zip 可用：缺了就自动装（本地钉住包优先，其次官方源）。

    为什么这一步是"装"而不是"只检测、缺了让人去下"：
    `hall_auto/security.py::extract_package` 用 `7z x` 解包待检安装包，
    而那个钉住包本来就在 `installer_dir/fixtures/7z2602-x64.exe`
    （`tools/reset_fixture.py` 早就用它静默装了）。既然包在手边，就不该让人再跑一趟官网。

    装的是**钉住的 26.02，不是最新版**：它同时是 P1-A「更新夹具」的起点
    （大厅目录里的 7-Zip 比它新，更新列表里才永远有它可更新）。
    装最新版会让 `test_update_fixture_via_hall` 的起点断言失配、用例转 skip。

    装不上**不算铺设失败**：只影响 P2 套件，而 P2 还要内部安全工具才能跑。
    """
    st = Step("7-Zip")
    if skip:
        st.log("按 --skip-seven-zip 跳过")
        return st
    if SEVEN_ZIP_EXE.is_file():
        st.log(f"已就位：{SEVEN_ZIP_EXE}（未安装任何东西）")
        return st

    if not is_admin():
        # 装到 C:\\Program Files 需要管理员；非提权直接调安装器会弹 UAC 把脚本卡死。
        st.log(
            f"未装 7-Zip 且当前非管理员 —— 装到 {SEVEN_ZIP_EXE.parent} 需要管理员，跳过自动安装。"
            "以管理员重跑本脚本即可自动装；仅 P2 安全验证受影响（会 skip）"
        )
        return st

    installer = installer_dir / "fixtures" / "7z2602-x64.exe"
    if not installer.is_file():
        if not allow_download:
            st.log(f"钉住包不在本地（{installer}），--no-download 下不下载；仅 P2 安全验证受影响（会 skip）")
            return st
        try:
            installer.parent.mkdir(parents=True, exist_ok=True)
            # 复用 fetch 的下载：带 .part 原子改名 + 递增重试，比这里重写一份稳。
            from hall_auto.fetch import _download

            _download(SEVEN_ZIP_URL, installer, timeout_sec=60, retries=3)
            st.log(f"已从官方源取到钉住包：{SEVEN_ZIP_URL}")
        except Exception as exc:
            st.log(f"钉住包取不到（{exc}）；仅 P2 安全验证受影响（会 skip），其余套件不受影响")
            return st

    digest = sha256_of(installer)
    if digest != SEVEN_ZIP_FIXTURE_SHA256:
        st.log(
            f"钉住包 sha256 不符（期望 {SEVEN_ZIP_FIXTURE_SHA256[:16]}… 实际 {digest[:16]}…），"
            "不用它安装；仅 P2 安全验证受影响（会 skip）"
        )
        return st

    st.log(f"未装 7-Zip，静默安装钉住版本 26.02：{installer}")
    try:
        proc = subprocess.run([str(installer), "/S"], capture_output=True, check=False, timeout=180)
    except subprocess.SubprocessError as exc:
        st.log(f"安装器起不来（{exc}）；仅 P2 安全验证受影响（会 skip）")
        return st

    deadline = time.time() + 120
    while time.time() < deadline:
        if SEVEN_ZIP_EXE.is_file():
            st.log(f"安装完成：{SEVEN_ZIP_EXE}")
            return st
        time.sleep(2)
    st.log(
        f"装完 120s 未见 {SEVEN_ZIP_EXE}（安装器 rc={proc.returncode}）；"
        "仅 P2 安全验证受影响（会 skip），其余套件不受影响"
    )
    return st


def step_share_login(skip: bool) -> Step:
    """连上共享盘（如需凭据）。

    每台测试机都要连一次共享盘，否则 `fetch.py` 走共享盘那一步会因凭据缺失而失败。
    用 `net use ... /persistent:yes` 建立持久连接，重启后自动重连。

    凭据**只走环境变量**（红线：不落盘、不进 git）：
        HALL_SHARE_USER      共享盘账号（如 hallshare）
        HALL_SHARE_PASSWORD  共享盘密码

    未配共享盘或未给凭据时跳过 —— 这不是错误，可能这台机器有本地包、或走下载。
    """
    st = Step("共享盘连接")
    if skip:
        st.log("按 --skip-share 跳过")
        return st

    try:
        from hall_auto.config import load_config
        from hall_auto.fetch import share_dirs, share_reachable

        cfg = load_config()
    except Exception as exc:
        st.log(f"读配置跳过：{exc}")
        return st

    dirs = share_dirs(cfg)
    if not dirs:
        st.log("未配置共享盘（package_share.dirs 为空），跳过")
        return st

    user = (os.environ.get("HALL_SHARE_USER") or "").strip()
    password = os.environ.get("HALL_SHARE_PASSWORD") or ""

    for d in dirs:
        ok, why = share_reachable(d)
        if ok:
            st.log(f"{d}: 已可达（无需再连）")
            continue
        if not user or not password:
            st.log(f"{d}: 当前不可达（{why}），且未设 HALL_SHARE_USER/PASSWORD，无法自动连接")
            continue
        # UNC 形式：\\host\share，取前两段作为 net use 的目标
        parts = [p for p in str(d).replace("/", "\\").split("\\") if p]
        if len(parts) < 2:
            st.log(f"{d}: 路径格式不像 UNC（\\\\host\\share），跳过")
            continue
        target = f"\\\\{parts[0]}\\{parts[1]}"
        cmd = ["net", "use", target, password, f"/user:{user}", "/persistent:yes"]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            st.log(f"{target}: 连接失败 rc={proc.returncode} {(proc.stdout or proc.stderr or '').strip()[:160]}")
            continue
        ok2, why2 = share_reachable(d)
        st.log(f"{target}: 已连接（{'可达' if ok2 else f'仍不可达：{why2}'}）")
    return st


def step_fetch_packages(allow_download: bool) -> Step:
    """把配置声明的安装包备齐：本地 → 共享盘 → 下载。

    组长 2026-09-18 明确要求「脚本在测试机自动下载对应大厅」，这一步就是落地。
    共享盘（`config.yaml` 的 `package_share.dirs`）优先于下载：局域网快且版本可控。
    任一环节失败不阻塞铺设（可能只是网络不通），但会记进结果让人看见。
    """
    st = Step("安装包准备")
    try:
        from hall_auto.config import load_config
        from hall_auto.fetch import configured_filenames, ensure_package, share_dirs, share_reachable

        cfg = load_config()
    except Exception as exc:
        st.fail(f"读配置失败，无法准备安装包：{exc}")
        return st

    shares = share_dirs(cfg)
    if shares:
        for d in shares:
            ok, why = share_reachable(d)
            st.log(f"共享盘 {d}: {'可达' if ok else f'不可达（{why}）'}")
    else:
        st.log("未配置共享盘（package_share.dirs 为空），将直接用下载")

    names = configured_filenames(cfg)
    if not names:
        st.log("配置里没有声明安装包（baseline_setup / latest_setup 都为空）")
        return st

    for name in names:
        try:
            res = ensure_package(cfg, name, allow_download=allow_download)
        except Exception as exc:
            st.fail(f"{name} 获取失败：{exc}")
            continue
        tag = {"local": "本地", "cache": "缓存", "share": "共享盘", "download": "已下载"}.get(
            res.source, res.source
        )
        size_mb = f"{res.bytes / 1048576:.1f}MB" if res.bytes else "?"
        st.log(f"{name}: {tag} {size_mb} sha256 {res.sha256[:16]}…")
    return st


def step_write_local_config(installer_dir: Path, node: str) -> Step:
    st = Step("生成本机配置 config.local.yaml")
    path = REPO_ROOT / "config.local.yaml"
    existing = _load_existing_local_config()
    # 只覆盖与机器绑定、必须逐台不同的字段；其余（超时、夹具名、update_fixture 摘要）保持原样
    existing["installer_dir"] = installer_dir.as_posix()
    existing.setdefault("latest_setup", "myappstore_1.6.11.4S_Setup.exe")
    existing.setdefault("timeouts", {"launch_sec": 120, "ready_sec": 150, "install_sec": 300})
    fixture_apps = existing.setdefault("fixture_apps", {})
    fixture_apps.setdefault("install", "网易云音乐")
    fixture_apps.setdefault("update", "7-Zip（64位）")
    fixture_apps.setdefault("uninstall", "网易云音乐")
    fixture_apps["sync"] = ""
    security = existing.setdefault("security", {})
    security["tools_dir"] = (installer_dir / "fixtures" / "security_tools").as_posix()
    existing.setdefault("node_id", node)
    try:
        import yaml

        header = (
            "# 本机配置（每台机器不同，已在 .gitignore）。\n"
            f"# 由 tools/bootstrap_machine.py 于 {datetime.now().isoformat(timespec='seconds')} 生成。\n"
            "# 重新铺设直接重跑该脚本，不要手工改这里除 installer_dir / node_id 之外的字段含义。\n"
        )
        path.write_text(header + yaml.safe_dump(existing, allow_unicode=True, sort_keys=False), encoding="utf-8")
        st.log(f"已写入 {path}（node_id={existing.get('node_id')}）")
    except Exception as exc:
        st.fail(f"写 config.local.yaml 失败：{exc}")
    return st


def _task_action(task: str) -> str:
    """读计划任务的动作命令行；不存在或读不到返回空串。

    `schtasks /Query /XML` 比 `/FO LIST` 好解析，且能拿到完整命令行。
    """
    proc = subprocess.run(
        f'schtasks /Query /TN {task} /XML',
        shell=True, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout or ""


def step_schtask(skip: bool) -> Step:
    """提权计划任务：**已存在且指向同一脚本就跳过**，不无脑重建。

    重建的代价不是"多花几秒"，是**触发时间被重置**——如果正好在跑批，
    重建会把任务状态抹掉。所以默认只检测，不覆盖。
    """
    st = Step("提权计划任务")
    if skip:
        st.log("按 --skip-schtask 跳过")
        return st
    if not is_admin():
        st.fail("非管理员无法建计划任务；以管理员重跑本脚本，或手工建（见 项目知识库/运行手册.md）")
        return st
    task = "HallAutoP1"
    script = REPO_ROOT / "run_p1_apps.ps1"

    # ---- 检测：任务已存在且动作指向本仓库的脚本 ----
    xml = _task_action(task)
    if xml:
        # 只认「这个任务的 Command/Arguments 里出现了本仓库的脚本路径」
        if script.name in xml and str(REPO_ROOT) in xml.replace("&amp;", "&"):
            st.log(f"任务 {task} 已存在且指向本仓库脚本，跳过（覆盖会重置触发时间）")
            st.log(f"触发：MSYS_NO_PATHCONV=1 schtasks /Run /TN {task}")
            st.log(f"要强制重建：先 schtasks /Delete /TN {task} /F 再重跑本脚本")
            return st
        st.log(f"任务 {task} 已存在但指向别的脚本，将覆盖重建")

    cmd = (
        f'schtasks /Create /TN {task} /SC ONCE /ST 00:00 /RL HIGHEST /F '
        f'/TR "powershell -NoProfile -ExecutionPolicy Bypass -File {script}"'
    )
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        st.fail(f"建任务失败 rc={proc.returncode}: {(proc.stdout or proc.stderr or '')[:300]}")
    else:
        st.log(f"任务 {task} 已建（/RL HIGHEST，动作跑 {script.name}）")
        st.log(f"触发：MSYS_NO_PATHCONV=1 schtasks /Run /TN {task}")
    return st


def step_selftest(skip: bool) -> Step:
    st = Step("自检 tests/unit")
    if skip:
        st.log("按 --skip-selftest 跳过")
        return st
    py = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    if not py.is_file():
        st.fail("没有 .venv，无法自检")
        return st
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [str(py), "-X", "utf8", "-m", "pytest", "tests/unit", "-m", SELFTEST_MARKER, "-q", "--skip-env-check"],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, check=False,
    )
    tail = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()][-3:]
    for ln in tail:
        st.log(ln)
    if proc.returncode != 0:
        st.fail(f"自检未通过 rc={proc.returncode}")
    return st


# ---------------- 主流程 ----------------


def step_node_env_template() -> Step:
    """准备节点本地凭据文件模板 `tools/farm_node.env`（已 gitignore）。

    **不生成任何真实凭据**，只在文件不存在时放一份带注释的模板并提示要填什么。
    密码只走这里（机器本地），不落共享盘、不进任务文件。

    为什么不自动从环境变量导出到文件：那会把"只在环境里"的密码落到磁盘上，
    与红线相悖。要落盘由人显式决定。
    """
    st = Step("节点凭据文件")
    path = REPO_ROOT / NODE_ENV_FILENAME
    if path.is_file():
        # 已存在：只报告有哪些项，**绝不回显值**
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            st.fail(f"读 {path.name} 失败：{exc}")
            return st
        from hall_auto.env_pack import parse_env_text

        keys = sorted(parse_env_text(text))
        st.log(f"{path.name} 已存在，含 {len(keys)} 项：{', '.join(keys) or '（空）'}")
        missing = [k for k in SUGGESTED_NODE_ENV if k not in keys]
        if missing:
            st.log(f"未配置（相关套件会 skip）：{', '.join(missing)}")
        return st

    template = "\n".join([
        "# 节点本地凭据（每台机器自己一份，已在 .gitignore，绝不进 git、不进共享盘）。",
        "# 只填这台机器用得上的；没配的对应套件会自动 skip，不影响其他套件。",
        "# 格式：KEY=VALUE（值不用加引号）。",
        "",
        "# --- 登录套件（P1-B）---",
        "# 密码登录用例",
        "HALL_TEST_USER=",
        "HALL_TEST_PASSWORD=",
        "# 微软 SSO 免密用例",
        "HALL_MS_USER=",
        "# 改密码往返用例（只在 -m manual 人在环时用）",
        "HALL_TEST_NEW_PASSWORD=",
        "",
        "# --- 共享盘取包（取不到包时才需要；也可用 net use /persistent:yes 代替）---",
        "HALL_SHARE_USER=",
        "HALL_SHARE_PASSWORD=",
        "",
    ])
    try:
        path.write_text(template, encoding="utf-8")
    except OSError as exc:
        st.fail(f"写 {path.name} 失败：{exc}")
        return st
    st.log(f"已生成模板 {path}（全空，填入本机凭据后生效）")
    st.log("提示：填完不必重跑本脚本，farm_agent 每次取任务时会重新读该文件")
    return st


def step_farm_root(skip: bool) -> Step:
    """检查/创建农场目录结构（tasks / done / results / logs）。

    `HALL_FARM_ROOT` 指向共享盘上的农场目录，控制机和所有节点共用同一个。
    目录不存在时 `farm_agent` 的 `ensure_dirs` 也会建，但**首次跑建议在这里建好**：
    免得节点以自己受限的权限往共享盘根下写，失败信息还不直观。
    """
    st = Step("农场目录")
    if skip:
        st.log("按 --skip-farm 跳过")
        return st
    raw = (os.environ.get("HALL_FARM_ROOT") or "").strip()
    if not raw:
        st.log(
            "未设 HALL_FARM_ROOT —— 单机跑不设也行（用 farm_agent --local 直接跑套件）；"
            "多机跑批必须设，指向共享盘上的农场目录，如 \\\\192.168.0.4\\hall-farm"
        )
        return st

    root = Path(raw)
    created: list[str] = []
    failed: list[str] = []
    for sub in ("tasks", "done", "results", "logs"):
        target = root / sub
        if target.is_dir():
            continue
        try:
            target.mkdir(parents=True, exist_ok=True)
            created.append(sub)
        except OSError as exc:
            failed.append(f"{sub}（{exc}）")
    if failed:
        st.fail(f"农场子目录建不出来：{'; '.join(failed)}")
        st.log(f"农场根：{root}（节点写不进去多半是共享盘权限/凭据没配）")
        return st
    st.log(f"农场根 {root}：{'新建 ' + ', '.join(created) if created else '四个子目录齐全'}")
    return st


def main() -> int:
    parser = argparse.ArgumentParser(description="测试机一次性环境铺设")
    parser.add_argument("--installer-dir", default="")
    parser.add_argument("--node-id", default="")
    parser.add_argument("--skip-venv", action="store_true")
    parser.add_argument("--skip-seven-zip", action="store_true", help="不检查/自动安装 7-Zip")
    parser.add_argument("--skip-schtask", action="store_true")
    parser.add_argument("--skip-selftest", action="store_true")
    parser.add_argument("--skip-share", action="store_true", help="不尝试连接共享盘")
    parser.add_argument("--skip-farm", action="store_true", help="不检查/创建农场目录结构")
    parser.add_argument("--no-download", action="store_true", help="不下载安装包，只用本地已有的")
    args = parser.parse_args()

    node = args.node_id.strip() or node_id()
    existing = _load_existing_local_config()
    installer_dir = Path(
        args.installer_dir
        or existing.get("installer_dir")
        or "C:/Users/Public/Desktop/Test/华硕大厅"
    )

    say(f"=== 华硕大厅自动化 · 机器铺设 [{node}] ===")
    say(f"仓库: {REPO_ROOT}")
    say(f"管理员: {is_admin()}")
    say("")

    steps: list[Step] = []

    def run(st: Step) -> None:
        steps.append(st)
        say(f"[{'OK' if st.ok else 'FAIL'}] {st.name}")
        for line in st.detail:
            say(f"    {line}")
        say("")

    run(step_env_check(node))
    run(step_venv(args.skip_venv))
    run(step_fixtures(installer_dir))
    run(step_seven_zip(installer_dir, allow_download=not args.no_download, skip=args.skip_seven_zip))
    run(step_write_local_config(installer_dir, node))
    run(step_node_env_template())
    run(step_share_login(args.skip_share))
    run(step_farm_root(args.skip_farm))
    run(step_fetch_packages(allow_download=not args.no_download))
    run(step_schtask(args.skip_schtask))
    run(step_selftest(args.skip_selftest))

    payload = {
        "node": node,
        "repo": str(REPO_ROOT),
        "admin": is_admin(),
        "profile": machine_profile(),
        "installer_dir": str(installer_dir),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "steps": [s.to_dict() for s in steps],
        "ok": all(s.ok for s in steps),
    }
    out_dir = REPO_ROOT / "reports" / "bootstrap"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"bootstrap_{node}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    failed = [s.name for s in steps if not s.ok]
    say("=" * 46)
    if failed:
        say(f"结果：{len(failed)} 步有问题 -> {'; '.join(failed)}")
    else:
        say("结果：全部通过，本机已可跑批")
    say(f"明细：{out}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
