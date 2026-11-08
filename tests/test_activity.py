"""Live-activity feed (metadata only), requires_env / requires_hermes (review points a, c, d)."""

from __future__ import annotations

import json
import os

import yaml
from conftest import ROOT, run
from test_entry import FakeCtx, load_entry, load_router

from hermes_palace_lib import events

SECRET = "SECRET-PROMPT-TEXT-sk-ant-0123456789"


def _spool(home):
    return [json.loads(x) for x in events.spool_path(home).read_text().splitlines()]


def _on(home):
    return events.make_hooks(lambda: True, home)


# ---- the allow-list -------------------------------------------------------------------------------------
def test_make_event_drops_everything_not_allow_listed():
    ev = events.make_event("tool_end", tool="terminal", args={"command": SECRET}, result=SECRET, goal=SECRET, summary=SECRET)
    assert set(ev) <= {"id", "ts", "kind", *events.FIELDS}
    assert SECRET not in json.dumps(ev)


def test_make_event_cuts_and_cleans_untrusted_text():
    ev = events.make_event("tool_start", tool="a\x1b[31m\nb" + "x" * 500, platform="cli\r\n\tx")
    assert "\n" not in ev["tool"] and "\x1b" not in ev["tool"] and len(ev["tool"]) <= 64
    assert ev["platform"] == "cli x"


def test_unknown_kind_is_refused():
    try:
        events.make_event("secrets")
    except ValueError:
        return
    raise AssertionError("an unknown kind must not be recorded")


# ---- the hooks ------------------------------------------------------------------------------------------
def test_hooks_record_tool_names_only_never_args_or_results(hermes_home):
    h = _on(hermes_home)
    h["pre_api_request"](
        session_id="s1", platform="cron", request_messages=[{"content": SECRET}], system_prompt=SECRET, user_message=SECRET
    )
    h["pre_tool_call"](tool_name="terminal", args={"command": SECRET}, session_id="s1", tool_call_id="c1")
    h["post_tool_call"](
        tool_name="terminal", args={"command": SECRET}, result=SECRET, session_id="s1", status="ok", duration_ms=42
    )
    raw = events.spool_path(hermes_home).read_text()
    assert SECRET not in raw and "sk-ant" not in raw
    kinds = [e["kind"] for e in _spool(hermes_home)]
    assert kinds == ["api_request", "tool_start", "tool_end"]
    end = _spool(hermes_home)[-1]
    assert end["tool"] == "terminal" and end["status"] == "ok" and end["duration_ms"] == 42


def test_tool_events_borrow_the_platform_of_their_session(hermes_home):
    h = _on(hermes_home)
    h["on_session_start"](session_id="s9", platform="telegram")
    h["pre_tool_call"](tool_name="read_file", session_id="s9")
    h["pre_tool_call"](tool_name="read_file", session_id="unknown")
    a, b = _spool(hermes_home)
    assert a["platform"] == "telegram" and b["platform"] == ""


def test_subagent_events_carry_ids_role_status_but_not_goal_summary_or_history(hermes_home):
    h = _on(hermes_home)
    h["on_session_start"](session_id="p1", platform="cron")
    h["subagent_start"](
        parent_session_id="p1",
        child_session_id="c1",
        child_subagent_id="sa-1",
        parent_subagent_id="",
        child_role="leaf",
        child_goal=SECRET,
    )
    h["pre_tool_call"](tool_name="web_search", session_id="c1")
    h["subagent_stop"](
        parent_session_id="p1",
        child_session_id="c1",
        child_role="leaf",
        child_status="completed",
        child_summary=SECRET,
        tool_call_history=[{"args": SECRET}],
        duration_ms=1500,
    )
    assert SECRET not in events.spool_path(hermes_home).read_text()
    start, tool, stop = _spool(hermes_home)
    assert start["subagent"] == "sa-1" and start["platform"] == "cron" and start["role"] == "leaf"
    assert tool["platform"] == "cron"  # inherited from the parent through the child session
    assert stop["status"] == "completed" and stop["duration_ms"] == 1500


def test_profile_comes_from_the_hermes_home(tmp_path):
    prof = tmp_path / "profiles" / "ursa"
    prof.mkdir(parents=True)
    h = events.make_hooks(lambda: True, prof)
    h["pre_tool_call"](tool_name="t", session_id="x")
    assert _spool(prof)[0]["profile"] == "ursa"
    assert events.profile_name(tmp_path) == "default"


def test_nothing_is_written_while_the_switch_is_off(hermes_home):
    h = events.make_hooks(lambda: False, hermes_home)
    h["pre_tool_call"](tool_name="terminal", session_id="s")
    assert not events.spool_path(hermes_home).exists()


def test_a_failing_gate_never_breaks_the_agent(hermes_home):
    def boom():
        raise RuntimeError("config unreadable")

    events.make_hooks(boom, hermes_home)["pre_tool_call"](tool_name="terminal", session_id="s")


# ---- the bounded spool ----------------------------------------------------------------------------------
def test_spool_is_bounded_private_and_keeps_the_newest(hermes_home):
    for i in range(3000):
        events.append(events.make_event("tool_start", tool=f"tool-{i}", session="s" * 60), hermes_home)
    path = events.spool_path(hermes_home)
    assert path.stat().st_size <= events.MAX_BYTES + 1024
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    got = events.read(0, 500, hermes_home)["events"]
    assert len(got) <= events.MAX_LIMIT and got[-1]["tool"] == "tool-2999"


