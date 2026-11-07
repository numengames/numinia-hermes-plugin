"""The Node minimum is one number (22.18) everywhere the user can read it."""

from __future__ import annotations

import json
import re
from pathlib import Path

from hermes_palace_lib import nodebin

ROOT = Path(__file__).resolve().parent.parent
NODE_MENTION = re.compile(r"\bNode(?:\.js)?\s*(?:>=|≥|\()?\s*(\d{2})(?:\.(\d+))?", re.IGNORECASE)


def test_warning_threshold_is_22_18():
    assert nodebin.NODE_RECOMMENDED == (22, 18, 0)


def test_engine_lock_node_is_22_18():
    assert json.loads((ROOT / "engine.lock.json").read_text())["node"] == ">=22.18"


def test_readme_says_22_18_or_later():
    text = (ROOT / "README.md").read_text()
    assert "22.18 or later" in text


def test_no_shipped_doc_states_another_node_minimum():
    files = [ROOT / "README.md", ROOT / "plugin.yaml", ROOT / "CONTRIBUTING.md", ROOT / "SECURITY.md"]
    files += sorted((ROOT / "docs").rglob("*.md")) + sorted((ROOT / "docs").rglob("*.yaml"))
    bad = []
    for path in files:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            for m in NODE_MENTION.finditer(line):
                if (int(m.group(1)), int(m.group(2) or 0)) not in {(22, 18)}:
                    bad.append(f"{path.name}:{n}: {m.group(0)!r}")
    assert not bad, bad
