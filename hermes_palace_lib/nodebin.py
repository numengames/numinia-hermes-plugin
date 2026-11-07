"""Node for the engine and the docs viewer: the one Hermes' package manager (``pm``) provides.

Why: the plugin must not ask a person to install Node. Hermes ships ``pm`` which pins, downloads and checksums a Node per
platform (``pm/lock.json``). We use it exactly as Hermes' own Photon platform does:

    pm.ensure("node")  ->  pm.installed_package("node").binary      environment from hermes_constants.with_hermes_node_path()

A Node found on ``PATH`` is used ONLY when ``pm`` itself cannot be imported (an older or stripped Hermes). If ``pm`` is there but
cannot give a Node (download failed, no binary recorded), that is :class:`~.errors.NodeMissing` -- we never silently mix toolchains.

Nothing is downloaded by importing this module; ``ensure=False`` only looks. A Node older than :data:`NODE_RECOMMENDED` produces a
warning, not an error (the engine release needs 22.18 to run without flags).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import NodeMissing

NODE_RECOMMENDED = (22, 18, 0)
FIX = "Run `hermes pm doctor`, then `hermes palace setup` again."
_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
_ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "XDG_RUNTIME_DIR", "NODE_OPTIONS")


@dataclass(frozen=True)
class NodeRuntime:
    binary: str
    version: tuple[int, int, int] | None
    source: str  # "pm" | "path"
    path_env: str  # the PATH to give a child (pm's own, so npm/node agree); "" = inherit
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def version_text(self) -> str:
        return ".".join(map(str, self.version)) if self.version else "unknown"


def _load_pm() -> Any:
    try:
        import pm
    except ImportError:
        return None
    return pm


def read_version(binary: str) -> tuple[int, int, int] | None:
    try:
        res = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    m = _VERSION.fullmatch((res.stdout or "").strip()) if res.returncode == 0 else None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _hermes_path_env() -> str:
    try:
        from hermes_constants import with_hermes_node_path
    except ImportError:
        return ""
    try:
        return str(with_hermes_node_path().get("PATH", ""))
    except Exception:  # noqa: BLE001 -- Hermes' helper may raise anything pm-specific; PATH then stays the inherited one
        return ""


def resolve_node(
    *,
    ensure: bool = True,
    pm_loader: Callable[[], Any] | None = None,
    version_of: Callable[[str], tuple[int, int, int] | None] = read_version,
    which: Callable[[str], str | None] | None = None,
    path_env: Callable[[], str] = _hermes_path_env,
) -> NodeRuntime:
    """The Node to run. Raises :class:`NodeMissing` (code ``NODE_MISSING``, not retryable) with what to do about it."""
    pm = (pm_loader or _load_pm)()  # looked up at call time so a test can replace _load_pm
    if pm is None:
        found = (which or shutil.which)("node")
        if not found:
            raise NodeMissing(
                "this Hermes has no package manager (`pm`) and there is no `node` on PATH. "
                "Update Hermes (`hermes update`), or install Node 22.18 or newer yourself."
            )
        return _runtime(found, "path", "", version_of)
    try:
        if ensure:
            pm.ensure("node")
        installed = pm.installed_package("node")
    except Exception as exc:  # noqa: BLE001 -- pm.InstallError and whatever its worker raises: all mean "no Node from pm"
        why = getattr(exc, "cause", None) or str(exc) or type(exc).__name__
        raise NodeMissing(f"Hermes could not provide Node ({str(why)[:200]}). {FIX}") from None
    binary = getattr(installed, "binary", None) if installed is not None else None
    if binary is None or not Path(str(binary)).is_file():
        raise NodeMissing(f"Hermes' package manager has no Node installed. {FIX}")
    return _runtime(str(binary), "pm", path_env(), version_of)


def _runtime(binary: str, source: str, path_env: str, version_of: Callable[[str], tuple[int, int, int] | None]) -> NodeRuntime:
    version = version_of(binary)
    warnings: list[str] = []
    if version is None:
        warnings.append(f"could not read the version of {binary}; continuing")
    elif version < NODE_RECOMMENDED:
        warnings.append(
            f"Node {'.'.join(map(str, version))} is older than {'.'.join(map(str, NODE_RECOMMENDED))}; "
            "the engine may need extra flags or fail to start"
        )
    return NodeRuntime(binary, version, source, path_env, tuple(warnings))


def child_env(node: NodeRuntime, base: dict[str, str] | None = None) -> dict[str, str]:
    """A short allow-listed environment for a child that runs Node (PATH from pm when it gave us the Node)."""
    src = os.environ if base is None else base
    env = {k: v for k, v in src.items() if k in _ENV_KEEP}
    if node.path_env:
        env["PATH"] = node.path_env
    return env
