"""
WebSocket endpoint for real-time job progress updates.
Clients connect to /api/ws/jobs/{job_id} and receive JSON messages
as the job progresses.
"""
import asyncio
import json
from datetime import datetime
from typing import Dict, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..services import job_service

router = APIRouter(tags=["websocket"])

# In-memory registry: job_id -> set of connected WebSocket clients
_connections: Dict[str, Set[WebSocket]] = {}


class ConnectionManager:
    async def connect(self, job_id: str, ws: WebSocket):
        await ws.accept()
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


async def broadcast_progress(job_id: str, pct: int, message: str, log_line: str = None):
    """
    Push progress to connected clients AND persist it. The dedicated job runner is a
    separate process, so its in-memory ``manager.broadcast`` reaches no clients — the
    DB writes (progress + per-line ``JobLog``) are what the WS endpoint reads on its
    2s poll. The in-memory broadcast still serves any in-process callers.
    """
    db: Session = SessionLocal()
    try:
        job_service.update_progress(db, job_id, pct, message)
        if log_line:
            job_service.append_job_log(db, job_id, log_line)
    finally:
        db.close()

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
    """
    await manager.connect(job_id, websocket)
    db: Session = SessionLocal()
    last_seq = 0

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
