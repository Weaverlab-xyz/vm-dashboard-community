"""Pairing the configured Portainer with its portainer_access Entitle adapter.

The adapter itself — the Remote Adapter contract, the `jit-` blast-radius guards, the
matching rules — is pinned in test_portainer_adapter.py. What is pinned HERE is the
pairing that makes one reachable from the Portainer page, and specifically the places
where a mistake is expensive or silent:

  * the TARGET. A managed node has to be reached over the VPC at its internal IP: its
    firewall is fail-closed and a public function has no stable egress IP to admit, so
    a public adapter deploys green and then times out on every grant.
  * the TOKEN. It must only ever reach the function as a cloud secret REFERENCE. A
    plaintext environment value is leaked the instant the row and the Job are written.
  * the ARMING. Unset FN_PORTAINER_DRY_RUN means dry run, so "armed" has to be written
    explicitly and read back from the value actually deployed — not from its absence.
  * the RETIREMENT. What a pairing leaves behind is a live Portainer API token in a
    cloud secret store and a grantable integration in Entitle's catalogue.

Runs offline against stubs; no cloud and no Portainer needed. Under pytest or
standalone:
    python tests/test_portainer_adapter_pairing.py
"""
import asyncio
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONF = {}


def _stub(name, **attrs):
    """Install a fake module, and rebind it on its parent package too.

    The second half matters and is easy to miss: the service under test reaches its
    collaborators with ``from . import x``, which resolves by getattr on the package
    FIRST. A module this suite has already imported for real — secrets_backend_service
    is imported below, deliberately — stays bound there, so replacing only the
    sys.modules entry leaves the real one in play and the test hits a live cloud SDK.
    """
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    parent_name, _, leaf = name.rpartition(".")
    parent = sys.modules.get(parent_name)
    if parent is not None:
        setattr(parent, leaf, module)
    return module


# Stubbed unconditionally, not gated on whether a dependency happens to be installed:
# gating on that is how a suite passes locally and fails in CI.
_stub("web_dashboard.services.config_service",
      get=lambda key, default="": CONF.get(key, default),
      get_bool=lambda key, default=False: bool(CONF.get(key, default)),
      set=lambda key, value: CONF.__setitem__(key, value),
      delete=lambda key: CONF.pop(key, None))

JOBS = []


def _create_job(db, job_type="", created_by="", metadata=None):
    JOBS.append({"job_type": job_type, "created_by": created_by,
                 "metadata": metadata or {}})
    return types.SimpleNamespace(id=f"job-{len(JOBS)}")


PROGRESS = []
COMPLETED = {}
FAILED = {}

_stub("web_dashboard.services.job_service",
      create_job=_create_job,
      set_running=lambda db, job_id: None,
      update_progress=lambda db, job_id, pct, msg: PROGRESS.append((pct, msg)),
      set_completed=lambda db, job_id, result: COMPLETED.update(result),
      set_failed=lambda db, job_id, msg: FAILED.update({"msg": msg}))
_stub("web_dashboard.config",
      settings=types.SimpleNamespace(aws_region="us-east-1",
                                     gcp_project_id="lab-project"))
_stub("web_dashboard.database", CloudFunction=object, Job=object, SessionLocal=None)

from web_dashboard.services import portainer_adapter_service as adapter  # noqa: E402
from web_dashboard.services import secrets_backend_service as backends  # noqa: E402


def _ready(**over):
    """Config for an install that CAN pair: Portainer configured, token stored,
    Cloud Functions on."""
    CONF.clear()
    CONF.update({"portainer_enabled": True, "cloud_functions_enabled": True,
                 "portainer_url": "https://203.0.113.9:9443",
                 "portainer_pat": "ptr_abc123",
                 "portainer_verify_ssl": False,
                 "entitle_registration_enabled": True})
    CONF.update(over)


# ── Eligibility ───────────────────────────────────────────────────────────────
# One source of truth for the card's enabled state and for start_pairing, so the
# button can never offer what the endpoint refuses.

