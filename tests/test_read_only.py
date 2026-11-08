"""P3-1: ``read_only`` mode. The world can look; it can never change anything in Hermes."""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
from conftest import run, until
from fake_engine import FakeEngine
from test_bridge import Reader, _http, listing, make_bridge, started, stop
from test_entry import FakeCtx, load_entry

from hermes_palace_lib import actions, client, config, supervisor
from hermes_palace_lib import settings as S
from hermes_palace_lib.worldtext import say

PAUSE = {
    "t": "peticion",
    "id": "p1",
    "objeto": "cron/8bc0eecea9b5",
    "verbo": "cron.pausar",
    "seguridad": "confirmar",
    "quien": {"id": "u1", "nombre": "Pablo"},
    "caduca": 9999999999999,
}


# ---- settings -------------------------------------------------------------------------------------------
def test_read_only_defaults_to_false_and_reads_the_setting(hermes_home):
    assert S.DEFAULTS["read_only"] is False
    assert S.PalaceSettings.from_getter(lambda k, d=None: d, hermes_home).read_only is False
    on = S.PalaceSettings.from_getter(lambda k, d=None: {"read_only": True}.get(k, d), hermes_home)
    assert on.read_only is True
    assert S.read_only(lambda k, d=None: "yes") is True
    assert S.read_only(lambda k, d=None: "banana") is False  # unknown text keeps the default


# ---- the bridge refuses every write verb ------------------------------------------------------------------
@pytest.mark.parametrize("verb", ["cron.pausar", "cron.reanudar", "cron.ejecutar", "sesion.nueva", "perfil.activar", "made.up"])
def test_every_world_request_is_refused_in_read_only(hermes_home, verb):
    async def main():
        eng = FakeEngine()
        await eng.start()
        b = make_bridge(hermes_home, eng, Reader(listing("cron_list_many.txt")), read_only=True)
        await started(b, eng)
        await until(lambda: "8bc0eecea9b5" in b.known_jobs)
        await eng.send({**PAUSE, "verbo": verb})
        r = await eng.wait_for(lambda f: f["t"] == "resultado" and f["peticion"] == "p1")
        assert r["ok"] is False
        assert "read-only" in r["texto"] and "solo lectura" in r["texto"]
        assert b.queue.pending() == [], "nothing may be parked for approval"
        await stop(b, eng)

    run(main())


def test_action_routes_refuse_to_hand_out_work_in_read_only(hermes_home):
    async def main():
        eng = FakeEngine()
        await eng.start()
        b = make_bridge(hermes_home, eng, Reader(listing("cron_list_many.txt")), read_only=True)
        port = await started(b, eng)
        st, body = await _http(port, b.hub.token, "POST", "/action/claim", {"job_id": "8bc0eecea9b5", "verb": "cron.pausar"})
        assert st == 403 and "read-only" in body["error"]
        st, status = await _http(port, b.hub.token, "GET", "/status")
        assert st == 200 and status["read_only"] is True
        await stop(b, eng)

    run(main())


def test_the_queue_itself_refuses_when_read_only():
    q = actions.ActionQueue(enabled=lambda: True, known_job=lambda j: True, read_only=lambda: True)
    res = asyncio.run(q.submit({"id": "x", "objeto": "cron/8bc0eecea9b5", "verbo": "cron.pausar"}))
    assert res["ok"] is False and res["code"] == "read_only" and q.pending() == []


# ---- nothing published implies control ---------------------------------------------------------------------
def _all_objects(eng: FakeEngine) -> list[dict]:
    out = []
    for f in eng.frames:
        if f["t"] == "lote":
            out += f["objetos"]
        elif f["t"] in ("alta", "cambio"):
            out.append(f["objeto"])
    return out


def test_no_object_offers_a_verb_and_the_world_card_says_read_only(hermes_home):
    async def main():
        eng = FakeEngine(zonas=[{"id": "z7", "tipos": ["cron"]}, {"id": "z0", "tipos": ["salud"]}])
        await eng.start()
        b = make_bridge(hermes_home, eng, Reader(listing("cron_list_many.txt")), read_only=True)
        await started(b, eng)
        await until(lambda: any(o["tipo"] == "cron" for o in _all_objects(eng)))
        await until(lambda: any(o["tipo"] == "salud" for o in _all_objects(eng)))
        objs = _all_objects(eng)
        assert [o for o in objs if o.get("acciones")] == [], "a read-only world must not offer buttons"
        card = next(o for o in objs if o["tipo"] == "salud")
        assert "read-only" in card["titulo"] and "read-only" in card["subtitulo"]
        await stop(b, eng)

    run(main())


def test_without_read_only_the_cron_objects_keep_their_verbs_and_no_card(hermes_home):
    async def main():
        eng = FakeEngine()
        await eng.start()
        b = make_bridge(hermes_home, eng, Reader(listing("cron_list_many.txt")))
        await started(b, eng)
        await until(lambda: any(o["tipo"] == "cron" and o["acciones"] for o in _all_objects(eng)))
        assert not any(o["tipo"] == "salud" for o in _all_objects(eng))
        assert b.hub.status()["read_only"] is False
        await stop(b, eng)

    run(main())


# ---- the Hermes-side tool ------------------------------------------------------------------------------------
def test_the_tool_refuses_without_touching_the_bridge_or_hermes(hermes_home, monkeypatch):
    calls = []
    monkeypatch.setattr(client, "call", lambda *a, **k: calls.append(a) or {"ok": False})
    monkeypatch.setattr(actions, "run_cron_cli", lambda *a, **k: calls.append("RAN") or {"ok": True, "text": "x"})
    ctx = FakeCtx({"read_only": True})
    mod = load_entry()
    mod.register(ctx)
    out = json.loads(ctx.tools["palace_cron_action"].handler({"job_id": "8bc0eecea9b5", "verb": "cron.pausar"}))
    assert out["ok"] is False and "read-only" in out["text"]
    assert calls == []


