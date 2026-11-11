"""Typed, user-facing errors with a stable ``code`` and a ``retryable`` flag.

The CLI prints ``hermes 3d: <CODE>: <message>``; a machine caller can branch on ``code``. Nothing here carries a secret.
"""

from __future__ import annotations


class Hermes3DError(RuntimeError):
    """An expected failure the user can act on. ``retryable`` is False when trying again cannot help."""

    code = "PLUGIN_ERROR"
    retryable = False

    def __init__(self, message: str, *, code: str | None = None, retryable: bool | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable

    def __str__(self) -> str:
        return f"{self.code}: {super().__str__()}"


class NodeMissing(Hermes3DError):
    """No usable Node: Hermes' package manager has none (and could not install it) and there is no fallback."""

    code = "NODE_MISSING"
    retryable = False


class PortBusy(Hermes3DError):
    code = "PORT_BUSY"
    retryable = False


class EngineStartFailed(Hermes3DError):
    code = "ENGINE_START_FAILED"
    retryable = True
