"""锁《测试机操作手册》桌面同步工具的策略（tools/sync_handbook.py）。

存在的理由：桌面那份分发副本是**手工拷的**，仓库一改它就旧，而且**不会报错**。
2026-09-21 真发生过 —— 桌面停在 09-18 的「10 步版」，仓库已经是「12 步版」，
差了整整三轮修复（含 6 个硬阻断）。这里把三件事钉死：

  1. `--check` 能准确说出「一致 / 旧了」，且旧了时退出码为 1（能进 CI / 进清单）；
  2. 覆盖前**必须留一份旧版备份** —— 万一对面手工批注过还能找回；
  3. 目标不存在时直接建，不报错。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

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
