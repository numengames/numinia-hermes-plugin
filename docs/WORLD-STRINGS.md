<!--
SPDX-FileCopyrightText: 2026 Numen Games S.L.
SPDX-License-Identifier: CC-BY-4.0
-->

# Strings shown inside the world (English)

The engine displays these to people in the world, and they are all English. Only the engine's schema (object types,
verbs and JSON keys) stays Spanish. Kept identical to
`hermes_3d_lib/worldtext.py` by `tests/test_docs.py`.

## Replies to world requests (`worldtext.MESSAGES`)

| Code | English (shown in the world, tool results, logs) |
|---|---|
| `cron_off` | the cron function is switched off in Hermes |
| `verb_not_allowed` | action not allowed |
| `bad_job_id` | invalid job identifier |
| `job_gone` | that job no longer exists in Hermes |
| `expired_in_world` | the request had already expired in the world |
| `replayed` | that request id was already used |
| `bad_request` | the request has no id |
| `too_many` | too many pending requests |
| `no_approver` | nobody approved the action in Hermes |
| `expired` | the request expired |
| `unknown_verb` | unknown verb |
| `no_actions` | Hermes does not accept actions from the world |
| `timeout` | Hermes did not answer in time |
| `error` | error |
| `invalid_request` | invalid request |
| `no_pending` | no pending request from the world for that job |
| `paused` | job paused |
| `resumed` | job resumed |
| `failed` | Hermes could not do it |
| `hermes_missing` | the `hermes` command was not found |
| `board_title` | Work |
| `board_sub` | {todo} to do · {doing} in progress · {done} done |
| `docs_sub` | Hermes documentation |
| `skills_other` | other |
| `read_only` | Hermes runs in read-only mode: the world cannot change anything |
| `read_only_card_title` | Hermes · read-only |
| `read_only_card_sub` | Read-only: the world cannot change anything in Hermes |
| `version_card_title` | Hermes 3D v{version} |
| `version_card_sub` | Engine {engine} · Hermes {hermes} |

## Card text built by the plugin (engine objects)

| Where | Text | What it is |
|---|---|---|
| `cronsync.py` | `next <date>` (subtitle), `(unnamed)` (title), `<profile> · N jobs` + `N failing · N healthy · N paused · N folded` (summary card): English, like the C7 projects zone | card subtitle/title in the world |
| `agentes.py` | `gateway up/down/?`, `no model`, `(unnamed)`, `Doing:`, `Done:` (the state itself stays the engine's enum: `activo` / `apagado` / `desconocido`) | card subtitle and title (Doing/Done = latest Kanban card running / done for that profile; only in the bytes left over) |
| `sessions.py` | `Session <id>`, `no-id` | card title |
| `protocol.py` | `(unnamed)` | fallback title |
| `gasto.py` | `Spend · <profile>`, `cache`, `sessions`, `est.` | card title and subtitle |
| object types and verbs | `cron`, `sesion`, `perfil`, `uso`; `cron.pausar`, `cron.reanudar` | the engine's schema (`features/espejo/esquema.ts`) |
| JSON keys of frames | `lote`, `alta`, `cambio`, `baja`, `objeto`, `titulo`, `subtitulo`, `estado`, `zona`, `acciones`, `texto` … | the engine's wire format |
