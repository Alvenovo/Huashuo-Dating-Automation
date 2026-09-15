from __future__ import annotations

import pytest

from hall_auto.about import normalize_ocr, parse_version


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("vl ． 6 ． 1 1 ． 4", "1.6.11.4"),
        ("v1.6.11.4", "1.6.11.4"),
        ("V1．6．8．17", "1.6.8.17"),
        ("华硕大厅 v1.6.11.4 版权所有", "1.6.11.4"),
        ("没有版本号", None),
    ],
)
def test_parse_version(raw, expected):
    assert parse_version(raw) == expected


@pytest.mark.unit
def test_normalize_ocr_strips_spaces_and_fixes_glyphs():
    assert normalize_ocr("vl ． 6 ． 1 1 ． 4") == "v1.6.11.4"
