"""Immutable domain models for task-specific communication topology."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet


@dataclass(frozen=True)
class EdgeCandidate:
    """A possible directed agent-to-agent communication edge.

    ``required`` is used for a task dependency or an explicitly requested
    control path.  Required edges are never removed only because Top-K is full.
    ``score`` is normalised to [0, 1] and ranks optional edges.
    """

    source: str
    target: str
    task_ids: FrozenSet[str] = frozenset()
    score: float = 0.5
    required: bool = False
    high_risk: bool = False
    reason: str = "task_relevance"

    def __post_init__(self) -> None:
        if not self.source or not self.target or self.source == self.target:
            raise ValueError("an edge requires two distinct non-empty agent IDs")
        if not 0 <= self.score <= 1:
            raise ValueError("edge score must be within [0, 1]")

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.target)


@dataclass(frozen=True)
class EdgeState:
    """One active communication edge and its stability window."""

    source: str
    target: str
    task_ids: FrozenSet[str]
    score: float
    required: bool
    high_risk: bool
    reason: str
    effective_from: float
    min_hold_until: float

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.target)


@dataclass(frozen=True)
class TopologySnapshot:
    """Inspectable current graph; timing fields are monotonic-clock values."""

    version: int
    nodes: tuple[str, ...]
    edges: tuple[EdgeState, ...]
    top_k: int
    effective_from: float
    min_hold_time_s: float
    connected: bool = True
    component_count: int = 0
    lambda2: float = 0.0
    spectral_target: float = 0.0
    spectral_target_met: bool = True
    generated_at: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "nodes": list(self.nodes),
            "edges": [
                {
                    "source": edge.source,
                    "target": edge.target,
                    "task_ids": sorted(edge.task_ids),
                    "score": edge.score,
                    "required": edge.required,
                    "high_risk": edge.high_risk,
                    "reason": edge.reason,
                    "effective_from": edge.effective_from,
                    "min_hold_until": edge.min_hold_until,
                }
                for edge in self.edges
            ],
            "edge_scores": {f"{edge.source}->{edge.target}": edge.score for edge in self.edges},
            "top_k": self.top_k,
            "effective_from": self.effective_from,
            "min_hold_time_s": self.min_hold_time_s,
            "connected": self.connected,
            "component_count": self.component_count,
            "lambda2": self.lambda2,
            "spectral_target": self.spectral_target,
            "spectral_target_met": self.spectral_target_met,
            "generated_at": self.generated_at,
        }

    def neighbors(self, agent_id: str) -> tuple[str, ...]:
        return tuple(sorted(edge.target for edge in self.edges if edge.source == agent_id))


@dataclass(frozen=True)
class RebuildResult:
    """The exact, inspectable effect of one incremental topology rebuild."""

    snapshot: TopologySnapshot
    added: tuple[EdgeState, ...] = ()
    removed: tuple[EdgeState, ...] = ()
    retained: tuple[EdgeState, ...] = ()
    affected_agents: FrozenSet[str] = frozenset()
    affected_task_ids: FrozenSet[str] = frozenset()
    full_rebuild: bool = False


@dataclass(frozen=True)
class KnowledgeDigest:
    """What one receiver has acknowledged for one task.

    The digest stores only fingerprints, never the original context text.  It
    is local metadata, not an extra field in the ten-field TaskMessage schema.
    """

    receiver: str
    task_id: str
    summary_hash: str | None = None
    fact_hashes: tuple[tuple[str, str], ...] = ()
    constraint_hashes: tuple[tuple[str, str], ...] = ()
    evidence_hashes: tuple[tuple[str, str], ...] = ()
    updated_at: float = 0.0

    def fact_map(self) -> dict[str, str]:
        return dict(self.fact_hashes)

    def constraint_map(self) -> dict[str, str]:
        return dict(self.constraint_hashes)

    def evidence_map(self) -> dict[str, str]:
        return dict(self.evidence_hashes)
