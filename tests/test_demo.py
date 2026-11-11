# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""P3-3: ``hermes 3d demo`` - your own Hermes as a world, read-only, with agents, spend and cron on."""

from __future__ import annotations

import re
import sqlite3
import sys

import pytest
from conftest import LOOP, run
from fake_engine import FakeEngine
from test_bridge import Reader, listing, started, stop
from test_entry import load_entry

from hermes_3d_lib import client, config, supervisor
from hermes_3d_lib.bridge_main import Bridge


def make_home(home):
    """A throwaway Hermes root with two named profiles and a state db with spend."""
    for name in ("alpha", "beta"):
        (home / "profiles" / name).mkdir(parents=True)
        (home / "profiles" / name / "config.yaml").write_text("model:\n  default: x\n")
    (home / "config.yaml").write_text("model:\n  default: x\n")
    db = sqlite3.connect(home / "state.db")
    db.execute(
        "CREATE TABLE sessions (id TEXT, source TEXT, started_at REAL, message_count INT, input_tokens INT, output_tokens INT, estimated_cost_usd REAL)"
    )
    db.commit()
    db.close()


def fake_runtime(monkeypatch, bridge_status, spawned):
    monkeypatch.setattr(
        supervisor, "start_engine", lambda cfg, **kw: {"reused": True, "port": cfg.engine_port, "link_port": 8791, "pid": None}
    )
    monkeypatch.setattr(
        supervisor,
        "spawn_bridge",
        lambda home, read_only=False, demo=False, hold_fd=None, world=None: spawned.append((read_only, demo)) or 4242,
    )

    def call(method, path, body=None, home=None, **kw):
        if path == "/status":
            return {"ok": True, "data": bridge_status}
        if path == "/stop":
            spawned.append("stopped")
            return {"ok": True, "data": {}}
        return {"ok": False}

    monkeypatch.setattr(client, "call", call)


def test_demo_starts_read_only_and_prints_url_and_counts_only(hermes_home, monkeypatch, capsys):
    make_home(hermes_home)
    spawned: list = []
    fake_runtime(monkeypatch, {"connected": True, "read_only": True, "demo": True, "switches": {}}, spawned)
    assert load_entry().cmd_demo() == 0
    out = capsys.readouterr().out
    assert spawned == [(True, True)]
    assert f"http://{LOOP}:8790/" in out and "read-only" in out
    assert "agents" in out and "cron" in out and "spend" in out
    assert "alpha" not in out and "beta" not in out, "counts only, never names"
    assert re.search(r"agents[^\n]*: 3", out)  # the default home plus two named profiles


def test_demo_never_writes_the_users_config(hermes_home, monkeypatch):
    make_home(hermes_home)
    before = (hermes_home / "config.yaml").read_bytes()
    fake_runtime(monkeypatch, {"connected": True, "read_only": True, "demo": True}, [])
    load_entry().cmd_demo()
    assert (hermes_home / "config.yaml").read_bytes() == before


def test_demo_refuses_and_stops_its_bridge_when_read_only_is_not_enforced(hermes_home, monkeypatch, capsys):
    make_home(hermes_home)
    spawned: list = []
    fake_runtime(monkeypatch, {"connected": True, "read_only": False, "demo": True}, spawned)
    mod = load_entry()
    mod.READ_ONLY_WAIT = 0.3
    with pytest.raises(supervisor.SupervisorError, match="read-only"):
        mod.cmd_demo()
    assert "stopped" in spawned, "the bridge we started must not be left running read-write"


def test_demo_refuses_when_the_bridge_cannot_be_asked(hermes_home, monkeypatch):
    make_home(hermes_home)
    spawned: list = []
    fake_runtime(monkeypatch, {}, spawned)
    monkeypatch.setattr(
        client, "call", lambda *a, **k: {"ok": False, "error": "bridge-not-running"} if a[1] == "/status" else {"ok": True}
    )
    mod = load_entry()
    mod.READ_ONLY_WAIT = 0.3
    with pytest.raises(supervisor.SupervisorError, match="read-only"):
        mod.cmd_demo()


def test_demo_does_not_reuse_a_running_read_write_bridge(hermes_home, monkeypatch):
    make_home(hermes_home)
    spawned: list = []
    fake_runtime(monkeypatch, {"connected": True, "read_only": False, "demo": False}, spawned)
    config.write_runtime({"pid": 1234, "port": 1, "token": "t"}, hermes_home)
    monkeypatch.setattr(config, "pid_alive", lambda pid: True)
    with pytest.raises(supervisor.SupervisorError, match="3d down"):
        load_entry().cmd_demo()
    assert spawned == []


def test_demo_without_an_engine_says_what_is_missing(hermes_home, capsys):
    make_home(hermes_home)
    mod = load_entry()
    assert mod._cli(type("A", (), {"hermes3d_cmd": "demo"})()) == 1
    assert "engine_path" in capsys.readouterr().err


def test_demo_is_a_cli_subcommand(hermes_home):
    import argparse

    mod = load_entry()
    p = argparse.ArgumentParser()
    mod._setup_cli(p)
    assert p.parse_args(["demo"]).hermes3d_cmd == "demo"


def test_demo_bridge_is_read_only_whatever_the_settings_and_mirrors_agents_spend_cron(hermes_home):
    (hermes_home / "config.yaml").write_text("plugins:\n  entries:\n    hermes-3d:\n      settings:\n        read_only: false\n")
    b = Bridge(hermes_home, demo=True)
    assert b.read_only is True and b.hub.status()["demo"] is True
    assert all(b.switches[f] for f in ("salud", "cron", "agentes", "gasto"))
    assert b.switches["sesiones"] is False, "session titles are not part of the demo"


def test_demo_bridge_refuses_world_requests_end_to_end(hermes_home):
    async def main():
        eng = FakeEngine()
        await eng.start()
        b = Bridge(
            hermes_home,
            demo=True,
            cron_reader=Reader(listing("cron_list_many.txt")),
            engine_url=eng.url,
            link_key=eng.secret,
            poll_seconds=0.05,
        )
        await started(b, eng)
        await eng.send(
            {
                "t": "peticion",
                "id": "p1",
                "objeto": "cron/8bc0eecea9b5",
                "verbo": "cron.pausar",
                "seguridad": "confirmar",
                "quien": {"id": "u", "nombre": "P"},
                "caduca": 9999999999999,
            }
        )
        r = await eng.wait_for(lambda f: f["t"] == "resultado")
        assert r["ok"] is False and "read-only" in r["texto"]
        await stop(b, eng)

    run(main())


def test_bridge_main_demo_flag(hermes_home, monkeypatch):
    from hermes_3d_lib import bridge_main

    seen = {}

    class Fake:
        def __init__(self, home, **kw):
            seen.update(kw)
            self.hub = type("H", (), {"token": "tok"})()

        async def run(self):
            return 0

    monkeypatch.setattr(bridge_main, "Bridge", Fake)
    monkeypatch.setattr(sys, "argv", ["bridge_main", "--demo"])
    assert bridge_main.main() == 0
    assert seen["demo"] is True


def test_spawn_bridge_passes_demo(hermes_home, monkeypatch):
    got = {}
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda argv, **kw: got.update(argv=argv) or type("P", (), {"pid": 5})())
    supervisor.spawn_bridge(hermes_home, read_only=True, demo=True)
    assert "--demo" in got["argv"]
