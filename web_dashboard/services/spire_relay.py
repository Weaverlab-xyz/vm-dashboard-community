"""Reach the dashboard's SPIRE server from a process that has no path to it.

``services/dashboard_spire`` drives the server with its own CLI, over one of two LOCAL
transports: ``docker exec`` into the compose overlay's container, or the admin socket of a
sidecar (``SPIRE_SERVER_SOCKET``). Both only exist where the server is. On Docker Compose
that is every container with the host's Docker socket, app and worker alike. On a managed
container runtime — Azure Container Apps, Cloud Run, ECS/Fargate, a Kubernetes pod — the
server is a sidecar of the APP, the socket is a volume of that pod only, and the worker is
a separate deployment no platform lets it share a volume with.

So a process with neither transport relays: it writes the call to ``spire_admin_calls``,
an app process that has a local transport claims it (``UPDATE … WHERE status='pending'``,
the job queue's portable claim), runs it, and writes the result back. The database is the
one thing every deployment already shares, on every platform, so this needs no new network
path, no new port and no new credential.

What crosses the table is the same argv the local transports run, and the requester
applies the same result checks — so a caller cannot tell which transport answered.
Encrypted at rest: stdin and both output streams (a join token, a JWT-SVID).
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# The subcommands dashboard_spire uses. A row asking for anything else is refused by the
# server side: the table is written by code, but the server executes what it reads.
ALLOWED_COMMANDS = frozenset({"agent", "bundle", "entry", "federation", "healthcheck",
                              "jwt", "token"})

POLL_S = 0.5                    # server: how often an idle relay looks for work
HEARTBEAT_S = 5                 # server: how often it says it is alive
ALIVE_WITHIN_S = 20             # requester: a relay seen this recently is alive
CLAIM_WAIT_S = 10               # requester: how long a call may sit unclaimed
RESULT_POLL_S = 0.25            # requester: how often it looks for its answer
RETAIN = timedelta(minutes=10)  # server: rows older than this are swept
IN_USE_RECHECK_S = 30           # server: how often an idle install re-checks the switches


class RelayError(Exception):
    """No relay answered. The message is safe to show and names what to fix."""


def identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _session():
    from ..database import SessionLocal
    return SessionLocal()


# ── the requester ────────────────────────────────────────────────────────────

def alive(db, now: Optional[datetime] = None) -> bool:
    from ..database import SpireAdminRelay
    now = now or datetime.utcnow()
    return (db.query(SpireAdminRelay)
            .filter(SpireAdminRelay.seen_at >= now - timedelta(seconds=ALIVE_WITHIN_S))
            .first()) is not None


def call(args: list, timeout: int, input: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run ``spire-server *args`` wherever the server is reachable, and return what it
    printed. Raises RelayError when no relay is alive or none answers in time."""
    from ..database import SpireAdminCall
    from . import config_service
    db = _session()
    try:
        if not alive(db):
            raise RelayError(
                "the SPIRE server relay is not running: this process has no local path to "
                "the dashboard's SPIRE server (no SPIRE_SERVER_SOCKET, no Docker socket), "
                "and no app process with SPIRE_SERVER_SOCKET has checked in. Start the "
                "SPIRE sidecar beside the app (docs/spiffe.md), "
                "or turn off the SPIRE features under Settings → Remote agents")
        row = SpireAdminCall(
            args_json=json.dumps([str(a) for a in args]),
            stdin_enc=config_service.encrypt_value(input) if input is not None else None,
            timeout_s=int(timeout), status="pending", requested_by=identity())
        db.add(row)
        db.commit()
        call_id = row.id
        started = time.monotonic()
        deadline = started + CLAIM_WAIT_S + int(timeout)
        while time.monotonic() < deadline:
            time.sleep(RESULT_POLL_S)
            db.expire_all()
            row = db.get(SpireAdminCall, call_id)
            if row is None:
                raise RelayError("the relayed SPIRE call was removed before it answered")
            if row.status == "done":
                out = subprocess.CompletedProcess(
                    args, int(row.returncode if row.returncode is not None else 1),
                    _decrypt(row.stdout_enc), _decrypt(row.stderr_enc))
                db.delete(row)
                db.commit()
                return out
            if row.status == "pending" and time.monotonic() - started > CLAIM_WAIT_S:
                break
        # Withdraw it if nobody took it, so a late relay does not run a call nobody is
        # waiting for. A claimed one is left for the sweep: it may still be running.
        db.query(SpireAdminCall).filter(SpireAdminCall.id == call_id,
                                        SpireAdminCall.status == "pending").delete()
        db.commit()
        raise RelayError(
            f"spire-server {' '.join(str(a) for a in args[:2])} was relayed but not answered "
            f"within {int(time.monotonic() - started)}s — the app's SPIRE sidecar may be "
            f"restarting")
    finally:
        db.close()


def _decrypt(value: Optional[str]) -> str:
    if not value:
        return ""
    from . import config_service
    return config_service.decrypt_value_strict(value)


# ── the server ───────────────────────────────────────────────────────────────

