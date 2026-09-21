"""手动跑：验证 `bootstrap_machine.ps1` 找不到 Python 时的四种表现。

    .venv\\Scripts\\python.exe -X utf8 tools\\check_bootstrap_wrapper.py

**为什么不进 `tests/unit`**：它要摆弄 PATH / PATHEXT 并用 `.cmd` 垫片冒充 python，
在别人的机器上容易受环境影响；而 `tests/unit` 会在**每台新机器的 bootstrap 第 12 步**
跑一遍，一个偶发红就会让测试同事以为环境坏了。所以行为验证放这儿按需跑，
`tests/unit/test_bootstrap_wrapper_policy.py` 只锁确定性检查（BOM / 语法 / 关键串）。

## 覆盖的四种情况

| # | 机器状态 | 期望 |
| --- | --- | --- |
| 1 | 没有 python，也没有 py | `[FAIL]` + 中文帮助 + 退出码 1 |
| 2 | PATH 上有 python | 跑起来，且**参数完整转发** |
| 3 | 只有 py 启动器 | 兜底跑起来 + `[提醒]`（能跑但自检会失败） |
| 4 | python 是 WindowsApps 下的假桩 | 认出「应用执行别名」+ 退出码 1 |

## 两个坑（都踩过）

- **PATHEXT 必须给**：PowerShell 靠它解析外部命令的扩展名。用 `subprocess` 传一个
  干净 env 时若不带上它，连 `python.cmd` 都找不到，四个分支会全红 —— 那是探针的问题，
  不是脚本的问题。
- **本沙箱不允许从 PowerShell 直接起 `.exe`**（`& python.exe --version` 拿不到输出、
  `$LASTEXITCODE` 为空）。所以垫片用 `.cmd`（走 cmd.exe）。被测逻辑不受影响：
  包装脚本干的事就是「解析 python → 拼参数 → 调起来」，垫片回显 `%*` 正好验证转发。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_PS1 = REPO_ROOT / "tools" / "bootstrap_machine.ps1"
STUB_PY = 'print("STUB_RAN")\n'

# 一次多带几个开关，顺便验证转发
FLAGS = ["-SkipVenv", "-SkipSchtask", "-SkipShare", "-NodeId", "R01"]


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _run(ps1: Path, path_env: str, flags: list[str] | None = None) -> tuple[int, str]:
    inner = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + f"& '{ps1}'"
    if flags:
        inner += " " + " ".join(flags)
    inner += "; exit $LASTEXITCODE"
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", inner],
        capture_output=True,
        env={
            "PATH": path_env,
            # 见模块 docstring：缺了 PATHEXT 连 .cmd 都找不到
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "SystemRoot": r"C:\Windows",
            "ComSpec": r"C:\Windows\System32\cmd.exe",
            "TEMP": tempfile.gettempdir(),
            "PYTHONUTF8": "1",
        },
    )
    return proc.returncode, _decode(proc.stdout) + _decode(proc.stderr)


def _make_fake_repo(root: Path) -> Path:
    """造一个假仓库：只有 tools/，**没有 .venv**（模拟全新机器）。"""
    tools = root / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REAL_PS1, tools / "bootstrap_machine.ps1")
    (tools / "bootstrap_machine.py").write_text(STUB_PY, encoding="utf-8")
    return tools / "bootstrap_machine.ps1"


def _fake_cmd(dirpath: Path, name: str) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / f"{name}.cmd").write_text("@echo STUB_RAN %*\r\n", encoding="ascii")


def main() -> int:
    if shutil.which("powershell") is None:
        print("本机没有 powershell，跳过")
        return 0

    sysroot = r"C:\Windows\System32"
    results: list[tuple[str, bool, str]] = []

    with tempfile.TemporaryDirectory(prefix="wrapper_probe_") as td:
        root = Path(td)
        ps1 = _make_fake_repo(root)

        rc, out = _run(ps1, sysroot)
        results.append((
            "1 无 python 无 py -> [FAIL] + 中文帮助 + 退出码 1",
            rc == 1 and "[FAIL]" in out and "Add Python to PATH" in out and "找不到可用的 Python" in out,
            f"rc={rc}\n{out}",
        ))

        bindir = root / "bin"
        _fake_cmd(bindir, "python")
        rc, out = _run(ps1, sysroot + ";" + str(bindir), FLAGS)
        forwarded = all(
            f in out
            for f in ["bootstrap_machine.py", "--skip-venv", "--skip-schtask",
                      "--skip-share", "--node-id", "R01"]
        )
        results.append((
            "2 有 python -> 跑起来且参数完整转发",
            rc == 0 and "STUB_RAN" in out and forwarded,
            f"rc={rc} 参数齐全={forwarded}\n{out}",
        ))

        pyonly = root / "pyonly"
        _fake_cmd(pyonly, "py")
        rc, out = _run(ps1, sysroot + ";" + str(pyonly))
        results.append((
            "3 只有 py -> 兜底 + [提醒]",
            rc == 0 and "STUB_RAN" in out and "[提醒]" in out,
            f"rc={rc}\n{out}",
        ))

        stubdir = root / "WindowsApps"
        _fake_cmd(stubdir, "python")
        rc, out = _run(ps1, sysroot + ";" + str(stubdir))
        results.append((
            "4 WindowsApps 桩 -> 认出「应用执行别名」+ 退出码 1",
            rc == 1 and "[FAIL]" in out and "应用执行别名" in out,
            f"rc={rc}\n{out}",
        ))

    print("=" * 62)
    passed = 0
    for name, ok, detail in results:
        print(("PASS  " if ok else "FAIL  ") + name)
        if not ok:
            print("  ---- 实际输出 ----")
            print("  " + detail.replace("\n", "\n  "))
        passed += ok
    print("=" * 62)
    print(f"{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
