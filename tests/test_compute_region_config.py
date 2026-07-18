"""Multi-region support (Phase 2c): compute paths consume per-region config.

Phase 2b generalized region_config.resolve_region(cloud, region); Phase 2c wires
the compute consumers to it (AWS/GCP deploy runners, network-options defaults,
GCP jumpoint subnet/network). The resolver itself is covered by
test_region_config.py; here we pin the two cleanly-testable wiring points that
transform the resolved value:

  * aws_service._get_network_options_sync feeds the deploy-form default subnet/SG
    from resolve_region('aws', region) — so picking a non-default region
    pre-selects that region's subnet/SG;
  * jumpoint_host_service._gcp_jumpoint_subnetwork builds the regional subnet
    self-link from resolve_region('gcp', region-of-zone)['jumpoint_subnetwork'].

resolve_region is replaced with a spy so these test the wiring, not the resolver.

Run: python tests/test_compute_region_config.py   (or under pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from web_dashboard.services import aws_service, jumpoint_host_service
    import web_dashboard.services.region_config as region_config
except Exception as exc:  # pragma: no cover — deps absent outside CI
    try:
        import pytest
        pytest.skip(f"compute service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


# ── AWS network-options defaults resolve per region ───────────────────────────

class _FakeEc2:
    def describe_subnets(self, *a, **k):
        return {"Subnets": []}

    def describe_security_groups(self, *a, **k):
        return {"SecurityGroups": []}


def test_aws_network_options_defaults_from_region_config():
    calls = []

    def _spy(cloud, region):
        calls.append((cloud, region))
        return {"default_subnet_id": "subnet-region", "default_security_group_id": "sg-region"}

    saved_ec2 = aws_service._get_ec2
    saved_resolve = region_config.resolve_region
    aws_service._get_ec2 = lambda region: _FakeEc2()
    region_config.resolve_region = _spy
    try:
        opts = aws_service._get_network_options_sync("us-west-2")
    finally:
        aws_service._get_ec2 = saved_ec2
        region_config.resolve_region = saved_resolve

    # The deploy-form defaults come from the region's config set, resolved for the
    # region the form is scoped to.
    assert opts["default_subnet_id"] == "subnet-region"
    assert opts["default_security_group_id"] == "sg-region"
    assert calls == [("aws", "us-west-2")]


# ── GCP jumpoint subnet self-link from region config ──────────────────────────

def test_gcp_jumpoint_subnetwork_builds_regional_self_link():
    saved = region_config.resolve_region
    region_config.resolve_region = lambda cloud, region: {"jumpoint_subnetwork": "jump-subnet"}
    try:
        link = jumpoint_host_service._gcp_jumpoint_subnetwork("proj-1", "us-central1-a")
    finally:
        region_config.resolve_region = saved
    # Bare name → expanded to a regional self-link using the zone's region.
    assert link == "projects/proj-1/regions/us-central1/subnetworks/jump-subnet"


def test_gcp_jumpoint_subnetwork_passes_through_self_link():
    saved = region_config.resolve_region
    full = "projects/p/regions/europe-west1/subnetworks/s"
    region_config.resolve_region = lambda cloud, region: {"jumpoint_subnetwork": full}
    try:
        link = jumpoint_host_service._gcp_jumpoint_subnetwork("proj-1", "europe-west1-b")
    finally:
        region_config.resolve_region = saved
    assert link == full  # already a self-link → unchanged


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
