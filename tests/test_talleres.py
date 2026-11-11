# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""Project workshops (talleres.py): one zone per project with its card, its board and its agents."""

import json

from hermes_3d_lib import talleres

T = frozenset({"proyecto", "tablero", "perfil", "sesion"})
ZONES = {"z0": frozenset({"salud"}), "t-web": T, "t-otros": T}
GEO = {"t-web": (100.0, 0.0, 28.0, 30.0), "t-otros": (100.0, 50.0, 40.0, 40.0)}
PROJECTS = [
    {"slug": "web", "name": "numengames-web", "board": "", "archived": False},
    {"slug": "old", "name": "Old", "board": "", "archived": True},
]
KANBAN = {
    "items": [{"id": "numengames-web"}, {"id": "motor"}],
    "abiertas": [
        {"tablero": "numengames-web", "asignado": "ana", "estado": "todo"},
        {"tablero": "motor", "asignado": "ana", "estado": "todo"},
        {"tablero": "numengames-web", "asignado": "ana", "estado": "blocked"},
        {"tablero": "motor", "asignado": "bob", "estado": "todo"},
        {"tablero": "motor", "asignado": "bob", "estado": "todo"},
        {"tablero": "numengames-web", "asignado": "bob", "estado": "running"},
    ],
}


def _objs():
    return {
        "proyecto/p-web": {"tipo": "proyecto", "id": "p-web", "titulo": "numengames-web", "zona": "z5"},
        "proyecto/p-old": {"tipo": "proyecto", "id": "p-old", "titulo": "Old", "zona": "z5"},
        "tablero/numengames-web": {
            "tipo": "tablero",
            "id": "numengames-web",
            "titulo": "numengames-web",
            "zona": "z9",
            "x": 1,
            "z": 2,
        },
        "tablero/motor": {"tipo": "tablero", "id": "motor", "titulo": "motor", "zona": "z9"},
        "agente/a-ana": {"tipo": "perfil", "id": "a-ana", "titulo": "ana", "zona": "z5"},
        "agente/a-bob": {"tipo": "perfil", "id": "a-bob", "titulo": "bob", "zona": "z5"},
        "agente/a-cid": {"tipo": "perfil", "id": "a-cid", "titulo": "cid", "zona": "z5"},
        "salud/x": {"tipo": "salud", "id": "x", "titulo": "x", "zona": "z0"},
    }


def test_no_workshop_zones_changes_nothing():
    objs = _objs()
    assert talleres.place(objs, PROJECTS, KANBAN, {"z5": T}, GEO) is objs


def test_each_thing_goes_to_its_workshop_or_to_others():
    out = talleres.place(_objs(), PROJECTS, KANBAN, ZONES, GEO)
    zona = {k: o["zona"] for k, o in out.items()}
    assert zona["proyecto/p-web"] == "t-web"  # its own workshop
    assert zona["tablero/numengames-web"] == "t-web"  # board named like the project
    assert zona["agente/a-ana"] == "t-web"  # most open cards there
    assert zona["agente/a-bob"] == "t-web"  # a running card wins over more open cards elsewhere
    assert zona["proyecto/p-old"] == "t-otros"
    assert zona["tablero/motor"] == "t-otros"
    assert zona["agente/a-cid"] == "t-otros"  # no open card
    assert zona["salud/x"] == "z0"  # untouched


def test_spots_are_inside_their_zone_and_distinct():
    out = talleres.place(_objs(), PROJECTS, KANBAN, ZONES, GEO)
    seen = set()
    for o in out.values():
        if o["zona"] in GEO:
            cx, cz, w, d = GEO[o["zona"]]
            assert abs(o["x"] - cx) <= w / 2 and abs(o["z"] - cz) <= d / 2
            assert (o["x"], o["z"]) not in seen
            seen.add((o["x"], o["z"]))
    board, card = out["tablero/numengames-web"], out["proyecto/p-web"]
    assert board["z"] < card["z"] < out["agente/a-ana"]["z"]  # board at the back, card, then agents


def test_without_geometry_the_engine_grid_places_them():
    out = talleres.place(_objs(), PROJECTS, KANBAN, ZONES, {})
    assert "x" not in out["tablero/numengames-web"] and out["tablero/numengames-web"]["zona"] == "t-web"


def test_workshop_boards_are_kept_first(tmp_path):
    assert talleres.workshop_boards(PROJECTS, KANBAN, ZONES) == {"numengames-web"}


def test_zone_reading_from_the_world_file(tmp_path):
    (tmp_path / "entorno").mkdir()
    (tmp_path / "entorno" / "espejo.json").write_text(
        json.dumps({"zonas": [{"id": "t-web", "centro": {"x": 1, "z": 2}, "ancho": 3, "fondo": 4, "tipos": ["proyecto"]}]})
    )
    assert talleres.read_geometry(tmp_path) == {"t-web": (1.0, 2.0, 3.0, 4.0)}
    assert talleres.read_zone_types(tmp_path) == {"t-web": frozenset({"proyecto"})}
    assert talleres.read_geometry(tmp_path / "nope") == {}
