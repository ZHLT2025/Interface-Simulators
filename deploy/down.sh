#!/usr/bin/env bash
# Stop and remove the simulator containers (datasets, .env and state/ are kept).
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose down
