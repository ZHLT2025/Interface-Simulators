#!/usr/bin/env bash
# Rebuild image and recreate only the given services (default: all python simulators).
set -euo pipefail
cd "$(dirname "$0")/.."
SVCS=("$@"); [[ ${#SVCS[@]} -eq 0 ]] && SVCS=(mom iot esb cv)
docker compose build "${SVCS[@]}"
docker compose up -d "${SVCS[@]}"
docker compose ps
