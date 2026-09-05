"""Receiver-specific knowledge summaries for low-entropy sending."""

from __future__ import annotations

import hashlib
import json
import time

from ..agent_runtime.task_messages import DeltaOperation, TaskMessage
from .models import KnowledgeDigest


def fingerprint(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class KnowledgeDigestStore:
    """Stores only the context a receiver has actually accepted."""

    def __init__(self) -> None:
        self._digests: dict[tuple[str, str], KnowledgeDigest] = {}

    def get(self, receiver: str, task_id: str) -> KnowledgeDigest:
        return self._digests.get((receiver, task_id), KnowledgeDigest(receiver=receiver, task_id=task_id))

    def record_delivered(self, message: TaskMessage, *, now: float | None = None) -> KnowledgeDigest:
        """Advance the receiver digest after a successful publish/ack only."""
        current = self.get(message.receiver, message.task_id)
        facts = current.fact_map()
        constraints = current.constraint_map()
        evidence = current.evidence_map()
        for delta in message.facts:
            if delta.op is DeltaOperation.REMOVE:
                facts.pop(delta.id, None)
            else:
                facts[delta.id] = fingerprint(delta.to_dict())
        for delta in message.constraints:
            if delta.op is DeltaOperation.REMOVE:
                constraints.pop(delta.id, None)
            else:
                constraints[delta.id] = fingerprint(delta.to_dict())
        for reference in message.evidence_refs:
            evidence[reference.id] = fingerprint(reference.to_dict())
        digest = KnowledgeDigest(
            receiver=message.receiver,
            task_id=message.task_id,
            summary_hash=fingerprint(message.summary) if message.summary is not None else current.summary_hash,
            fact_hashes=tuple(sorted(facts.items())),
            constraint_hashes=tuple(sorted(constraints.items())),
            evidence_hashes=tuple(sorted(evidence.items())),
            updated_at=time.monotonic() if now is None else now,
        )
        self._digests[(message.receiver, message.task_id)] = digest
        return digest
