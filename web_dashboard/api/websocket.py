"""
WebSocket endpoint for real-time job progress updates.
Clients connect to /api/ws/jobs/{job_id} and receive JSON messages
as the job progresses.
"""
import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, Optional, Set, Tuple

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from ..database import SessionLocal, User
from ..services import job_service
from .auth import _PAT_PREFIX, _get_user_from_pat, can_audit_jobs, decode_token

router = APIRouter(tags=["websocket"])
logger = logging.getLogger(__name__)

# Browsers cannot set an Authorization header on a WebSocket, and a token in the
# query string lands in proxy and access logs. The subprotocol header is the one
# client-settable channel that does neither, so the client offers
# `Sec-WebSocket-Protocol: vmdash.bearer, <token>`. The server MUST echo the
# selected subprotocol back on accept or the browser aborts the handshake.
_WS_AUTH_SUBPROTOCOL = "vmdash.bearer"

# In-memory registry: job_id -> set of connected WebSocket clients
_connections: Dict[str, Set[WebSocket]] = {}


def _authenticate(websocket: WebSocket, db: Session) -> Tuple[Optional[User], Optional[str]]:
    """Resolve the offered subprotocol credential to a User.

    Returns ``(user, subprotocol_to_echo)``; ``(None, None)`` when the credential is
    missing or invalid. Delegates to the same JWT/PAT resolution the HTTP dependency
    uses, so a revoked PAT or an expired JWT is rejected here for the same reasons and
    at the same moment it would be on a REST call.
    """
    offered = [p.strip() for p in
               (websocket.headers.get("sec-websocket-protocol") or "").split(",") if p.strip()]
    if len(offered) < 2 or offered[0] != _WS_AUTH_SUBPROTOCOL:
        return None, None
    token = offered[1]

    try:
        if token.startswith(_PAT_PREFIX):
            return _get_user_from_pat(token, db), _WS_AUTH_SUBPROTOCOL
        username = decode_token(token).username
        user = db.query(User).filter(User.username == username).first()
        if not user or not user.is_active:
            return None, None
        return user, _WS_AUTH_SUBPROTOCOL
    except HTTPException:
        return None, None
    except Exception:  # noqa: BLE001 — a malformed token is a failed auth, not a 500
        logger.warning("websocket auth: rejecting a malformed credential")
        return None, None


class ConnectionManager:
    async def connect(self, job_id: str, ws: WebSocket, subprotocol: Optional[str] = None):
        await ws.accept(subprotocol=subprotocol)
        _connections.setdefault(job_id, set()).add(ws)

    def disconnect(self, job_id: str, ws: WebSocket):
        if job_id in _connections:
            _connections[job_id].discard(ws)
            if not _connections[job_id]:
                del _connections[job_id]

    async def broadcast(self, job_id: str, data: dict):
        """Send a message to all clients watching this job."""
        dead = set()
        for ws in list(_connections.get(job_id, set())):
            try:
                await ws.send_json(data)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self.disconnect(job_id, ws)


manager = ConnectionManager()


def _persist_progress(job_id: str, pct: int, message: str, log_line: str = None) -> None:
    """The synchronous half of :func:`broadcast_progress`. Split out so it can be handed
    to a thread — see there for why that matters."""
    db: Session = SessionLocal()
    try:
        job_service.update_progress(db, job_id, pct, message)
        if log_line:
            job_service.append_job_log(db, job_id, log_line)
    finally:
        db.close()


