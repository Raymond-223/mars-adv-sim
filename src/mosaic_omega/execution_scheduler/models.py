"""Canonical execution-scheduler contracts with backwards-compatible aliases.

The public JSON representation follows the MOSAIC-Ω V3 tracing/schema rules while
keeping the existing Python call sites (`task_id`, `event_type`, `occurred_at`) valid.
GoalSpec and the upstream ToDAG remain outside this package.
"""

from __future__ import annotations

import time
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping


DEFAULT_SCHEMA_VERSION = "0.1"


class TaskState(str, Enum):
    CREATED = "CREATED"
    PLANNED = "PLANNED"
    READY = "READY"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PAUSED = "PAUSED"


class ActorKind(str, Enum):
    AGENT = "agent"
    MODEL = "model"
    TOOL = "tool"
    DEVICE = "device"


class ErrorClass(str, Enum):
    RETRYABLE = "RETRYABLE"
    REPLACEABLE = "REPLACEABLE"
    ROLLBACK_REQUIRED = "ROLLBACK_REQUIRED"
    REPLAN_REQUIRED = "REPLAN_REQUIRED"
    SAFE_STOP = "SAFE_STOP"


@dataclass(frozen=True)
class Event:
    """Append-only event.

    Canonical names required by the handbook are exposed through properties and
    ``to_dict``: ``node_id``, ``type``, ``timestamp``, ``parent_event_id`` and
    ``schema_version``. Legacy names remain usable inside the current project.
    """

    run_id: str
    event_type: str
    actor_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    task_id: str | None = None
    trace_id: str | None = None
    parent_event_id: str | None = None
    model_id: str | None = None
    schema_version: str = DEFAULT_SCHEMA_VERSION
    event_id: str = field(default_factory=lambda: f"evt_{uuid.uuid4().hex}")
    occurred_at: float = field(default_factory=time.time)
    sequence: int | None = None

    @property
    def node_id(self) -> str | None:
        return self.task_id

    @property
    def type(self) -> str:
        return self.event_type

    @property
    def timestamp(self) -> float:
        return self.occurred_at

    def to_dict(self) -> dict[str, Any]:
        # Canonical fields come first. Compatibility aliases are retained so old
        # logs/readers can be replayed without a migration step.
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "type": self.event_type,
            "event_type": self.event_type,
            "payload": deepcopy(self.payload),
            "timestamp": self.occurred_at,
            "occurred_at": self.occurred_at,
            "trace_id": self.trace_id,
            "parent_event_id": self.parent_event_id,
            "actor_id": self.actor_id,
            "model_id": self.model_id,
            "schema_version": self.schema_version,
            "sequence": self.sequence,
        }

    def to_storage_dict(self) -> dict[str, Any]:
        """Exact PostgreSQL event-table columns."""
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "node_id": self.node_id,
            "trace_id": self.trace_id,
            "parent_event_id": self.parent_event_id,
            "type": self.event_type,
            "actor_id": self.actor_id,
            "model_id": self.model_id,
            "timestamp": self.occurred_at,
            "schema_version": self.schema_version,
            "payload": deepcopy(self.payload),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Event":
        event_type = raw.get("type", raw.get("event_type"))
        if event_type is None:
            raise KeyError("type")
        timestamp = raw.get("timestamp", raw.get("occurred_at", time.time()))
        node_id = raw.get("node_id", raw.get("task_id"))
        return cls(
            run_id=str(raw["run_id"]),
            event_type=str(event_type),
            actor_id=str(raw.get("actor_id", "system")),
            payload=deepcopy(dict(raw.get("payload", {}))),
            task_id=str(node_id) if node_id is not None else None,
            trace_id=str(raw["trace_id"]) if raw.get("trace_id") else None,
            parent_event_id=(str(raw["parent_event_id"]) if raw.get("parent_event_id") else None),
            model_id=str(raw["model_id"]) if raw.get("model_id") else None,
            schema_version=str(raw.get("schema_version", DEFAULT_SCHEMA_VERSION)),
            event_id=str(raw.get("event_id", f"evt_{uuid.uuid4().hex}")),
            occurred_at=float(timestamp),
            sequence=int(raw["sequence"]) if raw.get("sequence") is not None else None,
        )


