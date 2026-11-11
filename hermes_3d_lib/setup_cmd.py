# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""``hermes 3d setup``: the first run. One path, no questions about presets.

Design rules
* Nothing is written without a confirmation, except with ``--yes`` (scripts).
* ``setup`` applies ONE default set (``DEFAULT_SET``: health, cron, agents, skills and docs, read-only, no direct database
  reads). More switches are chosen later, in the plugin settings; the catalog default stays "everything off but health".
* Settings are written through Hermes' own ``hermes config set`` (argv list, no shell), never by editing ``config.yaml`` here,
  so comments and the schema checks of Hermes stay intact.
* Idempotent: only the keys whose value differs are written; a second run says "nothing to change".
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config, docsviewer, embed, installer, linkkey, nodebin
from . import settings as S
from .errors import Hermes3DError, NodeMissing

CONFIG_TIMEOUT = 20

# What setup turns on. A first impression a person can judge in one minute: agents, cron jobs, skills and the docs library,
# all read from Hermes' own files or CLI (names, states, counts). The world cannot change anything. No sessions feed, no
# spend, no Kanban, no projects: those three need allow_direct_db_read, which stays off until you turn it on yourself.
DEFAULT_SET: dict[str, Any] = {
    "publish_health": True,
    "publish_cron": True,
    "publish_sessions": False,
    "publish_agents": True,
    "publish_spend": False,
    "publish_skills": True,
    "publish_kanban": False,
    "publish_projects": False,
    "publish_docs": True,
    "read_only": True,
    "allow_direct_db_read": False,
}

# key -> (name, what it shows, where it reads from)
SWITCH_TEXT: dict[str, tuple[str, str, str]] = {
    "publish_health": ("Health", "a card saying the bridge is alive", "nothing of yours"),
    "publish_cron": (
        "Cron jobs",
        "your scheduled jobs: name, schedule, state, next run",
        "cron/jobs.json, else `hermes cron list`",
    ),
    "publish_sessions": (
        "Sessions",
        "sessions starting and ending, and the live activity feed (tool names only)",
        "Hermes hooks",
    ),
    "publish_agents": ("Agents", "your Hermes profiles: name, model, gateway up or down, job counts", "files in the Hermes home"),
    "publish_spend": ("Spend", "tokens and estimated cost per period", "state.db, read directly (needs allow_direct_db_read)"),
    "publish_skills": ("Skills", "skill names, groups and short descriptions", "SKILL.md files"),
    "publish_kanban": ("Kanban", "cards per column and up to 3 titles", "kanban.db, read directly (needs allow_direct_db_read)"),
    "publish_projects": (
        "Projects",
        "project names, active or archived, and folder labels",
        "projects.db, read directly (needs allow_direct_db_read)",
    ),
    "publish_docs": ("Docs library", "a library of the Hermes documentation", "a list inside the plugin"),
    "read_only": ("Read-only", "the world can look but never change anything in Hermes", "setting"),
    "allow_direct_db_read": (
        "Direct database read",
        "lets Spend, Kanban and Projects read Hermes' private SQLite files, read-only: counts and titles",
        "setting",
    ),
}

FIRST_RUN_NEXT_TIME = "Want more, like Kanban or Projects? Turn on `allow_direct_db_read` and the switch you want, in Settings > Plugins > Hermes 3D."


class SetupError(RuntimeError):
    """A bad argument to ``setup`` (an engine folder that is not an engine)."""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""
    kind: str = ""  # "pending": expected on a first run, setup does it; "optional": not needed yet

    @property
    def mark(self) -> str:
        return "ok" if self.ok else {"pending": "  ", "optional": "--"}.get(self.kind, "!!")


def describe_switches() -> str:
    """Every switch in plain words: the name first, the config key in brackets, then what it shows and where it reads from."""
    lines = []
    for key, (name, shows, source) in SWITCH_TEXT.items():
        lines.append(f"  {name} ({key})\n      {shows}\n      reads: {source}")
    return "\n".join(lines)


# ---- checks ----------------------------------------------------------------------------------------------
def _node_version() -> str:
    """The version of the Node Hermes provides (what the engine will run on), or '' if there is none yet. Never downloads."""
    try:
        node = nodebin.resolve_node(ensure=False)
    except NodeMissing:
        return ""
    return "v" + node.version_text if node.version else ""


def node_ok() -> bool:
    m = re.match(r"v?(\d+)\.(\d+)", _node_version())
    return m is not None and (int(m.group(1)), int(m.group(2))) >= nodebin.NODE_RECOMMENDED[:2]


