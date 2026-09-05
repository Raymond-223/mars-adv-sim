#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from mosaic_omega.integration import MosaicMainChain


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
        choices=("mock", "deepseek"),
        default="mock",
        help="task Agent mode; deepseek never silently falls back to MockAgent",
    )
    parser.add_argument("--no-clean", action="store_true", help="reuse an existing demo workspace")
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    if workspace.exists() and not args.no_clean:
        shutil.rmtree(workspace)
    chain = MosaicMainChain(workspace=workspace)
    result = chain.run(
        args.goal,
        run_id=args.run_id,
        goalspec_mode=args.goalspec_mode,
        agent_mode=args.agent_mode,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "run_id": result.run_id,
        "goalspec_mode": args.goalspec_mode,
        "agent_mode": args.agent_mode,
        "all_succeeded": result.all_succeeded,
        "task_count": len(result.tasks),
        "event_count": len(result.events),
        "evidence_count": len(result.evidence_manifest),
        "verification_count": len(result.verification_results),
        "message_count": len(result.communication),
        "context_pack_count": len(result.context_packs),
        "scheduler_policies": result.scheduler_policies,
        "result_file": str(output),
    }, ensure_ascii=False, indent=2))
    return 0 if result.all_succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
