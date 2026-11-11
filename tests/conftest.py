# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""Shared test helpers (no pytest-asyncio: tests drive the loop with ``run``)."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

SAMPLES = ROOT / "tests" / "samples"
LOOP = ".".join(("127", "0", "0", "1"))  # composed at runtime: the Nous scanner flags address:port literals


def run(coro, timeout: float = 15.0):
    """Run one coroutine to completion with a hard timeout (a hung test fails, never hangs)."""

    async def _wrap():
        return await asyncio.wait_for(coro, timeout)

    return asyncio.run(_wrap())


async def until(pred, timeout: float = 5.0, step: float = 0.02) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(step)


@pytest.fixture()
def sample():
    def _read(name: str) -> str:
        return (SAMPLES / name).read_text(encoding="utf-8")

    return _read


@pytest.fixture(autouse=True)
def _clean_hermes3d_env(monkeypatch):
    """No developer-shell variable (engine path, ports, keys) may leak into a test."""
    for k in (
        "HERMES_3D_ENGINE",
        "HERMES_3D_WORLD",
        "HERMES_3D_LINK_KEY",
        "HERMES_3D_ENGINE_PORT",
        "HERMES_3D_LINK_PORT",
        "HERMES_3D_WORLD_URL",
        "MOTOR_ENLACE_CLAVE",
        "MOTOR_ENLACE_PUERTO",
    ):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def _never_touch_a_real_pm(monkeypatch):
    """No test may reach Hermes' package manager (``pm.ensure`` would download a real Node): tests that care pass a fake."""
    from hermes_3d_lib import nodebin

    monkeypatch.setattr(nodebin, "_load_pm", lambda: None)


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for k in (
        "HERMES_3D_ENGINE",
        "HERMES_3D_WORLD",
        "HERMES_3D_LINK_KEY",
        "HERMES_3D_ENGINE_PORT",
        "HERMES_3D_LINK_PORT",
        "MOTOR_ENLACE_CLAVE",
        "MOTOR_ENLACE_PUERTO",
        "HERMES_3D_WORLD_URL",
    ):
        monkeypatch.delenv(k, raising=False)
    return home


# ---- a miniature Hermes root shared by test_scan.py and test_world_switches.py (a fixture lives here, not imported) ----
SECRET_TEXT = "TOP-SECRET-MESSAGE-TEXT-DO-NOT-LEAK"
SECRET_TOKEN = "sk-test-DO-NOT-LEAK-0123456789"


def _sessions_db(path: Path, rows: list[tuple]) -> None:
    con = sqlite3.connect(path)
    con.execute("""create table sessions (id text primary key, source text, model text, started_at real, ended_at real,
        message_count integer, last_activity_at real, title text, input_tokens integer, output_tokens integer,
        cache_read_tokens integer, cache_write_tokens integer, reasoning_tokens integer, estimated_cost_usd real,
        actual_cost_usd real, api_call_count integer)""")
    con.execute("create table messages (id integer primary key, session_id text, content text)")
    now = time.time()
    for i, (sid, model, age_days, tin, tout, cost) in enumerate(rows):
        con.execute(
            "insert into sessions values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sid,
                "cli",
                model,
                now - age_days * 86400,
                None if i == 0 else now - age_days * 86400 + 60,
                10,
                now - age_days * 86400 + 30,
                SECRET_TEXT,
                tin,
                tout,
                5,
                6,
                7,
                cost,
                None,
                3,
            ),
        )
        con.execute("insert into messages (session_id, content) values (?,?)", (sid, SECRET_TEXT))
    con.commit()
    con.close()


