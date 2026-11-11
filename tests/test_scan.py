# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""Scan + agentes + gasto mirrors: read-only guarantees, section independence, parsers, switches, publishing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from conftest import LOOP, SECRET_TEXT, SECRET_TOKEN

from hermes_3d_lib import agentes, config, gasto, scan
from hermes_3d_lib import protocol as P
from hermes_3d_lib import settings as S

SAMPLES = Path(__file__).parent / "samples"


# ------------------------------------------------------------------------------------------------ the scan
def test_scan_returns_every_section_with_timestamp_and_error_field(fake_root):
    snap = scan.scan_hermes(fake_root, allow_direct_db=True)
    assert snap["schema"] == scan.SCHEMA and set(snap["sections"]) == set(scan.SECTIONS)
    for name, sec in snap["sections"].items():
        assert isinstance(sec["at"], float) and "error" in sec and sec["section"] == name
        assert sec["ok"] is True and sec["error"] == "", (name, sec["error"])


def test_scan_counts(fake_root):
    s = scan.summary(scan.scan_hermes(fake_root, allow_direct_db=True))
    assert s["agentes"] == 3  # default + alpha + beta
    assert s["cron"] == 2
    assert s["sesiones_total"] == 4 and s["sesiones_ventana"] == 3  # sess-old is 90 d old
    assert s["skills_distintas"] == 3  # skill-alpha, skill-beta, shared
    assert s["mcp"] == 2 and s["tableros"] == 1 and s["tarjetas"] == 3 and s["proyectos"] == 1 and s["documentos"] == 2
    assert s["tokens"] == 1000 + 200 + 3000 + 400 + 500 + 100
    assert s["secciones_con_error"] == []


def test_one_failing_section_never_blocks_the_others(fake_root, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(scan, "scan_skills", boom)
    snap = scan.scan_hermes(fake_root, allow_direct_db=True)
    assert snap["sections"]["skills"]["ok"] is False and "RuntimeError" in snap["sections"]["skills"]["error"]
    assert snap["sections"]["skills"]["at"] > 0
    assert all(snap["sections"][k]["ok"] for k in scan.SECTIONS if k != "skills")
    assert scan.summary(snap)["secciones_con_error"] == ["skills"]


def test_profile_without_state_db_is_no_data_not_an_error(fake_root):
    snap = scan.scan_hermes(fake_root, allow_direct_db=True)  # beta has no state.db
    assert snap["sections"]["sesiones"]["ok"] and "beta" not in snap["sections"]["sesiones"]["por_perfil"]
    assert not (fake_root / "profiles" / "beta" / "state.db").exists()  # and the scan did not create it


def test_corrupt_database_is_a_section_error_other_profiles_still_counted(fake_root):
    (fake_root / "profiles" / "beta" / "state.db").write_bytes(b"this is not sqlite" * 100)
    snap = scan.scan_hermes(fake_root, allow_direct_db=True)
    ses = snap["sections"]["sesiones"]
    assert "DatabaseError" in ses["error"] and ses["por_perfil"]["alpha"]["total"] == 3  # alpha still counted
    assert snap["sections"]["agentes"]["ok"] and snap["sections"]["gasto"]["error"]


def test_nonexistent_root_gives_empty_sections_not_an_exception(tmp_path):
    snap = scan.scan_hermes(tmp_path / "nope", allow_direct_db=True)
    assert snap["sections"]["agentes"]["count"] == 0 and snap["sections"]["gasto"]["tokens"] == 0


def test_scan_never_leaks_message_text_or_secrets(fake_root):
    blob = json.dumps(scan.scan_hermes(fake_root, allow_direct_db=True), ensure_ascii=False)
    assert SECRET_TEXT not in blob and SECRET_TOKEN not in blob
    assert "sess-a1" not in blob and "sess-d1" not in blob  # session ids only hashed
    assert hashlib.sha256(b"sess-a1").hexdigest()[:16] in blob
    assert "npx" not in blob and "example.invalid" not in blob  # mcp command/url never read out
    assert "Authorization" not in blob


def _tree_state(root: Path) -> dict[str, tuple[int, int, str]]:
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_scan_is_read_only_no_file_created_or_changed(fake_root):
    before = _tree_state(fake_root)
    scan.scan_hermes(fake_root, allow_direct_db=True)
    scan.scan_hermes(fake_root, allow_direct_db=True)
    assert _tree_state(fake_root) == before  # same files, same bytes, same mtimes: nothing written, nothing created


def test_sqlite_is_opened_read_only_and_cannot_write(fake_root):
    con = scan._open_ro(fake_root / "profiles" / "alpha" / "state.db")
    with pytest.raises(sqlite3.OperationalError):
        con.execute("delete from sessions")
    con.close()
    with pytest.raises(FileNotFoundError):
        scan._open_ro(fake_root / "does-not-exist.db")  # mode=ro never creates
    assert not (fake_root / "does-not-exist.db").exists()


def test_scan_does_not_spawn_hermes_processes(fake_root, monkeypatch):
    import subprocess

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("scan ran a subprocess"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("scan spawned a process"))
    scan.scan_hermes(fake_root, allow_direct_db=True)


def test_default_root_from_profile_or_default_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "x" / "profiles" / "ursa"))
    assert scan.default_root() == tmp_path / "x"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "x"))
    assert scan.default_root() == tmp_path / "x"
    monkeypatch.delenv("HERMES_HOME")
    assert scan.scan_hermes()["error"].startswith("RuntimeError")


