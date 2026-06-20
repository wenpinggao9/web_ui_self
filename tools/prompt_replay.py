#!/usr/bin/env python3
"""从 可观测性.json 回放 LLM 调用, 对比「代码内置旧提示词」与「prompts/*.md 新提示词」.

用法:
  python tools/prompt_replay.py 输出/UI测试/20260609_144659/电商_购物_下单_001/可观测性.json
  python tools/prompt_replay.py 输出/UI测试 --stages post_check,readiness --limit 3
  python tools/prompt_replay.py 输出/UI测试 --dry-run

报告输出到 输出/提示词回放/<时间戳>/report.md 与 results.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.execution.post_check import _DEFAULT_SYSTEM as OLD_POST_CHECK  # noqa: E402
from core.llm import LLMAdapter, PromptLoader  # noqa: E402
from core.llm_client import _extract_json  # noqa: E402
from core.planning.action_planner import _DEFAULT_SYSTEM as OLD_ACTION_PLAN  # noqa: E402
from core.preprocess.case_sort import _DEFAULT_SYSTEM as OLD_CASE_SORT  # noqa: E402
from core.preprocess.precondition import _DEFAULT_SYSTEM as OLD_PRECONDITION  # noqa: E402
from core.readiness.pre_check import _DEFAULT_SYSTEM as OLD_READINESS  # noqa: E402
from core.locating.llm_decider import _DEFAULT_SYSTEM as OLD_ELEMENT_DECIDE  # noqa: E402
from core.skill_loader import load_skill_text  # noqa: E402

REPLAY_STAGES = (
    "action_plan",
    "post_check",
    "readiness",
    "precondition",
    "case_sort",
    "element_decide",
)

OLD_DEFAULTS: dict[str, str] = {
    "action_plan": OLD_ACTION_PLAN,
    "post_check": OLD_POST_CHECK,
    "readiness": OLD_READINESS,
    "precondition": OLD_PRECONDITION,
    "case_sort": OLD_CASE_SORT,
    "element_decide": OLD_ELEMENT_DECIDE,
}


@dataclass
class ReplaySample:
    """一条可回放样本."""

    source: str
    stage: str
    step_no: Optional[int]
    user: str
    historical_raw: str
    intent_hint: str = ""
    old_data: Optional[dict[str, Any]] = None
    new_data: Optional[dict[str, Any]] = None
    old_error: Optional[str] = None
    new_error: Optional[str] = None
    notes: list[str] = field(default_factory=list)


def _discover_obs_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target] if target.name == "可观测性.json" else []
    if target.is_dir():
        return sorted(target.rglob("可观测性.json"))
    return []


def _extract_intent_hint(user: str, stage: str) -> str:
    patterns = [
        r"操作意图:\s*(.+)",
        r"intent=([^\n]+)",
        r"原始动作 type=.+ intent=(.+)",
        r"下一步动作: type=\w+ intent=([^\s]+)",
    ]
    for p in patterns:
        m = re.search(p, user)
        if m:
            return m.group(1).strip()[:120]
    return user.split("\n", 1)[0][:80]


def _collect_samples(obs_path: Path, stages: set[str], limit_per_file: int) -> list[ReplaySample]:
    data = json.loads(obs_path.read_text(encoding="utf-8"))
    out: list[ReplaySample] = []
    per_stage: dict[str, int] = {s: 0 for s in stages}

    def maybe_add(stage: str, user: str, raw: str, step_no: Optional[int]) -> None:
        if stage not in stages or per_stage[stage] >= limit_per_file:
            return
        per_stage[stage] += 1
        out.append(
            ReplaySample(
                source=str(obs_path),
                stage=stage,
                step_no=step_no,
                user=user,
                historical_raw=raw or "",
                intent_hint=_extract_intent_hint(user, stage),
            )
        )

    for call in data.get("global_llm_calls", []):
        maybe_add(call.get("stage", ""), call.get("user", ""), call.get("raw", ""), None)

    for step in data.get("steps", []):
        step_no = step.get("step_no")
        for call in step.get("llm_calls", []):
            maybe_add(call.get("stage", ""), call.get("user", ""), call.get("raw", ""), step_no)

    return out


def _system_for(stage: str, variant: str, prompts: PromptLoader, skill_text: str) -> str:
    default = OLD_DEFAULTS.get(stage, "")
    if variant == "old":
        base = default
    else:
        base = prompts.system(stage, default)
    if stage == "action_plan" and skill_text:
        return (skill_text + "\n\n" + base) if base else skill_text
    return base


def _safe_parse_json(raw: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    if not raw or not raw.strip():
        return None, "空响应"
    try:
        return _extract_json(raw), None
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def _call_llm(llm: LLMAdapter, stage: str, system: str, user: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    try:
        return llm.complete_json(stage, system, user).data, None
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def _norm_bool(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        if v.lower() in ("true", "1", "yes"):
            return True
        if v.lower() in ("false", "0", "no"):
            return False
    return None


def _compare_stage(stage: str, hist: Optional[dict], old: Optional[dict], new: Optional[dict]) -> list[str]:
    notes: list[str] = []
    if hist is None and old is None and new is None:
        return ["三者均无有效 JSON"]

    if stage == "post_check":
        for label, data in [("历史", hist), ("旧版", old), ("新版", new)]:
            if not data:
                continue
            ok = _norm_bool(data.get("step_ok"))
            notes.append(f"{label} step_ok={ok} retry_focus={data.get('retry_focus')}")
        if new and new.get("reason"):
            if re.search(r"\[\d+\]", str(new.get("reason"))):
                notes.append("新版 reason 含 [index] 证据")
        if old and new and _norm_bool(old.get("step_ok")) != _norm_bool(new.get("step_ok")):
            notes.append("⚠ 旧/新 step_ok 不一致")

    elif stage == "readiness":
        for label, data in [("历史", hist), ("旧版", old), ("新版", new)]:
            if not data:
                continue
            rec = data.get("recovery") or []
            notes.append(f"{label} ready={data.get('ready')} recovery={len(rec)}条")
        if new and new.get("recovery"):
            intents = [str(r.get("intent", "")) for r in new["recovery"] if isinstance(r, dict)]
            if any("展开" in i or "悬浮" in i or "hover" in i.lower() for i in intents):
                notes.append("新版 recovery 含菜单展开类动作")

    elif stage == "action_plan":
        for label, data in [("历史", hist), ("旧版", old), ("新版", new)]:
            if not data:
                continue
            actions = data.get("actions") or []
            types = [a.get("type") for a in actions if isinstance(a, dict)]
            notes.append(f"{label} actions={len(actions)} types={types[:8]}")
        if old and new:
            old_n = len(old.get("actions") or [])
            new_n = len(new.get("actions") or [])
            if old_n != new_n:
                notes.append(f"⚠ 动作数变化 {old_n} → {new_n}")

    elif stage == "element_decide":
        for label, data in [("历史", hist), ("旧版", old), ("新版", new)]:
            if data and "index" in data:
                notes.append(f"{label} index={data.get('index')}")
        if old and new and old.get("index") != new.get("index"):
            notes.append(f"⚠ 元素编号变化 {old.get('index')} → {new.get('index')}")

    else:
        notes.append("已记录 JSON 差异, 见 results.json")

    return notes


def _replay_samples(
    samples: list[ReplaySample],
    llm: Optional[LLMAdapter],
    prompts: PromptLoader,
    skill_text: str,
    dry_run: bool,
) -> None:
    for s in samples:
        if dry_run or llm is None:
            hist, _ = _safe_parse_json(s.historical_raw)
            s.old_data = hist
            s.new_data = hist
            s.notes = [f"dry-run: 历史 raw 长度 {len(s.historical_raw)}"]
            continue

        old_sys = _system_for(s.stage, "old", prompts, skill_text)
        new_sys = _system_for(s.stage, "new", prompts, skill_text)

        s.old_data, s.old_error = _call_llm(llm, s.stage, old_sys, s.user)
        s.new_data, s.new_error = _call_llm(llm, s.stage, new_sys, s.user)

        hist, _ = _safe_parse_json(s.historical_raw)
        s.notes = _compare_stage(s.stage, hist, s.old_data, s.new_data)
        if s.old_error:
            s.notes.append(f"旧版调用失败: {s.old_error[:120]}")
        if s.new_error:
            s.notes.append(f"新版调用失败: {s.new_error[:120]}")


def _summary_stats(samples: list[ReplaySample]) -> dict[str, Any]:
    by_stage: dict[str, dict[str, int]] = {}
    for s in samples:
        bucket = by_stage.setdefault(s.stage, {"total": 0, "old_err": 0, "new_err": 0, "diff": 0})
        bucket["total"] += 1
        if s.old_error:
            bucket["old_err"] += 1
        if s.new_error:
            bucket["new_err"] += 1
        if s.old_data != s.new_data and not s.old_error and not s.new_error:
            bucket["diff"] += 1
    return by_stage


def _render_markdown(samples: list[ReplaySample], args: argparse.Namespace, out_dir: Path) -> str:
    stats = _summary_stats(samples)
    lines = [
        "# 提示词回放对比报告",
        "",
        f"- 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 输入: `{args.target}`",
        f"- 环节: {', '.join(args.stages)}",
        f"- 每文件每环节上限: {args.limit}",
        f"- 模式: {'dry-run (未调 LLM)' if args.dry_run else '在线回放'}",
        "",
        "## 汇总",
        "",
        "| 环节 | 样本数 | 旧版失败 | 新版失败 | 旧新 JSON 不同 |",
        "|------|--------|----------|----------|----------------|",
    ]
    for stage in args.stages:
        b = stats.get(stage, {})
        lines.append(
            f"| {stage} | {b.get('total', 0)} | {b.get('old_err', 0)} | "
            f"{b.get('new_err', 0)} | {b.get('diff', 0)} |"
        )

    lines.extend(["", "## 样本明细", ""])
    for i, s in enumerate(samples, 1):
        rel = Path(s.source).relative_to(ROOT) if Path(s.source).is_relative_to(ROOT) else s.source
        lines.append(f"### {i}. {s.stage} — {s.intent_hint}")
        lines.append("")
        lines.append(f"- 来源: `{rel}`" + (f" step={s.step_no}" if s.step_no else ""))
        for note in s.notes:
            lines.append(f"- {note}")
        lines.append("")
        lines.append("<details><summary>历史 / 旧版 / 新版 JSON</summary>")
        lines.append("")
        lines.append("```json")
        hist, _ = _safe_parse_json(s.historical_raw)
        lines.append(json.dumps({"historical": hist, "old": s.old_data, "new": s.new_data}, ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="从可观测性.json 回放并对比旧/新提示词")
    ap.add_argument("target", help="可观测性.json 文件或包含它的目录")
    ap.add_argument("--config", default="config.yaml", help="LLM 配置")
    ap.add_argument("--stages", default="post_check,readiness,action_plan",
                    help="逗号分隔的环节名")
    ap.add_argument("--limit", type=int, default=5, help="每个文件每个环节最多回放条数")
    ap.add_argument("--dry-run", action="store_true", help="只收集样本并解析历史 raw, 不调 LLM")
    ap.add_argument("--output", default="", help="报告目录, 默认 输出/提示词回放/<时间戳>")
    args = ap.parse_args()

    args.stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    unknown = set(args.stages) - set(REPLAY_STAGES)
    if unknown:
        print(f"未知环节: {unknown}, 可选: {', '.join(REPLAY_STAGES)}", file=sys.stderr)
        return 2

    target = Path(args.target)
    if not target.is_absolute():
        target = ROOT / target
    obs_files = _discover_obs_files(target)
    if not obs_files:
        print(f"未找到 可观测性.json: {target}", file=sys.stderr)
        return 2

    config = yaml.safe_load((ROOT / args.config).read_text(encoding="utf-8"))
    prompts = PromptLoader(ROOT / "prompts", config.get("llm", {}).get("prompts"))
    skill_text = load_skill_text(ROOT / "prompts" / "skill.md")

    samples: list[ReplaySample] = []
    for obs in obs_files:
        samples.extend(_collect_samples(obs, set(args.stages), args.limit))

    if not samples:
        print("未找到匹配环节的 LLM 调用记录", file=sys.stderr)
        return 2

    llm: Optional[LLMAdapter] = None
    if not args.dry_run:
        llm = LLMAdapter(config["llm"])

    _replay_samples(samples, llm, prompts, skill_text, args.dry_run)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.output) if args.output else ROOT / "输出" / "提示词回放" / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    report_md = _render_markdown(samples, args, out_dir)
    (out_dir / "report.md").write_text(report_md, encoding="utf-8")

    results = {
        "meta": {
            "target": str(target),
            "stages": args.stages,
            "limit": args.limit,
            "dry_run": args.dry_run,
            "obs_files": [str(p) for p in obs_files],
            "sample_count": len(samples),
        },
        "summary": _summary_stats(samples),
        "samples": [asdict(s) for s in samples],
    }
    (out_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"回放完成: {len(samples)} 条样本")
    print(f"报告: {out_dir / 'report.md'}")
    print(f"数据: {out_dir / 'results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
