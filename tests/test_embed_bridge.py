# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""Path A, bridge half: /snapshot (same serializers as the link) and /action/submit (same approval road as a link `peticion`)."""

from __future__ import annotations

import asyncio

from conftest import run, until
from fake_engine import FakeEngine
from test_bridge import Reader, _http, listing, make_bridge, started, stop

from hermes_3d_lib import actions

REQ = {
    "id": "w1",
    "objeto": "cron/8bc0eecea9b5",
    "verbo": "cron.pausar",
    "seguridad": "confirmar",
    "quien": {"id": "u", "nombre": "x"},
}


def test_snapshot_is_what_the_link_publishes(hermes_home):
    async def main():
        eng = FakeEngine()
        await eng.start()
        b = make_bridge(hermes_home, eng, Reader(listing("cron_list_many.txt")))
        port = await started(b, eng)
        await until(lambda: "8bc0eecea9b5" in b.known_jobs)
        st, snap = await _http(port, b.hub.token, "GET", "/snapshot")
        await until(lambda: any(f["objetos"] for f in eng.of_type("lote")))
        link = [f for f in eng.of_type("lote") if f["objetos"]][-1]["objetos"]
        assert st == 200 and snap["read_only"] is False and "cron" in snap["funciones"]
        assert [o["id"] for o in snap["objetos"]] == [o["id"] for o in link]  # same objects, same order, same serializers
        assert {o["id"] for o in snap["objetos"]} >= {"8bc0eecea9b5"}
        await stop(b, eng)

    run(main())


def test_snapshot_needs_the_bridge_token(hermes_home):
    async def main():
        eng = FakeEngine()
        await eng.start()
        b = make_bridge(hermes_home, eng, Reader(""))
        port = await started(b, eng)
        st, _ = await _http(port, "wrong", "GET", "/snapshot")
        assert st in (401, 403)
        await stop(b, eng)

    run(main())


async def setup(hermes_home, **kw):
    eng = FakeEngine()
    await eng.start()
    b = make_bridge(hermes_home, eng, Reader(listing("cron_list_many.txt")), **kw)
    port = await started(b, eng)
    await until(lambda: "8bc0eecea9b5" in b.known_jobs)
    return eng, b, port


async def approve(port, b, ok=True):
    await until(lambda: len(b.queue.pending()) == 1)
    _, claim = await _http(port, b.hub.token, "POST", "/action/claim", {"job_id": "8bc0eecea9b5", "verb": "cron.pausar"})
    await _http(
        port,
        b.hub.token,
        "POST",
        "/action/resolve",
        {"token": claim["token"], "ok": ok, "texto": "tarea pausada" if ok else "no"},
    )


def test_submit_waits_for_approval_then_answers(hermes_home):
    async def main():
        eng, b, port = await setup(hermes_home)
        call = asyncio.create_task(_http(port, b.hub.token, "POST", "/action/submit", dict(REQ)))
        await asyncio.sleep(0.2)
        assert not call.done(), "nothing may be answered before Hermes approves"
        await approve(port, b)
        st, out = await call
        assert st == 200 and out["ok"] is True and out["texto"] == "tarea pausada"
        await stop(b, eng)

    run(main())


def test_submit_denied(hermes_home):
    async def main():
        eng, b, port = await setup(hermes_home)
        call = asyncio.create_task(_http(port, b.hub.token, "POST", "/action/submit", dict(REQ)))
        await approve(port, b, ok=False)
        _, out = await call
        assert out["ok"] is False
        await stop(b, eng)

    run(main())


def test_submit_with_nobody_to_approve_fails_closed(hermes_home, monkeypatch):
    monkeypatch.setattr(actions, "TTL_SECONDS", 0.3)

    async def main():
        eng, b, port = await setup(hermes_home)
        _, out = await _http(port, b.hub.token, "POST", "/action/submit", dict(REQ))
        assert out["ok"] is False and out["code"] == "no_approver"
        await stop(b, eng)

    run(main())


def test_submit_read_only_refuses_and_queues_nothing(hermes_home):
    async def main():
        eng, b, port = await setup(hermes_home, read_only=True)
        _, out = await _http(port, b.hub.token, "POST", "/action/submit", dict(REQ))
        assert out["ok"] is False and out["code"] == "read_only" and b.queue.pending() == []
        await stop(b, eng)

    run(main())


def test_submit_replayed_id_unknown_verb_and_junk(hermes_home, monkeypatch):
    monkeypatch.setattr(actions, "TTL_SECONDS", 0.2)

    async def main():
        eng, b, port = await setup(hermes_home)
        _, first = await _http(port, b.hub.token, "POST", "/action/submit", dict(REQ))
        assert first["code"] == "no_approver"
        _, again = await _http(port, b.hub.token, "POST", "/action/submit", dict(REQ))
        assert again["code"] == "replayed"
        _, bad = await _http(port, b.hub.token, "POST", "/action/submit", {**REQ, "id": "w2", "verbo": "cron.borrar"})
        assert bad["code"] == "unknown_verb"
        _, odd = await _http(port, b.hub.token, "POST", "/action/submit", {**REQ, "id": "w3", "objeto": "cron/../etc"})
        assert odd["code"] == "bad_job_id"
        await stop(b, eng)

    run(main())
