# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""Bugs found by tools/integration against the REAL engine (INT-02, INT-03), pinned with the fake engine.

INT-02: the engine tells the source `{t:"funciones", funciones:[...]}` whenever a person flips a switch inside the world; the plugin
        ignored it, so a function switched OFF in the world kept being polled/published and one switched ON never started.
INT-03: the cron poll's first `lote` held ONLY cron things; the engine's `lote` replaces everything the source had published, so it
        wiped the agent/spend things the connect-time lote had just put in the world.
"""

from __future__ import annotations

import asyncio

from conftest import run, until
from fake_engine import FakeEngine
from test_bridge import Reader, listing, make_bridge, stop

from hermes_3d_lib import config
from hermes_3d_lib import settings as S


def _cron_lotes(eng: FakeEngine) -> list[dict]:
    return [f for f in eng.of_type("lote") if any(o["tipo"] == "cron" for o in f["objetos"])]


def test_world_flip_off_then_on_is_followed(hermes_home):
    async def main():
        eng = FakeEngine(funciones={"salud": True, "cron": True, "sesiones": False})
        await eng.start()
        reader = Reader(listing("cron_list_many.txt"))
        b = make_bridge(hermes_home, eng, reader, switches={"salud": True, "cron": True, "sesiones": False})
        await b.hub.start()
        b._poller = asyncio.create_task(b.poll_loop())
        await eng.wait_connected()
        await eng.wait_for(lambda f: f["t"] == "lote" and any(o["tipo"] == "cron" for o in f["objetos"]))

        await eng.send({"t": "funciones", "funciones": ["salud"]})  # a person switched «cron» off inside the world
        await until(lambda: b.hub.engine_functions.get("cron") is False)
        assert not b.hub.function_on("cron")
        calls = reader.calls
        cycles = b.poll_cycles
        await until(lambda: b.poll_cycles >= cycles + 3)
        assert reader.calls == calls, "the CLI must not run while the world has the function off"

        before = len(_cron_lotes(eng))
        await eng.send({"t": "funciones", "funciones": ["salud", "cron"]})  # ... and on again
        await until(lambda: len(_cron_lotes(eng)) > before)  # the world is rebuilt with a fresh lote
        assert b.hub.function_on("cron")
        await stop(b, eng)

    run(main())


def test_a_world_flip_on_of_a_function_never_polled_starts_publishing(hermes_home):
    async def main():
        eng = FakeEngine(funciones={"salud": True, "cron": False, "sesiones": False})
        await eng.start()
        reader = Reader(listing("cron_list_many.txt"))
        b = make_bridge(hermes_home, eng, reader, switches={"salud": True, "cron": True, "sesiones": False})
        await b.hub.start()
        b._poller = asyncio.create_task(b.poll_loop())
        await eng.wait_connected()
        await until(lambda: b.poll_cycles >= 2)
        assert reader.calls == 0 and not _cron_lotes(eng)
        await eng.send({"t": "funciones", "funciones": ["salud", "cron"]})
        await eng.wait_for(lambda f: f["t"] == "lote" and any(o["tipo"] == "cron" for o in f["objetos"]))
        await stop(b, eng)

    run(main())


def test_funciones_frame_with_garbage_is_ignored_safely(hermes_home):
    async def main():
        eng = FakeEngine()
        await eng.start()
        b = make_bridge(hermes_home, eng, Reader(listing("cron_list_many.txt")))
        await b.hub.start()
        await eng.wait_connected()
        await eng.send({"t": "funciones", "funciones": "cron"})  # not a list: everything off, nothing raised
        await until(lambda: b.hub.engine_functions.get("cron") is False)
        assert b.hub.connected
        await stop(b, eng)

    run(main())


def test_cron_lote_carries_the_mirrors_too(hermes_home, fake_root):
    async def main():
        world_on = ("salud", "cron", "agentes")
        eng = FakeEngine(funciones={config.ENGINE_FUNCTION.get(n, n): (n in world_on) for n in S.FUNCTIONS})
        await eng.start()
        b = make_bridge(
            hermes_home,
            eng,
            Reader(listing("cron_list_many.txt")),
            switches={"salud": True, "cron": True, "sesiones": False, "agentes": True, "gasto": False},
            scan_root=fake_root,
            allow_direct_db=True,
        )
        await b.run_scan()
        await b.hub.start()
        b._poller = asyncio.create_task(b.poll_loop())
        await eng.wait_connected()
        await until(lambda: b.cron_snapshot is not None)
        await until(lambda: b.poll_cycles >= 3)
        lotes = eng.of_type("lote")
        assert lotes, "no lote at all"
        assert all({o["tipo"] for o in f["objetos"]} >= {"perfil"} for f in lotes), [
            sorted({o["tipo"] for o in f["objetos"]}) for f in lotes
        ]
        assert any(o["tipo"] == "cron" for f in lotes for o in f["objetos"])
        await stop(b, eng)

    run(main())


def _many(n: int) -> list[dict]:
    from hermes_3d_lib.protocol import make_object

    return [
        make_object(
            "cron",
            f"job{i:04d}",
            f"A job number {i} with a longish name to fill the frame",
            subtitulo="every 2h · next " + "x" * 60,
            estado="programado",
            zona="z7",
            acciones=["cron.pausar"],
        )
        for i in range(n)
    ]


def test_a_big_snapshot_is_not_wiped_by_its_own_second_lote(hermes_home):
    """INT-04: a snapshot over one 16 KB frame was sent as several `lote`s; each REPLACES the world, so only the last chunk survived."""
    from hermes_3d_lib.hub import Hub

    async def main():
        eng = FakeEngine()
        await eng.start()
        objs = _many(150)

        async def snap():
            return objs

        hub = Hub(token="t", engine_url=eng.url, link_key=eng.secret, snapshot=snap)
        await hub.start()
        await eng.wait_connected()
        await until(lambda: len(eng.world) >= 150)
        assert len(eng.world) == 150, len(eng.world)
        assert len(eng.of_type("lote")) == 1
        await hub.stop()
        await eng.stop()

    run(main())


def test_writes_stay_under_the_engine_rate_limit(hermes_home, monkeypatch):
    """INT-05: 100 changes in one poll arrived unpaced and the real engine closes the link (4429) above RAFAGA 40 / RITMO 20 per second."""
    from hermes_3d_lib import hub as hubmod

    monkeypatch.setattr(hubmod, "SEND_RATE", 60.0)
    monkeypatch.setattr(hubmod, "SEND_BURST", 10.0)

    async def main():
        eng = FakeEngine(rate=80.0, burst=15.0)
        await eng.start()
        hub = hubmod.Hub(token="t", engine_url=eng.url, link_key=eng.secret)
        await hub.start()
        await eng.wait_connected()
        for o in _many(100):
            assert hub.publish({"t": "alta", "objeto": o})
        await until(lambda: len(eng.world) >= 100, timeout=10)
        assert "ritmo" not in eng.refused and eng.connections == 1
        await hub.stop()
        await eng.stop()

    run(main())


def test_objects_for_a_zone_the_world_lacks_are_not_sent(hermes_home):
    """INT-06: the real engine rejects a WHOLE lote when one object names a zone the world does not have (the hermes template has no
    z4/z5 yet), so the cron things never appeared. The plugin now keeps only what the engine's `hola` says it can place."""
    from hermes_3d_lib.hub import Hub
    from hermes_3d_lib.protocol import make_object

    good = make_object("cron", "aaa111", "A job", estado="programado", zona="z7", acciones=[])
    no_zone = make_object("perfil", "alpha", "alpha", estado="activo", zona="z4", acciones=[])
    wrong_type = make_object("uso", "total", "spend", estado="activo", zona="z7", acciones=[])

    async def main():
        eng = FakeEngine(zonas=[{"id": "z7", "tipos": ["cron"]}, {"id": "z0", "tipos": ["salud"]}])
        await eng.start()

        async def snap():
            return [good, no_zone, wrong_type]

        hub = Hub(token="t", engine_url=eng.url, link_key=eng.secret, snapshot=snap)
        await hub.start()
        await eng.wait_connected()
        await until(lambda: len(eng.world) >= 1)
        assert list(eng.world) == ["cron/aaa111"], list(eng.world)
        assert hub.status()["skipped_no_zone"] == 2
        hub.publish({"t": "alta", "objeto": no_zone})
        hub.publish({"t": "cambio", "objeto": good})
        await until(lambda: eng.of_type("cambio"))
        assert not [f for f in eng.of_type("alta") if f["objeto"]["tipo"] == "perfil"]
        assert hub.status()["skipped_no_zone"] == 3
        hub.publish({"t": "lote", "objetos": [good, no_zone, wrong_type]})  # the poll's own lote goes through the same filter
        await until(lambda: hub.status()["skipped_no_zone"] == 5)
        assert list(eng.world) == ["cron/aaa111"]
        await hub.stop()
        await eng.stop()

    run(main())


def test_an_engine_that_announces_no_zones_gets_everything(hermes_home):
    from hermes_3d_lib.hub import Hub
    from hermes_3d_lib.protocol import make_object

    obj = make_object("perfil", "alpha", "alpha", estado="activo", zona="z4", acciones=[])

    async def main():
        eng = FakeEngine()
        await eng.start()

        async def snap():
            return [obj]

        hub = Hub(token="t", engine_url=eng.url, link_key=eng.secret, snapshot=snap)
        await hub.start()
        await eng.wait_connected()
        await until(lambda: "perfil/alpha" in eng.world)
        assert hub.status()["skipped_no_zone"] == 0
        await hub.stop()
        await eng.stop()

    run(main())
