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
import shutil
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

    **匹配按 basename 比**：`update_fixture.package` 是 `fixtures/7z2602-x64.exe` 这种
    带子目录的相对路径，而调用方传的 filename 可能带也可能不带子目录。早先按整串比，
    于是钉住包落到"没配 url_template → 走默认 ASUS CDN → 拼出 .../AppStore/fixtures/7z2602-x64.exe
    → 404"，白等三次重试才失败。
    """
    download = (cfg.raw.get("download") or {}) if isinstance(cfg.raw, dict) else {}
    template = str(download.get("url_template") or "").strip()
    if not template:
        # 兼容历史：update_fixture.url 通常就是直链
        fixture_url = str(cfg.update_fixture.url or "").strip()
        if fixture_url and _same_package(cfg.update_fixture.package, filename):
            return fixture_url
        template = DEFAULT_URL_TEMPLATE
    if "{filename}" in template:
        return template.replace("{filename}", filename)
    return template.rstrip("/") + "/" + filename


def _same_package(configured: str, filename: str) -> bool:
    """配置里的包名与本次要取的 filename 是不是同一个包（按 basename 比，容忍子目录）。"""
    if not configured or not filename:
        return False
    return Path(configured).name == Path(filename).name


def _expected_sha(cfg: Config, filename: str) -> str:
    """配置里有没有给这个文件的期望摘要。没有返回空串（只记录不校验）。"""
    if not isinstance(cfg.raw, dict):
        return ""
    checksums = ((cfg.raw.get("download") or {}).get("sha256") or {})
    if isinstance(checksums, dict):
        got = checksums.get(filename)
        if got:
            return str(got).strip().lower()
    # 更新夹具包自带摘要（同样按 basename 比，见 `_same_package` 的说明）
    if _same_package(cfg.update_fixture.package, filename):
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

# 共享盘拷贝的重试次数（只重试"读共享盘 + 写 .part"这段）。理由见 `_atomic_replace`。
SHARE_COPY_RETRIES = 3

# `.part` -> 正式名 的改名重试次数。改名自带瞬时锁重试，所以拷贝循环不用再套一层。
ATOMIC_REPLACE_ATTEMPTS = 5

# ---- 本机杀软误杀的判定（见 项目知识库/运行手册.md「坑 6」）----
#
# Windows Defender 的 ML 判定会把**进程刚写出的文件**判成 `Trojan:Win32/Bearfoos.A!ml`，
# 此后读它返回 `GetLastError=225 ERROR_VIRUS_INFECTED`。Python 的 errno 表里没有 225，
# 映射成 `EINVAL(22)`，而 `winerror` 是 `None` —— 于是栈上只剩一句
# `OSError: [Errno 22] Invalid argument`，**看着像参数写错了**。
#
# 判据取"errno 22 或 winerror 225"是有意放宽的：真码 `winerror` 只在用 ctypes 调
# `CreateFileW` 时才拿得到，走 `open()` 拿不到。宁可多认几个，也别让一线去改拷贝逻辑 ——
# 2026-09-21 就有人在"共享盘那个文件是不是坏了"上白查了两轮。
AV_ERRNO = 22
AV_WINERROR = 225


def looks_like_av_block(exc: BaseException | None) -> bool:
    """这个异常像不像「杀软把刚写出的文件拦了」。"""
    if exc is None:
        return False
    if getattr(exc, "winerror", None) == AV_WINERROR:
        return True
    return getattr(exc, "errno", None) == AV_ERRNO


def _av_advice(src: Path) -> str:
    """杀软误杀时追加的排查指引。**不改变异常类型，只把话说清楚。**"""
    return (
        f"\n   ⚠️ 这多半**不是**共享盘坏了、也不是拷贝代码的问题："
        f"Windows Defender 会把进程刚写出的文件判成病毒（`Trojan:Win32/Bearfoos.A!ml`），"
        f"此后读它返回 `GetLastError=225`，Python 只显示成 `[Errno 22] Invalid argument`。"
        f"\n   报错里的 `{src}` 是**本机刚写出来的临时文件**（不是共享盘那份），所以查共享盘是白查。"
        f"\n   先确认：`.venv\\Scripts\\python.exe -X utf8 tools\\probe_av_quarantine.py`（只读，只看 `[1]` 段）。"
        f"\n   探针报 225 → **先更新病毒库再复探**（误报跟着病毒库版本走：2026-09-21 那批库误报，"
        f"22:07 更新后不再复现）；更新后仍报 225，才关实时保护 / 加排除项。"
        f"\n   探针干净 → 本机现在没这个毛病，这条 EINVAL 要按真代码 bug 查，别再往杀软上想。"
        f"\n   完整链路见 `项目知识库/运行手册.md`「坑 6」。"
    )


# Windows「瞬时文件锁」的 winerror 码。这些不是错误状态，是别的进程**短暂**持有句柄：
#   5    拒绝访问   —— 杀软实时保护扫刚 close 的文件、索引服务
#   32   文件被占用 —— 同上，或另一进程正好在读
#   33   文件被锁区间
#   1224 文件有用户映射 —— 映射未撤干净
_TRANSIENT_LOCK_WINERRORS = frozenset({5, 32, 33, 1224})


def _is_transient_lock(exc: OSError) -> bool:
    """这个 OSError 是不是「等一会儿就好」的瞬时锁。

    注意：Windows 上 `OSError.winerror` **总是存在**，手工构造或非系统调用产生的
    异常里它是 `None`。所以必须判 `is not None`，不能只判 `hasattr`。
    """
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return winerror in _TRANSIENT_LOCK_WINERRORS
    # 没有 winerror：非 Windows（POSIX 只有 errno），用异常类型兜底
    return isinstance(exc, PermissionError)


def _atomic_replace(part: Path, dest: Path, attempts: int = ATOMIC_REPLACE_ATTEMPTS) -> None:
    """把 `.part` 原子改名成正式名，对 Windows 瞬时锁退避重试。

    **为什么必须重试**：Windows 上杀软实时保护会在文件 close 之后短暂持有句柄
    （索引服务同理），此时 `os.replace` 报 WinError 5/32。这不是失败状态，
    等几十毫秒就过去了。没有重试时，**一次瞬时锁 = 整条取包链硬失败**。

    这正是 2026-09-18 那次 `test_ensure_from_share_copies_and_verifies` 偶发失败的根因：
    下载路径（`_download`）对 OSError 有 3 次重试所以自愈，而共享盘拷贝一次都不重试，
    两条路径对同一种瞬时错误的行为不一致。
    """
    delay = 0.05
    for attempt in range(1, attempts + 1):
        try:
            os.replace(part, dest)
            return
        except OSError as exc:
            if not _is_transient_lock(exc) or attempt == attempts:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 1.0)


def _copy_from_share(src: Path, dest: Path) -> None:
    """从共享盘拷到本地缓存。走 .part 再原子改名（与下载同一套约定）。

    **必须自己建 dest 的父目录**：包名可能带子路径（如 `fixtures/7z2602-x64.exe`），
    缓存里对应的是 `_cache/fixtures/...`，而调用方只建了 `_cache`。
    不建的话 `part.open("wb")` 直接 `FileNotFoundError` —— 表现是
    "共享盘明明有这个文件，却报拷贝失败"，极具迷惑性。

    **必须重试**（与 `_download` 对齐）：读共享盘可能撞上瞬时错误
    （SMB 会话重建、UNC 短暂不可达），写 `.part` 可能撞上杀软扫到一半的句柄。
    退避重试就能过；改名那一步由 `_atomic_replace` 自己重试，不在这里重拷一遍。
    """
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        part.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FetchError(f"缓存目录不可写 {part.parent}：{exc}") from exc

    last: Exception | None = None
    for attempt in range(1, SHARE_COPY_RETRIES + 1):
        try:
            with src.open("rb") as fh_in, part.open("wb") as fh_out:
                while True:
                    chunk = fh_in.read(SHARE_COPY_CHUNK)
                    if not chunk:
                        break
                    fh_out.write(chunk)
                fh_out.flush()
                os.fsync(fh_out.fileno())
            break
        except OSError as exc:
            last = exc
            part.unlink(missing_ok=True)
            if attempt < SHARE_COPY_RETRIES:
                time.sleep(min(0.2 * attempt, 1.0))
    else:
        # 杀软误杀时把话说清楚 —— 否则 `[Errno 22]` 会把人引去查共享盘 / 改拷贝逻辑（见坑 6）。
        advice = _av_advice(src) if looks_like_av_block(last) else ""
        raise FetchError(f"从共享盘拷贝失败 {src}：{last}{advice}") from last

    try:
        _atomic_replace(part, dest)
    except OSError as exc:
        part.unlink(missing_ok=True)
        advice = _av_advice(part) if looks_like_av_block(exc) else ""
        raise FetchError(f"从共享盘拷贝失败 {src}：{exc}{advice}") from exc


def share_dirs(cfg: Config) -> list[Path]:
    """配置里声明的共享盘目录，按优先级排序。

    `package_share.dirs` 可为字符串或列表（多个共享盘做冗余）。
    环境变量 `HALL_PACKAGE_SHARE` 覆盖，便于单机临时切换而不用改配置。

    例：
        package_share:
          dirs:
            - "//LAPTOP-VS5F7HF4/hall-packages"
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


