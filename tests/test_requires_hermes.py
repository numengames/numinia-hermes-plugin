"""The Hermes version range is the same string in plugin.yaml, pyproject.toml and the catalog draft, and Hermes accepts it."""

from __future__ import annotations

import importlib.util
import os
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WANT = ">=0.22.0, <0.24"


def _three() -> dict[str, str]:
    return {
        "plugin.yaml": yaml.safe_load((ROOT / "plugin.yaml").read_text())["requires_hermes"],
        "pyproject.toml": tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["hermes-palace"]["requires_hermes"],
        "catalog draft": yaml.safe_load((ROOT / "docs/plugin-catalog/hermes-palace.yaml").read_text())["requires_hermes"],
    }


def test_the_three_declarations_do_not_drift():
    got = _three()
    assert set(got.values()) == {WANT}, got


def test_catalog_draft_passes_the_nous_catalog_validator(monkeypatch):
    agent = os.environ.get("HERMES_AGENT_DIR")
    script = Path(agent or "/nonexistent") / "scripts" / "validate_plugin_catalog.py"
    if not script.is_file():
        pytest.skip("needs a Hermes checkout (HERMES_AGENT_DIR)")
    spec = importlib.util.spec_from_file_location("nous_validate_catalog", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "nous_validate_catalog", mod)
    spec.loader.exec_module(mod)
    entry = yaml.safe_load((ROOT / "docs/plugin-catalog/hermes-palace.yaml").read_text())
    entry["sha"] = "0" * 40  # the draft carries placeholders until the repository is public
    entry["repo"] = "https://github.com/example/hermes-palace"
    entry["docs_url"] = entry["repo"]
    entry["maintainer"] = "example"
    errors, _warnings = mod.validate_entry(entry)
    assert errors == []