@dataclass
class TaskNodeView:
    """Materialized execution view of an upstream TaskNode."""

    run_id: str
    task_id: str
    task_type: str
    description: str
    state: TaskState = TaskState.CREATED
    depends_on: tuple[str, ...] = ()
    priority: int = 5
    required_capabilities: frozenset[str] = frozenset()
    required_permissions: frozenset[str] = frozenset()
    acceptance_conditions: tuple[str, ...] = ()
    privacy_level: str = "normal"
    data_location: str | None = None
    estimated_tokens: int = 0
    max_latency_ms: float | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    evidence_dependencies: tuple[str, ...] = ()
    resource_requirements: dict[str, Any] = field(default_factory=dict)
    risk: str = "normal"
    version: int = 0
    attempt: int = 0
    paused_from: TaskState | None = None
    assignment: "Assignment | None" = None
    result: "ExecutionResult | None" = None
    evidence: tuple["Evidence", ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def node_id(self) -> str:
        return self.task_id

    @property
    def status(self) -> TaskState:
        return self.state

    def to_dict(self) -> dict[str, Any]:
        acceptance = list(self.acceptance_conditions)
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "node_id": self.task_id,
            "task_type": self.task_type,
            "type": self.task_type,
            "description": self.description,
            "state": self.state.value,
            "status": self.state.value,
            "depends_on": list(self.depends_on),
            "predecessors": list(self.depends_on),
            "priority": self.priority,
            "required_capabilities": sorted(self.required_capabilities),
            "required_permissions": sorted(self.required_permissions),
            "acceptance_conditions": acceptance,
            "acceptance": acceptance,
            "privacy_level": self.privacy_level,
            "data_location": self.data_location,
            "estimated_tokens": self.estimated_tokens,
            "max_latency_ms": self.max_latency_ms,
            "inputs": deepcopy(self.inputs),
            "outputs": deepcopy(self.outputs),
            "evidence_dependencies": list(self.evidence_dependencies),
            "resource_requirements": deepcopy(self.resource_requirements),
            "risk": self.risk,
            "version": self.version,
            "attempt": self.attempt,
            "paused_from": self.paused_from.value if self.paused_from else None,
            "assignment": self.assignment.to_dict() if self.assignment else None,
            "result": self.result.to_dict() if self.result else None,
            "evidence": [item.to_dict() for item in self.evidence],
            "metadata": deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TaskNodeView":
        task_id = raw.get("task_id", raw.get("node_id"))
        if task_id is None:
            raise KeyError("task_id/node_id")
        state = raw.get("state", raw.get("status", TaskState.CREATED.value))
        depends_on = raw.get("depends_on", raw.get("predecessors", ()))
        acceptance = raw.get("acceptance_conditions", raw.get("acceptance", ()))
        return cls(
            run_id=str(raw["run_id"]),
            task_id=str(task_id),
            task_type=str(raw.get("task_type", raw.get("type", raw.get("required_skill", "general")))),
            description=str(raw.get("description", task_id)),
            state=TaskState(state),
            depends_on=tuple(str(item) for item in depends_on),
            priority=int(raw.get("priority", 5)),
            required_capabilities=frozenset(str(item) for item in raw.get("required_capabilities", ())),
            required_permissions=frozenset(str(item) for item in raw.get("required_permissions", ())),
            acceptance_conditions=tuple(str(item) for item in acceptance),
            privacy_level=str(raw.get("privacy_level", "normal")),
            data_location=str(raw["data_location"]) if raw.get("data_location") else None,
            estimated_tokens=int(raw.get("estimated_tokens", 0)),
            max_latency_ms=float(raw["max_latency_ms"]) if raw.get("max_latency_ms") is not None else None,
            inputs=deepcopy(dict(raw.get("inputs", {}))),
            outputs=deepcopy(dict(raw.get("outputs", {}))),
            evidence_dependencies=tuple(str(item) for item in raw.get("evidence_dependencies", ())),
            resource_requirements=deepcopy(dict(raw.get("resource_requirements", {}))),
            risk=str(raw.get("risk", "normal")),
            version=int(raw.get("version", 0)),
            attempt=int(raw.get("attempt", 0)),
            paused_from=TaskState(raw["paused_from"]) if raw.get("paused_from") else None,
            assignment=Assignment.from_dict(raw["assignment"]) if raw.get("assignment") else None,
            result=ExecutionResult.from_dict(raw["result"]) if raw.get("result") else None,
            evidence=tuple(Evidence.from_dict(item) for item in raw.get("evidence", ())),
            metadata=deepcopy(dict(raw.get("metadata", {}))),
        )


@dataclass(frozen=True)
class ToolCall:
    run_id: str
    task_id: str
    actor_id: str
    tool_name: str
    arguments: dict[str, Any]
    idempotency_key: str
    required_permissions: frozenset[str] = frozenset()
    timeout_s: float | None = None
    call_id: str = field(default_factory=lambda: f"call_{uuid.uuid4().hex}")
    trace_id: str | None = None
    parent_event_id: str | None = None
    model_id: str | None = None
    schema_version: str = DEFAULT_SCHEMA_VERSION
    timestamp: float = field(default_factory=time.time)

    @property
    def node_id(self) -> str:
        return self.task_id

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["required_permissions"] = sorted(self.required_permissions)
        result["node_id"] = self.task_id
        return result

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ToolCall":
        task_id = raw.get("task_id", raw.get("node_id"))
        if task_id is None:
            raise KeyError("task_id/node_id")
        return cls(
            run_id=str(raw["run_id"]),
            task_id=str(task_id),
            actor_id=str(raw["actor_id"]),
            tool_name=str(raw["tool_name"]),
            arguments=deepcopy(dict(raw.get("arguments", {}))),
            idempotency_key=str(raw["idempotency_key"]),
            required_permissions=frozenset(str(item) for item in raw.get("required_permissions", ())),
            timeout_s=float(raw["timeout_s"]) if raw.get("timeout_s") is not None else None,
            call_id=str(raw.get("call_id", f"call_{uuid.uuid4().hex}")),
            trace_id=str(raw["trace_id"]) if raw.get("trace_id") else None,
            parent_event_id=str(raw["parent_event_id"]) if raw.get("parent_event_id") else None,
            model_id=str(raw["model_id"]) if raw.get("model_id") else None,
            schema_version=str(raw.get("schema_version", DEFAULT_SCHEMA_VERSION)),
            timestamp=float(raw.get("timestamp", time.time())),
        )


@dataclass(frozen=True)
class ExecutionResult:
    call_id: str
    success: bool
    output: str = ""
    error: str | None = None
    exit_code: int | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)
    error_class: ErrorClass | None = None

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        raw["error_class"] = self.error_class.value if self.error_class else None
        return raw

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ExecutionResult":
        error_class = raw.get("error_class")
        return cls(
            call_id=str(raw["call_id"]),
            success=bool(raw.get("success", False)),
            output=str(raw.get("output", "")),
            error=raw.get("error"),
            exit_code=int(raw["exit_code"]) if raw.get("exit_code") is not None else None,
            started_at=float(raw.get("started_at", time.time())),
            finished_at=float(raw.get("finished_at", time.time())),
            metadata=deepcopy(dict(raw.get("metadata", {}))),
            error_class=ErrorClass(error_class) if error_class else None,
        )


