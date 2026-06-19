"""报告水印：从 runner.watermark 配置生成 HTML/JSON 标记."""
from __future__ import annotations

from html import escape
from typing import Any, Mapping, MutableMapping, Optional

DEFAULT_WATERMARK: dict[str, Any] = {
    "enabled": True,
    "banner": "高文苹",
    "footer": "高文苹",
    "overlay": "高文苹",
    "show_in_json": True,
}


def load_watermark_config(cfg: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """合并 runner.watermark 与默认值."""
    raw: dict[str, Any] = {}
    if cfg:
        wm = cfg.get("watermark")
        if isinstance(wm, dict):
            raw = wm
    out = dict(DEFAULT_WATERMARK)
    out.update(raw)
    out["enabled"] = bool(out.get("enabled", True))
    out["show_in_json"] = bool(out.get("show_in_json", True))
    for key in ("banner", "footer", "overlay"):
        if out.get(key) is not None:
            out[key] = str(out[key]).strip()
    return out


def watermark_json_meta(cfg: Mapping[str, Any]) -> dict[str, Any]:
    if not cfg.get("enabled") or not cfg.get("show_in_json"):
        return {}
    return {
        "_watermark": {
            "banner": cfg.get("banner") or "",
            "footer": cfg.get("footer") or "",
            "overlay": cfg.get("overlay") or cfg.get("banner") or "",
            "generator": "ui-automation",
        }
    }


def apply_watermark_to_report(
    report_data: MutableMapping[str, Any],
    cfg: Mapping[str, Any],
) -> None:
    meta = watermark_json_meta(cfg)
    if meta:
        report_data.update(meta)


def watermark_html_banner(cfg: Mapping[str, Any]) -> str:
    if not cfg.get("enabled"):
        return ""
    text = (cfg.get("banner") or "").strip()
    if not text:
        return ""
    return (
        f'<div class="mb-4 rounded-lg border border-indigo-100 bg-indigo-50 px-4 py-2 '
        f'text-center text-sm font-medium text-indigo-700">{escape(text)}</div>'
    )


def watermark_html_footer(cfg: Mapping[str, Any]) -> str:
    if not cfg.get("enabled"):
        return ""
    text = (cfg.get("footer") or "ui-automation").strip()
    return f'<footer class="text-center text-gray-500 text-xs mt-10">{escape(text)}</footer>'


def watermark_html_overlay(cfg: Mapping[str, Any]) -> str:
    if not cfg.get("enabled"):
        return ""
    text = (cfg.get("overlay") or cfg.get("banner") or "").strip()
    if not text:
        return ""
    return f"""
<div class="ui-watermark-overlay" aria-hidden="true">{escape(text)}</div>
<style>
.ui-watermark-overlay {{
  position: fixed; inset: 0; pointer-events: none; z-index: 9999;
  display: flex; align-items: center; justify-content: center;
  font-size: clamp(2rem, 8vw, 4.5rem); font-weight: 700;
  color: rgba(15, 23, 42, 0.05); transform: rotate(-24deg); user-select: none;
}}
</style>
"""


def watermark_html_extras(cfg: Mapping[str, Any]) -> str:
    return watermark_html_banner(cfg) + watermark_html_overlay(cfg)