def test_a_ready_install_has_no_reason():
    _ready()
    assert adapter.ineligible_reason() is None


def test_each_missing_prerequisite_names_itself():
    for key, expect in (("portainer_url", "portainer_url"),
                        ("portainer_pat", "API token"),
                        ("cloud_functions_enabled", "Cloud Functions"),
                        ("portainer_enabled", "Portainer is disabled")):
        _ready(**{key: "" if "enabled" not in key else False})
        reason = adapter.ineligible_reason()
        assert reason, key
        assert expect in reason, (key, reason)


def test_the_token_is_a_prerequisite_not_an_afterthought():
    """The adapter authenticates to Portainer with the PAT and nothing else, so a
    pairing without one deploys a function that 401s on every call."""
    _ready(portainer_pat="")
    assert adapter.ineligible_reason()
    try:
        adapter.start_pairing(None, created_by="t")
    except adapter.AdapterPairingError:
        pass
    else:
        raise AssertionError("start_pairing queued a job with no API token")


def test_start_pairing_validates_before_queuing():
    """An impossible pairing has to fail at the click, not three minutes into a job."""
    _ready(portainer_url="")
    before = len(JOBS)
    try:
        adapter.start_pairing(None, created_by="t")
    except adapter.AdapterPairingError:
        pass
    assert len(JOBS) == before, "a job was queued for a pairing that cannot work"


# ── The environment the function is told about its target ─────────────────────

def test_the_adapter_is_armed_unless_asked_otherwise():
    """The workload treats unset OR truthy as dry run, so armed must be an explicit
    "0" — the same call the Databases page makes for db_grant."""
    env = adapter.build_environment(portainer_url="https://x:9443", verify_ssl=False)
    assert env["FN_PORTAINER_DRY_RUN"] == "0"


def test_dry_run_is_honoured_when_asked_for():
    env = adapter.build_environment(portainer_url="https://x:9443", verify_ssl=False,
                                    dry_run=True)
    assert env["FN_PORTAINER_DRY_RUN"] == "1"


def test_the_environment_is_pure():
    """Every value is passed in. Reading the target from config here is what would let
    a later config change silently redirect a deployed adapter."""
    CONF.clear()
    env = adapter.build_environment(portainer_url="https://only-source:9443",
                                    verify_ssl=True)
    assert env["FN_PORTAINER_URL"] == "https://only-source:9443"
    assert env["FN_PORTAINER_VERIFY_SSL"] == "1"


def test_a_blank_target_is_refused_rather_than_deployed():
    try:
        adapter.build_environment(portainer_url="", verify_ssl=True)
    except adapter.AdapterPairingError:
        return
    raise AssertionError("an adapter with no target was built")


def test_the_environment_never_carries_the_token():
    """The PAT is a secret_environment REFERENCE. In `environment` it would be leaked
    the instant the CloudFunction row and the Job are written."""
    env = adapter.build_environment(portainer_url="https://x:9443", verify_ssl=False)
    assert "FN_PORTAINER_API_KEY" not in env
    assert not any("ptr_" in str(v) for v in env.values())


def test_the_name_is_deterministic():
    """Portainer is a singleton here, so re-pairing must find the SAME function rather
    than accumulate one per attempt."""
    _stub("web_dashboard.services.cloud_function_service",
          normalize_name=lambda n: n.lower())
    assert adapter.adapter_name() == adapter.adapter_name() == "jit-portainer"


# ── Where the adapter points ──────────────────────────────────────────────────

def _managed_node(**over):
    node = {"name": "portainer-server", "status": "RUNNING",
            "internal_ip": "10.128.0.5", "external_ip": "203.0.113.9"}
    node.update(over)
    _stub("web_dashboard.services.managed_node_service",
          PORTAINER=object(),
          node_cloud=lambda spec: "gcp",
          resolve_placement=lambda cloud, spec, region=None, zone=None: {
              "account": "lab-project", "region": "us-central1", "name": "portainer-server"},
          list_nodes=_async(lambda cloud, spec, p: [node]))
    return node


