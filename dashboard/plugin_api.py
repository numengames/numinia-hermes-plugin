# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""Dashboard/desktop backend routes of hermes-3d, mounted at /api/plugins/hermes-3d/.

Loaded by Hermes as a separate module by file path (and possibly in another process), so it
shares NO Python state with ``__init__.register``: it reads the bridge through its loopback
client (runtime file + bearer token) and the switches from ``config.yaml``.

Read-only on purpose, with two named exceptions for the world inside Desktop (path A): ``POST /embed-credential`` (makes a
short-lived credential, writes nothing) and ``POST /action`` (a request that waits for Hermes' approval before anything runs).
The on/off switches go through the normal Settings → Plugins page, which uses Hermes' own validated writer.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body


def _lib():
    """Import the sibling ``hermes_3d_lib`` package without depending on sys.path."""
    root = Path(__file__).resolve().parents[1]
    name = "hermes_3d_lib"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, root / name / "__init__.py", submodule_search_locations=[str(root / name)]
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _read_switches() -> dict[str, bool]:
    """Switches from the same ``config.yaml`` section ``ctx.get_config`` reads (no private Hermes import)."""
    _lib()
    from hermes_3d_lib import config as cfg
    from hermes_3d_lib import settings as S

    return S.switches(S.yaml_getter(cfg.hermes_home()))


router = APIRouter()
_BODY = Body(default=None)


@router.get("/status")
async def status() -> dict[str, Any]:
    """Bridge state for the status-bar chip. Always 200; ``bridge`` says whether it is up."""
    from hermes_3d_lib import client

    _lib()
    res = await asyncio.to_thread(client.call, "GET", "/status")
    if not res.get("ok"):
        return {"bridge": False, "connected": False, "reason": res.get("error", "unknown"), "switches": _read_switches()}
    data = res["data"]
    return {
        "bridge": True,
        "connected": bool(data.get("connected")),
        "port": data.get("port"),
        "last_error": data.get("last_error", ""),
        "sent": data.get("sent", 0),
        "received": data.get("received", 0),
        "switches": _read_switches(),
    }


@router.get("/switches")
async def switches() -> dict[str, Any]:
    """Local on/off per function; ``engine`` is what the world itself says (``bienvenida``)."""
    from hermes_3d_lib import client

    _lib()
    local = _read_switches()
    res = await asyncio.to_thread(client.call, "GET", "/status")
    engine = res["data"].get("engine_functions", {}) if res.get("ok") else {}
    rows = [
        {"name": name, "local": on, "engine": engine.get(name), "effective": bool(on and engine.get(name, True))}
        for name, on in local.items()
    ]
    return {"functions": rows, "connected": bool(res.get("ok") and res["data"].get("connected"))}


@router.get("/world")
async def world() -> dict[str, Any]:
    """URL of the local world page, for the 'open world' button. Loopback only, built here."""
    from hermes_3d_lib import config as cfg

    _lib()
    info = cfg.read_runtime()
    if not info or not cfg.pid_alive(info.get("pid", 0)):
        return {"url": None, "reason": "bridge-not-running"}
    from hermes_3d_lib import settings as S

    try:
        url = S.loopback_url("world_url", info.get("world_url"))
    except S.SettingsError:
        url = ""
    return {"url": url} if url else {"url": None, "reason": "world-not-started"}


@router.get("/events")
async def events(since: int = 0, limit: int = 100) -> dict[str, Any]:
    """Live activity (metadata only: kind, platform, profile, ids, tool NAME, status, timestamps).

    Fed by Hermes observer hooks into a bounded spool; empty and ``enabled: false`` while the ``sesiones`` switch is off.
    Poll with ``since=<next>`` to get only what is new."""
    _lib()
    from hermes_3d_lib import config as cfg
    from hermes_3d_lib import events as ev

    if not _read_switches().get("sesiones"):
        return {"enabled": False, "events": [], "next": since}
    out = ev.read(since, limit, cfg.hermes_home())
    return {"enabled": True, **out}


@router.get("/state")
async def state() -> dict[str, Any]:
    """One metadata-only snapshot for every way into a world (Desktop page and the HMAC link): switches, whether the bridge
    runs, the bridge's last scan sections (counts, titles, states), and the newest activity events.
    Never message bodies; ``scan`` is empty while the bridge is down."""
    _lib()
    from hermes_3d_lib import client
    from hermes_3d_lib import config as cfg
    from hermes_3d_lib import events as ev

    sw = _read_switches()
    got = await asyncio.to_thread(client.call, "GET", "/scan")
    scan = got["data"] if got.get("ok") and isinstance(got.get("data"), dict) else {}
    feed = ev.read(0, 20, cfg.hermes_home()) if sw.get("sesiones") else {"events": [], "next": 0}
    return {
        "bridge": bool(got.get("ok")),
        "switches": sw,
        "scan": scan,
        "events": feed.get("events", []),
        "next": feed.get("next", 0),
    }


# ---------------------------------------------------------------------------------- path A: the world inside Desktop
MAX_ACTION_BYTES = 16 * 1024  # the same cap as one embed message
ACTION_FIELDS = ("id", "objeto", "verbo", "seguridad", "quien", "caduca")


@router.post("/embed-credential")
async def embed_credential() -> dict[str, Any]:
    """The URL for the Desktop frame: the world page, ``?embed=1`` and a fresh short-lived credential in the fragment.

    The credential is made here from the plugin's own link key, which is never returned. Always 200 with ``ok``;
    a refusal says why (``engine-down``, ``no-link-key``, ``embed-off``) and the exact command to run.
    """
    _lib()
    from hermes_3d_lib import config as cfg
    from hermes_3d_lib import embed, linkkey
    from hermes_3d_lib import settings as S

    home = cfg.hermes_home()
    try:
        conf = S.Hermes3DSettings.from_yaml(home)
    except S.SettingsError as exc:
        return {
            "ok": False,
            "error": "bad-settings",
            "message": str(exc)[:200],
            "hint": "Fix the hermes-3d section of config.yaml",
        }
    out = await asyncio.to_thread(embed.issue, conf, linkkey.resolve(home))
    return out


@router.get("/world-snapshot")
async def world_snapshot() -> dict[str, Any]:
    """What the world should show right now, built by the SAME serializers as the HMAC link (``objetos``, ``extra``, ``funciones``)."""
    from hermes_3d_lib import client

    _lib()
    res = await asyncio.to_thread(client.call, "GET", "/snapshot", timeout=8.0)
    if not res.get("ok") or not isinstance(res.get("data"), dict):
        return {"ok": False, "error": res.get("error", "bridge-down"), "hint": "Run `hermes 3d up`"}
    return {"ok": True, **res["data"]}


@router.post("/action")
async def action(body: Any = _BODY) -> dict[str, Any]:
    """A request from the world inside Desktop (the frame's ``peticion``). It takes the SAME road as one from the HMAC link:
    read-only refuses, an unknown verb is refused, a request id works once, and nothing runs until Hermes approves it.
    With nobody to approve it fails closed. The answer is ``{ok, code?, texto}``, ready to go back to the frame as ``resultado``."""
    import json as _json

    from hermes_3d_lib import client
    from hermes_3d_lib.worldtext import say

    _lib()
    if not isinstance(body, dict) or len(_json.dumps(body, ensure_ascii=False).encode("utf-8")) > MAX_ACTION_BYTES:
        return say("invalid_request")
    msg = {k: body[k] for k in ACTION_FIELDS if k in body}
    res = await asyncio.to_thread(client.call, "POST", "/action/submit", msg, timeout=35.0)
    if not res.get("ok") or not isinstance(res.get("data"), dict):
        return say("no_actions")
    return res["data"]
