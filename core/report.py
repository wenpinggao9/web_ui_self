"""HTML/JSON 测试报告生成 (水印 + 可观测性面板)."""
from __future__ import annotations

import json
from datetime import datetime
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from .watermark import (
    apply_watermark_to_report,
    watermark_html_extras,
    watermark_html_footer,
)

if TYPE_CHECKING:
    from .execution.runner import ExecResult
    from .observability import ObservabilityCollector


def _format_duration(ms: int) -> str:
    if ms < 1000:
        return f"{ms}ms"
    sec = ms / 1000
    if sec < 60:
        return f"{sec:.1f}秒"
    return f"{int(sec // 60)}分{int(sec % 60)}秒"


def _rel_path(from_dir: Path, target: str | Path | None) -> str:
    if not target:
        return ""
    try:
        tp = Path(target)
        if not tp.is_absolute():
            return str(tp).replace("\\", "/")
        return str(tp.relative_to(from_dir.parent)).replace("\\", "/")
    except Exception:
        return str(target).replace("\\", "/")


def build_report_data(
    case_id: str,
    results: list["ExecResult"],
    total_ms: int,
    *,
    observability: Optional["ObservabilityCollector"] = None,
    out_dir: Optional[Path] = None,
    feature_titles: Optional[list[str]] = None,
) -> dict[str, Any]:
    """组装 report_data 结构."""
    passed = sum(1 for r in results if r.status == "PASS")
    failed = sum(1 for r in results if r.status == "FAIL")
    report_dir = (out_dir / "报告") if out_dir else Path("报告")

    details = []
    screenshot_timeline = []
    for r in results:
        ok = r.status == "PASS"
        action = {
            "type": r.action,
            "intent": r.raw_text,
            "selector": r.selector or r.locator_repr,
        }
        details.append({
            "step": f"步骤{r.step_no}: {r.action}",
            "action": action,
            "success": ok,
            "status": "passed" if ok else "failed",
            "message": r.message or "",
            "error": r.error or "",
            "selector": r.selector or r.locator_repr,
            "resolved_html": (r.resolved_html or "")[:200],
        })
        shot_rel = _rel_path(report_dir, r.screenshot)
        if shot_rel or r.screenshot:
            screenshot_timeline.append({
                "step_idx": r.step_no,
                "success": ok,
                "screenshot": shot_rel,
                "message": r.message or r.error or "",
                "action_type": r.action,
                "intent": r.raw_text,
            })

    data: dict[str, Any] = {
        "case_id": case_id,
        "feature_titles": feature_titles or [],
        "total_steps": len(results),
        "passed": passed,
        "failed": failed,
        "summary": "通过" if failed == 0 else "失败",
        "execution_time": _format_duration(total_ms),
        "generated_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "details": details,
        "screenshot_timeline": screenshot_timeline,
    }

    if observability is not None:
        obs_dict = observability.to_dict()
        failures = []
        llm_details = []
        dom_summary: dict[str, Any] = {}
        total_llm = len(obs_dict.get("global_llm_calls") or [])

        for st in obs_dict.get("steps") or []:
            step_no = st.get("step_no", 0)
            if st.get("status") == "FAIL":
                attr = st.get("failure_attribution") or {}
                failures.append({
                    "step": f"步骤{step_no}",
                    "intent": st.get("intent", ""),
                    "classification": _map_failure_class(attr.get("category", "")),
                    "suggestion": attr.get("suggestion", ""),
                })
            llm_calls = st.get("llm_calls") or []
            total_llm += len(llm_calls)
            if llm_calls:
                llm_details.append({
                    "step_idx": step_no,
                    "success": st.get("status") == "PASS",
                    "action": {"type": st.get("action_type", ""), "intent": st.get("intent", "")},
                    "llm_calls": [_compact_llm_call(c) for c in llm_calls],
                })
            dom_text = st.get("dom_snapshot") or ""
            if dom_text:
                dom_summary[str(step_no)] = {
                    "node_count": len([ln for ln in dom_text.splitlines() if ln.strip()]),
                    "page_url": "",
                }

        data["failure_analysis"] = {
            "total_failures": len(failures),
            "failures": failures,
        }
        data["llm_call_summary"] = {"total_calls": total_llm}
        data["llm_call_details"] = llm_details
        data["dom_snapshot_summary"] = dom_summary

    return data


