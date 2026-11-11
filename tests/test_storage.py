"""Data folder = Hermes' per-plugin storage."""

from __future__ import annotations

import stat

from hermes_3d_lib import config


def test_layout_follows_the_host_convention(hermes_home):
    assert config.data_dir_path(hermes_home) == hermes_home / "plugin-data" / "hermes-3d"
    d = config.plugin_data_dir(hermes_home)
    assert d == config.data_dir_path(hermes_home) and stat.S_IMODE(d.stat().st_mode) == 0o700


def test_host_plugin_data_dir_agrees_with_ours(hermes_home, monkeypatch):
    import sys
    import types

    calls = []
    mod = types.ModuleType("plugins.plugin_storage")
    mod.plugin_data_dir = lambda name: calls.append(name) or (hermes_home / "plugin-data" / name)
    monkeypatch.setitem(sys.modules, "plugins", types.ModuleType("plugins"))
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", mod)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    assert config.plugin_data_dir() == config.data_dir_path(hermes_home) and calls == ["hermes-3d"]
    mod.plugin_data_dir = lambda name: hermes_home / "elsewhere"  # a host that disagrees: our own layout wins
    assert config.plugin_data_dir() == config.data_dir_path(hermes_home)
    mod.plugin_data_dir = lambda name: (_ for _ in ()).throw(OSError("no"))  # a host that breaks: same
    assert config.plugin_data_dir() == config.data_dir_path(hermes_home)