def test_gateway_state_uses_files_only(fake_root):
    ag = {a["id"]: a for a in scan.scan_agentes(fake_root)["items"]}
    assert ag["alpha"]["gateway"] is True and ag["default"]["gateway"] is True  # served by the live default gateway
    assert ag["beta"]["gateway"] is False  # not served
    assert ag["alpha"]["tiene_cron"] and ag["alpha"]["cron_total"] == 2 and ag["alpha"]["cron_activos"] == 1
    assert ag["beta"]["tiene_cron"] is False and ag["alpha"]["modelo"] == "m-alpha"


def test_cron_section_states(fake_root):
    items = {i["id"]: i for i in scan.scan_cron(fake_root)["items"]}
    assert items["j1"]["estado"] == "programado" and items["j2"]["estado"] == "pausado"
    assert all("prompt" not in i for i in items.values())


def test_kanban_open_cards_exclude_done_and_body(fake_root):
    k = scan.scan_kanban(fake_root)
    assert k["items"][0]["por_estado"] == {"todo": 1, "done": 1, "running": 1}
    assert {c["id"] for c in k["abiertas"]} == {"t1", "t3"} and all("body" not in c for c in k["abiertas"])


def test_mcp_names_and_kind_only(fake_root):
    m = {i["nombre"]: i for i in scan.scan_mcp(fake_root)["items"]}
    assert (
        m["files"]["tipo"] == "stdio" and m["web"]["tipo"] == "http" and set(m["files"]) == {"perfil", "nombre", "tipo", "activo"}
    )


def test_documents_are_names_and_sizes_only(fake_root):
    d = scan.scan_documentos(fake_root)
    assert d["count"] == 2 and all(set(i) == {"perfil", "carpeta", "nombre", "bytes", "mtime"} for i in d["items"])


def test_spend_numbers_and_window(fake_root):
    g = scan.scan_gasto(fake_root, days=30)
    by = {p["perfil"]: p for p in g["por_perfil"]}
    assert (
        by["alpha"]["entrada"] == 4000 and by["alpha"]["salida"] == 600 and by["alpha"]["coste_estimado_usd"] == 1.75
    )  # old one excluded
    assert g["coste_estimado_usd"] == pytest.approx(1.85) and g["top_modelos"][0]["modelo"] == "m-alpha"
    assert scan.scan_gasto(fake_root, days=365)["tokens"] > g["tokens"]


# ----------------------------------------------------------------------------------------------- parsers
def test_parse_profile_list_sample():
    r = agentes.parse_profile_list((SAMPLES / "profile_list_many.txt").read_text(encoding="utf-8"))
    names = [p["name"] for p in r["profiles"]]
    assert names == ["default", "anacarquide", "byblos", "lexa", "ursa", "zephyr"]
    by = {p["name"]: p for p in r["profiles"]}
    assert by["ursa"]["active"] and by["ursa"]["model"] == "anthropic/claude-opus-5" and by["zephyr"]["gateway"] == "stopped"
    assert by["zephyr"]["distribution"] == "demo@1.0" and not by["lexa"]["active"]