def _async(fn):
    async def _inner(*a, **kw):
        return fn(*a, **kw)
    return _inner


def test_a_managed_node_is_reached_over_the_vpc_at_its_internal_ip():
    """The node firewall is fail-closed and a public function has no stable egress IP
    to admit, so the public URL is not an option — it deploys green and times out on
    every grant."""
    _ready()
    _managed_node()
    target = asyncio.run(adapter.resolve_target())
    assert target["url"] == "https://10.128.0.5:9443"
    assert target["network_mode"] == "vpc"
    assert target["via"] == "internal"
    assert target["managed"] is True
    # The node's certificate is issued for neither address, so verification cannot
    # succeed on this path and a failing handshake reads as an unreachable Portainer.
    assert target["verify_ssl"] is False
    # A VPC is regional: a function elsewhere could not attach to the node's network.
    assert target["cloud"] == "gcp" and target["region"] == "us-central1"


def test_an_unmanaged_portainer_uses_its_configured_url_and_the_callers_placement():
    _ready()
    _stub("web_dashboard.services.managed_node_service",
          PORTAINER=object(),
          node_cloud=lambda spec: "gcp",
          resolve_placement=lambda cloud, spec, region=None, zone=None: {"account": ""},
          list_nodes=_async(lambda cloud, spec, p: []))
    target = asyncio.run(adapter.resolve_target(cloud="aws", region="us-east-1",
                                                network_mode="public"))
    assert target["url"] == "https://203.0.113.9:9443"
    assert target["managed"] is False
    assert (target["cloud"], target["region"]) == ("aws", "us-east-1")
    assert target["via"] == "public"


def test_a_stopped_node_is_refused_rather_than_paired():
    """The Entitle registration preflight calls the adapter's own check_config, which
    reads the team list off Portainer — against a stopped node that surfaces as a
    broken adapter instead of a stopped node."""
    _ready()
    _managed_node(status="TERMINATED")
    try:
        asyncio.run(adapter.preflight())
    except adapter.AdapterPairingError as exc:
        assert "TERMINATED" in str(exc)
        return
    raise AssertionError("a pairing was allowed against a stopped node")


def test_an_unmanaged_portainer_without_a_placement_is_refused():
    """There is no node to take a cloud and region from, and deploy() needs both."""
    _ready()
    _stub("web_dashboard.services.managed_node_service",
          PORTAINER=object(), node_cloud=lambda spec: "gcp",
          resolve_placement=lambda cloud, spec, region=None, zone=None: {"account": ""},
          list_nodes=_async(lambda cloud, spec, p: []))
    for kwargs in ({"region": "us-east-1"}, {"cloud": "aws"}):
        try:
            asyncio.run(adapter.preflight(**kwargs))
        except adapter.AdapterPairingError:
            continue
        raise AssertionError(f"preflight accepted a half placement: {kwargs}")


# ── The staged token ──────────────────────────────────────────────────────────

def test_the_token_is_staged_as_a_reference_per_cloud():
    _ready()
    _stub("web_dashboard.services.secrets_backend_service",
          write_sync=lambda backend, key, value: f"dashboard-{key}",
          ref_for=lambda backend, key: f"dashboard-{key}",
          delete_sync=lambda backend, ref: None)
    for cloud in ("gcp", "azure"):
        out = adapter._stage_pat_secret(cloud)
        assert list(out) == ["FN_PORTAINER_API_KEY"]
        # A REFERENCE, never the value.
        assert out["FN_PORTAINER_API_KEY"] != CONF["portainer_pat"]
        assert "ptr_" not in out["FN_PORTAINER_API_KEY"]


def test_aws_stages_an_arn_not_a_name():
    """The execution role's policy names ARNs, and AWS appends a random six-character
    suffix to every secret — so the name cannot be turned into one by concatenation."""
    _ready()
    _stub("web_dashboard.services.secrets_backend_service",
          write_sync=lambda backend, key, value: f"dashboard/{key}",
          ref_for=lambda backend, key: f"dashboard/{key}",
          delete_sync=lambda backend, ref: None)
    adapter._aws_secret_arn = lambda name: (
        f"arn:aws:secretsmanager:us-east-1:1234:secret:{name}-Ab12Cd")
    out = adapter._stage_pat_secret("aws")
    assert out["FN_PORTAINER_API_KEY"].startswith("arn:aws:secretsmanager:")


