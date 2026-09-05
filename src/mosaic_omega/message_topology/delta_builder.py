"""Build receiver-aware TaskMessage deltas without changing the wire schema."""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from ..agent_runtime.task_messages import ConstraintDelta, DeltaOperation, EvidenceRef, FactDelta, TaskMessage
from ..agent_runtime.trace_context import TRACE_CONTEXTS
from .knowledge_digest import fingerprint
from .models import KnowledgeDigest


@dataclass(frozen=True)
class DeltaBuildResult:
    message: TaskMessage | None
    omitted_summary: bool
    omitted_facts: int
    omitted_constraints: int
    omitted_evidence_refs: int

    @property
    def is_empty(self) -> bool:
        return self.message is None


class DeltaBuilder:
    """Filters a candidate message to information unknown to its receiver."""

    def build(self, candidate: TaskMessage, digest: KnowledgeDigest) -> DeltaBuildResult:
        if candidate.receiver != digest.receiver or candidate.task_id != digest.task_id:
            raise ValueError("digest receiver/task must match the candidate message")
        summary = candidate.summary
        omit_summary = summary is None or fingerprint(summary) == digest.summary_hash
        if omit_summary:
            summary = None
        facts, omitted_facts = self._filter_deltas(candidate.facts, digest.fact_map())
        constraints, omitted_constraints = self._filter_deltas(candidate.constraints, digest.constraint_map())
        known_evidence = digest.evidence_map()
        evidence = tuple(
            item for item in candidate.evidence_refs
            if known_evidence.get(item.id) != fingerprint(item.to_dict())
        )
        omitted_evidence = len(candidate.evidence_refs) - len(evidence)
        if summary is None and not facts and not constraints and not evidence:
            return DeltaBuildResult(None, omit_summary, omitted_facts, omitted_constraints, omitted_evidence)
        message = TaskMessage.create(
            message_id=uuid.uuid4().hex,
            sender=candidate.sender,
            receiver=candidate.receiver,
            task_id=candidate.task_id,
            summary=summary,
            facts=facts,
            constraints=constraints,
            evidence_refs=evidence,
            priority=candidate.priority,
            ttl=candidate.ttl,
        )
        TRACE_CONTEXTS.register(
            message.message_id,
            task_id=message.task_id,
            parent_message_id=candidate.message_id,
        )
        return DeltaBuildResult(message, omit_summary, omitted_facts, omitted_constraints, omitted_evidence)

    @staticmethod
    def _filter_deltas(
        deltas: tuple[FactDelta, ...] | tuple[ConstraintDelta, ...],
        known: dict[str, str],
    ) -> tuple[tuple[FactDelta, ...] | tuple[ConstraintDelta, ...], int]:
        retained: list[FactDelta | ConstraintDelta] = []
        omitted = 0
        for delta in deltas:
            if delta.op is DeltaOperation.REMOVE:
                if delta.id in known:
                    retained.append(delta)
                else:
                    omitted += 1
            elif known.get(delta.id) != fingerprint(delta.to_dict()):
                retained.append(delta)
            else:
                omitted += 1
        return tuple(retained), omitted
