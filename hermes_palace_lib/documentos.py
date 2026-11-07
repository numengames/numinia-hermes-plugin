"""Mirror ``documentos``: a small CURATED list of documentation pages as ``documento`` things (the library, zone z1).

Each thing carries the page's address on the local docs viewer (``docs-visor``, lite build; origin
``http://127.0.0.1:<docs_viewer_port>``, default 8791). The engine drops a document whose origin is not in the world's
``documentos.origenes``, so the owner lists that origin there (the template does for the default port).
Published ONLY behind ``funciones.documentos`` (off by default). The list is fixed in code: nothing from the user's files.

Contract (M6): ``id`` <= 32, ``titulo`` <= 60, ``subtitulo`` <= 120, ``url`` <= 300 bytes in canonical form, ``acciones`` [].
"""

from __future__ import annotations

from typing import Any

from .protocol import MAX_URL, make_object
from .worldtext import say
from .zones import FALLBACK_ZONES

TYPE_DOCUMENTO = "documento"
ZONE_DOCUMENTOS = FALLBACK_ZONES[TYPE_DOCUMENTO]
DEFAULT_PORT = 8791
BASE_PATH = "/docs/"  # the lite viewer's base (docs-visor docs.lock.json base_url)

# (id, title, path under /docs/, one line) - titles are the pages' own (English: it is Hermes' documentation).
CURATED: tuple[tuple[str, str, str, str], ...] = (
    ("quickstart", "Hermes Agent Quickstart", "getting-started/quickstart", "First steps with Hermes."),
    ("installation", "Installation", "getting-started/installation", "Install and update Hermes."),
    ("configuration", "Hermes Agent Configuration", "user-guide/configuration", "Every setting in config.yaml."),
    ("profiles", "Profiles: Running Multiple Agents", "user-guide/profiles", "One Hermes, several agents."),
    ("sessions", "Sessions", "user-guide/sessions", "How conversations are stored and resumed."),
    ("security", "Security", "user-guide/security", "Approvals, sandboxing and secrets."),
    ("cron", "Scheduled Tasks (Cron)", "user-guide/features/cron", "Jobs that run on a schedule."),
    ("skills", "Skills System", "user-guide/features/skills", "What a skill is and how Hermes uses it."),
    ("kanban", "Kanban — Multi-Agent Profile Collaboration", "user-guide/features/kanban", "The work board shared by agents."),
    ("mcp", "MCP (Model Context Protocol)", "user-guide/features/mcp", "Connect tool servers."),
    ("memory", "Persistent Memory", "user-guide/features/memory", "What Hermes remembers between sessions."),
    ("delegation", "Subagent Delegation", "user-guide/features/delegation", "Hand work to subagents."),
    ("plugins", "Plugins", "user-guide/features/plugins", "Extend Hermes with plugins."),
    ("cli-commands", "CLI Commands Reference", "reference/cli-commands", "Every `hermes` command."),
)


def origin(port: int = DEFAULT_PORT) -> str:
    """The viewer's origin in the engine's canonical form."""
    return f"http://127.0.0.1:{int(port)}"


def page_url(path: str, port: int = DEFAULT_PORT) -> str:
    return f"{origin(port)}{BASE_PATH}{path.strip('/')}/"


def snapshot(port: int = DEFAULT_PORT) -> dict[str, dict[str, Any]]:
    """id -> ``documento`` object for the curated pages (a page whose URL would pass the byte limit is skipped)."""
    out: dict[str, dict[str, Any]] = {}
    sub = say("docs_sub")["texto"]
    for id_, title, path, line in CURATED:
        url = page_url(path, port)
        if len(url.encode("utf-8")) > MAX_URL:
            continue
        obj = make_object(
            TYPE_DOCUMENTO,
            id_,
            title,
            subtitulo=sub,
            estado="activo",
            zona=ZONE_DOCUMENTOS,
            acciones=[],
            url=url,
            descripcion=line,
        )
        out[obj["id"]] = obj
    return out
