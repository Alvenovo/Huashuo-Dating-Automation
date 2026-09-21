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
| 8 **安全工具** | `fixtures/security_tools/` 关键文件在否 | 一个字节都不拷 |
| 9 农场目录 | `tasks/ done/ results/ logs/` 齐否 | 齐则跳过 |
| 10 准备安装包 | 本地 → 缓存 → 共享盘 → 下载 | 命中即止 |
| 11 提权计划任务 | 查任务是否已存在且指向本仓库脚本 | 跳过（覆盖会重置触发时间） |
| 12 自检 | 跑 tests/unit | 可 `--skip-selftest` |

第 4 步为什么是**装**而不是**只检测**：`hall_auto/security.py` 的 P2 安全验证要用
`7z x` 解包待检安装包。这个钉住包本来就在 `installer_dir/fixtures/7z2602-x64.exe`
（`tools/reset_fixture.py` 早就用它静默装了），没理由再让人去官网手动下一趟。
装的是**钉住的 26.02 而非最新版** —— 它同时是 P1-A「更新夹具」的起点
（大厅目录里的 7-Zip 比它新，更新列表里才永远有它可更新）。
装不上**不算铺设失败**：只影响 P2，且 P2 还要内部安全工具才能跑。

第 8 步为什么从共享盘备而不是让人拷：内部安全工具按红线**不进公开仓库**，
于是每台新机器只能人肉拷一份，`security` 套件必然 skip。但共享盘不是 git 仓库 ——
包源机上放一次，所有节点自动取到，「N 台拷 N 次」压成「1 台拷 1 次」。
**必须在第 7 步之后**：没认证过共享盘就取不到。

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
    --skip-security-tools  不从共享盘准备 P2 内部安全工具
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
import shutil
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

# 钉住包相对 `installer_dir`（也相对共享盘根）的路径。
# 与 `config.yaml` 的 `update_fixture.package`、`reset_fixture.py` 读的
# `cfg.update_package_path` 必须是同一个位置，否则"取到了但用不上"。
FIXTURE_REL = "fixtures/7z2602-x64.exe"

# 节点本地凭据文件的名字（仓库根下，已在 .gitignore）。
# 与 hall_auto/env_pack.NODE_ENV_FILENAME 是同一条约定，这里 import 过来免得抄错。
from hall_auto.env_pack import NODE_ENV_FILENAME  # noqa: E402

# P2 内部安全工具在共享盘上的相对目录（相对共享根）与本机落地位置。
# 与 `step_write_local_config` 写的 `security.tools_dir` 必须指向同一个地方，否则白拷。
SECURITY_TOOLS_REL_DIR = "fixtures/security_tools"
# 缺任一关键文件就认为这份工具不完整（`hall_auto/security.py::security_tools` 也是这么判的）。
SECURITY_TOOLS_REQUIRED = ("CheckAppV.exe", "SignCheck_v2.ps1")

# bootstrap 时提示"哪些凭据还没配"用的建议清单（**只列键名，不涉及值**）。
SUGGESTED_NODE_ENV = (
    "HALL_TEST_USER",
    "HALL_TEST_PASSWORD",
    "HALL_MS_USER",
    "HALL_SHARE_USER",
    "HALL_SHARE_PASSWORD",
)

# 新机器（还没有 config.local.yaml）要用的大厅目标版 / 基线版。
#
# **为什么不能只靠仓库模板 `config.yaml`**：它是公开仓库的模板，里面的版本是旧的
# （`baseline_setup` 还指着早已从共享盘下架的 1.6.8.17S）。新机器读它 → 第 10 步
# 「安装包准备」既拿不到本地包、也拿不到共享盘包 → 转去猜的 CDN 模板（实测 404）
# → **直接 FAIL**，而手册写的预期结果是"全部通过"。所以这里必须显式落盘。
#
# 只做 `setdefault`：老机器手工配过的值一律不覆盖。
# 改版本时这两处要跟共享盘 `hall-packages` 上实际放的包保持一致。
DEFAULT_LATEST_SETUP = "myappstore_1.6.11.4S_Setup.exe"
DEFAULT_BASELINE_SETUP = "myappstore_1.6.10.7S_Setup.exe"

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
    seven_zip = installer_dir / FIXTURE_REL
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
        st.log(f"暂无 7-Zip 更新夹具包：{seven_zip}（下一步会按 缓存/共享盘/官网 顺序取）")
    tools_dir = installer_dir / "fixtures" / "security_tools"
    wanted = ["CheckAppV.exe", "SignCheck_v2.ps1"]
    missing = [n for n in wanted if not (tools_dir / n).is_file()]
    if missing:
        st.log(f"内部安全工具缺 {missing}（{tools_dir}）—— 仅 P2 安全验证受影响，会 skip")
    else:
        st.log(f"内部安全工具就位：{tools_dir}")
    return st


