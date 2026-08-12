"""On-demand Cloud NAT egress for sandbox GCE VMs (services/gcp_nat_service.py).

The sandbox denies VM internet through TWO independent gates (setup-gcp.sh): the
vm-subnet is left off the shared Cloud NAT, and a priority-1000 EGRESS DENY targets the
VM network tag. The dashboard opens both on the first VM deploy and closes them when the
last VM in that region is destroyed.

What is actually dangerous here, and therefore what these cover:

  * ``routers.patch`` replaces the repeated ``nats`` field wholesale. Re-sending only
    our gateway would DELETE the sandbox's jumpoint + k8s NAT — cutting the Jumpoint's
    egress, which is the SSH path to every VM in the sandbox.
  * The ref count is region-scoped. The GCP sandbox is multi-region with one Cloud
    Router per region, so a VM in us-central1 must not pin the us-east1 NAT — that is
    the standing cost the on-demand design exists to avoid.
  * The egress ALLOW must outrank the sandbox's deny (lower number wins in GCP).
    A NAT gateway with the deny still winning buys exactly nothing.

Run: python tests/test_gcp_vm_nat.py   (or under pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _skip(exc):  # pragma: no cover — deps absent outside CI
    try:
        import pytest
        pytest.skip(f"import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


try:
    from web_dashboard.services import gcp_nat_service
    from web_dashboard.services.gcp_service import nats_excluding
    from web_dashboard.services import region_config
    from web_dashboard.config import Settings
except Exception as exc:  # pragma: no cover
    _skip(exc)


class _Nat:
    """Stand-in for compute_v1.RouterNat — only ``.name`` matters to the merge."""
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"_Nat({self.name!r})"


class _Job:
    def __init__(self, meta):
        self.metadata_dict = meta


class _Query:
    def __init__(self, jobs):
        self._jobs = jobs

    def filter(self, *_a, **_k):
        return self

    def all(self):
        return self._jobs


class _DB:
    def __init__(self, jobs):
        self._jobs = jobs

    def query(self, *_a, **_k):
        return _Query(self._jobs)


# ── The read-modify-write that must not eat the sandbox's own NAT ─────────────

SANDBOX_NATS = [_Nat("dashboard-sandbox-nat"), _Nat("some-other-nat")]


def test_existing_gateways_are_preserved_when_adding_ours():
    # The list re-sent on patch must still carry every pre-existing gateway.
    kept = nats_excluding(SANDBOX_NATS, "dashboard-sandbox-vm-nat")
    assert [n.name for n in kept] == ["dashboard-sandbox-nat", "some-other-nat"], kept


def test_delete_removes_only_our_gateway():
    nats = SANDBOX_NATS + [_Nat("dashboard-sandbox-vm-nat")]
    kept = nats_excluding(nats, "dashboard-sandbox-vm-nat")
    assert [n.name for n in kept] == ["dashboard-sandbox-nat", "some-other-nat"], kept
    # …and the length change is what both callers use to detect present/absent.
    assert len(kept) != len(nats)


def test_absent_gateway_is_detected_by_unchanged_length():
    nats = list(SANDBOX_NATS)
    assert len(nats_excluding(nats, "dashboard-sandbox-vm-nat")) == len(nats)


def test_empty_router_is_handled():
    assert nats_excluding([], "dashboard-sandbox-vm-nat") == []


# ── Ref counting ──────────────────────────────────────────────────────────────

def test_active_count_is_region_scoped():
    db = _DB([
        _Job({"zone": "us-east1-c"}),
        _Job({"zone": "us-east1-b"}),
        _Job({"zone": "us-central1-a"}),
    ])
    assert gcp_nat_service._active_gce_count(db, "us-east1") == 2
    assert gcp_nat_service._active_gce_count(db, "us-central1") == 1


def test_destroyed_rows_do_not_hold_a_reference():
    db = _DB([
        _Job({"zone": "us-east1-c", "destroyed": True}),
        _Job({"zone": "us-east1-c"}),
    ])
    assert gcp_nat_service._active_gce_count(db, "us-east1") == 1


def test_all_destroyed_reaches_zero_so_the_nat_is_reclaimed():
    db = _DB([
        _Job({"zone": "us-east1-c", "destroyed": True}),
        _Job({"zone": "us-east1-b", "destroyed": True}),
    ])
    assert gcp_nat_service._active_gce_count(db, "us-east1") == 0


def test_a_vm_in_another_region_never_pins_this_regions_nat():
    # The standing-cost regression: one long-lived us-central1 VM must not keep
    # us-east1's on-demand gateway (and its billing) alive forever.
    db = _DB([_Job({"zone": "us-central1-a"})])
    assert gcp_nat_service._active_gce_count(db, "us-east1") == 0


def test_rows_without_a_zone_count_for_every_region():
    # No zone → no way to place the row, so it holds a reference everywhere rather
    # than being attributed to whichever region is configured as the default. Must not
    # touch the app DB: this runs inside a per-row loop.
    db = _DB([_Job({}), _Job({"zone": ""}), _Job({"zone": "us-east1-c"})])
    assert gcp_nat_service._active_gce_count(db, "us-east1") == 3
    assert gcp_nat_service._active_gce_count(db, "europe-west4") == 2


def test_a_region_name_is_not_confused_with_a_zone_prefix():
    # us-east1 must not match us-east10-a or a us-east1 row against us-east
    db = _DB([_Job({"zone": "us-east10-a"}), _Job({"zone": "us-east1-b"})])
    assert gcp_nat_service._active_gce_count(db, "us-east1") == 1
    assert gcp_nat_service._active_gce_count(db, "us-east10") == 1


# ── Precedence + naming ───────────────────────────────────────────────────────

def _names_with(overrides):
    """``_names()`` with config_service stubbed out — the real one reads the app DB."""
    orig = gcp_nat_service._cfg
    gcp_nat_service._cfg = lambda k: overrides.get(k, "")
    try:
        return gcp_nat_service._names()
    finally:
        gcp_nat_service._cfg = orig


def test_default_egress_priority_outranks_the_sandbox_deny():
    # setup-gcp.sh: deny-vm-egress = 1000, allow-vm-egress-vpc = 999,
    # allow-db-from-jumpoint = 998. Lower number wins in GCP.
    nat, rule, priority = _names_with({})
    assert priority < 998, priority
    assert priority == 900, priority
    assert nat == "dashboard-sandbox-vm-nat", nat
    assert rule == "dashboard-sandbox-vm-egress-ondemand", rule


def test_names_and_priority_are_overridable():
    nat, rule, priority = _names_with({
        "gcp_vm_nat_name": "custom-nat",
        "gcp_vm_egress_rule_name": "custom-rule",
        "gcp_vm_egress_rule_priority": "500",
    })
    assert (nat, rule, priority) == ("custom-nat", "custom-rule", 500)


def test_config_defaults_exist_so_get_bool_is_not_permanently_false():
    # config_service.get_bool falls back to getattr(settings, key) — a flag with no
    # field on Settings can never be True regardless of what the DB holds.
    s = Settings()
    assert s.gcp_vm_nat_enabled is True
    assert s.gcp_vm_nat_name == "dashboard-sandbox-vm-nat"
    assert s.gcp_vm_egress_rule_name == "dashboard-sandbox-vm-egress-ondemand"
    assert s.gcp_vm_egress_rule_priority == 900


def test_non_integer_priority_falls_back_instead_of_raising():
    _, _, priority = _names_with({"gcp_vm_egress_rule_priority": "not-a-number"})
    assert priority == 900, priority


# ── _resolve no-ops (non-sandbox projects must be untouched) ──────────────────

def _resolve_with(rc, project="proj-1"):
    orig_cfg = gcp_nat_service._cfg
    orig_resolve = region_config.resolve_region
    gcp_nat_service._cfg = lambda k: project if k == "gcp_project_id" else ""
    region_config.resolve_region = lambda cloud, region: rc
    try:
        return gcp_nat_service._resolve("us-east1")
    finally:
        gcp_nat_service._cfg = orig_cfg
        region_config.resolve_region = orig_resolve


def test_resolve_noops_without_a_cloud_router():
    assert _resolve_with({"router_name": "", "subnetwork": "vm-subnet",
                          "default_network_tag": "t", "network": "vpc"}) is None


def test_resolve_noops_without_a_network_tag():
    # No tag → nothing to scope the ALLOW to, so the deny would still win. Opening
    # the NAT alone would bill for egress that stays firewalled off.
    assert _resolve_with({"router_name": "r", "subnetwork": "vm-subnet",
                          "default_network_tag": "", "network": "vpc"}) is None


def test_resolve_noops_without_a_project():
    assert _resolve_with({"router_name": "r", "subnetwork": "vm-subnet",
                          "default_network_tag": "t", "network": "vpc"},
                         project="") is None


def test_resolve_basenames_a_self_link_subnetwork():
    # The sandbox emits a bare name in one region and a full path in another; the NAT
    # subnetwork ref is rebuilt from project+region, so only the leaf is wanted.
    out = _resolve_with({
        "router_name": "dashboard-sandbox-router",
        "subnetwork": "projects/p/regions/us-east1/subnetworks/dashboard-sandbox-vm-subnet",
        "default_network_tag": "dashboard-sandbox-vm",
        "network": "dashboard-sandbox-vpc",
    })
    assert out["subnetwork"] == "dashboard-sandbox-vm-subnet", out
    assert out["tags"] == ["dashboard-sandbox-vm"], out


def test_resolve_splits_a_multi_tag_csv():
    out = _resolve_with({"router_name": "r", "subnetwork": "s",
                         "default_network_tag": "a, b ,, c", "network": "vpc"})
    assert out["tags"] == ["a", "b", "c"], out


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"PASS: {t.__name__}")
    print(f"\nAll {len(tests)} tests passed.")
