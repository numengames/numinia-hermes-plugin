# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""P3-5 (C5, C6): specific exceptions in leaves, loud boundaries, and a ``last_error`` that says what happened."""

from __future__ import annotations

import logging
from pathlib import Path

from conftest import run, until
from fake_engine import FakeEngine
from test_bridge import Reader, listing, make_bridge, started, stop

from hermes_3d_lib import logsafe, scan


def test_describe_keeps_the_class_and_a_redacted_bounded_message():
    out = logsafe.describe(ValueError("bad thing token=abcdef123456789 happened"))
    assert out.startswith("ValueError: bad thing") and "abcdef123456789" not in out and "[redacted]" in out
    assert len(logsafe.describe(RuntimeError("x" * 900))) <= 140
    assert logsafe.describe(KeyError()) == "KeyError"
    assert "\n" not in logsafe.describe(OSError("a\nb\x00c"))


def test_a_leaf_with_an_expected_failure_is_a_section_error_not_a_crash(tmp_path):
    root = tmp_path / ".hermes"
    (root / "profiles" / "p1").mkdir(parents=True)
    (root / "profiles" / "p1" / "state.db").write_text("this is not sqlite")
    snap = scan.scan_hermes(root, allow_direct_db=True)
    assert snap["sections"]["sesiones"]["error"] or snap["sections"]["sesiones"].get("ok") is not None


def test_a_bug_in_a_section_is_logged_with_a_traceback_and_does_not_hide_the_others(caplog):
    def boom():
        raise RuntimeError("a real bug")

    with caplog.at_level(logging.WARNING):
        out = scan._guard("demo", boom)
    assert out["ok"] is False and "RuntimeError" in out["error"] and "a real bug" in out["error"]
    assert any(r.exc_info for r in caplog.records), "a bug must leave a traceback in the log"


def test_unexpected_exception_in_a_leaf_is_not_swallowed(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    (root / "profiles" / "p1").mkdir(parents=True)
    (root / "profiles" / "p1" / "config.yaml").write_text("model: x\n")
    monkeypatch.setattr(scan, "_jobs_file", lambda pdir: (_ for _ in ()).throw(RuntimeError("bug in _jobs_file")))
    snap = scan.scan_hermes(root, only=("cron",))
    assert snap["sections"]["cron"]["ok"] is False and "bug in _jobs_file" in snap["sections"]["cron"]["error"]


def test_poll_failure_puts_the_redacted_message_in_last_error(hermes_home):
    async def main():
        eng = FakeEngine()
        await eng.start()

        def reader():
            raise RuntimeError("cannot read secret=hunter2hunter2 now")

        b = make_bridge(hermes_home, eng, reader)
        await started(b, eng)
        await until(lambda: "cron poll" in b.hub.last_error)
        assert "RuntimeError" in b.hub.last_error and "cannot read" in b.hub.last_error
        assert "hunter2hunter2" not in b.hub.last_error
        await stop(b, eng)

    run(main())


def test_http_500_records_the_cause(hermes_home, monkeypatch):
    async def main():
        eng = FakeEngine()
        await eng.start()
        b = make_bridge(hermes_home, eng, Reader(listing("cron_list_many.txt")))
        port = await started(b, eng)

        async def broken(method, path, raw):
            raise ValueError("route exploded")

        monkeypatch.setattr(b.hub, "_route", broken)
        from test_bridge import _http

        st, _ = await _http(port, b.hub.token, "GET", "/status")
        assert st == 500
        assert "http handler: ValueError: route exploded" in b.hub.last_error
        await stop(b, eng)

    run(main())


def test_no_bare_broad_except_is_left_without_logging():
    """C5 as a contract: every ``except Exception`` in the library either logs with a traceback or re-raises."""
    import re

    lib = Path(scan.__file__).parent
    offenders = []
    for py in lib.glob("*.py"):
        lines = py.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if re.match(r"\s*except (Exception|BaseException)\b", line):
                window = "\n".join(lines[i : i + 6])
                if "exc_info" not in window and "raise" not in window and "logger.exception" not in window:
                    offenders.append(f"{py.name}:{i + 1}")
    assert offenders == []