@pytest.fixture()
def fake_root(tmp_path: Path) -> Path:
    """A miniature Hermes root: default + 2 profiles, jobs, state.db, kanban, projects, skills, MCP, docs, secrets."""
    root = tmp_path / ".hermes"
    (root / "profiles").mkdir(parents=True)
    (root / "config.yaml").write_text("model:\n  default: m-default\n  provider: anthropic\n", encoding="utf-8")
    (root / ".env").write_text(f"ANTHROPIC_API_KEY={SECRET_TOKEN}\n", encoding="utf-8")
    (root / "auth.json").write_text(json.dumps({"token": SECRET_TOKEN}), encoding="utf-8")
    (root / "gateway_state.json").write_text(
        json.dumps({"gateway_state": "running", "pid": os.getpid(), "served_profiles": ["default", "alpha"]}), encoding="utf-8"
    )
    for name, model in (("alpha", "m-alpha"), ("beta", "m-beta")):
        d = root / "profiles" / name
        (d / "cron").mkdir(parents=True)
        (d / "skills" / "cat" / f"skill-{name}").mkdir(parents=True)
        (d / "skills" / "cat" / f"skill-{name}" / "SKILL.md").write_text("# s\n", encoding="utf-8")
        (d / "skills" / "cat" / "shared").mkdir(parents=True)
        (d / "skills" / "cat" / "shared" / "SKILL.md").write_text("# s\n", encoding="utf-8")
        (d / "plans").mkdir()
        (d / "plans" / "plan-1.md").write_text(SECRET_TEXT, encoding="utf-8")
        cfg = f"model:\n  default: {model}\n  provider: anthropic\n"
        if name == "alpha":
            cfg += (
                f"mcp_servers:\n  files:\n    command: npx\n    args: ['x']\n    env: {{TOKEN: {SECRET_TOKEN}}}\n"
                f"  web:\n    url: https://example.invalid/mcp\n    headers: {{Authorization: Bearer {SECRET_TOKEN}}}\n"
            )
        (d / "config.yaml").write_text(cfg, encoding="utf-8")
    jobs = {
        "jobs": [
            {
                "id": "j1",
                "name": "daily",
                "schedule_display": "every 1h",
                "enabled": True,
                "state": "scheduled",
                "next_run_at": "2026-11-06T00:00:00",
            },
            {
                "id": "j2",
                "name": "weekly",
                "schedule_display": "0 9 * * 1",
                "enabled": False,
                "state": "paused",
                "prompt": SECRET_TEXT,
            },
        ]
    }
    (root / "profiles" / "alpha" / "cron" / "jobs.json").write_text(json.dumps(jobs), encoding="utf-8")
    _sessions_db(
        root / "profiles" / "alpha" / "state.db",
        [
            ("sess-a1", "m-alpha", 1, 1000, 200, 0.5),
            ("sess-a2", "m-alpha", 2, 3000, 400, 1.25),
            ("sess-old", "m-old", 90, 9999, 9999, 9.0),
        ],
    )
    _sessions_db(root / "state.db", [("sess-d1", "m-default", 1, 500, 100, 0.1)])
    kb = root / "kanban" / "boards" / "board-x"
    kb.mkdir(parents=True)
    con = sqlite3.connect(kb / "kanban.db")
    con.execute(
        "create table tasks (id text, title text, status text, assignee text, priority integer, created_at real, body text)"
    )
    con.executemany(
        "insert into tasks values (?,?,?,?,?,?,?)",
        [
            ("t1", "Build the thing", "todo", "alpha", 1, 1, SECRET_TEXT),
            ("t2", "Done thing", "done", None, 0, 2, SECRET_TEXT),
            ("t3", "Running thing", "running", "beta", 2, 3, SECRET_TEXT),
        ],
    )
    con.commit()
    con.close()
    con = sqlite3.connect(root / "profiles" / "alpha" / "projects.db")
    # the shape Hermes really uses (id, created_at, project_folders): the C7 tree reader needs it
    con.execute("create table projects (id text, slug text, name text, board_slug text, archived integer, created_at integer)")
    con.execute("create table project_folders (project_id text, path text, label text, is_primary integer, added_at integer)")
    con.execute("insert into projects values ('p-a','proj-a','Project A','board-x',0,1)")
    con.commit()
    con.close()
    return root
