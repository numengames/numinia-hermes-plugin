<!--
SPDX-FileCopyrightText: 2026 Numen Games S.L.
SPDX-License-Identifier: CC-BY-4.0
-->

# Changelog

Format: [Keep a Changelog](https://keepachangelog.com), versions follow [SemVer](https://semver.org).


## [0.8.2] - 2026-11-11

### Fixed
- `hermes plugins install` no longer asks an empty "declares Python dependencies" question. The gate tools (ruff, mypy,
  pytest, …) moved out of the root `pyproject.toml` into `tools/pyproject.toml` (+ `tools/uv.lock`), so Hermes sees a plugin
  with no Python dependencies and does not install test tools into its shared plugin environment. Contributors now run
  `uv sync --project tools --locked --group dev`.

## [0.8.1] - 2026-11-11

### Changed
- First run is one path with no choices. `hermes 3d setup` no longer asks for a preset (and `--profile` is gone): it applies
  one default set (Health, Cron jobs, Agents, Skills, Docs library, read-only, no direct database reads), asks once to write
  it, and says where to change the switches later. `--yes` still answers for scripts, and now writes that set.
- Setup copy reads the same from the first line to the world: capitalised headings ("What I found", "Switches"), steps that
  are expected on a first run (engine, Node, docs viewer) are no longer shown as `[!!]` errors, "link key" no longer reads
  "ok / missing", every switch shows its name before its config key, and `allow_direct_db_read` says it covers Spend, Kanban
  and Projects. No jargon ("checkout"), short sentences, and each step says what happens next.

## [0.8.0] - 2026-11-11

### Added
- Per-agent stick-figure bodies: one figure and one `pnj@1` per Hermes profile, with a fixed round, a spend line and a
  "Doing / Done" line from the Kanban (`hermes_3d_lib/cuerpos.py`, `docs/CUERPOS.md`, CC0 figure in `assets/`). Sown when
  an existing world starts; a brand-new world gets them on the next start.
- Project workshops: each project folder and its boards are placed in a workshop zone of the world (`talleres.py`).
- Cover image `assets/hermes-3d.jpg` and screenshots in `docs/images/`.
- Setting `deadline_clock` (default `false`): the hackathon countdown card and links are published only when asked for.

### Changed
- Everything the plugin writes into the world is English now (replies, card titles and subtitles, `docs/WORLD-STRINGS.md`).
  The engine's schema (object types, verbs, JSON keys, the `estado` enum) stays Spanish.
- The plugin is now called **hermes-3d**, titled "Hermes 3D" (it was published under its working name). Install with
  `hermes plugins install hermes-3d`, enable with `hermes plugins enable hermes-3d`; every command is `hermes 3d <...>`
  (for example `hermes 3d install-engine`). The Python package is `hermes_3d_lib`, the tools are `hermes3d_state` and
  `hermes3d_cron_action`, the data folder is `<HERMES_HOME>/plugin-data/hermes-3d/`, the environment variables start with
  `HERMES_3D_`, the Desktop route is `/hermes-3d`.
- The one-time migration of the pre-0.5 data folder is removed (that folder never shipped in a release).
- The Hermes version label the bridge sends to the engine is read from package metadata (`importlib.metadata`), not by
  importing `hermes_cli`: `hermes plugins validate` now passes with no warning (catalog rule 9).
- Every file states its licence (`reuse lint`: REUSE 3.3 compliant; MIT for code, CC-BY-4.0 for texts and images, CC0 for
  the figure), and `make check` runs `reuse lint` as a gate.
- The gate tools are a `dev` dependency group in `pyproject.toml`, locked in `uv.lock` with a 3-day release quarantine.
- New tests: the committed figure is rebuilt byte for byte by its generator; no shipped module imports Hermes outside
  the published plugin API.
- `AGENTS.md`, `CONTRIBUTING.md` and `SECURITY.md` describe the current plugin (name, gates, supported version).

### Fixed
- Starting the world (`hermes 3d up`) gave each body its spend and Kanban titles by reading `state.db` / `kanban.db`
  even with `allow_direct_db_read` off. Those reads now need the opt-in and the `gasto` / `kanban` switch, as everywhere
  else (THREAT-MODEL T19); without them a body carries its name only.
- The read-only SQLite open escapes the path (`Path.as_uri()`): a `#` or `?` in `HERMES_HOME` no longer drops `mode=ro`.
- The dashboard routes run their bridge calls in a worker thread: a world action waiting for approval no longer freezes
  the Hermes web server's event loop.
- `hermes` child processes no longer inherit approval or session variables (`HERMES_YOLO_MODE` and the like).
- Docs match the code: 256-bit bridge token, the approval prompt names job and verb, only pause/resume exist, and the
  README says the approval follows Hermes' own approval settings and when `config.yaml` is edited directly.

## [0.7.2-rc1] - 2026-11-05

### Changed
- The engine child ALWAYS gets `MOTOR_SALIR_SI_STDIN_CIERRA=1` and a held stdin pipe (an old bridge is replaced so a new engine has a holder); the installer also checks the pin against the release's `SHA256SUMS` when reachable (hard fail on mismatch); more installer tests (hardlink/device/setuid, verify-before-open).

## [0.7.1-rc1] - 2026-11-05

### Fixed (found by the first real run against release motor-e7f4a0d)
- A world the engine creates on this very start (Path A, `engine_link=false`) now gets `embed.activa: true` in its
  `entorno/espejo.json` and the engine is restarted once: the real `hermes` template ships with embed OFF and the engine reads
  the file only at start, so the first run used to end in "this world does not accept an embedded page". An existing world is never edited.
- Whether the mirror link is on is now read after `/health` (a new world's `espejo.json` did not exist yet), so the false
  note "espejo.json is missing or not active" and the skipped link wait on a first start are gone.

## [0.7.0-rc1] - 2026-11-05

### Added
- **Five-minute install.** `hermes palace setup --first-run`: Node from Hermes, the pinned engine, `engine_link=false`, start, "open the
  Palace page in Hermes Desktop". Each download or write is asked first (`--yes` for scripts).
- `nodebin`: Node via `pm.ensure("node")` / `pm.installed_package("node").binary` with the environment of `with_hermes_node_path()`;
  `PATH` only when `pm` cannot be imported; typed non-retryable `NODE_MISSING`; a warning (not an error) below 22.18.
- `enginekeys`: `MOTOR_SECRET`/`MOTOR_ADMIN` made once per install (0600), passed through the child's environment only.
- Typed errors (`PalaceError` with `code` and `retryable`): `NODE_MISSING`, `ENGINE_NOT_PUBLISHED`, `SHA256_MISMATCH`, `DOWNLOAD_TRUNCATED`,
  `UNSAFE_ARCHIVE`, `ROOT_REFUSED`, `PORT_BUSY`, `ENGINE_START_FAILED`, ...
- Engine child: `/health` wait, last log lines in the error, port-busy and port-clash checks, stdin pipe held by the bridge
  (`MOTOR_SALIR_SI_STDIN_CIERRA=1`), stale PID file dropped on start.
- Tests: fake release server (`tests/fake_release.py`), node-less stub engine (`tests/stub_engine.py`); 40+ new tests for the installer, node, engine child and first run.

### Changed
- `install-engine` now installs a **release asset** (sha256 pinned in `engine.lock.json`: tag, asset, URL template, size, entry file)
  instead of cloning a commit and running `npm ci`: no git, no npm, no build on the user's machine. Verified before unpacking; unpacked in Python.
- The engine entry file comes from the install marker (`app/<entry>`), so a release (`entrada.ts`) and a checkout (`main.ts`) both work.
- Node requirement: 22.18 or newer (was "24"), provided by Hermes.

## [0.6.0-rc1] - 2026-11-05

### Added
- **The world inside Hermes Desktop (path A).** A *Palace* page and sidebar row (`/palace`) host the world in a Desktop `SandboxedFrame`
  (`sandbox="allow-scripts allow-pointer-lock"`), with the states loading / engine down (exact command shown) / error / connected.
- `POST /embed-credential`: makes the engine's `e1.<exp>.<nonce>.<hmac>` credential from the link key (never returned; default 1 h,
  `embed_credential_ttl_s` 60-86400 s) and the world URL with the credential in the fragment. Contract-tested against the real engine.
- `GET /world-snapshot` (the same serializers as the HMAC link) and `POST /action` (the same approval road as a link request:
  read-only refuses, an id works once, no approver fails closed).
- The page bridge: `postMessage` snapshot / events / funciones to the frame, requests from the frame to `/action`, `resultado` back
  (frame window check, `v:1`, 16 KiB, rate limit, 8 in flight).
- `engine_link` setting (default `true`): path A needs `false`, because an engine takes one source and a local link displaces the embedded page.

### Changed
- The dashboard router is no longer GET-only: two named POSTs (`/embed-credential`, `/action`); the test pins exactly that.

## [0.5.0-rc1] - 2026-11-05

### Added
- `hermes_api`: first layer for the work board (`hermes kanban list --json`, argv list, 10 s timeout, every task cut to a fixed
  field set on parse) and for sessions (`SessionDB(read_only=True)`: totals and `get_session_list_row` for the sessions the hooks
  saw). Measured: no listing method of `SessionDB` is free of message-derived previews, so none is used. Direct database reads stay
  opt-in (`allow_direct_db_read`) and are the fallback.
- `tests/test_storage.py`, `test_requires_hermes.py`, `test_disclosure.py`, `test_hermes_api.py` (recorded fixtures of the real
  kanban JSON and the SessionDB probe).
- README table of every place the plugin starts a process; a test fails if a module is missing from it.

### Changed
- Data moved to `<HERMES_HOME>/plugin-data/hermes-palace/` (Hermes' per-plugin convention, modes 0700/0600 kept). The old
  `<HERMES_HOME>/hermes-palace/` is moved once, without following symlinks and without overwriting.
- `requires_hermes` is `>=0.22.0, <0.24` in `plugin.yaml`, `pyproject.toml` and the catalog draft (a test keeps them equal).
- Loopback addresses are composed at runtime; no address:port literal and no mention of the Hermes secrets file ships.
- Catalog draft: category `desktop`, disclosure names every process the plugin starts.

### Removed
- `tools/` (developer harnesses that import Hermes internals and start processes) left the shipped tree for the sibling
  repository `plugin-hermes-palace-dev`; `make integration` runs them from there. `plugins validate --install-deps` now reports no warnings.

## [0.4.0-rc1] - 2026-11-05

### Added
- Three new things for the M6 world, each behind its own switch (off by default, both sides must have it on):
  `habilidad` (`funciones.habilidades`, setting `publish_skills`: name, group = folder under `skills/`, on/off, description
  cut to 140; at most 8 groups, the rest as «otros»), `tablero` (`funciones.kanban`, `publish_kanban`: one board with
  cards per column and up to 3 redacted 32-byte titles; read-only) and `documento` (`funciones.documentos`,
  `publish_docs`: 14 curated pages with their address on the local docs viewer).
- `guia`: if the engine lists it, the hub passes the flag through (`Hub.guide`, `palace status`/bridge status); no switch of ours.
- `hermes palace docs [--stop]`: starts the lite docs viewer of the `docs-visor` repo on 127.0.0.1 (settings
  `docs_viewer_path`, `docs_viewer_port`), builds it with `build-docs.sh lite` when missing, records pid + start time +
  command line, and `palace status` shows it.
- `logsafe.redact_title`: free-text titles (kanban) lose secret shapes, e-mail addresses and long tokens before they leave.
- Integration run, step 12: the real engine accepts the three types with zero `descartadas`; the real game client sees them.

### Changed
- Zones come from the engine's own `hola.zonas` (first zone by id that admits the type); a documented table is only the
  fallback. Fixes the M6 mismatch: `perfil` is z6 and `uso` is z2 (the plugin used z4/z5).
- `scan --json` card titles are redacted like the world's.

## [0.3.0-rc1] - 2026-11-05

### Added
- `read_only` setting and `palace up --read-only`: the world cannot change anything in Hermes; every write verb is refused,
  no control is offered on objects, `/action/*` answers 403, and `palace status` and the world card say "read-only".
- `hermes palace demo`: your own Hermes (profiles, cron, spend; counts only) as a read-only world; refuses to run if
  read-only cannot be confirmed.
- `hermes palace install-engine`: user-initiated download pinned by `engine.lock.json` (`TODO-PUBLISH` = refuses),
  confirmation before downloading (`--yes`), Node >= 24 check, `npm ci` + build with timeouts, `engine_path` written to the
  settings, launcher shortcut (Linux tested).
- The engine's `ack` frame: objects left out of a `lote` show in `palace status` and the (redacted) log.
- Per-client rate limit on the bridge (429 + Retry-After).
- Desktop: explicit loading / error / disconnected / empty / ready states; English default with a Spanish locale.
- Manifest metadata: `tags`, `manifest_version: 2`, `api_version: 1`.
- `tests/test_hermes_doctor.py`: the host's `plugins validate` and `doctor --ci` run from pytest when `HERMES_AGENT_DIR` is set.

### Changed
- `last_error` carries a redacted, bounded message, not just the exception class.
- Scan and hub catch specific exceptions; a bug in a scan section leaves a traceback in the log.

### Fixed
- `stop_engine` waits for SIGKILL to take effect before reporting the engine stopped (flaky test).

## [0.2.0] - unreleased

### Fixed (found by the end-to-end run, tools/integration)
- `palace scan` did not count profiles made by `hermes profile create` (no config.yaml yet).
- Switches flipped in the world (`funciones` frames) were ignored: a function switched off kept being published.
- The cron poll's `lote` held only cron things, and the engine's `lote` replaces everything: it wiped agents and spend.
- Link writes were unpaced; the real engine closes the link above 20 frames/s (4429). Writes are now paced.
- The real engine refuses a whole `lote` for one object in a zone it lacks; the hub now sends only what `hola` says it can place.

### Added
- `tools/integration/run_integration.sh`: end-to-end run with the real engine and the real Hermes in a throwaway home.
- Step 10 of that run (`I1_DESKTOP=1`, load < 6): a second, isolated Hermes Desktop driven over CDP by
  `tools/integration/desktop_cdp.mjs` (no dependencies, Node 22.18 or later). It checks that the status chip appears, that
  clicking it shows the World pane with the function switches, and that the open-the-world button hands
  `ctx.os.openExternal` the URL `hermes palace up` printed (intercepted with a fake `xdg-open`, no real browser).
- Desktop: the chip's label is now a button that toggles the right sidebar where the World pane lives
  (`ctx.runAction('view.toggleRightSidebar')`). Before, the pane could only be reached through the sidebar toggle.

### Fixed (integration harness)
- The throwaway home is now `<work>/home/.hermes` with `HOME=<work>/home`. A home under any other name that sits below a
  `profiles/` folder is read by Hermes as a named profile of the real install, and `hermes gateway ensure` then answers
  `profile_parked` ("only one profile exists") for ever, so Desktop never leaves "Connecting…".

### Changed (breaking)
- `HERMES_PALACE_ENGINE`, `_WORLD`, `_ENGINE_PORT`, `_LINK_PORT`, `_WORLD_URL` are gone. Use the settings
  `engine_path`, `world_path`, `engine_port`, `link_port`, `world_url` in
  `plugins.entries.hermes-palace.settings`.
- Function switches are flat settings (`publish_cron`, …) instead of the nested `funciones.*` keys.
- The plugin no longer reads `<world>/enlace.env`. The key is `HERMES_PALACE_LINK_KEY` or a key the plugin
  generates (`link.key`, 0600) and hands to the engine as `MOTOR_ENLACE_CLAVE`.
- Every user-facing string of the plugin (CLI, errors, Desktop UI, docs) is English. Spanish remains only
  where the engine shows text to people in the world ([docs/WORLD-STRINGS.md](docs/WORLD-STRINGS.md)).

### Added
- `hermes palace up` passes `WWW_DIR` so the engine serves the world page.
- Loopback bridge hardening: Host validation, Origin and `Sec-Fetch-Site` refusal, JSON content type,
  connection cap, header/body caps, overall request deadline, strict `Content-Length`, no
  `Transfer-Encoding`, constant-time token compare.
- Secret redaction in every log line; log, pid, runtime and key files are 0600 regardless of umask.
- Process identity (start time + command line) before the plugin signals the engine it started.
- `make check` / `scripts/check.sh`; ruff, mypy (strict on new modules), 90 % coverage floor.

### Fixed
- Job-id regex accepted a trailing newline (`$` vs `fullmatch`); cron actions now run as an argv list.
- Seven flaky/red tests; fixed sleeps replaced by event and poll helpers with timeouts.

## [0.1.0]
First local version: bridge, cron/sessions/health mirror, scan, Desktop chip and pane.
