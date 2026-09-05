#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
[ -f .env ] || cp .env.example .env
docker compose --env-file .env -f deploy/docker-compose.yml up -d --wait postgres redis mqtt
