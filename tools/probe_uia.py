"""探壳：把华硕大厅主窗口及其弹层的 UIA 结构 dump 成文本，供 P0-03/04 排障和 P0-06/07 定位设计用。

用法：
    ./.venv/Scripts/python.exe tools/probe_uia.py            # 最多 4 次冷启动，抓到「启动卡片」就停
    ./.venv/Scripts/python.exe tools/probe_uia.py --once     # 只起一次，dump 首页结构
    ./.venv/Scripts/python.exe tools/probe_uia.py --search qq  # dump 搜索结果页结构
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hall_auto.config import REPO_ROOT, load_config
from hall_auto.launch import _overlay_windows, start_fresh, wait_main_window, wait_until_ready
from hall_auto.product import stop_main_process

OUT_DIR = REPO_ROOT / "reports" / "probe"


def _describe(node) -> str:
    def _get(attr):
        try:
            return getattr(node.element_info, attr) or ""
        except Exception:
            return ""

    try:
        rect = node.rectangle()
        box = f"({rect.left},{rect.top},{rect.width()}x{rect.height()})"
    except Exception:
        box = "-"
    return f"{_get('control_type'):12} aid={_get('automation_id'):28} name={_get('name')!r:34} {box}"


def dump_tree(label: str, root, max_nodes: int = 900) -> list[str]:
    lines = [f"===== {label} ====="]
    try:
        nodes = root.descendants()
    except Exception as exc:
        return lines + [f"  <descendants failed: {exc}>"]
    for index, node in enumerate(nodes[:max_nodes]):
        try:
            lines.append(f"  {index:4} {_describe(node)}")
        except Exception as exc:
            lines.append(f"  {index:4} <unreadable: {exc}>")
    if len(nodes) > max_nodes:
        lines.append(f"  ... {len(nodes) - max_nodes} more")
    return lines


def probe_once(cfg, tag: str) -> tuple[list[str], bool]:
    lines: list[str] = []
    card_seen = False
    app = start_fresh(cfg)
    main = wait_main_window(app, cfg)
    for _ in range(30):
        overlays = _overlay_windows(main, cfg.launch)
        if overlays:
            break
        time.sleep(1)
    overlays = _overlay_windows(main, cfg.launch)
    lines += dump_tree(f"main window title={main.window_text()!r}", main)
    for ov in overlays:
        if "启动卡片" in ov.name:
            card_seen = True
        lines += dump_tree(f"overlay name={ov.name!r}", ov.node, max_nodes=300)
    try:
        wait_until_ready(cfg, main)
        lines.append("===== wait_until_ready: PASS =====")
    except Exception as exc:
        lines.append(f"===== wait_until_ready: FAIL {exc} =====")
    stop_main_process(cfg.timeouts.process_stop_sec)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{tag}.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[{tag}] card_seen={card_seen} -> {path}")
    return lines, card_seen


def _web_area(main):
    for node in main.descendants(control_type="Document"):
        try:
            aid = node.element_info.automation_id or ""
        except Exception:
            aid = ""
        if aid == "RootWebArea":
            return node
    return main


def probe_search(cfg, keyword: str, tag: str) -> None:
    lines: list[str] = []
    app = start_fresh(cfg)
    main = wait_main_window(app, cfg)
    try:
        wait_until_ready(cfg, main)
    except Exception as exc:
        print(f"[{tag}] 就绪判定失败，继续探查: {exc}")

    box = main.child_window(auto_id="SearchBarInput", control_type="Edit").wrapper_object()
    box.set_edit_text(keyword)
    time.sleep(1)
    box.type_keys("{ENTER}", set_foreground=True)
    time.sleep(10)

    area = _web_area(main)
    lines += dump_tree(f"after search keyword={keyword!r} area_name={area.element_info.name!r}", area, max_nodes=1500)
    names = []
    for node in area.descendants(control_type="Button"):
        try:
            names.append(node.element_info.name or "")
        except Exception:
            continue
    lines.append("===== button names =====")
    lines += [f"  {name!r}" for name in names]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{tag}.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[{tag}] -> {path}")
    print("\n".join(lines[-40:]))
    stop_main_process(cfg.timeouts.process_stop_sec)


def probe_detail(cfg, keyword: str, title: str, tag: str) -> None:
    app = start_fresh(cfg)
    main = wait_main_window(app, cfg)
    try:
        wait_until_ready(cfg, main)
    except Exception as exc:
        print(f"[{tag}] 就绪判定失败，继续探查: {exc}")

    box = main.child_window(auto_id="SearchBarInput", control_type="Edit").wrapper_object()
    box.set_edit_text(keyword)
    time.sleep(1)
    box.type_keys("{ENTER}", set_foreground=True)
    time.sleep(10)

    from hall_auto.search import open_detail

    try:
        open_detail(main, title)
    except Exception as exc:
        print(f"[{tag}] 点击标题失败: {exc}")
    time.sleep(10)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    lines = dump_tree(f"detail title={title!r}", main, max_nodes=2000)
    names = []
    for node in main.descendants(control_type="Button"):
        try:
            names.append(node.element_info.name or "")
        except Exception:
            continue
    lines.append("===== button names =====")
    lines += [f"  {name!r}" for name in names]
    path = OUT_DIR / f"{stamp}_{tag}.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    try:
        main.set_focus()
        time.sleep(1)
        main.capture_as_image().save(OUT_DIR / f"{stamp}_{tag}.png")
    except Exception as exc:
        print(f"[{tag}] 截图失败: {exc}")
    print(f"[{tag}] -> {path}")
    print("\n".join(lines[-45:]))
    stop_main_process(cfg.timeouts.process_stop_sec)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="只启动一次")
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--search", metavar="KEYWORD", help="搜关键词后 dump 结果页结构")
    parser.add_argument("--detail", metavar="TITLE", help="搜关键词并点进该标题，dump 详情页结构")
    parser.add_argument("--keyword", default=None, help="配合 --detail 使用，默认取配置里的 search_keyword")
    args = parser.parse_args()

    cfg = load_config()
    if args.search:
        probe_search(cfg, args.search, "search")
        return 0
    if args.detail:
        probe_detail(cfg, args.keyword or cfg.search_keyword, args.detail, "detail")
        return 0
    attempts = 1 if args.once else args.attempts
    for attempt in range(attempts):
        _, card_seen = probe_once(cfg, f"probe{attempt + 1}")
        if args.once or card_seen:
            return 0
        time.sleep(2)
    print("未复现「启动卡片」弹层")
    return 0


if __name__ == "__main__":
    sys.exit(main())
