# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""Parse the text printed by ``hermes cron list --all`` (Hermes 0.22.x).

Hermes has no ``--json`` for ``cron list`` (B0-SUPERFICIES, request 2), so the public route is
to scrape the printed listing. The printer is ``hermes_cli/cron.py::cron_list``::

    <banner box with "Scheduled Jobs (profile: NAME)">

      <id> [active|paused|completed|disabled]
        Name:      ...
        Schedule:  ...
        Repeat:    ∞ | done/times
        Next run:  <iso>|None        (or "Overdue:  <iso>  (… ago — …)")
        Deliver:   ...
        Skills: / Script: / Monitor: / Mode: / Timezone: / Grace: / Workdir: / Python:
        Last run:  <iso>  ok | <status>: <error>
        Dispatch: / Execution:
        <free-text warning lines, 4-space indented, may start with "⚠">

      ⚠  Scheduler is not ready ...            (2-space indented footer)

Design rules:
* A job starts only at a line ``  <id> [<badge>]`` indented ≤ 8 spaces whose badge is one of
  the known words; a field/warning line is never a job even if it contains brackets
  (a job *name* such as ``nombre [corchetes]: x`` sits after ``Name:``).
* Field lines are ``    Label:   value`` with a label from a closed list; anything else that
  is indented under a job is kept as a bounded ``notes`` entry, never executed or trusted.
* Never raises. ANSI escapes are stripped (the listing is coloured on a TTY).
* Everything that came from a job (name, schedule, errors) is UNTRUSTED text: it is cut to a
  short single line without control characters here, before it leaves the process.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
BADGES = ("active", "paused", "completed", "disabled")
_HEADER = re.compile(r"^ {1,8}(?P<id>[A-Za-z0-9][A-Za-z0-9_\-]{3,63}) \[(?P<badge>[a-z]{3,20})\]\s*$")
_FIELD = re.compile(r"^ {3,8}(?P<key>[A-Za-z][A-Za-z ]{1,20}):\s*(?P<val>.*?)\s*$")

# Label as printed -> internal key. The set mirrors ``_job_rows`` in hermes_cli/cron.py.
_LABELS = {
    "name": "name",
    "schedule": "schedule",
    "repeat": "repeat",
    "next run": "next_run",
    "overdue": "next_run",
    "deliver": "deliver",
    "skills": "skills",
    "script": "script",
    "monitor": "monitor",
    "changed": "changed",
    "mode": "mode",
    "timezone": "timezone",
    "grace": "grace",
    "workdir": "workdir",
    "python": "python",
    "last run": "last_run",
    "dispatch": "dispatch",
    "execution": "execution",
}
MAX_TEXT = 120
MAX_NOTES = 4


def clean_text(value: Any, limit: int = MAX_TEXT) -> str:
    """Untrusted text -> short single-line string without control characters or ANSI."""
    text = _ANSI.sub("", str(value if value is not None else ""))
    text = _CTRL.sub("", text.replace("\r", " ").replace("\n", " ").replace("\t", " "))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def badge_to_state(badge: str) -> str:
    """Printed badge -> the small enum the world draws."""
    return {"active": "programado", "paused": "pausado", "completed": "terminado", "disabled": "apagado"}.get(
        badge, "desconocido"
    )


def _last_run_status(value: str) -> str:
    """``<iso>  ok`` / ``<iso>  error: boom`` / ``<iso>  ran, but…`` -> ``ok|fallo|aviso|""``."""
    parts = value.split(None, 1)
    rest = parts[1].strip().lower() if len(parts) > 1 else ""
    if not rest:
        return ""
    if rest == "ok" or rest.startswith("ok "):
        return "ok"
    if rest.startswith(("finished", "ran, but")):
        return "aviso"
    return "fallo"


