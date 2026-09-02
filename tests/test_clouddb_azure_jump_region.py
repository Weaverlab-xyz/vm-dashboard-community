"""The Azure jump VM a cloud-DB onboarding runs its DB client on must be the one in
the DATABASE's region.

Live failure this pins (2026-09-02, westus2 Azure MySQL): "Register in Password Safe"
died at 25% with

    ERROR 2005 (HY000): Unknown MySQL server host
    'clouddb-6496f193.mysql.database.azure.com' (-2)

The database was healthy and reachable — proven from the westus2 jumpoint, where the
same ``mysql:8.4`` client answered ``ERROR 1045 Access denied``. The DB client had run
on the *centralus* jump VM, because the onboarding paired the VM name returned by
``ensure_jumpoint_host`` — which had correctly created/found the VM in westus2 — with
the FLAT ``azure_resource_group``, which names the default region's group. Both groups
hold a VM called ``clouddb-jumpoint``, so Run Command succeeded against a real, healthy,
wrong VM whose VNet has no link to westus2's ``…private.mysql.database.azure.com`` zone.
The only symptom was a DNS error naming neither region. The ARM activity log is what
settled it: the ``runCommand`` calls are logged under the *default* region's resource
group and there are none under the database's.

#726 fixed that call site. These pin it, and finish the job: the same flat-key-first
read was in six more places in the same file, and on AWS — where ``aws_region`` really
is a key, unlike the ``azure_region`` the Azure read composed — it is not harmless.

What these therefore pin:

- the resource group comes from the ROW's region, and single-region installs (no
  ``azure_region_configs``) still resolve to the flat key — the backward-compat
  guarantee ``region_config`` is built on;
- the ctx carries that same group, because it is packed into the Password Safe
  managed-system address (``vmName;resourceGroup;…``): a wrong one there aims every
  future rotation at the wrong VM, long after this job is green;
- "which resource group is this gateway in" has ONE reader, so the ensure path that
  creates the VM and any caller that addresses it cannot drift apart;
- the row's region beats the configured default region everywhere in the DB service —
  the flat-key-first read was invisible on Azure only because it composed a key that
  does not exist (``azure_region``; Azure's is ``azure_location``);
- the failure message names the VM, its resource group and the region, since the
  client's own error is about DNS and cannot say which VM resolved it.

Runs under pytest, or standalone:  python tests/test_clouddb_azure_jump_region.py
"""
import asyncio
import json
import os
import re
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_JHS = os.path.join(_ROOT, "web_dashboard", "services", "jumpoint_host_service.py")

CONF = {}
RUNS = []          # (resource_group, vm_name, commands)
RUN_RESULTS = []   # queued vm_run_command results; the default is Success/0


class _Settings:
    def __getattr__(self, _key):
        return ""


class _CloudDatabase:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Job:
    id = None


def _install_stubs():
    confmod = types.ModuleType("web_dashboard.config")
    confmod.settings = _Settings()
    sys.modules["web_dashboard.config"] = confmod

    dbmod = types.ModuleType("web_dashboard.database")
    dbmod.CloudDatabase = _CloudDatabase
    dbmod.Job = _Job
    sys.modules["web_dashboard.database"] = dbmod

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key: CONF.get(key, "")
    cfg.set = lambda key, val: CONF.__setitem__(key, val)
    cfg.get_bool = lambda key, default=False: bool(CONF.get(key, default))
    sys.modules["web_dashboard.services.config_service"] = cfg

    az = types.ModuleType("web_dashboard.services.azure_service")

    async def _vm_run_command(resource_group, vm_name, commands, timeout=0):
        RUNS.append((resource_group, vm_name, list(commands)))
        return (RUN_RESULTS.pop(0) if RUN_RESULTS
                else {"status": "Success", "response_code": 0, "stdout": "", "stderr": ""})

    az.vm_run_command = _vm_run_command
    sys.modules["web_dashboard.services.azure_service"] = az

    js = types.ModuleType("web_dashboard.services.job_service")
    for name in ("update_progress", "append_job_log", "set_running", "set_failed",
                 "set_completed", "create_job"):
        setattr(js, name, lambda *a, **k: None)
    sys.modules["web_dashboard.services.job_service"] = js

    for name in ("terraform", "terraform_provider_env"):
        sys.modules[f"web_dashboard.services.{name}"] = types.ModuleType(
            f"web_dashboard.services.{name}")


