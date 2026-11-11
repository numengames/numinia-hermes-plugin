"""World -> Hermes actions (``cron.pausar`` / ``cron.reanudar``), through Hermes' own approval.

Why the shape is what it is (all verified against the installed Hermes 0.22.7 source):

* ``ctx.dispatch_tool`` goes straight to ``tools.registry.dispatch``; it does **not** run
  ``pre_tool_call`` hooks. So the plugin cannot "ask the gate" from a background thread with
  public API alone, and it must not import ``tools.approval`` (catalog rule 9).
* The public gate is a ``pre_tool_call`` hook that returns ``{"action": "approve", ...}`` for a
  tool call made inside an agent turn: Hermes then asks the person through its own prompt
  (CLI, gateway, desktop) and **fails closed** when nobody can answer (cron, other unattended
  runs, ``approvals`` modes). That is catalog rule 12.

So a request from the world never runs anything by itself. It is **queued** (``ActionQueue``)
and shown to Hermes as a pending action. Executing it is the job of the registered tool
``hermes3d_cron_action``; the ``pre_tool_call`` hook of this plugin marks every call to that tool
with ``approve``. If nobody approves within the TTL the world gets ``ok=false`` and the reason.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .crontable import clean_text
from .worldtext import say

TOOL_NAME = "hermes3d_cron_action"
VERB_TO_CLI = {"cron.pausar": "pause", "cron.reanudar": "resume"}
JOB_ID = re.compile(r"[a-z0-9][a-z0-9_-]{3,63}")  # always used with fullmatch: `$` would accept a trailing newline
SEEN_TTL = 600.0  # a request id is honoured once within this window (replay guard)
SEEN_MAX = 512
TTL_SECONDS = 25.0  # shorter than the engine's 30 s request expiry (A0 §3.2)
MAX_PENDING = 20


def _expire(fut: asyncio.Future[dict[str, Any]]) -> None:
    if not fut.done():
        fut.set_result(say("expired"))


@dataclass
class Pending:
    token: str
    peticion: str
    job_id: str
    verb: str
    quien: str
    created: float = field(default_factory=time.monotonic)
    future: asyncio.Future[dict[str, Any]] | None = None

    def expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) - self.created > TTL_SECONDS


EPOCH_MS_FLOOR = 10**12  # 2001-09 in ms; anything below is not a wall-clock time


def _expired(caduca: object) -> bool:
    """Has the engine's deadline already passed?

    The REAL engine stamps ``caduca`` as a SIM TICK number (``api.tick() + CADUCA_S * TICK_HZ``, features/espejo/server.ts), which
    the plugin cannot compare with its own clock. Treating that tick as epoch milliseconds refused every real request as «ya caducó»
    (found by tools/e2e_pedir_plugin.py against the real engine). So only a value that is plausibly an epoch-ms wall time is judged;
    a tick (or anything else) is ignored and the plugin's own ``TTL_SECONDS`` (shorter than the engine's 30 s) bounds the wait.
    """
    if not isinstance(caduca, (int, float)) or isinstance(caduca, bool) or caduca < EPOCH_MS_FLOOR:
        return False
    return caduca < time.time() * 1000


def _who(raw: object) -> str:
    """Who asked, for the approval prompt. The engine sends {id, nombre}; plain text is tolerated."""
    name = raw.get("nombre") if isinstance(raw, dict) else raw
    return "".join(ch for ch in str(name or "")[:40] if ch.isprintable())


class ActionQueue:
    """Pending world requests waiting for a human approval inside Hermes."""

    def __init__(
        self, *, enabled: Callable[[], bool], known_job: Callable[[str], bool], read_only: Callable[[], bool] = lambda: False
    ) -> None:
        self._read_only = read_only
        self._enabled = enabled
        self._known_job = known_job
        self._items: dict[str, Pending] = {}
        self._seen: OrderedDict[str, float] = OrderedDict()  # request ids already parked, newest last

    # ---- called by the hub (event loop thread) -----------------------------
    async def submit(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Validate and park a request; resolves when approved+executed, refused or expired."""
        if self._read_only():
            return say("read_only")
        if not self._enabled():
            return say("cron_off")
        verb = str(msg.get("verbo", ""))
        if verb not in VERB_TO_CLI:
            return say("verb_not_allowed")
        objeto = str(msg.get("objeto", ""))
        job_id = (objeto.split("/", 1)[1] if objeto.startswith("cron/") else objeto).lower()
        if not JOB_ID.fullmatch(job_id):
            return say("bad_job_id")
        if not self._known_job(job_id):
            return say("job_gone")
        self._purge()
        req_id = str(msg.get("id", ""))[:80]
        if not req_id:
            return say("bad_request")
        if self._is_replay(req_id):
            return say("replayed")  # the link frames are not signed: a captured request must not run twice
        if _expired(msg.get("caduca")):
            return say("expired_in_world")
        if len(self._items) >= MAX_PENDING:
            return say("too_many")
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        item = Pending(
            token=uuid.uuid4().hex[:12],
            peticion=str(msg.get("id", ""))[:80],
            job_id=job_id,
            verb=verb,
            quien=_who(msg.get("quien")),
            future=fut,
        )
        self._items[item.token] = item
        self._remember(req_id)
        try:
            return await asyncio.wait_for(asyncio.shield(fut), TTL_SECONDS)
        except TimeoutError:
            self._items.pop(item.token, None)
            return say("no_approver")

    def _is_replay(self, req_id: str) -> bool:
        now = time.monotonic()
        for key, at in list(self._seen.items()):
            if now - at > SEEN_TTL:
                self._seen.pop(key, None)
            else:
                break
        return req_id in self._seen

    def _remember(self, req_id: str) -> None:
        self._seen[req_id] = time.monotonic()
        while len(self._seen) > SEEN_MAX:
            self._seen.popitem(last=False)

    # ---- called from the tool handler (any thread) ---------------------------
    def pending(self) -> list[dict[str, Any]]:
        self._purge()
        return [
            {
                "token": p.token,
                "job_id": p.job_id,
                "verb": p.verb,
                "quien": p.quien,
                "age_s": round(time.monotonic() - p.created, 1),
            }
            for p in self._items.values()
        ]

    def claim(self, job_id: str, verb: str) -> Pending | None:
        """Take the oldest live pending request that matches (job, verb). Run only after approval."""
        self._purge()
        for token, p in list(self._items.items()):
            if p.job_id == job_id and p.verb == verb:
                return self._items.pop(token)
        return None

    def resolve(self, item: Pending, result: dict[str, Any]) -> None:
        fut = item.future
        if fut is not None and not fut.done():
            fut.get_loop().call_soon_threadsafe(lambda: fut.set_result(result) if not fut.done() else None)

    def _purge(self) -> None:
        now = time.monotonic()
        for token, p in list(self._items.items()):
            if p.expired(now):
                self._items.pop(token, None)
                fut = p.future
                if fut is not None and not fut.done():
                    fut.get_loop().call_soon_threadsafe(_expire, fut)