def _map_failure_class(category: str) -> str:
    m = {
        "选择器未找到": "selector_stale",
        "页面超时": "element_not_appearing",
        "元素不可见": "element_not_appearing",
        "值不匹配": "value_validation",
    }
    return m.get(category, "unknown")


def _compact_llm_call(call: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": call.get("stage", ""),
        "latency_ms": 0,
        "prompt_preview": (call.get("user") or "")[:300],
        "response_preview": (call.get("raw") or "")[:300],
    }


def save_case_report(
    case_id: str,
    results: list["ExecResult"],
    total_ms: int,
    report_dir: Path,
    *,
    out_dir: Optional[Path] = None,
    observability: Optional["ObservabilityCollector"] = None,
    watermark_cfg: Optional[dict[str, Any]] = None,
    feature_titles: Optional[list[str]] = None,
) -> tuple[Path, Path]:
    """保存用例 JSON + HTML 报告, 返回 (json_path, html_path)."""
    report_dir.mkdir(parents=True, exist_ok=True)
    wm = watermark_cfg or {}
    report_data = build_report_data(
        case_id, results, total_ms,
        observability=observability,
        out_dir=out_dir or report_dir.parent,
        feature_titles=feature_titles,
    )
    json_path = report_dir / "result.json"
    html_path = report_dir / "report.html"

    payload = dict(report_data)
    apply_watermark_to_report(payload, wm)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    html_content = generate_html_report(report_data, wm)
    html_path.write_text(html_content, encoding="utf-8")
    return json_path, html_path


def render_report(
    case_id: str,
    url: str,
    total_ms: int,
    results: list["ExecResult"],
    report_dir: Path,
    **kwargs: Any,
) -> Path:
    """兼容旧接口: 生成 HTML 报告."""
    _, html_path = save_case_report(
        case_id, results, total_ms, report_dir,
        watermark_cfg=kwargs.get("watermark_cfg"),
        observability=kwargs.get("observability"),
        out_dir=kwargs.get("out_dir"),
        feature_titles=kwargs.get("feature_titles"),
    )
    return html_path


