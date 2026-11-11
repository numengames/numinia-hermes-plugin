"""Path A (the world inside Hermes Desktop): the embed credential and the engine probe.

The Desktop page puts the world in a sandboxed frame with an opaque origin. The engine does not trust that origin (every
sandboxed or ``data:`` page of any site sends ``Origin: null``); it trusts a short-lived credential made with the link key:

    e1.<exp>.<nonce>.<hmac>     exp   = Unix milliseconds of expiry (at most 24 h ahead)
                                nonce = 16 base64url characters
                                hmac  = hex HMAC-SHA256(link key, "numinia-embed@1\\n" + exp + "." + nonce)

The credential is made here, in the backend, on every open of the page; the key itself never leaves this process. It travels
in the URL fragment of the frame (a fragment is never sent to a server, a log or a Referer).
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import secrets
import time
from typing import Any
from urllib.parse import urlsplit

from . import settings as S

PREFIX = "numinia-embed@1\n"
VERSION = "e1"
HINT_UP = "Run `hermes 3d up` to start the world"
HINT_SETUP = "Run `hermes 3d setup` first, then `hermes 3d up`"
HINT_EMBED = (
    'Turn the embed mode on in the world: entorno/espejo.json needs "embed": {"activa": true}, then restart with `hermes 3d up`'
)


def sign(key: str, exp: int, nonce: str) -> str:
    return hmac.new(key.encode("utf-8"), f"{PREFIX}{exp}.{nonce}".encode(), hashlib.sha256).hexdigest()


def make_credential(
    key: str, *, ttl_s: int = S.EMBED_TTL_DEFAULT_S, now_ms: int | None = None, nonce: str | None = None
) -> tuple[str, int]:
    """``(credential, expiry in ms)``. ``ttl_s`` is clamped to 60 s .. 24 h, the range the engine accepts."""
    ttl = max(S.EMBED_TTL_MIN_S, min(int(ttl_s), S.EMBED_TTL_MAX_S))
    exp = (int(time.time() * 1000) if now_ms is None else now_ms) + ttl * 1000
    nonce = nonce or secrets.token_urlsafe(12)  # 12 bytes -> exactly 16 base64url characters
    return f"{VERSION}.{exp}.{nonce}.{sign(key, exp, nonce)}", exp


def frame_url(base: str, credential: str) -> str:
    """The URL the frame loads: the world page with ``?embed=1`` and the credential in the fragment."""
    parts = urlsplit(base)
    path = parts.path or "/"
    return f"{parts.scheme}://{parts.netloc}{path}?embed=1#embed={credential}"


def probe_engine(base: str, timeout: float = 1.5) -> dict[str, Any]:
    """Ask the engine's ``/status``. ``{"up": False}`` when nothing answers; else ``{"up": True, "embed": bool}``."""
    parts = urlsplit(base)
    if parts.hostname not in S.LOOPBACK_NAMES or parts.port is None:
        return {"up": False}
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    try:
        conn.request("GET", "/status")
        resp = conn.getresponse()
        raw = resp.read(65536)
        data = json.loads(raw or b"{}") if resp.status == 200 else {}
    except (OSError, ValueError, http.client.HTTPException):
        return {"up": False}
    finally:
        conn.close()
    espejo = data.get("espejo") if isinstance(data, dict) else None
    return {"up": True, "embed": bool(isinstance(espejo, dict) and espejo.get("embed") is True)}


def issue(cfg: S.Hermes3DSettings, key: str | None, *, probe: Any = None) -> dict[str, Any]:
    """The answer of ``POST /embed-credential``. Always a dict with ``ok``; a refusal carries ``error`` and a ``hint``.

    Never contains the key. ``read_only`` is reported but does not matter here: looking at the world is always allowed.
    """
    base = cfg.page_url()
    seen = (probe or probe_engine)(base)
    if not seen.get("up"):
        hint = HINT_UP if cfg.engine_main() is not None else HINT_SETUP
        return {"ok": False, "error": "engine-down", "message": "The world engine is not running.", "hint": hint}
    if not key:
        return {
            "ok": False,
            "error": "no-link-key",
            "message": "The plugin has no link key, so it cannot open the world here.",
            "hint": HINT_UP,
        }
    if not seen.get("embed"):
        return {"ok": False, "error": "embed-off", "message": "This world does not accept an embedded page.", "hint": HINT_EMBED}
    credential, exp = make_credential(key, ttl_s=cfg.embed_ttl_s)
    return {
        "ok": True,
        "url": frame_url(base, credential),
        "expires_at_ms": exp,
        "ttl_s": cfg.embed_ttl_s,
        "read_only": cfg.read_only,
    }
