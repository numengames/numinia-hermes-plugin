# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""Locate, start, reuse and stop the Numinia Engine world, and the bridge process.

Nothing is hardcoded: the engine folder, the world folder and the ports are plugin settings
(``settings.py``); the link key comes from ``linkkey.py``.

The engine and this plugin are separate programs (engine AGPL, plugin MIT) that only talk over a local
WebSocket. We start the engine as a child *process* with its documented environment variables; we never
import its code. Every subprocess here is an argv list (no shell).
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import config, enginekeys, linkkey, logsafe, nodebin, procid
from . import settings as S
from .errors import Hermes3DError

logger = logging.getLogger("hermes_3d.supervisor")

TEMPLATE = "hermes"
SWITCH_FILE = Path("entorno") / "espejo.json"  # no file = the engine opens no link at all
ENGINE_START_TIMEOUT = 30.0


class SupervisorError(RuntimeError):
    """A clean, user-facing failure (never a traceback for an expected situation)."""


def _read_world_file(world: Path, relative: Path | str) -> str | None:
    """Text of a fixed file inside the world, or None. The final component is never a symlink."""
    try:
        fd = os.open(world / relative, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read(256 * 1024)


def _world_json(world: Path, relative: Path | str) -> dict[str, Any] | None:
    text = _read_world_file(world, relative)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def link_port(cfg: S.Hermes3DSettings) -> int:
    """The engine's link port: the ``link_port`` setting, else the world's espejo.json, else 8791."""
    if cfg.link_port:
        return cfg.link_port
    port = ((_world_json(cfg.world_path, SWITCH_FILE) or {}).get("fuente") or {}).get("puerto")
    if isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535:
        return port
    return S.DEFAULT_LINK_PORT


def espejo_enabled(world: Path) -> bool:
    """True when the world has an active entorno/espejo.json (otherwise the engine opens no link)."""
    return (_world_json(world, SWITCH_FILE) or {}).get("activa") is True


def world_info(world: Path) -> dict[str, Any] | None:
    """``mundo.json`` of an existing world, or None."""
    return _world_json(world, "mundo.json")


def check_world_is_ours(world: Path) -> None:
    """Reuse a folder only if it is empty/new or already a Numinia world (``mundo.json`` with the
    ``numinia-engine/mundo@`` schema). We never seed over foreign data. The engine does not record
    which template a world was born from, so the template itself cannot be verified here."""
    if not world.exists() or not any(world.iterdir()):
        return
    info = world_info(world)
    schema = str(info.get("esquema", "")) if info else ""
    if not schema.startswith("numinia-engine/mundo@"):
        raise SupervisorError(
            f"{world} is not empty and is not a Numinia world (no mundo.json with "
            "numinia-engine/mundo@…); set `world_path` to another folder"
        )


def link_listening(port: int, timeout: float = 0.6) -> bool:
    """True when something accepts connections on 127.0.0.1:<port>. The engine's link answers 404 to
    plain HTTP and talks only WebSocket, so a TCP connect is the honest liveness probe."""
    try:
        with socket.create_connection((config.HOST, port), timeout=timeout):
            return True
    except OSError:
        return False


def node_runtime(*, ensure: bool = True) -> nodebin.NodeRuntime:
    """Hermes' own Node (``pm``); raises ``NodeMissing`` (code NODE_MISSING, not retryable). See :mod:`.nodebin`."""
    return nodebin.resolve_node(ensure=ensure)


def node_binary() -> str:
    return node_runtime().binary


def engine_environment(
    cfg: S.Hermes3DSettings,
    key: str,
    lport: int,
    node: nodebin.NodeRuntime | None = None,
    keys: tuple[str, str] | None = None,
    exit_on_stdin_eof: bool = False,
) -> dict[str, str]:
    """The environment of the engine child: a short allow-list of ours plus the engine's documented variables.

    ``node`` supplies Hermes' own PATH; ``keys`` are ``(MOTOR_SECRET, MOTOR_ADMIN)``; ``exit_on_stdin_eof`` asks an engine that
    supports it (``MOTOR_SALIR_SI_STDIN_CIERRA=1``) to exit when the plugin's side of its stdin pipe closes.
    """
    env = (
        nodebin.child_env(node)
        if node is not None
        else {
            k: v
            for k, v in os.environ.items()
            if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "XDG_RUNTIME_DIR", "NODE_OPTIONS")
        }
    )
    env.update(
        {
            "MOTOR_MUNDO": str(cfg.world_path),
            "MOTOR_PLANTILLA": TEMPLATE,
            "PORT": str(cfg.engine_port),
            "HOST": config.HOST,
            "MOTOR_ENLACE_PUERTO": str(lport),
            linkkey.ENGINE_KEY_ENV: key,
        }
    )
    if keys is not None:
        env["MOTOR_SECRET"], env["MOTOR_ADMIN"] = keys
    if exit_on_stdin_eof:
        env["MOTOR_SALIR_SI_STDIN_CIERRA"] = "1"
    www = cfg.www_dir()
    if www is not None:
        env["WWW_DIR"] = str(www)  # the engine then serves the world's page on its game port
    return env


