"""Live Output on the job detail page shows each persisted line exactly once.

The bug: every agent_hypervisor job's opening lines appeared twice. The agent sent each
line once, and job_logs held each line once (one agent.connection_secret audit row per
job, one result line), so it was the page. ``jobs/detail.html`` had BOTH
``x-init="init()"`` and an ``init()`` method, which Alpine 3 already calls by itself, so
``init()`` ran twice and opened two WebSockets. Each socket replays every persisted line
on connect. The client's only dedupe compared a line to the previous one by text, which
catches a back-to-back repeat but not a replayed block: A B C (socket 1), A B C (socket 2),
D (socket 1), with socket 2's D dropped as "same as last". That is exactly the output
reported: the prologue twice, the result once.

What these pin:

- the WebSocket stamps every Live Output message with its JobLog ``seq``;
- the page dedupes by that seq, so two streams (or a reconnect) cannot print a line twice,
  and a line that really repeats is no longer swallowed;
- the page opens one stream, and its root element has no ``x-init="init()"``.

Run: python tests/test_job_live_output_dedupe.py   (or under pytest)
"""
import asyncio
import os
import re
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="live-output-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-live-output-tests")

from web_dashboard.database import Base, SessionLocal, engine  # noqa: E402
from web_dashboard.services import job_service  # noqa: E402
from web_dashboard.api import websocket as ws_api  # noqa: E402

Base.metadata.create_all(bind=engine)

_TEMPLATE = os.path.join(_ROOT, "web_dashboard", "templates", "jobs", "detail.html")

_PROLOGUE = [
    "Starting agent_hypervisor (policy 00b45550e976…)",
    "inventory_sync on weaverpc-workstation (workstation) — granted by policy digest abc",
    "fetching the credential for weaverpc-workstation from the dashboard — "
    "nothing is stored on this host",
    "read 38 VM(s) from Workstation at host.docker.internal",
]


class _FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, data):
        self.sent.append(data)

    async def close(self, code=1000):
        pass

    async def receive_text(self):
        await asyncio.sleep(10)


class _User:
    username = "operator"


def _finished_job(lines):
    db = SessionLocal()
    try:
        job = job_service.create_job(db, "agent_hypervisor", "operator")
        for line in lines:
            job_service.append_job_log(db, job.id, line)
        job.status = "completed"
        db.commit()
        return job.id
    finally:
        db.close()


def _stream(job_id):
    """Run the real WS endpoint against a fake socket and return what it sent."""
    sock = _FakeSocket()
    orig_auth, orig_connect = ws_api._authenticate, ws_api.manager.connect

    async def _connect(*_a, **_k):
        return None

    ws_api._authenticate = lambda _ws, _db: (_User(), None)
    ws_api.manager.connect = _connect
    try:
        asyncio.run(ws_api.job_progress_websocket(sock, job_id))
    finally:
        ws_api._authenticate, ws_api.manager.connect = orig_auth, orig_connect
    return [m for m in sock.sent if m.get("log_line")]


def _render(messages):
    """The page's onMessage log handling, mirrored from jobs/detail.html (pinned below)."""
    logs, last_seq = [], 0
    for data in messages:
        if data.get("log_line") and not (data.get("seq") and data["seq"] <= last_seq):
            if data.get("seq"):
                last_seq = data["seq"]
            logs.append(data["log_line"])
    return logs


def test_ws_messages_carry_the_joblog_seq():
    msgs = _stream(_finished_job(_PROLOGUE))
    assert [m["log_line"] for m in msgs] == _PROLOGUE
    assert [m["seq"] for m in msgs] == [1, 2, 3, 4]


def test_two_streams_render_each_line_once():
    # The reported shape: both sockets replay the prologue, then both tail the result.
    job_id = _finished_job(_PROLOGUE)
    one, two = _stream(job_id), _stream(job_id)
    interleaved = one[:3] + two[:3] + one[3:] + two[3:]
    assert _render(interleaved) == _PROLOGUE


def test_a_line_that_really_repeats_is_kept():
    lines = ["waiting for the guest…", "waiting for the guest…", "ready"]
    assert _render(_stream(_finished_job(lines))) == lines


def _source():
    with open(_TEMPLATE, encoding="utf-8") as fh:
        return fh.read()


def test_root_element_does_not_call_init_a_second_time():
    root = re.search(r"<div[^>]*x-data=[\"']jobDetail\([^>]*>", _source())
    assert root, "jobDetail root element not found"
    assert "x-init" not in root.group(0), root.group(0)


def test_page_opens_one_stream_and_dedupes_by_seq():
    src = _source()
    body = src[src.index("connectWs() {"):src.index("this.tracker.connect();")]
    assert re.search(r"if \(this\.tracker\) return;", body), "connectWs is not idempotent"
    assert "data.seq <= this._lastLogSeq" in body, "Live Output is not deduped by seq"
    assert "_lastLogLine" not in src, "the text-equality dedupe is back"


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
