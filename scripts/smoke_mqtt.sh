#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
[ -f .env ] || cp .env.example .env
compose() {
  docker compose --env-file .env -f deploy/docker-compose.yml --profile mqtt-smoke "$@"
}
cleanup() {
  compose down -v --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM
compose up --build --abort-on-container-exit --exit-code-from mqtt-smoke mqtt-smoke