def _ensure_seven_zip_fixture(installer_dir: Path, allow_download: bool, st: Step) -> Path | None:
    """确保钉住包 `fixtures/7z2602-x64.exe` 在手，返回它的路径（拿不到返回 None）。

    **为什么这件事要跟"本机装没装 7-Zip"分开**：
    这个包有两个身份 —— ① 7-Zip 的安装源 ② P1-A「更新夹具」的起点包
    （`reset_fixture.py` 靠它把 7-Zip 压回 26.02，大厅更新列表里才有东西可更新）。
    早先只在"7-Zip 没装"时才去取它，于是**已经装了 7-Zip 的机器永远拿不到夹具包**，
    `test_update_fixture_via_hall` 直接失败。两个身份各自都需要它，所以无条件确保。

    **取包顺序：本地直路径 → 缓存 → 共享盘 → 官网下载**（前三个都不出外网）。
    早先只有"本地直路径 + 官网下载"两条路，于是**没有外网的测试机上，明明共享盘
    躺着这个包也取不到** —— 7-Zip 装不上（手册却承诺"7-Zip 你完全不用管"），
    P1-A 更新用例也报「钉住安装包不存在」。

    **取到后一律落到规范位置 `installer_dir/fixtures/`**：`reset_fixture.py` 和
    P1-A 都按这里找它，只留在 `_cache/fixtures/` 下它们看不到。
    """
    installer = installer_dir / FIXTURE_REL
    if installer.is_file():
        digest = sha256_of(installer)
        if digest == SEVEN_ZIP_FIXTURE_SHA256:
            return installer
        st.log(
            f"钉住包 sha256 不符（期望 {SEVEN_ZIP_FIXTURE_SHA256[:16]}… 实际 {digest[:16]}…），"
            "当作没有，重新取一份"
        )
        try:
            installer.unlink()
        except OSError as exc:
            st.log(f"删不掉坏包（{exc}），放弃重取")
            return None

    got = _fixture_from_cache_or_share(installer_dir, st)
    if got is None:
        if not allow_download:
            st.log(f"钉住包不在本地（{installer}），--no-download 下不取；P1-A 更新用例会失败")
            return None
        got = _fixture_by_download(installer, st)
    if got is None:
        return None

    # 统一落到规范位置：只留在 `_cache/fixtures/` 下，reset_fixture 会报"不存在"。
    try:
        if got.resolve() != installer.resolve():
            installer.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(got, installer)
    except OSError as exc:
        st.log(f"把钉住包落到 {installer} 失败（{exc}），将直接使用 {got}")
        return got

    digest = sha256_of(installer)
    if digest != SEVEN_ZIP_FIXTURE_SHA256:
        st.log(f"取到的包 sha256 不符（{digest[:16]}…），不用它")
        return None
    st.log(f"已取到钉住包：{installer}")
    return installer


def _fixture_config(installer_dir: Path):
    """读配置并把 installer_dir 钉成本次铺设用的目录。

    为什么必须钉住：`step_seven_zip` 跑在**第 4 步**，而写 `config.local.yaml` 是第 5 步 ——
    新机器此刻读到的 `installer_dir` 还是仓库模板里的占位路径（`C:/Users/ASUS/...`）。
    不钉住的话，缓存查找会落到一个别人的目录上，取回来的包也不在本次要用的 installer_dir 里。
    `Config` 是 frozen dataclass，`dataclasses.replace` 换字段不产生副作用。
    """
    from dataclasses import replace

    from hall_auto.config import load_config

    return replace(load_config(), installer_dir=installer_dir)


