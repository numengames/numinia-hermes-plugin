"""Bodies for the bots: one black-and-white stick figure per Hermes profile, walking a short round.

Reuses the engine's own piece for people, ``pnj@1`` with ``ronda`` (docs/extender/misiones-y-personas-pnj.md): the plugin
only writes ``<world>/aventura/pnj/cuerpo-<profile>.json`` and copies the figure to ``<world>/datos/activos/<sha256>.glb``
(CC0, ``assets/muneco-palo.LICENCIA.md``). The engine reads both at start, so this runs before it starts.

PARTIAL by design (decision on t_a55cdae9): the round is fixed. ``pnj@1`` conditions (``si``) only look at a player's
quests, not at the live state of a profile, so "a working agent goes to its post" and "an idle agent rests" need an
engine piece that does not exist yet (request in docs/CUERPOS.md). Until then the figures walk their round always.

Stdlib only. Files not named ``cuerpo-*.json`` are never touched.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("hermes_3d.cuerpos")

MAX_BODIES = 14
PREFIX = "cuerpo-"
ASSET = Path(__file__).resolve().parents[1] / "assets" / "muneco-palo.glb"
_ID = re.compile(r"[^a-z0-9-]")

# z6 "La torre de control" is centred on (30, -30), 24 x 24 m: x 18..42, z -42..-18. The profile things stand in it, so the
# figures keep to the front aisle: two rows of seven, each figure pacing 2.4 m on its own stretch (spacing 3.4 m).
ROW_Z = (-20.5, -22.5)
FIRST_X = 19.7
SPACING = 3.4
HALF_STRETCH = 1.2
PER_ROW = 7


def body_id(profile: str) -> str:
    """Engine id of a body: ``cuerpo-<profile>`` lowercased, anything outside [a-z0-9-] becomes ``-``, at most 40 chars."""
    return (PREFIX + _ID.sub("-", str(profile).lower()))[:40]


def asset_sha(path: Path = ASSET) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def body_for(profile: str, index: int, sha: str, spend: str = "", activity: dict[str, str] | None = None) -> dict[str, Any]:
    """The ``pnj@1`` of one body. ``index`` (0..13) fixes its spot; speed and pauses differ a little so the 14 do not move as one."""
    row, col = divmod(index, PER_ROW)
    act = activity or {}
    x = round(FIRST_X + col * SPACING, 2)
    z = ROW_Z[row % len(ROW_Z)]
    a, b = round(x - HALF_STRETCH, 2), round(x + HALF_STRETCH, 2)
    return {
        "formato": "pnj@1",
        "id": body_id(profile),
        "nombre": str(profile)[:40],
        "oficio": "Hermes agent",
        "posicion": {"x": a, "z": z},
        "giro": 90,
        "radio": 2,
        "apariencia": {"modelo": sha, "escala": 1},
        "posiciones": [
            {
                "ronda": {
                    "modo": "ida_y_vuelta",
                    "velocidad": round(0.5 + 0.04 * (index % 5), 2),
                    "puntos": [
                        {"x": a, "z": z, "giro": 90, "pausa": 3 + index % 4},
                        {"x": b, "z": z, "giro": -90, "pausa": 4 + index % 3},
                    ],
                }
            }
        ],
        "dialogo": {
            "entradas": [{"nodo": "hola"}],
            "nodos": {
                "hola": {
                    "texto": f"{str(profile)[:40]} · Hermes profile."
                    + (f" Doing: {act['haciendo']}." if act.get("haciendo") else "")
                    + (f" Done: {act['hecho']}." if act.get("hecho") else "")
                    + (f" Spend, last period: {spend}." if spend else ""),
                    "opciones": [{"texto": "Close.", "accion": {"tipo": "despedir"}}],
                }
            },
        },
    }


def plan(
    profiles: list[str],
    sha: str,
    spend: dict[str, str] | None = None,
    activity: dict[str, dict[str, str]] | None = None,
) -> dict[str, dict[str, Any]]:
    """``{file name: pnj@1}`` for the first ``MAX_BODIES`` distinct profile names (sorted, so spots are stable)."""
    out: dict[str, dict[str, Any]] = {}
    for name in sorted({str(p) for p in profiles if str(p)}):
        if len(out) >= MAX_BODIES:
            logger.info("more than %d profiles: the rest get no body", MAX_BODIES)
            break
        body = body_for(name, len(out), sha, (spend or {}).get(name, ""), (activity or {}).get(name))
        if body["id"] + ".json" in out:  # two names that clean to the same id: keep the first
            continue
        out[body["id"] + ".json"] = body
    return out


def _write_if_changed(path: Path, data: bytes) -> bool:
    try:
        if path.read_bytes() == data:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return True


def sow(
    world: Path,
    profiles: list[str],
    asset: Path = ASSET,
    spend: dict[str, str] | None = None,
    activity: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Write the figure and one ``pnj@1`` per profile into ``world``; drop ``cuerpo-*`` files of profiles that are gone.

    Idempotent. Returns ``{"written": n, "removed": n, "bodies": n, "sha256": ...}`` or ``{"skipped": reason}``.
    Never raises for a missing figure or a read-only world (the world just has no bodies).
    """
    if not asset.is_file():
        return {"skipped": f"{asset.name} not found"}
    try:
        sha = asset_sha(asset)
        pnj_dir = world / "aventura" / "pnj"
        wanted = plan(profiles, sha, spend, activity)
        changed = int(_write_if_changed(world / "datos" / "activos" / f"{sha}.glb", asset.read_bytes()))
        for fname, body in wanted.items():
            changed += int(_write_if_changed(pnj_dir / fname, (json.dumps(body, ensure_ascii=False, indent=2) + "\n").encode()))
        removed = 0
        if pnj_dir.is_dir():
            for f in pnj_dir.glob(PREFIX + "*.json"):
                if f.name not in wanted:
                    f.unlink()
                    removed += 1
        return {"written": changed, "removed": removed, "bodies": len(wanted), "sha256": sha}
    except OSError as exc:
        logger.warning("could not write the bodies: %s", type(exc).__name__)
        return {"skipped": f"{type(exc).__name__}"}
