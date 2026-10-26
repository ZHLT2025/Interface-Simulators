#!/usr/bin/env bash
# Build and start the whole simulator stack. Safe to re-run (recreates containers with new env/config).
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] || ./deploy/init-env.sh
docker compose build
docker compose up -d
docker compose ps