def engine_healthy(port: int, timeout: float = 1.5) -> bool:
    """True when the engine on ``127.0.0.1:<port>`` answers ``GET /health`` with HTTP 200."""
    try:
        conn = http.client.HTTPConnection(config.HOST, port, timeout=timeout)
        try:
            conn.request("GET", "/health")
            return conn.getresponse().status == 200
        finally:
            conn.close()
    except (OSError, http.client.HTTPException):
        return False


def log_tail(path: Path, lines: int = 12) -> str:
    """The last lines of a log, secrets scrubbed (the engine never prints its keys, but never trust that)."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 4000))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return logsafe.redact("\n".join(text.strip().splitlines()[-lines:]))


class EngineStartFailed(SupervisorError, Hermes3DError):
    code = "ENGINE_START_FAILED"
    retryable = True


class PortBusy(SupervisorError, Hermes3DError):
    code = "PORT_BUSY"
    retryable = False


def check_ports(cfg: S.Hermes3DSettings, lport: int) -> None:
    """The ports we are about to use must differ, and the game port must be free (we only reuse OUR engine)."""
    if cfg.engine_port == cfg.docs_viewer_port or cfg.engine_port == lport:
        raise PortBusy(
            f"engine_port ({cfg.engine_port}) clashes with the docs viewer ({cfg.docs_viewer_port}) or the link ({lport}); "
            "change one with `hermes config set plugins.entries.hermes-3d.settings.engine_port <port>`"
        )
    if link_listening(cfg.engine_port):
        raise PortBusy(
            f"127.0.0.1:{cfg.engine_port} is already in use by another program. Stop it, or choose another port: "
            "`hermes config set plugins.entries.hermes-3d.settings.engine_port <port>`"
        )


def seed_embed(world: Path) -> bool:
    """Turn ``embed.activa`` on in the world's ``entorno/espejo.json``. True when the file changed.

    The real engine's ``hermes`` template ships with embed OFF and the engine reads the file only at start, so a world born
    just now (see :func:`start_engine`) must be edited and the engine restarted. Only that file, only that key; the rest of
    the owner's JSON is kept as parsed (the engine's own long ``notas`` text included).
    """
    target = world / SWITCH_FILE
    data = _world_json(world, SWITCH_FILE)
    if data is None:
        return False
    embed = data.get("embed")
    if isinstance(embed, dict) and embed.get("activa") is True:
        return False
    data["embed"] = {**(embed if isinstance(embed, dict) else {}), "activa": True}
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return True


def start_engine(cfg: S.Hermes3DSettings, hold_stdin: bool = True) -> dict[str, Any]:
    """Start the engine; on a world it just created for Path A (``engine_link`` off) turn embed on and restart it once.

    The real engine builds a new world from its template at the first start, with embed OFF, and reads the file only at
    start. Without this step the first run ends in "this world does not accept an embedded page". An existing world is
    never edited.
    """
    hold_stdin = True  # always: see _start_engine
    fresh = world_info(cfg.world_path) is None
    started = _start_engine(cfg, hold_stdin)
    if not fresh or cfg.engine_link or started.get("reused") or not seed_embed(cfg.world_path):
        return started
    if started.get("stdin_fd") is not None:
        os.close(started["stdin_fd"])
    stop_engine({"pid": started["pid"], **(started.get("identity") or {})})
    again = _start_engine(cfg, hold_stdin)
    again["embed_seeded"] = True
    return again


def _sow_bodies(cfg: S.Hermes3DSettings) -> None:
    """Before the engine starts (it reads ``aventura/`` only then): one stick figure per profile. Best effort, never fatal."""
    if (
        world_info(cfg.world_path) is None
    ):  # a folder we create here would make the engine refuse it as "not a world"; the next start sows
        return
    try:
        from . import agentes, cuerpos, scan

        names = [n for n, _d in scan.profile_dirs(scan.default_root())]
        from . import gasto

        try:  # the spend travels with the body (what you read when you talk to it); no spend must not skip the body
            spend = gasto.by_profile(scan.scan_gasto(scan.default_root()))
        except Exception:
            logger.debug("no spend for the bodies", exc_info=True)
            spend = {}
        try:  # what each agent is doing / did last, from the Kanban (titles only)
            activity = agentes.activity_by_profile(scan.scan_kanban(scan.default_root()))
        except Exception:
            logger.debug("no activity for the bodies", exc_info=True)
            activity = {}
        cuerpos.sow(cfg.world_path, names, spend=spend, activity=activity)
    except Exception:  # bodies are decoration: a failure must not stop the world
        logger.debug("could not sow the bodies", exc_info=True)


def _start_engine(cfg: S.Hermes3DSettings, hold_stdin: bool = False) -> dict[str, Any]:
    """Start the engine for the configured world unless it already runs (reuse).

    The engine ALWAYS gets a stdin pipe and ``MOTOR_SALIR_SI_STDIN_CIERRA=1``: its write end is returned as ``stdin_fd`` and the
    caller must hand it to a long-lived process (the bridge) or close it, so that when that process dies the engine sees EOF and
    exits. ``hold_stdin`` is kept for old callers and is ignored (a closed-stdin engine with the flag would exit at once).
    """
    hold_stdin = True
    main = cfg.engine_main()
    if main is None:
        why = "`engine_path` is not set" if cfg.engine_path is None else f"{cfg.engine_path} does not contain {S.ENGINE_MAIN}"
        raise SupervisorError(f"Numinia Engine not found: {why}. {S.ENGINE_HINT}")
    check_world_is_ours(cfg.world_path)
    lport = link_port(cfg)
    rec = config.read_engine_record(cfg.home)  # None for a dead or recycled pid: the stale file is dropped below
    if rec is None:
        config.clear_engine_pid(cfg.home)
    elif engine_healthy(cfg.engine_port):
        return {"reused": True, "port": cfg.engine_port, "link_port": lport, "pid": int(rec["pid"])}
    if link_listening(lport):
        return {"reused": True, "port": cfg.engine_port, "link_port": lport, "pid": None}
    check_ports(cfg, lport)
    _sow_bodies(cfg)
    key = linkkey.ensure(cfg.home)
    node = node_runtime()
    marker = cfg.engine_install()
    keys = enginekeys.ensure(cfg.home, str(marker.get("sha256", "checkout")) if marker else "checkout")
    log = config.plugin_data_dir(cfg.home) / "engine.log"
    read_fd, write_fd = os.pipe() if hold_stdin else (None, None)
    try:
        with config.open_log(log) as lf:
            proc = subprocess.Popen(
                [node.binary, str(main)],
                cwd=str(cfg.engine_path),
                env=engine_environment(cfg, key, lport, node, keys, exit_on_stdin_eof=hold_stdin),
                stdin=read_fd if read_fd is not None else subprocess.DEVNULL,
                stdout=lf,
                stderr=lf,
                start_new_session=True,
            )
    except BaseException:
        for fd in (read_fd, write_fd):
            if fd is not None:
                os.close(fd)
        raise
    if read_fd is not None:
        os.close(read_fd)
    try:
        ident = procid.identity(proc.pid)
        deadline = time.monotonic() + ENGINE_START_TIMEOUT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise EngineStartFailed(f"the engine exited with code {proc.returncode}; see {log}\n{log_tail(log)}")
            # read AFTER /health: a new world's espejo.json only exists once the engine has made it
            healthy = engine_healthy(cfg.engine_port)
            link_on = healthy and espejo_enabled(cfg.world_path)
            if healthy and (not link_on or link_listening(lport)):
                return {
                    "reused": False,
                    "port": cfg.engine_port,
                    "link_port": lport,
                    "pid": proc.pid,
                    "identity": ident,
                    "link_enabled": link_on,
                    "serves_page": cfg.www_dir() is not None,
                    "stdin_fd": write_fd,
                    "node_warnings": list(node.warnings),
                }
            time.sleep(0.2)
        if ident is not None and procid.same_process(proc.pid, ident):
            _signal_group(proc.pid, signal.SIGTERM)
        raise EngineStartFailed(
            f"the engine did not open 127.0.0.1:{cfg.engine_port} (/health) within {ENGINE_START_TIMEOUT:.0f} s; see {log}\n{log_tail(log)}"
        )
    except BaseException:
        if write_fd is not None:
            os.close(write_fd)
        raise


def _signal_group(pid: int, sig: int) -> None:
    """Signal the engine's process group (it leads its own session), else just the process."""
    try:
        if procid.own_group(pid):
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
    except ProcessLookupError:
        pass


