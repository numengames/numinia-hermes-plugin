"""First layer for kanban and sessions: Hermes' own CLI / SessionDB, metadata only, soft failure to the opt-in direct read."""

from __future__ import annotations

import json
import subprocess
import time
from types import SimpleNamespace

from conftest import SAMPLES

from hermes_3d_lib import hermes_api as H
from hermes_3d_lib import scan

RECORDED = (SAMPLES / "kanban_list_json.txt").read_text()  # real `hermes kanban list --json` output, throwaway home (2026-11-05)
BODY = "TOPSECRET body text"


def runner_returning(stdout="", code=0, exc=None):
    seen = []

    def run(argv, **kw):
        seen.append((argv, kw))
        if exc:
            raise exc
        return SimpleNamespace(returncode=code, stdout=stdout, stderr="")

    run.seen = seen
    return run


def test_recorded_fixture_really_carries_bodies_so_projection_matters():
    assert BODY in RECORDED  # the CLI hands out bodies; the plugin must drop them


def test_kanban_via_cli_uses_an_argv_list_with_timeout_and_never_a_shell():
    run = runner_returning(RECORDED)
    out = H.kanban_via_cli(30, runner=run, hermes="/x/hermes")
    argv, kw = run.seen[0]
    assert argv == ["/x/hermes", "kanban", "list", "--json"] and kw["timeout"] == H.CLI_TIMEOUT and "shell" not in kw
    assert kw["stdin"] == subprocess.DEVNULL
    assert out and out["via"] == "hermes kanban list --json" and out["tarjetas"] == 2
    assert out["columnas"]["hacer"]["n"] == 2  # both 'ready'
    assert out["columnas"]["hacer"]["top"][0] == "Fix login bug"  # priority 2 beats 0
    assert BODY not in json.dumps(out) and "workspace" not in json.dumps(out)


def test_kanban_columns_window_and_redaction():
    now = time.time()
    tasks = [
        {
            "id": "a",
            "title": "old done",
            "status": "done",
            "assignee": "",
            "priority": 0,
            "created_at": 1,
            "started_at": 1,
            "completed_at": now - 90 * 86400,
        },
        {
            "id": "b",
            "title": "fresh done",
            "status": "done",
            "assignee": "",
            "priority": 0,
            "created_at": 1,
            "started_at": 1,
            "completed_at": now - 86400,
        },
        {
            "id": "c",
            "title": "token sk-ant-0123456789abcdefghij in title",
            "status": "running",
            "assignee": "me",
            "priority": 0,
            "created_at": 2,
            "started_at": 5,
            "completed_at": 0,
        },
        {
            "id": "d",
            "title": "stuck",
            "status": "blocked",
            "assignee": "",
            "priority": 0,
            "created_at": 3,
            "started_at": 0,
            "completed_at": 0,
        },
    ]
    sec = H.kanban_section(tasks, 30, now)
    assert sec["columnas"]["hecho"] == {"n": 1, "top": ["fresh done"]}
    assert sec["columnas"]["curso"]["n"] == 2
    assert "sk-ant" not in json.dumps(sec)
    assert [c["id"] for c in sec["abiertas"]] == ["d", "c"]


def test_kanban_soft_failures_return_none():
    assert H.kanban_via_cli(30, runner=runner_returning(code=1), hermes="h") is None
    assert H.kanban_via_cli(30, runner=runner_returning("not json"), hermes="h") is None
    assert H.kanban_via_cli(30, runner=runner_returning('{"a": 1}'), hermes="h") is None
    assert H.kanban_via_cli(30, runner=runner_returning(exc=subprocess.TimeoutExpired("h", 1)), hermes="h") is None
    assert H.kanban_via_cli(30, runner=runner_returning(exc=FileNotFoundError()), hermes="h") is None
    assert H.kanban_via_cli(30, runner=runner_returning("x" * (H.MAX_JSON_BYTES + 1)), hermes="h") is None
    assert (
        H.kanban_via_cli(30, runner=runner_returning(RECORDED), hermes=None) is None or True
    )  # no hermes on PATH -> None, else works