@dataclass(frozen=True)
class Evidence:
    run_id: str
    task_id: str
    kind: str
    digest: str
    content: str = ""
    uri: str | None = None
    producer: str = "unknown"
    mime_type: str = "text/plain"
    scope: str = "task"
    verification_status: str = "UNVERIFIED"
    trace_id: str | None = None
    parent_event_id: str | None = None
    actor_id: str | None = None
    model_id: str | None = None
    schema_version: str = DEFAULT_SCHEMA_VERSION
    metadata: dict[str, Any] = field(default_factory=dict)
    evidence_id: str = field(default_factory=lambda: f"evi_{uuid.uuid4().hex}")
    created_at: float = field(default_factory=time.time)

    @property
    def node_id(self) -> str:
        return self.task_id

    @property
    def hash(self) -> str:
        return self.digest

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "node_id": self.task_id,
            "kind": self.kind,
            "uri": self.uri,
            "hash": self.digest,
            "digest": self.digest,
            "producer": self.producer,
            "mime_type": self.mime_type,
            "scope": self.scope,
            "created_at": self.created_at,
            "verification_status": self.verification_status,
            "content": self.content,
            "trace_id": self.trace_id,
            "parent_event_id": self.parent_event_id,
            "actor_id": self.actor_id or self.producer,
            "model_id": self.model_id,
            "schema_version": self.schema_version,
            "metadata": deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Evidence":
        task_id = raw.get("task_id", raw.get("node_id"))
        digest = raw.get("hash", raw.get("digest"))
        if task_id is None or digest is None:
            raise KeyError("task_id/node_id and hash/digest are required")
        producer = str(raw.get("producer", raw.get("actor_id", "unknown")))
        return cls(
            run_id=str(raw["run_id"]),
            task_id=str(task_id),
            kind=str(raw.get("kind", "artifact")),
            digest=str(digest),
            content=str(raw.get("content", "")),
            uri=str(raw["uri"]) if raw.get("uri") else None,
            producer=producer,
            mime_type=str(raw.get("mime_type", "text/plain")),
            scope=str(raw.get("scope", "task")),
            verification_status=str(raw.get("verification_status", "UNVERIFIED")),
            trace_id=str(raw["trace_id"]) if raw.get("trace_id") else None,
            parent_event_id=str(raw["parent_event_id"]) if raw.get("parent_event_id") else None,
            actor_id=str(raw["actor_id"]) if raw.get("actor_id") else producer,
            model_id=str(raw["model_id"]) if raw.get("model_id") else None,
            schema_version=str(raw.get("schema_version", DEFAULT_SCHEMA_VERSION)),
            metadata=deepcopy(dict(raw.get("metadata", {}))),
            evidence_id=str(raw.get("evidence_id", f"evi_{uuid.uuid4().hex}")),
            created_at=float(raw.get("created_at", time.time())),
        )


