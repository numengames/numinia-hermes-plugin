"""Cron adapter: snapshot -> world objects, and snapshot diff -> alta / cambio / baja / lote.

Pure functions (no I/O) so they are tested with real ``hermes cron list --all`` samples.
Zone ``z7`` is "El reloj" in A0 §2.6.
"""

from __future__ import annotations

import logging
from typing import Any

from .protocol import make_object

logger = logging.getLogger("hermes_3d.cronsync")

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
        sub = f"{sub} · next {nxt[:16].replace('T', ' ')}"
    return make_object(
        TYPE_CRON,
        engine_id(job["id"]),
        job.get("name") or "(unnamed)",
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


DETAIL_MAX = 2  # individual job cards per profile; with the summary card that is 3 things, so at most ~3 popups


def _slug(name: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in (name or "default").lower()).strip("-")
    return out[:18] or "default"


def declutter(jobs: list[dict[str, Any]], profile: str = "default", detail_max: int = DETAIL_MAX) -> dict[str, dict[str, Any]]:
    """Quiet view of the cron zone: ONE summary card per profile plus a few individual cards.

    Only the jobs worth a person's eyes get their own card: the ones whose last run failed first, then the
    ones that run soonest (up to ``detail_max`` per profile). Healthy, paused and finished jobs are folded into the
    summary's counts. The summary has no verbs. Pure; the action check still knows every job id.
    """
    full = snapshot(jobs)
    if not full:
        return {}
    counts = {"ok": 0, "fallo": 0, "pausado": 0, "off": 0}
    for obj in full.values():
        e = obj["estado"]
        counts["fallo" if e == "fallo" else "pausado" if e == "pausado" else "ok" if e == "programado" else "off"] += 1
    by_id = {engine_id(j["id"]): j for j in jobs if isinstance(j, dict) and "id" in j}
    failing = [k for k, o in full.items() if o["estado"] == "fallo"]
    upcoming = sorted(
        (k for k, o in full.items() if o["estado"] == "programado"),
        key=lambda k: str(by_id.get(k, {}).get("next_run") or "~"),
    )
    chosen = (failing + upcoming)[: max(0, detail_max)]
    parts = [f"{counts['ok']} healthy"]
    if counts["fallo"]:
        parts.insert(0, f"{counts['fallo']} failing")
    if counts["pausado"]:
        parts.append(f"{counts['pausado']} paused")
    if counts["off"]:
        parts.append(f"{counts['off']} off")
    hidden = len(full) - len(chosen)
    estado = "fallo" if counts["fallo"] else "programado" if counts["ok"] else "pausado" if counts["pausado"] else "apagado"
    sid = f"cron-p-{_slug(profile)}"
    out = {
        sid: make_object(
            TYPE_CRON,
            sid,
            f"{profile} · {len(full)} jobs",
            subtitulo=" · ".join(parts) + (f" · {hidden} folded" if hidden else ""),
            estado=estado,
            zona=ZONE_CRON,
            acciones=[],
        )
    }
    for k in chosen:
        out[k] = full[k]
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