def _fixture_from_cache_or_share(installer_dir: Path, st: Step) -> Path | None:
    """从本机缓存或共享盘取钉住包（都不出外网）。拿不到返回 None，不抛错。

    这条路的现实意义：测试机常常没有外网，而共享盘上有这个包。
    """
    try:
        cfg = _fixture_config(installer_dir)
    except Exception as exc:
        st.log(f"读配置失败，跳过缓存/共享盘取钉住包：{exc}")
        return None

    try:
        from hall_auto.fetch import cache_dir, ensure_from_share

        cached = cache_dir(cfg) / FIXTURE_REL
        if cached.is_file():
            st.log(f"钉住包命中本机缓存：{cached}")
            return cached
        got = ensure_from_share(cfg, FIXTURE_REL)
    except Exception as exc:
        st.log(f"从共享盘取钉住包失败（{exc}），继续尝试下载")
        return None
    if got is not None:
        st.log(f"钉住包取自共享盘：{got.path}")
        return got.path
    return None


def _fixture_by_download(installer: Path, st: Step) -> Path | None:
    """从官网取钉住包（复用 fetch 的 .part 原子改名 + 递增重试）。"""
    try:
        installer.parent.mkdir(parents=True, exist_ok=True)
        from hall_auto.fetch import _download

        _download(SEVEN_ZIP_URL, installer, timeout_sec=60, retries=3)
    except Exception as exc:
        st.log(
            f"钉住包取不到（{exc}）。若测试机没有外网，把它放到共享盘的 fixtures/ 子目录下"
            f"（`<共享>\\fixtures\\7z2602-x64.exe`）即可让所有节点走局域网拿到；"
            "P1-A 更新用例需要它"
        )
        return None
    return installer


def step_seven_zip(installer_dir: Path, allow_download: bool, skip: bool = False) -> Step:
    """确保 7-Zip 可用，并确保钉住夹具包在手。

    两件事，顺序有讲究：
      1. **先确保钉住包**（`fixtures/7z2602-x64.exe`）—— 它同时是安装源和 P1-A 更新夹具，
         与本机装没装 7-Zip 无关，所以无条件确保。
      2. **再按需安装 7-Zip** —— 已装就跳过，一个安装动作都不做。

    为什么 7-Zip 要自动装（而不是"只检测、缺了让人去下"）：
    `hall_auto/security.py::extract_package` 用 `7z x` 解包待检安装包。
    装的是**钉住的 26.02，不是最新版** —— 它同时是 P1-A 更新夹具的起点，
    装最新版会让 `test_update_fixture_via_hall` 的起点断言失配、用例转 skip。

    拿不到包 / 装不上**都不算铺设失败**：只影响 P2（解包）和 P1-A 的更新用例。
    """
    st = Step("7-Zip")
    if skip:
        st.log("按 --skip-seven-zip 跳过")
        return st

    fixture = _ensure_seven_zip_fixture(installer_dir, allow_download, st)

    if SEVEN_ZIP_EXE.is_file():
        st.log(f"已就位：{SEVEN_ZIP_EXE}（未安装任何东西）")
        return st

    if fixture is None:
        st.log("没有钉住包，装不了 7-Zip；仅 P2 安全验证受影响（会 skip）")
        return st

    if not is_admin():
        # 装到 C:\\Program Files 需要管理员；非提权直接调安装器会弹 UAC 把脚本卡死。
        st.log(
            f"未装 7-Zip 且当前非管理员 —— 装到 {SEVEN_ZIP_EXE.parent} 需要管理员，跳过自动安装。"
            "以管理员重跑本脚本即可自动装；仅 P2 安全验证受影响（会 skip）"
        )
        return st

    st.log(f"未装 7-Zip，静默安装钉住版本 26.02：{fixture}")
    try:
        proc = subprocess.run([str(fixture), "/S"], capture_output=True, check=False, timeout=180)
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


def _unc_target(raw: str) -> str:
    """把 UNC 路径（`\\\\host\\share\\sub`）裁成 `net use` 要的前两段 `\\\\host\\share`。

    非 UNC（本地盘符 / 相对路径）返回空串。
    **必须先判前缀**：`C:/hall-farm` 单纯按反斜杠切也切得出两段，
    会得到 `\\\\C:\\hall-farm` 这种垃圾目标。早先这个函数只被 `package_share.dirs`
    的值调用（配置里保证是 UNC）所以没暴露，现在 `HALL_FARM_ROOT` 可能是本地目录，
    不挡住就会拿本地路径去 `net use`。
    """
    text = str(raw).replace("/", "\\")
    if not text.startswith("\\\\"):
        return ""
    parts = [p for p in text.split("\\") if p]
    if len(parts) < 2:
        return ""
    return f"\\\\{parts[0]}\\{parts[1]}"


