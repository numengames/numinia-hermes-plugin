# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT
"""Starting the world sows one body per profile. Spend and Kanban titles come from Hermes' private databases, so they
travel with the bodies only under the same opt-in as everywhere else (allow_direct_db_read + the function's switch)."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from hermes_3d_lib import cuerpos, scan, supervisor


@pytest.fixture
def sown(monkeypatch, tmp_path):
    reads: list[str] = []
    sown: dict = {}
    monkeypatch.setattr(supervisor, "world_info", lambda _p: {"ok": True})
    monkeypatch.setattr(scan, "default_root", lambda: tmp_path)
    monkeypatch.setattr(scan, "profile_dirs", lambda _r: [("alpha", tmp_path)])
    monkeypatch.setattr(scan, "scan_gasto", lambda _r: reads.append("gasto") or {"perfiles": []})
    monkeypatch.setattr(scan, "scan_kanban", lambda _r: reads.append("kanban") or {"tableros": []})
    monkeypatch.setattr(
        cuerpos, "sow", lambda _w, names, spend, activity: sown.update(names=names, spend=spend, activity=activity)
    )

    def run(direct: bool, **switches: bool):
        reads.clear()
        cfg = SimpleNamespace(world_path=tmp_path, allow_direct_db_read=direct, switches={"salud": True, **switches})
        supervisor._sow_bodies(cfg)
        return reads, sown

    return run


def test_defaults_read_no_database_and_bodies_carry_names_only(sown):
    reads, out = sown(False)
    assert reads == [] and out == {"names": ["alpha"], "spend": {}, "activity": {}}


def test_switches_alone_do_not_open_the_databases(sown):
    reads, _ = sown(False, gasto=True, kanban=True)
    assert reads == []


def test_opt_in_alone_does_not_read_what_is_switched_off(sown):
    reads, _ = sown(True)
    assert reads == []


def test_opt_in_and_switch_read_exactly_that_source(sown):
    assert sown(True, gasto=True)[0] == ["gasto"]
    assert sown(True, kanban=True)[0] == ["kanban"]


def test_read_only_open_survives_uri_characters_in_the_path(tmp_path):
    """A "#" or "?" in HERMES_HOME must not end the URI early, drop mode=ro and create a file."""
    folder = tmp_path / "a#b?c"
    folder.mkdir()
    db = folder / "state.db"
    with sqlite3.connect(db) as con:
        con.execute("create table t (x)")
        con.execute("insert into t values (1)")
    con = scan._open_ro(db)
    try:
        assert con.execute("select x from t").fetchall() == [(1,)]
        with pytest.raises(sqlite3.OperationalError):
            con.execute("insert into t values (2)")
    finally:
        con.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a#b?c"]  # nothing created beside it