_install_stubs()
try:
    from web_dashboard.services import cloud_database_service as svc
    from web_dashboard.services import jumpoint_host_service as jhs
except Exception as exc:  # pragma: no cover — skip if other app deps are missing
    try:
        import pytest
        pytest.skip(f"cloud_database_service import unavailable: {exc}",
                    allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


async def _ensure_host(_cloud, _region):
    """The real ensure path returns only the VM NAME — which is the same in every
    region. That is exactly why the caller must resolve the group itself."""
    return "clouddb-jumpoint"


jhs.ensure_jumpoint_host = _ensure_host


class _FakeDB:
    def query(self, _model):
        raise AssertionError("the managed-user creation touches no rows")

    def commit(self):  # pragma: no cover
        pass


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _reset(**conf):
    CONF.clear()
    RUNS.clear()
    RUN_RESULTS.clear()
    # The live shape: the flat keys describe centralus, and westus2 has a config set
    # of its own (the sandbox emits one per region it is run in).
    CONF.update({
        "azure_location": "centralus",
        "azure_resource_group": "dashboard-sandbox-rg",
        "clouddb/db-6496f193/admin": "AdminPw1",
    })
    CONF.update(conf)


def _row(**kw):
    defaults = dict(id="db-6496f193", engine="mysql", cloud="azure", region="westus2",
                    private_host="clouddb-6496f193.mysql.database.azure.com", port=3306,
                    db_name="app_db", connect_db_name=None, status="available",
                    source="provisioned")
    defaults.update(kw)
    return _CloudDatabase(**defaults)


_TF_VARS = {"administrator_login": "dbadmin", "administrator_password": "AdminPw1"}
_WESTUS2 = json.dumps({"westus2": {"resource_group": "sandbox-westus2-rg"}})


def _create(row=None, **conf):
    _reset(**conf)
    return _run(svc._create_db_managed_user_azure(
        _FakeDB(), row=row or _row(), job_id="job-1", engine="mysql",
        tf_variables=dict(_TF_VARS)))


# ── the fix ───────────────────────────────────────────────────────────────────

def test_the_db_client_runs_on_the_jump_vm_in_the_databases_own_region():
    ctx = _create(azure_region_configs=_WESTUS2)
    assert RUNS, "no Run Command was issued"
    groups = {rg for rg, _vm, _cmds in RUNS}
    assert groups == {"sandbox-westus2-rg"}, (
        f"the DB client ran in {groups} — the flat azure_resource_group is centralus's "
        "and its identically-named jump VM cannot resolve westus2's private DNS zone")
    assert ctx["resource_group"] == "sandbox-westus2-rg"
    assert ctx["region"] == "westus2"
    assert ctx["jump_vm_name"] == "clouddb-jumpoint"


def test_both_run_commands_land_on_the_same_host():
    """The prep leg (install the client, drop the plugin key material) and the client
    leg have to be the same VM: prep on one and the client on another leaves the plugin
    key on a host no rotation will ever run on."""
    _create(azure_region_configs=_WESTUS2)
    assert len(RUNS) == 2, RUNS
    assert RUNS[0][:2] == RUNS[1][:2]


def test_a_single_region_install_still_resolves_to_the_flat_resource_group():
    """The #1 requirement of per-region config sets: an install with no
    azure_region_configs at all behaves exactly as it did before they existed."""
    ctx = _create(row=_row(region="centralus"))
    assert {rg for rg, _vm, _cmds in RUNS} == {"dashboard-sandbox-rg"}
    assert ctx["resource_group"] == "dashboard-sandbox-rg"


def test_a_region_whose_entry_omits_the_group_falls_back_rather_than_blanking():
    ctx = _create(azure_region_configs=json.dumps({"westus2": {"db_subnet_id": "s"}}))
    assert ctx["resource_group"] == "dashboard-sandbox-rg"


def test_the_failure_names_the_vm_that_actually_ran_the_client():
    """`error_message` is the only detail a failed job shows, and the client's own
    error is about DNS: without the host, the operator cannot tell a broken database
    from a database being reached from the wrong region."""
    _reset(azure_region_configs=_WESTUS2)
    RUN_RESULTS.append({"status": "Success", "response_code": 0})
    RUN_RESULTS.append({
        "status": "Failed", "response_code": 1,
        "stderr": "ERROR 2005 (HY000): Unknown MySQL server host "
                  "'clouddb-6496f193.mysql.database.azure.com' (-2)"})
    try:
        _run(svc._create_db_managed_user_azure(
            _FakeDB(), row=_row(), job_id="job-1", engine="mysql",
            tf_variables=dict(_TF_VARS)))
    except Exception as exc:
        msg = str(exc)
    else:
        raise AssertionError("a failed Run Command must raise")
    assert "clouddb-jumpoint" in msg and "sandbox-westus2-rg" in msg and "westus2" in msg, msg
    assert "Unknown MySQL server host" in msg, "the remote detail must survive"


# ── the resolution itself ─────────────────────────────────────────────────────

def test_the_gateway_resource_group_has_one_reader():
    """The ensure path creates the VM in this group and callers address it in this
    group. Two copies of the resolution is how they came apart in the first place —
    the same warmer/reader drift rule the cache services follow."""
    _reset(azure_region_configs=_WESTUS2)
    assert jhs.azure_host_resource_group("westus2") == "sandbox-westus2-rg"
    assert jhs.azure_host_resource_group("West US 2") == "sandbox-westus2-rg", \
        "an operator-shaped region name must normalise like everywhere else"
    assert jhs.azure_host_resource_group("") == "dashboard-sandbox-rg", \
        "a blank region is the managed host's default location"
    src = open(_JHS, encoding="utf-8").read()
    body = src.split("def azure_host_resource_group", 1)[1].split("\nasync def ", 1)[0]
    others = [m for m in re.findall(r'resolve_region\("azure",[^\n]*resource_group', src)
              if m not in body]
    assert not others, f"resolve the gateway group through the helper, not: {others}"


def test_the_rows_region_beats_the_configured_default_region():
    """Flat-key-first made a region choice a no-op for everything but the Terraform
    apply. It read as harmless because Azure's composed key (`azure_region`) does not
    exist — but `aws_region` does, so an AWS database in a second region addressed the
    default region's Gateway host and SSM endpoints."""
    _reset()
    CONF["aws_region"] = "us-east-2"
    assert svc._row_region(_row(cloud="aws", region="us-west-2")) == "us-west-2"
    assert svc._row_region(_row(cloud="aws", region="")) == "us-east-2"
    assert svc._row_region(_row(cloud="azure", region="westus2")) == "westus2"
    assert svc._row_region(_row(cloud="azure", region="")) == "centralus"


def test_a_row_with_no_region_dimension_does_not_raise():
    """These call sites include a best-effort teardown, and an agent-sourced row's
    cloud is not one region_catalog knows."""
    _reset()
    assert svc._row_region(_row(cloud="onprem", region="dc1")) == "dc1"


def test_no_call_site_left_reading_the_flat_region_key_first():
    src = open(os.path.join(_ROOT, "web_dashboard", "services",
                            "cloud_database_service.py"), encoding="utf-8").read()
    assert 'row.cloud + "_region"' not in src, \
        "compose no per-cloud region key here — _row_region owns the ordering"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    print("OK" if not failures else f"{failures} failure(s)")
    sys.exit(1 if failures else 0)
