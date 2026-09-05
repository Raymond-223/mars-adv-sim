#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from mosaic_omega.integration import MosaicMainChain
from mosaic_omega.integration.live_faults import LiveFaultController


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the authoritative MOSAIC-Ω V3 main chain")
    parser.add_argument(
        "goal",
        nargs="?",
        default="修复 ROS 仓库，必须通过测试，不得修改公共接口。",
    )
    parser.add_argument("--run-id", default="mainchain-demo")
    parser.add_argument("--workspace", default=".mosaic_workspace/mainchain-demo")
    parser.add_argument("--output", default="experiments/results/mainchain_demo.json")
    parser.add_argument(
        "--goalspec-mode",
        choices=("rule", "deepseek", "auto"),
        default="rule",
        help="GoalSpec extraction mode; use deepseek for strict real-API evidence",
    )
    parser.add_argument(
        "--agent-mode",
        choices=("deepseek", "preconfigured"),
        default="deepseek",
        help="task Agent mode; production CLI contains no mock mode",
    )
    parser.add_argument(
        "--scheduler-policy",
        choices=("greedy", "ortools"),
        default="greedy",
        help="execution scheduler policy; competition control plane always selects ortools",
    )
    parser.add_argument("--no-clean", action="store_true", help="reuse an existing demo workspace")
    parser.add_argument("--live-control", action="store_true", help="consume Console fault-injection mailbox at execution round boundaries")
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    if workspace.exists() and not args.no_clean:
        shutil.rmtree(workspace)
    chain = MosaicMainChain(workspace=workspace, scheduler_policy=args.scheduler_policy)
    live = LiveFaultController(workspace) if args.live_control else None
    result = chain.run(
        args.goal,
        run_id=args.run_id,
        goalspec_mode=args.goalspec_mode,
        agent_mode=args.agent_mode,
        round_hook=live.round_hook if live else None,
    )
    final_result = result
    transition = None
    if live and live.requirement_change:
        changed_goal = args.goal + " " + live.requirement_change
        changed_run_id = args.run_id + "-requirement-change"
        final_result = chain.run(
            changed_goal,
            run_id=changed_run_id,
            goalspec_mode=args.goalspec_mode,
            agent_mode=args.agent_mode,
        )
        transition = {
            "type": "requirement_change_recompile",
            "original_run_id": result.run_id,
            "new_run_id": final_result.run_id,
            "new_requirement": live.requirement_change,
            "mailbox_requests": live.applied_requests,
        }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = final_result.to_dict()
    if live:
        payload["live_control"] = {
            "enabled": True,
            "mailbox_requests": live.applied_requests,
            "transition": transition,
        }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "run_id": final_result.run_id,
        "original_run_id": result.run_id,
        "live_control": bool(live),
        "live_injections_applied": len(live.applied_requests) if live else 0,
        "requirement_change_transition": transition,
        "goalspec_mode": args.goalspec_mode,
        "agent_mode": args.agent_mode,
        "scheduler_policy_requested": args.scheduler_policy,
        "all_succeeded": final_result.all_succeeded,
        "task_count": len(final_result.tasks),
        "event_count": len(final_result.events),
        "evidence_count": len(final_result.evidence_manifest),
        "verification_count": len(final_result.verification_results),
        "message_count": len(final_result.communication),
        "context_pack_count": len(final_result.context_packs),
        "scheduler_policies": final_result.scheduler_policies,
        "result_file": str(output),
    }, ensure_ascii=False, indent=2))
    return 0 if final_result.all_succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
