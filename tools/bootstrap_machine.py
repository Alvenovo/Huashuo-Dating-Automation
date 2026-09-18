"""每台机器的一次性环境铺设（bootstrap）。

用途：把一台裸的测试机变成「能跑本套 UI 自动化」的节点机。幂等，可重复执行。

做的事（按序）：
  1. 环境自检：OS / 交互式会话 / Python 位数 / 7-Zip / 中文 OCR 语言包 / 管理员权限
  2. 建 .venv 并装 requirements.txt
  3. 校验夹具（大厅安装包 / 7z 夹具包 / 内部安全工具）是否就位，逐个查 SHA256
  4. 生成该机自己的 config.local.yaml（路径 / 夹具 / node_id）
  5. 连共享盘（配了 `package_share.dirs` 才做；凭据走 HALL_SHARE_USER/PASSWORD）
  6. 准备安装包：本地 → 共享盘 → 下载（组长 2026-09-18 要求）
  7. 建提权计划任务（装/卸用例需要）
  8. 跑 tests/unit 自检

注：**不修改显示缩放**。脚本走 Per-Monitor Aware 物理像素坐标，非 100% 不影响正确性
（本机真实 150% 下 P0/P1/P2 全部套件实测跑通），缩放只作为环境事实记录进画像。

用法（管理员 PowerShell 或普通 PowerShell 均可，第 7 步需要管理员）：
    $env:HALL_SHARE_USER="hallshare"; $env:HALL_SHARE_PASSWORD="<密码>"
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\bootstrap_machine.py [选项]

选项：
    --installer-dir PATH   华硕大厅安装包目录（默认沿用现有 config.local.yaml 或内置默认）
    --node-id ID           本机节点标识（默认主机名）
    --skip-venv            不重建虚拟环境
    --skip-schtask         不建计划任务
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

SELFTEST_MARKER = "unit"


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
    seven_zip = Path("C:/Program Files/7-Zip/7z.exe")
    if seven_zip.is_file():
        st.log(f"7-Zip 就位：{seven_zip}")
    else:
        st.fail(f"缺 7-Zip：{seven_zip}（P2 安全验证解包要用，装完后重跑本脚本）")
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
    st = Step("虚拟环境与依赖")
    venv = REPO_ROOT / ".venv"
    py = venv / "Scripts" / "python.exe"
    if skip and py.is_file():
        st.log(f"按 --skip-venv 跳过（沿用 {py}）")
        return st
    if not py.is_file():
        base = sys.executable
        st.log(f"创建虚拟环境：{base} -m venv .venv")
        proc = subprocess.run([base, "-m", "venv", str(venv)], capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            st.fail(f"建 venv 失败 rc={proc.returncode}: {(proc.stderr or '')[:300]}")
            return st
    req = REPO_ROOT / "requirements.txt"
    st.log(f"装依赖：{req.name}")
    proc = subprocess.run(
        [str(py), "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", str(req)],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        st.fail(f"pip install 失败 rc={proc.returncode}: {(proc.stderr or '')[-400:]}")
        return st
    proc = subprocess.run([str(py), "-m", "pip", "list"], capture_output=True, text=True, check=False)
    for line in (proc.stdout or "").splitlines():
        if line.lower().startswith(("pytest ", "pywinauto ", "pywin32 ", "pillow ", "pyyaml ")):
            st.log(f"  {line.strip()}")
    return st


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
        # 官方 26.02 包的固定摘要（config.yaml 里也钉着，两处必须一致）
        expected = "6745fa76dc2ea031596d8678f6f6b99c3c1b435b4164a63485adbbc7b8d82ef0"
        if digest == expected:
            st.log(f"7-Zip 更新夹具包 OK（sha256 {digest[:16]}…）")
        else:
            st.fail(f"7-Zip 夹具包 sha256 不符：期望 {expected[:16]}… 实际 {digest[:16]}…（本地包被换过）")
    else:
        st.log(f"缺 7-Zip 更新夹具包：{seven_zip}（P1-A 更新用例要，可从 https://www.7-zip.org/a/7z2602-x64.exe 下）")
    tools_dir = installer_dir / "fixtures" / "security_tools"
    wanted = ["CheckAppV.exe", "SignCheck_v2.ps1"]
    missing = [n for n in wanted if not (tools_dir / n).is_file()]
    if missing:
        st.log(f"内部安全工具缺 {missing}（{tools_dir}）—— 仅 P2 安全验证受影响，会 skip")
    else:
        st.log(f"内部安全工具就位：{tools_dir}")
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


def step_schtask(skip: bool) -> Step:
    st = Step("提权计划任务")
    if skip:
        st.log("按 --skip-schtask 跳过")
        return st
    if not is_admin():
        st.fail("非管理员无法建计划任务；以管理员重跑本脚本，或手工建（见 项目知识库/运行手册.md）")
        return st
    task = "HallAutoP1"
    script = REPO_ROOT / "run_p1_apps.ps1"
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


def main() -> int:
    parser = argparse.ArgumentParser(description="测试机一次性环境铺设")
    parser.add_argument("--installer-dir", default="")
    parser.add_argument("--node-id", default="")
    parser.add_argument("--skip-venv", action="store_true")
    parser.add_argument("--skip-schtask", action="store_true")
    parser.add_argument("--skip-selftest", action="store_true")
    parser.add_argument("--no-download", action="store_true", help="不下载安装包，只用本地已有的")
    parser.add_argument("--skip-share", action="store_true", help="不尝试连接共享盘")
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
    run(step_write_local_config(installer_dir, node))
    run(step_share_login(args.skip_share))
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
