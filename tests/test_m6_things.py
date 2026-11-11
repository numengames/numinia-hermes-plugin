"""M6 things: habilidad (skills), tablero (kanban), documento (docs pages), guia pass-through."""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time

import pytest
from conftest import LOOP

from hermes_3d_lib import documentos, habilidades, logsafe, scan, tablero
from hermes_3d_lib import settings as S
from hermes_3d_lib.protocol import MAX_TITLE, MAX_URL, OBJECT_KEYS, make_object

SECRET = "sk-" + "a1b2c3d4e5f6g7h8i9j0"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")


def valid_object(o):
    """The engine's ``leerObjeto`` shape, as far as it can be checked here: only its keys, byte limits, an id it accepts."""
    return (
        set(o) <= set(OBJECT_KEYS)
        and bool(ID_RE.match(o["id"]))
        and len(o["titulo"].encode()) <= MAX_TITLE
        and len(o["subtitulo"].encode()) <= 120
    )


# ---------------------------------------------------------------------------------------------- habilidades
def _skill(root, profile, rel, text):
    d = (root / "profiles" / profile if profile else root) / "skills" / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")


def test_scan_skills_reads_front_matter_only_and_groups(fake_root):
    long = "x" * 400
    _skill(fake_root, "alpha", "devops/deploy-it", f"---\nname: deploy-it\ndescription: {long}\n---\nSECRET BODY {SECRET}\n")
    _skill(fake_root, "alpha", "flat-one", "---\nname: flat-one\ndescription: flat\n---\n")
    _skill(fake_root, "alpha", "devops/broken", "---\nname: [unclosed\n---\n")
    _skill(fake_root, "alpha", "devops/nofm", "no front matter at all\n")
    (fake_root / "profiles" / "alpha" / "config.yaml").write_text("skills:\n  disabled: [flat-one]\n", encoding="utf-8")
    sk = scan.scan_skills(fake_root)
    by = {i["nombre"]: i for i in sk["items"]}
    assert by["deploy-it"]["grupo"] == "devops" and by["deploy-it"]["activo"] is True
    assert len(by["deploy-it"]["descripcion"]) <= 140 and by["flat-one"]["grupo"] == "general"
    assert by["flat-one"]["activo"] is False
    assert SECRET not in str(sk) and "SECRET BODY" not in str(sk)
    assert sk["grupos"]["devops"]["total"] >= 3 and sk["grupos"]["devops"]["activas"] >= 3
    assert sk["distintas"] == len(sk["items"]) and sk["total"] >= sk["distintas"]


def test_skill_enabled_anywhere_counts_as_enabled(fake_root):
    _skill(fake_root, "alpha", "g/dup", "---\nname: dup\ndescription: a\n---\n")
    _skill(fake_root, "beta", "g/dup", "---\nname: dup\ndescription: b\n---\n")
    (fake_root / "profiles" / "alpha" / "config.yaml").write_text("skills:\n  disabled: [dup]\n", encoding="utf-8")
    item = next(i for i in scan.scan_skills(fake_root)["items"] if i["nombre"] == "dup")
    assert item["activo"] is True and item["descripcion"] == "a"


def test_slug_and_unique_ids():
    assert habilidades.slug("My Skill!/ v2") == "my-skill-v2"
    assert habilidades.slug("???") == "" and len(habilidades.slug("a" * 80)) <= 32
    ids = habilidades.unique_ids(["A b", "a-b", "a_b", "???", "!!!", "x" * 60, "x" * 61])
    assert len(set(ids.values())) == 7 and all(ID_RE.match(v) for v in ids.values())
    assert habilidades.unique_ids(["A b", "a-b"]) == habilidades.unique_ids(["a-b", "A b"])  # stable


def test_unique_ids_extends_when_a_short_hash_collides(monkeypatch):
    real = habilidades.hashlib.sha256
    calls = {"n": 0}

    class Same:
        def __init__(self, data):
            calls["n"] += 1
            self._h = real(b"same" if calls["n"] <= 2 else data)

        def hexdigest(self):
            return self._h.hexdigest()

    monkeypatch.setattr(habilidades.hashlib, "sha256", Same)
    ids = habilidades.unique_ids(["a b", "a-b", "a_b"])
    assert len(set(ids.values())) == 3 and all(ID_RE.match(v) for v in ids.values())


def _items(n, group="g"):
    return [{"nombre": f"skill {i}", "grupo": group, "activo": True, "descripcion": "d", "legible": True} for i in range(n)]


