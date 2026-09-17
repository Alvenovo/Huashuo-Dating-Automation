"""行 14 清单对比的纯逻辑单测：解析、归一、索引、对比，不碰安装包与机器。"""
from __future__ import annotations

import pytest

from hall_auto import security


@pytest.mark.unit
def test_norm_rel_unifies_separators_case_and_prefix():
    assert security.norm_rel("Dir\\Sub\\App.EXE") == "dir/sub/app.exe"
    assert security.norm_rel("./dir/sub/app.exe") == "dir/sub/app.exe"
    assert security.norm_rel("/dir/sub/app.exe") == "dir/sub/app.exe"
    assert security.norm_rel("  dir/sub/app.exe  ") == "dir/sub/app.exe"


@pytest.mark.unit
def test_manifest_entries_skips_comments_blanks_and_takes_csv_first_column(tmp_path):
    manifest = tmp_path / "manifest.txt"
    manifest.write_text(
        "# 安全清单\n"
        "\n"
        "// 另一类注释\n"
        "dir\\sub\\app.exe\n"
        "dir/other.dll,signed,2026-01-01\n"
        "   \n",
        encoding="utf-8",
    )
    assert security.manifest_entries(manifest) == ("dir/sub/app.exe", "dir/other.dll")


@pytest.mark.unit
def test_manifest_entries_empty_file(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    assert security.manifest_entries(empty) == ()


@pytest.mark.unit
def test_package_file_index_scope_filters_signable(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "app.exe").write_bytes(b"x")
    (tmp_path / "sub" / "lib.dll").write_bytes(b"x")
    (tmp_path / "sub" / "readme.txt").write_bytes(b"x")
    (tmp_path / "top.msi").write_bytes(b"x")

    signable = security.package_file_index(tmp_path, "signable")
    assert signable == frozenset({"sub/app.exe", "sub/lib.dll", "top.msi"})

    everything = security.package_file_index(tmp_path, "all")
    assert everything == frozenset({"sub/app.exe", "sub/lib.dll", "sub/readme.txt", "top.msi"})


@pytest.mark.unit
def test_package_file_index_rejects_bad_scope(tmp_path):
    with pytest.raises(security.SecurityToolError):
        security.package_file_index(tmp_path, "bogus")


@pytest.mark.unit
def test_compare_manifest_reports_both_directions():
    diff = security.compare_manifest(["a.exe", "b.dll", "gone.exe"], ["a.exe", "b.dll", "extra.txt"])
    assert diff.missing == ("gone.exe",)
    assert diff.extra == ("extra.txt",)
    assert not diff.consistent


@pytest.mark.unit
def test_compare_manifest_consistent_when_equal():
    diff = security.compare_manifest(["a.exe", "b.dll"], ["b.dll", "a.exe"])
    assert diff.consistent
    assert diff.missing == () and diff.extra == ()
