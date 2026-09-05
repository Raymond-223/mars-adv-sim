"""Cross-module contract adapters for the MOSAIC-Ω V3 main chain.

The project deliberately has one owner for each domain model.  These adapters
translate at service boundaries instead of letting every subsystem invent a
second copy of GoalSpec / TaskNode / Event / Evidence / MessageEnvelope.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from ..execution_scheduler.models import (
    ActorKind,
    CapabilityProfile,
    Event as ExecutionEvent,
    Evidence as ExecutionEvidence,
    TaskNodeView,
)
from ..memory_recovery.models import MemoryEvent, MemoryEventType
from ..agent_runtime.models import AgentProfile, AgentStatus
from ..agent_runtime.task_messages import TaskMessage
from ..agent_runtime.trace_context import TRACE_CONTEXTS, business_content_hash, trace_id_for


def _stable_id(prefix: str, value: object, length: int = 16) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:length]}"


def canonical_goalspec(goalspec: Mapping[str, Any]) -> dict[str, Any]:
    """Expose the handbook GoalSpec fields without breaking the existing compiler.

    The current compiler uses ``main_goal`` / ``acceptance_conditions`` /
    ``budget``.  The handbook names are projected here so downstream services
    consume one stable contract while the compiler can remain backwards compatible.
    """
    raw = dict(goalspec)
    main_goal = raw.get("main_goal")
    if isinstance(main_goal, Mapping):
        objective = str(main_goal.get("goal_text", main_goal.get("objective", "")))
    else:
        objective = str(raw.get("objective", main_goal or ""))

    hard_constraints = list(raw.get("hard_constraints", ()))
    soft_preferences = list(raw.get("soft_preferences", ()))
    acceptance = list(raw.get("acceptance_predicates", raw.get("acceptance_conditions", ())))
    budgets = dict(raw.get("budgets", raw.get("budget", {})) or {})

    source_spans: list[str] = []
    for item in hard_constraints:
        if isinstance(item, Mapping):
            span = item.get("source_span") or item.get("constraint")
            if span:
                source_spans.append(str(span))
    if objective:
        source_spans.insert(0, objective)

    privacy_level = str(raw.get("privacy_level", "normal"))
    goal_id = str(raw.get("goal_id") or _stable_id("goal", raw))
    return {
        "goal_id": goal_id,
        "objective": objective,
        "hard_constraints": hard_constraints,
        "soft_preferences": soft_preferences,
        "acceptance_predicates": acceptance,
        "budgets": budgets,
        "privacy_level": privacy_level,
        "source_spans": list(dict.fromkeys(source_spans)),
        # Keep the exact compiler document for lossless replay/debugging.
        "source": raw,
    }


def tasknode_contract(task: TaskNodeView) -> dict[str, Any]:
    """Return the handbook TaskNode view from the execution projection."""
    raw = task.to_dict()
    return {
        "node_id": raw["node_id"],
        "type": raw["type"],
        "inputs": raw["inputs"],
        "outputs": raw["outputs"],
        "predecessors": raw["predecessors"],
        "evidence_dependencies": raw["evidence_dependencies"],
        "resource_requirements": raw["resource_requirements"],
        "risk": raw["risk"],
        "status": raw["status"],
        "acceptance": raw["acceptance"],
        "run_id": raw["run_id"],
        "priority": raw["priority"],
        "description": raw["description"],
    }


def runtime_agent_to_capability(
    profile: AgentProfile,
    *,
    status: AgentStatus | str = AgentStatus.ONLINE,
    current_load: int = 0,
    permissions: Sequence[str] = ("*",),
    cost_per_token: float = 0.0,
    latency_ms: float | None = None,
) -> CapabilityProfile:
    """Translate the dynamic-registry AgentProfile into scheduler capability data."""
    status_value = AgentStatus(status)
    metadata = {
        "endpoint": profile.endpoint,
        "tier": profile.tier.value,
        "labels": list(profile.labels),
        "resources": profile.resources.to_dict(),
    }
    return CapabilityProfile(
        actor_id=profile.agent_id,
        kind=ActorKind.AGENT,
        task_types=frozenset(profile.skills or ("*",)),
        capabilities=frozenset(profile.skills or ("*",)),
        permissions=frozenset(str(item) for item in permissions),
        reliability=profile.reliability,
        cost_per_token=float(cost_per_token),
        latency_ms=float(latency_ms or 0.0),
        online=status_value is AgentStatus.ONLINE,
        current_load=min(1.0, max(0.0, float(current_load) / max(1, profile.max_load))),
        capacity=max(1, profile.max_load),
        device_location=profile.tier.value,
        metadata=metadata,
    )


def event_contract(event: ExecutionEvent) -> dict[str, Any]:
    """Return only the canonical trace/event fields required by the handbook."""
    raw = event.to_dict()
    return {
        "event_id": raw["event_id"],
        "run_id": raw["run_id"],
        "task_id": raw.get("task_id"),
        "node_id": raw.get("node_id"),
        "trace_id": raw.get("trace_id"),
        "parent_event_id": raw.get("parent_event_id"),
        "actor_id": raw.get("actor_id"),
        "model_id": raw.get("model_id"),
        "timestamp": raw.get("timestamp"),
        "schema_version": raw.get("schema_version"),
        "type": raw.get("type"),
        "payload": raw.get("payload", {}),
    }


def evidence_contract(evidence: ExecutionEvidence) -> dict[str, Any]:
    raw = evidence.to_dict()
    return {
        "evidence_id": raw["evidence_id"],
        "uri": raw.get("uri"),
        "hash": raw["hash"],
        "producer": raw["producer"],
        "node_id": raw["node_id"],
        "mime_type": raw["mime_type"],
        "scope": raw["scope"],
        "created_at": raw["created_at"],
        "verification_status": raw["verification_status"],
        "run_id": raw["run_id"],
        "trace_id": raw.get("trace_id"),
        "parent_event_id": raw.get("parent_event_id"),
        "actor_id": raw.get("actor_id"),
        "model_id": raw.get("model_id"),
        "schema_version": raw.get("schema_version"),
    }


def message_envelope_contract(
    message: TaskMessage,
    *,
    run_id: str,
    topic: str = "task_context",
    budget: int | None = None,
) -> dict[str, Any]:
    """Project fixed 10-field TaskMessage into the handbook MessageEnvelope.

    The business wire schema remains unchanged.  ``content_hash`` and causal /
    trace metadata live in the sidecar so dynamic-registry MQTT compatibility is
    preserved while the main chain still exposes the handbook interface.
    """
    sidecar = TRACE_CONTEXTS.get(message.message_id)
    content_hash = sidecar.content_hash if sidecar and sidecar.content_hash else business_content_hash(message.to_dict())
    token_budget = budget if budget is not None else (sidecar.token_budget if sidecar else None)
    trace_id = trace_id_for(run_id, message.task_id, message.message_id)
    return {
        "message_id": message.message_id,
        "sender": message.sender,
        "receiver": message.receiver,
        "topic": topic,
        "priority": message.priority,
        "ttl": message.ttl,
        "budget": token_budget,
        "summary": message.summary,
        "evidence_refs": [item.to_dict() for item in message.evidence_refs],
        "content_hash": content_hash,
        "causal_parent": sidecar.parent_message_id if sidecar else None,
        "run_id": run_id,
        "task_id": message.task_id,
        "node_id": message.task_id,
        "trace_id": trace_id,
        "parent_event_id": sidecar.parent_event_id if sidecar else None,
        "actor_id": message.sender,
        "model_id": sidecar.model_id if sidecar else None,
        "timestamp": sidecar.created_at if sidecar else None,
        "schema_version": sidecar.schema_version if sidecar else "task-message-v1",
        "business_message": message.to_dict(),
    }


def topology_snapshot_contract(snapshot: Any) -> dict[str, Any]:
    raw = snapshot.to_dict() if hasattr(snapshot, "to_dict") else dict(snapshot)
    connectivity = raw.get("lambda2")
    if connectivity is None:
        connectivity = raw.get("connected")
    return {
        "version": raw.get("version"),
        "nodes": raw.get("nodes", []),
        "edges": raw.get("edges", []),
        "edge_scores": raw.get("edge_scores", {}),
        "lambda2": raw.get("lambda2"),
        "lambda2_or_connectivity": connectivity,
        "connected": raw.get("connected"),
        "component_count": raw.get("component_count"),
        "spectral_target": raw.get("spectral_target"),
        "spectral_target_met": raw.get("spectral_target_met"),
        "effective_from": raw.get("effective_from"),
        "min_hold_time": raw.get("min_hold_time", raw.get("min_hold_time_s")),
    }


def _memory_event_type(event: ExecutionEvent) -> MemoryEventType:
    event_type = event.event_type.upper()
    payload = event.payload
    if event_type == "TASK_STATE_CHANGED":
        target = str(payload.get("to", "")).upper()
        if target == "SUCCEEDED":
            return MemoryEventType.TASK_SUCCEEDED
        if target == "FAILED":
            return MemoryEventType.TASK_FAILED
    if "TIMEOUT" in event_type or "timeout" in json.dumps(payload, ensure_ascii=False).lower():
        return MemoryEventType.TOOL_TIMEOUT
    if event_type in {"TASK_RESULT_UPDATED", "TASK_VERIFIED", "TOOL_EXECUTED"}:
        return MemoryEventType.EVIDENCE_ADDED
    if event_type in {"PLAN_UPDATED", "GOAL_UPDATED"}:
        return MemoryEventType.GOAL_UPDATED
    return MemoryEventType.CUSTOM


def execution_event_to_memory(event: ExecutionEvent) -> MemoryEvent:
    """Losslessly retain the execution event in episodic memory metadata."""
    payload_text = json.dumps(event.payload, ensure_ascii=False, sort_keys=True, default=str)
    evidence_ids: list[str] = []
    evidence = event.payload.get("evidence") if isinstance(event.payload, Mapping) else None
    if isinstance(evidence, Mapping) and evidence.get("evidence_id"):
        evidence_ids.append(str(evidence["evidence_id"]))
    elif isinstance(evidence, list):
        for item in evidence:
            if isinstance(item, Mapping) and item.get("evidence_id"):
                evidence_ids.append(str(item["evidence_id"]))
    created = datetime.fromtimestamp(event.occurred_at, tz=timezone.utc).isoformat()
    task_id = event.task_id or "run"
    return MemoryEvent(
        event_type=_memory_event_type(event),
        run_id=event.run_id,
        task_id=task_id,
        node_id=task_id,
        content=f"{event.event_type}: {payload_text}",
        source=event.actor_id,
        evidence_refs=evidence_ids,
        metadata={"execution_event": event.to_dict()},
        event_id=event.event_id,
        trace_id=event.trace_id or "",
        parent_event_id=event.parent_event_id or "",
        actor_id=event.actor_id,
        model_id=event.model_id or "",
        schema_version=event.schema_version,
        created_at=created,
    )


def evidence_manifest(tasks: Sequence[TaskNodeView]) -> list[dict[str, Any]]:
    """Generate the evidence-carrying delivery manifest required by the handbook."""
    manifest: list[dict[str, Any]] = []
    for task in tasks:
        for evidence in task.evidence:
            item = evidence_contract(evidence)
            item.update({
                "task_id": task.task_id,
                "verification_status": evidence.verification_status,
                "verifier": "deterministic-verifier",
            })
            manifest.append(item)
    return manifest


def verification_results(events: Sequence[ExecutionEvent]) -> list[dict[str, Any]]:
    """Project TASK_VERIFIED events to the handbook VerificationResult contract."""
    results: list[dict[str, Any]] = []
    for event in events:
        if event.event_type != "TASK_VERIFIED":
            continue
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        verification = payload.get("verification", {})
        if not isinstance(verification, Mapping):
            verification = {}
        results.append({
            "target_id": verification.get("target_id", event.task_id),
            "passed": bool(verification.get("passed", payload.get("passed", False))),
            "predicate_results": list(verification.get("predicate_results", [])),
            "confidence": float(verification.get("confidence", 0.0)),
            "evidence_refs": list(verification.get("evidence_refs", payload.get("evidence_ids", ())) or ()),
            "risk_level": str(verification.get("risk_level", "unknown")),
            "action": str(verification.get("action", "safe_stop")),
            "verifier": str(verification.get("verifier", "deterministic-verifier")),
            "trace_id": event.trace_id,
            "event_id": event.event_id,
        })
    return results
