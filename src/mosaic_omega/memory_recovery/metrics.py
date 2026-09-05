"""Metrics required for long-memory evaluation and ablation."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict


@dataclass
class MemoryMetrics:
    recall_requests: int = 0
    recall_hits: int = 0
    recall_expected: int = 0
    recall_latency_ms_sum: float = 0.0
    context_pack_tokens: int = 0
    full_history_tokens: int = 0
    compression_ratio_sum: float = 0.0
    compression_samples: int = 0
    snapshot_creates: int = 0
    snapshot_restores: int = 0
    snapshot_consistency_checks: int = 0
    snapshot_consistency_passes: int = 0
    invalidations: int = 0
    invalidation_checks: int = 0
    invalidation_passes: int = 0

    def record_recall(self, *, hits: int, latency_ms: float = 0.0, expected: int = 0) -> None:
        self.recall_requests += 1
        self.recall_hits += max(0, hits)
        self.recall_expected += max(0, expected)
        self.recall_latency_ms_sum += max(0.0, latency_ms)

    def record_context_pack(self, *, context_tokens: int, full_history_tokens: int) -> None:
        self.context_pack_tokens += max(0, context_tokens)
        self.full_history_tokens += max(0, full_history_tokens)
        if full_history_tokens > 0:
            self.compression_ratio_sum += context_tokens / full_history_tokens
            self.compression_samples += 1

    def record_snapshot_create(self) -> None:
        self.snapshot_creates += 1

    def record_snapshot_restore(self, *, consistent: bool | None = None) -> None:
        self.snapshot_restores += 1
        if consistent is not None:
            self.snapshot_consistency_checks += 1
            if consistent:
                self.snapshot_consistency_passes += 1

    def record_invalidation(self, count: int = 1, *, correct: bool | None = None) -> None:
        self.invalidations += max(0, count)
        if correct is not None:
            self.invalidation_checks += 1
            if correct:
                self.invalidation_passes += 1

    def to_dict(self) -> Dict[str, float]:
        data = asdict(self)
        data["key_memory_recall_rate"] = (
            self.recall_hits / self.recall_expected if self.recall_expected else 0.0
        )
        data["avg_recall_latency_ms"] = (
            self.recall_latency_ms_sum / self.recall_requests if self.recall_requests else 0.0
        )
        data["avg_compression_ratio"] = (
            self.compression_ratio_sum / self.compression_samples if self.compression_samples else 0.0
        )
        data["snapshot_restore_consistency_rate"] = (
            self.snapshot_consistency_passes / self.snapshot_consistency_checks
            if self.snapshot_consistency_checks else 0.0
        )
        data["invalidation_propagation_accuracy"] = (
            self.invalidation_passes / self.invalidation_checks if self.invalidation_checks else 0.0
        )
        return data