def test_snapshot_objects_validate_and_states():
    items = [
        *_items(2),
        {"nombre": "off", "grupo": "g", "activo": False, "descripcion": "", "legible": True},
        {"nombre": "bad", "grupo": "g", "activo": True, "descripcion": "", "legible": False},
        {"grupo": "no name"},
        "junk",
    ]
    snap = habilidades.snapshot_from_scan({"items": items})
    assert len(snap) == 4
    for o in snap.values():
        assert o["tipo"] == "habilidad" and o["zona"] == "z4" and o["acciones"] == [] and valid_object(o)
        assert 1 <= len(o["grupo"].encode()) <= 32 and len(o["descripcion"].encode()) <= 200
    assert {o["estado"] for o in snap.values()} == {"activo", "apagado", "fallo"}
    assert habilidades.snapshot_from_scan({}) == {} and habilidades.snapshot_from_scan(None) == {}  # type: ignore[arg-type]


def test_groups_are_folded_to_at_most_eight():
    items = []
    for g in range(12):
        items += _items(12 - g, group=f"grp{g:02d}")
    snap = habilidades.snapshot_from_scan({"items": [dict(i, nombre=f"{i['grupo']}-{k}") for k, i in enumerate(items)]})
    groups = {o["grupo"] for o in snap.values()}
    assert len(groups) == 8 and "other" in groups and "grp00" in groups and "grp11" not in groups


def test_snapshot_caps_at_the_engine_limit_and_survives_an_odd_item(monkeypatch):
    snap = habilidades.snapshot_from_scan({"items": _items(260)})
    assert len(snap) == 200
    real = habilidades.make_object

    def boom(*a, **k):
        if a[2] == "skill 1":
            raise ValueError("odd")
        return real(*a, **k)

    monkeypatch.setattr(habilidades, "make_object", boom)
    assert len(habilidades.snapshot_from_scan({"items": _items(3)})) == 2


# ------------------------------------------------------------------------------------------------ tablero
def _kanban(root, rows, board="board-x", full=True):
    kb = root / "kanban" / "boards" / board
    kb.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(kb / "kanban.db")
    cols = "id text, title text, status text, assignee text, priority integer, created_at integer, started_at integer, completed_at integer, body text, result text"
    con.execute(f"create table tasks ({cols})" if full else "create table tasks (id text, title text, status text)")
    for r in rows:
        con.execute(
            "insert into tasks values (?,?,?,?,?,?,?,?,?,?)" if full else "insert into tasks values (?,?,?)", r if full else r[:3]
        )
    con.commit()
    con.close()


def test_kanban_columns_counts_titles_and_redaction(tmp_path):
    root = tmp_path / ".hermes"
    (root / "profiles").mkdir(parents=True)
    now = int(time.time())
    rows = [
        ("1", f"Fix login for bob@example.com {SECRET}", "todo", "a", 5, now, None, None, "BODY-SECRET", "RESULT-SECRET"),
        ("2", 'He said "hi" \\ there', "ready", None, 9, now, None, None, "b", None),
        ("3", "triaged", "triage", None, 0, now, None, None, "b", None),
        ("4", "four", "todo", None, 0, now - 5, None, None, "b", None),
        ("5", "running one", "running", "b", 0, now, now, None, "b", None),
        ("6", "blocked one", "blocked", None, 0, now, None, None, "b", None),
        ("7", "recent done", "done", None, 0, now, now, now - 60, "b", None),
        ("8", "ancient done", "done", None, 0, now - 400 * 86400, None, now - 399 * 86400, "b", None),
        ("9", "archived", "archived", None, 0, now, None, None, "b", None),
    ]
    _kanban(root, rows)
    k = scan.scan_kanban(root, days=30)
    cols = k["columnas"]
    assert k["tablero"] == "board-x"
    assert cols["hacer"]["n"] == 4 and len(cols["hacer"]["top"]) == 3
    assert cols["curso"]["n"] == 2 and cols["hecho"] == {"n": 1, "top": ["recent done"]}
    blob = str(k)
    for bad in (SECRET, "bob@example.com", "BODY-SECRET", "RESULT-SECRET"):
        assert bad not in blob
    assert cols["hacer"]["top"][0] != "" and all('"' not in t and "\\" not in t for c in cols.values() for t in c["top"])


def test_kanban_old_schema_and_no_board(tmp_path):
    root = tmp_path / ".hermes"
    (root / "profiles").mkdir(parents=True)
    assert scan.scan_kanban(root)["columnas"] == {} and scan.scan_kanban(root)["tablero"] == ""
    _kanban(
        root,
        [("1", "old schema", "todo", None, 0, 0, None, None, "", None), ("2", "d", "done", None, 0, 0, None, None, "", None)],
        full=False,
    )
    cols = scan.scan_kanban(root)["columnas"]
    assert cols["hacer"]["n"] == 1 and cols["hecho"]["n"] == 1


