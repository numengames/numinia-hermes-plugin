"""Node comes from Hermes' package manager (``pm``); PATH is only the fallback when ``pm`` itself is unavailable."""

from __future__ import annotations

import stat
import types
from pathlib import Path

import pytest

from hermes_3d_lib import nodebin
from hermes_3d_lib.errors import Hermes3DError, NodeMissing


def fake_node(tmp_path: Path) -> Path:
    exe = tmp_path / "pm-node"
    exe.write_text("#!/bin/sh\necho v26.12.1\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


class FakePm(types.SimpleNamespace):
    """Records the calls; ``installed`` is what ``installed_package('node')`` returns."""

    def __init__(self, installed, ensure_error: Exception | None = None):
        super().__init__(installed=installed, ensure_error=ensure_error, calls=[])

    def ensure(self, name, **kw):
        self.calls.append(("ensure", name))
        if self.ensure_error:
            raise self.ensure_error

    def installed_package(self, name, **kw):
        self.calls.append(("installed_package", name))
        return self.installed


def resolve(pm, **kw):
    kw.setdefault("version_of", lambda b: (26, 12, 1))
    kw.setdefault("path_env", lambda: "/pm/bin:/usr/bin")
    return nodebin.resolve_node(pm_loader=lambda: pm, **kw)


def test_found_uses_ensure_then_installed_package_binary_and_hermes_path(tmp_path):
    exe = fake_node(tmp_path)
    pm = FakePm(types.SimpleNamespace(binary=exe, version="26.12.1", path=tmp_path))
    node = resolve(pm)
    assert pm.calls == [("ensure", "node"), ("installed_package", "node")]
    assert node.binary == str(exe) and node.source == "pm" and node.version == (26, 12, 1) and node.warnings == ()
    assert node.path_env == "/pm/bin:/usr/bin"
    assert nodebin.child_env(node, {"PATH": "/x", "SECRET_TOKEN": "nope", "HOME": "/h"}) == {
        "PATH": "/pm/bin:/usr/bin",
        "HOME": "/h",
    }


def test_ensure_false_only_looks(tmp_path):
    pm = FakePm(types.SimpleNamespace(binary=fake_node(tmp_path)))
    resolve(pm, ensure=False)
    assert pm.calls == [("installed_package", "node")]


@pytest.mark.parametrize("installed", [None, types.SimpleNamespace(binary=None), types.SimpleNamespace(binary="/no/such/node")])
def test_pm_without_a_node_is_node_missing_and_never_falls_back_to_path(installed):
    pm = FakePm(installed)
    with pytest.raises(NodeMissing) as e:
        resolve(pm, which=lambda n: pytest.fail("PATH must not be consulted when pm exists"))
    assert e.value.code == "NODE_MISSING" and e.value.retryable is False
    assert "hermes pm doctor" in str(e.value) and str(e.value).startswith("NODE_MISSING: ")


def test_pm_install_error_is_node_missing_with_the_cause():
    class InstallError(RuntimeError):
        cause = "download failed: offline"

    pm = FakePm(None, ensure_error=InstallError("node: download failed: offline"))
    with pytest.raises(NodeMissing, match="offline"):
        resolve(pm, which=lambda n: "/usr/bin/node")


def test_pm_import_fails_falls_back_to_path_node():
    node = nodebin.resolve_node(pm_loader=lambda: None, which=lambda n: "/usr/bin/node", version_of=lambda b: (24, 1, 0))
    assert node.source == "path" and node.binary == "/usr/bin/node" and node.path_env == "" and node.warnings == ()


def test_pm_import_fails_and_no_path_node_is_node_missing():
    with pytest.raises(NodeMissing, match="no package manager"):
        nodebin.resolve_node(pm_loader=lambda: None, which=lambda n: None)


@pytest.mark.parametrize("ver", [(22, 17, 9), (20, 0, 0)])
def test_old_node_warns_but_does_not_block(tmp_path, ver):
    node = resolve(FakePm(types.SimpleNamespace(binary=fake_node(tmp_path))), version_of=lambda b: ver)
    assert node.version == ver and len(node.warnings) == 1 and "22.18.0" in node.warnings[0]


def test_unreadable_version_warns_and_continues(tmp_path):
    node = resolve(FakePm(types.SimpleNamespace(binary=fake_node(tmp_path))), version_of=lambda b: None)
    assert node.version is None and node.version_text == "unknown" and "could not read" in node.warnings[0]


def test_exactly_the_minimum_is_fine(tmp_path):
    assert resolve(FakePm(types.SimpleNamespace(binary=fake_node(tmp_path))), version_of=lambda b: (22, 18, 0)).warnings == ()


def test_read_version_runs_the_binary(tmp_path):
    assert nodebin.read_version(str(fake_node(tmp_path))) == (26, 12, 1)
    assert nodebin.read_version("/no/such/binary") is None
    bad = tmp_path / "bad"
    bad.write_text("#!/bin/sh\necho hello\n")
    bad.chmod(0o755)
    assert nodebin.read_version(str(bad)) is None


def test_default_loader_is_the_conftest_stub_and_a_real_pm_is_never_reached():
    assert nodebin._load_pm() is None  # conftest replaced it: a test cannot download a real Node


def test_hermes_path_env_degrades_quietly(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "hermes_constants", types.SimpleNamespace(with_hermes_node_path=lambda: {"PATH": "/a:/b"}))
    assert nodebin._hermes_path_env() == "/a:/b"

    def boom():
        raise OSError("x")

    monkeypatch.setitem(sys.modules, "hermes_constants", types.SimpleNamespace(with_hermes_node_path=boom))
    assert nodebin._hermes_path_env() == ""
    monkeypatch.setitem(sys.modules, "hermes_constants", None)  # import raises ImportError
    assert nodebin._hermes_path_env() == ""


def test_error_base_class():
    assert issubclass(NodeMissing, Hermes3DError)
    e = Hermes3DError("x", code="C", retryable=True)
    assert e.code == "C" and e.retryable is True and str(e) == "C: x"
