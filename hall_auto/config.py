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
    launch_sec: int = 45
    ready_sec: int = 90


@dataclass(frozen=True)
class LaunchSettings:
    cold_starts: int = 3
    required_auto_ids: tuple[str, ...] = ("SearchBarInput", "TabList", "MainWeb")
    webview_ready_names: tuple[str, ...] = ("专题页",)
    popup_whitelist_keywords: tuple[str, ...] = (
        "启动卡片",
        "更新",
        "协议",
        "隐私",
        "权限",
        "须知",
        "条款",
        "欢迎使用",
    )
    update_or_card_keywords: tuple[str, ...] = ("启动卡片", "更新")
    dismiss_buttons: tuple[str, ...] = ("关闭", "取消", "稍后", "以后再说", "我知道了")
    accept_buttons: tuple[str, ...] = ("同意", "确定", "允许", "是")


@dataclass(frozen=True)
class Config:
    installer_dir: Path
    baseline_setup: str
    latest_setup: str
    display_name_contains: str
    search_keyword: str
    search_min_hits: int
    timeouts: Timeouts
    launch: LaunchSettings
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
    launch_raw = data.get("launch") or {}

    def _tuple(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        value = launch_raw.get(key)
        if not value:
            return default
        return tuple(str(x) for x in value)

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
            launch_sec=int(timeouts_raw.get("launch_sec") or 45),
            ready_sec=int(timeouts_raw.get("ready_sec") or 90),
        ),
        launch=LaunchSettings(
            cold_starts=int(launch_raw.get("cold_starts") or 3),
            required_auto_ids=_tuple("required_auto_ids", ("SearchBarInput", "TabList", "MainWeb")),
            webview_ready_names=_tuple("webview_ready_names", ("专题页",)),
            popup_whitelist_keywords=_tuple(
                "popup_whitelist_keywords",
                ("启动卡片", "更新", "协议", "隐私", "权限", "须知", "条款", "欢迎使用"),
            ),
            update_or_card_keywords=_tuple(
                "update_or_card_keywords", ("启动卡片", "更新")
            ),
            dismiss_buttons=_tuple(
                "dismiss_buttons", ("关闭", "取消", "稍后", "以后再说", "我知道了")
            ),
            accept_buttons=_tuple("accept_buttons", ("同意", "确定", "允许", "是")),
        ),
        raw=data,
    )
