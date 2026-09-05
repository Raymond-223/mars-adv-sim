"""Semantic memory for stable facts and long-term constraints."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from .models import MemoryRecord, MemoryType, VerificationStatus
from .repository import MemoryRepository


def _text_from(value: Any, keys: Iterable[str]) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in keys:
            text = value.get(key)
            if text:
                return str(text).strip()
    if value is None:
        return ""
    return str(value).strip()


class SemanticMemory:
    def __init__(self, repository: MemoryRepository):
        self.repository = repository

    def ingest_goal_spec(self, run_id: str, task_id: str, node_id: str, goal_spec: Dict[str, Any]) -> List[MemoryRecord]:
        """Persist GoalSpec core in non-compressible semantic memory.

        Supports the canonical project schema (``objective``,
        ``hard_constraints``...) and the legacy ``main_goal.goal_text`` shape so
        the memory module can be integrated before other members' source code is
        available.
        """
        records: List[MemoryRecord] = []
        objective = goal_spec.get("objective")
        if not objective:
            main_goal = goal_spec.get("main_goal", {})
            objective = _text_from(main_goal, ["goal_text", "objective", "text"])
        objective_text = _text_from(objective, ["goal_text", "objective", "text"])
        if objective_text:
            records.append(self.add_fact(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                fact=objective_text,
                title="Goal",
                tags=["goal"],
                compressible=False,
                confidence=0.9,
                metadata={"goal_id": goal_spec.get("goal_id"), "source_spans": goal_spec.get("source_spans", [])},
            ))

        for item in goal_spec.get("hard_constraints", []) or []:
            text = _text_from(item, ["constraint", "text", "rule", "value"])
            if not text:
                continue
            records.append(self.add_fact(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                fact=text,
                title="Hard constraint",
                tags=["hard_constraint"],
                compressible=False,
                confidence=0.95,
                metadata={"source_item": item},
            ))

        # Explicit prohibition field is supported for scenario adapters.  A
        # prohibition remains non-compressible regardless of caller input.
        for item in goal_spec.get("prohibitions", []) or []:
            text = _text_from(item, ["rule", "constraint", "text", "value"])
            if not text:
                continue
            records.append(self.add_fact(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                fact=text,
                title="Prohibition",
                tags=["prohibition"],
                compressible=False,
                confidence=0.98,
                metadata={"source_item": item},
            ))
        return records

    def add_fact(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str,
        fact: str,
        title: str = "Fact",
        source: str = "semantic_memory",
        evidence_refs: Optional[List[str]] = None,
        tags: Optional[Iterable[str]] = None,
        compressible: bool = True,
        confidence: float = 0.65,
        importance: Optional[float] = None,
        access_scope: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryRecord:
        record = MemoryRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            memory_type=MemoryType.SEMANTIC,
            content=fact,
            summary=title,
            importance=importance if importance is not None else (0.9 if not compressible else 0.6),
            confidence=confidence,
            source=source,
            evidence_refs=evidence_refs or [],
            access_scope=access_scope or ["default"],
            verification_status=VerificationStatus.UNVERIFIED,
            compressible=compressible,
            tags=list(tags or ["fact"]),
            metadata=dict(metadata or {}),
        )
        return self.repository.save(record)

    def verify_fact(self, memory_id: str, evidence_id: Optional[str] = None) -> MemoryRecord:
        record = self.repository.get(memory_id)
        if not record:
            raise KeyError(f"memory not found: {memory_id}")
        record.verification_status = VerificationStatus.VERIFIED
        record.confidence = max(record.confidence, 0.9)
        if evidence_id and evidence_id not in record.evidence_refs:
            record.evidence_refs.append(evidence_id)
        return self.repository.update(record)
