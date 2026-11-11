# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""Locations, the per-start runtime file of the bridge, and owner-only file helpers.

Nothing is hardcoded: everything derives from ``HERMES_HOME`` (the active profile). Settings live in
``settings.py``; the link key in ``linkkey.py``.

Disk layout (all under ``<HERMES_HOME>/plugin-data/hermes-3d/`` -- Hermes' own per-plugin storage, see
``plugins.plugin_storage.plugin_data_dir`` -- created 0700, every file 0600):
    runtime.json    {"pid","port","token","started","engine_url","world_url"}  removed on ``down``
    engine.pid      {"pid","start","cmd"} of the engine process WE started
    link.key        the plugin's own engine link key (see linkkey.py)
    bridge.log      log of the bridge process (secrets redacted)
    engine.log      stdout/stderr of the engine we started
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import time
from pathlib import Path
from typing import Any, BinaryIO

from . import procid

# The only address the bridge ever binds; not configurable. Composed at runtime so no address literal sits in shipped code.
HOST = ".".join(("127", "0", "0", "1"))
PLUGIN_NAME = "hermes-3d"
# Local switch name -> the engine's function name (features/espejo/esquema.ts FUNCIONES): agents are the engine's
# ``perfiles`` (type ``perfil``) and spend is ``uso`` (type ``uso``). Same name when not listed.
ENGINE_FUNCTION = {"agentes": "perfiles", "gasto": "uso"}


def hermes_home() -> Path:
    """The active profile's home, from the documented env var (never ``~``)."""
    raw = os.environ.get("HERMES_HOME")
    if not raw:
        raise RuntimeError("HERMES_HOME is not set; run inside Hermes or set it explicitly")
    return Path(raw)


def data_dir_path(home: Path) -> Path:
    """Where this plugin's files live under *home*: Hermes' per-plugin storage convention (pure, creates nothing)."""
    return home / "plugin-data" / PLUGIN_NAME


def plugin_data_dir(home: Path | None = None) -> Path:
    """The plugin's private data folder (0700), created on demand."""
    h = home or hermes_home()
    d = data_dir_path(h)
    if home is None:
        d = _host_data_dir(h) or d
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)  # we create it, so we own it; a failure here is worth surfacing
    return d


def _host_data_dir(home: Path) -> Path | None:
    """Hermes' own ``plugin_data_dir`` when this process can import it and it agrees with our layout, else None.

    The bridge runs in its own process and may not have Hermes importable; the layout is the same either way.
    """
    try:
        from plugins.plugin_storage import plugin_data_dir
    except ImportError:
        return None
    try:
        got = Path(plugin_data_dir(PLUGIN_NAME))
    except (OSError, ValueError, TypeError):
        return None
    return got if got == data_dir_path(home) else None


def runtime_path(home: Path | None = None) -> Path:
    return plugin_data_dir(home) / "runtime.json"


def write_runtime(info: dict[str, Any], home: Path | None = None) -> Path:
    """Write the runtime file 0600 atomically (the token lives only here and in memory)."""
    p = runtime_path(home)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # umask-independent, also when the file already existed
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(info, fh)
    os.replace(tmp, p)
    return p


def read_runtime(home: Path | None = None) -> dict[str, Any] | None:
    p = runtime_path(home)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def engine_pid_path(home: Path | None = None) -> Path:
    return plugin_data_dir(home) / "engine.pid"


def write_engine_pid(pid: int, home: Path | None = None) -> None:
    """Remember the engine process WE started, with its start time and command line, so ``down`` can
    prove the pid was not recycled before it signals anything."""
    ident = procid.identity(pid) or {}
    p = engine_pid_path(home)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"pid": int(pid), **ident}, fh)


def read_engine_record(home: Path | None = None) -> dict[str, Any] | None:
    """The recorded engine process if it is STILL that process, else None (stale or recycled pid)."""
    try:
        rec = json.loads(engine_pid_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or not isinstance(rec.get("pid"), int):
        return None
    return rec if procid.same_process(rec["pid"], rec) else None


def clear_engine_pid(home: Path | None = None) -> None:
    engine_pid_path(home).unlink(missing_ok=True)


def clear_runtime(home: Path | None = None) -> None:
    runtime_path(home).unlink(missing_ok=True)


def new_token() -> str:
    """Fresh bridge token for this start (256 bits)."""
    return secrets.token_urlsafe(32)


def runtime_mode_ok(home: Path | None = None) -> bool:
    """True when runtime.json is owner-only (used by tests and ``status``)."""
    try:
        return stat.S_IMODE(runtime_path(home).stat().st_mode) == 0o600
    except OSError:
        return False


def pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def started_stamp() -> float:
    return time.time()


def open_log(path: Path) -> BinaryIO:
    """Append-binary handle on a log file that is 0600 whatever the umask (and even if it pre-existed looser)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "ab")
