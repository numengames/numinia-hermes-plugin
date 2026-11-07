"""Logging that cannot leak a secret and writes an owner-only file.

* The log file is created 0600 whatever the umask (``config.open_log`` semantics).
* Every record passes a filter that replaces known secret values (the bridge token, the link key) and the
  shapes secrets travel in (``Authorization: Bearer ...``, ``MOTOR_ENLACE_CLAVE=...``, ``token=...``) by
  ``[redacted]`` -- in the message, its arguments and the formatted traceback.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

REDACTED = "[redacted]"
_SHAPES = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b((?:token|key|secret|firma|password|clave|MOTOR_ENLACE_CLAVE|HERMES_PALACE_LINK_KEY)\s*[=:]\s*)\S{6,}"),
)


def redact(text: str, secrets_: frozenset[str] | set[str] = frozenset()) -> str:
    for secret in sorted(secrets_, key=len, reverse=True):
        if secret and len(secret) >= 6:
            text = text.replace(secret, REDACTED)
    for shape in _SHAPES:
        text = shape.sub(lambda m: m.group(1) + REDACTED, text)
    return text


# Free text that a person typed (a card title) and that is about to leave for the world: anything that LOOKS like a secret
# goes, even when it is not one. Over-redacting a title is cheap; leaking a key is not.
_TITLE_SHAPES = (
    re.compile(r"\b(?:sk|pk|rk|ghp|gho|ghs|github_pat|xox[abprs]|glpat|AKIA|AIza|ya29|hf|npm|pypi)[-_A-Za-z0-9]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]*)?"),  # JWT
    re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/@]+:[^\s/@]+@\S+"),  # url with user:password@
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),  # e-mail address
    re.compile(r"\b[0-9a-fA-F]{24,}\b"),  # long hex (hashes, keys)
    re.compile(r"(?<![\w/.-])[A-Za-z0-9_+/=-]{32,}(?![\w/.-])"),  # long opaque token
)


def redact_title(text: str, limit: int = 32) -> str:
    """A free-text title for the world: single line, secret shapes redacted, ``"`` and ``\\`` turned into ``'``, cut to ``limit`` BYTES."""
    out = "".join(ch if ch.isprintable() else " " for ch in str(text or ""))
    out = redact(out)
    for shape in _TITLE_SHAPES:
        out = shape.sub(REDACTED, out)
    out = re.sub(r"\s+", " ", out.replace('"', "'").replace("\\", "'")).strip()
    raw = out.encode("utf-8")
    return out if len(raw) <= limit else raw[:limit].decode("utf-8", errors="ignore").rstrip()


def fit_title(text: str, limit: int = 32) -> str:
    """Like ``redact_title`` but a title that does not fit is shortened at a word boundary and ends in ``…`` (3 bytes), so
    the world shows «Actions cache: save only…» instead of a title cut in the middle of a word. Result <= ``limit`` BYTES."""
    full = redact_title(text, 10_000)
    if len(full.encode("utf-8")) <= limit:
        return full
    room = limit - len("…".encode())
    head = full.encode("utf-8")[:room].decode("utf-8", errors="ignore")
    # the cut fell on a word edge when the next character is a space: keep it whole; else drop the broken last word
    if full[len(head) : len(head) + 1] != " " and " " in head and len(head.rsplit(" ", 1)[0]) >= room // 2:
        head = head.rsplit(" ", 1)[0]
    return head.rstrip(" -:,;·.") + "…"


def describe(exc: BaseException, limit: int = 120) -> str:
    """``ClassName: message`` for ``last_error``: printable, secret-shapes redacted, bounded."""
    text = "".join(ch if ch.isprintable() else " " for ch in str(exc)).strip()
    text = redact(text)[:limit]
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


class RedactingFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self.secrets: set[str] = set()

    def add_secret(self, value: str | None) -> None:
        if value:
            self.secrets.add(value)

    def filter(self, record: logging.LogRecord) -> bool:
        message = redact(record.getMessage(), self.secrets)
        record.msg, record.args = message, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text, self.secrets)
        return True


def install(path: Path, level: int = logging.INFO) -> RedactingFilter:
    """Route the root logger to ``path`` (0600) through a redacting filter; returns the filter."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    handler = logging.StreamHandler(os.fdopen(fd, "a", encoding="utf-8", buffering=1))
    flt = RedactingFilter()
    handler.addFilter(flt)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)
    return flt
