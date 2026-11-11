"""The bridge process: ``python -m hermes_3d_lib.bridge_main`` (one per Hermes profile).

Started by ``hermes 3d up`` (or auto-started on demand). It owns:
  * the loopback face (Hub) that hooks and the Desktop backend talk to,
  * the single WS link to the engine,
  * the cron poller (``hermes cron list --all``), when ``funciones.cron`` is on,
  * the session book, when ``funciones.sesiones`` is on,
  * the action queue for world->Hermes ``cron.pausar/reanudar``.

Why a separate process: Hermes loads plugins in the CLI, the gateway and each cron run, and hooks
fire per process. One bridge per profile keeps ONE socket to the engine and ONE source of truth.
It exits on ``POST /stop``, SIGTERM, or when its parent runtime file is replaced.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC
from pathlib import Path
from typing import Any, ClassVar

from . import (
    agentes,
    config,
    cronsync,
    documentos,
    gasto,
    habilidades,
    linkkey,
    logsafe,
    plazo,
    proyectos,
    scan,
    supervisor,
    tablero,
)
from . import hermes_api as hermes_api_mod
from . import protocol as P
from . import settings as S
from .actions import ActionQueue
from .crontable import parse_cron_list, read_jobs_file
from .hub import Hub
from .sessions import SessionBook
from .worldtext import say

logger = logging.getLogger("hermes_3d.bridge")
CRON_ARGS = ("cron", "list", "--all")
CRON_TIMEOUT = 20


def read_switches_from_yaml(home: Path) -> dict[str, bool]:
    """Switches without importing Hermes: the same ``config.yaml`` section ``ctx.get_config`` reads."""
    return S.switches(S.yaml_getter(home))


def run_cron_list(timeout: float = CRON_TIMEOUT) -> str | None:
    """Text of ``hermes cron list --all`` or None on failure. No shell, fixed argv, our own env."""
    exe = shutil.which("hermes")
    if exe is None:
        return None
    try:
        out = subprocess.run(
            [exe, *CRON_ARGS], capture_output=True, text=True, timeout=timeout, check=False, env={**os.environ, "NO_COLOR": "1"}
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


class Bridge:
    def __init__(
        self,
        home: Path,
        *,
        cron_reader: Callable[[], str | None] = run_cron_list,
        engine_url: str | None = None,
        link_key: str | None = None,
        poll_seconds: float | None = None,
        scan_root: Path | None = None,
        scan_days: int = scan.DEFAULT_DAYS,
        scan_every: float = 300.0,
        allow_direct_db: bool = False,
        hermes_api: Any = None,
        page_url: str = "",
        read_only: bool = False,
        demo: bool = False,
        docs_port: int = S.DEFAULT_DOCS_PORT,
    ) -> None:
        self.home = home
        self.docs_port = docs_port
        self.demo = demo
        self.read_only = read_only or demo  # the demo can never write, whatever else is asked
        self.page_url = page_url
        self.cron_reader = cron_reader
        self.cron_source: str = ""  # "file" (cron/jobs.json) or "cli" (hermes cron list), as of the last poll
        self.switches = read_switches_from_yaml(home)
        if demo:  # look at your own Hermes: health, agents, spend and cron on; session titles stay off
            self.switches.update({"salud": True, "cron": True, "agentes": True, "gasto": True})
        self.poll = poll_seconds if poll_seconds is not None else S.POLL_SECONDS_DEFAULT
        self.cron_snapshot: dict[str, dict[str, Any]] | None = None
        self.known_jobs: set[str] = set()
        self.sessions = SessionBook()
        self.queue = ActionQueue(
            enabled=lambda: self.hub.function_on("cron"),
            known_job=lambda j: j in self.known_jobs,
            read_only=lambda: self.read_only,
        )
        self.hub = Hub(
            token=config.new_token(),
            engine_url=engine_url,
            link_key=link_key,
            switches=self.switches,
            on_request=self.queue.submit,
            on_local_event=self.on_local_event,
            snapshot=self.full_snapshot,
            on_route=self.on_action_route,
            on_functions=self.on_world_functions,
            read_only=self.read_only,
            demo=demo,
        )
        self._poller: asyncio.Task[None] | None = None
        self._scanner: asyncio.Task[None] | None = None
        self._clock: asyncio.Task[None] | None = None
        self._claimed: dict[str, Any] = {}
        self.poll_cycles = 0  # completed poll rounds; tests wait on it instead of sleeping
        # scan (read-only snapshot of the user's Hermes) and what was last published from it
        self.scan: dict[str, Any] | None = None
        self.last_scan_at: float = 0.0
        self.scan_root: Path | None = scan_root
        self.scan_days: int = scan_days
        self.scan_every: float = scan_every
        self.hermes_api: Any = hermes_api  # first layer for kanban/sessions (module hermes_api); None = off (tests, demo)
        self._seen_session_ids: list[str] = []
        self.allow_direct_db: bool = allow_direct_db  # setting allow_direct_db_read; off = no database is ever opened
        self._scan_cache: dict[str, Any] = {}  # database sections reused while their files are unchanged
        self._mirror_published: dict[str, dict[str, Any]] | None = None

    async def on_action_route(self, method: str, path: str, raw: bytes) -> tuple[int, dict[str, Any]]:
        """Loopback routes used by the Hermes-side tool (`hermes3d_cron_action`) and the approval hook.

        GET  /action/pending          -> what the world asked and nobody has run yet
        POST /action/claim   {job_id, verb}            -> take the oldest matching request (after approval)
        POST /action/resolve {token, ok, texto}        -> answer the world with the real outcome
        """
        try:
            body = json.loads(raw.decode("utf-8") or "{}") if raw else {}
        except ValueError:
            return 400, {"error": "not JSON"}
        if method == "GET" and path == "/scan":
            return 200, {"scan": self.scan, "summary": scan.summary(self.scan) if self.scan else None}
        if method == "POST" and path == "/scan/refresh":
            snap = await self.run_scan()
            return 200, {"summary": scan.summary(snap)}
        if method == "GET" and path == "/snapshot":
            return 200, await self.world_snapshot()
        if (
            method == "POST" and path == "/action/submit"
        ):  # a request from the Desktop page: the same verdict as a link `peticion`
            res = await self.hub.answer(body if isinstance(body, dict) else {})
            return 200, {k: res[k] for k in ("ok", "code", "text", "texto") if k in res}
        if method == "GET" and path == "/action/pending":
            return 200, {"pending": self.queue.pending()}
        if self.read_only and path.startswith("/action/") and method == "POST":
            return 403, {"error": "read-only", **{k: v for k, v in say("read_only").items() if k in ("text", "texto")}}
        if method == "POST" and path == "/action/claim":
            item = self.queue.claim(str(body.get("job_id", "")), str(body.get("verb", "")))
            if item is None:
                return 404, {"error": "nothing pending for that job and verb"}
            self._claimed[item.token] = item
            return 200, {"token": item.token, "job_id": item.job_id, "verb": item.verb}
        if method == "POST" and path == "/action/resolve":
            item = self._claimed.pop(str(body.get("token", "")), None)
            if item is None:
                return 404, {"error": "unknown token"}
            self.queue.resolve(item, {"ok": bool(body.get("ok")), "texto": str(body.get("texto", ""))[:200]})
            return 200, {"ok": True}
        return 404, {"error": "not found"}

    async def world_snapshot(self) -> dict[str, Any]:
        """What a world that has no link yet must be shown: the SAME objects the link's first ``lote`` carries (same serializers,
        same zone filter, same read-only view), cut into the embed message sizes: ``objetos`` for ``snapshot`` and ``extra`` as
        ``alta`` events for whatever does not fit one 16 KB message."""
        frames = [self.hub._read_only_view(f) for f in P.full_snapshot_frames(self.hub._filter(await self.full_snapshot()))]
        return {
            "objetos": frames[0]["objetos"],
            "extra": [f["objeto"] for f in frames[1:]],
            "funciones": [name for name in self.switches if self.hub.function_on(name)],
            "read_only": self.read_only,
        }

    async def full_snapshot(self) -> list[dict[str, Any]]:
        objs: list[dict[str, Any]] = []
        if self.hub.function_on("cron") and self.cron_snapshot:
            objs += list(self.cron_snapshot.values())
        if self.hub.function_on("sesiones"):
            objs += self.sessions.all()
        mirrors = self.mirror_objects()
        self._mirror_published = dict(mirrors)  # what the lote on (re)connect puts in the world
        objs += list(mirrors.values())
        return objs

    # ---- scan (HISTORIA-DE-USUARIO §4): the first thing the plugin does ---------------------------------
    def mirror_objects(self) -> dict[str, dict[str, Any]]:
        """Objects of the scan-backed mirrors whose switch (local AND world) is on. Sections the engine has no
        type for yet are still in ``self.scan`` but published as nothing."""
        out: dict[str, dict[str, Any]] = {}
        sections = (self.scan or {}).get("sections", {})
        if self.read_only and self.hub.function_on("salud"):
            out["salud/hermes"] = self.read_only_card()
        if self.hub.function_on("agentes"):
            out.update({f"agente/{k}": v for k, v in agentes.snapshot_from_scan(sections.get("agentes", {})).items()})
        if self.hub.function_on("gasto"):
            out.update({f"gasto/{k}": v for k, v in gasto.gasto_objects(sections.get("gasto", {})).items()})
        if self.hub.function_on("habilidades"):
            skills = habilidades.snapshot_from_scan(sections.get("skills", {}))
            out.update({f"habilidad/{k}": v for k, v in skills.items()})
        if self.hub.function_on("kanban"):
            out.update({f"tablero/{k}": v for k, v in tablero.snapshot_from_scan(sections.get("kanban", {})).items()})
        if self.hub.function_on("proyectos"):
            out.update({f"proyecto/{k}": v for k, v in proyectos.project_objects(sections.get("arbol", {})).items()})
        if self.hub.function_on("documentos"):
            out.update({f"documento/{k}": v for k, v in documentos.snapshot(self.docs_port).items()})
            out.update({f"documento/{k}": v for k, v in plazo.snapshot(self.now()).items()})  # C12: clock, requirements, links
        return out

    @staticmethod
    def read_only_card() -> dict[str, Any]:
        """The one world card that says the world is read-only (zone z0, type salud)."""
        return P.make_object(
            "salud",
            "hermes",
            say("read_only_card_title")["texto"],
            subtitulo=say("read_only_card_sub")["texto"],
            estado="activo",
            zona="z0",
            acciones=[],
        )

    SWITCH_SECTION: ClassVar[dict[str, str]] = {
        "agentes": "agentes",
        "gasto": "gasto",
        "habilidades": "skills",
        "kanban": "kanban",
        "proyectos": "arbol",
    }

    def scan_sections(self) -> tuple[str, ...]:
        """Only what a switch asks for: with just ``salud`` on, the scan reads nothing of the user's Hermes."""
        return tuple(sec for fn, sec in self.SWITCH_SECTION.items() if self.hub.switches.get(fn))

    async def run_scan(self) -> dict[str, Any]:
        """Scan the user's Hermes read-only (off the event loop), keep the snapshot and publish the mirrors as diffs."""
        days = int(self.scan_days)
        snap = await asyncio.to_thread(
            lambda: scan.scan_hermes(
                self.scan_root,
                days=days,
                only=self.scan_sections(),
                allow_direct_db=self.allow_direct_db,
                cache=self._scan_cache,
                api=self.hermes_api,
                session_ids=list(self._seen_session_ids),
            )
        )
        self.scan = snap
        self.last_scan_at = snap["at"]
        self.publish_mirrors()
        return snap

    def publish_mirrors(self) -> None:
        """alta/cambio/baja for agentes+gasto against what was last published (never ``lote``: a lote would
        replace every other type in the world). The first publish after (re)connect is done by ``full_snapshot``."""
        new = self.mirror_objects()
        old = self._mirror_published
        if old is None or not self.hub.connected:
            self._mirror_published = new if self.hub.connected else None
            return
        for key, obj in new.items():
            if key not in old:
                self.hub.publish({"t": "alta", "objeto": obj})
            elif old[key] != obj:
                self.hub.publish({"t": "cambio", "objeto": obj})
        for key, obj in old.items():
            if key not in new:
                self.hub.publish({"t": "baja", "objeto": {"tipo": obj["tipo"], "id": obj["id"]}})
        self._mirror_published = new

    @staticmethod
    def now() -> Any:
        """The current UTC time (a method so a test can pin the clock)."""
        from datetime import datetime

        return datetime.now(UTC)

    async def clock_loop(self) -> None:
        """C12: once a minute (on the minute) rebuild the mirrors; ``publish_mirrors`` sends only what changed (the clock card)."""
        while True:
            now = self.now()
            await asyncio.sleep(60.0 - (now.second + now.microsecond / 1e6) % 60.0 + 0.05)
            if not self.hub.function_on("documentos"):
                continue
            try:
                self.publish_mirrors()
            except Exception as exc:
                self.hub.last_error = f"clock: {logsafe.describe(exc)}"
                logger.warning("clock tick failed", exc_info=True)

    async def scan_loop(self) -> None:
        while True:
            try:
                await self.run_scan()
            except Exception as exc:
                self.hub.last_error = f"scan: {logsafe.describe(exc)}"
                logger.warning("scan failed", exc_info=True)
            await asyncio.sleep(self.scan_every)

    async def on_local_event(self, ev: dict[str, Any]) -> None:
        kind = ev.get("kind")
        if kind == "sesion_inicio":
            sid0 = str(ev.get("session_id", ""))
            if sid0 and sid0 not in self._seen_session_ids:
                self._seen_session_ids = [*self._seen_session_ids, sid0][-50:]
        if kind == "sesion_inicio" and self.hub.function_on("sesiones"):
            for m in self.sessions.start(
                str(ev.get("session_id", "")), model=str(ev.get("model", "")), platform=str(ev.get("platform", ""))
            ):
                self.hub.publish(m)
        elif kind in ("sesion_fin", "sesion_cierre") and self.hub.function_on("sesiones"):
            sid = str(ev.get("session_id", ""))
            if kind == "sesion_cierre":  # finalize = the place is really over
                for m in self.sessions.end(sid):
                    self.hub.publish(m)
        elif kind == "pending":  # CLI asks what is waiting for approval
            return
        elif kind == "funciones" and self.hub.connected:
            self.hub.publish({"t": "pulso"})

    async def poll_once(self) -> None:
        if not self.hub.function_on("cron"):
            return
        if self.hub.engine_url and not self.hub.connected:
            return  # do not run the CLI until the world has said (bienvenida) that it wants cron
        # Hermes' own store first (no subprocess, nothing scraped); the printed table only when the file cannot be used.
        parsed = await asyncio.to_thread(read_jobs_file, self.home)
        self.cron_source = "file"
        if parsed is None:
            self.cron_source = "cli"
            text = await asyncio.to_thread(self.cron_reader)
            if text is None:
                self.hub.last_error = "hermes cron list failed"
                return
            parsed = parse_cron_list(text)
        new = cronsync.snapshot(parsed["jobs"])
        self.known_jobs = set(new)
        msgs = cronsync.diff(self.cron_snapshot, new)
        self.cron_snapshot = new
        for msg in msgs:
            if msg["t"] == "lote":
                # A lote REPLACES everything the source published: send the WHOLE world (cron + sessions + mirrors), never cron alone.
                if self.hub.connected:  # when not connected a full snapshot goes out on connect anyway
                    await self.publish_full()
            else:
                self.hub.publish(msg)

    async def publish_full(self) -> None:
        for frame in P.full_snapshot_frames(await self.full_snapshot()):
            self.hub.publish(frame)

    async def on_world_functions(self) -> None:
        """A person flipped a function switch inside the world (engine ``funciones`` frame): rebuild what the world should hold.

        The engine already removed the things of a function that went off; the ones that came on need a fresh read. Everything is
        re-sent as ONE full snapshot so nothing else in the world is lost.
        """
        self.cron_snapshot = None
        self._mirror_published = None
        if self.hub.function_on("cron"):
            await self.poll_once()
        else:
            await self.publish_full()

    async def poll_loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception as exc:
                self.hub.last_error = f"cron poll: {logsafe.describe(exc)}"
                logger.warning("cron poll failed", exc_info=True)
            self.poll_cycles += 1
            await asyncio.sleep(self.poll)

    async def run(self) -> int:
        port = await self.hub.start()
        info = {
            "pid": os.getpid(),
            "port": port,
            "token": self.hub.token,
            "started": config.started_stamp(),
            "engine_url": self.hub.engine_url or "",
            "world_url": self.page_url,
            "read_only": self.read_only,
        }
        config.write_runtime(info, self.home)
        self._scanner = asyncio.create_task(self.scan_loop())  # the first thing: read the user's Hermes (read-only)
        self._poller = asyncio.create_task(self.poll_loop())
        self._clock = asyncio.create_task(self.clock_loop())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.hub._stop.set)
        try:
            await self.hub.wait_stopped()
        finally:
            if self._scanner:
                self._scanner.cancel()
            if self._poller:
                self._poller.cancel()
            if self._clock:
                self._clock.cancel()
            await self.hub.stop()
            config.clear_runtime(self.home)
        return 0


