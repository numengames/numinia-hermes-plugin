# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""``hermes 3d docs``: start and stop the lite documentation viewer of the ``docs-visor`` repo.

The viewer is a separate program (``serve.mjs``, MIT, no dependencies) that serves an offline copy of the Hermes
documentation on **127.0.0.1 only** with a CSP that lets only the engine's origin frame it. The world's library
(``documento`` things) opens its pages. This module only runs it: it never imports its code.

* Where: setting ``docs_viewer_path`` (the ``docs-visor`` checkout; default = the folder ``docs-visor`` next to this
  plugin's folder). It must hold ``build-docs.sh`` and ``serve.mjs``.
* Build: if ``<HERMES_HOME>/plugin-data/hermes-3d/docs-site/lite/index.html`` is missing, ``build-docs.sh lite --out <that>``
  is run first (it fetches the pinned upstream commit with git; nothing is written outside ``plugin-data/hermes-3d/``).
* Port: setting ``docs_viewer_port`` (default 8791). The document origin ``http://127.0.0.1:<port>`` must be in the
  world's ``documentos.origenes``, and the port must not be the engine's link port.
* Process: started detached; pid, start time and command line are recorded (``docs-viewer.pid``) so ``--stop`` only
  signals the process this plugin started.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from . import config, procid, supervisor
from . import settings as S

BUILD_SCRIPT = "build-docs.sh"
SERVE_SCRIPT = "serve.mjs"
SITE_DIRNAME = "docs-site"  # under <HERMES_HOME>/plugin-data/hermes-3d/
BUILD_TIMEOUT = 900.0
START_TIMEOUT = 15.0
BASE_PATH = "/docs/"
_ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "XDG_RUNTIME_DIR", "HERMES_AGENT_SRC")


class DocsError(supervisor.SupervisorError):
    """A clean, user-facing failure of ``hermes 3d docs``."""


def default_path() -> Path:
    """The ``docs-visor`` folder next to this plugin's folder (``<plugins>/hermes-3d`` -> ``<plugins>/docs-visor``)."""
    return Path(__file__).resolve().parents[2] / "docs-visor"


def viewer_dir(cfg: S.Hermes3DSettings) -> Path:
    path = cfg.docs_viewer_path or default_path()
    for name in (BUILD_SCRIPT, SERVE_SCRIPT):
        if not (path / name).is_file():
            raise DocsError(
                f"docs viewer not found: {path / name} is missing. Set the plugin setting `docs_viewer_path` to the "
                "folder of the docs-visor repo (it holds build-docs.sh and serve.mjs), e.g. "
                "`hermes config set plugins.entries.hermes-3d.settings.docs_viewer_path /path/to/docs-visor`."
            )
    return path


def site_root(home: Path) -> Path:
    return config.plugin_data_dir(home) / SITE_DIRNAME


def lite_site(home: Path) -> Path:
    return site_root(home) / "lite"


def pid_path(home: Path) -> Path:
    return config.plugin_data_dir(home) / "docs-viewer.pid"


def url_for(port: int) -> str:
    return f"http://{config.HOST}:{int(port)}{BASE_PATH}"


def read_record(home: Path) -> dict[str, Any] | None:
    """The viewer process WE started, if it is still that process (a recycled pid is never trusted)."""
    try:
        rec = json.loads(pid_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or not isinstance(rec.get("pid"), int):
        return None
    return rec if procid.same_process(rec["pid"], rec) else None


def _write_record(home: Path, pid: int, port: int, ident: dict[str, Any] | None) -> None:
    fd = os.open(pid_path(home), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"pid": int(pid), "port": int(port), **(ident or {})}, fh)


def status(home: Path, cfg: S.Hermes3DSettings) -> dict[str, Any]:
    """What ``hermes 3d status`` shows: running (by us), listening (by anybody), built."""
    rec = read_record(home)
    port = int(rec["port"]) if rec and isinstance(rec.get("port"), int) else cfg.docs_viewer_port
    return {
        "running": rec is not None,
        "pid": rec["pid"] if rec else None,
        "port": port,
        "listening": supervisor.link_listening(port),
        "built": (lite_site(home) / "index.html").is_file(),
        "url": url_for(port),
    }


def _env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k in _ENV_KEEP}


def build(home: Path, cfg: S.Hermes3DSettings, *, run: Any = subprocess.run) -> bool:
    """Build the lite site unless it is there. True when a build was run. Raises ``DocsError`` when it fails."""
    if (lite_site(home) / "index.html").is_file():
        return False
    vdir = viewer_dir(cfg)
    try:
        res = run(
            ["bash", str(vdir / BUILD_SCRIPT), "lite", "--out", str(site_root(home))],
            cwd=str(vdir),
            env=_env(),
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DocsError(f"could not run the docs build: {type(exc).__name__}") from None
    if res.returncode != 0 or not (lite_site(home) / "index.html").is_file():
        tail = " ".join((res.stderr or res.stdout or "").strip().splitlines()[-2:])[:200]
        raise DocsError(f"the docs build failed (exit {res.returncode}): {tail}")
    return True


def start(home: Path, cfg: S.Hermes3DSettings) -> dict[str, Any]:
    """Start the viewer (building it first if needed). Returns ``{"url", "port", "pid", "reused", "built"}``."""
    vdir = viewer_dir(cfg)  # first: "where is the viewer" is the more basic question
    port = cfg.docs_viewer_port
    lport = supervisor.link_port(cfg)
    if port == lport:
        raise DocsError(
            f"docs_viewer_port ({port}) is the engine's link port; pick another with "
            "`hermes config set plugins.entries.hermes-3d.settings.docs_viewer_port <port>` and list "
            f"http://127.0.0.1:<port> in the world's entorno/espejo.json documentos.origenes"
        )
    rec = read_record(home)
    if rec is not None:
        return {
            "url": url_for(rec.get("port", port)),
            "port": rec.get("port", port),
            "pid": rec["pid"],
            "reused": True,
            "built": False,
        }
    if supervisor.link_listening(port):
        raise DocsError(f"127.0.0.1:{port} is already in use by another program; change `docs_viewer_port`")
    node = supervisor.node_binary()
    built = build(home, cfg)
    log = config.plugin_data_dir(home) / "docs-viewer.log"
    cmd = [
        node,
        str(vdir / SERVE_SCRIPT),
        str(lite_site(home)),
        str(port),
        "--lite",
        "--engine-origin",
        f"http://{config.HOST}:{cfg.engine_port}",
    ]
    with config.open_log(log) as lf:
        proc = subprocess.Popen(
            cmd, cwd=str(vdir), env=_env(), stdin=subprocess.DEVNULL, stdout=lf, stderr=lf, start_new_session=True
        )
    ident = procid.identity(proc.pid)
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise DocsError(f"the docs viewer exited with code {proc.returncode}; see {log}")
        if supervisor.link_listening(port):
            _write_record(home, proc.pid, port, ident)
            return {"url": url_for(port), "port": port, "pid": proc.pid, "reused": False, "built": built}
        time.sleep(0.1)
    supervisor._signal_group(proc.pid, 15)
    raise DocsError(f"the docs viewer did not open 127.0.0.1:{port} within {START_TIMEOUT:.0f} s; see {log}")


def stop(home: Path) -> bool:
    """Stop the viewer this plugin started. True when nothing of ours is left running (also when nothing was started)."""
    rec = read_record(home)
    ok = supervisor.stop_engine(rec) if rec else True
    if ok:
        pid_path(home).unlink(missing_ok=True)
    return ok
