from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class RecoveryAction(str, Enum):
    RETRY = "retry"
    REPLACE = "replace"
    ROLLBACK = "rollback"
    REPLAN = "replan"
    SAFE_STOP = "safe_stop"


@dataclass(frozen=True)
class RecoveryPlan:
    run_id: str
    failed_task_id: str
    action: RecoveryAction
    affected_task_ids: tuple[str, ...]
    reason: str
    retry_allowed: bool

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        raw["action"] = self.action.value
        return raw
