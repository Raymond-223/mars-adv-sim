#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
from mosaic_omega.agent_runtime.split_inference import run_pipeline_split

def main() -> int:
    p=argparse.ArgumentParser(); p.add_argument("--output", required=True); args=p.parse_args()
    vector=[0.15,-0.2,0.45,0.7,-0.55,0.31,0.08,-0.11]
    result=run_pipeline_split(vector)
    out=Path(args.output); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"benchmark":"split_inference_reference","verified_equivalent":result["verified_equivalent"],"output":out.name},ensure_ascii=False))
    return 0 if result["verified_equivalent"] and result["process_boundary_verified"] else 1
if __name__ == "__main__": raise SystemExit(main())
