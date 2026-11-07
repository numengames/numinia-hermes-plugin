"""Data folder = Hermes' per-plugin storage; the pre-0.5 folder is moved once, safely."""

from __future__ import annotations

import os
import stat

from hermes_palace_lib import config


def test_layout_follows_the_host_convention(hermes_home):
    assert config.data_dir_path(hermes_home) == hermes_home / "plugin-data" / "hermes-palace"
    d = config.palace_dir(hermes_home)
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
    assert config.palace_dir() == config.data_dir_path(hermes_home) and calls == ["hermes-palace"]
    mod.plugin_data_dir = lambda name: hermes_home / "elsewhere"  # a host that disagrees: our own layout wins
    assert config.palace_dir() == config.data_dir_path(hermes_home)
    mod.plugin_data_dir = lambda name: (_ for _ in ()).throw(OSError("no"))  # a host that breaks: same
    assert config.palace_dir() == config.data_dir_path(hermes_home)


def test_migration_moves_known_entries_once_with_owner_only_modes(hermes_home):
    old = hermes_home / "hermes-palace"
    (old / "mundo").mkdir(parents=True)
    (old / "mundo" / "world.txt").write_text("w")
    (old / "link.key").write_text("k")
    (old / "link.key").chmod(0o666)
    (old / "runtime.json").write_text("{}")
    (old / "stranger.txt").write_text("not ours")
    moved = config.migrate_legacy_dir(hermes_home)
    new = config.data_dir_path(hermes_home)
    assert sorted(moved) == ["link.key", "mundo", "runtime.json"]
    assert (new / "mundo" / "world.txt").read_text() == "w" and (new / "link.key").read_text() == "k"
    assert stat.S_IMODE((new / "link.key").stat().st_mode) == 0o600 and stat.S_IMODE((new / "mundo").stat().st_mode) == 0o700
    assert (old / "stranger.txt").exists()  # unknown names are never touched, so the old folder stays
    assert config.migrate_legacy_dir(hermes_home) == []  # once


def test_migration_removes_the_empty_old_folder_and_never_overwrites(hermes_home):
    old, new = hermes_home / "hermes-palace", config.data_dir_path(hermes_home)
    old.mkdir()
    new.mkdir(parents=True)
    (old / "link.key").write_text("old")
    (new / "link.key").write_text("new")
    assert config.migrate_legacy_dir(hermes_home) == []
    assert (new / "link.key").read_text() == "new" and (old / "link.key").read_text() == "old"
    (old / "link.key").unlink()
    config.migrate_legacy_dir(hermes_home)
    assert not old.exists()


def test_migration_never_follows_symlinks(hermes_home, tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("outside")
    secret.chmod(0o644)
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    old = hermes_home / "hermes-palace"
    old.mkdir()
    os.symlink(secret, old / "link.key")
    os.symlink(outside_dir, old / "mundo")
    assert config.migrate_legacy_dir(hermes_home) == []
    assert not (config.data_dir_path(hermes_home) / "link.key").exists()
    assert stat.S_IMODE(secret.stat().st_mode) == 0o644 and stat.S_IMODE(outside_dir.stat().st_mode) != 0o700
    # the old folder itself being a symlink is not followed either
    old2 = tmp_path / "elsewhere"
    old2.mkdir()
    (old2 / "link.key").write_text("x")
    (hermes_home / "hermes-palace").unlink(missing_ok=True) if (hermes_home / "hermes-palace").is_symlink() else None
    for child in (hermes_home / "hermes-palace").iterdir():
        child.unlink()
    (hermes_home / "hermes-palace").rmdir()
    os.symlink(old2, hermes_home / "hermes-palace")
    assert config.migrate_legacy_dir(hermes_home) == [] and (old2 / "link.key").exists()


def test_settings_default_world_lives_in_the_new_folder_after_migration(hermes_home):
    from hermes_palace_lib import settings as S

    (hermes_home / "hermes-palace" / "mundo").mkdir(parents=True)
    cfg = S.PalaceSettings.from_getter(lambda k, d=None: d, hermes_home)
    assert cfg.world_path == hermes_home / "plugin-data" / "hermes-palace" / "mundo" and cfg.world_path.is_dir()