def test_a_broken_secret_store_names_the_stage_and_the_panel():
    """The job detail view shows error_message and nothing else, so a bare SDK error
    here reads as a broken Portainer rather than an unconfigured secret store."""
    _ready()

    def _boom(backend, key, value):
        raise RuntimeError("PermissionDenied on secretmanager.googleapis.com")

    _stub("web_dashboard.services.secrets_backend_service", write_sync=_boom,
          ref_for=lambda backend, key: key, delete_sync=lambda backend, ref: None)
    try:
        adapter._stage_pat_secret("gcp")
    except adapter.AdapterPairingError as exc:
        assert "GCP Secret Manager" in str(exc)
        assert "Portainer API token" in str(exc)
        return
    raise AssertionError("a secret-store failure was not surfaced")


def test_an_unsupported_cloud_has_no_secret_backend():
    _ready()
    try:
        adapter._stage_pat_secret("oci")
    except adapter.AdapterPairingError as exc:
        assert "oci" in str(exc)
        return
    raise AssertionError("a cloud with no secret store was accepted")


# ── Retiring the staged token ─────────────────────────────────────────────────
# The REAL secrets_backend_service here: reproducing the writer's key mangling by hand
# is exactly how a delete quietly targets a name nothing was written under.

def test_the_delete_is_addressed_at_the_writers_ref_not_the_key():
    seen = {}
    real_ref_for = backends.ref_for
    _stub("web_dashboard.services.secrets_backend_service",
          ref_for=real_ref_for,
          delete_sync=lambda backend, ref: seen.update({"ref": ref}),
          write_sync=lambda backend, key, value: real_ref_for(backend, key))
    ref = adapter.retire_pat_secret("gcp")
    assert seen["ref"] == ref == real_ref_for("gcp_sm", "portainer-adapter-pat")
    # And that really is a mangled form, not the key echoed back.
    assert seen["ref"] != "portainer-adapter-pat"


def test_an_absent_secret_is_a_no_op_not_a_failure():
    """A Portainer that was never paired has no staged token, and that is the normal
    outcome — for every cloud's flavour of "it isn't there"."""
    class _GoogleNotFound(Exception):
        code = 404

    class _AzureNotFound(Exception):
        status_code = 404

    class _AwsMissing(Exception):
        response = {"Error": {"Code": "ResourceNotFoundException"}}

    for exc_cls, cloud in ((_GoogleNotFound, "gcp"), (_AzureNotFound, "azure"),
                           (_AwsMissing, "aws")):
        def _raise(backend, ref, _e=exc_cls):
            raise _e("nope")

        _stub("web_dashboard.services.secrets_backend_service",
              ref_for=backends.ref_for, delete_sync=_raise,
              write_sync=lambda backend, key, value: key)
        assert adapter.retire_pat_secret(cloud) == "", cloud


def test_a_real_delete_failure_is_raised_with_the_ref_named():
    """What is left behind is a live Portainer API token. An operator who has to remove
    it by hand needs to be told what to remove."""
    def _raise(backend, ref):
        raise RuntimeError("403 Forbidden")

    _stub("web_dashboard.services.secrets_backend_service",
          ref_for=backends.ref_for, delete_sync=_raise,
          write_sync=lambda backend, key, value: key)
    try:
        adapter.retire_pat_secret("gcp")
    except adapter.AdapterPairingError as exc:
        assert "portainer-adapter-pat" in str(exc)
        assert "by hand" in str(exc)
        return
    raise AssertionError("a leaked credential was swallowed")


def test_a_cloud_with_no_backend_retires_nothing_quietly():
    assert adapter.retire_pat_secret("oci") == ""


