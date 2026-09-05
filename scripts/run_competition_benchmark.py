#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from scripts.benchmark_long_horizon import run_long_horizon_benchmark
from scripts.benchmark_topology_ablation import run_topology_ablation_study
from scripts.benchmark_scheduler_ablation import run_scheduler_ablation
from scripts.benchmark_memory_ablation import run_memory_ablation
from mosaic_omega.agent_runtime.split_inference import run_pipeline_split


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--benchmark", choices=("long_horizon", "topology_replay", "scheduler_ablation", "memory_ablation", "split_inference_reference"), required=True)
    p.add_argument("--workspace", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    work = Path(args.workspace).resolve() / "benchmarks" / args.benchmark
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    if args.benchmark == "long_horizon":
        result = run_long_horizon_benchmark(1000, workspace_dir=str(work))
    elif args.benchmark == "topology_replay":
        result = run_topology_ablation_study(workspace_dir=str(work), num_runs=5)
    elif args.benchmark == "scheduler_ablation":
        result = run_scheduler_ablation(workspace_dir=str(work))
    elif args.benchmark == "memory_ablation":
        result = run_memory_ablation(workspace_dir=str(work))
    else:
        result = run_pipeline_split([0.15,-0.2,0.45,0.7,-0.55,0.31,0.08,-0.11])
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"benchmark": args.benchmark, "output": str(out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
