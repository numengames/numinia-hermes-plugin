# Strings shown inside the world (Spanish, formal «usted»)

The engine displays these to people in the world, so they stay Spanish. Kept identical to
`hermes_3d_lib/worldtext.py` by `tests/test_docs.py`.

## Replies to world requests (`worldtext.MESSAGES`)

| Code | Spanish (shown in the world) | English (Hermes tool result, logs) |
|---|---|---|
| `cron_off` | la función cron está apagada en Hermes | the cron function is switched off in Hermes |
| `verb_not_allowed` | acción no permitida | action not allowed |
| `bad_job_id` | identificador de tarea no válido | invalid job identifier |
| `job_gone` | esa tarea ya no existe en Hermes | that job no longer exists in Hermes |
| `expired_in_world` | la petición ya caducó en el mundo | the request had already expired in the world |
| `replayed` | esa petición ya se usó | that request id was already used |
| `bad_request` | la petición no trae identificador | the request has no id |
| `too_many` | demasiadas peticiones pendientes | too many pending requests |
| `no_approver` | sin aprobador: nadie ha aprobado la acción en Hermes | nobody approved the action in Hermes |
| `expired` | la petición caducó | the request expired |
| `unknown_verb` | verbo desconocido | unknown verb |
| `no_actions` | Hermes no acepta acciones del mundo | Hermes does not accept actions from the world |
| `timeout` | Hermes no ha respondido a tiempo | Hermes did not answer in time |
| `error` | error | error |
| `invalid_request` | petición no válida | invalid request |
| `no_pending` | no hay ninguna petición del mundo pendiente para esa tarea | no pending request from the world for that job |
| `paused` | tarea pausada | job paused |
| `resumed` | tarea reanudada | job resumed |
| `failed` | Hermes no pudo hacerlo | Hermes could not do it |
| `hermes_missing` | Hermes no está disponible | the `hermes` command was not found |
| `board_title` | Trabajo | Work |
| `board_sub` | {todo} por hacer · {doing} en curso · {done} hecho | {todo} to do · {doing} in progress · {done} done |
| `docs_sub` | Documentación de Hermes | Hermes documentation |
| `skills_other` | otros | other |
| `read_only` | Hermes está en modo solo lectura (read-only): el mundo no puede cambiar nada | Hermes runs in read-only mode: the world cannot change anything |
| `read_only_card_title` | Hermes · read-only | Hermes · read-only |
| `read_only_card_sub` | Solo lectura (read-only): el mundo no puede cambiar nada en Hermes | Read-only: the world cannot change anything in Hermes |

## Card text built by the plugin (engine objects)

| Where | Text | Why Spanish |
|---|---|---|
| `cronsync.py` | `próxima <date>` (subtitle), `(sin nombre)` | card subtitle/title in the world |
| `agentes.py` | `activo` / `apagado` / `desconocido` (state), `gateway activo/parado/¿?`, `sin modelo`, `(sin nombre)` | card state and subtitle |
| `sessions.py` | `Sesión <id>`, `sin-id` | card title |
| `protocol.py` | `(sin nombre)` | fallback title |
| object types and verbs | `cron`, `sesion`, `perfil`, `uso`; `cron.pausar`, `cron.reanudar` | the engine's schema (`features/espejo/esquema.ts`) |
| JSON keys of frames | `lote`, `alta`, `cambio`, `baja`, `objeto`, `titulo`, `subtitulo`, `estado`, `zona`, `acciones`, `texto` … | the engine's wire format |
