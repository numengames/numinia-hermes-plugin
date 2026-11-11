# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""Zone alignment (M6): the engine's ``hola.zonas`` decides, a documented table is only the fallback."""

from __future__ import annotations

from conftest import run
from fake_engine import FakeEngine

from hermes_3d_lib import agentes, gasto
from hermes_3d_lib.hub import Hub
from hermes_3d_lib.protocol import make_object
from hermes_3d_lib.zones import FALLBACK_ZONES, zone_for

# the zone map of the engine's ``hermes`` template (plantillas/hermes/entorno/espejo.json)
TEMPLATE = [
    {"id": "z0", "tipos": ["salud"]},
    {"id": "z7", "tipos": ["cron"]},
    {"id": "z3", "tipos": ["sesion"]},
    {"id": "z8", "tipos": ["mensaje"]},
    {"id": "z6", "tipos": ["perfil"]},
    {"id": "z2", "tipos": ["uso"]},
    {"id": "z1", "tipos": ["documento"]},
    {"id": "z4", "tipos": ["habilidad"]},
    {"id": "z5", "tipos": ["proyecto"]},
    {"id": "z9", "tipos": ["tablero"]},
]


def test_fallback_table_is_the_template_map():
    assert {t: z for t, z in FALLBACK_ZONES.items()} == {
        "salud": "z0", "documento": "z1", "uso": "z2", "sesion": "z3", "habilidad": "z4",
        "perfil": "z6", "cron": "z7", "mensaje": "z8", "tablero": "z9", "proyecto": "z5",
    }  # fmt: skip
    # the mismatch the M6 note describes: the plugin used z4 for perfil and z5 for uso
    assert agentes.ZONE_AGENTES == "z6" and gasto.ZONE_GASTO == "z2"


def test_zone_for_prefers_the_engine_and_falls_back_to_the_table():
    zones = {"z5": frozenset({"perfil"}), "z9": frozenset({"perfil", "tablero"})}
    assert zone_for("perfil", zones) == "z5"  # first by id
    assert zone_for("tablero", zones) == "z9"
    assert zone_for("documento", zones) == "z1"  # the engine has no zone for it: the table
    assert zone_for("documento", None) == "z1" and zone_for("nonsense", {}) == ""


def _hub(zones):
    hub = Hub(token="t", engine_url=None, link_key=None, switches={"salud": True})
    hub.engine_zones = {z["id"]: frozenset(z["tipos"]) for z in zones}
    return hub


def test_retarget_moves_a_thing_to_the_zone_the_engine_announced():
    hub = _hub([{"id": "z5", "tipos": ["perfil"]}, {"id": "z2", "tipos": ["uso"]}])
    old = make_object("perfil", "a-x", "x", estado="activo", zona="z4")  # the pre-M6 zone
    assert hub.retarget(old)["zona"] == "z5" and old["zona"] == "z4"  # a copy, the input is untouched
    good = make_object("uso", "total", "t", estado="activo", zona="z2")
    assert hub.retarget(good) is good
    assert hub.zone_for("perfil") == "z5"


def test_retarget_leaves_alone_what_no_zone_admits_and_old_engines():
    nothing = make_object("mcp", "m", "m", zona="z4")
    assert _hub([{"id": "z0", "tipos": ["salud"]}]).retarget(nothing) is nothing
    assert (
        Hub(token="t", engine_url=None, link_key=None, switches={}).retarget(nothing) is nothing
    )  # no zones announced: trusted as is
    assert _hub([]).retarget("not a dict") == "not a dict"


def test_real_template_zones_take_the_whole_batch_with_no_skips():
    async def go():
        eng = FakeEngine(zonas=TEMPLATE, funciones={"salud": True, "perfiles": True, "uso": True})
        url = await eng.start()
        hub = Hub(token="t", engine_url=url, link_key=eng.secret, switches={"salud": True, "agentes": True, "gasto": True})
        stale = [  # built with the pre-M6 zones on purpose
            make_object("perfil", "a-ursa", "ursa", estado="activo", zona="z4"),
            make_object("uso", "total", "total", estado="activo", zona="z5"),
        ]

        async def snap():
            return stale

        hub.snapshot = snap
        await hub.start()
        await eng.wait_connected()
        lote = await eng.wait_for(lambda f: f.get("t") == "lote")
        await hub.stop()
        await eng.stop()
        return lote, hub.skipped_no_zone

    lote, skipped = run(go())
    assert {(o["tipo"], o["zona"]) for o in lote["objetos"]} == {("perfil", "z6"), ("uso", "z2")}
    assert skipped == 0
