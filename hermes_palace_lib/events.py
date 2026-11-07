"""Live-activity feed: Hermes observer hooks -> a small bounded spool -> ``GET /api/plugins/hermes-palace/events``.

Metadata only. An event carries its kind, the platform (``cli``/``cron``/``telegram``/...; ``subagent`` for a
delegated child), the profile, an opaque id, the tool NAME, a status and timestamps. It never carries tool arguments,
tool results, prompts, goals, summaries, messages or model output: :func:`make_event` builds the record from an
allow-list and :func:`clean` cuts every value, so a hook payload cannot leak a field by accident.

Why a file and not a queue in memory: the hooks run inside whichever Hermes process is working (CLI, gateway, cron
runner, a delegated child) and the dashboard route runs in another one. They share the Hermes home, so the spool is
``<HERMES_HOME>/plugin-data/hermes-palace/events.jsonl`` (0600, appended with one ``O_APPEND`` write per event, trimmed to the last
:data:`KEEP` lines when it passes :data:`MAX_BYTES`). Nothing is written unless the ``sesiones`` switch is on.

The hook payloads (hermes-agent 0.22.7) do not all carry ``platform``: only ``on_session_start/end`` and
``pre_api_request`` do. The tool and subagent hooks carry session ids, so :class:`Platforms` remembers
``session_id -> platform`` (bounded) from the hooks that do, and tool/subagent events borrow it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config

logger = logging.getLogger(__name__)

SPOOL_NAME = "events.jsonl"
MAX_BYTES = 64 * 1024
KEEP = 200  # lines kept after a trim
MAX_LINE = 1024  # one O_APPEND write below PIPE_BUF-like sizes stays atomic enough; longer events are cut field-wise
DEFAULT_LIMIT = 100
MAX_LIMIT = 200
PLATFORM_MEMORY = 256

KINDS = ("subagent_start", "subagent_stop", "tool_start", "tool_end", "api_request")
# The only keys that can ever leave: kind, ts and id are added by make_event itself.
FIELDS = ("platform", "model", "profile", "session", "subagent", "parent", "tool", "status", "role", "duration_ms")
_LIMITS = {
    "platform": 20,
    "model": 64,
    "profile": 40,
    "session": 64,
    "subagent": 64,
    "parent": 64,
    "tool": 64,
    "status": 20,
    "role": 24,
}


def clean(value: Any, limit: int) -> str:
    """A short single-line string without control characters (hook values are untrusted text)."""
    text = "".join(ch if ch.isprintable() else " " for ch in str(value if value is not None else ""))
    return " ".join(text.split())[:limit]


def make_event(kind: str, **fields: Any) -> dict[str, Any]:
    """Allow-listed event. Unknown keys (``args``, ``result``, ``goal``, ``summary``, ...) are dropped, not cut."""
    if kind not in KINDS:
        raise ValueError(f"unknown event kind {kind!r}")
    ts = time.time()
    ev: dict[str, Any] = {"id": time.time_ns(), "ts": round(ts, 3), "kind": kind}
    for key in FIELDS:
        if key not in fields:
            continue
        val = fields[key]
        if key == "duration_ms":
            try:
                ev[key] = max(0, int(val))
            except (TypeError, ValueError):
                ev[key] = 0
        else:
            ev[key] = clean(val, _LIMITS[key])
    return ev


def profile_name(home: Path | None = None) -> str:
    """``default`` or the profile folder name, from the Hermes home the hook runs in."""
    base = Path(home or config.hermes_home())
    return base.name if base.parent.name == "profiles" else "default"


class Platforms:
    """Bounded ``session_id -> {platform, model, role}`` memory: tool and subagent hooks carry none of it themselves."""

    def __init__(self, size: int = PLATFORM_MEMORY) -> None:
        self._size = size
        self._data: OrderedDict[str, dict[str, str]] = OrderedDict()
        self._lock = threading.Lock()

    def learn(self, session_id: str, platform: str = "", model: str = "", role: str = "") -> None:
        sid = clean(session_id, 64)
        if not sid:
            return
        new = {"platform": clean(platform, 20), "model": clean(model, 64), "role": clean(role, 24)}
        with self._lock:
            cur = self._data.setdefault(sid, {})
            cur.update({k: v for k, v in new.items() if v})
            self._data.move_to_end(sid)
            while len(self._data) > self._size:
                self._data.popitem(last=False)

    def context(self, session_id: str) -> dict[str, str]:
        with self._lock:
            return dict(self._data.get(clean(session_id, 64), {}))

    def get(self, session_id: str) -> str:
        return self.context(session_id).get("platform", "")


BROADCAST_NAMES = {
    "subagent_start": "agent.started",
    "subagent_stop": "agent.stopped",
    "tool_start": "tool.started",
    "tool_end": "tool.finished",
    "api_request": "api.request",
}


def desktop_broadcast(name: str, payload: dict[str, Any]) -> bool:
    """``plugin.hermes-palace.<name>`` to Hermes Desktop. A no-op (False) unless the gateway of ``hermes serve`` is in
    this process: from a plain CLI or gateway run nobody is listening, so nothing happens."""
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
    except ImportError:
        return False
    broadcast_plugin_event("hermes-palace", name, payload)
    return True


# ------------------------------------------------------------------------------------------------- the spool
def spool_path(home: Path | None = None) -> Path:
    return config.palace_dir(home) / SPOOL_NAME


def append(event: dict[str, Any], home: Path | None = None) -> bool:
    """One atomic append; trims the file when it is big. Never raises (a hook must not break a session)."""
    try:
        path = spool_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))[:MAX_LINE] + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            size = os.fstat(fd).st_size
        finally:
            os.close(fd)
        if size > MAX_BYTES:
            _trim(path)
        return True
    except OSError:
        return False


def _trim(path: Path) -> None:
    try:
        lines = path.read_bytes().splitlines()[-KEEP:]
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(b"\n".join(lines) + b"\n")
        os.replace(tmp, path)
    except OSError:
        pass


def read(since: int = 0, limit: int = DEFAULT_LIMIT, home: Path | None = None) -> dict[str, Any]:
    """Events with ``id > since`` (oldest first, at most ``limit``) and the id to ask for next. Never raises."""
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    out: list[dict[str, Any]] = []
    try:
        raw = spool_path(home).read_bytes()[-(MAX_BYTES * 2) :]
    except OSError:
        raw = b""
    for line in raw.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue  # a cut first line after a trim, or a partial write
        if not isinstance(ev, dict) or ev.get("kind") not in KINDS or not isinstance(ev.get("id"), int):
            continue
        if ev["id"] > since:
            out.append({k: ev[k] for k in ("id", "ts", "kind", *FIELDS) if k in ev})
    out = out[-limit:] if since <= 0 else out[:limit]
    return {"events": out, "next": out[-1]["id"] if out else since}


# ------------------------------------------------------------------------------------------------- the hooks
def make_hooks(
    enabled: Callable[[], bool],
    home: Path | None = None,
    broadcast: Callable[[str, dict[str, Any]], Any] = desktop_broadcast,
) -> dict[str, Callable[..., None]]:
    """Hook callbacks keyed by Hermes hook name. ``enabled`` is asked on every call (the user can flip the switch live)."""
    platforms = Platforms()

    def emit(kind: str, **fields: Any) -> None:
        ctx = platforms.context(fields.get("session", ""))
        for key in ("platform", "model", "role"):
            if not fields.get(key) and ctx.get(key):
                fields[key] = ctx[key]
        try:
            if enabled():
                ev = make_event(kind, profile=profile_name(home), **fields)
                append(ev, home)
                broadcast(BROADCAST_NAMES[kind], ev)  # same allow-listed record as the spool; separate guard below
        except Exception:
            logger.debug("palace: activity event dropped", exc_info=True)  # observer hooks must never break the agent

    def on_session_start(session_id: str = "", platform: str = "", model: str = "", **_kw: Any) -> None:
        platforms.learn(session_id, platform, model)

    def pre_llm_call(session_id: str = "", platform: str = "", model: str = "", **_kw: Any) -> None:
        # ``user_message`` and ``conversation_history`` are in the payload: never read.
        platforms.learn(session_id, platform, model)

    def pre_api_request(session_id: str = "", platform: str = "", model: str = "", **_kw: Any) -> None:
        # The payload also holds the prompt, the messages and the system prompt: none of it is read here.
        platforms.learn(session_id, platform, model)
        emit("api_request", platform=platform, model=model, session=session_id)

    def pre_tool_call(tool_name: str = "", session_id: str = "", **_kw: Any) -> None:
        emit("tool_start", platform=platforms.get(session_id), session=session_id, tool=tool_name)

    def post_tool_call(tool_name: str = "", session_id: str = "", status: str = "", duration_ms: int = 0, **_kw: Any) -> None:
        emit(
            "tool_end",
            platform=platforms.get(session_id),
            session=session_id,
            tool=tool_name,
            status=status,
            duration_ms=duration_ms,
        )

    def subagent_start(
        parent_session_id: str = "",
        child_session_id: str = "",
        child_subagent_id: str = "",
        parent_subagent_id: str = "",
        child_role: str = "",
        **_kw: Any,
    ) -> None:
        # ``child_goal`` is the delegated instruction: never read.
        pctx = platforms.context(parent_session_id)
        plat = pctx.get("platform", "")
        platforms.learn(child_session_id, plat, pctx.get("model", ""), child_role)
        emit(
            "subagent_start",
            platform=plat,
            session=child_session_id,
            subagent=child_subagent_id,
            parent=parent_subagent_id or parent_session_id,
            role=child_role,
        )

    def subagent_stop(
        parent_session_id: str = "",
        child_session_id: str = "",
        child_role: str = "",
        child_status: str = "",
        duration_ms: int = 0,
        **_kw: Any,
    ) -> None:
        # ``child_summary`` and ``tool_call_history`` carry model text and tool arguments: never read.
        emit(
            "subagent_stop",
            platform=platforms.get(parent_session_id),
            session=child_session_id,
            parent=parent_session_id,
            role=child_role,
            status=child_status,
            duration_ms=duration_ms,
        )

    return {
        "on_session_start": on_session_start,
        "pre_llm_call": pre_llm_call,
        "pre_api_request": pre_api_request,
        "pre_tool_call": pre_tool_call,
        "post_tool_call": post_tool_call,
        "subagent_start": subagent_start,
        "subagent_stop": subagent_stop,
    }
