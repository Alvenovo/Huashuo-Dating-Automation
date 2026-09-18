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


class ResetBlocked(RuntimeError):
    """复位被拒绝执行（不可逆操作的保护闸）。"""


def reset_allowed() -> bool:
    """复位是否被允许卸载已装的大厅。门禁与环境变量/命令行开关同口径。

    **为什么复位要单独一道门禁**：pytest 的 `--allow-install` 只拦用例，
    拦不住复位 —— 复位是在用例之外先把大厅卸掉。没有这道闸时，
    在办公机上跑一次 `reset_machine.py` 会真把大厅卸了，且没有任何提示。
    """
    return os.environ.get("HALL_ALLOW_INSTALL") == "1"


def check_packages_available(cfg: Config, *, allow_download: bool = True) -> tuple[bool, list[dict]]:
    """**只读**探测「三个包都能拿到吗」，不下载、不拷贝、不改任何东西。

    探测顺序与 `ensure_package` 一致但不落盘：本地 → 缓存 → 共享盘。
    共享盘那步只做可达性 + `is_file()`，不做拷贝 —— 目的是判断"万一卸了能不能装上"。

    返回 (是否全都能拿到, 每个包的探测结果)。

    **为什么卸载前必须先跑这个**：先把大厅卸了、再把包拉下来，一旦包拉不到，
    机器就停在「大厅被卸掉且装不回来」的状态 —— 对跑批是最坏的结果。
    正确的顺序是「先确认能拿到包，再决定卸不卸」。
    """
    from hall_auto.fetch import cache_dir, share_dirs, share_reachable

    results: list[dict] = []
    shares = share_dirs(cfg)
    reachable_shares: list = []
    for directory in shares:
        ok, why = share_reachable(directory)
        if ok:
            reachable_shares.append(directory)
        results.append({"share": str(directory), "reachable": ok, "why": why})

    if not cfg.installer_dir.is_dir():
        results.append({
            "note": f"installer_dir 不存在：{cfg.installer_dir}",
            "hint": "多机跑批时机器上没人拷包，属正常；但每个包都得能从缓存/共享盘/下载拿到",
        })

    names = configured_filenames(cfg)
    all_ok = True
    for name in names:
        local = cfg.installer_dir / name
        if local.is_file():
            results.append({"filename": name, "available": True, "source": "local"})
            continue
        cached = cache_dir(cfg) / name
        if cached.is_file():
            results.append({"filename": name, "available": True, "source": "cache"})
            continue
        hit = next((d for d in reachable_shares if (d / name).is_file()), None)
        if hit is not None:
            results.append({"filename": name, "available": True, "source": "share", "url": str(hit)})
            continue
        if allow_download:
            # 只有**配置里明确给了地址**才算"能拿到"。默认 CDN 模板（dlcdnets.asus.com）
            # 只是个猜测 —— 组长已两次确认没有官方地址，那些 URL 实际全是 404。
            # 把"猜的地址"当可用，就会把大厅卸掉然后装不回来，正是要防的最坏结果。
            template = str(((cfg.raw or {}).get("download") or {}).get("url_template") or "").strip()
            from hall_auto.fetch import package_url

            url = package_url(cfg, name) if template else ""
            results.append({
                "filename": name,
                "available": bool(template),
                "source": "download" if template else "none",
                "url": url,
                "error": "" if template else (
                    "本地、缓存、共享盘都没有，且下载地址未配置（download.url_template 为空）"
                    "——没有可用的获取路径，拒绝卸载"
                ),
                "hint": "" if template else "先补包：放到 installer_dir、或接通共享盘、或让组长给下载地址",
            })
            if not template:
                all_ok = False
            continue
        results.append({
            "filename": name,
            "available": False,
            "source": "none",
            "error": "本地、缓存、共享盘都没有，且本轮禁止下载",
        })
        all_ok = False
    return all_ok, results


