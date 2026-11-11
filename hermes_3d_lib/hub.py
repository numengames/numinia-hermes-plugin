# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""The bridge ("hub"): ONE loopback process per Hermes profile.

Two faces, one process:

* **local face** — a tiny HTTP server bound to ``127.0.0.1`` (never anything else) that the
  hooks of the CLI, gateway and cron processes push to. Every request needs the per-start token
  (``Authorization: Bearer``, constant-time compare). Requests with an ``Origin`` header (a web
  page) are rejected, so a browser tab cannot talk to it. Bodies are capped.
* **engine face** — the single WebSocket *client* to the engine's local link (A0 §3.2: the
  plugin opens the connection, the engine never connects to Hermes). HMAC handshake, reconnect
  with growing back-off (cap 30 s), a full ``lote`` after every (re)connect.

The hub never runs a command by itself. A world ``peticion`` is handed to ``on_request``; in the
plugin that handler only *queues* it for the approval path (see ``actions.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from . import config
from . import protocol as P
from . import settings as S
from .logsafe import describe, redact
from .worldtext import say
from .zones import zone_for

logger = logging.getLogger("hermes_3d.hub")

MAX_BODY = 32 * 1024
MAX_HEADER_BYTES = 8 * 1024  # request line + headers together
MAX_HEADERS = 40
MAX_CONNECTIONS = 32  # concurrent local connections; the 33rd gets 503
RATE_PER_SECOND = 50.0  # requests per second one client may sustain on the loopback face
RATE_BURST = 100  # and the burst it may spend at once; beyond that it gets 429
MAX_TRACKED_CLIENTS = 64
READ_DEADLINE = 10.0  # seconds for the WHOLE request (head + body): a drip-feed cannot outlive it
ALLOWED_ORIGINS: frozenset[str] = frozenset()  # a browser page has no business here: every Origin is refused
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
REASONS = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    429: "Too Many Requests",
    413: "Content Too Large",
    414: "URI Too Long",
    415: "Unsupported Media Type",
    421: "Misdirected Request",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    503: "Service Unavailable",
}
SEND_RATE = 15.0  # frames per second we allow ourselves: the engine closes the link above 20/s sustained (RITMO)
SEND_BURST = 30.0  # ... and above a burst of 40 (RAFAGA)
BACKOFF_START = 1.0
BACKOFF_CAP = 30.0
REQUEST_TTL = 30.0


CLOSE_REASONS = {
    4401: "the engine refused the link key (wrong or stale signature)",
    4403: "the engine refused the connection (not loopback or has an Origin header)",
    4409: "another source is already connected to the engine link",
    4429: "the engine closed the link: too many messages per second",
    4408: "the engine closed the link: no signal from us for too long",
    4400: "the engine closed the link: it did not understand a message",
}


def _explain(exc: BaseException) -> str:
    """One readable line for ``last_error`` (the engine closes with a code instead of a frame)."""
    rcvd = getattr(exc, "rcvd", None)
    code = getattr(rcvd, "code", None)
    if code in CLOSE_REASONS:
        return CLOSE_REASONS[code]
    return f"{type(exc).__name__}: {str(exc)[:160]}"


def _zones(raw: Any) -> dict[str, frozenset[str]]:
    out: dict[str, frozenset[str]] = {}
    if isinstance(raw, list):
        for z in raw[:64]:
            if isinstance(z, dict) and isinstance(z.get("id"), str) and isinstance(z.get("tipos"), list):
                out[z["id"]] = frozenset(str(t) for t in z["tipos"][:16])
    return out


def _guide_on(raw: Any) -> bool:
    """``guia`` is the world page's own switch; the engine does not announce it to a source, but if it does, pass it through."""
    return isinstance(raw, list) and "guia" in raw


def _functions_on(raw: Any) -> dict[str, bool]:
    """The engine lists the functions that are ON (a list of names); anything not listed is off."""
    names = {str(n) for n in raw} if isinstance(raw, list) else set()
    return {n: (config.ENGINE_FUNCTION.get(n, n) in names) for n in S.FUNCTIONS}


MAX_ACK_ITEMS = 20  # the engine sends at most 20 and a count of the rest (``mas``)