@dataclass
class CapabilityProfile:
    actor_id: str
    kind: ActorKind
    task_types: frozenset[str]
    capabilities: frozenset[str] = frozenset()
    permissions: frozenset[str] = frozenset()
    reliability: float = 0.8
    fixed_cost: float = 0.0
    cost_per_token: float = 0.0
    latency_ms: float = 0.0
    energy_cost: float = 0.0
    context_limit: int = 0
    device_location: str | None = None
    online: bool = True
    current_load: float = 0.0
    capacity: int = 1
    posterior: dict[str, dict[str, float]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def cost(self) -> float:
        return self.fixed_cost

    @property
    def latency(self) -> float:
        return self.latency_ms

    def supports(self, task: TaskNodeView) -> bool:
        task_type_ok = "*" in self.task_types or task.task_type in self.task_types
        capability_ok = "*" in self.capabilities or task.required_capabilities.issubset(self.capabilities)
        return self.online and task_type_ok and capability_ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor_id": self.actor_id,
            "kind": self.kind.value,
            "task_types": sorted(self.task_types),
            "capabilities": sorted(self.capabilities),
            "permissions": sorted(self.permissions),
            "reliability": self.reliability,
            "cost": self.fixed_cost,
            "fixed_cost": self.fixed_cost,
            "cost_per_token": self.cost_per_token,
            "latency": self.latency_ms,
            "latency_ms": self.latency_ms,
            "energy_cost": self.energy_cost,
            "context_limit": self.context_limit,
            "device_location": self.device_location,
            "online": self.online,
            "current_load": self.current_load,
            "capacity": self.capacity,
            "posterior": deepcopy(self.posterior),
            "metadata": deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CapabilityProfile":
        kind = raw.get("kind", ActorKind.AGENT.value)
        return cls(
            actor_id=str(raw["actor_id"]),
            kind=ActorKind(kind),
            task_types=frozenset(str(item) for item in raw.get("task_types", ())),
            capabilities=frozenset(str(item) for item in raw.get("capabilities", ())),
            permissions=frozenset(str(item) for item in raw.get("permissions", ())),
            reliability=float(raw.get("reliability", 0.8)),
            fixed_cost=float(raw.get("fixed_cost", raw.get("cost", 0.0))),
            cost_per_token=float(raw.get("cost_per_token", 0.0)),
            latency_ms=float(raw.get("latency_ms", raw.get("latency", 0.0))),
            energy_cost=float(raw.get("energy_cost", 0.0)),
            context_limit=int(raw.get("context_limit", 0)),
            device_location=str(raw["device_location"]) if raw.get("device_location") else None,
            online=bool(raw.get("online", True)),
            current_load=float(raw.get("current_load", 0.0)),
            capacity=max(1, int(raw.get("capacity", 1))),
            posterior=deepcopy(dict(raw.get("posterior", {}))),
            metadata=deepcopy(dict(raw.get("metadata", {}))),
        )


@dataclass(frozen=True)
class Assignment:
    task_id: str
    agent_id: str
    model_id: str
    tool_id: str
    resource_id: str
    total_cost: float
    cost_breakdown: dict[str, float]
    policy: str
    reason: str
    # execution_tier is retained as a compatibility alias and always means the
    # actual tier used by the selected Agent. Never use a recommendation here.
    execution_tier: str = "edge"
    recommended_tier: str = "edge"
    actual_execution_tier: str = "edge"
    placement_fallback: bool = False
    placement_evidence: dict[str, Any] = field(default_factory=dict)
    partition_policy: str = "none"
    partition_descriptor: dict[str, Any] = field(default_factory=dict)
    solver_provenance: dict[str, Any] = field(default_factory=dict)
    assignment_id: str = field(default_factory=lambda: f"asn_{uuid.uuid4().hex}")
    run_id: str | None = None
    trace_id: str | None = None
    schema_version: str = DEFAULT_SCHEMA_VERSION
    created_at: float = field(default_factory=time.time)

    @property
    def node_id(self) -> str:
        return self.task_id

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        raw["node_id"] = self.task_id
        return raw

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Assignment":
        task_id = raw.get("task_id", raw.get("node_id"))
        if task_id is None:
            raise KeyError("task_id/node_id")
        return cls(
            task_id=str(task_id),
            agent_id=str(raw["agent_id"]),
            model_id=str(raw["model_id"]),
            tool_id=str(raw["tool_id"]),
            resource_id=str(raw["resource_id"]),
            total_cost=float(raw.get("total_cost", 0.0)),
            cost_breakdown={str(k): float(v) for k, v in raw.get("cost_breakdown", {}).items()},
            policy=str(raw.get("policy", "unknown")),
            reason=str(raw.get("reason", "")),
            execution_tier=str(raw.get("actual_execution_tier", raw.get("execution_tier", "edge"))),
            recommended_tier=str(raw.get("recommended_tier", raw.get("execution_tier", "edge"))),
            actual_execution_tier=str(raw.get("actual_execution_tier", raw.get("execution_tier", "edge"))),
            placement_fallback=bool(raw.get("placement_fallback", False)),
            placement_evidence=deepcopy(dict(raw.get("placement_evidence", {}))),
            partition_policy=str(raw.get("partition_policy", "none")),
            partition_descriptor=deepcopy(dict(raw.get("partition_descriptor", {}))),
            solver_provenance=deepcopy(dict(raw.get("solver_provenance", {}))),
            assignment_id=str(raw.get("assignment_id", f"asn_{uuid.uuid4().hex}")),
            run_id=str(raw["run_id"]) if raw.get("run_id") else None,
            trace_id=str(raw["trace_id"]) if raw.get("trace_id") else None,
            schema_version=str(raw.get("schema_version", DEFAULT_SCHEMA_VERSION)),
            created_at=float(raw.get("created_at", time.time())),
        )
