"""LLM 子系统: 适配器 + 提示词加载."""
from .adapter import LLMAdapter, LLMResult
from .prompt_loader import PromptLoader

__all__ = ["LLMAdapter", "LLMResult", "PromptLoader"]
