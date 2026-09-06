"""Pure dashboard projections over authoritative MOSAIC-Ω runtime data.

Truthfulness rule: a dashboard value is either copied from an authoritative
runtime record, derived by an explicitly documented formula, marked as an
estimate/configuration, or shown as unavailable.  No synthetic runtime values
are introduced in this module.
"""
from __future__ import annotations

import time
from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

from .tracing import trace_summary


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "to_dict"):
        return dict(value.to_dict())
    return {}


def _status(task: Mapping[str, Any]) -> str:
    return str(task.get("status", task.get("state", "UNKNOWN")))


def _event_type(event: Mapping[str, Any]) -> str:
    return str(event.get("type", event.get("event_type", "UNKNOWN")))


def _task_id(task: Mapping[str, Any]) -> str:
    return str(task.get("node_id", task.get("task_id", "")))


def _event_task_id(event: Mapping[str, Any]) -> str:
    return str(event.get("node_id", event.get("task_id", "")) or "")


def _event_time(event: Mapping[str, Any]) -> float:
    value = event.get("timestamp", event.get("occurred_at", 0.0))
    return float(value or 0.0)


def _payload(event: Mapping[str, Any]) -> dict[str, Any]:
    raw = event.get("payload", {})
    return dict(raw) if isinstance(raw, Mapping) else {}