def test_parse_profile_list_empty_and_garbage():
    assert agentes.parse_profile_list((SAMPLES / "profile_list_empty.txt").read_text(encoding="utf-8")) == {
        "profiles": [],
        "empty": True,
    }
    assert agentes.parse_profile_list("\x00garbage\n\x1b[31mred\x1b[0m")["profiles"] == []
    assert agentes.parse_profile_list("")["profiles"] == []


def test_parse_insights_sample():
    r = gasto.parse_insights((SAMPLES / "insights_30d.txt").read_text(encoding="utf-8"))
    assert (r["days"], r["sessions"], r["messages"], r["input"], r["output"], r["tokens"]) == (
        30,
        120,
        4200,
        2_000_000,
        300_000,
        2_300_000,
    )
    assert r["cost_estimated"] == 41.25
    assert r["models"] == [
        {"model": "claude-sonnet-5-5", "sessions": 90, "tokens": 1_900_000},
        {"model": "claude-opus-5-5", "sessions": 30, "tokens": 400_000},
    ]


def test_parse_insights_empty_and_garbage():
    r = gasto.parse_insights((SAMPLES / "insights_empty.txt").read_text(encoding="utf-8"))
    assert r["empty"] and r["days"] == 7 and r["tokens"] is None
    assert gasto.parse_insights("lorem ipsum")["tokens"] is None and gasto.parse_insights("")["models"] == []


def test_scan_and_insights_agree_on_shape_of_numbers():
    ins = gasto.parse_insights((SAMPLES / "insights_30d.txt").read_text(encoding="utf-8"))
    assert ins["input"] + ins["output"] == ins["tokens"]


# ---------------------------------------------------------------------------------------- engine objects
def test_agent_objects_respect_protocol_limits(fake_root):
    objs = agentes.snapshot_from_scan(scan.scan_agentes(fake_root))
    assert set(objs) == {"a-default", "a-alpha", "a-beta"}
    for o in objs.values():
        assert (
            o["tipo"] == "perfil" and len(o["titulo"].encode()) <= P.MAX_TITLE and len(o["subtitulo"].encode()) <= P.MAX_SUBTITLE
        )
        assert o["acciones"] == []
    assert objs["a-alpha"]["estado"] == "activo" and objs["a-beta"]["estado"] == "apagado"
    assert "1/2 cron" in objs["a-alpha"]["subtitulo"]


def test_agent_id_is_safe():
    assert agentes.agent_id("Weird Name/../x") == "a-weird-name-..-x" and len(agentes.agent_id("x" * 100)) <= 32


def test_gasto_objects_cards(fake_root):
    objs = gasto.gasto_objects(scan.scan_gasto(fake_root))
    assert set(objs) == {"total"}  # per-profile spend lives with the agent, not in the spend zone
    for o in objs.values():
        assert o["tipo"] == "uso" and len(o["titulo"].encode()) <= P.MAX_TITLE and len(o["subtitulo"].encode()) <= P.MAX_SUBTITLE
    assert "tokens" in objs["total"]["subtitulo"] and "$" in objs["total"]["subtitulo"] and "30 d" in objs["total"]["subtitulo"]
    assert gasto.gasto_objects({"error": "x", "por_perfil": []}) == {}


def test_spend_lives_with_the_agent(fake_root):
    section = scan.scan_gasto(fake_root)
    spend = gasto.by_profile(section)
    assert set(spend) == {"alpha", "default"}  # beta has no sessions
    for text in spend.values():
        assert "est." in text and "$" in text and "30 d" in text  # the estimate label stays
    agents = agentes.snapshot_from_scan(scan.scan_agentes(fake_root), spend)
    card = agents[agentes.agent_id("alpha")]
    assert spend["alpha"] in card["subtitulo"] and len(card["subtitulo"].encode()) <= P.MAX_SUBTITLE
    assert "est." not in agents[agentes.agent_id("beta")]["subtitulo"]


