from __future__ import annotations

import base64
import html
import io
import json
import platform
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image

from hall_auto.config import REPO_ROOT
from hall_auto.screen import grab_virtual_screen

EVIDENCE_ROOT = REPO_ROOT / "reports" / "evidence"
_PREVIEW_MAX_WIDTH = 1280

MODE_ALL = "all"
MODE_FAILURE = "failure-only"

OUTCOME_PASSED = "passed"
OUTCOME_FAILED = "failed"
OUTCOME_SKIPPED = "skipped"


def new_run_dir(root: Path = EVIDENCE_ROOT) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    base = root / datetime.now().strftime("%Y-%m-%d_%H%M")
    cand, n = base, 1
    while cand.exists():
        n += 1
        cand = root / f"{base.name}_{n}"
    cand.mkdir(parents=True)
    return cand


def module_of(nodeid: str) -> str:
    rel = Path(nodeid.split("::")[0])
    return rel.parts[1] if len(rel.parts) >= 2 else "other"


def case_of(nodeid: str) -> str:
    return nodeid.split("::")[-1]


def _safe_name(name: str, limit: int = 80) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_.\-\u4e00-\u9fff]", "_", name)
    cleaned = cleaned.strip(" .")
    return (cleaned or "case")[:limit]


def capture_screen(path: Path) -> bool:
    try:
        img, _ = grab_virtual_screen()
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path)
        return True
    except Exception:
        return False


@dataclass
class CaseRecord:
    module: str
    case: str
    nodeid: str
    outcome: str
    duration_s: float
    timestamp: str
    assertion: str
    screenshot: str | None
    title: str = ""

    def to_dict(self) -> dict:
        return {
            "module": self.module,
            "case": self.case,
            "title": self.title,
            "nodeid": self.nodeid,
            "outcome": self.outcome,
            "duration_s": round(self.duration_s, 2),
            "timestamp": self.timestamp,
            "assertion": self.assertion,
            "screenshot": self.screenshot,
        }


