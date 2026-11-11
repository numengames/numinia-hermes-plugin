"""Bodies for the bots: 14 stick figures, one ``pnj@1`` round each (decision on t_a55cdae9: fixed round, partial)."""

from __future__ import annotations

import json

from hermes_3d_lib import cuerpos

NAMES = [f"bot{i:02d}" for i in range(14)]


def test_talking_to_a_body_shows_its_spend(tmp_path):
    cuerpos.sow(tmp_path, NAMES, spend={"bot01": "$4.20 est. · 12,3 M tokens · 30 d"})

    def talk(n):
        return json.loads((tmp_path / "aventura" / "pnj" / f"cuerpo-{n}.json").read_text())["dialogo"]["nodos"]["hola"]["texto"]

    assert "$4.20 est." in talk("bot01") and "30 d" in talk("bot01")
    assert "$" not in talk("bot02")  # no spend known: nothing invented


def test_fourteen_distinct_figures_with_a_round_each(tmp_path):
    res = cuerpos.sow(tmp_path, NAMES)
    assert res["bodies"] == 14 and res["sha256"] == cuerpos.asset_sha()
    files = sorted((tmp_path / "aventura" / "pnj").glob("cuerpo-*.json"))
    assert len(files) == 14
    bodies = [json.loads(f.read_text()) for f in files]
    assert len({b["id"] for b in bodies}) == 14
    assert len({(b["posicion"]["x"], b["posicion"]["z"]) for b in bodies}) == 14  # told apart by spot and by name
    assert {b["nombre"] for b in bodies} == set(NAMES)
    for b in bodies:
        assert b["formato"] == "pnj@1"
        assert b["apariencia"]["modelo"] == res["sha256"]
        ronda = b["posiciones"][0]["ronda"]
        assert 2 <= len(ronda["puntos"]) <= 8 and 0.3 <= ronda["velocidad"] <= 1.4
        for p in ronda["puntos"]:
            assert 18 <= p["x"] <= 42 and -42 <= p["z"] <= -18  # inside z6, the control tower
            assert 0 <= p["pausa"] <= 60
    assert (tmp_path / "datos" / "activos" / f"{res['sha256']}.glb").read_bytes() == cuerpos.ASSET.read_bytes()


def test_no_two_stretches_cross(tmp_path):
    cuerpos.sow(tmp_path, NAMES)
    spans = []
    for f in (tmp_path / "aventura" / "pnj").glob("cuerpo-*.json"):
        pts = json.loads(f.read_text())["posiciones"][0]["ronda"]["puntos"]
        xs = sorted(p["x"] for p in pts)
        spans.append((pts[0]["z"], xs[0], xs[-1]))
    for i, (z1, a1, b1) in enumerate(spans):
        for z2, a2, b2 in spans[i + 1 :]:
            assert z1 != z2 or b1 < a2 or b2 < a1


def test_idempotent_and_removes_only_its_own_stale_files(tmp_path):
    cuerpos.sow(tmp_path, NAMES)
    mine = tmp_path / "aventura" / "pnj"
    other = mine / "alma.json"
    other.write_text("{}")
    again = cuerpos.sow(tmp_path, NAMES[:13])
    assert again["written"] == 0 and again["removed"] == 1
    assert other.exists() and len(list(mine.glob("cuerpo-*.json"))) == 13


def test_more_than_fourteen_profiles_get_fourteen_bodies(tmp_path):
    assert cuerpos.sow(tmp_path, [f"p{i:02d}" for i in range(20)])["bodies"] == 14


def test_missing_figure_is_not_fatal(tmp_path):
    assert "skipped" in cuerpos.sow(tmp_path, NAMES, asset=tmp_path / "nope.glb")


def test_activity_line_from_kanban_only(tmp_path):
    """Doing/done come from the Kanban's own per-agent lines; an agent with none gets none (nothing invented)."""
    import sqlite3
    import time

    from hermes_3d_lib import agentes, scan

    db = tmp_path / "kanban.db"
    con = sqlite3.connect(db)
    con.execute(
        "create table tasks (id text, title text, status text, assignee text, priority int, created_at int, started_at int, completed_at int)"
    )
    now = int(time.time())
    con.executemany(
        "insert into tasks values (?,?,?,?,?,?,?,?)",
        [
            ("a", "Build the thing", "running", "alpha", 0, now - 90, now - 60, None),
            ("b", "Old done card", "done", "alpha", 0, now - 900, now - 800, now - 700),
            ("c", "Newest done card", "done", "alpha", 0, now - 500, now - 400, now - 100),
            ("d", "Only done", "done", "beta", 0, now - 500, now - 400, now - 100),
        ],
    )
    con.commit()
    con.close()
    acc: dict = {}
    c = sqlite3.connect(db)
    scan._agent_lines(c, 30, acc)
    c.close()
    act = agentes.activity_by_profile(
        {"por_agente": {k: {x: y for x, y in v.items() if not x.startswith("_")} for k, v in acc.items()}}
    )
    assert act["alpha"] == {"haciendo": "Build the thing", "hecho": "Newest done card"}
    assert act["beta"] == {"hecho": "Only done"} and "gamma" not in act
    obj = agentes.agent_to_object({"nombre": "alpha", "modelo": "m", "gateway": True}, "", act["alpha"])
    assert "Doing: Build the thing" in obj["subtitulo"] and "Done: Newest done card" in obj["subtitulo"]
    assert len(obj["subtitulo"].encode()) <= 120 and obj["subtitulo"].startswith("m · gateway up")
    assert "Doing" not in agentes.agent_to_object({"nombre": "g", "modelo": "m"}, "", None)["subtitulo"]
    body = cuerpos.body_for("alpha", 0, "x" * 64, "", act["alpha"])
    assert "Doing: Build the thing." in body["dialogo"]["nodos"]["hola"]["texto"]