def _execute(args, timeout: int, stdin: Optional[str]) -> subprocess.CompletedProcess:
    """Run one call through this process's local transport. Never raises: whatever went
    wrong is returned as a failed process, for the requester's own checks to word."""
    from . import dashboard_spire
    if not isinstance(args, list) or not args or not all(isinstance(a, str) for a in args):
        return subprocess.CompletedProcess(args, 2, "", "malformed relayed call")
    if args[0] not in ALLOWED_COMMANDS:
        return subprocess.CompletedProcess(
            args, 2, "", f"spire-server {args[0]} is not a command the relay runs")
    try:
        return dashboard_spire.exec_local(args, timeout, stdin)
    except dashboard_spire.DashboardSpireError as exc:
        return subprocess.CompletedProcess(args, 1, "", str(exc))
    except Exception as exc:  # noqa: BLE001 -- answered, not raised
        return subprocess.CompletedProcess(args, 1, "", f"relay failed: {type(exc).__name__}")


def serve_once(db, me: str) -> int:
    """Claim and answer every pending call. Returns how many this process answered."""
    from ..database import SpireAdminCall
    from . import config_service
    done = 0
    pending = (db.query(SpireAdminCall.id).filter(SpireAdminCall.status == "pending")
               .order_by(SpireAdminCall.created_at).limit(10).all())
    for (call_id,) in pending:
        won = (db.query(SpireAdminCall)
               .filter(SpireAdminCall.id == call_id, SpireAdminCall.status == "pending")
               .update({"status": "running", "served_by": me,
                        "claimed_at": datetime.utcnow()}, synchronize_session=False))
        db.commit()
        if won != 1:
            continue                       # another process took it
        row = db.get(SpireAdminCall, call_id)
        if row is None:
            continue
        try:
            args = json.loads(row.args_json)
            stdin = _decrypt(row.stdin_enc) if row.stdin_enc else None
        except Exception:  # noqa: BLE001 -- a row we cannot read is answered as such
            args, stdin = None, None
        proc = _execute(args, max(1, min(int(row.timeout_s or 30), 300)), stdin)
        row.returncode = int(proc.returncode)
        row.stdout_enc = config_service.encrypt_value(proc.stdout or "")
        row.stderr_enc = config_service.encrypt_value(proc.stderr or "")
        row.status = "done"
        row.finished_at = datetime.utcnow()
        db.commit()
        done += 1
    return done


def heartbeat(db, me: str, transport: str, now: Optional[datetime] = None) -> None:
    from ..database import SpireAdminRelay
    now = now or datetime.utcnow()
    row = db.get(SpireAdminRelay, me)
    if row is None:
        db.add(SpireAdminRelay(id=me, transport=transport, seen_at=now))
    else:
        row.seen_at = now
        row.transport = transport
    db.commit()


def sweep(db, now: Optional[datetime] = None) -> None:
    """Calls nobody collected, and relays that stopped checking in."""
    from ..database import SpireAdminCall, SpireAdminRelay
    now = now or datetime.utcnow()
    db.query(SpireAdminCall).filter(SpireAdminCall.created_at < now - RETAIN).delete()
    db.query(SpireAdminRelay).filter(SpireAdminRelay.seen_at < now - RETAIN).delete()
    db.commit()


def withdraw(db, me: str) -> None:
    from ..database import SpireAdminRelay
    db.query(SpireAdminRelay).filter(SpireAdminRelay.id == me).delete()
    db.commit()


def _wanted(db) -> bool:
    """Serve only while something on this install uses the server, so an install that
    never turned SPIRE on does not poll a table twice a second forever."""
    from . import dashboard_spire
    return dashboard_spire.server_in_use() or dashboard_spire.registered(db) is not None


class RelayServer(threading.Thread):
    """Answers relayed calls for this process. A plain daemon thread, not an asyncio task
    over ``to_thread``: one call can take a minute (``federation refresh``), and that
    minute must not be a slot of the request thread pool."""

    def __init__(self, transport: str):
        super().__init__(name="spire-relay", daemon=True)
        self.transport = transport
        self.me = identity()
        self.stop_event = threading.Event()

    def run(self) -> None:
        last_beat = last_sweep = last_check = 0.0
        serving = False
        while not self.stop_event.is_set():
            now = time.monotonic()
            try:
                db = _session()
                try:
                    if now - last_check >= IN_USE_RECHECK_S:
                        serving = _wanted(db)
                        last_check = now
                    if serving:
                        if now - last_beat >= HEARTBEAT_S:
                            heartbeat(db, self.me, self.transport)
                            last_beat = now
                        serve_once(db, self.me)
                        if now - last_sweep >= 60:
                            sweep(db)
                            last_sweep = now
                finally:
                    db.close()
            except Exception as exc:  # noqa: BLE001 -- a background thread: log, carry on
                logger.warning("SPIRE relay: pass failed: %s", exc)
            self.stop_event.wait(POLL_S if serving else 5)
        try:
            db = _session()
            try:
                withdraw(db, self.me)
            finally:
                db.close()
        except Exception:  # noqa: BLE001 -- best effort on shutdown
            pass

    def stop(self) -> None:
        self.stop_event.set()


def start() -> Optional[RelayServer]:
    """Start serving relayed calls when this process holds a SIDECAR's admin socket.

    Only the socket transport serves. On Docker Compose every container that needs the
    server — app and worker — already reaches it with ``docker exec``, so nothing there
    ever relays, and an install that never left compose never polls this table.
    """
    from . import dashboard_spire
    transport = dashboard_spire.transport()
    if transport != "socket":
        return None
    server = RelayServer(transport)
    server.start()
    logger.info("SPIRE relay: serving relayed calls over %s", transport)
    return server
