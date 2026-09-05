"""Procedural memory for reusable skills and standard workflows."""
from __future__ import annotations

from typing import Iterable, List, Optional
from uuid import uuid4

from .models import MemoryRecord, ProcedureRecord, VerificationStatus, utc_now
from .ranking import lexical_similarity
from .repository import MemoryRepository


class ProceduralMemory:
    def __init__(self, repository: MemoryRepository):
        self.repository = repository

    def add_procedure(
        self,
        title: str,
        steps: List[str],
        *,
        preconditions: Optional[List[str]] = None,
        success_criteria: Optional[List[str]] = None,
        tags: Optional[Iterable[str]] = None,
        source: str = "manual",
    ) -> MemoryRecord:
        procedure = ProcedureRecord(
            procedure_id=f"proc_{uuid4().hex}",
            title=title,
            steps=steps,
            preconditions=preconditions or [],
            success_criteria=success_criteria or [],
            tags=list(tags or []),
            source=source,
            verification_status=VerificationStatus.UNVERIFIED,
        )
        return self.repository.save(procedure.to_memory_record())

    def record_outcome(self, procedure_id: str, *, succeeded: bool) -> MemoryRecord:
        record = self.repository.get(procedure_id)
        if not record:
            raise KeyError(f"procedure not found: {procedure_id}")
        if succeeded:
            record.metadata["success_count"] = int(record.metadata.get("success_count", 0)) + 1
            if record.metadata["success_count"] >= 2:
                record.verification_status = VerificationStatus.VERIFIED
        else:
            record.metadata["failure_count"] = int(record.metadata.get("failure_count", 0)) + 1
        record.metadata["updated_at"] = utc_now()
        return self.repository.update(record)

    def suggest(self, query: str, limit: int = 5) -> List[MemoryRecord]:
        procedures = [r for r in self.repository.all() if r.memory_type.value == "procedural"]
        scored = [(lexical_similarity(query, r.summary + " " + r.content + " " + " ".join(r.tags)), r) for r in procedures]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for score, r in scored if score > 0][:limit]
