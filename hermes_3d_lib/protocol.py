"""Wire protocol of the local link between the Hermes plugin and the Numinia Engine "espejo" piece.

Source of truth: the engine's own ``packages/server/src/enlace-espejo.ts`` (what is built wins over
the A0 §3 draft). One JSON object per WebSocket text frame, at most 16 KB, every frame has ``v``.
The PLUGIN opens the connection (to the engine's own link port, 127.0.0.1 only, no ``Origin``).

Handshake (HMAC, the key never travels):
    engine -> plugin   {"v":1,"t":"reto","nonce":"<hex, single use>"}
    plugin -> engine   {"v":1,"t":"hola","plugin":"hermes-3d","hermes_version":"…",
                        "firma":"<hex hmac-sha256(key, 'numinia-enlace@1\\n' + nonce)>"}
    engine -> plugin   {"v":1,"t":"hola","funciones":["salud","cron"],"zonas":[…],"acciones":{…}}
                       (``funciones`` lists what is ON; what is off is never asked for)
A wrong or late signature closes the socket (4401); a second source gets 4409.

Afterwards:
    plugin -> engine   lote{objetos[]} | alta{objeto} | cambio{objeto} | baja{objeto:{tipo,id}}
                       | resultado{peticion,ok,texto} | pulso
    engine -> plugin   peticion{id,objeto,verbo,seguridad,quien:{id,nombre},caduca} | pulso
                       | error{en,motivo}
An ``objeto`` is ``{tipo,id,titulo,subtitulo,estado,zona,acciones[]}`` (nothing else: the engine rejects unknown keys); title ≤ 60,
subtitle ≤ 120 characters (the engine validates the same limits).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 16 * 1024
MAX_TITLE = 60  # BYTES (UTF-8): the engine rejects, never cuts
MAX_SUBTITLE = 120  # BYTES
MAX_OBJECTS_PER_BATCH = 200  # A0 §4.2: at most 200 visible objects per zone
KNOWN_VERBS = ("cron.pausar", "cron.reanudar", "cron.ejecutar", "sesion.nueva")
SIGN_PREFIX = "numinia-enlace@1\n"  # domain separation, as the engine's firmarReto()
LINK_PATH = ""  # the engine's link listens on the root of its own port

_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")  # engine: ID_RE
_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,24}$")


class ProtocolError(ValueError):
    """A frame that must not be sent or must be dropped."""


def sign_nonce(secret: str | bytes, nonce: str) -> str:
    """HMAC-SHA256(key, "numinia-enlace@1\\n" + nonce) as hex, exactly the engine's ``firmarReto``."""
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    return hmac.new(key, (SIGN_PREFIX + nonce).encode("utf-8"), hashlib.sha256).hexdigest()


def verify_signature(secret: str | bytes, nonce: str, firma: str) -> bool:
    """Constant-time check (used by the fake engine in tests and by anyone mirroring the engine)."""
    return hmac.compare_digest(sign_nonce(secret, nonce), str(firma))


def encode(msg: dict[str, Any]) -> str:
    """Serialise one frame; refuses anything over the frame limit (never truncates silently)."""
    msg = {"v": PROTOCOL_VERSION, **msg}
    text = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode("utf-8")) > MAX_FRAME_BYTES:
        raise ProtocolError(f"frame {msg.get('t')!r} is larger than {MAX_FRAME_BYTES} bytes")
    return text


def decode(raw: str | bytes) -> dict[str, Any]:
    """Parse one incoming frame; raises ProtocolError on anything malformed or oversized."""
    data = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    if len(data) > MAX_FRAME_BYTES:
        raise ProtocolError("frame too large")
    try:
        msg = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"not JSON: {exc}") from None
    if not isinstance(msg, dict) or not isinstance(msg.get("t"), str):
        raise ProtocolError("frame without type")
    if msg.get("v") != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version {msg.get('v')!r}")
    return msg