def test_read_since_returns_only_newer_events_and_a_cursor(hermes_home):
    for n in ("a", "b", "c"):
        events.append(events.make_event("tool_start", tool=n), hermes_home)
    first = events.read(0, 100, hermes_home)
    assert [e["tool"] for e in first["events"]] == ["a", "b", "c"]
    assert events.read(first["events"][0]["id"], 100, hermes_home)["events"][0]["tool"] == "b"
    again = events.read(first["next"], 100, hermes_home)
    assert again["events"] == [] and again["next"] == first["next"]


def test_read_skips_garbage_and_foreign_kinds(hermes_home):
    p = events.spool_path(hermes_home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        'not json\n{"id": 5, "kind": "secret", "tool": "x"}\n[1]\n{"id": 6, "kind": "tool_start", "tool": "ok", "args": "leak"}\n'
    )
    got = events.read(0, 10, hermes_home)["events"]
    assert [e["tool"] for e in got] == ["ok"] and "args" not in got[0]


def test_read_without_a_spool_is_empty(hermes_home):
    assert events.read(0, 10, hermes_home) == {"events": [], "next": 0}


# ---- wired into the plugin ------------------------------------------------------------------------------
def test_register_wires_the_activity_hooks_and_the_manifest_lists_them(hermes_home):
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    ctx = FakeCtx()
    load_entry().register(ctx)
    for name in ("subagent_start", "subagent_stop", "pre_tool_call", "post_tool_call", "pre_api_request"):
        assert name in ctx.hooks and name in manifest["provides_hooks"], name


def test_registered_hooks_follow_the_sessions_switch_and_keep_the_approval_directive(hermes_home):
    ctx = FakeCtx()
    load_entry().register(ctx)
    ctx.hooks["post_tool_call"](tool_name="terminal", session_id="s", status="ok", duration_ms=1)
    assert not events.spool_path(hermes_home).exists()  # sesiones is off by default
    ctx.settings["publish_sessions"] = True
    assert ctx.hooks["pre_tool_call"](tool_name="terminal", args={"command": SECRET}, session_id="s") is None
    d = ctx.hooks["pre_tool_call"](
        tool_name="palace_cron_action", args={"job_id": "8bc0eecea9b5", "verb": "cron.pausar"}, session_id="s"
    )
    assert d and d["action"] == "approve"
    assert [e["tool"] for e in _spool(hermes_home)] == ["terminal", "palace_cron_action"]
    assert SECRET not in events.spool_path(hermes_home).read_text()


def test_events_route_is_metadata_only_and_empty_while_off(hermes_home):
    api = load_router()
    assert run(api.events())["enabled"] is False
    events.append(events.make_event("tool_start", tool="terminal", platform="cli"), hermes_home)
    off = run(api.events())
    assert off["enabled"] is False and off["events"] == []
    (hermes_home / "config.yaml").write_text(
        "plugins:\n  entries:\n    hermes-palace:\n      settings:\n        publish_sessions: true\n"
    )
    on = run(api.events())
    assert on["enabled"] is True and [e["tool"] for e in on["events"]] == ["terminal"]
    assert run(api.events(since=on["next"]))["events"] == []


# ---- requires_env / requires_hermes ---------------------------------------------------------------------
def test_manifest_blocks_nothing_on_missing_env_and_pins_a_range():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert "requires_env" not in manifest, "the plugin makes its own link key; no env var may gate its load"
    assert [e["name"] for e in manifest["optional_env"]] == ["HERMES_PALACE_LINK_KEY"]
    assert manifest["config_schema"]["link_key"]["type"] == "secret"  # the user-facing way to set it
    spec = manifest["requires_hermes"]
    assert spec.startswith(">=") and "<" in spec


def test_plugin_loads_and_registers_with_no_environment_at_all(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith(("HERMES_PALACE_", "MOTOR_ENLACE_")):
            monkeypatch.delenv(k)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    ctx = FakeCtx()
    load_entry().register(ctx)  # must not raise: no LINK_KEY, no engine, nothing
    assert "palace_state" in ctx.tools
    from hermes_palace_lib import linkkey

    assert len(linkkey.ensure(home)) >= 32  # the plugin makes its own key on first use
    assert linkkey.ENV_LINK_KEY not in os.environ


def test_requires_hermes_range_is_accepted_by_the_hermes_validator_and_gate(monkeypatch):
    import pytest

    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    if os.environ.get("HERMES_AGENT_DIR"):
        monkeypatch.syspath_prepend(os.environ["HERMES_AGENT_DIR"])
    pm = pytest.importorskip("hermes_cli.plugins_manifest", reason="needs a Hermes checkout on sys.path")
    pv = pytest.importorskip("hermes_cli.plugin_validate")
    spec = manifest["requires_hermes"]
    assert pv._requires_hermes_spec_valid(spec)
    assert pm.version_satisfies(spec, "0.22.7") and not pm.version_satisfies(spec, "0.21.9")
    assert (
        pm.version_satisfies(spec, "0.23.9")
        and not pm.version_satisfies(spec, "0.24.0")
        and not pm.version_satisfies(spec, "1.0.0")
    )
