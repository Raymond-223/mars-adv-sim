# -*- coding: utf-8 -*-
"""Controlled scheduler-policy ablation using the production Scheduler and CostModel.

The benchmark compares assignment decisions only.  It does not execute LLM calls or
claim end-to-end task latency.  OR-Tools is never silently replaced with a fallback:
if the package is unavailable, that arm is reported as unavailable.
"""
from __future__ import annotations

import importlib.util
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from mosaic_omega.execution_scheduler.adapters.postgres import MemoryDatabase
from mosaic_omega.execution_scheduler.capability import CapabilityRegistry
from mosaic_omega.execution_scheduler.config import Settings
from mosaic_omega.execution_scheduler.cost_model import CostModel
from mosaic_omega.execution_scheduler.models import ActorKind, CapabilityProfile, TaskNodeView, TaskState
from mosaic_omega.execution_scheduler.scheduler import NoFeasibleAssignment, Scheduler


def _settings(workspace: Path, policy: str) -> Settings:
    return Settings.from_env({
        "EXECUTION_WORKSPACE": str(workspace),
        "SCHEDULER_POLICY": policy,
        "SCHEDULER_ALLOW_FALLBACK": "false",
        "SCHEDULER_WEIGHT_LATENCY": "1.0",
        "SCHEDULER_WEIGHT_TOKEN": "1.0",
        "SCHEDULER_WEIGHT_ENERGY": "1.0",
        "SCHEDULER_WEIGHT_FAILURE": "10.0",
        "SCHEDULER_WEIGHT_MIGRATION": "2.0",
    })


def _registry() -> tuple[CapabilityRegistry, dict[str, CapabilityProfile]]:
    db = MemoryDatabase()
    registry = CapabilityRegistry(db)
    profiles = [
        # Heterogeneous Agents.  Tier metadata is used by the placement explainer.
        CapabilityProfile("agent-device", ActorKind.AGENT, frozenset({"*"}), frozenset({"*"}), frozenset({"*"}), reliability=.97, fixed_cost=.06, latency_ms=18, device_location="device", metadata={"tier":"device"}),
        CapabilityProfile("agent-edge", ActorKind.AGENT, frozenset({"*"}), frozenset({"*"}), frozenset({"*"}), reliability=.99, fixed_cost=.10, latency_ms=34, device_location="edge", metadata={"tier":"edge"}),
        CapabilityProfile("agent-cloud", ActorKind.AGENT, frozenset({"*"}), frozenset({"*"}), frozenset({"*"}), reliability=.995, fixed_cost=.18, latency_ms=82, device_location="cloud", metadata={"tier":"cloud"}),
        CapabilityProfile("model-shared", ActorKind.MODEL, frozenset({"*"}), frozenset({"*"}), frozenset({"*"}), reliability=.995, fixed_cost=.02, cost_per_token=.000002, latency_ms=12, context_limit=1_000_000),
        CapabilityProfile("tool-runtime", ActorKind.TOOL, frozenset({"*"}), frozenset({"*"}), frozenset({"*"}), reliability=.995, fixed_cost=.01, latency_ms=8),
        # Device cost/token and load make placement non-trivial. Two slots each.
        CapabilityProfile("dev-local", ActorKind.DEVICE, frozenset({"*"}), frozenset({"*"}), frozenset({"*"}), reliability=.985, fixed_cost=.04, cost_per_token=.0000005, latency_ms=14, energy_cost=.05, device_location="local", capacity=2, current_load=0.0, metadata={"gpu_count":0,"allowed_privacy_levels":["normal","public","internal","confidential"]}),
        CapabilityProfile("edge-node", ActorKind.DEVICE, frozenset({"*"}), frozenset({"*"}), frozenset({"*"}), reliability=.995, fixed_cost=.08, cost_per_token=.0000010, latency_ms=28, energy_cost=.03, device_location="edge", capacity=2, current_load=0.0, metadata={"gpu_count":1,"allowed_privacy_levels":["normal","public","internal","confidential"]}),
        CapabilityProfile("cloud-node", ActorKind.DEVICE, frozenset({"*"}), frozenset({"*"}), frozenset({"*"}), reliability=.999, fixed_cost=.16, cost_per_token=.0000017, latency_ms=70, energy_cost=.01, device_location="cloud", capacity=2, current_load=0.0, metadata={"gpu_count":1,"allowed_privacy_levels":["normal","public","internal","confidential"]}),
    ]
    for profile in profiles:
        registry.register(profile)
    return registry, {p.actor_id: p for p in profiles}


def _tasks() -> list[TaskNodeView]:
    run_id = "scheduler-ablation-controlled"
    rows = [
        # id, type, priority, tokens, latency, privacy
        ("T1-realtime", "planning", 10, 1_200, 45, "normal"),
        ("T2-private", "analysis", 9, 3_200, 120, "confidential"),
        ("T3-heavy", "reasoning", 8, 38_000, 220, "normal"),
        ("T4-medium", "analysis", 7, 12_000, 140, "internal"),
        ("T5-light", "report", 6, 800, 180, "normal"),
        ("T6-heavy", "reasoning", 5, 54_000, 260, "normal"),
    ]
    return [
        TaskNodeView(
            run_id=run_id,
            task_id=task_id,
            task_type=task_type,
            description=f"controlled scheduler task {task_id}",
            state=TaskState.READY,
            priority=priority,
            required_capabilities=frozenset({"*"}),
            required_permissions=frozenset(),
            privacy_level=privacy,
            estimated_tokens=tokens,
            max_latency_ms=max_latency,
        )
        for task_id, task_type, priority, tokens, max_latency, privacy in rows
    ]


