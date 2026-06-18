"""DOM 子系统: 语义DOM抽取 (步骤⑧)."""
from .semantic_dom import DomIndex, build_selector, extract_dom_index, extract_semantic_dom, wait_for_dom_stable

__all__ = ["extract_semantic_dom", "extract_dom_index", "DomIndex", "build_selector", "wait_for_dom_stable"]
