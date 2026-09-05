# -*- coding: utf-8 -*-
"""Single-run long-horizon benchmark for XH-202631.

This benchmark intentionally separates two claims:

* the *runtime scale* claim is measured from one authoritative EventStore run;
* the executor is an offline deterministic benchmark executor, never presented
  as a real LLM Agent and never reported as provider/API billing token usage.

The run contains a staged 64-task DAG (16 stages × 4 lanes).  It traverses the
same scheduler, ToolRuntime, verifier, EventStore, memory, topology and recovery
services as production.  After the initial successful completion, one real
Evidence invalidation is injected into the same run and its affected execution
closure is re-executed through RecoveryEngine.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

try:
    import psutil
except ImportError:  # optional measurement only
    psutil = None  # type: ignore

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from mosaic_omega.execution_scheduler.models import TaskState
from mosaic_omega.goalspec import compile_goal
from mosaic_omega.integration import MosaicMainChain
from scripts.benchmark_support import register_deterministic_benchmark_resources


def _build_monolithic_plan(*, stages: int = 16, lanes: int = 4) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    if stages < 1 or lanes < 1:
        raise ValueError("stages and lanes must be positive")

    goal_text = (
        "完成一个超长程复杂群体任务：持续维护全局目标与约束，在多阶段规划、分析、执行、"
        "验证和审查之间进行异构协作，所有节点必须产生可验收证据。"
    )
    goalspec = compile_goal(goal_text, mode="rule")
    skills = ("planning", "analysis", "execution", "verification", "review")
    plan: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []

    for stage in range(1, stages + 1):
        for lane in range(1, lanes + 1):
            task_id = f"long_s{stage:02d}_l{lane:02d}"
            skill = skills[(stage + lane - 2) % len(skills)]
            dependency = f"long_s{stage - 1:02d}_l{lane:02d}" if stage > 1 else None
            acceptance = f"file_exists:.mosaic_deliverables/bench-long-monolithic-001/{task_id}.md"
            node = {
                "task_id": task_id,
                "task_type": skill,
                "description": (
                    f"阶段 {stage}/{stages}，通道 {lane}/{lanes}：执行 {skill} 子任务；"
                    f"保持全局目标、上游证据与当前约束连续，并产出可验证结果。"
                ),
                "depends_on": [dependency] if dependency else [],
                "priority": max(1, 10 - stage // 2),
                "required_skills": [skill],
                "required_permissions": ["*"],
                "acceptance_conditions": [acceptance],
                "estimated_tokens": 120,
                "metadata": {
                    "benchmark_stage": stage,
                    "benchmark_lane": lane,
                    "tool": {"name": "task", "arguments": {}},
                    "measurement_contract": "deterministic_offline_executor_not_real_llm",
                },
            }
            plan.append(node)
            if dependency:
                edges.append({"source": dependency, "target": task_id})

    taskgraph = {
        "revision": 1,
        "status": "ready",
        "benchmark_generated": True,
        "nodes": plan,
        "edges": edges,
    }
    return goalspec, taskgraph, plan


def _resume_after_recovery(chain: MosaicMainChain, run_id: str, max_rounds: int = 200) -> bool:
    for _ in range(max_rounds):
        ready = chain.execution.events.tasks(run_id, TaskState.READY)
        if not ready:
            break
        chain.execution.run_once(run_id)
        chain._sync_new_events_to_memory(run_id)  # noqa: SLF001 - benchmark integration hook
        chain._sync_topology(run_id)  # noqa: SLF001
        if not chain.execution.orchestrator.last_round_made_progress:
            break
    tasks = chain.execution.events.tasks(run_id)
    succeeded = bool(tasks) and all(task.state is TaskState.SUCCEEDED for task in tasks)
    # Recovery re-execution uses the same runtime but sits outside ``run_plan``.
    # Publish one final authoritative projection so the user/judge UI cannot be
    # left on an intermediate recovery percentage after every task has succeeded.
    chain._sync_new_events_to_memory(run_id)  # noqa: SLF001
    chain._sync_topology(run_id)  # noqa: SLF001
    chain._observe(run_id, "recovery_complete" if succeeded else "recovery_blocked", force=True)  # noqa: SLF001
    return succeeded


def run_long_horizon_benchmark(
    target_event_count: int = 1000,
    workspace_dir: str = ".benchmark_workspace",
    *,
    stages: int = 16,
    lanes: int = 4,
    inject_evidence_fault: bool = True,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Execute one authoritative long-horizon run and report measured facts."""
    print(
        f"=== [MOSAIC-Ω Single-Run Long-Horizon Benchmark] target events >= {target_event_count}; "
        f"DAG={stages}x{lanes} ==="
    )
    process = psutil.Process(os.getpid()) if psutil is not None else None
    start_memory_mb = process.memory_info().rss / (1024 * 1024) if process else None
    started = time.perf_counter()

    workspace = Path(workspace_dir).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    run_id = "bench-long-monolithic-001"
    goalspec, taskgraph, plan = _build_monolithic_plan(stages=stages, lanes=lanes)
    required_skills = [str(skill) for node in plan for skill in node.get("required_skills", ())]

    chain = MosaicMainChain(workspace=workspace, scheduler_policy="greedy")
    register_deterministic_benchmark_resources(chain, required_skills)
    initial = chain.run_plan(
        goalspec=goalspec,
        taskgraph=taskgraph,
        plan=plan,
        run_id=run_id,
        max_rounds=max(stages + 10, 100),
        auto_register_deepseek_resources=False,
        runtime_metadata={
            "goalspec_mode": "rule",
            "agent_mode": "deterministic_tool_executor",
            "competition_real_api": False,
            "measurement_purpose": "single-run long-horizon infrastructure benchmark",
            "single_monolithic_run": True,
        },
    )

    pre_fault_events = len(chain.execution.events.events(run_id=run_id))
    fault_injected = False
    recovered = None
    invalidated_evidence_id = None
    affected_task_ids: list[str] = []
    if inject_evidence_fault and initial.evidence_manifest:
        # Pick a middle-stage evidence item so recovery has a non-trivial but bounded closure.
        target_index = min(len(initial.evidence_manifest) - 1, max(0, len(initial.evidence_manifest) // 2))
        target = initial.evidence_manifest[target_index]
        invalidated_evidence_id = str(target["evidence_id"])
        recovery = chain.invalidate_evidence(
            run_id,
            invalidated_evidence_id,
            reason="single-run benchmark evidence-integrity fault",
        )
        affected_task_ids = [str(x) for x in recovery.get("affected_task_ids", ())]
        fault_injected = True
        recovered = _resume_after_recovery(chain, run_id)

    tasks = chain.execution.events.tasks(run_id)
    events = chain.execution.events.events(run_id=run_id)
    communication = [m for m in chain.communication_log if m.get("run_id") == run_id]
    message_bytes = sum(
        len(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        for message in communication
    )
    # Explicitly an estimate for the deterministic benchmark; never API provider usage.
    estimated_token_equivalent = sum(max(0, task.estimated_tokens) for task in tasks) + len(events) * 35
    all_succeeded = bool(tasks) and all(task.state is TaskState.SUCCEEDED for task in tasks)

    duration_s = time.perf_counter() - started
    end_memory_mb = process.memory_info().rss / (1024 * 1024) if process else None
    storage_bytes = sum(f.stat().st_size for f in workspace.glob("**/*") if f.is_file())

    summary = {
        "benchmark_name": "MOSAIC-Ω single-run long-horizon measured benchmark",
        "measurement_mode": "single_authoritative_eventstore_run_with_deterministic_executor_and_evidence_recovery",
        "single_monolithic_run_claimed": True,
        "executor_truth_class": "DETERMINISTIC_TOOL_EXECUTOR",
        "competition_real_api": False,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "run_id": run_id,
        "dag_shape": {"stages": stages, "lanes": lanes},
        "target_event_count": target_event_count,
        "pre_fault_event_count_measured": pre_fault_events,
        "achieved_event_count_measured": len(events),
        "target_reached": len(events) >= target_event_count,
        "total_task_count_measured": len(tasks),
        "completed_task_count_measured": sum(task.state is TaskState.SUCCEEDED for task in tasks),
        "all_succeeded_measured": all_succeeded,
        "fault_injection": {
            "type": "evidence_invalidation" if fault_injected else None,
            "injected_count_measured": 1 if fault_injected else 0,
            "recovered_count_measured": 1 if recovered else 0,
            "recovered_by_reexecution": recovered,
            "invalidated_evidence_id": invalidated_evidence_id,
            "affected_task_ids": affected_task_ids,
        },
        "total_messages_measured": len(communication),
        "total_message_volume_kb_measured": round(message_bytes / 1024, 2),
        "estimated_token_equivalent": estimated_token_equivalent,
        "token_metric_is_estimate": True,
        "token_metric_source": "sum(TaskNodeView.estimated_tokens) + 35 * measured EventStore event count",
        "total_duration_s_measured": round(duration_s, 3),
        "avg_event_latency_ms_derived": round(duration_s * 1000 / max(1, len(events)), 2),
        "start_memory_mb_measured": round(start_memory_mb, 2) if start_memory_mb is not None else None,
        "end_memory_mb_measured": round(end_memory_mb, 2) if end_memory_mb is not None else None,
        "total_storage_mb_measured": round(storage_bytes / (1024 * 1024), 2),
        "provenance_note": (
            "All event/task/message counts come from one authoritative run_id. "
            "The executor is deterministic and is not evidence of a real LLM/API call."
        ),
    }

    result_file = Path(output_path) if output_path else ROOT / "experiments" / "results" / "benchmark_1000_events_v3_monolithic.json"
    result_file.parent.mkdir(parents=True, exist_ok=True)
    result_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("\n=== Summary ===")
    print(f"Run ID               : {run_id}")
    print(f"Measured events      : {len(events)} (target reached={len(events) >= target_event_count})")
    print(f"Measured tasks       : {len(tasks)} / succeeded={summary['completed_task_count_measured']}")
    print(f"Evidence recovery    : injected={fault_injected} recovered={recovered}")
    print(f"Measured messages    : {len(communication)}")
    print(f"Estimated token eq.  : {estimated_token_equivalent} [ESTIMATE, not API billing tokens]")
    print(f"Saved                : {result_file}")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the single-authority long-horizon benchmark.")
    parser.add_argument("--target-events", type=int, default=1000)
    parser.add_argument("--workspace", default=".benchmark_workspace")
    parser.add_argument("--stages", type=int, default=16)
    parser.add_argument("--lanes", type=int, default=4)
    parser.add_argument("--no-evidence-fault", action="store_true")
    parser.add_argument("--output", default=str(ROOT / "experiments" / "results" / "benchmark_1000_events_v3_monolithic.json"))
    args = parser.parse_args()
    run_long_horizon_benchmark(
        args.target_events,
        workspace_dir=args.workspace,
        stages=args.stages,
        lanes=args.lanes,
        inject_evidence_fault=not args.no_evidence_fault,
        output_path=args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