def _ortools_available() -> bool:
    return importlib.util.find_spec("ortools") is not None


def _summarize(policy: str, workspace: Path) -> dict[str, Any]:
    registry, profile_map = _registry()
    settings = _settings(workspace, policy)
    scheduler = Scheduler(registry, CostModel(settings), settings)
    tasks = _tasks()

    if policy == "ortools" and not _ortools_available():
        return {
            "policy": policy,
            "available": False,
            "reason": "Python package 'ortools' is not installed in this runtime; no fallback result is relabeled as OR-Tools.",
            "assignments": [],
        }

    started = time.perf_counter()
    try:
        # Call each concrete policy directly so the OR-Tools arm cannot silently fall back.
        if policy == "ortools":
            assignments = scheduler._ortools(tasks)  # noqa: SLF001 - deliberate benchmark of concrete solver
        elif policy == "greedy":
            assignments = scheduler._greedy(tasks, "greedy")  # noqa: SLF001
        else:
            assignments = scheduler._round_robin(tasks, "round_robin")  # noqa: SLF001
    except (ImportError, NoFeasibleAssignment, RuntimeError, ValueError) as exc:
        return {"policy": policy, "available": False, "reason": f"{type(exc).__name__}: {exc}", "assignments": []}
    scheduler_runtime_ms = (time.perf_counter() - started) * 1000.0

    counts: dict[str, int] = {}
    predicted_latencies: list[float] = []
    rows: list[dict[str, Any]] = []
    for assignment in assignments:
        counts[assignment.resource_id] = counts.get(assignment.resource_id, 0) + 1
        actor_ids = (assignment.agent_id, assignment.model_id, assignment.tool_id, assignment.resource_id)
        predicted = max(profile_map[x].latency_ms for x in actor_ids if x in profile_map)
        predicted_latencies.append(predicted)
        rows.append({
            "task_id": assignment.task_id,
            "agent_id": assignment.agent_id,
            "resource_id": assignment.resource_id,
            "actual_execution_tier": assignment.actual_execution_tier,
            "recommended_tier": assignment.recommended_tier,
            "total_cost": round(float(assignment.total_cost), 6),
            "cost_breakdown": {k: round(float(v), 6) for k, v in assignment.cost_breakdown.items()},
            "reason": assignment.reason,
            "solver_provenance": assignment.solver_provenance,
            "predicted_bundle_latency_ms_model": predicted,
        })

    device_counts = [counts.get(x, 0) for x in ("dev-local", "edge-node", "cloud-node")]
    objective = sum(a.total_cost for a in assignments)
    return {
        "policy": policy,
        "available": True,
        "assignment_count": len(assignments),
        "unassigned_count": len(tasks) - len(assignments),
        "scheduler_runtime_ms_measured": round(scheduler_runtime_ms, 4),
        "scheduler_objective_total_cost": round(objective, 6),
        "scheduler_objective_avg_cost": round(objective / max(1, len(assignments)), 6),
        "device_assignment_counts": dict(sorted(counts.items())),
        "load_balance_stddev_assignments": round(statistics.pstdev(device_counts), 6),
        "predicted_execution_makespan_ms_model": round(max(predicted_latencies, default=0.0), 3),
        "assignments": rows,
    }


def run_scheduler_ablation(workspace_dir: str, output_path: str | None = None) -> dict[str, Any]:
    workspace = Path(workspace_dir).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    modes = {policy: _summarize(policy, workspace / policy) for policy in ("round_robin", "greedy", "ortools")}

    available = [x for x in modes.values() if x.get("available")]
    best = min(available, key=lambda x: x["scheduler_objective_total_cost"]) if available else None
    result = {
        "study_name": "MOSAIC-Ω scheduler controlled ablation",
        "measurement_mode": "production Scheduler + CostModel; assignment-only controlled benchmark",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "controlled_variables": {
            "same_tasks": True,
            "same_capability_registry": True,
            "same_cost_weights": True,
            "same_device_capacities": True,
            "task_count": len(_tasks()),
        },
        "modes": modes,
        "best_available_policy_by_scheduler_objective": best["policy"] if best else None,
        "truth_notes": [
            "scheduler_runtime_ms_measured is wall-clock time spent making assignments, not end-to-end task latency.",
            "predicted_execution_makespan_ms_model is derived from registered profile latency, not a measured task execution time.",
            "No LLM/provider API is called by this benchmark.",
            "OR-Tools is reported unavailable when the dependency is missing; no fallback is presented as an OR-Tools result.",
        ],
    }
    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = __import__("argparse").ArgumentParser(description="Run scheduler policy ablation.")
    parser.add_argument("--workspace", default=".scheduler_ablation_workspace")
    parser.add_argument("--output", default=str(ROOT / "experiments" / "results" / "scheduler_ablation_v1.9.0.json"))
    args = parser.parse_args()
    result = run_scheduler_ablation(args.workspace, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
