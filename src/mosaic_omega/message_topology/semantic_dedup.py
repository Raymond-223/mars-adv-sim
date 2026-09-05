"""Message-ID and content-hash de-duplication for QoS1 and repeated content."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import time

from ..agent_runtime.task_messages import TaskMessage


def content_hash(message: TaskMessage) -> str:
    """Hash business content, deliberately excluding transient message_id/ttl."""
    raw = message.to_dict()
    raw.pop("message_id")
    raw.pop("ttl")
    raw["facts"] = sorted(raw["facts"], key=lambda item: (item["id"], item["op"], item.get("text") or ""))
    raw["constraints"] = sorted(raw["constraints"], key=lambda item: (item["id"], item["op"], item.get("text") or ""))
    raw["evidence_refs"] = sorted(raw["evidence_refs"], key=lambda item: item["id"])
    canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DeduplicationResult:
    duplicate: bool
    reason: str | None
    content_hash: str


class SemanticDeduplicator:
    """Bounds recent ID and semantic-content history by capacity and time."""

    def __init__(self, ttl_s: float = 600.0, max_entries: int = 4_096) -> None:
        if ttl_s <= 0 or max_entries < 1:
            raise ValueError("ttl_s and max_entries must be positive")
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._ids: OrderedDict[str, float] = OrderedDict()
        self._hashes: OrderedDict[str, float] = OrderedDict()

    def check_and_record(self, message: TaskMessage, *, now: float | None = None) -> DeduplicationResult:
        result = self.check(message, now=now)
        if not result.duplicate:
            self.record(message, now=now)
        return result

    def check(self, message: TaskMessage, *, now: float | None = None) -> DeduplicationResult:
        """Inspect without recording; useful before a publish attempt."""
        timestamp = time.monotonic() if now is None else now
        self._expire(timestamp)
        digest = content_hash(message)
        if message.message_id in self._ids:
            self._ids.move_to_end(message.message_id)
            return DeduplicationResult(True, "message_id", digest)
        if digest in self._hashes:
            self._hashes.move_to_end(digest)
            return DeduplicationResult(True, "content_hash", digest)
        return DeduplicationResult(False, None, digest)

    def record(self, message: TaskMessage, *, now: float | None = None) -> None:
        """Record only after the message has entered a durable delivery path."""
        timestamp = time.monotonic() if now is None else now
        self._expire(timestamp)
        digest = content_hash(message)
        self._ids[message.message_id] = timestamp
        self._hashes[digest] = timestamp
        self._trim(self._ids)
        self._trim(self._hashes)

    def _expire(self, timestamp: float) -> None:
        cutoff = timestamp - self._ttl_s
        for cache in (self._ids, self._hashes):
            while cache and next(iter(cache.values())) < cutoff:
                cache.popitem(last=False)

    def _trim(self, cache: OrderedDict[str, float]) -> None:
        while len(cache) > self._max_entries:
            cache.popitem(last=False)
