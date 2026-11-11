# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""``hermes 3d setup --first-run`` end to end: fake release server -> installer -> stub engine, with Node mocked (no real pm)."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import sys
import time
from types import SimpleNamespace

import pytest
import yaml
from conftest import ROOT
from fake_release import FakeRelease, as_script, tar_gz
from test_entry import load_entry

from hermes_3d_lib import config, installer, nodebin, setup_cmd, supervisor
from hermes_3d_lib import settings as S
from hermes_3d_lib.errors import NodeMissing

PYNODE = nodebin.NodeRuntime(sys.executable, (26, 12, 1), "pm", "")
STUB_ENTRY = "stub.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def stub_asset() -> bytes:
    stub = (ROOT / "tests" / "stub_engine.py").read_bytes()
    members = [
        ("app/", None, {}),
        (f"app/{STUB_ENTRY}", stub, {}),
        ("www/", None, {}),
        ("www/index.html", b"<html>plaza</html>", {}),
    ]
    return as_script(tar_gz(members))


class YamlSetter:
    """Stands in for `hermes config set`: edits config.yaml (no real hermes binary is ever run by these tests)."""

    def __init__(self, home, ok=True):
        self.home, self.ok, self.calls = home, ok, []

    def __call__(self, key, value):
        self.calls.append((key, value))
        if not self.ok:
            return False
        path = self.home / "config.yaml"
        data = yaml.safe_load(path.read_text()) if path.exists() else {}
        node = data
        parts = key.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = {"true": True, "false": False}.get(value, value)
        path.write_text(yaml.safe_dump(data))
        return True


@pytest.fixture()
def env(hermes_home, tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        installer.shutil, "which", lambda n: None
    )  # no `hermes` binary: engine_path goes through the yaml fallback
    monkeypatch.setattr(supervisor, "ENGINE_START_TIMEOUT", 15.0)
    port = free_port()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "entries": {
                        S.PLUGIN_ID: {
                            "settings": {"engine_port": port, "docs_viewer_port": free_port(), "link_port": free_port()}
                        }
                    }
                }
            }
        )
    )
    holders: list[int] = []
    pids: list[int] = []

    def spawn(home, read_only=False, demo=False, hold_fd=None, world=None):
        if hold_fd is not None:
            holders.append(os.dup(hold_fd))  # the stand-in for the bridge: keeps the engine's stdin open
        return 4242

    monkeypatch.setattr(supervisor, "spawn_bridge", spawn)
    mod = load_entry()
    monkeypatch.setattr(mod, "_home", lambda: hermes_home)
    monkeypatch.setattr(mod, "_settings", lambda: S.Hermes3DSettings.from_yaml(hermes_home))
    yield SimpleNamespace(home=hermes_home, port=port, mod=mod, holders=holders, pids=pids, tmp=tmp_path)
    for fd in holders:
        with contextlib.suppress(OSError):
            os.close(fd)
    rec = None
    with contextlib.suppress(OSError, ValueError):
        rec = json.loads(config.engine_pid_path(hermes_home).read_text())
    if rec:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(rec["pid"], signal.SIGKILL)


def run(env, rel, *, yes=True, answers=(), setter=None, node=None, probe=None):
    lock = rel.lock(env.tmp, entry=STUB_ENTRY)
    lines: list[str] = []
    ans = iter(answers)
    setter = setter or YamlSetter(env.home)
    rc = setup_cmd.first_run(
        home=env.home,
        yes=yes,
        setter=setter,
        starter=lambda cfg: env.mod.cmd_up(cfg=cfg),
        ask=lambda prompt: next(ans),
        out=lambda *a, **k: lines.append(" ".join(map(str, a))),
        lock_path=lock,
        url=rel.url,
        probe=probe,
    )
    return rc, "\n".join(lines), setter


def test_the_whole_path_works_against_the_fake_release_and_is_fast(env, monkeypatch, capsys):
    monkeypatch.setattr(nodebin, "resolve_node", lambda ensure=True, **kw: PYNODE)
    t0 = time.monotonic()
    with FakeRelease(stub_asset()) as rel:
        rc, text, setter = run(env, rel)
    seconds = time.monotonic() - t0
    assert rc == 0, text
    assert "Your Hermes is a place now." in text
    for block in ("Checking this computer", "The plan.", "Doing it"):
        assert block in text
    assert "never runs it" in text and "14.4 MB" not in text  # the fake release has its own size
    assert "✓ Downloaded" in text and "✓ Checksum matches" in text and "✓ Unpacked" in text and "✓ World started" in text
    assert "hermes 3d setup --explain" in text and "Switches" not in text
    assert "Shortcut" not in text and not list(env.home.rglob("hermes-3d.desktop"))
    cfg = S.Hermes3DSettings.from_yaml(env.home)
    assert cfg.engine_link is False and cfg.engine_main() is not None and cfg.engine_path.name == rel.sha256[:12]
    assert ("plugins.entries.hermes-3d.settings.engine_link", "false") in setter.calls
    assert supervisor.engine_healthy(env.port)
    with capsys.disabled():
        print(f"FIRST_RUN_SECONDS={seconds:.2f}")
    assert seconds < 60
    keys = (config.plugin_data_dir(env.home) / "engine-keys.json").read_text()
    assert json.loads(keys)["secret"] not in text and json.loads(keys)["secret"] not in capsys.readouterr().out


