"""P1-A 夹具复位：把夹具应用静默卸掉，给真装真卸用例一个干净起点。

必须在管理员终端里跑（卸载程序要写 HKLM）。走注册表 UninstallString，不经过大厅界面 ——
这是准备动作，不是被测流程；被测的卸载仍由 tests/launch/test_p1_apps.py 从大厅里点。
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
import time
import winreg
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hall_auto.apps import UNINSTALL_ROOTS, installed_match, installer_pids, uninstall_entry  # noqa: E402
from hall_auto.config import load_config  # noqa: E402


def kill_leftover_installers() -> None:
    """清掉上一轮残留的厂商安装器进程。

    不清的话 install_fixture 会以「向导还挂着」直接拒绝开跑；
    而这种进程非提权 taskkill 报拒绝访问，只能在这个提权脚本里收。
    """
    for pid in sorted(installer_pids()):
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/F", "/T"],
            capture_output=True, text=True, errors="replace", check=False,
        )
        print(f"残留安装器 pid={pid} taskkill rc={result.returncode} "
              f"{(result.stdout or result.stderr or '').strip()[:120]}")


def _entry(app_name: str) -> tuple[str, str] | None:
    matched = installed_match(app_name)
    if not matched:
        return None
    for hive, root in UNINSTALL_ROOTS:
        try:
            with winreg.OpenKey(hive, root) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        sub = winreg.EnumKey(key, i)
                        with winreg.OpenKey(key, sub) as item:
                            name, _ = winreg.QueryValueEx(item, "DisplayName")
                            if str(name) != matched:
                                continue
                            command, _ = winreg.QueryValueEx(item, "UninstallString")
                    except OSError:
                        continue
                    return matched, str(command)
        except OSError:
            continue
    return None


def reset(app_name: str, timeout_sec: int = 300) -> bool:
    found = _entry(app_name)
    if found is None:
        print(f"{app_name}: 本来就没装，起点已干净")
        return True
    matched, command = found
    exe, _, args = command.strip('"').partition('" ')
    argv = [exe, *args.split()] if args.strip() else [exe]
    # NSIS 卸载器认 /S；不认的话它会照常弹窗，人工点掉也算复位成功。
    if "/S" not in argv:
        argv.append("/S")
    print(f"{matched}: 静默卸载 {argv}")
    subprocess.run(argv, check=False)
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not installed_match(app_name):
            print(f"{matched}: 注册表已清")
            return True
        time.sleep(3)
    print(f"{matched}: 卸载超时，注册表里还在")
    return False


def reset_update_fixture(cfg, timeout_sec: int = 180) -> bool:
    """把更新夹具压回钉住的老版本：装了别的版本就先静默卸，再静默装钉住包。

    大厅目录永远比钉住版本新一代（实测 26.02 对目录 26.03），所以复位完更新列表必有它。
    """
    uf = cfg.update_fixture
    if not uf.configured:
        print("没配更新夹具，跳过")
        return True
    package = cfg.update_package_path
    if not package.is_file():
        print(f"{uf.name}: 钉住安装包不存在 {package}（官方源 {uf.url}）")
        return False

    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    if uf.sha256 and digest != uf.sha256:
        print(f"{uf.name}: 安装包 SHA256 不符（{digest}），不装")
        return False

    entry = uninstall_entry(uf.registry_hint)
    if entry and entry["version"] == uf.pinned_version:
        print(f"{uf.name}: 已是钉住版本 {uf.pinned_version}，起点干净")
        return True
    if entry:
        command = entry["quiet"] or entry["uninstall"]
        exe, _, args = command.strip('"').partition('" ')
        argv = [exe, *args.split()] if args.strip() else [exe]
        if "/S" not in argv:
            argv.append("/S")
        print(f"{uf.name}: 当前 {entry['version']}，静默卸载 {argv}")
        subprocess.run(argv, check=False)
        deadline = time.time() + timeout_sec
        while time.time() < deadline and uninstall_entry(uf.registry_hint):
            time.sleep(3)
        if uninstall_entry(uf.registry_hint):
            print(f"{uf.name}: 卸载超时")
            return False
    print(f"{uf.name}: 静默装钉住版本 {uf.pinned_version}")
    subprocess.run([str(package), "/S"], check=False)
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        entry = uninstall_entry(uf.registry_hint)
        if entry and entry["version"] == uf.pinned_version:
            print(f"{uf.name}: 复位完成 {entry}")
            return True
        time.sleep(3)
    print(f"{uf.name}: 装钉住版本超时，注册表 {uninstall_entry(uf.registry_hint)}")
    return False


_NEED_ADMIN_HINT = """\
需要管理员权限：复位夹具要写 HKLM，而**卸载程序本身是提权进程** ——
非提权进程 `CreateProcess` 直接失败（WinError 740 请求的操作需要提升）。

  做法（二选一）：
    ① 用**管理员** PowerShell 重跑本脚本；
    ② 让跑批的提权段去做 —— 投 `--suites apps-lifecycle` 会自动带这一步（零人工）。

  注意：机器本来就干净时**不需要管理员**（本脚本会直接报「起点干净」）。
  触发这一条说明**有东西要卸/要杀**，那就必须提权。
"""


def _is_needs_elevation(exc: OSError) -> bool:
    """WinError 740 = ERROR_ELEVATION_REQUIRED：**被启动的那个程序**要求提权。

    ⚠️ 是"被启动的程序要提权"，不是"我们权限不够" —— 厂商卸载器自带
    `requireAdministrator` 清单，非提权进程连 `CreateProcess` 都过不去。
    所以这个错误**必须翻成一句人话**，不能让一线看到裸 traceback 猜。
    """
    return getattr(exc, "winerror", None) == 740


def main() -> int:
    try:
        kill_leftover_installers()
        cfg = load_config()
        targets = {name for name in (cfg.fixture_apps.install, cfg.fixture_apps.uninstall) if name}
        results = [reset(name) for name in sorted(targets)]
        results.append(reset_update_fixture(cfg))
        return 0 if all(results) else 1
    except OSError as exc:
        # 2026-09-23 真机：非管理员跑（或机器上有东西要卸）时裸抛 traceback，
        # 而 docstring 早就写着「必须在管理员终端里跑」—— 写了约束却没检查，等于没写。
        if not _is_needs_elevation(exc):
            raise
        print(_NEED_ADMIN_HINT, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
