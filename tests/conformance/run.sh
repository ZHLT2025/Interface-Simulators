#!/usr/bin/env bash
# Run the conformance suite from a throw-away container against the deployed stack (public IP by default).
# Usage: tests/conformance/run.sh [pytest args]   e.g.  tests/conformance/run.sh -k mom
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
mkdir -p docs/evidence
docker run --rm --env-file .env -e SIM_HOST="${SIM_HOST:-185.189.48.12}" \
  -v "$ROOT/tests:/app/tests:ro" -v "$ROOT/docs/evidence:/evidence" -w /app \
  interface-simulators:local sh -c "pip install -q pytest==8.3.4 >/dev/null 2>&1; python -m pytest tests/unit tests/conformance -v --tb=short -p no:cacheprovider $* 2>&1 | tee /evidence/conformance-last-run.txt"
