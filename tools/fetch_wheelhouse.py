"""在有外网的机器上跑一次，把离线装依赖要的东西全部下下来。

    .venv\\Scripts\\python.exe -X utf8 tools\\fetch_wheelhouse.py
    .venv\\Scripts\\python.exe -X utf8 tools\\fetch_wheelhouse.py --to "\\\\LAPTOP-VS5F7HF4\\hall-packages\\wheelhouse"
    .venv\\Scripts\\python.exe -X utf8 tools\\fetch_wheelhouse.py --with-python-installer

## 为什么需要它

测试机**没有外网**。没有外网就意味着：
- 装不了依赖（pip 连不上源）
- 也下不了 Python 安装包本身

原先给的两条绕法都不够用：
1. 「用内网镜像」—— 不一定有；
2. 「整份拷 `.venv` + `--skip-venv`」—— **脆**：`.venv/pyvenv.cfg` 里的 `home`
   指向原机器的 Python 安装路径，`.venv/Scripts/*.exe` 这些启动器里又写死了原 venv
   的绝对路径。新机器上用户名或 Python 安装位置只要有一处不同，拷过去的 venv 就起不来。

所以改成**把 wheel 搬过去、在新机器上本机建 venv**：路径问题根本不存在，
装出来的环境也跟有网机器一模一样（版本锁定，不受镜像源当时有什么影响）。

## 产物

    <dest>/                        依赖的全部 .whl（含传递依赖）
    <dest>/wheelhouse.json         清单：Python 版本、requirements 哈希、文件列表
    <dest>/python-3.12.10-amd64.exe   （只有加了 --with-python-installer 才有）

把整个目录放到共享盘 `hall-packages\\wheelhouse\\`，测试机跑 bootstrap 时会自动取。

## ⚠️ wheel 是跟 Python 版本绑死的

`cp312` 的 wheel 装不进 Python 3.13。所以清单里记了 Python 版本，
bootstrap 会比对；不一致会明确报出来，而不是丢一堆 pip 的编译错误给一线看。
**新机器上的 Python 版本要和这里跑的一致**（都是 3.12 就行，小版本无所谓）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from hall_auto.wheelhouse import (  # noqa: E402
    MANIFEST_NAME,
    sha256_of,
    write_manifest,
)

REQUIREMENTS = REPO_ROOT / "requirements.txt"

# python.org 的安装包地址是固定格式，直接拼
PYTHON_INSTALLER_URL = "https://www.python.org/ftp/python/{ver}/python-{ver}-amd64.exe"


def _download_wheels(dest: Path, python_exe: Path) -> tuple[bool, str]:
    cmd = [
        str(python_exe), "-m", "pip", "download",
        "-r", str(REQUIREMENTS), "-d", str(dest),
        # 强制只收 wheel：拿到 sdist 的话离线机器上还得有编译环境，等于没解决
        "--only-binary=:all:", "--disable-pip-version-check",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "")[-600:]
    return True, ""


def _download_python_installer(dest: Path, ver: str) -> tuple[bool, str]:
    url = PYTHON_INSTALLER_URL.format(ver=ver)
    target = dest / Path(url).name
    cmd = [
        sys.executable, "-c",
        (
            "import sys, urllib.request;"
            "urllib.request.urlretrieve(sys.argv[1], sys.argv[2]);"
            "print('ok')"
        ),
        url, str(target),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not target.is_file():
        return False, f"{url}\n{(proc.stderr or '')[-400:]}"
    return True, str(target)


def main() -> int:
    parser = argparse.ArgumentParser(description="下载离线装依赖所需的 wheel（在有外网的机器上跑）")
    parser.add_argument("--to", default="", help="目标目录（默认 installer_dir/wheelhouse，再不行 ./wheelhouse）")
    parser.add_argument("--with-python-installer", action="store_true",
                        help="顺便下 Python 安装包（无外网机器连 Python 都装不了）")
    args = parser.parse_args()

    if not REQUIREMENTS.is_file():
        print(f"[FAIL] 找不到 {REQUIREMENTS}")
        return 1

    dest = Path(args.to.strip()) if args.to.strip() else (REPO_ROOT / "wheelhouse")
    dest.mkdir(parents=True, exist_ok=True)

    py_xy = f"{sys.version_info.major}.{sys.version_info.minor}"
    print(f"[1/3] 下载 wheel -> {dest}")
    print(f"      用 {sys.executable}（Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}）")
    ok, why = _download_wheels(dest, Path(sys.executable))
    if not ok:
        print(f"[FAIL] pip download 失败：\n{why}")
        return 1

    req_digest = sha256_of(REQUIREMENTS)
    manifest = write_manifest(dest, py_xy, req_digest)
    print(f"[2/3] 清单已写：{dest / MANIFEST_NAME}（{manifest['wheel_count']} 个 wheel，Python {py_xy}）")

    if args.with_python_installer:
        ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        print(f"[3/3] 下载 Python 安装包 {ver} …")
        ok, why = _download_python_installer(dest, ver)
        if ok:
            print(f"      已下：{why}")
        else:
            # 不算致命：wheel 已经拿到了，Python 安装包可以另外找 IT 要
            print(f"      ⚠️ 没下下来（不影响 wheel）：{why}")
    else:
        print("[3/3] 跳过 Python 安装包（要的话加 --with-python-installer）")

    print()
    print(f"完成。把整个 {dest} 目录放到共享盘 hall-packages\\wheelhouse\\ 即可。")
    print("测试机上 bootstrap 第 2 步会**优先**用它离线装（找到就不碰网络）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
