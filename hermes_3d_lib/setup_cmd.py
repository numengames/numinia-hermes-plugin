# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""``hermes 3d setup``: a guided first run. Checks what is missing, explains every switch, writes settings after you say yes.

Design rules
* Nothing is written without a confirmation, except with ``--yes`` (scripts), and ``--yes`` never *chooses* for you: with no
  ``--profile`` and no ``--engine-path`` it changes nothing.
* Settings are written through Hermes' own ``hermes config set`` (argv list, no shell), never by editing ``config.yaml`` here,
  so comments and the schema checks of Hermes stay intact.
* Idempotent: only the keys whose value differs are written; a second run says "nothing to change".
* Profiles are metadata only and never open a Hermes database (``allow_direct_db_read`` stays off in all of them).
  The catalog default stays "everything off but health"; ``showcase`` is the opt-in first impression.
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

# What each profile sets (only the keys that differ from "everything off but health").
PROFILES: dict[str, dict[str, Any]] = {
    "minimal": {
        "publish_health": True,
        "publish_cron": False,
        "publish_sessions": False,
        "publish_agents": False,
        "publish_spend": False,
        "publish_skills": False,
        "publish_kanban": False,
        "publish_projects": False,
        "publish_docs": False,
        "read_only": False,
        "allow_direct_db_read": False,
    },
    # A first impression a person can judge in one minute: profiles, cron jobs, skills and the docs library, all read from
    # Hermes' own files or CLI (names, states, counts), the world cannot change anything. No sessions feed, no spend, no kanban.
    "showcase": {
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
    },
}
PROFILE_TEXT = {
    "minimal": "only the health card (what the plugin does out of the box)",
    "showcase": "health + cron + agents + skills + docs, read-only; no sessions, spend or kanban",
}

# key -> (what it shows, where it reads from)
SWITCH_TEXT: dict[str, tuple[str, str]] = {
    "publish_health": ("a card saying the bridge is alive", "nothing of yours"),
    "publish_cron": ("your scheduled jobs: name, schedule, state, next run", "cron/jobs.json, else `hermes cron list`"),
    "publish_sessions": ("sessions starting and ending, and the live activity feed (tool names only)", "Hermes hooks"),
    "publish_agents": ("your profiles: name, model, gateway up or down, job counts", "files in the Hermes home"),
    "publish_spend": ("tokens and estimated cost per period", "DIRECT read of state.db (needs allow_direct_db_read)"),
    "publish_skills": ("skill names, groups and short descriptions", "SKILL.md files"),
    "publish_kanban": ("cards per column and up to 3 titles", "DIRECT read of kanban.db (needs allow_direct_db_read)"),
    "publish_projects": (
        "project names, active/archived and folder labels",
        "DIRECT read of projects.db (needs allow_direct_db_read)",
    ),
    "publish_docs": ("a library of the Hermes documentation", "a list inside the plugin"),
    "read_only": ("the world can look but never change anything in Hermes", "setting"),
    "allow_direct_db_read": ("lets spend and kanban read Hermes' private SQLite files, read-only, counts and titles", "setting"),
}


class SetupError(RuntimeError):
    """A bad argument to ``setup`` (unknown profile, engine folder that is not an engine)."""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""


