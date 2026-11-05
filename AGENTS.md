# Working in this repository

- **What it is:** hermes-mundo, a Hermes dashboard plugin that shows Hermes as a 3D world you walk through (cron, profiles, MCP, skills, sessions). Provisional repository name: numinia-hermes-plugin.
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
- Tests: `python3 -m pytest tests` (the backend `dashboard/plugin_api.py`).
