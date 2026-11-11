"""C12: the delivery clock, the entry requirements and the official links, as ``documento`` things in the library (z1).

No engine change: a ``documento`` already carries a title, a subtitle, a description and an address. The clock is ONE
thing whose title is the time left (``Deadline · 4d 03h 12m``); the bridge rebuilds it every minute and
``publish_mirrors`` sends a ``cambio`` only when the minute changed. Nothing is read from the user's Hermes: the
list is fixed in code and the only input is the clock. Published ONLY behind ``funciones.documentos`` (the same
switch as the library). Every address is a public https page of the hackathon / Hermes docs; the world's owner must
list their ORIGINS in ``documentos.origenes`` (``ORIGINS``) or the engine drops the thing.

Contract (M6): ``id`` <= 32, ``titulo`` <= 60 B, ``subtitulo`` <= 120 B, ``descripcion`` <= 200 B, ``url`` <= 300 B.
After the deadline the clock turns into ``Deadline · closed`` (estado ``fallo``) and stops changing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .protocol import MAX_URL, make_object
from .zones import FALLBACK_ZONES

TYPE_DOCUMENTO = "documento"
ZONE = FALLBACK_ZONES[TYPE_DOCUMENTO]  # z1; the library
DEADLINE = datetime(2026, 11, 11, 22, 0, tzinfo=UTC)  # 5 PM EST, Nov 11 (announcement of the hackathon)
DEADLINE_TEXT = "Nov 11, 22:00 UTC"

# Official pages (from the hackathon announcement of Nous Research and its developer docs).
URL_SUBMISSION = "https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission"
URL_DEVGUIDE = "https://hermes-agent.nousresearch.com/docs/developer-guide/plugins"
URL_FORM = "https://form.typeform.com/to/FwJLvOln"
URL_DISCORD = "https://discord.gg/nousresearch"
URL_X = "https://x.com/NousResearch"

# Origins the world's owner lists in ``documentos.origenes`` so these things open (the template only lists the local docs).
ORIGINS = ("https://hermes-agent.nousresearch.com", "https://form.typeform.com", "https://discord.gg", "https://x.com")

CLOCK_ID = "plazo-reloj"

# (id, title, subtitle, description, url)
STATIC: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "plazo-1-pr",
        "1 · Pull request to the catalog",
        "Add plugin-catalog/<name>.yaml with the 40-char commit",
        "Open one pull request to NousResearch/hermes-agent that adds one file. Read the submission guide first.",
        URL_SUBMISSION,
    ),
    (
        "plazo-2-video",
        "2 · Demo video under 90 s on X",
        "Show the plugin working; tag @NousResearch",
        "Post the video on X and tag @NousResearch. Keep it under 90 seconds.",
        URL_X,
    ),
    (
        "plazo-3-form",
        "3 · Entry form",
        "Send the PR link and the video link",
        "Submit both links through the form. You can send it as soon as the pull request is open.",
        URL_FORM,
    ),
    (
        "plazo-guide",
        "Plugin developer guide",
        "How to build, test and validate a plugin",
        "Official Hermes developer guide: hermes plugins doctor and hermes plugins validate.",
        URL_DEVGUIDE,
    ),
    (
        "plazo-discord",
        "Nous Research Discord",
        "Questions about the hackathon; entry threads open at close",
        "Official Nous Research Discord.",
        URL_DISCORD,
    ),
)


def _aware(now: datetime | None) -> datetime:
    now = now or datetime.now(UTC)
    return now if now.tzinfo else now.replace(tzinfo=UTC)


def remaining(now: datetime | None = None) -> int:
    """Whole seconds left, never negative."""
    return max(0, int((DEADLINE - _aware(now)).total_seconds()))


def left_text(seconds: int) -> str:
    """``4d 03h 12m`` / ``03h 12m`` / ``12m`` (rounded DOWN to the minute: the card changes once a minute)."""
    m = seconds // 60
    d, rest = divmod(m, 1440)
    h, mi = divmod(rest, 60)
    if d:
        return f"{d}d {h:02d}h {mi:02d}m"
    if h:
        return f"{h:02d}h {mi:02d}m"
    return f"{mi}m"


def clock_object(now: datetime | None = None) -> dict[str, Any]:
    secs = remaining(now)
    if secs <= 0:
        title, sub, state = "Deadline · closed", f"Submissions closed {DEADLINE_TEXT}", "fallo"
        desc = "The hackathon deadline has passed."
    else:
        title = f"Deadline · {left_text(secs)}"
        sub = f"Submissions close {DEADLINE_TEXT} (5 PM EST)"
        state = "programado" if secs < 86400 else "activo"
        desc = "Time left to send the pull request, the video and the form. Rounded down to the minute."
    return make_object(
        TYPE_DOCUMENTO, CLOCK_ID, title, subtitulo=sub, estado=state, zona=ZONE, acciones=[], url=URL_FORM, descripcion=desc
    )


def snapshot(now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """id -> ``documento`` object: the clock first, then the three requirements and the two links."""
    out: dict[str, dict[str, Any]] = {CLOCK_ID: clock_object(now)}
    for id_, title, sub, desc, url in STATIC:
        if len(url.encode("utf-8")) > MAX_URL:
            continue
        out[id_] = make_object(
            TYPE_DOCUMENTO, id_, title, subtitulo=sub, estado="activo", zona=ZONE, acciones=[], url=url, descripcion=desc
        )
    return out
