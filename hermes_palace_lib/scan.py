"""``palace scan``: the FIRST thing the plugin does when the world opens — a full READ-ONLY scan of the
user's Hermes (HISTORIA-DE-USUARIO §4): profiles/agents, cron jobs, sessions, skills, MCP servers,
kanban, projects, documents and spend.

Hard rules (the scan is the one place that touches the user's real data):

* **Read only, by construction.** SQLite is opened ``mode=ro`` (never created, never written; a WAL
  being live is fine). Files are only ``open(..., 'r')``-ed or ``stat``-ed. No ``hermes`` command is
  run by the primary path (every hermes CLI run writes a boot-bootstrap record in the home; the
  CLI-text parsers in ``agentes.py``/``gasto.py`` exist for cross-checks only and are never called here).
* **No message text, no secrets.** Only counts, names, ids (hashed when they are session ids), states,
  timestamps and token/cost numbers leave this module. ``.env``, ``auth.json``, ``mcp-tokens`` and
  ``config.yaml`` values other than a whitelist are never read into the snapshot. Free text that
  came from a user (task titles, cron names, skill names) is cleaned/cut like the rest of the plugin.
* **Sections are independent.** Each one is run inside its own guard; a failing section yields
  ``{"ok": False, "error": "<ExceptionName>: short", "at": <epoch>, ...empty data}`` and never stops
  the others. Every section carries ``at`` (epoch seconds) and ``error`` (``""`` when fine).

``scan_hermes(root)`` returns a plain dict (JSON-serialisable)::

    {"schema": "hermes-palace/scan@1", "at": ..., "root_name": ".hermes", "took_ms": ...,
     "sections": {"agentes": {...}, "cron": {...}, "sesiones": {...}, "skills": {...}, "mcp": {...},
                  "kanban": {...}, "proyectos": {...}, "documentos": {...}, "gasto": {...}}}
"""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from .crontable import clean_text
from .logsafe import fit_title, redact_title

logger = logging.getLogger("hermes_palace.scan")

try:
    import yaml as _yaml

    _YAML_ERRORS: tuple[type[BaseException], ...] = (_yaml.YAMLError,)
except ImportError:  # pragma: no cover - PyYAML ships with Hermes
    _yaml = None  # type: ignore[assignment]
    _YAML_ERRORS = ()

# What reading Hermes' own files can legitimately fail with. Anything else is a bug and must surface (_guard logs it).
_EXPECTED: tuple[type[BaseException], ...] = (
    OSError,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
    IndexError,
    sqlite3.Error,
    *_YAML_ERRORS,
)
_EXPECTED_YAML: tuple[type[BaseException], ...] = (*_EXPECTED, ImportError)

SCHEMA = "hermes-palace/scan@1"
SECTIONS = (
    "agentes",
    "cron",
    "sesiones",
    "skills",
    "mcp",
    "kanban",
    "proyectos",
    "arbol",
    "enchufes",
    "mensajeria",
    "documentos",
    "gasto",
)
MAX_ITEMS = 200  # per section: the world shows at most this many things anyway
DEFAULT_DAYS = 30
DB_TIMEOUT = 3.0
# Sections that read one of Hermes' own SQLite files. Hermes documents no public API for these numbers, so they are a
# direct read of its private schema: they run only when the user set ``allow_direct_db_read`` (see README, "What is read
# from where"). Everything else is read from files or from the CLI/hooks.
DB_SECTIONS = ("sesiones", "kanban", "proyectos", "arbol", "gasto")
SKIPPED_DB_OFF = "direct_db_read_off"
CACHE_MAX_AGE = 3600.0  # a cached database section is recomputed at least this often (the day-window slides)
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")


# ---------------------------------------------------------------------------------------------- helpers
def default_root() -> Path:
    """The user's Hermes root (the DEFAULT home, whose ``profiles/`` holds the named ones).

    If ``HERMES_HOME`` is a profile (``<root>/profiles/<name>``) the root is two levels up.
    """
    raw = os.environ.get("HERMES_HOME")
    if not raw:
        raise RuntimeError("HERMES_HOME is not set")
    p = Path(raw)
    if p.parent.name == "profiles":
        return p.parent.parent
    return p


class NoDatabase(FileNotFoundError):
    """The file does not exist (a fresh profile has no ``state.db`` yet): callers treat it as 'no data', not as a failure."""


