"""Configuration for the logical task communication topology."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TopologyConfig:
    """Safety and stability settings for one topology manager instance."""

    top_k: int = 2
    edge_score_threshold: float = 0.35
    min_hold_time_s: float = 5.0
    high_risk_priority: int = 8
    min_algebraic_connectivity: float = 0.0
    semantic_dedup_ttl_s: float = 600.0
    semantic_dedup_capacity: int = 4_096

    def __post_init__(self) -> None:
        if self.top_k < 0:
            raise ValueError("top_k must be non-negative")
        if not 0 <= self.edge_score_threshold <= 1:
            raise ValueError("edge_score_threshold must be within [0, 1]")
        if self.min_hold_time_s < 0:
            raise ValueError("min_hold_time_s must be non-negative")
        if not 1 <= self.high_risk_priority <= 10:
            raise ValueError("high_risk_priority must be within [1, 10]")
        if self.min_algebraic_connectivity < 0:
            raise ValueError("min_algebraic_connectivity must be non-negative")