def reset_before_run(
    cfg: Config,
    *,
    allow_download: bool = True,
    allow_uninstall: bool = False,
    require_packages: bool = True,
) -> dict:
    """跑批前的机器复位：检测已装的大厅，有就卸掉，并把要用的包备齐。

    组长 2026-09-18 要求：「不论装没装对应大厅，都先检测，有检测到就先删掉，重新下载」。
    这个函数就是那句话的落地。**卸载是不可逆的，所以顺序很关键**：

      1. 先**只读**探测三个包能不能拿到（本地 → 缓存 → 共享盘 → 下载）
      2. 拿不到 → **拒绝卸载**，报 `ResetBlocked`，机器保持原样
      3. 拿得到 → 才真卸

    早先的版本是「先卸再取包」，一旦包取不到，机器就停在"大厅被卸掉且装不回来"，
    对跑批是最坏结果。顺序反了。

    `allow_uninstall=False`（默认）时，检测到已装大厅也**不卸**，只报告 ——
    办公机上误跑一次就少一个大厅。要真卸必须显式给 `allow_uninstall=True`
    或设 `HALL_ALLOW_INSTALL=1`（与 pytest 门禁同口径）。

    返回复位报告（写进证据），不改任何配置。
    """
    import time as _time

    report: dict = {
        "started_at": _time.strftime("%Y-%m-%dT%H:%M:%S"),
        "installer_dir": str(cfg.installer_dir),
        "steps": [],
    }

    # ---- 第 1 步（只读）：包能不能拿到？拿不到就别动现有安装 ----
    if require_packages:
        packs_ok, pack_results = check_packages_available(cfg, allow_download=allow_download)
        report["precheck"] = pack_results
        report["packages_precheck_ok"] = packs_ok
        if not packs_ok:
            missing = [p.get("filename") for p in pack_results if p.get("available") is False]
            report["ok"] = False
            report["blocked"] = True
            report["finished_at"] = _time.strftime("%Y-%m-%dT%H:%M:%S")
            raise ResetBlocked(
                "安装包拿不到，拒绝卸载已装大厅（否则机器会停在卸了装不回来的状态）。"
                f"缺失：{', '.join(m for m in missing if m)}。"
                f"请先补齐包：把包放到 {cfg.installer_dir}，或接通共享盘，或给出下载地址。"
            )

    # ---- 第 2 步：检测已装的大厅 ----
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
        if not allow_uninstall:
            # 门禁未开：只报告，不卸载。**绝不因为"脚本本来就要卸"就默认卸**。
            report["steps"].append({
                "step": "uninstall",
                "ok": True,
                "skipped": True,
                "detail": (
                    "已装大厅但未开卸载闸：加 --allow-uninstall 或设 HALL_ALLOW_INSTALL=1 才会卸。"
                    "（办公机上误跑一次就少一个大厅，所以默认不卸。）"
                ),
            })
        else:
            stop_product(cfg.timeouts.process_stop_sec)
            uninstall_exe = Path(existing.uninstall_string.strip().strip('"'))
            _run_nsis(uninstall_exe, cfg.timeouts.uninstall_sec)
            _wait_until(
                lambda: read_installed() is None,
                cfg.timeouts.version_settle_sec,
                "等待卸载完成（复位）",
            )
            report["steps"].append({"step": "uninstall", "ok": True})

    # ---- 第 3 步：真正把包备齐（此时已确认有路可走）----
    #
    # 注意：**不创建 installer_dir**。早先的版本会 mkdir 出配置里那个复制来的假路径
    # （别人机器上的 C:/Users/xxx/...），建出一个空壳目录，报告还显示 ok —— 假绿。
    # 拿不到包就该失败，不该造一个空目录把失败藏起来。
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
    failed = [p["filename"] for p in fetched if p.get("source") == "failed"]
    if failed:
        report["ok"] = False
        report["error"] = f"安装包获取失败：{', '.join(failed)}"
    else:
        report["ok"] = True
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
