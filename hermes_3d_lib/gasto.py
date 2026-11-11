# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""Mirror ``gasto``: everything that is SPENT, as ``uso`` things (one per profile + one overall).

Sources: the scan (``scan.scan_gasto``: read-only SQLite sums, the same numbers ``hermes insights`` uses) and
``parse_insights`` for the text of ``hermes insights --days N`` (``agent/insights.py::format_terminal``),
used for cross-checks only; the plugin never runs ``hermes insights`` on the real home (it would write a
boot-bootstrap record there). No session text is ever read or published: only tokens, cost, model names.

Published ONLY behind ``funciones.gasto`` (off by default). The card (title/subtitle, ≤ 60/120 BYTES) carries
tokens, estimated cost, top model and window; the engine type ``uso`` needs a zone that admits it (NECESIDADES-MOTOR.md).
"""

from __future__ import annotations

import re
from typing import Any

from .crontable import clean_text
from .protocol import make_object
from .zones import FALLBACK_ZONES

TYPE_GASTO = "uso"  # the engine's own type for spend (function ``uso``)
ZONE_GASTO = FALLBACK_ZONES[TYPE_GASTO]  # z2; the hub swaps in the zone the engine announces
TOTAL_ID = "total"

_NUM = r"([\d,]+)"
_MONEY = r"\$?([\d,]+(?:\.\d+)?)"


def human(n: float | int) -> str:
    """12_345_678 -> '12,3 M'; 4_200 -> '4,2 k' (Spanish decimal comma; keeps titles short)."""
    n = float(n or 0)
    for lim, suf in ((1e9, " G"), (1e6, " M"), (1e3, " k")):
        if abs(n) >= lim:
            return f"{n / lim:.1f}".replace(".", ",") + suf
    return str(int(n))


def money(v: float | int) -> str:
    return f"${float(v or 0):,.2f}"


def parse_insights(text: str) -> dict[str, Any]:
    """Text of ``hermes insights --days N`` -> ``{days, sessions, messages, tokens, input, output, cost_estimated, models[], empty}``.

    Tolerant: a missing line is simply absent (``None``); never raises. ANSI is stripped.
    """
    t = re.sub(r"\x1b\[[0-9;]*m", "", text or "")
    out: dict[str, Any] = {
        "days": None,
        "sessions": None,
        "messages": None,
        "tokens": None,
        "input": None,
        "output": None,
        "cost_estimated": None,
        "models": [],
        "empty": False,
    }
    if re.search(r"No sessions found in the last", t):
        out["empty"] = True
        m = re.search(r"last (\d+) days", t)
        out["days"] = int(m.group(1)) if m else None
        return out
    m = re.search(r"Last (\d+) days", t)
    out["days"] = int(m.group(1)) if m else None
    m = re.search(r"Sessions:\s+(\d+)\s+Messages:\s+" + _NUM, t)
    if m:
        out["sessions"], out["messages"] = int(m.group(1)), int(m.group(2).replace(",", ""))
    m = re.search(r"Input tokens:\s+" + _NUM + r"\s+Output tokens:\s+" + _NUM, t)
    if m:
        out["input"], out["output"] = int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
    m = re.search(r"Total tokens:\s+" + _NUM, t)
    if m:
        out["tokens"] = int(m.group(1).replace(",", ""))
    m = re.search(r"Estimated:\s+" + _MONEY, t)
    if m:
        out["cost_estimated"] = float(m.group(1).replace(",", ""))
    sec = re.search(r"Models Used\s*\n\s*[─-]*\s*\n?\s*Model\s+Sessions\s+Tokens\s*\n((?:\s+\S.*\n?)+)", t)
    if sec:
        for line in sec.group(1).splitlines():
            mm = re.match(r"\s+(\S.*?)\s{2,}(\d+)\s+([\d,]+)\s*$", line)
            if mm:
                out["models"].append(
                    {
                        "model": clean_text(mm.group(1), 40),
                        "sessions": int(mm.group(2)),
                        "tokens": int(mm.group(3).replace(",", "")),
                    }
                )
    return out


def _card(name: str, tokens: int, cost: float, top: str, days: int, sessions: int, cache: int = 0) -> tuple[str, str]:
    """Subtitle: ``tokens · cache N · $cost est. · sessions · days · model``. Cache tokens (read + write) are far larger than
    in+out, so they travel as their OWN segment and are never added to ``tokens`` (the engine draws them apart); the cost is
    always followed by ``est.`` (an estimate, not what the provider billed)."""
    title = f"Spend · {name}"
    mid = f" · cache {human(cache)}" if cache else ""
    sub = f"{human(tokens)} tokens{mid} · {money(cost)} est. · {sessions} sessions · {days} d" + (f" · {top}" if top else "")
    return title, sub


def gasto_objects(section: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``scan['sections']['gasto']`` -> id -> objects: ``gasto/total`` plus ``gasto/<profile>`` for each profile with sessions."""
    out: dict[str, dict[str, Any]] = {}
    if not section or (section.get("error") and not section.get("por_perfil")):
        return out
    days = int(section.get("dias") or 0)
    top_models = section.get("top_modelos") or []
    t, s = _card(
        "total",
        int(section.get("tokens") or 0),
        float(section.get("coste_estimado_usd") or 0),
        (top_models[0]["modelo"] if top_models else ""),
        days,
        int(section.get("sesiones") or 0),
        sum(int(p.get("cache_lectura") or 0) + int(p.get("cache_escritura") or 0) for p in section.get("por_perfil") or []),
    )
    out[TOTAL_ID] = make_object(TYPE_GASTO, TOTAL_ID, t, subtitulo=s, estado="activo", zona=ZONE_GASTO, acciones=[])
    # Per-profile spend has no cards here any more: it lives with the agent (its card and its body's talk), see by_profile.
    return out


def by_profile(section: dict[str, Any]) -> dict[str, str]:
    """``scan['sections']['gasto']`` -> ``{profile: "$4.20 est. · 12,3 M tokens · 30 d"}`` for each profile with sessions.

    The figure the spend zone used to show per profile, now shown with the agent. Always labelled ``est.``."""
    out: dict[str, str] = {}
    if not section:
        return out
    days = int(section.get("dias") or 0)
    for p in section.get("por_perfil") or []:
        if not p.get("sesiones"):
            continue
        out[str(p.get("perfil", ""))] = (
            f"{money(float(p.get('coste_estimado_usd') or 0))} est. · {human(int(p.get('tokens') or 0))} tokens · {days} d"
        )
    return out
