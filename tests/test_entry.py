"""Entry module, approval hook, tools, session hooks, CLI, supervisor and dashboard router."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from conftest import LOOP, ROOT

from hermes_3d_lib import actions, client, config, supervisor
from hermes_3d_lib import settings as S


def load_entry():
    spec = importlib.util.spec_from_file_location("hermes_3d_entry", ROOT / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermes_3d_entry"] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeCtx:
    """The slice of PluginContext the plugin uses; records every registration."""

    def __init__(self, settings=None):
        self.tools, self.hooks, self.cli, self.settings = {}, {}, {}, settings or {}
        self.dispatched = []

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = SimpleNamespace(schema=schema, handler=handler, toolset=toolset)

    def register_hook(self, name, cb, **kw):
        self.hooks[name] = cb

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli[name] = (setup_fn, handler_fn)

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def dispatch_tool(self, name, args, **kw):
        self.dispatched.append((name, args))
        return json.dumps({"output": "ok", "exit_code": 0})


def test_register_matches_manifest(hermes_home):
    import yaml

    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    ctx = FakeCtx()
    load_entry().register(ctx)
    assert sorted(ctx.tools) == sorted(manifest["provides_tools"])
    assert sorted(ctx.hooks) == sorted(manifest["provides_hooks"])
    assert "3d" in ctx.cli
    assert manifest["capabilities"] == []
    assert manifest["requires_hermes"].startswith(">=")


def test_default_switches_only_health(hermes_home):
    assert S.switches(lambda k, d=None: d) == S.DEFAULT_SWITCHES
    assert [n for n, on in S.DEFAULT_SWITCHES.items() if on] == ["salud"]
    sw = S.switches(lambda k, d=None: {"publish_cron": True, "publish_health": "off"}.get(k, d))
    assert sw["cron"] is True and sw["salud"] is False
    assert set(sw) == set(S.FUNCTIONS)


def test_manifest_schema_defaults_equal_code_defaults():
    import yaml

    schema = yaml.safe_load((ROOT / "plugin.yaml").read_text())["config_schema"]
    for name, on in S.DEFAULT_SWITCHES.items():
        assert schema[S.SWITCH_KEYS[name]]["default"] is on
    for key, default in S.DEFAULTS.items():
        assert schema[key]["default"] == default, key


def test_pre_tool_call_escalates_only_our_tool():
    assert actions.approval_directive("terminal", {"command": "ls"}) is None
    d = actions.approval_directive(actions.TOOL_NAME, {"job_id": "8bc0eecea9b5", "verb": "cron.pausar"})
    assert d["action"] == "approve" and "hermes cron pause 8bc0eecea9b5" in d["message"]
    assert d["rule_key"] == "hermes-3d:cron:pause"


def test_approval_message_cannot_be_forged_by_job_text():
    d = actions.approval_directive(actions.TOOL_NAME, {"job_id": "x" * 500 + "\nrm -rf /", "verb": "cron.pausar"})
    assert len(d["message"]) < 200


class Runner:
    """Records the argv of every subprocess call and answers with a canned result."""

    def __init__(self, returncode=0, stdout="Paused job\n", stderr="", exc=None):
        self.calls, self.result, self.exc = [], SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr), exc

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.exc:
            raise self.exc
        return self.result


def test_run_cron_cli_runs_an_argv_list_without_a_shell():
    run = Runner()
    out = actions.run_cron_cli("8bc0eecea9b5", "cron.pausar", runner=run, hermes="/usr/bin/hermes")
    assert out["ok"] is True and out["texto"] == "job paused" and out["text"] == "job paused"
    argv, kw = run.calls[0]
    assert argv == ["/usr/bin/hermes", "cron", "pause", "8bc0eecea9b5"] and isinstance(argv, list)
    assert not kw.get("shell") and kw["timeout"] == actions.CLI_TIMEOUT
    assert actions.run_cron_cli("8bc0eecea9b5", "cron.reanudar", runner=run, hermes="h")["texto"] == "job resumed"


@pytest.mark.parametrize(
    "bad_id",
    [
        "a;rm -rf /",
        "$(id)",
        "../x",
        "8bc0eecea9b5\n",
        "8bc0eecea9b5\nrm",
        "-rf",
        "-8bc0eecea9b5",
        "8BC0EECEA9B5",
        " 8bc0eecea9b5",
        "abc",
        "a" * 65,
        "",
        "caf\u00e9caf\u00e9",
        "8bc0\x00eecea9b5",
    ],
)
def test_job_ids_are_validated_strictly_before_anything_runs(bad_id):
    run = Runner()
    assert actions.run_cron_cli(bad_id, "cron.pausar", runner=run, hermes="h")["ok"] is False
    assert run.calls == [], "an invalid id must never reach a subprocess"
    assert actions.JOB_ID.fullmatch(bad_id) is None


@pytest.mark.parametrize("verb", ["cron.eliminar", "cron.ejecutar", "", "pause"])
def test_only_pause_and_resume_are_allowed(verb):
    run = Runner()
    assert actions.run_cron_cli("8bc0eecea9b5", verb, runner=run, hermes="h")["ok"] is False and run.calls == []


def test_run_cron_cli_failures_are_clean():
    failed = actions.run_cron_cli("8bc0eecea9b5", "cron.pausar", runner=Runner(1, "No job with id\n"), hermes="h")
    assert failed["ok"] is False and "No job with id" in failed["text"]
    assert (
        actions.run_cron_cli("8bc0eecea9b5", "cron.pausar", runner=Runner(exc=subprocess.TimeoutExpired("h", 1)), hermes="h")[
            "code"
        ]
        == "timeout"
    )
    assert actions.run_cron_cli("8bc0eecea9b5", "cron.pausar", runner=Runner(exc=FileNotFoundError()), hermes="h")["ok"] is False


def test_hermes_missing_is_reported(monkeypatch):
    monkeypatch.setattr(actions.shutil, "which", lambda name: None)
    assert actions.run_cron_cli("8bc0eecea9b5", "cron.pausar", runner=Runner())["code"] == "hermes_missing"


def test_no_shell_anywhere_in_the_code():
    for path in [ROOT / "__init__.py", ROOT / "dashboard/plugin_api.py", *(ROOT / "hermes_3d_lib").glob("*.py")]:
        text = path.read_text(encoding="utf-8")
        for needle in ("shell=True", "os.system", "os.popen", 'dispatch_tool("terminal"', "commands.getoutput"):
            assert needle not in text, f"{path.name}: {needle}"


def test_action_tool_without_pending_request_does_nothing(hermes_home):
    ctx = FakeCtx()
    mod = load_entry()
    mod.register(ctx)
    out = json.loads(ctx.tools["hermes3d_cron_action"].handler({"job_id": "8bc0eecea9b5", "verb": "cron.pausar"}))
    assert out["ok"] is False and ctx.dispatched == []


def test_state_tool_is_read_only_and_degrades(hermes_home, monkeypatch):
    (hermes_home / "skills" / "demo").mkdir(parents=True)
    (hermes_home / "skills" / "demo" / "SKILL.md").write_text("x")
    ctx = FakeCtx()
    mod = load_entry()
    mod.register(ctx)
    import hermes_3d_lib.bridge_main as bm

    monkeypatch.setattr(bm, "run_cron_list", lambda timeout=20: (ROOT / "tests/samples/cron_list_many.txt").read_text())
    out = json.loads(ctx.tools["hermes3d_state"].handler({"what": "all"}))
    assert out["bridge"]["running"] is False
    assert out["switches"]["salud"] is True and out["switches"]["cron"] is False
    assert len(out["cron"]["jobs"]) == 5 and out["skills"] == {"count": 1, "names": ["demo"]}
    assert ctx.dispatched == []


def test_session_hooks_do_nothing_when_switch_off_and_survive_dead_bridge(hermes_home):
    ctx = FakeCtx()
    load_entry().register(ctx)
    ctx.hooks["on_session_start"](session_id="s1", model="m", platform="cli")  # off: silent no-op
    ctx.settings["publish_sessions"] = True
    ctx.hooks["on_session_start"](session_id="s1", model="m", platform="cli", extra="kw")  # bridge down: no raise
    ctx.hooks["on_session_end"](session_id="s1", completed=True)
    ctx.hooks["on_session_finalize"](session_id="s1")


def test_session_hooks_send_ids_only(hermes_home, monkeypatch):
    sent = []
    monkeypatch.setattr(client, "call", lambda m, p, body=None, **kw: sent.append((m, p, body)) or {"ok": True})
    ctx = FakeCtx({"publish_sessions": True})
    mod = load_entry()
    mod.register(ctx)
    monkeypatch.setattr(mod.client, "call", lambda m, p, body=None, **kw: sent.append((m, p, body)) or {"ok": True})
    ctx.hooks["on_session_start"](session_id="abc", model="m", platform="cli", user_message="SECRET TEXT")
    assert sent[0][2] == {"kind": "sesion_inicio", "session_id": "abc", "model": "m", "platform": "cli"}
    assert "SECRET" not in json.dumps(sent)


# ---- supervisor -----------------------------------------------------------------------------------
def test_foreign_world_is_never_reused(tmp_path):
    w = tmp_path / "w"
    w.mkdir()
    (w / "datos.txt").write_text("mine")
    with pytest.raises(supervisor.SupervisorError, match="not a Numinia world"):
        supervisor.check_world_is_ours(w)
    (w / "mundo.json").write_text(json.dumps({"esquema": "other/thing@1"}))
    with pytest.raises(supervisor.SupervisorError, match="not a Numinia world"):
        supervisor.check_world_is_ours(w)
    (w / "mundo.json").write_text("{broken")
    with pytest.raises(supervisor.SupervisorError):
        supervisor.check_world_is_ours(w)
    (w / "mundo.json").write_text(json.dumps({"esquema": "numinia-engine/mundo@2", "motor": {"build": "dev"}}))
    supervisor.check_world_is_ours(w)  # what the engine itself writes
    supervisor.check_world_is_ours(tmp_path / "does-not-exist")
    (tmp_path / "empty").mkdir()
    supervisor.check_world_is_ours(tmp_path / "empty")


def test_signature_matches_the_engines_firmarReto():
    # Independent vector: HMAC-SHA256(key, "numinia-enlace@1\n" + nonce), what enlace-espejo.ts computes.
    import hashlib
    import hmac

    key, nonce = "k" * 16, "ab" * 24
    want = hmac.new(key.encode(), ("numinia-enlace@1\n" + nonce).encode(), hashlib.sha256).hexdigest()
    from hermes_3d_lib import protocol

    assert protocol.sign_nonce(key, nonce) == want
    assert protocol.sign_nonce(key, nonce) != hmac.new(key.encode(), nonce.encode(), hashlib.sha256).hexdigest()


def test_link_listening_is_a_plain_tcp_probe():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    assert supervisor.link_listening(s.getsockname()[1]) is True
    s.close()
    assert supervisor.link_listening(s.getsockname()[1] if False else 1) is False


def test_no_hardcoded_paths_in_sources():
    bad = ("/var/home", "/home/", "/Users/", "arkhitektonian", "\\.hermes/profiles")
    for p in [
        *list((ROOT / "hermes_3d_lib").glob("*.py")),
        ROOT / "__init__.py",
        ROOT / "dashboard/plugin_api.py",
        ROOT / "desktop/plugin.js",
    ]:
        text = p.read_text()
        for b in bad:
            assert b not in text, f"{p.name} contains {b!r}"


def test_bind_address_is_not_configurable():
    src = (ROOT / "hermes_3d_lib/hub.py").read_text()
    assert "0.0.0.0" not in src and 'P_HOST = "127.0.0.1"' in src
    assert config.HOST == "127.0.0.1"


def test_client_degrades_without_bridge(hermes_home):
    assert client.call("GET", "/status")["error"] == "bridge-not-running"
    config.write_runtime({"pid": 999999, "port": 1, "token": "t"}, hermes_home)
    assert client.call("GET", "/status")["error"] == "bridge-stale"


def test_cli_status_and_usage(hermes_home, capsys):
    mod = load_entry()
    assert mod._cli(SimpleNamespace(hermes3d_cmd=None)) == 2
    assert mod._cli(SimpleNamespace(hermes3d_cmd="status")) == 0
    out = capsys.readouterr().out
    assert "NOT SET (setting engine_path)" in out and "bridge        : not running" in out
    assert f"engine link   : not listening on {LOOP}:8791" in out and "link key      : missing" in out


def test_cli_up_without_engine_is_clean_error(hermes_home, capsys):
    mod = load_entry()
    assert mod._cli(SimpleNamespace(hermes3d_cmd="up")) == 1
    assert "engine_path" in capsys.readouterr().err


# ---- dashboard router ---------------------------------------------------------------------------------
def load_router():
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_hermes-3d", ROOT / "dashboard/plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_router_status_when_bridge_down(hermes_home):
    from conftest import run

    api = load_router()
    st = run(api.status())
    assert st["bridge"] is False and st["connected"] is False and st["switches"]["salud"] is True
    sw = run(api.switches())
    assert {r["name"] for r in sw["functions"]} == {
        "salud",
        "cron",
        "sesiones",
        "agentes",
        "gasto",
        "habilidades",
        "kanban",
        "proyectos",
        "documentos",
    } and sw["connected"] is False
    assert run(api.world())["url"] is None


def test_router_world_url_only_loopback(hermes_home):
    import os

    from conftest import run

    api = load_router()
    config.write_runtime({"pid": os.getpid(), "port": 1, "token": "t", "world_url": "http://evil.example/"}, hermes_home)
    assert run(api.world())["url"] is None
    config.write_runtime({"pid": os.getpid(), "port": 1, "token": "t", "world_url": f"http://{LOOP}:8790/"}, hermes_home)
    assert run(api.world())["url"] == f"http://{LOOP}:8790/"


def test_router_writes_only_through_the_two_named_posts():
    """Everything is a GET except the two doors of path A: the credential for the frame and the request that goes to approval."""
    api = load_router()
    posts = {r.path for r in api.router.routes if "POST" in r.methods}
    assert posts == {"/embed-credential", "/action"}
    assert {m for r in api.router.routes for m in r.methods} == {"GET", "POST"}
    assert not any(m in r.methods for r in api.router.routes for m in ("PUT", "DELETE", "PATCH"))


def test_manifest_carries_the_optional_discovery_metadata(hermes_home):
    """B8: license, homepage, tags, manifest_version and api_version are declared (and are the right types)."""
    import yaml

    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert manifest["license"] == "MIT" and manifest["homepage"].startswith("https://")
    assert isinstance(manifest["tags"], list) and manifest["tags"] and all(isinstance(t, str) for t in manifest["tags"])
    assert manifest["manifest_version"] == 2 and manifest["api_version"] == 1
