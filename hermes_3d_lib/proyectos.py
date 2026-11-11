"""Project shelf-tree reader (C7): the projects of every Hermes profile, read from ``projects.db``.

Source: ``<home>/projects.db`` of the default home and of every ``profiles/<name>`` (Hermes' private schema:
``projects`` + ``project_folders``). Strictly READ-ONLY: the file is opened ``mode=ro`` with ``query_only`` (never
created, never written), through ``scan._open_ro``.

The tree has three levels::

    shelf (root) -> project (real name) -> folder (label, or the last path segment)

What leaves this module: project name, slug, board slug, archived flag, folder labels. NEVER a full path, the
project description, or anything else from the file. Every string is cleaned and cut like the rest of the plugin.
Player-visible group names are English.

``read_project_tree(root)`` returns a plain, JSON-serialisable dict::

    {"schema": "hermes-3d/projects@1", "label": "Projects", "count": n, "active": a, "archived": b,
     "projects": [{"id", "slug", "name", "board", "archived", "profile", "folders": [{"label"}]}],
     "groups": [{"name": "Active", "slugs": [...]}, {"name": "Archived", "slugs": [...]}],
     "error": ""}
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from .crontable import clean_text
from .protocol import make_object
from .scan import _EXPECTED, NoDatabase, _open_ro, profile_dirs
from .zones import FALLBACK_ZONES

SCHEMA = "hermes-3d/projects@1"
LABEL = "Projects"  # player-visible: English
GROUP_ACTIVE = "Active"
GROUP_ARCHIVED = "Archived"
MAX_PROJECTS = 200  # the world admits 200 things per zone
MAX_FOLDERS = 20  # per project
NAME_CARD = 60
FOLDER_CARD = 40
_SEP = re.compile(r"[\\/]+")


def folder_label(label: Any, path: Any) -> str:
    """The folder's own label, else the last segment of its path. Never the whole path."""
    text = clean_text(label, FOLDER_CARD)
    if text:
        return text
    parts = [p for p in _SEP.split(str(path or "")) if p]
    return clean_text(parts[-1], FOLDER_CARD) if parts else ""


def _read_one(path: Path, pname: str) -> list[dict[str, Any]]:
    con = _open_ro(path)
    try:
        rows = con.execute(
            "select id, slug, name, board_slug, archived from projects order by archived, created_at, name"
        ).fetchall()
        folders: dict[str, list[dict[str, str]]] = {}
        try:
            for pid, label, fpath in con.execute(
                "select project_id, label, path from project_folders order by is_primary desc, added_at, path"
            ):
                lab = folder_label(label, fpath)
                if lab:
                    folders.setdefault(str(pid), []).append({"label": lab})
        except sqlite3.OperationalError:  # an older Hermes without project_folders: projects only
            folders = {}
        out = []
        for pid, slug, name, board, archived in rows:
            nm = clean_text(name, NAME_CARD)
            if not nm:
                continue
            out.append(
                {
                    "id": clean_text(pid, 40),
                    "slug": clean_text(slug, 40),
                    "name": nm,
                    "board": clean_text(board or "", 40),
                    "archived": bool(archived),
                    "profile": pname,
                    "folders": folders.get(str(pid), [])[:MAX_FOLDERS],
                }
            )
        return out
    finally:
        con.close()


def read_project_tree(root: Path) -> dict[str, Any]:
    """The project tree of every profile under ``root`` (a Hermes home). Never raises for a missing/odd database."""
    projects: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for pname, pdir in profile_dirs(Path(root)):
        db = pdir / "projects.db"
        if not db.is_file():
            continue
        try:
            found = _read_one(db, pname)
        except NoDatabase:
            continue
        except _EXPECTED as exc:
            errors.append(f"{pname}: {type(exc).__name__}")
            continue
        for p in found:
            key = p["slug"] or p["id"]
            if key in seen:
                continue
            seen.add(key)
            projects.append(p)
    projects = projects[:MAX_PROJECTS]
    active = [p["slug"] or p["id"] for p in projects if not p["archived"]]
    archived = [p["slug"] or p["id"] for p in projects if p["archived"]]
    groups = [g for g in ({"name": GROUP_ACTIVE, "slugs": active}, {"name": GROUP_ARCHIVED, "slugs": archived}) if g["slugs"]]
    return {
        "schema": SCHEMA,
        "label": LABEL,
        "count": len(projects),
        "active": len(active),
        "archived": len(archived),
        "projects": projects,
        "groups": groups,
        "error": "; ".join(errors)[:200],
    }


TYPE_PROYECTO = "proyecto"  # the engine's type for a project folder (function ``proyectos``)
ZONE_PROYECTO = FALLBACK_ZONES[TYPE_PROYECTO]  # z5; the hub swaps in the zone the engine announces
_ID_BAD = re.compile(r"[^a-z0-9._-]")


def project_objects(section: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``read_project_tree`` result -> id -> world object, one ``proyecto`` per project, texts in English.

    Title = the real project name; ``grupo`` = Active / Archived (the shelf); ``descripcion`` = the folder labels;
    subtitle = status, folder count and board. Archived projects are ``apagado`` (dashed folder in the world).
    """
    out: dict[str, dict[str, Any]] = {}
    for p in (section or {}).get("projects") or []:
        key = _ID_BAD.sub("-", str(p.get("slug") or p.get("id") or "").lower())[:30].strip("-._") or "x"
        pid = "p-" + key
        if pid in out:
            continue
        archived = bool(p.get("archived"))
        folders = [f["label"] for f in p.get("folders") or [] if f.get("label")]
        n = len(folders)
        sub = f"{GROUP_ARCHIVED if archived else GROUP_ACTIVE} \u00b7 {n} folder{'s' if n != 1 else ''}"
        if p.get("board"):
            sub += f" \u00b7 board {p['board']}"
        out[pid] = make_object(
            TYPE_PROYECTO,
            pid,
            str(p.get("name") or ""),
            subtitulo=sub,
            estado="apagado" if archived else "activo",
            zona=ZONE_PROYECTO,
            acciones=[],
            grupo=GROUP_ARCHIVED if archived else GROUP_ACTIVE,
            descripcion=("Folders: " + ", ".join(folders)) if folders else "No folders registered",
        )
    return out
