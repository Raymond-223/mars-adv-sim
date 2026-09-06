"""Unified data models for the MOSAIC-Ω memory module.

The model layer deliberately contains no Redis / object-store / vector-index
logic.  Business modules exchange these dataclasses so the memory subsystem can
be developed and tested independently from the rest of the project.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryType(str, Enum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


class VerificationStatus(str, Enum):
    UNVERIFIED = "UNVERIFIED"
    VERIFIED = "VERIFIED"
    STALE = "STALE"
    REJECTED = "REJECTED"


class MemoryEventType(str, Enum):
    BUILD_FAILED = "BUILD_FAILED"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    TASK_SUCCEEDED = "TASK_SUCCEEDED"
    TASK_FAILED = "TASK_FAILED"
    AGENT_SWITCHED = "AGENT_SWITCHED"
    EVIDENCE_ADDED = "EVIDENCE_ADDED"
    GOAL_UPDATED = "GOAL_UPDATED"
    CUSTOM = "CUSTOM"


@dataclass
class MemoryRecord:
    """Unified persisted memory record.

    Required fields match the memory-module contract.  Goal, hard-constraint
    and prohibition records are safety-critical and are automatically forced to
    ``compressible=False`` even if a caller accidentally passes True.
    """

    run_id: str
    task_id: str
    node_id: str
    memory_type: MemoryType
    content: str
    summary: str
    importance: float = 0.5
    confidence: float = 0.5
    source: str = "unknown"
    evidence_refs: List[str] = field(default_factory=list)
    access_scope: List[str] = field(default_factory=lambda: ["default"])
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    compressible: bool = True
    memory_id: str = field(default_factory=lambda: f"mem_{uuid4().hex}")
    created_at: str = field(default_factory=utc_now)
    expires_at: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.importance = min(1.0, max(0.0, float(self.importance)))
        self.confidence = min(1.0, max(0.0, float(self.confidence)))
        self.evidence_refs = list(dict.fromkeys(str(x) for x in self.evidence_refs if x))
        self.access_scope = list(dict.fromkeys(str(x) for x in self.access_scope if x)) or ["default"]
        self.tags = list(dict.fromkeys(str(x) for x in self.tags if x))
        if {"goal", "hard_constraint", "prohibition"}.intersection(self.tags):
            self.compressible = False

    @property
    def is_core_constraint(self) -> bool:
        return bool({"goal", "hard_constraint", "prohibition"}.intersection(self.tags)) or not self.compressible

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["memory_type"] = self.memory_type.value
        data["verification_status"] = self.verification_status.value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryRecord":
        return cls(
            memory_id=str(data.get("memory_id", f"mem_{uuid4().hex}")),
            run_id=str(data.get("run_id", "")),
            task_id=str(data.get("task_id", "")),
            node_id=str(data.get("node_id", "")),
            memory_type=MemoryType(data.get("memory_type", MemoryType.EPISODIC.value)),
            content=str(data.get("content", "")),
            summary=str(data.get("summary", "")),
            importance=float(data.get("importance", 0.5)),
            confidence=float(data.get("confidence", 0.5)),
            source=str(data.get("source", "unknown")),
            evidence_refs=list(data.get("evidence_refs", [])),
            created_at=str(data.get("created_at", utc_now())),
            expires_at=data.get("expires_at"),
            access_scope=list(data.get("access_scope", ["default"])),
            verification_status=VerificationStatus(
                data.get("verification_status", VerificationStatus.UNVERIFIED.value)
            ),
            compressible=bool(data.get("compressible", True)),
            tags=list(data.get("tags", [])),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class MemoryEvent:
    """Memory-specific projection of an authoritative execution Event."""

    event_type: MemoryEventType
    run_id: str
    task_id: str
    node_id: str
    content: str
    source: str = "agent"
    evidence_refs: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: f"evt_{uuid4().hex}")
    trace_id: str = ""
    parent_event_id: str = ""
    actor_id: str = ""
    model_id: str = ""
    schema_version: str = "v0.1"
    created_at: str = field(default_factory=utc_now)


@dataclass
class ProcedureRecord:
    procedure_id: str
    title: str
    steps: List[str]
    preconditions: List[str] = field(default_factory=list)
    success_criteria: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    source: str = "manual"
    success_count: int = 0
    failure_count: int = 0
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_memory_record(
        self,
        run_id: str = "global",
        task_id: str = "procedure",
        node_id: str = "procedure",
    ) -> MemoryRecord:
        content = "\n".join(f"{i + 1}. {step}" for i, step in enumerate(self.steps))
        return MemoryRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            memory_type=MemoryType.PROCEDURAL,
            content=content,
            summary=self.title,
            importance=0.75,
            confidence=0.7,
            source=self.source,
            verification_status=self.verification_status,
            compressible=True,
            memory_id=self.procedure_id,
            tags=self.tags,
            metadata={
                "preconditions": self.preconditions,
                "success_criteria": self.success_criteria,
                "success_count": self.success_count,
                "failure_count": self.failure_count,
                "updated_at": self.updated_at,
            },
        )


@dataclass
class ContextPack:
    """Fixed-format compact context returned to runtime callers."""

    run_id: str
    node_id: str
    goal: str = ""
    hard_constraints: List[str] = field(default_factory=list)
    prohibitions: List[str] = field(default_factory=list)
    relevant_facts: List[str] = field(default_factory=list)
    previous_results: List[str] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)
    relevant_experiences: List[str] = field(default_factory=list)
    procedures: List[str] = field(default_factory=list)
    token_estimate: int = 0
    full_history_token_estimate: int = 0
    compression_ratio: float = 0.0
    memory_ids: List[str] = field(default_factory=list)
    truncated: bool = False
    #: How this pack was selected: candidate stages, ranking scores, the records
    #: that were dropped and why, and what the budget removed.  Without it the
    #: console could only display the finished pack and had no way to show the
    #: retrieval → ranking → dedupe → compression pipeline that produced it.
    selection_trace: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Snapshot:
    snapshot_id: str
    run_id: str
    created_at: str
    state: Dict[str, Any]
    compressed: bool = True
    checksum: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
