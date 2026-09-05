"""Estimate whether a context update can change a receiver's next decision."""

from __future__ import annotations

from dataclasses import dataclass

from ..agent_runtime.task_messages import DeltaOperation, TaskMessage


@dataclass(frozen=True)
class ImpactAssessment:
    score: float
    critical: bool
    reasons: tuple[str, ...]


class DecisionImpactEstimator:
    """Conservative, explainable first version of decision-impact scoring."""

    def assess(self, message: TaskMessage) -> ImpactAssessment:
        score = message.priority / 10
        reasons: list[str] = ["priority"]
        critical = message.priority >= 8
        if message.constraints:
            score = max(score, 0.85)
            critical = True
            reasons.append("constraints")
        if message.evidence_refs:
            score = max(score, 0.80)
            critical = True
            reasons.append("evidence")
        if any(item.op is DeltaOperation.REMOVE for item in message.facts + message.constraints):
            score = max(score, 0.75)
            reasons.append("retraction")
        return ImpactAssessment(min(score, 1.0), critical, tuple(reasons))
