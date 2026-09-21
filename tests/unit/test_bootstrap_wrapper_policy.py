"""锁 `tools/bootstrap_machine.ps1` 的两件易碎事（2026-09-21 加）。

## 为什么要单开一个文件

这个包装脚本是**测试同事唯一会敲的那条命令**。它以前长这样：

    $py = if (Test-Path $venvPy) { $venvPy } else { "python" }
    & $py -X utf8 @argv

新机器上没装 Python、或装了没勾 "Add Python to PATH" 时，PowerShell 只抛一句英文的
`The term 'python' is not recognized as the name of a cmdlet...`。一线看不懂，
更看不出根因是 PATH 没配 —— 而 Python 恰恰是**唯一需要人工装的第 0 层**，
是新手最容易卡住的一步。

所以加了前置检查。这里把它钉住，防止哪天被"简化"回去。

## 两件易碎事

1. **编码**：文件必须带 UTF-8 BOM。PowerShell 5.1 会把**无 BOM** 的 UTF-8 当 ANSI 读 ——
   中文变乱码，甚至把行吞掉。而报错信息全是中文，一乱码就白做了。
   （同理：任何编辑器另存为 "UTF-8" 而不是 "UTF-8 with BOM" 都会踩这个坑。）
2. **前置检查不能被删**。没有它，失败信息就退回成那句英文 cmdlet 报错。

## 为什么不在这里真起子进程测四个分支

真起进程要摆弄 PATH / PATHEXT 和 `.cmd` 垫片，脆；而这些用例会在**每台新机器的
bootstrap 第 12 步自检**里跑一遍 —— 一个偶发红会让测试同事以为环境坏了。
所以这里只做**确定性检查**（字节、语法树、关键串），四个分支的行为验证见
`tools/check_bootstrap_wrapper.py`（手动跑）。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PS1 = REPO_ROOT / "tools" / "bootstrap_machine.ps1"

# 包装脚本要转发的 10 个开关，必须与 Python 侧 argparse 对齐
_EXPECTED_PARAMS = [
    "InstallerDir", "NodeId", "Wheelhouse", "SkipVenv", "SkipSevenZip", "SkipSecurityTools",
    "SkipSchtask", "SkipSelftest", "SkipShare", "SkipFarm", "NoDownload",
]


@pytest.fixture(scope="module")
def ps1_text() -> str:
    assert PS1.is_file(), f"包装脚本不见了：{PS1}"
    return PS1.read_text(encoding="utf-8-sig")


def test_ps1_is_saved_with_utf8_bom():
    """**核心**：没有 BOM，PowerShell 5.1 会把中文报错读成乱码。

    这条不是在挑格式 —— 报错信息是中文的，乱码就等于没有报错。
    """
    head = PS1.read_bytes()[:3]
    assert head == b"\xef\xbb\xbf", (
        f"bootstrap_machine.ps1 丢了 UTF-8 BOM（前三字节 {head!r}）—— "
        "PowerShell 5.1 会把中文读成乱码。用编辑器另存为「UTF-8 with BOM」。"
    )


def test_ps1_parses_with_powershell():
    """语法要能被真正的 PowerShell 解析器接受（不是靠肉眼）。"""
    if shutil.which("powershell") is None:
        pytest.skip("本机没有 powershell，跳过")
    script = (
        f"$e=$null; $null=[System.Management.Automation.Language.Parser]"
        f"::ParseFile('{PS1}', [ref]$null, [ref]$e); 'PARSE_ERRORS=' + $e.Count"
    )
    proc = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                          capture_output=True, timeout=60)
    out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    assert "PARSE_ERRORS=0" in out, f"包装脚本语法有错：\n{out}"


def test_ps1_resolves_python_before_running(ps1_text):
    """**核心**：起 Python 之前必须先找、找不到要说人话。

    没有这段，失败信息就是那句英文 `The term 'python' is not recognized...`。
    """
    assert "function Resolve-PythonForBootstrap" in ps1_text, "前置的 Python 解析函数被删了"
    assert "[FAIL] 这台机器上找不到可用的 Python" in ps1_text, "缺 Python 时的中文帮助没了"
    assert "Add Python to PATH" in ps1_text, "帮助里必须点明「勾 Add Python to PATH」这个根因"
    assert "Write-PythonMissingHelp" in ps1_text, "帮助函数没被调用"


def test_ps1_detects_the_store_stub(ps1_text):
    """Windows 自带的假 `python.exe` 桩（会弹微软商店）必须被认出来。

    它**看着像装了**，最容易被误判成"已经装好了"。
    """
    assert "WindowsApps" in ps1_text, "没检查「应用执行别名」桩"
    assert "应用执行别名" in ps1_text, "认出来了但没说清是什么"


def test_ps1_falls_back_to_py_launcher(ps1_text):
    """`python` 不在 PATH、但 `py` 在时应该能顶上 —— 总比直接失败强。"""
    assert 'Get-Command py' in ps1_text, "没有 py 启动器兜底"
    assert "[提醒]" in ps1_text, "用了兜底路径却没提醒（手册的自检会因此失败）"


def test_ps1_never_runs_a_bare_python(ps1_text):
    """防回退：不许再出现「不检查就直接 `& python`」这种写法。"""
    for line in ps1_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped.startswith("&"):
            continue
        assert "$py" in stripped, f"这行在直接调外部命令、没走解析结果：{line}"


def test_ps1_forwards_all_switches(ps1_text):
    """11 个开关必须一个不少地转发给 Python 侧（历史上两边对齐过，别再漂）。

    `Wheelhouse` 是 2026-09-21 加的：无外网机器上 `wheelhouse\\` 不一定放在脚本会
    自动找的位置，得能手动指路。只加 Python 侧不加包装脚本，等于一线根本用不上。
    """
    missing = [p for p in _EXPECTED_PARAMS if p not in ps1_text]
    assert not missing, f"这些开关没被转发：{missing}"
