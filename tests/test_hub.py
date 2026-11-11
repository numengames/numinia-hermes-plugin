# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import run, until
from fake_engine import FakeEngine

from hermes_3d_lib import protocol as P
from hermes_3d_lib.hub import Hub


async def _http(
    port: int, method: str, path: str, token: str | None, body: dict | None = None, origin: str | None = None
) -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    raw = json.dumps(body).encode() if body is not None else b""
    head = f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: {len(raw)}\r\nContent-Type: application/json\r\n"
    if token is not None:
        head += f"Authorization: Bearer {token}\r\n"
    if origin is not None:
        head += f"Origin: {origin}\r\n"
    writer.write(head.encode() + b"\r\n" + raw)
    await writer.drain()
    data = await reader.read()
    writer.close()
    status = int(data.split(b" ", 2)[1])
    payload = data.split(b"\r\n\r\n", 1)[1]
    return status, json.loads(payload or b"{}")


def make_hub(engine: FakeEngine | None, **kw) -> Hub:
    return Hub(
        token="tok-123",
        engine_url=engine.url if engine else None,
        link_key=engine.secret if engine else None,
        hermes_version="0.22.7",
        switches={"salud": True, "cron": True, "sesiones": True},
        **kw,
    )


def test_handshake_ok_and_lote_on_connect():
    async def main():
        eng = FakeEngine()
        await eng.start()
        obj = P.make_object("cron", "abc12345", "mi tarea", estado="programado", zona="z7")

        async def snap():
            return [obj]

        hub = make_hub(eng, snapshot=snap)
        await hub.start()
        await asyncio.wait_for(hub.connected_event.wait(), 5)
        lote = await eng.wait_for(lambda f: f["t"] == "lote")
        assert lote["objetos"][0]["id"] == "abc12345"
        assert eng.hellos[0]["plugin"] == "hermes-3d"
        assert eng.origins_seen == [None]  # the plugin sends no Origin
        assert hub.engine_functions == {
            "salud": True,
            "cron": True,
            "sesiones": True,
            "agentes": False,
            "gasto": False,
            "habilidades": False,
            "kanban": False,
            "proyectos": False,
            "documentos": False,
        }
        await hub.stop()
        await eng.stop()

    run(main())


def test_wrong_secret_is_refused_and_never_connected():
    async def main():
        eng = FakeEngine(secret="right")
        await eng.start()
        hub = Hub(token="t", engine_url=eng.url, link_key="wrong", switches={"salud": True})
        await hub.start()
        await until(lambda: "firma" in eng.refused)
        assert not hub.connected
        assert "refused the link key" in hub.last_error
        await hub.stop()
        await eng.stop()

    run(main())


def test_reconnect_resends_full_lote():
    async def main():
        eng = FakeEngine()
        await eng.start()
        calls = {"n": 0}

        async def snap():
            calls["n"] += 1
            return [P.make_object("cron", f"job{calls['n']:05d}", "t")]

        hub = make_hub(eng, snapshot=snap)
        await hub.start()
        await eng.wait_for(lambda f: f["t"] == "lote")
        await eng.drop()
        await until(lambda: len(eng.of_type("lote")) >= 2, timeout=8)
        assert eng.connections >= 2
        assert eng.of_type("lote")[1]["objetos"][0]["id"] == "job00002"  # fresh snapshot, not a replay
        await hub.stop()
        await eng.stop()

    run(main(), timeout=20)


def test_local_face_needs_token_and_rejects_origin():
    async def main():
        hub = make_hub(None)
        port = await hub.start()
        assert (await _http(port, "GET", "/status", None))[0] == 401
        assert (await _http(port, "GET", "/status", "wrong"))[0] == 401
        assert (await _http(port, "GET", "/status", "tok-123", origin="http://evil.example"))[0] == 403
        st, body = await _http(port, "GET", "/status", "tok-123")
        assert st == 200 and body["port"] == port
        assert hub.rejected_local == 3
        await hub.stop()

    run(main())


def test_local_face_binds_loopback_only():
    async def main():
        hub = make_hub(None)
        await hub.start()
        assert hub._server.sockets[0].getsockname()[0] == "127.0.0.1"
        await hub.stop()

    run(main())


def test_event_endpoint_delivers_and_caps_body():
    async def main():
        got = []

        async def on_ev(ev):
            got.append(ev)

        hub = make_hub(None, on_local_event=on_ev)
        port = await hub.start()
        st, _ = await _http(port, "POST", "/event", "tok-123", {"kind": "sesion_inicio", "id": "x"})
        assert st == 200 and got == [{"kind": "sesion_inicio", "id": "x"}]
        assert (await _http(port, "POST", "/event", "tok-123", {"nokind": 1}))[0] == 400
        big = {"kind": "x", "pad": "a" * 40000}
        assert (await _http(port, "POST", "/event", "tok-123", big))[0] == 413
        await hub.stop()

    run(main())


def test_peticion_roundtrip_and_unknown_verb():
    async def main():
        eng = FakeEngine()
        await eng.start()

        async def on_req(msg):
            return {"ok": True, "texto": f"hecho {msg['objeto']}"}

        hub = make_hub(eng, on_request=on_req)
        await hub.start()
        await eng.wait_connected()
        await eng.send({"t": "peticion", "id": "p1", "objeto": "cron/abc", "verbo": "cron.pausar", "quien": "dueño"})
        r = await eng.wait_for(lambda f: f["t"] == "resultado" and f["peticion"] == "p1")
        assert r["ok"] is True and "cron/abc" in r["texto"]
        await eng.send({"t": "peticion", "id": "p2", "objeto": "cron/abc", "verbo": "rm -rf", "quien": "x"})
        r2 = await eng.wait_for(lambda f: f["t"] == "resultado" and f["peticion"] == "p2")
        assert r2["ok"] is False
        await hub.stop()
        await eng.stop()

    run(main())


def test_function_needs_both_switches():
    hub = Hub(token="t", engine_url=None, link_key=None, switches={"salud": True, "cron": True})
    assert hub.function_on("cron")
    hub.engine_functions = {"cron": False}
    assert not hub.function_on("cron")
    hub.engine_functions = {"cron": True}
    hub.switches["cron"] = False
    assert not hub.function_on("cron")


def test_oversized_frame_is_refused_not_truncated():
    big = {"t": "lote", "objetos": [{"x": "y" * 20000}]}
    with pytest.raises(P.ProtocolError):
        P.encode(big)