def _open_ro(path: Path) -> sqlite3.Connection:
    """Open a SQLite file strictly read-only. Raises ``NoDatabase`` (never creates the file) if it does not exist."""
    if not path.is_file():
        raise NoDatabase(path.name)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=DB_TIMEOUT)
    con.execute("pragma query_only = 1")
    return con


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # core dependency of Hermes

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except _EXPECTED_YAML:  # unreadable config = no info, not a crash
        return {}
    return data if isinstance(data, dict) else {}


def _sid(session_id: str) -> str:
    """Session ids never leave in clear: the same 16-hex SHA-256 prefix the world uses (sessions.engine_id)."""
    return hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:16]


def _guard(name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run one section; whatever happens it returns a dict with ``at`` and ``error``."""
    at = time.time()
    try:
        out = fn()
        out.setdefault("error", "")
        out["ok"] = not out["error"]
    except Exception as exc:  # one failing section must not hide the others; a bug leaves a traceback
        logger.warning("scan section %s failed", name, exc_info=True)
        out = {"ok": False, "error": f"{type(exc).__name__}: {clean_text(exc, 100)}"}
    out["at"] = at
    out["section"] = name
    return out


_HOME_MARKERS = ("config.yaml", "state.db", "SOUL.md")


def _looks_like_home(d: Path) -> bool:
    """A Hermes home has config.yaml/state.db once used, but a brand-new one (``hermes profile create``) has only
    SOUL.md and its folders, so SOUL.md counts too. Only file existence is tested; nothing is opened."""
    return any((d / m).is_file() for m in _HOME_MARKERS)


def profile_dirs(root: Path) -> list[tuple[str, Path]]:
    """``[(name, dir)]``: ``default`` (the root itself) first, then every ``profiles/<name>`` that looks like a home."""
    out: list[tuple[str, Path]] = []
    if _looks_like_home(root):
        out.append(("default", root))
    base = root / "profiles"
    if base.is_dir():
        for d in sorted(base.iterdir()):
            if d.is_dir() and _NAME.match(d.name) and _looks_like_home(d):
                out.append((d.name, d))
    return out


# ------------------------------------------------------------------------------------------------ agentes
def _gateway_running(pdir: Path, root: Path, name: str) -> bool | None:
    """Gateway liveness from files only (never signals, never unlinks): ``gateway.pid`` + the process exists,
    or the default gateway's ``gateway_state.json`` listing this profile as served and state ``running``."""
    try:
        raw = (pdir / "gateway.pid").read_text(encoding="utf-8").strip()
        pid = int(json.loads(raw).get("pid")) if raw.startswith("{") else int(raw.split()[0])
        if pid > 1:
            os.kill(pid, 0)
            return True
    except ProcessLookupError:
        pass
    except (OSError, ValueError, TypeError, AttributeError, IndexError):
        pass
    try:
        st = json.loads((root / "gateway_state.json").read_text(encoding="utf-8"))
        served = st.get("served_profiles")
        if st.get("gateway_state") == "running" and isinstance(served, list) and name in [str(s) for s in served]:
            pid = int(st.get("pid") or 0)
            if pid > 1:
                os.kill(pid, 0)
            return True
    except ProcessLookupError:
        return False
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return False


def _jobs_file(pdir: Path) -> list[dict[str, Any]]:
    p = pdir / "cron" / "jobs.json"
    if not p.is_file():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    jobs = data.get("jobs") if isinstance(data, dict) else data
    return [j for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else []


def scan_agentes(root: Path) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for name, pdir in profile_dirs(root):
        cfg = _read_yaml(pdir / "config.yaml")
        model = cfg.get("model")
        model_name = (model.get("default") if isinstance(model, dict) else model) or ""
        provider = model.get("provider") if isinstance(model, dict) else ""
        try:
            jobs = _jobs_file(pdir)
            n_jobs, n_active = len(jobs), sum(1 for j in jobs if j.get("enabled", True) and j.get("state") != "paused")
        except _EXPECTED:
            n_jobs = n_active = 0
        prof = _read_yaml(pdir / "profile.yaml")
        items.append(
            {
                "id": name,
                "nombre": clean_text(name, 40),
                "modelo": clean_text(model_name, 60),
                "proveedor": clean_text(provider or "", 30),
                "gateway": _gateway_running(pdir, root, name),
                "tiene_cron": n_jobs > 0,
                "cron_total": n_jobs,
                "cron_activos": n_active,
                "descripcion_len": len(str(prof.get("description") or "")),  # the text itself stays home
            }
        )
    return {"count": len(items), "items": items[:MAX_ITEMS]}


# --------------------------------------------------------------------------------------------------- cron
def scan_cron(root: Path) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    per_profile: dict[str, int] = {}
    errors: list[str] = []
    for name, pdir in profile_dirs(root):
        try:
            jobs = _jobs_file(pdir)
        except _EXPECTED as exc:
            errors.append(f"{name}: {type(exc).__name__}")
            continue
        per_profile[name] = len(jobs)
        for j in jobs:
            paused = j.get("state") == "paused" or j.get("paused_at") or j.get("enabled") is False
            sched = j.get("schedule_display") or ""
            items.append(
                {
                    "perfil": name,
                    "id": clean_text(j.get("id", ""), 40),
                    "nombre": clean_text(j.get("name", ""), 60),
                    "estado": "pausado" if paused else "programado",
                    "schedule": clean_text(sched, 60),
                    "ultimo": clean_text(j.get("last_status") or "", 20),
                    "proxima": clean_text(j.get("next_run_at") or "", 32),
                    "fallos_seguidos": int(j.get("failure_streak") or 0)
                    if str(j.get("failure_streak") or 0).lstrip("-").isdigit()
                    else 0,
                }
            )
    return {"count": len(items), "por_perfil": per_profile, "items": items[:MAX_ITEMS], "error": "; ".join(errors)[:200]}


# ----------------------------------------------------------------------------------------------- sesiones
def scan_sesiones(root: Path, days: int = DEFAULT_DAYS) -> dict[str, Any]:
    """Counts, source/model names and times only. Titles and message text are NOT selected."""
    since = time.time() - days * 86400
    per: dict[str, dict[str, Any]] = {}
    recent: list[dict[str, Any]] = []
    errors: list[str] = []
    for name, pdir in profile_dirs(root):
        try:
            con = _open_ro(pdir / "state.db")
        except NoDatabase:
            continue
        except _EXPECTED as exc:
            errors.append(f"{name}: {type(exc).__name__}")
            continue
        try:
            total = con.execute("select count(*) from sessions").fetchone()[0]
            row = con.execute(
                "select count(*), coalesce(sum(message_count),0), max(coalesce(last_activity_at, started_at)) "
                "from sessions where started_at >= ?",
                (since,),
            ).fetchone()
            per[name] = {"total": total, "ventana": row[0], "mensajes": row[1], "ultima": row[2]}
            for sid, src, model, started, ended, msgs in con.execute(
                "select id, source, model, started_at, ended_at, message_count from sessions "
                "order by coalesce(last_activity_at, started_at) desc limit 5"
            ):
                recent.append(
                    {
                        "perfil": name,
                        "id": _sid(sid),
                        "fuente": clean_text(src or "", 20),
                        "modelo": clean_text(model or "", 40),
                        "inicio": started,
                        "fin": ended,
                        "mensajes": msgs or 0,
                        "activa": ended is None,
                    }
                )
        except _EXPECTED as exc:
            errors.append(f"{name}: {type(exc).__name__}")
        finally:
            con.close()
    recent.sort(key=lambda r: r.get("inicio") or 0, reverse=True)
    return {
        "dias": days,
        "total": sum(v["total"] for v in per.values()),
        "ventana": sum(v["ventana"] for v in per.values()),
        "por_perfil": per,
        "recientes": recent[:MAX_ITEMS],
        "error": "; ".join(errors)[:200],
    }


# ------------------------------------------------------------------------------------------------- skills
SKILL_DESC = 140  # the card shows 140 characters of the description
SKILL_HEAD_BYTES = 4096  # only the front matter of a SKILL.md is read, never the skill's content
UNGROUPED = "general"


def _front_matter(path: Path) -> dict[str, Any]:
    """The YAML front matter (``name``, ``description``) of a SKILL.md: at most ``SKILL_HEAD_BYTES``, nothing after it."""
    with open(path, "rb") as fh:
        head = fh.read(SKILL_HEAD_BYTES).decode("utf-8", errors="replace")
    if not head.startswith("---"):
        return {}
    block = head[3:].split("\n---", 1)[0]
    try:
        import yaml

        data = yaml.safe_load(block)
    except _EXPECTED_YAML:
        return {}
    return data if isinstance(data, dict) else {}


def _disabled_skills(pdir: Path) -> set[str]:
    skills_cfg = _read_yaml(pdir / "config.yaml").get("skills")
    raw = skills_cfg.get("disabled") if isinstance(skills_cfg, dict) else None
    return {str(x) for x in raw} if isinstance(raw, list | tuple | set) else set()


def scan_skills(root: Path) -> dict[str, Any]:
    """Skills of every profile: counts, plus one item per DISTINCT name (group = the folder under ``skills/``,
    ``enabled`` = not listed in ``skills.disabled`` of at least one profile that has it, description cut to 140).
    Only the front matter is read; the skill's instructions never are."""
    per: dict[str, int] = {}
    by_name: dict[str, dict[str, Any]] = {}
    for name, pdir in profile_dirs(root):
        base = pdir / "skills"
        found = sorted(glob.glob(str(base / "**" / "SKILL.md"), recursive=True))
        per[name] = len(found)
        off = _disabled_skills(pdir)
        for f in found:
            path = Path(f)
            rel = path.relative_to(base).parts
            folder = path.parent.name
            try:
                meta = _front_matter(path)
                ok = True
            except OSError:
                meta, ok = {}, False
            skill = clean_text(meta.get("name") or folder, 60)
            group = clean_text(rel[0], 32) if len(rel) > 2 else UNGROUPED
            enabled = skill not in off and folder not in off
            cur = by_name.get(skill)
            if cur is None:
                by_name[skill] = {
                    "nombre": skill,
                    "grupo": group,
                    "activo": enabled,
                    "descripcion": clean_text(meta.get("description") or "", SKILL_DESC),
                    "legible": ok,
                }
            else:  # the same skill in another profile: on if it is on anywhere
                cur["activo"] = bool(cur["activo"] or enabled)
                cur["legible"] = bool(cur["legible"] or ok)
                if not cur["descripcion"]:
                    cur["descripcion"] = clean_text(meta.get("description") or "", SKILL_DESC)
    items = sorted(by_name.values(), key=lambda i: (i["grupo"], i["nombre"]))
    groups: dict[str, dict[str, int]] = {}
    for it in items:
        g = groups.setdefault(it["grupo"], {"total": 0, "activas": 0})
        g["total"] += 1
        g["activas"] += 1 if it["activo"] else 0
    return {
        "por_perfil": per,
        "distintas": len(by_name),
        "total": sum(per.values()),
        "nombres": sorted(by_name)[:MAX_ITEMS],
        "items": items[:MAX_ITEMS],
        "grupos": groups,
    }


# ----------------------------------------------------------------------------------------------------- mcp
def scan_mcp(root: Path) -> dict[str, Any]:
    """MCP server NAMES and transport kind per profile. Commands, args, URLs, env and tokens are never read out."""
    items: list[dict[str, Any]] = []
    for name, pdir in profile_dirs(root):
        servers = _read_yaml(pdir / "config.yaml").get("mcp_servers")
        if not isinstance(servers, dict):
            continue
        for sname, spec in servers.items():
            kind = (
                "http"
                if isinstance(spec, dict) and spec.get("url")
                else "stdio"
                if isinstance(spec, dict) and spec.get("command")
                else "?"
            )
            enabled = not (isinstance(spec, dict) and spec.get("enabled") is False)
            items.append({"perfil": name, "nombre": clean_text(sname, 40), "tipo": kind, "activo": enabled})
    return {"count": len(items), "items": items[:MAX_ITEMS]}


# ----------------------------------------------------------------------------------------------- kanban
KANBAN_COLUMNS = {
    "hacer": ("triage", "todo", "ready"),
    "curso": ("running", "blocked"),
    "hecho": ("done",),  # only what was finished inside the scan window
}
TOP_CARDS = 3  # the engine accepts at most 3 titles per column (esquema.ts LIMITES.tarjetasPorColumna)
MAX_CARDS = 99999


def _columns(con: sqlite3.Connection, days: int) -> dict[str, Any]:
    """The three columns of one board: ``{"hacer": {"n", "top"}, ...}``. Counts and titles only (never body, comments or results).

    Older boards may lack ``priority``/``completed_at``/``started_at``: each missing column just drops its refinement."""
    since = int(time.time()) - max(1, days) * 86400
    have = {str(r[1]) for r in con.execute("pragma table_info(tasks)")}

    def pick(*cands: str) -> str:
        return next((c for c in cands if c in have), "rowid")

    order = {
        "hacer": f"{pick('priority')} desc, {pick('created_at')} desc",
        "curso": f"{pick('started_at', 'created_at')} desc",
        "hecho": f"{pick('completed_at', 'created_at')} desc",
    }
    out: dict[str, Any] = {}
    for col, statuses in KANBAN_COLUMNS.items():
        marks = ",".join("?" for _ in statuses)
        windowed = col == "hecho" and "completed_at" in have
        extra = " and completed_at >= ?" if windowed else ""
        args: tuple[Any, ...] = (*statuses, since) if windowed else statuses
        # the only interpolated parts are constants of this module (status names, column names): no user text in the SQL
        n = con.execute(f"select count(*) from tasks where status in ({marks}){extra}", args).fetchone()[0]  # noqa: S608
        rows = con.execute(
            f"select title from tasks where status in ({marks}){extra} order by {order[col]} limit {TOP_CARDS}",  # noqa: S608
            args,
        ).fetchall()
        out[col] = {"n": min(int(n), MAX_CARDS), "top": [t for t in (fit_title(r[0], 32) for r in rows) if t]}
    return out


def scan_kanban(root: Path, days: int = DEFAULT_DAYS) -> dict[str, Any]:
    """Boards -> counts per status; card titles + status + assignee only (no body, comments, results).

    ``columnas`` is the work board the world shows (the one with most open cards): por hacer = triage/todo/ready,
    en curso = running/blocked, hecho = done inside ``days``; at most 3 redacted titles of 32 bytes per column."""
    boards: list[dict[str, Any]] = []
    errors: list[str] = []
    dbs: list[tuple[str, Path]] = []
    if (root / "kanban.db").is_file():
        dbs.append(("default", root / "kanban.db"))
    bdir = root / "kanban" / "boards"
    if bdir.is_dir():
        for d in sorted(bdir.iterdir()):
            if d.is_dir() and (d / "kanban.db").is_file() and _NAME.match(d.name):
                dbs.append((d.name, d / "kanban.db"))
    cards: list[dict[str, Any]] = []
    for bname, path in dbs:
        try:
            con = _open_ro(path)
        except _EXPECTED as exc:
            errors.append(f"{bname}: {type(exc).__name__}")
            continue
        try:
            by = {s: n for s, n in con.execute("select status, count(*) from tasks group by status")}
            boards.append({"id": bname, "tarjetas": sum(by.values()), "por_estado": by, "columnas": _columns(con, days)})
            for tid, title, status, assignee in con.execute(
                "select id, title, status, assignee from tasks "
                "where status not in ('done','archived') order by priority desc, created_at desc limit 25"
            ):
                cards.append(
                    {
                        "tablero": bname,
                        "id": clean_text(tid, 40),
                        "titulo": redact_title(title, 60),
                        "estado": clean_text(status, 20),
                        "asignado": clean_text(assignee or "", 30),
                    }
                )
        except _EXPECTED as exc:
            errors.append(f"{bname}: {type(exc).__name__}")
        finally:
            con.close()

    def _open(b: dict[str, Any]) -> int:
        return int(b["columnas"]["hacer"]["n"] + b["columnas"]["curso"]["n"])

    chosen = max(boards, key=_open) if boards else None  # first one wins a tie (max keeps the first maximum)
    return {
        "tableros": len(boards),
        "tablero": chosen["id"] if chosen else "",
        "columnas": chosen["columnas"] if chosen else {},
        "tarjetas": sum(b["tarjetas"] for b in boards),
        "items": boards[:MAX_ITEMS],
        "abiertas": cards[:MAX_ITEMS],
        "error": "; ".join(errors)[:200],
    }


# ------------------------------------------------------------------------------------------- proyectos
def scan_proyectos(root: Path) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    dbs = [(n, d / "projects.db") for n, d in profile_dirs(root) if (d / "projects.db").is_file()]
    seen: set[str] = set()
    for pname, path in dbs:
        try:
            con = _open_ro(path)
        except _EXPECTED as exc:
            errors.append(f"{pname}: {type(exc).__name__}")
            continue
        try:
            for slug, name, board, archived in con.execute("select slug, name, board_slug, archived from projects"):
                key = f"{slug}"
                if key in seen:
                    continue
                seen.add(key)
                items.append(
                    {
                        "perfil": pname,
                        "slug": clean_text(slug, 40),
                        "nombre": clean_text(name, 60),
                        "tablero": clean_text(board or "", 40),
                        "archivado": bool(archived),
                    }
                )
        except _EXPECTED as exc:
            errors.append(f"{pname}: {type(exc).__name__}")
        finally:
            con.close()
    return {"count": len(items), "items": items[:MAX_ITEMS], "error": "; ".join(errors)[:200]}


# ----------------------------------------------------------------------------------------- documentos
DOC_DIRS = ("plans", "notes", "memories", "handoffs")


def scan_documentos(root: Path, profile: str | None = None) -> dict[str, Any]:
    """File NAMES, sizes and mtimes of a profile's document folders (never contents)."""
    items: list[dict[str, Any]] = []
    for name, pdir in profile_dirs(root):
        if profile and name != profile:
            continue
        for sub in DOC_DIRS:
            d = pdir / sub
            if not d.is_dir():
                continue
            for f in sorted(d.rglob("*.md"))[:MAX_ITEMS]:
                try:
                    st = f.stat()
                except OSError:
                    continue
                items.append(
                    {"perfil": name, "carpeta": sub, "nombre": clean_text(f.name, 60), "bytes": st.st_size, "mtime": st.st_mtime}
                )
    items.sort(key=lambda i: i["mtime"], reverse=True)
    return {"count": len(items), "items": items[:MAX_ITEMS]}


# --------------------------------------------------------------------------------------------------- gasto
def scan_gasto(root: Path, days: int = DEFAULT_DAYS) -> dict[str, Any]:
    """Spend per profile over ``days``: tokens, estimated/actual cost, top models. Same numbers
    ``hermes insights`` derives from ``sessions`` (input/output/cache/reasoning tokens, *_cost_usd)."""
    since = time.time() - days * 86400
    per: list[dict[str, Any]] = []
    errors: list[str] = []
    models: dict[str, dict[str, float]] = {}
    for name, pdir in profile_dirs(root):
        try:
            con = _open_ro(pdir / "state.db")
        except NoDatabase:
            continue
        except _EXPECTED as exc:
            errors.append(f"{name}: {type(exc).__name__}")
            continue
        try:
            r = con.execute(
                "select count(*), coalesce(sum(input_tokens),0), coalesce(sum(output_tokens),0), "
                "coalesce(sum(cache_read_tokens),0), coalesce(sum(cache_write_tokens),0), coalesce(sum(reasoning_tokens),0), "
                "coalesce(sum(estimated_cost_usd),0), coalesce(sum(actual_cost_usd),0), coalesce(sum(api_call_count),0) "
                "from sessions where started_at >= ?",
                (since,),
            ).fetchone()
            top = []
            for model, n, tot, cost in con.execute(
                "select coalesce(model,'?'), count(*), coalesce(sum(input_tokens+output_tokens),0), "
                "coalesce(sum(coalesce(actual_cost_usd, estimated_cost_usd)),0) "
                "from sessions where started_at >= ? group by 1 order by 3 desc limit 5",
                (since,),
            ):
                top.append({"modelo": clean_text(model, 40), "sesiones": n, "tokens": tot, "coste_usd": round(cost or 0, 4)})
                m = models.setdefault(clean_text(model, 40), {"sesiones": 0, "tokens": 0, "coste_usd": 0.0})
                m["sesiones"] += n
                m["tokens"] += tot
                m["coste_usd"] += cost or 0
            per.append(
                {
                    "perfil": name,
                    "sesiones": r[0],
                    "entrada": r[1],
                    "salida": r[2],
                    "cache_lectura": r[3],
                    "cache_escritura": r[4],
                    "razonamiento": r[5],
                    "tokens": r[1] + r[2],
                    "coste_estimado_usd": round(r[6] or 0, 4),
                    "coste_real_usd": round(r[7] or 0, 4),
                    "llamadas": r[8],
                    "modelos": top,
                }
            )
        except _EXPECTED as exc:
            errors.append(f"{name}: {type(exc).__name__}")
        finally:
            con.close()
    ranked = sorted(models.items(), key=lambda kv: kv[1]["tokens"], reverse=True)[:5]
    top_models = [{"modelo": k, **{a: (round(b, 4) if a == "coste_usd" else b) for a, b in v.items()}} for k, v in ranked]
    return {
        "dias": days,
        "por_perfil": per,
        "top_modelos": top_models,
        "tokens": sum(p["tokens"] for p in per),
        "sesiones": sum(p["sesiones"] for p in per),
        "coste_estimado_usd": round(sum(p["coste_estimado_usd"] for p in per), 4),
        "coste_real_usd": round(sum(p["coste_real_usd"] for p in per), 4),
        "error": "; ".join(errors)[:200],
    }


# ---------------------------------------------------------------------------------------- change signal
def _db_paths(root: Path, section: str) -> list[Path]:
    """The database files a section would open (no I/O beyond listing the profile folders)."""
    if section in ("sesiones", "gasto"):
        return [d / "state.db" for _n, d in profile_dirs(root)]
    if section in ("proyectos", "arbol"):
        return [d / "projects.db" for _n, d in profile_dirs(root)]
    if section == "kanban":
        paths = [root / "kanban.db"]
        bdir = root / "kanban" / "boards"
        if bdir.is_dir():
            paths += [d / "kanban.db" for d in sorted(bdir.iterdir()) if d.is_dir() and _NAME.match(d.name)]
        return paths
    return []


def db_signature(root: Path, section: str) -> tuple[Any, ...]:
    """A cheap fingerprint of a section's databases: ``os.stat`` of each file AND of its ``-wal`` (size, mtime_ns).

    No database is opened. In WAL mode a commit is appended to ``<db>-wal`` and the main file may not change for a long
    time, so the WAL is part of the signature. ``()`` for sections that read no database."""
    out: list[tuple[str, int, int]] = []
    for path in _db_paths(root, section):
        for f in (path, path.with_name(path.name + "-wal")):
            try:
                st = f.stat()
            except OSError:
                continue
            out.append((str(f), st.st_size, st.st_mtime_ns))
    return tuple(out)


def _tree(base: Path) -> dict[str, Any]:
    """The ``arbol`` section is the C7 shelf-tree (``proyectos.read_project_tree``); ``proyectos`` stays the flat summary."""
    from .proyectos import read_project_tree  # lazy: proyectos imports this module

    return read_project_tree(base)


def _plugins(base: Path) -> dict[str, Any]:
    """The ``enchufes`` section is the C9 installed-plugins reader (``enchufes.read_plugins``): files only."""
    from .enchufes import read_plugins  # lazy: enchufes imports this module

    return read_plugins(base)


def _messaging(base: Path) -> dict[str, Any]:
    """The ``mensajeria`` section is the C10 messaging-platform status reader (``mensajeria.read_messaging``): files only."""
    from .mensajeria import read_messaging  # lazy: mensajeria imports this module

    return read_messaging(base)


# --------------------------------------------------------------------------------------------- the scan
def scan_hermes(
    root: Path | str | None = None,
    *,
    days: int = DEFAULT_DAYS,
    only: tuple[str, ...] | None = None,
    allow_direct_db: bool = False,
    cache: dict[str, Any] | None = None,
    api: Callable[..., Any] | None = None,
    session_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Run every section (``only=None``), some of them, or none (``only=()``) and return the typed snapshot. Never raises.

    ``allow_direct_db`` (default False) is the user's opt-in to read Hermes' SQLite files; without it the sections in
    :data:`DB_SECTIONS` are reported as skipped and no database is opened. ``cache`` (any dict kept by the caller) lets a
    database section be reused while its files' :func:`db_signature` and the window are unchanged and the entry is younger
    than :data:`CACHE_MAX_AGE`.

    ``api`` (default None = off, so tests and ``palace scan`` never start a process by accident) is the first layer for the
    ``kanban`` and ``sesiones`` sections: the module ``hermes_api`` (or a stand-in with the same two functions
    ``kanban_via_cli(days)`` and ``sessions_via_api(profiles, ids)``). It uses Hermes' own CLI / ``SessionDB``; only when it cannot
    answer, and only with ``allow_direct_db``, does the direct SQL read below run."""
    t0 = time.time()
    try:
        base = Path(root) if root is not None else default_root()
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        base = Path("/nonexistent-hermes-root")
        err0 = f"{type(exc).__name__}: {clean_text(exc, 80)}"
    else:
        err0 = ""
    runners: dict[str, Callable[[], dict[str, Any]]] = {
        "agentes": lambda: scan_agentes(base),
        "cron": lambda: scan_cron(base),
        "sesiones": lambda: scan_sesiones(base, days),
        "skills": lambda: scan_skills(base),
        "mcp": lambda: scan_mcp(base),
        "kanban": lambda: scan_kanban(base, days),
        "proyectos": lambda: scan_proyectos(base),
        "arbol": lambda: _tree(base),
        "enchufes": lambda: _plugins(base),
        "mensajeria": lambda: _messaging(base),
        "documentos": lambda: scan_documentos(base),
        "gasto": lambda: scan_gasto(base, days),
    }
    wanted = [s for s in SECTIONS if only is None or s in only]
    sections: dict[str, dict[str, Any]] = {}
    for name in wanted:
        if name not in DB_SECTIONS:
            sections[name] = _guard(name, runners[name])
            continue
        via_api = None
        if api is not None and name in ("kanban", "sesiones"):
            via_api = _guard(name, partial(_first_layer, name, api, base, days, session_ids or []))
        answered = via_api is not None and bool(via_api.get("ok")) and not via_api.get("empty")
        if answered and name == "kanban" and via_api and via_api.get("tarjetas") == 0 and allow_direct_db:
            answered = False  # an empty board from the CLI may be another board layout than the file we were told to read
        if via_api is not None and answered:
            sections[name] = via_api
        elif not allow_direct_db:
            sections[name] = {"ok": True, "error": "", "skipped": SKIPPED_DB_OFF, "at": t0, "section": name}
        else:
            sections[name] = _cached_db_section(name, runners[name], base, days, cache, t0)
    return {
        "schema": SCHEMA,
        "at": t0,
        "root_name": base.name,
        "took_ms": int((time.time() - t0) * 1000),
        "error": err0,
        "sections": sections,
    }


def _first_layer(name: str, api: Any, base: Path, days: int, session_ids: list[str]) -> dict[str, Any]:
    """One section from Hermes' own interfaces; ``{"empty": True}`` when they cannot answer (the caller then falls back)."""
    if name == "kanban":
        out = api.kanban_via_cli(days)
    elif name == "sesiones":
        out = api.sessions_via_api(profile_dirs(base), session_ids)
    else:
        out = None
    return out if isinstance(out, dict) else {"empty": True, "error": ""}


def _cached_db_section(
    name: str, run: Callable[[], dict[str, Any]], base: Path, days: int, cache: dict[str, Any] | None, now: float
) -> dict[str, Any]:
    if cache is None:
        return _guard(name, run)
    sig = (days, db_signature(base, name))
    hit = cache.get(name)
    if hit is not None and hit["sig"] == sig and 0 <= now - hit["at"] < CACHE_MAX_AGE:
        return {**hit["section"], "from_cache": True}
    section = _guard(name, run)
    if section.get("ok"):  # a failure is retried at the next scan
        cache[name] = {"sig": sig, "at": time.time(), "section": section}
    return section


def summary(snap: dict[str, Any]) -> dict[str, Any]:
    """Counts-only digest (what the report and the chip show)."""
    s = snap.get("sections", {})

    def g(sec: str, key: str, default: Any = 0) -> Any:
        return s.get(sec, {}).get(key, default)

    return {
        "agentes": g("agentes", "count"),
        "cron": g("cron", "count"),
        "sesiones_total": g("sesiones", "total"),
        "sesiones_ventana": g("sesiones", "ventana"),
        "skills_distintas": g("skills", "distintas"),
        "mcp": g("mcp", "count"),
        "tableros": g("kanban", "tableros"),
        "tarjetas": g("kanban", "tarjetas"),
        "proyectos": g("proyectos", "count"),
        "documentos": g("documentos", "count"),
        "tokens": g("gasto", "tokens"),
        "coste_estimado_usd": g("gasto", "coste_estimado_usd"),
        "secciones_con_error": [k for k, v in s.items() if v.get("error")],
    }