def run_checks(cfg: S.Hermes3DSettings) -> list[Check]:
    rows: list[Check] = []
    ver = _node_version()
    rows.append(
        Check(
            "node",
            node_ok(),
            ver or "not installed yet; setup asks Hermes to install it",
            "" if node_ok() else "hermes 3d setup --first-run   (Hermes' package manager installs Node 22.18 or newer)",
            kind="pending",
        )
    )
    main = cfg.engine_main()
    rows.append(
        Check(
            "engine",
            main is not None,
            str(cfg.engine_path) if main else "not installed yet; setup downloads it",
            ""
            if main
            else "hermes 3d setup --first-run   (or: hermes 3d install-engine, or setup --engine-path /path/to/engine)",
            kind="pending",
        )
    )
    if main is not None:
        rows.append(
            Check(
                "engine page",
                cfg.www_dir() is not None,
                "built (dist/plaza)" if cfg.www_dir() else "the engine has no built page yet",
                "" if cfg.www_dir() else "build the engine's page (see the engine README)",
            )
        )
    try:
        dv = docsviewer.status(cfg.home, cfg)
        built = bool(dv.get("built"))
        rows.append(
            Check(
                "docs viewer",
                built or bool(dv.get("running")),
                "running" if dv.get("running") else ("built" if built else "optional; built the first time you open the library"),
                "" if built or dv.get("running") else "hermes 3d docs   (builds it on first use)",
                kind="optional",
            )
        )
    except (OSError, ValueError, KeyError, RuntimeError, S.SettingsError) as exc:
        rows.append(Check("docs viewer", False, f"cannot tell ({type(exc).__name__})", "hermes 3d docs"))
    try:
        where = linkkey.source(cfg.home)
        rows.append(Check("link key", True, "created on first start" if where == "missing" else where))
    except (OSError, ValueError, RuntimeError) as exc:
        rows.append(Check("link key", False, f"cannot tell ({type(exc).__name__})"))
    rows.append(
        Check(
            "hermes CLI",
            shutil.which("hermes") is not None,
            "on PATH" if shutil.which("hermes") else "not on PATH; cron reads the jobs file only",
        )
    )
    return rows