def test_human_numbers():
    assert (
        gasto.human(33_686_258) == "33,7 M"
        and gasto.human(4200) == "4,2 k"
        and gasto.human(12) == "12"
        and gasto.human(2e9) == "2,0 G"
    )


# ------------------------------------------------------------------------- switches + publishing (bridge)
def test_switches_include_agentes_gasto_off_by_default():
    assert S.DEFAULT_SWITCHES["agentes"] is False and S.DEFAULT_SWITCHES["gasto"] is False
    sw = S.switches(lambda key, default=None: default)
    assert sw["agentes"] is False and sw["gasto"] is False and sw["salud"] is True


def _bridge(fake_root, tmp_path, on: list[str]):
    from hermes_3d_lib import bridge_main

    b = bridge_main.Bridge(
        tmp_path / "home", engine_url=f"ws://{LOOP}:1", link_key="k", scan_root=fake_root, allow_direct_db=True
    )
    b.hub.engine_functions = {n: (n in on) for n in S.FUNCTIONS}
    b.hub.switches = {n: True for n in S.FUNCTIONS}
    return b


def test_mirror_objects_publish_nothing_when_switches_off(fake_root, tmp_path):
    b = _bridge(fake_root, tmp_path, on=["salud"])
    asyncio.run(b.run_scan())
    assert b.scan is not None and b.scan["sections"]["agentes"]["count"] == 3  # snapshot always kept
    assert set(b.mirror_objects()) == {"salud/version"}  # the always-on version card (the deadline card is opt-in)


def test_mirror_objects_only_the_switched_on_function(fake_root, tmp_path):
    b = _bridge(fake_root, tmp_path, on=["salud", "agentes"])
    asyncio.run(b.run_scan())
    keys = set(b.mirror_objects())
    assert keys - {"salud/version"} == {"agente/a-default", "agente/a-alpha", "agente/a-beta"}
    b2 = _bridge(fake_root, tmp_path, on=["salud", "gasto"])
    asyncio.run(b2.run_scan())
    assert {k.split("/")[0] for k in b2.mirror_objects()} - {"salud"} == {"gasto"}


def test_scan_sections_without_engine_type_publish_nothing(fake_root, tmp_path):
    """Sections the engine has no type for (cron/sessions/mcp...) never leave; the typed ones do (M6 adds three)."""
    b = _bridge(fake_root, tmp_path, on=list(S.FUNCTIONS))
    asyncio.run(b.run_scan())
    assert {k.split("/")[0] for k in b.mirror_objects()} <= {
        "salud",
        "agente",
        "gasto",
        "habilidad",
        "tablero",
        "documento",
        "proyecto",
    }
    assert b.scan["sections"]["kanban"]["tarjetas"] == 3  # kept in the snapshot
    assert "mcp" not in b.scan["sections"]  # no switch asks for it: the bridge does not even read it (`hermes 3d scan` does)


def test_publish_mirrors_diffs_alta_cambio_baja(fake_root, tmp_path):
    b = _bridge(fake_root, tmp_path, on=["salud", "agentes"])
    sent: list[dict] = []
    b.hub.publish = lambda m: sent.append(m)  # type: ignore[assignment]
    b.hub.connected = True
    asyncio.run(b.run_scan())  # first publish after connect: full_snapshot does it
    assert sent == []
    asyncio.run(b.full_snapshot())
    (fake_root / "profiles" / "gamma").mkdir()
    (fake_root / "profiles" / "gamma" / "config.yaml").write_text("model:\n  default: m-g\n", encoding="utf-8")
    asyncio.run(b.run_scan())
    assert [m["t"] for m in sent] == ["alta"] and sent[0]["objeto"]["id"] == "a-gamma"
    sent.clear()
    import shutil

    shutil.rmtree(fake_root / "profiles" / "gamma")
    asyncio.run(b.run_scan())
    assert [m["t"] for m in sent] == ["baja"] and sent[0]["objeto"]["id"] == "a-gamma"