def copy_tree_from_share(cfg: Config, rel_dir: str, dest_dir: Path) -> tuple[int, str]:
    """把共享盘上的**整个子目录**拷到本地。返回 (拷贝文件数, 说明)。

    与 `ensure_from_share` 的分工：那个只管单个安装包文件；这里是**目录型资源**，
    目前唯一的用途是 P2 的内部安全工具（`fixtures/security_tools/`）。

    为什么需要它：内部安全工具按红线**不进公开仓库**，于是每台新机器都只能靠人拷。
    但共享盘不是 git 仓库 —— 包源机上放一次，所有节点就能自动取到，
    把「N 台机器拷 N 次」压成「1 台机器拷 1 次」。

    不做 sha 校验（目录内容不是单一文件）：由调用方按"关键文件在不在"判定完整性，
    所以这里**允许部分拷入**，但把数量返给调用方。共享盘没配 / 不可达 / 没这个目录
    -> `(0, 原因)`，**不抛错**（共享盘挂了不该让整台机器的铺设失败）。
    """
    rel = rel_dir.strip().strip("/\\").replace("\\", "/")
    if not rel:
        return 0, "相对目录为空"
    for directory in share_dirs(cfg):
        ok, _why = share_reachable(directory)
        if not ok:
            continue
        src = directory.joinpath(*rel.split("/"))
        try:
            if not src.is_dir():
                continue
            files = [f for f in src.rglob("*") if f.is_file()]
        except OSError:
            continue  # 中途断了，换下一个共享盘
        if not files:
            continue
        copied = 0
        for item in files:
            target = Path(dest_dir) / item.relative_to(src)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                # 内部工具在共享盘上是**只读**的（copy2 会把只读位带过来），
                # 直接覆盖已有的只读文件会 WinError 5。先解锁再删。
                if target.exists():
                    target.chmod(0o777)
                    target.unlink()
                shutil.copy2(item, target)
                target.chmod(0o777)
            except OSError as exc:
                raise FetchError(f"从共享盘拷贝 {item} 失败：{exc}") from exc
            copied += 1
        return copied, f"{src} -> {dest_dir}"
    return 0, "共享盘未配置 / 不可达 / 没有该目录"


def _download(url: str, dest: Path, timeout_sec: int, retries: int) -> None:
    """下到 dest.part，成功后原子改名。失败重试，重试间隔递增。

    与 `_copy_from_share` 同理：包名可能带子路径，父目录要自己建。
    """
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        part.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FetchError(f"缓存目录不可写 {part.parent}：{exc}") from exc
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
            _atomic_replace(part, dest)  # 同盘原子（内含瞬时锁重试）
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