# ---- writing ---------------------------------------------------------------------------------------------
def hermes_config_set(home: Path) -> Callable[[str, str], bool]:
    """A setter that runs ``hermes config set <key> <value>`` for this Hermes home. argv list, no shell, timeout."""

    def setter(key: str, value: str) -> bool:
        exe = shutil.which("hermes")
        if exe is None:
            return False

        try:
            res = subprocess.run(
                [exe, "config", "set", key, value],
                capture_output=True,
                text=True,
                timeout=CONFIG_TIMEOUT,
                check=False,
                env=config.hermes_child_env(HERMES_HOME=str(home)),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return res.returncode == 0

    return setter


def _key(name: str) -> str:
    return f"plugins.entries.{S.PLUGIN_ID}.settings.{name}"


def _same(current: Any, wanted: Any) -> bool:
    if isinstance(wanted, bool):
        return S._flag(current, False) is wanted if current is not None else wanted is False
    return str(current) == str(wanted)


def plan(wanted: dict[str, Any], get: S.Getter) -> dict[str, Any]:
    """Only the settings whose current value differs from what is wanted. Unset means the catalog default."""
    out: dict[str, Any] = {}
    for name, value in wanted.items():
        current = get(name, S.DEFAULTS.get(name))
        if not _same(current, value):
            out[name] = value
    return out


def _engine_folder(raw: str, home: Path) -> str:
    folder = S.checked_folder("engine_path", raw, home=home)
    if folder is None or not (folder / S.ENGINE_MAIN).is_file():
        raise SetupError(f"{raw!r} is not a Numinia Engine folder (no {S.ENGINE_MAIN} inside)")
    return str(folder)


def _say_checks(rows: list[Check]) -> None:
    print("What I found")
    for r in rows:
        print(f"  [{r.mark}] {r.name:<12} {r.detail}")
        if r.fix and not r.ok and r.kind == "":
            print(f"       -> {r.fix}")


def _ask(prompt: str) -> str:
    return input(prompt).strip().lower()


def _shown(value: Any) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def cmd_setup(
    args: Any,
    *,
    home: Path,
    setter: Callable[[str, str], bool] | None = None,
    get: S.Getter | None = None,
    starter: Callable[[S.Hermes3DSettings], int] | None = None,
    ask: Callable[[str], str] | None = None,
) -> int:
    """``hermes 3d setup [--engine-path DIR] [--yes] [--first-run]``. One path: the default set, no preset to choose."""
    yes = bool(getattr(args, "yes", False))
    engine_arg = getattr(args, "engine_path", None)
    get = get or S.yaml_getter(home)
    setter = setter or hermes_config_set(home)
    cfg = S.Hermes3DSettings.from_getter(get, home)

    print("Hermes 3D setup. Nothing changes until you say yes.")
    print("The world shows names, counts and states, never your messages.\n")
    _say_checks(run_checks(cfg))
    print("\nSwitches")
    print(describe_switches())

    wanted: dict[str, Any] = dict(DEFAULT_SET)
    if engine_arg:
        wanted["engine_path"] = _engine_folder(engine_arg, home)
    try:
        changes = plan(wanted, get)
        if not changes:
            print("\nNothing to change. Your settings already match.")
        else:
            print("\nSetup will set:")
            for name, value in changes.items():
                label = SWITCH_TEXT[name][0] if name in SWITCH_TEXT else name
                print(f"  {label} ({name}) = {_shown(value)}")
            print("Direct database read stays off. Spend, Kanban and Projects stay closed until you turn it on yourself.")
            if not yes and _ask("Write these settings? [y/N] ") not in ("y", "yes"):
                print("Not written.")
                changes = {}
        failed = []
        for name, value in changes.items():
            if not setter(_key(name), _shown(value)):
                failed.append(name)
        if changes and not failed:
            print(f"Wrote {len(changes)} setting{'s' if len(changes) != 1 else ''}. {FIRST_RUN_NEXT_TIME}")
        if failed:
            print(
                f"Could not write: {', '.join(failed)}. Is `hermes` on your PATH? You can also set them in Settings > Plugins.",
                file=sys.stderr,
            )
            return 1
    except EOFError:
        print("No terminal to ask on, so nothing was written. Run it again with --yes.", file=sys.stderr)
        return 1

    if getattr(args, "first_run", False):
        return first_run(home=home, yes=yes, setter=setter, starter=starter, ask=ask or _ask, out=print)
    print("\nNext")
    print("  hermes 3d setup --first-run   one command: Node from Hermes, the engine, then start the world")
    print("  hermes 3d demo                see your own Hermes as a world, read-only (needs the engine)")
    print("  hermes 3d scan                what the world would show, without the engine")
    print("  hermes 3d up                  start the engine and the bridge with your settings")
    return 0


def first_run(
    *,
    home: Path,
    yes: bool,
    setter: Callable[[str, str], bool],
    starter: Callable[[S.Hermes3DSettings], int] | None,
    ask: Callable[[str], str],
    out: Callable[..., None] = print,
    lock_path: Path | None = None,
    url: str | None = None,
    probe: Callable[[str], dict[str, Any]] | None = None,
) -> int:
    """The whole path, in order, stopping at the first step that cannot pass.

    1 Node from Hermes' package manager  2 the pinned engine  3 ``engine_link: false`` (the world lives inside Desktop)
    4 start engine + bridge, then say where to click. Each download or write is asked first; ``yes`` answers for scripts.
    """

    def confirm(prompt: str) -> bool:
        return yes or ask(prompt) in ("y", "yes")

    try:
        out("\nStep 1 of 4: Node")
        try:
            node = nodebin.resolve_node(ensure=False)
        except NodeMissing:
            if not confirm("Hermes has no Node yet. Let Hermes' package manager download the one it pins? [y/N] "):
                out("Stopped. Without Node nothing else was done.")
                return 1
            node = nodebin.resolve_node(ensure=True)
        out(
            f"  Node {node.version_text} from {'Hermes' if node.source == 'pm' else 'your PATH (this Hermes has no package manager)'}."
        )
        for warning in node.warnings:
            out(f"  Note: {warning}")

        out("\nStep 2 of 4: Engine")
        cfg = S.Hermes3DSettings.from_yaml(home)
        if cfg.engine_main() is None:
            res = installer.install_engine(home, lock_path=lock_path, url=url, yes=yes, confirm=confirm, out=out)
            out(f"  Engine ready ({res['seconds']} s, {res['size_mb']} MB).")
        else:
            out(f"  Engine already installed at {cfg.engine_path}.")

        out("\nStep 3 of 4: Settings")
        cfg = S.Hermes3DSettings.from_yaml(home)
        if cfg.engine_link and confirm("Open the world inside Hermes Desktop? This sets engine_link to false. [y/N] "):
            if not setter(_key("engine_link"), "false"):
                out("  Could not write engine_link. Is `hermes` on your PATH? Set it in Settings > Plugins, then run this again.")
                return 1
            cfg = S.Hermes3DSettings.from_yaml(home)
        out(f"  engine_link = {str(cfg.engine_link).lower()}, engine port {cfg.engine_port}.")
        if cfg.engine_link:
            out("  engine_link is still true, so the page cannot open inside Hermes Desktop (the engine takes one source).")

        out("\nStep 4 of 4: Start")
        rc = starter(cfg) if starter is not None else 0
        if rc != 0:
            return rc
        seen = (probe or embed.probe_engine)(cfg.page_url())
        if seen.get("up") and seen.get("embed"):
            out("\nReady. Open the Hermes 3D page in Hermes Desktop.")
        elif seen.get("up"):
            out("\nThe engine is running, but this world does not accept an embedded page yet.")
            out(f"  {embed.HINT_EMBED}")
        else:
            out("\nThe engine did not answer after starting. Run `hermes 3d status` to see why.")
            return 1
        return 0
    except Hermes3DError as exc:
        out(f"\nStopped. {exc}")
        return 1
    except EOFError:
        out("\nNo terminal to ask on. Run it again with --yes.")
        return 1
