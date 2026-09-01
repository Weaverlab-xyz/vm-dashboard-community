"""The Databases provision form's region picker, and the network it commits to.

The reported failure: an Azure MySQL Flexible Server provisioned into ``westus2``
died ~90 seconds into ``terraform apply`` with

    VnetWithDifferentLocationNotSupported: The virtual network
    'dashboard-sandbox-vnet' coming from a different location 'centralus' is not
    supported. Requested resource location is 'westus2'.

Nothing was wrong with the module or the region: ``location`` really was ``westus2``.
The *subnet* was the default region's, because every network field a private database
needs resolves through ``region_config.resolve_region``, which falls each field back to
the flat config key. A region with no config set — or with a set that leaves the DB
subnet blank — therefore resolves to the DEFAULT region's subnet while the form, the
row and the job all say ``westus2``. That is the whole bug class: multi-region was real
for ``location`` and fictional for the network.

Two halves, so two groups of tests:

  * ``_region_choices`` offers ``region_config.deployable_regions`` — the configured
    default plus each region with a config set — and NOT the full ``region_catalog``,
    which is what an operator may *configure*, not where a database may land. Same rule
    as the k8s, Cloud Functions and gateway pickers. OCI is the deliberate exception:
    no per-region sets, and a free-tier Autonomous DB is a public endpoint that uses no
    regional network of ours.
  * ``_reject_cross_region_network`` refuses the mismatch at REQUEST time, before
    ``provision`` writes a row, mints an admin credential or creates a job — the same
    reasoning as the VM path's ``api/azure._reject_cross_region_network``, and it
    **fails open** the same way, because a lookup we could not perform (no creds, no
    Reader on the VNet's resource group, a throttled RDS call) must not block an
    otherwise valid provision.

Collaborators are replaced with spies (the house pattern from
test_cloud_function_region_picker.py), so no DB, no cloud SDK and no config store is
needed — ``config_service`` reads open a real session and do not swallow the failure.

Run: python tests/test_clouddb_region_picker.py   (or under pytest)
"""
import asyncio
import contextlib
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

try:
    from fastapi import HTTPException

    from web_dashboard.api import cloud_databases
    from web_dashboard.services import region_catalog
