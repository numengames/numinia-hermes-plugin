"""Starting and stopping the engine as a child, against a node-less stub (``stub_engine.py``)."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import ROOT

from hermes_3d_lib import config, enginekeys, nodebin, procid, supervisor
from hermes_3d_lib import settings as S
from hermes_3d_lib.errors import Hermes3DError

PYNODE = nodebin.NodeRuntime(sys.executable, (26, 12, 1), "pm", "")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture()
def rig(hermes_home, tmp_path, monkeypatch):
    """An installed-looking engine (marker + app/stub.py + www/) whose "node" is the Python interpreter."""
    eng = tmp_path / "engine"
    (eng / "app").mkdir(parents=True)
    (eng / "www").mkdir()
    (eng / "www" / "index.html").write_text("<html>")
    shutil.copy(ROOT / "tests" / "stub_engine.py", eng / "app" / "stub.py")
    (eng / ".hermes-3d-engine.json").write_text(json.dumps({"schema": 1, "sha256": "a" * 64, "entry": "stub.py"}))
    monkeypatch.setattr(supervisor, "node_runtime", lambda **kw: PYNODE)
    monkeypatch.setattr(supervisor, "ENGINE_START_TIMEOUT", 15.0)
    cfg = S.Hermes3DSettings(
        home=hermes_home, engine_path=eng, world_path=hermes_home / "plugin-data" / "hermes-3d" / "mundo",
        engine_port=free_port(), docs_viewer_port=free_port(), link_port=free_port(),
    )  # fmt: skip
    started: list[int] = []
    yield type(
        "Rig", (), {"cfg": cfg, "eng": eng, "started": started, "mode": lambda self, m: (eng / "app" / "mode").write_text(m)}
    )()
    for pid in started:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)


def start(rig, **kw):
    out = supervisor.start_engine(rig.cfg, **kw)
    if out.get("pid") and not out["reused"]:
        rig.started.append(out["pid"])
    return out


def seen(rig) -> dict:
    return json.loads((rig.eng / "app" / "seen.json").read_text())


def wait_gone(pid: int, secs: float = 8.0) -> bool:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if (
            not config.pid_alive(pid) or Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z"
            if Path(f"/proc/{pid}").exists()
            else True
        ):
            return True
        time.sleep(0.05)
    return False


def test_starts_waits_for_health_and_gives_the_child_what_it_needs(rig, monkeypatch):
    monkeypatch.setenv("SOME_API_KEY", "must-not-leak")
    out = start(rig)
    assert out["reused"] is False and out["pid"] and supervisor.engine_healthy(rig.cfg.engine_port)
    got = seen(rig)
    assert {"MOTOR_SECRET", "MOTOR_ADMIN", "MOTOR_ENLACE_CLAVE", "PORT", "HOST", "WWW_DIR"} <= set(got["has"])
    assert got["secret_len"] >= 40 and got["admin_len"] >= 20 and got["leaked"] == []
    assert got["argv"][0].endswith("app/stub.py") and not any("MOTOR" in a for a in got["argv"])
    assert out["serves_page"] is True and out["node_warnings"] == []


def test_secrets_are_stored_0600_per_install_and_never_in_the_log(rig, hermes_home):
    start(rig)
    path = enginekeys.keys_path(hermes_home)
    assert path.stat().st_mode & 0o777 == 0o600
    data = json.loads(path.read_text())
    log = (config.plugin_data_dir(hermes_home) / "engine.log").read_text()
    assert data["secret"] not in log and data["admin"] not in log
    assert enginekeys.ensure(hermes_home, "a" * 64) == (data["secret"], data["admin"])  # stable for the same install
    assert enginekeys.ensure(hermes_home, "b" * 64)[0] != data["secret"]  # a new install gets new ones


def test_keys_file_with_loose_mode_or_garbage_is_replaced(hermes_home):
    first = enginekeys.ensure(hermes_home, "x")
    path = enginekeys.keys_path(hermes_home)
    path.chmod(0o644)
    assert enginekeys.ensure(hermes_home, "x") != first
    path.write_text("{not json")
    path.chmod(0o600)
    assert len(enginekeys.ensure(hermes_home, "x")[0]) >= 40
    path.unlink()
    path.symlink_to("/etc/hostname")
    assert len(enginekeys.ensure(hermes_home, "x")[0]) >= 40 and not path.is_symlink()


def test_engine_exits_when_the_plugin_side_of_stdin_closes(rig):
    out = start(rig, hold_stdin=True)
    assert seen(rig)["exit_on_eof"] == "1" and isinstance(out["stdin_fd"], int)
    pid = out["pid"]
    assert config.pid_alive(pid)
    os.close(out["stdin_fd"])  # what happens when the bridge dies
    assert wait_gone(pid)


@pytest.mark.parametrize("kw", [{}, {"hold_stdin": False}, {"hold_stdin": True}])
def test_the_child_env_always_has_exit_on_stdin_close(rig, kw):
    out = start(rig, **kw)
    assert seen(rig)["exit_on_eof"] == "1" and isinstance(out["stdin_fd"], int)  # always, whatever the caller asked
    os.close(out["stdin_fd"])


def test_the_env_builder_sets_the_flag_in_the_spawned_popen_call(rig, monkeypatch):
    got: dict = {}
    real = supervisor.subprocess.Popen

    def spy(argv, **kw):
        got.update(env=kw["env"], stdin=kw["stdin"])
        return real(argv, **kw)

    monkeypatch.setattr(supervisor.subprocess, "Popen", spy)
    out = start(rig)
    assert got["env"]["MOTOR_SALIR_SI_STDIN_CIERRA"] == "1" and got["stdin"] != subprocess.DEVNULL
    os.close(out["stdin_fd"])


def test_stdin_stays_open_while_the_engine_runs_and_closes_on_down(rig):
    out = start(rig)
    held = os.dup(out["stdin_fd"])  # the bridge's copy
    os.close(out["stdin_fd"])  # the plugin side hands over and lets go
    time.sleep(1.0)  # an engine that saw EOF would be gone by now
    assert config.pid_alive(out["pid"]) and supervisor.engine_healthy(rig.cfg.engine_port)
    os.fstat(held)
    os.close(held)  # `down` / the bridge dying: the last write end closes
    assert wait_gone(out["pid"])


def test_crash_on_start_reports_the_last_log_lines(rig):
    rig.mode("crash")
    with pytest.raises(supervisor.EngineStartFailed) as e:
        start(rig, hold_stdin=True)
    assert "exited with code 3" in str(e.value) and "boom: cannot open the world" in str(e.value)
    assert e.value.code == "ENGINE_START_FAILED" and isinstance(e.value, Hermes3DError)


def test_engine_that_never_answers_health_times_out_and_is_stopped(rig, monkeypatch):
    rig.mode("silent")
    monkeypatch.setattr(supervisor, "ENGINE_START_TIMEOUT", 1.5)
    with pytest.raises(supervisor.EngineStartFailed, match=r"did not open .*\(/health\)"):
        start(rig)
    pid = procid_of_last(rig)
    assert wait_gone(pid)


def procid_of_last(rig) -> int:
    out = subprocess.run(
        ["pgrep", "-f", str(rig.eng / "app" / "stub.py")], capture_output=True, text=True, check=False
    ).stdout.split()
    return int(out[0]) if out else 0


def test_port_busy_is_a_clear_non_retryable_error_and_starts_nothing(rig):
    with socket.socket() as s:
        s.bind(("127.0.0.1", rig.cfg.engine_port))
        s.listen()
        with pytest.raises(supervisor.PortBusy) as e:
            start(rig)
    assert str(rig.cfg.engine_port) in str(e.value) and "engine_port" in str(e.value)
    assert e.value.code == "PORT_BUSY" and e.value.retryable is False
    assert not (rig.eng / "app" / "seen.json").exists()


def test_engine_port_must_not_clash_with_docs_viewer_or_link(rig):
    for field in ("docs_viewer_port", "link_port"):
        cfg = S.Hermes3DSettings(**{**rig.cfg.__dict__, field: rig.cfg.engine_port})
        with pytest.raises(supervisor.PortBusy, match="clashes"):
            supervisor.start_engine(cfg)


def test_default_ports_do_not_clash_with_each_other():
    d = S.Hermes3DSettings(home=Path("/x"))
    assert d.engine_port == 8790 and len({d.engine_port, d.docs_viewer_port}) == 2


def test_our_own_running_engine_is_reused_and_a_second_one_is_not_started(rig, hermes_home):
    out = start(rig)
    config.write_engine_pid(out["pid"], hermes_home)
    again = supervisor.start_engine(rig.cfg)
    assert again["reused"] is True and again["pid"] == out["pid"]


def test_a_stale_pid_file_is_dropped_and_the_engine_starts(rig, hermes_home):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    config.write_engine_pid(dead.pid, hermes_home)
    config.engine_pid_path(hermes_home).write_text(json.dumps({"pid": dead.pid, "start": 1, "cmd": "x"}))
    out = start(rig)
    assert out["reused"] is False and not config.engine_pid_path(hermes_home).exists()


def test_a_recycled_pid_is_never_killed_by_start_or_stop(rig, hermes_home):
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        ident = procid.identity(bystander.pid) or {}
        config.engine_pid_path(hermes_home).write_text(
            json.dumps({"pid": bystander.pid, **ident, "start": "reused-pid-other-start"})
        )
        assert config.read_engine_record(hermes_home) is None  # start time differs: not our engine
        out = start(rig)
        assert out["reused"] is False and bystander.poll() is None
        assert supervisor.stop_engine({"pid": bystander.pid, **ident, "start": "other"}) is True
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()


def test_stop_engine_stops_what_start_engine_started(rig, hermes_home):
    out = start(rig)
    config.write_engine_pid(out["pid"], hermes_home)
    rec = config.read_engine_record(hermes_home)
    assert rec and supervisor.stop_engine(rec) is True
    assert not supervisor.engine_healthy(rig.cfg.engine_port)


def test_old_node_warning_travels_with_the_start(rig, monkeypatch):
    old = nodebin.NodeRuntime(sys.executable, (22, 12, 0), "pm", "", ("Node 22.12.0 is older than 22.18.0; x",))
    monkeypatch.setattr(supervisor, "node_runtime", lambda **kw: old)
    assert start(rig)["node_warnings"] == ["Node 22.12.0 is older than 22.18.0; x"]


def test_pm_path_reaches_the_child_environment(rig, monkeypatch):
    pm_node = nodebin.NodeRuntime(sys.executable, (26, 12, 1), "pm", "/pm/bin:" + os.environ.get("PATH", ""))
    monkeypatch.setattr(supervisor, "node_runtime", lambda **kw: pm_node)
    start(rig)
    assert seen(rig)["path"].startswith("/pm/bin:")


def test_log_tail_scrubs_secrets_and_survives_a_missing_file(tmp_path):
    assert supervisor.log_tail(tmp_path / "nope.log") == ""
    log = tmp_path / "e.log"
    log.write_text("\n".join(f"line {i}" for i in range(40)) + "\nAuthorization: Bearer abcdef0123456789abcdef0123456789\n")
    tail = supervisor.log_tail(log, lines=3)
    assert "line 39" in tail and "line 10" not in tail and "abcdef0123456789abcdef" not in tail


def test_start_engine_closes_the_pipe_if_popen_fails(rig, monkeypatch):
    opened: list[int] = []
    real_pipe = os.pipe

    def spy():
        pair = real_pipe()
        opened.extend(pair)
        return pair

    def boom(*a, **k):
        raise OSError("no exec")

    monkeypatch.setattr(supervisor.os, "pipe", spy)
    monkeypatch.setattr(supervisor.subprocess, "Popen", boom)
    with pytest.raises(OSError, match="no exec"):
        supervisor.start_engine(rig.cfg, hold_stdin=True)
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)  # both ends closed


def test_cmd_up_hands_the_stdin_fd_to_the_new_bridge_then_closes_it(rig, hermes_home, monkeypatch, capsys):
    from test_entry import load_entry

    mod = load_entry()
    monkeypatch.setattr(mod, "_settings", lambda: rig.cfg)
    monkeypatch.setattr(mod, "_home", lambda: hermes_home)
    got: dict = {}

    def spawn(home, read_only=False, demo=False, hold_fd=None):
        got["fd"] = hold_fd
        os.fstat(hold_fd)  # still open while the bridge is being started
        return 4242

    monkeypatch.setattr(supervisor, "spawn_bridge", spawn)
    assert mod.cmd_up() == 0
    rig.started.append(json.loads(config.engine_pid_path(hermes_home).read_text())["pid"])
    with pytest.raises(OSError):
        os.fstat(got["fd"])  # the plugin side closed its copy; only the bridge holds the engine's stdin now
    assert "started the engine" in capsys.readouterr().out


def test_cmd_up_with_an_old_bridge_replaces_it_so_the_new_engine_has_a_holder(rig, hermes_home, monkeypatch):
    from test_entry import load_entry

    mod = load_entry()
    monkeypatch.setattr(mod, "_settings", lambda: rig.cfg)
    monkeypatch.setattr(mod, "_home", lambda: hermes_home)
    state = {"alive": True}
    monkeypatch.setattr(config, "read_runtime", lambda home=None: {"pid": 31337})
    monkeypatch.setattr(config, "pid_alive", lambda pid: state["alive"] if pid == 31337 else os.path.exists(f"/proc/{pid}"))
    calls: list = []

    def call(method, path, **k):
        calls.append(path)
        state["alive"] = False
        return {"ok": True}

    monkeypatch.setattr(mod.client, "call", call)
    got: dict = {}
    monkeypatch.setattr(
        supervisor, "spawn_bridge", lambda home, read_only=False, demo=False, hold_fd=None: got.update(fd=os.dup(hold_fd)) or 7
    )
    assert mod.cmd_up() == 0
    rig.started.append(config.read_engine_record(hermes_home)["pid"])
    assert seen(rig)["exit_on_eof"] == "1" and calls == ["/stop"] and config.pid_alive(rig.started[-1])
    os.close(got["fd"])


# ---- a world born on this very start (real engine: the template ships with embed OFF) ---------------------------------
def _espejo(rig) -> dict:
    return json.loads((rig.cfg.world_path / "entorno" / "espejo.json").read_text())


def test_fresh_world_gets_embed_on_for_path_a_and_the_engine_restarts_once(rig):
    (rig.eng / "app" / "seed").write_text("1")
    rig.cfg = dataclasses.replace(rig.cfg, engine_link=False)
    out = start(rig)
    assert _espejo(rig)["embed"] == {"activa": True}
    assert _espejo(rig)["fuente"]["puerto"] == 8791  # the rest of the file is untouched
    assert out["link_enabled"] is True  # read AFTER the engine made the world, not before
    assert out["embed_seeded"] is True
    assert supervisor.engine_healthy(rig.cfg.engine_port)
    assert json.loads((rig.cfg.world_path / "mundo.json").read_text())["esquema"].startswith("numinia-engine/mundo@")


def test_embed_is_not_touched_for_path_b_or_for_an_existing_world(rig):
    (rig.eng / "app" / "seed").write_text("1")
    rig.cfg = dataclasses.replace(rig.cfg, engine_link=True)
    out = start(rig)
    assert "embed" not in _espejo(rig) and not out.get("embed_seeded")
    supervisor.stop_engine({"pid": out["pid"], **(out["identity"] or {})})
    rig.cfg = dataclasses.replace(rig.cfg, engine_link=False)  # the world exists now: the user's file is theirs
    out = start(rig)
    assert "embed" not in _espejo(rig) and not out.get("embed_seeded")


def test_seed_embed_edits_only_the_embed_key_and_survives_junk(tmp_path):
    f = tmp_path / "entorno"
    f.mkdir()
    assert supervisor.seed_embed(tmp_path) is False  # no file
    (f / "espejo.json").write_text("{not json")
    assert supervisor.seed_embed(tmp_path) is False
    (f / "espejo.json").write_text('{"embed": {"activa": false}}')
    assert supervisor.seed_embed(tmp_path) is True
    assert json.loads((f / "espejo.json").read_text())["embed"] == {"activa": True}
    assert supervisor.seed_embed(tmp_path) is False  # already on
    (f / "espejo.json").write_text("[1]")
    assert supervisor.seed_embed(tmp_path) is False
