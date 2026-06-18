"""HTML 测试报告生成."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .execution.runner import ExecResult


def render_report(case_id: str, url: str, total_ms: int, results: list["ExecResult"], report_dir: Path) -> Path:
    """生成 HTML 报告文件."""
    from rich.markup import escape as _esc

    report_dir.mkdir(parents=True, exist_ok=True)
    html_path = report_dir / "report.html"

    rows = []
    for r in results:
        color = {"PASS": "#22c55e", "FAIL": "#ef4444"}.get(r.status, "#6b7280")
        rows.append(
            f'<tr>'
            f'<td class="step">{r.step_no}</td>'
            f'<td>{_esc(r.raw_text)}</td>'
            f'<td class="action">{_esc(r.action)}</td>'
            f'<td style="color:{color};font-weight:bold">{_esc(r.status)}</td>'
            f'<td>{r.duration_ms}ms</td>'
            f'<td class="msg">{_esc(r.message or "")}</td>'
            f'<td class="msg" style="color:#ef4444">{_esc(r.error or "")}</td>'
            f'</tr>'
        )

    passed = sum(1 for r in results if r.status == "PASS")
    failed = sum(1 for r in results if r.status == "FAIL")

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>UI Test Report - {case_id}</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #f9fafb; }}
h1 {{ font-size: 1.4rem; margin-bottom: 0.5rem; }}
.summary {{ display: flex; gap: 1.5rem; margin-bottom: 1rem; font-size: 0.95rem; }}
table {{ border-collapse: collapse; width: 100%; font-size: 0.85rem; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
th, td {{ padding: 6px 10px; border-bottom: 1px solid #e5e7eb; text-align: left; }}
th {{ background: #f3f4f6; font-weight: 600; }}
tr:hover {{ background: #f9fafb; }}
.action {{ color: #6366f1; }}
.msg {{ font-family: monospace; font-size: 0.8rem; max-width: 300px; word-break: break-all; }}
</style></head><body>
<h1>{_esc(case_id)}</h1>
<div class="summary">
  <span>Total: {len(results)}</span>
  <span style="color:#22c55e">Passed: {passed}</span>
  <span style="color:#ef4444">Failed: {failed}</span>
  <span>Duration: {total_ms}ms</span>
</div>
<table><tr><th>#</th><th>Intent</th><th>Action</th><th>Status</th><th>Time</th><th>Message</th><th>Error</th></tr>
{''.join(rows)}
</table></body></html>"""

    html_path.write_text(html, encoding="utf-8")
    return html_path
