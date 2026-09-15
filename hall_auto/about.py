from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from PIL import Image

from hall_auto.config import Config, REPO_ROOT
from hall_auto.launch import (
    LaunchError,
    _press_button,
    start_fresh,
    wait_main_window,
    wait_until_ready,
)
from hall_auto.product import stop_main_process

ABOUT_DIR = REPO_ROOT / "reports" / "about"
OCR_SCRIPT = REPO_ROOT / "tools" / "ocr_text.ps1"
VERSION_RE = re.compile(r"\d+\.\d+\.\d+\.\d+")

# 关于页的版本号 UIA name 只是占位符 ver（产品无障碍缺陷），只能 OCR 截图。
# 中文 OCR 引擎对这行浅灰小字的常见误认，按字形归一：
_OCR_FIXES = {
    "．": ".",
    "，": ".",
    "、": ".",
    "l": "1",
    "I": "1",
    "O": "0",
    "o": "0",
    "Z": "2",
    "S": "5",
    "B": "8",
}


def normalize_ocr(text: str) -> str:
    out = []
    for ch in text:
        if ch.isspace():
            continue
        out.append(_OCR_FIXES.get(ch, ch))
    return "".join(out)


def parse_version(text: str) -> str | None:
    match = VERSION_RE.search(normalize_ocr(text))
    return match.group(0) if match else None


def _click_aid(main, aid: str) -> bool:
    for node in main.descendants(control_type="Button"):
        try:
            if (node.element_info.automation_id or "") == aid:
                return _press_button(node)
        except Exception:
            continue
    return False


def _version_node(main):
    for node in main.descendants(control_type="Text"):
        try:
            if (node.element_info.automation_id or "") == "VersionLabel":
                return node
        except Exception:
            continue
    return None


def _ocr_image(path: Path) -> str:
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(OCR_SCRIPT),
            "-ImagePath",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if proc.returncode != 0:
        raise LaunchError(f"OCR 失败: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def _prepare_for_ocr(raw: Path, ready: Path) -> None:
    img = Image.open(raw).convert("L")
    big = img.resize((img.width * 8, img.height * 8), Image.LANCZOS)
    padded = Image.new("L", (big.width + 80, big.height + 80), 255)
    padded.paste(big, (40, 40))
    padded.point(lambda p: 0 if p < 200 else 255).save(ready)


def read_about_version(cfg: Config) -> str:
    """进 设置 → 关于，OCR 关于页版本号。返回四段版本号字符串。"""
    ABOUT_DIR.mkdir(parents=True, exist_ok=True)
    app = start_fresh(cfg)
    try:
        main = wait_main_window(app, cfg)
        wait_until_ready(cfg, main)
        if not _click_aid(main, "SetBtn"):
            raise LaunchError("关于页：点不到设置按钮 SetBtn")
        time.sleep(2)
        if not _click_aid(main, "AboutBtn"):
            raise LaunchError("关于页：点不到关于按钮 AboutBtn")
        time.sleep(2)
        node = None
        for _ in range(10):
            node = _version_node(main)
            if node is not None:
                break
            time.sleep(1)
        if node is None:
            raise LaunchError("关于页：找不到 VersionLabel 节点")
        try:
            main.set_focus()
            time.sleep(0.5)
        except Exception:
            pass
        stamp = time.strftime("%Y%m%d_%H%M%S")
        raw = ABOUT_DIR / f"{stamp}_version_raw.png"
        node.capture_as_image().save(raw)
        ready = ABOUT_DIR / f"{stamp}_version_ocr.png"
        _prepare_for_ocr(raw, ready)
        text = _ocr_image(ready)
        version = parse_version(text)
        if version is None:
            raise LaunchError(f"关于页：OCR 没认出四段版本号，原文 {text!r}，截图 {ready}")
        return version
    finally:
        stop_main_process(cfg.timeouts.process_stop_sec)
