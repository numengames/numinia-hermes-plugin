"""Mirror ``agentes``: every Hermes profile (agent) as a ``perfil`` thing in the world.

Two sources, one shape:

* the **scan** (``scan.scan_agentes``) — primary, read-only files/SQLite, no ``hermes`` process;
* ``parse_profile_list`` — parses the text of ``hermes profile list`` (``hermes_cli/profile_cmd.py::_profile_list``)
  for cross-checks and for installs where only the CLI is reachable. It is NEVER run by the plugin on
  the user's real home (every ``hermes`` run writes a boot-bootstrap record there).

Published ONLY behind ``funciones.agentes`` (off by default; the engine function is ``perfiles``). The engine
knows the type ``perfil`` but a world needs a zone that admits it (QA/NECESIDADES-MOTOR.md); without one the engine
answers with an ``error`` frame and the bridge keeps the reason in ``last_error`` — nothing breaks.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .crontable import clean_text
from .protocol import make_object
from .zones import FALLBACK_ZONES

logger = logging.getLogger("hermes_3d.agentes")

TYPE_AGENTE = "perfil"  # the engine's own type for a Hermes profile (function ``perfiles``)
ZONE_AGENTES = FALLBACK_ZONES[TYPE_AGENTE]  # z6; the hub swaps in the zone the engine announces
_PROFILE_ID = re.compile(r"[^a-z0-9._-]")

# ``hermes profile list`` columns: marker+name(15) model(28) gateway(12) alias(12) distribution
_ROW = re.compile(
    r"^(?P<mark>\s*(?:◆)?\s?)(?P<name>\S+(?:\s\(\S+\))?)\s+(?P<model>\S.*?)\s{2,}(?P<gw>running|stopped)\s+(?P<alias>\S+)\s+(?P<dist>\S.*?)\s*$"
)


def parse_profile_list(text: str) -> dict[str, Any]:
    """Text of ``hermes profile list`` -> ``{"profiles":[{name,model,gateway,alias,distribution,active}],"empty":bool}``. Never raises."""
    profiles: list[dict[str, Any]] = []
    empty = False
    for raw in (text or "").splitlines():
        line = re.sub(r"\x1b\[[0-9;]*m", "", raw).rstrip()
        s = line.strip()
        if not s:
            continue
        if s.startswith("No profiles found"):
            empty = True
            continue
        if s.startswith("Profile ") or set(s) <= set("─ "):
            continue
        m = _ROW.match(line)
        if not m:
            continue
        name = m.group("name").split(" ")[0]
        profiles.append(
            {
                "name": clean_text(name, 40),
                "model": clean_text(m.group("model"), 40),
                "gateway": m.group("gw"),
                "alias": clean_text(m.group("alias"), 20),
                "distribution": clean_text(m.group("dist"), 30),
                "active": "◆" in m.group("mark"),
            }
        )
    return {"profiles": profiles, "empty": empty}


def agent_id(name: str) -> str:
    """Engine id: ``a-<profile>`` lowercased, characters outside [a-z0-9._-] replaced, ≤ 32."""
    return ("a-" + _PROFILE_ID.sub("-", str(name).lower()))[:32]


def agent_state(item: dict[str, Any]) -> str:
    gw = item.get("gateway")
    if gw is True or gw == "running":
        return "activo"
    if gw is False or gw == "stopped":
        return "apagado"
    return "desconocido"


def agent_to_object(item: dict[str, Any]) -> dict[str, Any]:
    """One scanned/parsed profile -> an ``objeto`` of type ``agente`` (no verbs: v0 only shows)."""
    name = str(item.get("nombre") or item.get("name") or item.get("id") or "")
    model = str(item.get("modelo") or item.get("model") or "")
    bits = [model or "sin modelo"]
    if item.get("tiene_cron") or item.get("cron_total"):
        bits.append(f"{item.get('cron_activos', 0)}/{item.get('cron_total', 0)} cron")
    gw = item.get("gateway")
    bits.append(
        "gateway " + ("activo" if gw is True or gw == "running" else "parado" if gw is False or gw == "stopped" else "¿?")
    )
    return make_object(
        TYPE_AGENTE,
        agent_id(item.get("id") or name),
        name or "(sin nombre)",
        subtitulo=" · ".join(bits),
        estado=agent_state(item),
        zona=ZONE_AGENTES,
        acciones=[],
    )


def snapshot_from_scan(section: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``scan['sections']['agentes']`` -> id -> object. An errored/empty section gives ``{}``."""
    out: dict[str, dict[str, Any]] = {}
    for it in (section or {}).get("items", []) or []:
        try:
            obj = agent_to_object(it)
            out[obj["id"]] = obj
        except (KeyError, TypeError, ValueError, AttributeError):  # one odd profile must not hide the rest
            logger.debug("skipping a profile that cannot be shown", exc_info=True)
    return out
