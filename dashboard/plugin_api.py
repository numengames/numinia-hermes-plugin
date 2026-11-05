# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""numinia-hermes-plugin backend routes, mounted at /api/plugins/numinia-hermes-plugin/.

Thin aggregator over Hermes' own public dashboard endpoints. It never edits
files: reads call GET routes, writes (cron pause/resume) call Hermes' own POST
routes, forwarding the caller's session token. Secrets are redacted.
"""
import asyncio
import re
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()

VERSION = "0.1.0"
REDACTED = "***"
_TIMEOUT = 10.0

_SECRET_KEY = re.compile(
    r"(token|secret|passw(or)?d|passwd|api[_-]?key|apikey|authorization|"
    r"credential|private[_-]?key|cookie|bearer)",
    re.I,
)
_SECRET_VALUE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}|gh[pousr]_[A-Za-z0-9]{16,}|xox[abprs]-[A-Za-z0-9\-]{8,}|"
    r"AIza[0-9A-Za-z_\-]{20,}|Bearer\s+[A-Za-z0-9._\-]{8,}|"
    r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+)"
)
_SECRET_ARG = re.compile(
    r"(?i)(--?[\w-]*(?:token|secret|password|api[_-]?key|key)[\w-]*[=\s:]+)(\S+)"
)
_SECRET_ASSIGN = re.compile(
    r"(?i)\b([\w]*(?:token|secret|password|api[_-]?key)[\w]*=)(\S+)"
)


def redact_text(s: str) -> str:
    s = _SECRET_VALUE.sub(REDACTED, s)
    s = _SECRET_ARG.sub(lambda m: m.group(1) + REDACTED, s)
    s = _SECRET_ASSIGN.sub(lambda m: m.group(1) + REDACTED, s)
    return s


def redact(obj: Any, _key: str = "") -> Any:
    """Recursively mask secret-looking keys and values."""
    if _key == "env" and isinstance(obj, dict):
        return {k: REDACTED for k in obj}  # env values are never shown
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _SECRET_KEY.search(str(k)) and isinstance(v, (str, int, float)) and v not in ("", None):
                out[k] = REDACTED
            else:
                out[k] = redact(v, str(k))
        return out
    if isinstance(obj, list):
        return [redact(v, _key) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


# --- upstream access (tests monkeypatch `upstream`) -------------------------

def _client(request: Request) -> httpx.AsyncClient:
    headers = {}
    tok = request.headers.get("X-Hermes-Session-Token")
    if tok:
        headers["X-Hermes-Session-Token"] = tok
    return httpx.AsyncClient(
        base_url=str(request.base_url), headers=headers, timeout=_TIMEOUT
    )


async def upstream(request: Request, method: str, path: str, **kw) -> Any:
    """Call a Hermes dashboard route; raises httpx.HTTPError on failure."""
    async with _client(request) as c:
        r = await c.request(method, path, **kw)
        r.raise_for_status()
        return r.json()


async def _zone(request: Request, path: str, params: dict | None = None):
    """Return (data, error). One failing endpoint must not sink the world."""
    try:
        return await upstream(request, "GET", path, params=params), None
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            detail = e.response.json().get("detail", "")
        except Exception:
            pass
        return None, f"{e.response.status_code} {redact_text(str(detail))}".strip()
    except Exception as e:  # network, bad JSON...
        return None, f"{type(e).__name__}: {redact_text(str(e))}"[:200]


def _list(data: Any, key: str) -> list:
    if isinstance(data, dict):
        data = data.get(key, [])
    return data if isinstance(data, list) else []


def _pick(d: dict, keys: tuple) -> dict:
    return {k: d.get(k) for k in keys if k in d}


# --- zone builders ---------------------------------------------------------

_JOB_KEYS = (
    "id", "name", "schedule_display", "enabled", "state", "next_run_at",
    "last_run_at", "last_status", "failure_streak", "paused_at", "deliver",
)


async def zone_cron(request, profile):
    data, err = await _zone(request, "/api/cron/jobs", profile)
    jobs = [_pick(j, _JOB_KEYS) for j in _list(data, "jobs") if isinstance(j, dict)]
    return {"jobs": jobs, "error": err}


async def zone_chat(request, profile):
    p = dict(profile or {})
    p["limit"] = 20
    data, err = await _zone(request, "/api/sessions", p)
    sessions = _list(data, "sessions")
    keep = ("id", "title", "source", "model", "started_at", "last_active",
            "message_count", "preview", "is_active")
    return {"sessions": [_pick(s, keep) for s in sessions if isinstance(s, dict)],
            "error": err}


async def zone_agents(request, profile):
    prof, e1 = await _zone(request, "/api/profiles")
    act, e2 = await _zone(request, "/api/profiles/active")
    st, e3 = await _zone(request, "/api/status")
    keep = ("name", "is_default", "model", "provider", "skill_count",
            "gateway_running", "description", "display_name", "bot_title")
    profiles = [_pick(p, keep) for p in _list(prof, "profiles") if isinstance(p, dict)]
    status = _pick(st, ("version", "gateway_running", "active_agents", "gateway_busy")) \
        if isinstance(st, dict) else {}
    return {
        "profiles": profiles,
        "active": act.get("active") if isinstance(act, dict) else None,
        "status": status,
        "error": e1 or e2 or e3,
    }


async def zone_mcp(request, profile):
    data, err = await _zone(request, "/api/mcp/servers", profile)
    keep = ("name", "transport", "command", "args", "env", "enabled", "tools",
            "source", "plugin")
    servers = [_pick(s, keep) for s in _list(data, "servers") if isinstance(s, dict)]
    return {"servers": servers, "error": err}


async def zone_options(request, profile):
    info, e1 = await _zone(request, "/api/model/info", profile)
    tools, e2 = await _zone(request, "/api/tools/toolsets", profile)
    cfg, e3 = await _zone(request, "/api/config", profile)
    model = _pick(info, ("model", "provider", "effective_context_length")) \
        if isinstance(info, dict) else {}
    toolsets = [_pick(t, ("name", "label", "enabled", "available", "configured", "tools"))
                for t in _list(tools, "toolsets") if isinstance(t, dict)]
    config = _pick(cfg, ("model", "toolsets")) if isinstance(cfg, dict) else {}
    return {"model": model, "toolsets": toolsets, "config": config,
            "error": e1 or e2 or e3}


async def zone_documents(request, profile):
    sk, e1 = await _zone(request, "/api/skills", profile)
    mem, e2 = await _zone(request, "/api/memory", profile)
    keep = ("name", "description", "category", "enabled", "usage", "provenance")
    skills = [_pick(s, keep) for s in _list(sk, "skills") if isinstance(s, dict)]
    memory = {}
    if isinstance(mem, dict):
        memory = {
            "active": mem.get("active"),
            "providers": [_pick(p, ("name", "available")) for p in _list(mem, "providers")
                          if isinstance(p, dict)],
        }
    return {"skills": skills, "memory": memory, "error": e1 or e2}


ZONES = {
    "cron": zone_cron, "chat": zone_chat, "agents": zone_agents,
    "mcp": zone_mcp, "options": zone_options, "documents": zone_documents,
}


# --- routes ----------------------------------------------------------------

@router.get("/health")
async def health():
    return {"ok": True, "plugin": "numinia-hermes-plugin", "version": VERSION}


@router.get("/world")
async def world(request: Request, profile: str | None = None):
    pq = {"profile": profile} if profile else None
    names = list(ZONES)
    results = await asyncio.gather(*(ZONES[n](request, pq) for n in names))
    return redact({"version": VERSION, "zones": dict(zip(names, results))})


_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


async def _cron_action(request: Request, job_id: str, action: str, profile):
    if not _ID.match(job_id):
        raise HTTPException(400, "invalid job id")
    pq = {"profile": profile} if profile else None
    try:
        job = await upstream(request, "POST", f"/api/cron/jobs/{job_id}/{action}", params=pq)
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, f"hermes refused {action}")
    except httpx.HTTPError:
        raise HTTPException(502, "hermes unreachable")
    return {"ok": True, "job": redact(_pick(job, _JOB_KEYS)) if isinstance(job, dict) else None}


@router.post("/cron/{job_id}/pause")
async def cron_pause(job_id: str, request: Request, profile: str | None = None):
    return await _cron_action(request, job_id, "pause", profile)


@router.post("/cron/{job_id}/resume")
async def cron_resume(job_id: str, request: Request, profile: str | None = None):
    return await _cron_action(request, job_id, "resume", profile)

# NOTE: no /cron/{id}/run: H0 did not execute trigger/fire (needs model+gateway).