# ----------------------------------------------------------------------------------------------- CLI
def test_cli_scan_json_is_valid_and_leak_free(fake_root, monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "hp_entry", Path(__file__).parents[1] / "__init__.py", submodule_search_locations=[str(Path(__file__).parents[1])]
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("HERMES_HOME", str(fake_root))
    assert mod.cmd_scan(True, 30) == 0
    out = capsys.readouterr().out
    snap = json.loads(out)
    assert snap["schema"] == scan.SCHEMA and SECRET_TEXT not in out and SECRET_TOKEN not in out
    assert mod.cmd_scan(False, 30) == 0 and "agentes" in capsys.readouterr().out


# ---------------------------------------------------------- bridge end to end (fake engine) + HTTP + CLI up
def _e2e_imports():
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from conftest import run, until
    from fake_engine import FakeEngine

    return run, until, FakeEngine


def test_first_lote_carries_agentes_and_gasto_only_when_switched_on(fake_root, hermes_home):
    run, _until, FakeEngine = _e2e_imports()
    from hermes_3d_lib.bridge_main import Bridge

    async def main(on, world_on=("salud", "agentes", "gasto")):
        eng = FakeEngine(funciones={config.ENGINE_FUNCTION.get(n, n): (n in world_on) for n in S.FUNCTIONS})
        await eng.start()
        b = Bridge(
            hermes_home,
            cron_reader=lambda: "",
            engine_url=eng.url,
            link_key=eng.secret,
            poll_seconds=0.05,
            scan_root=fake_root,
            allow_direct_db=True,
        )
        b.switches.update({"salud": True, "cron": False, "sesiones": False, **{n: True for n in on}})
        b.hub.switches = b.switches
        await b.run_scan()  # the first thing the plugin does
        await b.hub.start()
        await eng.wait_connected()
        lote = await eng.wait_for(lambda f: f["t"] == "lote")
        types = sorted({o["tipo"] for o in lote["objetos"]})
        await b.hub.stop()
        await eng.stop()
        return types, len(lote["objetos"])

    assert run(main([]))[0] == ["salud"]  # just the version card
    types, n = run(main(["agentes"]))
    assert types == ["perfil", "salud"] and n == 3 + 1
    types, n = run(main(["agentes", "gasto"]))
    assert types == ["perfil", "salud", "uso"] and n == 3 + 1 + 1
    # local switch on but the WORLD's switch off: fail closed, nothing published
    assert run(main(["agentes", "gasto"], world_on=("salud",)))[0] == ["salud"]


def test_scan_http_routes_need_the_bearer_token(fake_root, hermes_home):
    run, _until, FakeEngine = _e2e_imports()
    from test_bridge import _http

    from hermes_3d_lib.bridge_main import Bridge

    async def main():
        eng = FakeEngine()
        await eng.start()
        b = Bridge(
            hermes_home,
            cron_reader=lambda: "",
            engine_url=eng.url,
            link_key=eng.secret,
            poll_seconds=0.05,
            scan_root=fake_root,
            allow_direct_db=True,
        )
        b.hub.switches["agentes"] = True  # the scan reads only what a switch asks for
        port = await b.hub.start()
        st_no, _ = await _http(port, "wrong-token", "GET", "/scan")
        st0, body0 = await _http(port, b.hub.token, "GET", "/scan")
        st1, body1 = await _http(port, b.hub.token, "POST", "/scan/refresh")
        st2, body2 = await _http(port, b.hub.token, "GET", "/scan")
        await b.hub.stop()
        await eng.stop()
        return st_no, st0, body0, st1, body1, st2, body2

    st_no, st0, body0, st1, body1, st2, body2 = run(main())
    assert st_no == 401 and st0 == 200 and body0["scan"] is None
    assert st1 == 200 and body1["summary"]["agentes"] == 3
    assert st2 == 200 and body2["scan"]["schema"] == scan.SCHEMA and SECRET_TEXT not in json.dumps(body2)


def test_hermes3d_up_prints_the_exact_world_url(hermes_home, monkeypatch, capsys):
    import importlib.util

    import yaml

    from hermes_3d_lib import supervisor

    spec = importlib.util.spec_from_file_location(
        "hp_entry2", Path(__file__).parents[1] / "__init__.py", submodule_search_locations=[str(Path(__file__).parents[1])]
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    monkeypatch.setattr(
        supervisor, "start_engine", lambda *a, **k: {"reused": True, "link_port": 8791, "link_enabled": True, "pid": 0}
    )
    monkeypatch.setattr(supervisor, "spawn_bridge", lambda *a, **k: 4242)

    def configure(**settings):
        (hermes_home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"entries": {"hermes-3d": {"settings": settings}}}}))

    configure(engine_port=18999)
    assert mod.cmd_up() == 0
    assert f"Open the world: http://{LOOP}:18999/" in capsys.readouterr().out
    configure(engine_port=18999, world_url=f"http://{LOOP}:18999/?mundo=hermes")
    assert mod.cmd_up() == 0
    assert f"Open the world: http://{LOOP}:18999/?mundo=hermes" in capsys.readouterr().out


