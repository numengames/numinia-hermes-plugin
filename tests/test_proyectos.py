"""C7: the project shelf-tree reader. Synthetic homes only; the run against the real Hermes is C7-evidence/run_c7.py."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from hermes_3d_lib import proyectos


def _db(path: Path, projects: list[tuple], folders: list[tuple] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(
        """create table projects (id text primary key, slug text not null unique, name text not null, description text,
           icon text, color text, board_slug text, primary_path text, created_at integer not null,
           archived integer not null default 0);
           create table project_folders (project_id text not null, path text not null, label text,
           is_primary integer not null default 0, added_at integer not null, primary key (project_id, path));"""
    )
    con.executemany("insert into projects values (?,?,?,?,?,?,?,?,?,?)", projects)
    con.executemany("insert into project_folders values (?,?,?,?,?)", folders or [])
    con.commit()
    con.close()


def _home(tmp_path: Path) -> Path:
    root = tmp_path / ".hermes"
    (root / "profiles" / "alpha").mkdir(parents=True)
    (root / "config.yaml").write_text("{}\n")
    (root / "profiles" / "alpha" / "config.yaml").write_text("{}\n")
    _db(
        root / "profiles" / "alpha" / "projects.db",
        [
            ("p1", "web", "My Website", "SECRET DESCRIPTION", None, None, "web-board", "/home/u/web", 2, 0),
            ("p2", "old", "Old Thing", None, None, None, None, "/home/u/old", 1, 1),
        ],
        [
            ("p1", "/home/u/web", None, 1, 1),
            ("p1", "/home/u/docs\\site", "Docs", 0, 2),
            ("p2", "/home/u/old", None, 1, 1),
        ],
    )
    return root


def test_tree_has_real_names_folders_and_groups(tmp_path):
    t = proyectos.read_project_tree(_home(tmp_path))
    assert t["schema"] == proyectos.SCHEMA and t["error"] == ""
    assert (t["count"], t["active"], t["archived"]) == (2, 1, 1)
    by = {p["slug"]: p for p in t["projects"]}
    assert by["web"]["name"] == "My Website" and by["web"]["board"] == "web-board" and by["web"]["profile"] == "alpha"
    assert [f["label"] for f in by["web"]["folders"]] == ["web", "Docs"]  # label, else last path segment
    assert by["old"]["archived"] is True
    assert [g["name"] for g in t["groups"]] == ["Active", "Archived"]
    assert t["label"] == "Projects"


def test_no_path_and_no_description_leave_the_reader(tmp_path):
    blob = json.dumps(proyectos.read_project_tree(_home(tmp_path)))
    assert "/home/u" not in blob and "SECRET DESCRIPTION" not in blob


def test_reader_never_writes(tmp_path):
    root = _home(tmp_path)
    db = root / "profiles" / "alpha" / "projects.db"
    before = (hashlib.sha256(db.read_bytes()).hexdigest(), db.stat().st_mtime_ns)
    proyectos.read_project_tree(root)
    assert (hashlib.sha256(db.read_bytes()).hexdigest(), db.stat().st_mtime_ns) == before
    assert sorted(p.name for p in db.parent.iterdir()) == ["config.yaml", "projects.db"]  # no -wal/-journal left
    # and the connection it uses refuses writes
    from hermes_3d_lib.scan import _open_ro

    con = _open_ro(db)
    try:
        try:
            con.execute("delete from projects")
            raised = False
        except sqlite3.Error:
            raised = True
    finally:
        con.close()
    assert raised


def test_missing_or_broken_database_does_not_raise(tmp_path):
    root = tmp_path / ".hermes"
    (root / "profiles" / "beta").mkdir(parents=True)
    (root / "config.yaml").write_text("{}\n")
    (root / "profiles" / "beta" / "config.yaml").write_text("{}\n")
    t = proyectos.read_project_tree(root)
    assert t["count"] == 0 and t["projects"] == [] and t["groups"] == []
    (root / "profiles" / "beta" / "projects.db").write_bytes(b"not a database at all, just text")
    t = proyectos.read_project_tree(root)
    assert t["count"] == 0 and t["error"].startswith("beta:")


def test_older_schema_without_folders_table(tmp_path):
    root = _home(tmp_path)
    con = sqlite3.connect(root / "profiles" / "alpha" / "projects.db")
    con.execute("drop table project_folders")
    con.commit()
    con.close()
    t = proyectos.read_project_tree(root)
    assert t["count"] == 2 and all(p["folders"] == [] for p in t["projects"])


# ------------------------------------------------------------------------------------------ C7 zone + figure
def test_project_objects_are_english_real_names_in_zone_z5(tmp_path):
    objs = proyectos.project_objects(proyectos.read_project_tree(_home(tmp_path)))
    assert set(objs) == {"p-web", "p-old"}
    web, old = objs["p-web"], objs["p-old"]
    assert (web["tipo"], web["titulo"], web["zona"], web["estado"]) == ("proyecto", "My Website", "z5", "activo")
    assert web["grupo"] == "Active" and web["descripcion"] == "Folders: web, Docs"
    assert web["subtitulo"] == "Active \u00b7 2 folders \u00b7 board web-board"
    assert (old["titulo"], old["estado"], old["grupo"]) == ("Old Thing", "apagado", "Archived")
    assert old["subtitulo"] == "Archived \u00b7 1 folder" and old["descripcion"] == "Folders: old"
    for o in objs.values():  # fits the engine's limits (bytes) and offers no action: read-only
        assert len(o["titulo"].encode()) <= 60 and len(o["subtitulo"].encode()) <= 120 and len(o["descripcion"].encode()) <= 200
        assert o["acciones"] == []
    blob = json.dumps(objs)
    assert "/home/u" not in blob and "SECRET DESCRIPTION" not in blob


def test_project_objects_empty_or_failed_section():
    assert proyectos.project_objects({}) == {} and proyectos.project_objects({"projects": []}) == {}


def test_bridge_publishes_projects_only_when_the_function_is_on(tmp_path):
    import asyncio

    from hermes_3d_lib import bridge_main
    from hermes_3d_lib import settings as S

    root = _home(tmp_path)

    def make(on):
        b = bridge_main.Bridge(tmp_path / "bh", engine_url="ws://127.0.0.1:1", link_key="k", scan_root=root, allow_direct_db=True)
        b.hub.engine_functions = {n: (n in on) for n in S.FUNCTIONS}
        b.hub.switches = {n: True for n in S.FUNCTIONS}
        return b

    off = make(["salud"])
    asyncio.run(off.run_scan())
    assert off.mirror_objects() == {}
    on = make(["salud", "proyectos"])
    asyncio.run(on.run_scan())
    assert set(on.mirror_objects()) == {"proyecto/p-web", "proyecto/p-old"}
