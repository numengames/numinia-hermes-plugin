# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""Process identity: prove a pid is still *the process we started* before signalling it.

A bare pid can be recycled by the kernel after the process dies; ``os.killpg(pid, ...)`` on a recycled pid
would signal a stranger. We record (start time, command line) when we spawn and compare before any signal.
Linux reads ``/proc``; other POSIX systems use ``ps`` with an argv list (no shell).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any


def identity(pid: int, settle: float = 0.5) -> dict[str, Any] | None:
    """``{"start": <opaque>, "cmd": <command line>}`` of a live pid, or None.

    Just after ``exec`` the kernel briefly reports an empty command line; we wait up to ``settle`` seconds for
    the real one, because an identity recorded from that blank would never match again.
    """
    if not isinstance(pid, int) or pid <= 1:
        return None
    deadline = time.monotonic() + settle
    while True:
        found = _identity_once(pid)
        if found is None or found["cmd"] or time.monotonic() >= deadline:
            return found if found and found["cmd"] else None
        time.sleep(0.01)


def _identity_once(pid: int) -> dict[str, Any] | None:
    proc = Path("/proc") / str(pid)
    try:
        stat = (proc / "stat").read_text(encoding="utf-8", errors="replace")
        # the command name is parenthesised and may contain spaces: split after the last ')'
        start = stat.rsplit(")", 1)[1].split()[19]
        cmd = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        return {"start": start, "cmd": cmd}
    except (OSError, IndexError):
        pass
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=,command=", "-p", str(pid)], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = out.stdout.strip()
    if out.returncode != 0 or not line:
        return None
    return {"start": line[:24], "cmd": line[25:].strip()}


def same_process(pid: int, recorded: dict[str, Any] | None) -> bool:
    """True only when ``pid`` is alive AND has the recorded start time and command line."""
    if not recorded:
        return False
    now = identity(pid)
    return now is not None and now["start"] == recorded.get("start") and now["cmd"] == recorded.get("cmd")


def own_group(pid: int) -> bool:
    """True when ``pid`` leads its own process group (we start the engine with ``start_new_session``)."""
    try:
        return os.getpgid(pid) == pid
    except OSError:
        return False
