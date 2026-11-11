"""hermes-3d — Hermes plugin entry point (MIT).

Registers, and nothing more (the manifest declares exactly this):
  tools  : hermes3d_state (read-only), hermes3d_cron_action (pause/resume a cron job after approval)
  hooks  : pre_tool_call (escalates hermes3d_cron_action to Hermes' own approval),
           on_session_start / on_session_end / on_session_finalize (publish `sesion` places)
  CLI    : `hermes 3d up|status|down`

It never edits Hermes files, never imports Hermes internals, never opens a port to the network
(the loopback bridge binds 127.0.0.1 only and lives in its own process, see bridge_main.py).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:  # the plugin dir is not a package on sys.path when Hermes loads it
    sys.path.insert(0, str(_HERE))

from hermes_3d_lib import actions, client, config, docsviewer, events, installer, linkkey, setup_cmd, supervisor
from hermes_3d_lib import settings as S
from hermes_3d_lib.crontable import clean_text
from hermes_3d_lib.errors import Hermes3DError
from hermes_3d_lib.worldtext import say

logger = logging.getLogger("hermes_3d")

STATE_SCHEMA = {
    "name": "hermes3d_state",
    "description": (
        "Read-only snapshot of this Hermes profile as the Hermes 3D world sees it: "
        "cron jobs, installed skills, plugin switches and the bridge state. Changes nothing."
    ),
    "parameters": {
        "type": "object",
        "properties": {"what": {"type": "string", "enum": ["all", "cron", "skills", "config", "bridge"], "default": "all"}},
    },
}


def _home() -> Path:
    return config.hermes_home()


# `ctx.get_config` is the documented reader of plugins.entries.hermes-3d.settings; it is bound at
# register() time. Before that (tests, tools) the same file is read directly: one source, one precedence.
_GETTER: S.Getter | None = None


def _getter() -> S.Getter:
    return _GETTER or S.yaml_getter(_home())


def _settings() -> S.Hermes3DSettings:
    return S.Hermes3DSettings.from_getter(_getter(), _home())


# ---- tools ---------------------------------------------------------------------------
def make_state_tool(ctx: Any):
    def handler(args: dict[str, Any], **_kw: Any) -> str:
        what = str((args or {}).get("what") or "all")
        out: dict[str, Any] = {}
        if what in ("all", "bridge"):
            res = client.call("GET", "/status")
            out["bridge"] = res["data"] if res.get("ok") else {"running": False, "reason": res.get("error")}
        if what in ("all", "config"):
            out["switches"] = S.switches(ctx.get_config)
        if what in ("all", "cron"):
            from hermes_3d_lib.bridge_main import run_cron_list
            from hermes_3d_lib.crontable import parse_cron_list, read_jobs_file

            parsed = read_jobs_file(_home())  # Hermes' own jobs.json first, the printed table only as fallback
            if parsed is None:
                text = run_cron_list()
                parsed = None if text is None else parse_cron_list(text)
            out["cron"] = (
                {"error": "hermes cron list failed"}
                if parsed is None
                else {
                    "jobs": [
                        {k: j[k] for k in ("id", "name", "estado", "schedule", "next_run", "ultimo")} for j in parsed["jobs"]
                    ]
                }
            )
        if what in ("all", "skills"):
            skills_dir = _home() / "skills"
            names: list[str] = []
            if skills_dir.is_dir():
                for p in sorted(skills_dir.glob("*/SKILL.md"))[:200]:
                    names.append(clean_text(p.parent.name, 60))
            out["skills"] = {"count": len(names), "names": names}
        return json.dumps(out, ensure_ascii=False)

    return handler


def make_action_tool(ctx: Any):
    def handler(args: dict[str, Any], **_kw: Any) -> str:
        job_id, verb = str((args or {}).get("job_id", "")), str((args or {}).get("verb", ""))
        if S.read_only(_getter()):
            return json.dumps({k: v for k, v in say("read_only").items() if k in ("ok", "text")})
        claim = client.call("POST", "/action/claim", {"job_id": job_id, "verb": verb})
        if claim.get("status") == 403:  # the bridge was started with --read-only
            return json.dumps({k: v for k, v in say("read_only").items() if k in ("ok", "text")})
        if not claim.get("ok"):
            return json.dumps({k: v for k, v in say("no_pending").items() if k in ("ok", "text")})
        token = claim["data"]["token"]
        result = actions.run_cron_cli(job_id, verb)
        client.call("POST", "/action/resolve", {"token": token, "ok": result["ok"], "texto": result["texto"]})
        return json.dumps({k: v for k, v in result.items() if k in ("ok", "text")})

    return handler


# ---- hooks ------------------------------------------------------------------------------
_ACTIVITY: dict[str, Any] = {}  # events.make_hooks(...) callbacks, set by register()


def _activity(hook: str, **kw: Any) -> None:
    fn = _ACTIVITY.get(hook)
    if fn is not None:
        fn(**kw)


def pre_tool_call(tool_name: str = "", args: dict[str, Any] | None = None, **kw: Any):
    # The activity feed gets the tool NAME and the session id only; ``args`` goes to the approval check alone.
    _activity("pre_tool_call", tool_name=tool_name, session_id=kw.get("session_id", ""))
    return actions.approval_directive(tool_name, args)


def _observer(name: str):
    """An observer hook that feeds the activity feed. Payload fields are picked by name inside ``events``."""

    def hook(**kw: Any) -> None:
        _activity(name, **kw)

    hook.__name__ = f"hermes3d_{name}"
    return hook


def make_session_hooks(ctx: Any):
    def _push(kind: str, **fields: Any) -> None:
        if not S.switches(ctx.get_config).get("sesiones"):
            return
        try:  # a dead bridge must never slow or break a session
            client.call("POST", "/event", {"kind": kind, **fields}, timeout=0.5)
        except Exception:
            logger.debug("hermes-3d: session event dropped", exc_info=True)

    def on_session_start(session_id: str = "", model: str = "", platform: str = "", **_kw: Any) -> None:
        _activity("on_session_start", session_id=session_id, platform=platform)
        _push("sesion_inicio", session_id=str(session_id)[:64], model=str(model)[:40], platform=str(platform)[:20])

    def on_session_end(session_id: str = "", **_kw: Any) -> None:
        # Fires at every turn finalisation, so it is NOT the end of the place (see on_session_finalize).
        _push("sesion_fin", session_id=str(session_id)[:64])

    def on_session_finalize(session_id: str = "", **_kw: Any) -> None:
        _push("sesion_cierre", session_id=str(session_id)[:64])

    return on_session_start, on_session_end, on_session_finalize


# ---- CLI: hermes 3d up|status|down ---------------------------------------------------------
def _setup_cli(parser: Any) -> None:
    sub = parser.add_subparsers(dest="hermes3d_cmd")
    up = sub.add_parser("up", help="locate the engine, start or reuse the hermes world, start the bridge")
    up.add_argument("--read-only", action="store_true", help="the world can look but never change anything in Hermes")
    sub.add_parser(
        "demo",
        help="see your own Hermes (profiles, cron, spend) as a world, read-only; refuses to start unless read-only is enforced",
    )
    ie = sub.add_parser(
        "install-engine",
        help="download the pinned Numinia Engine release, verify its sha256, unpack it (asks first; refuses unless engine.lock.json pins a release)",
    )
    ie.add_argument("--yes", action="store_true", help="do not ask for confirmation (scripts)")
    ie.add_argument("--no-shortcut", action="store_true", help="do not create a launcher shortcut")
    ie.add_argument("--url", help=argparse.SUPPRESS)  # tests only: the sha256 of the lock still has to match
    st = sub.add_parser(
        "setup",
        help="guided first run: checks Node, the engine and the docs viewer, explains the switches, writes settings after you confirm",
    )
    st.add_argument(
        "--profile",
        choices=("minimal", "showcase"),
        help="minimal = health only; showcase = health, cron, agents, skills, docs, read-only",
    )
    st.add_argument("--engine-path", help="folder of a Numinia Engine checkout to remember as engine_path")
    st.add_argument("--yes", action="store_true", help="do not ask (scripts); changes only what --profile/--engine-path name")
    st.add_argument(
        "--first-run", action="store_true", help="the whole path: Node from Hermes, pinned engine, engine_link=false, start it"
    )
    sub.add_parser("status", help="show engine and bridge state")
    sub.add_parser("down", help="stop the bridge (and the engine if this plugin started it)")
    dv = sub.add_parser("docs", help="start the local documentation viewer (127.0.0.1 only) the world's library opens")
    dv.add_argument("--stop", action="store_true", help="stop the viewer this plugin started")
    sc = sub.add_parser(
        "scan", help="read-only scan of this Hermes (profiles, cron, sessions, skills, MCP, kanban, projects, docs, spend)"
    )
    sc.add_argument("--json", action="store_true", help="print the full typed snapshot as JSON (counts and names only)")
    sc.add_argument("--days", type=int, default=30, help="window for sessions and spend (default 30)")
    sc.add_argument(
        "--direct-db",
        action="store_true",
        help="read state.db / kanban.db for this run (else only if allow_direct_db_read is on)",
    )
    parser.set_defaults(hermes3d_cmd=None)


def _cli(args: Any) -> int:
    cmd = getattr(args, "hermes3d_cmd", None)
    fn = {
        "up": lambda: cmd_up(read_only=bool(getattr(args, "read_only", False))),
        "demo": cmd_demo,
        "setup": lambda: setup_cmd.cmd_setup(args, home=_home(), starter=lambda cfg: cmd_up(cfg=cfg)),
        "install-engine": lambda: cmd_install_engine(args),
        "status": cmd_status,
        "down": cmd_down,
        "docs": lambda: cmd_docs(stop=bool(getattr(args, "stop", False))),
        "scan": lambda: cmd_scan(
            bool(getattr(args, "json", False)), int(getattr(args, "days", 30) or 30), bool(getattr(args, "direct_db", False))
        ),
    }.get(cmd or "")
    if fn is None:
        print("usage: hermes 3d setup|up|demo|status|down|scan|docs")
        return 2
    try:
        return fn()
    except (supervisor.SupervisorError, S.SettingsError, Hermes3DError, setup_cmd.SetupError) as exc:
        print(f"hermes 3d: {exc}", file=sys.stderr)
        return 1


def cmd_up(read_only: bool = False, demo: bool = False, cfg: S.Hermes3DSettings | None = None) -> int:
    home = _home()
    cfg = cfg or _settings()
    read_only = read_only or cfg.read_only or demo
    rt = config.read_runtime(home)
    bridge_running = bool(rt and config.pid_alive(rt.get("pid", 0)))
    started = supervisor.start_engine(cfg)  # the engine always has a stdin pipe; the bridge holds the write end
    stdin_fd = started.get("stdin_fd")
    try:
        if bridge_running and stdin_fd is not None:
            # an old bridge cannot take over the new engine's pipe: replace it, or the engine would exit with this command
            client.call("POST", "/stop", home=home)
            end = time.monotonic() + 8
            while rt and config.pid_alive(rt.get("pid", 0)) and time.monotonic() < end:
                time.sleep(0.1)
            bridge_running, rt = False, None
        return _up_rest(home, cfg, started, rt, bridge_running, stdin_fd, read_only, demo)
    finally:
        if stdin_fd is not None:
            os.close(stdin_fd)


def _up_rest(
    home: Any, cfg: Any, started: dict[str, Any], rt: Any, bridge_running: bool, stdin_fd: Any, read_only: bool, demo: bool
) -> int:
    for warning in started.get("node_warnings", []):
        print(f"warning: {warning}", file=sys.stderr)
    print(
        ("reusing the engine already running" if started["reused"] else "started the engine")
        + f" (game 127.0.0.1:{cfg.engine_port}, link 127.0.0.1:{started['link_port']})"
    )
    if started["reused"]:
        print(
            f"note: the engine was not started by this plugin; if the bridge cannot connect, set {linkkey.ENV_LINK_KEY} "
            "to that engine's MOTOR_ENLACE_CLAVE (see README, Configuration)"
        )
    elif not started.get("link_enabled", True):
        print(
            f"note: {cfg.world_path / supervisor.SWITCH_FILE} is missing or not active, so the engine opens no link; "
            "the bridge keeps retrying until the world turns the mirror on"
        )
    if not started["reused"] and not started.get("serves_page", False):
        print(
            "note: no built page found under the engine folder (dist/plaza/index.html), so the engine serves no page; "
            "build the engine's page or serve it yourself"
        )
    if bridge_running and rt:
        running = client.call("GET", "/status", home=home).get("data", {})
        if (read_only and not running.get("read_only")) or (demo and not running.get("demo")):
            raise supervisor.SupervisorError(
                "a bridge is already running in another mode; run `hermes 3d down` and then start it again "
                f"(`hermes 3d {'demo' if demo else 'up --read-only'}`)"
            )
        print(f"bridge already running (pid {rt['pid']})")
    else:
        pid = supervisor.spawn_bridge(home, read_only=read_only, demo=demo, hold_fd=stdin_fd)
        print(f"bridge started (pid {pid}); log: {config.plugin_data_dir(home) / 'bridge.log'}")
    if demo:
        _require_read_only(home)
    if read_only:
        print("mode: read-only (the world can look; it cannot change anything in Hermes)")
    if started.get("pid"):
        config.write_engine_pid(started["pid"], home)
    print(f"open the world: {cfg.page_url()}")
    return 0


READ_ONLY_WAIT = 10.0  # seconds the demo waits for the bridge to confirm read-only


def _require_read_only(home: Any, wait: float | None = None) -> None:
    """The demo looks at the user's real Hermes: unless the bridge itself says read-only, stop it and refuse."""
    import time

    end = time.monotonic() + (READ_ONLY_WAIT if wait is None else wait)
    while True:
        data = client.call("GET", "/status", home=home).get("data") or {}
        if data.get("read_only") is True and data.get("demo") is True:
            return
        if time.monotonic() >= end or data.get("read_only") is False:
            break
        time.sleep(0.2)
    client.call("POST", "/stop", home=home)
    raise supervisor.SupervisorError(
        "refusing to show your Hermes: read-only could not be confirmed on the bridge, so it was stopped"
    )


