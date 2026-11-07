# hermes-palace

Hermes as a place. A [Hermes Agent](https://hermes-agent.nousresearch.com) plugin that mirrors your
Hermes (health, scheduled jobs, sessions, agents, spend) into a local
[Numinia Engine](https://numen.games) world of type `hermes`, so you can see it, walk through it and,
with your approval, act on it.

- Plugin: **MIT**. Engine: **AGPL-3.0**. Two separate programs that talk over a local WebSocket on
  `127.0.0.1`; the plugin never imports or copies engine code.
- Needs Hermes ≥ 0.22 and < 0.24, Python ≥ 3.11 (Hermes' own), Node 22.18 or later for the engine, **provided by Hermes** (its package manager), not by you.
- Status: **v0.7.2-rc1, local only.** Not published, not in any catalog.

## Install in five minutes

Three commands, no Node to install, nothing to compile. Measured against the fake release in the tests: 0.22 s for the whole
`setup --first-run` flow; on a real machine the time is the download (the engine release is ~14 MB) plus ~1 s for `/health`.

```
hermes plugins install <this plugin>        # (or copy the folder into <HERMES_HOME>/plugins/hermes-palace and enable it)
hermes palace setup --first-run             # asks before each download or write; add --yes for scripts
# then open the "Palace" page in Hermes Desktop
```

What `setup --first-run` does, in order, and what each step downloads, runs and writes:

| # | Step | Downloads | Runs | Writes |
|---|---|---|---|---|
| 1 | Node | Hermes' package manager fetches **its own pinned, checksummed Node** (only if missing, after you say yes) | `pm.ensure("node")`; `node --version` | Hermes' own tools folder (Hermes does this, not the plugin) |
| 2 | Engine | **One file** from the GitHub release named in `engine.lock.json` (`github.com/numengames/numinia-engine/releases`, pinned tag + asset). The command shows URL and size and asks first. | Nothing from the download is executed: the plugin unpacks `app/` and `www/` itself, after the sha256 matches | `<HERMES_HOME>/plugin-data/hermes-palace/engine/<sha12>/` (0700) and `engine_path` in your `config.yaml` |
| 3 | Settings | nothing | `hermes config set ...engine_link false` (after you say yes) | one key in `config.yaml` |
| 4 | Start | nothing | the engine (`node app/<entry>`, as you, never root, `127.0.0.1` only) and the bridge | `engine-keys.json` (0600), `engine.pid`, `engine.log`, `bridge.log` |

Guarantees (catalog rule 17 style disclosure):

- **Network:** only to GitHub releases, only for the pinned asset, only when you run `install-engine` or `setup --first-run`. `https` only
  (plain `http` only to the loopback, for the tests). No telemetry.
- **Integrity:** the sha256 of the download is compared with the one pinned in `engine.lock.json` **before anything is unpacked**; a wrong
  or truncated download is deleted and nothing is installed. If the release publishes `SHA256SUMS` next to the asset, the pin is checked against it too (skipped silently if it cannot be fetched; a mismatch aborts). The lock says `TODO-PUBLISH` until a release that contains the Hermes
  world template is pinned: until then the command refuses with a clear message.
- **Unpacking:** done in Python, never by running the downloaded installer script. Only `app/` and `www/` are kept; any path outside the
  folder, absolute path, `..`, hard link, device, fifo, or symlink that leaves the folder refuses the whole asset; setuid/setgid bits are
  dropped; at most 400 MB and 20 000 entries.
- **Privileges:** runs as your user; refuses to run as root (the engine refuses too). Idempotent: with the same sha256 installed, nothing is downloaded.
- **Node:** from Hermes (`pm.ensure("node")`, `pm.installed_package("node").binary`, environment from `with_hermes_node_path()`).
  A Node on `PATH` is used only when this Hermes has no package manager. A Node older than 22.18 gives a warning, not an error.
  If Hermes cannot provide Node you get `NODE_MISSING` (not retryable) and what to run.
- **Secrets:** the engine's `MOTOR_SECRET` and `MOTOR_ADMIN` are made once per install, kept in `engine-keys.json` (0600), handed over
  through the child's environment, never printed or put on a command line.
- **Lifecycle:** the engine is always started with a stdin pipe that the bridge keeps open (`MOTOR_SALIR_SI_STDIN_CIERRA=1` is always set); an engine that supports
  `MOTOR_SALIR_SI_STDIN_CIERRA=1` exits when the bridge dies (engine side: still to be done, see `QA/NECESIDADES-MOTOR.md`). Either way
  `hermes palace down` stops it by recorded PID **and start time** (a recycled PID is never signalled) and a stale PID file is dropped on the next start.
- **Ports:** engine 8790 (`engine_port`), docs viewer and link 8791. A busy or clashing port stops with the setting to change.

## Disclosure

- **Processes:** `hermes palace up` starts a detached bridge process and, if the engine is not already
  listening, a Node.js Numinia Engine process (`engine_path` setting). Both run as you, on this machine.
- **Network:** the bridge serves HTTP on `127.0.0.1` (random port, per-start token) and connects out to the
  engine's link on `127.0.0.1`. Nothing leaves the machine; no telemetry.
- **Reads:** `hermes palace scan` and the bridge read, read-only, the `state.db`, `kanban.db`, `projects.db`,
  `config.yaml` (MCP server names only) and `skills/` of **every profile** under the Hermes root; with the
  `cron` function on they run `hermes cron list --all`. See "What `hermes palace scan` reads".
- **Actions:** only after your explicit approval in Hermes, `hermes cron pause|resume <id>`.
- **Writes:** only `<HERMES_HOME>/plugin-data/hermes-palace/` (runtime file, link key, `bridge.log`, `engine.log`) and the
  world folder you configure. Message text and secrets are never read.

### Every place the plugin starts a process

All are argv lists (never a shell string), with a timeout where they wait, run as you, and a failure is reported, not retried
in a loop. The Nous scanner reports each one as `python_subprocess` (medium); this table is the explanation.

| Module | Command | Why |
|---|---|---|
| `bridge_main.py` | `hermes cron list --all` (fallback when `cron/jobs.json` cannot be used) | the only public listing when the file is unreadable |
| `actions.py` | after your approval in Hermes, `hermes cron pause\|resume\|run <id>` | the only public way to change cron today |
| `supervisor.py` | the bridge (`python -m ...bridge_main`) and the Node engine (`node ... main.ts`) | `hermes palace up`; both detached, pid recorded with its start time |
| `docsviewer.py` | `build-docs.sh`, `node serve.mjs` of the docs-visor folder | `hermes palace docs` only |
| `installer.py` | `hermes config set` (saves `engine_path`); the download itself is in-process `urllib`, the unpacking in-process Python | `hermes palace install-engine` / `setup` only |
| `nodebin.py` | `<node> --version` on the Node that Hermes' package manager provides | `palace up` / `setup` / `docs` |
| `setup_cmd.py` | `node --version`, `hermes config set` | `hermes palace setup` only |
| `hermes_api.py` | `hermes kanban list --json` (argv list, 10 s timeout, soft failure) | first layer for the work board; falls back to the opt-in database read |
| `procid.py` | `ps -o lstart=,command= -p <pid>` | prove a recorded pid was not recycled before signalling it (non-Linux fallback; Linux reads `/proc`) |

## Known limitation: cron has no public hooks yet

Hermes exposes no public event for "a cron run started or ended". Until it does, the cron room is fed by a workaround:

- the schedule from `cron/jobs.json` (read-only; `hermes cron list` as fallback);
- run history opened read-only when the optional direct read is on;
- liveness from `pre_api_request` events with `platform == "cron"`.

This is slower and less precise than a hook, so a pause or run may show up a few seconds late. We asked upstream for observer hooks `cron_run_start` / `cron_run_end`: [NousResearch/hermes-agent#149341](https://github.com/NousResearch/hermes-agent/issues/149341), complementing #65401 (`on_cron_delivery`). A stable `hermes cron list --json` is requested in #118072. When either lands, the plugin will use it and the workaround becomes the fallback.

## What it does

| Function (setting) | Default | The world sees | The world may ask |
|---|---|---|---|
| `salud` (`publish_health`) | **on** | Hermes is up (bridge state) | nothing |
| `cron` (`publish_cron`) | off | one object per scheduled job (name, schedule, state) | pause / resume a job, **only after you approve it in Hermes** |
| `sesiones` (`publish_sessions`) | off | one place per session (hashed id, platform, model) | nothing |
| `agentes` (`publish_agents`) | off | one object per Hermes profile (name, model, gateway, cron counts) | nothing |
| `gasto` (`publish_spend`) | off | tokens and estimated cost per profile | nothing |
| `habilidades` (`publish_skills`) | off | one tablet per skill in the workshop (name, group, on/off, first 140 characters of the description) | nothing |
| `kanban` (`publish_kanban`) | off | the work board as one object: cards per column (to do / in progress / done) and up to 3 card titles each | nothing |
| `documentos` (`publish_docs`) | off | a short fixed list of documentation pages in the library (title and address on the local docs viewer) **plus six fixed cards for the Nous plugin hackathon**: a deadline clock (rebuilt once a minute, from the system clock only, until 2026-11-11 22:00 UTC; then it reads «closed»), the three entry steps and the official links (catalog-submission guide, plugin developer guide, entry form, Discord). The plugin never fetches those pages; the world's viewer opens one only if its origin is in `documentos.origenes` | open a page (the world's viewer) |

`guia` (the in-world guide) is a switch of the world page only; if the engine lists it, the plugin passes the flag
through and does nothing else with it.

**Zones.** The plugin asks the engine: the zone of each thing comes from the `zonas` the engine announces in its
`hola` (the first zone, by id, that admits the type). Only when the engine does not announce zones does the plugin
use a documented table: `salud` z0, `documento` z1, `uso` z2, `sesion` z3, `habilidad` z4, `perfil` z6, `cron` z7,
`mensaje` z8, `tablero` z9.

**Kanban columns** (the plugin's choice): *to do* = triage, todo, ready; *in progress* = running, blocked; *done* =
done within `scan_days`. Card titles are redacted (secret shapes, e-mail addresses, long tokens) and cut to 32 bytes.

The world has its own switch per function (`entorno/espejo.json`); a function runs only when **both** sides
have it on.

## What `hermes palace scan` reads

A single read-only pass over the Hermes root (the default home whose `profiles/` holds the named ones), for
**all** profiles:

| Source | Used for | Never read |
|---|---|---|
| `profiles/*/state.db` (SQLite, opened `mode=ro`, `query_only`) | session counts, models, token and cost totals | message text |
| `profiles/*/kanban.db`, `projects.db` (same, read-only) | task counts per status, project names | task bodies, comments |
| `profiles/*/config.yaml` → `mcp_servers` | server **names** and transport kind (`stdio` / `http`) | commands, arguments, URLs, env, tokens |
| `profiles/*/skills/**/SKILL.md` | skill names, group (folder), description (front matter only, first 4 KB) and counts | skill instructions |
| `profiles/*/kanban.db` titles | up to 3 card titles per column, redacted | bodies, comments, results, assignees (in the world) |
| `gateway_state.json` | whether a gateway is running | |

`.env`, `auth.json`, `mcp-tokens` and any `config.yaml` value outside a fixed whitelist are never opened.
Every database is opened read-only; no file is created or written outside `<HERMES_HOME>/plugin-data/hermes-palace/`.
`hermes palace scan --json` prints exactly the snapshot (counts and names only). What is **published** is the
subset behind the switches above.

## What is read from where, and what is published

The plugin prefers, in this order: **(1) a documented Hermes API or hook, (2) the `hermes` CLI, (3) files in the Hermes
home, (4) a direct read of Hermes' private SQLite files**, which is **off by default** (`allow_direct_db_read: false`)
because those schemas are not a public surface and may change between Hermes versions. Nothing is read for a function
whose switch is off: with only `publish_health` on (the default) the plugin opens none of your Hermes files.

| Function (switch) | Read from | Layer | Published (metadata only) | Never read or published |
|---|---|---|---|---|
| Health (`publish_health`) | nothing of yours: the bridge's own state | own state | a `Hermes · read-only` card when read-only is on | |
| Cron (`publish_cron`) | `hermes cron list --all` (text table; pinned parser, soft failure: no table = no change published); jobs file for the scan | CLI | job id, name, schedule text, state | the job prompt |
| Sessions (`publish_sessions`) | Hermes hooks `on_session_start/end/finalize` | public hook | hashed id, model, platform | titles, message text |
| Agents (`publish_agents`) | profile folders: `config.yaml` model whitelist, `gateway_state.json`, jobs file | files | profile name, model, provider, gateway up, cron counts | descriptions, any other config value, `.env`, `auth.json` |
| Skills (`publish_skills`) | `SKILL.md` front matter (first 4 KB) of every profile | files | name, group, enabled, description cut to 140 | skill instructions |
| Docs (`publish_docs`) | a curated list inside the plugin | own data | title, local viewer address | |
| Spend (`publish_spend`) | `state.db`, `sessions` table sums | **direct DB (opt-in)** | token and cost totals, top model names | message text, session titles |
| Kanban (`publish_kanban`) | **first** `hermes kanban list --json` (each task cut down to id, title, status, assignee, priority and times the moment it is parsed; bodies and results are dropped); only if that fails **and** you opted in, `kanban.db` `tasks` | CLI, then **direct DB (opt-in)** | counts per column, 3 redacted titles (32 bytes) | card bodies, comments, results |

Why the database is the only way for two of them: Hermes documents public APIs for the config (`load_config_readonly`),
the skill catalogue (`find_all_skills`), starting subagents (`ctx.subagent_lifecycle`) and observer hooks (`subagent_start`,
`subagent_stop`, `kanban_task_*`), but all of them work inside one Hermes process on the *active* profile; this plugin
mirrors *every* profile from a separate bridge process, and none of them returns per-profile token or cost totals.
`hermes kanban list --json` is now the first layer for the work board. For sessions, `hermes palace scan` first asks Hermes' own
`SessionDB(read_only=True)`: `session_count()` and `session_count_by_source()` for totals and `get_session_list_row(id)` for the
title, source, model, start/end and message count of the sessions the hooks have seen (that row holds no system prompt and no
message text). The listing methods are not used: both build a `preview` from message bodies in SQL. A recent-sessions list
without having seen the hooks therefore still needs the opt-in direct read.
`palace demo` is your explicit look at your own spend, read-only, so it reads the spend totals for that run.

How the database is read when you opt in: `mode=ro` with `query_only`, only aggregate queries on the `sessions` and
`tasks` tables (never `messages`; `python tools/audit_scan_sql.py` prints every statement, the columns SQLite says it
reads and its query plan on a throwaway 1M-message database), and only when the file changed: the bridge compares
`os.stat` (size and mtime, including the `-wal` file) and re-queries just then, and at least once an hour.
`state.db` can be several GB; an unchanged one is never opened.

## First run: `hermes palace setup`

After installing, run `hermes palace setup`. It checks Node (22.18 or later), the engine folder, the built page, the docs viewer and
the link key, lists every switch in plain words with where it reads from, and asks before it writes anything (through
`hermes config set`, never by editing `config.yaml` itself). It is idempotent, and with `--yes` it never prompts and changes
only what `--profile` / `--engine-path` name.

| Profile | Switches | Why |
|---|---|---|
| (catalog default) | health only | what an install does without asking you anything |
| `minimal` | health only | the same, written explicitly |
| `showcase` | health, cron, agents, skills, docs, **read-only** | a first impression in one minute: names, states and counts from Hermes' own files and CLI; the world cannot change anything. No sessions feed, no spend, no kanban, `allow_direct_db_read` off |

`showcase` is a choice you make, not a default: the catalog stance stays "everything off but health". Without an engine,
`hermes palace scan` shows what the world would mirror, and `hermes palace demo` is the one-command look once an engine is set.

## Install and use

1. Get a Numinia Engine checkout and build its page (`dist/plaza`).
2. Copy this folder to `<HERMES_HOME>/plugins/hermes-palace/` and run `hermes plugins enable hermes-palace`.
3. Tell the plugin where the engine is (see Configuration):
   `hermes config set plugins.entries.hermes-palace.settings.engine_path /path/to/engine`
4. `hermes palace up` — starts (or reuses) the engine, starts the bridge, prints the world URL.
   `hermes palace status` shows engine, link, key source and bridge. `hermes palace down` stops the bridge
   and the engine **only if this plugin started it** (it proves the process is still the one it started
   before sending any signal).
5. In Hermes Desktop (turn the plugin on in Capabilities → Plugins; it is opt-in) the status bar shows
   **World · connected**. Click the label to show or hide the World panel (the function switches); the small
   link button next to it opens the world page.

How the engine is located: only through the `engine_path` setting. There is no search of your disk and no
environment variable. The folder must contain `packages/server/src/main.ts`; if
`<engine_path>/dist/plaza/index.html` exists it is passed to the engine as `WWW_DIR`, so the engine serves the
page on the game port. Without it `hermes palace up` says so and the world page must be served another way.

## Configuration

Settings live in `config.yaml` under `plugins.entries.hermes-palace.settings` (Desktop: Settings → Plugins).
Inside Hermes they are read with `ctx.get_config`; the bridge process and the dashboard router read the same
file. **Precedence: `config.yaml` value → default.** There are no environment-variable overrides.

| Setting | Default | Meaning |
|---|---|---|
| `engine_path` | `""` | Engine checkout (needed for `palace up`) |
| `world_path` | `<HERMES_HOME>/plugin-data/hermes-palace/mundo` | World folder; absolute, no `..`, no symlink, never `/` or `$HOME` |
| `engine_port` | `8790` | Game/HTTP port (1024-65535) |
| `link_port` | `0` | `0` = the world's `entorno/espejo.json`, else `8791` |
| `world_url` | `""` | Page the Desktop button opens; loopback `http://` only |
| `publish_health` / `_cron` / `_sessions` / `_agents` / `_spend` / `_skills` / `_kanban` / `_docs` | `true` / `false` ×7 | Function switches |
| `docs_viewer_path` | `""` | Folder of the `docs-visor` repo for `hermes palace docs`; empty = the folder `docs-visor` next to this plugin |
| `docs_viewer_port` | `8791` | Port of the local docs viewer (127.0.0.1 only); must differ from the engine link port |
| `scan_days` | `30` | Window for session and spend counts (1-365) |
| `read_only` | `false` | The world can look but never change anything in Hermes (see below) |
| `allow_direct_db_read` | `false` | Opt-in: let the plugin read Hermes' own `state.db` / `kanban.db` (spend, recent sessions, and kanban when the CLI fails; see "What is read from where") |
| `cron_poll_seconds` | `15` | Cron poll interval (3-600) |
| `engine_link` | `true` | Open the HMAC link to the engine (path B). Set `false` when the world lives inside Desktop (path A): an open link displaces the embedded page |
| `embed_credential_ttl_s` | `3600` | Life of the credential that opens the world inside Desktop (60-86400; the engine refuses more than 24 h) |

The one secret is the environment variable **`HERMES_PALACE_LINK_KEY`** (declared in `optional_env`, never `requires_env`, so Hermes never asks for it or blocks on it; Hermes
keeps it in `.env`). Where the key comes from, in order:

1. `HERMES_PALACE_LINK_KEY`: use it to join an engine someone else started (it is that engine's
   `MOTOR_ENLACE_CLAVE`).
2. A key the plugin generates itself in `<HERMES_HOME>/plugin-data/hermes-palace/link.key` (mode 0600). When the plugin
   starts the engine it passes this key through the engine's documented `MOTOR_ENLACE_CLAVE`, so the engine
   never needs to write, and the plugin never reads, the engine's `enlace.env`.

Invalid settings are refused with a message naming the key; they are never silently replaced.

## Walk inside your agent (Desktop)

Enable the plugin in Hermes Desktop and a **Palace** row appears in the sidebar (route `/palace`). The page puts the world in a
Desktop `SandboxedFrame` (`sandbox="allow-scripts allow-pointer-lock"`: opaque origin, no `allow-same-origin`, no popups, no
navigation of the app). It needs the engine running with **embed mode on** (`"embed": { "activa": true }` in the world's
`entorno/espejo.json`) and the plugin's link key. The page shows one of four states:

| State | What you see |
|---|---|
| loading | "Opening the world…" |
| engine down | "The world is not running." and the exact command: `hermes palace up` (or `hermes palace setup` the first time) |
| error | the reason (backend off, plugin disabled, settings invalid) and a *Try again* button |
| connected | the world, a *Connected* badge (and *read-only* when it is), a *Reload* button |

How it works (details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), threats: [docs/THREAT-MODEL.md](docs/THREAT-MODEL.md)):

* The page asks the plugin backend (`POST /api/plugins/hermes-palace/embed-credential`) for a URL. The backend makes
  `e1.<expiry>.<nonce>.<hmac>` from the **link key** (never returned) and puts it in the URL **fragment**, so no server or log sees it.
  It lives one hour by default (`embed_credential_ttl_s`, never more than the engine's 24 h cap) and a new one is made each time the page opens.
* When the world says `hola`, the page pushes the same objects the HMAC link publishes (`GET /world-snapshot`, built by the same
  serializers) and then only the differences as events, on Hermes' own `plugin.hermes-palace.*` events plus a 15 s poll.
* A request from the world (pause a cron job) goes to `POST /action`, which takes **the same road as a request from the link**:
  read-only refuses, an id works once, and nothing runs until Hermes' approval gate says yes. With nobody to approve, it fails closed.
  The answer returns to the frame as `resultado`.
* The status-bar chip and the right-hand panel keep working as before. The HMAC link (path B) is unchanged and is what hosted worlds use.

## The documentation library

`hermes palace docs` starts the lite documentation viewer of the `docs-visor` repo on `127.0.0.1:<docs_viewer_port>`
(builds it first with `build-docs.sh lite` if it is not built; the build fetches the pinned upstream commit with
git and writes only under `<HERMES_HOME>/plugin-data/hermes-palace/docs-site/`). It prints the address, `hermes palace docs --stop`
stops the viewer this plugin started (and nothing else), and `hermes palace status` shows it. The world only opens
pages whose origin is in its `documentos.origenes`: the engine's `hermes` template lists the loopback origin on port `8791` (the `docs_viewer_port` default).
**That is also the engine's default link port**, so on the stock template set `docs_viewer_port` to another port *and*
add that origin to the world's `entorno/espejo.json` (or move `fuente.puerto`); `palace docs` refuses to start on the
link port and says so.

## Read-only mode

Set `read_only: true` (or run `hermes palace up --read-only`) and the plugin rejects every world-to-Hermes write verb
(cron pause, resume, run, and any future one) with a clear refusal, parks nothing for approval, offers no verb on any
object, and shows a `Hermes · read-only` card in the world. `hermes palace status` prints `mode: read-only`.
The default is `false`. If a read-write bridge is already running, `palace up --read-only` stops with an instruction
(`palace down` first) instead of silently keeping write access.

## Installing the engine

The engine is a separate AGPL program and is never bundled. `hermes palace install-engine` (also step 2 of `setup --first-run`)
downloads it **only when you run it**: it reads `engine.lock.json` (release `tag`, `asset`, URL template, `sha256`, `size_bytes`, `entry`
file), shows URL, size and destination, and asks (`--yes` for scripts). The asset is the engine's own release file (`instalar-motor.sh`:
a script header, the line `__CARGA_DEBAJO__`, then a base64 `tar.gz` of `app/` and `www/`); a plain `.tar.gz` or `.zip` with the same
two folders also works. It is never executed: Python reads the payload, checks the sha256 first, then unpacks into
`<hermes home>/plugin-data/hermes-palace/engine/<sha12>/`, writes `engine_path`, and creates a launcher shortcut (`.desktop` on Linux,
`.command` on macOS, `.bat` on Windows; Linux is the tested one). The engine entry is `app/<entry>` (the real release: `packages/server/src/entrada.ts`),
run by Hermes' Node. While `engine.lock.json` says `TODO-PUBLISH` the command refuses: it never downloads an unpinned file.
To pin a release: set `tag`, `sha256` (the `SHA256SUMS` line for `asset`), `size_bytes`, and `entry` if it moved.

## Demo: see your own Hermes as a world

`hermes palace demo` starts the world over the real scan of the current Hermes home, in read-only mode, with profiles
(agents), spend and cron mirrored (session titles stay off). It prints the URL and what is mirrored as counts only.
It does not touch your `config.yaml`: the bridge is started with `--demo`, which forces read-only whatever the settings say.
If a read-write bridge is running it stops and tells you to `palace down` first; if the bridge cannot confirm read-only
within 10 seconds, the demo stops the bridge it started and refuses. The world's own function switches (owner panel)
still decide what is shown.

## Security model

Short form (details: [SECURITY.md](SECURITY.md), [docs/THREAT-MODEL.md](docs/THREAT-MODEL.md)):

- The bridge binds `127.0.0.1` only. It requires a per-start bearer token (constant-time compare), a
  loopback `Host`, no `Origin`, no cross-site `Sec-Fetch-Site`; it caps connections, header and body sizes,
  and the time a request may take, and rejects ambiguous `Content-Length` and any `Transfer-Encoding`.
- Nothing runs by itself: a request from the world is parked and executed only through the
  `palace_cron_action` tool, which Hermes' approval gate asks you to approve. Unattended it fails closed.
- Every subprocess is an argv list (no shell); job ids must match `[a-z0-9][a-z0-9_-]{3,63}` exactly.
- Logs redact the token and the link key and are created mode 0600; the runtime, pid and key files too.
- `capabilities: []`: no LLM calls, no platform actions, no tool override.

## Uninstall

`hermes palace down`, `hermes plugins disable hermes-palace`, delete `<HERMES_HOME>/plugins/hermes-palace/`
and `<HERMES_HOME>/plugin-data/hermes-palace/` (runtime file, key, logs; the world folder is yours and is not removed),
and remove the `plugins.entries.hermes-palace` block from `config.yaml`.

## Development

`make check` runs every gate against a throw-away `HERMES_HOME` (see [CONTRIBUTING.md](CONTRIBUTING.md)).
Documentation index: [docs/INDEX.md](docs/INDEX.md). Licence: MIT, see `LICENSE`.
