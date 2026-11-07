"""C10: the messaging-platform status reader. Synthetic homes only; the run against the real Hermes is C10-evidence/run_c10.py."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hermes_palace_lib import mensajeria, scan

NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
SECRET = "123456:AAH-this-is-a-fake-bot-token-0123456789"


def _stamp(seconds_ago: int) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


def _home(tmp_path: Path, hb: int = 10) -> Path:
    root = tmp_path / ".hermes"
    (root / "profiles" / "alpha").mkdir(parents=True)
    (root / "profiles" / "beta").mkdir(parents=True)
    (root / "config.yaml").write_text("model: x\n")
    (root / "profiles" / "alpha" / "config.yaml").write_text(
        f"platforms:\n  telegram:\n    enabled: true\n    token: '{SECRET}'\n    extra:\n      allowed: [42]\n"
        "  discord:\n    enabled: false\n    token: nope\n"
    )
    (root / "profiles" / "beta" / "config.yaml").write_text("platforms:\n  slack:\n    token: x\n")
    state = {
        "pid": 500,
        "gateway_state": "running",
        "updated_at": _stamp(hb),
        "active_agents": 2,
        "hermes_home": "/secret/path",
        "argv": ["/secret/path/main.py", "gateway"],
        "served_profiles": ["default", "alpha", "beta"],
        "platforms": {
            "telegram": {"state": "disconnected", "writer_pid": 99, "error_message": f"boom {SECRET}"},  # old process
            "alpha:telegram": {"state": "connected", "writer_pid": 500, "needs_attention": False, "ingress_url": "http://x"},
            "beta:slack": {
                "state": "fatal",
                "writer_pid": 500,
                "needs_attention": True,
                "error_code": "auth_failed",
                "error_message": f"invalid token {SECRET}",
            },
            "beta:matrix": {"state": "retrying", "writer_pid": 500, "retrying_since": _stamp(30), "error_code": "Bad Code!"},
        },
    }
    (root / "gateway_state.json").write_text(json.dumps(state))
    return root


def test_one_light_per_platform_with_the_right_colour(tmp_path):
    t = mensajeria.read_messaging(_home(tmp_path), now=NOW.timestamp())
    by = {(p["profile"], p["platform"]): p for p in t["platforms"]}
    assert by[("alpha", "telegram")]["light"] == "green" and by[("alpha", "telegram")]["configured"]
    assert by[("beta", "slack")]["light"] == "red" and by[("beta", "slack")]["error_code"] == "auth_failed"
    assert by[("beta", "matrix")]["light"] == "amber" and by[("beta", "matrix")]["retrying"]
    assert by[("beta", "matrix")]["error_code"] == ""  # not a plain code: dropped
    assert by[("alpha", "discord")]["light"] == "grey" and by[("alpha", "discord")]["state"] == "disabled"
    assert t["count"] == len(t["platforms"]) == 5 and t["connected"] == 1 and t["attention"] == 1
    g = t["gateways"][0]
    assert g["live"] and g["state"] == "running" and g["active_agents"] == 2 and g["profiles_served"] == 3


def test_a_record_left_by_an_older_gateway_process_is_stale_not_green(tmp_path):
    t = mensajeria.read_messaging(_home(tmp_path), now=NOW.timestamp())
    old = next(p for p in t["platforms"] if p["profile"] == "default" and p["platform"] == "telegram")
    assert old["stale"] and old["light"] == "grey" and old["state"] == "unknown"


def test_a_gateway_with_an_old_heartbeat_makes_everything_stale(tmp_path):
    t = mensajeria.read_messaging(_home(tmp_path, hb=600), now=NOW.timestamp())
    assert t["gateways"][0]["state"] == "stale" and not t["gateways"][0]["live"]
    assert t["connected"] == 0 and all(p["light"] == "grey" for p in t["platforms"])


def test_no_secret_no_path_no_message_in_the_output(tmp_path):
    blob = json.dumps(mensajeria.read_messaging(_home(tmp_path), now=NOW.timestamp()))
    for bad in (SECRET, "AAH", "/secret", "boom", "invalid token", "http://", "allowed", "nope", str(tmp_path)):
        assert bad not in blob


def test_files_are_not_changed_and_odd_files_do_not_raise(tmp_path):
    root = _home(tmp_path)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    mensajeria.read_messaging(root, now=NOW.timestamp())
    assert before == {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    (root / "gateway_state.json").write_text("{not json")
    (root / "profiles" / "beta" / "config.yaml").write_text("platforms: [1, 2]\n")
    (root / "profiles" / "alpha" / "gateway_state.json").write_text(
        json.dumps({"platforms": {"x:y:z": 5, "../up": {"state": "connected"}}})
    )
    t = mensajeria.read_messaging(root, now=NOW.timestamp())
    assert t["schema"] == mensajeria.SCHEMA and t["error"] == ""


def test_a_home_without_messaging_gives_an_empty_list(tmp_path):
    root = tmp_path / ".hermes"
    root.mkdir()
    (root / "config.yaml").write_text("model: x\n")
    t = mensajeria.read_messaging(root)
    assert t["count"] == 0 and t["platforms"] == [] and t["gateways"] == []


def test_scan_has_the_mensajeria_section_without_a_database(tmp_path):
    snap = scan.scan_hermes(_home(tmp_path), only={"mensajeria"})
    sec = snap["sections"]["mensajeria"]
    assert sec["ok"] and sec["count"] == 5 and "mensajeria" not in scan.DB_SECTIONS
