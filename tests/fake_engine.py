"""A FAKE Numinia Engine "enlace-espejo" server for tests.

Speaks the protocol of A0 §3 / ``hermes_palace_lib.protocol`` exactly as the engine side is meant
to: single-use nonce challenge, HMAC check, ``Origin`` rejection, 16 KB frames, ``hola`` with
``funciones`` (a list of what is ON), then collects every frame the plugin sends and can push ``peticion`` frames.

It is not the engine and proves nothing about the engine's code; it proves the plugin side of
the contract.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any

import websockets

from hermes_palace_lib import protocol as P


class FakeEngine:
    def __init__(
        self,
        secret: str = "s3cret-link-key-0123456789",
        funciones: dict[str, bool] | None = None,
        fixed_port: int = 0,
        rate: float = 0.0,
        burst: float = 40.0,
        zonas: list[dict[str, Any]] | None = None,
    ) -> None:
        self.zonas = zonas if zonas is not None else []  # what `hola` announces; [] = the old engines that announced nothing
        self.rate, self.burst = rate, burst  # token bucket like the real engine (RITMO 20/s, RAFAGA 40); rate 0 = unlimited
        self.world: dict[
            str, dict[str, Any]
        ] = {}  # what the source has put in the world, with the engine's lote/alta/baja semantics
        self.fixed_port = fixed_port
        self.secret = secret
        self.funciones = funciones if funciones is not None else {"salud": True, "cron": True, "sesiones": True}
        self.frames: list[dict[str, Any]] = []  # everything the plugin sent after the handshake
        self.hellos: list[dict[str, Any]] = []
        self.refused: list[str] = []
        self.used_nonces: set[str] = set()
        self.connections = 0
        self.origins_seen: list[str | None] = []
        self._server: Any = None
        self._current: Any = None
        self.port = 0
        self._cond = asyncio.Condition()
        self._connected = asyncio.Event()

    async def start(self) -> str:
        self._server = await websockets.serve(
            self._handler, "127.0.0.1", self.fixed_port, max_size=P.MAX_FRAME_BYTES, process_request=self._reject_origin
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self.url

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def _reject_origin(self, connection: Any, request: Any):
        origin = request.headers.get("Origin")
        self.origins_seen.append(origin)
        if origin is not None:
            self.refused.append("origin")
            return connection.respond(403, "origin not allowed\n")
        return None

    async def _handler(self, ws: Any) -> None:
        self.connections += 1
        if self._current is not None:
            self.refused.append("ocupado")
            await ws.close(4409, "ya hay una fuente conectada")
            return
        nonce = secrets.token_hex(32)
        self.used_nonces.add(nonce)
        await ws.send(json.dumps({"v": 1, "t": "reto", "nonce": nonce}))
        try:
            hello = P.decode(await asyncio.wait_for(ws.recv(), timeout=5))
        except Exception:
            await ws.close()
            return
        self.hellos.append(hello)
        if hello.get("t") != "hola" or not P.verify_signature(self.secret, nonce, hello.get("firma", "")):
            self.refused.append("firma")
            await ws.close(4401, "firma no válida")
            return
        await ws.send(
            json.dumps(
                {
                    "v": 1,
                    "t": "hola",
                    "funciones": [k for k, v in self.funciones.items() if v],
                    "zonas": self.zonas,
                    "acciones": {"cron.pausar": "cron", "cron.reanudar": "cron"},
                }
            )
        )
        self._current = ws
        self._connected.set()
        try:
            tokens, last = self.burst, time.monotonic()
            async for raw in ws:
                if self.rate:
                    now = time.monotonic()
                    tokens = min(self.burst, tokens + (now - last) * self.rate)
                    last = now
                    if tokens < 1:
                        self.refused.append("ritmo")
                        await ws.close(4429, "demasiado rápido")
                        return
                    tokens -= 1
                try:
                    msg = P.decode(raw)
                except P.ProtocolError:
                    self.refused.append("frame")
                    continue
                self._apply(msg)
                async with self._cond:
                    self.frames.append(msg)
                    self._cond.notify_all()
        except websockets.ConnectionClosed:
            pass
        finally:
            if self._current is ws:
                self._current = None
                self._connected.clear()

    def _apply(self, msg: dict[str, Any]) -> None:
        """The engine's semantics: `lote` REPLACES everything the source had published; alta/cambio put; baja removes."""
        t = msg.get("t")
        if t == "lote":
            self.world = {f"{o['tipo']}/{o['id']}": o for o in msg.get("objetos", [])}
        elif t in ("alta", "cambio"):
            o = msg["objeto"]
            self.world[f"{o['tipo']}/{o['id']}"] = o
        elif t == "baja":
            o = msg["objeto"]
            self.world.pop(f"{o['tipo']}/{o['id']}", None)

    # ---- test helpers -----------------------------------------------------
    async def wait_for(self, pred, timeout: float = 5.0) -> dict[str, Any]:
        async def _w():
            async with self._cond:
                while True:
                    for f in self.frames:
                        if pred(f):
                            return f
                    await self._cond.wait()

        return await asyncio.wait_for(_w(), timeout)

    async def wait_connected(self, timeout: float = 5.0) -> None:
        """Block until a plugin has completed the handshake (event, not polling)."""
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except TimeoutError:
            raise TimeoutError("plugin never connected") from None

    async def send(self, msg: dict[str, Any]) -> None:
        await self._current.send(json.dumps({"v": 1, **msg}))

    async def drop(self) -> None:
        """Close the current connection (simulates the engine restarting)."""
        if self._current is not None:
            await self._current.close()

    def of_type(self, t: str) -> list[dict[str, Any]]:
        return [f for f in self.frames if f.get("t") == t]
