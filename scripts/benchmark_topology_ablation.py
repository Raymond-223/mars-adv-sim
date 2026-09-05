# -*- coding: utf-8 -*-
"""Replay-based topology communication ablation.

This script avoids pretending that Full Mesh / Static Star were three separate
physical network executions. It runs the MOSAIC sparse chain, captures real
message envelopes, then replays the same message set through explicit routing
fan-out models and reports transmission/byte costs as replay-derived metrics.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from mosaic_omega.integration import MosaicMainChain
from scripts.benchmark_support import run_deterministic_benchmark_chain


def _size(message: dict[str, Any]) -> int:
    return len(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _replay_cost(messages: list[dict[str, Any]], agent_count: int, mode: str) -> dict[str, Any]:
    if mode == "mosaic_sparse":
        factors = [1 for _ in messages]
    elif mode == "static_star":
        # sender -> coordinator -> receiver; coordinator-endpoint cases are conservatively one hop.
        factors = [1 if m.get("sender") == "coordinator" or m.get("receiver") == "coordinator" else 2 for m in messages]
    elif mode == "full_mesh":
        factors = [max(1, agent_count - 1) for _ in messages]
    else:
        raise ValueError(mode)

    transmissions = sum(factors)
    byte_volume = sum(_size(message) * factor for message, factor in zip(messages, factors))
    # Approximation only; not model-provider token usage.
    token_equivalent = round(byte_volume / 4)
    return {
        "transmissions_replay": transmissions,
        "message_bytes_replay": byte_volume,
        "estimated_token_equivalent": token_equivalent,
        "token_metric_is_estimate": True,
    }


def run_topology_ablation_study(
    workspace_dir: str = ".ablation_workspace",
    num_runs: int = 5,
    output_path: str | None = None,
) -> dict[str, Any]:
    print("=== MOSAIC-Ω topology replay ablation ===")
    workspace = Path(workspace_dir).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    goal = "编排并执行复杂多机器人分布式协作巡检任务，包含路径规划、障碍物判断、传感器校准与终态确认。"

    aggregate_messages: list[dict[str, Any]] = []
    success_count = 0
    latencies: list[float] = []
    agent_ids: set[str] = set()

    for index in range(1, num_runs + 1):
        chain = MosaicMainChain(workspace=workspace / f"run-{index}", scheduler_policy="greedy")
        t0 = time.perf_counter()
        result = run_deterministic_benchmark_chain(chain, goal, run_id=f"ablation-source-{index}")
        latencies.append((time.perf_counter() - t0) * 1000)
        success_count += int(result.all_succeeded)
        aggregate_messages.extend(result.communication)
        agent_ids.update(
            str((task.get("assignment") or {}).get("agent_id"))
            for task in result.tasks
            if (task.get("assignment") or {}).get("agent_id")
        )

    agent_count = max(2, len(agent_ids))
    modes = {
        mode: _replay_cost(aggregate_messages, agent_count, mode)
        for mode in ("full_mesh", "static_star", "mosaic_sparse")
    }
    full_tx = max(1, modes["full_mesh"]["transmissions_replay"])
    full_bytes = max(1, modes["full_mesh"]["message_bytes_replay"])
    for mode, item in modes.items():
        item["transmission_reduction_vs_full_mesh_pct"] = round(
            (full_tx - item["transmissions_replay"]) / full_tx * 100, 2
        )
        item["byte_reduction_vs_full_mesh_pct"] = round(
            (full_bytes - item["message_bytes_replay"]) / full_bytes * 100, 2
        )

    summary = {
        "study_name": "MOSAIC-Ω topology communication replay ablation",
        "measurement_mode": "same-message-set routing replay",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_runtime": {
            "runs": num_runs,
            "success_rate_pct_measured": round(success_count / max(1, num_runs) * 100, 2),
            "avg_latency_ms_measured": round(sum(latencies) / max(1, len(latencies)), 2),
            "captured_messages_measured": len(aggregate_messages),
            "active_agent_count_measured": len(agent_ids),
        },
        "modes": modes,
        "provenance_note": (
            "mosaic_sparse is the captured runtime message set; full_mesh/static_star are replay fan-out costs, "
            "not independent physical-network executions"
        ),
    }

    out = Path(output_path) if output_path else ROOT / "experiments" / "results" / "topology_ablation_v2_replay.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Saved: {out}")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the replay-based topology communication ablation.")
    parser.add_argument("--workspace", default=".ablation_workspace")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--output", default=str(ROOT / "experiments" / "results" / "topology_ablation_v2_replay.json"))
    args = parser.parse_args()
    run_topology_ablation_study(
        workspace_dir=args.workspace,
        num_runs=args.runs,
        output_path=args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
