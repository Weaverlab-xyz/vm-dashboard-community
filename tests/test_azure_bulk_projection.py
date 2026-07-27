"""Unit tests: the Azure batch→single projection, and who owns the ACI container.

`_run_bulk_deploy` now delegates to `_run_deploy`, which takes an `AzureDeployRequest`.
The batch takes an `AzureBulkDeployRequest` plus per-item overrides, so something has to
project one onto the other. Two ways that goes wrong, both silent:

  * a field the projection forgets falls back to the model default, so a VM quietly
    deploys into the wrong subnet, without its NSGs, or without the Entitle opt-in the
    operator ticked;
  * a count fan-out child carries only `vm_name` — everything else is meant to be
    inherited from the batch request — so a projection that trusts the item blindly
    deploys a VM with no image.

The other half is `_AciRef.owned`. A single deploy stops its ACI container group when
the VM create fails; a batch must not, because its siblings are still using it. That is
one boolean between "clean up after yourself" and "strand the rest of the batch".

Imported as real package modules with the Azure SDK stubbed. Runs under pytest, or
standalone:  python tests/test_azure_bulk_projection.py
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _install_stubs():
    def mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules.setdefault(name, m)
        return m

    sa_orm = mod("sqlalchemy.orm", Session=type("Session", (), {}))
    mod("sqlalchemy", orm=sa_orm)
    mod("web_dashboard.database", Job=type("Job", (), {"id": None}),
        SessionLocal=lambda: None)
    az = mod("web_dashboard.services.azure_service")
    az.AzureError = type("AzureError", (Exception,), {})
    mod("web_dashboard.services.cache_service")
    mod("web_dashboard.services.job_service")


_install_stubs()

try:
    from web_dashboard.services import azure_vm_service as svc
    from web_dashboard.models.azure import (
        AzureBulkDeployItem, AzureBulkDeployRequest, AzureDeployRequest,
    )
except Exception as exc:  # pragma: no cover
    try:
        import pytest
        pytest.skip(f"azure_vm_service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


def _bulk(**over):
    base = dict(
        items=[AzureBulkDeployItem(vm_name="web-01")],
        image_id="/img/batch-default",
        vm_size="Standard_D2s_v3",
        location="westus2",
        resource_group="rg-x",
        subnet_id="/subnets/vm-subnet",
        nsg_ids=["/nsg/a", "/nsg/b"],
        create_public_ip=True,
        os_type="Linux",
        trusted_launch=False,
        ssh_username="labuser",
        ssh_public_key="ssh-ed25519 AAAA",
        workgroup="hydra",
        register_in_entitle=True,
        register_in_passwordsafe=True,
        ssh_key_secret_override="kv-secret",
        jump_group="jg", jumpoint_name="jp",
        pra_credential_ref="azure_kv://cred",
        docker_deploy_key_ref="azure_kv://dkey",
    )
    base.update(over)
    return AzureBulkDeployRequest(**base)


# ── the projection ───────────────────────────────────────────────────────────

def test_every_shared_field_survives_the_projection():
    """The failure this guards is silent: a forgotten field falls back to the model
    default, so the VM deploys into the wrong subnet or without its NSGs and still
    reports success."""
    req = _bulk()
    item = svc._AzureBulkItem.from_child({"vm_name": "web-01"}, req)
    child = svc._child_request(item, req)

    shared = (set(AzureDeployRequest.model_fields)
              & set(AzureBulkDeployRequest.model_fields)) - {"count"}
    for field in sorted(shared):
        assert getattr(child, field) == getattr(req, field), (
            f"{field} was not carried onto the child request")


def test_the_projection_is_a_single_deploy():
    req = _bulk()
    item = svc._AzureBulkItem.from_child({"vm_name": "web-01"}, req)
    child = svc._child_request(item, req)
    assert child.vm_name == "web-01"
    assert child.count == 1, "a projected child must not itself fan out again"


def test_a_per_item_image_beats_the_batch_default():
    """Bulk means one VM per selected image, so the item's image has to win."""
    req = _bulk()
    item = svc._AzureBulkItem.from_child({
        "vm_name": "win-01",
        "image_id": "/img/per-item",
        "image_publisher": "MicrosoftWindowsServer",
        "image_offer": "WindowsServer",
        "image_sku": "2022-datacenter",
        "image_version": "latest",
        "os_type": "Windows",
        "trusted_launch": True,
    }, req)
    child = svc._child_request(item, req)
    assert child.image_id == "/img/per-item"
    assert child.image_publisher == "MicrosoftWindowsServer"
    assert child.os_type == "Windows"
    assert child.trusted_launch is True
    # …while everything genuinely batch-level still comes from the request.
    assert child.subnet_id == req.subnet_id
    assert child.vm_size == req.vm_size


def test_a_count_fanout_child_inherits_its_image():
    """Count fan-out children are persisted carrying ONLY vm_name — the rest is meant
    to be resolved off the parent request. A projection that trusted the item blindly
    would deploy a VM with no image at all."""
    req = _bulk(image_id="/img/batch-default", os_type="Linux")
    item = svc._AzureBulkItem.from_child({"vm_name": "web-07"}, req)
    child = svc._child_request(item, req)
    assert child.vm_name == "web-07"
    assert child.image_id == "/img/batch-default"
    assert child.os_type == "Linux"


def test_windows_is_decided_per_item_not_per_batch():
    """A multi-select batch can span both OS types, and Windows changes the whole
    per-VM flow — generated password, no Shell Jump."""
    req = _bulk(os_type="Linux")
    linux = svc._child_request(
        svc._AzureBulkItem.from_child({"vm_name": "lin-01"}, req), req)
    win = svc._child_request(
        svc._AzureBulkItem.from_child({"vm_name": "win-01", "os_type": "Windows"}, req), req)
    assert linux.os_type == "Linux"
    assert win.os_type == "Windows"


# ── who owns the ACI container ───────────────────────────────────────────────

def test_a_single_deploy_owns_its_container():
    assert svc._AciRef("owned", group_name="aci-1").owned is True


def test_a_batch_container_is_never_torn_down_by_one_vm():
    """The assertion that keeps a failing VM from stranding its siblings."""
    assert svc._AciRef("shared", group_name="aci-1").owned is False


def test_a_container_that_never_started_is_not_owned():
    assert svc._AciRef("owned", error="quota").owned is False
    assert svc._AciRef("none").owned is False


def test_the_ref_records_a_group_or_an_error_but_not_both():
    started, failed = {}, {}
    svc._AciRef("owned", group_name="aci-1").record(started)
    svc._AciRef("owned", error="boom").record(failed)
    assert started == {"aci_group_name": "aci-1"}
    assert failed == {"aci_error": "boom"}


def test_a_failed_deploy_key_is_visible_in_both_modes():
    """deploy_key_note used to be assigned and never read on the batch path, so an ACI
    built without a deploy key left no trace anywhere an operator would look."""
    note = " [deploy key fetch failed: nope]"
    for mode in ("owned", "shared"):
        assert note in svc._AciRef(mode, group_name="aci-1", deploy_key_note=note).note()


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
