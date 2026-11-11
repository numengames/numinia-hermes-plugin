"""Failure paths: engine start-up errors, the spawned bridge's environment, the client, and bridge main()."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest
from conftest import LOOP, run
from test_supervisor import fake_engine_checkout, settings_for

from hermes_3d_lib import bridge_main, client, config, linkkey, nodebin, procid, supervisor
from hermes_3d_lib import settings as S
from hermes_3d_lib.errors import NodeMissing

FAKE_NODE = nodebin.NodeRuntime("/usr/bin/node", (26, 12, 1), "pm", "")


class Proc:
    pid = os.getpid()

    def __init__(self, code=None):
        self.returncode = code

    def poll(self):
        return self.returncode


def test_engine_that_exits_at_once_is_reported_with_its_log(hermes_home, tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *a, **k: Proc(3))
    monkeypatch.setattr(supervisor, "node_runtime", lambda **kw: FAKE_NODE)
    monkeypatch.setattr(supervisor, "link_listening", lambda *a, **k: False)
    with pytest.raises(supervisor.SupervisorError, match=r"exited with code 3.*engine\.log"):
        supervisor.start_engine(settings_for(hermes_home, engine=fake_engine_checkout(tmp_path / "e")))


def test_engine_that_never_listens_times_out_and_is_stopped(hermes_home, tmp_path, monkeypatch):
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        monkeypatch.setattr(supervisor, "ENGINE_START_TIMEOUT", 0.3)
        monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *a, **k: sleeper)
        monkeypatch.setattr(supervisor, "node_runtime", lambda **kw: FAKE_NODE)
        monkeypatch.setattr(supervisor, "link_listening", lambda *a, **k: False)
        with pytest.raises(supervisor.SupervisorError, match="did not open"):
            supervisor.start_engine(settings_for(hermes_home, engine=fake_engine_checkout(tmp_path / "e")))
        assert sleeper.wait(timeout=5) == -signal.SIGTERM
    finally:
        sleeper.kill()


def test_missing_node_is_a_clean_error(monkeypatch):
    monkeypatch.setattr(nodebin.shutil, "which", lambda name: None)  # conftest: no pm in tests, so PATH is the fallback
    with pytest.raises(NodeMissing, match="NODE_MISSING"):
        supervisor.node_binary()


def test_stubborn_process_gets_sigkill_after_the_grace_period(hermes_home):
    code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('up', flush=True); time.sleep(60)"
    proc = subprocess.Popen([sys.executable, "-c", code], start_new_session=True, stdout=subprocess.PIPE)
    try:
        assert proc.stdout.readline().strip() == b"up"
        config.write_engine_pid(proc.pid, hermes_home)
        record = config.read_engine_record(hermes_home)
        assert supervisor.stop_engine(record, grace=0.4) is True
        assert proc.wait(timeout=5) == -signal.SIGKILL
    finally:
        proc.kill()


def test_stop_engine_without_a_record_is_a_no_op():
    assert supervisor.stop_engine(None) is True


def test_spawned_bridge_gets_a_minimal_environment(hermes_home, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-the-bridge")
    monkeypatch.setenv(linkkey.ENV_LINK_KEY, "k" * 24)
    seen = {}

    def popen(argv, **kw):
        seen.update(argv=argv, **kw)
        return type("P", (), {"pid": 4242})()

    monkeypatch.setattr(supervisor.subprocess, "Popen", popen)
    assert supervisor.spawn_bridge(hermes_home) == 4242
    assert "OPENAI_API_KEY" not in seen["env"] and seen["env"]["HERMES_HOME"] == str(hermes_home)
    assert seen["env"][linkkey.ENV_LINK_KEY] == "k" * 24 and "k" * 24 not in " ".join(seen["argv"])
    assert seen["argv"][1:] == ["-m", "hermes_3d_lib.bridge_main"] and seen["start_new_session"] is True


def test_procid_identity_of_this_process_matches_itself_and_not_a_changed_record():
    ident = procid.identity(os.getpid())
    assert ident and ident["cmd"] and procid.same_process(os.getpid(), ident)
    assert not procid.same_process(os.getpid(), {**ident, "start": "0"})
    assert not procid.same_process(os.getpid(), {**ident, "cmd": "something else"})
    assert not procid.same_process(os.getpid(), None) and not procid.own_group(2**30)


def test_procid_falls_back_to_ps_when_proc_is_unreadable(monkeypatch):
    real_read_text = procid.Path.read_text

    def no_proc(self, *a, **k):
        if str(self).startswith("/proc/"):
            raise OSError("no /proc here")
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(procid.Path, "read_text", no_proc)
    ident = procid.identity(os.getpid())
    assert ident is None or (ident["start"] and ident["cmd"])
    monkeypatch.setattr(procid.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no ps")))
    assert procid.identity(os.getpid()) is None


def test_client_reports_missing_stale_and_unreachable_bridges(hermes_home):
    assert client.call("GET", "/status", home=hermes_home) == {"ok": False, "error": "bridge-not-running"}
    config.write_runtime({"pid": 2**22 + 7, "port": 9, "token": "t" * 16}, hermes_home)
    assert client.call("GET", "/status", home=hermes_home)["error"] == "bridge-stale"
    config.write_runtime({"pid": os.getpid(), "port": 9, "token": "t" * 16}, hermes_home)
    assert client.call("GET", "/status", home=hermes_home)["error"].startswith("bridge-unreachable")


def test_client_talks_to_a_real_hub_and_sends_json(hermes_home):
    from hermes_3d_lib.hub import Hub

    async def main():
        hub = Hub(token="tok-0123456789abcdef", engine_url=None, link_key=None, switches={"salud": True})
        port = await hub.start()
        config.write_runtime({"pid": os.getpid(), "port": port, "token": hub.token}, hermes_home)
        try:
            return await asyncio.get_running_loop().run_in_executor(None, lambda: client.call("GET", "/status", home=hermes_home))
        finally:
            await hub.stop()

    import asyncio

    res = run(main())
    assert res["ok"] is True and res["data"]["connected"] is False


def test_bridge_main_refuses_invalid_settings_and_logs_privately(hermes_home, monkeypatch):
    (hermes_home / "config.yaml").write_text("plugins:\n  entries:\n    hermes-3d:\n      settings:\n        engine_port: 12\n")
    logger_root = bridge_main.logging.getLogger()
    saved = list(logger_root.handlers)
    try:
        assert bridge_main.main() == 2
    finally:
        for h in list(logger_root.handlers):
            logger_root.removeHandler(h)
        for h in saved:
            logger_root.addHandler(h)
    log = hermes_home / "plugin-data" / "hermes-3d" / "bridge.log"
    assert "engine_port" in log.read_text() and (log.stat().st_mode & 0o777) == 0o600


def test_bridge_main_runs_with_valid_settings(hermes_home, monkeypatch):
    called = {}

    async def fake_run(self):
        called["url"], called["key"], called["page"] = self.hub.engine_url, self.hub.link_key, self.page_url
        return 0

    monkeypatch.setattr(bridge_main.Bridge, "run", fake_run)
    key = linkkey.ensure(hermes_home)
    logger_root = bridge_main.logging.getLogger()
    saved = list(logger_root.handlers)
    try:
        assert bridge_main.main() == 0
    finally:
        for h in list(logger_root.handlers):
            logger_root.removeHandler(h)
        for h in saved:
            logger_root.addHandler(h)
    assert called["key"] == key and called["url"].startswith(f"ws://{LOOP}:8791") and called["page"] == f"http://{LOOP}:8790/"
    assert S.Hermes3DSettings.from_yaml(hermes_home).link_port == 0


def test_cron_list_runner_uses_an_argv_list(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(bridge_main.shutil, "which", lambda n: "/usr/bin/hermes")
    monkeypatch.setattr(bridge_main.subprocess, "run", fake_run)
    assert bridge_main.run_cron_list() == "ok"
    assert seen["argv"] == ["/usr/bin/hermes", "cron", "list", "--all"] and not seen["kw"].get("shell")
    monkeypatch.setattr(bridge_main.shutil, "which", lambda n: None)
    assert bridge_main.run_cron_list() is None


def test_runtime_file_is_private_and_removed(hermes_home):
    config.write_runtime({"pid": 1, "port": 2, "token": "x" * 16}, hermes_home)
    assert (config.runtime_path(hermes_home).stat().st_mode & 0o777) == 0o600
    config.clear_runtime(hermes_home)
    config.clear_runtime(hermes_home)  # idempotent
    assert config.read_runtime(hermes_home) is None
    time.sleep(0)  # keep the import used
    json.dumps({})
