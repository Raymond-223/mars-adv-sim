"""Repository abstraction for memory records.

Business modules depend only on ``MemoryRepository``.  Redis is one adapter,
not an assumption baked into recall/ranking logic.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Protocol, Sequence, Set

from .models import MemoryRecord, MemoryType, VerificationStatus


class MemoryRepository(Protocol):
    def save(self, record: MemoryRecord, ttl_seconds: Optional[int] = None) -> MemoryRecord: ...
    def get(self, memory_id: str) -> Optional[MemoryRecord]: ...
    def update(self, record: MemoryRecord, ttl_seconds: Optional[int] = None) -> MemoryRecord: ...
    def delete(self, memory_id: str) -> bool: ...
    def query(
        self,
        *,
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
        node_id: Optional[str] = None,
        memory_type: Optional[MemoryType] = None,
        statuses: Optional[Iterable[VerificationStatus]] = None,
        tags: Optional[Iterable[str]] = None,
        limit: int = 50,
    ) -> List[MemoryRecord]: ...
    def query_by_node(self, run_id: str, node_id: str, limit: int = 50) -> List[MemoryRecord]: ...
    def query_by_nodes(self, run_id: str, node_ids: Iterable[str], limit: int = 200) -> List[MemoryRecord]: ...
    def query_by_evidence(self, evidence_id: str, limit: int = 50) -> List[MemoryRecord]: ...
    def get_many(self, memory_ids: Iterable[str]) -> List[MemoryRecord]: ...
    def all(self) -> List[MemoryRecord]: ...


def _matches(
    record: MemoryRecord,
    *,
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
    node_id: Optional[str] = None,
    memory_type: Optional[MemoryType] = None,
    statuses: Optional[Iterable[VerificationStatus]] = None,
    tags: Optional[Iterable[str]] = None,
) -> bool:
    if run_id is not None and record.run_id != run_id:
        return False
    if task_id is not None and record.task_id != task_id:
        return False
    if node_id is not None and record.node_id != node_id:
        return False
    if memory_type is not None and record.memory_type != memory_type:
        return False
    if statuses is not None and record.verification_status not in set(statuses):
        return False
    if tags is not None and not set(tags).intersection(record.tags):
        return False
    return True


class InMemoryRepository:
    def __init__(self):
        self.records: Dict[str, MemoryRecord] = {}
        self.by_node: Dict[str, Set[str]] = defaultdict(set)
        self.by_evidence: Dict[str, Set[str]] = defaultdict(set)

    def _unindex(self, record: MemoryRecord) -> None:
        self.by_node[f"{record.run_id}:{record.node_id}"].discard(record.memory_id)
        for evidence_id in record.evidence_refs:
            self.by_evidence[evidence_id].discard(record.memory_id)

    def save(self, record: MemoryRecord, ttl_seconds: Optional[int] = None) -> MemoryRecord:
        old = self.records.get(record.memory_id)
        if old:
            self._unindex(old)
        self.records[record.memory_id] = record
        self.by_node[f"{record.run_id}:{record.node_id}"].add(record.memory_id)
        for evidence_id in record.evidence_refs:
            self.by_evidence[evidence_id].add(record.memory_id)
        return record

    def get(self, memory_id: str) -> Optional[MemoryRecord]:
        return self.records.get(memory_id)

    def get_many(self, memory_ids: Iterable[str]) -> List[MemoryRecord]:
        return [self.records[mid] for mid in dict.fromkeys(memory_ids) if mid in self.records]

    def update(self, record: MemoryRecord, ttl_seconds: Optional[int] = None) -> MemoryRecord:
        return self.save(record, ttl_seconds=ttl_seconds)

    def delete(self, memory_id: str) -> bool:
        record = self.records.pop(memory_id, None)
        if record is None:
            return False
        self._unindex(record)
        return True

    def query(
        self,
        *,
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
        node_id: Optional[str] = None,
        memory_type: Optional[MemoryType] = None,
        statuses: Optional[Iterable[VerificationStatus]] = None,
        tags: Optional[Iterable[str]] = None,
        limit: int = 50,
    ) -> List[MemoryRecord]:
        result = [
            r for r in self.records.values()
            if _matches(
                r, run_id=run_id, task_id=task_id, node_id=node_id,
                memory_type=memory_type, statuses=statuses, tags=tags,
            )
        ]
        result.sort(key=lambda r: r.created_at, reverse=True)
        return result[: max(0, limit)]

    def query_by_node(self, run_id: str, node_id: str, limit: int = 50) -> List[MemoryRecord]:
        records = self.get_many(self.by_node.get(f"{run_id}:{node_id}", set()))
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records[:limit]

    def query_by_nodes(self, run_id: str, node_ids: Iterable[str], limit: int = 200) -> List[MemoryRecord]:
        ids: Set[str] = set()
        for node_id in node_ids:
            ids.update(self.by_node.get(f"{run_id}:{node_id}", set()))
        records = self.get_many(ids)
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records[:limit]

    def query_by_evidence(self, evidence_id: str, limit: int = 50) -> List[MemoryRecord]:
        records = self.get_many(self.by_evidence.get(evidence_id, set()))
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records[:limit]

    def all(self) -> List[MemoryRecord]:
        return list(self.records.values())


class RedisMemoryRepository:
    """Redis-backed repository with explicit secondary indexes.

    Redis remains replaceable behind the Repository boundary.  Query paths use
    indexes rather than a full key scan in normal operation; a legacy scan is
    kept only as a compatibility fallback for old archives.
    """

    ALL_INDEX = "memory:index:all"

    def __init__(self, store):
        self.store = store

    def _key(self, memory_id: str) -> str:
        return f"memory:{memory_id}"

    def _index_keys(self, record: MemoryRecord) -> List[str]:
        keys = [
            self.ALL_INDEX,
            f"memory:index:run:{record.run_id}",
            f"memory:index:task:{record.run_id}:{record.task_id}",
            f"memory:index:node:{record.run_id}:{record.node_id}",
            f"memory:index:type:{record.run_id}:{record.memory_type.value}",
            f"memory:index:status:{record.run_id}:{record.verification_status.value}",
        ]
        keys.extend(f"memory:index:evidence:{eid}" for eid in record.evidence_refs)
        keys.extend(f"memory:index:tag:{record.run_id}:{tag}" for tag in record.tags)
        return keys

    def _unindex(self, record: MemoryRecord) -> None:
        for key in self._index_keys(record):
            self.store.srem(key, [record.memory_id])

    def save(self, record: MemoryRecord, ttl_seconds: Optional[int] = None) -> MemoryRecord:
        old = self.get(record.memory_id)
        if old:
            self._unindex(old)
        self.store.set_json(self._key(record.memory_id), record.to_dict(), ttl_seconds=ttl_seconds)
        for key in self._index_keys(record):
            self.store.sadd(key, [record.memory_id])
        return record

    def get(self, memory_id: str) -> Optional[MemoryRecord]:
        data = self.store.get_json(self._key(memory_id))
        return MemoryRecord.from_dict(data) if data else None

    def get_many(self, memory_ids: Iterable[str]) -> List[MemoryRecord]:
        ids = list(dict.fromkeys(memory_ids))
        data = self.store.mget_json([self._key(mid) for mid in ids]) if ids else []
        return [MemoryRecord.from_dict(item) for item in data if item]

    def update(self, record: MemoryRecord, ttl_seconds: Optional[int] = None) -> MemoryRecord:
        return self.save(record, ttl_seconds=ttl_seconds)

    def delete(self, memory_id: str) -> bool:
        record = self.get(memory_id)
        if record is None:
            return False
        self._unindex(record)
        self.store.delete(self._key(memory_id))
        return True

    def _candidate_ids(
        self,
        *,
        run_id: Optional[str],
        task_id: Optional[str],
        node_id: Optional[str],
        memory_type: Optional[MemoryType],
        statuses: Optional[Iterable[VerificationStatus]],
        tags: Optional[Iterable[str]],
    ) -> Sequence[str]:
        sets: List[Set[str]] = []
        if run_id is not None:
            sets.append(set(self.store.smembers(f"memory:index:run:{run_id}")))
            if task_id is not None:
                sets.append(set(self.store.smembers(f"memory:index:task:{run_id}:{task_id}")))
            if node_id is not None:
                sets.append(set(self.store.smembers(f"memory:index:node:{run_id}:{node_id}")))
            if memory_type is not None:
                sets.append(set(self.store.smembers(f"memory:index:type:{run_id}:{memory_type.value}")))
            if statuses:
                status_union: Set[str] = set()
                for status in statuses:
                    status_union.update(self.store.smembers(f"memory:index:status:{run_id}:{status.value}"))
                sets.append(status_union)
            if tags:
                tag_union: Set[str] = set()
                for tag in tags:
                    tag_union.update(self.store.smembers(f"memory:index:tag:{run_id}:{tag}"))
                sets.append(tag_union)
        if not sets:
            ids = set(self.store.smembers(self.ALL_INDEX))
            if not ids:  # compatibility with old Redis archives
                ids = {k.split("memory:", 1)[1] for k in self.store.scan_keys("memory:mem_*")}
            return list(ids)
        ids = sets[0]
        for s in sets[1:]:
            ids &= s
        return list(ids)

    def query(
        self,
        *,
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
        node_id: Optional[str] = None,
        memory_type: Optional[MemoryType] = None,
        statuses: Optional[Iterable[VerificationStatus]] = None,
        tags: Optional[Iterable[str]] = None,
        limit: int = 50,
    ) -> List[MemoryRecord]:
        status_list = list(statuses) if statuses is not None else None
        tag_list = list(tags) if tags is not None else None
        ids = self._candidate_ids(
            run_id=run_id, task_id=task_id, node_id=node_id,
            memory_type=memory_type, statuses=status_list, tags=tag_list,
        )
        records = [
            r for r in self.get_many(ids)
            if _matches(
                r, run_id=run_id, task_id=task_id, node_id=node_id,
                memory_type=memory_type, statuses=status_list, tags=tag_list,
            )
        ]
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records[:limit]

    def query_by_node(self, run_id: str, node_id: str, limit: int = 50) -> List[MemoryRecord]:
        ids = self.store.smembers(f"memory:index:node:{run_id}:{node_id}")
        records = self.get_many(ids)
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records[:limit]

    def query_by_nodes(self, run_id: str, node_ids: Iterable[str], limit: int = 200) -> List[MemoryRecord]:
        ids: Set[str] = set()
        for node_id in node_ids:
            ids.update(self.store.smembers(f"memory:index:node:{run_id}:{node_id}"))
        records = self.get_many(ids)
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records[:limit]

    def query_by_evidence(self, evidence_id: str, limit: int = 50) -> List[MemoryRecord]:
        ids = self.store.smembers(f"memory:index:evidence:{evidence_id}")
        records = self.get_many(ids)
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records[:limit]

    def all(self) -> List[MemoryRecord]:
        ids = self.store.smembers(self.ALL_INDEX)
        if not ids:
            ids = [k.split("memory:", 1)[1] for k in self.store.scan_keys("memory:mem_*")]
        return self.get_many(ids)