def _ask(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        raise installer.InstallError("no terminal to ask on; run it again with --yes if you do want the download") from None


def cmd_install_engine(args: Any) -> int:
    """``hermes 3d install-engine``: user-initiated; see :mod:`hermes_3d_lib.installer`."""
    res = installer.install_engine(
        _home(),
        lock_path=installer.default_lock_path(),
        url=getattr(args, "url", None),
        yes=bool(getattr(args, "yes", False)),
        confirm=_ask,
        no_shortcut=bool(getattr(args, "no_shortcut", False)),
    )
    print(f"installed {res['tag']} ({res['sha256'][:12]}); start the world with: hermes 3d up")
    return 0


def cmd_demo() -> int:
    """``hermes 3d demo``: your real Hermes as a world, read-only, agents + spend + cron on, counts only."""
    from hermes_3d_lib import scan

    home = _home()
    snap = scan.scan_hermes(days=_settings().scan_days, allow_direct_db=True)  # the demo is the explicit, read-only look at spend
    s = scan.summary(snap)
    cmd_up(demo=True)
    print("mirrored in the world (counts only, nothing is written to Hermes):")
    print(f"  agents (profiles): {s['agentes']}")
    print(f"  cron jobs        : {s['cron']}")
    print(f"  spend            : {s['tokens']} tokens, about ${s['coste_estimado_usd']} in the last {_settings().scan_days} days")
    print("mode: read-only enforced by the bridge (the world cannot pause, resume or run anything)")
    print(f"hermes home scanned: {snap['root_name']} ({home.name})")
    return 0


def cmd_scan(as_json: bool, days: int, direct_db: bool = False) -> int:
    """``hermes 3d scan [--json] [--direct-db]``: read-only; never starts anything, never calls the engine.

    Spend, sessions, kanban and projects come from Hermes' private SQLite files, so they are read only when the setting
    ``allow_direct_db_read`` is on or you pass ``--direct-db`` for this one run."""
    from hermes_3d_lib import scan

    allow = direct_db or _settings().allow_direct_db_read
    snap = scan.scan_hermes(days=days, allow_direct_db=allow)
    if as_json:
        print(json.dumps(snap, ensure_ascii=False, indent=2))
    else:
        s = scan.summary(snap)
        print(f"scan of {snap['root_name']} in {snap['took_ms']} ms (read-only)")
        for k, v in s.items():
            print(f"  {k:20} {v}")
        for name, sec in snap["sections"].items():
            if sec.get("error"):
                print(f"  ! {name}: {sec['error']}")
            elif sec.get("skipped"):
                print(f"  - {name}: not read (allow_direct_db_read is off; use --direct-db for this run)")
    return 0 if all(sec.get("ok") for sec in snap["sections"].values()) else 1


def _mode_text(read_only: bool) -> str:
    if read_only:
        return "read-only"
    return "read-write (cron pause/resume from the world, each one needs your approval)"


def cmd_status() -> int:
    home = _home()
    cfg = _settings()
    print(f"engine folder : {cfg.engine_path if cfg.engine_main() else 'NOT SET (setting engine_path)'}")
    lport = supervisor.link_port(cfg)
    print(
        f"engine link   : {'listening' if supervisor.link_listening(lport) else 'not listening'} on 127.0.0.1:{lport}"
        f"{'' if supervisor.espejo_enabled(cfg.world_path) else ' (world has no active entorno/espejo.json)'}"
    )
    print(f"link key      : {linkkey.source(home)}")
    dv = docsviewer.status(home, cfg)
    print(
        "docs viewer   : "
        + (
            f"running (pid {dv['pid']}) at {dv['url']}"
            if dv["running"]
            else f"{'port in use by another program' if dv['listening'] else 'not running'} "
            f"(`hermes 3d docs`; {'built' if dv['built'] else 'not built yet'})"
        )
    )
    res = client.call("GET", "/status", home=home)
    if res.get("ok"):
        d = res["data"]
        print(
            f"bridge        : running, link {'connected' if d.get('connected') else 'NOT connected'}"
            f"{' - ' + d['last_error'] if d.get('last_error') else ''}"
        )
        print(f"switches      : {d.get('switches')}")
        print(f"mode          : {_mode_text(bool(d.get('read_only')))}")
        dropped = d.get("dropped") or {}
        if dropped.get("total"):
            print(f"left out by the engine: {dropped['total']} object(s) of the last lote")
            for it in dropped.get("items", [])[:5]:
                print(f"  - {it.get('tipo')}/{it.get('id')}: {it.get('motivo')}")
    else:
        print(f"bridge        : not running ({res.get('error')})")
        print(
            f"mode          : {_mode_text(cfg.read_only)}{' (setting, applied at the next `hermes 3d up`)' if cfg.read_only else ''}"
        )
    return 0


def cmd_docs(stop: bool = False) -> int:
    """``hermes 3d docs [--stop]``: the local docs viewer (lite build) on 127.0.0.1 for the world's library."""
    home = _home()
    cfg = _settings()
    if stop:
        if docsviewer.stop(home):
            print("docs viewer stopped (or it was not running)")
            return 0
        print("the docs viewer did not stop; leaving it")
        return 1
    info = docsviewer.start(home, cfg)
    if info["built"]:
        print("built the lite documentation site")
    print(("docs viewer already running" if info["reused"] else "docs viewer started") + f" (pid {info['pid']}, 127.0.0.1 only)")
    print(f"open the docs: {info['url']}")
    print(f"the world must list http://127.0.0.1:{info['port']} in documentos.origenes (entorno/espejo.json)")
    return 0


def cmd_down() -> int:
    home = _home()
    res = client.call("POST", "/stop", home=home)
    print("bridge asked to stop" if res.get("ok") else f"bridge: {res.get('error')}")
    rec = config.read_engine_record(home)
    if rec is None:
        config.clear_engine_pid(home)
        print("engine left as it was (not started by this plugin, or no longer running)")
    elif supervisor.stop_engine(rec):
        config.clear_engine_pid(home)
        print(f"engine stopped (pid {rec['pid']})")
    else:
        print(f"engine pid {rec['pid']} did not stop; leaving it")
        return 1
    return 0


# ---- register ----------------------------------------------------------------------------------
def register(ctx: Any) -> None:
    global _GETTER
    _GETTER = ctx.get_config
    ctx.register_tool(
        name="hermes3d_state",
        toolset="hermes-3d",
        schema=STATE_SCHEMA,
        handler=make_state_tool(ctx),
        description="Read-only Hermes 3D snapshot",
        emoji="🏛",
    )
    ctx.register_tool(
        name=actions.TOOL_NAME,
        toolset="hermes-3d",
        schema=actions.tool_schema(),
        handler=make_action_tool(ctx),
        description="Pause/resume a cron job asked for from the world (needs approval)",
        emoji="⏯",
    )
    _ACTIVITY.clear()
    _ACTIVITY.update(events.make_hooks(lambda: bool(S.switches(ctx.get_config).get("sesiones"))))
    ctx.register_hook("pre_tool_call", pre_tool_call)
    for name in ("post_tool_call", "pre_llm_call", "pre_api_request", "subagent_start", "subagent_stop"):
        ctx.register_hook(name, _observer(name))
    start, end, finalize = make_session_hooks(ctx)
    ctx.register_hook("on_session_start", start)
    ctx.register_hook("on_session_end", end)
    ctx.register_hook("on_session_finalize", finalize)
    ctx.register_cli_command(
        "3d",
        "Hermes 3D world: up | status | down",
        _setup_cli,
        _cli,
        description="Run the local Numinia 'hermes' world and its bridge",
    )
