# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""`hermes 3d setup`: guided, idempotent, writes only after confirmation, never prompts with --yes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_entry import load_entry

from hermes_3d_lib import settings as S
from hermes_3d_lib import setup_cmd


class Recorder:
    """Stands in for `hermes config set <key> <value>`: records the call and applies it to a settings dict."""

    def __init__(self, store: dict | None = None, fail: bool = False):
        self.calls, self.store, self.fail = [], store if store is not None else {}, fail

    def __call__(self, key: str, value: str) -> bool:
        self.calls.append((key, value))
        if self.fail:
            return False
        self.store[key.rsplit(".", 1)[1]] = {"true": True, "false": False}.get(value, value)
        return True


def getter(store):
    return lambda k, d=None: store.get(k, d)


def engine_checkout(tmp_path):
    root = tmp_path / "engine"
    (root / "packages" / "server" / "src").mkdir(parents=True)
    (root / "packages" / "server" / "src" / "main.ts").write_text("//")
    return root


# ---- the default set ---------------------------------------------------------------------------------------
def test_default_set_is_metadata_only_read_only_and_never_opens_a_database():
    d = setup_cmd.DEFAULT_SET
    assert d["allow_direct_db_read"] is False
    assert d["publish_spend"] is False and d["publish_kanban"] is False and d["publish_projects"] is False
    assert d["publish_sessions"] is False  # the live feed is its own decision
    assert d["read_only"] is True and d["publish_cron"] is True and d["publish_health"] is True


def test_there_is_no_preset_question_or_flag():
    import argparse

    assert not hasattr(setup_cmd, "PROFILES") and not hasattr(setup_cmd, "PROFILE_TEXT")
    p = argparse.ArgumentParser()
    load_entry()._setup_cli(p)
    with pytest.raises(SystemExit):
        p.parse_args(["setup", "--profile", "showcase"])


def test_catalog_default_stays_everything_off_except_health():
    import yaml
    from conftest import ROOT

    schema = yaml.safe_load((ROOT / "plugin.yaml").read_text())["config_schema"]
    on = [k for k, v in schema.items() if k.startswith("publish_") and v["default"] is True]
    assert on == ["publish_health"] and schema["read_only"]["default"] is False
    assert schema["allow_direct_db_read"]["default"] is False


def test_every_switch_has_a_plain_description():
    text = setup_cmd.describe_switches()
    for key in S.SWITCH_KEYS.values():
        assert key in text
    assert "direct" in text.lower()  # the ones that read Hermes' databases say so
    assert "Health (publish_health)" in text  # the human name comes before the config key
    assert "Spend, Kanban and Projects" in text  # allow_direct_db_read names all three


# ---- checks -------------------------------------------------------------------------------------------------
def test_checks_report_node_engine_docs_and_hermes(hermes_home, tmp_path, monkeypatch):
    monkeypatch.setattr(setup_cmd.shutil, "which", lambda n: None)
    rows = {r.name: r for r in setup_cmd.run_checks(S.Hermes3DSettings.from_getter(getter({}), hermes_home))}
    assert not rows["node"].ok and "22.18" in rows["node"].fix
    assert not rows["engine"].ok and "install-engine" in rows["engine"].fix
    assert "docs viewer" in rows and "link key" in rows
    assert rows["engine"].mark == "  " and rows["docs viewer"].mark == "--"  # expected on a first run, not errors
    assert "checkout" not in rows["engine"].detail and rows["link key"].ok and "missing" not in rows["link key"].detail


def test_node_version_is_parsed_and_compared(monkeypatch):
    for out, ok in (
        ("v24.1.0\n", True),
        ("v26.12.1", True),
        ("v22.18.0", True),
        ("v22.17.9", False),
        ("v22.4.0", False),
        ("garbage", False),
    ):
        monkeypatch.setattr(setup_cmd, "_node_version", lambda out=out: out)
        assert setup_cmd.node_ok() is ok, out


# ---- the command --------------------------------------------------------------------------------------------
def test_yes_writes_exactly_the_diff_of_the_default_set(hermes_home, capsys):
    rec = Recorder()
    rc = setup_cmd.cmd_setup(SimpleNamespace(yes=True, engine_path=None), home=hermes_home, setter=rec)
    assert rc == 0
    keys = {k.rsplit(".", 1)[1] for k, _ in rec.calls}
    assert keys == {"publish_cron", "publish_agents", "publish_skills", "publish_docs", "read_only"}  # health is already on
    assert all(k.startswith("plugins.entries.hermes-3d.settings.") for k, _ in rec.calls)
    out = capsys.readouterr().out
    assert "never your messages" in out and "Direct database read stays off" in out
    assert "Presets" not in out and "profile" not in out.lower().replace("hermes profiles", "")


