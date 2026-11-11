"""Plugin settings: the one place that turns ``plugins.entries.hermes-3d.settings`` into typed values.

Precedence (highest first), identical in every process that needs a setting:

1. ``config.yaml`` -> ``plugins.entries.hermes-3d.settings.<key>`` (what Settings -> Plugins in the
   Desktop and ``hermes config set`` write; inside Hermes it is read with ``ctx.get_config``).
2. The default in ``plugin.yaml`` ``config_schema`` (repeated in :data:`DEFAULTS`).

There are no environment variables for settings. The only environment variable the plugin reads is the
secret ``HERMES_3D_LINK_KEY`` (see :mod:`hermes_3d_lib.linkkey`).

The module is pure (no Hermes import): the Hermes process hands it ``ctx.get_config``; the bridge and the
dashboard router, which have no ``ctx``, hand it :func:`yaml_getter`, which reads the same file.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any
from urllib.parse import urlsplit

from . import config

PLUGIN_ID = "hermes-3d"
Getter = Callable[[str, Any], Any]
logger = logging.getLogger("hermes_3d.settings")

# Engine function name -> the setting that switches it on or off (flat keys: one shape, no nesting).
FUNCTIONS = ("salud", "cron", "sesiones", "agentes", "gasto", "habilidades", "kanban", "proyectos", "documentos")
SWITCH_KEYS = {
    "salud": "publish_health",
    "cron": "publish_cron",
    "sesiones": "publish_sessions",
    "agentes": "publish_agents",
    "gasto": "publish_spend",
    "habilidades": "publish_skills",
    "kanban": "publish_kanban",
    "proyectos": "publish_projects",
    "documentos": "publish_docs",
}
DEFAULT_SWITCHES = {name: name == "salud" for name in FUNCTIONS}  # only ``salud`` is on by default
# ``guia`` (the in-world guide) is a switch of the world page only: the engine never announces it to a source, so the
# plugin has no switch for it. If an engine does list it, the hub passes the flag through (``Hub.guide``).

DEFAULT_ENGINE_PORT = 8790
DEFAULT_LINK_PORT = 8791
DEFAULT_DOCS_PORT = 8791  # the docs viewer; the engine template lists this port in documentos.origenes
EMBED_TTL_DEFAULT_S = 3600  # the engine asks for about an hour
EMBED_TTL_MIN_S = 60
EMBED_TTL_MAX_S = 24 * 3600  # the engine refuses a credential made for longer
POLL_SECONDS_DEFAULT = 15.0
POLL_SECONDS_MIN = 3.0
SCAN_DAYS_DEFAULT = 30
LOOPBACK_NAMES = ("127.0.0.1", "localhost")
ENGINE_MAIN = Path("packages") / "server" / "src" / "main.ts"
ENGINE_WWW = Path("dist") / "plaza"
ENGINE_MARKER = ".hermes-3d-engine.json"  # written by install-engine; names the entry file inside app/

DEFAULTS: dict[str, Any] = {
    "engine_path": "",
    "world_path": "",
    "engine_port": DEFAULT_ENGINE_PORT,
    "link_port": 0,
    "world_url": "",
    "cron_poll_seconds": POLL_SECONDS_DEFAULT,
    "scan_days": SCAN_DAYS_DEFAULT,
    "read_only": False,
    "engine_link": True,
    "embed_credential_ttl_s": EMBED_TTL_DEFAULT_S,
    "allow_direct_db_read": False,
    "deadline_clock": False,
    "docs_viewer_path": "",
    "docs_viewer_port": DEFAULT_DOCS_PORT,
    **{key: DEFAULT_SWITCHES[name] for name, key in SWITCH_KEYS.items()},
}


class SettingsError(RuntimeError):
    """A setting has an invalid value; the message names the key and what is accepted."""


# ---- readers ---------------------------------------------------------------------------------------------
def yaml_getter(home: Path) -> Getter:
    """A ``(key, default)`` reader over ``<home>/config.yaml`` for processes that have no ``ctx``.

    Missing file or missing section is not an error (defaults apply); an unreadable or invalid file is
    logged once at WARNING and treated as empty, so a typo never takes the bridge down silently *and*
    never hides the cause.
    """
    settings: dict[str, Any] = {}
    path = home / "config.yaml"
    import yaml  # ships with Hermes

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entry = ((data.get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID) or {}
        raw = entry.get("settings")
        settings = raw if isinstance(raw, dict) else {}
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError, yaml.YAMLError) as exc:
        logger.warning("cannot read plugin settings from %s (%s); using defaults", path, type(exc).__name__)

    def get(key: str, default: Any = None) -> Any:
        return settings.get(key, default)

    return get


# ---- value parsers ---------------------------------------------------------------------------------------
def _port(key: str, raw: Any, *, allow_zero: bool = False) -> int:
    if isinstance(raw, bool):
        raise SettingsError(f"{key} must be a port number, got {raw!r}")
    try:
        port = int(str(raw).strip())
    except ValueError:
        raise SettingsError(f"{key} must be a number, got {raw!r}") from None
    if allow_zero and port == 0:
        return 0
    if not 1024 <= port <= 65535:
        raise SettingsError(f"{key} must be between 1024 and 65535, got {port}")
    return port


def _flag(raw: Any, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().lower() in ("true", "false", "on", "off", "1", "0", "yes", "no"):
        return raw.strip().lower() in ("true", "on", "1", "yes")
    return default


def loopback_url(key: str, raw: Any) -> str:
    """``http://127.0.0.1:<port>/...`` (or localhost) or ''. Anything else is refused, never rewritten."""
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise SettingsError(f"{key} is not a valid URL: {text!r}") from None
    if parts.scheme != "http" or parts.hostname not in LOOPBACK_NAMES or port is None or parts.username or parts.password:
        raise SettingsError(f"{key} must look like http://127.0.0.1:<port>/ (loopback only), got {text!r}")
    return text


