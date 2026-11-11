#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

# One entry point for every quality gate. Exit status is non-zero if any gate fails.
#
#   PY=/path/to/python          interpreter with the `dev` group of tools/pyproject.toml (uv sync --project tools --group dev; default: python3)
#   REACT_DIR=/path/node_modules  folder with react + react-dom for the desktop test (the gate is skipped, loudly, if unset)
#   HERMES_AGENT_DIR=/path/hermes-agent  a Hermes checkout whose venv runs `plugins validate`/`doctor` (skipped, loudly, if unset)
#
# Everything runs against a throw-away HERMES_HOME: no gate can touch a real Hermes install.
set -uo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
cd "$here"
py="${PY:-python3}"
home="$(mktemp -d)"
trap '"$py" -c "import shutil, sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" "$home"' EXIT
export HERMES_HOME="$home" PYTHONDONTWRITEBYTECODE=1
failed=()
skipped=()

gate() { # gate <name> <command...>
  local name="$1"; shift
  printf '\n== %s\n' "$name"
  if "$@"; then printf '   PASS  %s\n' "$name"; else printf '   FAIL  %s\n' "$name"; failed+=("$name"); fi
}

gate "ruff format"  "$py" -m ruff format --check .
gate "ruff lint"    "$py" -m ruff check .
gate "mypy"         "$py" -m mypy
gate "mypy --strict (new modules)" "$py" -m mypy --strict hermes_3d_lib/settings.py hermes_3d_lib/linkkey.py \
                                    hermes_3d_lib/procid.py hermes_3d_lib/logsafe.py hermes_3d_lib/worldtext.py
if "$py" -m reuse --version >/dev/null 2>&1; then
  gate "reuse lint (licence of every file)" "$py" -m reuse lint --quiet
else
  skipped+=("reuse lint (install the dev group: uv sync --project tools --group dev)")
fi
gate "pytest + coverage >= 90%" "$py" -m pytest -q --no-header --cov --cov-report=term-missing:skip-covered

if [ -n "${REACT_DIR:-}" ] && command -v node >/dev/null; then
  gate "node desktop tests" env REACT_DIR="$REACT_DIR" node --test tests/desktop.test.mjs
else
  skipped+=("node desktop tests (set REACT_DIR and install node)")
fi

if [ -n "${HERMES_AGENT_DIR:-}" ] && [ -x "$HERMES_AGENT_DIR/venv/bin/python" ]; then
  # `hermes plugins ...` needs a repaired install; the same functions are called directly on the checkout.
  hermes_py() { (cd "$HERMES_AGENT_DIR" && "$HERMES_AGENT_DIR/venv/bin/python" -c "$1" "$here"); }
  gate "hermes plugins validate" hermes_py 'import sys; sys.path.insert(0, "."); from hermes_cli import plugins_cmd as p; p._catalog().cmd_validate(sys.argv[1])'
  gate "hermes plugins doctor --ci" hermes_py 'import sys; sys.path.insert(0, "."); from hermes_cli import plugins_cmd as p; p.cmd_plugin_doctor(sys.argv[1], ci=True)'
elif command -v hermes >/dev/null && hermes plugins --help >/dev/null 2>&1; then
  gate "hermes plugins validate" hermes plugins validate "$here"
  gate "hermes plugins doctor --ci" hermes plugins doctor "$here" --ci
else
  skipped+=("hermes plugins validate/doctor (set HERMES_AGENT_DIR or repair the hermes CLI)")
fi

printf '\n== summary\n'
for s in "${skipped[@]}"; do printf '   SKIPPED  %s\n' "$s"; done
if [ ${#failed[@]} -gt 0 ]; then printf '   FAILED: %s\n' "${failed[*]}"; exit 1; fi
printf '   all gates passed (%d skipped)\n' "${#skipped[@]}"
