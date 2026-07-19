"""Unit tests for cloud_stats.summarize_instances — the pure count/RBAC helper
behind the per-cloud dashboard-stats endpoints. No deps; pure dicts.
Runs under pytest, or standalone:  python tests/test_cloud_stats.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from web_dashboard.services.cloud_stats import summarize_by_region, summarize_instances

_AWS = [
    {"workgroup": "hydra", "state": "running"},
    {"workgroup": "hydra", "state": "stopped"},
    {"workgroup": "weaverlab", "state": "running"},
]
_GCP = [
    {"workgroup": "hydra", "status": "RUNNING"},
    {"workgroup": "hydra", "status": "TERMINATED"},
]

# Multi-region fixtures: same rows as _AWS/_GCP but region-tagged.
_AWS_REGIONS = [
    {"workgroup": "hydra", "state": "running", "region": "us-east-2"},
    {"workgroup": "hydra", "state": "stopped", "region": "us-east-2"},
    {"workgroup": "weaverlab", "state": "running", "region": "us-west-2"},
]
# Azure keys region as "location", not "region".
_AZURE_REGIONS = [
    {"workgroup": "hydra", "state": "running", "location": "centralus"},
    {"workgroup": "hydra", "state": "running", "location": "westus2"},
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


def test_by_region_groups_total_and_running():
    assert summarize_by_region(_AWS_REGIONS, None, "state", "region") == {
        "us-east-2": {"total": 2, "running": 1},
        "us-west-2": {"total": 1, "running": 1},
    }


def test_by_region_respects_workgroup_visibility():
    # weaverlab's us-west-2 row drops out entirely, taking its region with it.
    assert summarize_by_region(_AWS_REGIONS, ["hydra"], "state", "region") == {
        "us-east-2": {"total": 2, "running": 1},
    }
    assert summarize_by_region(_AWS_REGIONS, [], "state", "region") == {}


def test_by_region_uses_location_for_azure():
    assert summarize_by_region(_AZURE_REGIONS, None, "state", "location") == {
        "centralus": {"total": 1, "running": 1},
        "westus2": {"total": 1, "running": 1},
    }


def test_by_region_running_value_is_case_insensitive():
    rows = [{"workgroup": "hydra", "status": "RUNNING", "region": "us-central1"}]
    assert summarize_by_region(rows, None, "status", "region") == {
        "us-central1": {"total": 1, "running": 1},
    }


def test_by_region_blank_region_buckets_as_unknown():
    rows = [
        {"workgroup": "hydra", "state": "running"},           # region absent
        {"workgroup": "hydra", "state": "stopped", "region": ""},  # region blank
    ]
    assert summarize_by_region(rows, None, "state", "region") == {
        "unknown": {"total": 2, "running": 1},
    }


def test_by_region_empty_rows_are_safe():
    assert summarize_by_region([], None, "state", "region") == {}
    assert summarize_by_region(None, None, "state", "region") == {}


def test_by_region_totals_agree_with_summarize_instances():
    # The breakdown must never disagree with the headline tile.
    flat = summarize_instances(_AWS_REGIONS, ["hydra"], "state")
    grouped = summarize_by_region(_AWS_REGIONS, ["hydra"], "state", "region")
    assert sum(v["total"] for v in grouped.values()) == flat["total"]
    assert sum(v["running"] for v in grouped.values()) == flat["running"]


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
