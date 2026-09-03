from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "config.yaml"
LOCAL_CONFIG = REPO_ROOT / "config.local.yaml"


@dataclass(frozen=True)
class Timeouts:
    install_sec: int = 900
    uninstall_sec: int = 300
    version_settle_sec: int = 180
    process_stop_sec: int = 30


@dataclass(frozen=True)
class Config:
    installer_dir: Path
    baseline_setup: str
    latest_setup: str
    display_name_contains: str
    search_keyword: str
    search_min_hits: int
    timeouts: Timeouts
    raw: dict

    @property
    def baseline_path(self) -> Path:
        return self.installer_dir / self.baseline_setup

    @property
    def latest_path(self) -> Path:
        return self.installer_dir / self.latest_setup


def _read_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置必须是映射: {path}")
    return data


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Path | None = None) -> Config:
    config_path = Path(os.environ["HALL_CONFIG"]) if os.environ.get("HALL_CONFIG") else path or DEFAULT_CONFIG
    data = _read_yaml(config_path)
    if config_path.resolve() == DEFAULT_CONFIG.resolve():
        data = _deep_merge(data, _read_yaml(LOCAL_CONFIG))

    installer_dir = os.environ.get("HALL_INSTALLER_DIR") or data.get("installer_dir") or ""
    timeouts_raw = data.get("timeouts") or {}

    return Config(
        installer_dir=Path(str(installer_dir)),
        baseline_setup=str(data.get("baseline_setup") or ""),
        latest_setup=str(data.get("latest_setup") or ""),
        display_name_contains=str(data.get("display_name_contains") or "华硕大厅"),
        search_keyword=str(data.get("search_keyword") or "qq"),
        search_min_hits=int(data.get("search_min_hits") or 2),
        timeouts=Timeouts(
            install_sec=int(timeouts_raw.get("install_sec") or 900),
            uninstall_sec=int(timeouts_raw.get("uninstall_sec") or 300),
            version_settle_sec=int(timeouts_raw.get("version_settle_sec") or 180),
            process_stop_sec=int(timeouts_raw.get("process_stop_sec") or 30),
        ),
        raw=data,
    )
