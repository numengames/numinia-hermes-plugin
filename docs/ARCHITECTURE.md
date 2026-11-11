# Architecture

```
 Hermes process                       bridge process (one per profile)           Numinia Engine (separate program)
 ┌────────────────────┐   HTTP        ┌────────────────────────────┐   WebSocket  ┌─────────────────────────┐
 │ __init__.py        │  127.0.0.1    │ bridge_main.py  Bridge      │  127.0.0.1   │ link ("espejo")          │
 │  register(ctx)     │ ───────────▶  │ hub.py  Hub (HTTP + link)   │ ───────────▶ │ packages/server          │
 │  tools, hooks, CLI │  bearer token │ cron / scan / sessions poll │  HMAC login  │ serves WWW_DIR page      │
 │ dashboard/…api.py  │               └────────────────────────────┘              └─────────────────────────┘
 │ desktop/plugin.js  │
 └────────────────────┘
```

## Processes
- **Hermes** loads `__init__.py`: tools `hermes3d_state` (read-only) and `hermes3d_cron_action`, the hooks (session
  start/end/finalize, `pre_tool_call` approval gate) and the `hermes 3d` CLI.
- **Bridge** (`python -m hermes_3d_lib.bridge_main`) is started detached by `hermes 3d up`. It owns the HTTP
  face (`Hub`), the connection to the engine (the plugin is always the client), the cron poll and the scan.
- **Engine** is started by `supervisor.start_engine` only if its link is not already listening. Its folder is
  the `engine_path` setting; it receives `MOTOR_MUNDO`, `MOTOR_PLANTILLA=hermes`, `PORT`, `HOST`,
  `MOTOR_ENLACE_PUERTO`, `MOTOR_ENLACE_CLAVE` and `WWW_DIR` and an allow-listed copy of `PATH`, `HOME`, …

## Modules
| Module | Responsibility |
|---|---|
| `settings.py` | Typed settings from `config.yaml` (via `ctx.get_config` or direct read), validation, derived paths |
| `linkkey.py` | Link key: env var, else plugin-owned 0600 file; format validation |
| `config.py` | Locations under `<HERMES_HOME>/plugin-data/hermes-3d/`, runtime/pid files, owner-only helpers |
| `procid.py` | Process identity (start time + command line) so a recycled pid is never signalled |
| `supervisor.py` | Locate/start/stop the engine, spawn the bridge; world-folder checks |
| `hub.py` | Local HTTP face (hardened) and the link client with reconnect/backoff |
| `bridge_main.py` | The bridge: wires hub, cron poll, scan, sessions, approvals |
| `protocol.py` | Frame builders/validators for the engine's wire format |
| `actions.py` | Pending world requests, approval hand-off, `run_cron_cli` (argv list) |
| `cronsync.py`, `crontable.py`, `sessions.py`, `agentes.py`, `gasto.py`, `scan.py` | Read Hermes, build engine objects |
| `logsafe.py` | 0600 log file + redacting filter |
| `worldtext.py` | The Spanish sentences the world displays, with English twins |

## Files (`<HERMES_HOME>/plugin-data/hermes-3d/`, dir 0700, files 0600)
`runtime.json` (pid, port, token, URLs; removed on stop), `engine.pid` (pid + start + cmdline of the engine we
started), `link.key`, `bridge.log`, `engine.log`.

## Link protocol (engine docs/EXTENDER.md, "Espejo (v1)")
Plugin opens the connection; engine sends `reto{nonce}`; plugin answers `hola` with
`HMAC-SHA256(key, "numinia-enlace@1\n" + nonce)`; the key never travels. Then `lote/alta/cambio/baja`
(objects), `peticion` (engine → plugin) and `resultado` (plugin → engine). Frames ≤ 16 KB; title ≤ 60 and
subtitle ≤ 120 bytes.
The engine may answer a `lote` with `ack{en:'lote', puestas, quitadas, descartadas:[{tipo,id,motivo}], mas}` when it left
objects out (unknown zone, zone without that type). The hub keeps the last one (bounded to 20 items, text cut and
redacted), logs a warning and exposes it as `dropped` in `/status` and `hermes 3d status`. Engines that never send
it are unaffected; a reconnect clears it.

## Decisions
- **Key ownership (rule A10).** Chosen: plugin-generated key handed over as `MOTOR_ENLACE_CLAVE`, or the
  env var for engines we did not start. Rejected: reading `<world>/enlace.env` (another program's secret file).
- **Settings only in `config.yaml`.** One source, one precedence, visible in the Desktop; no env overrides.
- **Bridge is a process, not a thread.** It must outlive a Hermes restart and must not run inside the gateway.


## Path A and path B (the world inside Desktop, or beside it)

```
Path A  (local, Desktop, owner-only)               Path B  (hosted worlds, anything that can open a WebSocket)

 Hermes Desktop                                     hermes 3d up
 ├─ plugin.js  /hermes-3d page  ──ctx.rest──► plugin_api.py            bridge_main ──HMAC WebSocket──► engine
 │    │  ▲   snapshot/event/funciones            ├ POST /embed-credential (mint e1.…)                 (one source)
 │    │  │   postMessage (v:1, 16 KiB, rate)     ├ GET  /world-snapshot   (link serializers)
 │    ▼  │                                       └ POST /action ─► hub.answer ─► approval gate ─► cron
 │  SandboxedFrame  (opaque origin)
 │    └─ the world (engine page, ?embed=1#embed=e1.…) ──POST /espejo/embed (credential)──► engine
 └─ plugin.hermes-3d.* events (hermes serve) ──► the page refreshes the picture
```

Both paths use the same serializers and the same `hub.answer` verdicts. An engine takes **one** source: a connected local link
displaces the embedded page, so path A runs with `engine_link: false`. The page is a messenger, never an authority.
