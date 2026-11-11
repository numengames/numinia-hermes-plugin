# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""Tiny client for the loopback bridge, used by the dashboard router and by `hermes 3d status`.

It reads ``<HERMES_HOME>/hermes-3d/runtime.json`` (0600, written by the bridge at start) and
talks to ``127.0.0.1:<port>`` with the per-start bearer token. Synchronous and dependency-free
(stdlib ``http.client``) so it also works when the router runs in another process
(``plugins.isolation: host``) or the bridge is not running (every call degrades to an error dict,
never raises).
"""

from __future__ import annotations

import http.client
import json
from pathlib import Path
from typing import Any

from . import config


def call(
    method: str, path: str, body: dict[str, Any] | None = None, *, timeout: float = 2.0, home: Path | None = None
) -> dict[str, Any]:
    """``{"ok":True,"status":200,"data":{...}}`` or ``{"ok":False,"error":"…"}``."""
    info = config.read_runtime(home)
    if not info or not isinstance(info.get("port"), int) or not info.get("token"):
        return {"ok": False, "error": "bridge-not-running"}
    if not config.pid_alive(info.get("pid", 0)):
        return {"ok": False, "error": "bridge-stale"}
    raw = json.dumps(body).encode() if body is not None else b""
    headers = {"Authorization": f"Bearer {info['token']}", "Content-Length": str(len(raw)), "Content-Type": "application/json"}
    conn = http.client.HTTPConnection(config.HOST, info["port"], timeout=timeout)
    try:
        conn.request(method, path, body=raw or None, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        return {"ok": 200 <= resp.status < 300, "status": resp.status, "data": data}
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return {"ok": False, "error": f"bridge-unreachable: {type(exc).__name__}"}
    finally:
        conn.close()
