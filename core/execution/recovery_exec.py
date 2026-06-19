"""就绪恢复动作执行: post_verify + skip_main 判定 (参考 V3)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from ..planning import PlannedAction

_SUBMIT_KEYWORDS = (
    "保存", "提交", "确认", "发布", "完成", "新增", "创建",
    "save", "submit", "confirm", "create",
)


@dataclass
class RecoveryStepOutcome:
    """单条 recovery 的执行结果."""

    action: PlannedAction
    dispatch_ok: bool
    post_ok: Optional[bool] = None
    message: str = ""

    @property
    def success(self) -> bool:
        if self.post_ok is None:
            return self.dispatch_ok
        return self.dispatch_ok and self.post_ok


@dataclass
class RecoveryExecResult:
    seq: int
    outcomes: list[RecoveryStepOutcome] = field(default_factory=list)
    skip_main: bool = False


def should_skip_main_after_recovery(
    outcomes: list[RecoveryStepOutcome],
    main_action: PlannedAction,
) -> bool:
    """恢复动作已成功完成与主动作相同的提交/保存类意图 → 跳过主动作."""
    if not outcomes or not main_action:
        return False

    main_intent = (main_action.intent or "").lower().strip()
    main_type = (main_action.type or "").lower().strip()
    if not main_intent or main_type not in ("click", "fill", "upload"):
        return False

    if not any(k in main_intent for k in _SUBMIT_KEYWORDS):
        return False

    for outcome in outcomes:
        if not outcome.success:
            continue
        rec = outcome.action
        rec_type = (rec.type or "").lower().strip()
        rec_intent = (rec.intent or "").lower().strip()
        if rec_type != main_type:
            continue
        shared = [
            k for k in _SUBMIT_KEYWORDS
            if len(k) >= 2 and k in rec_intent and k in main_intent
        ]
        if len(shared) >= 2:
            return True
        if not shared:
            continue
        er_remain, main_remain = rec_intent, main_intent
        for k in shared:
            er_remain = er_remain.replace(k, "", 1)
            main_remain = main_remain.replace(k, "", 1)
        er_tokens = set(re.split(r"[\s,，。、]+", er_remain)) - {""}
        main_tokens = set(re.split(r"[\s,，。、]+", main_remain)) - {""}
        overlap = len(er_tokens & main_tokens) / max(len(er_tokens | main_tokens), 1)
        if overlap > 0.5:
            return True
    return False
