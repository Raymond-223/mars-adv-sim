from __future__ import annotations

import json
from pathlib import Path

from scenarios.ros_repair.runner import run_scenario


def main() -> int:
    workspace = Path(".mosaic_workspace/ros_repair_demo").resolve()
    result = run_scenario(workspace)
    out = workspace / "run_result.json"
    out.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = workspace / "evidence_manifest.json"
    manifest.write_text(
        json.dumps(result.evidence_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "run_id": result.run_id,
        "all_succeeded": result.all_succeeded,
        "completed": result.completed_task_ids,
        "evidence_count": len(result.evidence_manifest),
        "evidence_manifest": str(manifest),
        "result": str(out),
    }, ensure_ascii=False, indent=2))
    return 0 if result.all_succeeded else 2


if __name__ == "__main__":
    raise SystemExit(main())