def test_world_function_names_are_the_engines_own():
    from hermes_3d_lib.hub import _functions_on

    assert _functions_on(["salud", "perfiles", "uso"]) == {
        "salud": True,
        "cron": False,
        "sesiones": False,
        "agentes": True,
        "gasto": True,
        "habilidades": False,
        "kanban": False,
        "proyectos": False,
        "documentos": False,
    }
    assert _functions_on(["agentes", "gasto"])["agentes"] is False  # the engine does not know those words
    assert agentes.TYPE_AGENTE == "perfil" and gasto.TYPE_GASTO == "uso"


def test_fresh_profiles_made_by_hermes_are_agents(tmp_path):
    """INT-01 (found by tools/integration): `hermes profile create` makes SOUL.md, .env, cron/, sessions/... but no
    config.yaml and no state.db, so a brand-new profile (and a brand-new default home) was invisible to the scan."""
    root = tmp_path / "root"
    for name in ("alpha", "beta"):
        d = root / "profiles" / name
        for sub in ("cron", "sessions", "memories", "skills"):
            (d / sub).mkdir(parents=True)
        (d / "SOUL.md").write_text("soul", encoding="utf-8")
        (d / ".env").write_text("SECRET=x", encoding="utf-8")
    (root / "SOUL.md").write_text("soul", encoding="utf-8")
    (root / "profiles" / "not-a-profile").mkdir()  # a stray empty folder is still not an agent
    names = [n for n, _ in scan.profile_dirs(root)]
    assert names == ["default", "alpha", "beta"]
    assert scan.scan_agentes(root)["count"] == 3


def test_gasto_card_cache_apart_and_cost_estimated():
    sec = {
        "dias": 30,
        "tokens": 1_500_000,
        "sesiones": 3,
        "coste_estimado_usd": 12.5,
        "top_modelos": [{"modelo": "m1"}],
        "por_perfil": [
            {
                "perfil": "a",
                "sesiones": 3,
                "tokens": 1_500_000,
                "cache_lectura": 40_000_000,
                "cache_escritura": 2_000_000,
                "coste_estimado_usd": 12.5,
                "modelos": [{"modelo": "m1"}],
            }
        ],
    }
    for o in gasto.gasto_objects(sec).values():
        sub = o["subtitulo"]
        assert "1,5 M tokens" in sub and "cache 42,0 M" in sub and "$12.50 est." in sub
        assert sub.index("tokens") < sub.index("cache") < sub.index("$")
        assert len(sub.encode()) <= 120
    sec["por_perfil"][0].update(cache_lectura=0, cache_escritura=0)
    assert all("cache" not in o["subtitulo"] for o in gasto.gasto_objects(sec).values())


def test_version_card_exists_with_salud_on_and_is_absent_with_it_off(fake_root, tmp_path):
    import re

    from conftest import ROOT

    b = _bridge(fake_root, tmp_path, on=["salud"])
    card = b.mirror_objects()["salud/version"]
    version = re.search(r"^version:\s*[\"']?([^\"'\s#]+)", (ROOT / "plugin.yaml").read_text(encoding="utf-8"), re.M).group(1)
    assert card["tipo"] == "salud" and card["zona"] == "z0"
    assert card["titulo"].startswith("Hermes 3D v") and card["titulo"] == f"Hermes 3D v{version}"
    assert card["acciones"] == []  # a label, never a control
    off = _bridge(fake_root, tmp_path, on=[])
    assert "salud/version" not in off.mirror_objects()