# ── Status, as the card reads it ──────────────────────────────────────────────

class _FakeFn:
    def __init__(self, **kw):
        self.id = "fn-1"
        self.status = "available"
        self.cloud = "gcp"
        self.region = "us-central1"
        self.network_mode = "vpc"
        self.invoke_url = "https://fn.example/"
        self.entitle_integration_id = "int-9"
        self.env_ref = '{"FN_PORTAINER_URL": "https://10.128.0.5:9443", ' \
                       '"FN_PORTAINER_DRY_RUN": "0"}'
        for k, v in kw.items():
            setattr(self, k, v)


def _fnsvc(found=None, **extra):
    attrs = dict(normalize_name=lambda n: n.lower(),
                 find_by_names=lambda db, names, workload="": (
                     {names[0]: found} if found is not None else {}))
    attrs.update(extra)
    _stub("web_dashboard.services.cloud_function_service", **attrs)


def test_status_reports_no_adapter_without_inventing_one():
    _ready()
    _fnsvc()
    st = adapter.status(None)
    assert st["fn_id"] == "" and st["viable"] is True
    assert st["ineligible_reason"] == ""
    # The card does `.length` on this before the first fetch resolves.
    assert isinstance(st["source_cidrs"], list)


def test_status_surfaces_the_deployed_adapter():
    _ready()
    _fnsvc(found=_FakeFn())
    st = adapter.status(None)
    assert st["fn_id"] == "fn-1"
    assert st["entitle_integration_id"] == "int-9"
    assert st["target_url"] == "https://10.128.0.5:9443"
    assert st["dry_run"] is False


def test_a_missing_dry_run_setting_reads_as_dry_run():
    """The workload's default is dry run, so absence must NOT read as armed — a card
    that says ARMED over a no-op adapter is the failure this closes."""
    _ready()
    _fnsvc(found=_FakeFn(env_ref='{"FN_PORTAINER_URL": "https://x:9443"}'))
    assert adapter.status(None)["dry_run"] is True


def test_a_corrupt_env_ref_does_not_take_the_card_down():
    _ready()
    _fnsvc(found=_FakeFn(env_ref="not json"))
    st = adapter.status(None)
    assert st["fn_id"] == "fn-1" and st["target_url"] == ""


def test_status_carries_the_entitle_flag_so_the_card_can_explain_itself():
    """An adapter with no integration means two different things — registration is
    switched off, or the registration failed — and the remedies differ."""
    _ready(entitle_registration_enabled=False)
    _fnsvc(found=_FakeFn(entitle_integration_id=""))
    st = adapter.status(None)
    assert st["entitle_enabled"] is False and st["entitle_integration_id"] == ""


# ── The job ───────────────────────────────────────────────────────────────────

def test_the_pairing_refuses_a_duplicate_rather_than_redeploying():
    """deploy() does not look the name up and every deploy starts from an empty
    Terraform directory, so a second pairing wedges a duplicate row in 'deploying'
    behind an "already exists" apply failure."""
    _ready()
    _managed_node()
    FAILED.clear()
    _fnsvc(found=_FakeFn(),
           _resolved_network=lambda *a, **kw: {"vpc_subnetwork": "default"})
    asyncio.run(adapter.run_job(None, job_id="j", meta={"action": "pair"}))
    assert "already has the adapter" in FAILED.get("msg", "")


