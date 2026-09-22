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

> 下面这条只约束**机器路径**那条守卫（`os.walk` + `SKIP_DIRS`，只看可执行代码）。
> 文件末尾的**凭据**守卫走的是 `git ls-files`（"进仓库的东西"为准），
> 范围含文档 / YAML / 测试夹具 —— 两条的扫描集**故意不一样**，别互相套用。

- 只看 **`.py` / `.ps1`**（可执行代码），且**排除 `tests/`** —— 测试里的路径是夹具数据，
  本来就可以是任意机器路径（如 `test_apps_policy.py` 里解析卸载日志用的 `C:\\Users\\admin\\...`）。
- 只认**字符串字面量里**的路径：`"C:/Users/xxx/..."`。注释与 docstring 里提到
  `C:/Users/xxx/...` 只是**说明文字**，不算硬编码 —— 全仓好几处正是这么解释这条坑的。
- 允许的名字：`Public` + `config.yaml` 里那只金丝雀的名字（**动态读**，不在这里写死）。
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


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


# ---------------- AV 排除项脚本：只能显式调用，不许被 bootstrap 自动带跑 ----------------
#
# 背景（2026-09-21 首台真机）：Windows Defender 会把进程刚写出的临时文件判成
# `Trojan:Win32/Bearfoos.A!ml`，读它返回 GetLastError=225（Python 显示成 `[Errno 22]`），
# 十几秒后文件被隔离删除。治本是加 Defender 排除项 —— 但那是**安全策略变更**：
# 这些目录与 python.exe 从此不再被实时查毒。
#
# 所以 `tools/add_av_exclusions.ps1` 必须是"人点名才跑"的：可预演、可回滚、
# 只动本项目相关的那几条。这条用例锁的就是**别哪天顺手把它塞进 bootstrap 的 12 步里** ——
# 那样 20 台机器会在无人拍板的情况下被削弱防护。

AV_EXCLUSION_SCRIPT = REPO_ROOT / "tools" / "add_av_exclusions.ps1"
BOOTSTRAP_PY = REPO_ROOT / "tools" / "bootstrap_machine.py"


def test_av_exclusion_script_exists_and_is_reversible():
    """脚本要在，且加/删/预演三条路都在。"""
    src = AV_EXCLUSION_SCRIPT.read_text(encoding="utf-8-sig")

    assert "Add-MpPreference" in src and "-ExclusionPath" in src, "得真的加排除项"
    assert "Remove-MpPreference" in src, (
        "必须有回滚路径 —— 安全策略变更不可回滚等于把机器锁死"
    )
    assert "[switch]$Remove" in src and "[switch]$DryRun" in src, (
        "`-Remove` / `-DryRun` 两个开关缺一不可"
    )
    assert "Administrator" in src, "必须先判管理员，否则报错会指向错误的方向"
    assert "config.local.yaml" in src, (
        "installer_dir 必须从本机 config.local.yaml 读 —— 写死路径在别的机器上必然找不到"
    )
    assert "退出码" in src, "退出码要写在 .NOTES 里，脚本才好被串进流程"


def test_bootstrap_never_runs_av_exclusion_script():
    """**核心防线**：bootstrap 不许调用它（加排除项必须有人拍板）。

    要改成自动跑，请先确认安全侧同意，并同步改这条用例与《运行手册》坑 6 ——
    别只是把断言删了。
    """
    src = BOOTSTRAP_PY.read_text(encoding="utf-8")
    assert "add_av_exclusions" not in src, (
        "bootstrap 里出现了对 add_av_exclusions 的调用 —— "
        "加 Defender 排除项是安全策略变更，只能由人显式执行"
    )
    assert "Add-MpPreference" not in src, "bootstrap 不许直接改 Defender 设置"


