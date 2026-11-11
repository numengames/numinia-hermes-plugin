"""Path A, backend half: the embed credential, the engine probe, /embed-credential, /world-snapshot and /action."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time

import pytest
from conftest import ROOT, run
from test_entry import load_router

from hermes_3d_lib import embed, linkkey
from hermes_3d_lib import settings as S

KEY = "k" * 40


def parts(cred: str):
    version, exp, nonce, mac = cred.split(".")
    return version, int(exp), nonce, mac


def test_credential_has_the_engines_exact_shape_and_hmac():
    cred, exp = embed.make_credential(KEY, ttl_s=3600, now_ms=1_000_000, nonce="A" * 16)
    assert cred.startswith("e1.4600000.AAAAAAAAAAAAAAAA.") and exp == 4_600_000
    want = hmac.new(KEY.encode(), b"numinia-embed@1\n4600000.AAAAAAAAAAAAAAAA", hashlib.sha256).hexdigest()
    assert parts(cred)[3] == want


def test_random_nonce_is_16_base64url_chars_and_never_repeats():
    seen = set()
    for _ in range(50):
        _, _, nonce, _ = parts(embed.make_credential(KEY)[0])
        assert re.fullmatch(r"[A-Za-z0-9_-]{16}", nonce)
        seen.add(nonce)
    assert len(seen) == 50


@pytest.mark.parametrize("ttl,want", [(10**9, 86400), (1, 60), (3600, 3600)])
def test_lifetime_is_clamped_to_the_engines_range(ttl, want):
    now = int(time.time() * 1000)
    _, exp = embed.make_credential(KEY, ttl_s=ttl, now_ms=now)
    assert exp - now == want * 1000


def test_setting_default_clamp_and_junk(tmp_path):
    assert S.DEFAULTS["embed_credential_ttl_s"] == 3600
    assert S.embed_ttl(None) == 3600 and S.embed_ttl("abc") == 3600 and S.embed_ttl(True) == 3600
    assert S.embed_ttl(999999) == 86400 and S.embed_ttl(5) == 60 and S.embed_ttl("120") == 120


def test_frame_url_puts_the_credential_in_the_fragment_only():
    url = embed.frame_url("http://example.test:1234/", "e1.1.2.3")
    assert url == "http://example.test:1234/?embed=1#embed=e1.1.2.3"
    assert "e1." not in url.split("#")[0]


def conf(home, **kw):
    return S.Hermes3DSettings(home=home, **kw)


def test_issue_engine_down_gives_a_clear_error_with_the_command(hermes_home):
    out = embed.issue(conf(hermes_home), KEY, probe=lambda base: {"up": False})
    assert out["ok"] is False and out["error"] == "engine-down" and "hermes 3d" in out["hint"]
    assert "url" not in out


def test_issue_without_key_or_with_embed_off_is_refused(hermes_home):
    assert embed.issue(conf(hermes_home), None, probe=lambda b: {"up": True, "embed": True})["error"] == "no-link-key"
    off = embed.issue(conf(hermes_home), KEY, probe=lambda b: {"up": True, "embed": False})
    assert off["error"] == "embed-off" and "activa" in off["hint"]


def test_issue_ok_never_contains_the_key(hermes_home):
    out = embed.issue(conf(hermes_home, embed_ttl_s=600), KEY, probe=lambda b: {"up": True, "embed": True})
    assert out["ok"] is True and out["ttl_s"] == 600 and "#embed=e1." in out["url"] and "?embed=1" in out["url"]
    assert KEY not in json.dumps(out)
    exp = parts(out["url"].split("#embed=")[1])[1]
    assert exp == out["expires_at_ms"]


def test_probe_only_talks_to_loopback_and_survives_a_dead_port():
    assert embed.probe_engine("http://example.org:80/") == {"up": False}
    assert embed.probe_engine("http://localhost:1/") == {"up": False}


def test_probe_reads_embed_flag_from_status(monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    body = {"espejo": {"embed": True}}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            raw = json.dumps(body).encode()
            self.send_response(200 if self.path == "/status" else 404)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("localhost", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://localhost:{srv.server_port}/"
        assert embed.probe_engine(base) == {"up": True, "embed": True}
        body["espejo"] = {}
        assert embed.probe_engine(base) == {"up": True, "embed": False}
        body.clear()
        assert embed.probe_engine(base) == {"up": True, "embed": False}
    finally:
        srv.shutdown()


# ---- the routes ---------------------------------------------------------------------------------------
def test_route_credential_when_engine_is_down(hermes_home):
    api = load_router()
    out = run(api.embed_credential())
    assert out["ok"] is False and out["error"] in ("engine-down",) and "hermes 3d" in out["hint"]


def test_route_credential_ok_with_a_probe_and_key(hermes_home, monkeypatch):
    key = linkkey.ensure(hermes_home)
    monkeypatch.setattr(embed, "probe_engine", lambda base, timeout=1.5: {"up": True, "embed": True})
    api = load_router()
    out = run(api.embed_credential())
    assert out["ok"] is True and "#embed=e1." in out["url"] and key not in json.dumps(out)
    # a tampered credential no longer matches the HMAC of the same key
    _version, exp, nonce, mac = parts(out["url"].split("#embed=")[1])
    assert mac == embed.sign(key, exp, nonce) and mac != embed.sign(key, exp + 1, nonce)


def test_route_credential_bad_settings(hermes_home):
    (hermes_home / "config.yaml").write_text("plugins:\n  hermes-3d:\n    engine_port: 99999999\n")
    out = run(load_router().embed_credential())
    assert out["ok"] is False


def test_route_snapshot_without_bridge(hermes_home):
    api = load_router()
    out = run(api.world_snapshot())
    assert out["ok"] is False and "hermes 3d up" in out["hint"]


def test_route_action_refuses_junk_and_oversize(hermes_home):
    api = load_router()
    assert run(api.action(None))["code"] == "invalid_request"
    assert run(api.action(["x"]))["code"] == "invalid_request"
    assert run(api.action({"id": "x" * 20000}))["code"] == "invalid_request"


def test_route_action_without_bridge_fails_closed(hermes_home):
    api = load_router()
    out = run(api.action({"id": "p1", "objeto": "cron/8bc0eecea9b5", "verbo": "cron.pausar"}))
    assert out["ok"] is False and out["code"] == "no_actions"


def test_shipped_tree_has_no_forbidden_constructs():
    for p in ["desktop/plugin.js", "hermes_3d_lib/embed.py", "dashboard/plugin_api.py"]:
        text = (ROOT / p).read_text()
        for bad in ("eval(", "new Function", "import(", "document.write"):
            assert bad not in text, (p, bad)


def test_engine_link_can_be_switched_off_for_path_a(hermes_home):
    """An engine takes one source and a local link displaces the embedded page: path A needs the link off, path B keeps it on."""
    from hermes_3d_lib import bridge_main

    on, off = conf(hermes_home), conf(hermes_home, engine_link=False)
    assert on.engine_link is True and off.engine_link is False
    assert bridge_main.link_url(on, "k" * 8).startswith("ws://")
    assert bridge_main.link_url(off, "k" * 8) is None
    assert bridge_main.link_url(on, None) is None
