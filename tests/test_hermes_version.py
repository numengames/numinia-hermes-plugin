# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT
"""The version label the bridge sends to the engine comes from package metadata, never from a Hermes internal."""

from __future__ import annotations

import ast
import importlib.metadata
from pathlib import Path

from hermes_3d_lib import bridge_main

ROOT = Path(__file__).resolve().parents[1]


def test_version_comes_from_package_metadata(monkeypatch):
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.23.1" if name == "hermes-agent" else "")
    assert bridge_main._hermes_version() == "0.23.1"


def test_version_is_a_question_mark_when_hermes_is_not_a_package(monkeypatch):
    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    assert bridge_main._hermes_version() == "?"


def test_no_module_imports_hermes_cli_outside_the_documented_api():
    """Catalog rule 9: the only Hermes modules the shipped code imports are the published plugin API ones."""
    allowed = {"hermes_cli.plugin_events", "agent.secret_scope", "hermes_constants"}
    offenders = []
    for path in sorted([*ROOT.glob("hermes_3d_lib/*.py"), ROOT / "__init__.py", *ROOT.glob("dashboard/*.py")]):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                if top in {"hermes_cli", "agent", "gateway", "tools", "cron", "run_agent", "model_tools"} and name not in allowed:
                    offenders.append(f"{path.relative_to(ROOT)}:{getattr(node, 'lineno', 0)} {name}")
    assert offenders == []
