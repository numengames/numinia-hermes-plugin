<!--
SPDX-FileCopyrightText: 2026 Numen Games S.L.
SPDX-License-Identifier: CC-BY-4.0
-->

# Working in this repository

- **What it is:** numinia-hermes-plugin, a Hermes dashboard plugin that shows Hermes as a 3D world you walk through (cron, profiles, MCP, skills, sessions). The plugin id, the repository and the install folder all carry the same name; the tab is labelled "Mundo".
- **Project:** Hackathon (temporary, until 2026-11-11 22:00 UTC; Hermes plugins hackathon).
- **Human owner:** Pablo (@PabloFMM). He merges; agents open pull requests.
- **Published at:** not yet; the entry is a PR to NousResearch/hermes-agent `plugin-catalog/`.

## Rules

- The organisation's rules live in the Summa:
  [numinia-archive/AGENTS.md](https://github.com/numengames/numinia-archive/blob/main/AGENTS.md).
- The catalogue's rules come first: public Hermes surfaces only, no self-updating code
  (the bundle `dashboard/dist/index.js` is committed and pinned), every action that changes
  Hermes goes through Hermes's own approval.
- Pull requests, commits and comments in English. Never commit a secret or a real Hermes home.
- Licence: MIT (the edge). Never copy Numinia Engine code (AGPL-3.0-only) into this repository.
- Tests: `python3 -m pytest tests` (the backend `dashboard/plugin_api.py`).
