"""锁「每条用例都带套件标记」—— 漏了会被**静默 deselect**，一条都不跑还不报错。

## 为什么要这条守卫（2026-09-23 我自己踩的）

在 `tests/launch/test_p1_apps.py` 里，我把一个 helper 插在了**装饰器和函数定义之间**：

    @pytest.mark.apps
    @pytest.mark.login
    def _occlusion_suffix(pid): ...        # ← 标记挂到它身上了

    def test_sync_list_logged_in(cfg): ...  # ← 一个标记都没有

后果：`--suites apps-detect` 收集到 `4 items / 3 deselected / 1 selected` ——
**那条用例一条都没跑**，而报告上只表现为「少了一条」，很容易被当成"没投这个套件"或"本来就少"。

这正是「坑 6」（13 个文件漏了 `unit` 标记、被静默 deselect）的同一类问题。
**deselect 不报错、不警告**，所以只能靠用例级守卫兜。

守卫口径：每个 `def test_*` 至少带一个**套件标记**（与 `hall_auto/suites.py` 的 marker 对齐）。
`parametrize` / `skipif` 这类不算套件标记。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from hall_auto.suites import SUITES  # noqa: E402

pytestmark = pytest.mark.unit

# 从套件定义里**推**出合法标记，而不是再抄一份 —— 抄一份迟早漂移。
# 一个套件可能用复合表达式（`apps and not destructive` / `login and not manual`），
# 取里面的标识符即可；`not` 是关键字不算。
SUITE_MARKERS = {
    token
    for suite in SUITES.values()
    for token in suite.marker.replace("(", " ").replace(")", " ").split()
    if token.isidentifier() and token != "not"
}
assert {"unit", "apps", "login", "install"} <= SUITE_MARKERS, SUITE_MARKERS


def _test_files() -> list[Path]:
    return [
        p
        for p in sorted((REPO_ROOT / "tests").rglob("test_*.py"))
        if "__pycache__" not in p.parts
    ]


def _mark_names(value: ast.expr) -> set[str]:
    """从一个表达式里取 `pytest.mark.X` 的 X。

    认三种写法：`@pytest.mark.unit`、`pytestmark = pytest.mark.unit`、
    `pytestmark = [pytest.mark.a, pytest.mark.b]`。
    """
    if isinstance(value, ast.Attribute):
        if (
            isinstance(value.value, ast.Attribute)
            and value.value.attr == "mark"
            and isinstance(value.value.value, ast.Name)
            and value.value.value.id == "pytest"
        ):
            return {value.attr}
        return set()
    if isinstance(value, (ast.List, ast.Tuple)):
        out: set[str] = set()
        for item in value.elts:
            out |= _mark_names(item)
        return out
    return set()


def _suite_markers(node: ast.FunctionDef) -> set[str]:
    """取函数级 `@pytest.mark.X` 里的 X。"""
    found: set[str] = set()
    for dec in node.decorator_list:
        found |= _mark_names(dec)
    return found


def _module_markers(tree: ast.Module) -> set[str]:
    """取模块级 `pytestmark = ...`。

    ⚠️ **不认这个会满屏假阳性** —— 仓库里大量文件是「模块级声明一次」的写法
    （`pytestmark = pytest.mark.unit`）。守卫第一次跑就是被这个坑到：
    报了十几个"漏标记"，实际全都合规。守卫假阳性会让人直接忽略它，等于没有。
    """
    found: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            continue
        found |= _mark_names(node.value)
    return found


def _unmarked_cases(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_marks = _module_markers(tree)
    out: list[tuple[int, str]] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        if not ((module_marks | _suite_markers(node)) & SUITE_MARKERS):
            out.append((node.lineno, node.name))
    return out


def test_every_case_carries_a_suite_marker():
    """**核心回归锁**：漏标记 = 静默 deselect = 一条都不跑。

    这条红了**别改断言** —— 去给那个用例补标记，或者检查是不是把 helper
    插在了装饰器和 `def` 之间（那会把标记挂到 helper 上）。
    """
    problems: list[str] = []
    for path in _test_files():
        for lineno, name in _unmarked_cases(path):
            rel = path.relative_to(REPO_ROOT).as_posix()
            problems.append(f"  {rel}:{lineno}  {name}")

    assert not problems, (
        "这些用例没有任何套件标记 —— 任何 `-m` 过滤都会把它们静默 deselect：\n"
        + "\n".join(problems)
        + "\n\n⚠️ 常见成因：helper 被插在装饰器和 def 之间，标记挂到了 helper 上。"
    )


def test_the_guard_itself_detects_an_unmarked_case(tmp_path):
    """守卫自检：给一段**真的漏标记**的源码，必须能认出来。

    没有这条，上面那条可能因为「路径没扫到」或「AST 判断写反」而**永远绿** ——
    守卫假绿比没有守卫更糟。
    """
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "import pytest\n"
        "\n"
        "@pytest.mark.apps\n"
        "@pytest.mark.login\n"
        "def _helper(pid):\n"
        "    return pid\n"
        "\n"
        "\n"
        "def test_lost_its_markers():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    found = _unmarked_cases(sample)
    assert [name for _lineno, name in found] == ["test_lost_its_markers"], (
        f"守卫认不出漏标记的用例，等于没有：{found}"
    )


def test_the_guard_accepts_a_properly_marked_case(tmp_path):
    """反向锁：正常带标记的用例不许被误报（否则守卫会被当成噪音忽略）。"""
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "import pytest\n"
        "\n"
        "\n"
        "@pytest.mark.apps\n"
        "@pytest.mark.login\n"
        "def test_ok():\n"
        "    assert True\n"
        "\n"
        "\n"
        "@pytest.mark.unit\n"
        "@pytest.mark.parametrize('x', [1, 2])\n"
        "def test_ok_parametrized(x):\n"
        "    assert x\n",
        encoding="utf-8",
    )
    assert _unmarked_cases(sample) == []