def test_second_run_downloads_nothing_and_reuses_the_running_engine(env, monkeypatch):
    monkeypatch.setattr(nodebin, "resolve_node", lambda ensure=True, **kw: PYNODE)
    with FakeRelease(stub_asset()) as rel:
        assert run(env, rel)[0] == 0
        hits = len(rel.hits)
        rc, text, setter = run(env, rel)
        assert rc == 0 and len(rel.hits) == hits and "already installed, nothing to download" in text and setter.calls == []


def test_node_missing_asks_then_lets_pm_install_it(env, monkeypatch):
    calls = []

    def resolve(ensure=True, **kw):
        calls.append(ensure)
        if not ensure and len(calls) == 1:
            raise NodeMissing("none yet")
        return PYNODE

    monkeypatch.setattr(nodebin, "resolve_node", resolve)
    with FakeRelease(stub_asset()) as rel:
        rc, text, _ = run(env, rel, yes=False, answers=[""])
    assert rc == 0 and calls[:2] == [False, True], text


def test_declining_the_node_download_stops_before_anything_else(env, monkeypatch):
    def resolve(ensure=True, **kw):
        raise NodeMissing("none yet")

    monkeypatch.setattr(nodebin, "resolve_node", resolve)
    with FakeRelease(stub_asset()) as rel:
        rc, text, _ = run(env, rel, yes=False, answers=["n"])
        assert rc == 0 and "Nothing was changed" in text and "Install Node with Hermes" in text and rel.hits == []


def test_pm_failing_to_install_node_is_a_typed_stop(env, monkeypatch):
    def resolve(ensure=True, **kw):
        raise NodeMissing("Hermes could not provide Node (offline). Run `hermes pm doctor`, then `hermes 3d setup` again.")

    monkeypatch.setattr(nodebin, "resolve_node", resolve)
    with FakeRelease(stub_asset()) as rel:
        rc, text, _ = run(env, rel)
        assert rc == 1 and "hermes pm doctor" in text and "✗" in text and rel.hits == []


def test_a_bad_checksum_stops_at_the_engine_step_and_starts_nothing(env, monkeypatch):
    monkeypatch.setattr(nodebin, "resolve_node", lambda ensure=True, **kw: PYNODE)
    with FakeRelease(stub_asset()) as rel:
        lock = rel.lock(env.tmp, entry=STUB_ENTRY, sha256="2" * 64)
        lines = []
        rc = setup_cmd.first_run(
            home=env.home, yes=True, setter=YamlSetter(env.home), starter=lambda cfg: pytest.fail("must not start"),
            ask=lambda p: "y", out=lambda *a, **k: lines.append(" ".join(map(str, a))), lock_path=lock, url=rel.url,
        )  # fmt: skip
    assert rc == 1 and "sha256 of the download" in "\n".join(lines) and not supervisor.engine_healthy(env.port)


def test_an_unpinned_lock_stops_with_the_publish_message(env, monkeypatch, tmp_path):
    lock = tmp_path / "unpinned.lock.json"
    lock.write_text(json.dumps({"tag": "TODO-PUBLISH", "sha256": "TODO-PUBLISH", "asset": "instalar-motor.sh"}))
    monkeypatch.setattr(nodebin, "resolve_node", lambda ensure=True, **kw: PYNODE)
    lines = []
    rc = setup_cmd.first_run(
        home=env.home,
        yes=True,
        setter=YamlSetter(env.home),
        starter=None,
        ask=lambda p: "y",
        out=lambda *a, **k: lines.append(" ".join(map(str, a))),
        lock_path=lock,
    )
    assert rc == 1 and "not pinned yet" in "\n".join(lines)