def _run_metadata(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    for event in events:
        if _event_type(event) == "RUN_CREATED":
            metadata = _payload(event).get("metadata", {})
            return dict(metadata) if isinstance(metadata, Mapping) else {}
    return {}


def _state_provenance(task_id: str, status: str, events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for event in events:
        if _event_task_id(event) != task_id:
            continue
        et = _event_type(event)
        payload = _payload(event)
        if et in {"TASK_STATE_CHANGED", "TASK_RECOVERED", "TASK_REPLANNED"}:
            if str(payload.get("to", "")) == status:
                candidates.append(event)
        elif et == "TASK_CREATED" and status == "CREATED":
            candidates.append(event)
    if not candidates:
        return {
            "event_id": None,
            "event_type": None,
            "entered_at": None,
            "reason": "no matching authoritative state event found",
            "source": "EventStore",
        }
    event = candidates[-1]
    return {
        "event_id": event.get("event_id"),
        "event_type": _event_type(event),
        "entered_at": event.get("timestamp", event.get("occurred_at")),
        "actor_id": event.get("actor_id"),
        "reason": _payload(event).get("reason", "task created" if _event_type(event) == "TASK_CREATED" else ""),
        "source": "EventStore",
    }


def _recovery_events(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    # Failure/error events are not recovery by themselves.  Only explicit
    # recovery/replan/rollback/safe-stop actions or decisions belong here.
    recovery_types = {
        "TASK_RECOVERED",
        "TASK_REPLANNED",
        "RECOVERY_PLANNED",
        "REPLAN_REQUIRED",
        "ROLLBACK_REQUIRED",
        "ROLLBACK_EXECUTED",
        "SAFE_STOP_TRIGGERED",
        "EVIDENCE_INVALIDATION_RECOVERY",
    }
    return [event for event in events if _event_type(event).upper() in recovery_types]


LOCAL_TRANSPORTS = frozenset({"local_process"})


def _is_local_execution(provenance: Mapping[str, Any]) -> bool:
    """Whether this execution record describes on-device work with no provider call.

    Device-tier deterministic Agents record provenance for traceability, but
    counting them as model API requests would inflate the request/token numbers
    and hide the fact that no inference happened.
    """
    if provenance.get("network_egress") is False:
        return True
    return str(provenance.get("transport") or "") in LOCAL_TRANSPORTS


def _local_executions(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        if _event_type(event) != "TOOL_EXECUTED":
            continue
        call = _payload(event).get("tool_call", {})
        if not isinstance(call, Mapping):
            continue
        arguments = call.get("arguments", {})
        if not isinstance(arguments, Mapping):
            continue
        provenance = arguments.get("api_provenance")
        if not isinstance(provenance, Mapping) or not _is_local_execution(provenance):
            continue
        rows.append({
            "event_id": event.get("event_id"),
            "task_id": _event_task_id(event),
            "actor_id": event.get("actor_id"),
            "tool_name": call.get("tool_name"),
            "execution_semantics": provenance.get("execution_semantics", "deterministic_local_compilation"),
            "network_egress": False,
            "source_path": "events[TOOL_EXECUTED].payload.tool_call.arguments.api_provenance",
        })
    return rows


def _api_calls(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for event in events:
        if _event_type(event) != "TOOL_EXECUTED":
            continue
        payload = _payload(event)
        call = payload.get("tool_call", {})
        if not isinstance(call, Mapping):
            continue
        arguments = call.get("arguments", {})
        if not isinstance(arguments, Mapping):
            continue
        provenance = arguments.get("api_provenance")
        if not isinstance(provenance, Mapping):
            continue
        if _is_local_execution(provenance):
            continue
        usage = provenance.get("usage", {})
        calls.append({
            "event_id": event.get("event_id"),
            "timestamp": event.get("timestamp", event.get("occurred_at")),
            "task_id": _event_task_id(event),
            "actor_id": event.get("actor_id"),
            "provider": provenance.get("provider"),
            "model": provenance.get("model"),
            "request_id": provenance.get("request_id"),
            "base_url": provenance.get("base_url"),
            "role": provenance.get("role"),
            "transport": provenance.get("transport"),
            "endpoint_host": provenance.get("endpoint_host"),
            "official_endpoint_verified": provenance.get("official_endpoint_verified"),
            "prompt_tokens": usage.get("prompt_tokens") if isinstance(usage, Mapping) else None,
            "completion_tokens": usage.get("completion_tokens") if isinstance(usage, Mapping) else None,
            "total_tokens": usage.get("total_tokens") if isinstance(usage, Mapping) else None,
            "source_path": "events[TOOL_EXECUTED].payload.tool_call.arguments.api_provenance",
        })
    return calls


def _api_usage(calls: Sequence[dict[str, Any]]) -> dict[str, Any]:
    def summed(field: str) -> tuple[int | None, int]:
        values = [item.get(field) for item in calls]
        numeric = [int(v) for v in values if isinstance(v, (int, float))]
        return (sum(numeric), len(numeric)) if numeric else (None, 0)

    prompt, prompt_samples = summed("prompt_tokens")
    completion, completion_samples = summed("completion_tokens")
    total, total_samples = summed("total_tokens")
    request_ids = [str(item["request_id"]) for item in calls if item.get("request_id")]
    return {
        "request_count": len(calls),
        "request_id_count": len(set(request_ids)),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "prompt_token_samples": prompt_samples,
        "completion_token_samples": completion_samples,
        "total_token_samples": total_samples,
        "token_status": "measured_api_usage" if total_samples else "insufficient_data",
        "calls": list(calls),
        "formula": "sum(api_provenance.usage.<field>) over TOOL_EXECUTED events with numeric API usage",
    }


def _execution_semantics(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Classify what the authoritative ToolRuntime actually executed.

    ``task`` persists an Agent reasoning result as a deliverable, but it is not an
    external/local tool action.  Judge-facing closed-loop evidence therefore
    requires at least one successful concrete tool call (read/write/shell/build/test
    or another non-``task`` ToolRuntime tool) instead of treating text generation
    as physical/software execution.

    The same rule applies to a ``write_file`` whose content is the planning
    Agent's own text: wrapping generated prose in a file write does not turn it
    into software or device execution.  Such calls carry ``api_provenance``
    (provider or local compiler authorship), which is how they are told apart
    from a tool call that genuinely acts on the environment.
    """
    rows: list[dict[str, Any]] = []
    for event in events:
        if _event_type(event) != "TOOL_EXECUTED":
            continue
        payload = _payload(event)
        call = payload.get("tool_call")
        result = payload.get("result")
        if not isinstance(call, Mapping):
            continue
        tool_name = str(call.get("tool_name") or "").strip() or "UNKNOWN"
        success = bool(result.get("success")) if isinstance(result, Mapping) else False
        arguments = call.get("arguments", {}) if isinstance(call.get("arguments"), Mapping) else {}
        intent = str(arguments.get("execution_intent") or "").strip()
        if intent:
            # The adapter declared what this call is. Trust the declaration.
            authored_by_planner = intent == "persist_planner_output"
        else:
            # Legacy events without a declaration: a call carrying planner
            # provenance is persisting planner output.
            authored_by_planner = isinstance(arguments.get("api_provenance"), Mapping)
        rows.append({
            "event_id": event.get("event_id"),
            "task_id": _event_task_id(event),
            "actor_id": event.get("actor_id"),
            "tool_name": tool_name,
            "success": success,
            "execution_intent": intent or ("persist_planner_output" if authored_by_planner else "act_on_environment"),
            "persists_planner_output": authored_by_planner,
            "source": "EventStore TOOL_EXECUTED.payload.tool_call/result",
        })
    reasoning = [row for row in rows if row["tool_name"] == "task" or row["persists_planner_output"]]
    concrete = [row for row in rows if not (row["tool_name"] == "task" or row["persists_planner_output"])]
    successful_concrete = [row for row in concrete if row["success"]]
    if successful_concrete:
        verdict = "CONCRETE_TOOL_EXECUTION_VERIFIED"
    elif rows and not concrete:
        verdict = "REASONING_DELIVERABLE_ONLY"
    elif concrete:
        verdict = "CONCRETE_TOOL_EXECUTION_FAILED_OR_UNVERIFIED"
    else:
        verdict = "NO_TOOL_EXECUTION"
    return {
        "verdict": verdict,
        "tool_call_count": len(rows),
        "reasoning_deliverable_call_count": len(reasoning),
        "concrete_tool_call_count": len(concrete),
        "successful_concrete_tool_call_count": len(successful_concrete),
        "concrete_tools": sorted({str(row["tool_name"]) for row in concrete}),
        "calls": rows,
        "rule": (
            "tool_name=task, and any tool call that merely persists the planning Agent's own output "
            "(identified by api_provenance on the ToolCall), is reasoning output persisted as a deliverable; "
            "it does not count as concrete software/device execution. Judge closed-loop evidence requires a "
            "successful ToolRuntime call that acts on the environment."
        ),
    }


def _graph_layers(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, str]],
) -> dict[str, int]:
    """Longest-path depth per node, so a DAG can be drawn in dependency layers.

    A flat row of boxes cannot show which work is parallel; the layer index is
    what makes "these four tasks run at the same time" visible.
    """
    parents: dict[str, list[str]] = {str(n["id"]): [] for n in nodes}
    for edge in edges:
        target, source = str(edge.get("target")), str(edge.get("source"))
        if target in parents and source in parents:
            parents[target].append(source)
    level: dict[str, int] = {}

    def depth(node_id: str, seen: frozenset[str]) -> int:
        if node_id in level:
            return level[node_id]
        if node_id in seen:  # defensive: the execution graph is acyclic
            return 0
        value = 0
        for parent in parents.get(node_id, ()):
            value = max(value, depth(parent, seen | {node_id}) + 1)
        level[node_id] = value
        return value

    for node in nodes:
        depth(str(node["id"]), frozenset())
    return level


def _critical_path(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, str]],
    durations: Mapping[str, float],
) -> list[str]:
    """Longest measured-duration path through the DAG.

    Weighted by real per-task wall clock when available, so the highlighted path
    is the one that actually determined how long the run took, not a guess.
    """
    parents: dict[str, list[str]] = {str(n["id"]): [] for n in nodes}
    for edge in edges:
        target, source = str(edge.get("target")), str(edge.get("source"))
        if target in parents and source in parents:
            parents[target].append(source)
    best: dict[str, tuple[float, list[str]]] = {}

    def walk(node_id: str, seen: frozenset[str]) -> tuple[float, list[str]]:
        if node_id in best:
            return best[node_id]
        if node_id in seen:
            return 0.0, []
        own = float(durations.get(node_id, 0.0) or 0.0)
        chain: list[str] = []
        total = 0.0
        for parent in parents.get(node_id, ()):
            weight, path = walk(parent, seen | {node_id})
            if weight >= total:
                total, chain = weight, path
        best[node_id] = (total + own, [*chain, node_id])
        return best[node_id]

    winner: tuple[float, list[str]] = (0.0, [])
    for node in nodes:
        candidate = walk(str(node["id"]), frozenset())
        if candidate[0] > winner[0]:
            winner = candidate
    return winner[1]


def _phase_timing_summary(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate measured per-phase wall clock so slowness can be attributed.

    Without this the console could only report a total run duration, which made
    it impossible to tell a slow model provider apart from slow tool execution
    or slow verification.
    """
    per_task: list[dict[str, Any]] = []
    totals: dict[str, float] = {}
    for event in events:
        if _event_type(event) != "TASK_PHASE_TIMING":
            continue
        payload = _payload(event)
        timings = payload.get("timings_ms", {})
        if not isinstance(timings, Mapping):
            continue
        row = {
            "task_id": _event_task_id(event),
            "agent_id": payload.get("agent_id"),
            "timings_ms": {str(k): float(v) for k, v in timings.items() if isinstance(v, (int, float))},
        }
        per_task.append(row)
        for key, value in row["timings_ms"].items():
            totals[key] = totals.get(key, 0.0) + value
    slowest = sorted(per_task, key=lambda item: item["timings_ms"].get("total_ms", 0.0), reverse=True)
    wall = totals.get("total_ms", 0.0)
    return {
        "sample_count": len(per_task),
        "totals_ms": {key: round(value, 3) for key, value in sorted(totals.items())},
        "share_of_task_time": {
            key: round(value / wall, 4)
            for key, value in sorted(totals.items())
            if wall > 0 and key != "total_ms"
        },
        "slowest_tasks": slowest[:10],
        "measurement": (
            "wall clock measured inside Orchestrator._execute_assignment; totals sum across tasks and "
            "therefore exceed run duration when tasks execute in parallel"
        ),
    }


def _scheduling_rounds(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every explainable scheduler decision in this run, oldest first."""
    rows: list[dict[str, Any]] = []
    for event in events:
        if _event_type(event) != "SCHEDULING_ROUND":
            continue
        payload = _payload(event)
        payload["event_id"] = event.get("event_id")
        payload["timestamp"] = event.get("timestamp", event.get("occurred_at"))
        rows.append(payload)
    return rows


def _latest_task_scheduling(rounds: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in rounds:
        for task in record.get("tasks", []) or []:
            if isinstance(task, Mapping) and task.get("task_id"):
                latest[str(task["task_id"])] = dict(task) | {
                    "round_index": record.get("round_index"),
                    "solver_status": record.get("solver_status"),
                    "observed_at": record.get("timestamp"),
                }
    return latest


#: Scheduling-lifecycle labels shown to the user.  The point of this vocabulary
#: is that "not running" is never rendered as one undifferentiated ``未分配``:
#: waiting on a dependency, waiting for capacity and having no legal executor are
#: three different situations with three different remedies.
SCHEDULING_STATES = (
    "BLOCKED",              # dependencies have not all succeeded yet
    "CANDIDATE_READY",      # READY, feasible candidates found, not yet solved
    "QUEUED",               # feasible but no Agent/resource slot this round
    "NO_FEASIBLE_AGENT",    # every candidate eliminated by a hard constraint
    "ASSIGNED",             # committed Assignment exists, execution not started
    "RUNNING",
    "VERIFYING",
    "REASSIGNING",          # recovery is re-solving after a failure/offline Agent
    "SUCCEEDED",
    "FAILED",
    "PAUSED",
)


def _scheduling_state(
    task: Mapping[str, Any],
    *,
    task_states: Mapping[str, str],
    diagnostic: Mapping[str, Any] | None,
    reassigning: bool,
) -> dict[str, Any]:
    """Resolve one task's scheduling-lifecycle state and the reason for it."""
    status = _status(task)
    if status in {"SUCCEEDED", "FAILED", "PAUSED", "RUNNING", "VERIFYING"}:
        return {
            "state": status,
            "reason": f"authoritative task state is {status}",
            "source": "EventStore task projection",
        }
    if reassigning:
        return {
            "state": "REASSIGNING",
            "reason": "recovery returned this task to the scheduler after a failure or Agent going offline",
            "source": "EventStore recovery events",
        }
    if status == "PLANNED":
        pending = [
            str(parent) for parent in (task.get("predecessors") or task.get("depends_on") or [])
            if task_states.get(str(parent)) != "SUCCEEDED"
        ]
        return {
            "state": "BLOCKED",
            "reason": (
                f"waiting for {len(pending)} dependency task(s) to succeed: {pending[:6]}"
                if pending else "planned but not yet released by the orchestrator"
            ),
            "blocking_task_ids": pending,
            "source": "task graph dependencies + EventStore task states",
        }
    if status == "READY":
        if diagnostic is None:
            return {
                "state": "CANDIDATE_READY",
                "reason": "READY and awaiting the next scheduling round; no solver decision recorded yet",
                "source": "EventStore task state",
            }
        state = str(diagnostic.get("state") or "QUEUED")
        if state == "ASSIGNED":
            # The solver committed a bundle but the task projection has not moved
            # to RUNNING yet.
            state = "ASSIGNED"
        return {
            "state": state if state in SCHEDULING_STATES else "QUEUED",
            "reason": str(diagnostic.get("reason") or ""),
            "candidate_count": diagnostic.get("candidate_count", 0),
            "rejected_count": diagnostic.get("rejected_count", 0),
            "round_index": diagnostic.get("round_index"),
            "source": "Scheduler SCHEDULING_ROUND diagnostic",
        }
    return {
        "state": status,
        "reason": f"task state {status} has no scheduling interpretation",
        "source": "EventStore task projection",
    }


def _display_status_contracts() -> list[dict[str, str]]:
    """Contracts for every state/verdict-like value rendered by the operator console.

    `entered_at` semantics are explicit: event-sourced states point to the event
    time; derived verdicts are recomputed when the snapshot is generated and
    therefore do not pretend to have an independent persisted transition time.
    """
    return [
        {"family": "Task", "state": "CREATED/PLANNED/READY/RUNNING/VERIFYING/SUCCEEDED/FAILED/PAUSED", "timing": "task_graph.nodes[].status_provenance.entered_at", "entered_when": "the matching EventStore transition/recovery/replan event becomes authoritative for the task", "source": "EventStore"},
        {"family": "Run", "state": "CREATED/RUNNING/SUCCEEDED/FAILED/PAUSED", "timing": "generated_at", "entered_when": "derived from the current task projection at snapshot generation; this is a projection verdict, not a separately persisted run state machine", "source": "build_dashboard_snapshot"},
        {"family": "Agent authenticity", "state": "REAL_API_VERIFIED and non-strict verdicts", "timing": "generated_at", "entered_when": "derived at snapshot generation from committed assignments + bound adapter metadata + TOOL_EXECUTED API provenance; strict-real is fail-closed", "source": "_authenticity"},
        {"family": "Scheduler", "state": "ORTOOLS_VERIFIED / ORTOOLS_PROVENANCE_INVALID / NON_ORTOOLS_RUN", "timing": "generated_at", "entered_when": "derived from committed Assignment.policy and solver_provenance at snapshot generation", "source": "build_dashboard_snapshot.scheduler.solver_integrity"},
        {"family": "Agent registry", "state": "ONLINE / OFFLINE / UNKNOWN", "timing": "scheduler.capabilities[].metadata.status_updated_at", "entered_when": "CapabilityRegistry register/heartbeat/offline updates the authoritative profile; UNKNOWN is shown when the field is unavailable", "source": "CapabilityRegistry"},
        {"family": "Verification", "state": "PASS / FAIL / UNKNOWN", "timing": "evidence.verification_results[].timestamp", "entered_when": "TASK_VERIFIED is appended after verifier evaluation; UNKNOWN is only a display of missing boolean result", "source": "EventStore TASK_VERIFIED"},
        {"family": "Evidence", "state": "VERIFIED / REJECTED / UNVERIFIED", "timing": "evidence.items[].verification_provenance.timestamp or evidence.items[].created_at", "entered_when": "VERIFIED/REJECTED is tied to the TASK_VERIFIED event that names the evidence_id; UNVERIFIED is the ToolRuntime creation state", "source": "ToolRuntime + EventStore"},
        {"family": "ContextPack", "state": "FULL / TRUNCATED", "timing": "memory.context_packs[].created_at", "entered_when": "ContextBuilder materializes the pack and sets truncated according to its token-budget selection logic", "source": "MemoryService ContextPack"},
        {"family": "API usage", "state": "API USAGE MEASURED / INSUFFICIENT DATA", "timing": "generated_at", "entered_when": "derived from whether TOOL_EXECUTED api_provenance contains numeric provider response usage", "source": "_api_usage"},
        {"family": "Snapshot freshness", "state": "SNAPSHOT FRESH / SNAPSHOT STALE", "timing": "browser-now", "entered_when": "frontend-only derived status: max(0,browser_now-generated_at) <= 5s means FRESH; it is explicitly not a backend execution state", "source": "console app.js"},
        {"family": "Console waiting", "state": "等待快照", "timing": "browser fetch failure/no snapshot", "entered_when": "frontend has not obtained /api/snapshot; it does not mean any Agent is running", "source": "console app.js"},
    ]


def _authenticity(
    assignments: Sequence[dict[str, Any]],
    capabilities: Sequence[dict[str, Any]],
    events: Sequence[dict[str, Any]],
    run_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    caps = {str(c.get("actor_id")): c for c in capabilities if c.get("kind") == "agent"}
    assigned = sorted({str(a.get("agent_id")) for a in assignments if a.get("agent_id")})
    details: list[dict[str, Any]] = []
    for actor_id in assigned:
        cap = caps.get(actor_id, {})
        metadata = cap.get("metadata", {}) if isinstance(cap.get("metadata"), Mapping) else {}
        details.append({
            "actor_id": actor_id,
            "adapter_bound": bool(metadata.get("adapter_bound", False)),
            "adapter_class": metadata.get("adapter_class"),
            "mode": metadata.get("authenticity_mode", "unclassified"),
            "online": cap.get("online"),
            "registered_at": metadata.get("registered_at"),
            "status_updated_at": metadata.get("status_updated_at"),
            "api_transport": metadata.get("api_transport"),
            "endpoint_host": metadata.get("endpoint_host"),
            "official_endpoint_verified": bool(metadata.get("official_endpoint_verified", False)),
        })

    modes = {str(item.get("mode")) for item in details}
    api_calls = _api_calls(events)
    api_by_actor = Counter(str(item.get("actor_id")) for item in api_calls)
    executed_by_actor = Counter(
        str(event.get("actor_id")) for event in events if _event_type(event) == "TOOL_EXECUTED"
    )
    missing_api = [
        actor_id for actor_id in assigned
        if caps.get(actor_id, {}).get("metadata", {}).get("authenticity_mode") == "real_api"
        and executed_by_actor.get(actor_id, 0) > api_by_actor.get(actor_id, 0)
    ]
    unbound = [item["actor_id"] for item in details if not item["adapter_bound"]]
    mock = [item["actor_id"] for item in details if item["mode"] == "mock"]
    explicit_fallback = [item["actor_id"] for item in details if item["mode"] == "api_with_explicit_fallback"]
    test_fixture = [item["actor_id"] for item in details if item["mode"] == "test_fixture"]
    test_endpoint = [item["actor_id"] for item in details if item["mode"] == "api_test_endpoint"]
    real_transports = {"openai_sdk", "stdlib_http"}
    bad_real_provenance = [
        item for item in api_calls
        if str(item.get("actor_id")) in assigned
        and str(item.get("transport")) not in real_transports
    ]
    missing_request_id = [
        item for item in api_calls
        if str(item.get("actor_id")) in assigned and not item.get("request_id")
    ]
    capability_by_actor = {item["actor_id"]: item for item in details}
    unverified_official_endpoint = [
        item["actor_id"] for item in details
        if item.get("mode") == "real_api" and not item.get("official_endpoint_verified")
    ]
    endpoint_mismatch = [
        item for item in api_calls
        if str(item.get("actor_id")) in assigned
        and (
            not item.get("official_endpoint_verified")
            or str(item.get("endpoint_host") or "").casefold()
            != str(capability_by_actor.get(str(item.get("actor_id")), {}).get("endpoint_host") or "").casefold()
        )
    ]

    # Device-tier deterministic Agents are a declared part of the heterogeneous
    # architecture, not an unclassified mix: they run locally on purpose so that
    # privacy-restricted work and already-compiled requirement baselines never
    # need a provider call.  They are listed explicitly so a reviewer can see
    # exactly which nodes did not use a model.
    local_deterministic = [item["actor_id"] for item in details if item["mode"] == "deterministic_tool_executor"]
    real_api_agents = [item["actor_id"] for item in details if item["mode"] == "real_api"]
    real_api_clean = (
        bool(real_api_agents)
        and not missing_api
        and bool(api_calls)
        and not bad_real_provenance
        and not missing_request_id
        and not unverified_official_endpoint
        and not endpoint_mismatch
    )

    if mock:
        verdict = "MOCK_EXECUTION"
    elif unbound:
        verdict = "UNBOUND_AGENT"
    elif explicit_fallback:
        verdict = "FALLBACK_CAPABLE_NOT_COMPETITION_STRICT"
    elif test_fixture:
        verdict = "TEST_FIXTURE_NOT_COMPETITION_STRICT"
    elif test_endpoint:
        verdict = "API_TEST_ENDPOINT_NOT_COMPETITION_STRICT"
    elif details and modes == {"real_api"} and real_api_clean:
        verdict = "REAL_API_VERIFIED"
    elif details and modes == {"real_api", "deterministic_tool_executor"} and real_api_clean:
        verdict = "REAL_API_AND_LOCAL_DETERMINISTIC_VERIFIED"
    elif details and modes == {"remote_rpc"}:
        verdict = "REMOTE_RPC_BOUND"
    elif details and modes == {"deterministic_tool_executor"}:
        verdict = "DETERMINISTIC_TOOL_EXECUTOR"
    elif details:
        verdict = "MIXED_OR_UNCLASSIFIED"
    else:
        verdict = "NO_ASSIGNED_AGENT"

    local_rows = _local_executions(events)
    return {
        "verdict": verdict,
        "competition_strict_real_agent": verdict in {
            "REAL_API_VERIFIED", "REAL_API_AND_LOCAL_DETERMINISTIC_VERIFIED"
        },
        "expected_agent_mode": run_metadata.get("agent_mode"),
        "assigned_agent_count": len(assigned),
        "assigned_agents": details,
        "mock_agents": mock,
        "unbound_agents": unbound,
        "real_api_agents": real_api_agents,
        "local_deterministic_agents": local_deterministic,
        "local_execution_count": len(local_rows),
        "local_executions": local_rows,
        "real_api_missing_provenance": missing_api,
        "test_fixture_agents": test_fixture,
        "api_test_endpoint_agents": test_endpoint,
        "bad_real_provenance_count": len(bad_real_provenance),
        "missing_request_id_count": len(missing_request_id),
        "unverified_official_endpoint_agents": unverified_official_endpoint,
        "endpoint_mismatch_count": len(endpoint_mismatch),
        "api_usage": _api_usage(api_calls),
        "rule": (
            "REAL_API_VERIFIED requires all assigned agents to be explicitly bound real_api adapters, "
            "an official-provider endpoint, at least one TOOL_EXECUTED API provenance record using a real network transport, "
            "non-empty provider request IDs, and no real_api execution lacking provenance. "
            "REAL_API_AND_LOCAL_DETERMINISTIC_VERIFIED additionally allows declared device-tier deterministic "
            "Agents, whose executions are counted separately under local_executions and never as model API requests."
        ),
    }


def _communication_summary(
    messages: Sequence[dict[str, Any]],
    telemetry: Mapping[str, Any],
    api_usage: Mapping[str, Any],
) -> dict[str, Any]:
    action_counts = Counter(str(item.get("policy_action", "UNKNOWN")).upper() for item in messages)
    # INTERNAL is an explicitly recorded intra-Agent handoff, not an inter-Agent
    # message.  It is reported separately so a low message count can be read as
    # "the same Agent kept the context" rather than "communication failed".
    internal = [item for item in messages if str(item.get("policy_action", "")).upper() == "INTERNAL"]
    routed = [item for item in messages if str(item.get("policy_action", "")).upper() != "INTERNAL"]
    token_usage = {
        "status": api_usage.get("token_status", "insufficient_data"),
        "input_total": api_usage.get("prompt_tokens"),
        "output_total": api_usage.get("completion_tokens"),
        "total": api_usage.get("total_tokens"),
        "request_count": api_usage.get("request_count", 0),
        "source": "DeepSeek/OpenAI-compatible response.usage captured in TOOL_EXECUTED api_provenance",
        "note": (
            None if api_usage.get("token_status") == "measured_api_usage"
            else "当前运行没有可核验 API usage；控制台不会使用字符数或估算值冒充模型 Token。"
        ),
    }
    return {
        "total": len(routed),
        "decision_count": len(messages),
        "internal_handoff_count": len(internal),
        "internal_handoffs": internal,
        "action_counts": dict(action_counts),
        "items": list(messages),
        "queue_wait_ms": telemetry.get("queue_wait_ms", {}) if isinstance(telemetry, Mapping) else {},
        "mqtt_rtt_ms": telemetry.get("mqtt_rtt_ms", {}) if isinstance(telemetry, Mapping) else {},
        "token_usage": token_usage,
    }


def _memory_summary(
    memory_records: Sequence[dict[str, Any]],
    context_packs: Mapping[str, Any],
    metrics: Mapping[str, Any],
) -> dict[str, Any]:
    type_counts = Counter(str(item.get("memory_type", "unknown")) for item in memory_records)
    status_counts = Counter(str(item.get("verification_status", "unknown")) for item in memory_records)
    packs = list(context_packs.values())
    type_counts["working"] = max(type_counts.get("working", 0), len(packs))
    total_pack_tokens = sum(
        int(pack.get("token_estimate", 0) or 0) for pack in packs if isinstance(pack, Mapping)
    )
    total_history_tokens = sum(
        int(pack.get("full_history_token_estimate", 0) or 0) for pack in packs if isinstance(pack, Mapping)
    )
    # Aggregate the per-pack retrieval traces into one pipeline view so the
    # console can render full history -> candidates -> ranking -> dedupe ->
    # ContextPack -> awakening instead of only the finished packs.
    traces = [
        pack.get("selection_trace") for pack in packs
        if isinstance(pack, Mapping) and isinstance(pack.get("selection_trace"), Mapping)
    ]
    stale_records = [
        item for item in memory_records
        if str(item.get("verification_status", "")).upper() in {"STALE", "REJECTED"}
    ]
    pipeline = {
        "sample_count": len(traces),
        "stages": list(traces[-1].get("pipeline", ())) if traces else [],
        "totals": {
            "full_history_records": sum(
                int((t.get("full_history") or {}).get("record_count", 0) or 0) for t in traces
            ),
            "raw_candidates": sum(int(t.get("raw_candidate_count", 0) or 0) for t in traces),
            "deduplicated_candidates": sum(int(t.get("deduplicated_candidate_count", 0) or 0) for t in traces),
            "filtered_out": sum(int(t.get("filtered_out_count", 0) or 0) for t in traces),
            "forced_core": sum(int(t.get("forced_count", 0) or 0) for t in traces),
            "selected": sum(int(t.get("selected_count", 0) or 0) for t in traces),
        },
        "ranking_formula": traces[-1].get("ranking_formula") if traces else None,
        "stale_records": [
            {
                "memory_id": item.get("memory_id"),
                "summary": str(item.get("summary") or item.get("content") or "")[:160],
                "verification_status": item.get("verification_status"),
                "node_id": item.get("node_id"),
            }
            for item in stale_records[:40]
        ],
        "stale_record_count": len(stale_records),
        "rule": (
            "each ContextPack carries its own selection_trace; totals sum across packs, so a memory "
            "recalled by several nodes is counted once per pack, not once per run"
        ),
    }

    metric_copy = dict(metrics)
    metric_samples = {
        "key_memory_recall_rate": int(metric_copy.get("recall_expected", 0) or 0),
        "avg_recall_latency_ms": int(metric_copy.get("recall_requests", 0) or 0),
        "snapshot_restore_consistency_rate": int(metric_copy.get("snapshot_consistency_checks", 0) or 0),
        "invalidation_propagation_accuracy": int(metric_copy.get("invalidation_checks", 0) or 0),
    }
    return {
        "record_count": len(memory_records),
        "type_counts": dict(type_counts),
        "status_counts": dict(status_counts),
        "metrics": metric_copy,
        "metric_samples": metric_samples,
        "context_pack_count": len(packs),
        "context_pack_token_estimate": total_pack_tokens,
        "full_history_token_estimate": total_history_tokens,
        "token_reduction_estimate": (
            1 - total_pack_tokens / total_history_tokens if total_history_tokens else None
        ),
        "token_measurement_semantics": "ESTIMATED_CHARACTER_HEURISTIC",
        "token_estimator": "memory_recovery.context_builder.estimate_tokens: max(1, (len(text)+1)//2)",
        "measured_api_token_source": "authenticity.api_usage / communication.token_usage only",
        "pipeline": pipeline,
        "context_packs": dict(context_packs),
    }


def _lineage() -> list[dict[str, str]]:
    """Machine-readable source/formula contract for every runtime number shown in the console."""
    endpoint = "/api/snapshot[?run_id=...]"
    rows = [
        ("Run Status", "run.status", "Task projections + EventStore", "SUCCEEDED if all tasks succeeded; FAILED if any failed; PAUSED if terminal paused; RUNNING if events exist; otherwise CREATED; derived when snapshot is generated"),
        ("Run Phase", "phase", "MosaicMainChain observability publisher", "direct lifecycle phase supplied by the runtime when snapshot is written"),
        ("任务成功率", "run.success_rate + run.succeeded/run.task_count", "tasks[].state/status", "count(SUCCEEDED) / count(tasks); numerator/denominator shown directly"),
        ("Progress ring %", "run.succeeded + run.task_count", "tasks[].state/status", "round(100 * succeeded / task_count); task_count=0 is rendered as no-task state"),
        ("端到端时延", "run.e2e_latency_ms", "events[].timestamp", "(max(timestamp)-min(timestamp))*1000 when at least two timestamped events exist; otherwise unavailable; event-span, not model-only latency"),
        ("Events", "run.event_count", "EventStore events", "len(events)"),
        ("Evidence", "run.evidence_count", "task projections[].evidence", "sum(len(task.evidence))"),
        ("Verified Tasks", "run.verified_task_count", "TASK_VERIFIED events", "count(TASK_VERIFIED where payload.passed=true)"),
        ("Messages", "run.message_count", "communication policy decisions or envelopes", "len(communication_decisions if present else communication)"),
        ("Active execution units", "run.active_agent_count", "CapabilityRegistry", "count(kind=agent and online is explicitly true); missing/unknown online is never treated as active"),
        ("Task status counts", "task_graph.status_counts", "tasks[].state/status", "Counter(status); each legend number is the exact counter value"),
        ("Task priority", "task_graph.nodes[].priority", "TaskNodeView.priority", "direct field; no UI recomputation"),
        ("Task evidence count", "task_graph.nodes[].evidence_count", "TaskNodeView.evidence", "len(task.evidence)"),
        ("Task status entered at", "task_graph.nodes[].status_provenance.entered_at", "TASK_STATE_CHANGED/TASK_RECOVERED/TASK_REPLANNED", "latest matching event whose payload.to equals current task status"),
        ("Task status event id", "task_graph.nodes[].status_provenance.event_id", "EventStore", "event_id of authoritative transition event"),
        ("Topology Nodes", "topology.nodes", "TopologySnapshot.nodes", "len(nodes)"),
        ("Topology Edges", "topology.edges", "TopologySnapshot.edges", "len(edges)"),
        ("Topology edge score", "topology.edges[].score or topology.edge_scores", "message_topology edge scoring", "0.40*dependency_strength + 0.30*information_value + 0.20*reliability + 0.10*latency_score; no UI fallback score"),
        ("Topology edge width", "topology.edges[].score", "browser rendering of backend score", "1px if score absent; otherwise 1 + 3*max(0,score) px; visual width does not invent a score"),
        ("λ2 / connectivity", "topology.lambda2_or_connectivity", "TopologySnapshot", "backend topology guard output; UI does not synthesize"),
        ("Topology rebuild P95", "topology.telemetry.topology_recovery_ms.p95", "topology telemetry", "backend measured p95; unavailable if no measurement"),
        ("Message total", "communication.total", "communication decisions/envelopes", "len(communication.items)"),
        ("Message action counts", "communication.action_counts", "communication[].policy_action", "Counter(policy_action) for SEND/MERGE/DEFER/DROP"),
        ("Message bar width", "communication.action_counts", "communication action counters", "100 * action_count / max(1, max(action_counts)); bar is only a visual ratio"),
        ("Message priority", "communication.items[].priority", "MessageEnvelope/communication decision", "direct backend field"),
        ("Message TTL", "communication.items[].ttl", "MessageEnvelope/communication decision", "direct backend field"),
        ("API prompt tokens total", "communication.token_usage.input_total", "TOOL_EXECUTED...api_provenance.usage.prompt_tokens", "sum exact provider response usage; unavailable if not reported"),
        ("API completion tokens total", "communication.token_usage.output_total", "TOOL_EXECUTED...api_provenance.usage.completion_tokens", "sum exact provider response usage; unavailable if not reported"),
        ("API total tokens", "communication.token_usage.total", "TOOL_EXECUTED...api_provenance.usage.total_tokens", "sum exact provider response usage; never estimated from text length"),
        ("API request count", "communication.token_usage.request_count", "TOOL_EXECUTED api_provenance records", "count(api_provenance records)"),
        ("Queue wait P95", "communication.queue_wait_ms.p95", "runtime telemetry", "backend measured p95; unavailable if no sample"),
        ("MQTT RTT P95", "communication.mqtt_rtt_ms.p95", "MQTT telemetry", "backend measured p95; unavailable if no sample"),
        ("Assignments", "scheduler.assignment_count", "tasks[].assignment", "len(non-null assignments)"),
        ("Avg Cost", "scheduler.average_cost", "assignments[].total_cost", "mean(total_cost); cost-model score, not currency"),
        ("Assignment total cost", "scheduler.assignments[].total_cost", "CostModel.evaluate", "direct committed Assignment.total_cost"),
        ("Assignment cost breakdown", "scheduler.assignments[].cost_breakdown.*", "CostModel.evaluate", "direct component values; UI does not rescale"),
        ("Scheduler policy counts", "scheduler.policy_counts", "committed assignments[].policy", "Counter(policy); candidate_uncommitted values are never exposed as final assignments"),
        ("Solver provenance", "scheduler.assignments[].solver_provenance", "Scheduler", "OR-Tools assignments require an engine under the ortools.* namespace (CP-SAT joint assignment) and OPTIMAL/FEASIBLE status; built-in policies identify mosaic_builtin"),
        ("Agent online", "scheduler.capabilities[].online", "CapabilityRegistry", "registry.register/heartbeat/offline; timestamp in metadata.status_updated_at"),
        ("Agent endpoint host", "scheduler.capabilities[].metadata.endpoint_host", "bound execution adapter", "direct adapter endpoint host; competition strict requires official_endpoint_verified=true"),
        ("Official endpoint verification", "scheduler.capabilities[].metadata.official_endpoint_verified", "bound execution adapter", "true only when adapter uses approved real network transport and official provider endpoint"),
        ("Agent registered/status time", "scheduler.capabilities[].metadata.registered_at/status_updated_at", "CapabilityRegistry bridge", "direct backend timestamps"),
        ("Agent reliability", "scheduler.capabilities[].reliability/posterior", "CapabilityProfile + BetaPosterior", "configured prior until posterior sample count > 0"),
        ("Posterior sample count", "scheduler.capabilities[].posterior", "BetaPosterior alpha/beta", "sum(max(0,alpha+beta-2)) over posterior entries"),
        ("Agent latency", "scheduler.capabilities[].latency_ms", "CapabilityProfile", "shown only when metadata.latency_measurement_count>0; otherwise UNMEASURED"),
        ("Memory records", "memory.record_count", "MemoryService observability_records", "len(records)"),
        ("Memory layer counts", "memory.type_counts", "MemoryService records + current ContextPacks", "Counter(memory_type); working=max(record working count, context_pack_count)"),
        ("Memory metric values", "memory.metrics.*", "MemoryService metrics", "direct backend metric value, shown only with sample count where applicable"),
        ("Memory metric sample n", "memory.metric_samples.*", "MemoryService counters", "explicit sample count used to decide whether metric may be displayed"),
        ("STALE/REJECTED memory", "memory.status_counts.STALE/REJECTED", "MemoryRecord.verification_status", "Counter(exact verification_status)"),
        ("ContextPack token estimate", "memory.context_pack_token_estimate", "ContextPack.token_estimate", "sum heuristic estimates; estimator=max(1,(len(text)+1)//2), explicitly not model tokenizer usage"),
        ("Full history token estimate", "memory.full_history_token_estimate", "ContextPack.full_history_token_estimate", "sum heuristic history estimates"),
        ("Context reduction estimate", "memory.token_reduction_estimate", "ContextPack token estimates", "1 - context_pack_token_estimate/full_history_token_estimate"),
        ("ContextPack count", "memory.context_pack_count", "context_packs", "len(context_packs)"),
        ("ContextPack item counts", "memory.context_packs[].hard_constraints/prohibitions/evidence_refs/memory_ids", "ContextPack", "len(each list)"),
        ("Recovery Events", "recovery.event_count", "EventStore", "count explicit recovery action/decision event types only; failure/error alone is not recovery"),
        ("Affected Nodes", "recovery.affected_nodes", "recovery event payload.affected_task_ids", "set union of affected_task_ids"),
        ("Unaffected Nodes", "recovery.unaffected_node_count", "task graph + affected_nodes", "task_count-len(affected_nodes), only when recovery events exist"),
        ("Evidence manifest count", "evidence.items", "task projections[].evidence", "len(evidence.items)"),
        ("Verified/Rejected", "evidence.verified_count/rejected_count", "task evidence[].verification_status", "count exact VERIFIED/REJECTED status"),
        ("Verification result count", "evidence.verification_results", "TASK_VERIFIED events", "len(verification_results)"),
        ("Verification confidence", "evidence.verification_results[].confidence", "Verifier result event payload", "direct verifier field; no UI inference"),
        ("Verification PASS/FAIL time", "evidence.verification_results[].timestamp", "TASK_VERIFIED EventStore event", "direct event timestamp for the verifier decision"),
        ("Evidence verification status time", "evidence.items[].verification_provenance.timestamp", "TASK_VERIFIED EventStore event", "timestamp of TASK_VERIFIED that names evidence_id; unverified evidence falls back to its creation timestamp and is labeled as such"),
        ("ContextPack FULL/TRUNCATED time", "memory.context_packs[].created_at", "ContextPack", "pack creation timestamp; truncated flag is decided during that build"),
        ("Trace event count", "traces[].event_count", "events grouped by trace_id", "count(events in trace)"),
        ("Trace duration", "traces[].duration_ms", "events grouped by trace_id", "(max(timestamp)-min(timestamp))*1000"),
        ("Event/recovery timestamps", "events[].timestamp / recovery.timeline[].timestamp", "EventStore", "direct authoritative event timestamp; UI formats local clock only"),
        ("Execution semantics", "execution.verdict / execution.concrete_tool_call_count", "EventStore TOOL_EXECUTED", "classify tool_name=task as reasoning deliverable only; count successful non-task ToolRuntime calls as concrete execution evidence"),
        ("API Requests", "authenticity.api_usage.request_count", "TOOL_EXECUTED api_provenance", "count API provenance records"),
        ("API Total Tokens", "authenticity.api_usage.total_tokens", "provider response.usage.total_tokens", "sum numeric total_tokens fields; shown as — if insufficient_data"),
        ("Per-call API tokens", "authenticity.api_usage.calls[].prompt_tokens/completion_tokens/total_tokens", "provider response usage captured in event", "direct per-request usage; no estimation"),
        ("Per-call API endpoint", "authenticity.api_usage.calls[].endpoint_host/official_endpoint_verified", "TOOL_EXECUTED api_provenance", "direct adapter-captured endpoint host + verification flag; strict verdict rejects mismatch/unverified endpoint"),
        ("Assigned Agents", "authenticity.assigned_agent_count", "scheduler assignments", "count unique assignment.agent_id"),
        ("Snapshot freshness age", "generated_at", "Observability snapshot writer + browser clock", "max(0, browser_now-generated_at); threshold 5s; does not imply Agent execution"),
        ("Display status contracts", "state_machine.display_status_contracts", "backend + frontend contract registry", "declares timing/entry semantics for every status/verdict-like value shown by the console"),
    ]
    return [
        {"ui": ui, "api": endpoint, "field": field, "source": source, "calculation": calculation}
        for ui, field, source, calculation in rows
    ]

def build_dashboard_snapshot(
    *,
    run_id: str,
    phase: str,
    tasks: Iterable[Any],
    events: Iterable[Any],
    capabilities: Iterable[Any],
    communication: Sequence[Mapping[str, Any]],
    communication_decisions: Sequence[Mapping[str, Any]],
    context_packs: Mapping[str, Mapping[str, Any]],
    memory_records: Iterable[Any],
    memory_metrics: Mapping[str, Any],
    topology_snapshot: Mapping[str, Any],
    topology_telemetry: Mapping[str, Any],
    metric_snapshot: Mapping[str, Any],
    topology_history: Sequence[Mapping[str, Any]] = (),
    scheduling_rounds: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    task_dicts = [_as_dict(task) for task in tasks]
    event_dicts = [_as_dict(event) for event in events]
    capability_dicts = [_as_dict(item) for item in capabilities]
    memory_dicts = [_as_dict(item) for item in memory_records]
    event_dicts.sort(key=_event_time)

    status_counts = Counter(_status(task) for task in task_dicts)
    terminal = {"SUCCEEDED", "FAILED", "PAUSED"}
    succeeded = status_counts.get("SUCCEEDED", 0)
    timestamps = [_event_time(event) for event in event_dicts if _event_time(event) > 0]
    duration_ms = round((max(timestamps) - min(timestamps)) * 1000, 6) if len(timestamps) >= 2 else None
    all_terminal = bool(task_dicts) and all(_status(task) in terminal for task in task_dicts)
    if task_dicts and succeeded == len(task_dicts):
        run_status = "SUCCEEDED"
    elif status_counts.get("FAILED", 0):
        run_status = "FAILED"
    elif status_counts.get("PAUSED", 0) and all_terminal:
        run_status = "PAUSED"
    elif event_dicts:
        run_status = "RUNNING"
    else:
        run_status = "CREATED"

    # Scheduling lifecycle inputs, resolved once for every node below.
    round_records = list(scheduling_rounds) or _scheduling_rounds(event_dicts)
    task_diagnostics = _latest_task_scheduling(round_records)
    task_states = {_task_id(task): _status(task) for task in task_dicts}
    reassigning_ids = {
        _event_task_id(event) for event in event_dicts
        if _event_type(event) in {"TASK_RECOVERED", "TASK_REPLANNED"} and _event_task_id(event)
    }

    graph_nodes: list[dict[str, Any]] = []
    graph_edges: list[dict[str, str]] = []
    assignments: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for task in task_dicts:
        node_id = _task_id(task)
        status = _status(task)
        assignment = task.get("assignment") if isinstance(task.get("assignment"), Mapping) else None
        diagnostic = task_diagnostics.get(node_id)
        scheduling = _scheduling_state(
            task,
            task_states=task_states,
            diagnostic=diagnostic,
            # A task that was recovered/replanned and is now waiting again is
            # genuinely being reassigned, not merely idle.
            reassigning=node_id in reassigning_ids and status in {"READY", "PLANNED"},
        )
        graph_nodes.append({
            "id": node_id,
            "label": task.get("description") or node_id,
            "type": task.get("type", task.get("task_type", "general")),
            "status": status,
            "status_provenance": _state_provenance(node_id, status, event_dicts),
            "scheduling_state": scheduling["state"],
            "scheduling": scheduling,
            "candidates": (diagnostic or {}).get("candidates", []),
            "eliminated_candidates": (diagnostic or {}).get("rejected", []),
            "elimination_summary": (diagnostic or {}).get("rejection_summary", {}),
            "standby_candidates": (diagnostic or {}).get("standby", []),
            "risk": task.get("risk", "normal"),
            "priority": task.get("priority", 5),
            "assignment": dict(assignment) if assignment else None,
            "acceptance": task.get("acceptance", task.get("acceptance_conditions", [])),
            "evidence_count": len(task.get("evidence", []) or []),
            "attempt": task.get("attempt", 0),
        })
        for parent in task.get("predecessors", task.get("depends_on", [])) or []:
            graph_edges.append({"source": str(parent), "target": node_id})
        if assignment:
            assignments.append({
                "task_id": node_id,
                "task_type": task.get("type", task.get("task_type")),
                "risk": task.get("risk", "normal"),
                **dict(assignment),
            })
        for item in task.get("evidence", []) or []:
            if isinstance(item, Mapping):
                evidence.append(dict(item))

    verification_events: list[dict[str, Any]] = []
    for event in event_dicts:
        if _event_type(event) != "TASK_VERIFIED":
            continue
        payload = _payload(event)
        verification = payload.get("verification", {})
        if isinstance(verification, Mapping):
            verification_events.append(dict(verification) | {
                "event_id": event.get("event_id"),
                "trace_id": event.get("trace_id"),
                "task_id": _event_task_id(event),
                "timestamp": event.get("timestamp", event.get("occurred_at")),
            })

    verification_by_evidence: dict[str, dict[str, Any]] = {}
    for event in event_dicts:
        if _event_type(event) != "TASK_VERIFIED":
            continue
        payload = _payload(event)
        passed = payload.get("passed")
        for evidence_id in payload.get("evidence_ids", []) or []:
            verification_by_evidence[str(evidence_id)] = {
                "event_id": event.get("event_id"),
                "timestamp": event.get("timestamp", event.get("occurred_at")),
                "passed": passed,
                "source": "EventStore TASK_VERIFIED",
            }
    for item in evidence:
        evidence_id = str(item.get("evidence_id") or "")
        if evidence_id in verification_by_evidence:
            item["verification_provenance"] = verification_by_evidence[evidence_id]
        else:
            item["verification_provenance"] = {
                "event_id": None,
                "timestamp": item.get("created_at"),
                "passed": None,
                "source": "ToolRuntime evidence creation; no TASK_VERIFIED event found",
            }

    task_durations = {
        _event_task_id(event): float((_payload(event).get("timings_ms") or {}).get("total_ms", 0.0) or 0.0)
        for event in event_dicts
        if _event_type(event) == "TASK_PHASE_TIMING" and _event_task_id(event)
    }
    graph_levels = _graph_layers(graph_nodes, graph_edges)
    critical_path = _critical_path(graph_nodes, graph_edges, task_durations or {
        # Without measured timings, fall back to unit weights so the path is still
        # the structurally longest chain rather than nothing.
        str(node["id"]): 1.0 for node in graph_nodes
    })
    critical_set = set(critical_path)
    for node in graph_nodes:
        node["level"] = graph_levels.get(node["id"], 0)
        node["on_critical_path"] = node["id"] in critical_set
        node["duration_ms"] = task_durations.get(node["id"])

    recovery_events = _recovery_events(event_dicts)
    affected_nodes: set[str] = set()
    for event in recovery_events:
        payload = _payload(event)
        for value in payload.get("affected_task_ids", []) or []:
            affected_nodes.add(str(value))

    topology = dict(topology_snapshot)
    topology.setdefault("nodes", [])
    topology.setdefault("edges", [])
    topology["telemetry"] = dict(topology_telemetry)
    # Full version history so the console can replay v1 -> v2 -> node failure ->
    # edge removal -> alternate route -> restored connectivity.  The runtime kept
    # this history internally but never published it, so only the final graph was
    # ever visible.
    history = [dict(item) for item in topology_history]
    topology["history"] = history
    topology["version_count"] = len({item.get("version") for item in history if item.get("version") is not None})
    topology["rebuild_events"] = [
        _payload(event) | {
            "event_id": event.get("event_id"),
            "timestamp": event.get("timestamp", event.get("occurred_at")),
        }
        for event in event_dicts
        if _event_type(event) == "TOPOLOGY_REBUILT"
    ]

    run_metadata = _run_metadata(event_dicts)
    auth = _authenticity(assignments, capability_dicts, event_dicts, run_metadata)
    execution_summary = _execution_semantics(event_dicts)
    messages = [dict(item) for item in (communication_decisions or communication)]
    communication_summary = _communication_summary(messages, topology_telemetry, auth["api_usage"])

    active_agents = [
        item for item in capability_dicts if item.get("kind") == "agent" and item.get("online") is True
    ]
    scheduler_policy_counts = Counter(str(item.get("policy", "unknown")) for item in assignments)
    cost_values = [float(item.get("total_cost", 0.0) or 0.0) for item in assignments]
    ortools_assignments = [item for item in assignments if item.get("policy") == "ortools"]
    ortools_provenance_ok = bool(ortools_assignments) and all(
        isinstance(item.get("solver_provenance"), Mapping)
        and str(item["solver_provenance"].get("engine", "")).startswith("ortools.")
        and item["solver_provenance"].get("status") in {"OPTIMAL", "FEASIBLE"}
        for item in ortools_assignments
    )
    solver_integrity = {
        "verdict": (
            "ORTOOLS_VERIFIED" if ortools_provenance_ok
            else "ORTOOLS_PROVENANCE_INVALID" if ortools_assignments
            else "NON_ORTOOLS_RUN"
        ),
        "ortools_assignment_count": len(ortools_assignments),
        "ortools_provenance_verified": ortools_provenance_ok,
        "rule": "policy=ortools is trusted only with ortools.* solver provenance and OPTIMAL/FEASIBLE status",
    }

    generated_at = time.time()
    snapshot = {
        "schema_version": "mosaic-console-v1",
        "generated_at": generated_at,
        "phase": phase,
        "runtime_metadata": run_metadata,
        "authenticity": auth,
        "execution": execution_summary,
        "data_lineage": _lineage(),
        "state_machine": {
            "normal_transitions": [
                "CREATED->PLANNED", "PLANNED->READY", "READY->RUNNING",
                "RUNNING->VERIFYING", "VERIFYING->SUCCEEDED",
            ],
            "failure": "nonterminal states may enter FAILED through the execution/recovery path",
            "recovery": "TASK_RECOVERED resets RUNNING/VERIFYING/FAILED to READY; TASK_REPLANNED resets affected tasks to PLANNED",
            "source": "execution_scheduler/state_machine.py + EventStore transition/recovery events",
            "display_status_contracts": _display_status_contracts(),
            "transition_contracts": [
                {"state": "CREATED", "event": "TASK_CREATED", "entered_when": "task is materialized into the execution projection", "source": "EventStore"},
                {"state": "PLANNED", "event": "TASK_STATE_CHANGED or TASK_REPLANNED", "entered_when": "orchestrator accepts task into a plan, or replan resets an affected task", "source": "EventStore payload.to=PLANNED"},
                {"state": "READY", "event": "TASK_STATE_CHANGED or TASK_RECOVERED", "entered_when": "all required predecessors/guards are satisfied, or recovery explicitly returns the task to READY", "source": "EventStore payload.to=READY"},
                {"state": "RUNNING", "event": "TASK_STATE_CHANGED", "entered_when": "a committed Assignment is dispatched to its bound execution adapter", "source": "EventStore payload.to=RUNNING"},
                {"state": "VERIFYING", "event": "TASK_STATE_CHANGED", "entered_when": "ToolRuntime returned an execution result/evidence and verifier evaluation begins", "source": "EventStore payload.to=VERIFYING"},
                {"state": "SUCCEEDED", "event": "TASK_STATE_CHANGED", "entered_when": "verifier accepts the result/evidence under task acceptance conditions", "source": "EventStore payload.to=SUCCEEDED"},
                {"state": "FAILED", "event": "TASK_STATE_CHANGED", "entered_when": "execution/verifier/recovery classifies the task as failed and emits the transition", "source": "EventStore payload.to=FAILED"},
                {"state": "PAUSED", "event": "TASK_STATE_CHANGED", "entered_when": "runtime safety/control path explicitly pauses the task", "source": "EventStore payload.to=PAUSED"},
            ],
        },
        "run": {
            "run_id": run_id,
            "status": run_status,
            "task_count": len(task_dicts),
            "succeeded": succeeded,
            "failed": status_counts.get("FAILED", 0),
            "running": status_counts.get("RUNNING", 0) + status_counts.get("VERIFYING", 0),
            "ready": status_counts.get("READY", 0),
            "paused": status_counts.get("PAUSED", 0),
            "all_terminal": all_terminal,
            "success_rate": succeeded / len(task_dicts) if task_dicts else None,
            "e2e_latency_ms": duration_ms,
            "event_count": len(event_dicts),
            "evidence_count": len(evidence),
            "verified_task_count": sum(1 for item in verification_events if bool(item.get("passed", False))),
            "verification_event_count": len(verification_events),
            "message_count": len(messages),
            "active_agent_count": len(active_agents),
        },
        "task_graph": {
            "nodes": graph_nodes,
            "edges": graph_edges,
            "levels": graph_levels,
            "layer_count": (max(graph_levels.values()) + 1) if graph_levels else 0,
            "critical_path": critical_path,
            "critical_path_source": (
                "longest path weighted by measured TASK_PHASE_TIMING total_ms"
                if task_durations else "longest path by node count; no timing measured yet"
            ),
            "status_counts": dict(status_counts),
            "scheduling_state_counts": dict(
                Counter(node["scheduling_state"] for node in graph_nodes)
            ),
            "scheduling_state_vocabulary": list(SCHEDULING_STATES),
        },
        "scheduling": {
            "rounds": round_records,
            "round_count": len(round_records),
            "latest_round": round_records[-1] if round_records else None,
            "task_states": {
                node["id"]: node["scheduling"] for node in graph_nodes
            },
            "blocked_task_ids": [n["id"] for n in graph_nodes if n["scheduling_state"] == "BLOCKED"],
            "queued_task_ids": [n["id"] for n in graph_nodes if n["scheduling_state"] == "QUEUED"],
            "infeasible_task_ids": [
                n["id"] for n in graph_nodes if n["scheduling_state"] == "NO_FEASIBLE_AGENT"
            ],
            "reassigning_task_ids": [
                n["id"] for n in graph_nodes if n["scheduling_state"] == "REASSIGNING"
            ],
            "rule": (
                "BLOCKED means unmet dependencies; QUEUED means feasible candidates exist but no Agent or "
                "resource slot was free this round; NO_FEASIBLE_AGENT means every candidate was eliminated "
                "by a hard constraint. These are never collapsed into a single unassigned label."
            ),
        },
        "performance": _phase_timing_summary(event_dicts),
        "tasks": task_dicts,
        "topology": topology,
        "communication": communication_summary,
        "scheduler": {
            "assignments": assignments,
            "policy_counts": dict(scheduler_policy_counts),
            "assignment_count": len(assignments),
            "average_cost": round(sum(cost_values) / len(cost_values), 6) if cost_values else None,
            "solver_integrity": solver_integrity,
            "capabilities": capability_dicts,
        },
        "memory": _memory_summary(memory_dicts, context_packs, memory_metrics),
        "recovery": {
            "event_count": len(recovery_events),
            "affected_nodes": sorted(affected_nodes),
            "unaffected_node_count": (
                max(0, len(task_dicts) - len(affected_nodes)) if recovery_events else None
            ),
            "timeline": recovery_events,
        },
        "evidence": {
            "items": evidence,
            "verification_results": verification_events,
            "verified_count": sum(1 for item in evidence if item.get("verification_status") == "VERIFIED"),
            "rejected_count": sum(1 for item in evidence if item.get("verification_status") == "REJECTED"),
        },
        "events": event_dicts,
        "traces": trace_summary(event_dicts),
        "metrics": dict(metric_snapshot),
    }
    return snapshot
