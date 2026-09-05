#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from scenarios.financial_research.runner import run_scenario as run_financial
from scenarios.ros_repair.runner import run_scenario as run_ros
from mosaic_omega.integration.live_faults import LiveFaultController


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a competition scenario through the authoritative main chain")
    parser.add_argument("--scenario", choices=("ros_repair", "financial_research"), required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--agent-mode", choices=("deepseek", "tool"), default="deepseek")
    parser.add_argument("--live-control", action="store_true", help="consume Console live fault mailbox between execution rounds")
    args = parser.parse_args()

    runner = run_ros if args.scenario == "ros_repair" else run_financial
    live = LiveFaultController(args.workspace) if args.live_control else None
    result = runner(
        args.workspace, run_id=args.run_id, agent_mode=args.agent_mode,
        round_hook=live.round_hook if live else None,
    )
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "scenario": args.scenario,
        "run_id": result.run_id,
        "agent_mode": args.agent_mode,
        "live_control": bool(live),
        "live_injections_applied": len(live.applied_requests) if live else 0,
        "all_succeeded": result.all_succeeded,
        "task_count": len(result.tasks),
        "event_count": len(result.events),
        "evidence_count": len(result.evidence_manifest),
        "scheduler_policies": result.scheduler_policies,
        "output": str(out),
    }, ensure_ascii=False, indent=2))
    return 0 if result.all_succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