def test_settings_write_failure_stops(env, monkeypatch):
    monkeypatch.setattr(nodebin, "resolve_node", lambda ensure=True, **kw: PYNODE)
    with FakeRelease(stub_asset()) as rel:
        rc, text, _ = run(env, rel, setter=YamlSetter(env.home, ok=False))
    assert rc == 1 and "could not write" in text


def test_world_without_embed_gets_the_hint_not_a_false_ready(env, monkeypatch):
    monkeypatch.setattr(nodebin, "resolve_node", lambda ensure=True, **kw: PYNODE)
    with FakeRelease(stub_asset()) as rel:
        rc, text, _ = run(env, rel, probe=lambda base: {"up": True, "embed": False})
    assert rc == 0 and "ready:" not in text and "embed" in text


def test_engine_not_answering_after_start_is_reported(env, monkeypatch):
    monkeypatch.setattr(nodebin, "resolve_node", lambda ensure=True, **kw: PYNODE)
    with FakeRelease(stub_asset()) as rel:
        rc, text, _ = run(env, rel, probe=lambda base: {"up": False})
    assert rc == 1 and "did not answer" in text


def test_a_failing_start_returns_its_exit_code(env, monkeypatch):
    monkeypatch.setattr(nodebin, "resolve_node", lambda ensure=True, **kw: PYNODE)
    with FakeRelease(stub_asset()) as rel:
        lines = []
        rc = setup_cmd.first_run(
            home=env.home, yes=True, setter=YamlSetter(env.home), starter=lambda cfg: 5, ask=lambda p: "y",
            out=lambda *a, **k: lines.append(" ".join(map(str, a))), lock_path=rel.lock(env.tmp, entry=STUB_ENTRY), url=rel.url,
        )  # fmt: skip
    assert rc == 5


def test_no_terminal_without_yes_is_a_clear_stop(env, monkeypatch):
    def resolve(ensure=True, **kw):
        raise NodeMissing("none")

    monkeypatch.setattr(nodebin, "resolve_node", resolve)

    def eof(prompt):
        raise EOFError

    lines = []
    rc = setup_cmd.first_run(
        home=env.home,
        yes=False,
        setter=YamlSetter(env.home),
        starter=None,
        ask=eof,
        out=lambda *a, **k: lines.append(" ".join(map(str, a))),
    )
    assert rc == 1 and "--yes" in "\n".join(lines)


def test_plain_setup_is_the_first_run_and_explain_lists_the_switches(env, monkeypatch, capsys):
    called = {}
    monkeypatch.setattr(setup_cmd, "first_run", lambda **kw: called.update(kw) or 0)
    args = SimpleNamespace(yes=True, engine_path=None, first_run=False, explain=False)
    assert setup_cmd.cmd_setup(args, home=env.home, setter=YamlSetter(env.home)) == 0 and called["yes"] is True
    capsys.readouterr()
    args.explain = True
    assert setup_cmd.cmd_setup(args, home=env.home, setter=YamlSetter(env.home)) == 0
    out = capsys.readouterr().out
    assert "publish_kanban" in out and "Settings > Plugins" in out


def test_the_one_question_defaults_to_yes_and_is_asked_once(env, monkeypatch):
    monkeypatch.setattr(nodebin, "resolve_node", lambda ensure=True, **kw: PYNODE)
    asked = []
    with FakeRelease(stub_asset()) as rel:
        lock = rel.lock(env.tmp, entry=STUB_ENTRY)
        rc = setup_cmd.first_run(
            home=env.home, yes=False, setter=YamlSetter(env.home), starter=lambda cfg: env.mod.cmd_up(cfg=cfg),
            ask=lambda p: asked.append(p) or "", out=lambda *a, **k: None, lock_path=lock, url=rel.url,
        )  # fmt: skip
    assert rc == 0 and len(asked) == 1 and "[Y/n]" in asked[0]


def test_hermes_version_comes_from_the_public_cli_and_is_checked_against_the_range(monkeypatch):
    monkeypatch.setattr(setup_cmd.shutil, "which", lambda n: "/x/hermes")
    monkeypatch.setattr(
        setup_cmd.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="Hermes Agent v0.23.1+1963.ge2a0 (2026.9.24)\n"),
    )
    assert setup_cmd.hermes_version() == "0.23.1"
    assert setup_cmd.version_supported("0.23.1", ">=0.22.0, <0.24") is True
    assert setup_cmd.version_supported("0.24.0", ">=0.22.0, <0.24") is False
    assert setup_cmd.version_supported("", ">=0.22.0") is None


def test_cli_parses_first_run(env):
    import argparse

    p = argparse.ArgumentParser()
    env.mod._setup_cli(p)
    assert p.parse_args(["setup", "--first-run", "--yes"]).first_run is True
