"""Unit tests for the Azure VM-listing resource-group fan-out.

A VM deployed into a non-default region lives in that region's resource group,
so listing only the default RG hides it. `listing_resource_groups` decides which
groups a listing must cover. It lives in a pure service module and takes its
config lookups as callables, so these tests need neither fastapi, the Azure SDK,
nor a configured app.

Runs under pytest, or standalone:  python tests/test_azure_vm_listing_regions.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from web_dashboard.services.azure_listing import listing_resource_groups


def _meta(rg, destroyed=False):
    return {"resource_group": rg, "destroyed": destroyed}


# region → resource group, as azure_region_configs would resolve it
_RG_BY_REGION = {"centralus": "vm-cli-rg", "westus2": "vm-cli-rg-westus2"}


def _call(job_meta, regions=(), default="vm-cli-rg"):
    return listing_resource_groups(
        job_meta,
        default_rg=lambda: default,
        rg_for=lambda r: _RG_BY_REGION.get(r, ""),
        configured_regions=lambda: list(regions),
    )


def test_default_rg_always_included():
    assert _call({}) == {"vm-cli-rg"}


def test_job_resource_groups_are_included():
    job_meta = {"vm-a": _meta("vm-cli-rg"), "vm-b": _meta("vm-cli-rg-westus2")}
    assert _call(job_meta) == {"vm-cli-rg", "vm-cli-rg-westus2"}


def test_destroyed_jobs_do_not_add_resource_groups():
    # A torn-down VM's RG shouldn't cost a describe_vms call on every listing.
    job_meta = {"vm-old": _meta("vm-cli-rg-eastus", destroyed=True)}
    assert _call(job_meta) == {"vm-cli-rg"}


def test_configured_regions_contribute_their_resource_groups():
    # The payoff: a region configured but with no deploy job yet is still listed,
    # so VMs created outside the dashboard in that region show up.
    assert _call({}, regions=("centralus", "westus2")) == {"vm-cli-rg", "vm-cli-rg-westus2"}


def test_regions_without_a_resource_group_are_skipped():
    assert _call({}, regions=("unconfigured-region",)) == {"vm-cli-rg"}


def test_sources_are_unioned_without_duplicates():
    job_meta = {"vm-a": _meta("vm-cli-rg"), "vm-b": _meta("vm-cli-rg-westus2")}
    assert _call(job_meta, regions=("centralus", "westus2")) == {
        "vm-cli-rg", "vm-cli-rg-westus2"}


def test_broken_region_config_still_yields_the_other_sources():
    # A malformed azure_region_configs must degrade to today's behaviour rather
    # than blanking the VM list.
    def boom():
        raise ValueError("malformed region map")

    got = listing_resource_groups(
        {"vm-b": _meta("vm-cli-rg-westus2")},
        default_rg=lambda: "vm-cli-rg",
        rg_for=lambda r: "",
        configured_regions=boom,
    )
    assert got == {"vm-cli-rg", "vm-cli-rg-westus2"}


def test_blank_default_rg_is_not_added():
    assert _call({"vm-a": _meta("rg-a")}, default="") == {"rg-a"}


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
