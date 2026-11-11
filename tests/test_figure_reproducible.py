# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT
"""The committed figure is exactly what its generator makes: a binary asset a reviewer can rebuild from source."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIGURE = ROOT / "assets" / "muneco-palo.glb"
RECORDED_SHA256 = "b6fbdaea4f949aaf773b2cbc1d70d3e0f9cc507d1422db00c817941c29b47224"  # assets/muneco-palo.LICENCIA.md


def test_committed_figure_matches_its_record():
    assert hashlib.sha256(FIGURE.read_bytes()).hexdigest() == RECORDED_SHA256
    assert RECORDED_SHA256 in (ROOT / "assets" / "muneco-palo.LICENCIA.md").read_text(encoding="utf-8")


def test_generator_rebuilds_the_committed_figure_byte_for_byte(tmp_path):
    out = tmp_path / "figure.glb"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "hacer_muneco.py"), str(out)], check=True, capture_output=True, timeout=60
    )
    assert out.read_bytes() == FIGURE.read_bytes()
