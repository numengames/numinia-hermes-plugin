"""Supervisor: engine start-up environment, link port, world safety, and safe stopping of processes."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys

import pytest

from hermes_palace_lib import config, linkkey, nodebin, procid, supervisor
from hermes_palace_lib import settings as S

FAKE_NODE = nodebin.NodeRuntime("/usr/bin/node", (26, 12, 1), "pm", "")


def fake_engine_checkout(root, page=True):
    (root / "packages/server/src").mkdir(parents=True)
    (root / "packages/server/src/main.ts").write_text("//")
    if page:
        (root / "dist/plaza").mkdir(parents=True)
        (root / "dist/plaza/index.html").write_text("<html>")
    return root


def settings_for(home, engine=None, **kw):
    return S.PalaceSettings(home=home, engine_path=engine, world_path=home / "plugin-data" / "hermes-palace" / "mundo", **kw)


def test_engine_not_configured_gives_actionable_error(hermes_home):
    with pytest.raises(supervisor.SupervisorError, match="engine_path"):
        supervisor.start_engine(settings_for(hermes_home))


def test_engine_folder_without_main_ts_is_rejected(hermes_home, tmp_path):
    with pytest.raises(supervisor.SupervisorError, match="does not contain"):
        supervisor.start_engine(settings_for(hermes_home, engine=tmp_path))


def test_engine_environment_serves_the_page_and_carries_the_key(hermes_home, tmp_path, monkeypatch):
    monkeypatch.setenv("SOME_API_KEY", "must-not-leak")
    eng = fake_engine_checkout(tmp_path / "engine")
    cfg = settings_for(hermes_home, engine=eng, engine_port=9100)
    env = supervisor.engine_environment(cfg, "k" * 24, 9101)
    assert env["WWW_DIR"] == str(eng / "dist" / "plaza")
    assert env["MOTOR_ENLACE_CLAVE"] == "k" * 24 and env["MOTOR_ENLACE_PUERTO"] == "9101"
    assert env["PORT"] == "9100" and env["HOST"] == "127.0.0.1" and env["MOTOR_PLANTILLA"] == "hermes"
    assert "SOME_API_KEY" not in env, "the engine gets an allow-list, not our whole environment"


def test_no_built_page_means_no_www_dir(hermes_home, tmp_path):
    cfg = settings_for(hermes_home, engine=fake_engine_checkout(tmp_path / "e", page=False))
    assert "WWW_DIR" not in supervisor.engine_environment(cfg, "k" * 24, 8791)


def test_start_engine_passes_www_dir_key_and_never_puts_the_key_on_argv(hermes_home, tmp_path, monkeypatch):
    eng = fake_engine_checkout(tmp_path / "engine")
    seen = {}

    class Proc:
        pid = os.getpid()
        returncode = None

        def poll(self):
            return None

    def popen(argv, **kw):
        seen.update(argv=argv, env=kw["env"], cwd=kw["cwd"])
        return Proc()

    monkeypatch.setattr(supervisor.subprocess, "Popen", popen)
    monkeypatch.setattr(supervisor, "node_runtime", lambda **kw: FAKE_NODE)
    monkeypatch.setattr(supervisor, "engine_healthy", lambda port, timeout=1.5: seen.get("argv") is not None)
    monkeypatch.setattr(supervisor, "link_listening", lambda port, timeout=0.6: False)
    out = supervisor.start_engine(settings_for(hermes_home, engine=eng))
    key = linkkey.resolve(hermes_home)
    assert out["reused"] is False and out["serves_page"] is True
    assert seen["env"]["WWW_DIR"].endswith("dist/plaza") and seen["env"]["MOTOR_ENLACE_CLAVE"] == key
    assert key not in " ".join(seen["argv"])
    assert (hermes_home / "plugin-data" / "hermes-palace" / "engine.log").stat().st_mode & 0o777 == 0o600


def test_running_link_is_reused_without_starting_anything(hermes_home, tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, "link_listening", lambda port, timeout=0.6: True)
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *a, **k: pytest.fail("must not start a second engine"))
    out = supervisor.start_engine(settings_for(hermes_home, engine=fake_engine_checkout(tmp_path / "e")))
    assert out["reused"] is True and out["pid"] is None


def test_link_port_setting_then_espejo_json_then_default(hermes_home, tmp_path):
    world = tmp_path / "w"
    cfg = S.PalaceSettings(home=hermes_home, world_path=world)
    assert supervisor.link_port(cfg) == 8791 and supervisor.espejo_enabled(world) is False
    (world / "entorno").mkdir(parents=True)
    (world / "entorno" / "espejo.json").write_text(
        json.dumps({"activa": True, "fuente": {"tipo": "enlace-local", "puerto": 9100}})
    )
    assert supervisor.link_port(cfg) == 9100 and supervisor.espejo_enabled(world) is True
    assert supervisor.link_port(S.PalaceSettings(home=hermes_home, world_path=world, link_port=9300)) == 9300
    (world / "entorno" / "espejo.json").write_text("{not json")
    assert supervisor.link_port(cfg) == 8791 and supervisor.espejo_enabled(world) is False


def test_world_files_are_not_read_through_a_symlink(hermes_home, tmp_path):
    world = tmp_path / "w"
    (world / "entorno").mkdir(parents=True)
    secret = tmp_path / "elsewhere.json"
    secret.write_text(json.dumps({"activa": True, "fuente": {"puerto": 9999}}))
    (world / "entorno" / "espejo.json").symlink_to(secret)
    assert supervisor.espejo_enabled(world) is False
    assert supervisor.link_port(S.PalaceSettings(home=hermes_home, world_path=world)) == 8791


def test_foreign_world_is_never_reused(tmp_path):
    w = tmp_path / "w"
    w.mkdir()
    (w / "datos.txt").write_text("mine")
    with pytest.raises(supervisor.SupervisorError, match="not a Numinia world"):
        supervisor.check_world_is_ours(w)
    (w / "mundo.json").write_text(json.dumps({"esquema": "numinia-engine/mundo@2"}))
    supervisor.check_world_is_ours(w)


# ---- process identity: never signal a recycled pid ------------------------------------------------------------
def sleeper():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)


def test_stop_engine_stops_the_recorded_process(hermes_home):
    proc = sleeper()
    try:
        config.write_engine_pid(proc.pid, hermes_home)
        record = config.read_engine_record(hermes_home)
        assert record and record["pid"] == proc.pid
        assert supervisor.stop_engine(record, grace=3) is True
        assert proc.wait(timeout=3) == -signal.SIGTERM
    finally:
        proc.kill()


def test_a_recycled_pid_is_never_signalled(hermes_home):
    stranger = sleeper()  # plays "an unrelated process that got the dead engine's pid"
    try:
        stale = {"pid": stranger.pid, "start": "1", "cmd": "node packages/server/src/main.ts"}
        assert procid.same_process(stranger.pid, stale) is False
        assert supervisor.stop_engine(stale, grace=0.2) is True
        assert stranger.poll() is None, "the stranger must still be running"
        config.engine_pid_path(hermes_home).write_text(json.dumps(stale))
        assert config.read_engine_record(hermes_home) is None
    finally:
        stranger.kill()


def test_a_bare_integer_pid_file_is_not_trusted(hermes_home):
    proc = sleeper()
    try:
        config.engine_pid_path(hermes_home).write_text(str(proc.pid))
        assert config.read_engine_record(hermes_home) is None
    finally:
        proc.kill()


def test_identity_is_none_for_dead_or_invalid_pids():
    assert procid.identity(0) is None and procid.identity(1) is None and procid.identity(2**30) is None


def test_logs_and_pid_file_are_private_whatever_the_umask(hermes_home):
    old = os.umask(0)
    try:
        with config.open_log(config.palace_dir(hermes_home) / "x.log") as fh:
            fh.write(b"x")
        config.write_engine_pid(os.getpid(), hermes_home)
    finally:
        os.umask(old)
    for name in ("x.log", "engine.pid"):
        assert (hermes_home / "plugin-data" / "hermes-palace" / name).stat().st_mode & 0o777 == 0o600
    (hermes_home / "plugin-data" / "hermes-palace" / "loose.log").write_text("a")
    (hermes_home / "plugin-data" / "hermes-palace" / "loose.log").chmod(0o666)
    config.open_log(hermes_home / "plugin-data" / "hermes-palace" / "loose.log").close()
    assert (hermes_home / "plugin-data" / "hermes-palace" / "loose.log").stat().st_mode & 0o777 == 0o600
