"""把要发给测试同事的文档同步到桌面。

## 为什么需要这个脚本

桌面那几份是**手工拷的**，仓库一改它就旧，而且**不会报错**。
2026-09-21 真发生过一次：桌面还停在 09-18 的「10 步版」，仓库已经是「12 步版」，
差了整整三轮修复（含 6 个硬阻断）。**手工同步靠记性，一定会漏** —— 而且漏的代价是
测试同事拿着旧手册跑，踩的正是已经修好的坑。

改完手册/清单跑一下这个，或者把它塞进发文档之前的清单里（见 `项目知识库/交付前验收.md`）。

## 同步哪几份

见 `DOCS`。加新文档就往那个列表里加一行 —— **别再去手工拷桌面**。

## 用法

    .venv\\Scripts\\python.exe -X utf8 tools\\sync_handbook.py
    .venv\\Scripts\\python.exe -X utf8 tools\\sync_handbook.py --to "D:\\共享\\手册"
    .venv\\Scripts\\python.exe -X utf8 tools\\sync_handbook.py --check   # 只比对，不写

退出码：`0` 全部已是最新（或同步成功）／`1` `--check` 下发现不一致，或源文件缺失。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KB = REPO_ROOT / "项目知识库"

# 要发出去的文档。加一份就往这里加一行。
DOCS: list[Path] = [
    KB / "测试机操作手册.md",
    KB / "新机操作清单.md",
]


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def default_target(doc: Path) -> Path:
    """默认落点：当前用户桌面。"""
    return Path.home() / "Desktop" / doc.name


def _target_for(doc: Path, to_raw: str) -> Path:
    """`--to` 收两种：目录（每份文档放进去）或具体 .md 文件（只允许同步一份）。"""
    raw = (to_raw or "").strip()
    if not raw:
        return default_target(doc)
    given = Path(raw)
    if given.suffix.lower() != ".md":
        return given / doc.name
    if len(DOCS) != 1:
        raise SystemExit(
            f"[FAIL] --to 给到具体 .md 文件时只能同步一份文档，当前配置了 {len(DOCS)} 份："
            f"{[d.name for d in DOCS]}。请给目录。"
        )
    return given


def sync_one(doc: Path, to_raw: str, check: bool) -> int:
    """处理一份文档，返回 0 一致/成功，1 旧了或源缺失。"""
    if not doc.is_file():
        print(f"[FAIL] 源文件不存在：{doc}")
        return 1

    target = _target_for(doc, to_raw)
    src_digest = _sha256(doc)
    src_size = doc.stat().st_size

    if not target.exists():
        if check:
            print(f"[旧] 目标不存在：{target}")
            return 1
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(doc, target)
        print(f"[新] 已同步 -> {target}（{src_size} 字节）")
        return 0

    dst_digest = _sha256(target)
    dst_size = target.stat().st_size

    if src_digest == dst_digest:
        print(f"[同] 已是最新：{target}（{src_size} 字节，sha256 一致）")
        return 0

    if check:
        print(
            f"[旧] 与仓库不一致，需要同步：{target}\n"
            f"     仓库版 {src_size} 字节 / 目标版 {dst_size} 字节\n"
            f"     跑一次不带 --check 的即可同步"
        )
        return 1

    # 覆盖前先把旧的留一份，万一对面手工批注过还能找回
    backup = target.with_name(f"{target.stem}-旧版备份{target.suffix}")
    try:
        shutil.copy2(target, backup)
        print(f"[备] 旧版已留档 -> {backup}")
    except OSError as exc:
        print(f"[备] 旧版留档失败（{exc}），继续覆盖")

    shutil.copy2(doc, target)
    print(f"[新] 已同步 -> {target}（{dst_size} -> {src_size} 字节）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="同步要发给测试同事的文档到桌面")
    parser.add_argument("--to", default="", help="目标目录或目标文件（默认当前用户桌面）")
    parser.add_argument("--check", action="store_true", help="只比对，不写文件")
    args = parser.parse_args()

    worst = 0
    for i, doc in enumerate(DOCS):
        if len(DOCS) > 1:
            print(f"--- {doc.name} ---")
        worst = max(worst, sync_one(doc, args.to, args.check))
        if i + 1 < len(DOCS):
            print()
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
