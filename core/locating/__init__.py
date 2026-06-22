"""定位层: 五级降级链 (步骤⑨). L1缓存→L2记忆→L3规则→L4学习→L5大模型."""
from .cache import SelectorCache
from .llm_decider import LLMElementDecider
from .memory import SelectorMemory
from .resolver import LocatorResolver
from .structure_learner import StructureLearner

__all__ = [
    "LocatorResolver", "LLMElementDecider", "SelectorCache",
    "SelectorMemory", "StructureLearner",
]