def _num(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def parse_ack(msg: dict[str, Any]) -> dict[str, Any]:
    """Digest an engine ``ack`` (en='lote'): which objects a lote left out and why, bounded and redacted.

    The engine sends it only when something was dropped (unknown zone, zone without that type); engines that
    never send it leave the status at zero. Text is cut, stripped of control characters and passed through the
    secret-shape redaction because it ends up in the log and in ``hermes 3d status``.
    """
    raw = msg.get("descartadas")
    items: list[dict[str, str]] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        items.append(
            {
                "tipo": redact(P.cut_bytes(entry.get("tipo", ""), 40)),
                "id": redact(P.cut_bytes(entry.get("id", ""), 64)),
                "motivo": redact(P.cut_bytes(entry.get("motivo", ""), 120)),
            }
        )
        if len(items) >= MAX_ACK_ITEMS:
            break
    more = _num(msg.get("mas"))
    return {
        "puestas": _num(msg.get("puestas")),
        "quitadas": _num(msg.get("quitadas")),
        "mas": more,
        "total": len(items) + more,
        "descartadas": items,
    }


class _ClientLimiter:
    """Per-client token bucket (client = peer address) for the local HTTP face; bounded memory."""

    def __init__(self) -> None:
        self.buckets: dict[str, tuple[float, float]] = {}  # client -> (tokens, last seen)

    def allow(self, client: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        tokens, last = self.buckets.pop(client, (float(RATE_BURST), now))
        tokens = min(float(RATE_BURST), tokens + max(0.0, now - last) * RATE_PER_SECOND)
        ok = tokens >= 1.0
        self.buckets[client] = (tokens - 1.0 if ok else tokens, now)  # re-inserted last: dicts keep insertion order
        while len(self.buckets) > MAX_TRACKED_CLIENTS:
            self.buckets.pop(next(iter(self.buckets)))  # forget the least recently seen
        return ok


class _Bucket:
    """Token bucket for frames sent to the engine (module constants read at use, so tests can shrink them)."""

    def __init__(self) -> None:
        self.tokens = SEND_BURST
        self.last = asyncio.get_running_loop().time()

    async def take(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            now = loop.time()
            self.tokens = min(SEND_BURST, self.tokens + (now - self.last) * SEND_RATE)
            self.last = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            await asyncio.sleep((1 - self.tokens) / SEND_RATE)


class Hub:
    def __init__(
        self,
        *,
        token: str,
        engine_url: str | None,
        link_key: str | None,
        hermes_version: str = "",
        switches: dict[str, bool] | None = None,
        on_request: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        on_local_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        snapshot: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
        on_route: Callable[[str, str, bytes], Awaitable[tuple[int, dict[str, Any]]]] | None = None,
        on_functions: Callable[[], Awaitable[None]] | None = None,
        port: int = 0,
        read_only: bool = False,
        demo: bool = False,
    ) -> None:
        self.read_only = read_only
        self.demo = demo
        self.on_route = on_route
        self.on_functions = on_functions
        self.token = token
        self.engine_url = engine_url
        self.link_key = link_key
        self.hermes_version = hermes_version
        self.switches = dict(switches or {"salud": True})
        self.engine_functions: dict[str, bool] = {}
        self.guide = False  # the engine listed ``guia`` (a page-only switch; passed through, never acted on)
        self.engine_zones: dict[str, frozenset[str]] = {}  # zone id -> the types it admits, from the engine's `hola`
        self.skipped_no_zone = (
            0  # objects not sent because the world has no zone for them (the engine would refuse the whole lote)
        )
        self.on_request = on_request
        self.on_local_event = on_local_event
        self.snapshot = snapshot
        self.port = port
        self.connected = False
        self.last_error = ""
        self.dropped: dict[str, Any] = parse_ack({})
        self.sent = 0
        self.received = 0
        self.rejected_local = 0
        self.open_connections = 0
        self._limiter = _ClientLimiter()
        self._ws: Any = None
        self._server: asyncio.AbstractServer | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._answers: set[asyncio.Task[None]] = set()
        self._stop = asyncio.Event()
        self._outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self.connected_event = asyncio.Event()

    # ---- lifecycle --------------------------------------------------------
    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle_http, P_HOST, self.port, limit=MAX_HEADER_BYTES * 2)
        self.port = self._server.sockets[0].getsockname()[1]
        if self.engine_url and self.link_key:
            self._tasks.append(asyncio.create_task(self._engine_loop(), name="hermes-3d-engine"))
        else:
            self.last_error = "no engine link configured"
        return self.port

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
            except (OSError, RuntimeError, ValueError, P.ProtocolError):
                logger.debug("engine task ended with an error", exc_info=True)
        if self._ws is not None:
            try:
                await self._ws.close()
            except (OSError, RuntimeError):
                logger.debug("closing the engine socket failed", exc_info=True)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    # ---- helpers ----------------------------------------------------------
    def function_on(self, name: str) -> bool:
        """A function runs only if the local switch AND the world's switch are both on."""
        local = self.switches.get(name, False)
        remote = self.engine_functions.get(name, True) if self.engine_functions else True
        return bool(local and remote)

    def publish(self, msg: dict[str, Any]) -> bool:
        """Queue a message for the engine (non-blocking). False when the queue is full."""
        msg = self._read_only_view(msg)
        if msg.get("t") in ("alta", "cambio") and isinstance(msg.get("objeto"), dict):
            msg = {**msg, "objeto": self.retarget(msg["objeto"])}
        if msg.get("t") in ("alta", "cambio") and not self.placeable(msg.get("objeto")):
            self.skipped_no_zone += 1
            return True
        if msg.get("t") == "lote" and isinstance(msg.get("objetos"), list):
            msg = {**msg, "objetos": self._filter(msg["objetos"])}
        try:
            self._outbox.put_nowait(msg)
            return True
        except asyncio.QueueFull:
            self.last_error = "outbox full; message dropped"
            return False

    def _read_only_view(self, msg: dict[str, Any]) -> dict[str, Any]:
        """In read-only mode nothing that goes to the world offers a verb: the world shows, it never asks."""
        if not self.read_only:
            return msg
        if isinstance(msg.get("objeto"), dict) and msg["objeto"].get("acciones"):
            return {**msg, "objeto": {**msg["objeto"], "acciones": []}}
        if isinstance(msg.get("objetos"), list):
            return {
                **msg,
                "objetos": [{**o, "acciones": []} if isinstance(o, dict) and o.get("acciones") else o for o in msg["objetos"]],
            }
        return msg

    def status(self) -> dict[str, Any]:
        return {
            "read_only": self.read_only,
            "demo": self.demo,
            "connected": self.connected,
            "port": self.port,
            "sent": self.sent,
            "received": self.received,
            "rejected_local": self.rejected_local,
            "switches": self.switches,
            "engine_functions": self.engine_functions,
            "guide": self.guide,
            "skipped_no_zone": self.skipped_no_zone,
            "last_error": self.last_error,
            "dropped": {
                "total": self.dropped["total"],
                "puestas": self.dropped["puestas"],
                "quitadas": self.dropped["quitadas"],
                "items": self.dropped["descartadas"],
            },
            "engine_url_set": bool(self.engine_url),
        }

    # ---- engine face ------------------------------------------------------
    async def _engine_loop(self) -> None:
        import websockets  # core dependency of Hermes; imported lazily so import of the plugin never needs it

        delay = BACKOFF_START
        url = str(self.engine_url)
        while not self._stop.is_set():
            try:
                # No Origin header is sent (websockets sends none unless asked): the engine rejects Origin.
                async with websockets.connect(url, max_size=P.MAX_FRAME_BYTES, open_timeout=5, ping_interval=20) as ws:
                    self._ws = ws
                    await self._handshake(ws)
                    self.connected = True
                    self.connected_event.set()
                    self.last_error = ""
                    self.dropped = parse_ack({})  # a new connection starts with a fresh lote
                    delay = BACKOFF_START
                    await self._send_full_snapshot(ws)
                    await self._pump(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = _explain(exc)
                logger.info("engine link down: %s", self.last_error, exc_info=logger.isEnabledFor(logging.DEBUG))
            finally:
                self.connected = False
                self.connected_event.clear()
                self._ws = None
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            delay = min(delay * 2, BACKOFF_CAP)

    async def _handshake(self, ws: Any) -> None:
        first = P.decode(await asyncio.wait_for(ws.recv(), timeout=5))
        if first.get("t") != "reto" or not isinstance(first.get("nonce"), str):
            raise P.ProtocolError(f"expected reto, got {first.get('t')!r}")
        await ws.send(
            P.encode(
                {
                    "t": "hola",
                    "plugin": "hermes-3d",
                    "hermes_version": self.hermes_version,
                    "firma": P.sign_nonce(self.link_key or "", first["nonce"]),
                }
            )
        )
        reply = P.decode(await asyncio.wait_for(ws.recv(), timeout=5))
        if reply.get("t") == "error":
            raise P.ProtocolError(f"engine refused the link: {str(reply.get('motivo', ''))[:80]}")
        if reply.get("t") != "hola":
            raise P.ProtocolError(f"expected hola, got {reply.get('t')!r}")
        self.engine_functions = _functions_on(reply.get("funciones"))
        self.guide = _guide_on(reply.get("funciones"))
        self.engine_zones = _zones(reply.get("zonas"))

    def placeable(self, obj: Any) -> bool:
        """Can the world place this object? The engine refuses a WHOLE lote for one object in a zone it lacks, so ask first.
        An engine that announced no zones is trusted (nothing to check against)."""
        if not self.engine_zones or not isinstance(obj, dict):
            return True
        zone = self.engine_zones.get(str(obj.get("zona", "")))
        return zone is not None and str(obj.get("tipo", "")) in zone

    def zone_for(self, tipo: str) -> str:
        """Zone id for a type: what the engine announced in ``hola``, else the documented fallback table (zones.py)."""
        return zone_for(tipo, self.engine_zones)

    def retarget(self, obj: Any) -> Any:
        """The same object in a zone that admits its type, when the engine announced one (never touches a good zone).

        The engine's zone ids are the truth: the plugin's own table is only a fallback, so an owner who moved ``perfil``
        from z6 to z5 still sees agents. If no announced zone admits the type the object is returned unchanged
        (``placeable`` then leaves it out and counts it)."""
        if not self.engine_zones or not isinstance(obj, dict) or self.placeable(obj):
            return obj
        zid = zone_for(str(obj.get("tipo", "")), self.engine_zones)
        if zid and zid in self.engine_zones and zid != obj.get("zona"):
            return {**obj, "zona": zid}
        return obj

    def _filter(self, objs: list[Any]) -> list[Any]:
        objs = [self.retarget(o) for o in objs]
        keep = [o for o in objs if self.placeable(o)]
        self.skipped_no_zone += len(objs) - len(keep)
        return keep

    async def _send_full_snapshot(self, ws: Any) -> None:
        objs: list[dict[str, Any]] = []
        if self.snapshot is not None:
            objs = await self.snapshot()
        objs = self._filter(objs)
        bucket = _Bucket()
        for frame in P.full_snapshot_frames(objs):
            await bucket.take()
            await ws.send(P.encode(self._read_only_view(frame)))
            self.sent += 1

    async def _pump(self, ws: Any) -> None:
        reader = asyncio.create_task(self._read_loop(ws))
        writer = asyncio.create_task(self._write_loop(ws))
        done, pending = await asyncio.wait({reader, writer}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:
            exc = t.exception()
            if exc:
                raise exc

    async def _write_loop(self, ws: Any) -> None:
        bucket = _Bucket()
        while True:
            try:
                msg = await asyncio.wait_for(self._outbox.get(), timeout=10)
            except TimeoutError:
                await ws.send(P.encode({"t": "pulso"}))
                continue
            try:
                await bucket.take()
                await ws.send(P.encode(msg))
                self.sent += 1
            except P.ProtocolError as exc:
                self.last_error = str(exc)

    async def _read_loop(self, ws: Any) -> None:
        async for raw in ws:
            try:
                msg = P.decode(raw)
            except P.ProtocolError as exc:
                self.last_error = f"bad frame from engine: {exc}"
                continue
            self.received += 1
            kind = msg["t"]
            if kind == "pulso":
                continue
            if kind == "error":
                self.last_error = f"engine: {str(msg.get('en', ''))[:24]}: {str(msg.get('motivo', ''))[:120]}"
            elif kind == "ack":
                if msg.get("en") == "lote":
                    self.dropped = parse_ack(msg)
                    if self.dropped["total"]:
                        logger.warning(
                            "engine left out %d object(s) of the lote: %s",
                            self.dropped["total"],
                            "; ".join(f"{d['tipo']}/{d['id']}: {d['motivo']}" for d in self.dropped["descartadas"][:5]),
                        )
            elif kind == "funciones":
                # a person flipped a switch inside the world: this is the NEW full list of what is on (what is off is never asked for)
                self.engine_functions = _functions_on(msg.get("funciones"))
                self.guide = _guide_on(msg.get("funciones"))
                if self.on_functions is not None:
                    task = asyncio.create_task(self._functions_changed(), name="hermes-3d-functions")
                    self._answers.add(task)
                    task.add_done_callback(self._answers.discard)
            elif kind == "peticion":
                task = asyncio.create_task(self._answer_request(msg), name="hermes-3d-answer")
                self._answers.add(task)  # keep a reference: the loop only holds weak ones
                task.add_done_callback(self._answers.discard)

    async def _functions_changed(self) -> None:
        try:
            if self.on_functions is not None:
                await self.on_functions()
        except Exception as exc:
            self.last_error = f"functions resync: {describe(exc)}"
            logger.warning("resync after a world switch failed", exc_info=True)

    async def answer(self, msg: dict[str, Any]) -> dict[str, Any]:
        """The verdict on one world request (read-only, verb check, the approval queue). Same path for the engine link
        (``peticion`` frames) and for the Desktop page (``/action/submit``)."""
        verb = str(msg.get("verbo", ""))
        if self.read_only:  # whatever the verb: nothing is queued, nothing reaches Hermes
            return say("read_only")
        if verb not in P.KNOWN_VERBS:
            return say("unknown_verb")
        if self.on_request is None:
            return say("no_actions")
        try:
            return await asyncio.wait_for(self.on_request(msg), timeout=REQUEST_TTL - 2)
        except TimeoutError:
            return say("timeout")
        except Exception as exc:
            logger.warning("handling a world request failed", exc_info=True)
            return say("error", detail=type(exc).__name__)

    async def _answer_request(self, msg: dict[str, Any]) -> None:
        req_id = str(msg.get("id", ""))[:80]
        res = await self.answer(msg)
        self.publish({"t": "resultado", "peticion": req_id, "ok": bool(res.get("ok")), "texto": str(res.get("texto", ""))[:200]})

    # ---- local face -------------------------------------------------------
    def _host_ok(self, host: str) -> bool:
        """Exactly ``127.0.0.1`` or ``localhost``, optionally with THIS server's port. Defeats DNS rebinding."""
        name, sep, port = host.partition(":")
        return name.lower() in LOOPBACK_HOSTS and (not sep or port == str(self.port))

    @staticmethod
    def _token_ok(supplied: str, expected: str) -> bool:
        """Constant-time comparison of fixed-length digests (hides even the length of the token)."""
        return hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(), hashlib.sha256(expected.encode()).digest())

    async def _read_request(self, reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes] | tuple[int, str]:
        """Parse one request within the overall deadline. Returns (method, path, headers, body) or (status, why)."""
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.LimitOverrunError, ValueError):
            return 431, "headers too large"
        except asyncio.IncompleteReadError:
            return 400, "incomplete request"
        if len(head) > MAX_HEADER_BYTES:
            return 431, "headers too large"
        lines = head.decode("latin-1").split("\r\n")[:-2]
        parts = lines[0].split(" ")
        if len(parts) != 3 or parts[2] != "HTTP/1.1":
            return 400, "bad request line"
        method, path, _ = parts
        if len(path) > 512:
            return 414, "path too long"
        if method not in ("GET", "POST"):
            return 405, "method not allowed"
        if len(lines) - 1 > MAX_HEADERS:
            return 431, "too many headers"
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            name = name.strip().lower()
            if not sep or not name or name != name.strip() or " " in name:
                return 400, "bad header"
            if name in headers and name in (
                "host",
                "content-length",
                "authorization",
                "content-type",
                "transfer-encoding",
                "origin",
                "sec-fetch-site",
            ):
                return 400, f"duplicate {name}"
            headers[name] = value.strip()
        if "transfer-encoding" in headers:
            return 400, "transfer-encoding is not accepted"
        if "host" not in headers:
            return 400, "no Host"
        if not self._host_ok(headers["host"]):
            return 421, "host not allowed"
        if "origin" in headers and headers["origin"] not in ALLOWED_ORIGINS:
            return 403, "origin not allowed"
        if headers.get("sec-fetch-site", "none") != "none":
            return 403, "browser-initiated request refused"
        supplied = headers.get("authorization", "")
        supplied = supplied[7:] if supplied[:7].lower() == "bearer " else ""
        if not self._token_ok(supplied, self.token):
            return 401, "unauthorized"
        raw_len = headers.get("content-length", "0")
        if not (raw_len.isascii() and raw_len.isdigit()) or len(raw_len) > 9:
            return (413, "body too large") if raw_len.isdigit() else (400, "bad content-length")
        length = int(raw_len)
        if length > MAX_BODY:
            return 413, "body too large"
        if method == "POST" and length and headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return 415, "content-type must be application/json"
        try:
            body = await reader.readexactly(length) if length else b""
        except asyncio.IncompleteReadError:
            return 400, "incomplete body"
        return method, path, headers, body

    async def _handle_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.open_connections >= MAX_CONNECTIONS:
            self.rejected_local += 1
            await self._respond(reader, writer, 503, {"error": "too many connections"})
            return
        peer = writer.get_extra_info("peername")
        if not self._limiter.allow(str(peer[0]) if peer else "?"):
            self.rejected_local += 1
            await self._respond(reader, writer, 429, {"error": "too many requests"}, retry_after=1)
            return
        self.open_connections += 1
        status, body = 400, {"error": "bad request"}
        try:
            try:
                parsed = await asyncio.wait_for(self._read_request(reader), READ_DEADLINE)
            except TimeoutError:
                status, body = 408, {"error": "request timeout"}
            else:
                if isinstance(parsed[0], int):
                    status, body = parsed[0], {"error": parsed[1]}
                    if status in (401, 403, 421):
                        self.rejected_local += 1
                else:
                    method, path, _headers, raw = parsed
                    status, body = await self._route(str(method), str(path), raw)
        except ConnectionError:
            return
        except Exception as exc:
            logger.warning("hub http error", exc_info=True)
            self.last_error = f"http handler: {describe(exc)}"
            status, body = 500, {"error": "internal"}
        finally:
            self.open_connections -= 1
        await self._respond(reader, writer, status, body)

    @staticmethod
    async def _respond(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        status: int,
        body: dict[str, Any],
        retry_after: int = 0,
    ) -> None:
        payload = json.dumps(body).encode()
        extra = f"Retry-After: {retry_after}\r\n" if retry_after else ""
        writer.write(
            f"HTTP/1.1 {status} {REASONS.get(status, 'Error')}\r\nContent-Type: application/json\r\n"
            f"{extra}Content-Length: {len(payload)}\r\nConnection: close\r\nCache-Control: no-store\r\n"
            "X-Content-Type-Options: nosniff\r\n\r\n".encode()
            + payload
        )
        try:
            await writer.drain()
            # Read (and drop) what the client is still sending: closing with unread bytes makes the kernel
            # reset the connection and the client would lose the status line.
            await asyncio.wait_for(reader.read(MAX_BODY * 2), 0.2)
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    async def _route(self, method: str, path: str, raw: bytes) -> tuple[int, dict[str, Any]]:
        if method == "GET" and path == "/status":
            return 200, self.status()
        if method == "POST" and path == "/event":
            try:
                ev = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                return 400, {"error": "not JSON"}
            if not isinstance(ev, dict) or not isinstance(ev.get("kind"), str):
                return 400, {"error": "event without kind"}
            if self.on_local_event:
                await self.on_local_event(ev)
            return 200, {"ok": True}
        if method == "POST" and path == "/stop":
            self._stop.set()
            return 200, {"ok": True}
        if self.on_route is not None and (path.startswith("/action/") or path in ("/scan", "/scan/refresh", "/snapshot")):
            return await self.on_route(method, path, raw)
        return 404, {"error": "not found"}

    async def wait_stopped(self) -> None:
        await self._stop.wait()


P_HOST = "127.0.0.1"  # literal on purpose: the bind address is not configurable