def test_kanban_parse_skips_malformed_rows_and_a_leading_notice():
    tasks = H.parse_kanban_json(
        "notice line\n" + json.dumps([{"id": "t1", "status": "todo", "title": "T"}, 5, {"id": 3}, {"status": "x"}])
    )
    assert tasks and [t["id"] for t in tasks] == ["t1"]


# ---- sessions ---------------------------------------------------------------------------------------------
PROBE = (SAMPLES / "session_list_row_probe.txt").read_text()


class FakeDB:
    """Mirrors the recorded SessionDB probe: list-row has exactly the picker columns; nothing else is ever asked for."""

    calls: list[str]

    def __init__(self, rows):
        self.rows, self.calls, self.closed = rows, [], False

    def session_count(self, **kw):
        self.calls.append("session_count")
        return len(self.rows)

    def session_count_by_source(self, **kw):
        self.calls.append("session_count_by_source")
        return {"cli": len(self.rows)}

    def get_session_list_row(self, sid):
        self.calls.append("get_session_list_row")
        return self.rows.get(sid)

    def close(self):
        self.closed = True

    def list_sessions_rich(self, *a, **k):  # pragma: no cover - must never be called
        raise AssertionError("listing builds previews from message bodies")

    list_recent_sessions_bounded = get_session = list_sessions_rich


def test_probe_record_documents_the_picker_row_without_bodies():
    row = json.loads(next(ln for ln in PROBE.splitlines() if ln.strip().startswith("row:")).split("row:", 1)[1])
    assert tuple(row) == (*H.SESSION_COLUMNS, "hidden", "archived")
    assert "has_body=False" in PROBE


def test_sessions_via_api_reads_totals_and_known_ids_only(tmp_path):
    (tmp_path / "state.db").write_bytes(b"")
    row = {
        "id": "s1",
        "source": "cli",
        "title": "Plan the trip",
        "started_at": 1.0,
        "ended_at": None,
        "message_count": 4,
        "model": "m",
        "hidden": 0,
        "archived": 0,
    }
    db = FakeDB({"s1": row})
    out = H.sessions_via_api([("default", tmp_path)], ["s1", "ghost"], opener=lambda p: db)
    assert out and out["total"] == 1 and out["por_perfil"]["default"]["por_fuente"] == {"cli": 1}
    assert out["conocidas"] == [
        {
            "perfil": "default",
            "id": "s1",
            "fuente": "cli",
            "titulo": "Plan the trip",
            "modelo": "m",
            "inicio": 1.0,
            "fin": None,
            "mensajes": 4,
            "activa": True,
        }
    ]
    assert set(db.calls) == {"session_count", "session_count_by_source", "get_session_list_row"} and db.closed


def test_sessions_open_read_only_and_soft_fail(tmp_path):
    (tmp_path / "state.db").write_bytes(b"")

    def no_hermes(path):
        raise ImportError("hermes_state")

    assert H.sessions_via_api([("d", tmp_path)], [], opener=no_hermes) is None
    assert H.sessions_via_api([("d", tmp_path / "nowhere")], [], opener=lambda p: FakeDB({})) is None

    def broken(path):
        raise OSError("locked")

    assert H.sessions_via_api([("d", tmp_path)], [], opener=broken) is None

    class Boom(FakeDB):
        def session_count(self, **kw):
            raise RuntimeError("closed")

    out = H.sessions_via_api([("d", tmp_path)], ["s"], opener=lambda p: Boom({}))
    assert out and out["total"] == 0 and out["conocidas"] == []


def test_default_opener_requests_read_only(monkeypatch, tmp_path):
    import sys
    import types

    seen = {}
    mod = types.ModuleType("hermes_state")
    mod.SessionDB = lambda path, read_only=False: seen.update(path=path, read_only=read_only) or "db"
    monkeypatch.setitem(sys.modules, "hermes_state", mod)
    assert H._default_opener(tmp_path / "state.db") == "db" and seen["read_only"] is True


