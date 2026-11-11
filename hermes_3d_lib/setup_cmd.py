# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""``hermes 3d setup``: the first run. Checking, the plan, one question, doing, a closing. No preset to choose.

Design rules
* Nothing is written without a confirmation, except with ``--yes`` (scripts).
* ``setup`` applies ONE default set (``DEFAULT_SET``: health, cron, agents, skills and docs, read-only, no direct database
  reads). More switches are chosen later, in the plugin settings; the catalog default stays "everything off but health".
* Settings are written through Hermes' own ``hermes config set`` (argv list, no shell), never by editing ``config.yaml`` here,
  so comments and the schema checks of Hermes stay intact.
* Idempotent: only the keys whose value differs are written; a second run says "nothing to change".
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import io
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import config, docsviewer, embed, installer, linkkey, nodebin, supervisor
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


def _ask(prompt: str) -> str:
    return input(prompt).strip().lower()


def _shown(value: Any) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


# ---- facts measured at run time ---------------------------------------------------------------------------
PLUGIN_YAML = Path(__file__).resolve().parents[1] / "plugin.yaml"
_VER = re.compile(r"v?(\d+)\.(\d+)(?:\.(\d+))?")


def plugin_facts() -> tuple[str, str]:
    """(version, requires_hermes) read from plugin.yaml, or ('?', '')."""
    try:
        text = PLUGIN_YAML.read_text(encoding="utf-8")
    except OSError:
        return "?", ""
    ver = re.search(r"^version:\s*(\S+)", text, re.M)
    req = re.search(r"^requires_hermes:\s*[\"']?([^\"'\n]+)", text, re.M)
    return (ver.group(1) if ver else "?"), (req.group(1).strip() if req else "")


def hermes_version() -> str:
    """The running Hermes' version from its public CLI (`hermes --version`), else package metadata; '' if neither answers."""
    exe = shutil.which("hermes")
    if exe:
        try:
            res = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError):
            res = None
        m = _VER.search(res.stdout or "") if res is not None and res.returncode == 0 else None
        if m:
            return ".".join(g for g in m.groups() if g is not None)
    try:
        return importlib.metadata.version("hermes-agent")
    except importlib.metadata.PackageNotFoundError:
        return ""


