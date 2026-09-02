"""Destroying an Azure VM that has no deploy job must look in EVERY region's
resource group — not just the default one's.

Live failure this pins (2026-09-02): the Azure tab listed one VM,
``clouddb-jumpoint`` in **westus2** with "deployed by: unknown", and pressing
Destroy answered

    No active deployment found for VM 'clouddb-jumpoint'. It may have already been
    terminated or was not deployed from this dashboard.

The VM was neither terminated nor foreign. With no ``azure_deploy`` job (the shared
cloud-DB jumpoint is created by the tunnel path, not a deploy), the request falls to
``_destroy_without_deploy_job``, which probed the FLAT ``azure_resource_group`` —
i.e. the *default* region's group — while the listing that produced the row already
fans out over every configured region's group (``listing_resource_groups``). So a VM
in any non-default region was listed and then declared gone: the one 404 wording that
sends you looking in the Azure portal for something you just saw in the table.

What these pin:

- the destroy probe walks the SAME resource groups as the listing, in the same order,
  so a VM that is listed can always be destroyed — and when two regions each hold a
  VM of that name (``clouddb-jumpoint`` does), the one destroyed is the one the row
  showed;
- the resource group recorded on the destroy job comes from the VM's ARM id, because
  that group is what ``_run_destroy`` → ``terminate_vm`` addresses and what a wrong
  answer would aim the NIC/PIP deletion at;
- a group that errors (no Reader, deleted RG) does not mask a VM in the next group,
  and an error with NO VM found anywhere reports 502 rather than the "already
  terminated" 404;
- the genuine 404 names the groups searched, since the previous wording asserted the
  one thing the dashboard cannot know.

Heavy cloud deps (fastapi/azure-sdk/…) are only present in CI; when missing the file
SKIPs cleanly so the per-file runner stays green.

Run: python tests/test_azure_destroy_region.py   (or under pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_dashboard.services.azure_listing import (destroy_probe_order,
                                                  resource_group_from_vm_id)

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from web_dashboard.api import azure
    from web_dashboard.api.auth import get_current_user
    from web_dashboard.database import Job, VirtualDesktop, get_db
except Exception as exc:  # pragma: no cover — deps absent outside CI
    try:
        import pytest
        pytest.skip(f"azure api import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


SUB = "/subscriptions/00000000-0000-0000-0000-000000000000"
DEFAULT_RG = "dashboard-sandbox-rg"          # centralus, the flat azure_resource_group
WESTUS2_RG = "dashboard-sandbox-westus2-rg"  # only reachable via the region map


def _arm_id(rg: str, vm_name: str) -> str:
    return f"{SUB}/resourceGroups/{rg}/providers/Microsoft.Compute/virtualMachines/{vm_name}"


# ── azure_listing pure helpers ────────────────────────────────────────────────

def test_resource_group_from_vm_id():
    assert resource_group_from_vm_id(_arm_id(WESTUS2_RG, "vm1")) == WESTUS2_RG
    # ARM is inconsistent about the segment's casing depending on which API wrote
    # the id, so the match cannot be case-sensitive.
    assert resource_group_from_vm_id(
        f"{SUB}/resourcegroups/rg-a/providers/Microsoft.Compute/virtualMachines/vm1") == "rg-a"
    # The group's own casing is preserved — it is what later ARM calls should use.
    assert resource_group_from_vm_id(_arm_id("RG-Mixed", "vm1")) == "RG-Mixed"
    # Anything that isn't an ARM id has no group to report, and must say so rather
    # than guess: the caller falls back to the group that answered the probe.
    for junk in ("", None, "vm1", "/subscriptions/x/providers/Microsoft.Compute"):
        assert resource_group_from_vm_id(junk) == "", junk
    # A trailing "resourceGroups" with nothing after it must not IndexError.
    assert resource_group_from_vm_id(f"{SUB}/resourceGroups") == ""


def test_destroy_probe_order():
    groups = {WESTUS2_RG, DEFAULT_RG, "rg-eastus"}
    # No preference: the listing's own sorted order, so a name collision across
    # regions resolves to the same VM the list showed.
    assert destroy_probe_order(groups) == sorted(groups)
    # A group derived from the resource itself (a desktop seat's ARM id) wins, and
    # isn't probed twice.
    assert destroy_probe_order(groups, preferred=WESTUS2_RG) == (
        [WESTUS2_RG] + [g for g in sorted(groups) if g != WESTUS2_RG])
    # A preferred group nobody else knows about is still probed.
    assert destroy_probe_order({DEFAULT_RG}, preferred="rg-seat") == ["rg-seat", DEFAULT_RG]
    # Blanks are dropped, not probed as "".
    assert destroy_probe_order({DEFAULT_RG, ""}, preferred=None) == [DEFAULT_RG]
    assert destroy_probe_order(set()) == []
    assert destroy_probe_order(None) == []


# ── DELETE /api/azure/vms/{vm_name} — the fan-out ────────────────────────────

class _AdminUser:
    is_effective_admin = True
    is_admin = True
    username = "tester"
    workgroups_list: list = []
    effective_permissions_dict: dict = {}


class _FakeJob:
    def __init__(self, jid, metadata):
        self.id = jid
        self.metadata_dict = metadata


class _OtherDeployJob:
    """A live deploy job for a DIFFERENT VM. It contributes its resource group to
    the fan-out (same as the listing) without matching the VM being destroyed."""

    def __init__(self, rg):
        self.id = "j-other"
        self.status = "completed"
        self.workgroup = "eng"
        self.created_by = "tester"
        self.metadata_dict = {"vm_name": "some-other-vm", "resource_group": rg}


class _Seat:
    def __init__(self, vm_resource_id):
        self.vm_resource_id = vm_resource_id


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_a, **_k):
        return self

    def order_by(self, *_a, **_k):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    """Dispatches on the queried model so the Job fan-out and the desktop-seat
    lookup can be stubbed independently."""

    def __init__(self, jobs=(), seats=()):
        self._jobs = list(jobs)
        self._seats = list(seats)

    def query(self, model, *_a, **_k):
        if model is VirtualDesktop:
            return _FakeQuery(self._seats)
        if model is Job:
            return _FakeQuery(self._jobs)
        return _FakeQuery([])


_CAPTURED: dict = {}
_PROBED: list = []


def _install_stubs(vms, regions=("centralus", "westus2"), errors=()):
    """``vms`` maps resource group -> the VM names that exist in it; ``errors`` is
    the set of groups whose lookup blows up (no Reader, deleted RG)."""
    _CAPTURED.clear()
    del _PROBED[:]

    azure._rg = lambda: DEFAULT_RG
    azure._rg_for = lambda loc: {"centralus": DEFAULT_RG, "westus2": WESTUS2_RG}.get(loc, "")

    from web_dashboard.services import region_config
    region_config.load_region_configs = lambda cloud: {r: {} for r in regions}

    async def _get_vm(rg, vm_name):
        _PROBED.append(rg)
        if rg in errors:
            raise azure.AzureError(f"Failed to get VM {vm_name}: no access to {rg}")
        if vm_name in vms.get(rg, ()):
            return {"vm_id": _arm_id(rg, vm_name), "name": vm_name, "state": "VM running"}
        return None

    azure.azure_service.get_vm = _get_vm

    def _create_job(db, *, job_type, created_by, metadata=None, **_kw):
        _CAPTURED["job_type"] = job_type
        _CAPTURED["metadata"] = metadata
        return _FakeJob("job-destroy", metadata)

    azure.job_service.create_job = _create_job
    azure.job_service.log_audit = lambda *a, **k: None

    from web_dashboard.services import vdesktop_service

    async def _drop_seat(db, vm_name):
        _CAPTURED["seat_dropped"] = vm_name

    vdesktop_service.drop_seat_by_vm = _drop_seat


def _make_client(db) -> TestClient:
    app = FastAPI()
    app.include_router(azure.router)
    app.dependency_overrides[get_current_user] = lambda: _AdminUser()
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app, raise_server_exceptions=False)


def test_destroy_finds_a_vm_in_a_non_default_region_rg():
    """The regression: ``clouddb-jumpoint`` exists only in westus2's group, which
    is reachable through the region map alone."""
    _install_stubs({WESTUS2_RG: ("clouddb-jumpoint",)})
    r = _make_client(_FakeDB()).delete("/api/azure/vms/clouddb-jumpoint")
    assert r.status_code == 200, (r.status_code, r.text)
    assert _CAPTURED["job_type"] == "azure_destroy"
    # The group terminate_vm will address — the wrong one deletes nothing (or, when
    # both regions hold that name, the wrong VM).
    assert _CAPTURED["metadata"]["resource_group"] == WESTUS2_RG, _CAPTURED["metadata"]
    assert _CAPTURED["metadata"]["deploy_job_id"] is None
    # The default group is probed too — this must not become "westus2 only".
    assert DEFAULT_RG in _PROBED, _PROBED


def test_destroy_prefers_the_group_the_listing_showed_on_a_name_collision():
    """Both regions hold a ``clouddb-jumpoint``. The listing walks the groups
    sorted and keeps the first, so the destroy must resolve the same one."""
    _install_stubs({DEFAULT_RG: ("clouddb-jumpoint",), WESTUS2_RG: ("clouddb-jumpoint",)})
    r = _make_client(_FakeDB()).delete("/api/azure/vms/clouddb-jumpoint")
    assert r.status_code == 200, (r.status_code, r.text)
    first = sorted({DEFAULT_RG, WESTUS2_RG})[0]
    assert _CAPTURED["metadata"]["resource_group"] == first, _CAPTURED["metadata"]
    assert _PROBED == [first], _PROBED  # and it stops on the first hit


def test_destroy_records_the_group_from_the_arm_id_not_the_probe():
    """ARM's id is authoritative — and carries ARM's own casing, which is what
    later Compute/Network calls should be given."""
    _install_stubs({})

    async def _get_vm(rg, vm_name):
        _PROBED.append(rg)
        if rg != WESTUS2_RG:
            return None
        # Same group, ARM's casing.
        return {"vm_id": _arm_id(WESTUS2_RG.upper(), vm_name), "name": vm_name}

    azure.azure_service.get_vm = _get_vm
    r = _make_client(_FakeDB()).delete("/api/azure/vms/vm-eu")
    assert r.status_code == 200, (r.status_code, r.text)
    assert _CAPTURED["metadata"]["resource_group"] == WESTUS2_RG.upper()


def test_destroy_falls_back_to_the_answering_group_without_an_arm_id():
    _install_stubs({})

    async def _get_vm(rg, vm_name):
        return {"name": vm_name} if rg == WESTUS2_RG else None   # no vm_id at all

    azure.azure_service.get_vm = _get_vm
    r = _make_client(_FakeDB()).delete("/api/azure/vms/vm-eu")
    assert r.status_code == 200, (r.status_code, r.text)
    assert _CAPTURED["metadata"]["resource_group"] == WESTUS2_RG


def test_destroy_covers_a_group_only_another_vms_deploy_job_knows():
    """Same three sources as the listing: a group that appears only on a live
    deploy job for some other VM is still probed."""
    _install_stubs({"rg-from-a-job": ("orphan-vm",)}, regions=("centralus",))
    db = _FakeDB(jobs=[_OtherDeployJob("rg-from-a-job")])
    r = _make_client(db).delete("/api/azure/vms/orphan-vm")
    assert r.status_code == 200, (r.status_code, r.text)
    assert _CAPTURED["metadata"]["resource_group"] == "rg-from-a-job"


def test_destroy_probes_a_desktop_seats_own_group_first():
    """A VDI seat's ARM id names its group directly, so it beats every config read
    — and the seat row (plus its PRA RDP jump) is dropped before the terminate."""
    _install_stubs({"rg-vdi-pool": ("seat-07",)})
    db = _FakeDB(seats=[_Seat(_arm_id("rg-vdi-pool", "seat-07"))])
    r = _make_client(db).delete("/api/azure/vms/seat-07")
    assert r.status_code == 200, (r.status_code, r.text)
    assert _PROBED[0] == "rg-vdi-pool", _PROBED
    assert _CAPTURED["metadata"]["resource_group"] == "rg-vdi-pool"
    assert _CAPTURED.get("seat_dropped") == "seat-07"


def test_destroy_404_names_the_groups_it_searched():
    """A VM that really is gone still 404s — but the detail can no longer claim it
    "may have already been terminated" and leave it there."""
    _install_stubs({})
    r = _make_client(_FakeDB()).delete("/api/azure/vms/ghost-vm")
    assert r.status_code == 404, (r.status_code, r.text)
    detail = r.json()["detail"]
    assert DEFAULT_RG in detail and WESTUS2_RG in detail, detail
    assert "ghost-vm" in detail, detail
    assert "metadata" not in _CAPTURED, "no destroy job for a VM that isn't there"


def test_one_unreachable_group_does_not_mask_the_vm_in_the_next():
    _install_stubs({WESTUS2_RG: ("clouddb-jumpoint",)}, errors=(DEFAULT_RG,))
    r = _make_client(_FakeDB()).delete("/api/azure/vms/clouddb-jumpoint")
    assert r.status_code == 200, (r.status_code, r.text)
    assert _CAPTURED["metadata"]["resource_group"] == WESTUS2_RG


def test_a_failed_lookup_with_no_vm_anywhere_is_502_not_404():
    """Credentials gone / ARM down is not "already terminated" — reporting the 404
    there is what sends someone hunting in the portal."""
    _install_stubs({}, errors=(DEFAULT_RG, WESTUS2_RG))
    r = _make_client(_FakeDB()).delete("/api/azure/vms/clouddb-jumpoint")
    assert r.status_code == 502, (r.status_code, r.text)
    assert "no access to" in r.json()["detail"], r.text
    assert "metadata" not in _CAPTURED


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
