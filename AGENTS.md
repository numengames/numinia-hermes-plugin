<!--
SPDX-FileCopyrightText: 2026 Numen Games S.L.
SPDX-License-Identifier: CC-BY-4.0
-->

# Working in this repository

- **What it is:** Hermes 3D (`hermes-3d`), a Hermes plugin that turns Hermes into a 3D world you walk through
  (health, cron, sessions, profiles, spend, skills, Kanban, projects). Repository: `numinia-hermes-plugin`;
  plugin id, data folder and CLI (`hermes 3d ...`) carry the name `hermes-3d`.
- **Human owner:** Pablo (@PabloFMM). He merges; agents open pull requests.
- **Published at:** submitted to the Hermes plugin catalog (a PR adding `plugin-catalog/hermes-3d.yaml` to
  NousResearch/hermes-agent; draft in `docs/plugin-catalog/hermes-3d.yaml`).

## Rules

- The organisation's rules live in the Summa:
  [numinia-archive/AGENTS.md](https://github.com/numengames/numinia-archive/blob/main/AGENTS.md).
- The catalogue's rules come first: public Hermes surfaces only, no self-updating code (the engine is a separate,
  user-requested, sha256-pinned download: `engine.lock.json`), every action that changes Hermes goes through
  Hermes's own approval.
- Pull requests, commits and comments in English. Never commit a secret or a real Hermes home.
- Licence: MIT for code, CC-BY-4.0 for texts and images (`REUSE.toml`; `reuse lint` must pass).
  Never copy Numinia Engine code (AGPL-3.0-only) into this repository.
- Gates: `make check` (ruff, mypy, pytest with coverage >= 90%, Desktop tests, `hermes plugins validate` and
  `doctor --ci`); tools in the `dev` dependency group of `pyproject.toml`, locked in `uv.lock`.