# ---- the approval hook ---------------------------------------------------------
def approval_directive(tool_name: str, args: dict[str, Any] | None) -> dict[str, str] | None:
    """``pre_tool_call`` callback body: escalate every call of our tool to Hermes' human gate.

    Returns ``{"action":"approve",...}`` (Hermes asks the person; denial, timeout or an
    unattended context block the call), or ``None`` for any other tool.
    """
    if tool_name != TOOL_NAME:
        return None
    a = args or {}
    verb = str(a.get("verb", ""))[:20]
    job = str(a.get("job_id", ""))[:64]
    cli = VERB_TO_CLI.get(verb, "?")
    return {
        "action": "approve",
        "message": f"The Hermes 3D world asks to run `hermes cron {cli} {job}`",
        "rule_key": f"hermes-3d:cron:{cli}",
    }


# ---- the tool handler ----------------------------------------------------------
def tool_schema() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": (
            "Carry out a pause/resume of a cron job that someone asked for from the Hermes "
            "Hermes 3D 3D world. Needs human approval; fails closed when nobody can approve."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "cron job id as shown by `hermes cron list`"},
                "verb": {"type": "string", "enum": list(VERB_TO_CLI), "description": "cron.pausar or cron.reanudar"},
            },
            "required": ["job_id", "verb"],
        },
    }


CLI_TIMEOUT = 30
Runner = Callable[..., Any]


def run_cron_cli(job_id: str, verb: str, *, runner: Runner = subprocess.run, hermes: str | None = None) -> dict[str, Any]:
    """Execute ``hermes cron pause|resume <id>`` as an argv list: no shell, nothing interpolated.

    Called only after Hermes' own approval gate said yes (see ``approval_directive``).
    """
    cli = VERB_TO_CLI.get(verb)
    if cli is None or not JOB_ID.fullmatch(job_id or ""):
        return say("invalid_request")
    exe = hermes or shutil.which("hermes")
    if exe is None:
        return say("hermes_missing")
    try:
        done = runner(
            [exe, "cron", cli, job_id],
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT,
            check=False,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except subprocess.TimeoutExpired:
        return say("timeout")
    except OSError as exc:
        return say("failed", detail=type(exc).__name__)
    if done.returncode != 0:
        lines = (done.stdout or done.stderr or "").strip().splitlines()
        return say("failed", detail=clean_text(lines[-1], 120) if lines else f"exit code {done.returncode}")
    return say("paused" if cli == "pause" else "resumed", ok=True)


Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
