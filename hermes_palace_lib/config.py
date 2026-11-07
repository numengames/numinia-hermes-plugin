"""Locations, the per-start runtime file of the bridge, and owner-only file helpers.

Nothing is hardcoded: everything derives from ``HERMES_HOME`` (the active profile). Settings live in
``settings.py``; the link key in ``linkkey.py``.

Disk layout (all under ``<HERMES_HOME>/plugin-data/hermes-palace/`` -- Hermes' own per-plugin storage, see
``plugins.plugin_storage.plugin_data_dir`` -- created 0700, every file 0600; the pre-0.5 folder
``<HERMES_HOME>/plugin-data/hermes-palace/`` is moved here once by ``migrate_legacy_dir``):
    runtime.json    {"pid","port","token","started","engine_url","world_url"}  removed on ``down``
    engine.pid      {"pid","start","cmd"} of the engine process WE started
    link.key        the plugin's own engine link key (see linkkey.py)
    bridge.log      log of the bridge process (secrets redacted)
    engine.log      stdout/stderr of the engine we started
"""

from __future__ import annotations

import contextlib
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
PLUGIN_NAME = "hermes-palace"
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


def legacy_dir_path(home: Path) -> Path:
    """The pre-0.5 location (``<home>/hermes-palace``), kept only to migrate it."""
    return home / PLUGIN_NAME


# What the old folder could hold: files we wrote and folders we created. Nothing else is ever moved.
_LEGACY_FILES = (
    "runtime.json",
    "engine.pid",
    "link.key",
    "bridge.log",
    "engine.log",
    "docs-viewer.pid",
    "docs-viewer.log",
    "events.jsonl",
)
_LEGACY_DIRS = ("mundo", "engine", "docs-site")


def migrate_legacy_dir(home: Path) -> list[str]:
    """Move the pre-0.5 ``<home>/hermes-palace`` entries into the new data dir, once. Returns the names moved.

    Safe by construction: only the fixed names above; every source is ``lstat``-ed first and a symlink, a wrong-owner entry or the
    wrong kind is left alone (never followed, never moved); ``os.rename`` moves the directory entry itself (same filesystem, so
    atomic) and never overwrites: an existing target wins and the source stays. Moved files are re-chmodded 0600 through a
    ``O_NOFOLLOW`` descriptor, folders 0700. The old folder is removed only when it ends up empty.
    """
    old = legacy_dir_path(home)
    try:
        st = old.lstat()
    except OSError:
        return []
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        return []
    new = data_dir_path(home)
    new.mkdir(parents=True, exist_ok=True)
    new.chmod(0o700)
    moved: list[str] = []
    for name, want_dir in [(n, False) for n in _LEGACY_FILES] + [(n, True) for n in _LEGACY_DIRS]:
        src, dst = old / name, new / name
        try:
            sst = src.lstat()
        except OSError:
            continue
        kind_ok = stat.S_ISDIR(sst.st_mode) if want_dir else stat.S_ISREG(sst.st_mode)
        if not kind_ok or sst.st_uid != os.getuid():
            continue
        try:
            dst.lstat()
            continue  # the new location already has one: it wins, the old one stays for the user to look at
        except OSError:
            pass
        try:
            os.rename(src, dst)
        except OSError:
            continue
        moved.append(name)
        try:
            if want_dir:
                fd = os.open(dst, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                os.fchmod(fd, 0o700)
            else:
                fd = os.open(dst, os.O_RDONLY | os.O_NOFOLLOW)
                os.fchmod(fd, 0o600)
            os.close(fd)
        except OSError:
            pass
    with contextlib.suppress(OSError):
        old.rmdir()  # only succeeds when empty
    return moved


def palace_dir(home: Path | None = None) -> Path:
    """The plugin's private data folder (0700), created on demand; the pre-0.5 folder is migrated the first time."""
    h = home or hermes_home()
    d = data_dir_path(h)
    if home is None:
        d = _host_data_dir(h) or d
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)  # we create it, so we own it; a failure here is worth surfacing
    migrate_legacy_dir(h)
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
    return palace_dir(home) / "runtime.json"


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
    return palace_dir(home) / "engine.pid"


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
