"""Every sentence that is shown INSIDE the Numinia world (English).

The engine draws these strings to the people in the world. The JSON keys, object types and verbs stay the
engine's own (Spanish) schema; only the words people read are English. ``docs/WORLD-STRINGS.md`` lists them for
translators and reviewers; a test keeps that table and this module identical.
"""

from __future__ import annotations

from typing import Any

# code -> English text (tool results, logs and the world)
MESSAGES: dict[str, str] = {
    "cron_off": "the cron function is switched off in Hermes",
    "verb_not_allowed": "action not allowed",
    "bad_job_id": "invalid job identifier",
    "job_gone": "that job no longer exists in Hermes",
    "expired_in_world": "the request had already expired in the world",
    "replayed": "that request id was already used",
    "bad_request": "the request has no id",
    "too_many": "too many pending requests",
    "no_approver": "nobody approved the action in Hermes",
    "expired": "the request expired",
    "unknown_verb": "unknown verb",
    "no_actions": "Hermes does not accept actions from the world",
    "timeout": "Hermes did not answer in time",
    "error": "error",
    "invalid_request": "invalid request",
    "no_pending": "no pending request from the world for that job",
    "paused": "job paused",
    "resumed": "job resumed",
    "failed": "Hermes could not do it",
    "read_only": "Hermes runs in read-only mode: the world cannot change anything",
    "read_only_card_title": "Hermes · read-only",
    "read_only_card_sub": "Read-only: the world cannot change anything in Hermes",
    "version_card_title": "Hermes 3D v{version}",
    "version_card_sub": "Engine {engine} · Hermes {hermes}",
    "board_title": "Work",
    "board_sub": "{todo} to do · {doing} in progress · {done} done",
    "docs_sub": "Hermes documentation",
    "skills_other": "other",
    "hermes_missing": "the `hermes` command was not found",
}


def say(code: str, ok: bool = False, detail: str = "") -> dict[str, Any]:
    """``{"ok", "code", "text", "texto"}``: both carry the same English sentence (``texto`` is the engine's wire key for
    what the world shows); ``detail`` is appended."""
    suffix = f": {detail}" if detail else ""
    text = MESSAGES[code] + suffix
    return {"ok": ok, "code": code, "text": text, "texto": text}
