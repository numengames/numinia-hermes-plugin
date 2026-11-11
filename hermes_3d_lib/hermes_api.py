"""First layer for Kanban and sessions: Hermes' OWN interfaces, before any direct read of its SQLite files.

* **Kanban** -- ``hermes kanban list --json`` (a public CLI). The JSON carries task bodies and results, so every row is projected
  to a fixed whitelist the moment it is parsed and the rest is dropped; the world sees counts and redacted titles only.
* **Sessions** -- ``SessionDB(read_only=True)``: ``session_count()`` / ``session_count_by_source()`` for totals and
  ``get_session_list_row(id)`` for one session's id, title, source, model, start/end times and message count. That row is the
  picker projection (no system prompt, no message text). The listing methods (``list_sessions_rich``,
  ``list_recent_sessions_bounded``) are NOT used: both build a ``preview`` from message bodies inside SQL (verified, see
  calidad/REVIEW-RESPONSE.md), so ids come from the session hooks instead of a listing.

Both fail soft: a missing ``hermes``, a timeout, bad JSON or an import error returns ``None`` and the caller falls back to the opt-in
direct read (``allow_direct_db_read``) or publishes nothing.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .crontable import clean_text
from .logsafe import redact_title

logger = logging.getLogger(__name__)

CLI_TIMEOUT = 10.0
MAX_JSON_BYTES = 4_000_000
KANBAN_STATUS_COLUMN = {
    "triage": "hacer",
    "todo": "hacer",
    "ready": "hacer",
    "running": "curso",
    "blocked": "curso",
    "done": "hecho",
}
TOP_CARDS = 3
MAX_OPEN_CARDS = 25
Runner = Callable[..., Any]


def _num(value: Any) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def project_task(raw: Any) -> dict[str, Any] | None:
    """One kanban JSON task -> the fixed set of fields we keep. Body, result, workspace, skills... never leave this function."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not isinstance(raw.get("status"), str):
        return None
    return {
        "id": clean_text(raw["id"], 40),
        "title": raw.get("title") if isinstance(raw.get("title"), str) else "",
        "status": clean_text(raw["status"], 20),
        "assignee": raw.get("assignee") if isinstance(raw.get("assignee"), str) else "",
        "priority": _num(raw.get("priority")),
        "created_at": _num(raw.get("created_at")),
        "started_at": _num(raw.get("started_at")),
        "completed_at": _num(raw.get("completed_at")),
    }


def kanban_section(tasks: list[dict[str, Any]], days: int, now: float | None = None) -> dict[str, Any]:
    """Same shape as ``scan.scan_kanban`` (one board), built from projected tasks."""
    since = (now if now is not None else time.time()) - max(1, days) * 86400
    by: dict[str, int] = {}
    for t in tasks:
        by[t["status"]] = by.get(t["status"], 0) + 1
    sort_key: dict[str, Callable[[dict[str, Any]], tuple[float, float]]] = {
        "hacer": lambda t: (t["priority"], t["created_at"]),
        "curso": lambda t: (t["started_at"] or t["created_at"], 0.0),
        "hecho": lambda t: (t["completed_at"] or t["created_at"], 0.0),
    }
    columns: dict[str, Any] = {}
    for col in ("hacer", "curso", "hecho"):
        rows = [t for t in tasks if KANBAN_STATUS_COLUMN.get(t["status"]) == col]
        if col == "hecho":
            rows = [t for t in rows if t["completed_at"] >= since]
        rows.sort(key=sort_key[col], reverse=True)
        top = [x for x in (redact_title(t["title"], 32) for t in rows[:TOP_CARDS]) if x]
        columns[col] = {"n": len(rows), "top": top}
    open_rows = [t for t in tasks if t["status"] not in ("done", "archived")]
    open_rows.sort(key=lambda t: (t["priority"], t["created_at"]), reverse=True)
    board = {"id": "default", "tarjetas": len(tasks), "por_estado": by, "columnas": columns}
    return {
        "tableros": 1,
        "tablero": "default",
        "columnas": columns,
        "tarjetas": len(tasks),
        "items": [board],
        "abiertas": [
            {
                "tablero": "default",
                "id": t["id"],
                "titulo": redact_title(t["title"], 60),
                "estado": t["status"],
                "asignado": clean_text(t["assignee"], 30),
            }
            for t in open_rows[:MAX_OPEN_CARDS]
        ],
        "via": "hermes kanban list --json",
        "error": "",
    }