except Exception as exc:  # pragma: no cover — deps absent outside CI
    try:
        import pytest
        pytest.skip(f"cloud_databases import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


@contextlib.contextmanager
def _patched(module, **attrs):
    """Swap module attributes for the duration of a test, then put them back."""
    saved = {k: getattr(module, k) for k in attrs}
    for k, v in attrs.items():
        setattr(module, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(module, k, v)


# One cloud with a second configured region, one without — the two shapes that matter.
_CONFIGURED = {
    "aws": ["us-east-2", "eu-west-1"],
    "azure": ["centralus", "westus2"],
    "gcp": ["us-central1"],
}

_CENTRALUS_SUBNET = ("/subscriptions/s/resourceGroups/dashboard-sandbox-rg/providers/"
                     "Microsoft.Network/virtualNetworks/dashboard-sandbox-vnet/"
                     "subnets/db-mysql-subnet")
_WESTUS2_SUBNET = ("/subscriptions/s/resourceGroups/dashboard-sandbox-rg/providers/"
                   "Microsoft.Network/virtualNetworks/dashboard-sandbox-vnet-westus2/"
                   "subnets/db-mysql-subnet")


class _Azure:
    """``azure_service`` stand-in. ``locations`` maps an ARM id to the region it is in;
    a missing id yields "", which is what the real one returns when it cannot look the
    resource up (see azure_service.resource_locations) — the fail-open path."""

    def __init__(self, locations=None):
        self.locations = locations or {}
        self.asked = []

    async def resource_locations(self, ids):
        self.asked.append(list(ids))
        return {i: self.locations.get(i, "") for i in ids}


class _Aws:
    """``aws_service`` stand-in serving one region's DB pickers, or raising."""

    def __init__(self, groups=(), sgs=(), error=None):
        self.groups, self.sgs, self.error = list(groups), list(sgs), error
        self.asked = []

    async def get_db_options(self, region):
        self.asked.append(region)
        if self.error:
            raise self.error
        return {"db_subnet_groups": [{"name": g} for g in self.groups],
                "security_groups": [{"id": s} for s in self.sgs]}


class _Cache:
    """``cache_service`` stand-in: no store, just call the fetch."""
    TTL = {"aws_db_options": 60}

    @staticmethod
    def key_param(name, **kw):
        return name

    @staticmethod
    async def get_or_refresh(_key, _ttl, fetch):
        return await fetch(), None


def _reject(engine="mysql", cloud="azure", region="westus2", opts=None,
            ids=None, configured=None, azure=None, aws=None):
    """Run ``_reject_cross_region_network`` against stubbed collaborators. Returns the
    Azure/AWS spies so a test can assert what was (or was not) asked."""
    azure = azure if azure is not None else _Azure()
    aws = aws if aws is not None else _Aws()
    conf = configured if configured is not None else _CONFIGURED
    with _patched(cloud_databases,
                  deployable_regions=lambda c: list(conf.get(c, [])),
                  azure_service=azure, aws_service=aws, cache_service=_Cache), \
         _patched(cloud_databases.cloud_database_service,
                  regional_network_ids=lambda **kw: dict(ids or {})):
        asyncio.run(cloud_databases._reject_cross_region_network(
            engine, cloud, region, dict(opts or {})))
    return azure, aws


def _expect_400(**kw):
    try:
        _reject(**kw)
    except HTTPException as exc:
        assert exc.status_code == 400, exc.status_code
        return exc.detail
    raise AssertionError("expected an HTTP 400")


# ── what the provision form is offered ────────────────────────────────────────

def test_picker_offers_configured_regions_not_the_whole_catalog():
    """The silent-placement bug in one assertion: the catalog lists ~30 Azure regions,
    only two of which have a DB subnet of their own. Offering the rest is offering a
    deploy onto the default region's network."""
    for cloud, expected in _CONFIGURED.items():
        with _patched(cloud_databases, deployable_regions=lambda c: list(_CONFIGURED[c])):
            choices = cloud_databases._region_choices(cloud, expected[0])
        assert choices == expected, (cloud, choices)
        assert len(choices) < len(region_catalog.region_ids(cloud)), cloud


def test_picker_keeps_the_full_catalog_for_oci():
    """OCI carries no per-region config sets, and a free-tier Autonomous DB is a public
    endpoint with no network of ours — so there is nothing to be out of region with,
    and restricting the list would only remove working capability."""
    choices = cloud_databases._region_choices("oci", "us-ashburn-1")
    assert choices == region_catalog.region_ids("oci")


def test_picker_forces_the_resolved_region_in_first():
    """Whatever the operator just picked (or has configured) is always present and
    selected, even if it somehow is not in either list — otherwise the form would
    silently re-point at a different region on reload."""
    with _patched(cloud_databases, deployable_regions=lambda c: ["centralus", "westus2"]):
        assert cloud_databases._region_choices("azure", "westus2")[0] == "westus2"
        assert cloud_databases._region_choices("azure", "germanywestcentral") == [
            "germanywestcentral", "centralus", "westus2"]


def test_resolved_region_is_normalised_even_when_it_comes_from_the_flat_default():
    """``region_catalog.resolve`` returns the RAW ``azure_location`` for a blank region,
    so an operator who typed "West US 2" had the form posting that back as a region id
    — and every per-region lookup keys on "westus2". Canonicalise once, in this route."""
    with _patched(region_catalog, default_region=lambda c: "West US 2"):
        assert cloud_databases._resolve_db_region("azure", None) == "westus2"
        assert cloud_databases._resolve_db_region("azure", " West US 2 ") == "westus2"


def test_resolve_region_rejects_a_malformed_region():
    try:
        cloud_databases._resolve_db_region("azure", "not a region!")
    except HTTPException as exc:
        assert exc.status_code == 400
        return
    raise AssertionError("expected an HTTP 400")


# ── what the provision path refuses ───────────────────────────────────────────

def test_unconfigured_region_is_refused_before_anything_is_created():
    """No config set for the region at all — the plain case. The message has to name
    where to fix it, because nothing in the Azure error ever does."""
    detail = _expect_400(region="germanywestcentral")
    assert "germanywestcentral" in detail
    assert "Multi-region" in detail


def test_unconfigured_region_check_needs_no_cloud_call():
    """Check 1 must not depend on cloud credentials or a Reader grant — it is the one
    that always runs, and an operator without either still deserves the 400."""
    azure = _Azure()
    aws = _Aws()
    try:
        _reject(region="germanywestcentral", azure=azure, aws=aws)
    except HTTPException:
        pass
    assert azure.asked == [] and aws.asked == []


def test_azure_subnet_from_another_region_is_refused():
    """The reported failure. ``westus2`` IS configured, but its set leaves the MySQL
    subnet blank, so ``resolve_region`` hands back the centralus one — indistinguishable
    in the settings panel, because an ARM id carries the resource group, not the region."""
    detail = _expect_400(
        ids={"delegated_subnet_id": _CENTRALUS_SUBNET},
        azure=_Azure({_CENTRALUS_SUBNET: "centralus"}))
    assert "centralus" in detail and "westus2" in detail
    assert "VnetWithDifferentLocationNotSupported" in detail


def test_azure_subnet_in_the_requested_region_is_accepted():
    azure, _ = _reject(ids={"delegated_subnet_id": _WESTUS2_SUBNET},
                       azure=_Azure({_WESTUS2_SUBNET: "westus2"}))
    assert azure.asked == [[_WESTUS2_SUBNET]]


def test_azure_display_name_location_is_not_a_mismatch():
    """Azure answers ``location`` as "West US 2" about as often as "westus2". Comparing
    the raw strings would reject a perfectly good subnet."""
    _reject(ids={"delegated_subnet_id": _WESTUS2_SUBNET},
            azure=_Azure({_WESTUS2_SUBNET: "West US 2"}))


def test_azure_unresolvable_subnet_fails_open():
    """No Reader on the VNet's resource group → "" → let it through. Blocking here would
    turn a missing IAM grant into "you cannot provision databases at all", and Terraform
    still refuses a real mismatch. Same call the VM deploy route makes."""
    _reject(ids={"delegated_subnet_id": _CENTRALUS_SUBNET}, azure=_Azure({}))


def test_azure_sqlserver_private_endpoint_subnet_is_checked_too():
    """Azure SQL DB reports its subnet as ``subnet_id``, not ``delegated_subnet_id`` —
    reading only the latter would leave one engine unguarded."""
    detail = _expect_400(engine="sqlserver", ids={"subnet_id": _CENTRALUS_SUBNET},
                         azure=_Azure({_CENTRALUS_SUBNET: "centralus"}))
    assert "centralus" in detail


def test_aws_subnet_group_missing_from_the_region_is_refused():
    """RDS resolves a DB subnet group by NAME within one region, so the default
    region's name simply is not there — and the raw AWS error says only "not found"."""
    detail = _expect_400(
        cloud="aws", region="eu-west-1", ids={"db_subnet_group_name": "sandbox-db-subnets"},
        aws=_Aws(groups=["eu-db-subnets"]))
    assert "sandbox-db-subnets" in detail and "eu-west-1" in detail


def test_aws_security_group_from_another_region_is_refused():
    detail = _expect_400(
        cloud="aws", region="eu-west-1",
        ids={"db_subnet_group_name": "eu-db-subnets", "vpc_security_group_ids": ["sg-us"]},
        aws=_Aws(groups=["eu-db-subnets"], sgs=["sg-eu"]))
    assert "sg-us" in detail and "eu-west-1" in detail


def test_aws_in_region_network_is_accepted():
    _, aws = _reject(cloud="aws", region="eu-west-1",
                     ids={"db_subnet_group_name": "eu-db-subnets",
                          "vpc_security_group_ids": ["sg-eu"]},
                     aws=_Aws(groups=["eu-db-subnets"], sgs=["sg-eu"]))
    assert aws.asked == ["eu-west-1"]


def test_aws_lookup_failure_fails_open():
    """A throttled or credential-less RDS/EC2 call must not block the provision."""
    _reject(cloud="aws", region="eu-west-1", ids={"db_subnet_group_name": "whatever"},
            aws=_Aws(error=RuntimeError("throttled")))


def test_aws_empty_picker_lists_fail_open():
    """An account whose describe calls answer with nothing (a fresh region, a policy
    that filters everything) must not read as "your subnet group is in the wrong
    region" — an empty list is an unanswered question, not a negative answer."""
    _reject(cloud="aws", region="eu-west-1",
            ids={"db_subnet_group_name": "sandbox-db-subnets",
                 "vpc_security_group_ids": ["sg-us"]},
            aws=_Aws(groups=[], sgs=[]))


def test_gcp_configured_region_asks_no_cloud_anything():
    """Cloud SQL takes its private IP from a GLOBAL VPC, so there is no regional network
    to confirm — but the region still has to be configured, or the row can never get an
    Entitle adapter beside it (pair_adapter already refuses one)."""
    azure, aws = _reject(engine="postgres", cloud="gcp", region="us-central1", ids={})
    assert azure.asked == [] and aws.asked == []


def test_oci_is_exempt_entirely():
    """No per-region config sets exist for OCI, so check 1 would reject every region but
    the default — for a database that needs no network of ours at all."""
    _reject(engine="oracle", cloud="oci", region="us-phoenix-1", ids={})


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
