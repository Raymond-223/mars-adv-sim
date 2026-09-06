"""Recall core following the required retrieval order.

Order: Working Memory -> TaskGraph neighbourhood -> Evidence-related memories ->
core semantic facts -> vector candidates -> permission/status filter -> ranking/cut.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from .config import MemoryConfig
from .models import MemoryRecord, MemoryType, VerificationStatus
from .ranking import is_forced, rank_records, scope_allowed, score_record
from .repository import MemoryRepository
from .vector_index import VectorIndex
from .working_memory import WorkingMemory


def _digest(record: MemoryRecord) -> Dict[str, object]:
    """Compact, display-safe view of one memory record."""
    return {
        "memory_id": record.memory_id,
        "memory_type": record.memory_type.value if hasattr(record.memory_type, "value") else str(record.memory_type),
        "summary": (record.summary or record.content or "")[:160],
        "tags": list(record.tags),
        "node_id": record.node_id,
        "verification_status": (
            record.verification_status.value
            if hasattr(record.verification_status, "value") else str(record.verification_status)
        ),
        "importance": record.importance,
        "confidence": record.confidence,
        "created_at": record.created_at,
        "is_core_constraint": bool(record.is_core_constraint),
    }


class Retriever:
    def __init__(
        self,
        repository: MemoryRepository,
        working_memory: WorkingMemory,
        vector_index: Optional[VectorIndex] = None,
        config: Optional[MemoryConfig] = None,
    ):
        self.repository = repository
        self.working_memory = working_memory
        self.vector_index = vector_index or VectorIndex()
        self.config = config

    @staticmethod
    def _dedupe(records: Iterable[MemoryRecord]) -> List[MemoryRecord]:
        result: List[MemoryRecord] = []
        seen = set()
        for record in records:
            if record.memory_id not in seen:
                seen.add(record.memory_id)
                result.append(record)
        return result

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
    ) -> Dict[str, object]:
        working = self.working_memory.get_state(run_id, node_id)
        valid_statuses = [VerificationStatus.UNVERIFIED, VerificationStatus.VERIFIED]
        candidates: List[MemoryRecord] = []
        # Per-stage provenance so the console can show where each candidate came
        # from instead of only the finished ContextPack.
        stages: List[Dict[str, object]] = []

        def stage(name: str, description: str, found: List[MemoryRecord]) -> None:
            candidates.extend(found)
            stages.append({
                "stage": name,
                "description": description,
                "found": len(found),
                "running_total": len(candidates),
            })

        # 1) TaskGraph predecessor / neighbourhood memories.
        graph_nodes = list(dict.fromkeys(taskgraph_nodes or []))
        if node_id not in graph_nodes:
            graph_nodes.append(node_id)
        stage(
            "taskgraph_neighbourhood",
            "memories written by this node and its DAG predecessors",
            list(self.repository.query_by_nodes(run_id, graph_nodes, limit=300)),
        )

        # 2) Evidence-related memories are explicitly joined, not merely given a score bonus.
        evidence_joined: List[MemoryRecord] = []
        for evidence_id in dict.fromkeys(evidence_ids or []):
            evidence_joined.extend(
                r for r in self.repository.query_by_evidence(evidence_id, limit=100)
                if r.run_id == run_id
            )
        stage("evidence_join", "memories explicitly linked to required evidence", evidence_joined)

        # 3) Stable semantic core for the run/task.  Core facts must survive 1000+ steps.
        stage(
            "semantic_core",
            "goal / hard_constraint / prohibition facts that must survive long horizons",
            list(self.repository.query(
                run_id=run_id,
                task_id=task_id,
                memory_type=MemoryType.SEMANTIC,
                statuses=valid_statuses,
                tags=["goal", "hard_constraint", "prohibition"],
                limit=500,
            )),
        )

        # 4) Other task-scoped facts/episodes/procedures.
        stage(
            "task_scoped_history",
            "remaining task-scoped facts, episodes and procedures",
            list(self.repository.query(
                run_id=run_id,
                task_id=task_id,
                statuses=valid_statuses,
                limit=max(500, limit * 20),
            )),
        )

        # 5) Vector index is candidate recall only.
        if query:
            vector_limit = self.config.vector_candidate_limit if self.config else 50
            stage(
                "vector_recall",
                "similarity candidates; recall only, never a ranking authority",
                [
                    record
                    for record in self.repository.get_many(self.vector_index.query(query, limit=vector_limit))
                    if record.run_id == run_id
                ],
            )

        raw_count = len(candidates)
        candidates = self._dedupe(candidates)
        deduped_count = len(candidates)

        filtered_out: List[Dict[str, object]] = []
        kept: List[MemoryRecord] = []
        for record in candidates:
            if record.verification_status not in set(valid_statuses):
                filtered_out.append(_digest(record) | {
                    "drop_reason": f"verification_status={_digest(record)['verification_status']} is not recallable",
                })
            elif not scope_allowed(record, allowed_scopes):
                filtered_out.append(_digest(record) | {
                    "drop_reason": f"access_scope {list(record.access_scope)} is outside the caller's allowed scopes",
                })
            else:
                kept.append(record)
        candidates = kept

        ranked = rank_records(
            candidates,
            query=query,
            taskgraph_nodes=graph_nodes,
            allowed_scopes=allowed_scopes,
            evidence_ids=evidence_ids,
        )
        scores = {
            record.memory_id: round(score_record(
                record,
                query=query,
                taskgraph_nodes=graph_nodes,
                allowed_scopes=allowed_scopes,
                evidence_ids=evidence_ids,
            ), 6)
            for record in ranked
        }

        forced = [r for r in ranked if is_forced(r)]
        normal = [r for r in ranked if not is_forced(r)]
        # Safety-critical records never lose a competition for the recall limit.
        selected = forced + normal[: max(0, limit - len(forced))]
        selected = self._dedupe(selected)
        selected_ids = {r.memory_id for r in selected}

        trace = {
            "stages": stages,
            "raw_candidate_count": raw_count,
            "deduplicated_candidate_count": deduped_count,
            "duplicate_count": raw_count - deduped_count,
            "filtered_out": filtered_out[:40],
            "filtered_out_count": len(filtered_out),
            "recall_limit": limit,
            "forced_count": len(forced),
            "ranked": [
                _digest(record) | {"score": scores.get(record.memory_id), "selected": record.memory_id in selected_ids}
                for record in ranked[:60]
            ],
            "dropped_by_recall_limit": [
                _digest(record) | {
                    "score": scores.get(record.memory_id),
                    "drop_reason": f"ranked below the recall limit of {limit}",
                }
                for record in normal[max(0, limit - len(forced)):][:40]
            ],
            "selected_count": len(selected),
            "ranking_formula": (
                "0.26*taskgraph_relevance + 0.16*evidence_relevance + 0.18*importance + 0.10*confidence "
                "+ 0.10*recency + 0.06*novelty + 0.14*lexical_similarity (+0.12 if VERIFIED); "
                "core constraints are forced to 100.0 and never lose the recall competition"
            ),
        }

        return {
            "working": working,
            "records": selected,
            "semantic": [r for r in selected if r.memory_type == MemoryType.SEMANTIC],
            "episodic": [r for r in selected if r.memory_type == MemoryType.EPISODIC],
            "procedural": [r for r in selected if r.memory_type == MemoryType.PROCEDURAL],
            "candidate_count": len(candidates),
            "trace": trace,
        }