def parse_kanban_json(text: str) -> list[dict[str, Any]] | None:
    """Parse the CLI output (a JSON list of tasks) and project every task. None = not the shape we know."""
    if len(text) > MAX_JSON_BYTES:
        return None
    start = text.find("[")  # the CLI may print a notice line before the JSON
    try:
        data = json.loads(text[start:]) if start >= 0 else None
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    return [t for t in (project_task(x) for x in data) if t is not None]


def kanban_via_cli(days: int, *, runner: Runner = subprocess.run, hermes: str | None = None) -> dict[str, Any] | None:
    """``hermes kanban list --json`` as an argv list with a timeout; None on any failure (the caller falls back)."""
    exe = hermes or shutil.which("hermes")
    if exe is None:
        return None
    try:
        done = runner(
            [exe, "kanban", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT,
            check=False,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if done.returncode != 0:
        return None
    tasks = parse_kanban_json(done.stdout or "")
    return None if tasks is None else kanban_section(tasks, days)


# ------------------------------------------------------------------------------------------------ sessions
SESSION_COLUMNS = ("id", "source", "title", "started_at", "ended_at", "message_count", "model")
Opener = Callable[[Path], Any]


def _default_opener(db_path: Path) -> Any:
    from hermes_state import SessionDB

    return SessionDB(db_path, read_only=True)


def project_session(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict) or not isinstance(row.get("id"), str):
        return None
    ended = row.get("ended_at")
    return {
        "id": row["id"],
        "fuente": clean_text(row.get("source") or "", 20),
        "titulo": redact_title(row.get("title") or "", 60),
        "modelo": clean_text(row.get("model") or "", 40),
        "inicio": row.get("started_at") if isinstance(row.get("started_at"), int | float) else None,
        "fin": ended if isinstance(ended, int | float) else None,
        "mensajes": int(row["message_count"])
        if isinstance(row.get("message_count"), int) and not isinstance(row.get("message_count"), bool)
        else 0,
        "activa": ended is None,
    }


def sessions_via_api(
    profiles: list[tuple[str, Path]], session_ids: list[str], *, opener: Opener | None = None, limit: int = 50
) -> dict[str, Any] | None:
    """Totals and the metadata of the given session ids from Hermes' own ``SessionDB`` (read-only). None if it cannot be used.

    ``session_ids`` are the sessions the hooks have seen; nothing is listed, so no message-derived preview is ever produced.
    """
    open_db = opener or _default_opener
    per: dict[str, Any] = {}
    found: list[dict[str, Any]] = []
    remaining = [s for s in session_ids if isinstance(s, str)][:limit]
    opened = 0
    for name, pdir in profiles:
        path = pdir / "state.db"
        if not path.is_file():
            continue
        try:
            db = open_db(path)
        except ImportError:
            return None
        except (OSError, ValueError, RuntimeError):
            logger.warning("SessionDB could not open a profile database", exc_info=True)
            continue
        opened += 1
        try:
            per[name] = {
                "total": int(db.session_count(include_archived=True)),
                "por_fuente": dict(db.session_count_by_source(include_archived=True)),
            }
            for sid in list(remaining):
                row = project_session(db.get_session_list_row(sid))
                if row is not None:
                    found.append({"perfil": name, **row})
                    remaining.remove(sid)
        except (OSError, ValueError, RuntimeError, TypeError, AttributeError):
            logger.warning("SessionDB read failed for a profile", exc_info=True)
        finally:
            close = getattr(db, "close", None)
            if callable(close):
                close()
    if not opened:
        return None
    return {
        "total": sum(v["total"] for v in per.values()),
        "por_perfil": per,
        "conocidas": found[:limit],
        "via": "SessionDB(read_only=True)",
        "error": "",
    }
