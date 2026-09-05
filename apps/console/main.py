#!/usr/bin/env python3
"""Launch the interactive MOSAIC-Ω competition console."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
for p in (PROJECT_ROOT, PROJECT_ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def main() -> int:
    parser = argparse.ArgumentParser(description="MOSAIC-Ω interactive competition console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--workspace", default=".mosaic_workspace/competition-console")
    parser.add_argument("--snapshot-dir", default=None)
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    snapshot_dir = Path(args.snapshot_dir).resolve() if args.snapshot_dir else workspace / "observability"
    frontend_dir = Path(__file__).resolve().parent / "frontend"

    from backend.server import serve

    serve(
        args.host,
        args.port,
        snapshot_dir=snapshot_dir,
        frontend_dir=frontend_dir,
        project_root=PROJECT_ROOT,
        workspace=workspace,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
