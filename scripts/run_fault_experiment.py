#!/usr/bin/env python3
"""Standalone, reproducible competition fault experiment.

This path is used only when the Console has no active custom/scenario Run.
The same authoritative live-fault implementation used by the mailbox control
path is applied at a MainChain round boundary. UI code never mutates runtime
state.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from mosaic_omega.integration import MosaicMainChain
from mosaic_omega.integration.live_faults import LiveFaultOutcome, apply_live_fault


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--fault", choices=("agent_offline", "tool_failure", "requirement_change", "evidence_invalidation"), required=True)
    p.add_argument("--workspace", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--requirement", default="新增约束：最终结果必须包含风险说明与证据引用。")
    args = p.parse_args()

    workspace = Path(args.workspace).resolve()
    chain = MosaicMainChain(workspace=workspace, scheduler_policy="ortools")
    base_goal = "完成一个可验证的软件工程长程任务；必须给出证据并通过验收。"
    request = {
        "request_id": f"standalone-{uuid.uuid4().hex[:12]}",
        "run_id": args.run_id,
        "fault": args.fault,
        "requirement": args.requirement if args.fault == "requirement_change" else None,
        "requested_at": time.time(),
        "requested_by": "competition-console-standalone-experiment",
        "state": "PENDING",
    }
    injected: dict[str, Any] = {
        "done": False,
        "events_before_fault": None,
        "outcome": None,
        "error": None,
    }

    def inject_between_rounds(runtime: MosaicMainChain, run_id: str, round_index: int) -> bool | None:
        if injected["done"]:
            return None
        injected["done"] = True
        injected["events_before_fault"] = len(runtime.execution.events.events(run_id=run_id))
        try:
            outcome: LiveFaultOutcome = apply_live_fault(runtime, run_id, round_index, request)
        except Exception as exc:
            injected["error"] = f"{type(exc).__name__}: {exc}"
            raise
        injected["outcome"] = {
            "request_id": outcome.request_id,
            "fault": outcome.fault,
            "applied": outcome.applied,
            "continue_run": outcome.continue_run,
            "requirement_change": outcome.requirement_change,
            "detail": outcome.detail or {},
            "round_index": round_index,
        }
        return outcome.continue_run

    result = chain.run(
        base_goal,
        run_id=args.run_id,
        goalspec_mode="deepseek",
        agent_mode="deepseek",
        round_hook=inject_between_rounds,
    )

    final_run_id = args.run_id
    final_success = result.all_succeeded
    changed_result = None
    outcome = injected.get("outcome") or {}
    if outcome.get("requirement_change"):
        changed_goal = base_goal + " " + str(outcome["requirement_change"])
        changed_id = args.run_id + "-requirement-change"
        changed_result = chain.run(
            changed_goal,
            run_id=changed_id,
            goalspec_mode="deepseek",
            agent_mode="deepseek",
        )
        final_run_id = changed_id
        final_success = changed_result.all_succeeded
        outcome.setdefault("detail", {}).update({
            "changed_goal": changed_goal,
            "new_run_id": changed_id,
            "new_run_succeeded": changed_result.all_succeeded,
            "new_task_count": len(changed_result.tasks),
        })

    original_events = chain.execution.events.events(run_id=args.run_id)
    final_tasks = chain.execution.events.tasks(final_run_id)
    event_types = [event.event_type for event in original_events]
    summary = {
        "run_id": args.run_id,
        "final_run_id": final_run_id,
        "fault": args.fault,
        "injection_mode": "standalone_round_boundary_same_authoritative_fault_implementation",
        "injected_mid_run": bool(injected["done"]),
        "events_before_fault": injected["events_before_fault"],
        "events_after_fault_original_run": len(original_events),
        "fault_event_recorded": "FAULT_INJECTED" in event_types,
        "recovery_event_counts": {
            "RECOVERY_PLANNED": event_types.count("RECOVERY_PLANNED"),
            "TASK_RECOVERED": event_types.count("TASK_RECOVERED"),
            "TASK_REPLANNED": event_types.count("TASK_REPLANNED"),
        },
        "original_run_succeeded": result.all_succeeded,
        "final_success": bool(final_success),
        "final_states": {task.task_id: task.state.value for task in final_tasks},
        "outcome": outcome,
        "error": injected["error"],
        "truth_note": (
            "tool_failure is a controlled ToolRuntime-boundary failure; agent_offline is a real registry-offline action; "
            "requirement_change recompiles a new run; evidence_invalidation uses actual emitted evidence"
        ),
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if final_success and summary["fault_event_recorded"] and bool(outcome.get("applied")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
