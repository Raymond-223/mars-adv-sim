"""Recall core following the required retrieval order.

Order: Working Memory -> TaskGraph neighbourhood -> Evidence-related memories ->
core semantic facts -> vector candidates -> permission/status filter -> ranking/cut.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from .config import MemoryConfig
from .models import MemoryRecord, MemoryType, VerificationStatus
from .ranking import is_forced, rank_records, scope_allowed
from .repository import MemoryRepository
from .vector_index import VectorIndex
from .working_memory import WorkingMemory


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

        # 1) TaskGraph predecessor / neighbourhood memories.
        graph_nodes = list(dict.fromkeys(taskgraph_nodes or []))
        if node_id not in graph_nodes:
            graph_nodes.append(node_id)
        candidates.extend(self.repository.query_by_nodes(run_id, graph_nodes, limit=300))

        # 2) Evidence-related memories are explicitly joined, not merely given a score bonus.
        for evidence_id in dict.fromkeys(evidence_ids or []):
            candidates.extend(
                r for r in self.repository.query_by_evidence(evidence_id, limit=100)
                if r.run_id == run_id
            )

        # 3) Stable semantic core for the run/task.  Core facts must survive 1000+ steps.
        core = self.repository.query(
            run_id=run_id,
            task_id=task_id,
            memory_type=MemoryType.SEMANTIC,
            statuses=valid_statuses,
            tags=["goal", "hard_constraint", "prohibition"],
            limit=500,
        )
        candidates.extend(core)

        # 4) Other task-scoped facts/episodes/procedures.
        candidates.extend(self.repository.query(
            run_id=run_id,
            task_id=task_id,
            statuses=valid_statuses,
            limit=max(500, limit * 20),
        ))

        # 5) Vector index is candidate recall only.
        if query:
            vector_limit = self.config.vector_candidate_limit if self.config else 50
            for record in self.repository.get_many(self.vector_index.query(query, limit=vector_limit)):
                if record.run_id == run_id:
                    candidates.append(record)

        candidates = self._dedupe(candidates)
        candidates = [
            r for r in candidates
            if r.verification_status in set(valid_statuses) and scope_allowed(r, allowed_scopes)
        ]

        ranked = rank_records(
            candidates,
            query=query,
            taskgraph_nodes=graph_nodes,
            allowed_scopes=allowed_scopes,
            evidence_ids=evidence_ids,
        )

        forced = [r for r in ranked if is_forced(r)]
        normal = [r for r in ranked if not is_forced(r)]
        # Safety-critical records never lose a competition for the recall limit.
        selected = forced + normal[: max(0, limit - len(forced))]
        selected = self._dedupe(selected)

        return {
            "working": working,
            "records": selected,
            "semantic": [r for r in selected if r.memory_type == MemoryType.SEMANTIC],
            "episodic": [r for r in selected if r.memory_type == MemoryType.EPISODIC],
            "procedural": [r for r in selected if r.memory_type == MemoryType.PROCEDURAL],
            "candidate_count": len(candidates),
        }
