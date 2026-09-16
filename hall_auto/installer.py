from __future__ import annotations

import subprocess
import time
from pathlib import Path

from hall_auto.config import Config
from hall_auto.product import InstalledProduct, image_running, read_installed, stop_product
from hall_auto.version import expected_version_from_setup_name, versions_equal
from hall_auto.waiting import wait_until


class InstallError(RuntimeError):
    pass


def _run_nsis(exe: Path, timeout_sec: int) -> None:
    if not exe.is_file():
        raise InstallError(f"找不到可执行文件: {exe}")
    proc = subprocess.Popen(
        [str(exe), "/S"],
        cwd=str(exe.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        code = proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        raise InstallError(f"NSIS 超时: {exe} ({timeout_sec}s)") from exc
    if code not in (0, None):
        raise InstallError(f"NSIS 退出码 {code}: {exe}")
    # NSIS /S 常先返回、解包子进程还在。进程消失不是完成信号，后面靠版本轮询。
    deadline = time.time() + min(30, timeout_sec)
    while time.time() < deadline and image_running(exe.stem):
        time.sleep(1)


def _wait_until(predicate, timeout_sec: int, message: str) -> None:
    if not wait_until(predicate, timeout_sec=timeout_sec, interval=1):
        raise InstallError(f"{message}；等了 {timeout_sec}s 仍未满足")


def wait_version(expected: str, timeout_sec: int) -> InstalledProduct:
    def _ok():
        info = read_installed()
        if info is None:
            return False
        return versions_equal(info.display_version, expected) and versions_equal(
            info.file_version, expected
        )

    _wait_until(_ok, timeout_sec, f"等待版本变成 {expected}")
    info = read_installed()
    if info is None:
        raise InstallError("版本已满足判定但读不到卸载项")
    return info


def uninstall_hall(cfg: Config) -> None:
    info = read_installed()
    if info is None:
        return
    stop_product(cfg.timeouts.process_stop_sec)
    uninstall_exe = Path(info.uninstall_string.strip().strip('"'))
    _run_nsis(uninstall_exe, cfg.timeouts.uninstall_sec)
    _wait_until(lambda: read_installed() is None, cfg.timeouts.version_settle_sec, "等待卸载完成")


def install_setup(cfg: Config, setup_path: Path) -> InstalledProduct:
    expected = expected_version_from_setup_name(setup_path.name)
    if not setup_path.is_file():
        raise InstallError(f"安装包不存在: {setup_path}")
    stop_product(cfg.timeouts.process_stop_sec)
    _run_nsis(setup_path, cfg.timeouts.install_sec)
    return wait_version(expected, cfg.timeouts.install_sec)


def install_baseline_clean(cfg: Config) -> InstalledProduct:
    uninstall_hall(cfg)
    return install_setup(cfg, cfg.baseline_path)


def upgrade_to_latest(cfg: Config) -> InstalledProduct:
    expected_baseline = expected_version_from_setup_name(cfg.baseline_setup)
    current = read_installed()
    if current is None or not versions_equal(current.display_version, expected_baseline):
        install_baseline_clean(cfg)
    try:
        return install_setup(cfg, cfg.latest_path)
    except InstallError:
        uninstall_hall(cfg)
        return install_setup(cfg, cfg.latest_path)
