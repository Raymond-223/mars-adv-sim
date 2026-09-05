"""MemoryService: sole public business entrypoint for memory operations."""
from __future__ import annotations

import json
import time
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional

from ..storage import LocalObjectStore
from .adapters.redis_store import RedisStore
from .config import MemoryConfig, load_config
from .context_builder import ContextBuilder, estimate_tokens
from .episodic_memory import EpisodicMemory
from .invalidation import InvalidationManager
from .metrics import MemoryMetrics
from .models import ContextPack, MemoryEvent, MemoryRecord, VerificationStatus
from .procedural_memory import ProceduralMemory
from .repository import InMemoryRepository, MemoryRepository, RedisMemoryRepository
from .retriever import Retriever
from .semantic_memory import SemanticMemory
from .snapshot import SnapshotManager
from .vector_index import VectorIndex
from .working_memory import WorkingMemory


class MemoryService:
    def __init__(
        self,
        *,
        config: Optional[MemoryConfig] = None,
        repository: Optional[MemoryRepository] = None,
        use_redis: bool = False,
        redis_store=None,
    ):
        self.config = config or load_config()
        self.store = redis_store if redis_store is not None else (RedisStore(self.config.redis_url) if use_redis else None)
        self.repository: MemoryRepository = repository or (
            RedisMemoryRepository(self.store) if self.store else InMemoryRepository()
        )
        self.object_store = LocalObjectStore(self.config.object_store_dir)
        self.vector_index = VectorIndex()
        self.working_memory = WorkingMemory(self.config, store=self.store)
        self.episodic_memory = EpisodicMemory(self.repository)
        self.semantic_memory = SemanticMemory(self.repository)
        self.procedural_memory = ProceduralMemory(self.repository)
        self.retriever = Retriever(self.repository, self.working_memory, self.vector_index, self.config)
        self.context_builder = ContextBuilder(self.retriever, self.config)
        self.invalidation = InvalidationManager(self.repository)
        self.snapshot_manager = SnapshotManager(
            self.repository, self.object_store, self.config, working_memory=self.working_memory
        )
        self.metrics = MemoryMetrics()
        self._event_counts: Dict[str, int] = defaultdict(int)

    def set_working_state(self, run_id: str, node_id: str, **state: Any) -> Dict[str, Any]:
        return self.working_memory.set_state(run_id, node_id, **state)

    def ingest_event(self, event: MemoryEvent) -> MemoryRecord:
        record = self.episodic_memory.ingest_event(event)
        self.vector_index.upsert(record)
        self._event_counts[event.run_id] += 1
        every = self.config.snapshot_every_n_events
        if every > 0 and self._event_counts[event.run_id] % every == 0:
            self.create_snapshot(event.run_id)
        return record

    def ingest_goal_spec(self, run_id: str, task_id: str, node_id: str, goal_spec: Dict[str, Any]) -> List[MemoryRecord]:
        records = self.semantic_memory.ingest_goal_spec(run_id, task_id, node_id, goal_spec)
        for record in records:
            self.vector_index.upsert(record)
        return records

    def add_fact(self, **kwargs: Any) -> MemoryRecord:
        record = self.semantic_memory.add_fact(**kwargs)
        self.vector_index.upsert(record)
        return record

    def verify_fact(self, memory_id: str, evidence_id: Optional[str] = None) -> MemoryRecord:
        record = self.semantic_memory.verify_fact(memory_id, evidence_id=evidence_id)
        self.vector_index.upsert(record)
        return record

    def add_procedure(self, title: str, steps: List[str], **kwargs: Any) -> MemoryRecord:
        record = self.procedural_memory.add_procedure(title, steps, **kwargs)
        self.vector_index.upsert(record)
        return record

    def record_procedure_outcome(self, procedure_id: str, *, succeeded: bool) -> MemoryRecord:
        record = self.procedural_memory.record_outcome(procedure_id, succeeded=succeeded)
        self.vector_index.upsert(record)
        return record

    def retrieve(
        self,
        *,
        run_id: str,
        node_id: str,
        task_id: Optional[str] = None,
        taskgraph_nodes: Optional[Iterable[str]] = None,
        query: str = "",
        evidence_ids: Optional[Iterable[str]] = None,
        allowed_scopes: Optional[Iterable[str]] = None,
        limit: int = 10,
        expected_memory_ids: Optional[Iterable[str]] = None,
    ) -> Dict[str, object]:
        start = time.perf_counter()
        result = self.retriever.retrieve(
            run_id=run_id,
            node_id=node_id,
            task_id=task_id,
            taskgraph_nodes=taskgraph_nodes,
            query=query,
            evidence_ids=evidence_ids,
            allowed_scopes=allowed_scopes,
            limit=limit,
        )
        records: List[MemoryRecord] = result.get("records", [])  # type: ignore[assignment]
        expected = set(expected_memory_ids or [])
        hits = len(expected.intersection(r.memory_id for r in records)) if expected else len(records)
        self.metrics.record_recall(
            hits=hits,
            expected=len(expected),
            latency_ms=(time.perf_counter() - start) * 1000,
        )
        return result

    def build_context_pack(
        self,
        *,
        run_id: str,
        node_id: str,
        task_id: Optional[str] = None,
        taskgraph_nodes: Optional[Iterable[str]] = None,
        query: str = "",
        evidence_ids: Optional[Iterable[str]] = None,
        allowed_scopes: Optional[Iterable[str]] = None,
    ) -> ContextPack:
        pack = self.context_builder.build(
            run_id=run_id,
            node_id=node_id,
            task_id=task_id,
            taskgraph_nodes=taskgraph_nodes,
            query=query,
            evidence_ids=evidence_ids,
            allowed_scopes=allowed_scopes,
        )
        history = self.repository.query(
            run_id=run_id,
            task_id=task_id,
            statuses=[VerificationStatus.UNVERIFIED, VerificationStatus.VERIFIED],
            limit=100000,
        )
        full_history_text = json.dumps([r.to_dict() for r in history], ensure_ascii=False, separators=(",", ":"))
        full_tokens = estimate_tokens(full_history_text)
        pack.full_history_token_estimate = full_tokens
        pack.compression_ratio = (pack.token_estimate / full_tokens) if full_tokens else 0.0
        self.metrics.record_context_pack(
            context_tokens=pack.token_estimate,
            full_history_tokens=full_tokens,
        )
        return pack

    def invalidate_by_evidence(
        self,
        evidence_id: str,
        *,
        reason: str = "",
        status: VerificationStatus = VerificationStatus.STALE,
    ) -> List[MemoryRecord]:
        updated = self.invalidation.invalidate_by_evidence(evidence_id, reason=reason, status=status)
        for record in updated:
            self.vector_index.delete(record.memory_id)
        correct = all(
            self.repository.get(r.memory_id) is not None
            and self.repository.get(r.memory_id).verification_status in {VerificationStatus.STALE, VerificationStatus.REJECTED}  # type: ignore[union-attr]
            for r in updated
        )
        self.metrics.record_invalidation(len(updated), correct=correct)
        return updated

    def reject_memory(self, memory_id: str, reason: str = "") -> MemoryRecord:
        record = self.invalidation.reject_memory(memory_id, reason=reason)
        self.vector_index.delete(record.memory_id)
        self.metrics.record_invalidation(1, correct=record.verification_status == VerificationStatus.REJECTED)
        return record

    def create_snapshot(self, run_id: str):
        snapshot = self.snapshot_manager.create_snapshot(run_id)
        self.metrics.record_snapshot_create()
        return snapshot

    def restore_snapshot(
        self,
        snapshot_id_or_key: str,
        *,
        apply: bool = False,
        fallback_to_previous: bool = True,
    ) -> Dict[str, Any]:
        state = self.snapshot_manager.restore_snapshot(
            snapshot_id_or_key,
            apply=apply,
            fallback_to_previous=fallback_to_previous,
        )
        consistent = None
        if apply:
            consistent, _ = self.snapshot_manager.verify_consistency(state)
            self.rebuild_vector_index(run_id=state.get("run_id"))
        self.metrics.record_snapshot_restore(consistent=consistent)
        return state

    def rebuild_vector_index(self, *, run_id: Optional[str] = None) -> int:
        records = self.repository.query(
            run_id=run_id,
            statuses=[VerificationStatus.UNVERIFIED, VerificationStatus.VERIFIED],
            limit=100000,
        ) if run_id else [
            r for r in self.repository.all()
            if r.verification_status in {VerificationStatus.UNVERIFIED, VerificationStatus.VERIFIED}
        ]
        self.vector_index.rebuild(records)
        return len(records)

    def clear_vector_index(self) -> None:
        self.vector_index.clear()

    def observability_records(self, run_id: str, *, limit: int = 10000) -> List[MemoryRecord]:
        """Return a read-only run projection for observability.

        Console code never talks to Redis or repository adapters directly; the
        MemoryService remains the only business boundary for memory state.
        """
        return self.repository.query(run_id=run_id, limit=max(0, int(limit)))

    def export_metrics(self) -> Dict[str, float]:
        return self.metrics.to_dict()
