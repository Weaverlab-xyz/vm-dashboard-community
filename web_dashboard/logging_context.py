"""Log correlation: a contextvar threaded into every log line as ``%(cid)s``.

A single correlation id ties together the log lines of one unit of work:

* the HTTP app sets it to a per-request id (``main.py`` middleware), echoed back
  as the ``X-Request-ID`` response header;
* the job runner sets it to the job id while a job is dispatched
  (``jobs_worker.py``), so every service call the job fans out to — and the
  WebSocket progress writes it triggers — carries the same id.

:func:`install_log_correlation` installs a ``LogRecord`` factory that copies the
current value onto **every** record as ``record.cid`` (``"-"`` when unset), so any
handler's formatter can render ``%(cid)s`` without per-call logger plumbing. Call
it once, before ``logging.basicConfig``, and use :data:`LOG_FORMAT`.
"""
import contextvars
import logging
import uuid

# The current unit-of-work id. Empty string = no active correlation.
_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="")

# Format string both entrypoints pass to logging.basicConfig — identical to the
# previous format plus the [cid] field.
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s [%(cid)s]: %(message)s"

_installed = False


def get_correlation_id() -> str:
    return _correlation_id.get()


def set_correlation_id(value: str) -> "contextvars.Token[str]":
    """Set the current correlation id; returns a token for :func:`reset_correlation_id`."""
    return _correlation_id.set(value or "")


def reset_correlation_id(token: "contextvars.Token[str]") -> None:
    _correlation_id.reset(token)


def new_request_id() -> str:
    """A short, log-friendly id for a request with no inbound X-Request-ID."""
    return uuid.uuid4().hex[:12]


class _Correlation:
    """Context manager: set the correlation id for the duration of a block."""

    def __init__(self, value: str):
        self._value = value
        self._token: "contextvars.Token[str] | None" = None

    def __enter__(self) -> str:
        self._token = _correlation_id.set(self._value or "")
        return self._value

    def __exit__(self, *exc) -> bool:
        if self._token is not None:
            _correlation_id.reset(self._token)
        return False


def correlation(value: str) -> _Correlation:
    """``with correlation(job_id): ...`` — scope a correlation id to a block."""
    return _Correlation(value)


def install_log_correlation() -> None:
    """Make every ``LogRecord`` carry ``cid`` (the current correlation id, or ``"-"``).

    Idempotent. Must run before any handler uses a ``%(cid)s`` format so records
    always have the attribute (a missing field would raise at format time).
    """
    global _installed
    if _installed:
        return
    _old_factory = logging.getLogRecordFactory()

    def _factory(*args, **kwargs):
        record = _old_factory(*args, **kwargs)
        record.cid = _correlation_id.get() or "-"
        return record

    logging.setLogRecordFactory(_factory)
    _installed = True
