"""Session adapter: "new session = new place" (F0 row 2, A0 §2.5).

Hooks give us ids and metadata only (on_session_start / on_session_end). We never read message
text here: the world gets a place per session with a neutral title and the model/platform, and
the place is ``activo`` until the session is finalized (then ``apagado``: the engine's own word).
"""

from __future__ import annotations

import hashlib
from typing import Any

from .protocol import make_object

ZONE_SESSIONS = "z3"
TYPE_SESSION = "sesion"
MAX_SESSIONS = 50  # recent places kept; older ones are removed (baja) to bound the world


def engine_id(session_id: str) -> str:
    """Stable engine id from whatever Hermes calls the session: ``s-`` + 16 hex of its SHA-256.
    (Hermes ids can be long or carry characters the engine's id rule refuses.)"""
    return "s-" + hashlib.sha256(str(session_id or "sin-id").encode("utf-8")).hexdigest()[:16]


def session_object(session_id: str, *, model: str = "", platform: str = "", active: bool = True) -> dict[str, Any]:
    sid = str(session_id or "")[:64] or "sin-id"
    short = "".join(ch for ch in sid[-8:] if ch.isalnum())
    return make_object(
        TYPE_SESSION,
        engine_id(sid),
        f"Sesión {short}",
        subtitulo=" · ".join(p for p in (str(platform or "")[:20], str(model or "")[:40]) if p),
        estado="activo" if active else "apagado",
        zona=ZONE_SESSIONS,
        acciones=[],
    )


class SessionBook:
    """Remembers which session places exist so end/start map to cambio/alta/baja."""

    def __init__(self, limit: int = MAX_SESSIONS) -> None:
        self.limit = limit
        self._order: list[str] = []
        self._objs: dict[str, dict[str, Any]] = {}

    def start(self, session_id: str, **kw: Any) -> list[dict[str, Any]]:
        obj = session_object(session_id, active=True, **kw)
        key = obj["id"]
        msgs: list[dict[str, Any]] = []
        kind = "cambio" if key in self._objs else "alta"
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)
        self._objs[key] = obj
        msgs.append({"t": kind, "objeto": obj})
        while len(self._order) > self.limit:
            old = self._order.pop(0)
            gone = self._objs.pop(old, None)
            if gone:
                msgs.append({"t": "baja", "objeto": {"tipo": gone["tipo"], "id": gone["id"]}})
        return msgs

    def end(self, session_id: str) -> list[dict[str, Any]]:
        key = engine_id(str(session_id or "")[:64] or "sin-id")
        prev = self._objs.get(key)
        if prev is None:
            return []
        obj = dict(prev, estado="apagado")
        self._objs[key] = obj
        return [{"t": "cambio", "objeto": obj}]

    def all(self) -> list[dict[str, Any]]:
        return [self._objs[k] for k in self._order]
