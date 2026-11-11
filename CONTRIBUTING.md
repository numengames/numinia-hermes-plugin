<!--
SPDX-FileCopyrightText: 2026 Numen Games S.L.
SPDX-License-Identifier: CC-BY-4.0
-->

# Contributing

## Rules
- **English everywhere** in code, comments, logs, CLI output, docs, commits and PRs, and in the text the engine
  shows to people in the world ([docs/WORLD-STRINGS.md](docs/WORLD-STRINGS.md), kept equal to
  `hermes_3d_lib/worldtext.py` by a test). Only the engine's schema (object types, verbs, JSON keys) stays Spanish.
- No hardcoded paths. Everything derives from `HERMES_HOME`; settings come from `config.yaml` through
  `hermes_3d_lib/settings.py`. The only environment variable is the secret `HERMES_3D_LINK_KEY`.
- Never read another program's credential file. Never log a secret. Never use a shell: subprocesses take an argv list.
- A bug fix starts with a failing test. Tests use a temporary `HERMES_HOME` (fixture `hermes_home`) and never
  sleep for a fixed time: use `until(...)` / events with timeouts.
- Never touch a real Hermes install from a test or script.

## Checks
```
uv sync --locked --group dev   # the gate tools, pinned in uv.lock
PY=.venv/bin/python REACT_DIR=…/node_modules HERMES_AGENT_DIR=…/hermes-agent make check
```
Runs: `ruff format --check`, `ruff check`, `mypy` (+ `--strict` on the new modules), `reuse lint`, `pytest` with a
90 % coverage floor on `hermes_3d_lib`, the Desktop `node --test`, `plugins validate`, `plugins doctor --ci`.
Gates whose tool is not configured are reported as SKIPPED, never as passed.

## Commits
Conventional Commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`; `!` for breaking). One logical change
per commit, files added by name. Update `CHANGELOG.md` for anything a user can notice.
