"""The Azure VM list's Deployed By column, for VMs no ``azure_deploy`` job created.

The listing walks every dashboard resource group, so it also shows VMs other features
create: the Rancher management node (a ``rancher_node_deploy`` job) and the shared
BeyondTrust Gateway host (no job of its own — whichever VM, database or K8s tunnel
first needs it creates it, and the rest reuse it). Both used to read "unknown", which
looks like lost data. Now:

  * the Rancher node shows the user who deployed it — the newest deploy that CREATED
    it, not one that merely reused a running node;
  * the Gateway host shows "shared (Gateway)", for the current ``purpose=gateway`` tag
    and the legacy ``purpose=clouddb-jumpoint`` one existing hosts still carry;
  * an azure_deploy job still wins, and a VM nobody accounts for is still "unknown".

Run: python tests/test_azure_deployed_by.py   (or under pytest)
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Probe the third-party deps by name; the first-party import below is unguarded, so a
# broken api/azure.py fails this file instead of skipping it.
try:
    import fastapi  # noqa: F401
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover — deps absent outside CI
    try:
        import pytest
        pytest.skip(f"third-party dep unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

from web_dashboard.api import azure  # noqa: E402


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    """Answers each query with the rows of the job type it asked for. The filter
    expression is SQLAlchemy's, so the type is read off its right-hand literal."""

    def __init__(self, rows):
        self._rows = rows

    def query(self, *_a, **_k):
        db = self

        class _Q(_FakeQuery):
            def filter(self, *conds, **_k):
                types = {getattr(getattr(c, "right", None), "value", None) for c in conds}
                types.discard(None)
                if types & {"azure_deploy", "rancher_node_deploy"}:
                    self._rows = [r for r in self._rows if r.job_type in types]
                return self
        return _Q(db._rows)


class _Job:
    def __init__(self, job_type, created_by, metadata, jid="j", workgroup=None):
        self.id = jid
        self.job_type = job_type
        self.created_by = created_by
        self.workgroup = workgroup
        self.metadata_dict = metadata


def _rancher(user, *, name="rancher-server", cloud="azure", reused=False):
    return _Job("rancher_node_deploy", user,
                {"cloud": cloud, "name": name, "reused": reused})


def _vm(name, purpose=None):
    tags = {"managed-by": "vm-dashboard"}
    if purpose:
        tags["purpose"] = purpose
    return {"vm_id": name, "name": name, "state": "running",
            "location": "centralus", "tags": tags}


def _listing(vms, jobs):
    async def _describe_vms(rg):
        return list(vms)

    async def _get_vm(rg, name):
        return None

    # Restored afterwards: these are shared module attributes, and other test files
    # in the same pytest process call the real ones.
    saved = [(azure, "_rg"), (azure, "_listing_resource_groups"),
             (azure.azure_service, "describe_vms"), (azure.azure_service, "get_vm")]
    saved = [(o, a, getattr(o, a)) for o, a in saved]
    azure._rg = lambda: "rg-default"
    azure._listing_resource_groups = lambda job_meta: {"rg-default"}
    azure.azure_service.describe_vms = _describe_vms
    azure.azure_service.get_vm = _get_vm
    try:
        rows = asyncio.run(azure._fetch_vms(_FakeDB(jobs)))
    finally:
        for o, a, v in saved:
            setattr(o, a, v)
    return {r["name"]: r["deployed_by"] for r in rows}


def test_rancher_node_shows_its_deployer():
    got = _listing([_vm("rancher-server", "rancher")], [_rancher("admin")])
    assert got == {"rancher-server": "admin"}, got


def test_a_reuse_does_not_take_credit_for_creating_the_node():
    # Newest first, as the query orders them: bob reused what alice created.
    jobs = [_rancher("bob", reused=True), _rancher("alice")]
    assert azure._rancher_deployers(_FakeDB(jobs)) == {"rancher-server": "alice"}


def test_a_node_only_seen_through_reuses_still_names_someone():
    jobs = [_rancher("bob", reused=True), _rancher("carol", reused=True)]
    assert azure._rancher_deployers(_FakeDB(jobs)) == {"rancher-server": "bob"}


def test_another_clouds_rancher_node_is_not_this_one():
    got = _listing([_vm("rancher-server", "rancher")], [_rancher("admin", cloud="gcp")])
    assert got == {"rancher-server": "unknown"}, got


def test_gateway_host_is_shared_under_both_tags():
    got = _listing([_vm("clouddb-jumpoint", "clouddb-jumpoint"), _vm("gw-new", "gateway")],
                   [])
    assert got == {"clouddb-jumpoint": azure.SHARED_GATEWAY_DEPLOYER,
                   "gw-new": azure.SHARED_GATEWAY_DEPLOYER}, got


def test_an_azure_deploy_job_still_wins_and_strays_stay_unknown():
    deploy = _Job("azure_deploy", "tester", {"vm_name": "app-1"}, jid="d1")
    got = _listing([_vm("app-1"), _vm("stray")], [deploy])
    assert got == {"app-1": "tester", "stray": "unknown"}, got


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
