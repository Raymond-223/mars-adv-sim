"""File-backed live fault-injection channel for competition-controlled runs.

The browser never mutates runtime state directly.  The Console control plane
writes an atomic request file; the running MainChain process consumes it only at
round boundaries, writes a FAULT_INJECTED EventStore event, executes the real
RecoveryEngine action, and persists an acknowledgement file.

Requirement changes are special: the current run is stopped at the round
boundary and the caller recompiles a new high-level run.  This is explicit in
the acknowledgement and never disguised as an in-place GoalSpec mutation.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mosaic_omega.execution_scheduler.models import ErrorClass, TaskState


@dataclass(frozen=True)
class LiveFaultOutcome:
    request_id: str
    fault: str
    applied: bool
    continue_run: bool = True
    requirement_change: str | None = None
    detail: dict[str, Any] | None = None


class LiveFaultMailbox:
    """Atomic request/ack mailbox shared by Console and a running subprocess."""

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / "control" / "injections"
        self.root.mkdir(parents=True, exist_ok=True)

    def _run_dir(self, run_id: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(run_id))
        path = self.root / safe
        path.mkdir(parents=True, exist_ok=True)
        return path

    def enqueue(self, run_id: str, fault: str, *, requirement: str | None = None, requested_by: str = "competition-console") -> dict[str, Any]:
        request_id = f"inj-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        payload = {
            "request_id": request_id,
            "run_id": run_id,
            "fault": str(fault),
            "requirement": requirement,
            "requested_at": time.time(),
            "requested_by": requested_by,
            "state": "PENDING",
            "state_source": "LiveFaultMailbox.enqueue",
        }
        run_dir = self._run_dir(run_id)
        target = run_dir / f"{request_id}.pending.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
        return payload

    def next_pending(self, run_id: str) -> tuple[Path, dict[str, Any]] | None:
        for path in sorted(self._run_dir(run_id).glob("*.pending.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                return path, data
        return None

    def acknowledge(self, pending_path: Path, request: dict[str, Any], *, state: str, detail: dict[str, Any] | None = None) -> Path:
        state = str(state).upper()
        if state not in {"APPLIED", "FAILED", "REJECTED"}:
            raise ValueError(f"unsupported injection state: {state}")
        payload = dict(request)
        payload.update({
            "state": state,
            "finished_at": time.time(),
            "state_source": "running MainChain round hook",
            "detail": detail or {},
        })
        suffix = state.casefold()
        target = pending_path.with_name(pending_path.name.replace(".pending.json", f".{suffix}.json"))
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
        try:
            pending_path.unlink()
        except FileNotFoundError:
            pass
        return target

    def status(self, run_id: str | None = None) -> list[dict[str, Any]]:
        roots = [self._run_dir(run_id)] if run_id else [p for p in self.root.iterdir() if p.is_dir()]
        rows: list[dict[str, Any]] = []
        for run_dir in roots:
            for path in sorted(run_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if isinstance(data, dict):
                    data = dict(data)
                    data["mailbox_path"] = str(path)
                    rows.append(data)
        return rows


def apply_live_fault(runtime: Any, run_id: str, round_index: int, request: dict[str, Any]) -> LiveFaultOutcome:
    """Apply one control request to the authoritative runtime at a round boundary."""

    fault = str(request.get("fault", "")).strip()
    request_id = str(request.get("request_id", "unknown"))
    tasks = runtime.execution.events.tasks(run_id)
    completed = [task for task in tasks if task.state is TaskState.SUCCEEDED]
    target = completed[-1] if completed else (tasks[0] if tasks else None)
    if target is None:
        raise RuntimeError("live fault injection has no task to target")

    base_payload = {
        "request_id": request_id,
        "fault_type": fault.upper(),
        "requested_at": request.get("requested_at"),
        "requested_by": request.get("requested_by"),
        "injected_after_round": round_index,
        "injection_mode": "live_console_mailbox_round_boundary",
    }

    if fault == "requirement_change":
        requirement = str(request.get("requirement") or "").strip()
        if not requirement:
            raise ValueError("requirement_change requires non-empty requirement")
        runtime.execution.events.append_event(
            "FAULT_INJECTED", run_id, actor_id="competition-console", task_id=target.task_id,
            payload={**base_payload, "new_requirement": requirement, "control_semantics": "stop current run and recompile changed high-level goal"},
        )
        return LiveFaultOutcome(
            request_id=request_id,
            fault=fault,
            applied=True,
            continue_run=False,
            requirement_change=requirement,
            detail={"target_task_id": target.task_id, "new_requirement": requirement},
        )

    if fault == "agent_offline":
        assigned = target.assignment.agent_id if target.assignment else None
        if not assigned:
            assigned = next((task.assignment.agent_id for task in tasks if task.assignment), None)
        if not assigned:
            raise RuntimeError("no assigned agent available for offline injection")
        runtime.execution.events.append_event(
            "FAULT_INJECTED", run_id, actor_id="competition-console", task_id=target.task_id,
            payload={
                **base_payload,
                "agent_id": assigned,
                "control_semantics": "mark registry agent offline; subsequent scheduler rounds must exclude it",
            },
        )
        runtime.registry_bridge.offline(assigned)
        runtime._sync_topology(run_id, changed_task_ids=())
        runtime._observe(run_id, "live_agent_offline_applied", force=True)
        return LiveFaultOutcome(
            request_id=request_id, fault=fault, applied=True,
            detail={
                "agent_id": assigned,
                "recovery_semantics": "future ready tasks are rescheduled against the updated online registry; no fake TASK_RECOVERED event is emitted",
            },
        )

    if fault == "tool_failure":
        # Arm the normal ToolRuntime failure boundary. The next real tool call
        # returns a failed ExecutionResult; the unchanged Orchestrator then
        # invokes RecoveryEngine through its ordinary failure path.
        armed = runtime.execution.tools.inject_failure(
            run_id,
            reason="competition controlled live tool failure",
            request_id=request_id,
        )
        runtime.execution.events.append_event(
            "FAULT_INJECTED", run_id, actor_id="competition-console", task_id=target.task_id,
            payload={
                **base_payload,
                "fault_type": "NEXT_TOOL_EXECUTION_FAILURE",
                "armed_fault": armed,
                "control_semantics": "next matching ToolRuntime.execute returns RETRYABLE failed ExecutionResult",
            },
        )
        runtime._observe(run_id, "live_tool_failure_armed", force=True)
        return LiveFaultOutcome(
            request_id=request_id, fault=fault, applied=True,
            detail={
                "injection_mode": "tool_runtime_next_call_failure",
                "armed_fault": armed,
                "recovery_semantics": "RecoveryEngine is invoked later by the normal Orchestrator when the failed ExecutionResult is processed",
            },
        )

    if fault == "evidence_invalidation":
        evidence = next((ev for task in completed for ev in task.evidence), None)
        if evidence is None:
            raise RuntimeError("no actual completed-task evidence exists yet; retry after at least one task succeeds")
        runtime.execution.events.append_event(
            "FAULT_INJECTED", run_id, actor_id="competition-console", task_id=evidence.task_id,
            payload={**base_payload, "fault_type": "EVIDENCE_INVALIDATION", "evidence_id": evidence.evidence_id},
        )
        plan_dict = runtime.invalidate_evidence(
            run_id, evidence.evidence_id, reason="competition controlled live evidence integrity failure"
        )
        return LiveFaultOutcome(
            request_id=request_id, fault=fault, applied=True,
            detail={"target_task_id": evidence.task_id, "evidence_id": evidence.evidence_id, "invalidation_plan": plan_dict},
        )

    raise ValueError(f"unknown live fault type: {fault}")


class LiveFaultController:
    """Round-hook helper used by subprocess scripts launched from the Console."""

    def __init__(self, workspace: str | Path) -> None:
        self.mailbox = LiveFaultMailbox(workspace)
        self.last_outcome: LiveFaultOutcome | None = None
        self.requirement_change: str | None = None
        self.applied_requests: list[dict[str, Any]] = []

    def round_hook(self, runtime: Any, run_id: str, round_index: int) -> bool | None:
        item = self.mailbox.next_pending(run_id)
        if item is None:
            return None
        path, request = item
        try:
            outcome = apply_live_fault(runtime, run_id, round_index, request)
        except Exception as exc:
            detail = {"error": f"{type(exc).__name__}: {exc}", "round_index": round_index}
            self.mailbox.acknowledge(path, request, state="FAILED", detail=detail)
            self.applied_requests.append({**request, "state": "FAILED", "detail": detail})
            # A failed injection request must not falsify the underlying run.
            return None
        detail = dict(outcome.detail or {}) | {"round_index": round_index, "continue_run": outcome.continue_run}
        self.mailbox.acknowledge(path, request, state="APPLIED", detail=detail)
        self.last_outcome = outcome
        self.applied_requests.append({**request, "state": "APPLIED", "detail": detail})
        if outcome.requirement_change:
            self.requirement_change = outcome.requirement_change
        return outcome.continue_run
