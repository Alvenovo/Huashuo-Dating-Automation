"""仓库卫生：机器绝对路径不许进代码（2026-09-21 加）。

## 为什么值得单开一个文件

项目要从 1 台铺到 20+ 台**异构**测试机。代码里写死某一台的用户 profile
（`C:/Users/<名字>/...`）在别的机器上必然找不到 —— 而这条路上最贵的不是失败，
是**失败的样子**：`tools/bootstrap_machine.py` 的 `_foreign_profile_owner` 注释里记着，
取包会先试着在 `C:\\Users\\` 下建幽灵目录（要管理员）或直接权限失败，
**报错长得像"共享盘坏了"**，一线会往完全错的方向查半天。

所以 2026-09-21 把口径定下来（写进 `AGENTS.md`「机器绝对路径不进代码」）：
取本机位置用 `$PSScriptRoot`（PS）/ `Path.home()` / `config.local.yaml`（已忽略）/ 环境变量。

## 唯一的例外是**故意的**，别去"清理"它

`config.yaml` 的 `installer_dir` 模板值是 `C:/Users/ASUS/...`，看着像漏改的残留，
其实是**金丝雀**：bootstrap 第 1 步靠它认出「这台机器还没铺过环境 / 整包拷贝带了老机器的
`config.local.yaml`」，认出来才会提示你加 `-InstallerDir` 重跑。

**换成 `C:/Users/Public/...`（脚本自己的默认值）这条检查就静默失效** ——
新机拿这个不存在的目录去取包、失败，而报错指向错误方向。这正是本项目反复出现的「假绿」形态，
而且**清理它的人不会看到任何报错**，所以这里把它锁住。

## 扫描范围与判据（两个都不显然，写清楚）

- 只看 **`.py` / `.ps1`**（可执行代码），且**排除 `tests/`** —— 测试里的路径是夹具数据，
  本来就可以是任意机器路径（如 `test_apps_policy.py` 里解析卸载日志用的 `C:\\Users\\admin\\...`）。
- 只认**字符串字面量里**的路径：`"C:/Users/xxx/..."`。注释与 docstring 里提到
  `C:/Users/xxx/...` 只是**说明文字**，不算硬编码 —— 全仓好几处正是这么解释这条坑的。
- 允许的名字：`Public` + `config.yaml` 里那只金丝雀的名字（**动态读**，不在这里写死）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_YAML = REPO_ROOT / "config.yaml"
GITIGNORE = REPO_ROOT / ".gitignore"
HANDOVER = REPO_ROOT / "会话交接.md"

ARCHIVE_DIRNAME = "交接归档"
CODE_SUFFIXES = {".py", ".ps1"}

# 按**目录名**剪枝（用 os.walk 剪，不然 rglob 会钻进 .venv 的几万个文件里）
SKIP_DIRS = {
    ".git", ".venv", ".pytest_cache", ".workbuddy-ai", "__pycache__",
    "reports", "wheelhouse", "node_modules", "tests", ARCHIVE_DIRNAME,
}

# 只匹配**引号里**的路径。行内 `"` 出现在路径之后时不会命中（正则从左往右找，先遇到引号才有戏），
# 所以 docstring 里 `（\`C:/Users/ASUS/...\`）` 这种说明文字天然被排除。
_QUOTED_USER_PATH = re.compile(r"""["'][^"'\n]*?[Cc]:[\\/]Users[\\/]([^\\/\s"']+)""")

# 故意不 import yaml：这条检查不该有第三方依赖（新机上 PyYAML 可能还没装）
_CONFIG_INSTALLER_DIR = re.compile(r"""^installer_dir:\s*["']([^"']+)["']""", re.MULTILINE)


def _owner_of(path_text: str) -> str:
    """`C:/Users/<owner>/...` → `<owner>`；不是这个形态就返回空串。"""
    parts = path_text.replace("\\", "/").split("/")
    if len(parts) >= 3 and parts[1].lower() == "users":
        return parts[2]
    return ""


def _canary_owner() -> str:
    """从 `config.yaml` 的 `installer_dir` 里取金丝雀的用户名（当前是 `ASUS`）。"""
    match = _CONFIG_INSTALLER_DIR.search(CONFIG_YAML.read_text(encoding="utf-8"))
    assert match, f"{CONFIG_YAML} 里找不到 installer_dir —— 模板被改坏或读法不对"
    return _owner_of(match.group(1))


def _code_files() -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if Path(name).suffix.lower() in CODE_SUFFIXES:
                out.append(Path(dirpath) / name)
    return sorted(out)


def test_no_hardcoded_user_profile_path_in_code():
    """代码里不许出现具体用户 profile 的绝对路径。"""
    allowed = {"public", _canary_owner().lower()}
    offenders: list[str] = []
    for path in _code_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            for owner in _QUOTED_USER_PATH.findall(line):
                if owner.lower() not in allowed:
                    offenders.append(f"{rel}:{lineno}  ->  C:/Users/{owner}/...")

    assert not offenders, (
        "代码里出现了具体用户 profile 的绝对路径（在别的机器上必然找不到，"
        "而报错会指向「共享盘坏了」这个错方向）：\n  "
        + "\n  ".join(offenders)
        + "\n改成本机无关的取法：$PSScriptRoot / Path.home() / config.local.yaml / 环境变量。"
        "\n口径见 AGENTS.md「机器绝对路径不进代码」。"
    )


def test_config_template_installer_dir_stays_a_detectable_canary():
    """`config.yaml` 的模板值必须保持「一眼看出不是本机」的形态。

    `_foreign_profile_owner`（`tools/bootstrap_machine.py`）明确把
    `C:/Users/Public/...` 和**当前用户自己的目录**排除在"外来的"之外。
    所以模板值一旦变成这两者之一，新机读到它就不会被警告，金丝雀失效。
    """
    owner = _canary_owner()
    assert owner, (
        f"`installer_dir` 不再是 `C:/Users/<name>/...` 形态（取到 {owner!r}）——"
        " `_foreign_profile_owner` 只认这种形态，换了就认不出来。"
    )
    assert owner.lower() != "public", (
        "`installer_dir` 被改成了 `C:/Users/Public/...` —— 这是脚本自己的默认值，"
        "`_foreign_profile_owner` 会把它当成「自己人」而不报警，金丝雀就此失效。"
    )
    # 注：如果哪天本机用户名恰好就叫 ASUS，这只金丝雀在这台机器上本来就叫不响 ——
    # 那属于"换一台机器跑"的问题，不在这里断言，免得把确定性用例变成看机器脸色。


def test_handover_archive_stays_local_only():
    """`交接归档/` 是**本机专属**（2026-09-21 定为方案 C）。

    三个选项里选它，理由：归档里是**老的进度快照 + 过期文档副本**
    （`测试机操作手册-桌面旧版-*.md`）。把过期手册提交进仓库比死链更糟 ——
    有人会照着旧版敲命令。而稳定知识本来就该在 `项目知识库/`（已入库）。

    代价是**引用它时必须写明「仅本机」**，否则 clone 的人会照着一个永远不存在的目录找。
    这条锁的就是「别把那句『仅本机』删掉」。
    """
    assert ARCHIVE_DIRNAME + "/" in GITIGNORE.read_text(encoding="utf-8-sig"), (
        f".gitignore 里 `{ARCHIVE_DIRNAME}/` 被删了 —— 归档会跟着提交进仓库（内含过期手册副本）"
    )

    lines = HANDOVER.read_text(encoding="utf-8-sig").splitlines()
    mentions = [i for i, ln in enumerate(lines) if ARCHIVE_DIRNAME in ln]
    assert mentions, (
        f"《会话交接.md》不再提 `{ARCHIVE_DIRNAME}` —— 若是有意去掉指针，"
        "请一并删掉这条用例；否则说明归档被漏写了。"
    )
    # 容忍换行：提及处往后 2 行内出现「本机」即可
    for i in mentions:
        window = "\n".join(lines[i:i + 3])
        assert "本机" in window, (
            f"《会话交接.md》第 {i + 1} 行引用 `{ARCHIVE_DIRNAME}` 时没写「仅本机」——"
            " clone / 整包拷贝后该目录不存在，读者会白找一场。"
        )