def test_kanban_picks_the_board_with_most_open_cards(tmp_path):
    root = tmp_path / ".hermes"
    (root / "profiles").mkdir(parents=True)
    _kanban(root, [("1", "a", "todo", None, 0, 0, None, None, "", None)], board="small")
    _kanban(root, [(str(i), "b", "running", None, 0, 0, None, None, "", None) for i in range(3)], board="big")
    assert scan.scan_kanban(root)["tablero"] == "big"


def test_redact_title():
    assert logsafe.redact_title("  a\n\tb  ") == "a b"
    assert logsafe.redact_title("word " * 30, 32) == ("word " * 30)[:32].strip()
    assert len(logsafe.redact_title("ñ " * 40, 32).encode()) <= 32
    for secret in (
        SECRET,
        "https://u:pw@host/x",
        "a@b.co",
        "eyJhbGciOiJI.eyJzdWIiOiIx.sig",
        "0123456789abcdef0123456789",
        "A" * 40,
    ):
        out = logsafe.redact_title(f"see {secret} now", 80)
        assert secret not in out
    assert logsafe.redact_title(None) == ""  # type: ignore[arg-type]
    assert logsafe.redact_title("\x00\x07") == ""


def test_tablero_object_follows_the_contract():
    section = {
        "tablero": "b",
        "columnas": {
            "hacer": {"n": 120000, "top": ["a", "b", "c", "d", ""]},
            "curso": {"n": True, "top": "nope"},
            "hecho": {"n": -4, "top": ['q"x', "y\\z"], "extra": 1},
        },
    }
    snap = tablero.snapshot_from_scan(section)
    assert list(snap) == ["trabajo"]
    o = snap["trabajo"]
    assert o["tipo"] == "tablero" and o["zona"] == "z9" and o["acciones"] == [] and valid_object(o)
    assert o["tablero"] == {
        "hacer": {"n": 99999, "top": ["a", "b", "c"]},
        "curso": {"n": 0, "top": []},
        "hecho": {"n": 0, "top": ["q'x", "y'z"]},
    }
    assert tablero.snapshot_from_scan({}) == {} and tablero.snapshot_from_scan({"tablero": "b", "columnas": 3}) == {}
    assert tablero.board_field(None) == {c: {"n": 0, "top": []} for c in tablero.COLUMNS}  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------- documentos
def test_documentos_urls_ids_and_origin():
    snap = documentos.snapshot()
    assert 5 <= len(snap) <= 20
    for id_, o in snap.items():
        assert ID_RE.match(id_) and o["tipo"] == "documento" and o["zona"] == "z1" and o["acciones"] == []
        assert o["url"].startswith(f"http://{LOOP}:8791/docs/") and len(o["url"].encode()) <= MAX_URL
        assert len(o["titulo"].encode()) <= 60 and valid_object(o)
    assert documentos.snapshot(9000)["quickstart"]["url"] == f"http://{LOOP}:9000/docs/getting-started/quickstart/"
    assert documentos.origin(9000) == f"http://{LOOP}:9000"


def test_documentos_skips_an_url_over_the_limit(monkeypatch):
    monkeypatch.setattr(documentos, "page_url", lambda path, port=0: f"http://{LOOP}:1/" + "x" * 400)
    assert documentos.snapshot() == {}


def test_make_object_accepts_the_m6_fields_and_cuts_oversize():
    o = make_object("habilidad", "s", "S", estado="activo", zona="z4", grupo="g", descripcion="d")
    assert o["grupo"] == "g" and o["descripcion"] == "d"
    o = make_object("documento", "d", "D", estado="activo", zona="z1", url=f"http://{LOOP}:8791/x")
    assert o["url"] == f"http://{LOOP}:8791/x"
    o = make_object("habilidad", "s", "S", grupo="g" * 60, descripcion="é" * 300)
    assert len(o["grupo"].encode()) <= 32 and len(o["descripcion"].encode()) <= 200


# ------------------------------------------------------------------------------------- bridge and settings
VERSION_KEYS = {"salud/version"}  # the always-on card, not part of any function (the deadline card is opt-in)


def _bridge(fake_root, tmp_path, on, clock=False):
    from hermes_3d_lib import bridge_main

    b = bridge_main.Bridge(
        tmp_path / "home",
        engine_url=f"ws://{LOOP}:1",
        link_key="k",
        scan_root=fake_root,
        allow_direct_db=True,
        docs_port=8800,
        deadline_clock=clock,
    )
    b.hub.engine_functions = {n: (n in on) for n in S.FUNCTIONS}
    b.hub.switches = {n: (n in on) for n in S.FUNCTIONS}
    return b


