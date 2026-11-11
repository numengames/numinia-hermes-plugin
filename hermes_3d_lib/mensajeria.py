"""Messaging-platform status reader (C10): one status light per platform (Telegram, Discord, ...) per profile.

Source, per Hermes home (the default home and every ``profiles/<name>``):

* ``<home>/gateway_state.json`` (written by the Hermes gateway itself): only ``gateway_state``, ``pid``,
  ``updated_at``, ``active_agents`` (a number), the NUMBER of ``served_profiles`` and, per entry of ``platforms``,
  only ``state``, ``needs_attention``, ``retrying_since`` (as a yes/no), ``writer_pid`` and a short ``error_code``;
* ``<home>/config.yaml``: only the NAMES of the keys under ``platforms`` (which platforms are set up) and the boolean
  ``enabled`` of each. The values under a platform (``token``, ``extra``, allow-lists, home channel) are never
  copied: they stay inside the parsed dict and nothing is read from them.

Strictly READ-ONLY and STATE ONLY. Never read, returned or logged: tokens, ``error_message`` (it can carry URLs or
keys), ``hermes_home``, ``argv``, chat or user ids, ``ingress_url``/``listener_base``, ``channel_directory.json``,
``.env``, ``pairing``, ``auth.json``. No process is signalled or started and no network call is made. A file over
256 kB is ignored. Player-visible words are English.

A platform record is only trusted when the gateway that wrote it is the live one: the gateway heartbeat
(``updated_at``) is younger than 120 s (the same TTL Hermes uses) AND the record's ``writer_pid`` equals the
gateway ``pid``. Anything else (a record left behind by an earlier gateway process, a stopped gateway) is reported
as ``stale`` with a grey light, never as green.

Light: ``green`` connected; ``amber`` connecting, retrying, paused or disconnected while the gateway is live;
``red`` fatal or needs attention; ``grey`` disabled, stale, unknown or configured-but-no-record.

``read_messaging(root)`` returns a plain, JSON-serialisable dict::

    {"schema": "hermes-3d/messaging@1", "label": "Messaging", "count": n, "connected": c, "attention": a,
     "gateways": [{"home", "state", "live", "active_agents", "profiles_served"}],
     "platforms": [{"platform", "profile", "state", "light", "stale", "needs_attention", "retrying",
                    "error_code", "configured"}], "error": ""}
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .crontable import clean_text
from .scan import _EXPECTED, _NAME, _read_yaml, profile_dirs

_EXPECTED_VALUE: tuple[type[BaseException], ...] = (*_EXPECTED, ValueError)
SCHEMA = "hermes-3d/messaging@1"
LABEL = "Messaging"
MAX_FILE = 256 * 1024
MAX_PLATFORMS = 200  # the world admits 200 things per zone
STALE_AFTER_S = 120  # Hermes' own TTL for a gateway_state.json heartbeat
PLATFORM_STATES = ("connected", "connecting", "disconnected", "disabled", "fatal", "paused", "retrying")
GATEWAY_STATES = ("starting", "running", "degraded", "startup_failed", "stopped", "draining")
_CODE = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,39}$")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except _EXPECTED_VALUE:
        return {}
    return data if isinstance(data, dict) else {}


def _age_s(stamp: Any, now: float) -> float | None:
    try:
        dt = datetime.fromisoformat(str(stamp))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return max(0.0, now - dt.timestamp())


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _name(value: Any) -> str:
    """A platform or profile name from a file: only the plain-name shape is kept."""
    s = str(value)
    return s if _NAME.match(s) else ""


def _light(state: str, stale: bool, attention: bool) -> str:
    if stale:
        return "grey"
    if state == "fatal" or attention:
        return "red"
    if state == "connected":
        return "green"
    if state in ("connecting", "retrying", "paused", "disconnected"):
        return "amber"
    return "grey"


def read_messaging(root: Path, *, now: float | None = None) -> dict[str, Any]:
    """The state of every messaging platform of every profile under ``root``. Never raises for odd files."""
    now = time.time() if now is None else now
    gateways: list[dict[str, Any]] = []
    found: dict[tuple[str, str], dict[str, Any]] = {}
    set_up: set[tuple[str, str]] = set()  # (home, platform) with a ``platforms.<platform>`` key in config.yaml
    errors: list[str] = []
    for hname, hdir in profile_dirs(Path(root)):
        try:
            gs = _read_json(hdir / "gateway_state.json")
            cfg = _read_yaml(hdir / "config.yaml")
        except _EXPECTED as exc:  # pragma: no cover - the helpers already swallow these
            errors.append(f"{hname}: {type(exc).__name__}")
            continue
        # -- configured platforms: key names and the boolean ``enabled`` only
        block = cfg.get("platforms")
        configured: dict[str, bool | None] = {}
        if isinstance(block, dict):
            for key, val in block.items():
                nm = _name(key)
                if nm:
                    en = val.get("enabled") if isinstance(val, dict) else None
                    configured[nm] = en if isinstance(en, bool) else None
                    set_up.add((hname, nm))
        # -- the gateway of this home
        live = False
        gpid = _int(gs.get("pid"))
        if gs:
            gstate = str(gs.get("gateway_state") or "")
            gstate = gstate if gstate in GATEWAY_STATES else "unknown"
            age = _age_s(gs.get("updated_at"), now)
            fresh = age is not None and age <= STALE_AFTER_S
            live = fresh and gstate in ("running", "degraded", "starting", "draining") and gpid is not None
            served = gs.get("served_profiles")
            gateways.append(
                {
                    "home": hname,
                    "state": gstate if fresh or gstate in ("stopped", "startup_failed") else "stale",
                    "live": live,
                    "active_agents": _int(gs.get("active_agents")) or 0,
                    "profiles_served": len(served) if isinstance(served, list) else 0,
                }
            )
        # -- the platform records
        plats = gs.get("platforms")
        if isinstance(plats, dict):
            for key, rec in plats.items():
                if not isinstance(rec, dict):
                    continue
                prof, _, plat = str(key).rpartition(":")
                plat, prof = _name(plat), _name(prof) if prof else hname
                if not plat or not prof:
                    continue
                state = str(rec.get("state") or "")
                state = state if state in PLATFORM_STATES else "unknown"
                wpid = _int(rec.get("writer_pid"))
                stale = not live or (wpid is not None and gpid is not None and wpid != gpid)
                attention = rec.get("needs_attention") is True
                code = str(rec.get("error_code") or "")
                entry = {
                    "platform": plat,
                    "profile": prof,
                    "state": "unknown" if stale else state,
                    "light": _light(state, stale, attention),
                    "stale": stale,
                    "needs_attention": attention and not stale,
                    "retrying": bool(rec.get("retrying_since")) and not stale,
                    "error_code": code if _CODE.match(code) and not stale else "",
                    "_at": str(rec.get("updated_at") or ""),
                }
                old = found.get((prof, plat))
                # one entry per (profile, platform): a trusted record beats a stale one, then the newer stamp wins
                if old is None or (not stale, entry["_at"]) > (not old["stale"], old["_at"]):
                    found[(prof, plat)] = entry
        # -- configured but no record anywhere in this home
        for plat, en in configured.items():
            if (hname, plat) not in found:
                found[(hname, plat)] = {
                    "platform": plat,
                    "profile": hname,
                    "state": "disabled" if en is False else "unknown",
                    "light": "grey",
                    "stale": False,
                    "needs_attention": False,
                    "retrying": False,
                    "error_code": "",
                    "_at": "",
                }
    out: list[dict[str, Any]] = []
    for (prof, plat), e in sorted(found.items()):
        e["configured"] = (prof, plat) in set_up  # the profile name is the home name
        e.pop("_at", None)
        out.append(e)
    out = out[:MAX_PLATFORMS]
    return {
        "schema": SCHEMA,
        "label": LABEL,
        "count": len(out),
        "connected": sum(1 for p in out if p["light"] == "green"),
        "attention": sum(1 for p in out if p["light"] == "red"),
        "gateways": gateways,
        "platforms": out,
        "error": clean_text("; ".join(errors), 200),
    }