class EvidenceSession:
    def __init__(self, run_dir: Path, mode: str):
        self.run_dir = run_dir
        self.mode = mode
        self.records: list[CaseRecord] = []

    def _should_capture(self, outcome: str) -> bool:
        return self.mode == MODE_ALL or outcome == OUTCOME_FAILED

    def record(self, nodeid: str, outcome: str, duration: float, assertion: str, title: str = "") -> CaseRecord:
        module, case = module_of(nodeid), case_of(nodeid)
        screenshot_rel = None
        if self._should_capture(outcome):
            case_dir = self.run_dir / _safe_name(module) / _safe_name(case)
            case_dir.mkdir(parents=True, exist_ok=True)
            png = case_dir / "final.png"
            if capture_screen(png):
                screenshot_rel = f"{_safe_name(module)}/{_safe_name(case)}/final.png"
            rec = CaseRecord(
                module=module, case=case, nodeid=nodeid, outcome=outcome,
                duration_s=duration, timestamp=datetime.now().isoformat(timespec="seconds"),
                assertion=assertion, screenshot=screenshot_rel, title=title,
            )
            (case_dir / "result.json").write_text(
                json.dumps(rec.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        else:
            rec = CaseRecord(
                module=module, case=case, nodeid=nodeid, outcome=outcome,
                duration_s=duration, timestamp=datetime.now().isoformat(timespec="seconds"),
                assertion=assertion, screenshot=None, title=title,
            )
        self.records.append(rec)
        return rec

    def finish(self) -> Path:
        passed = sum(1 for r in self.records if r.outcome == OUTCOME_PASSED)
        failed = sum(1 for r in self.records if r.outcome == OUTCOME_FAILED)
        skipped = sum(1 for r in self.records if r.outcome == OUTCOME_SKIPPED)
        total_s = round(sum(r.duration_s for r in self.records), 2)
        summary = {
            "run": self.run_dir.name,
            "machine": platform.node(),
            "mode": self.mode,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "total": len(self.records),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "duration_s": total_s,
            "cases": [r.to_dict() for r in self.records],
        }
        (self.run_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report = self.run_dir / "report.html"
        report.write_text(_render_html(summary), encoding="utf-8")
        return report


def prune_old_runs(root: Path = EVIDENCE_ROOT, keep_days: int = 30) -> list[Path]:
    if keep_days <= 0 or not root.exists():
        return []
    cutoff = datetime.now() - timedelta(days=keep_days)
    removed = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        try:
            day = datetime.strptime(d.name.split("_")[0] + "_" + d.name.split("_")[1], "%Y-%m-%d_%H%M")
        except (ValueError, IndexError):
            continue
        if day < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d)
    return removed


_CSS = """
  :root{--pass:#1a7f37;--pass-bg:#e6f4ea;--fail:#c62828;--fail-bg:#fdecea;
        --skip:#656d76;--skip-bg:#eef1f4;--ink:#1f2328;--muted:#656d76;--line:#d0d7de;--card:#fff;--bg:#f6f8fa;}
  *{box-sizing:border-box;}
  body{margin:0;background:var(--bg);color:var(--ink);font-family:"Microsoft YaHei","Segoe UI",system-ui,sans-serif;font-size:14px;line-height:1.5;}
  .wrap{max-width:1100px;margin:0 auto;padding:24px 16px 64px;}
  header.top{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px 24px;margin-bottom:16px;}
  header.top h1{margin:0 0 6px;font-size:20px;}
  header.top .meta{color:var(--muted);font-size:13px;}
  .stats{display:flex;gap:12px;margin-top:16px;flex-wrap:wrap;}
  .stat{flex:1;min-width:110px;border:1px solid var(--line);border-radius:8px;padding:10px 14px;}
  .stat .n{font-size:24px;font-weight:700;} .stat .l{color:var(--muted);font-size:12px;}
  .stat.pass .n{color:var(--pass);} .stat.fail .n{color:var(--fail);} .stat.skip .n{color:var(--skip);}
  .toolbar{display:flex;gap:8px;align-items:center;margin:0 0 12px;flex-wrap:wrap;}
  .toolbar input{flex:1;min-width:180px;padding:8px 12px;border:1px solid var(--line);border-radius:8px;font-size:13px;}
  .btn{padding:7px 14px;border:1px solid var(--line);background:var(--card);border-radius:20px;cursor:pointer;font-size:13px;}
  .btn.active{background:var(--ink);color:#fff;border-color:var(--ink);}
  table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;}
  th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:middle;}
  th{background:#eef1f4;font-size:12px;color:var(--muted);font-weight:600;}
  tr:last-child td{border-bottom:none;}
  tr.row-failed{background:var(--fail-bg);}
  tr.row-failed:hover{background:#f8d7d3;}
  tr.row-skipped{background:var(--skip-bg);}
  tr.row-skipped:hover{background:#e3e7eb;}
  .badge{display:inline-block;padding:2px 10px;border-radius:12px;font-size:12px;font-weight:600;}
  .badge.pass{color:var(--pass);background:var(--pass-bg);} .badge.fail{color:var(--fail);background:var(--fail-bg);}
  .badge.skipped{color:var(--skip);background:var(--skip-bg);}
  .mod{display:inline-block;padding:1px 8px;border-radius:6px;background:#eef1f4;color:var(--muted);font-size:12px;}
  .case{font-weight:600;} .title{font-size:13px;color:#3b4149;margin-top:2px;} .assert{color:var(--muted);font-size:12px;margin-top:2px;}
  .err{color:var(--fail);font-size:12px;margin-top:4px;font-family:Consolas,monospace;white-space:pre-wrap;}
  .dur{color:var(--muted);font-size:12px;white-space:nowrap;}
  .thumb{width:120px;height:72px;object-fit:cover;border:1px solid var(--line);border-radius:6px;cursor:zoom-in;background:#fff;display:block;}
  .noimg{color:var(--muted);font-size:12px;}
  .lb{position:fixed;inset:0;background:rgba(0,0,0,.8);display:none;align-items:center;justify-content:center;z-index:99;cursor:zoom-out;}
  .lb.open{display:flex;} .lb img{max-width:92vw;max-height:92vh;border-radius:6px;box-shadow:0 8px 40px rgba(0,0,0,.5);}
  footer{color:var(--muted);font-size:12px;text-align:center;margin-top:24px;}
"""

_JS = """
  const rows=[...document.querySelectorAll('#tbl tbody tr')];
  let filter='all';
  function apply(){
    const q=document.getElementById('q').value.trim().toLowerCase();
    rows.forEach(r=>{
      const okF = filter==='all' || r.dataset.s===filter;
      const okQ = !q || r.dataset.text.toLowerCase().includes(q);
      r.style.display=(okF&&okQ)?'':'none';
    });
  }
  document.querySelectorAll('.btn').forEach(b=>b.onclick=()=>{
    document.querySelectorAll('.btn').forEach(x=>x.classList.remove('active'));
    b.classList.add('active'); filter=b.dataset.f; apply();
  });
  document.getElementById('q').oninput=apply;
  const lb=document.getElementById('lb'), lbimg=document.getElementById('lbimg');
  document.querySelectorAll('.thumb').forEach(t=>t.onclick=()=>{lbimg.src=t.src;lb.classList.add('open');});
  lb.onclick=()=>lb.classList.remove('open');
"""


def _embed(png_rel: str, run_dir: Path) -> str | None:
    p = run_dir / png_rel
    if not p.exists():
        return None
    # 原图约 1MB/张，全量内嵌会让报告上百 MB；报告里只放 1280 宽 JPEG 预览，原图留在磁盘
    with Image.open(p) as img:
        if img.width > _PREVIEW_MAX_WIDTH:
            ratio = _PREVIEW_MAX_WIDTH / img.width
            img = img.resize((_PREVIEW_MAX_WIDTH, int(img.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=70)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _render_html(summary: dict, run_dir: Path | None = None) -> str:
    run_dir = run_dir or (EVIDENCE_ROOT / summary["run"])
    total = summary["total"] or 1
    rate = round(summary["passed"] / total * 100)
    rows = []
    for c in summary["cases"]:
        outcome = c["outcome"]
        badge = {"passed": "通过", "failed": "失败", "skipped": "跳过"}.get(outcome, outcome)
        data_uri = _embed(c["screenshot"], run_dir) if c["screenshot"] else None
        img = (f'<img class="thumb" alt="final" src="{data_uri}">'
               if data_uri else '<span class="noimg">无截图</span>')
        err = f'<div class="err">{html.escape(c["assertion"])}</div>' if (outcome == "failed" and c["assertion"]) else ""
        title_line = f'<div class="title">{html.escape(c["title"])}</div>' if c.get("title") else ""
        assert_line = f'<div class="assert">{html.escape(c["assertion"]) or "—"}</div>' if outcome != "failed" else ""
        rows.append(
            f'<tr class="row-{outcome}" data-s="{outcome}" data-text="{html.escape(c["module"] + " " + c["case"] + " " + c.get("title", ""))}">'
            f'<td><span class="badge {outcome}">{badge}</span></td>'
            f'<td><span class="mod">{html.escape(c["module"])}</span></td>'
            f'<td><div class="case">{html.escape(c["case"])}</div>{title_line}{assert_line}{err}</td>'
            f'<td class="dur">{c["duration_s"]}s</td>'
            f'<td>{img}</td></tr>'
        )
    body_rows = "\n".join(rows) or '<tr><td colspan="5" class="noimg">本轮无用例</td></tr>'
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>华硕大厅自动化 · 证据汇总报告 {html.escape(summary["run"])}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <h1>华硕大厅自动化 · 证据汇总报告</h1>
    <div class="meta">Run: <b>{html.escape(summary["run"])}</b> &nbsp;·&nbsp; 机器: {html.escape(summary["machine"])} &nbsp;·&nbsp; 总耗时: {summary["duration_s"]}s &nbsp;·&nbsp; 采集策略: {html.escape(summary["mode"])}</div>
    <div class="stats">
      <div class="stat"><div class="n">{summary["total"]}</div><div class="l">用例总数</div></div>
      <div class="stat pass"><div class="n">{summary["passed"]}</div><div class="l">通过</div></div>
      <div class="stat fail"><div class="n">{summary["failed"]}</div><div class="l">失败</div></div>
      <div class="stat skip"><div class="n">{summary["skipped"]}</div><div class="l">跳过</div></div>
      <div class="stat"><div class="n">{rate}%</div><div class="l">通过率</div></div>
    </div>
  </header>
  <div class="toolbar">
    <input id="q" type="text" placeholder="搜索用例名 / 模块…">
    <button class="btn active" data-f="all">全部</button>
    <button class="btn" data-f="passed">只看通过</button>
    <button class="btn" data-f="failed">只看失败</button>
  </div>
  <table id="tbl">
    <thead><tr><th style="width:90px">结果</th><th style="width:90px">模块</th><th>用例 / 关键断言</th><th style="width:70px">耗时</th><th style="width:130px">终态截图</th></tr></thead>
    <tbody>
{body_rows}
    </tbody>
  </table>
  <footer>自包含单文件（截图 base64 内嵌），可直接发送/存档</footer>
</div>
<div class="lb" id="lb"><img id="lbimg" alt=""></div>
<script>{_JS}</script>
</body>
</html>
"""
