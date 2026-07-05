"""Unit tests for services/ephemeral_secrets.py (naming + GC expiry — pure).

Loaded by file path (stdlib only). Runs under pytest, or standalone:
    python tests/test_ephemeral_secrets.py
"""
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "ephemeral_secrets.py")
_spec = importlib.util.spec_from_file_location("ephemeral_secrets", _PATH)
eph = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eph)


def test_aws_secret_name_deterministic_and_prefixed():
    n = eph.aws_secret_name("abc-123", 0)
    assert n == f"{eph.NAME_PREFIX}-abc-123-0"


def test_gcp_secret_id_sanitises_and_prefixes():
    # GCP ids allow only letters/digits/hyphen — a job id with other chars is scrubbed
    sid = eph.gcp_secret_id("job:9/x", 2)
    assert sid == f"{eph.GCP_ID_PREFIX}-job-9-x-2"
    assert all(c.isalnum() or c == "-" for c in sid)


def test_expired_returns_ids_older_than_ttl():
    now = 1_000_000.0
    items = [
        {"id": "old", "created_ts": now - 40 * 60},    # 40 min → expired (ttl 30)
        {"id": "fresh", "created_ts": now - 5 * 60},    # 5 min → keep
        {"id": "edge", "created_ts": now - 30 * 60},    # exactly ttl → expired (<=)
    ]
    assert set(eph.expired(items, 30, now)) == {"old", "edge"}


def test_expired_treats_missing_created_ts_as_expired():
    now = 1_000_000.0
    items = [{"id": "unknown-age"}, {"id": "zero", "created_ts": 0}]
    assert set(eph.expired(items, 30, now)) == {"unknown-age", "zero"}


def test_expired_disabled_when_ttl_non_positive():
    now = 1_000_000.0
    items = [{"id": "old", "created_ts": 0}]
    assert eph.expired(items, 0, now) == []
    assert eph.expired(items, -5, now) == []


def test_expired_empty():
    assert eph.expired([], 30, 1.0) == []
    assert eph.expired(None, 30, 1.0) == []


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
