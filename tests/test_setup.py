"""`hermes palace setup`: guided, idempotent, writes only after confirmation, never prompts with --yes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_entry import load_entry

from hermes_palace_lib import settings as S
from hermes_palace_lib import setup_cmd


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


# ---- profiles ----------------------------------------------------------------------------------------------
def test_profiles_are_metadata_only_read_only_and_never_open_a_database():
    for name, prof in setup_cmd.PROFILES.items():
        assert prof["allow_direct_db_read"] is False, name
        assert prof.get("publish_spend", False) is False and prof.get("publish_kanban", False) is False, name
        assert prof["publish_sessions"] is False, name  # the live feed is its own decision
    assert setup_cmd.PROFILES["showcase"]["read_only"] is True
    assert setup_cmd.PROFILES["showcase"]["publish_cron"] is True


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
    assert "direct" in text.lower()  # the two that read Hermes' databases say so


# ---- checks -------------------------------------------------------------------------------------------------
def test_checks_report_node_engine_docs_and_hermes(hermes_home, tmp_path, monkeypatch):
    monkeypatch.setattr(setup_cmd.shutil, "which", lambda n: None)
    rows = {r.name: r for r in setup_cmd.run_checks(S.PalaceSettings.from_getter(getter({}), hermes_home))}
    assert not rows["node"].ok and "22.18" in rows["node"].fix
    assert not rows["engine"].ok and "install-engine" in rows["engine"].fix
    assert "docs viewer" in rows and "link key" in rows


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
def test_dry_flow_with_yes_and_a_profile_writes_exactly_the_diff(hermes_home, capsys):
    rec = Recorder()
    rc = setup_cmd.cmd_setup(SimpleNamespace(yes=True, profile="showcase", engine_path=None), home=hermes_home, setter=rec)
    assert rc == 0
    keys = {k.rsplit(".", 1)[1] for k, _ in rec.calls}
    assert keys == {"publish_cron", "publish_agents", "publish_skills", "publish_docs", "read_only"}  # health is already on
    assert all(k.startswith("plugins.entries.hermes-palace.settings.") for k, _ in rec.calls)
    out = capsys.readouterr().out
    assert "metadata only" in out and "allow_direct_db_read" in out and "off" in out


def test_second_run_changes_nothing(hermes_home, capsys):
    store: dict = {}
    rec = Recorder(store)
    setup_cmd.cmd_setup(
        SimpleNamespace(yes=True, profile="showcase", engine_path=None), home=hermes_home, setter=rec, get=getter(store)
    )
    rec2 = Recorder(store)
    setup_cmd.cmd_setup(
        SimpleNamespace(yes=True, profile="showcase", engine_path=None), home=hermes_home, setter=rec2, get=getter(store)
    )
    assert rec2.calls == [] and "nothing to change" in capsys.readouterr().out


def test_yes_without_a_profile_or_engine_path_changes_nothing_and_never_prompts(hermes_home, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("prompted")))
    rec = Recorder()
    assert setup_cmd.cmd_setup(SimpleNamespace(yes=True, profile=None, engine_path=None), home=hermes_home, setter=rec) == 0
    assert rec.calls == [] and "hermes palace demo" in capsys.readouterr().out


def test_interactive_no_answers_writes_nothing(hermes_home, monkeypatch):
    answers = iter(["showcase", "n"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    rec = Recorder()
    assert setup_cmd.cmd_setup(SimpleNamespace(yes=False, profile=None, engine_path=None), home=hermes_home, setter=rec) == 0
    assert rec.calls == []


def test_interactive_yes_writes_after_the_confirmation(hermes_home, monkeypatch):
    answers = iter(["minimal", "y"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    rec = Recorder()
    setup_cmd.cmd_setup(SimpleNamespace(yes=False, profile=None, engine_path=None), home=hermes_home, setter=rec)
    assert rec.calls == [] or all(not k.endswith("publish_spend") for k, _ in rec.calls)  # minimal = what is already the default


def test_no_terminal_means_no_write(hermes_home, monkeypatch):
    def eof(*a):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    rec = Recorder()
    assert (
        setup_cmd.cmd_setup(SimpleNamespace(yes=False, profile="showcase", engine_path=None), home=hermes_home, setter=rec) == 1
    )
    assert rec.calls == []


def test_engine_path_is_validated_before_it_is_written(hermes_home, tmp_path, capsys):
    rec = Recorder()
    with pytest.raises((setup_cmd.SetupError, S.SettingsError)):
        setup_cmd.cmd_setup(
            SimpleNamespace(yes=True, profile=None, engine_path=str(tmp_path / "nope")), home=hermes_home, setter=rec
        )
    assert rec.calls == []
    good_root = engine_checkout(tmp_path)
    ok = setup_cmd.cmd_setup(SimpleNamespace(yes=True, profile=None, engine_path=str(good_root)), home=hermes_home, setter=rec)
    assert ok == 0 and rec.calls == [("plugins.entries.hermes-palace.settings.engine_path", str(good_root))]


def test_a_failed_write_is_reported_not_hidden(hermes_home, capsys):
    rec = Recorder(fail=True)
    rc = setup_cmd.cmd_setup(SimpleNamespace(yes=True, profile="showcase", engine_path=None), home=hermes_home, setter=rec)
    assert rc == 1 and "could not" in capsys.readouterr().err.lower()


def test_unknown_profile_is_refused(hermes_home):
    with pytest.raises(setup_cmd.SetupError):
        setup_cmd.cmd_setup(
            SimpleNamespace(yes=True, profile="everything", engine_path=None), home=hermes_home, setter=Recorder()
        )


def test_setter_uses_an_argv_list_without_a_shell(hermes_home, monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, kw=kw)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(setup_cmd.shutil, "which", lambda n: "/usr/bin/hermes")
    monkeypatch.setattr(setup_cmd.subprocess, "run", fake_run)
    assert setup_cmd.hermes_config_set(hermes_home)("plugins.entries.hermes-palace.settings.read_only", "true")
    assert seen["argv"][:3] == ["/usr/bin/hermes", "config", "set"] and not seen["kw"].get("shell")
    assert seen["kw"]["env"]["HERMES_HOME"] == str(hermes_home)


# ---- wiring -------------------------------------------------------------------------------------------------
def test_setup_is_a_registered_subcommand(hermes_home):
    import argparse

    mod = load_entry()
    p = argparse.ArgumentParser()
    mod._setup_cli(p)
    ns = p.parse_args(["setup", "--yes", "--profile", "showcase"])
    assert ns.palace_cmd == "setup" and ns.yes and ns.profile == "showcase"


def test_up_without_an_engine_points_to_setup_and_to_what_works_without_one(hermes_home, capsys):
    mod = load_entry()
    assert mod._cli(SimpleNamespace(palace_cmd="up")) == 1
    err = capsys.readouterr().err
    assert "engine_path" in err and "hermes palace setup" in err and "hermes palace scan" in err
