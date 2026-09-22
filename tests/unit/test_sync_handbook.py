"""锁《测试机操作手册》桌面同步工具的策略（tools/sync_handbook.py）。

存在的理由：桌面那份分发副本是**手工拷的**，仓库一改它就旧，而且**不会报错**。
2026-09-21 真发生过 —— 桌面停在 09-18 的「10 步版」，仓库已经是「12 步版」，
差了整整三轮修复（含 6 个硬阻断）。这里把四件事钉死：

  1. `--check` 能准确说出「一致 / 旧了」，且旧了时退出码为 1（能进 CI / 进清单）；
  2. 覆盖前**留一份旧版备份** —— 万一对面手工批注过还能找回（且**第二次覆盖不覆盖留档**）。
     但**上一版就是本脚本产物时不留档**：2026-09-22 用户反馈桌面被堆了一堆「旧版备份」，
     全是我们自己上一轮的输出。判据见 `_is_our_output()`；
  3. 目标不存在时直接建，不报错；
  4. **同步出去的是「发放版」** —— 凭据占位符用 `HALL_SHARE_PASSWORD` 现场替换；
     没设变量时**报错退出，绝不静默降级成占位版**（见文件末「发放版」那一节）。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load():
    path = REPO_ROOT / "tools" / "sync_handbook.py"
    spec = importlib.util.spec_from_file_location("sync_handbook_under_test", path)
    assert spec and spec.loader, f"加载失败：{path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sh_real():
    """加载工具，**不改** DOCS —— 用来钉住"仓库里到底是哪几份文档"。"""
    return _load()


@pytest.fixture
def sh(monkeypatch, tmp_path):
    """加载工具，并把它的文档列表收成**一份临时文件**，避免碰真仓库、真桌面。

    注意：`default_target()` 从文档名取文件名，所以这里文档一换，默认落点的
    文件名也跟着换。测"落点规则"的用例必须用 `sh.DOCS[0].name` 断言，
    不能写死 `测试机操作手册.md` —— 那条由 `test_docs_are_repo_files` 负责。

    **必须把 DOCS 整个换掉**（而不是只换第一个元素）：真仓库那两份一旦留在列表里，
    跑测试就会真的往用户桌面上写文件。
    """
    module = _load()
    src = tmp_path / "源手册.md"
    src.write_text("# 手册\n新版内容\n", encoding="utf-8")
    monkeypatch.setattr(module, "DOCS", [src])
    return module


def _run(module, monkeypatch, *argv) -> int:
    monkeypatch.setattr(sys, "argv", ["sync_handbook.py", *argv])
    return module.main()


def test_check_reports_current_when_identical(sh, monkeypatch, tmp_path, capsys):
    target = tmp_path / "out" / "源手册.md"
    assert _run(sh, monkeypatch, "--to", str(tmp_path / "out")) == 0
    capsys.readouterr()

    assert _run(sh, monkeypatch, "--to", str(tmp_path / "out"), "--check") == 0
    assert "[同]" in capsys.readouterr().out
    assert target.is_file()


def test_check_reports_stale_when_differs(sh, monkeypatch, tmp_path, capsys):
    """**核心**：旧了必须能被发现，且退出码非 0（否则清单里等于没查）。"""
    out = tmp_path / "out"
    out.mkdir()
    (out / "源手册.md").write_text("# 手册\n09-18 的旧内容\n", encoding="utf-8")

    assert _run(sh, monkeypatch, "--to", str(out), "--check") == 1
    printed = capsys.readouterr().out
    assert "[旧]" in printed
    assert "需要同步" in printed


def test_check_reports_missing_target_as_stale(sh, monkeypatch, tmp_path, capsys):
    assert _run(sh, monkeypatch, "--to", str(tmp_path / "空的"), "--check") == 1
    assert "[旧]" in capsys.readouterr().out


def test_sync_creates_target_when_absent(sh, monkeypatch, tmp_path, capsys):
    out = tmp_path / "out"
    assert _run(sh, monkeypatch, "--to", str(out)) == 0
    assert "[新]" in capsys.readouterr().out
    assert (out / "源手册.md").read_text(encoding="utf-8") == sh.DOCS[0].read_text(encoding="utf-8")


def test_sync_backs_up_old_copy_before_overwriting(sh, monkeypatch, tmp_path, capsys):
    """**核心**：覆盖前必须留档 —— 手工批注过的副本不该被无声抹掉。"""
    out = tmp_path / "out"
    out.mkdir()
    target = out / "源手册.md"
    target.write_text("# 手册\n对面手工改过的内容\n", encoding="utf-8")

    assert _run(sh, monkeypatch, "--to", str(out)) == 0
    printed = capsys.readouterr().out
    assert "[备]" in printed, printed

    backup = out / "源手册-旧版备份.md"
    assert backup.is_file(), f"没留备份：{list(out.iterdir())}"
    assert "对面手工改过的内容" in backup.read_text(encoding="utf-8")
    assert target.read_text(encoding="utf-8") == sh.DOCS[0].read_text(encoding="utf-8")


def test_sync_is_idempotent(sh, monkeypatch, tmp_path, capsys):
    """同步两次不该产生备份（第二次本来就一致）。"""
    out = tmp_path / "out"
    _run(sh, monkeypatch, "--to", str(out))
    capsys.readouterr()

    assert _run(sh, monkeypatch, "--to", str(out)) == 0
    assert "[同]" in capsys.readouterr().out
    assert not (out / "源手册-旧版备份.md").exists()


def test_to_accepts_explicit_file_path(sh, monkeypatch, tmp_path):
    """`--to` 给到具体 .md 文件时按文件处理，不再拼一层文件名。"""
    explicit = tmp_path / "随便叫什么都行.md"
    assert _run(sh, monkeypatch, "--to", str(explicit)) == 0
    assert explicit.is_file()


def test_docs_are_repo_files(sh_real):
    """`DOCS` 里每一份都必须真的存在 —— 改错路径会静默同步错内容。

    这条也是"新增文档别忘了进 DOCS"的看门人：往 `项目知识库/` 加了要发出去的文档
    却忘了加进列表，桌面那份就又开始手工漂了（2026-09-21 的原始事故）。
    """
    assert sh_real.DOCS[0] == REPO_ROOT / "项目知识库" / "测试机操作手册.md"
    missing = [d for d in sh_real.DOCS if not d.is_file()]
    assert not missing, f"DOCS 里这些文件不存在：{missing}"
    assert len(sh_real.DOCS) == len(set(sh_real.DOCS)), "DOCS 里有重复项"


def test_docs_includes_new_machine_checklist(sh_real):
    """《新机操作清单》必须在同步列表里 —— 它是发给一线的那份速查。"""
    names = [d.name for d in sh_real.DOCS]
    assert "新机操作清单.md" in names, f"清单没进 DOCS：{names}"


def test_default_target_is_desktop(sh):
    """默认落点必须是当前用户桌面 —— 这是"发给测试同事"的那份。

    断言的是**落点规则**（桌面目录 + 文档同名），不是具体文件名；
    具体文件名由 `test_docs_are_repo_files` 钉住。
    """
    assert sh.default_target(sh.DOCS[0]) == Path.home() / "Desktop" / sh.DOCS[0].name


def test_syncs_every_doc_in_the_list(sh, monkeypatch, tmp_path, capsys):
    """**核心**：多份文档时每一份都要同步到 —— 只同步第一份等于没修这个毛病。"""
    a = tmp_path / "甲.md"
    b = tmp_path / "乙.md"
    a.write_text("甲\n", encoding="utf-8")
    b.write_text("乙\n", encoding="utf-8")
    monkeypatch.setattr(sh, "DOCS", [a, b])

    out = tmp_path / "out"
    assert _run(sh, monkeypatch, "--to", str(out)) == 0
    assert (out / "甲.md").read_text(encoding="utf-8") == "甲\n"
    assert (out / "乙.md").read_text(encoding="utf-8") == "乙\n"

    capsys.readouterr()
    assert _run(sh, monkeypatch, "--to", str(out), "--check") == 0
    assert capsys.readouterr().out.count("[同]") == 2


def test_check_reports_stale_if_any_doc_is_stale(sh, monkeypatch, tmp_path, capsys):
    """只要有一份旧了，退出码就得是 1 —— 否则清单里等于没查。"""
    a = tmp_path / "甲.md"
    b = tmp_path / "乙.md"
    a.write_text("甲\n", encoding="utf-8")
    b.write_text("乙\n", encoding="utf-8")
    monkeypatch.setattr(sh, "DOCS", [a, b])

    out = tmp_path / "out"
    _run(sh, monkeypatch, "--to", str(out))
    (out / "乙.md").write_text("乙改过了\n", encoding="utf-8")
    capsys.readouterr()

    assert _run(sh, monkeypatch, "--to", str(out), "--check") == 1
    printed = capsys.readouterr().out
    assert "[旧]" in printed and "[同]" in printed, printed


def test_to_md_file_rejected_when_multiple_docs(sh, monkeypatch, tmp_path):
    """多份文档时 `--to` 给具体 .md 是有歧义的，必须报错而不是猜一个。"""
    a = tmp_path / "甲.md"
    b = tmp_path / "乙.md"
    a.write_text("甲\n", encoding="utf-8")
    b.write_text("乙\n", encoding="utf-8")
    monkeypatch.setattr(sh, "DOCS", [a, b])

    with pytest.raises(SystemExit):
        _run(sh, monkeypatch, "--to", str(tmp_path / "哪个.md"))


def test_missing_source_fails_clearly(sh, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sh, "DOCS", [tmp_path / "根本没有.md"])
    assert _run(sh, monkeypatch, "--to", str(tmp_path / "out")) == 1
    assert "[FAIL]" in capsys.readouterr().out


# ---------------- 两份文档之间的漂移 ----------------
#
# 《测试机操作手册》和《新机操作清单》讲同一套动作。两份都发给一线，
# 一处改了另一处忘改，**一线按旧的那份敲就会失败** —— 而且失败信息只会说 401/404，
# 看不出是文档过期。下面把最容易漂的两点钉住。

_ZIPBALL = "https://api.github.com/repos/Alvenovo/Huashuo-Dating-Automation/zipball/main"
# 网页按钮用的地址。**GitHub 的 API 文档里没有它**，私有仓库不保证能用。
_UNDOCUMENTED = "archive/refs/heads/main.zip"


def _fenced_blocks(text: str) -> str:
    """把所有 ``` 围起来的代码块拼起来 —— 只看"会被照抄去敲"的部分。"""
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        if inside:
            out.append(line)
    return "\n".join(out)


@pytest.fixture(scope="module")
def doc_texts() -> dict[str, str]:
    from importlib.util import module_from_spec, spec_from_file_location

    path = REPO_ROOT / "tools" / "sync_handbook.py"
    spec = spec_from_file_location("sync_handbook_docs", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    missing = [d for d in module.DOCS if not d.is_file()]
    assert not missing, f"DOCS 里这些文件不存在：{missing}"
    return {d.name: d.read_text(encoding="utf-8") for d in module.DOCS}


def test_both_docs_use_the_documented_download_url(doc_texts):
    """两份文档的下载命令必须是**同一个**、而且是官方文档记载的那个端点。

    2026-09-21 发现原来写的是 `github.com/.../archive/refs/heads/main.zip` ——
    那是网页按钮用的地址，GitHub 的 API 文档里没有它。私有仓库要走有文档保证的
    `api.github.com/repos/.../zipball/{ref}`（要求细粒度令牌 `Contents: Read-only`）。
    """
    for name, text in doc_texts.items():
        assert _ZIPBALL in _fenced_blocks(text), f"{name} 的下载命令里没有统一地址"


def test_no_doc_tells_people_to_run_the_undocumented_url(doc_texts):
    """老地址不许出现在**代码块**里 —— 出现在正文里解释"为什么不用它"是允许的。"""
    for name, text in doc_texts.items():
        assert _UNDOCUMENTED not in _fenced_blocks(text), (
            f"{name} 的代码块里还留着 `{_UNDOCUMENTED}` —— 那是网页按钮的地址，"
            "GitHub 文档没记载，私有仓库不保证可用"
        )


def test_both_docs_state_the_required_token_permission(doc_texts):
    """令牌只需要 `Contents: Read-only` 这一个权限 —— 两份都得写清楚。

    写错权限的代价很具体：给多了是安全风险，给少了报 404（GitHub 对权限不足
    会假装"仓库不存在"），一线会去查错方向。
    """
    for name, text in doc_texts.items():
        assert "Contents" in text, f"{name} 没提 Contents 权限"
        assert "Read-only" in text, f"{name} 没写清权限级别是 Read-only"


def test_both_docs_mention_the_token_prefix(doc_texts):
    """细粒度令牌 `github_pat_` 开头 —— 让人一眼能认出拿的是哪种令牌。"""
    for name, text in doc_texts.items():
        assert "github_pat_" in text, f"{name} 没说令牌长什么样"


# 2026-09-21 复核发现的**漏项**（用户当场问「新测试机连接共享盘步骤呢」）：
# 两份文档第一次访问共享盘的动作是 `& "\\...\repo\install_git.ps1"` / `git clone "\\..."`，
# 而 `hallshare` 凭据要到**后面几步**才设成环境变量 —— 共享的 NTFS 只授权了 `hallshare`
# （本机账号在网络上是"别人"），所以新机器跑那两条命令**必然**报
# 「找不到网络路径」/「拒绝访问」，而报错会把人引向"网络坏了"。
# 正确顺序：**先认证共享盘，再 clone**。
_NET_USE_SHARE = r"net use \\LAPTOP-VS5F7HF4\hall-packages /user:hallshare"


def test_both_docs_authenticate_the_share_before_cloning(doc_texts):
    """两份文档都必须写「先连共享盘」的命令，而且**排在 clone 之前**。

    这是新机的**第一个网络动作**，漏了它整条链在第一行就断 —— 属于
    《交付前验收.md》里「前置条件清单漏项」那一类：不在代码里，在现实里。
    """
    for name, text in doc_texts.items():
        blocks = _fenced_blocks(text)
        assert _NET_USE_SHARE in blocks, (
            f"{name} 的代码块里没有「先连共享盘」的命令 —— "
            f"新机器上 `git clone` 共享盘会报「找不到网络路径」，而那是没认证、不是网络坏"
        )
        assert blocks.index(_NET_USE_SHARE) < blocks.index("git clone"), (
            f"{name} 里 `git clone` 出现在连共享盘之前 —— 顺序反了，新机第一条命令就会失败"
        )


# 2026-09-21 第一台真机实测（用户当场跑）又暴露两个坑，同属"文档承诺 ≠ 真机能跑"：
#   ① `git clone` 走 UNC 路径会被 Git 判为"非本地目录" → `fatal: detected dubious ownership`，
#      必须先 `git config --global --add safe.directory '<UNC 裸仓库>'`；
#   ② 裸仓库叫 `hall-auto.git`，clone 按**仓库名**落地成 `hall-auto`，
#      而文档接着 `cd Huashuo-Dating-Automation` → 路径不存在，后面每一步全断。
_SAFE_DIRECTORY = "safe.directory"
_CLONE_TARGET = "Huashuo-Dating-Automation"


def test_both_docs_allow_the_unc_bare_repo_before_cloning(doc_texts):
    """两份文档都要写 `safe.directory` 放行，而且**排在 clone 之前**。"""
    for name, text in doc_texts.items():
        blocks = _fenced_blocks(text)
        assert _SAFE_DIRECTORY in blocks, (
            f"{name} 没写 `git config --global --add safe.directory` —— "
            "UNC 路径下 `git clone` 会报 fatal: detected dubious ownership"
        )
        assert blocks.index(_SAFE_DIRECTORY) < blocks.index("git clone"), (
            f"{name} 里 clone 排在 safe.directory 之前 —— 真机上第一条命令就失败"
        )


def test_both_docs_clone_into_the_expected_directory(doc_texts):
    """clone 必须显式给目标目录名 —— 不给就落地成 `hall-auto`，后面 `cd` 全报路径不存在。"""
    for name, text in doc_texts.items():
        blocks = _fenced_blocks(text)
        lines = [ln for ln in blocks.splitlines() if "git clone" in ln and "hall-auto.git" in ln]
        assert lines, f"{name} 里找不到从共享盘 clone 的命令"
        for ln in lines:
            assert _CLONE_TARGET in ln, (
                f"{name} 的 clone 没写目标目录名（`hall-auto.git` 会落地成 `hall-auto`，"
                f"后续 `cd {_CLONE_TARGET}` 必然报路径不存在）：{ln.strip()}"
            )


# 2026-09-22 发新机前复核查出的**顺序错**（《交付前验收.md》第 11 条）：
# 《新机操作清单》第 3.5 步（关实时保护）让一线跑
#     .venv\Scripts\python.exe -X utf8 tools\probe_av_quarantine.py
# 可 `.venv` 要到**第 4 步 bootstrap** 才建出来 —— 全新机器跑到 3.5 步时它还不存在，
# 报的是「系统找不到指定的路径」，一线会以为 Python 没装好，往错的方向查半天。
# 探针只用标准库，任何 Python 都能跑。规则：**排在 bootstrap 之前的步骤不许引用 `.venv`**。
_BOOTSTRAP_CMD = "tools\\bootstrap_machine.ps1"
_VENV_PY = ".venv\\Scripts\\python.exe"


def test_checklist_does_not_use_the_venv_before_bootstrap(doc_texts):
    """《新机操作清单》的**代码块**里，`.venv\\Scripts\\python.exe` 只能排在 bootstrap 之后。

    `.venv` 是 bootstrap 第 2 步建的。在它之前引用必然「找不到路径」，
    而那个报错指向的是"Python 好像没装好"，跟真因（`.venv` 还没建）差着十万八千里 ——
    正是本项目反复治的"报错指向错误方向"那一类。

    **只看代码块**（同 `_fenced_blocks` 的既有口径）：正文里解释"为什么不要用它"是允许的，
    那正是本节开头那条提示。
    """
    blocks = _fenced_blocks(doc_texts["新机操作清单.md"])
    assert _BOOTSTRAP_CMD in blocks, (
        f"清单的代码块里找不到 bootstrap 命令（`{_BOOTSTRAP_CMD}`）—— 守卫的前提没了，先修守卫"
    )
    bootstrap_at = blocks.index(_BOOTSTRAP_CMD)
    early = [ln.strip() for ln in blocks[:bootstrap_at].splitlines() if _VENV_PY in ln]
    assert not early, (
        "《新机操作清单》在跑 bootstrap 之前就让人敲带 `.venv` 的命令：\n  "
        + "\n  ".join(early)
        + "\n`.venv` 要到 bootstrap 第 2 步才建出来，全新机器上这条命令必然报"
        "「系统找不到指定的路径」，而一线会以为 Python 没装好。"
        "探针只用标准库，改用系统 `python`"
    )


# 2026-09-22 用户**真跑**时踩出来的文档缺陷（不是代码缺陷）：
# 《新机操作清单》第 3 步让人「新开管理员窗口」设环境变量，可新窗口的当前目录是
# `C:\WINDOWS\system32` —— Windows 的规定，**不继承**你原来那个窗口。
# 紧接着的第 3.5 步探针用的是**相对路径** `tools\probe_av_quarantine.py`，真机报的是：
#
#     can't open file 'C:\WINDOWS\system32\tools\probe_av_quarantine.py'
#
# 读起来像「探针被杀了 / 脚本丢了」，真因只是当前目录不对 —— 又一条「报错指向错误方向」。
# 规则：**管理员窗口那个代码块里必须自带 `cd` 回仓库根**；相对路径命令所在代码块同理
# （有人会从别处跳回来单敲那一条，见第 12 步的报错对照表）。
#
# 锚点用**带空格赋值的那一整行**：附录速查表里的 `$env:HALL_SHARE_USER="hallshare"`
# 是无空格连写，不该被这条规则管（速查表是给已经会的人抄的，本来就不含 `cd`）。
_ADMIN_ENV_LINE = '$env:HALL_SHARE_USER = "hallshare"'
_REPO_CD = "cd C:\\Users\\你的用户名\\Desktop\\Huashuo-Dating-Automation"
_PROBE_CMD = "tools\\probe_av_quarantine.py"


def _fenced_blocks_list(text: str) -> list[str]:
    """按 ``` 切成**一个个**代码块（`_fenced_blocks` 的逐块版）。

    判断"这条命令能不能照抄"必须**逐块**看：`cd` 落在上一个代码块里、跟命令不在一块，
    跳着读文档的人就会漏掉 —— 2026-09-22 真机踩的正是这个。
    """
    out: list[str] = []
    cur: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            if inside:
                out.append("\n".join(cur))
                cur = []
            inside = not inside
            continue
        if inside:
            cur.append(line)
    if cur:
        out.append("\n".join(cur))
    return out


def test_admin_window_blocks_cd_back_to_the_repo(doc_texts):
    """设共享盘变量那个（管理员窗口）代码块必须自带 `cd` 回仓库根。

    管理员窗口是**全新窗口**，当前目录是 `C:\\WINDOWS\\system32`，而后面
    `tools\\...` 全是相对路径。缺了 `cd`，下一步报的是「找不到文件」——
    看着像脚本没了，实际只是当前目录不对。
    """
    for name, text in doc_texts.items():
        blocks = [b for b in _fenced_blocks_list(text) if _ADMIN_ENV_LINE in b]
        assert blocks, f"{name} 里找不到「设共享盘变量」的代码块 —— 守卫前提没了，先修守卫"
        for block in blocks:
            assert _REPO_CD in block, (
                f"{name} 的「设共享盘变量」代码块里没有 `cd` 回仓库根目录：\n"
                f"---\n{block}\n---\n"
                "新开的管理员窗口当前目录是 C:\\WINDOWS\\system32（Windows 规定，不继承旧窗口），"
                "而后面 `tools\\...` 全是相对路径 —— 不 cd 就会报"
                "「can't open file 'C:\\WINDOWS\\system32\\tools\\probe_av_quarantine.py'」，"
                "读起来像探针丢了，真因只是当前目录不对"
            )


def test_probe_command_blocks_cd_back_to_the_repo(doc_texts):
    """探针命令所在代码块必须自带 `cd` —— 它会被从别处跳回来单独敲。

    第 12 步的报错对照表就写着「先跑探针确认」，照做的人当前目录不保证是仓库根。
    """
    for name, text in doc_texts.items():
        for block in _fenced_blocks_list(text):
            if _PROBE_CMD not in block:
                continue
            assert _REPO_CD in block, (
                f"{name} 里 `{_PROBE_CMD}` 所在代码块没有 `cd`：\n"
                f"---\n{block}\n---\n"
                "这条命令会被从别处跳回来单独敲，当前目录不保证是仓库根；"
                "在 C:\\WINDOWS\\system32 下敲会报「找不到文件」，把人引向「脚本丢了」这个错方向"
            )


# 2026-09-22 同一次复核查出的**版本口径错**：
# 离线 `wheelhouse` 是给 Python 3.12 打的（`cp312` 的 wheel + `python-3.12.10-amd64.exe`），
# 而清单第 1 步让人从 python.org 下「64 位安装包」——今天打开给的是 3.13/3.14。
# 有外网的机器没事（首台真机就是 Py 3.13.14 过的，所以这个坑一直没暴露），
# **无外网的机器会在第 4 步第 2 步硬卡**，而同一份清单还写着「外网也不是必需的」。
# 规则：两份文档都必须把版本钉到 **3.12**，不许再写「3.x」（那等于"随便装"）。
def test_both_docs_pin_python_312(doc_texts):
    """两份文档都要把 Python 版本钉到 3.12，且不许再出现「Python 3.x」这种模糊说法。

    断言**逐行收集违规行**，不直接对整篇文档做 `in` —— 后者失败时 pytest 会把
    整份文档当 diff 打出来（实测 479 行），真正要看的那一行反而淹了。
    """
    for name, text in doc_texts.items():
        assert "Python 3.12" in text, (
            f"{name} 没把 Python 版本钉到 3.12 —— 离线 wheelhouse 是 `cp312`，"
            "装了 3.13/3.14 的无外网机器会在 bootstrap 第 2 步 FAIL"
        )
        vague = [ln.strip() for ln in text.splitlines() if "Python 3.x" in ln]
        assert not vague, (
            f"{name} 里还有「Python 3.x」—— 那是「版本随便」的意思，"
            "而离线依赖包只认 3.12。一线照它装 3.13/3.14，无外网机器就卡在第 2 步，"
            "还得回来重装一遍 Python。违规行：\n  " + "\n  ".join(vague)
        )


# 2026-09-21 第二台真机实测又暴露一个坑，而且是**报错指向错误方向**的那一类：
#   节点读不到共享盘裸仓库的 pack 文件（那 3 个文件的 ACL 里没有 `hallshare`，
#   是 `git clone --bare` 走硬链接把本地目录的 ACL 带过来的），
#   而 git 报的却是 `failed to copy file to '<桌面上的目标文件>': Permission denied` ——
#   一线会去查桌面保护策略、杀软、磁盘空间，**全错**。
#   真因在共享盘那一侧，只有共享宿主能修（`tools/check_share_acl.ps1`）。
#   两份文档都必须把这条写进报错对照表，否则下一个测试同事会照错方向再浪费半天。
_PERMISSION_DENIED = "Permission denied"
_ACL_TOOL = "check_share_acl"


def test_both_docs_point_the_pack_permission_error_at_the_share_acl(doc_texts):
    """两份文档都要说明：pack 拷贝报 `Permission denied` 是**共享盘权限**问题。

    这条的价值在于**它反直觉** —— 报错里的路径是本地目标文件，真因却在共享盘。
    文档不写，一线必然在本机反复折腾（换目录、关杀软、清盘），全是白费。
    """
    for name, text in doc_texts.items():
        assert _PERMISSION_DENIED in text, (
            f"{name} 的报错对照表里没有 `Permission denied` 这一条 —— "
            "真机上撞到时一线只能自己猜，而这一条**猜必错**"
        )
        assert _ACL_TOOL in text, (
            f"{name} 没提 `tools\\check_share_acl.ps1` —— "
            "这条错只有共享宿主能修，文档必须把球明确踢给负责人，"
            "否则一线会在测试机上做一堆无用功"
        )


# ---------------- 知识库引用的脚本必须真的在仓库里 ----------------
#
# 2026-09-21 差点犯：确认"杀软误杀临时文件"用的探针先放进了 `reports/probe/`，
# 而整个 `reports/` 被 `.gitignore` 忽略 —— 可《运行手册》坑 6 正是叫新机器的人去跑它。
# clone 之后那条命令必然落空，而且**不报错**，只是让人白跑一趟。
# 探针已挪到 `tools/probe_av_quarantine.py`；这条用例锁住"别再指向空气"。

_KB_DIR = REPO_ROOT / "项目知识库"
_TOOLS_REF = re.compile(r"tools[\\/]([A-Za-z0-9_]+\.(?:py|ps1))")


def test_kb_tool_references_exist():
    """知识库里 `tools\\xxx.py|ps1` 形式的引用必须真的有这个文件。

    （只查 `tools\\`：`reports/` 下的引用是**本机**跑过的证据，clone 后不存在属正常，
    但那种引用要写明「本机」，与《会话交接.md》引用 `交接归档/` 的口径一致。）
    """
    missing: list[str] = []
    for doc in sorted(_KB_DIR.glob("*.md")):
        for name in sorted(set(_TOOLS_REF.findall(doc.read_text(encoding="utf-8")))):
            if not (REPO_ROOT / "tools" / name).is_file():
                missing.append(f"{doc.name} -> tools/{name}")
    assert not missing, (
        "知识库引用了不存在的 tools 脚本（新机器照着敲会白跑一趟）：\n  "
        + "\n  ".join(missing)
    )


# ---------------- 发放版：真密码从环境变量来 ----------------
#
# 2026-09-21 定：桌面那份要发出去的**只要发放版**（带真密码），不要占位版。
# 而密码不能进 git —— 于是仓库存占位符、同步时用环境变量现场替换。
#
# 这一组用例钉住的是**最要命的那个静默失败**：忘了设环境变量时，
# 脚本绝不能"悄悄地"把一份密码写着 `<共享盘密码>` 的文档落到桌面。
# 那份发出去**看起来是好的**，一线要到 `net use` 报 401 才发现 —— 和本项目
# 反复治的"假绿"是同一类病。

_ENV_NAME = "HALL_SHARE_PASSWORD"


@pytest.fixture(autouse=True)
def _no_ambient_password(monkeypatch):
    """测试期间清掉凭据环境变量 —— 本机 shell 真设了它也不能影响判定。

    不清的话，「没设密码必须报错」那条会在开发机上随机失败，
    而失败原因（本机正好设了变量）跟被测逻辑毫无关系。
    """
    monkeypatch.delenv(_ENV_NAME, raising=False)


def _doc_with_placeholder(tmp_path: Path, name: str = "清单.md") -> Path:
    src = tmp_path / name
    src.write_text(
        '# 清单\n\nnet use \\\\HOST\\share /user:hallshare "<共享盘密码>" /persistent:yes\n',
        encoding="utf-8",
    )
    return src


def test_password_env_name_matches_the_documented_one(sh_real):
    """环境变量名钉死 —— 上面那个 autouse fixture 用的是字面量，别让它悄悄脱钩。"""
    assert sh_real.PASSWORD_ENV == _ENV_NAME


def test_placeholders_are_substituted(sh, monkeypatch, tmp_path, capsys):
    src = _doc_with_placeholder(tmp_path)
    monkeypatch.setattr(sh, "DOCS", [src])
    monkeypatch.setenv(_ENV_NAME, "PW-EXAMPLE")

    out = tmp_path / "out"
    assert _run(sh, monkeypatch, "--to", str(out)) == 0
    text = (out / "清单.md").read_text(encoding="utf-8")
    assert "PW-EXAMPLE" in text
    assert "<共享盘密码>" not in text
    assert "发放版" in capsys.readouterr().out


def test_refuses_to_write_when_password_missing(sh, monkeypatch, tmp_path, capsys):
    """**核心**：没设密码时不许静默降级成占位版 —— 那份发出去就是废文档。"""
    src = _doc_with_placeholder(tmp_path)
    monkeypatch.setattr(sh, "DOCS", [src])

    out = tmp_path / "out"
    assert _run(sh, monkeypatch, "--to", str(out)) == 1
    assert not (out / "清单.md").exists(), "没设密码却写出了文件 —— 正是要防的静默降级"
    printed = capsys.readouterr().out
    assert "[FAIL]" in printed
    assert _ENV_NAME in printed, "报错必须点名要设哪个环境变量"
    assert "--placeholder" in printed, "得告诉人怎么故意生成占位版"


def test_check_also_refuses_without_password(sh, monkeypatch, tmp_path, capsys):
    """`--check` 同样不能装一致 —— 它算不出发放版，就没资格说"最新"。"""
    src = _doc_with_placeholder(tmp_path)
    monkeypatch.setattr(sh, "DOCS", [src])

    assert _run(sh, monkeypatch, "--to", str(tmp_path / "out"), "--check") == 1
    assert "[FAIL]" in capsys.readouterr().out


def test_placeholder_flag_allows_placeholder_output(sh, monkeypatch, tmp_path, capsys):
    """`--placeholder` 是显式降级 —— 只给调试脚本本身用。"""
    src = _doc_with_placeholder(tmp_path)
    monkeypatch.setattr(sh, "DOCS", [src])

    out = tmp_path / "out"
    assert _run(sh, monkeypatch, "--to", str(out), "--placeholder") == 0
    text = (out / "清单.md").read_text(encoding="utf-8")
    assert "<共享盘密码>" in text
    assert "发放版" not in text, "占位版不该挂「发放版」横幅 —— 那是自欺"


def test_new_password_placeholder_is_never_substituted(sh, monkeypatch, tmp_path):
    """**核心**：`<新密码>` 是"你自己想一个新的"，换成共享盘密码是**错的**。

    它出现在改密流程 `net user hallshare "<新密码>"` 里。换成旧密码的话，
    照抄那条命令会真的把共享盘密码设回原值 —— 错得还很隐蔽。
    """
    src = tmp_path / "手册.md"
    src.write_text(
        '# 手册\n\nnet user hallshare "<新密码>"\n\n$env:HALL_SHARE_PASSWORD = "<密码>"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(sh, "DOCS", [src])
    monkeypatch.setenv(_ENV_NAME, "PW-EXAMPLE")

    out = tmp_path / "out"
    assert _run(sh, monkeypatch, "--to", str(out)) == 0
    text = (out / "手册.md").read_text(encoding="utf-8")
    assert "<新密码>" in text, "改密占位符被替换了 —— 会把共享盘密码设成旧值"
    assert text.count("PW-EXAMPLE") == 1, "只该替换那一个凭据占位符"


def test_release_banner_is_added_under_the_title(sh, monkeypatch, tmp_path):
    src = _doc_with_placeholder(tmp_path)
    monkeypatch.setattr(sh, "DOCS", [src])
    monkeypatch.setenv(_ENV_NAME, "PW-EXAMPLE")

    out = tmp_path / "out"
    _run(sh, monkeypatch, "--to", str(out))
    lines = (out / "清单.md").read_text(encoding="utf-8").split("\n")
    assert lines[0] == "# 清单", "标题必须还在第一行"
    banner = "\n".join(lines[:4])
    assert "发放版" in banner and "明文凭据" in banner
    assert "不要 commit" in banner, "横幅要拦住误 commit —— 这是最实际的泄露路径"


def test_unregistered_placeholder_blocks_the_release(sh, monkeypatch, tmp_path, capsys):
    """有人加了新占位符却没登记进白名单 → 必须报错，不许原样发出去。"""
    src = tmp_path / "清单.md"
    src.write_text('# 清单\n\nnet use \\\\HOST\\share "<共享盘密码>" "<另一个密码>"\n', encoding="utf-8")
    monkeypatch.setattr(sh, "DOCS", [src])
    monkeypatch.setenv(_ENV_NAME, "PW-EXAMPLE")

    out = tmp_path / "out"
    assert _run(sh, monkeypatch, "--to", str(out)) == 1
    assert not (out / "清单.md").exists()
    printed = capsys.readouterr().out
    assert "另一个密码" in printed, "要指名道姓说哪个占位符没登记"


def test_real_docs_contain_only_registered_placeholders(sh_real):
    """**仓库那两份**替换后不许残留凭据占位符（`<新密码>` 除外）。

    这是"新增了一种占位符"的看门人：漏登记的话，发放版里它会**原样**发出去，
    而发放版本身看不出来 —— 一线照抄那条命令就失败。
    """
    for doc in sh_real.DOCS:
        text = doc.read_text(encoding="utf-8")
        rendered = sh_real.render_release(text, "PW-EXAMPLE", doc.name)
        leftover = sh_real.leftover_placeholders(rendered)
        assert not leftover, (
            f"{doc.name} 里有没登记的凭据占位符 {leftover} —— "
            f"加进 sync_handbook.PASSWORD_PLACEHOLDERS，或者确认它不是凭据"
        )


def test_second_overwrite_keeps_the_first_backup(sh, monkeypatch, tmp_path, capsys):
    """第二次覆盖不能把上一次的留档也覆盖掉 —— 那等于没有留档。

    真机上就撞过：桌面已有 `新机操作清单-旧版备份.md`（更早那份手工版），
    再同步一次会把**手工批注过的那份**抹掉。
    """
    out = tmp_path / "out"
    out.mkdir()
    target = out / "源手册.md"

    target.write_text("第一版\n", encoding="utf-8")
    assert _run(sh, monkeypatch, "--to", str(out)) == 0
    capsys.readouterr()
    assert (out / "源手册-旧版备份.md").read_text(encoding="utf-8") == "第一版\n"

    target.write_text("第二版\n", encoding="utf-8")
    assert _run(sh, monkeypatch, "--to", str(out)) == 0
    capsys.readouterr()

    backups = sorted(p.name for p in out.glob("源手册-旧版备份*.md"))
    assert len(backups) == 2, f"第二次覆盖没另存一份：{backups}"
    assert (out / "源手册-旧版备份.md").read_text(encoding="utf-8") == "第一版\n", (
        "第一次的留档被覆盖了"
    )


def test_plain_doc_without_placeholders_still_syncs(sh, monkeypatch, tmp_path):
    """没有凭据占位符的文档照常同步 —— 发放版逻辑不该拦住无关文档。"""
    src = tmp_path / "普通.md"
    src.write_text("# 普通文档\n\n没有凭据。\n", encoding="utf-8")
    monkeypatch.setattr(sh, "DOCS", [src])

    out = tmp_path / "out"
    assert _run(sh, monkeypatch, "--to", str(out)) == 0
    assert (out / "普通.md").read_text(encoding="utf-8") == src.read_text(encoding="utf-8")


def test_repeated_sync_over_our_own_output_leaves_no_backups(sh, monkeypatch, tmp_path, capsys):
    """**用户反馈的正是这个**：反复同步不许在桌面堆「旧版备份」。

    2026-09-22 用户说「我要留的是新机操作清单，怎么给我生成那么多旧机备份」——
    一查桌面 3 份/文档，全是我们自己上一轮的产物。
    留档的意义是保住**人改过的东西**（见 `test_sync_backs_up_old_copy_before_overwriting`），
    保住自己的输出没有意义。
    """
    src = _doc_with_placeholder(tmp_path)
    monkeypatch.setattr(sh, "DOCS", [src])
    monkeypatch.setenv(_ENV_NAME, "PW-EXAMPLE")
    out = tmp_path / "out"

    for i in range(3):  # 第一次建，后两次是覆盖
        src.write_text(
            f'# 清单\n\n第 {i + 1} 版\n'
            'net use \\\\HOST\\share /user:hallshare "<共享盘密码>"\n',
            encoding="utf-8",
        )
        assert _run(sh, monkeypatch, "--to", str(out)) == 0
        capsys.readouterr()

    leftovers = sorted(p.name for p in out.iterdir() if "旧版备份" in p.name)
    assert leftovers == [], f"覆盖自己的输出不该留档，却留下 {len(leftovers)} 份：{leftovers}"
    assert len(list(out.iterdir())) == 1, f"落点该只有一份当前版：{sorted(p.name for p in out.iterdir())}"


def test_release_mark_actually_appears_in_the_banner(sh_real):
    """指纹必须真的在横幅里 —— 否则 `_is_our_output` 恒为 False，备份又开始堆。

    这种脱钩不会报错：横幅文案改了、指纹没跟着改，判定就静默失效，
    表现是「桌面又莫名其妙多了一堆旧版备份」。属于本项目反复踩的假绿同类。
    """
    assert sh_real._RELEASE_MARK in sh_real._BANNER.format(name="x.md"), (
        "`_RELEASE_MARK` 没出现在 `_BANNER` 里 —— 覆盖自己的产物时会被误判成手工文件、"
        "于是每同步一次就多一份「旧版备份」"
    )
