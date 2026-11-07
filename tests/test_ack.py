"""P3-2: the engine's ``ack`` frame names the objects a lote left out; the bridge shows them (redacted)."""

from __future__ import annotations

import json
import logging

from conftest import SAMPLES, run, until
from fake_engine import FakeEngine
from test_bridge import Reader, listing, make_bridge, started, stop

from hermes_palace_lib import hub as hubmod


def recorded() -> dict:
    return json.loads((SAMPLES / "engine_ack_lote.json").read_text(encoding="utf-8"))


def test_parse_ack_keeps_counts_and_a_bounded_redacted_list():
    info = hubmod.parse_ack(recorded())
    assert info["puestas"] == 5 and info["quitadas"] == 1 and info["mas"] == 3
    assert [d["id"] for d in info["descartadas"]] == ["8bc0eecea9b5", "ursa"]
    assert "abcdef123456789" not in json.dumps(info) and "[redacted]" in info["descartadas"][1]["motivo"]
    assert info["total"] == 5  # 2 listed + 3 more


def test_parse_ack_survives_garbage():
    for junk in (
        {"t": "ack"},
        {"t": "ack", "descartadas": "x", "mas": "y", "puestas": None},
        {"t": "ack", "descartadas": [1, None, {"tipo": 5}], "mas": -4},
    ):
        info = hubmod.parse_ack(junk)
        assert isinstance(info["descartadas"], list) and info["mas"] >= 0 and info["total"] >= 0
    huge = {"t": "ack", "descartadas": [{"tipo": "t" * 999, "id": "i" * 999, "motivo": "m" * 999}] * 500, "mas": 1}
    out = hubmod.parse_ack(huge)
    assert len(out["descartadas"]) <= 20 and len(out["descartadas"][0]["motivo"]) <= 120


def test_status_and_log_show_dropped_objects(hermes_home, caplog):
    async def main():
        eng = FakeEngine()
        await eng.start()
        b = make_bridge(hermes_home, eng, Reader(listing("cron_list_many.txt")))
        await started(b, eng)
        assert b.hub.status()["dropped"]["total"] == 0
        with caplog.at_level(logging.WARNING):
            await eng.send(recorded())
            await until(lambda: b.hub.status()["dropped"]["total"] == 5)
        d = b.hub.status()["dropped"]
        assert d["items"][0] == {"tipo": "cron", "id": "8bc0eecea9b5", "motivo": "zona desconocida «z9»"}
        assert "left out" in caplog.text and "8bc0eecea9b5" in caplog.text and "abcdef123456789" not in caplog.text
        assert b.hub.last_error == "" or "ack" not in b.hub.last_error  # an ack is information, not an error
        await stop(b, eng)

    run(main())


def test_an_engine_that_never_sends_ack_works_and_reports_nothing_dropped(hermes_home):
    async def main():
        eng = FakeEngine()
        await eng.start()
        b = make_bridge(hermes_home, eng, Reader(listing("cron_list_many.txt")))
        await started(b, eng)
        await until(lambda: eng.of_type("lote"))
        assert b.hub.status()["dropped"] == {"total": 0, "puestas": 0, "quitadas": 0, "items": []}
        await stop(b, eng)

    run(main())


def test_dropped_resets_when_the_link_reconnects(hermes_home):
    async def main():
        eng = FakeEngine()
        await eng.start()
        b = make_bridge(hermes_home, eng, Reader(listing("cron_list_many.txt")))
        await started(b, eng)
        await eng.send(recorded())
        await until(lambda: b.hub.status()["dropped"]["total"] == 5)
        await eng.drop()
        await until(lambda: b.hub.status()["dropped"]["total"] == 0, timeout=15)
        await stop(b, eng)

    run(main())


def test_palace_status_prints_the_dropped_objects(hermes_home, monkeypatch, capsys):
    from conftest import ROOT  # noqa: F401
    from test_entry import load_entry

    from hermes_palace_lib import client

    data = {
        "connected": True,
        "read_only": False,
        "switches": {},
        "dropped": {
            "total": 5,
            "puestas": 5,
            "quitadas": 1,
            "items": [{"tipo": "cron", "id": "8bc0eecea9b5", "motivo": "zona desconocida"}],
        },
    }
    monkeypatch.setattr(client, "call", lambda *a, **k: {"ok": True, "data": data})
    load_entry().cmd_status()
    out = capsys.readouterr().out
    assert "left out by the engine: 5" in out and "cron/8bc0eecea9b5" in out and "zona desconocida" in out
    data["dropped"] = {"total": 0, "items": []}
    load_entry().cmd_status()
    assert "left out" not in capsys.readouterr().out
