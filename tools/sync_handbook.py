"""把《测试机操作手册》同步一份到桌面 —— 那份是发给测试同事的。

## 为什么需要这个脚本

桌面那份是**手工拷的**，仓库一改它就旧，而且**不会报错**。
2026-09-21 真发生过一次：桌面还停在 09-18 的「10 步版」，仓库已经是「12 步版」，
差了整整三轮修复（含 6 个硬阻断）。**手工同步靠记性，一定会漏** —— 而且漏的代价是
测试同事拿着旧手册跑，踩的正是已经修好的坑。

改完手册跑一下这个，或者把它塞进发手册之前的清单里（见 `项目知识库/交付前验收.md`）。

## 用法

    .venv\\Scripts\\python.exe -X utf8 tools\\sync_handbook.py
    .venv\\Scripts\\python.exe -X utf8 tools\\sync_handbook.py --to "D:\\共享\\手册"
    .venv\\Scripts\\python.exe -X utf8 tools\\sync_handbook.py --check   # 只比对，不写

退出码：`0` 已是最新（或同步成功）／`1` `--check` 下发现不一致。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "项目知识库" / "测试机操作手册.md"


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def default_target() -> Path:
    """默认落点：当前用户桌面。"""
    return Path.home() / "Desktop" / SOURCE.name


def main() -> int:
    parser = argparse.ArgumentParser(description="同步《测试机操作手册》到桌面")
    parser.add_argument("--to", default="", help="目标目录或目标文件（默认当前用户桌面）")
    parser.add_argument("--check", action="store_true", help="只比对，不写文件")
    args = parser.parse_args()

    if not SOURCE.is_file():
        print(f"[FAIL] 源文件不存在：{SOURCE}")
        return 1

    raw = args.to.strip()
    if not raw:
        target = default_target()
    else:
        given = Path(raw)
        target = given if given.suffix.lower() == ".md" else given / SOURCE.name

    src_digest = _sha256(SOURCE)
    src_size = SOURCE.stat().st_size

    if not target.exists():
        if args.check:
            print(f"[旧] 目标不存在：{target}")
            return 1
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE, target)
        print(f"[新] 已同步 -> {target}（{src_size} 字节）")
        return 0

    dst_digest = _sha256(target)
    dst_size = target.stat().st_size

    if src_digest == dst_digest:
        print(f"[同] 已是最新：{target}（{src_size} 字节，sha256 一致）")
        return 0

    if args.check:
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

    shutil.copy2(SOURCE, target)
    print(f"[新] 已同步 -> {target}（{dst_size} -> {src_size} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
