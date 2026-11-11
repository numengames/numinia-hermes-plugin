# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""Link key: environment first, else the plugin's own 0600 key file. Never another program's credential file."""

from __future__ import annotations

import os
import stat

from hermes_3d_lib import linkkey


def test_no_key_anywhere_is_none(hermes_home):
    assert linkkey.resolve(hermes_home) is None and linkkey.source(hermes_home) == "missing"


def test_enlace_env_in_a_world_is_never_read(hermes_home, tmp_path):
    world = tmp_path / "w"
    world.mkdir()
    (world / "enlace.env").write_text("MOTOR_ENLACE_CLAVE=k3y-from-engine-file-0123\n")
    assert linkkey.resolve(hermes_home) is None


def test_generated_key_is_private_stable_and_valid(hermes_home):
    key = linkkey.ensure(hermes_home)
    assert linkkey.valid(key) and linkkey.ensure(hermes_home) == key == linkkey.resolve(hermes_home)
    path = linkkey.key_path(hermes_home)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert linkkey.source(hermes_home) == "plugin key file"


def test_key_file_is_private_whatever_the_umask(hermes_home):
    old = os.umask(0)
    try:
        linkkey.ensure(hermes_home)
    finally:
        os.umask(old)
    assert stat.S_IMODE(linkkey.key_path(hermes_home).stat().st_mode) == 0o600


def test_environment_wins_and_short_values_are_ignored(hermes_home, monkeypatch):
    own = linkkey.ensure(hermes_home)
    monkeypatch.setenv(linkkey.ENV_LINK_KEY, "k3y-from-env-0123456789")
    assert linkkey.resolve(hermes_home) == "k3y-from-env-0123456789"
    assert linkkey.source(hermes_home) == "environment (HERMES_3D_LINK_KEY)"
    monkeypatch.setenv(linkkey.ENV_LINK_KEY, "short")
    assert linkkey.resolve(hermes_home) == own


def test_loose_or_symlinked_key_file_is_not_trusted(hermes_home, tmp_path):
    path = linkkey.key_path(hermes_home)
    path.write_text("x" * 40 + "\n")
    path.chmod(0o644)
    assert linkkey.resolve(hermes_home) is None
    path.unlink()
    target = tmp_path / "t"
    target.write_text("y" * 40 + "\n")
    target.chmod(0o600)
    path.symlink_to(target)
    assert linkkey.resolve(hermes_home) is None
    new = linkkey.ensure(hermes_home)  # replaces the symlink with a real private file
    assert not path.is_symlink() and linkkey.valid(new) and target.read_text().startswith("y")
