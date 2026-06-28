"""Unit tests for cloud_stats.summarize_instances — the pure count/RBAC helper
behind the per-cloud dashboard-stats endpoints. No deps; pure dicts.
Runs under pytest, or standalone:  python tests/test_cloud_stats.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from web_dashboard.services.cloud_stats import summarize_instances

_AWS = [
    {"workgroup": "hydra", "state": "running"},
    {"workgroup": "hydra", "state": "stopped"},
    {"workgroup": "weaverlab", "state": "running"},
]
_GCP = [
    {"workgroup": "hydra", "status": "RUNNING"},
    {"workgroup": "hydra", "status": "TERMINATED"},
]


def test_admin_sees_all():
    assert summarize_instances(_AWS, None, "state") == {"total": 3, "running": 2}


def test_non_admin_filtered_by_workgroup():
    assert summarize_instances(_AWS, ["hydra"], "state") == {"total": 2, "running": 1}
    assert summarize_instances(_AWS, ["weaverlab"], "state") == {"total": 1, "running": 1}
    assert summarize_instances(_AWS, [], "state") == {"total": 0, "running": 0}


def test_running_value_is_case_insensitive_gcp():
    # GCP reports status=RUNNING (uppercase); default running_value="running".
    assert summarize_instances(_GCP, None, "status") == {"total": 2, "running": 1}


def test_missing_field_and_empty_rows_are_safe():
    assert summarize_instances([], None, "state") == {"total": 0, "running": 0}
    assert summarize_instances(None, None, "state") == {"total": 0, "running": 0}
    # rows lacking the running field count as not-running, not an error
    assert summarize_instances([{"workgroup": "x"}], None, "state") == {"total": 1, "running": 0}


def test_rows_without_workgroup_are_owner_invisible_to_non_admin():
    rows = [{"state": "running"}]  # no workgroup key
    assert summarize_instances(rows, ["hydra"], "state") == {"total": 0, "running": 0}
    assert summarize_instances(rows, None, "state") == {"total": 1, "running": 1}


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