# ---------------- `-m unit` 的选择集必须等于 `tests/unit` 的全集 ----------------
#
# 2026-09-21 发现（做发放版时顺带撞上，属于本项目最恨的那类"假绿"）：
# 运行手册写的标准单测命令是 `pytest tests/unit -m unit`，而 13 个文件
# （307 条用例）**压根没有 unit 标记**，于是被静默 deselect —— 屏幕上是
# 「127 passed」一片绿。实际 `pytest tests/unit` 收 434 条，其中 **4 条一直在失败**
# （杀软误杀 `[Errno 22]`，见 运行手册 坑 6），从来没有被人看见。
#
# **"绿"和"跑过"是两回事。** 标记漏了不会报错，只会让那些用例安静地不跑 ——
# 和"桌面副本漂了但同步脚本说没事"是同一个病。这条用例把选择集钉死。

_UNIT_MARK = "pytestmark = pytest.mark.unit"


def _has_unit_marker(node: ast.FunctionDef) -> bool:
    """这个函数上有没有 `@pytest.mark.unit` 装饰器。"""
    for dec in node.decorator_list:
        if (
            isinstance(dec, ast.Attribute)
            and dec.attr == "unit"
            and isinstance(dec.value, ast.Attribute)
            and dec.value.attr == "mark"
            and isinstance(dec.value.value, ast.Name)
            and dec.value.value.id == "pytest"
        ):
            return True
    return False


def test_every_unit_file_is_selected_by_the_unit_marker():
    """`tests/unit` 下每条用例都必须能被 `-m unit` 选中。

    两种合格写法：文件里有一行模块级 `pytestmark = pytest.mark.unit`，
    或者每个 `def test_*` 自己带 `@pytest.mark.unit`。

    ⚠️ **必须走 AST，不能看 `def` 的上一行**（2026-09-22 修）：
    早先这里判的是 `lines[i-1].strip() == "@pytest.mark.unit"` ——
    对**带 `@pytest.mark.parametrize` 的用例**必然误判，因为那种写法是

        @pytest.mark.unit
        @pytest.mark.parametrize(("a", "b"), [...])   # 这里可能跨十几行
        def test_x(a, b):

    `def` 的上一行是参数表的收尾 `)`，不是 marker。于是 5 个文件里 9 条**本来就标好的**
    用例被报成"漏标" —— 假红和假绿一样有害：它会让人去给已经标过的用例再加一行。
    """
    files = sorted((REPO_ROOT / "tests" / "unit").glob("test_*.py"))
    assert len(files) >= 20, f"tests/unit 只找到 {len(files)} 个测试文件，路径八成写错了"

    naked: list[str] = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        if _UNIT_MARK in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                if not _has_unit_marker(node):
                    naked.append(f"{f.name}::{node.name}")

    assert not naked, (
        "这些用例不会被 `pytest tests/unit -m unit`（运行手册里的标准单测命令）选中，"
        "于是它们红了也没人知道 —— 要么给文件加一行 `pytestmark = pytest.mark.unit`，"
        "要么给每条用例加 `@pytest.mark.unit`：\n  " + "\n  ".join(sorted(naked))
    )



# ---------------- 模块顶层名字不许重名（重名 = 前一份静默不生效）----------------
#
# 2026-09-22 清出来的实例：`test_bootstrap_policy.py` 里有 **4 个测试函数被定义了两次**，
# 是整块粘贴事故。这次 4 对**恰好逐字相同**，所以没丢断言 —— 纯死代码。
#
# 但**不能因此就认为它无害**：Python 里后一份覆盖前一份，pytest 又是按属性收集的，
# 于是**第一份根本不执行**。只要两份**不完全一样**（改了一处、漏改另一处），
# 就是一条**静默不执行的守卫** —— 屏幕全绿、断言没跑，本项目最怕的那种假绿。
#
# 所以判据取「**重名即红**」，不区分内容：内容相同只是"暂时无害"，
# 而区分内容需要人去读，读的人会漏。4 对逐字相同的都能藏一个月，何况不一样的。
#
# 仓库里**没有任何 linter**（无 pyproject / ruff / flake8，requirements 只有 5 个包），
# 所以这类「静默覆盖」没有任何现成工具兜底 —— 手写守卫就是唯一的兜底。
#
# 覆盖范围（2026-09-22 从"只扫 tests/ 的 `def test_*`"推广）：
#   - 根：`hall_auto/` `tools/` `tests/`
#   - 种类：模块顶层的 函数 / 类 / 赋值（含注解赋值）—— 常量被写两遍同样是静默覆盖
# 只管**模块顶层**：`if/else` 两分支各赋一次是正常写法，不算。
# 跨文件重名不管 —— 那本来就允许（`pytestmark` 按文件生效）。