def describe_switches() -> str:
    """Every switch in plain words, with where it reads from."""
    lines = []
    for key, (shows, source) in SWITCH_TEXT.items():
        lines.append(f"  {key:<22} {shows}\n  {'':<22} reads: {source}")
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
            ver or "Hermes has no Node installed yet",
            "" if node_ok() else "hermes 3d setup --first-run   (Hermes' package manager installs Node 22.18 or newer)",
        )
    )
    main = cfg.engine_main()
    rows.append(
        Check(
            "engine",
            main is not None,
            str(cfg.engine_path) if main else "engine_path is not set or is not an engine checkout",
            ""
            if main
            else "hermes 3d setup --first-run   (or: hermes 3d install-engine, or setup --engine-path /path/to/engine)",
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
                "running" if dv.get("running") else ("built" if built else "not built yet"),
                "" if built or dv.get("running") else "hermes 3d docs   (builds it on first use)",
            )
        )
    except (OSError, ValueError, KeyError, RuntimeError, S.SettingsError) as exc:
        rows.append(Check("docs viewer", False, f"cannot tell ({type(exc).__name__})", "hermes 3d docs"))
    try:
        rows.append(Check("link key", True, linkkey.source(cfg.home)))
    except (OSError, ValueError, RuntimeError) as exc:
        rows.append(Check("link key", False, f"cannot tell ({type(exc).__name__})"))
    rows.append(
        Check(
            "hermes CLI",
            shutil.which("hermes") is not None,
            "on PATH" if shutil.which("hermes") else "not on PATH (cron falls back to the jobs file only)",
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
        raise SetupError(f"{raw!r} is not a Numinia Engine checkout (no {S.ENGINE_MAIN} inside)")
    return str(folder)


def _say_checks(rows: list[Check]) -> None:
    print("what I found:")
    for r in rows:
        print(f"  [{'ok' if r.ok else '!!'}] {r.name:<12} {r.detail}")
        if r.fix and not r.ok:
            print(f"       -> {r.fix}")


def _ask(prompt: str) -> str:
    return input(prompt).strip().lower()


def cmd_setup(
    args: Any,
    *,
    home: Path,
    setter: Callable[[str, str], bool] | None = None,
    get: S.Getter | None = None,
    starter: Callable[[S.Hermes3DSettings], int] | None = None,
    ask: Callable[[str], str] | None = None,
) -> int:
    """``hermes 3d setup [--profile minimal|showcase] [--engine-path DIR] [--yes]``."""
    yes = bool(getattr(args, "yes", False))
    profile = getattr(args, "profile", None)
    engine_arg = getattr(args, "engine_path", None)
    if profile is not None and profile not in PROFILES:
        raise SetupError(f"unknown profile {profile!r}; choose one of: {', '.join(PROFILES)}")
    get = get or S.yaml_getter(home)
    setter = setter or hermes_config_set(home)
    cfg = S.Hermes3DSettings.from_getter(get, home)

    print("Hermes 3D setup. Nothing is changed until you confirm; everything it publishes is metadata only.\n")
    _say_checks(run_checks(cfg))
    print("\nthe switches (all off but health unless you choose a profile):")
    print(describe_switches())

    wanted: dict[str, Any] = {}
    if engine_arg:
        wanted["engine_path"] = _engine_folder(engine_arg, home)
    try:
        if profile is None and not yes:
            print("\nprofiles:")
            for name, text in PROFILE_TEXT.items():
                print(f"  {name:<9} {text}")
            choice = _ask("choose a profile (minimal / showcase) or press Enter to skip: ")
            if choice in PROFILES:
                profile = choice
            elif choice:
                raise SetupError(f"unknown profile {choice!r}")
        if profile is not None:
            wanted.update(PROFILES[profile])
        changes = plan(wanted, get)
        if not changes:
            print("\nnothing to change." if wanted else "\nnothing chosen, nothing changed.")
        else:
            print("\nI would set:")
            for name, value in changes.items():
                print(f"  {name} = {str(value).lower() if isinstance(value, bool) else value}")
            print("  (allow_direct_db_read stays off: spend and kanban stay closed unless you turn it on yourself)")
            if not yes and _ask("write these settings? [y/N] ") not in ("y", "yes"):
                print("not written.")
                changes = {}
        failed = []
        for name, value in changes.items():
            if not setter(_key(name), str(value).lower() if isinstance(value, bool) else str(value)):
                failed.append(name)
        if changes and not failed:
            print(f"written: {len(changes)} setting(s).")
        if failed:
            print(f"could not write: {', '.join(failed)} (is `hermes` on PATH? set them in Settings -> Plugins)", file=sys.stderr)
            return 1
    except EOFError:
        print("no terminal to ask on; nothing written. Run again with --yes and --profile/--engine-path.", file=sys.stderr)
        return 1

    if getattr(args, "first_run", False):
        return first_run(home=home, yes=yes, setter=setter, starter=starter, ask=ask or _ask, out=print)
    print("\nnext:")
    print("  hermes 3d setup --first-run   one command: Node from Hermes, the engine, start it (Camino A)")
    print("  hermes 3d demo   see your own Hermes as a world, read-only (needs an engine)")
    print("  hermes 3d scan   what the world would mirror, without an engine")
    print("  hermes 3d up     start the engine and the bridge with your settings")
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
    """The whole path for the Camino A user, in order, stopping at the first step that cannot pass.

    1 Node from Hermes' package manager  2 the pinned engine  3 ``engine_link: false`` (the world lives inside Desktop)
    4 start engine + bridge  5 say where to click. Each download or write is asked first; ``yes`` answers for scripts.
    """

    def confirm(prompt: str) -> bool:
        return yes or ask(prompt) in ("y", "yes")

    try:
        out("\nfirst run: 1/4 Node")
        try:
            node = nodebin.resolve_node(ensure=False)
        except NodeMissing:
            if not confirm("Hermes has no Node yet. Let Hermes' package manager download its pinned Node? [y/N] "):
                out("stopped: no Node, nothing else was done.")
                return 1
            node = nodebin.resolve_node(ensure=True)
        out(f"  node {node.version_text} from {'Hermes (pm)' if node.source == 'pm' else 'PATH (this Hermes has no pm)'}")
        for warning in node.warnings:
            out(f"  warning: {warning}")

        out("first run: 2/4 engine")
        cfg = S.Hermes3DSettings.from_yaml(home)
        if cfg.engine_main() is None:
            res = installer.install_engine(home, lock_path=lock_path, url=url, yes=yes, confirm=confirm, out=out)
            out(f"  engine ready ({res['seconds']} s, {res['size_mb']} MB)")
        else:
            out(f"  engine already set: {cfg.engine_path}")

        out("first run: 3/4 settings")
        cfg = S.Hermes3DSettings.from_yaml(home)
        if cfg.engine_link and confirm("Set engine_link=false so the world opens inside Hermes Desktop? [y/N] "):
            if not setter(_key("engine_link"), "false"):
                out("could not write engine_link (is `hermes` on PATH?). Set it in Settings -> Plugins and run again.")
                return 1
            cfg = S.Hermes3DSettings.from_yaml(home)
        out(f"  engine_link = {str(cfg.engine_link).lower()}, engine port {cfg.engine_port}")
        if cfg.engine_link:
            out("  note: engine_link is still true, so the embedded page cannot be used (an engine takes one source).")

        out("first run: 4/4 start")
        rc = starter(cfg) if starter is not None else 0
        if rc != 0:
            return rc
        seen = (probe or embed.probe_engine)(cfg.page_url())
        if seen.get("up") and seen.get("embed"):
            out("\nready: open the Hermes 3D page in Hermes Desktop.")
        elif seen.get("up"):
            out("\nthe engine is running but this world does not accept an embedded page yet:")
            out(f"  {embed.HINT_EMBED}")
        else:
            out("\nthe engine did not answer after starting; see `hermes 3d status`.")
            return 1
        return 0
    except Hermes3DError as exc:
        out(f"\nstopped: {exc}")
        return 1
    except EOFError:
        out("\nno terminal to ask on; run again with --yes.")
        return 1