def test_the_entitle_leg_is_the_only_skippable_one():
    """An adapter that is deployed and pointed at its Portainer is useful on its own,
    so a disabled Entitle integration must not make the card dead weight on exactly
    the installs still being set up."""
    _ready(entitle_registration_enabled=False)
    _managed_node()
    COMPLETED.clear()
    FAILED.clear()
    PROGRESS.clear()
    deployed = _FakeFn(entitle_integration_id="")
    calls = []

    def _deploy(db, **kw):
        calls.append(kw)
        return {"fn_id": "fn-1", "job_id": "j2", "tf_variables": {}}

    _fnsvc(_resolved_network=lambda *a, **kw: {"vpc_subnetwork": "default"},
           deploy=_deploy,
           run_deploy_apply=_async(lambda db, **kw: None),
           get_function=lambda db, fn_id: deployed,
           start_entitle_register=lambda *a, **kw: (_ for _ in ()).throw(
               AssertionError("Entitle was called with registration disabled")))
    _stub("web_dashboard.services.secrets_backend_service",
          write_sync=lambda backend, key, value: f"dashboard-{key}",
          ref_for=lambda backend, key: f"dashboard-{key}",
          delete_sync=lambda backend, ref: None)
    _stub("web_dashboard.services.portainer_node_service",
          refresh_portainer_firewall=_async(lambda db=None, placement=None: {}))
    _stub("web_dashboard.services.gcp_service",
          get_network_options=_async(lambda project, region, zone: {
              "subnets": [{"name": "default", "ip_cidr_range": "10.128.0.0/20"}]}))

    asyncio.run(adapter.run_job(None, job_id="j", meta={"action": "pair"}))
    assert not FAILED, FAILED
    assert COMPLETED["entitle_skipped"] is True
    assert COMPLETED["entitle_integration_id"] == ""
    assert COMPLETED["dry_run"] is False, "the button deploys ARMED"
    # The token reached deploy() as a secret_environment reference and nothing else.
    assert "FN_PORTAINER_API_KEY" in calls[0]["secret_environment"]
    assert "FN_PORTAINER_API_KEY" not in calls[0]["environment"]
    assert calls[0]["network_mode"] == "vpc"
    # And the node firewall was opened to the function's own subnet, without which
    # every grant would time out against the internal IP.
    assert COMPLETED["firewall_opened_to"] == ["10.128.0.0/20"]
    assert CONF[adapter.SOURCE_CIDR_KEY] == "10.128.0.0/20"


