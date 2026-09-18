"""机器复位：检测已装大厅 → 有就卸掉 → 把要用的安装包备齐。

组长 2026-09-18 的要求：「不论装没装对应大厅，都先检测，有检测到就先删掉，
重新下载」。本工具就是这句话的可执行版本，跑批前每台机器跑一次。

用法（管理员 PowerShell，P0 安装层需要管理员）：
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\reset_machine.py
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\reset_machine.py --dry-run
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\reset_machine.py --no-download

选项：
    --dry-run        只报告要做什么，不实际卸载/下载
    --no-download    只找本地已有的包，允许没找到（离线检查用）
    --json           结果按 JSON 打到 stdout（供上层脚本消费）

产物：
    reports/reset/reset_<node>_<时间>.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from hall_auto.config import load_config  # noqa: E402
from hall_auto.dpi import is_interactive_session, node_id  # noqa: E402
from hall_auto.fetch import configured_filenames, ensure_package  # noqa: E402
from hall_auto.installer import reset_before_run  # noqa: E402
from hall_auto.product import is_admin, read_installed  # noqa: E402


def say(text: str) -> None:
    print(text, flush=True)


def dry_run_report(cfg) -> dict:
    """只探测，不动手。"""
    from hall_auto.fetch import share_dirs, share_reachable

    existing = read_installed()
    lines: list[dict] = []
    if existing is None:
        lines.append({"action": "skip-uninstall", "why": "未检测到已安装的大厅"})
    else:
        lines.append({
            "action": "uninstall",
            "display_name": existing.display_name,
            "display_version": existing.display_version,
            "install_dir": str(existing.install_dir),
            "uninstall_string": existing.uninstall_string,
        })

    shares = share_dirs(cfg)
    share_state: list[dict] = []
    for d in shares:
        ok, why = share_reachable(d)
        share_state.append({"dir": str(d), "reachable": ok, "why": why})

    for name in configured_filenames(cfg):
        local = cfg.installer_dir / name
        if local.is_file():
            lines.append({"action": "use-local", "filename": name})
        elif any(s["reachable"] for s in share_state):
            lines.append({"action": "copy-from-share", "filename": name})
        else:
            lines.append({"action": "download", "filename": name})
    return {
        "dry_run": True,
        "plan": lines,
        "admin": is_admin(),
        "shares": share_state,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="跑批前机器复位")
    parser.add_argument("--dry-run", action="store_true", help="只报告不执行")
    parser.add_argument("--no-download", action="store_true", help="只找本地包，不下载")
    parser.add_argument("--json", action="store_true", help="结果以 JSON 打到 stdout")
    args = parser.parse_args()

    cfg = load_config()
    node = node_id()

    if not args.json:
        say(f"=== 机器复位 [{node}] ===")
        say(f"installer_dir = {cfg.installer_dir}")
        say(f"管理员: {is_admin()} / 交互会话: {is_interactive_session()}")
        say("")

    if args.dry_run:
        payload = dry_run_report(cfg)
        payload["node"] = node
        if args.json:
            say(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            shares = payload.get("shares") or []
            if shares:
                say("共享盘状态：")
                for s in shares:
                    mark = "可达" if s["reachable"] else f"不可达（{s['why']}）"
                    say(f"  {s['dir']}: {mark}")
                say("")
            say("【预演】将要执行：")
            for line in payload["plan"]:
                act = line["action"]
                if act == "uninstall":
                    say(f"  卸载 {line['display_name']} {line['display_version']} @ {line['install_dir']}")
                elif act == "use-local":
                    say(f"  用本地包 {line['filename']}")
                elif act == "copy-from-share":
                    say(f"  从共享盘取 {line['filename']}")
                elif act == "download":
                    say(f"  下载 {line['filename']}")
                elif act == "skip-uninstall":
                    say(f"  {line['why']}")
            say("")
            say("（未做任何改动；去掉 --dry-run 才真执行）")
        return 0

    if not cfg.installer_dir.is_dir():
        cfg.installer_dir.mkdir(parents=True, exist_ok=True)

    if args.no_download:
        report: dict = {"node": node, "packages": []}
        existing = read_installed()
        report["installed_before"] = (
            {"display_name": existing.display_name, "display_version": existing.display_version}
            if existing else None
        )
        for name in configured_filenames(cfg):
            local = cfg.installer_dir / name
            report["packages"].append({
                "filename": name,
                "found": local.is_file(),
                "path": str(local) if local.is_file() else "",
            })
        missing = [p["filename"] for p in report["packages"] if not p["found"]]
        report["missing"] = missing
    else:
        try:
            report = reset_before_run(cfg, allow_download=True)
        except Exception as exc:
            say(f"复位失败：{exc}")
            return 1
        report["node"] = node

    out_dir = REPO_ROOT / "reports" / "reset"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"reset_{node}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        say(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for step in report.get("steps", []):
            if step.get("step") == "detect":
                if step.get("found"):
                    say(f"[发现] {step.get('display_name')} {step.get('display_version')} -> 卸载")
                else:
                    say("[发现] 无已装大厅")
            elif step.get("step") == "uninstall":
                say(f"[卸载] {'完成' if step.get('ok') else '失败'}")
        for pkg in report.get("packages", []):
            src = pkg.get("source", "n/a")
            tag = {"local": "本地", "cache": "缓存", "download": "下载"}.get(src, src)
            extra = f" ({pkg['bytes']} 字节)" if pkg.get("bytes") else ""
            err = f" !! {pkg['error']}" if pkg.get("error") else ""
            say(f"[安装包] {pkg['filename']}: {tag}{extra}{err}")
        say("")
        say(f"明细：{out}")
    return 0 if report.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