def stop_engine(record: dict[str, Any] | None, grace: float = 5.0) -> bool:
    """SIGTERM the engine we started, SIGKILL after ``grace``. True when it is gone.

    Signals are sent only while the pid still has the recorded start time and command line.
    """
    if not record:
        return True
    pid = int(record["pid"])
    if not procid.same_process(pid, record):
        return True  # gone, or the pid now belongs to somebody else: never signal it
    _signal_group(pid, signal.SIGTERM)
    end = time.monotonic() + grace
    while time.monotonic() < end:
        if not procid.same_process(pid, record):
            return True
        time.sleep(0.1)
    if procid.same_process(pid, record):
        _signal_group(pid, signal.SIGKILL)
        end = time.monotonic() + 2.0  # SIGKILL is asynchronous: give the kernel a moment to reap it
        while time.monotonic() < end and procid.same_process(pid, record):
            time.sleep(0.05)
    return not procid.same_process(pid, record)


def spawn_bridge(
    home: Path, read_only: bool = False, demo: bool = False, hold_fd: int | None = None, world: Path | None = None
) -> int:
    """Start the bridge process detached (one per profile). Returns its pid.

    It receives only ``HERMES_HOME`` (it reads the settings from config.yaml itself) and, if set, the
    secret link key through the environment (never on the command line).
    """
    log = config.plugin_data_dir(home) / "bridge.log"
    env = {
        k: v
        for k, v in os.environ.items()
        if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "XDG_RUNTIME_DIR", "PYTHONPATH", linkkey.ENV_LINK_KEY)
    }
    env["HERMES_HOME"] = str(home)
    with config.open_log(log) as lf:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "hermes_3d_lib.bridge_main",
                *(["--read-only"] if read_only else []),
                *(["--demo"] if demo else []),
                *(["--world", str(world)] if world else []),
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=lf,
            stderr=lf,
            start_new_session=True,
            pass_fds=(hold_fd,) if hold_fd is not None else (),
        )
    return proc.pid
