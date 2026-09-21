"""离线依赖包（wheelhouse）的清单读写 —— 工具和 bootstrap 共用一份 schema。

## 背景

测试机**没有外网**。所以依赖的 wheel 得先在**有网**的机器上下好、搬过去。
产出方是 `tools/fetch_wheelhouse.py`，消费方是 `tools/bootstrap_machine.py`
第 2 步。两边共用这里的 `MANIFEST_NAME` / `build_manifest` / `verify_manifest` ——
**schema 只写一份**，不然加个字段就要两边改，改漏一边就是静默失效。

## 为什么清单里非要记 Python 版本

`cp312` 的 wheel 装不进 Python 3.13。版本不匹配时 pip 抛的是
`No matching distribution found` 或者一堆编译错误 —— **一线看不出根因是"wheel 是为
另一个 Python 版本下的"**。所以这里主动比对，直接给一句人话。

（这正是本项目反复踩的那类坑：报错指向错误的方向。）
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

MANIFEST_NAME = "wheelhouse.json"


def sha256_of(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def build_manifest(dest: Path, python_xy: str, requirements_sha256: str) -> dict:
    """扫一遍目录产出清单。**纯读**，不写盘 —— 方便测。"""
    wheels = sorted(p.name for p in dest.glob("*.whl"))
    return {
        "python_xy": python_xy,
        "requirements_sha256": requirements_sha256,
        "wheel_count": len(wheels),
        "wheels": wheels,
        "built_at": datetime.now().isoformat(timespec="seconds"),
    }


def write_manifest(dest: Path, python_xy: str, requirements_sha256: str) -> dict:
    data = build_manifest(dest, python_xy, requirements_sha256)
    (dest / MANIFEST_NAME).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return data


def verify_manifest(dest: Path, want_xy: str, want_req_sha256: str = "") -> tuple[bool, str]:
    """这个 wheelhouse 能不能在本机用。返回 (能不能用, 为什么)。

    `why` 是**给人看的**：能用的场合给出 wheel 数，不能用的场合说清是缺清单、
    版本不匹配、还是 requirements 改过了。
    """
    manifest = Path(dest) / MANIFEST_NAME
    if not manifest.is_file():
        return False, f"没有清单文件 {MANIFEST_NAME}（这个目录不是 fetch_wheelhouse.py 产出的）"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"清单读不了：{exc}"

    got_xy = str(data.get("python_xy") or "")
    if not got_xy:
        return False, "清单里没记 Python 版本，没法判断能不能用"
    if got_xy != want_xy:
        return False, (
            f"wheel 是给 Python {got_xy} 下的，本机是 {want_xy} —— 装不上。"
            f"处理：在有网的机器上用 Python {want_xy} 重跑一次 fetch_wheelhouse.py"
        )
    if want_req_sha256 and str(data.get("requirements_sha256") or "") != want_req_sha256:
        return False, "requirements.txt 改过了，这份 wheelhouse 是旧的 —— 重跑一次 fetch_wheelhouse.py"
    count = int(data.get("wheel_count") or 0)
    if count <= 0:
        return False, "清单里一个 .whl 都没有"
    return True, f"Python {got_xy}，{count} 个 wheel"
