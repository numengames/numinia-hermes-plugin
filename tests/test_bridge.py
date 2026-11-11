"""Bridge end to end against the fake engine: cron publishing, switches, sessions, actions."""

from __future__ import annotations

import asyncio
import json
import os

from conftest import SAMPLES, run, until
from fake_engine import FakeEngine

from hermes_3d_lib import actions, config
from hermes_3d_lib.bridge_main import Bridge
from hermes_3d_lib.sessions import engine_id


def listing(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


class Reader:
    """Stand-in for `hermes cron list --all` that returns whatever text the test sets."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def __call__(self) -> str | None:
        self.calls += 1
        return self.text


def make_bridge(home, eng, reader, switches=None, **kw) -> Bridge:
    b = Bridge(home, cron_reader=reader, engine_url=eng.url, link_key=eng.secret, poll_seconds=0.05, **kw)
    b.switches.update(switches or {"salud": True, "cron": True, "sesiones": True})
    b.hub.switches = b.switches
    return b


async def started(b: Bridge, eng: FakeEngine):
    port = await b.hub.start()
    b._poller = asyncio.create_task(b.poll_loop())
    await eng.wait_connected()
    return port


async def stop(b: Bridge, eng: FakeEngine):
    if b._poller:
        b._poller.cancel()
    await b.hub.stop()
    await eng.stop()


def test_first_connect_sends_lote_then_changes_as_alta_cambio_baja(hermes_home):
    async def main():
        eng = FakeEngine()
        await eng.start()
        reader = Reader(listing("cron_list_many.txt"))
        b = make_bridge(hermes_home, eng, reader)
        await started(b, eng)
        lote = await eng.wait_for(lambda f: f["t"] == "lote" and len(f["objetos"]) == 5)
        assert {o["tipo"] for o in lote["objetos"]} == {"cron"}
        # a job disappears, one changes state, one appears
        text = listing("cron_list_many.txt")
        text = text.replace("  da4a429b66c8 [active]", "  zzzz0000aaaa [active]", 1).replace(
            "  8bc0eecea9b5 [active]", "  8bc0eecea9b5 [paused]", 1
        )
        reader.text = text
        await eng.wait_for(lambda f: f["t"] == "alta" and f["objeto"]["id"] == "zzzz0000aaaa")
        await eng.wait_for(lambda f: f["t"] == "baja" and f["objeto"]["id"] == "da4a429b66c8")
        cam = await eng.wait_for(lambda f: f["t"] == "cambio" and f["objeto"]["id"] == "8bc0eecea9b5")
        assert cam["objeto"]["estado"] == "pausado" and cam["objeto"]["acciones"] == ["cron.reanudar"]
        await stop(b, eng)

    run(main())


def test_cron_function_off_publishes_nothing_and_does_not_poll(hermes_home):
    async def main():
        eng = FakeEngine()
        await eng.start()
        reader = Reader(listing("cron_list_many.txt"))
        b = make_bridge(hermes_home, eng, reader, switches={"salud": True, "cron": False, "sesiones": False})
        await started(b, eng)
        await until(lambda: b.poll_cycles >= 3)  # several poll rounds really happened
        assert reader.calls == 0, "must not even run the CLI while the function is off"
        assert [f for f in eng.frames if f["t"] in ("alta", "cambio", "baja")] == []
        assert all(not f.get("objetos") for f in eng.of_type("lote"))
        await stop(b, eng)

    run(main())


def test_world_side_switch_off_also_stops_cron(hermes_home):
    async def main():
        eng = FakeEngine(funciones={"salud": True, "cron": False, "sesiones": True})
        await eng.start()
        reader = Reader(listing("cron_list_many.txt"))
        b = make_bridge(hermes_home, eng, reader)
        await started(b, eng)
        await until(lambda: b.poll_cycles >= 3)
        assert reader.calls == 0
        await stop(b, eng)

    run(main())


def test_cron_cli_failure_is_reported_not_raised(hermes_home):
    async def main():
        eng = FakeEngine()
        await eng.start()
        b = make_bridge(hermes_home, eng, lambda: None)
        await started(b, eng)
        await until(lambda: "cron list failed" in b.hub.last_error)
        await stop(b, eng)

    run(main())


def test_sessions_become_places_and_close_only_on_finalize(hermes_home):
    async def main():
        eng = FakeEngine()
        await eng.start()
        b = make_bridge(hermes_home, eng, Reader(listing("cron_list_empty.txt")))
        await started(b, eng)
        await b.on_local_event({"kind": "sesion_inicio", "session_id": "20261105_abcdef12", "model": "m", "platform": "cli"})
        alta = await eng.wait_for(lambda f: f["t"] == "alta" and f["objeto"]["tipo"] == "sesion")
        assert alta["objeto"]["estado"] == "activo" and alta["objeto"]["zona"] == "z3"
        await b.on_local_event({"kind": "sesion_fin", "session_id": "20261105_abcdef12"})  # a turn ended
        # Frames reach the engine in order: once the sentinel's alta is seen, a cambio for the first
        # session would already be there if the bridge had (wrongly) sent one.
        await b.on_local_event({"kind": "sesion_inicio", "session_id": "20261105_sentinel1", "model": "m", "platform": "cli"})
        await eng.wait_for(lambda f: f["t"] == "alta" and f["objeto"]["id"] == engine_id("20261105_sentinel1"))
        assert not [f for f in eng.frames if f["t"] == "cambio" and f["objeto"]["tipo"] == "sesion"]
        await b.on_local_event({"kind": "sesion_cierre", "session_id": "20261105_abcdef12"})
        cam = await eng.wait_for(lambda f: f["t"] == "cambio" and f["objeto"]["tipo"] == "sesion")
        assert cam["objeto"]["estado"] == "apagado"
        await stop(b, eng)

    run(main())


def test_sessions_off_by_default_ignored(hermes_home):
    async def main():
        eng = FakeEngine()
        await eng.start()
        b = make_bridge(hermes_home, eng, Reader(""), switches={"salud": True, "cron": False, "sesiones": False})
        await started(b, eng)
        await b.on_local_event({"kind": "sesion_inicio", "session_id": "s1"})
        before = len(eng.of_type("pulso"))
        b.hub.publish({"t": "pulso"})  # sentinel: ordered behind anything s1 would have queued
        await until(lambda: len(eng.of_type("pulso")) > before)
        assert [f for f in eng.frames if f["t"] == "alta"] == []
        await stop(b, eng)

    run(main())


async def _http(port, token, method, path, body=None):
    from test_hub import _http as h

    return await h(port, method, path, token, body)


def test_world_pause_request_waits_for_approval_then_runs_and_answers(hermes_home):
    """peticion -> queued -> (Hermes approves & the tool claims it) -> CLI -> resultado."""

    async def main():
        eng = FakeEngine()
        await eng.start()
        b = make_bridge(hermes_home, eng, Reader(listing("cron_list_many.txt")))
        port = await started(b, eng)
        await until(lambda: "8bc0eecea9b5" in b.known_jobs)
        await eng.send(
            {
                "t": "peticion",
                "id": "p1",
                "objeto": "cron/8bc0eecea9b5",
                "verbo": "cron.pausar",
                "seguridad": "confirmar",
                "quien": {"id": "u1", "nombre": "Pablo"},
                "caduca": 9999999999999,
            }
        )
        await until(lambda: len(b.queue.pending()) == 1)
        assert not eng.of_type("resultado"), "nothing may run or be answered before approval"
        st, claim = await _http(port, b.hub.token, "POST", "/action/claim", {"job_id": "8bc0eecea9b5", "verb": "cron.pausar"})
        assert st == 200
        st, _ = await _http(
            port, b.hub.token, "POST", "/action/resolve", {"token": claim["token"], "ok": True, "texto": "tarea pausada"}
        )
        r = await eng.wait_for(lambda f: f["t"] == "resultado" and f["peticion"] == "p1")
        assert r["ok"] is True and r["texto"] == "tarea pausada"
        await stop(b, eng)

    run(main())


def test_world_request_for_unknown_job_or_bad_id_is_refused_without_queueing(hermes_home):
    async def main():
        eng = FakeEngine()
        await eng.start()
        b = make_bridge(hermes_home, eng, Reader(listing("cron_list_many.txt")))
        await started(b, eng)
        await until(lambda: b.known_jobs)
        for i, obj in enumerate(["cron/0000deadbeef", "cron/../../etc/passwd", "cron/a b;rm -rf", "cron/"]):
            await eng.send(
                {
                    "t": "peticion",
                    "id": f"x{i}",
                    "objeto": obj,
                    "verbo": "cron.pausar",
                    "seguridad": "confirmar",
                    "quien": {"id": "u1", "nombre": "Pablo"},
                    "caduca": 9999999999999,
                }
            )
            r = await eng.wait_for(lambda f, i=i: f["t"] == "resultado" and f["peticion"] == f"x{i}")
            assert r["ok"] is False
        assert b.queue.pending() == []
        await stop(b, eng)

    run(main())


def test_request_with_cron_function_off_is_refused(hermes_home):
    async def main():
        eng = FakeEngine()
        await eng.start()
        b = make_bridge(
            hermes_home, eng, Reader(listing("cron_list_many.txt")), switches={"salud": True, "cron": False, "sesiones": False}
        )
        await started(b, eng)
        b.known_jobs = {"8bc0eecea9b5"}
        await eng.send(
            {
                "t": "peticion",
                "id": "p9",
                "objeto": "cron/8bc0eecea9b5",
                "verbo": "cron.pausar",
                "seguridad": "confirmar",
                "quien": {"id": "u1", "nombre": "Pablo"},
                "caduca": 9999999999999,
            }
        )
        r = await eng.wait_for(lambda f: f["t"] == "resultado" and f["peticion"] == "p9")
        assert r["ok"] is False and "apagada" in r["texto"]
        await stop(b, eng)

    run(main())


def test_unattended_no_approver_fails_cleanly_after_ttl(hermes_home, monkeypatch):
    monkeypatch.setattr(actions, "TTL_SECONDS", 0.4)

    async def main():
        eng = FakeEngine()
        await eng.start()
        b = make_bridge(hermes_home, eng, Reader(listing("cron_list_many.txt")))
        await started(b, eng)
        await until(lambda: b.known_jobs)
        await eng.send(
            {
                "t": "peticion",
                "id": "p7",
                "objeto": "cron/8bc0eecea9b5",
                "verbo": "cron.pausar",
                "seguridad": "confirmar",
                "quien": {"id": "u1", "nombre": "Pablo"},
                "caduca": 9999999999999,
            }
        )
        r = await eng.wait_for(lambda f: f["t"] == "resultado" and f["peticion"] == "p7", timeout=5)
        assert r["ok"] is False and "aprob" in r["texto"]
        assert b.queue.pending() == []
        await stop(b, eng)

    run(main())


def test_runtime_file_is_private_and_removed(hermes_home):
    async def main():
        eng = FakeEngine()
        await eng.start()
        b = make_bridge(hermes_home, eng, Reader(""))
        task = asyncio.create_task(b.run())
        await until(lambda: config.read_runtime(hermes_home) is not None)
        rt = config.read_runtime(hermes_home)
        assert rt["port"] == b.hub.port and rt["pid"] == os.getpid() and len(rt["token"]) >= 40
        assert config.runtime_mode_ok(hermes_home)
        assert oct(config.plugin_data_dir(hermes_home).stat().st_mode & 0o777) == "0o700"
        b.hub._stop.set()
        await asyncio.wait_for(task, 5)
        assert config.read_runtime(hermes_home) is None
        await eng.stop()

    run(main())


def test_token_changes_on_every_start(hermes_home):
    tokens = {config.new_token() for _ in range(5)}
    assert len(tokens) == 5 and all(len(t) >= 40 for t in tokens)
    _ = json


def test_request_already_expired_in_the_world_is_not_queued(hermes_home):
    async def main():
        eng = FakeEngine()
        await eng.start()
        b = make_bridge(hermes_home, eng, Reader(listing("cron_list_many.txt")))
        await started(b, eng)
        await until(lambda: "8bc0eecea9b5" in b.known_jobs)
        await eng.send(
            {
                "t": "peticion",
                "id": "old",
                "objeto": "cron/8bc0eecea9b5",
                "verbo": "cron.pausar",
                "seguridad": "confirmar",
                "quien": {"id": "u1", "nombre": "Pablo"},
                "caduca": 1_700_000_000_000,
            }
        )
        r = await eng.wait_for(lambda f: f["t"] == "resultado" and f["peticion"] == "old")
        assert r["ok"] is False and "caduc" in r["texto"] and not b.queue.pending()
        await stop(b, eng)

    run(main())


def test_engine_tick_deadline_is_not_read_as_wall_clock(hermes_home):
    """REGRESSION (found against the real engine): `caduca` is a sim TICK (e.g. 1234), not epoch ms. It must not be refused as expired."""

    async def main():
        eng = FakeEngine()
        await eng.start()
        b = make_bridge(hermes_home, eng, Reader(listing("cron_list_many.txt")))
        await started(b, eng)
        await until(lambda: "8bc0eecea9b5" in b.known_jobs)
        await eng.send(
            {
                "t": "peticion",
                "id": "tick1",
                "objeto": "cron/8bc0eecea9b5",
                "verbo": "cron.pausar",
                "seguridad": "confirmar",
                "quien": {"id": "u1", "nombre": "Pablo"},
                "caduca": 1234,
            }
        )
        await until(lambda: len(b.queue.pending()) == 1)  # parked, not refused
        assert not eng.of_type("resultado")
        await stop(b, eng)

    run(main())
