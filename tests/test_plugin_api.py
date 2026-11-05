# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

import importlib.util
import json
import pathlib
import re

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SRC = pathlib.Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
spec = importlib.util.spec_from_file_location("mundo_api", SRC)
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

SECRET = "sk-abcdef1234567890SECRET"

FIXTURES = {
    "/api/cron/jobs": [{"id": "8bc0eecea9b5", "name": "demo", "schedule_display": "every 60m",
                        "enabled": True, "state": "scheduled", "next_run_at": "2026-11-04T23:54:01+01:00",
                        "last_run_at": None, "last_status": None, "failure_streak": 0,
                        "prompt": "never exposed " + SECRET}],
    "/api/sessions": {"sessions": [{"id": "s1", "title": "hola", "message_count": 3, "api_key": SECRET}]},
    "/api/profiles": {"profiles": [{"name": "default", "is_default": True, "model": None,
                                    "skill_count": 2, "gateway_running": False, "path": "/x"}]},
    "/api/profiles/active": {"active": "default", "current": "default"},
    "/api/status": {"version": "0.22.7", "gateway_running": False, "active_agents": 0,
                    "gateway_busy": False, "secret_thing": SECRET},
    "/api/mcp/servers": {"servers": [{"name": "demo", "transport": "stdio", "command": "py",
                                      "args": ["srv.py", "--token=" + SECRET],
                                      "env": {"GITHUB_TOKEN": SECRET, "FOO": "bar"},
                                      "enabled": True, "tools": None, "source": "config", "plugin": None}]},
    "/api/model/info": {"model": "m", "provider": "p", "effective_context_length": 1000, "capabilities": {}},
    "/api/tools/toolsets": [{"name": "web", "label": "Web", "enabled": True, "available": True,
                             "configured": True, "tools": ["web_search"]}],
    "/api/config": {"model": "m", "toolsets": ["hermes-cli"], "providers": {"x": {"api_key": SECRET}}},
    "/api/skills": [{"name": "s", "description": "d", "category": "c", "enabled": True,
                     "usage": 0, "provenance": "bundled"}],
    "/api/memory": {"active": "", "providers": [{"name": "byterover", "available": False, "key": SECRET}]},
}


def make_client(monkeypatch, fixtures=None, fail=None, calls=None):
    fixtures = FIXTURES if fixtures is None else fixtures
    fail = fail or {}

    async def fake(request, method, path, **kw):
        if calls is not None:
            calls.append((method, path, kw.get("params")))
        if path in fail:
            req = httpx.Request(method, "http://t" + path)
            resp = httpx.Response(fail[path][0], json={"detail": fail[path][1]}, request=req)
            raise httpx.HTTPStatusError("boom", request=req, response=resp)
        return fixtures[path]

    monkeypatch.setattr(api, "upstream", fake)
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def test_health(monkeypatch):
    c = make_client(monkeypatch)
    assert c.get("/health").json()["ok"] is True


def test_plugin_identity_is_consistent(monkeypatch):
    """One id and one version everywhere: Hermes names the route after the manifest."""
    root = SRC.parents[1]
    yaml_text = (root / "plugin.yaml").read_text()
    manifest = json.loads((root / "dashboard" / "manifest.json").read_text())
    name = re.search(r"^name:\s*(\S+)", yaml_text, re.M).group(1)
    version = re.search(r"^version:\s*(\S+)", yaml_text, re.M).group(1)
    assert name == manifest["name"] == "numinia-hermes-plugin"
    assert manifest["tab"]["path"] == "/" + name
    assert version == manifest["version"] == api.VERSION
    health = make_client(monkeypatch).get("/health").json()
    assert health["plugin"] == name
    bundle = (root / "dashboard" / "dist" / "index.js").read_text()
    assert f'register("{name}"' in bundle and f"/api/plugins/{name}/world" in bundle
    assert "hermes-mundo" not in bundle


def test_world_has_six_zones(monkeypatch):
    w = make_client(monkeypatch).get("/world").json()
    assert set(w["zones"]) == {"cron", "chat", "agents", "mcp", "options", "documents"}
    z = w["zones"]
    assert z["cron"]["jobs"][0]["id"] == "8bc0eecea9b5"
    assert "prompt" not in z["cron"]["jobs"][0]
    assert z["chat"]["sessions"][0]["title"] == "hola"
    assert z["agents"]["active"] == "default" and z["agents"]["profiles"][0]["name"] == "default"
    assert z["mcp"]["servers"][0]["name"] == "demo"
    assert z["options"]["toolsets"][0]["name"] == "web" and z["options"]["model"]["model"] == "m"
    assert z["documents"]["skills"][0]["name"] == "s"
    assert all(v["error"] is None for v in z.values())


def test_no_secret_leaks(monkeypatch):
    raw = make_client(monkeypatch).get("/world").text
    assert SECRET not in raw
    assert "SECRET" not in raw
    assert "bar" not in raw  # env values masked wholesale


def test_zone_failure_is_isolated(monkeypatch):
    c = make_client(monkeypatch, fail={"/api/sessions": (503, "Session store is not initialized")})
    z = c.get("/world").json()["zones"]
    assert z["chat"]["sessions"] == []
    assert z["chat"]["error"].startswith("503")
    assert z["cron"]["error"] is None and len(z["cron"]["jobs"]) == 1


def test_profile_param_forwarded(monkeypatch):
    calls = []
    make_client(monkeypatch, calls=calls).get("/world?profile=ursa")
    cron = [c for c in calls if c[1] == "/api/cron/jobs"][0]
    assert cron[2] == {"profile": "ursa"}


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_cron_actions_use_hermes_endpoint(monkeypatch, action):
    calls = []
    fx = {f"/api/cron/jobs/abc123/{action}": {"id": "abc123", "state": "paused", "enabled": False}}
    c = make_client(monkeypatch, fixtures=fx, calls=calls)
    r = c.post(f"/cron/abc123/{action}")
    assert r.status_code == 200 and r.json()["job"]["state"] == "paused"
    assert calls == [("POST", f"/api/cron/jobs/abc123/{action}", None)]


def test_cron_action_rejects_bad_id_and_upstream_error(monkeypatch):
    c = make_client(monkeypatch, fixtures={}, fail={"/api/cron/jobs/zz/pause": (404, "nope")})
    assert c.post("/cron/bad..id/pause").status_code in (400, 404)
    assert c.post("/cron/zz/pause").status_code == 404


def test_no_run_route(monkeypatch):
    c = make_client(monkeypatch)
    assert c.post("/cron/abc/run").status_code in (404, 405)


def test_redact_unit():
    assert api.redact({"a": {"password": "x"}, "n": 1})["a"]["password"] == "***"
    assert api.redact_text("Bearer abcdefghij12345") == "***"
