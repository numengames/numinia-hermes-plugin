# Threat model

Assets: the bridge token, the link key, your Hermes data (read by `scan`), the ability to pause/resume cron
jobs, the integrity of processes the plugin starts or signals.

Attackers considered: (1) a web page in your browser; (2) another local user or process on the machine;
(3) a malicious or buggy engine/world; (4) malformed data in your own Hermes files (names, ids).
Not considered: an attacker who already runs code as your user or can write `<HERMES_HOME>`.

| # | Threat | Mitigation | Test |
|---|---|---|---|
| T1 | DNS rebinding to the bridge | `Host` must be `127.0.0.1`/`localhost` (+ our port) → 421 | `test_host_header_must_be_loopback_defeating_dns_rebinding` |
| T2 | Cross-site request / CSRF from a page | any `Origin` → 403; `Sec-Fetch-Site` other than `none` → 403; POST needs `application/json` | `test_browser_initiated_requests…`, `test_any_origin_is_refused…`, `test_post_requires_json_content_type` |
| T3 | Token guessing / timing | 128-bit random token, compared as SHA-256 digests with `hmac.compare_digest` | `test_token_is_compared_in_constant_time` |
| T4 | Slowloris / connection exhaustion | 32 connections max (503), overall read deadline (408), header and body caps | `test_connection_cap…`, `test_slowloris…`, `test_idle_connection…` |
| T5 | Request smuggling | duplicate or malformed `Content-Length` → 400; any `Transfer-Encoding` → 400; HTTP/1.1 only | `test_duplicate_content_length…`, `test_transfer_encoding…` |
| T6 | Oversized input | header block 8 KB, 40 headers, path 512, body 32 KB | `test_oversized_declared_body…`, `test_header_block_size…` |
| T7 | Secret in logs | redacting filter (known values + shapes); logs 0600 | `test_every_log_line_is_redacted…` |
| T8 | Shell/argument injection via job id | argv list, no shell; id must `fullmatch` `[a-z0-9][a-z0-9_-]{3,63}` | `test_job_ids_are_validated_strictly…`, `test_no_shell_anywhere_in_the_code` |
| T9 | Unapproved cron action by the world | request parked; executed only via tool behind Hermes' approval gate; fails closed unattended; run-now not offered | `test_world_pause_request_waits_for_approval…` |
| T10 | Signalling an unrelated process (pid reuse) | pid file holds start time + cmdline; verified before every signal | `test_a_recycled_pid_is_never_signalled` |
| T11 | Weak or foreign key handling | key from env or own 0600 file; loose/symlinked key file rejected; never from `enlace.env` | `tests/test_linkkey.py` |
| T12 | World folder abuse (symlink, `..`, `/`, `$HOME`) | `checked_folder` refuses them; fixed files opened with `O_NOFOLLOW` | `test_world_path_refuses…`, `test_world_files_are_not_read_through_a_symlink` |
| T13 | Scan modifies or leaks data | SQLite `mode=ro`; only counts/names leave; MCP commands/URLs/env never read | `tests/test_scan.py` |
| T14 | Engine inherits our secrets | engine and bridge get allow-listed environments | `test_engine_environment_serves_the_page_and_carries_the_key` |
| T15 | The world changes something in Hermes (cron pause/resume/run, any future write verb) | `read_only: true` (or `hermes 3d up --read-only`) makes the bridge answer every world request with a refusal, queue nothing, answer 403 on `/action/*`, publish no verbs on any object, and show a `read-only` card in the world; the Hermes-side tool refuses before touching the bridge, and `hermes 3d up --read-only` refuses to reuse a read-write bridge | `tests/test_read_only.py`, integration step 11 |
| T16 | `install-engine` downloads something unexpected, or unpacks something that writes outside its folder | user-initiated only; refuses unless `engine.lock.json` pins a tag and a 64-hex sha256 (`TODO-PUBLISH` = refuse); confirmation before any download (`--yes` skips only that); https only (http to loopback for tests); size cap while reading; sha256 and length checked **before** unpacking, partial folder deleted on any failure; unpacked in Python, the asset is never executed; only `app/`+`www/`, no `..`/absolute/backslash names, no hard link/device/fifo, no symlink leaving the folder, no write through a link, setuid/setgid dropped, 400 MB / 20 000 entries cap; refuses to run as root; idempotent by sha256. Accepted residual risk: whoever controls the pinned release AND its checksum line in `engine.lock.json` controls the code that runs; the lock is in the plugin, reviewed with it | `tests/test_installer.py` (good, bad sum, truncated, oversize, traversal, absolute, symlink, hard link, device, fifo, zip symlink, root, idempotent) |
| T17 | Node: a hostile or old `node` on `PATH` runs instead of Hermes' | Node comes from `pm.ensure("node")` (pinned and checksummed by Hermes); `PATH` is consulted only when `pm` cannot be imported; if `pm` is there but gives no Node the plugin stops with `NODE_MISSING` instead of falling back; the child gets an allow-listed environment | `tests/test_nodebin.py` |
| T18 | The engine child leaks its secrets or outlives the plugin / kills the wrong process | `MOTOR_SECRET`/`MOTOR_ADMIN` per install in a 0600 file, passed in the environment only, never in argv, logs (tail is redacted) or output; stdin pipe held by the bridge so an engine that supports `MOTOR_SALIR_SI_STDIN_CIERRA=1` exits with it; PID file stores start time and command line, a recycled PID is never signalled, stale files are dropped on start; port busy/clash stops before launching | `tests/test_engine_child.py` |
| T17 | A local client floods the bridge | per-client token bucket (peer address; 50 req/s, burst 100, at most 64 clients tracked) → `429` with `Retry-After`, on top of the connection cap (T4) | `tests/test_rate_limit.py` |
| T18 | Replay of a captured world request (`peticion`) | the handshake nonce is chosen by the engine and single use, but later frames are not signed; the bridge honours each request `id` once in a 10-minute window (bounded to 512 ids) and refuses a missing id | `tests/test_replay.py` |
| T19 | Direct reads of Hermes' private databases | `allow_direct_db_read` is off by default; nothing is opened without it; only aggregate queries on `sessions`/`tasks`, never `messages`; re-queried only when the file (or its `-wal`) changed | `tests/test_sources.py`, `tools/audit_scan_sql.py` |
| T20 | The activity feed leaks prompts, arguments or results | events are built from an allow-list (kind, platform, profile, ids, tool NAME, status, duration); the spool is 0600, bounded to 64 KB, written only while the sessions switch is on | `tests/test_activity.py` |

