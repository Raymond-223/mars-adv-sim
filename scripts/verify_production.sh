#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
./scripts/smoke_production.sh
./scripts/smoke_mqtt.sh