def test_the_tool_obeys_a_bridge_that_was_started_read_only_by_flag(hermes_home, monkeypatch):
    ran = []
    monkeypatch.setattr(client, "call", lambda *a, **k: {"ok": False, "status": 403, "data": {"error": "read-only"}})
    monkeypatch.setattr(actions, "run_cron_cli", lambda *a, **k: ran.append(1) or {"ok": True, "text": "x"})
    ctx = FakeCtx()  # config.yaml says read_only false; the bridge says otherwise
    load_entry().register(ctx)
    out = json.loads(ctx.tools["palace_cron_action"].handler({"job_id": "8bc0eecea9b5", "verb": "cron.pausar"}))
    assert out["ok"] is False and "read-only" in out["text"] and ran == []


# ---- the CLI ---------------------------------------------------------------------------------------------------
def _parse(mod, argv):
    import argparse

    p = argparse.ArgumentParser()
    mod._setup_cli(p)
    return p.parse_args(argv)


def test_up_accepts_the_read_only_flag(hermes_home):
    mod = load_entry()
    assert _parse(mod, ["up", "--read-only"]).read_only is True
    assert _parse(mod, ["up"]).read_only is False


def _fake_start(monkeypatch, spawned):
    monkeypatch.setattr(
        supervisor,
        "start_engine",
        lambda cfg, **kw: {"reused": True, "port": cfg.engine_port, "link_port": 8791, "pid": None},
    )
    monkeypatch.setattr(
        supervisor, "spawn_bridge", lambda home, read_only=False, demo=False, hold_fd=None: spawned.append(read_only) or 4242
    )


def test_up_flag_reaches_the_bridge_process(hermes_home, monkeypatch, capsys):
    spawned: list[bool] = []
    _fake_start(monkeypatch, spawned)
    mod = load_entry()
    assert mod.cmd_up(read_only=True) == 0
    assert spawned == [True]
    assert "read-only" in capsys.readouterr().out


def test_the_setting_alone_also_starts_the_bridge_read_only(hermes_home, monkeypatch):
    spawned: list[bool] = []
    _fake_start(monkeypatch, spawned)
    (hermes_home / "config.yaml").write_text(
        "plugins:\n  entries:\n    hermes-palace:\n      settings:\n        read_only: true\n"
    )
    assert load_entry().cmd_up() == 0
    assert spawned == [True]


def test_up_refuses_to_reuse_a_bridge_that_is_not_read_only(hermes_home, monkeypatch):
    _fake_start(monkeypatch, [])
    config.write_runtime({"pid": 1234, "port": 1, "token": "t"}, hermes_home)
    monkeypatch.setattr(config, "pid_alive", lambda pid: True)
    monkeypatch.setattr(client, "call", lambda *a, **k: {"ok": True, "data": {"read_only": False}})
    with pytest.raises(supervisor.SupervisorError, match="palace down"):
        load_entry().cmd_up(read_only=True)


def test_status_prints_the_mode(hermes_home, monkeypatch, capsys):
    mod = load_entry()
    monkeypatch.setattr(
        client, "call", lambda *a, **k: {"ok": True, "data": {"connected": True, "read_only": True, "switches": {}}}
    )
    mod.cmd_status()
    assert "read-only" in capsys.readouterr().out
    monkeypatch.setattr(
        client, "call", lambda *a, **k: {"ok": True, "data": {"connected": True, "read_only": False, "switches": {}}}
    )
    mod.cmd_status()
    assert "read-write" in capsys.readouterr().out
    monkeypatch.setattr(client, "call", lambda *a, **k: {"ok": False, "error": "bridge-not-running"})
    (hermes_home / "config.yaml").write_text(
        "plugins:\n  entries:\n    hermes-palace:\n      settings:\n        read_only: true\n"
    )
    mod.cmd_status()
    assert "read-only (setting" in capsys.readouterr().out


def test_bridge_main_reads_the_flag_and_records_it(hermes_home, monkeypatch):
    from hermes_palace_lib import bridge_main

    seen = {}

    class Fake:
        def __init__(self, home, **kw):
            seen.update(kw)
            self.hub = SimpleNamespace(token="tok")

        async def run(self):
            return 0

    monkeypatch.setattr(bridge_main, "Bridge", Fake)
    monkeypatch.setattr(sys, "argv", ["bridge_main", "--read-only"])
    assert bridge_main.main() == 0
    assert seen["read_only"] is True
    monkeypatch.setattr(sys, "argv", ["bridge_main"])
    assert bridge_main.main() == 0
    assert seen["read_only"] is False


def test_spawn_bridge_passes_the_flag_on_the_command_line(hermes_home, monkeypatch):
    captured = {}

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        return SimpleNamespace(pid=77)

    monkeypatch.setattr(supervisor.subprocess, "Popen", fake_popen)
    assert supervisor.spawn_bridge(hermes_home, read_only=True) == 77
    assert captured["argv"][-1] == "--read-only"
    supervisor.spawn_bridge(hermes_home)
    assert "--read-only" not in captured["argv"]


def test_say_read_only_has_both_languages():
    r = say("read_only")
    assert "read-only" in r["text"] and "solo lectura" in r["texto"]
