# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""Which zone of the world a thing goes to.

The engine says which zones exist and which types each one admits in its ``hola`` (``zonas``); that is the source of
truth, because a world's owner may redraw the map. The table below is only the fallback for an engine that announces
no zones (old engines) or none that admits a type: it is the zone map of the engine's ``hermes`` template
(``plantillas/hermes/entorno/espejo.json``, mundo-hermes M6), kept here so the plugin never hard-codes a zone id at
the place it builds a thing.

History: before M6 the plugin put ``perfil`` in ``z4`` and ``uso`` in ``z5`` (proposals). The template settled on
``z6`` for ``perfil`` and ``z2`` for ``uso``; ``z4`` is now the workshop (``habilidad``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# type -> zone id of the engine's ``hermes`` template
FALLBACK_ZONES: dict[str, str] = {
    "salud": "z0",  # El umbral
    "documento": "z1",  # La biblioteca
    "uso": "z2",  # the machine room
    "sesion": "z3",  # Las sesiones
    "habilidad": "z4",  # El taller
    "perfil": "z6",  # La torre de control
    "cron": "z7",  # El reloj
    "mensaje": "z8",  # La plaza
    "proyecto": "z5",  # Projects (C7, English)
    "tablero": "z9",  # El tablero
}


def zone_for(tipo: str, engine_zones: Mapping[str, Any] | None = None) -> str:
    """The zone id for ``tipo``: the first zone (by id) the engine announced that admits it, else the fallback table.

    ``engine_zones`` is ``{zone id: collection of admitted types}`` as the hub keeps it. Unknown type -> ``""``.
    """
    for zid in sorted(engine_zones or {}):
        if tipo in (engine_zones or {})[zid]:
            return zid
    return FALLBACK_ZONES.get(tipo, "")
