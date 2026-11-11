# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""Project workshops: one zone of the world per project, with its project card, its Kanban board and its agents.

A world opts in by drawing zones whose id is ``t-<project slug>`` in ``entorno/espejo.json`` (one per project it
wants apart) and, optionally, ``t-otros`` for everything else (archived projects, boards with no workshop, agents
that work on no workshop's project). A world without ``t-`` zones is left exactly as before: this module then
changes nothing.

What goes where (only for things the world can place: the zone must admit the type):

* a ``proyecto`` -> its own workshop, else ``t-otros``;
* a ``tablero`` (one per Kanban board) -> the workshop of the project whose board it is (the project's
  ``board_slug``, else a board named like the project's slug or name), else ``t-otros``;
* an ``agente`` -> the workshop of the project it works on: the board of its running card, else the board where it
  has most open cards; with no open card (or no workshop for that board) -> ``t-otros``.

Geometry comes from the world's own ``espejo.json`` (centre, ancho, fondo per zone), read-only. With it, every thing
in a workshop gets an explicit spot (board at the back, project card in front of it, agents in rows in front),
because the engine's 4 m grid would put a 10.8 m board on top of its neighbours. Without it (file missing or odd)
things keep no x/z and the engine places them on its grid.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

PREFIX = "t-"
OTHERS = PREFIX + "otros"
_BAD = re.compile(r"[^a-z0-9._-]+")

# layout inside a workshop, metres from its centre (z grows towards the zone's sign, which sits at the +z edge)
BOARD_DZ = -8.0  # the board stands at the back
PROJECT_DZ = -1.0  # the project card (cork + branches) in front of the board
AGENT_DZ = 4.0  # first row of agents
AGENT_STEP = 4.0  # agents 4 m apart, rows 4 m apart
AGENTS_PER_ROW = 5
# in t-otros: boards along the back, then project cards, then agents, all on a grid
OTHERS_BOARD_STEP = 14.0
OTHERS_CARD_STEP = 4.0


def zone_id(slug: str) -> str:
    """Workshop zone id for a project slug (engine ids: lowercase, [a-z0-9._-], <= 32)."""
    return (PREFIX + _BAD.sub("-", str(slug).lower()).strip("-._"))[:32]


def read_geometry(world: Path | None) -> dict[str, tuple[float, float, float, float]]:
    """``{zone id: (x, z, ancho, fondo)}`` from ``<world>/entorno/espejo.json``; ``{}`` when it cannot be read."""
    if not world:
        return {}
    try:
        raw = json.loads((Path(world) / "entorno" / "espejo.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, tuple[float, float, float, float]] = {}
    for z in raw.get("zonas") or [] if isinstance(raw, dict) else []:
        try:
            c = z["centro"]
            out[str(z["id"])] = (float(c["x"]), float(c["z"]), float(z["ancho"]), float(z["fondo"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def read_zone_types(world: Path | None) -> dict[str, frozenset[str]]:
    """``{zone id: admitted types}`` from the world's ``espejo.json`` (the engine announces the same in ``hola``)."""
    try:
        raw = json.loads((Path(world) / "entorno" / "espejo.json").read_text(encoding="utf-8")) if world else {}
    except (OSError, ValueError):
        return {}
    out: dict[str, frozenset[str]] = {}
    for z in raw.get("zonas") or [] if isinstance(raw, dict) else []:
        if isinstance(z, dict) and isinstance(z.get("id"), str) and isinstance(z.get("tipos"), list):
            out[z["id"]] = frozenset(str(t) for t in z["tipos"])
    return out


def workshop_boards(projects: list[dict[str, Any]], kanban: dict[str, Any], zones: dict[str, Any]) -> set[str]:
    """Boards that belong to a project with its own workshop (shown before any other board)."""
    shops = _workshops(zones)
    boards = {str(b.get("id")) for b in (kanban or {}).get("items") or [] if isinstance(b, dict)}
    out = set()
    for p in projects or []:
        if zone_id(str(p.get("slug") or p.get("id") or "")) in shops:
            b = board_of(p, boards)
            if b:
                out.add(b)
    return out


def _workshops(zones: dict[str, Any]) -> set[str]:
    return {z for z in zones if z.startswith(PREFIX)}


def board_of(project: dict[str, Any], boards: set[str]) -> str:
    """The Kanban board of a project: its ``board`` field, else a board named like its slug or its name."""
    for cand in (project.get("board"), project.get("slug"), project.get("name")):
        if cand and str(cand) in boards:
            return str(cand)
    return ""


def agent_boards(open_cards: list[dict[str, Any]]) -> dict[str, str]:
    """``{assignee: board}``: the board of a running card, else the board with most open cards (ties: first seen)."""
    running: dict[str, str] = {}
    counts: dict[str, Counter[str]] = {}
    for c in open_cards or []:
        who, board = str(c.get("asignado") or ""), str(c.get("tablero") or "")
        if not who or not board:
            continue
        if c.get("estado") == "running" and who not in running:
            running[who] = board
        counts.setdefault(who, Counter())[board] += 1
    out = {who: cnt.most_common(1)[0][0] for who, cnt in counts.items()}
    out.update(running)
    return out


def _admits(zones: dict[str, Any], zid: str, tipo: str) -> bool:
    return zid in zones and tipo in zones[zid]


def place(
    objs: dict[str, dict[str, Any]],
    projects: list[dict[str, Any]],
    kanban: dict[str, Any],
    zones: dict[str, Any],
    geometry: dict[str, tuple[float, float, float, float]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return ``objs`` (key -> world object, keys ``proyecto/…``, ``tablero/…``, ``agente/…``) with each project,
    board and agent moved to its workshop. ``zones`` = ``{zone id: admitted types}`` (from the world file or the
    engine's ``hola``). No ``t-`` zone -> ``objs`` unchanged."""
    shops = _workshops(zones)
    if not shops:
        return objs
    geometry = geometry or {}
    out = dict(objs)
    boards = {str(b.get("id")) for b in (kanban or {}).get("items") or [] if isinstance(b, dict)}

    # project slug/name -> workshop; board -> workshop
    shop_of_board: dict[str, str] = {}
    shop_of_project: dict[str, str] = {}
    for p in projects or []:
        slug = str(p.get("slug") or p.get("id") or "")
        zid = zone_id(slug)
        if zid not in shops:
            continue
        shop_of_project["p-" + _BAD.sub("-", slug.lower())[:30].strip("-._")] = zid
        b = board_of(p, boards)
        if b:
            shop_of_board[b] = zid

    def target(tipo: str, zid: str) -> str:
        if zid and _admits(zones, zid, tipo):
            return zid
        return OTHERS if _admits(zones, OTHERS, tipo) else ""

    by_zone: dict[str, dict[str, list[str]]] = {}
    who_board = agent_boards((kanban or {}).get("abiertas") or [])
    for key, o in objs.items():
        tipo = str(o.get("tipo", ""))
        if tipo == "proyecto":
            zid = target(tipo, shop_of_project.get(str(o.get("id")), ""))
        elif tipo == "tablero":
            zid = target(tipo, shop_of_board.get(str(o.get("titulo")), shop_of_board.get(str(o.get("id")), "")))
        elif tipo == "perfil":
            zid = target(tipo, shop_of_board.get(who_board.get(str(o.get("titulo")), ""), ""))
        else:
            continue
        if not zid:
            continue
        o = {k: v for k, v in o.items() if k not in ("x", "z")}
        o["zona"] = zid
        out[key] = o
        by_zone.setdefault(zid, {}).setdefault(tipo, []).append(key)

    for zid, kinds in by_zone.items():
        if zid not in geometry:
            continue
        spots = _layout(zid, kinds, geometry[zid])
        for key, (x, z) in spots.items():
            out[key] = {**out[key], "x": round(x, 2), "z": round(z, 2)}
    return out


def _inside(x: float, z: float, g: tuple[float, float, float, float]) -> bool:
    cx, cz, w, d = g
    return abs(x - cx) <= w / 2 - 1 and abs(z - cz) <= d / 2 - 1


def _row(n: int, step: float) -> list[float]:
    return [(i - (n - 1) / 2) * step for i in range(n)]


def _layout(zid: str, kinds: dict[str, list[str]], g: tuple[float, float, float, float]) -> dict[str, tuple[float, float]]:
    """Spots for the things of one zone; a spot that falls outside the zone is left out (the engine's grid places it)."""
    cx, cz, w, d = g
    spots: dict[str, tuple[float, float]] = {}
    boards = sorted(kinds.get("tablero", []))
    cards = sorted(kinds.get("proyecto", []))
    agents = sorted(kinds.get("perfil", []))
    if zid != OTHERS:
        z0 = cz - d / 2 + 4 if boards else cz + BOARD_DZ
        for dx, k in zip(_row(len(boards), OTHERS_BOARD_STEP), boards, strict=False):
            spots[k] = (cx + dx, z0)
        zp = z0 + 7 if boards else cz + PROJECT_DZ
        for dx, k in zip(_row(len(cards), AGENT_STEP), cards, strict=False):
            spots[k] = (cx + dx, zp)
        za = zp + 5
    else:
        z0 = cz - d / 2 + 4
        for dx, k in zip(_row(min(len(boards), max(1, int(w // OTHERS_BOARD_STEP))), OTHERS_BOARD_STEP), boards, strict=False):
            spots[k] = (cx + dx, z0)
        zp = z0 + (7 if boards else 0)
        cols = max(1, int((w - 4) // OTHERS_CARD_STEP) + 1)
        for i, k in enumerate(cards):
            r, c = divmod(i, cols)
            spots[k] = (cx + _row(min(cols, len(cards) - r * cols), OTHERS_CARD_STEP)[c], zp + r * OTHERS_CARD_STEP)
        za = zp + ((len(cards) + cols - 1) // cols) * OTHERS_CARD_STEP + 2 if cards else zp
    per_row = AGENTS_PER_ROW if zid != OTHERS else max(1, int((w - 4) // AGENT_STEP) + 1)
    for i, k in enumerate(agents):
        r, c = divmod(i, per_row)
        spots[k] = (cx + _row(min(per_row, len(agents) - r * per_row), AGENT_STEP)[c], za + r * AGENT_STEP)
    return {k: v for k, v in spots.items() if _inside(v[0], v[1], g)}