_SOURCE_ROOTS = ("hall_auto", "tools", "tests")


def _module_python_files():
    out = []
    for root in _SOURCE_ROOTS:
        out += sorted(p for p in (REPO_ROOT / root).rglob("*.py") if "__pycache__" not in p.parts)
    return out


def test_no_top_level_name_is_defined_twice_in_a_module():
    offenders: list[str] = []
    for f in _module_python_files():
        seen: dict[str, list[int]] = {}
        for node in ast.parse(f.read_text(encoding="utf-8")).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                seen.setdefault(node.name, []).append(node.lineno)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        seen.setdefault(t.id, []).append(node.lineno)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                seen.setdefault(node.target.id, []).append(node.lineno)
        for name, linenos in seen.items():
            if len(linenos) > 1:
                rel = f.relative_to(REPO_ROOT).as_posix()
                offenders.append(f"{rel}::{name}（行 {', '.join(map(str, linenos))}）")

    assert not offenders, (
        "同一个模块里顶层名字被定义了两次 —— **只有最后一份生效，前一份静默不跑**。"
        "测试函数重名 = 一条不执行的守卫；常量重名 = 值跟你写的不一样。\n  "
        + "\n  ".join(sorted(offenders))
    )


def test_no_duplicate_keys_in_dict_literals():
    """字面量里同一个 key 写两遍 —— Python **静默保留最后一个**，不报错、不警告。

    最坏的情况是配置表：`{"a": 1, "a": 2}` 看着有两条，实际只有一条，
    屏幕上没有任何提示。所以按「重复即红」锁死。
    """
    offenders: list[str] = []
    for f in _module_python_files():
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Dict):
                continue
            keys: dict[str, list[int]] = {}
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(
                    k.value, (str, int, float, bool, type(None))
                ):
                    keys.setdefault(repr(k.value), []).append(k.lineno)
            for key, linenos in keys.items():
                if len(linenos) > 1:
                    rel = f.relative_to(REPO_ROOT).as_posix()
                    offenders.append(f"{rel}:{linenos[0]}  key {key} 写了 {len(linenos)} 次")

    assert not offenders, (
        "dict 字面量里同一个 key 出现了多次 —— Python 只留最后一个，**前面那个静默消失**。\n  "
        + "\n  ".join(sorted(offenders))
    )


# ---------------------------------------------------------------------------
# 凭据：仓库里只能出现占位符（2026-09-22 加）
#
# 这一节的**扫描范围比上面宽** —— 文档、YAML、ini 全算，而且**不排除 `tests/`**。
# 起因就是漏在文档和测试夹具里，不是漏在可执行代码里：
#
#   - `项目知识库/运行手册.md`：`net user hallshare "<真口令>" /add` 写死，
#     连同连接命令一起。仓库当时是 **public**。
#   - `hall_auto/login.py` 的注释 + `tests/unit/test_login_policy.py` 的常量：
#     各写了一次真实微软测试号邮箱（就是收登录验证码那个账号）。
#
# ## 两条守卫都**故意不含任何真值字面值**
#
# 把真值写进断言 = 用一个新的泄漏去防旧的泄漏，而且以后改口令还得改测试。
# 所以只查**形状**：占位符必须是 `<...>`；邮箱 local part 不许是纯数字。
# ---------------------------------------------------------------------------

TEXT_SUFFIXES = {
    ".py", ".ps1", ".md", ".yaml", ".yml", ".ini", ".txt", ".json", ".env", ".cfg",
}

