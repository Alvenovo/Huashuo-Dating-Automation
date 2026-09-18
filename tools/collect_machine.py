"""采集本机环境画像，输出一行 JSON 或一行 CSV，用于汇总多台测试机的配置。

用途：拿到 20 台机器后，在每台上跑一次，把输出汇总成表，按维度分组
（OS / 分辨率 / 缩放 / WebView2 / MS 会话），再决定谁跑哪类用例。

用法：
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\collect_machine.py           # 人类可读
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\collect_machine.py --json    # 一行 JSON
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\collect_machine.py --csv     # 一行 CSV（带表头）
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\collect_machine.py --csv --no-header

不依赖第三方库，裸机（没建 .venv）用系统 python 也能跑。
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import io
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

FIELDS = [
    "node_id",
    "os",
    "arch",
    "screen",
    "scale_percent",
    "monitors",
    "python",
    "python_bits",
    "webview2",
    "webview2_version",
    "hall_installed",
    "hall_version",
    "ms_session",
    "seven_zip",
    "ocr_zh",
    "is_admin",
    "interactive",
]


def _webview2() -> tuple[str, str]:
    """WebView2 运行时是否安装 + 版本。

    实现已下沉到 `hall_auto.profile`（单一权威），这里只做薄包装，
    免得跟 `dpi.machine_profile()` 的字段口径漂成两份。
    """
    from hall_auto.profile import webview2_version

    version = webview2_version()
    return ("yes" if version else "no"), version


def _hall_installed() -> tuple[str, str]:
    """注册表里已装大厅的 (是否安装, 版本)。实现同样在 `hall_auto.profile`。"""
    from hall_auto.profile import hall_installed_version

    version = hall_installed_version()
    return ("yes" if version else "no"), version


def _ocr_zh() -> str:
    from hall_auto.profile import ocr_has_chinese

    return ocr_has_chinese()


def collect() -> dict:
    # 走 hall_auto.dpi：它是环境探测的唯一权威实现（内部已先设 Per-Monitor Aware，
    # 否则读到的是被系统缩放虚拟化后的假值，永远报 100%），别再抄一份。
    from hall_auto.dpi import (
        current_scale_percent,
        is_interactive_session,
        machine_profile,
        node_id,
    )

    profile = machine_profile()
    wv2, wv2_ver = _webview2()
    hall, hall_ver = _hall_installed()
    try:
        admin = str(bool(ctypes.windll.shell32.IsUserAnAdmin()))
    except (AttributeError, OSError):
        admin = "unknown"
    row = {
        "node_id": node_id(),
        "os": profile["os"],
        "arch": profile["arch"] or os.environ.get("PROCESSOR_ARCHITECTURE", ""),
        "screen": profile["screen"],
        "scale_percent": str(current_scale_percent()),
        "monitors": str(profile["monitors"]),
        "python": sys.version.split()[0],
        "python_bits": str(64 if sys.maxsize > 2**32 else 32),
        "webview2": wv2,
        "webview2_version": wv2_ver,
        "hall_installed": hall,
        "hall_version": hall_ver,
        "ms_session": profile.get("ms_session", "unknown"),
        "seven_zip": "yes" if Path("C:/Program Files/7-Zip/7z.exe").is_file() else "no",
        "ocr_zh": _ocr_zh(),
        "is_admin": admin,
        "interactive": str(is_interactive_session()),
    }
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description="采集本机环境画像，供多机汇总")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--json", action="store_true", help="输出一行 JSON")
    group.add_argument("--csv", action="store_true", help="输出 CSV")
    parser.add_argument("--no-header", action="store_true", help="CSV 不带表头（多台追加时用）")
    args = parser.parse_args()

    row = collect()
    if args.json:
        print(json.dumps(row, ensure_ascii=False))
    elif args.csv:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\n")
        if not args.no_header:
            writer.writeheader()
        writer.writerow(row)
        print(buf.getvalue().rstrip("\n"))
    else:
        width = max(len(k) for k in FIELDS)
        for key in FIELDS:
            print(f"{key.ljust(width)} : {row[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
