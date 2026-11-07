"""Cron state from ``cron/jobs.json`` first, ``hermes cron list --all`` only as the fallback (review point b)."""

from __future__ import annotations

import asyncio
import json

from conftest import run
from fake_engine import FakeEngine
from test_bridge import Reader, make_bridge, started, stop

from hermes_palace_lib import crontable

JOBS = {
    "jobs": [
        {
            "id": "8bc0eecea9b5",
            "name": "mundo-demo",
            "prompt": "SECRET PROMPT sk-ant-0123456789",
            "schedule_display": "every 60m",
            "enabled": True,
            "state": "scheduled",
            "next_run_at": "2026-11-05T12:00:00+01:00",
            "last_status": "ok",
        },
        {
            "id": "e6c4d06884ec",
            "name": "resumen\x1b[31m diario",
            "schedule_display": "every 120m",
            "enabled": True,
            "state": "scheduled",
            "last_status": "error",
            "failure_streak": 2,
        },
        {"id": "e4c43140efbc", "name": "weekly", "schedule_display": "0 9 * * *", "enabled": False, "state": "paused"},
        {"id": "da4a429b66c8", "name": "once", "schedule_display": "once", "enabled": False, "state": "completed"},
        {"id": "aaaabbbbcccc", "name": "off", "schedule_display": "daily", "enabled": False, "state": "scheduled"},
    ]
}


def write(home, data):
    (home / "cron").mkdir(parents=True, exist_ok=True)
    (home / "cron" / "jobs.json").write_text(json.dumps(data) if not isinstance(data, str) else data)


def test_states_match_what_the_cli_prints(tmp_path):
    write(tmp_path, JOBS)
    parsed = crontable.read_jobs_file(tmp_path)
    assert parsed is not None
    by = {j["id"]: j for j in parsed["jobs"]}
    assert [by[i]["badge"] for i in by] == ["active", "active", "paused", "completed", "disabled"]
    assert [by[i]["estado"] for i in by] == ["programado", "programado", "pausado", "terminado", "apagado"]
    assert by["8bc0eecea9b5"]["ultimo"] == "ok" and by["e6c4d06884ec"]["ultimo"] == "fallo"
    assert by["8bc0eecea9b5"]["schedule"] == "every 60m" and by["8bc0eecea9b5"]["next_run"].startswith("2026-11-05")
    assert "\x1b" not in by["e6c4d06884ec"]["name"]


def test_the_job_prompt_never_leaves_the_file_reader(tmp_path):
    write(tmp_path, JOBS)
    assert "SECRET" not in json.dumps(crontable.read_jobs_file(tmp_path))


def test_same_shape_as_the_cli_parser(tmp_path):
    write(tmp_path, JOBS)
    from_cli = crontable.parse_cron_list("  8bc0eecea9b5 [active]\n    Name:      x\n")["jobs"][0]
    from_file = crontable.read_jobs_file(tmp_path)["jobs"][0]
    assert set(from_file) == set(from_cli)


def test_missing_empty_or_broken_file_returns_none_so_the_cli_is_used(tmp_path):
    assert crontable.read_jobs_file(tmp_path) is None
    write(tmp_path, "{not json")
    assert crontable.read_jobs_file(tmp_path) is None
    write(tmp_path, {"jobs": "nope"})
    assert crontable.read_jobs_file(tmp_path) is None
    write(tmp_path, {"jobs": []})
    assert crontable.read_jobs_file(tmp_path) == {"jobs": [], "profile": None, "warnings": [], "empty": True}


def test_a_huge_file_is_not_read(tmp_path):
    write(tmp_path, '{"jobs": [' + '{"id": "aaaabbbbcccc"},' * 200_000 + "{}]}")
    assert crontable.read_jobs_file(tmp_path) is None


def test_bad_ids_and_duplicates_are_dropped(tmp_path):
    write(
        tmp_path,
        {"jobs": [{"id": "ok-id-1234", "name": "a"}, {"id": "ok-id-1234", "name": "dup"}, {"id": "x y;rm", "name": "bad"}, 7]},
    )
    assert [j["id"] for j in crontable.read_jobs_file(tmp_path)["jobs"]] == ["ok-id-1234"]


def test_bridge_prefers_the_file_and_never_runs_the_cli(hermes_home):
    async def main():
        eng = FakeEngine()
        await eng.start()
        write(hermes_home, JOBS)
        reader = Reader("")
        b = make_bridge(hermes_home, eng, reader)
        await started(b, eng)
        await asyncio.sleep(0.3)
        assert reader.calls == 0 and b.cron_source == "file"
        assert set(b.known_jobs) == {j["id"] for j in JOBS["jobs"]}
        await stop(b, eng)

    run(main())


def test_bridge_falls_back_to_the_cli_when_there_is_no_file(hermes_home):
    async def main():
        eng = FakeEngine()
        await eng.start()
        reader = Reader((__import__("pathlib").Path(__file__).parent / "samples" / "cron_list_one.txt").read_text())
        b = make_bridge(hermes_home, eng, reader)
        await started(b, eng)
        await asyncio.sleep(0.3)
        assert reader.calls >= 1 and b.cron_source == "cli" and b.known_jobs
        await stop(b, eng)

    run(main())
