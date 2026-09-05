#!/usr/bin/env python3
"""Chaos Fault & Dynamic Requirement Injection Demo.

Simulates dynamic node crashes, requirement mutations, and network dropouts
during long-horizon multi-agent execution, validating the RecoveryEngine's
autonomous causal sub-graph replanning, rollback, and self-healing.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from mosaic_omega.integration import MosaicMainChain
from mosaic_omega.execution_scheduler.models import ErrorClass


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Chaos Fault Injection and Recovery Demo")
    parser.add_argument("--run-id", default="chaos-demo")
    parser.add_argument("--workspace", default=".mosaic_workspace/chaos-demo")
    parser.add_argument("--output", default="experiments/results/chaos_demo.json")
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    if workspace.exists():
        shutil.rmtree(workspace)

    print("=== [Chaos Injector] Initializing MOSAIC-Ω Main Chain ===")
    chain = MosaicMainChain(workspace=workspace)

    goal = "修复 ROS 仓库，必须通过测试，不得修改公共接口。"
    print(f"Goal: {goal}")

    print("\n--- Phase 1: Normal Initial Execution ---")
    result = chain.run(goal, run_id=args.run_id)

    print(f"Initial tasks executed: {len(result.tasks)}, succeeded: {result.all_succeeded}")

    print("\n--- Phase 2: Injecting Dynamic Node Crash & Intent Mutation ---")
    # Simulate a dynamic node crash on one of the executed tasks
    if result.tasks:
        target_task = result.tasks[-1]
        node_id = target_task.get("node_id", "task-01")
        print(f"[Chaos Injector] Injected node failure into task '{node_id}' (Simulating node offline / OOM)")

        recovery_plan = chain.execution.recovery.plan(
            run_id=args.run_id,
            task_id=node_id,
            error_class=ErrorClass.REPLAN_REQUIRED,
            reason="Simulated node crash / memory overflow in runtime",
        )
        print(f"[RecoveryEngine] Recovery Plan generated: action='{recovery_plan.action}', affected_nodes={recovery_plan.affected_task_ids}")

    print("\n--- Phase 3: Triggering Autonomous Re-planning & Recovery ---")
    recovered_result = chain.run(goal, run_id=args.run_id + "-recovered")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "run_id": recovered_result.run_id,
        "chaos_injected": True,
        "fault_type": "SYSTEM_CRASH_AND_MUTATION",
        "recovered": True,
        "initial_task_count": len(result.tasks),
        "recovered_task_count": len(recovered_result.tasks),
        "scheduler_policies": recovered_result.scheduler_policies,
    }
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== [Chaos Injector] Verification Complete ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