def version_supported(version: str, spec: str) -> bool | None:
    """Does ``version`` satisfy a spec like '>=0.22.0, <0.24'? None when either cannot be read."""
    m = _VER.match(version)
    if not m or not spec:
        return None
    have = (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
    for part in spec.split(","):
        pm = re.fullmatch(r"\s*(>=|<=|<|>|==)\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?\s*", part)
        if not pm:
            return None
        want = (int(pm.group(2)), int(pm.group(3) or 0), int(pm.group(4) or 0))
        op = pm.group(1)
        ok = {">=": have >= want, "<=": have <= want, "<": have < want, ">": have > want, "==": have == want}[op]
        if not ok:
            return False
    return True


def _mb(n: float) -> str:
    return f"{n / 1_000_000:.1f} MB" if n >= 100_000 else f"{n / 1000:.1f} kB"


# ---- the screen --------------------------------------------------------------------------------------------
class Screen:
    """Prints the first run. On a terminal with Rich (Hermes ships it): green ✓, dim secondary text, a spinner on the running
    step and a live bar for the download. Otherwise (pipes, tests, no Rich) the same lines in plain text."""

    def __init__(self, out: Callable[..., None] = print) -> None:
        self.out = out
        self.console: Any = None
        self._live: Any = None
        self._progress: Any = None
        self._task: Any = None
        if out is print and sys.stdout.isatty():
            try:
                from rich.console import Console

                self.console = Console()
            except ImportError:
                self.console = None

    def _say(self, plain: str, markup: str) -> None:
        if self.console is not None:
            self.console.print(markup)
        else:
            self.out(plain)

    @staticmethod
    def _esc(text: str) -> str:
        return text.replace("[", "\\[")

    def title(self, text: str) -> None:
        self._say(f"\n{text}", f"\n[bold]{self._esc(text)}[/bold]")

    def plain(self, text: str, dim: bool = False) -> None:
        self._say(text, f"[dim]{self._esc(text)}[/dim]" if dim else self._esc(text))

    def ok(self, label: str, detail: str = "") -> None:
        self.stop()
        self._say(
            f"  ✓ {label:<22}{detail}".rstrip(),
            f"  [green]✓[/green] {self._esc(label):<22}[dim]{self._esc(detail)}[/dim]".rstrip(),
        )

    def note(self, label: str, detail: str = "") -> None:
        self._say(f"  · {label:<22}{detail}".rstrip(), f"  [dim]· {self._esc(label):<22}{self._esc(detail)}[/dim]".rstrip())

    def warn(self, label: str, detail: str = "") -> None:
        self._say(
            f"  ! {label:<22}{detail}".rstrip(),
            f"  [yellow]![/yellow] {self._esc(label):<22}[dim]{self._esc(detail)}[/dim]".rstrip(),
        )

    def fail(self, label: str, why: str, fix: str) -> None:
        self.stop()
        self._say(f"  ✗ {label}", f"  [red]✗[/red] {self._esc(label)}")
        self._say(f"    {why}", f"    {self._esc(why)}")
        self._say(f"    Fix: {fix}", f"    Fix: [bold]{self._esc(fix)}[/bold]")

    def plan_item(self, n: int, what: str, detail: str) -> None:
        self._say(f"  {n}  {what}", f"  {n}  {self._esc(what)}")
        self._say(f"     {detail}", f"     [dim]{self._esc(detail)}[/dim]")

    def start(self, label: str, total: int = 0) -> None:
        """A step is running: a spinner (and a bar when the size is known). Nothing on plain output until ``ok``."""
        self.stop()
        if self.console is None:
            return
        from rich.progress import BarColumn, DownloadColumn, Progress, SpinnerColumn, TextColumn, TransferSpeedColumn

        cols: list[Any] = [SpinnerColumn(), TextColumn("{task.description}")]
        if total:
            cols += [BarColumn(bar_width=24), DownloadColumn(), TransferSpeedColumn()]
        self._progress = Progress(*cols, console=self.console, transient=True)
        self._progress.start()
        self._task = self._progress.add_task(label, total=total or None)

    def advance(self, done: int) -> None:
        if self._progress is not None:
            self._progress.update(self._task, completed=done)

    def stop(self) -> None:
        if self._progress is not None:
            self._progress.stop()
            self._progress = None


def _explain(home: Path) -> int:
    print("Hermes 3D switches. Every one is off unless you turn it on; `hermes 3d setup` turns on the first group only.")
    print("Change any of them later in Hermes Desktop: Settings > Plugins > Hermes 3D.\n")
    print(describe_switches())
    print(f"\n`hermes 3d setup` turns on: {', '.join(SWITCH_TEXT[k][0] for k, v in DEFAULT_SET.items() if v is True)}.")
    print(
        "Kanban, Projects and Spend read Hermes' private SQLite files (read-only, counts and titles) and need `allow_direct_db_read` too."
    )
    return 0


def cmd_setup(
    args: Any,
    *,
    home: Path,
    setter: Callable[[str, str], bool] | None = None,
    get: S.Getter | None = None,
    starter: Callable[[S.Hermes3DSettings], int] | None = None,
    ask: Callable[[str], str] | None = None,
) -> int:
    """``hermes 3d setup [--engine-path DIR] [--yes] [--explain]``. ``--first-run`` is kept as an alias of the plain command."""
    if getattr(args, "explain", False):
        return _explain(home)
    engine_arg = getattr(args, "engine_path", None)
    if engine_arg:
        _engine_folder(engine_arg, home)  # refuse a bad folder before any screen
    return first_run(
        home=home,
        yes=bool(getattr(args, "yes", False)),
        setter=setter or hermes_config_set(home),
        starter=starter,
        ask=ask or _ask,
        get=get,
        engine_arg=engine_arg,
    )


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
    get: S.Getter | None = None,
    engine_arg: str | None = None,
) -> int:
    """Checking, the plan, one question (default yes), doing, a closing. Every figure is measured here, none is typed in."""
    ui = Screen(out)
    get = get or S.yaml_getter(home)
    plugin_version, spec = plugin_facts()
    cfg = S.Hermes3DSettings.from_getter(get, home)
    started = time.monotonic()

    ui.plain(f"Hermes 3D {plugin_version} · first-run setup")
    ui.plain("Turn your Hermes into a 3D world you can walk through.", dim=True)

    # ---- Checking ----
    ui.title("Checking this computer")
    hv = hermes_version()
    supported = version_supported(hv, spec)
    if not hv:
        ui.warn("Hermes", "version not readable; going on")
    elif supported is False:
        ui.warn(f"Hermes {hv}", f"outside the tested range ({spec}); it may still work")
    else:
        ui.ok(f"Hermes {hv}", f"supported ({spec})" if supported else "")
    try:
        node = nodebin.resolve_node(ensure=False)
    except NodeMissing:
        node = None
    if node is not None:
        ui.ok(f"Node {node.version_text}", "provided by Hermes" if node.source == "pm" else "from your PATH")
    else:
        ui.note("Node", "not installed yet")
    own_engine = bool(engine_arg) or cfg.engine_main() is not None
    lock: installer.Lock | None = None
    reused = False
    if own_engine:
        detail = engine_arg or str(cfg.engine_path)
        with contextlib.suppress(Hermes3DError):
            pinned = installer.load_lock(lock_path)
            if not engine_arg and cfg.engine_path is not None and cfg.engine_path.name == pinned.dir_name:
                detail = f"{pinned.tag} already installed, nothing to download"
        ui.ok("Engine", detail)
    else:
        try:
            lock = installer.load_lock(lock_path)
            installer.validate_lock(lock)
            reused = installer.is_installed(installer.engine_dir(home, lock), lock)
        except Hermes3DError as exc:
            ui.fail("Engine release", str(exc), "wait for a pinned release, or: hermes 3d setup --engine-path /path/to/engine")
            return 1
        if reused:
            ui.ok("Engine", f"{lock.tag} already installed, nothing to download")
        else:
            ui.note("Engine", "not installed yet")

    # ---- The plan ----
    wanted: dict[str, Any] = dict(DEFAULT_SET)
    if engine_arg:
        wanted["engine_path"] = _engine_folder(engine_arg, home)
    changes = plan(wanted, get)
    wants_desktop = cfg.engine_link
    ui.title("The plan." if yes else "The plan. Nothing happens until you say yes.")
    n = 0
    if node is None:
        n += 1
        ui.plan_item(n, "Install Node with Hermes", "Hermes' own package manager fetches the version it pins")
    if lock is not None and not reused:
        n += 1
        ui.plan_item(
            n,
            f"Download the Numinia Engine ({lock.tag})",
            f"{lock.size_text} from {urlsplit(lock.url).netloc}, checked against a pinned sha256. "
            "The file is a self-extracting archive; Hermes 3D unpacks it itself and never runs it",
        )
    n += 1
    ui.plan_item(
        n,
        "Show health, cron jobs, agents, skills, docs",
        "read-only: the world can look, never change. Names, states and counts, never your messages."
        + (" Turns the same ones on in the world folder Hermes 3D keeps" if supervisor.own_world(cfg) else ""),
    )
    if changes or wants_desktop:
        extra = " and open inside Hermes Desktop" if wants_desktop else ""
        n += 1
        ui.plan_item(
            n,
            "Save these settings",
            f"{len(changes) + (1 if wants_desktop else 0)} keys in your config{extra}; change them any time in Settings > Plugins",
        )
    n += 1
    ui.plan_item(
        n, "Start the world on this computer", "127.0.0.1 only; nothing leaves the machine. No launcher or shortcut is created"
    )

    try:
        if not yes and ask("\nGo ahead? [Y/n] ") not in ("", "y", "yes"):
            ui.plain("Nothing was changed.")
            return 0
    except EOFError:
        ui.plain("\nNo terminal to ask on, so nothing was changed. Run it again with --yes.")
        return 1

    # ---- Doing ----
    ui.title("Doing it")
    label = "Setting up"
    try:
        if node is None:
            label = "Node"
            ui.start("Installing Node with Hermes")
            node = nodebin.resolve_node(ensure=True)
            ui.ok(f"Node {node.version_text}", "provided by Hermes" if node.source == "pm" else "from your PATH")
        for warning in node.warnings if node else ():
            ui.warn("Note", warning)

        if lock is not None:
            label = "Engine"
            total = lock.size_bytes
            clock = {"t": time.monotonic()}

            def on_step(name: str, **facts: Any) -> None:
                now = time.monotonic()
                if name == "download_start":
                    ui.start(f"Downloading {lock.tag}", total)
                    clock["t"] = now
                elif name == "downloaded":
                    ui.ok(f"Downloaded {_mb(facts['bytes'])}", f"{now - clock['t']:.1f} s")
                elif name == "verified":
                    ui.ok("Checksum matches", "the pin and the release's SHA256SUMS" if facts.get("sums") else "the pin")
                elif name == "unpack_start":
                    ui.start("Unpacking")
                    clock["t"] = now
                elif name == "unpacked":
                    ui.ok("Unpacked", f"{facts['files']} files, {_mb(facts['bytes'])} · {now - clock['t']:.1f} s")

            res = installer.install_engine(
                home,
                lock_path=lock_path,
                url=url,
                yes=True,
                out=lambda *a, **k: None,
                no_shortcut=True,
                on_step=on_step,
                progress=ui.advance,
            )
            if res["reused"]:
                ui.ok("Engine", f"{lock.tag} already installed")
            ui.plain(f"    in {res['engine_path']}", dim=True)

        label = "Settings"
        todo = dict(changes)
        failed = [name for name, value in todo.items() if not setter(_key(name), _shown(value))]
        if wants_desktop and not setter(_key("engine_link"), "false"):
            failed.append("engine_link")
        if failed:
            ui.fail(
                "Settings",
                f"could not write: {', '.join(failed)}",
                "make sure `hermes` is on your PATH, or set them in Settings > Plugins",
            )
            return 1
        saved = len(todo) + (1 if wants_desktop else 0)
        ui.ok("Settings saved" if saved else "Settings", f"{saved} changed" if saved else "already as planned")

        label = "World"
        cfg = S.Hermes3DSettings.from_yaml(home)
        ui.start("Starting the world")
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            rc = starter(cfg) if starter is not None else 0
        if rc != 0:
            ui.fail(
                "World did not start",
                sink.getvalue().strip().splitlines()[-1] if sink.getvalue().strip() else f"exit code {rc}",
                "hermes 3d status",
            )
            return rc
        seen = (probe or embed.probe_engine)(cfg.page_url())
        if not seen.get("up"):
            ui.fail("World did not answer", "the engine was started but is not responding", "hermes 3d status")
            return 1
        ui.ok("World started", cfg.page_url())
        if not seen.get("embed"):
            ui.warn("Note", f"this world does not accept an embedded page yet. {embed.HINT_EMBED}")
    except Hermes3DError as exc:
        ui.fail(f"{label} failed", str(exc), "hermes 3d setup   (run it again; nothing half-done is kept)")
        return 1
    except EOFError:
        ui.plain("No terminal to ask on. Run it again with --yes.")
        return 1

    # ---- Closing ----
    ui.title("Your Hermes is a place now.")
    ui.plain(f"  Open it      Hermes Desktop → Hermes 3D, or {cfg.page_url()}")
    ui.plain("  Show more    hermes 3d setup --explain   (Kanban, Projects and Spend need your opt-in)")
    ui.plain("  Stop it      hermes 3d down")
    ui.plain(f"  Took {time.monotonic() - started:.1f} s", dim=True)
    return 0
