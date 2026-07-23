"""Regression: GCE instance launches must qualify a bare subnetwork name.

The sandbox stores ``gcp_subnetwork`` as a bare name (``dashboard-sandbox-vm-subnet``).
GCE's ``networkInterfaces[].subnetwork`` field wants a partial/full self-link, so a
bare name is rejected at insert time with:

    Invalid value for field 'resource.networkInterfaces[0].subnetwork':
    'dashboard-sandbox-vm-subnet'. The URL is malformed.

``_qualify_subnetwork`` / ``_qualify_network`` normalize these to region/global
partial self-links (region derived from the instance zone) before the insert.

Run: python tests/test_gcp_subnetwork_qualify.py   (or under pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from web_dashboard.services.gcp_service import (
        _qualify_subnetwork,
        _qualify_network,
    )
except Exception as exc:  # pragma: no cover — deps absent outside CI
    try:
        import pytest
        pytest.skip(f"gcp_service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

PROJECT = "project-4e93c8e3-4e96-4bc0-9d1"


def test_bare_subnet_is_region_qualified_from_zone():
    # The exact failing case from the reported gce_deploy job.
    out = _qualify_subnetwork("dashboard-sandbox-vm-subnet", PROJECT, "us-east1-b")
    assert out == (
        f"projects/{PROJECT}/regions/us-east1/subnetworks/dashboard-sandbox-vm-subnet"
    ), out


def test_region_derived_per_zone():
    out = _qualify_subnetwork("vm-subnet", PROJECT, "us-central1-a")
    assert out == f"projects/{PROJECT}/regions/us-central1/subnetworks/vm-subnet", out


def test_already_qualified_subnet_passthrough():
    ref = f"projects/{PROJECT}/regions/us-east1/subnetworks/vm-subnet"
    assert _qualify_subnetwork(ref, PROJECT, "us-east1-b") == ref
    # A partial self-link is left untouched too.
    partial = "regions/us-east1/subnetworks/vm-subnet"
    assert _qualify_subnetwork(partial, PROJECT, "us-east1-b") == partial


def test_empty_subnet_passthrough():
    assert _qualify_subnetwork("", PROJECT, "us-east1-b") == ""


def test_bare_network_is_global_qualified():
    assert _qualify_network("dashboard-sandbox-vpc") == "global/networks/dashboard-sandbox-vpc"


def test_already_qualified_network_passthrough():
    ref = f"projects/{PROJECT}/global/networks/dashboard-sandbox-vpc"
    assert _qualify_network(ref) == ref
    assert _qualify_network("") == ""


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"PASS: {t.__name__}")
    print(f"\nAll {len(tests)} tests passed.")
