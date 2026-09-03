"""华硕大厅 PC 自动化公共库。"""

from hall_auto.config import load_config
from hall_auto.version import expected_version_from_setup_name

__all__ = ["load_config", "expected_version_from_setup_name"]
