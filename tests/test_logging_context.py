"""Unit tests for the log-correlation plumbing (web_dashboard.logging_context).

Verifies the LogRecord factory stamps the current correlation id onto every
record (as `cid`, `"-"` when unset), that the contextvar set/reset + `correlation`
context manager behave, and that LOG_FORMAT actually renders the id. Pure stdlib
(logging + contextvars) — no app deps. Runs under pytest, or standalone:
    python tests/test_logging_context.py
"""
import logging
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from web_dashboard import logging_context as lc

lc.install_log_correlation()
_LOGGER = logging.getLogger("test.logging_context")


def _emit(msg: str) -> str:
    """Format one record through LOG_FORMAT and return the rendered line."""
    record = _LOGGER.makeRecord("test", logging.INFO, __file__, 0, msg, (), None)
    return logging.Formatter(lc.LOG_FORMAT).format(record)


def test_default_cid_is_dash():
    record = _LOGGER.makeRecord("test", logging.INFO, __file__, 0, "x", (), None)
    assert record.cid == "-"
    assert "[-]" in _emit("hello")


def test_set_and_reset_correlation_id():
    token = lc.set_correlation_id("req-abc")
    try:
        assert lc.get_correlation_id() == "req-abc"
        assert "[req-abc]" in _emit("during")
    finally:
        lc.reset_correlation_id(token)
    assert lc.get_correlation_id() == ""
    assert "[-]" in _emit("after")


def test_correlation_context_manager_scopes_and_restores():
    assert lc.get_correlation_id() == ""
    with lc.correlation("job-123") as value:
        assert value == "job-123"
        assert lc.get_correlation_id() == "job-123"
        assert "[job-123]" in _emit("inside")
    assert lc.get_correlation_id() == ""  # restored on exit


def test_correlation_restores_even_on_exception():
    try:
        with lc.correlation("job-err"):
            raise ValueError("boom")
    except ValueError:
        pass
    assert lc.get_correlation_id() == ""


def test_nested_correlation_restores_outer():
    with lc.correlation("outer"):
        with lc.correlation("inner"):
            assert lc.get_correlation_id() == "inner"
        assert lc.get_correlation_id() == "outer"  # inner reset, outer intact


def test_new_request_id_is_short_and_unique():
    a, b = lc.new_request_id(), lc.new_request_id()
    assert a != b
    assert a.isalnum() and 0 < len(a) <= 12


def test_install_is_idempotent():
    # A second install must not double-wrap or change behaviour.
    lc.install_log_correlation()
    with lc.correlation("again"):
        assert "[again]" in _emit("x")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
