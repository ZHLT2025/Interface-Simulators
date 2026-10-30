#!/usr/bin/env bash
# Run the manual-single-round control tests in a throw-away container with NO network at all.
#
#   tests/control/run-isolated.sh [pytest args...]
#
# Properties of this runner (E02 SIM 隔离技术测试):
#   * --network none  : only loopback exists inside the container, so the suite physically cannot
#                       reach a deployed system; the stand-in CV target binds 127.0.0.1 itself.
#   * the image under test is built from this worktree and tagged interface-simulators-5w:<shortsha>.
#   * the container is named e02s-* and is removed on exit; no existing container, network or host
#     port is touched.
#
# Environment:
#   IMAGE       image to run (default: interface-simulators-5w:<git short sha of HEAD>)
#   PYTEST_LIB  directory containing a pure-python pytest (pytest/_pytest/pluggy/iniconfig/
#               packaging/pygments/py.py). The runtime image deliberately does not ship pytest;
#               in an offline environment stage it from a host interpreter, e.g.
#                 for m in pytest _pytest pluggy iniconfig packaging pygments; do
#                   cp -r "$(python3 -c 'import sysconfig;print(sysconfig.get_paths()["purelib"])')/$m" "$PYTEST_LIB/"
#                 done
#   V11_CV_DIR  optional: host path of the V1.1 package production/simulator_inputs/cv directory.
#               Mounted read-only; enables the formal 318-event / 62-stop-point plan tests.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SHA="$(git -C "$ROOT" rev-parse --short HEAD)"
IMAGE="${IMAGE:-interface-simulators-5w:$SHA}"
PYTEST_LIB="${PYTEST_LIB:?set PYTEST_LIB to a directory containing a pure-python pytest}"

args=(--rm --name "e02s-tests-$$" --network none
      -v "$ROOT:/app" -w /app
      -v "$PYTEST_LIB:/pytestlib:ro" -e PYTHONPATH=/pytestlib:/app)
if [ -n "${V11_CV_DIR:-}" ]; then
  args+=(-v "$V11_CV_DIR:/pkg-cv:ro" -e V11_CV_DIR=/pkg-cv)
fi
exec docker run "${args[@]}" "$IMAGE" \
  python -m pytest tests/unit tests/control -v --tb=short -p no:cacheprovider "$@"
