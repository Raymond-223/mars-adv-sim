"""Pluggable candidate vector index.

This default implementation uses lexical similarity so it has no heavy runtime
dependency.  It is *never* a source of truth: deleting the index must not break
recovery because authoritative records remain in Repository/Event/TaskGraph.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Tuple

from .models import MemoryRecord
from .ranking import lexical_similarity


class VectorIndex:
    def __init__(self):
        self._documents: Dict[str, str] = {}

    def upsert(self, record: MemoryRecord) -> None:
        self._documents[record.memory_id] = "\n".join([
            record.summary, record.content, " ".join(record.tags)
        ])

    def delete(self, memory_id: str) -> None:
        self._documents.pop(memory_id, None)

    def clear(self) -> None:
        self._documents.clear()

    def query(self, query: str, limit: int = 20) -> List[str]:
        scored: List[Tuple[float, str]] = [
            (lexical_similarity(query, text), memory_id)
            for memory_id, text in self._documents.items()
        ]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [memory_id for score, memory_id in scored if score > 0][:limit]

    def rebuild(self, records: Iterable[MemoryRecord]) -> None:
        self.clear()
        for record in records:
            if record.verification_status.value not in {"STALE", "REJECTED"}:
                self.upsert(record)