def step_share_login(skip: bool) -> Step:
    """连上共享盘（如需凭据）。

    每台测试机都要连一次共享盘，否则 `fetch.py` 走共享盘那一步会因凭据缺失而失败。
    用 `net use ... /persistent:yes` 建立持久连接，重启后自动重连。

    **两个来源都要连**：安装包共享（`package_share.dirs`，只读）和
    农场共享（`HALL_FARM_ROOT`，读写）。早先只连前者，农场的 UNC 靠
    "同一台服务器已经认证过了"这个**隐含前提**兜着 —— 隐含的前提会在换机器、
    换服务器、或加了 `--skip-share` 时突然不成立，所以这里显式连一次。

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

    wanted: list[str] = [str(d) for d in share_dirs(cfg)]
    farm = (os.environ.get("HALL_FARM_ROOT") or "").strip()
    if farm.replace("/", "\\").startswith("\\\\"):
        wanted.append(farm)

    if not wanted:
        st.log("未配置共享盘（package_share.dirs 为空）且未设 HALL_FARM_ROOT，跳过")
        return st

    user = (os.environ.get("HALL_SHARE_USER") or "").strip()
    password = os.environ.get("HALL_SHARE_PASSWORD") or ""

    for d in wanted:
        ok, why = share_reachable(Path(d))
        if ok:
            st.log(f"{d}: 已可达（无需再连）")
            continue
        if not user or not password:
            st.log(f"{d}: 当前不可达（{why}），且未设 HALL_SHARE_USER/PASSWORD，无法自动连接")
            continue
        target = _unc_target(d)
        if not target:
            st.log(f"{d}: 路径格式不像 UNC（\\\\host\\share），跳过")
            continue
        cmd = ["net", "use", target, password, f"/user:{user}", "/persistent:yes"]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            detail = (proc.stdout or proc.stderr or "").strip()[:160]
            st.log(f"{target}: 连接失败 rc={proc.returncode} {detail}")
            continue
        ok2, why2 = share_reachable(Path(d))
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

    # 夹具包（update_fixture.package）拿不到只影响 P1-A 的更新用例，
    # 不该把整台机器的铺设判死 —— 它已经在第 4 步尝试取过一次了。
    fixture_name = str(getattr(cfg.update_fixture, "package", "") or "")

    for name in names:
        try:
            res = ensure_package(cfg, name, allow_download=allow_download)
        except Exception as exc:
            if name == fixture_name:
                st.log(f"{name}: 拿不到（{exc}）—— 仅 P1-A 更新用例受影响，其余套件不受影响")
                st.log("  补救：把它放到共享盘的 fixtures/ 子目录下，节点就能走局域网取到")
            else:
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
    existing.setdefault("latest_setup", DEFAULT_LATEST_SETUP)
    # 基线版同样要落盘：`config.yaml` 里那个 1.6.8.17S 已经不在共享盘上了，
    # 不写的话第 10 步取基线包必失败（见 DEFAULT_BASELINE_SETUP 的说明）。
    existing.setdefault("baseline_setup", DEFAULT_BASELINE_SETUP)
    existing.setdefault("timeouts", {"launch_sec": 120, "ready_sec": 150, "install_sec": 300})
    fixture_apps = existing.setdefault("fixture_apps", {})
    fixture_apps.setdefault("install", "网易云音乐")
    fixture_apps.setdefault("update", "7-Zip（64位）")
    fixture_apps.setdefault("uninstall", "网易云音乐")
    fixture_apps["sync"] = ""
    security = existing.setdefault("security", {})
    security["tools_dir"] = (installer_dir / "fixtures" / "security_tools").as_posix()
    existing.setdefault("node_id", node)
    # 农场目录也落盘：节点机因此**不必每次开新窗口重设 HALL_FARM_ROOT**。
    # 只在本次设了环境变量时覆盖 —— 没设就保留上一轮写进去的值（否则重跑一次会把配置擦掉）。
    farm = (os.environ.get("HALL_FARM_ROOT") or "").strip()
    if farm:
        existing["farm_root"] = farm
    try:
        import yaml

        header = (
            "# 本机配置（每台机器不同，已在 .gitignore）。\n"
            f"# 由 tools/bootstrap_machine.py 于 {datetime.now().isoformat(timespec='seconds')} 生成。\n"
            "# 重新铺设直接重跑该脚本，不要手工改这里除 installer_dir / node_id 之外的字段含义。\n"
        )
        path.write_text(header + yaml.safe_dump(existing, allow_unicode=True, sort_keys=False), encoding="utf-8")
        st.log(f"已写入 {path}（node_id={existing.get('node_id')}）")
        if farm:
            st.log(f"farm_root 已落盘：{farm}（farm_agent / farm_control 会自动读，不必再设环境变量）")
        else:
            st.log(
                "未设 HALL_FARM_ROOT，config.local.yaml 里也没写 farm_root —— "
                "单机跑不受影响；多机跑批要设一次（跑过之后就会落盘）"
            )
    except Exception as exc:
        st.fail(f"写 config.local.yaml 失败：{exc}")
    return st


def step_security_tools(installer_dir: Path, skip: bool = False) -> Step:
    """把 P2 的内部安全工具从共享盘备到本机。

    为什么需要这一步：`step_write_local_config` 把 `security.tools_dir` 指向
    `<installer_dir>/fixtures/security_tools`，但**此前从没人往里放东西** ——
    内部工具（CheckAppV / SignAppsV / SignCheck_v2）按红线不进公开仓库，
    只能人肉拷，于是每台新机器的 `security` 套件必然 skip（报告上一片黄）。

    共享盘**不是** git 仓库，放它完全合规：包源机上放一次，所有节点自动取到，
    把「N 台机器拷 N 次」压成「1 台机器拷 1 次」。

    本机已有完整工具 -> 一个字节都不拷（幂等）。
    共享盘上没有 / 不可达 / 拷完仍缺关键文件 -> **只提示不判失败**：
    只影响 P2，其余套件（launch / apps / login / settings）不受影响。
    """
    st = Step("安全工具")
    if skip:
        st.log("按 --skip-security-tools 跳过")
        return st

    dest = installer_dir / "fixtures" / "security_tools"
    if all((dest / name).is_file() for name in SECURITY_TOOLS_REQUIRED):
        st.log(f"已就位：{dest}（未做任何拷贝）")
        return st

    try:
        from hall_auto.config import load_config
        from hall_auto.fetch import copy_tree_from_share

        cfg = load_config()
    except Exception as exc:
        st.log(f"读配置失败，跳过安全工具准备：{exc}")
        return st

    try:
        count, why = copy_tree_from_share(cfg, SECURITY_TOOLS_REL_DIR, dest)
    except Exception as exc:
        st.log(f"从共享盘取安全工具失败（{exc}）—— 仅 P2 受影响（会 skip）")
        return st

    if not count:
        st.log(f"共享盘上没有 {SECURITY_TOOLS_REL_DIR}（{why}）—— 仅 P2 受影响（会 skip）")
        st.log(
            f"补救：在包源机把内部安全工具拷到共享盘的 {SECURITY_TOOLS_REL_DIR}/ 下"
            "（`<共享>\\fixtures\\security_tools\\`），所有节点即自动取到"
        )
        return st

    missing = [name for name in SECURITY_TOOLS_REQUIRED if not (dest / name).is_file()]
    if missing:
        st.log(f"已拷 {count} 个文件，但缺关键文件 {missing} —— 仅 P2 受影响（会 skip）")
        return st
    st.log(f"已从共享盘备好 {count} 个文件 -> {dest}")
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
    """跑 `tests/unit` 全量自检。

    **不要加 `-m` 过滤**：早先用 `-m unit` 只跑带 unit 标记的那部分，而 `tests/unit`
    下 260 条里只有 127 条带标记 —— 漏掉的正好是 `test_fetch_policy` /
    `test_farm_agent_policy` / `test_env_pack_policy` / `test_reset_policy` /
    `test_bootstrap_policy` 这些**多机链路**模块。手册写的是"全绿说明环境 OK"，
    只跑一半属于自检名不副实（路径已限定 tests/unit，不需要再按 marker 收窄）。
    """
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
        [str(py), "-X", "utf8", "-m", "pytest", "tests/unit", "-q", "--skip-env-check"],
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
    """准备节点本地凭据文件模板（仓库根下 `farm_node.env`，已 gitignore）。

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
            "多机跑批必须设，指向共享盘上的农场目录，如 \\\\LAPTOP-VS5F7HF4\\hall-farm"
            "（写机器名别写 IP，DHCP 换 IP 后会报「系统错误 67 找不到网络名」）"
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
    parser.add_argument(
        "--skip-security-tools", action="store_true", help="不从共享盘准备 P2 内部安全工具"
    )
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
    # 必须在 step_share_login 之后：安全工具是从共享盘取的，没认证过就取不到。
    run(step_security_tools(installer_dir, skip=args.skip_security_tools))
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
