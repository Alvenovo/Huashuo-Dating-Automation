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

