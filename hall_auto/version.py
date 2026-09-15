from __future__ import annotations

import re
from pathlib import Path

_SETUP_NAME = re.compile(
    r"(?i)^myappstore_(\d+(?:\.\d+)*)(s)?_setup\.exe$"
)


def expected_version_from_setup_name(filename: str) -> str:
    """安装包文件名 -> 系统期望版本。末尾 S 不进入版本号。"""
    name = Path(filename).name
    match = _SETUP_NAME.match(name)
    if not match:
        raise ValueError(f"无法从安装包文件名解析版本: {filename}")
    return match.group(1)


def parse_dotted_version(text: str) -> tuple[int, ...]:
    parts = [p for p in str(text).strip().split(".") if p != ""]
    if not parts or not all(p.isdigit() for p in parts):
        raise ValueError(f"不是点分数字版本: {text!r}")
    return tuple(int(p) for p in parts)


def versions_equal(actual: str | None, expected: str) -> bool:
    if actual is None:
        return False
    return str(actual).strip() == str(expected).strip()


def four_segment(version: str) -> str:
    """点分版本补成四段：注册表 26.03 对应 exe FileVersion 26.3.0.0。"""
    parts = [int(p) for p in parse_dotted_version(version)]
    while len(parts) < 4:
        parts.append(0)
    return ".".join(str(p) for p in parts[:4])
