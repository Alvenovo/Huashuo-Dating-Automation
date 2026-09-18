"""机器复位：检测已装大厅 → 有就卸掉 → 把要用的安装包备齐。

组长 2026-09-18 的要求：「不论装没装对应大厅，都先检测，有检测到就先删掉，
重新下载」。本工具就是这句话的可执行版本，跑批前每台机器跑一次。

## 两道保护闸（别再拆掉）

1. **包预检先于卸载**。顺序是「先确认三个包都能拿到 → 才卸」，不是「先卸再取包」。
   反过来的话，包一旦取不到，机器就停在「大厅被卸掉且装不回来」——对跑批是最坏结果。
   包拿不到时**拒绝卸载**并以退出码 3 报 `blocked`。
2. **卸载要显式开闸**。默认（没给 `--allow-uninstall`、没设 `HALL_ALLOW_INSTALL=1`）
   检测到已装大厅也**只报告不卸**。办公机上误跑一次就少一个大厅，
   而 pytest 的 `--allow-install` 只管用例、管不到这里。

## 不再做的事

**不再自动创建 `installer_dir`**。配置里的路径常常是从别的机器整份拷来的
（`C:/Users/别人/...`），在原位置建空目录会把"包不存在"这个事实藏起来，报告还是绿 —— 假绿。
拿不到包就该失败。

用法（管理员 PowerShell，P0 安装层需要管理员）：
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\reset_machine.py                    # 只报告不卸
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\reset_machine.py --allow-uninstall  # 真卸
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\reset_machine.py --dry-run          # 预演
    .\\.venv\\Scripts\\python.exe -X utf8 tools\\reset_machine.py --no-download      # 只查本地包

选项：
    --dry-run          只报告要做什么，不实际卸载/下载
    --allow-uninstall  允许卸载已装大厅（不可逆）；等价于 HALL_ALLOW_INSTALL=1
    --no-download      只找本地已有的包，允许没找到（离线检查用）
    --json             结果按 JSON 打到 stdout（供上层脚本消费）

退出码：
    0  成功（或已是最干净起点）
    1  复位过程中出错（取包失败等）
    3  被保护闸拒绝（包拿不到 → 不卸）

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
from hall_auto.installer import ResetBlocked, reset_allowed, reset_before_run  # noqa: E402
from hall_auto.product import is_admin, read_installed  # noqa: E402


def say(text: str) -> None:
    print(text, flush=True)


def dry_run_report(cfg, *, allow_download: bool = True) -> dict:
    """只探测，不动手。

    **包预检结果要显示出来**，因为它是"能不能卸"的前置条件：包拿不到时，
    真跑会拒绝卸载。预演里先看到，比真跑时吃一个 ResetBlocked 强。

    每个包的取用动作（本地 / 缓存 / 共享盘 / 下载）**直接用预检结果** ——
    `check_packages_available` 已经逐包探过，这里不再重探一遍共享盘
    （UNC 不可达时每次探测最多 5s 超时，重复探会白等）。
    """
    from hall_auto.installer import check_packages_available, reset_allowed

    existing = read_installed()
    lines: list[dict] = []
    packs_ok, pack_results = check_packages_available(cfg, allow_download=allow_download)
    can_uninstall = reset_allowed()

    if existing is None:
        lines.append({"action": "skip-uninstall", "why": "未检测到已安装的大厅"})
    elif not packs_ok:
        lines.append({
            "action": "blocked",
            "why": "安装包拿不到 → 拒绝卸载（否则机器会停在卸了装不回来的状态）",
        })
    elif not can_uninstall:
        lines.append({
            "action": "skip-uninstall",
            "why": (
                f"检测到已装大厅 {existing.display_name} {existing.display_version}，"
                "但未开卸载闸：加 --allow-uninstall 或设 HALL_ALLOW_INSTALL=1"
            ),
            "display_name": existing.display_name,
            "display_version": existing.display_version,
        })
    else:
        lines.append({
            "action": "uninstall",
            "display_name": existing.display_name,
            "display_version": existing.display_version,
            "install_dir": str(existing.install_dir),
            "uninstall_string": existing.uninstall_string,
        })

    # 共享盘状态从预检结果里取，不再自己探一遍网络。
    share_state = [
        {"dir": r["share"], "reachable": bool(r.get("reachable")), "why": r.get("why", "")}
        for r in pack_results if "share" in r
    ]
    source_action = {
        "local": "use-local",
        "cache": "use-cache",
        "share": "copy-from-share",
        "download": "download",
    }
    for r in pack_results:
        name = r.get("filename")
        if not name:
            continue
        action = source_action.get(str(r.get("source") or ""))
        if action:
            lines.append({"action": action, "filename": name})
        else:
            # 没有可用来源：老实说"拿不到"，不要显示成 download 装作有路可走。
            lines.append({
                "action": "blocked",
                "filename": name,
                "why": r.get("error") or "本地 / 缓存 / 共享盘都没有可用来源",
            })
    return {
        "dry_run": True,
        "plan": lines,
        "admin": is_admin(),
        "uninstall_allowed": can_uninstall,
        "packages_precheck_ok": packs_ok,
        "packages_precheck": pack_results,
        "shares": share_state,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="跑批前机器复位")
    parser.add_argument("--dry-run", action="store_true", help="只报告不执行")
    parser.add_argument("--no-download", action="store_true", help="只找本地包，不下载")
    parser.add_argument("--json", action="store_true", help="结果以 JSON 打到 stdout")
    parser.add_argument(
        "--allow-uninstall",
        action="store_true",
        help="允许卸载已装的大厅（不可逆）。不显式给就只报告不卸；也可设 HALL_ALLOW_INSTALL=1",
    )
    args = parser.parse_args()

    cfg = load_config()
    node = node_id()

    if not args.json:
        say(f"=== 机器复位 [{node}] ===")
        say(f"installer_dir = {cfg.installer_dir}")
        say(f"管理员: {is_admin()} / 交互会话: {is_interactive_session()}")
        say("")

    if args.dry_run:
        payload = dry_run_report(cfg, allow_download=not args.no_download)
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
            say(f"卸载闸: {'已开' if payload['uninstall_allowed'] else '未开（只报告不卸）'}"
                f" / 包预检: {'通过' if payload['packages_precheck_ok'] else '不通过（会拒绝卸载）'}")
            say("")
            say("【预演】将要执行：")
            for line in payload["plan"]:
                act = line["action"]
                if act == "uninstall":
                    say(f"  卸载 {line['display_name']} {line['display_version']} @ {line['install_dir']}")
                elif act == "use-local":
                    say(f"  用本地包 {line['filename']}")
                elif act == "use-cache":
                    say(f"  用缓存包 {line['filename']}")
                elif act == "copy-from-share":
                    say(f"  从共享盘取 {line['filename']}")
                elif act == "download":
                    say(f"  下载 {line['filename']}")
                elif act == "blocked":
                    # 两种 blocked：整轮被拦（只有 why）／单个包没来源（有 filename + why）
                    what = f"{line['filename']} " if line.get("filename") else ""
                    say(f"  [阻断] {what}{line.get('why', '')}")
                elif act == "skip-uninstall":
                    say(f"  {line['why']}")
            say("")
            say("（未做任何改动；去掉 --dry-run 才真执行）")
        return 0

    if args.no_download:
        # ---- 离线模式：只查本地包，不卸载、不取包 ----
        # 这里**不再 mkdir installer_dir**：配置里的路径常常是从别的机器拷来的，
        # 在那个位置建空目录会把"包不存在"这个事实藏起来（假绿）。
        report: dict = {"node": node, "packages": []}
        existing = read_installed()
        report["installed_before"] = (
            {"display_name": existing.display_name, "display_version": existing.display_version}
            if existing else None
        )
        report["installer_dir_exists"] = cfg.installer_dir.is_dir()
        for name in configured_filenames(cfg):
            local = cfg.installer_dir / name
            report["packages"].append({
                "filename": name,
                "found": local.is_file(),
                "path": str(local) if local.is_file() else "",
            })
        missing = [p["filename"] for p in report["packages"] if not p["found"]]
        report["missing"] = missing
        report["ok"] = not missing
    else:
        try:
            report = reset_before_run(
                cfg,
                allow_download=True,
                allow_uninstall=args.allow_uninstall or reset_allowed(),
            )
        except ResetBlocked as exc:
            # 包拿不到 → 拒绝卸载。这不是崩溃，是保护闸生效，退出码用 3 区分于普通失败。
            say(f"复位被拒绝：{exc}")
            blocked = {
                "node": node, "ok": False, "blocked": True, "reason": str(exc),
                "installer_dir": str(cfg.installer_dir),
            }
            out_dir = REPO_ROOT / "reports" / "reset"
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / f"reset_{node}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            out.write_text(json.dumps(blocked, ensure_ascii=False, indent=2), encoding="utf-8")
            if args.json:
                say(json.dumps(blocked, ensure_ascii=False, indent=2))
            say(f"明细：{out}")
            return 3
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
                if step.get("skipped"):
                    say(f"[卸载] 跳过 —— {step.get('detail', '')}")
                else:
                    say(f"[卸载] {'完成' if step.get('ok') else '失败'}")
        for pkg in report.get("packages", []):
            src = pkg.get("source", "n/a")
            tag = {"local": "本地", "cache": "缓存", "share": "共享盘", "download": "下载"}.get(src, src)
            extra = f" ({pkg['bytes']} 字节)" if pkg.get("bytes") else ""
            err = f" !! {pkg['error']}" if pkg.get("error") else ""
            say(f"[安装包] {pkg['filename']}: {tag}{extra}{err}")
        if report.get("error"):
            say(f"!! {report['error']}")
        say("")
        say(f"明细：{out}")
    return 0 if report.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
