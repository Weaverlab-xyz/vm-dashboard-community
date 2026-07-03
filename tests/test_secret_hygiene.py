"""Unit tests for services/secret_hygiene.py (secret age / staleness).

`score` and `summarize` are pure — the caller resolves each secret's changed_at
(vault metadata for external refs, AppConfig.updated_at otherwise) and hands
`summarize` a list of items. So the test needs only stdlib.
Runs under pytest, or standalone:  python tests/test_secret_hygiene.py
"""
import importlib.util
import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "secret_hygiene.py")

# Load by file path — the module is pure (datetime/typing only), so no package
# import chain is needed.
_spec = importlib.util.spec_from_file_location("secret_hygiene", _PATH)
sh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sh)

NOW = datetime(2026, 7, 1, 12, 0, 0)


# ── score ───────────────────────────────────────────────────────────────────

def test_score_unknown_changed_at_not_stale():
    assert sh.score(None, 90, now=NOW) == {"age_days": None, "stale": False}


def test_score_fresh_not_stale():
    r = sh.score(NOW - timedelta(days=3), 90, now=NOW)
    assert r["age_days"] == 3 and r["stale"] is False


def test_score_old_is_stale():
    r = sh.score(NOW - timedelta(days=120), 90, now=NOW)
    assert r["age_days"] == 120 and r["stale"] is True


def test_score_boundary():
    assert sh.score(NOW - timedelta(days=90), 90, now=NOW)["stale"] is True
    assert sh.score(NOW - timedelta(days=89), 90, now=NOW)["stale"] is False


def test_score_disabled_when_max_age_zero_or_negative():
    assert sh.score(NOW - timedelta(days=999), 0, now=NOW)["stale"] is False
    assert sh.score(NOW - timedelta(days=999), -1, now=NOW)["stale"] is False


# ── summarize ────────────────────────────────────────────────────────────────

def _items():
    return [
        {"key": "bt_client_secret", "source": "bt_safe",
         "changed_at": NOW - timedelta(days=5)},          # vault says fresh
        {"key": "aws_secret_access_key", "source": "database",
         "changed_at": NOW - timedelta(days=200)},        # DB, stale
        {"key": "vsphere_password", "source": "database",
         "changed_at": None},                             # unknown age
    ]


def test_summarize_flags_stale_and_sorts_oldest_first():
    rep = sh.summarize(_items(), 90, now=NOW)
    assert rep["enabled"] is True and rep["max_age_days"] == 90
    assert rep["stale_count"] == 1 and rep["stale_keys"] == ["aws_secret_access_key"]
    # oldest first; unknown-age last
    assert [i["key"] for i in rep["items"]] == [
        "aws_secret_access_key", "bt_client_secret", "vsphere_password"]
    assert rep["items"][-1]["age_days"] is None


def test_summarize_preserves_source_and_serializes_changed_at():
    rep = sh.summarize(_items(), 90, now=NOW)
    bt = next(i for i in rep["items"] if i["key"] == "bt_client_secret")
    assert bt["source"] == "bt_safe"            # vault-sourced age retained
    assert bt["changed_at"].startswith("2026-06-26")  # ISO string
    assert bt["stale"] is False                 # rotated 5 days ago → fresh


def test_summarize_disabled_reports_ages_but_no_stale():
    rep = sh.summarize(_items(), 0, now=NOW)
    assert rep["enabled"] is False
    assert rep["stale_count"] == 0 and rep["stale_keys"] == []
    aws = next(i for i in rep["items"] if i["key"] == "aws_secret_access_key")
    assert aws["age_days"] == 200 and aws["stale"] is False


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
