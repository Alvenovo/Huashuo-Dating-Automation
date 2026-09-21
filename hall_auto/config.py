from __future__ import annotations

import os
import platform
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
class FixtureApps:
    """P1-A 夹具应用。执行类动作只允许对这里的应用做，日常软件一律不碰。"""

    install: str = ""
    update: str = ""
    uninstall: str = ""
    sync: str = ""


@dataclass(frozen=True)
class UpdateFixture:
    """P1-A 更新夹具：钉一个官方老版本，让大厅目录里永远有更新可点。

    复位（tools/reset_fixture.py）把机器压回 pinned_version；用例从大厅点「更新」，
    断言注册表版本变成向导标题声明的目标版本，且不写死版本号。
    """

    name: str = ""
    registry_hint: str = ""
    pinned_version: str = ""
    package: str = ""
    sha256: str = ""
    url: str = ""
    install_dir: str = ""
    version_exe: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.name and self.registry_hint and self.pinned_version and self.package)


@dataclass(frozen=True)
class SecuritySettings:
    """P2 安全验证。CheckAppV / SignCheck_v2 是公司内部工具，不进仓库，本机 tools_dir 指过去。

    CheckAppV.exe 只扫自己所在目录（递归），所以产品目录（PersonalStorage 非提权不可写）
    走「只读拷贝镜像」：把带签名文件按相对路径拷进工作目录再跑，验的是文件字节，与用例等价。
    """

    tools_dir: str = ""
    seven_zip: str = "C:/Program Files/7-Zip/7z.exe"
    personal_storage_dir: str = "C:/Program Files (x86)/ASUS/PersonalStorage"
    package: str = ""
    work_dir: str = ""
    # 行 14「安全清单 vs 安装包清单对比」：开发给的安全清单，一行一个相对包根路径；空则用例 skip。
    manifest: str = ""
    # 对比范围：signable 只比可签名文件（与 SIGNABLE_GLOBS 同口径），all 比包内全部文件。
    manifest_scope: str = "signable"

    @property
    def configured(self) -> bool:
        return bool(self.tools_dir)


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
    fixture_apps: FixtureApps
    update_fixture: UpdateFixture
    security: SecuritySettings
    node_id: str
    # 多机跑批的任务中转站（共享盘上的农场目录，含 tasks/ done/ results/ logs/）。
    # 环境变量 HALL_FARM_ROOT 优先，其次 config.local.yaml 的 farm_root（bootstrap 会写）。
    # 落到配置里是为了消灭「每开一个新窗口都要重设环境变量」这个坑 ——
    # 忘了设的表现是 farm_agent 直接退出、一个任务都取不到，且看不出原因。
    farm_root: str
    raw: dict

    @property
    def baseline_path(self) -> Path:
        return self.installer_dir / self.baseline_setup

    @property
    def latest_path(self) -> Path:
        return self.installer_dir / self.latest_setup

    @property
    def update_package_path(self) -> Path:
        return self.installer_dir / self.update_fixture.package

    @property
    def update_version_exe(self) -> Path:
        return Path(self.update_fixture.install_dir) / self.update_fixture.version_exe

    @property
    def security_package_path(self) -> Path:
        name = self.security.package or self.latest_setup
        path = Path(name)
        return path if path.is_absolute() else self.installer_dir / name

    @property
    def security_manifest_path(self) -> Path:
        path = Path(self.security.manifest)
        return path if path.is_absolute() else self.installer_dir / path

    def test_account(self) -> tuple[str, str]:
        """凭据优先走环境变量，密码不落盘（规则红线）。"""
        stored = (self.raw.get("accounts") or {}).get("password") or {}
        user = os.environ.get("HALL_TEST_USER") or str(stored.get("user") or "")
        password = os.environ.get("HALL_TEST_PASSWORD") or str(stored.get("password") or "")
        return user, password

    def new_password(self) -> str:
        """改密码往返用例的临时新密码，只走环境变量，绝不读配置文件、绝不落盘。"""
        return os.environ.get("HALL_TEST_NEW_PASSWORD") or ""

    def microsoft_account(self) -> str:
        """微软登录邮箱，优先环境变量；committed 的 config.yaml 不放邮箱（仓库公开）。"""
        stored = ((self.raw.get("accounts") or {}).get("microsoft") or {})
        return os.environ.get("HALL_MS_USER") or str(stored.get("email") or "")


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
    fixture_raw = data.get("fixture_apps") or {}
    update_raw = data.get("update_fixture") or {}
    security_raw = data.get("security") or {}

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
        fixture_apps=FixtureApps(
            install=str(fixture_raw.get("install") or ""),
            update=str(fixture_raw.get("update") or ""),
            uninstall=str(fixture_raw.get("uninstall") or ""),
            sync=str(fixture_raw.get("sync") or ""),
        ),
        update_fixture=UpdateFixture(
            name=str(update_raw.get("name") or ""),
            registry_hint=str(update_raw.get("registry_hint") or ""),
            pinned_version=str(update_raw.get("pinned_version") or ""),
            package=str(update_raw.get("package") or ""),
            sha256=str(update_raw.get("sha256") or ""),
            url=str(update_raw.get("url") or ""),
            install_dir=str(update_raw.get("install_dir") or ""),
            version_exe=str(update_raw.get("version_exe") or ""),
        ),
        security=SecuritySettings(
            tools_dir=str(security_raw.get("tools_dir") or ""),
            seven_zip=str(security_raw.get("seven_zip") or "C:/Program Files/7-Zip/7z.exe"),
            personal_storage_dir=str(
                security_raw.get("personal_storage_dir")
                or "C:/Program Files (x86)/ASUS/PersonalStorage"
            ),
            package=str(security_raw.get("package") or ""),
            work_dir=str(security_raw.get("work_dir") or ""),
            manifest=str(security_raw.get("manifest") or ""),
            manifest_scope=str(security_raw.get("manifest_scope") or "signable"),
        ),
        # 节点标识：多机跑批时区分是哪台机器。优先 HALL_NODE_ID，其次 config，最后主机名。
        node_id=os.environ.get("HALL_NODE_ID")
        or str(data.get("node_id") or "")
        or platform.node()
        or "unknown-node",
        # 环境变量优先（临时切换农场用），其次配置（bootstrap 写的常驻值）。
        # 两边都为空是合法的 —— 单机 `farm_agent --local` 不需要农场目录，
        # 真要跑多机时由 farm_agent / farm_control 自己报错退出。
        farm_root=os.environ.get("HALL_FARM_ROOT")
        or str(data.get("farm_root") or ""),
        raw=data,
    )
