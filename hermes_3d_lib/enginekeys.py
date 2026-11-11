# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""The engine's own secrets (``MOTOR_SECRET`` signs, ``MOTOR_ADMIN`` is the owner code), made once per engine install.

They live in ``<plugin data dir>/engine-keys.json`` (0600, created atomically, never followed through a symlink), are handed to the
engine child through its environment and are never printed, logged or put on a command line. A new engine install (another sha256)
gets new ones, so a leaked pair dies with the install it belonged to.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from pathlib import Path

from . import config

KEY_FILE = "engine-keys.json"
_VALID = re.compile(r"\A[A-Za-z0-9_-]{16,128}\Z")


def keys_path(home: Path) -> Path:
    return config.plugin_data_dir(home) / KEY_FILE


def _read(path: Path, install_id: str) -> tuple[str, str] | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
            return None
        data = json.loads(os.read(fd, 4096).decode("utf-8", "ignore"))
    except ValueError:
        return None
    finally:
        os.close(fd)
    if not isinstance(data, dict) or data.get("install") != install_id:
        return None
    secret, admin = data.get("secret"), data.get("admin")
    if isinstance(secret, str) and isinstance(admin, str) and _VALID.fullmatch(secret) and _VALID.fullmatch(admin):
        return secret, admin
    return None


def ensure(home: Path, install_id: str) -> tuple[str, str]:
    """``(MOTOR_SECRET, MOTOR_ADMIN)`` for this install; generated and stored (0600) on first use."""
    path = keys_path(home)
    existing = _read(path, install_id)
    if existing:
        return existing
    secret, admin = secrets.token_urlsafe(32), secrets.token_urlsafe(16)
    if path.exists() or path.is_symlink():
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"install": install_id, "secret": secret, "admin": admin}, fh)
    return secret, admin
