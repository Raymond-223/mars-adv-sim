"""Evidence/fact invalidation and dependency propagation."""
from __future__ import annotations

from typing import Iterable, List, Set

from .models import MemoryRecord, VerificationStatus
from .repository import MemoryRepository


class InvalidationManager:
    def __init__(self, repository: MemoryRepository):
        self.repository = repository

    def invalidate_memory(
        self,
        memory_id: str,
        *,
        status: VerificationStatus = VerificationStatus.STALE,
        reason: str = "",
        propagate: bool = True,
    ) -> MemoryRecord:
        record = self.repository.get(memory_id)
        if not record:
            raise KeyError(f"memory not found: {memory_id}")
        record.verification_status = status
        record.metadata["invalidation_reason"] = reason
        self.repository.update(record)
        if propagate:
            self.invalidate_dependents([memory_id], status=VerificationStatus.STALE, reason=reason)
        return record

    def invalidate_by_evidence(
        self,
        evidence_id: str,
        *,
        status: VerificationStatus = VerificationStatus.STALE,
        reason: str = "",
    ) -> List[MemoryRecord]:
        affected = self.repository.query_by_evidence(evidence_id, limit=5000)
        updated: List[MemoryRecord] = []
        ids: List[str] = []
        for record in affected:
            record.verification_status = status
            record.metadata["invalidation_reason"] = reason or f"evidence invalidated: {evidence_id}"
            updated.append(self.repository.update(record))
            ids.append(record.memory_id)
        updated.extend(self.invalidate_dependents(ids, status=VerificationStatus.STALE, reason=reason))
        # Dedupe if a dependent is also directly evidence-linked.
        return list({r.memory_id: r for r in updated}.values())

    def invalidate_dependents(
        self,
        root_memory_ids: Iterable[str],
        *,
        status: VerificationStatus = VerificationStatus.STALE,
        reason: str = "",
    ) -> List[MemoryRecord]:
        """Transitively invalidate memories declaring ``depends_on_memory_ids``."""
        roots: Set[str] = set(root_memory_ids)
        if not roots:
            return []
        updated: List[MemoryRecord] = []
        changed = True
        while changed:
            changed = False
            for record in self.repository.all():
                if record.memory_id in roots:
                    continue
                deps = set(record.metadata.get("depends_on_memory_ids", []))
                if deps.intersection(roots) and record.verification_status not in {VerificationStatus.STALE, VerificationStatus.REJECTED}:
                    record.verification_status = status
                    record.metadata["invalidation_reason"] = reason or "dependency invalidated"
                    self.repository.update(record)
                    roots.add(record.memory_id)
                    updated.append(record)
                    changed = True
        return updated

    def reject_memory(self, memory_id: str, reason: str = "") -> MemoryRecord:
        return self.invalidate_memory(
            memory_id,
            status=VerificationStatus.REJECTED,
            reason=reason,
            propagate=True,
        )

    def mark_stale(self, memory_id: str, reason: str = "") -> MemoryRecord:
        return self.invalidate_memory(memory_id, status=VerificationStatus.STALE, reason=reason)
