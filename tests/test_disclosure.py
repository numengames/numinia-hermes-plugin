# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""Every module that starts a process is named in the README table of subprocesses (the scanner flags each one)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_every_module_using_subprocess_is_in_the_readme_table():
    users = {
        p.name
        for p in (ROOT / "hermes_3d_lib").glob("*.py")
        if re.search(r"\bsubprocess\.(run|Popen|call|check_)", p.read_text())
    }
    users |= {p.name for p in ROOT.glob("*.py") if re.search(r"\bsubprocess\.", p.read_text())}
    readme = (ROOT / "README.md").read_text()
    table = readme[readme.index("### Every place the plugin starts a process") :]
    table = table[: table.index("\n## ")]
    listed = set(re.findall(r"\| `([a-z_]+\.py)` \|", table))
    assert users <= listed, f"undocumented subprocess users: {sorted(users - listed)}"
    assert listed <= {p.name for p in (ROOT / "hermes_3d_lib").glob("*.py")}


def test_no_address_port_literal_or_secrets_file_path_ships():
    pattern = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{2,5}|~/\.hermes/\.env|\$HOME/\.hermes/\.env")
    skip = {".git", ".mypy_cache", ".ruff_cache", "__pycache__", "node_modules"}
    hits = []
    for p in ROOT.rglob("*"):
        if (
            p.is_file()
            and not skip & set(p.relative_to(ROOT).parts)
            and p.suffix in {".py", ".md", ".yaml", ".json", ".mjs", ".js", ".sh", ".txt"}
        ):
            for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
                if pattern.search(line) and p.name != "test_disclosure.py":
                    hits.append(f"{p.relative_to(ROOT)}:{n}")
    assert hits == []


def test_nothing_hermes_internal_is_imported_by_shipped_code():
    internals = re.compile(r"^\s*(?:from|import)\s+(?:hermes_cli\.plugins\b|tools\.|model_tools\b|run_agent\b|cli\b)", re.M)
    bad = [
        str(p.relative_to(ROOT))
        for p in ROOT.rglob("*.py")
        if "tests" not in p.parts and ".git" not in p.parts and internals.search(p.read_text())
    ]
    assert bad == []