def checked_folder(key: str, raw: Any, *, home: Path) -> Path | None:
    """A user-given folder: no ``..``, resolved, never a symlink, never ``/``, ``$HOME`` or a parent of them.

    The folder may not exist yet (a new world). Symlinks *inside* the world are the engine's business; the
    plugin only opens fixed file names in it, without following a final symlink (see ``supervisor``).
    """
    text = str(raw or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if ".." in PurePath(path).parts:
        raise SettingsError(f"{key} must not contain '..': {text!r}")
    if not path.is_absolute():
        raise SettingsError(f"{key} must be an absolute path (or start with ~), got {text!r}")
    if path.is_symlink():
        raise SettingsError(f"{key} points at a symlink ({path}); give the real folder")
    resolved = path.resolve(strict=False)
    protected = {Path(resolved.anchor), home.resolve(strict=False)}
    user_home = os.environ.get("HOME")
    if user_home:
        protected.add(Path(user_home).resolve(strict=False))
    for root in protected:
        if resolved == root or resolved in root.parents:
            raise SettingsError(f"{key} must not be {root} or one of its parents ({resolved}); pick a dedicated folder")
    return resolved


@dataclass(frozen=True)
class Hermes3DSettings:
    home: Path
    engine_path: Path | None = None
    world_path: Path = field(default=Path("."))
    engine_port: int = DEFAULT_ENGINE_PORT
    link_port: int = 0  # 0 = read it from the world's entorno/espejo.json, else DEFAULT_LINK_PORT
    world_url: str = ""
    poll_seconds: float = POLL_SECONDS_DEFAULT
    scan_days: int = SCAN_DAYS_DEFAULT
    engine_link: bool = True  # open the HMAC link to the engine (path B); false = the world lives inside Desktop only (path A)
    read_only: bool = False  # the world may not change anything in Hermes (see THREAT-MODEL, T15)
    deadline_clock: bool = (
        False  # off: the hackathon delivery clock and links are not published (a public install shows no countdown)
    )
    allow_direct_db_read: bool = False  # opt-in: spend/kanban/session counts come from Hermes' private SQLite files
    switches: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_SWITCHES))
    docs_viewer_path: Path | None = None  # docs-visor checkout; None = the sibling folder (see docsviewer.default_path)
    docs_viewer_port: int = DEFAULT_DOCS_PORT
    embed_ttl_s: int = EMBED_TTL_DEFAULT_S  # life of the credential that opens the world inside Desktop (60 s .. 24 h)

    @classmethod
    def from_getter(cls, get: Getter, home: Path) -> Hermes3DSettings:
        world = checked_folder("world_path", get("world_path", ""), home=home) or (config.data_dir_path(home) / "mundo")
        try:
            poll = float(get("cron_poll_seconds", POLL_SECONDS_DEFAULT))
        except (TypeError, ValueError):
            poll = POLL_SECONDS_DEFAULT
        try:
            days = int(get("scan_days", SCAN_DAYS_DEFAULT))
        except (TypeError, ValueError):
            days = SCAN_DAYS_DEFAULT
        return cls(
            home=home,
            engine_path=checked_folder("engine_path", get("engine_path", ""), home=home),
            world_path=world,
            engine_port=_port("engine_port", get("engine_port", DEFAULT_ENGINE_PORT)),
            link_port=_port("link_port", get("link_port", 0), allow_zero=True),
            world_url=loopback_url("world_url", get("world_url", "")),
            poll_seconds=max(POLL_SECONDS_MIN, min(poll, 600.0)),
            scan_days=max(1, min(days, 365)),
            engine_link=_flag(get("engine_link", None), True),
            read_only=read_only(get),
            allow_direct_db_read=_flag(get("allow_direct_db_read", None), False),
            deadline_clock=_flag(get("deadline_clock", None), False),
            switches=switches(get),
            docs_viewer_path=checked_folder("docs_viewer_path", get("docs_viewer_path", ""), home=home),
            docs_viewer_port=_port("docs_viewer_port", get("docs_viewer_port", DEFAULT_DOCS_PORT)),
            embed_ttl_s=embed_ttl(get("embed_credential_ttl_s", None)),
        )

    @classmethod
    def from_yaml(cls, home: Path) -> Hermes3DSettings:
        return cls.from_getter(yaml_getter(home), home)

    # ---- derived ---------------------------------------------------------------------------------------
    def engine_install(self) -> dict[str, Any] | None:
        """The marker of an engine installed by ``install-engine`` (a release unpacked into ``app/`` + ``www/``), or None."""
        if self.engine_path is None:
            return None
        try:
            data = json.loads((self.engine_path / ENGINE_MARKER).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        entry = data.get("entry") if isinstance(data, dict) else None
        return data if isinstance(entry, str) and (self.engine_path / "app" / entry).is_file() else None

    def engine_main(self) -> Path | None:
        """The engine's entry file: the installed release's, else ``main.ts`` of a checkout; None when unset or not an engine."""
        if self.engine_path is None:
            return None
        marker = self.engine_install()
        if marker is not None:
            return self.engine_path / "app" / str(marker["entry"])
        main = self.engine_path / ENGINE_MAIN
        return main if main.is_file() else None

    def www_dir(self) -> Path | None:
        """The built page (installed release ``www/`` or a checkout's ``dist/plaza``), passed as ``WWW_DIR``."""
        if self.engine_path is None:
            return None
        www = self.engine_path / ("www" if self.engine_install() is not None else ENGINE_WWW)
        return www if (www / "index.html").is_file() else None

    def page_url(self) -> str:
        return self.world_url or f"http://127.0.0.1:{self.engine_port}/"


def embed_ttl(raw: Any) -> int:
    """Seconds an embed credential lives: the setting, clamped to 60 s .. 24 h (the engine's own cap); junk = the default."""
    if isinstance(raw, bool) or raw is None:
        return EMBED_TTL_DEFAULT_S
    try:
        value = int(str(raw).strip())
    except ValueError:
        return EMBED_TTL_DEFAULT_S
    return max(EMBED_TTL_MIN_S, min(value, EMBED_TTL_MAX_S))


def switches(get: Getter) -> dict[str, bool]:
    """Per-function on/off (defaults: only ``salud``). ``get`` is ``ctx.get_config`` or any ``(key, default)``."""
    return {name: _flag(get(SWITCH_KEYS[name], None), DEFAULT_SWITCHES[name]) for name in FUNCTIONS}


def read_only(get: Getter) -> bool:
    """``read_only`` (default false): when true the world can look but never change anything in Hermes."""
    return _flag(get("read_only", None), False)


ENGINE_HINT = (
    "Run `hermes 3d setup --engine-path /path/to/engine` (the folder with packages/server/src/main.ts), or "
    "`hermes 3d install-engine` to download a pinned one. Nothing else needs an engine: `hermes 3d scan` "
    "shows what the world would mirror without one. You can also set the plugin setting `engine_path` yourself "
    "(`hermes config set plugins.entries.hermes-3d.settings.engine_path /path/to/engine`)."
)
