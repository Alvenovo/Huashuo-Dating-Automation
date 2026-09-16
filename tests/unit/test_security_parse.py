"""security.py 解析层回归：样本按 2026-09-16 真跑 CheckAppV / SignCheck_v2 的输出格式造。"""
from __future__ import annotations

import pytest

from hall_auto import security

pytestmark = pytest.mark.unit


def _checkappv_bytes(body: str) -> bytes:
    return "请按任意键继续. . . \r\n".encode("gbk") + body.encode("utf-16-le")


def test_parse_checkappv_unsigned_one():
    body = (
        "🔍 开始扫描并签名目录: C:\\x\n"
        "📄 (1) 检查: C:\\x\\a.exe\n"
        "❌ 文件未签名 )\n"
        "🎉 完成！共有 1 个文件未签名。\n"
        "❌ C:\\x\\a.exe\n"
    )
    result = security.parse_checkappv(_checkappv_bytes(body))
    assert result.unsigned_count == 1
    assert result.unsigned_files == ("C:\\x\\a.exe",)


def test_parse_checkappv_all_signed():
    body = "📄 (1) 检查: C:\\x\\a.dll\n✅ 文件已签名\n🎉 完成！共有 0 个文件未签名。\n"
    result = security.parse_checkappv(_checkappv_bytes(body))
    assert result.unsigned_count == 0
    assert result.unsigned_files == ()


def test_parse_checkappv_garbage_raises():
    with pytest.raises(security.SecurityToolError):
        security.parse_checkappv(b"\x00\x01\x02")


def test_signcheck_ecc_rows(tmp_path):
    report = tmp_path / "ComplianceCheck_x.csv"
    report.write_text(
        '"File Name","Public Key Algorithm"\n'
        '"a.exe","RSA (2048-bit)"\n'
        '"b.dll","ECC (256-bit)"\n',
        encoding="utf-8-sig",
    )
    assert security.signcheck_ecc_rows(report) == ["b.dll:ECC (256-bit)"]


def test_mirror_signable_files(tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.exe").write_bytes(b"1")
    (src / "sub" / "b.dll").write_bytes(b"2")
    (src / "c.txt").write_bytes(b"3")
    dest = tmp_path / "mirror"
    count = security.mirror_signable_files(src, dest)
    assert count == 2
    assert (dest / "a.exe").read_bytes() == b"1"
    assert (dest / "sub" / "b.dll").read_bytes() == b"2"
    assert not (dest / "c.txt").exists()
