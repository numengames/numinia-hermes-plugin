# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""Session context (platform/model/role) learned from pre_llm_call, pre_api_request and subagent_start; Desktop broadcast."""

from __future__ import annotations

import json

from hermes_3d_lib import events


def hooks(tmp_path, sent):
    return events.make_hooks(lambda: True, tmp_path, broadcast=lambda name, payload: sent.append((name, payload)))


def rows(tmp_path):
    return [json.loads(x) for x in events.spool_path(tmp_path).read_text().splitlines()]


def test_tool_events_take_platform_and_model_from_pre_llm_call(tmp_path):
    sent: list = []
    h = hooks(tmp_path, sent)
    h["pre_llm_call"](session_id="s1", platform="telegram", model="m-1", user_message="SECRET words")
    h["pre_tool_call"](tool_name="terminal", session_id="s1", args={"command": "SECRET"}, tool_call_id="c1", turn_id="t1")
    ev = rows(tmp_path)[-1]
    assert (ev["platform"], ev["model"], ev["tool"]) == ("telegram", "m-1", "terminal")
    assert "SECRET" not in json.dumps(rows(tmp_path)) + json.dumps(sent)


def test_subagent_inherits_the_parent_context_and_carries_its_role(tmp_path):
    sent: list = []
    h = hooks(tmp_path, sent)
    h["pre_api_request"](session_id="p", platform="cron", model="m-2")
    h["subagent_start"](
        parent_session_id="p", child_session_id="c", child_subagent_id="sa1", child_role="leaf", child_goal="SECRET goal"
    )
    h["pre_tool_call"](tool_name="read_file", session_id="c")
    ev = rows(tmp_path)[-1]
    assert (ev["platform"], ev["model"], ev["role"]) == ("cron", "m-2", "leaf")
    h["subagent_stop"](
        parent_session_id="p", child_session_id="c", child_status="completed", duration_ms=12, child_summary="SECRET"
    )
    assert rows(tmp_path)[-1]["status"] == "completed"
    assert "SECRET" not in json.dumps(rows(tmp_path)) + json.dumps(sent)


def test_every_event_is_also_broadcast_with_a_valid_plugin_event_name(tmp_path):
    sent: list = []
    h = hooks(tmp_path, sent)
    h["pre_llm_call"](session_id="s", platform="cli", model="m")
    h["pre_tool_call"](tool_name="x", session_id="s")
    h["post_tool_call"](tool_name="x", session_id="s", duration_ms=3)
    h["subagent_start"](parent_session_id="s", child_session_id="c", child_subagent_id="a", child_role="leaf")
    h["subagent_stop"](parent_session_id="s", child_session_id="c", child_status="ok", duration_ms=1)
    names = [n for n, _ in sent]
    assert names == ["tool.started", "tool.finished", "agent.started", "agent.stopped"]
    assert all(set(p) <= set(events.FIELDS) | {"id", "ts", "kind"} for _, p in sent)
    assert [r["kind"] for r in rows(tmp_path)] == ["tool_start", "tool_end", "subagent_start", "subagent_stop"]


def test_nothing_is_broadcast_while_the_switch_is_off(tmp_path):
    sent: list = []
    h = events.make_hooks(lambda: False, tmp_path, broadcast=lambda n, p: sent.append(n))
    h["pre_tool_call"](tool_name="x", session_id="s")
    assert sent == [] and not events.spool_path(tmp_path).exists()


def test_a_broken_broadcast_never_reaches_the_agent(tmp_path):
    def boom(n, p):
        raise RuntimeError("x")

    h = events.make_hooks(lambda: True, tmp_path, broadcast=boom)
    h["pre_tool_call"](tool_name="x", session_id="s")  # must not raise
    assert rows(tmp_path)[-1]["tool"] == "x"


def test_the_default_broadcast_is_a_noop_without_hermes_serve(tmp_path, monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "hermes_cli.plugin_events", None)  # import fails: not under Desktop
    assert events.desktop_broadcast("tool.started", {"tool": "x"}) is False


def test_state_endpoint_is_metadata_only_and_works_with_the_bridge_down(hermes_home):
    from conftest import run
    from test_entry import load_router

    api = load_router()
    out = run(api.state())
    assert out["bridge"] is False and out["scan"] == {} and out["events"] == []
    assert out["switches"]["salud"] is True
