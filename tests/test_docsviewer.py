"""``hermes 3d docs``: a fake docs-visor (build-docs.sh + serve.mjs) stands in for the real viewer."""

from __future__ import annotations

import importlib.util
import os
import socket
import stat
import subprocess
import time
from pathlib import Path

import pytest
from conftest import LOOP

from hermes_3d_lib import docsviewer, procid
from hermes_3d_lib import settings as S

FAKE_BUILD = """#!/usr/bin/env bash
# fake build-docs.sh: build-docs.sh lite --out DIR  ->  DIR/lite/index.html
set -e
[ "$1" = "lite" ] && [ "$2" = "--out" ] || { echo "bad args: $*" >&2; exit 3; }
[ -z "${FAKE_BUILD_FAILS:-}" ] || { echo "network down" >&2; exit 7; }
mkdir -p "$3/lite" && echo "<title>docs</title>" > "$3/lite/index.html"
echo "$*" > "$3/build.args"
"""
FAKE_SERVE = """import http from "node:http";
const [root, port] = process.argv.slice(2);
if (process.env.FAKE_SERVE_EXITS) { console.error("boom"); process.exit(5); }
http.createServer((q, r) => { r.end("ok " + root); }).listen(Number(port), "127.0.0.1");
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def viewer(tmp_path, hermes_home):
    d = tmp_path / "docs-visor"
    d.mkdir()
    (d / "build-docs.sh").write_text(FAKE_BUILD)
    (d / "build-docs.sh").chmod(0o755)
    (d / "serve.mjs").write_text(FAKE_SERVE)
    return d


def cfg_for(home: Path, viewer_dir: Path | None, port: int, **extra) -> S.Hermes3DSettings:
    values = {"docs_viewer_port": port, "link_port": free_port(), **extra}
    if viewer_dir is not None:
        values["docs_viewer_path"] = str(viewer_dir)
    return S.Hermes3DSettings.from_getter(lambda k, d=None: values.get(k, d), home)


@pytest.fixture
def cleanup(hermes_home):
    yield
    docsviewer.stop(hermes_home)


def test_defaults_and_settings(hermes_home):
    cfg = S.Hermes3DSettings.from_getter(lambda k, d=None: d, hermes_home)
    assert cfg.docs_viewer_port == 8791 and cfg.docs_viewer_path is None
    assert docsviewer.default_path().name == "docs-visor"
    assert docsviewer.url_for(9000) == f"http://{LOOP}:9000/docs/"


def test_missing_viewer_folder_says_how_to_fix_it(hermes_home, tmp_path):
    cfg = cfg_for(hermes_home, tmp_path / "nowhere", free_port())
    with pytest.raises(docsviewer.DocsError, match="docs_viewer_path"):
        docsviewer.viewer_dir(cfg)
    cfg = cfg_for(hermes_home, None, free_port())  # the sibling folder does not exist next to this checkout
    if not (docsviewer.default_path() / "serve.mjs").is_file():
        with pytest.raises(docsviewer.DocsError, match="not found"):
            docsviewer.start(hermes_home, cfg)


def test_start_builds_serves_records_and_stops(hermes_home, viewer, cleanup):
    port = free_port()
    cfg = cfg_for(hermes_home, viewer, port)
    st = docsviewer.status(hermes_home, cfg)
    assert not st["running"] and not st["listening"] and not st["built"]
    info = docsviewer.start(hermes_home, cfg)
    assert info["built"] is True and info["reused"] is False and info["url"] == f"http://127.0.0.1:{port}/docs/"
    assert (docsviewer.site_root(hermes_home) / "build.args").read_text().split()[:3] == [
        "lite",
        "--out",
        str(docsviewer.site_root(hermes_home)),
    ]
    st = docsviewer.status(hermes_home, cfg)
    assert st["running"] and st["listening"] and st["built"] and st["pid"] == info["pid"] and st["port"] == port
    assert stat.S_IMODE(docsviewer.pid_path(hermes_home).stat().st_mode) == 0o600
    again = docsviewer.start(hermes_home, cfg)  # reuse: no second process, no rebuild
    assert again["reused"] is True and again["pid"] == info["pid"] and again["built"] is False
    # 127.0.0.1 only: the fake binds loopback, and the command line the plugin used carries no wildcard address
    assert "0.0.0.0" not in (procid.identity(info["pid"]) or {"cmd": ""})["cmd"]
    assert docsviewer.stop(hermes_home) is True
    assert not docsviewer.pid_path(hermes_home).exists()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and docsviewer.status(hermes_home, cfg)["listening"]:
        time.sleep(0.05)
    assert not docsviewer.status(hermes_home, cfg)["listening"]
    assert docsviewer.stop(hermes_home) is True  # stopping nothing is fine


def test_second_start_does_not_rebuild(hermes_home, viewer, cleanup):
    cfg = cfg_for(hermes_home, viewer, free_port())
    docsviewer.start(hermes_home, cfg)
    docsviewer.stop(hermes_home)
    info = docsviewer.start(hermes_home, cfg)
    assert info["built"] is False


def test_build_failure_is_a_clean_error(hermes_home, viewer, monkeypatch):
    monkeypatch.setenv("FAKE_BUILD_FAILS", "1")
    cfg = cfg_for(hermes_home, viewer, free_port())
    monkeypatch.setattr(docsviewer, "_ENV_KEEP", (*docsviewer._ENV_KEEP, "FAKE_BUILD_FAILS"))
    with pytest.raises(docsviewer.DocsError, match=r"build failed \(exit 7\): network down"):
        docsviewer.start(hermes_home, cfg)


def test_build_that_cannot_run(hermes_home, viewer):
    cfg = cfg_for(hermes_home, viewer, free_port())

    def boom(*a, **k):
        raise FileNotFoundError("bash")

    with pytest.raises(docsviewer.DocsError, match="could not run the docs build: FileNotFoundError"):
        docsviewer.build(hermes_home, cfg, run=boom)


def test_port_clash_with_the_link_and_with_a_stranger(hermes_home, viewer):
    link = free_port()
    cfg = cfg_for(hermes_home, viewer, link, link_port=link)
    with pytest.raises(docsviewer.DocsError, match="link port"):
        docsviewer.start(hermes_home, cfg)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        busy = int(s.getsockname()[1])
        cfg = cfg_for(hermes_home, viewer, busy)
        with pytest.raises(docsviewer.DocsError, match="already in use"):
            docsviewer.start(hermes_home, cfg)


def test_viewer_that_dies_at_start(hermes_home, viewer, monkeypatch):
    monkeypatch.setenv("FAKE_SERVE_EXITS", "1")
    monkeypatch.setattr(docsviewer, "_ENV_KEEP", (*docsviewer._ENV_KEEP, "FAKE_SERVE_EXITS"))
    with pytest.raises(docsviewer.DocsError, match=r"exited with code 5"):
        docsviewer.start(hermes_home, cfg_for(hermes_home, viewer, free_port()))


def test_viewer_that_never_listens(hermes_home, viewer, monkeypatch):
    (viewer / "serve.mjs").write_text("setTimeout(() => {}, 60000);\n")
    monkeypatch.setattr(docsviewer, "START_TIMEOUT", 0.6)
    with pytest.raises(docsviewer.DocsError, match="did not open"):
        docsviewer.start(hermes_home, cfg_for(hermes_home, viewer, free_port()))
    time.sleep(0.3)


def test_a_recycled_or_garbage_record_is_never_trusted(hermes_home):
    pidfile = docsviewer.pid_path(hermes_home)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text("not json")
    assert docsviewer.read_record(hermes_home) is None
    pidfile.write_text('{"pid": "x"}')
    assert docsviewer.read_record(hermes_home) is None
    sleeper = subprocess.Popen(["sleep", "30"])  # a stranger that happens to have the pid
    try:
        pidfile.write_text(f'{{"pid": {sleeper.pid}, "port": 1, "start": "0", "cmd": "other"}}')
        assert docsviewer.read_record(hermes_home) is None
        assert docsviewer.stop(hermes_home) is True and sleeper.poll() is None  # it was NOT signalled
    finally:
        sleeper.kill()
        sleeper.wait()


def _entry(name="hp_entry_docs"):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parents[1] / "__init__.py", submodule_search_locations=[str(Path(__file__).parents[1])]
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _configure(home, **settings):
    import yaml

    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"entries": {"hermes-3d": {"settings": settings}}}}))


def test_cli_docs_start_status_stop(hermes_home, viewer, capsys, cleanup):
    port = free_port()
    _configure(hermes_home, docs_viewer_path=str(viewer), docs_viewer_port=port, link_port=free_port())
    mod = _entry()
    assert mod.cmd_docs() == 0
    out = capsys.readouterr().out
    assert f"open the docs: http://127.0.0.1:{port}/docs/" in out and "built the lite" in out and "documentos.origenes" in out
    assert mod.cmd_docs() == 0 and "already running" in capsys.readouterr().out
    mod.cmd_status()
    assert "docs viewer   : running" in capsys.readouterr().out
    assert mod.cmd_docs(stop=True) == 0 and "stopped" in capsys.readouterr().out
    mod.cmd_status()
    assert "docs viewer   : not running" in capsys.readouterr().out


def test_cli_docs_error_is_clean(hermes_home, tmp_path, capsys):
    import argparse

    _configure(hermes_home, docs_viewer_path=str(tmp_path / "nope"))
    mod = _entry()
    rc = mod._cli(argparse.Namespace(hermes3d_cmd="docs", stop=False))
    assert rc == 1 and "docs viewer not found" in capsys.readouterr().err


def test_cli_docs_stop_that_cannot_stop(hermes_home, monkeypatch, capsys):
    mod = _entry()
    monkeypatch.setattr(mod.docsviewer, "stop", lambda home: False)
    assert mod.cmd_docs(stop=True) == 1 and "did not stop" in capsys.readouterr().out
    assert os.environ.get("HERMES_HOME") == str(hermes_home)
