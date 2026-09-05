"""Deterministic scoring for optional task communication edges."""

from __future__ import annotations

from dataclasses import dataclass

from .models import EdgeCandidate


@dataclass(frozen=True)
class EdgeSignals:
    dependency_strength: float = 0.0
    information_value: float = 0.5
    reliability: float = 1.0
    latency_score: float = 1.0

    def __post_init__(self) -> None:
        if not all(0 <= value <= 1 for value in self.__dict__.values()):
            raise ValueError("edge signals must be within [0, 1]")


class EdgeScorer:
    """Scores an edge without coupling topology logic to network telemetry."""

    def score(self, signals: EdgeSignals) -> float:
        return round(
            0.40 * signals.dependency_strength
            + 0.30 * signals.information_value
            + 0.20 * signals.reliability
            + 0.10 * signals.latency_score,
            6,
        )

    def candidate(
        self,
        source: str,
        target: str,
        *,
        task_id: str,
        signals: EdgeSignals,
        required: bool = False,
        high_risk: bool = False,
        reason: str = "scored_relevance",
    ) -> EdgeCandidate:
        return EdgeCandidate(
            source=source,
            target=target,
            task_ids=frozenset({task_id}),
            score=self.score(signals),
            required=required,
            high_risk=high_risk,
            reason=reason,
        )
