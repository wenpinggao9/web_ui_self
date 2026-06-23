"""DOM 子系统: 语义DOM抽取 (步骤⑧)."""
from .semantic_dom import (
    DomIndex,
    build_selector,
    compact_dom_lines,
    dom_index_from_items,
    extract_dom_index,
    extract_items,
    extract_semantic_dom,
    extract_semantic_items,
    format_indexed_dom_line,
    wait_for_dom_stable,
    wait_for_semantic_items_settle,
)

__all__ = [
    "extract_semantic_dom",
    "extract_semantic_items",
    "extract_dom_index",
    "extract_items",
    "dom_index_from_items",
    "compact_dom_lines",
    "format_indexed_dom_line",
    "DomIndex",
    "build_selector",
    "wait_for_dom_stable",
    "wait_for_semantic_items_settle",
]
