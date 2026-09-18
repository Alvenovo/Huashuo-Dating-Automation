from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from hall_auto.config import Config
from hall_auto.fetch import FetchResult, configured_filenames, ensure_package
from hall_auto.product import InstalledProduct, image_running, read_installed, stop_product
from hall_auto.version import expected_version_from_setup_name, versions_equal
from hall_auto.waiting import wait_until


class InstallError(RuntimeError):
    pass


def resolve_setup(cfg: Config, filename: str, *, allow_download: bool = True) -> Path:
    """拿到安装包路径：本地优先，缺失则下载到缓存。

    多机跑批时机器上没有人工拷来的包，靠这条路把包装到本地。
    `HALL_ALLOW_PACKAGE_DOWNLOAD=0` 可显式关掉下载（离线验收用）。
    """
    env_flag = os.environ.get("HALL_ALLOW_PACKAGE_DOWNLOAD", "").strip().lower()
    if env_flag in ("0", "false", "no"):
        allow_download = False
    try:
        return ensure_package(cfg, filename, allow_download=allow_download).path
    except Exception as exc:  # FetchError 及底层网络异常统一转成安装层错误
        raise InstallError(f"取安装包失败 {filename}：{exc}") from exc


def resolve_setup_full(cfg: Config, filename: str, *, allow_download: bool = True) -> FetchResult:
    """同上，但要完整的获取结果（含来源与摘要），供证据留痕。"""
    env_flag = os.environ.get("HALL_ALLOW_PACKAGE_DOWNLOAD", "").strip().lower()
    if env_flag in ("0", "false", "no"):
        allow_download = False
    try:
        return ensure_package(cfg, filename, allow_download=allow_download)
    except Exception as exc:
        raise InstallError(f"取安装包失败 {filename}：{exc}") from exc


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
    return install_setup(cfg, resolve_setup(cfg, cfg.baseline_setup))


def reset_before_run(cfg: Config, *, allow_download: bool = True) -> dict:
    """跑批前的机器复位：检测已装的大厅，有就卸掉，并把要用的包备齐。

    组长 2026-09-18 要求：「不论装没装对应大厅，都先检测，有检测到就先删掉，重新下载」。
    这个函数就是那句话的落地。

    返回复位报告（写进证据），不改任何配置。
    """
    import time as _time

    report: dict = {"started_at": _time.strftime("%Y-%m-%dT%H:%M:%S"), "steps": []}

    existing = read_installed()
    if existing is None:
        report["steps"].append({"step": "detect", "found": False, "detail": "未检测到已安装的大厅"})
    else:
        report["steps"].append({
            "step": "detect",
            "found": True,
            "display_name": existing.display_name,
            "display_version": existing.display_version,
            "install_dir": str(existing.install_dir),
        })
        stop_product(cfg.timeouts.process_stop_sec)
        uninstall_exe = Path(existing.uninstall_string.strip().strip('"'))
        _run_nsis(uninstall_exe, cfg.timeouts.uninstall_sec)
        _wait_until(
            lambda: read_installed() is None,
            cfg.timeouts.version_settle_sec,
            "等待卸载完成（复位）",
        )
        report["steps"].append({"step": "uninstall", "ok": True})

    fetched: list[dict] = []
    for name in configured_filenames(cfg):
        try:
            res = ensure_package(cfg, name, allow_download=allow_download)
            fetched.append({
                "filename": name, "source": res.source, "sha256": res.sha256,
                "bytes": res.bytes, "url": res.url or "",
            })
        except Exception as exc:
            fetched.append({"filename": name, "source": "failed", "error": str(exc)})
    report["packages"] = fetched
    report["ok"] = all(p.get("source") != "failed" for p in fetched)
    report["finished_at"] = _time.strftime("%Y-%m-%dT%H:%M:%S")
    return report


def upgrade_to_latest(cfg: Config) -> InstalledProduct:
    expected_baseline = expected_version_from_setup_name(cfg.baseline_setup)
    current = read_installed()
    if current is None or not versions_equal(current.display_version, expected_baseline):
        install_baseline_clean(cfg)
    latest = resolve_setup(cfg, cfg.latest_setup)
    try:
        return install_setup(cfg, latest)
    except InstallError:
        uninstall_hall(cfg)
        return install_setup(cfg, latest)
