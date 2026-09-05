"""Episodic memory generated from append-only runtime events."""
from __future__ import annotations

from typing import Dict

from .models import MemoryEvent, MemoryEventType, MemoryRecord, MemoryType, VerificationStatus
from .repository import MemoryRepository

_EVENT_SUMMARY_PREFIX = {
    MemoryEventType.BUILD_FAILED: "Build failed",
    MemoryEventType.TOOL_TIMEOUT: "Tool timeout",
    MemoryEventType.TASK_SUCCEEDED: "Task succeeded",
    MemoryEventType.TASK_FAILED: "Task failed",
    MemoryEventType.AGENT_SWITCHED: "Agent switched",
    MemoryEventType.EVIDENCE_ADDED: "Evidence added",
    MemoryEventType.GOAL_UPDATED: "Goal updated",
    MemoryEventType.CUSTOM: "MemoryEvent",
}


class EpisodicMemory:
    def __init__(self, repository: MemoryRepository):
        self.repository = repository

    def ingest_event(self, event: MemoryEvent) -> MemoryRecord:
        status = VerificationStatus.VERIFIED if event.event_type == MemoryEventType.TASK_SUCCEEDED else VerificationStatus.UNVERIFIED
        record = MemoryRecord(
            run_id=event.run_id,
            task_id=event.task_id,
            node_id=event.node_id,
            memory_type=MemoryType.EPISODIC,
            content=event.content,
            summary=self._summarize_event(event),
            importance=self._importance_for(event),
            confidence=0.75 if event.evidence_refs else 0.5,
            source=event.source,
            evidence_refs=event.evidence_refs,
            verification_status=status,
            compressible=True,
            tags=[event.event_type.value.lower()],
            metadata={
                "event_id": event.event_id,
                "event_type": event.event_type.value,
                "trace_id": event.trace_id,
                "parent_event_id": event.parent_event_id,
                "actor_id": event.actor_id,
                "model_id": event.model_id,
                "schema_version": event.schema_version,
                **event.metadata,
            },
        )
        return self.repository.save(record)

    def _summarize_event(self, event: MemoryEvent) -> str:
        prefix = _EVENT_SUMMARY_PREFIX.get(event.event_type, "MemoryEvent")
        compact = event.content.replace("\n", " ").strip()
        if len(compact) > 160:
            compact = compact[:157] + "..."
        return f"{prefix}: {compact}"

    def _importance_for(self, event: MemoryEvent) -> float:
        mapping: Dict[MemoryEventType, float] = {
            MemoryEventType.BUILD_FAILED: 0.85,
            MemoryEventType.TOOL_TIMEOUT: 0.75,
            MemoryEventType.TASK_SUCCEEDED: 0.8,
            MemoryEventType.TASK_FAILED: 0.9,
            MemoryEventType.AGENT_SWITCHED: 0.7,
            MemoryEventType.EVIDENCE_ADDED: 0.75,
            MemoryEventType.GOAL_UPDATED: 0.95,
            MemoryEventType.CUSTOM: 0.5,
        }
        return mapping.get(event.event_type, 0.5)
