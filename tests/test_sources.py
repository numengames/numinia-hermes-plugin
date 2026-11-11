# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""Where the scan reads from: direct database reads are opt-in, switch-gated and change-driven (review point 1 and 2)."""

from __future__ import annotations

import os
import sqlite3
import time

import yaml
from conftest import LOOP, ROOT, run

from hermes_3d_lib import bridge_main, scan
from hermes_3d_lib import settings as S

DB_SECTIONS = ("sesiones", "kanban", "proyectos", "arbol", "gasto")


def _count_opens(monkeypatch) -> list[str]:
    opened: list[str] = []
    real = scan._open_ro

    def spy(path):
        opened.append(str(path))
        return real(path)

    monkeypatch.setattr(scan, "_open_ro", spy)
    return opened


# ---- the opt-in -------------------------------------------------------------------------------------------
def test_direct_db_read_is_off_by_default_everywhere():
    assert S.DEFAULTS["allow_direct_db_read"] is False
    schema = yaml.safe_load((ROOT / "plugin.yaml").read_text())["config_schema"]
    assert schema["allow_direct_db_read"]["default"] is False
    assert S.Hermes3DSettings.from_getter(lambda k, d=None: d, ROOT).allow_direct_db_read is False
    assert S.Hermes3DSettings.from_getter(lambda k, d=None: {"allow_direct_db_read": "on"}.get(k, d), ROOT).allow_direct_db_read


def test_scan_without_the_opt_in_opens_no_database(fake_root, monkeypatch):
    opened = _count_opens(monkeypatch)
    snap = scan.scan_hermes(fake_root)
    assert opened == []
    for name in DB_SECTIONS:
        sec = snap["sections"][name]
        assert sec["ok"] is True and sec["skipped"] == "direct_db_read_off", name
    assert snap["sections"]["agentes"]["count"] == 3  # file-based sections still work
    s = scan.summary(snap)
    assert s["tokens"] == 0 and s["tarjetas"] == 0 and s["sesiones_total"] == 0


def test_scan_with_the_opt_in_reads_as_before(fake_root):
    s = scan.summary(scan.scan_hermes(fake_root, allow_direct_db=True))
    assert s["sesiones_total"] == 4 and s["tarjetas"] == 3 and s["tokens"] > 0


def test_only_none_means_everything_and_empty_means_nothing(fake_root):
    assert set(scan.scan_hermes(fake_root, only=())["sections"]) == set()
    assert set(scan.scan_hermes(fake_root, only=None)["sections"]) == set(scan.SECTIONS)


# ---- the change signal ------------------------------------------------------------------------------------
def test_second_scan_reuses_database_sections_while_files_are_unchanged(fake_root, monkeypatch):
    cache: dict = {}
    first = scan.scan_hermes(fake_root, allow_direct_db=True, cache=cache)
    opened = _count_opens(monkeypatch)
    second = scan.scan_hermes(fake_root, allow_direct_db=True, cache=cache)
    assert opened == [], "an unchanged state.db/kanban.db must not be queried again"
    assert second["sections"]["gasto"]["from_cache"] is True
    assert second["sections"]["gasto"]["tokens"] == first["sections"]["gasto"]["tokens"]
    assert scan.summary(second)["tarjetas"] == scan.summary(first)["tarjetas"]


def test_a_changed_kanban_db_requeries_only_the_kanban(fake_root, monkeypatch):
    cache: dict = {}
    scan.scan_hermes(fake_root, allow_direct_db=True, cache=cache)
    kb = fake_root / "kanban" / "boards" / "board-x" / "kanban.db"
    con = sqlite3.connect(kb)
    con.execute("insert into tasks values ('t9','New card','todo','alpha',1,9,'x')")
    con.commit()
    con.close()
    st = kb.stat()
    os.utime(kb, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))  # a coarse-mtime filesystem must still see it
    opened = _count_opens(monkeypatch)
    snap = scan.scan_hermes(fake_root, allow_direct_db=True, cache=cache)
    assert opened and all(p.endswith("kanban.db") for p in opened)
    assert snap["sections"]["kanban"]["tarjetas"] == 4 and "from_cache" not in snap["sections"]["kanban"]
    assert snap["sections"]["gasto"]["from_cache"] is True


