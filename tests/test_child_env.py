# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT
"""A `hermes` child the plugin starts never inherits an approval bypass or another session's identity (catalog rule 12)."""

from __future__ import annotations

import subprocess

from hermes_3d_lib import actions, config


def test_child_env_drops_every_approval_and_session_variable(monkeypatch):
    for name in config.NO_INHERIT:
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = config.hermes_child_env(HERMES_HOME="/h")
    assert not set(config.NO_INHERIT) & set(env)
    assert env["PATH"] == "/usr/bin" and env["NO_COLOR"] == "1" and env["HERMES_HOME"] == "/h"


def test_cron_pause_runs_without_yolo_even_when_the_agent_has_it(monkeypatch):
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    seen: dict = {}

    def runner(argv, **kw):
        seen.update(argv=argv, env=kw["env"])
        return subprocess.CompletedProcess(argv, 0, stdout="paused", stderr="")

    actions.run_cron_cli("abc123", "cron.pausar", runner=runner, hermes="/bin/hermes")
    assert seen["argv"][:3] == ["/bin/hermes", "cron", "pause"]
    assert "HERMES_YOLO_MODE" not in seen["env"]
