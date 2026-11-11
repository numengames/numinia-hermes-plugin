"""Every sentence that is shown INSIDE the Numinia world (Spanish, formal «usted»: the engine's contract).

The engine draws these strings to the people in the world, so they stay Spanish. Each one has an English
twin for Hermes (tool results, logs). ``docs/WORLD-STRINGS.md`` lists them for translators and reviewers;
a test keeps that table and this module identical.
"""

from __future__ import annotations

from typing import Any

# code -> (English for Hermes, Spanish for the world)
MESSAGES: dict[str, tuple[str, str]] = {
    "cron_off": ("the cron function is switched off in Hermes", "la función cron está apagada en Hermes"),
    "verb_not_allowed": ("action not allowed", "acción no permitida"),
    "bad_job_id": ("invalid job identifier", "identificador de tarea no válido"),
    "job_gone": ("that job no longer exists in Hermes", "esa tarea ya no existe en Hermes"),
    "expired_in_world": ("the request had already expired in the world", "la petición ya caducó en el mundo"),
    "replayed": ("that request id was already used", "esa petición ya se usó"),
    "bad_request": ("the request has no id", "la petición no trae identificador"),
    "too_many": ("too many pending requests", "demasiadas peticiones pendientes"),
    "no_approver": ("nobody approved the action in Hermes", "sin aprobador: nadie ha aprobado la acción en Hermes"),
    "expired": ("the request expired", "la petición caducó"),
    "unknown_verb": ("unknown verb", "verbo desconocido"),
    "no_actions": ("Hermes does not accept actions from the world", "Hermes no acepta acciones del mundo"),
    "timeout": ("Hermes did not answer in time", "Hermes no ha respondido a tiempo"),
    "error": ("error", "error"),
    "invalid_request": ("invalid request", "petición no válida"),
    "no_pending": (
        "no pending request from the world for that job",
        "no hay ninguna petición del mundo pendiente para esa tarea",
    ),
    "paused": ("job paused", "tarea pausada"),
    "resumed": ("job resumed", "tarea reanudada"),
    "failed": ("Hermes could not do it", "Hermes no pudo hacerlo"),
    "read_only": (
        "Hermes runs in read-only mode: the world cannot change anything",
        "Hermes está en modo solo lectura (read-only): el mundo no puede cambiar nada",
    ),
    "read_only_card_title": ("Hermes · read-only", "Hermes · read-only"),
    "read_only_card_sub": (
        "Read-only: the world cannot change anything in Hermes",
        "Solo lectura (read-only): el mundo no puede cambiar nada en Hermes",
    ),
    "version_card_title": ("Hermes 3D v{version}", "Hermes 3D v{version}"),
    "version_card_sub": ("Engine {engine} · Hermes {hermes}", "Motor {engine} · Hermes {hermes}"),
    "board_title": ("Work", "Trabajo"),
    "board_sub": (
        "{todo} to do · {doing} in progress · {done} done",
        "{todo} por hacer · {doing} en curso · {done} hecho",
    ),
    "docs_sub": ("Hermes documentation", "Documentación de Hermes"),
    "skills_other": ("other", "otros"),
    "hermes_missing": ("the `hermes` command was not found", "Hermes no está disponible"),
}


def say(code: str, ok: bool = False, detail: str = "") -> dict[str, Any]:
    """``{"ok", "code", "text" (English), "texto" (Spanish, for the world)}``; ``detail`` is appended to both."""
    en, es = MESSAGES[code]
    suffix = f": {detail}" if detail else ""
    return {"ok": ok, "code": code, "text": en + suffix, "texto": es + suffix}