def parse_cron_list(text: str) -> dict[str, Any]:
    """Parse the listing into ``{"jobs": [...], "profile": str|None, "warnings": [...], "empty": bool}``.

    Each job dict: ``id``, ``badge``, ``estado`` (programado|pausado|terminado|apagado), ``ultimo``
    (ok|fallo|aviso|""), the cleaned label fields (``""`` when absent) and ``notes`` (≤4 short
    free-text lines). ``empty`` is True for the "No scheduled jobs" message.
    """
    jobs: list[dict[str, Any]] = []
    warnings: list[str] = []
    profile: str | None = None
    current: dict[str, Any] | None = None
    seen: set[str] = set()
    empty = False

    for raw in _ANSI.sub("", text or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("No scheduled jobs"):
            empty = True
            continue
        m_prof = re.search(r"Scheduled Jobs\s*\(profile:\s*([^)]+)\)", stripped)
        if m_prof:
            profile = clean_text(m_prof.group(1), 60)
            continue
        if stripped[0] in "┌└├│─╭╰╮╯┐┘":
            continue
        m_head = _HEADER.match(line)
        if m_head and m_head.group("badge") in BADGES:
            job_id = m_head.group("id")
            if job_id in seen:  # a repeated id would double-count; keep the first
                current = None
                continue
            seen.add(job_id)
            current = {
                "id": job_id,
                "badge": m_head.group("badge"),
                "estado": badge_to_state(m_head.group("badge")),
                "ultimo": "",
                "name": "",
                "schedule": "",
                "repeat": "",
                "next_run": "",
                "deliver": "",
                "skills": "",
                "script": "",
                "monitor": "",
                "changed": "",
                "mode": "",
                "timezone": "",
                "grace": "",
                "workdir": "",
                "python": "",
                "last_run": "",
                "dispatch": "",
                "execution": "",
                "notes": [],
            }
            jobs.append(current)
            continue
        # 2-space indented footer lines after the last job end the job block.
        indent = len(line) - len(line.lstrip(" "))
        if indent <= 2:
            current = None
            if stripped.startswith("⚠"):
                warnings.append(clean_text(stripped.lstrip("⚠ ").strip(), 200))
            continue
        if current is None:
            continue
        m_field = _FIELD.match(line)
        label = m_field.group("key").strip().lower() if m_field else ""
        if m_field and label in _LABELS:
            key = _LABELS[label]
            value = clean_text(m_field.group("val"))
            current[key] = value
            if key == "last_run":
                current["ultimo"] = _last_run_status(value)
            if label == "overdue":
                current["notes"].append("overdue") if len(current["notes"]) < MAX_NOTES else None
        elif len(current["notes"]) < MAX_NOTES:
            current["notes"].append(clean_text(stripped.lstrip("⚠ ").strip(), 160))
    return {"jobs": jobs, "profile": profile, "warnings": warnings, "empty": empty}


# ----------------------------------------------------------------------------------------- jobs.json (preferred)
MAX_JOBS_FILE = 2 * 1024 * 1024
MAX_JOBS = 500
_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{3,63}$")


def _file_badge(job: dict[str, Any]) -> str:
    """The word ``hermes cron list`` prints, from the stored record (same rules as Hermes' ``effective_job_state``)."""
    enabled = job.get("enabled", True)
    stored = str(job.get("state") or "").strip()
    if stored == "completed":
        return "completed"
    if not enabled:
        return "paused" if (stored == "paused" or job.get("paused_at")) else "disabled"
    return "active"


def read_jobs_file(home: Path) -> dict[str, Any] | None:
    """Jobs from ``<home>/cron/jobs.json`` in the SAME shape as :func:`parse_cron_list`, or ``None`` to use the CLI.

    Preferred over the printed table: no subprocess and no scraping. The file is Hermes' own store, so a missing,
    oversize, unreadable or unexpected file simply means "ask the CLI instead". Only id, name, schedule, state and
    run times are copied; the job prompt, script and skills stay in the file."""
    path = Path(home) / "cron" / "jobs.json"
    try:
        if not path.is_file() or path.stat().st_size > MAX_JOBS_FILE:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    raw = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return None
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw[:MAX_JOBS]:
        if not isinstance(item, dict):
            continue
        job_id = str(item.get("id") or "")
        if not _JOB_ID.match(job_id) or job_id in seen:
            continue
        seen.add(job_id)
        badge = _file_badge(item)
        status = str(item.get("last_status") or "").lower()
        last_at = clean_text(item.get("last_run_at") or "", 40)
        jobs.append(
            {
                "id": job_id,
                "badge": badge,
                "estado": badge_to_state(badge),
                "ultimo": "ok" if status == "ok" else ("fallo" if status == "error" else ""),
                "name": clean_text(item.get("name") or ""),
                "schedule": clean_text(item.get("schedule_display") or ""),
                "repeat": "",
                "next_run": clean_text(item.get("next_run_at") or "", 40) if badge == "active" else "",
                "deliver": "",
                "skills": "",
                "script": "",
                "monitor": "",
                "changed": "",
                "mode": "",
                "timezone": "",
                "grace": "",
                "workdir": "",
                "python": "",
                "last_run": f"{last_at}  {status}".strip() if last_at else "",
                "dispatch": "",
                "execution": "",
                "notes": [],
            }
        )
    return {"jobs": jobs, "profile": None, "warnings": [], "empty": not jobs}
