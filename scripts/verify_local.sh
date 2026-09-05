#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
export PYTHONPATH="${PYTHONPATH:-}:$ROOT/src:$ROOT"
python -m compileall -q src apps scenarios scripts tests
pytest -q
python scripts/demo_main_chain.py
python scripts/demo_ros_repair.py