def test_the_firewall_is_opened_before_entitle_is_told_about_the_adapter():
    """Registration preflights the adapter's check_config route, which talks to
    Portainer. A closed firewall surfaces there as "Portainer is unreachable" and
    leaves the operator debugging the wrong half."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services",
                            "portainer_adapter_service.py"), encoding="utf-8").read()
    body = src.split("async def _run_pair(")[1]
    assert body.index("_open_firewall_to_adapter") < body.index("start_entitle_register")


def test_an_unresolvable_subnet_leaves_the_firewall_alone_rather_than_failing():
    """A pairing that produced a working function should not fail on the firewall step;
    portainer_allowed_source_cidrs stays the manual way in."""
    _ready()
    _managed_node()
    CONF.pop(adapter.SOURCE_CIDR_KEY, None)
    _stub("web_dashboard.services.gcp_service",
          get_network_options=_async(lambda project, region, zone: {"subnets": []}))
    _stub("web_dashboard.services.portainer_node_service",
          refresh_portainer_firewall=_async(lambda db=None, placement=None: {}))
    opened = asyncio.run(adapter._open_firewall_to_adapter(
        None, {"managed": True, "cloud": "gcp", "region": "us-central1",
               "network": {"vpc_subnetwork": "missing"}}))
    assert opened == []
    assert not CONF.get(adapter.SOURCE_CIDR_KEY)


def test_the_firewall_refresh_is_told_which_region_to_apply_to():
    """refresh_portainer_firewall with no placement falls back to the DEFAULT region on
    AWS/Azure, and would apply the allow-list to that region's VPC while the node and
    the adapter are somewhere else — a firewall reported as opened, on the wrong
    network."""
    _ready()
    _managed_node()
    seen = {}

    async def _refresh(db=None, placement=None):
        seen["placement"] = placement
        return {}

    _stub("web_dashboard.services.portainer_node_service",
          refresh_portainer_firewall=_refresh)
    _stub("web_dashboard.services.gcp_service",
          get_network_options=_async(lambda project, region, zone: {
              "subnets": [{"name": "default", "ip_cidr_range": "10.128.0.0/20"}]}))
    asyncio.run(adapter._open_firewall_to_adapter(
        None, {"managed": True, "cloud": "gcp", "region": "us-central1",
               "network": {"vpc_subnetwork": "default"}}))
    assert seen["placement"] is not None, "the firewall would target the default region"
    assert seen["placement"]["region"] == "us-central1", seen


def test_a_firewall_that_cannot_be_opened_fails_the_pairing():
    """Warning and carrying on would register an integration whose every grant times
    out, and the timeout surfaces in Entitle rather than here."""
    _ready()
    _managed_node()

    def _boom(db=None, placement=None):
        raise RuntimeError("403 on compute.firewalls.update")

    async def _refresh(db=None, placement=None):
        return _boom()

    _stub("web_dashboard.services.portainer_node_service",
          refresh_portainer_firewall=_refresh)
    _stub("web_dashboard.services.gcp_service",
          get_network_options=_async(lambda project, region, zone: {
              "subnets": [{"name": "default", "ip_cidr_range": "10.128.0.0/20"}]}))
    try:
        asyncio.run(adapter._open_firewall_to_adapter(
            None, {"managed": True, "cloud": "gcp", "region": "us-central1",
                   "network": {"vpc_subnetwork": "default"}}))
    except adapter.AdapterPairingError as exc:
        assert "10.128.0.0/20" in str(exc), "name the range so it can be added by hand"
        assert "portainer_allowed_source_cidrs" in str(exc)
        return
    raise AssertionError("a closed firewall was reported as a successful pairing")


def test_an_unmanaged_portainer_has_no_firewall_of_ours_to_change():
    opened = asyncio.run(adapter._open_firewall_to_adapter(
        None, {"managed": False, "cloud": "aws", "region": "us-east-1",
               "network": {}}))
    assert opened == []


# ── Retiring the adapter ──────────────────────────────────────────────────────

def test_retiring_a_portainer_that_was_never_paired_still_clears_the_firewall_entry():
    """A pairing that failed after the firewall step leaves a range in the node's
    allow-list with no function to find."""
    _ready()
    CONF[adapter.SOURCE_CIDR_KEY] = "10.128.0.0/20"
    _fnsvc()
    assert asyncio.run(adapter.retire_adapter(None)) == ""
    assert CONF[adapter.SOURCE_CIDR_KEY] == ""


def test_the_integration_goes_before_the_function():
    """Shut the tap, then drain: an integration left in Entitle's catalogue stays
    grantable and errors on every request."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services",
                            "portainer_adapter_service.py"), encoding="utf-8").read()
    body = src.split("async def retire_adapter(")[1]
    assert body.index("start_entitle_register") < body.index("start_decommission")
    # ...and the staged token goes last, once nothing reads it any more.
    assert body.index("start_decommission") < body.index("retire_pat_secret")


def test_a_failed_entitle_removal_does_not_skip_the_destroy():
    """The function is the part that costs money either way, and it can only fail now."""
    _ready()
    destroyed = []
    fn = _FakeFn()

    def _start_ent(db, fn_id, action="", created_by=""):
        raise RuntimeError("Entitle unreachable")

    def _start_dec(db, fn_id, created_by=""):
        destroyed.append(fn_id)
        return {"job_id": "j3"}

    def _run_dec(db, fn_id="", job_id=""):
        fn.status = "deleted"

    _fnsvc(found=fn, start_entitle_register=_start_ent,
           start_decommission=_start_dec,
           run_decommission=_async(_run_dec))
    _stub("web_dashboard.services.secrets_backend_service",
          ref_for=backends.ref_for, delete_sync=lambda backend, ref: None,
          write_sync=lambda backend, key, value: key)
    _stub("web_dashboard.services.portainer_node_service",
          refresh_portainer_firewall=_async(lambda db=None, placement=None: {}))

    class _Db:
        def refresh(self, row):
            pass

    try:
        asyncio.run(adapter.retire_adapter(_Db()))
    except adapter.AdapterPairingError as exc:
        assert "Entitle" in str(exc)
    assert destroyed == ["fn-1"], "the destroy was skipped when Entitle failed"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