def generate_html_report(report_data: dict[str, Any], watermark_cfg: dict[str, Any]) -> str:
    """生成 HTML 报告 (步骤详情 + 可观测性 Tab)."""
    steps_html = ""
    for idx, step in enumerate(report_data.get("details", []), start=1):
        status_class = (
            "border-green-500 bg-green-50" if step.get("success") else "border-red-500 bg-red-50"
        )
        badge = (
            '<span class="px-3 py-1 rounded-full text-xs font-medium bg-green-100 text-green-700">通过</span>'
            if step.get("success") else
            '<span class="px-3 py-1 rounded-full text-xs font-medium bg-red-100 text-red-700">失败</span>'
        )
        action = step.get("action", {})
        action_type = action.get("type", "")
        intent = str(action.get("intent", ""))[:120]
        selector = str(step.get("selector") or "")[:100]
        sel_html = (
            f'<p class="text-xs text-gray-500 mt-1 font-mono">selector: {escape(selector)}</p>'
            if selector else ""
        )
        err = step.get("error") or ""
        err_html = (
            f'<p class="text-xs text-red-600 mt-1">{escape(err[:200])}</p>' if err else ""
        )
        steps_html += f"""
            <div class="border-l-4 {status_class} rounded-lg p-4 mb-3 shadow-sm">
              <div class="flex items-center justify-between">
                <div class="flex items-center flex-wrap gap-2">
                  {badge}
                  <span class="font-semibold">步骤 {idx}</span>
                  <span class="text-sm text-gray-600">{escape(action_type)}</span>
                  <span class="text-sm text-gray-500">{escape(intent)}</span>
                </div>
              </div>
              <p class="text-sm text-gray-600 mt-1">{escape(str(step.get('message', ''))[:300])}</p>
              {sel_html}{err_html}
            </div>
            """

    replay_html = _generate_replay_html(report_data)
    hit_rate_html = _generate_hit_rate_html(report_data.get("hit_rate_summary", {}))
    failure_html = _generate_failure_html(report_data.get("failure_analysis", {}))
    llm_summary_html = _generate_llm_summary_html(report_data)
    dom_summary_html = _generate_dom_summary_html(report_data)

    success_rate = (
        f"{(report_data.get('passed', 0) / report_data.get('total_steps', 1) * 100):.0f}%"
        if report_data.get("total_steps", 0) > 0 else "0%"
    )
    summary_class = "bg-green-100 text-green-700" if report_data.get("failed", 1) == 0 else "bg-red-100 text-red-700"
    summary_icon = "fa-check-circle" if report_data.get("failed", 1) == 0 else "fa-times-circle"
    feature = " / ".join(report_data.get("feature_titles") or [])
    feature_line = f'<p class="text-gray-500 text-sm">模块: {escape(feature)}</p>' if feature else ""

    wm_extras = watermark_html_extras(watermark_cfg)
    wm_footer = watermark_html_footer(watermark_cfg)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{escape(report_data.get("case_id", "测试报告"))}</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link href="https://cdn.jsdelivr.net/npm/font-awesome@4.7.0/css/font-awesome.min.css" rel="stylesheet">
  <style>
    .tab-btn {{ cursor:pointer; padding:0.5rem 1.25rem; border-radius:0.5rem 0.5rem 0 0; font-weight:600; font-size:0.875rem; transition:all .2s; }}
    .tab-btn.active {{ background:#fff; color:#1d4ed8; box-shadow:0 -1px 3px rgba(0,0,0,.08); }}
    .tab-btn:not(.active) {{ background:#f3f4f6; color:#6b7280; }}
    .tab-panel {{ display:none; }}
    .tab-panel.active {{ display:block; }}
  </style>
</head>
<body class="bg-gray-100 min-h-screen">
  {wm_extras}
  <div class="max-w-5xl mx-auto py-8 px-4">
    <header class="bg-white rounded-xl shadow-lg p-6 mb-6">
      <h1 class="text-2xl font-bold text-gray-800 mb-2">测试报告</h1>
      <p class="text-gray-600">用例ID: {escape(report_data.get("case_id", ""))}</p>
      {feature_line}
      <span class="inline-block mt-2 px-3 py-1 rounded-full text-sm font-semibold {summary_class}">
        <i class="fa {summary_icon} mr-1"></i>{("通过" if report_data.get("failed", 1) == 0 else "失败")}
      </span>
      <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mt-4">
        <div class="bg-gray-50 rounded-lg p-4 text-center"><p class="text-gray-500 text-sm">总步骤数</p><p class="text-2xl font-bold">{report_data.get("total_steps", 0)}</p></div>
        <div class="bg-green-50 rounded-lg p-4 text-center"><p class="text-gray-500 text-sm">通过</p><p class="text-2xl font-bold text-green-600">{report_data.get("passed", 0)}</p></div>
        <div class="bg-red-50 rounded-lg p-4 text-center"><p class="text-gray-500 text-sm">失败</p><p class="text-2xl font-bold text-red-600">{report_data.get("failed", 0)}</p></div>
        <div class="bg-blue-50 rounded-lg p-4 text-center"><p class="text-gray-500 text-sm">成功率</p><p class="text-2xl font-bold text-blue-600">{success_rate}</p></div>
      </div>
      <div class="mt-4 text-sm text-gray-600">
        <p>执行时长: {escape(report_data.get("execution_time", ""))}</p>
        <p>生成时间: {escape(report_data.get("generated_time", ""))}</p>
      </div>
    </header>

    <div class="flex gap-1 mb-0">
      <button class="tab-btn active" onclick="switchTab('steps')">步骤详情</button>
      <button class="tab-btn" onclick="switchTab('observability')">可观测性</button>
    </div>

    <div class="bg-white rounded-b-xl rounded-tr-xl shadow-lg p-6">
      <div id="tab-steps" class="tab-panel active">
        <section class="space-y-4">{steps_html}</section>
      </div>
      <div id="tab-observability" class="tab-panel">
        <div class="space-y-6">
          {replay_html}
          {hit_rate_html}
          {failure_html}
          {llm_summary_html}
          {dom_summary_html}
        </div>
      </div>
    </div>
    {wm_footer}
  </div>
  <script>
    function switchTab(name) {{
      document.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));
      document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
      document.getElementById('tab-'+name).classList.add('active');
      event.target.classList.add('active');
    }}
    var replayData = [];
    var replayIdx = 0;
    function replayShow(i) {{
      replayIdx = i;
      var item = replayData[i];
      if (!item) return;
      var img = document.getElementById('replay-screenshot');
      if (img && item.screenshot) {{
        img.src = item.screenshot;
        img.style.display = 'block';
      }} else if (img) {{ img.style.display = 'none'; }}
      var badge = document.getElementById('replay-badge');
      if (badge) {{
        badge.textContent = item.success ? '通过' : '失败';
        badge.className = 'px-3 py-1 rounded-full text-xs font-medium ' + (item.success ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700');
      }}
      var info = document.getElementById('replay-info');
      if (info) {{ info.textContent = '步骤 ' + item.step_idx + ' · ' + (item.action_type || '') + ' · ' + (item.intent || '').substring(0, 60); }}
      var msg = document.getElementById('replay-message');
      if (msg) {{ msg.textContent = item.message || ''; }}
      var counter = document.getElementById('replay-counter');
      if (counter) {{ counter.textContent = (i+1) + ' / ' + replayData.length; }}
    }}
    function replayPrev() {{ if (replayIdx > 0) replayShow(replayIdx - 1); }}
    function replayNext() {{ if (replayIdx < replayData.length - 1) replayShow(replayIdx + 1); }}
    function toggleSection(id) {{
      var el = document.getElementById(id);
      if (!el) return;
      var arrow = document.getElementById(id + '-arrow');
      if (el.style.display === 'none' || el.style.display === '') {{
        el.style.display = 'block';
        if (arrow) arrow.style.transform = 'rotate(90deg)';
      }} else {{
        el.style.display = 'none';
        if (arrow) arrow.style.transform = 'rotate(0deg)';
      }}
    }}
    var repairActions = [];
    function addRepairAction(btn) {{
      try {{
        var data = JSON.parse(btn.getAttribute('data-failure'));
        data.action_type = btn.getAttribute('data-action');
        repairActions.push(data);
        btn.textContent = '已添加'; btn.disabled = true;
        btn.className = 'px-2 py-1 rounded text-xs font-medium bg-green-100 text-green-700 cursor-default';
      }} catch(e) {{ console.error(e); }}
    }}
    function downloadRepairActions() {{
      if (!repairActions.length) {{ alert('暂无修复动作'); return; }}
      var blob = new Blob([JSON.stringify(repairActions, null, 2)], {{type: 'application/json'}});
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url; a.download = 'repair_actions.json'; a.click();
      URL.revokeObjectURL(url);
    }}
  </script>
</body>
</html>
"""


def _generate_hit_rate_html(hit_rate_summary: dict[str, Any]) -> str:
    if not hit_rate_summary:
        return '<div class="text-gray-400 text-center py-4">无命中率数据</div>'
    overall = hit_rate_summary.get("overall", {})
    modules = {k: v for k, v in hit_rate_summary.items() if k != "overall" and isinstance(v, dict)}
    cards = ""
    for name, stats in modules.items():
        hit_rate = stats.get("hit_rate", stats.get("exact_hit_rate", 0))
        lookups = stats.get("lookups", 0)
        hits = stats.get("hits", stats.get("total_hits", stats.get("exact_hits", 0)))
        color = "text-green-600" if hit_rate >= 50 else "text-yellow-600" if hit_rate >= 10 else "text-red-600"
        label = {
            "selector_cache": "元素缓存", "selector_memory": "记忆库",
            "page_structure_learner": "结构学习",
        }.get(name, name)
        cards += (
            f'<div class="bg-gray-50 rounded-lg p-4"><p class="text-sm text-gray-500 mb-1">{escape(label)}</p>'
            f'<p class="text-3xl font-bold {color}">{hit_rate}%</p>'
            f'<p class="text-xs text-gray-400 mt-1">查询 {lookups} · 命中 {hits}</p></div>'
        )
    non_llm_rate = overall.get("non_llm_rate", 0)
    non_llm_steps = overall.get("non_llm_steps", 0)
    total_steps = overall.get("total_steps", 0)
    return (
        f'<div><h3 class="text-lg font-semibold text-gray-800 mb-3">'
        f'<i class="fa fa-bullseye mr-2 text-blue-500"></i>命中率总览</h3>'
        f'<div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">{cards}'
        f'<div class="bg-blue-50 rounded-lg p-4"><p class="text-sm text-gray-500 mb-1">非 LLM 解析</p>'
        f'<p class="text-3xl font-bold text-blue-600">{non_llm_rate}%</p>'
        f'<p class="text-xs text-gray-400 mt-1">{non_llm_steps} / {total_steps} 步</p></div></div></div>'
    )


def _generate_replay_html(report_data: dict[str, Any]) -> str:
    timeline = report_data.get("screenshot_timeline", [])
    trace_path = report_data.get("trace_zip_path", "")
    if not timeline and not trace_path:
        return '<div class="text-gray-400 text-center py-4">无回放数据</div>'
    timeline_json = json.dumps(timeline, ensure_ascii=False)
    sections = '<div><h3 class="text-lg font-semibold text-gray-800 mb-3"><i class="fa fa-play-circle mr-2 text-blue-500"></i>执行回放</h3>'
    if timeline:
        sections += """
            <div class="bg-gray-50 rounded-lg p-4 mb-3">
              <div class="flex items-center justify-between mb-2">
                <span id="replay-badge" class="px-3 py-1 rounded-full text-xs font-medium bg-gray-100 text-gray-500">-</span>
                <span id="replay-counter" class="text-sm text-gray-500">0 / 0</span>
              </div>
              <img id="replay-screenshot" class="w-full rounded border mb-2" style="max-height:400px;object-fit:contain;display:none;" alt="步骤截图" />
              <p id="replay-info" class="text-sm text-gray-700 font-medium mb-1"></p>
              <p id="replay-message" class="text-xs text-gray-500 mb-2"></p>
              <div class="flex gap-2">
                <button onclick="replayPrev()" class="px-4 py-2 bg-white border rounded-lg text-sm font-medium hover:bg-gray-50">上一步</button>
                <button onclick="replayNext()" class="px-4 py-2 bg-white border rounded-lg text-sm font-medium hover:bg-gray-50">下一步</button>
              </div>
            </div>
            """
    if trace_path:
        sections += f"""
            <div class="bg-blue-50 rounded-lg p-3 text-sm">
              <p class="font-medium text-blue-700 mb-1"><i class="fa fa-download mr-1"></i>Playwright Trace</p>
              <p class="text-blue-600 text-xs mb-1">路径: <code>{escape(trace_path)}</code></p>
            </div>
            """
    sections += "</div>"
    return sections + f"<script>replayData = {timeline_json}; if(replayData.length) replayShow(0);</script>"


def _generate_failure_html(failure_analysis: dict[str, Any]) -> str:
    if not failure_analysis or not failure_analysis.get("failures"):
        return '<div class="bg-green-50 rounded-lg p-4 text-green-700 text-center"><i class="fa fa-check-circle mr-1"></i>无失败步骤</div>'
    action_map = {
        "selector_stale": ("清除缓存", "clear_cache"),
        "llm_misjudgment": ("更新记忆", "update_memory"),
        "element_not_appearing": ("增加等待", "add_wait"),
        "page_navigation": ("添加规则", "add_rule"),
        "value_validation": ("修正值", "fix_value"),
        "unknown": ("人工复核", "manual_review"),
    }
    rows = ""
    for f in failure_analysis["failures"]:
        cls = f.get("classification", "unknown")
        cls_color = {
            "selector_stale": "text-orange-600", "element_not_appearing": "text-red-600",
            "value_validation": "text-yellow-600", "unknown": "text-gray-500",
        }.get(cls, "text-gray-600")
        btn_label, action_type = action_map.get(cls, ("人工复核", "manual_review"))
        failure_json = json.dumps(f, ensure_ascii=False).replace('"', "&quot;")
        rows += (
            f'<tr class="border-b">'
            f'<td class="py-2 px-3 text-sm">{escape(str(f.get("step", ""))[:60])}</td>'
            f'<td class="py-2 px-3 text-sm">{escape(str(f.get("intent", ""))[:60])}</td>'
            f'<td class="py-2 px-3 text-sm font-semibold {cls_color}">{escape(cls)}</td>'
            f'<td class="py-2 px-3 text-xs text-gray-500">{escape(f.get("suggestion", "")[:120])}</td>'
            f'<td class="py-2 px-3"><button class="px-2 py-1 rounded text-xs font-medium bg-indigo-100 text-indigo-700 hover:bg-indigo-200" data-failure="{failure_json}" data-action="{action_type}" onclick="addRepairAction(this)">{btn_label}</button></td>'
            f'</tr>'
        )
    return (
        f'<div><h3 class="text-lg font-semibold text-gray-800 mb-3"><i class="fa fa-exclamation-triangle mr-2 text-red-500"></i>'
        f'失败归因（{failure_analysis.get("total_failures", 0)} 处）</h3>'
        f'<div class="overflow-x-auto"><table class="w-full text-left">'
        f'<thead class="bg-gray-100"><tr>'
        f'<th class="py-2 px-3 text-xs text-gray-500">步骤</th>'
        f'<th class="py-2 px-3 text-xs text-gray-500">意图</th>'
        f'<th class="py-2 px-3 text-xs text-gray-500">分类</th>'
        f'<th class="py-2 px-3 text-xs text-gray-500">建议</th>'
        f'<th class="py-2 px-3 text-xs text-gray-500">操作</th>'
        f'</tr></thead><tbody>{rows}</tbody></table></div>'
        f'<div class="mt-3 text-right">'
        f'<button onclick="downloadRepairActions()" class="px-4 py-2 bg-indigo-600 text-white rounded-lg text-sm font-medium hover:bg-indigo-700">导出修复清单</button>'
        f'</div></div>'
    )


def _generate_llm_summary_html(report_data: dict[str, Any]) -> str:
    details = report_data.get("llm_call_details", [])
    summary = report_data.get("llm_call_summary", {})
    if not summary and not details:
        return ""
    total = summary.get("total_calls", 0) if summary else sum(len(s.get("llm_calls", [])) for s in details)
    header = (
        f'<div><h3 class="text-lg font-semibold text-gray-800 mb-3">'
        f'<i class="fa fa-cloud mr-2 text-indigo-500"></i>LLM 决策追踪（共 {total} 次调用）</h3>'
    )
    if not details:
        return header + '<p class="text-gray-400 text-sm">无逐步 LLM 明细</p></div>'
    sections = ""
    for step_trace in details:
        step_idx = step_trace.get("step_idx", 0)
        llm_calls = step_trace.get("llm_calls", [])
        action = step_trace.get("action", {})
        action_type = action.get("type", "")
        intent = str(action.get("intent", ""))[:80]
        success = step_trace.get("success", False)
        status_badge = (
            '<span class="px-2 py-0.5 rounded text-xs font-medium bg-green-100 text-green-700">通过</span>'
            if success else
            '<span class="px-2 py-0.5 rounded text-xs font-medium bg-red-100 text-red-700">失败</span>'
        )
        section_id = f"llm-section-{step_idx}"
        sections += f"""
            <div class="border rounded-lg mb-2">
              <div class="px-4 py-3 bg-gray-50 cursor-pointer flex items-center justify-between" onclick="toggleSection('{section_id}')">
                <div class="flex items-center gap-2 flex-wrap">
                  <i id="{section_id}-arrow" class="fa fa-caret-right text-gray-400"></i>
                  <span class="font-medium text-sm">步骤 {step_idx}</span>
                  <span class="text-xs text-gray-500">{escape(action_type)}</span>
                  <span class="text-xs text-gray-500">{escape(intent)}</span>
                  {status_badge}
                  <span class="text-xs text-gray-400">{len(llm_calls)} 次调用</span>
                </div>
              </div>
              <div id="{section_id}" class="px-4 py-3 border-t" style="display:none;">
            """
        for call_idx, call in enumerate(llm_calls):
            sections += f"""
                <div class="mb-3 bg-white rounded border p-3">
                  <div class="flex items-center justify-between mb-1">
                    <span class="text-xs font-medium text-gray-600">调用 #{call_idx + 1} · {escape(str(call.get('model', '')))}</span>
                  </div>
                  <p class="text-xs text-gray-500 font-medium">Prompt 预览:</p>
                  <p class="text-xs text-gray-700 bg-gray-50 rounded p-2 whitespace-pre-wrap">{escape(call.get('prompt_preview', '')[:400])}</p>
                  <p class="text-xs text-gray-500 font-medium mt-2">Response 预览:</p>
                  <p class="text-xs text-gray-700 bg-gray-50 rounded p-2 whitespace-pre-wrap">{escape(call.get('response_preview', '')[:400])}</p>
                </div>
                """
        sections += "</div></div>"
    return header + f'<div class="space-y-1">{sections}</div></div>'


def _generate_dom_summary_html(report_data: dict[str, Any]) -> str:
    summary = report_data.get("dom_snapshot_summary", {})
    if not summary:
        return ""
    tags = ""
    for step_idx, info in sorted(summary.items(), key=lambda x: int(x[0]) if str(x[0]).isdigit() else 0):
        node_count = info.get("node_count", 0) if isinstance(info, dict) else 0
        url = info.get("page_url", "")[:60] if isinstance(info, dict) else ""
        tags += f'<span class="inline-block bg-gray-100 rounded px-2 py-1 text-xs mr-1 mb-1">步骤 {step_idx}: {node_count} 节点 · {escape(url)}</span>'
    return (
        f'<div><h3 class="text-lg font-semibold text-gray-800 mb-3">'
        f'<i class="fa fa-sitemap mr-2 text-green-500"></i>DOM 快照</h3>'
        f'<div class="flex flex-wrap">{tags}</div></div>'
    )


def save_batch_overview(
    batch_dir: Path,
    *,
    source_file: str,
    case_results: list[dict[str, Any]],
    watermark_cfg: dict[str, Any],
    execution_time: str = "",
    batch_timestamp: str = "",
) -> tuple[Path, Path]:
    """保存批次汇总 JSON + HTML (report_overview.*)."""
    total_cases = len(case_results)
    passed_cases = sum(1 for c in case_results if c.get("passed") or c.get("success"))
    failed_cases = total_cases - passed_cases
    success_rate = f"{(passed_cases / total_cases * 100):.1f}%" if total_cases else "0%"

    report_data: dict[str, Any] = {
        "type": "batch_overview",
        "source_file": source_file,
        "batch_timestamp": batch_timestamp or batch_dir.name,
        "total_cases": total_cases,
        "passed_cases": passed_cases,
        "failed_cases": failed_cases,
        "success_rate": success_rate,
        "execution_time": execution_time,
        "cases": case_results,
        "generated_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    apply_watermark_to_report(report_data, watermark_cfg)

    json_path = batch_dir / "report_overview.json"
    html_path = batch_dir / "report_overview.html"
    json_path.write_text(json.dumps(report_data, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(_generate_batch_html(report_data, watermark_cfg), encoding="utf-8")
    return json_path, html_path


def _generate_batch_html(report_data: dict[str, Any], watermark_cfg: dict[str, Any]) -> str:
    total_steps = sum(int(c.get("total_steps", 0) or 0) for c in report_data.get("cases", []))
    total_passed_steps = sum(int(c.get("passed_steps", 0) or 0) for c in report_data.get("cases", []))
    total_failed_steps = sum(int(c.get("failed_steps", 0) or 0) for c in report_data.get("cases", []))
    step_success_rate = f"{(total_passed_steps / total_steps * 100):.1f}%" if total_steps else "0.0%"

    case_sections = []
    for idx, case in enumerate(report_data.get("cases", []), start=1):
        case_id = escape(str(case.get("case_id", "")))
        passed = int(case.get("passed_steps", 0) or 0)
        failed = int(case.get("failed_steps", 0) or 0)
        case_total = int(case.get("total_steps", 0) or 0)
        case_time = escape(str(case.get("execution_time", "")))
        case_rate = escape(str(case.get("step_success_rate", "0%")))
        ok = case.get("passed") or case.get("success")
        status = "通过" if ok else "失败"

        step_items = []
        for step_idx, step in enumerate(case.get("details", []) or [], start=1):
            step_ok = bool(step.get("success"))
            step_title = escape(str(step.get("step", f"步骤{step_idx}")))
            step_msg = escape(str(step.get("message", "")))
            step_items.append(
                f"<li class='bg-white rounded-xl shadow'>"
                f"<div class='p-4 border-l-4 {'border-green-500 bg-green-50' if step_ok else 'border-red-500 bg-red-50'}'>"
                f"<div class='flex justify-between'><h4 class='font-medium'>{step_title}</h4>"
                f"<span class='px-3 py-1 rounded-full text-xs {'bg-green-100 text-green-700' if step_ok else 'bg-red-100 text-red-700'}'>"
                f"{'成功' if step_ok else '失败'}</span></div>"
                f"<div class='mt-2 bg-gray-50 rounded p-3 text-sm'>{step_msg}</div></div></li>"
            )

        case_sections.append(f"""
<details class="bg-white rounded-xl shadow overflow-hidden mb-4">
  <summary class="cursor-pointer p-5 flex justify-between items-center">
    <span class="font-semibold">{idx}. {case_id}</span>
    <span class="px-3 py-1 rounded-full text-xs {'bg-green-100 text-green-700' if ok else 'bg-red-100 text-red-700'}">{status}</span>
  </summary>
  <div class="px-5 pb-5 border-t">
    <div class="grid grid-cols-2 md:grid-cols-5 gap-3 mt-4 text-sm">
      <div class="bg-gray-50 rounded p-3"><p class="text-xs text-gray-500">执行时长</p><p class="font-semibold">{case_time}</p></div>
      <div class="bg-gray-50 rounded p-3"><p class="text-xs text-gray-500">步骤总数</p><p class="font-semibold">{case_total}</p></div>
      <div class="bg-gray-50 rounded p-3"><p class="text-xs text-gray-500">成功</p><p class="font-semibold text-green-600">{passed}</p></div>
      <div class="bg-gray-50 rounded p-3"><p class="text-xs text-gray-500">失败</p><p class="font-semibold text-red-600">{failed}</p></div>
      <div class="bg-gray-50 rounded p-3"><p class="text-xs text-gray-500">步骤成功率</p><p class="font-semibold">{case_rate}</p></div>
    </div>
    <ul class="list-none mt-4 space-y-3">{''.join(step_items) or '<li class="text-gray-500 text-sm">无步骤明细</li>'}</ul>
  </div>
</details>
""")

    wm_extras = watermark_html_extras(watermark_cfg)
    wm_footer = watermark_html_footer(watermark_cfg)
    all_ok = report_data.get("failed_cases", 0) == 0

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>文件级总报告</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link href="https://cdn.jsdelivr.net/npm/font-awesome@4.7.0/css/font-awesome.min.css" rel="stylesheet">
</head>
<body class="bg-gray-100 text-gray-800">
  {wm_extras}
  <div class="max-w-6xl mx-auto px-4 py-8">
    <header class="mb-8">
      <div class="flex flex-col md:flex-row md:items-center justify-between mb-4">
        <div>
          <h1 class="text-3xl font-bold">文件级汇总报告</h1>
          <p class="text-gray-500">来源: {escape(str(report_data.get("source_file", "")))}</p>
        </div>
        <span class="px-4 py-1 rounded-full text-sm {'bg-green-100 text-green-700' if all_ok else 'bg-red-100 text-red-700'}">
          <i class="fa {'fa-check-circle' if all_ok else 'fa-times-circle'} mr-1"></i>{'通过' if all_ok else '失败'}
        </span>
      </div>
      <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
        <div class="bg-white rounded-lg p-4 text-center shadow"><p class="text-gray-500 text-sm">用例总数</p><p class="text-2xl font-bold">{report_data.get("total_cases", 0)}</p></div>
        <div class="bg-white rounded-lg p-4 text-center shadow"><p class="text-gray-500 text-sm">通过</p><p class="text-2xl font-bold text-green-600">{report_data.get("passed_cases", 0)}</p></div>
        <div class="bg-white rounded-lg p-4 text-center shadow"><p class="text-gray-500 text-sm">失败</p><p class="text-2xl font-bold text-red-600">{report_data.get("failed_cases", 0)}</p></div>
        <div class="bg-white rounded-lg p-4 text-center shadow"><p class="text-gray-500 text-sm">用例成功率</p><p class="text-2xl font-bold text-blue-600">{escape(str(report_data.get("success_rate", "")))}</p></div>
      </div>
      <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div class="bg-white rounded-lg p-4 text-center shadow"><p class="text-gray-500 text-sm">步骤总数</p><p class="text-2xl font-bold">{total_steps}</p></div>
        <div class="bg-white rounded-lg p-4 text-center shadow"><p class="text-gray-500 text-sm">步骤通过</p><p class="text-2xl font-bold text-green-600">{total_passed_steps}</p></div>
        <div class="bg-white rounded-lg p-4 text-center shadow"><p class="text-gray-500 text-sm">步骤失败</p><p class="text-2xl font-bold text-red-600">{total_failed_steps}</p></div>
        <div class="bg-white rounded-lg p-4 text-center shadow"><p class="text-gray-500 text-sm">步骤成功率</p><p class="text-2xl font-bold text-blue-600">{step_success_rate}</p></div>
      </div>
      <div class="mt-4 text-sm text-gray-600">
        <p>批次: {escape(str(report_data.get("batch_timestamp", "")))}</p>
        <p>执行时长: {escape(str(report_data.get("execution_time", "")))}</p>
        <p>生成时间: {escape(str(report_data.get("generated_time", "")))}</p>
      </div>
    </header>
    {''.join(case_sections)}
    {wm_footer}
  </div>
</body>
</html>
"""