async def broadcast_progress(job_id: str, pct: int, message: str, log_line: str = None):
    """
    Push progress to connected clients AND persist it. The dedicated job runner is a
    separate process, so its in-memory ``manager.broadcast`` reaches no clients — the
    DB writes (progress + per-line ``JobLog``) are what the WS endpoint reads on its
    2s poll. The in-memory broadcast still serves any in-process callers.

    The DB half runs in a thread. This is called from ``terraform._stream``'s ``on_line``
    callback — which is awaited directly on the event loop, once PER OUTPUT LINE of a
    terraform apply — and each call is a session open, an UPDATE, a ``SELECT max(seq)``,
    an INSERT and two commits. At one job per worker that was merely wasteful; now that
    ``jobs_worker`` runs several jobs concurrently it is shared infrastructure, and doing
    it synchronously would make one job's streamed output delay every other job's progress
    writes, cancel checks and ``asyncio.sleep`` timers — including the heartbeats whose
    lateness makes a sibling worker's reconcile fail a live job.

    ``append_job_log``'s ``SELECT max(seq) + 1`` stays safe: ``_stream`` awaits ``on_line``
    sequentially, so there is still exactly one writer per ``job_id`` and the sequence
    cannot interleave. (``uq_job_log_seq`` is the backstop, not the mechanism.)
    """
    await asyncio.to_thread(_persist_progress, job_id, pct, message, log_line)

    payload = {
        "job_id": job_id,
        "type": "progress",
        "progress_pct": pct,
        "progress_message": message,
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if log_line:
        payload["log_line"] = log_line

    await manager.broadcast(job_id, payload)


@router.websocket("/api/ws/jobs/{job_id}")
async def job_progress_websocket(websocket: WebSocket, job_id: str):
    """
    WebSocket endpoint for real-time job progress.

    The client connects here immediately after receiving a job_id.
    The server:
      1. Sends the current job state immediately on connect.
      2. Replays any persisted Live Output lines (so opening or RECONNECTING to an
         in-flight or already-finished job shows the full stream).
      3. Polls the DB every 2s, pushing state changes and tailing new log lines.
      4. Closes once the job reaches a terminal state.

    Progress + logs are written to the DB by whoever runs the job (now the dedicated
    job runner, a separate process), so this endpoint is driven entirely by the DB.

    Authenticated and authorized before the handshake completes: Live Output carries
    whatever a playbook or terraform run printed, so an unauthenticated reader here
    would be a credential-disclosure channel that needs nothing but a job id.
    """
    db: Session = SessionLocal()
    last_seq = 0

    user, subprotocol = _authenticate(websocket, db)
    if user is None:
        db.close()
        await websocket.close(code=1008)  # policy violation
        return

    # Same ownership rule as GET /api/jobs/{id}. Checked before accept so a caller
    # cannot distinguish "not yours" from "does not exist" — both are one close frame.
    job = job_service.get_job(db, job_id)
    if not job or (job.created_by != user.username and not can_audit_jobs(user)):
        db.close()
        await websocket.close(code=1008)
        return

    await manager.connect(job_id, websocket, subprotocol=subprotocol)

    def _log_msg(line: str, job) -> dict:
        return {
            "job_id": job_id,
            "type": "progress",
            "progress_pct": job.progress_pct,
            "progress_message": job.progress_message,
            "log_line": line,
            "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    try:
        # Send current job state to the newly connected client
        job = job_service.get_job(db, job_id)
        if not job:
            await websocket.send_json({"error": "Job not found", "job_id": job_id})
            await websocket.close()
            return

        await websocket.send_json({
            "job_id": job_id,
            "type": "state",
            "status": job.status,
            "progress_pct": job.progress_pct,
            "progress_message": job.progress_message,
            "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

        # Replay persisted Live Output BEFORE any terminal close, so a client that
        # opens a finished job (or reconnects mid-job) still sees the full output.
        for seq, line in job_service.get_job_logs(db, job_id, after_seq=last_seq):
            await websocket.send_json(_log_msg(line, job))
            last_seq = seq

        # If already terminal, close immediately
        if job.status in ("completed", "failed", "cancelled"):
            await websocket.close()
            return

        # Poll the DB every 2 seconds and push updates until terminal
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            except WebSocketDisconnect:
                break

            db.expire_all()
            job = job_service.get_job(db, job_id)
            if not job:
                break

            await websocket.send_json({
                "job_id": job_id,
                "type": "state",
                "status": job.status,
                "progress_pct": job.progress_pct,
                "progress_message": job.progress_message,
                "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            })

            # Tail any new Live Output lines persisted since the last poll.
            for seq, line in job_service.get_job_logs(db, job_id, after_seq=last_seq):
                await websocket.send_json(_log_msg(line, job))
                last_seq = seq

            if job.status in ("completed", "failed", "cancelled"):
                break

    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(job_id, websocket)
        db.close()