def main() -> int:
    demo = "--demo" in sys.argv[1:]
    read_only_flag = "--read-only" in sys.argv[1:] or demo
    home = config.hermes_home()
    log = logsafe.install(config.plugin_data_dir(home) / "bridge.log")
    try:
        cfg = S.Hermes3DSettings.from_yaml(home)
    except S.SettingsError as exc:
        logger.error("invalid plugin settings: %s", exc)
        return 2
    key = linkkey.resolve(home)
    if key:
        log.add_secret(key)
    url = link_url(cfg, key)
    bridge = Bridge(
        home,
        engine_url=url,
        link_key=key,
        poll_seconds=cfg.poll_seconds,
        scan_root=scan.default_root(),
        scan_days=cfg.scan_days,
        allow_direct_db=cfg.allow_direct_db_read or demo,  # `hermes 3d demo` is the user's explicit, read-only look at spend
        hermes_api=None if demo else hermes_api_mod,
        page_url=cfg.page_url(),
        read_only=cfg.read_only or read_only_flag,
        demo=demo,
        docs_port=cfg.docs_viewer_port,
    )
    log.add_secret(bridge.hub.token)
    return asyncio.run(bridge.run())


def link_url(cfg: S.Hermes3DSettings, key: str | None) -> str | None:
    """The engine link to open, or None. Path A (the world inside Desktop) needs ``engine_link: false``: an engine takes ONE source,
    and a local link that connects displaces the embedded page (engine contract, «Quién toca»)."""
    if not key or not cfg.engine_link:
        return None
    return f"ws://{config.HOST}:{supervisor.link_port(cfg)}{P.LINK_PATH}"


if __name__ == "__main__":
    sys.exit(main())
