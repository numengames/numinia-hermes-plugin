"""T9: the host's own plugin validator and doctor run as part of pytest when a Hermes checkout is available.

Set ``HERMES_AGENT_DIR`` to a hermes-agent checkout that has ``venv/bin/python`` (the same variable ``make check`` uses).
Without it the tests skip, loudly, with the reason. They run in a SUBPROCESS of that venv and are read-only: a
throwaway ``HERMES_HOME`` is used so the user's real Hermes is never touched.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from conftest import ROOT

AGENT = os.environ.get("HERMES_AGENT_DIR", "")
VENV_PY = Path(AGENT) / "venv" / "bin" / "python" if AGENT else None

pytestmark = pytest.mark.skipif(
    not (VENV_PY and VENV_PY.is_file()), reason="set HERMES_AGENT_DIR to a hermes-agent checkout with venv/bin/python"
)

CALL = "import sys; sys.path.insert(0, '.'); from hermes_cli import plugins_cmd as p; p._catalog().cmd_validate(sys.argv[1])"
DOCTOR = "import sys; sys.path.insert(0, '.'); from hermes_cli import plugins_cmd as p; p.cmd_plugin_doctor(sys.argv[1], ci=True)"


def run_in_host(code: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL")}
    env["HERMES_HOME"] = str(tmp_path / "home")
    env["HOME"] = str(tmp_path)
    (tmp_path / "home").mkdir()
    assert VENV_PY is not None
    return subprocess.run(
        [str(VENV_PY), "-c", code, str(ROOT)], cwd=AGENT, env=env, capture_output=True, text=True, timeout=240, check=False
    )


def test_plugins_validate_passes(tmp_path):
    res = run_in_host(CALL, tmp_path)
    assert res.returncode == 0, (res.stdout + res.stderr)[-800:]


def test_plugins_doctor_ci_passes(tmp_path):
    res = run_in_host(DOCTOR, tmp_path)
    assert res.returncode == 0, (res.stdout + res.stderr)[-800:]