def test_second_run_changes_nothing(hermes_home, capsys):
    store: dict = {}
    rec = Recorder(store)
    setup_cmd.cmd_setup(SimpleNamespace(yes=True, engine_path=None), home=hermes_home, setter=rec, get=getter(store))
    rec2 = Recorder(store)
    setup_cmd.cmd_setup(SimpleNamespace(yes=True, engine_path=None), home=hermes_home, setter=rec2, get=getter(store))
    assert rec2.calls == [] and "Nothing to change" in capsys.readouterr().out


def test_yes_never_prompts(hermes_home, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("prompted")))
    rec = Recorder()
    assert setup_cmd.cmd_setup(SimpleNamespace(yes=True, engine_path=None), home=hermes_home, setter=rec) == 0
    assert rec.calls and "hermes 3d demo" in capsys.readouterr().out


def test_interactive_asks_one_question_only_and_no_writes_nothing(hermes_home, monkeypatch):
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        return "n"

    monkeypatch.setattr("builtins.input", fake_input)
    rec = Recorder()
    assert setup_cmd.cmd_setup(SimpleNamespace(yes=False, engine_path=None), home=hermes_home, setter=rec) == 0
    assert rec.calls == [] and len(prompts) == 1 and "profile" not in prompts[0].lower()


def test_interactive_yes_writes_after_the_confirmation_and_says_how_to_change_later(hermes_home, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    rec = Recorder()
    setup_cmd.cmd_setup(SimpleNamespace(yes=False, engine_path=None), home=hermes_home, setter=rec)
    assert {k.rsplit(".", 1)[1] for k, _ in rec.calls} == {
        "publish_cron",
        "publish_agents",
        "publish_skills",
        "publish_docs",
        "read_only",
    }
    assert "Settings > Plugins" in capsys.readouterr().out


def test_no_terminal_means_no_write(hermes_home, monkeypatch):
    def eof(*a):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    rec = Recorder()
    assert setup_cmd.cmd_setup(SimpleNamespace(yes=False, engine_path=None), home=hermes_home, setter=rec) == 1
    assert rec.calls == []


def test_engine_path_is_validated_before_it_is_written(hermes_home, tmp_path, capsys):
    rec = Recorder()
    with pytest.raises((setup_cmd.SetupError, S.SettingsError)):
        setup_cmd.cmd_setup(SimpleNamespace(yes=True, engine_path=str(tmp_path / "nope")), home=hermes_home, setter=rec)
    assert rec.calls == []
    good_root = engine_checkout(tmp_path)
    ok = setup_cmd.cmd_setup(SimpleNamespace(yes=True, engine_path=str(good_root)), home=hermes_home, setter=rec)
    assert ok == 0 and ("plugins.entries.hermes-3d.settings.engine_path", str(good_root)) in rec.calls


def test_a_failed_write_is_reported_not_hidden(hermes_home, capsys):
    rec = Recorder(fail=True)
    rc = setup_cmd.cmd_setup(SimpleNamespace(yes=True, engine_path=None), home=hermes_home, setter=rec)
    assert rc == 1 and "could not" in capsys.readouterr().err.lower()


def test_setter_uses_an_argv_list_without_a_shell(hermes_home, monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, kw=kw)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(setup_cmd.shutil, "which", lambda n: "/usr/bin/hermes")
    monkeypatch.setattr(setup_cmd.subprocess, "run", fake_run)
    assert setup_cmd.hermes_config_set(hermes_home)("plugins.entries.hermes-3d.settings.read_only", "true")
    assert seen["argv"][:3] == ["/usr/bin/hermes", "config", "set"] and not seen["kw"].get("shell")
    assert seen["kw"]["env"]["HERMES_HOME"] == str(hermes_home)


# ---- wiring -------------------------------------------------------------------------------------------------
def test_setup_is_a_registered_subcommand(hermes_home):
    import argparse

    mod = load_entry()
    p = argparse.ArgumentParser()
    mod._setup_cli(p)
    ns = p.parse_args(["setup", "--yes"])
    assert ns.hermes3d_cmd == "setup" and ns.yes


def test_up_without_an_engine_points_to_setup_and_to_what_works_without_one(hermes_home, capsys):
    mod = load_entry()
    assert mod._cli(SimpleNamespace(hermes3d_cmd="up")) == 1
    err = capsys.readouterr().err
    assert "engine_path" in err and "hermes 3d setup" in err and "hermes 3d scan" in err