# 共享盘**账号名**（不是凭据 —— 账号名在 `net use` 命令里本来就明文可见）。
# 抽成常量还有个副作用：报错文案里就不会出现「账号名紧跟一个引号串」的形态，
# 否则这条守卫会**扫到自己** —— 第一版就是这么红的（报错模板自匹配），
# 所以连"描述那个形态"的注释也不能照写，只能像这样绕开说。
SHARE_USER = "hallshare"

# 只在账号名后面跟着引号串的地方找口令 —— 那一定是"拿这个账号去认证"。
# `-AccountName "hallshare"`（账号名自己）不匹配：账号名与引号之间没有空白。
_HALLSHARE_QUOTED = re.compile(rf"""{SHARE_USER}\s+["']([^"'\n]*)["']""")

# 允许的形态：`<共享盘密码>` / `<新密码>` 这类占位符
_PLACEHOLDER = re.compile(r"^<[^<>]+>$")

# 邮箱 local part 是 7 位以上纯数字 —— QQ 号邮箱的形态。
# `\b` 保证了 `alven3330859445@gmail.com`（git 身份，本地部分以字母开头）不会被误伤。
_NUMERIC_MAILBOX = re.compile(r"\b\d{7,}@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _credential_scan_files() -> list[Path]:
    """仓库里**已被 git 跟踪**的、可能带凭据的文本文件（含文档与测试夹具）。

    用 `git ls-files` 而不是 `os.walk`：这条守卫只管"**进仓库**的东西"。
    `config.local.yaml` / `farm_node.env` 这类**故意 gitignore 的本机凭据文件**
    里写真口令是**允许的**（那正是它们存在的意义），扫进来只会变成假红，
    然后被人把守卫删掉 —— 比没有守卫更糟。
    """
    proc = subprocess.run(
        ["git", "-c", "core.quotePath=false", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        pytest.skip(f"拿不到 git ls-files（{proc.stderr.strip()[:120]}），跳过凭据扫描")

    out: list[Path] = []
    for rel in proc.stdout.split("\0"):
        if rel and Path(rel).suffix.lower() in TEXT_SUFFIXES:
            out.append(REPO_ROOT / rel)
    return sorted(out)


def test_share_credential_is_always_a_placeholder():
    """凡是用 `hallshare` 认证的地方，口令必须是 `<...>` 占位符。

    真值只走 `HALL_SHARE_PASSWORD` / `farm_node.env`（已 gitignore）；
    要发给测试同事的发放版由 `tools/sync_handbook.py` 现场替换占位符 ——
    **仓库那份永远只有占位符**，这就是既有机制，别绕过它。
    """
    offenders: list[str] = []
    for path in _credential_scan_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            for value in _HALLSHARE_QUOTED.findall(line):
                if not _PLACEHOLDER.match(value.strip()):
                    offenders.append(f'{rel}:{lineno}  ->  {SHARE_USER} "{value}"')

    assert not offenders, (
        "共享盘口令被写死进了仓库（它本该只走 HALL_SHARE_PASSWORD / farm_node.env）：\n  "
        + "\n  ".join(offenders)
        + "\n换成 `<共享盘密码>` 占位符（改密流程那个是 `<新密码>`，见 sync_handbook）。"
        "\n口径见 AGENTS.md：密码 / 测试号 / 微软邮箱只走环境变量，不落盘、不进 git。"
    )


def test_no_personal_numeric_mailbox_in_repo():
    """仓库里不许出现「local part 是纯数字」的邮箱（QQ 号邮箱的形态）。

    真邮箱走 `HALL_MS_USER`；写文档和夹具时用 `someone@outlook.com` 这类假值。
    """
    offenders: list[str] = []
    for path in _credential_scan_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            for hit in _NUMERIC_MAILBOX.findall(line):
                offenders.append(f"{rel}:{lineno}  ->  {hit}")

    assert not offenders, (
        "仓库里出现了 QQ 号形态的个人邮箱（真实测试号邮箱只走 HALL_MS_USER）：\n  "
        + "\n  ".join(offenders)
        + "\n占位符用 someone@outlook.com。"
    )
