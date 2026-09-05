"""Worker process for the reference split-inference cloud stage."""
from __future__ import annotations
import json
import sys
import time
from .split_inference import cloud_stage

def main() -> int:
    raw = sys.stdin.buffer.read()
    payload = json.loads(raw.decode("utf-8"))
    started = time.perf_counter()
    output = cloud_stage(payload["activation"])
    elapsed = (time.perf_counter() - started) * 1000.0
    sys.stdout.write(json.dumps({"output": output, "cloud_compute_ms": elapsed, "process_stage": True}, separators=(",", ":")))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