ESTADOS = ("activo", "programado", "pausado", "fallo", "apagado", "desconocido")  # engine: ESTADOS
OBJECT_KEYS = (
    "tipo", "id", "titulo", "subtitulo", "estado", "zona", "x", "z", "luz", "acciones",
    "url", "grupo", "descripcion", "tablero",
)  # fmt: skip
MAX_URL = 300  # BYTES (M6 ``documento``)
MAX_GROUP = 32  # BYTES (M6 ``habilidad``: the category the shelf rows are grouped by)
MAX_DESCRIPTION = 200  # BYTES (M6 ``habilidad`` / ``documento``)
MAX_VERBS_PER_OBJECT = 6


def cut_bytes(text: str, limit: int) -> str:
    """Cut to at most ``limit`` UTF-8 BYTES without splitting a character (the engine counts bytes)."""
    out = "".join(ch for ch in str(text or "") if ch.isprintable())
    raw = out.encode("utf-8")
    if len(raw) <= limit:
        return out
    return raw[:limit].decode("utf-8", errors="ignore")


def make_object(
    tipo: str,
    id_: str,
    titulo: str,
    *,
    subtitulo: str = "",
    estado: str = "desconocido",
    zona: str = "",
    acciones: list[str] | None = None,
    luz: bool | None = None,
    url: str | None = None,
    grupo: str | None = None,
    descripcion: str | None = None,
    tablero: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an ``objeto`` exactly as the engine's ``leerObjeto`` accepts it: only its keys, lowercase
    id up to 32, a known estado, text cut to BYTES, verbs from the known list. Raises on a bad id/type."""
    if not _TYPE.match(tipo or ""):
        raise ProtocolError(f"bad object type {tipo!r}")
    if not _ID.match(id_ or ""):
        raise ProtocolError(f"bad object id {id_!r}")
    verbs = [v for v in (acciones or []) if v in KNOWN_VERBS][:MAX_VERBS_PER_OBJECT]
    obj: dict[str, Any] = {
        "tipo": tipo,
        "id": id_,
        "titulo": cut_bytes(titulo, MAX_TITLE).strip() or "(sin nombre)",
        "subtitulo": cut_bytes(subtitulo, MAX_SUBTITLE),
        "estado": estado if estado in ESTADOS else "desconocido",
        "zona": zona,
        "acciones": verbs,
    }
    if luz is not None:
        obj["luz"] = bool(luz)
    # M6 fields: the engine accepts each ONLY on its own type (``url`` on documento, ``grupo`` on habilidad,
    # ``tablero`` on tablero) and rejects the whole thing otherwise, so they are added only when given.
    if url is not None:
        obj["url"] = url
    if grupo is not None:
        obj["grupo"] = cut_bytes(grupo, MAX_GROUP).strip()
    if descripcion is not None:
        obj["descripcion"] = cut_bytes(descripcion, MAX_DESCRIPTION)
    if tablero is not None:
        obj["tablero"] = tablero
    return obj


def chunk_objects(objs: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split a full snapshot into frames that fit ``MAX_FRAME_BYTES`` and the per-batch cap."""
    batches: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    size = 200  # envelope slack
    for o in objs[: MAX_OBJECTS_PER_BATCH * 5]:
        n = len(json.dumps(o, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
        if cur and (size + n > MAX_FRAME_BYTES - 512 or len(cur) >= MAX_OBJECTS_PER_BATCH):
            batches.append(cur)
            cur, size = [], 200
        cur.append(o)
        size += n
    if cur or not batches:
        batches.append(cur)
    return batches


def full_snapshot_frames(objs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The frames that put a COMPLETE snapshot in the world.

    ``lote`` REPLACES everything the source had published, so only the first frame may be a ``lote``; a second one would wipe the first.
    The rest of a snapshot too big for one 16 KB frame goes as ``alta`` frames (the engine's per-zone cap still applies).
    """
    batches = chunk_objects(objs)
    frames: list[dict[str, Any]] = [{"t": "lote", "objetos": batches[0]}]
    frames += [{"t": "alta", "objeto": o} for batch in batches[1:] for o in batch]
    return frames