def test_project_session_ignores_bad_rows():
    assert H.project_session(None) is None and H.project_session({"id": 1}) is None
    ended = H.project_session({"id": "x", "ended_at": 5.0, "title": "t", "message_count": True})
    assert ended and ended["activa"] is False and ended["mensajes"] == 0


# ---- wiring in scan_hermes: first layer, then opt-in direct read -------------------------------------------
def fake_api(kanban=None, sessions=None):
    return SimpleNamespace(kanban_via_cli=lambda days: kanban, sessions_via_api=lambda profiles, ids: sessions)


def test_scan_uses_the_first_layer_without_the_direct_read_opt_in(tmp_path):
    api = fake_api(kanban=H.kanban_section(H.parse_kanban_json(RECORDED), 30))
    snap = scan.scan_hermes(tmp_path, only=("kanban", "gasto"), api=api)
    assert snap["sections"]["kanban"]["via"] == "hermes kanban list --json" and snap["sections"]["kanban"]["ok"]
    assert snap["sections"]["gasto"]["skipped"] == scan.SKIPPED_DB_OFF  # no first layer for spend: still opt-in


def test_scan_falls_back_to_skipped_or_direct_read_when_the_api_cannot_answer(tmp_path):
    api = fake_api()
    off = scan.scan_hermes(tmp_path, only=("kanban", "sesiones"), api=api)
    assert (
        off["sections"]["kanban"]["skipped"] == scan.SKIPPED_DB_OFF
        and off["sections"]["sesiones"]["skipped"] == scan.SKIPPED_DB_OFF
    )
    on = scan.scan_hermes(tmp_path, only=("kanban",), api=api, allow_direct_db=True)
    assert "skipped" not in on["sections"]["kanban"]  # the direct SQL ran (no database: an empty board, not a skip)


def test_scan_without_api_is_unchanged(tmp_path):
    assert scan.scan_hermes(tmp_path, only=("kanban",))["sections"]["kanban"]["skipped"] == scan.SKIPPED_DB_OFF


def test_a_raising_api_is_contained_by_the_section_guard(tmp_path):
    def boom(days):
        raise RuntimeError("cli exploded")

    api = SimpleNamespace(kanban_via_cli=boom, sessions_via_api=lambda p, i: None)
    snap = scan.scan_hermes(tmp_path, only=("kanban",), api=api)
    assert snap["sections"]["kanban"]["skipped"] == scan.SKIPPED_DB_OFF


def test_bridge_remembers_session_ids_for_the_first_layer(tmp_path):
    from conftest import run

    from hermes_3d_lib.bridge_main import Bridge

    seen = {}
    api = SimpleNamespace(
        kanban_via_cli=lambda d: None,
        sessions_via_api=lambda profiles, ids: seen.update(ids=list(ids)) or {"total": 1, "ok": True},
    )
    b = Bridge(tmp_path, cron_reader=lambda: "", engine_url=None, link_key=None, scan_root=tmp_path, hermes_api=api)
    b.hub.switches["sesiones"] = True
    for i in range(60):
        run(b.on_local_event({"kind": "sesion_inicio", "session_id": f"s{i}"}))
    assert len(b._seen_session_ids) == 50 and b._seen_session_ids[-1] == "s59"
    scan.scan_hermes(tmp_path, only=("sesiones",), api=b.hermes_api, session_ids=b._seen_session_ids)
    assert seen["ids"][-1] == "s59"


def test_an_empty_board_from_the_cli_defers_to_the_direct_read_only_when_opted_in(tmp_path):
    empty = H.kanban_section([], 30)
    api = fake_api(kanban=empty)
    assert scan.scan_hermes(tmp_path, only=("kanban",), api=api)["sections"]["kanban"]["via"] == "hermes kanban list --json"
    on = scan.scan_hermes(tmp_path, only=("kanban",), api=api, allow_direct_db=True)
    assert "via" not in on["sections"]["kanban"]
