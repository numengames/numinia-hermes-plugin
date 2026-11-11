# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""Mirror ``kanban``: the work board as ONE ``tablero`` thing (zone z9), read-only.

Source: the scan (``scan.scan_kanban``), which opens ``kanban.db`` read-only and keeps, for the board with most open
cards, per column the count and at most 3 titles (redacted, 32 bytes). Card bodies, comments, results and assignees
never reach this module. Published ONLY behind ``funciones.kanban`` (off by default).

Columns (the plugin's decision, M6 contract): hacer = triage/todo/ready, curso = running/blocked, hecho = done inside
the scan window. The ``tablero`` field is exactly ``{"hacer": {"n", "top"}, "curso": {...}, "hecho": {...}}``.
"""

from __future__ import annotations

import re
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

# --- layout (space only): a board is 10.8 m wide (engine formas-extra.ts), but the engine's own grid is 4 m, so
# several boards left to it would overlap. Each board gets an explicit spot in zone z9: two columns 14 m apart
# (3.2 m clear between boards), rows 10 m apart (a 10 m aisle to stand in and read), at most MAX_BOARDS boards.
# Offsets are from the z9 centre of the engine's `hermes` template. Every spot is inside the ORIGINAL z9 (24 x 20 m;
# the engine checks |dx| <= ancho/2 and |dz| <= fondo/2), so an older world still accepts the batch; the template
# now makes z9 34 x 32 m so the outer boards also stand on the floor of the zone.
Z9_CENTER = (30.0, 30.0)
COL_DX = 7.0
ROW_DZ = 10.0
MAX_BOARDS = 6
_BAD_ID = re.compile(r"[^a-z0-9._-]+")


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


def board_id(name: str, taken: set[str]) -> str:
    """A board's folder name -> a thing id the engine accepts (lowercase, <= 32), unique among ``taken``."""
    base = _BAD_ID.sub("-", str(name).lower()).strip("-._")[:32] or "tablero"
    cand, i = base, 2
    while cand in taken:
        suf = f"-{i}"
        cand, i = base[: 32 - len(suf)] + suf, i + 1
    taken.add(cand)
    return cand


def layout(n: int, center: tuple[float, float] = Z9_CENTER) -> list[tuple[float, float]]:
    """Spots (x, z) for ``n`` boards in reading order: two per row, rows centred on the zone, a lone last board centred."""
    n = max(0, min(n, MAX_BOARDS))
    rows = (n + 1) // 2
    out: list[tuple[float, float]] = []
    for i in range(n):
        r, c = divmod(i, 2)
        alone = r == rows - 1 and n % 2 == 1
        dx = 0.0 if alone else (-COL_DX if c == 0 else COL_DX)
        out.append((center[0] + dx, center[1] + (r - (rows - 1) / 2) * ROW_DZ))
    return out


def snapshot_boards(section: dict[str, Any], first: set[str] | frozenset[str] = frozenset()) -> dict[str, dict[str, Any]]:
    """One ``tablero`` per kanban board with cards (most open cards first, at most ``MAX_BOARDS``), each at its own
    spot so no two touch. A section without ``items`` (an old scan) falls back to the single board."""
    items = [b for b in (section or {}).get("items") or [] if isinstance(b, dict) and isinstance(b.get("columnas"), dict)]
    if not items:
        return snapshot_from_scan(section)
    fields = [(b, board_field(b["columnas"])) for b in items]
    fields = [(b, f) for b, f in fields if f["hacer"]["n"] + f["curso"]["n"] + f["hecho"]["n"] > 0]
    # boards in ``first`` (those with a project workshop) are never the ones cut by MAX_BOARDS
    fields.sort(
        key=lambda bf: (
            str(bf[0].get("id", "")) not in first,
            -(bf[1]["hacer"]["n"] + bf[1]["curso"]["n"]),
            str(bf[0].get("id", "")),
        )
    )
    fields = fields[:MAX_BOARDS]
    es_sub = MESSAGES["board_sub"][1]
    taken: set[str] = set()
    out: dict[str, dict[str, Any]] = {}
    for (b, field), (x, z) in zip(fields, layout(len(fields)), strict=False):
        name = str(b.get("id", ""))
        bid = board_id(name, taken)
        out[bid] = make_object(
            TYPE_TABLERO,
            bid,
            name or MESSAGES["board_title"][1],
            subtitulo=es_sub.format(todo=field["hacer"]["n"], doing=field["curso"]["n"], done=field["hecho"]["n"]),
            estado="activo",
            zona=ZONE_TABLERO,
            acciones=[],
            tablero=field,
            x=x,
            z=z,
        )
    return out
