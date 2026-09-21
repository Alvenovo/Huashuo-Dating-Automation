"""华硕大厅 PC 自动化公共库。

## 这个 `__init__.py` 必须保持"零重依赖"（2026-09-21 加，别改回去）

原来这里是直接：

    from hall_auto.config import load_config

而 `hall_auto/config.py` 顶层有 `import yaml`。于是**任何** `import hall_auto.xxx`
——包括跟 YAML 毫无关系的 `hall_auto.dpi`、`hall_auto.wheelhouse`——都会连带要求
装了 PyYAML。

后果是自举链条断在最前面：全新机器上跑 `tools/bootstrap_machine.py`，
它在**打印第一行之前**就

    ModuleNotFoundError: No module named 'yaml'

崩掉。而这个脚本的第 2 步干的恰恰是"装依赖"。报错指向"环境没铺好"，
实际是"铺环境的脚本自己起不来" —— 又一处**报错指向错误方向**。

改成 PEP 562 惰性导出：`from hall_auto import load_config` 照样能用，
但只有真的取这个属性时才去 import `config`（那时才需要 yaml）。
子模块（`from hall_auto import dpi`）本来就由导入系统自己处理，不受影响。
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 只给类型检查/IDE 看，运行时不执行
    from hall_auto.config import load_config as load_config
    from hall_auto.version import (
        expected_version_from_setup_name as expected_version_from_setup_name,
    )

__all__ = ["load_config", "expected_version_from_setup_name"]

# 惰性导出表：名字 -> (模块, 模块里的属性名)
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "load_config": ("hall_auto.config", "load_config"),
    "expected_version_from_setup_name": ("hall_auto.version", "expected_version_from_setup_name"),
}


def __getattr__(name: str) -> Any:
    """取 `_LAZY_EXPORTS` 里的名字时才真正 import 对应模块（PEP 562）。"""
    try:
        module_name, attr = _LAZY_EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(importlib.import_module(module_name), attr)
    globals()[name] = value  # 取过一次就缓存，后续直接命中 globals
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
