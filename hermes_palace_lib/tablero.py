"""Mirror ``kanban``: the work board as ONE ``tablero`` thing (zone z9), read-only.

Source: the scan (``scan.scan_kanban``), which opens ``kanban.db`` read-only and keeps, for the board with most open
cards, per column the count and at most 3 titles (redacted, 32 bytes). Card bodies, comments, results and assignees
never reach this module. Published ONLY behind ``funciones.kanban`` (off by default).

Columns (the plugin's decision, M6 contract): hacer = triage/todo/ready, curso = running/blocked, hecho = done inside
the scan window. The ``tablero`` field is exactly ``{"hacer": {"n", "top"}, "curso": {...}, "hecho": {...}}``.
"""

from __future__ import annotations

from typing import Any

from .logsafe import fit_title
from .protocol import make_object
from .worldtext import MESSAGES
from .zones import FALLBACK_ZONES

TYPE_TABLERO = "tablero"
ZONE_TABLERO = FALLBACK_ZONES[TYPE_TABLERO]
BOARD_ID = "trabajo"  # one single thing
COLUMNS = ("hacer", "curso", "hecho")
TOP = 3  # the engine refuses more than 3 titles per column
MAX_N = 99999


def board_field(columnas: dict[str, Any]) -> dict[str, Any]:
    """Columns from the scan -> the exact ``tablero`` value: three columns, no extra keys, ``n`` in 0..99999, <= 3 titles
    of <= 32 bytes with no quote or backslash (re-cleaned here: this is the last stop before the world)."""
    out: dict[str, Any] = {}
    for col in COLUMNS:
        raw = columnas.get(col) if isinstance(columnas, dict) else None
        raw = raw if isinstance(raw, dict) else {}
        n = raw.get("n")
        n = n if isinstance(n, int) and not isinstance(n, bool) else 0
        tops = raw.get("top")
        tops = tops if isinstance(tops, list) else []
        out[col] = {"n": max(0, min(n, MAX_N)), "top": [t for t in (fit_title(str(x), 32) for x in tops) if t][:TOP]}
    return out


def snapshot_from_scan(section: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``scan['sections']['kanban']`` -> ``{"trabajo": object}``; no board (or a failed section) gives ``{}``."""
    section = section or {}
    columnas = section.get("columnas")
    if not section.get("tablero") or not isinstance(columnas, dict):
        return {}
    field = board_field(columnas)
    es_title, es_sub = MESSAGES["board_title"][1], MESSAGES["board_sub"][1]
    obj = make_object(
        TYPE_TABLERO,
        BOARD_ID,
        es_title,
        subtitulo=es_sub.format(todo=field["hacer"]["n"], doing=field["curso"]["n"], done=field["hecho"]["n"]),
        estado="activo",
        zona=ZONE_TABLERO,
        acciones=[],
        tablero=field,
    )
    return {obj["id"]: obj}
