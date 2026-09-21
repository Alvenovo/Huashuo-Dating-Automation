"""锁安装包获取策略（hall_auto/fetch.py）。

多机跑批时包自己下载是核心能力，出错代价是 20 台机器装错包或装半截包。
这里把优先级、校验、原子写、并发安全全部钉死。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from unittest import mock

import pytest

from hall_auto import fetch
from hall_auto.config import load_config


@pytest.fixture
def cfg(tmp_path):
    """用一份最小配置，installer_dir 指向临时目录。

    **必须屏蔽本机的 `config.local.yaml`**：`load_config()` 在读到 `DEFAULT_CONFIG` 时
    会 deep-merge 它，本机一旦真配了 `package_share.dirs`，这条用例就会挂
    （2026-09-18 实际踩过）。用 `HALL_CONFIG` 指向临时空配置，隔离掉本机状态。
    """
    empty = tmp_path / "config.yaml"
    empty.write_text("{}\n", encoding="utf-8")
    env = {"HALL_INSTALLER_DIR": str(tmp_path), "HALL_CONFIG": str(empty)}
    with mock.patch.dict("os.environ", env, clear=False):
        os.environ.pop("HALL_PACKAGE_SHARE", None)
        yield load_config()


def _write(path: Path, content: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


# ---------------- 查找优先级 ----------------


def test_local_wins_over_download(cfg, tmp_path):
    """本地有包就直接用，不联网。这是既有跑法，不能被下载能力改坏。"""
    payload = b"local-setup"
    digest = _write(tmp_path / "myappstore_1.0.0S_Setup.exe", payload)
    with mock.patch.object(fetch, "_download") as dl:
        res = fetch.ensure_package(cfg, "myappstore_1.0.0S_Setup.exe")
    assert res.source == "local"
    assert res.sha256 == digest
    dl.assert_not_called()


def test_cache_wins_over_download(cfg, tmp_path):
    """本地没有但缓存有，用缓存，不联网。"""
    payload = b"cached-setup"
    digest = _write(fetch.cache_dir(cfg) / "myappstore_1.0.0S_Setup.exe", payload)
    with mock.patch.object(fetch, "_download") as dl:
        res = fetch.ensure_package(cfg, "myappstore_1.0.0S_Setup.exe")
    assert res.source == "cache"
    assert res.sha256 == digest
    dl.assert_not_called()


def test_download_when_nothing_local(cfg):
    """本地与缓存都没有才下载。"""
    def fake_download(url, dest, timeout_sec, retries):
        _write(dest, b"downloaded")

    with mock.patch.object(fetch, "_download", side_effect=fake_download) as dl:
        res = fetch.ensure_package(cfg, "myappstore_1.0.0S_Setup.exe")
    assert res.source == "download"
    assert res.path.is_file()
    dl.assert_called_once()


# ---------------- 安全约束 ----------------


def test_allow_download_false_raises(cfg):
    """显式禁止下载时，找不到就报错，不能偷偷联网。"""
    with mock.patch.object(fetch, "_download") as dl:
        with pytest.raises(fetch.FetchError, match="禁止下载"):
            fetch.ensure_package(cfg, "nope.exe", allow_download=False)
    dl.assert_not_called()


def test_sha_mismatch_on_cached_is_discarded(cfg):
    """缓存里的包摘要不对，应当丢掉并重新下，而不是拿坏包去装。"""
    bad = fetch.cache_dir(cfg) / "myappstore_1.0.0S_Setup.exe"
    _write(bad, b"corrupted")
    downloaded: dict = {}

    def fake_download(url, dest, timeout_sec, retries):
        downloaded["called"] = True
        _write(dest, b"good")

    # 期望摘要不匹配任何下载内容 -> 先删坏缓存再下，下完校验不过抛错
    with mock.patch.object(fetch, "_expected_sha", return_value="0" * 64), \
         mock.patch.object(fetch, "_download", side_effect=fake_download):
        with pytest.raises(fetch.FetchError, match="sha256 不符"):
            fetch.ensure_package(cfg, "myappstore_1.0.0S_Setup.exe")
    assert downloaded.get("called") is True, "坏缓存被删后应当重新下载"
    # 坏缓存被删掉、由新下载的内容顶替（证明没直接拿坏包用）
    assert bad.read_bytes() == b"good", "坏缓存应被新下载的内容替换"


def test_mismatch_after_download_raises(cfg):
    """下载完摘要不符必须报错 —— 半截包装进去比失败更糟。"""
    def fake_download(url, dest, timeout_sec, retries):
        _write(dest, b"truncated")

    with mock.patch.object(fetch, "_expected_sha", return_value="f" * 64), \
         mock.patch.object(fetch, "_download", side_effect=fake_download):
        with pytest.raises(fetch.FetchError, match="sha256 不符"):
            fetch.ensure_package(cfg, "myappstore_1.0.0S_Setup.exe")


def test_no_expected_sha_only_records(cfg):
    """没配期望摘要时只记录实际值，不拦（开发期便利）。"""
    def fake_download(url, dest, timeout_sec, retries):
        _write(dest, b"whatever")

    with mock.patch.object(fetch, "_download", side_effect=fake_download):
        res = fetch.ensure_package(cfg, "myappstore_1.0.0S_Setup.exe")
    assert res.sha256 == hashlib.sha256(b"whatever").hexdigest()


def test_part_file_used_not_final(cfg):
    """下载走 .part 再原子改名 —— 别的进程永远看不到半截包。"""
    seen: dict = {}

    def fake_download(url, dest, timeout_sec, retries):
        seen["dest"] = dest
        seen["exists_before_write"] = dest.exists()
        _write(dest.with_suffix(dest.suffix + ".part"), b"x")
        dest.with_suffix(dest.suffix + ".part").replace(dest)

    with mock.patch.object(fetch, "_download", side_effect=fake_download):
        fetch.ensure_package(cfg, "myappstore_1.0.0S_Setup.exe")
    # 传给 _download 的是最终路径，_download 内部用 .part —— 这里只确认最终文件存在
    assert seen["exists_before_write"] is False


# ---------------- URL 与文件名 ----------------


def test_url_template_placeholder(cfg):
    url = fetch.package_url(cfg, "abc.exe")
    assert "abc.exe" in url
    assert url.startswith("https://")


def test_configured_filenames_dedupes(cfg):
    """配置里同一个包出现在多处，只下发一次。"""
    cfg_like = mock.Mock()
    cfg_like.baseline_setup = "a.exe"
    cfg_like.latest_setup = "b.exe"
    cfg_like.update_fixture = mock.Mock(package="a.exe")
    cfg_like.raw = {}
    names = fetch.configured_filenames(cfg_like)
    assert names == ["a.exe", "b.exe"]


def test_ensure_all_isolates_failures(cfg):
    """批量获取时单个失败不影响其他，错误记在各自的 error 字段。"""
    def picker(c, name, **kw):
        if name == "bad.exe":
            raise fetch.FetchError("下不动")
        return fetch.FetchResult(path=Path("ok.exe"), filename=name, source="local")

    with mock.patch.object(fetch, "ensure_package", side_effect=picker):
        results = fetch.ensure_all(cfg, ["good.exe", "bad.exe", "good2.exe"])
    assert [r.source for r in results] == ["local", "failed", "local"]
    assert "下不动" in results[1].error


# ---------------- 共享盘 ----------------


FAKE_CFG_WITH_SHARE = {
    "installer_dir": "",
    "package_share": {"dirs": ["//share-host/hall-packages"]},
}


@pytest.fixture
def cfg_share(tmp_path):
    """带共享盘配置的 Config。"""
    local = tmp_path / "installer"
    local.mkdir()
    data = dict(FAKE_CFG_WITH_SHARE)
    data["installer_dir"] = str(local)
    import yaml
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with mock.patch.dict("os.environ", {"HALL_CONFIG": str(cfg_file)}, clear=False):
        from hall_auto.config import load_config
        # 与 `cfg` 同理：本机/上一个用例残留的这两个变量会把缓存目录指到别处，
        # 断言就会变成「比两个同样被污染的值」，假绿且难查。
        os.environ.pop("HALL_PACKAGE_CACHE", None)
        os.environ.pop("HALL_PACKAGE_SHARE", None)
        yield load_config(cfg_file)


def test_share_dirs_from_config(cfg_share):
    dirs = fetch.share_dirs(cfg_share)
    assert len(dirs) == 1
    assert "share-host" in str(dirs[0])


def test_share_dirs_env_overrides(cfg_share):
    """环境变量优先，便于单机临时切换共享盘。"""
    with mock.patch.dict("os.environ", {"HALL_PACKAGE_SHARE": "//other-host/pkgs"}):
        dirs = fetch.share_dirs(cfg_share)
    assert len(dirs) == 1
    assert "other-host" in str(dirs[0])


def test_share_dirs_empty_when_unset(cfg, tmp_path):
    """没配共享盘时返回空列表，不是报错。"""
    assert fetch.share_dirs(cfg) == []


def test_share_reachable_unreachable_is_not_error():
    """不可达只返回 False，不抛 —— 共享盘是"能省就省"，不是必须路径。"""
    ok, why = fetch.share_reachable(Path("//nonexistent-host-xyz/nope"), timeout_sec=2)
    assert ok is False
    assert why  # 有说明


def test_share_reachable_timeout_does_not_hang():
    """探测必须带超时：断网时 is_dir() 会卡 20s+，不能让它拖死跑批。"""
    import time as _time

    def slow_is_dir(self):
        _time.sleep(10)
        return True

    start = _time.monotonic()
    with mock.patch.object(Path, "is_dir", slow_is_dir):
        ok, why = fetch.share_reachable(Path("//slow/one"), timeout_sec=1)
    elapsed = _time.monotonic() - start
    assert ok is False
    assert "超时" in why
    assert elapsed < 5, f"应当 1s 左右返回，实际 {elapsed:.1f}s"


def test_ensure_from_share_returns_none_when_unreachable(cfg_share):
    """共享盘不可达 -> 返回 None，让调用方继续走下载兜底。"""
    with mock.patch.object(fetch, "share_reachable", return_value=(False, "不通")):
        assert fetch.ensure_from_share(cfg_share, "a.exe") is None


def test_ensure_from_share_returns_none_when_file_absent(cfg_share, tmp_path):
    """共享盘可达但没有这个包 -> None（不是错误）。"""
    share = tmp_path / "share"
    share.mkdir()
    with mock.patch.object(fetch, "share_dirs", return_value=[share]):
        assert fetch.ensure_from_share(cfg_share, "missing.exe") is None


def test_ensure_from_share_copies_and_verifies(cfg_share, tmp_path):
    """共享盘有包 -> 拷进本地缓存，source=share。"""
    share = tmp_path / "share"
    share.mkdir()
    payload = b"from-share"
    (share / "a.exe").write_bytes(payload)

    with mock.patch.object(fetch, "share_dirs", return_value=[share]):
        res = fetch.ensure_from_share(cfg_share, "a.exe")

    assert res is not None
    assert res.source == "share"
    assert res.path.read_bytes() == payload
    assert res.path.parent == fetch.cache_dir(cfg_share)


def test_ensure_package_prefers_share_over_download(cfg_share, tmp_path):
    """共享盘有 -> 不联网下载。这是配共享盘的核心收益。"""
    share = tmp_path / "share"
    share.mkdir()
    (share / "a.exe").write_bytes(b"x")

    with mock.patch.object(fetch, "share_dirs", return_value=[share]), \
         mock.patch.object(fetch, "_download") as dl:
        res = fetch.ensure_package(cfg_share, "a.exe")
    assert res.source == "share"
    dl.assert_not_called()


def test_ensure_package_falls_back_to_download(cfg_share, tmp_path):
    """共享盘挂了 -> 自动降级下载，不报错。"""
    def fake_download(url, dest, timeout_sec, retries):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"downloaded")

    with mock.patch.object(fetch, "share_dirs", return_value=[tmp_path / "gone"]), \
         mock.patch.object(fetch, "_download", side_effect=fake_download):
        res = fetch.ensure_package(cfg_share, "a.exe")
    assert res.source == "download"


def test_allow_share_false_skips_share(cfg_share):
    """allow_share=False 时不碰共享盘。"""
    with mock.patch.object(fetch, "ensure_from_share") as share:
        with pytest.raises(fetch.FetchError):
            fetch.ensure_package(cfg_share, "a.exe", allow_share=False, allow_download=False)
    share.assert_not_called()


def test_share_copy_sha_mismatch_raises(cfg_share, tmp_path):
    """共享盘拷来的包摘要不对 -> 报错，不静默使用坏包。"""
    share = tmp_path / "share"
    share.mkdir()
    (share / "a.exe").write_bytes(b"tampered")

    with mock.patch.object(fetch, "share_dirs", return_value=[share]), \
         mock.patch.object(fetch, "_expected_sha", return_value="f" * 64):
        with pytest.raises(fetch.FetchError, match="sha256 不符"):
            fetch.ensure_from_share(cfg_share, "a.exe")


def test_share_copy_uses_part_then_rename(cfg_share, tmp_path):
    """共享盘拷贝同样走 .part 再原子改名，中途断了不留半截包。"""
    share = tmp_path / "share"
    share.mkdir()
    (share / "a.exe").write_bytes(b"data")

    dest = fetch.cache_dir(cfg_share) / "a.exe"
    with mock.patch.object(fetch, "share_dirs", return_value=[share]):
        fetch.ensure_from_share(cfg_share, "a.exe")

    assert dest.is_file()
    assert not dest.with_suffix(dest.suffix + ".part").exists()


def test_share_dirs_tries_multiple_in_order(cfg_share, tmp_path):
    """配了多个共享盘：第一个不可达时用第二个。"""
    bad = tmp_path / "bad"
    good = tmp_path / "good"
    good.mkdir()
    (good / "a.exe").write_bytes(b"ok")

    with mock.patch.object(fetch, "share_dirs", return_value=[bad, good]):
        res = fetch.ensure_from_share(cfg_share, "a.exe")
    assert res is not None
    assert res.source == "share"



# ---------------- 包名带子路径（fixtures/xxx）----------------
#
# 2026-09-18 踩到的真 bug：`update_fixture.package` 是 `fixtures/7z2602-x64.exe`，
# 带一层子目录。`ensure_from_share` 只建了 `_cache/`，没建 `_cache/fixtures/`，
# 于是 `_copy_from_share` 里 `part.open("wb")` 直接 FileNotFoundError。
# 表现极具迷惑性：**共享盘上明明有这个文件，却报"从共享盘拷贝失败"**。
# 本机一直走"本地已有"分支，所以这个洞在真实多机场景才会暴露。


def test_share_copy_creates_nested_cache_dir(cfg_share, tmp_path):
    """包名带子路径 -> 缓存的子目录要自动建出来。"""
    share = tmp_path / "share"
    (share / "fixtures").mkdir(parents=True)
    payload = b"nested-fixture"
    (share / "fixtures" / "7z2602-x64.exe").write_bytes(payload)

    with mock.patch.object(fetch, "share_dirs", return_value=[share]):
        res = fetch.ensure_from_share(cfg_share, "fixtures/7z2602-x64.exe")

    assert res is not None, "带子路径的包必须能从共享盘取到"
    assert res.source == "share"
    assert res.path.read_bytes() == payload
    assert res.path.parent.name == "fixtures"


def test_download_creates_nested_cache_dir(cfg_share):
    """下载同理：目标带子路径时父目录要自己建，别等 open() 才炸。"""
    dest = fetch.cache_dir(cfg_share) / "fixtures" / "7z2602-x64.exe"

    def _fake_urlopen(req, timeout=None):
        class _Resp:
            def read(self, n=-1):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Resp()

    with mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        fetch._download("http://example.invalid/x", dest, timeout_sec=1, retries=1)

    assert dest.parent.is_dir()
    assert dest.is_file()


# ---------------- 目录型资源（内部安全工具）----------------
#
# `ensure_from_share` 只管**单个文件**。P2 的内部安全工具是一整个目录
# （CheckAppV.exe / SignAppsV.exe / SignCheck_v2.ps1 / signcheck_v2.zip），
# 按红线不进公开仓库，此前只能人肉拷到每台机器，导致 `security` 套件必然 skip。
# 共享盘不是 git 仓库 -> 放一次，所有节点自动取。这组用例锁的就是这条路径。


def test_copy_tree_from_share_copies_nested_files(cfg_share, tmp_path):
    """整棵子树都要拷过来，且相对目录结构保持（工具按相对路径互相找）。"""
    share = tmp_path / "share"
    (share / "fixtures" / "security_tools" / "sub").mkdir(parents=True)
    (share / "fixtures" / "security_tools" / "CheckAppV.exe").write_bytes(b"checkappv")
    (share / "fixtures" / "security_tools" / "SignCheck_v2.ps1").write_text("x", encoding="utf-8")
    (share / "fixtures" / "security_tools" / "sub" / "extra.dat").write_bytes(b"nested")

    dest = tmp_path / "local" / "fixtures" / "security_tools"
    with mock.patch.object(fetch, "share_dirs", return_value=[share]):
        count, why = fetch.copy_tree_from_share(cfg_share, "fixtures/security_tools", dest)

    assert count == 3, f"应拷 3 个文件，实际 {count}（{why}）"
    assert (dest / "CheckAppV.exe").read_bytes() == b"checkappv"
    assert (dest / "SignCheck_v2.ps1").is_file()
    assert (dest / "sub" / "extra.dat").read_bytes() == b"nested"


def test_copy_tree_from_share_returns_zero_when_dir_absent(cfg_share, tmp_path):
    """共享盘上没有这个目录 -> 返回 0，**不抛错**（只影响 P2，不该让整台机器铺设失败）。"""
    share = tmp_path / "share"
    share.mkdir()

    dest = tmp_path / "local" / "security_tools"
    with mock.patch.object(fetch, "share_dirs", return_value=[share]):
        count, why = fetch.copy_tree_from_share(cfg_share, "fixtures/security_tools", dest)

    assert count == 0
    assert why
    assert not dest.exists(), "没取到东西就不该留个空目录假装成功"


def test_copy_tree_from_share_unreachable_share_is_not_error(cfg_share, tmp_path):
    """共享盘整个不可达 -> 同样返回 0 不抛错（与 ensure_from_share 的降级口径一致）。"""
    dest = tmp_path / "local" / "security_tools"
    with mock.patch.object(fetch, "share_dirs", return_value=[tmp_path / "nope"]):
        count, _why = fetch.copy_tree_from_share(cfg_share, "fixtures/security_tools", dest)
    assert count == 0


def test_copy_tree_from_share_blank_rel_dir(cfg_share, tmp_path):
    """相对目录为空 -> 直接返回 0，别去拷整个共享根。"""
    count, why = fetch.copy_tree_from_share(cfg_share, "   ", tmp_path / "dest")
    assert count == 0
    assert "空" in why


def test_copy_tree_from_share_overwrites_readonly_file(cfg_share, tmp_path):
    """**回归锁**：内部工具在共享盘上是只读的（copy2 带只读位）。

    上一轮拷了一半、这轮重跑时要覆盖已存在的只读文件 —— Windows 上直接覆盖会 WinError 5。
    必须先解锁再删。不修的话表现是"重跑 bootstrap 时安全工具永远拷不全"。
    """
    share = tmp_path / "share"
    (share / "tools").mkdir(parents=True)
    (share / "tools" / "CheckAppV.exe").write_bytes(b"fresh")

    dest = tmp_path / "local" / "tools"
    dest.mkdir(parents=True)
    stale = dest / "CheckAppV.exe"
    stale.write_bytes(b"stale")
    stale.chmod(0o444)  # 只读，模拟上一轮 copy2 留下的状态

    with mock.patch.object(fetch, "share_dirs", return_value=[share]):
        count, _why = fetch.copy_tree_from_share(cfg_share, "tools", dest)

    assert count == 1
    assert stale.read_bytes() == b"fresh", "只读文件必须被覆盖，不能静默跳过"


# ---------------- 钉住包：包名带子目录时的 url / sha 匹配 ----------------
#
# 2026-09-21 查出的真 bug：`update_fixture.package` 是 `fixtures/7z2602-x64.exe`，
# 而调用方传进来的 filename 有时带子目录、有时不带。早先按**整串**比较，于是：
#   - `_expected_sha` 对钉住包永远返回空 -> 配置里钉的 sha256 **从未生效**；
#   - `package_url` 匹配不上 -> 回落默认 ASUS CDN，拼出
#     `.../AppStore/fixtures/7z2602-x64.exe`（404），白等三次重试才失败。
# 两处都改成按 basename 比。

FIXTURE_SHA = "6745fa76dc2ea031596d8678f6f6b99c3c1b435b4164a63485adbbc7b8d82ef0"


@pytest.fixture
def cfg_fixture(tmp_path):
    """带 update_fixture 的 Config，且**故意不配** download.url_template。"""
    import yaml

    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text(yaml.safe_dump({
        "installer_dir": str(tmp_path),
        "latest_setup": "myappstore_1.6.11.4S_Setup.exe",
        "download": {"url_template": "", "sha256": {}},
        "update_fixture": {
            "name": "7-Zip（64位）",
            "package": "fixtures/7z2602-x64.exe",
            "sha256": FIXTURE_SHA,
            "url": "https://www.7-zip.org/a/7z2602-x64.exe",
        },
    }, allow_unicode=True), encoding="utf-8")
    with mock.patch.dict("os.environ", {"HALL_CONFIG": str(cfg_file)}, clear=False):
        yield load_config(cfg_file)


def test_same_package_compares_basenames():
    """带子目录与不带子目录指的是同一个包。"""
    assert fetch._same_package("fixtures/7z2602-x64.exe", "fixtures/7z2602-x64.exe")
    assert fetch._same_package("fixtures/7z2602-x64.exe", "7z2602-x64.exe")
    assert not fetch._same_package("fixtures/7z2602-x64.exe", "other.exe")
    assert not fetch._same_package("", "a.exe")
    assert not fetch._same_package("a.exe", "")


def test_expected_sha_matches_nested_package_name(cfg_fixture):
    """**回归锁**：钉住包的 sha256 必须真的被认出来（否则配置形同虚设）。"""
    assert fetch._expected_sha(cfg_fixture, "fixtures/7z2602-x64.exe") == FIXTURE_SHA
    assert fetch._expected_sha(cfg_fixture, "7z2602-x64.exe") == FIXTURE_SHA


def test_package_url_uses_fixture_url_for_nested_name(cfg_fixture):
    """钉住包要走它自己的直链，不能回落到 ASUS CDN 拼出一个 404 地址。"""
    assert fetch.package_url(cfg_fixture, "fixtures/7z2602-x64.exe") == \
        "https://www.7-zip.org/a/7z2602-x64.exe"


def test_package_url_still_falls_back_to_default_cdn(cfg_fixture):
    """别的包没配直链时仍走默认 CDN —— 别把回落路径改坏。"""
    url = fetch.package_url(cfg_fixture, "myappstore_1.6.11.4S_Setup.exe")
    assert "dlcdnets.asus.com" in url
    assert url.endswith("myappstore_1.6.11.4S_Setup.exe")


def test_ensure_package_verifies_fixture_sha_from_share(cfg_fixture, tmp_path):
    """端到端：共享盘上的钉住包摘要不符时必须被拒（半截包/被换过的包）。"""
    share = tmp_path / "share"
    (share / "fixtures").mkdir(parents=True)
    (share / "fixtures" / "7z2602-x64.exe").write_bytes(b"tampered")

    with mock.patch.object(fetch, "share_dirs", return_value=[share]):
        with pytest.raises(fetch.FetchError):
            fetch.ensure_from_share(cfg_fixture, "fixtures/7z2602-x64.exe")


# ---------------- 瞬时文件锁（2026-09-18 偶发失败的根因）----------------
#
# 背景：`test_ensure_from_share_copies_and_verifies` 曾在一次全量跑中偶发失败，
# 报 `FetchError: 从共享盘拷贝失败`，根因当时没定位。
# 2026-09-21 用「注入一次瞬时 PermissionError」复现出来：`_copy_from_share`
# 一次都不重试，而 `_download` 有 3 次重试 —— 同一种瞬时错误，两条路径行为不一致。
# Windows 上杀软实时保护 / 索引服务会在文件 close 后短暂持有句柄，rename 报 WinError 5/32，
# 这是**正常的、暂时的**，退避重试就能过。下面把这组行为钉死。


def test_is_transient_lock_recognises_winerror():
    """WinError 5/32/33/1224 判为瞬时锁；文件不存在这类不是。"""
    for code in (5, 32, 33, 1224):
        exc = OSError(13, "boom")
        exc.winerror = code
        assert fetch._is_transient_lock(exc), code
    missing = OSError(2, "no such file")
    missing.winerror = 2
    assert not fetch._is_transient_lock(missing)


def test_atomic_replace_retries_transient_lock(cfg, tmp_path):
    """**回归锁**：一次瞬时锁不该让改名失败。"""
    part = tmp_path / "a.exe.part"
    dest = tmp_path / "a.exe"
    part.write_bytes(b"payload")
    real = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "Access is denied", str(dst))
        return real(src, dst)

    with mock.patch.object(os, "replace", side_effect=flaky), \
         mock.patch.object(fetch.time, "sleep", lambda s: None):
        fetch._atomic_replace(part, dest)
    assert dest.read_bytes() == b"payload"
    assert calls["n"] == 2


def test_atomic_replace_does_not_retry_permanent_error(tmp_path):
    """永久错误（文件不存在）不该被无脑重试 —— 否则真失败要白等好几秒。"""
    part = tmp_path / "a.exe.part"
    dest = tmp_path / "a.exe"
    part.write_bytes(b"payload")
    calls = {"n": 0}

    def missing(src, dst):
        calls["n"] += 1
        raise FileNotFoundError(2, "No such file or directory", str(src))

    with mock.patch.object(os, "replace", side_effect=missing):
        with pytest.raises(FileNotFoundError):
            fetch._atomic_replace(part, dest)
    assert calls["n"] == 1


def test_share_copy_survives_transient_lock(cfg_share, tmp_path):
    """**回归锁**：共享盘拷贝撞上一次瞬时锁仍应成功取到包（2026-09-18 那次失败）。"""
    share = tmp_path / "share"
    share.mkdir()
    payload = b"from-share"
    (share / "a.exe").write_bytes(payload)
    real = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "Access is denied", str(dst))
        return real(src, dst)

    with mock.patch.object(fetch, "share_dirs", return_value=[share]), \
         mock.patch.object(os, "replace", side_effect=flaky), \
         mock.patch.object(fetch.time, "sleep", lambda s: None):
        res = fetch.ensure_from_share(cfg_share, "a.exe")

    assert res is not None, "一次瞬时锁就把整条取包链打死了（回归）"
    assert res.source == "share"
    assert res.path.read_bytes() == payload


def test_share_copy_fails_loudly_when_lock_persists(cfg_share, tmp_path):
    """锁一直不释放 -> 重试到底后仍报错，不静默给个坏结果；且不留 .part。"""
    share = tmp_path / "share"
    share.mkdir()
    (share / "a.exe").write_bytes(b"from-share")
    calls = {"n": 0}

    def always_locked(src, dst):
        calls["n"] += 1
        raise PermissionError(13, "Access is denied", str(dst))

    with mock.patch.object(fetch, "share_dirs", return_value=[share]), \
         mock.patch.object(os, "replace", side_effect=always_locked), \
         mock.patch.object(fetch.time, "sleep", lambda s: None):
        with pytest.raises(fetch.FetchError, match="从共享盘拷贝失败"):
            fetch.ensure_from_share(cfg_share, "a.exe")

    assert calls["n"] == fetch.ATOMIC_REPLACE_ATTEMPTS, "改名重试次数应与常量一致"
    cache = fetch.cache_dir(cfg_share)
    assert not list(cache.rglob("*.part")), "失败后不该留 .part 残留"


def test_download_also_survives_transient_lock(cfg, tmp_path):
    """下载路径同样要能扛住瞬时锁 —— 两条路径行为必须一致。"""
    dest = fetch.cache_dir(cfg) / "b.exe"
    real = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "Access is denied", str(dst))
        return real(src, dst)

    class _FakeResp:
        def __init__(self):
            self._sent = False

        def read(self, n=-1):
            if self._sent:
                return b""
            self._sent = True
            return b"downloaded"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    # 每次 urlopen 都要拿到**新**对象：重试会再读一次，复用同一个会读到空流
    with mock.patch.object(fetch.urllib.request, "urlopen", side_effect=lambda *a, **k: _FakeResp()), \
         mock.patch.object(os, "replace", side_effect=flaky), \
         mock.patch.object(fetch.time, "sleep", lambda s: None):
        fetch._download("http://example.invalid/x.exe", dest, 5, 3)
    assert dest.read_bytes() == b"downloaded"
