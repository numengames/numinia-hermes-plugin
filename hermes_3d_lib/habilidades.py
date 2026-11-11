"""Mirror ``habilidades``: the skills of every Hermes profile as ``habilidad`` things (the workshop, zone z4).

Source: the scan (``scan.scan_skills``): name, group (the folder under ``skills/``), enabled, description cut to 140.
Published ONLY behind ``funciones.habilidades`` (off by default). Read-only: no verbs. The skill's instructions are
never read or published, only the front matter's ``name`` and ``description``.

Contract (QA/NECESIDADES-MOTOR.md, 2026-11-05 M6): ``id`` <= 32 (slug, collisions resolved here), ``titulo`` <= 60,
``grupo`` 1-32 bytes (at most ~8 groups: the rest are merged into «otros»), ``descripcion`` <= 200 bytes,
``estado`` activo | apagado | fallo.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from .crontable import clean_text
from .protocol import MAX_OBJECTS_PER_BATCH, make_object
from .worldtext import say
from .zones import FALLBACK_ZONES

logger = logging.getLogger("hermes_3d.habilidades")

TYPE_HABILIDAD = "habilidad"
ZONE_HABILIDADES = FALLBACK_ZONES[TYPE_HABILIDAD]
MAX_GROUPS = 8  # the workshop grows to the south with every group; ~8 is what the engine note recommends
DESCRIPTION_CARD = 140
_SLUG_BAD = re.compile(r"[^a-z0-9._-]+")


def slug(name: str) -> str:
    """Engine id from a skill name: lowercase [a-z0-9._-], starts with a letter or digit, at most 32."""
    s = _SLUG_BAD.sub("-", str(name).lower()).strip("-._")
    return s[:32].rstrip("-._")


def unique_ids(names: list[str]) -> dict[str, str]:
    """name -> unique engine id. The first holder of a slug keeps it; a collision (or an empty slug) gets a stable
    hash suffix (``<slug>-<4 hex of sha256(name)>``), still <= 32."""
    out: dict[str, str] = {}
    taken: set[str] = set()
    for name in sorted(names):
        base = slug(name)
        cand = base
        if not cand or cand in taken:
            h = hashlib.sha256(name.encode("utf-8")).hexdigest()[:4]
            cand = f"{base[: 32 - 5].rstrip('-._')}-{h}" if base else f"skill-{h}"
            n = 0
            while cand in taken:  # a 16-bit suffix can collide too: extend deterministically
                n += 1
                cand = f"{base[: 32 - 9].rstrip('-._')}-{hashlib.sha256(f'{name}{n}'.encode()).hexdigest()[:8]}"
        taken.add(cand)
        out[name] = cand
    return out


def _fold_groups(items: list[dict[str, Any]]) -> dict[str, str]:
    """group -> group to publish: the ``MAX_GROUPS - 1`` biggest keep their name, the rest become «otros»."""
    sizes: dict[str, int] = {}
    for it in items:
        sizes[it["grupo"]] = sizes.get(it["grupo"], 0) + 1
    if len(sizes) <= MAX_GROUPS:
        return {g: g for g in sizes}
    keep = sorted(sizes, key=lambda g: (-sizes[g], g))[: MAX_GROUPS - 1]
    other = say("skills_other")["texto"]
    return {g: (g if g in keep else other) for g in sizes}


def skill_state(item: dict[str, Any]) -> str:
    if item.get("legible") is False:
        return "fallo"
    return "activo" if item.get("activo", True) else "apagado"


def snapshot_from_scan(section: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``scan['sections']['skills']`` -> id -> object. An errored/empty section gives ``{}``."""
    items = [i for i in (section or {}).get("items", []) or [] if isinstance(i, dict) and i.get("nombre")]
    items = items[:MAX_OBJECTS_PER_BATCH]  # the engine admits 200 per zone
    names = [str(i["nombre"]) for i in items]
    ids = unique_ids(names)
    groups = _fold_groups(items)
    other = say("skills_other")["texto"]
    out: dict[str, dict[str, Any]] = {}
    for it in items:
        try:
            name = str(it["nombre"])
            obj = make_object(
                TYPE_HABILIDAD,
                ids[name],
                name,
                estado=skill_state(it),
                zona=ZONE_HABILIDADES,
                acciones=[],
                grupo=groups.get(it.get("grupo", ""), other),
                descripcion=clean_text(it.get("descripcion", ""), DESCRIPTION_CARD),
            )
            out[obj["id"]] = obj
        except (KeyError, TypeError, ValueError, AttributeError):  # one odd skill must not hide the rest
            logger.debug("skipping a skill that cannot be shown", exc_info=True)
    return out
