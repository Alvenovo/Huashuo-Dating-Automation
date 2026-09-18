"""安装包获取：本地夹具目录优先，缺失则从官方地址下载到本地缓存。

## 为什么需要这个模块

多机跑批时，安装包有两种来源：

1. **本地夹具目录**（`installer_dir`）：本机开发时手工放的，或从共享盘拷来的。
   这是过去唯一的路子，每台机器靠人拷，20 台就是 20 次手工操作。
2. **官方地址下载**：组长 2026-09-18 明确要求「我想执行脚本在测试机自动下载对应大厅」。
   脚本自己把包拉下来，机器到位就能跑，不用人到每台机器上拷。

本模块把这两条路统一成 `ensure_package()`：**本地有就用本地的（快、且能钉版本），
本地没有才下载**。本地优先是为了不动既有跑法——本机开发时把包放好，行为跟以前完全一样。

## 红线

- **下载只下白名单语言**的包（默认 `zh-CN`）。官方 CDN 上有多个语言版本，下错语言
  会让「关于页版本」「更新提示文案」这类断言全部失配。
- **校验 sha256**（配置里给了期望值就校验，没给只记录实际值）。下载中断会产生
  半截包，NSIS `/S` 对半截包的行为未定义——静默装一半比失败更糟。
- **不覆盖已有文件**（除 `.part` 临时文件）。缓存命中直接返回，避免 20 台机器
  同时下载时互相踩踏共享盘上的同一份包。

## 缓存布局

    <installer_dir>/_cache/<文件名>
    <installer_dir>/_cache/<文件名>.part     下载中的临时文件

`.part` 下完校验通过才 rename 成正式名（同盘 rename 原子），所以并发场景下
别的进程永远看不到半截包。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from hall_auto.config import Config

# 默认下载源。官方大厅安装包 CDN，按语言区分。
DEFAULT_URL_TEMPLATE = (
    "https://dlcdnets.asus.com/pub/ASUS/AppStore/{filename}"
)

# 只下这个语言的包。改这里之前先确认大厅「关于页」与更新文案的语言断言。
DEFAULT_ACCEPT_LANGUAGE = "zh-CN"

CHUNK = 1 << 20  # 1 MiB
DEFAULT_TIMEOUT_SEC = 60
DEFAULT_RETRIES = 3


class FetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class FetchResult:
    """一个安装包的获取结果。`source` 告诉调用方这包是哪来的，写进证据。"""

    path: Path
    filename: str
    source: str  # "local" / "cache" / "download" / "failed"
    sha256: str = ""
    bytes: int = 0
    url: str = ""
    error: str = ""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cache_dir(cfg: Config) -> Path:
    """安装包下载缓存目录。跟 installer_dir 同盘，保证 rename 是原子的。"""
    raw = os.environ.get("HALL_PACKAGE_CACHE", "").strip()
    base = Path(raw) if raw else cfg.installer_dir / "_cache"
    return base


def package_url(cfg: Config, filename: str) -> str:
    """拼下载地址。

    配置里 `download.url_template` 优先（支持 `{filename}` 占位）；没配走默认 CDN。
    `update_fixture.url` 若与本 filename 匹配也认——历史配置里钉过更新包直链。
    """
    download = (cfg.raw.get("download") or {}) if isinstance(cfg.raw, dict) else {}
    template = str(download.get("url_template") or "").strip()
    if not template:
        # 兼容历史：update_fixture.url 通常就是直链
        fixture_url = str(cfg.update_fixture.url or "").strip()
        if fixture_url and cfg.update_fixture.package and Path(cfg.update_fixture.package).name == filename:
            return fixture_url
        template = DEFAULT_URL_TEMPLATE
    if "{filename}" in template:
        return template.replace("{filename}", filename)
    return template.rstrip("/") + "/" + filename


def _expected_sha(cfg: Config, filename: str) -> str:
    """配置里有没有给这个文件的期望摘要。没有返回空串（只记录不校验）。"""
    if not isinstance(cfg.raw, dict):
        return ""
    checksums = ((cfg.raw.get("download") or {}).get("sha256") or {})
    if isinstance(checksums, dict):
        got = checksums.get(filename)
        if got:
            return str(got).strip().lower()
    # 更新夹具包自带摘要
    if cfg.update_fixture.package and Path(cfg.update_fixture.package).name == filename:
        return str(cfg.update_fixture.sha256 or "").strip().lower()
    return ""


def _verify(path: Path, expected: str) -> str:
    actual = sha256_of(path)
    if expected and actual != expected:
        raise FetchError(
            f"{path.name} sha256 不符：期望 {expected[:16]}… 实际 {actual[:16]}…（下载损坏或被替换）"
        )
    return actual


# ---------------- 共享盘（SMB / UNC）----------------
#
# UNC 路径（\\host\share\...）和本地路径的失败形态不一样，是这里最需要小心的点：
#   - 网络不通 / 主机名解析失败 / 凭据不对 → OSError（WinError 53/64/1326/1231…）
#   - 本地路径不存在                        → FileNotFoundError（是 OSError 的子类）
# 所以判断存在性必须 catch OSError，不能只 catch FileNotFoundError，否则一条断网就崩。
# is_file()/stat() 在网络路径上还可能**卡住**（默认 SMB 超时可达 20s+），
# 因此共享盘必须放在「本地之后、下载之前」，且探测前先做一次轻量可达性检查。

# 共享盘探测/拷贝的超时（秒）。SMB 自身超时由系统控制，这里只兜底整体耗时。
SHARE_PROBE_SEC = 5
SHARE_COPY_CHUNK = 1 << 20


def share_dirs(cfg: Config) -> list[Path]:
    """配置里声明的共享盘目录，按优先级排序。

    `package_share.dirs` 可为字符串或列表（多个共享盘做冗余）。
    环境变量 `HALL_PACKAGE_SHARE` 覆盖，便于单机临时切换而不用改配置。

    例：
        package_share:
          dirs:
            - "//192.168.0.4/hall-packages"
            - "//LAPTOP-VS5F7HF4/hall-packages"
    """
    env = os.environ.get("HALL_PACKAGE_SHARE", "").strip()
    raw: object = env
    if not env and isinstance(cfg.raw, dict):
        raw = (cfg.raw.get("package_share") or {}).get("dirs")

    if not raw:
        return []
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        return []
    return [Path(str(x).strip()) for x in items if str(x).strip()]


def share_reachable(directory: Path, timeout_sec: int = SHARE_PROBE_SEC) -> tuple[bool, str]:
    """探测共享目录是否可达。返回 (是否可达, 说明)。

    **必须带超时**：网络盘 is_dir() 在断网时可能卡 20s+，逐个包都卡一次会让跑批变得极慢。
    用线程包一层，超时即判不可达 —— 宁可当它没有（后面还有下载兜底），也别卡住。
    """
    import threading

    result: dict = {"ok": False, "why": ""}

    def _probe() -> None:
        try:
            result["ok"] = directory.is_dir()
            if not result["ok"]:
                result["why"] = "目录不存在或不是目录"
        except OSError as exc:
            result["why"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=_probe, daemon=True)
    thread.start()
    thread.join(timeout=timeout_sec)
    if thread.is_alive():
        return False, f"探测超时（{timeout_sec}s），判定不可达"
    return result["ok"], result["why"]


def _copy_from_share(src: Path, dest: Path) -> None:
    """从共享盘拷到本地缓存。走 .part 再原子改名（与下载同一套约定）。"""
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        with src.open("rb") as fh_in, part.open("wb") as fh_out:
            while True:
                chunk = fh_in.read(SHARE_COPY_CHUNK)
                if not chunk:
                    break
                fh_out.write(chunk)
            fh_out.flush()
            os.fsync(fh_out.fileno())
        os.replace(part, dest)
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise FetchError(f"从共享盘拷贝失败 {src}：{exc}") from exc


def ensure_from_share(cfg: Config, filename: str) -> FetchResult | None:
    """尝试从共享盘取包并拷进本地缓存。

    返回 None 表示「共享盘没配 / 都不可达 / 都没有这个包」——**不抛错**，
    交给调用方继续走下载兜底。共享盘是"能省流量就省"，不是必须成功的路径。
    """
    for directory in share_dirs(cfg):
        ok, why = share_reachable(directory)
        if not ok:
            continue
        candidate = directory / filename
        try:
            if not candidate.is_file():
                continue
            size = candidate.stat().st_size
        except OSError:
            continue  # 中途断了，换下一个共享盘

        dest_dir = cache_dir(cfg)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise FetchError(f"缓存目录不可写 {dest_dir}：{exc}") from exc

        dest = dest_dir / filename
        _copy_from_share(candidate, dest)
        digest = _verify(dest, _expected_sha(cfg, filename))
        return FetchResult(
            path=dest,
            filename=filename,
            source="share",
            sha256=digest,
            bytes=size,
            url=str(candidate),
        )
    return None


def _download(url: str, dest: Path, timeout_sec: int, retries: int) -> None:
    """下到 dest.part，成功后原子改名。失败重试，重试间隔递增。"""
    part = dest.with_suffix(dest.suffix + ".part")
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "HallAuto/1.0 (+qa)",
                    "Accept-Language": DEFAULT_ACCEPT_LANGUAGE,
                },
            )
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp, part.open("wb") as fh:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(part, dest)  # 同盘原子
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            part.unlink(missing_ok=True)
            if attempt < retries:
                time.sleep(min(2 ** attempt, 15))
    raise FetchError(f"下载失败（试了 {retries} 次）：{url} -> {last}")


def ensure_package(
    cfg: Config,
    filename: str,
    *,
    allow_download: bool = True,
    allow_share: bool = True,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    retries: int = DEFAULT_RETRIES,
) -> FetchResult:
    """确保拿到安装包，返回它的路径与来源。

    查找顺序：
      1. `installer_dir/<文件名>`   —— 本地夹具目录
      2. `_cache/<文件名>`          —— 本地缓存（已下过/拷过）
      3. 共享盘（若配了 `package_share.dirs`）—— 拷进缓存，省外网流量
      4. 官方下载                    —— 最后兜底

    1/2 命中都不碰网络。3 不可达就静默跳过，绝不因为共享盘挂了让整轮跑批失败。
    `allow_download=False` 时 4 被禁（`allow_share=False` 同理禁 3）。
    """
    if not filename:
        raise FetchError("文件名为空")

    local = cfg.installer_dir / filename
    if local.is_file():
        return FetchResult(
            path=local,
            filename=filename,
            source="local",
            sha256=sha256_of(local),
            bytes=local.stat().st_size,
        )

    cached = cache_dir(cfg) / filename
    if cached.is_file():
        try:
            digest = _verify(cached, _expected_sha(cfg, filename))
        except FetchError:
            cached.unlink(missing_ok=True)  # 缓存坏了就当没有，重新取
        else:
            return FetchResult(
                path=cached,
                filename=filename,
                source="cache",
                sha256=digest,
                bytes=cached.stat().st_size,
            )

    if allow_share:
        got = ensure_from_share(cfg, filename)
        if got is not None:
            return got

    if not allow_download:
        raise FetchError(
            f"{filename} 本地/缓存/共享盘都没有，且本轮禁止下载（installer_dir={cfg.installer_dir}）"
        )

    dest_dir = cache_dir(cfg)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    url = package_url(cfg, filename)
    _download(url, dest, timeout_sec, retries)
    digest = _verify(dest, _expected_sha(cfg, filename))
    return FetchResult(
        path=dest,
        filename=filename,
        source="download",
        sha256=digest,
        bytes=dest.stat().st_size,
        url=url,
    )


def ensure_all(
    cfg: Config,
    filenames: list[str],
    *,
    allow_download: bool = True,
    allow_share: bool = True,
) -> list[FetchResult]:
    """批量获取。逐个独立，一个失败不影响其他（调用方拿 error 字段自己决定）。"""
    out: list[FetchResult] = []
    for name in filenames:
        if not name:
            continue
        try:
            out.append(
                ensure_package(cfg, name, allow_download=allow_download, allow_share=allow_share)
            )
        except FetchError as exc:
            out.append(FetchResult(path=Path(), filename=name, source="failed", error=str(exc)))
    return out


def configured_filenames(cfg: Config) -> list[str]:
    """配置里声明要用的全部安装包文件名（去重保序）。"""
    names: list[str] = []
    for candidate in (
        cfg.baseline_setup,
        cfg.latest_setup,
        cfg.update_fixture.package,
        (cfg.raw.get("download") or {}).get("packages") if isinstance(cfg.raw, dict) else None,
    ):
        if isinstance(candidate, (list, tuple)):
            for item in candidate:
                if item and str(item) not in names:
                    names.append(str(item))
        elif candidate and str(candidate) not in names:
            names.append(str(candidate))
    return names
