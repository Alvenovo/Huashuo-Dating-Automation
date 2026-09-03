from __future__ import annotations

import ctypes
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

import winreg

UNINSTALL_KEY = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\AsusMemberCenter"
UNINSTALL_KEY_32 = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\AsusMemberCenter"
EXE_NAME = "AsusMemberCenter.exe"
DEFAULT_INSTALL_DIR = Path(r"C:\Program Files (x86)\ASUS\ASUS Member Center")

CRITICAL_PROCESS_NAMES = ("AsusMemberCenter",)
HELPER_PROCESS_NAMES = (
    "AppStoreServer",
    "NotifyApp",
    "UpAppNotify",
    "AppCheck",
)
PROCESS_NAMES = CRITICAL_PROCESS_NAMES + HELPER_PROCESS_NAMES


@dataclass(frozen=True)
class InstalledProduct:
    display_name: str
    display_version: str
    uninstall_string: str
    install_dir: Path
    exe_path: Path
    file_version: str | None
    key_path: str


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def _query_values(hive: int, subkey: str) -> dict[str, str] | None:
    try:
        with winreg.OpenKey(hive, subkey) as key:
            data: dict[str, str] = {}
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                data[str(name)] = "" if value is None else str(value)
                i += 1
            return data
    except FileNotFoundError:
        return None


def _file_version(path: Path) -> str | None:
    if not path.is_file():
        return None
    GetFileVersionInfoSizeW = ctypes.windll.version.GetFileVersionInfoSizeW
    GetFileVersionInfoW = ctypes.windll.version.GetFileVersionInfoW
    VerQueryValueW = ctypes.windll.version.VerQueryValueW

    size = GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    if not GetFileVersionInfoW(str(path), 0, size, buf):
        return None

    class VS_FIXEDFILEINFO(ctypes.Structure):
        _fields_ = [
            ("dwSignature", wintypes.DWORD),
            ("dwStrucVersion", wintypes.DWORD),
            ("dwFileVersionMS", wintypes.DWORD),
            ("dwFileVersionLS", wintypes.DWORD),
            ("dwProductVersionMS", wintypes.DWORD),
            ("dwProductVersionLS", wintypes.DWORD),
            ("dwFileFlagsMask", wintypes.DWORD),
            ("dwFileFlags", wintypes.DWORD),
            ("dwFileOS", wintypes.DWORD),
            ("dwFileType", wintypes.DWORD),
            ("dwFileSubtype", wintypes.DWORD),
            ("dwFileDateMS", wintypes.DWORD),
            ("dwFileDateLS", wintypes.DWORD),
        ]

    lptr = ctypes.c_void_p()
    llen = wintypes.UINT()
    if not VerQueryValueW(buf, "\\", ctypes.byref(lptr), ctypes.byref(llen)):
        return None
    info = ctypes.cast(lptr, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
    return "{}.{}.{}.{}".format(
        info.dwFileVersionMS >> 16,
        info.dwFileVersionMS & 0xFFFF,
        info.dwFileVersionLS >> 16,
        info.dwFileVersionLS & 0xFFFF,
    )


def _install_dir_from(values: dict[str, str]) -> Path:
    loc = (values.get("InstallLocation") or "").strip().strip('"')
    if loc:
        return Path(loc)
    icon = (values.get("DisplayIcon") or "").strip().strip('"')
    if icon:
        return Path(icon).parent
    return DEFAULT_INSTALL_DIR


def read_installed() -> InstalledProduct | None:
    for hive, subkey in (
        (winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY),
        (winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY_32),
    ):
        values = _query_values(hive, subkey)
        if not values:
            continue
        install_dir = _install_dir_from(values)
        exe_path = install_dir / EXE_NAME
        hive_name = "HKLM"
        return InstalledProduct(
            display_name=values.get("DisplayName") or "",
            display_version=(values.get("DisplayVersion") or "").strip(),
            uninstall_string=values.get("UninstallString") or values.get("QuietUninstallString") or "",
            install_dir=install_dir,
            exe_path=exe_path,
            file_version=_file_version(exe_path),
            key_path=f"{hive_name}\\{subkey}",
        )
    return None


def image_running(stem: str) -> bool:
    target = stem.lower().removesuffix(".exe")
    return any(name.lower() == target for name in _running_image_names())


def _running_image_names() -> set[str]:
    result = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        check=False,
    )
    names: set[str] = set()
    for line in result.stdout.splitlines():
        if not line.startswith('"'):
            continue
        image = line.split(",")[0].strip('"')
        if image.lower().endswith(".exe"):
            names.add(image[:-4])
        else:
            names.add(image)
    return names


def _kill_image(name: str) -> None:
    for args in (
        ["taskkill", "/IM", f"{name}.exe", "/F", "/T"],
        ["taskkill", "/IM", f"{name}.exe", "/F"],
    ):
        subprocess.run(args, capture_output=True, check=False)


def stop_main_process(timeout_sec: int = 30) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if "AsusMemberCenter" not in _running_image_names():
            return
        _kill_image("AsusMemberCenter")
        time.sleep(1)
    if "AsusMemberCenter" in _running_image_names():
        raise TimeoutError("未能结束主进程 AsusMemberCenter")


def stop_product(timeout_sec: int = 30) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        running = [name for name in PROCESS_NAMES if name in _running_image_names()]
        if not running:
            return
        for name in running:
            _kill_image(name)
        time.sleep(1)
    leftover_critical = [
        name for name in CRITICAL_PROCESS_NAMES if name in _running_image_names()
    ]
    if leftover_critical:
        raise TimeoutError(f"未能结束主进程: {leftover_critical}")