Residual risks: any local process of the same user can read `runtime.json`/`link.key` (0600 stops *other*
users only); the engine is trusted to enforce its own limits; the Node engine is not sandboxed.

## The link: who connects to whom, and who is "the person"

* **Direction.** The plugin opens the connection, to the engine's own link port on `127.0.0.1` (no `Origin`). The engine never connects to Hermes. Default and only supported deployment: both on the same machine.
* **What the handshake proves.** The engine sends a random single-use nonce; the plugin answers `HMAC-SHA256(key, "numinia-enlace@1\n" + nonce)`. So the engine learns that the peer knows the key, and a captured signature is useless against a later nonce. The key never travels. It does NOT prove the engine to the plugin (a process that grabs the port first can play the engine) and later frames carry no MAC, no timestamp and no sequence number.
* **What a fake or malicious engine can do.** Publish objects (the plugin only ever sends metadata), and send `peticion` frames. A `peticion` is never executed: with `read_only` it is refused outright; otherwise it is parked and runs only if a person approves it inside Hermes (its approval prompt names the job, the verb and the claimed requester). It can ask for at most the verbs in the allow-list (`cron.pausar`, `cron.reanudar`) on jobs that exist.
* **Owner binding (honest limit).** `quien: {id, nombre}` is the *engine's claim* about who touched the object. The plugin shows it in the approval prompt and does nothing else with it: it is display text, not authentication. In a single-owner local world that is acceptable because the approver is the person who owns this Hermes and the engine runs as them. In a shared or hosted world it is NOT enough: a visitor could be named as anyone. The protection that holds either way is that the owner's own Hermes approval is the only thing that executes an action. Binding the request to the owner's identity before that prompt needs the engine to sign the request (see `QA/NECESIDADES-MOTOR.md`, "Signed requests"); until then `read_only: true` is the right setting for any world other people can enter.
* **Replay.** Within one connection the bridge refuses a repeated request `id` (T18). Across connections the nonce is fresh each time. A captured frame stream cannot be replayed into a new session, but on a hostile loopback an attacker who can already read the socket can do more than replay.
* **Hosted plaza (not supported yet).** Would need: TLS (`wss://`) with a pinned certificate or a mutually authenticated tunnel; one key per world; a signed envelope per frame with `ts` + `nonce` + `seq` inside the HMAC and a small accepted clock skew; a server-side rate limit; and the owner identity carried and signed by the engine. None of this exists; the engine URL is built from the literal `127.0.0.1` plus a port (`bridge_main.py`), and there is no setting for another host.


## Path A: the world inside Desktop (embed credential)

* **The credential** `e1.<exp>.<nonce>.<hmac>` is the only thing that authenticates the framed page to the engine. It is made per page open
  from the link key (which never leaves the plugin process and is never returned), lives 1 h by default (24 h at most, engine-enforced),
  and travels in the URL fragment: no server, log or `Referer` sees it. Whoever holds it can act as the machine's owner for that world until it expires.
* **Why `Origin: null` is not trusted.** Every sandboxed or `data:` page on any site sends `Origin: null`; it proves nothing. The engine admits a
  request only with embed mode on, from loopback without forwarding headers, with a valid unexpired credential. The page only hears `window.parent`.
* **Owner-only in local mode.** The embedded page is the machine owner by construction (`quien.nombre` is "dueño local"). That is acceptable only
  because the engine is on loopback; do not use path A against a remote engine.
* **The parent is a messenger.** `plugin.js` ignores any message whose `source` is not the frame's window, requires `v:1`, caps size (16 KiB),
  rate (burst 5, 2/s) and in-flight requests (8), and sends only known fields to `/action`.
* **Requests keep the approval road.** `POST /action` runs `hub.answer`, the verdict path of a link `peticion`: read-only first, unknown verbs refused,
  an id works once, and nothing runs until the approval gate says yes; with nobody to approve it fails closed (proved with a real Desktop).
* **Residual.** A frame cannot be told apart from another sandboxed page by origin; the 1 h credential limits a leak. The Desktop `postMessage`
  target must be `*` (opaque origin), so the parent never sends secrets to the frame.