@pytest.mark.parametrize(("fn", "prefix"), [("habilidades", "habilidad/"), ("kanban", "tablero/"), ("documentos", "documento/")])
def test_each_function_publishes_only_its_own_type(fake_root, tmp_path, fn, prefix):
    _skill(fake_root, "alpha", "g/one", "---\nname: one\ndescription: d\n---\n")
    b = _bridge(fake_root, tmp_path, on=["salud", fn], clock=fn == "documentos")
    asyncio.run(b.run_scan())
    keys = set(b.mirror_objects()) - VERSION_KEYS - {"salud/plazo"}
    assert keys and all(k.startswith(prefix) for k in keys)
    if fn == "documentos":
        for key, o in {k: v for k, v in b.mirror_objects().items() if k not in VERSION_KEYS | {"salud/plazo"}}.items():
            if key.startswith("documento/plazo-"):  # C12 deadline links point at public https pages, not the local docs server
                assert o["url"].startswith("https://")
            else:
                assert o["url"].startswith(f"http://{LOOP}:8800/")
    off = _bridge(fake_root, tmp_path, on=["salud"])
    asyncio.run(off.run_scan())
    assert set(off.mirror_objects()) == VERSION_KEYS


def test_a_function_needs_both_sides(fake_root, tmp_path):
    b = _bridge(fake_root, tmp_path, on=["salud", "kanban"])
    b.hub.switches["kanban"] = False  # the plugin side is off
    asyncio.run(b.run_scan())
    assert set(b.mirror_objects()) == VERSION_KEYS
    b.hub.switches["kanban"] = True
    b.hub.engine_functions["kanban"] = False  # the world side is off
    assert set(b.mirror_objects()) == VERSION_KEYS


def test_new_switches_default_off_and_map_to_settings():
    assert S.switches(lambda k, d=None: d) == {**{n: False for n in S.FUNCTIONS}, "salud": True}
    sw = S.switches(lambda k, d=None: {"publish_skills": True, "publish_kanban": "yes", "publish_docs": True}.get(k, d))
    assert sw["habilidades"] and sw["kanban"] and sw["documentos"] and not sw["gasto"]


def test_guia_flag_passes_through_the_hub():
    from hermes_3d_lib.hub import _functions_on, _guide_on

    assert _guide_on(["salud", "guia"]) is True and _guide_on(["salud"]) is False and _guide_on(None) is False
    assert "guia" not in _functions_on(["salud", "guia"])  # not a source switch: never acted on


def test_fit_title_cuts_at_a_word_with_an_ellipsis():
    assert logsafe.fit_title("short title", 32) == "short title"
    cut = logsafe.fit_title("Actions cache: save only from main branch", 32)
    assert cut == "Actions cache: save only from…" and len(cut.encode()) <= 32
    long = logsafe.fit_title("Anticonstitutionnellement pipelines", 32)
    assert long.endswith("…") and len(long.encode()) <= 32
    assert len(logsafe.fit_title("ñ " * 40, 32).encode()) <= 32
    assert logsafe.fit_title("ab " * 10 + "ab", 32) == "ab " * 10 + "ab"  # exactly fits: untouched
    assert logsafe.fit_title(None, 32) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------- tableros: layout
def _cols(n):
    return {"hacer": {"n": n, "top": ["t"]}, "curso": {"n": 0, "top": []}, "hecho": {"n": 0, "top": []}}


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6, 9])
def test_boards_never_overlap_or_touch(n):
    section = {"items": [{"id": f"Board {i}", "columnas": _cols(50 - i)} for i in range(n)]}
    snap = tablero.snapshot_boards(section)
    assert len(snap) == min(n, tablero.MAX_BOARDS)
    W, DEPTH = 10.8, 1.0  # board width (engine formas-extra.ts) and a generous thickness
    objs = list(snap.values())
    for o in objs:
        assert valid_object(o) and o["zona"] == "z9"
        assert abs(o["x"] - 30) <= 12 and abs(o["z"] - 30) <= 10  # inside the ORIGINAL z9 (24 x 20)
    for i, a in enumerate(objs):
        for b in objs[i + 1 :]:
            gap_x, gap_z = abs(a["x"] - b["x"]) - W, abs(a["z"] - b["z"]) - DEPTH
            assert gap_x >= 3 or gap_z >= 8, (a["id"], b["id"], gap_x, gap_z)  # clear room to stand and read


def test_boards_are_named_ordered_and_ids_unique():
    section = {
        "items": [
            {"id": "small", "columnas": _cols(1)},
            {"id": "Numinia.com", "columnas": _cols(9)},
            {"id": "empty", "columnas": _cols(0)},
            {"id": "numinia-com", "columnas": _cols(5)},
        ]
    }
    snap = tablero.snapshot_boards(section)
    assert list(snap) == ["numinia.com", "numinia-com", "small"]  # most open first, empty board left out
    assert [o["titulo"] for o in snap.values()] == ["Numinia.com", "numinia-com", "small"]
    assert tablero.snapshot_boards({"items": [{"id": "x", "columnas": _cols(1)}]})["x"]["x"] == 30.0  # a lone board is centred
