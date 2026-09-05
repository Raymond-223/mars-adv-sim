#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec python -X utf8 apps/console/main.py "$@"