def test_a_wal_commit_counts_as_a_change(fake_root, monkeypatch):
    """In WAL mode a commit lands in ``state.db-wal`` first; the main file's mtime may not move for hours."""
    cache: dict = {}
    scan.scan_hermes(fake_root, allow_direct_db=True, cache=cache)
    (fake_root / "state.db-wal").write_bytes(b"x" * 64)
    opened = _count_opens(monkeypatch)
    scan.scan_hermes(fake_root, allow_direct_db=True, cache=cache)
    assert any(p.endswith("state.db") for p in opened)


def test_a_cached_section_expires_so_the_sliding_window_stays_honest(fake_root, monkeypatch):
    cache: dict = {}
    scan.scan_hermes(fake_root, allow_direct_db=True, cache=cache)
    opened = _count_opens(monkeypatch)
    real = time.time
    monkeypatch.setattr(scan.time, "time", lambda: real() + scan.CACHE_MAX_AGE + 1)
    scan.scan_hermes(fake_root, allow_direct_db=True, cache=cache)
    assert opened, "after CACHE_MAX_AGE the window is recomputed even if nothing changed"


def test_a_different_window_never_reuses_the_cache(fake_root, monkeypatch):
    cache: dict = {}
    scan.scan_hermes(fake_root, days=30, allow_direct_db=True, cache=cache)
    opened = _count_opens(monkeypatch)
    scan.scan_hermes(fake_root, days=7, allow_direct_db=True, cache=cache)
    assert opened


def test_signature_is_cheap_stat_only(fake_root, monkeypatch):
    opened = _count_opens(monkeypatch)
    sig = scan.db_signature(fake_root, "gasto")
    assert opened == [] and sig, "the signature is built from os.stat only"
    assert scan.db_signature(fake_root, "agentes") == ()  # file sections have no database to watch


# ---- the bridge: only what a switch asks for --------------------------------------------------------------
def _bridge(tmp_path, fake_root, **kw):
    return bridge_main.Bridge(tmp_path / "home", engine_url=f"ws://{LOOP}:1", link_key="k", scan_root=fake_root, **kw)


def test_bridge_scans_nothing_when_only_health_is_on(tmp_path, fake_root, monkeypatch):
    opened = _count_opens(monkeypatch)
    b = _bridge(tmp_path, fake_root, allow_direct_db=True)
    assert b.scan_sections() == ()
    run(b.run_scan())
    assert opened == [] and b.scan["sections"] == {}


def test_bridge_scans_the_section_of_each_switch_that_is_on(tmp_path, fake_root):
    b = _bridge(tmp_path, fake_root, allow_direct_db=True)
    b.hub.switches.update({"agentes": True, "kanban": True})
    assert set(b.scan_sections()) == {"agentes", "kanban"}
    b.hub.switches.update({"gasto": True, "habilidades": True})
    assert set(b.scan_sections()) == {"agentes", "kanban", "gasto", "skills"}


def test_bridge_without_the_opt_in_never_opens_a_database_even_with_switches_on(tmp_path, fake_root, monkeypatch):
    opened = _count_opens(monkeypatch)
    b = _bridge(tmp_path, fake_root)  # allow_direct_db defaults to False
    b.hub.switches.update({"agentes": True, "kanban": True, "gasto": True})
    run(b.run_scan())
    assert opened == []
    assert b.scan["sections"]["gasto"]["skipped"] == "direct_db_read_off"


def test_bridge_second_refresh_does_not_query_unchanged_databases(tmp_path, fake_root, monkeypatch):
    b = _bridge(tmp_path, fake_root, allow_direct_db=True)
    b.hub.switches.update({"kanban": True, "gasto": True})
    run(b.run_scan())
    opened = _count_opens(monkeypatch)
    run(b.run_scan())
    assert opened == []
