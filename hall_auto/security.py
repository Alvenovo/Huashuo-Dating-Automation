"""P2 安全验证：组长用例表「安全验证」板块固定项的无人值守实现。

四条用例的自动化口径（2026-09-16 实测探针定）：
- CheckAppV.exe 只扫**自己所在目录**（递归），可无人值守（空 stdin 不挂、rc=0）、不改写任何
  被扫文件（hash 对照过）；输出是 GBK 暂停行 + UTF-16LE 正文，判据是内嵌签名
  （_catalog_ 签名的系统文件会被判未签名，所以断言只认它自己的输出，与组长口径一致）。
- 产品目录（PersonalStorage）非提权不可写、硬链接也被拒，所以走只读拷贝镜像：
  按相对路径把带签名文件拷进工作目录再跑 CheckAppV，验的是文件字节，与用例等价。
- SignCheck_v2.ps1 头部 Read-Host、尾部 Pause 都能用 stdin 喂两行绕过，csv 落在扫描目录。
"""
from __future__ import annotations

import csv
import os
import re
import shutil
import subprocess
import winreg
from dataclasses import dataclass
from pathlib import Path

from .config import Config

SIGNABLE_GLOBS = ("*.exe", "*.dll", "*.sys", "*.msi", "*.cab", "*.ocx", "*.ps1")
UNSIGNED_COUNT_RE = re.compile(r"共有\s*(\d+)\s*个文件未签名")
_PUBLISHER_EXPECTED = "华硕电脑（上海）有限公司"
_UNINSTALL_KEYS = (
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM32"),
    # 按用户安装的应用（WorkBuddy 等）落在 HKCU，也是「程序和功能」的一部分，不扫会漏判未安装。
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKCU"),
)


class SecurityToolError(RuntimeError):
    pass


def _copy_writable(src: Path, dest: Path) -> None:
    """copy2 会保留源文件只读位（微信源文件/产品目录都带），后续覆盖和 rmtree 会 WinError 5。"""
    if dest.exists():
        os.chmod(dest, 0o777)
        dest.unlink()
    shutil.copy2(src, dest)
    os.chmod(dest, 0o777)


def _rmtree_writable(path: Path) -> None:
    def _onexc(func, target, exc):
        os.chmod(target, 0o777)
        func(target)

    shutil.rmtree(path, onexc=_onexc)


@dataclass(frozen=True)
class CheckResult:
    unsigned_count: int
    unsigned_files: tuple[str, ...]
    raw_text: str


def decode_checkappv(raw: bytes) -> str:
    """正文是 UTF-16LE；开头可能有一段 GBK 的「请按任意键继续」，errors=ignore 吃掉。"""
    return raw.decode("utf-16-le", errors="ignore")


def parse_checkappv(raw: bytes) -> CheckResult:
    text = decode_checkappv(raw)
    match = UNSIGNED_COUNT_RE.search(text)
    if match is None:
        raise SecurityToolError(f"CheckAppV 输出里没有未签名计数行: {text[:200]!r}")
    unsigned = tuple(
        line[2:].strip()
        for line in text.splitlines()
        if line.startswith("❌ ") and (":\\" in line or ":/" in line)
    )
    return CheckResult(unsigned_count=int(match.group(1)), unsigned_files=unsigned, raw_text=text)


def security_tools(cfg: Config) -> tuple[Path, Path]:
    tools = Path(cfg.security.tools_dir)
    checkappv = tools / "CheckAppV.exe"
    script = tools / "SignCheck_v2.ps1"
    missing = [str(p) for p in (checkappv, script) if not p.is_file()]
    if missing:
        raise SecurityToolError(
            f"安全工具缺失（内部工具不进仓库，config.local.yaml 的 security.tools_dir 指过去）: {missing}"
        )
    return checkappv, script


def work_root(cfg: Config) -> Path:
    root = Path(cfg.security.work_dir) if cfg.security.work_dir else Path("reports/_secwork")
    root.mkdir(parents=True, exist_ok=True)
    return root


def extract_package(cfg: Config, dest: Path) -> Path:
    package = cfg.security_package_path
    if not package.is_file():
        raise SecurityToolError(f"待检安装包不存在: {package}")
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [cfg.security.seven_zip, "x", "-y", f"-o{dest}", str(package)],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SecurityToolError(
            f"7z 解包失败 rc={proc.returncode}: {proc.stderr.decode('gbk', errors='ignore')[:300]}"
        )
    return dest


def run_checkappv(target_dir: Path, cfg: Config) -> CheckResult:
    """把 CheckAppV 拷进目标目录跑（它只扫自己所在目录），返回未签名计数与清单。"""
    checkappv, _ = security_tools(cfg)
    _copy_writable(checkappv, target_dir / checkappv.name)
    proc = subprocess.run(
        [str(target_dir / checkappv.name)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    return parse_checkappv(proc.stdout)


def mirror_signable_files(src_dir: Path, dest_dir: Path) -> int:
    """只读拷贝镜像：产品目录不可写，把带签名文件按相对路径拷出来扫，验的是字节。"""
    src = Path(src_dir)
    if not src.is_dir():
        raise SecurityToolError(f"待镜像目录不存在: {src}")
    dest = Path(dest_dir)
    if dest.exists():
        _rmtree_writable(dest)
    count = 0
    for pattern in SIGNABLE_GLOBS:
        for file in src.rglob(pattern):
            if not file.is_file():
                continue
            target = dest / file.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_writable(file, target)
            count += 1
    return count


def run_signcheck(target_dir: Path, cfg: Config) -> Path:
    """无人值守跑 SignCheck_v2.ps1：stdin 喂「路径 + 空行」绕过头部 Read-Host 和尾部 Pause。"""
    _, script = security_tools(cfg)
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ],
        input=f"{target_dir}\n\n".encode("gbk", errors="ignore"),
        capture_output=True,
        check=False,
    )
    reports = sorted(target_dir.glob("ComplianceCheck_*.csv"))
    if not reports:
        raise SecurityToolError(
            f"SignCheck_v2 没产出 csv rc={proc.returncode}: "
            f"{proc.stderr.decode('gbk', errors='ignore')[:300]}"
        )
    return reports[-1]


def signcheck_ecc_rows(report: Path) -> list[str]:
    with report.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    bad = []
    for row in rows:
        algo = (row.get("Public Key Algorithm") or "").upper()
        if "ECC" in algo or "ECDSA" in algo:
            bad.append(f"{row.get('File Name')}:{row.get('Public Key Algorithm')}")
    return bad


def control_panel_entries() -> list[dict[str, str]]:
    """控制面板「程序和功能」= 卸载注册表镜像，HKLM 两位宽 + HKCU 共三视图都扫。"""
    entries = []
    for hive, key_path, label in _UNINSTALL_KEYS:
        try:
            base = winreg.OpenKey(hive, key_path)
        except OSError:
            continue
        with base:
            for i in range(winreg.QueryInfoKey(base)[0]):
                name = winreg.EnumKey(base, i)
                with winreg.OpenKey(base, name) as sub:
                    values = {}
                    for field in ("DisplayName", "Publisher", "DisplayVersion"):
                        try:
                            values[field], _ = winreg.QueryValueEx(sub, field)
                        except OSError:
                            values[field] = ""
                    values["key"] = f"{label}\\{key_path}\\{name}"
                    entries.append(values)
    return entries


def hall_entries(entries: list[dict[str, str]], display_name_contains: str) -> list[dict[str, str]]:
    return [e for e in entries if display_name_contains in (e.get("DisplayName") or "")]


def expected_publisher() -> str:
    return _PUBLISHER_EXPECTED
