"""Cron adapter: snapshot -> world objects, and snapshot diff -> alta / cambio / baja / lote.

Pure functions (no I/O) so they are tested with real ``hermes cron list --all`` samples.
Zone ``z7`` is "El reloj" in A0 §2.6.
"""

from __future__ import annotations

import logging
from typing import Any

from .protocol import make_object

logger = logging.getLogger("hermes_palace.cronsync")

ZONE_CRON = "z7"


def engine_id(hermes_id: str) -> str:
    """Hermes job ids are 12 hex chars; the engine wants lowercase [a-z0-9._-] up to 32."""
    return str(hermes_id).lower()


TYPE_CRON = "cron"


def job_to_object(job: dict[str, Any]) -> dict[str, Any]:
    """One parsed job -> an ``objeto`` of type ``cron``.

    The title is the job name (untrusted, already cleaned and cut by the parser). Only the verbs
    that make sense in the current state are offered; ``cron.ejecutar`` is never offered in v0
    (it launches a real agent run; A0 §3.5 puts it behind explicit approval, F0 keeps it for later).
    """
    estado = job.get("estado", "desconocido")
    if estado == "programado":
        verbs = ["cron.pausar"]
    elif estado == "pausado":
        verbs = ["cron.reanudar"]
    else:
        verbs = []
    if estado == "terminado":
        estado = "apagado"  # the engine has no "terminado": a finished one-shot is simply off
    if estado == "programado" and job.get("ultimo") == "fallo":
        estado = "fallo"  # the last run failed; the job is still scheduled, so keep its verb
    sub = job.get("schedule", "")
    nxt = job.get("next_run", "")
    if nxt and nxt != "None" and estado == "programado":
        sub = f"{sub} · próxima {nxt[:16].replace('T', ' ')}"
    return make_object(
        TYPE_CRON,
        engine_id(job["id"]),
        job.get("name") or "(sin nombre)",
        subtitulo=sub,
        estado=estado,
        zona=ZONE_CRON,
        acciones=verbs,
    )


def snapshot(jobs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """id -> object, in listing order."""
    out: dict[str, dict[str, Any]] = {}
    for job in jobs:
        try:
            obj = job_to_object(job)
            out[obj["id"]] = obj
        except (KeyError, TypeError, ValueError, AttributeError):  # one odd job must not hide the rest
            logger.debug("skipping a job that cannot be shown", exc_info=True)
    return out


def diff(old: dict[str, dict[str, Any]] | None, new: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Messages (without envelope ``v``) that turn the world from ``old`` into ``new``.

    * ``old is None`` (first publish or after a reconnect): one ``lote`` with everything.
    * otherwise ``alta`` for new ids, ``cambio`` when any visible field changed, ``baja`` for gone ids.
    """
    if old is None:
        return [{"t": "lote", "tipo": TYPE_CRON, "objetos": list(new.values())}]
    msgs: list[dict[str, Any]] = []
    for key, obj in new.items():
        if key not in old:
            msgs.append({"t": "alta", "objeto": obj})
        elif old[key] != obj:
            msgs.append({"t": "cambio", "objeto": obj})
    for key, obj in old.items():
        if key not in new:
            msgs.append({"t": "baja", "objeto": {"tipo": obj["tipo"], "id": obj["id"]}})
    return msgs
