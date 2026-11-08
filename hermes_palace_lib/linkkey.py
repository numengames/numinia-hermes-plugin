"""The engine link key: where it comes from and how it is stored.

Decision (documented in docs/ARCHITECTURE.md): the plugin never reads another program's credential file
(the engine's ``<world>/enlace.env``). The key has exactly two sources, in this order:

1. ``HERMES_PALACE_LINK_KEY`` in the environment (Hermes loads its own secrets file into the process environment). Use this to
   join an engine somebody else started: put *that* engine's ``MOTOR_ENLACE_CLAVE`` here.
2. A key the plugin generated itself, stored in the plugin's own data folder
   (``<HERMES_HOME>/hermes-palace/link.key``, mode 0600, created atomically, never followed through a
   symlink). When ``hermes palace up`` launches the engine it hands this key over through the engine's
   documented ``MOTOR_ENLACE_CLAVE`` variable (engine docs/EXTENDER.md, "Espejo (v1)", "Clave"), so
   the engine never has to write ``enlace.env`` and the plugin never has to read it.

The key is never printed, logged, or put on a command line.
"""

from __future__ import annotations

import os
import re
import secrets
import stat
from pathlib import Path

from . import config

ENV_LINK_KEY = "HERMES_PALACE_LINK_KEY"
ENGINE_KEY_ENV = "MOTOR_ENLACE_CLAVE"  # the engine's own name for the same secret
KEY_FILE = "link.key"
_VALID = re.compile(r"\A[A-Za-z0-9_-]{16,128}\Z")


def valid(key: str | None) -> bool:
    return bool(key) and _VALID.fullmatch(key or "") is not None


def key_path(home: Path) -> Path:
    return config.palace_dir(home) / KEY_FILE


def _read_private(path: Path) -> str | None:
    """Read ``path`` only if it is a regular file we own with no group/other access; never follow a symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
            return None
        text = os.read(fd, 512).decode("ascii", "ignore").strip()
    finally:
        os.close(fd)
    return text if valid(text) else None


def _secret() -> str:
    """The key from Hermes' own secret scope (profile-aware) when available, else the plain environment."""
    try:
        from agent.secret_scope import get_secret

        return str(get_secret(ENV_LINK_KEY, "") or "")
    except (ImportError, RuntimeError):  # no Hermes in this process (bridge, tests) or an unscoped multiplexer: use the env
        return os.environ.get(ENV_LINK_KEY, "")


def resolve(home: Path) -> str | None:
    """The key to use right now, or None when neither source has one."""
    env = _secret().strip()
    if valid(env):
        return env
    return _read_private(key_path(home))


def ensure(home: Path) -> str:
    """Return :func:`resolve`'s key, generating and storing one (0600) when there is none yet."""
    existing = resolve(home)
    if existing:
        return existing
    path = key_path(home)
    key = secrets.token_urlsafe(32)
    if path.exists() or path.is_symlink():  # present but unusable (wrong mode, owner, content): replace it
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(key + "\n")
    return key


def source(home: Path) -> str:
    """Where the key comes from, for ``status`` (never the key itself)."""
    if valid(_secret().strip()):
        return f"environment ({ENV_LINK_KEY})"
    return "plugin key file" if _read_private(key_path(home)) else "missing"
