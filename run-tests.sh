#!/usr/bin/env bash
# Run the test-suite with the Python of a Hermes install (it already has `websockets`).
# Usage: HERMES_PYTHON=/path/to/hermes/venv/bin/python PYTEST_TARGET=/path/with/pytest ./run-tests.sh [pytest args]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
py="${HERMES_PYTHON:-python3}"
if [ -n "${PYTEST_TARGET:-}" ]; then export PYTHONPATH="$PYTEST_TARGET${PYTHONPATH:+:$PYTHONPATH}"; fi
cd "$here"
exec "$py" -m pytest -q "$@"
