# numinia-hermes-plugin

A Hermes Agent plugin that turns Hermes itself into an explorable 3D world, built with the Numinia Engine.

**hermes-mundo** adds a tab to the Hermes web dashboard (`hermes dashboard`): a 3D plaza with six
zones (scheduled jobs, profiles, MCP servers, skills, sessions, configuration) drawn from Hermes's
own data. Status: 0.1.0, for the Hermes plugins hackathon (closes 2026-11-11, 22:00 UTC).

## Layout

- `plugin.yaml`: the plugin's manifest for Hermes.
- `dashboard/manifest.json` and `dashboard/dist/index.js`: the tab; the bundle (three.js inside) is committed and pinned, never fetched at run time.
- `dashboard/plugin_api.py`: the backend (FastAPI, under `/api/plugins/hermes-mundo/`). It only reads Hermes's public surfaces; pausing or resuming a job goes through Hermes's own endpoint and approval.
- `tests/`: `python3 -m pytest tests` (needs `fastapi`, `httpx`, `pytest`).

## Licence

MIT (`LICENSE`): this is the edge (PRI-005). The full world runs in the Numinia Engine, which is
AGPL-3.0-only and lives in its own repository; the plugin talks to it over a local connection and
contains none of its code. The names Numinia and Numen Games are not licensed.
