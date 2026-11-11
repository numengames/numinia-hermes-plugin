"""Docs that must not drift from the code."""

from __future__ import annotations

import re

from conftest import ROOT

from hermes_3d_lib import settings as S
from hermes_3d_lib.worldtext import MESSAGES


def test_world_strings_table_matches_the_module():
    text = (ROOT / "docs" / "WORLD-STRINGS.md").read_text(encoding="utf-8")
    rows = dict(re.findall(r"^\| `(\w+)` \| (.+?) \|$", text, re.M))
    for code, en in MESSAGES.items():
        assert rows.get(code) == en, code


def test_every_setting_is_documented_in_the_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for key in S.DEFAULTS:
        short = key.replace("publish_", "")
        assert key in readme or f"_{short}" in readme or short in readme, key


def test_docs_index_links_exist():
    index = (ROOT / "docs" / "INDEX.md").read_text(encoding="utf-8")
    for target in re.findall(r"\]\(([^)]+\.md)\)", index):
        assert (ROOT / "docs" / target).resolve().is_file(), target


def test_readme_names_no_removed_environment_variable():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for gone in ("HERMES_3D_ENGINE", "HERMES_3D_WORLD", "HERMES_3D_ENGINE_PORT", "HERMES_3D_LINK_PORT"):
        assert gone not in readme
    assert "HERMES_3D_LINK_KEY" in readme
