"""The plant's function runtime, from the dashboard's side (ot_faas_service).

This module puts an Entitle adapter on the OT broker's OpenFaaS runtime and registers
it. Nothing about it can be checked at runtime here — the broker is a private host in
a subnet whose only ingress is the PRA Gateway — so what is pinned is the set of
properties whose failure is either silent or expensive:

* **no credential reaches job metadata.** ``extra_vars`` is persisted to the job row,
  so it may carry only the NAMES of bound variables; the values ride ``secret_vars``,
  which the runner resolves at run time. The real ``ansible_run_meta`` is used rather
  than a stub, because that round-trip through its closed allowlist IS the property.
* **the integration is agent-brokered, against THIS cell's agent.** ``private=True``
  is what lets the endpoint be a name that resolves only inside the plant; the wrong
  agent name would register an integration another plant's agent would try to serve.
* **the package fits the channel it travels through.** It goes as base64 inside one
  ``--extra-vars`` argv element, and Linux caps that at MAX_ARG_STRLEN — past it the
  run dies with an unhelpful E2BIG, so the refusal has to happen here with a remedy.
* **the shared secret's env var and the Secret it mounts from agree.** They are
  computed in two places (here and the play) and a mismatch is a function that fails
  closed with a 500 for a reason nobody can see.
* **teardown keeps the state when it cannot use it.** The Terraform state is the only
  handle on a tenant-side integration; dropping it on failure strands the integration.

Run: python tests/test_ot_faas_service.py   (or under pytest)
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

CONF = {}
CREATED_JOBS = []
META = {}
REGISTER_CALLS = []
DEREGISTER_CALLS = []
_FAIL_DEREGISTER = []


def _mod(name, **attrs):
    module = types.ModuleType(name)
    for key, val in attrs.items():
        setattr(module, key, val)
    sys.modules[name] = module
    return module


class _Job:
    def __init__(self, job_id, created_by="tester"):
        self.id = job_id
        self.created_by = created_by


def _create_job(db, *, job_type, created_by, workgroup, metadata):
    job = _Job(f"job-{len(CREATED_JOBS) + 1}", created_by)
    CREATED_JOBS.append({"job_type": job_type, "created_by": created_by,
                         "workgroup": workgroup, "metadata": metadata, "id": job.id})
    return job


def _update_metadata(db, job_id, values):
    META.setdefault(job_id, {}).update(values)


class _Ctx:
    def __init__(self, **hcl):
        self.hcl = dict(hcl)


async def _register_rest(**kwargs):
    REGISTER_CALLS.append(kwargs)
    return {"integration_id": "int-123", "outputs": {},
            "tf_state_json": '{"resources": []}'}


async def _deregister(state, ctx=None):
    DEREGISTER_CALLS.append({"state": state, "ctx": ctx})
    if _FAIL_DEREGISTER:
        raise RuntimeError("entitle said no")


def _install_stubs():
    _mod("web_dashboard.config", settings=types.SimpleNamespace(), Settings=object)
    _mod("web_dashboard.services.config_service",
         get=lambda key, default="", workgroup=None: CONF.get(key, default),
         get_bool=lambda key, default=False: bool(CONF.get(key, default)),
         set=lambda key, value, workgroup=None: CONF.__setitem__(key, value),
         delete=lambda key, workgroup=None: CONF.pop(key, None))
    _mod("web_dashboard.services.job_service",
         create_job=_create_job,
         update_metadata=_update_metadata,
         update_progress=lambda db, job_id, pct, msg: None,
         get_job=lambda db, job_id: _Job(job_id, "tester"))
    _mod("web_dashboard.services.storage_service",
         active_backend=lambda: "local")
    _mod("web_dashboard.services.entitle_registration_service",
         register_rest=_register_rest,
         deregister=_deregister,
         local_tenant_ctx=lambda **kw: _Ctx(**kw))
    _mod("web_dashboard.services.ot_service", agent_token_name=lambda name: f"ot-{name}")


_install_stubs()

# The REAL ones: ansible_run_meta is pure and stdlib-only (its own docstring says the
# round-trip is meant to be testable without FastAPI or a database), and the packager
# builds a real zip. Stubbing either would test the stub.
from web_dashboard.services import ansible_run_meta  # noqa: E402
from web_dashboard.services import cloud_function_package as pkg  # noqa: E402
from web_dashboard.services import ot_faas_service as svc  # noqa: E402
from web_dashboard.functions.fnruntime import secretref  # noqa: E402


def _reset(**conf):
    CONF.clear()
    CONF.update({"ot_faas_enabled": True})
    CONF.update(conf)
    CREATED_JOBS.clear()
    META.clear()
    REGISTER_CALLS.clear()
    DEREGISTER_CALLS.clear()
    _FAIL_DEREGISTER.clear()


def _cmeta(**over):
    meta = {"instance_name": "ot-cell-01",
            "ot_broker_job_id": "broker-1",
            "ot_agent_token_name": "ot-ot-cell-01",
            "private_ip": "10.0.0.5"}
    meta.update(over)
    return meta


def _run(coro):
    return asyncio.run(coro)


# ── The duplication guard ────────────────────────────────────────────────────

def test_the_file_suffix_matches_secretrefs_own_convention():
    """``ot_faas_service`` spells the ``_FILE`` suffix rather than importing
    ``secretref.file_env_for``, because that package is zip material and importing it
    mutates sys.path in the dashboard process. This is the assertion that makes the
    duplication safe."""
    assert (svc.SHARED_SECRET_ENV + svc.SECRET_FILE_SUFFIX
            == secretref.file_env_for(svc.SHARED_SECRET_ENV)), (
        "the service's file-variable spelling has drifted from secretref's, so the "
        "function would look for its secret under a name nothing sets")


def test_the_mount_dir_is_where_openfaas_actually_puts_a_secret():
    assert svc.SECRET_MOUNT_DIR == "/var/openfaas/secrets"
    path = svc.secret_file_path("abcd1234efgh", svc.SHARED_SECRET_ENV)
    assert path.startswith(svc.SECRET_MOUNT_DIR + "/")
    assert path.endswith(svc.secret_name("abcd1234efgh", svc.SHARED_SECRET_ENV))


# ── Names and keys ───────────────────────────────────────────────────────────

def test_the_endpoint_resolves_only_inside_the_plant():
    _reset()
    url = svc.base_url()
    assert url.startswith("http://gateway.openfaas.svc.cluster.local:8080/function/"), url
    assert svc.FUNCTION_NAME in url
    # Not a public name and not an address: that is the property that lets Entitle be
    # told about it at all without opening a path into the plant.
    assert "://localhost" not in url and not url.rstrip("/").endswith(".io")


def test_the_endpoint_is_overridable_so_the_runtime_can_be_swapped():
    _reset(ot_faas_base_url="http://ot-adapter.ot-jit.svc.cluster.local:8080")
    assert svc.base_url() == "http://ot-adapter.ot-jit.svc.cluster.local:8080", (
        "the base URL is not overridable, so swapping OpenFaaS for Nuclio or a plain "
        "Deployment — the answer to the CE licence ceiling — would need a code change")


def test_the_bearer_key_is_this_cells_own():
    key = svc.bearer_config_key("vmjob-9")
    assert key == "ot/vmjob-9/faas_bearer", key
    assert svc.bearer_config_key("a") != svc.bearer_config_key("b"), (
        "one shared bearer would mean every cell's adapter could be called with every "
        "other cell's credential")


def test_a_secret_name_is_a_dns_label():
    name = svc.secret_name("ABCD1234efgh5678", svc.SHARED_SECRET_ENV)
    assert name.islower() and "_" not in name, name
    assert all(c.isalnum() or c == "-" for c in name), name
    assert not name.startswith("-") and not name.endswith("-"), name
    # Scoped to the cell, so two cells on one cluster could not collide.
    assert svc.secret_name("aaaa", "FN_X") != svc.secret_name("bbbb", "FN_X")


def test_the_bearer_is_minted_once_and_read_back():
    _reset()
    first = svc.ensure_bearer("vmjob-1")
    assert len(first) > 20, first
    assert svc.ensure_bearer("vmjob-1") == first, (
        "a second call minted a new bearer, so a re-run of the play would write a "
        "credential the already-registered integration does not present")
    assert CONF[svc.bearer_config_key("vmjob-1")] == first


# ── The package ──────────────────────────────────────────────────────────────

def test_the_package_is_built_for_the_openfaas_target():
    _reset()
    encoded, sha, name = svc.build_package()
    assert name == svc.DEFAULT_WORKLOAD == "entitle_webhook_echo", (
        "the default workload should be the no-op reference adapter, so the whole "
        "path is provable before a target-specific adapter exists")
    import base64
    import hashlib
    import io
    import zipfile
    blob = base64.b64decode(encoded)
    assert hashlib.sha256(blob).hexdigest() == sha, (
        "the digest does not describe the bytes — the loader refuses on mismatch, so "
        "this would be a pod that never starts")
    names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
    assert "openfaas_entry.py" in names and "workload.py" in names, names
    assert "fnruntime/secretref.py" in names, (
        "without secretref the shared secret cannot be read from its mounted file")


def test_the_package_fits_the_channel_it_travels_through():
    _reset()
    encoded, _sha, _name = svc.build_package()
    assert len(encoded) < svc.PACKAGE_BUDGET_B64 < svc.MAX_ARG_STRLEN, (
        f"{len(encoded)} b64 bytes against a {svc.PACKAGE_BUDGET_B64} budget and a "
        f"{svc.MAX_ARG_STRLEN} kernel limit")


def test_an_oversized_package_is_refused_with_somewhere_to_go():
    _reset()
    original = svc.PACKAGE_BUDGET_B64
    try:
        svc.PACKAGE_BUDGET_B64 = 100
        try:
            svc.build_package()
            raise AssertionError("an oversized package was accepted")
        except svc.OTFaasError as exc:
            message = str(exc)
    finally:
        svc.PACKAGE_BUDGET_B64 = original
    assert "argv" in message or "MAX_ARG_STRLEN" in message or "single argv" in message, (
        f"the refusal does not say WHY there is a limit: {message}")
    assert "mounted Secret" in message, (
        f"the refusal names no route out, so the operator is just stuck: {message}")


def test_an_unknown_workload_is_refused_by_name():
    _reset()
    try:
        svc.build_package("no_such_workload")
        raise AssertionError("an unknown workload was accepted")
    except svc.OTFaasError as exc:
        assert "no_such_workload" in str(exc)
        assert "fnworkloads" in str(exc), "the refusal does not say where workloads live"


# ── Refusals ─────────────────────────────────────────────────────────────────

def test_every_refusal_names_a_remedy():
    _reset()
    cases = {
        "the feature is off": ({"ot_faas_enabled": False}, _cmeta()),
        "no broker": ({}, _cmeta(ot_broker_job_id="")),
        "no agent token": ({}, _cmeta(ot_agent_token_name="")),
    }
    for label, (conf, cmeta) in cases.items():
        _reset(**conf)
        reason = svc.skip_reason(cmeta)
        assert reason, f"{label}: no refusal at all"
        assert len(reason.split()) > 8, f"{label}: refusal is too terse to act on: {reason}"
    _reset()
    assert svc.skip_reason(_cmeta()) == "", "a complete cell was refused"


def test_a_missing_agent_token_is_refused_here_not_by_terraform():
    """``_common_attrs_hcl(private=True)`` RAISES without an agent name. A Terraform
    traceback is a worse answer than a sentence, so this is caught first."""
    _reset()
    reason = svc.skip_reason(_cmeta(ot_agent_token_name=""))
    assert "agent" in reason.lower() and "re-wire" in reason.lower(), reason


def test_a_broker_with_no_address_is_refused_before_a_job_is_queued():
    _reset()
    assert svc.skip_reason(_cmeta(), {"private_ip": ""}), (
        "a broker with no address would queue a run against an empty target")
    assert svc.skip_reason(_cmeta(), {"private_ip": "10.0.0.9"}) == ""


# ── The deploy, and the secret boundary ──────────────────────────────────────

def _queue():
    cmeta = _cmeta()
    note = _run(svc.queue_deploy(None, "parent-1", "child-1", cmeta,
                                 broker_id="broker-1",
                                 bmeta={"private_ip": "10.0.0.9",
                                        "instance_name": "ot-cell-01-dmz"}))
    return cmeta, note, CREATED_JOBS[-1] if CREATED_JOBS else None


def test_the_deploy_queues_an_ansible_local_run_against_the_broker():
    _reset()
    cmeta, note, job = _queue()
    assert job is not None, note
    assert job["job_type"] == "ansible_local", job["job_type"]
    assert job["metadata"]["target"] == "10.0.0.9"
    assert job["metadata"]["asset"] == svc.FAAS_DEPLOY_PLAYBOOK
    assert "queued" in note


def test_no_credential_reaches_job_metadata():
    """The property, and the reason the real ansible_run_meta is used here.

    ``extra_vars`` is persisted to the job row. The bearer must appear in it nowhere —
    only the NAME of the variable it is bound to, with the VALUE reachable solely
    through ``secret_vars``' config-key reference, which the runner resolves at run
    time and adds to the scrub list.
    """
    _reset()
    bearer = svc.ensure_bearer("child-1")
    cmeta, _note, job = _queue()
    meta = job["metadata"]
    # It round-tripped through the closed allowlist, so this is the real shape.
    assert set(meta) <= set(ansible_run_meta.RUN_META_KEYS) | {"description",
                                                               "asset_backend"}, meta
    blob = repr(meta["extra_vars"])
    assert bearer not in blob, "the bearer VALUE is in extra_vars, which is persisted"
    assert bearer not in repr(meta), "the bearer VALUE is somewhere in job metadata"
    assert meta["secret_vars"] == {"otfn_bearer": svc.bearer_config_key("child-1")}, (
        "the bearer is not bound by reference through secret_vars")
    # And the play is told which variable to harvest, by name.
    assert "otfn_bearer" in meta["extra_vars"]["otfn_secrets"].values()


def test_the_gate_is_pointed_at_the_file_the_secret_mounts_to():
    """The env var and the Secret name are computed in two places — here and the play —
    and a mismatch is a function that fails closed with a 500 nobody can explain."""
    _reset()
    _cm, _note, job = _queue()
    extra = job["metadata"]["extra_vars"]
    want_name = svc.secret_name("child-1", svc.SHARED_SECRET_ENV)
    assert want_name in extra["otfn_secrets"], extra["otfn_secrets"]
    file_var = svc.SHARED_SECRET_ENV + svc.SECRET_FILE_SUFFIX
    assert extra["otfn_env"][file_var] == f"{svc.SECRET_MOUNT_DIR}/{want_name}", (
        "the env var points somewhere the Secret is not mounted")


def test_the_deploy_records_its_artifacts_on_the_cell_row():
    _reset()
    cmeta, _note, job = _queue()
    for key in ("ot_faas_job_id", "ot_faas_workload", "ot_faas_package_sha256",
                "ot_faas_base_url"):
        assert cmeta.get(key), f"{key} was not recorded on the cell"
        assert META["child-1"].get(key) == cmeta[key], f"{key} not persisted"
    # The cell row is the one the destroy sweep and the expiry reaper read.
    assert "ot_faas_job_id" in META["broker-1"], (
        "the broker row does not reference the run, so its job page is unfindable")


def test_a_second_deploy_is_a_no_op_not_a_second_job():
    _reset()
    cmeta, _note, _job = _queue()
    again = _run(svc.queue_deploy(None, "parent-1", "child-1", cmeta,
                                  broker_id="broker-1",
                                  bmeta={"private_ip": "10.0.0.9"}))
    assert len(CREATED_JOBS) == 1, "a second run was queued over a live one"
    assert "already queued" in again, again


def test_a_refused_cell_queues_nothing():
    _reset(ot_faas_enabled=False)
    note = _run(svc.queue_deploy(None, "p", "c", _cmeta(), broker_id="b",
                                 bmeta={"private_ip": "10.0.0.9"}))
    assert CREATED_JOBS == [], "a refused deploy still queued a job"
    assert "skipped" in note and "ot_faas_enabled" in note, note


def test_the_probe_only_run_carries_no_package_and_no_secret():
    _reset()
    job_id = _run(svc.queue_probe(None, "child-1", _cmeta(),
                                  {"private_ip": "10.0.0.9"}))
    assert job_id
    meta = CREATED_JOBS[-1]["metadata"]
    assert meta["extra_vars"].get("otfn_probe_only") is True
    assert "otfn_pkg_b64" not in meta["extra_vars"], (
        "the probe carries a package, so it is not safe to run against a live grant")
    assert meta["secret_vars"] == {}, "the probe carries a credential it does not need"


# ── Registration ─────────────────────────────────────────────────────────────

def test_registration_is_agent_brokered_against_this_cells_own_agent():
    _reset()
    svc.ensure_bearer("child-1")
    cmeta = _cmeta(ot_faas_base_url=svc.base_url())
    note = _run(svc.register(None, "child-1", cmeta))
    assert REGISTER_CALLS, note
    call = REGISTER_CALLS[0]
    assert call["private"] is True, (
        "private=False would make Entitle call the endpoint itself — and it is a name "
        "that resolves only inside the plant")
    assert call["ephemeral"] is True
    assert call["base_url"] == svc.base_url()
    assert call["shared_secret"] == CONF[svc.bearer_config_key("child-1")]
    assert call["ctx"].hcl["agent_token_name"] == "ot-ot-cell-01", (
        "the wrong agent name registers an integration another plant's agent would "
        "try to serve")
    assert "agent-brokered" in note


def test_registration_records_the_state_and_is_then_idempotent():
    _reset()
    svc.ensure_bearer("child-1")
    cmeta = _cmeta()
    _run(svc.register(None, "child-1", cmeta))
    assert cmeta["ot_faas_entitle_tf_state"], "no state recorded"
    assert cmeta["ot_faas_entitle_integration_id"] == "int-123"
    assert META["child-1"]["ot_faas_entitle_tf_state"]
    again = _run(svc.register(None, "child-1", cmeta))
    assert len(REGISTER_CALLS) == 1, (
        "Re-wire registered a second integration nobody is tracking")
    assert "already registered" in again


def test_registration_refuses_without_the_bearer_it_would_announce():
    _reset()
    try:
        _run(svc.register(None, "child-1", _cmeta()))
        raise AssertionError("registered with no bearer at all")
    except svc.OTFaasError as exc:
        assert "bearer" in str(exc).lower() and "deploy" in str(exc).lower(), str(exc)
    assert REGISTER_CALLS == []


def test_a_registration_failure_says_the_function_is_unaffected():
    _reset()
    svc.ensure_bearer("child-1")
    entitle = sys.modules["web_dashboard.services.entitle_registration_service"]
    original = entitle.register_rest

    async def _boom(**kwargs):
        raise RuntimeError("tenant unreachable")

    entitle.register_rest = _boom
    try:
        _run(svc.register(None, "child-1", _cmeta()))
        raise AssertionError("a failing registration did not raise")
    except svc.OTFaasError as exc:
        assert "unaffected" in str(exc), (
            f"the operator is not told the function survived: {exc}")
    finally:
        entitle.register_rest = original


# ── Teardown ─────────────────────────────────────────────────────────────────

def test_destroy_deregisters_with_the_same_tenant_it_registered_against():
    _reset()
    svc.ensure_bearer("child-1")
    cmeta = _cmeta()
    _run(svc.register(None, "child-1", cmeta))
    assert _run(svc.destroy("child-1", cmeta)) == ""
    assert DEREGISTER_CALLS, "nothing was deregistered"
    assert DEREGISTER_CALLS[0]["ctx"].hcl["agent_token_name"] == "ot-ot-cell-01", (
        "a destroy pointed at another tenant authenticates fine, removes nothing, and "
        "reports success")


def test_destroy_clears_the_bearer_only_after_the_integration_is_gone():
    _reset()
    svc.ensure_bearer("child-1")
    cmeta = _cmeta()
    _run(svc.register(None, "child-1", cmeta))
    _run(svc.destroy("child-1", cmeta))
    assert svc.bearer_config_key("child-1") not in CONF, (
        "a destroyed cell leaves its adapter bearer in the config store forever")


def test_destroy_keeps_the_state_when_it_could_not_be_used():
    """The state is the ONLY handle on a tenant-side integration."""
    _reset()
    svc.ensure_bearer("child-1")
    cmeta = _cmeta()
    _run(svc.register(None, "child-1", cmeta))
    _FAIL_DEREGISTER.append(True)
    problem = _run(svc.destroy("child-1", cmeta))
    assert problem, "a failed deregister reported success"
    assert "retried" in problem, problem
    assert cmeta["ot_faas_entitle_tf_state"], "the state was dropped on failure"
    assert svc.bearer_config_key("child-1") in CONF, (
        "the bearer was cleared while the integration still exists, so the "
        "integration now points at a function nobody can call")


def test_destroy_still_clears_a_bearer_from_a_deploy_that_never_registered():
    _reset()
    svc.ensure_bearer("child-1")
    assert _run(svc.destroy("child-1", _cmeta())) == ""
    assert svc.bearer_config_key("child-1") not in CONF
    assert DEREGISTER_CALLS == [], "there was no integration to deregister"


# ── What the card says ───────────────────────────────────────────────────────

def test_describe_gates_on_the_artifact_not_on_the_queued_job():
    """The `cell_wiring_complete` lesson: a queued step is not a finished one, and the
    duplicated copies that read a step instead of an artifact went green on the first
    of several."""
    _reset()
    cmeta, _note, _job = _queue()
    assert svc.describe(cmeta)["registered"] is False, (
        "a cell whose deploy is merely QUEUED reads as registered")
    svc.ensure_bearer("child-1")
    _run(svc.register(None, "child-1", cmeta))
    assert svc.describe(cmeta)["registered"] is True
    assert svc.describe(cmeta)["integration_id"] == "int-123"


def test_describe_is_safe_on_a_cell_that_predates_the_feature():
    _reset()
    out = svc.describe({})
    assert out["registered"] is False and out["workload"] == ""
    assert out["enabled"] is False, "a cell with no broker reads as enabled"


# ── The contract with the play ───────────────────────────────────────────────
# Every variable name below is shared between this service and a YAML file that runs
# on a different host. A rename on one side only is a run that succeeds having changed
# nothing it was asked to — the worst shape of failure available here — so the two
# halves are held against each other rather than documented.

_PLAY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                     "examples", "playbooks", "kubesolo",
                     "openfaas-function-deploy.yml")


def _play():
    import yaml
    with open(_PLAY, encoding="utf-8") as handle:
        return list(yaml.safe_load_all(handle))[0][0]


def _play_text():
    with open(_PLAY, encoding="utf-8") as handle:
        return handle.read()


def _flat_tasks(play):
    out = []
    for task in play["tasks"]:
        out.append(task)
        for key in ("block", "rescue", "always"):
            out.extend(task.get(key) or [])
    return out


def test_the_play_declares_every_variable_the_service_sends():
    _reset()
    _cm, _note, job = _queue()
    declared = set(_play()["vars"])
    for name in job["metadata"]["extra_vars"]:
        assert name in declared, (
            f"the service sends {name!r} and the play does not declare it — an "
            f"undeclared var is silently ignored, so the run would succeed having "
            f"changed nothing")
    for name in job["metadata"]["secret_vars"]:
        assert name in declared or name in _play_text(), (
            f"the play never reads the bound variable {name!r}")


def test_the_play_and_the_service_agree_on_the_probe_only_switch():
    _reset()
    job_id = _run(svc.queue_probe(None, "child-1", _cmeta(), {"private_ip": "10.0.0.9"}))
    assert job_id
    switch = CREATED_JOBS[-1]["metadata"]["extra_vars"]
    assert "otfn_probe_only" in _play()["vars"], (
        "the service asks for a probe-only run and the play has no such mode, so it "
        "would deploy an empty package over a live function")
    assert switch["otfn_probe_only"] is True


def test_the_play_gives_each_secret_one_key_equal_to_its_own_name():
    """That is what makes OpenFaaS mount it as a FILE rather than a directory, which
    is the shape `secretref`'s file channel reads without disambiguating."""
    text = _play_text()
    assert "{{ item.key }}: {{ lookup('vars', item.value) | b64encode }}" in text, (
        "the Secret's data key is not the Secret's own name")
    assert "name: {{ item.key }}" in text


def test_the_play_never_puts_a_credential_in_argv():
    text = _play_text()
    # Comment lines stripped: the play EXPLAINS why it avoids this, and asserting on
    # the raw text would make that explanation fail the test it explains. Whole-line
    # comments only — for a negative assertion, over-stripping is the direction that
    # turns a real violation into a pass.
    code = "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith("#"))
    assert "--from-literal" not in code, (
        "the play puts a secret value in argv, where ps shows it to every local user "
        "on the node")
    # Written to disk instead, privately, and removed whatever happens.
    assert 'mode: "0600"' in text and 'mode: "0700"' in text
    always = [t for t in _flat_tasks(_play())
              if t.get("name", "").startswith("Remove the staged")]
    assert always, "the staged package and secrets are never removed"


def test_the_play_pins_one_replica_so_no_grant_pays_a_cold_start():
    text = _play_text()
    assert 'com.openfaas.scale.min: "{{ otfn_replicas }}"' in text
    assert 'com.openfaas.scale.max: "{{ otfn_replicas }}"' in text, (
        "min without max lets the gateway scale the function, and scale-to-zero would "
        "make the first Entitle call of every grant wait for a cold start")


def test_the_play_probes_from_a_pod_and_treats_a_401_as_a_pass():
    """The probe carries no credential, so the shared-secret gate SHOULD refuse it —
    and a 401 is therefore the proof that the whole chain ran: gateway routed, loader
    unpacked, Python started, fnruntime.auth executed. What it rules out is a 404 (no
    route) and no response at all."""
    text = _play_text()
    assert "run otfn-probe" in text, "the probe does not run as a pod"
    assert "gateway.{{ otfn_gateway_namespace }}.svc.cluster.local:8080" in text, (
        "the probe does not go through cluster DNS, so it proves less than it looks")
    assert "200|401|403)" in text, "a 401 is not accepted, so every probe would fail"
    assert "PROBE-FAIL-ROUTE" in text and "PROBE-FAIL-NORESPONSE" in text, (
        "the probe does not distinguish 'no route' from 'nothing answered', which are "
        "different layers and different remedies")
    assert "--image-pull-policy=Never" in text, (
        "the probe image would be pulled, and the broker has no egress")


def test_the_play_refuses_a_host_that_has_no_function_runtime():
    names = [t.get("name", "") for t in _flat_tasks(_play())]
    assert any("no function runtime" in n for n in names), (
        "a broker baked before the runtime existed is THE error an operator will hit, "
        "and every later symptom is a Function object accepted and then ignored")
    assert "OT_FAAS=openfaas" in _play_text(), "the remedy does not name the knob"


def test_the_play_templates_render_and_produce_the_objects_the_runtime_needs():
    """Rendered here because a Jinja error in a play only surfaces at run time, and
    this play's two templates are the Function and its Secrets."""
    import base64

    import jinja2
    import yaml

    play = _play()
    variables = dict(play["vars"])
    variables.update({
        "otfn_name": svc.FUNCTION_NAME,
        "otfn_pkg_b64": "UEsDBBQ=",
        "otfn_pkg_sha256": "ab" * 32,
        "otfn_env": {"FN_CLOUD": "openfaas",
                     svc.SHARED_SECRET_ENV + svc.SECRET_FILE_SUFFIX: "/var/openfaas/secrets/s"},
        "otfn_secrets": {"fn-shared-secret-abcd1234": "otfn_bearer"},
        "otfn_bearer": "a-bearer",
    })
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["b64encode"] = lambda s: base64.b64encode(str(s).encode()).decode()
    env.filters["dictsort"] = lambda d: sorted(d.items())

    def _lookup(kind, name):
        assert kind == "vars"
        return variables[name]

    def _render(text, **extra):
        ctx = dict(variables, lookup=_lookup, **extra)
        return env.from_string(text).render(**ctx)

    tasks = _flat_tasks(play)
    fn = next(t for t in tasks if t.get("name") == "Write the Function manifest")
    doc = yaml.safe_load(_render(fn["ansible.builtin.copy"]["content"]))
    assert doc["apiVersion"] == "openfaas.com/v1" and doc["kind"] == "Function"
    assert doc["spec"]["image"] == svc.FUNCTION_IMAGE
    assert doc["spec"]["environment"]["OTFN_PKG_SHA256"] == "ab" * 32
    assert doc["spec"]["secrets"] == ["fn-shared-secret-abcd1234"]
    # The loader reads these two names; the bake test holds the other half.
    assert {"OTFN_PKG_B64", "OTFN_PKG_SHA256"} <= set(doc["spec"]["environment"])

    sec = next(t for t in tasks if t.get("name") == "Stage each secret as a manifest")
    sdoc = yaml.safe_load(_render(sec["ansible.builtin.copy"]["content"],
                                  item={"key": "fn-shared-secret-abcd1234",
                                        "value": "otfn_bearer"}))
    assert list(sdoc["data"]) == ["fn-shared-secret-abcd1234"]
    assert base64.b64decode(sdoc["data"]["fn-shared-secret-abcd1234"]) == b"a-bearer"


# ── The call sites ───────────────────────────────────────────────────────────
# Read from source, because exercising a three-cloud destroy sweep here would mean
# faking three cloud SDKs to assert an ORDERING. The ordering is the property.

_SERVICES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                         "web_dashboard", "services")
_DESTROY_HOSTS = {
    "gcp_vm_service.py": "deploy_meta",
    "aws_vm_service.py": "meta",
    "azure_vm_service.py": "meta",
}


def _service_src(name):
    with open(os.path.join(_SERVICES, name), encoding="utf-8") as handle:
        return handle.read()


def _adapter_teardown(src):
    """Just the adapter-teardown block, sliced from the comment all three share.

    Slicing from the metadata KEY instead would start mid-expression — after the
    variable name the assertions are about — which is how the first version of this
    passed and failed for reasons unrelated to the code.
    """
    start = src.index("The plant's Entitle adapter, BEFORE the agent token")
    return src[start:src.index("ot_agent_token_key", start)]


def _fn_src(name, func):
    import ast
    src = _service_src(name)
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{name}: {func} not found")


def test_the_adapter_is_wired_after_the_agent_that_calls_it():
    body = _fn_src("ot_service.py", "_wire_cell")
    assert "ot_faas_service.queue_deploy" in body, "_wire_cell never deploys the adapter"
    assert body.index("_install_plant_agent") < body.index("ot_faas_service.queue_deploy"), (
        "the adapter is deployed before the agent that calls it, and an integration "
        "whose agent is not there yet is useless")
    assert "ot_faas_service.register" in body, "_wire_cell never registers it"


def test_the_adapter_note_is_initialised_outside_the_broker_block():
    """The bug this module's own tunnel summary already paid for once.

    A cell with no broker never enters that block, so a note initialised inside it is
    an unset local by the time the summary dict reads it — UnboundLocalError instead
    of a summary, which is how a Re-wire of a complete cell used to fail.
    """
    body = _fn_src("ot_service.py", "_wire_cell")
    init = body.index('faas_note = ""')
    # The LAST guard before the deploy: this function has more than one `if broker_id:`
    # and comparing against the first would pass for the wrong reason.
    guard = body.rindex("if broker_id:", 0, body.index("ot_faas_service.queue_deploy"))
    assert init < guard, (
        "faas_note is initialised inside the broker block, so a cell without one "
        "raises UnboundLocalError when the summary is built")
    assert '"entitle_adapter": faas_note' in body, "the summary never reports it"


def test_a_failing_adapter_never_fails_a_cell_that_wired_correctly():
    body = _fn_src("ot_service.py", "_wire_cell")
    tail = body[body.index("ot_faas_service.queue_deploy"):]
    assert "except Exception" in tail, (
        "an adapter failure propagates, so a cell whose VM, tunnels and Web Jump all "
        "came up would be reported as a failed deploy — the same best-effort posture "
        "the Purdue rules take")
    assert "can be retried" in tail or "retried" in tail, (
        "a registration failure does not tell the operator the function survived it")


def test_every_cloud_deregisters_the_adapter_before_the_agent_token():
    """Ordering, and it is not taste.

    The integration is registered agent-brokered. Destroying the token first leaves it
    bound to an agent that no longer exists — and a deregister pointed at a dead agent
    authenticates fine, removes nothing, and reports success.
    """
    for name in _DESTROY_HOSTS:
        src = _service_src(name)
        assert "ot_faas_service" in src, f"{name} never deregisters the plant adapter"
        assert src.index("ot_faas_entitle_tf_state") < src.index("ot_agent_token_key"), (
            f"{name} destroys the agent token before the integration that depends on it")


def test_every_cloud_reads_the_metadata_name_it_actually_has():
    """GCP spells it deploy_meta and the other two spell it meta. A copy-paste across
    them is a NameError inside a teardown — the one place an exception is worst."""
    for name, variable in _DESTROY_HOSTS.items():
        src = _service_src(name)
        block = _adapter_teardown(src)
        assert f'{variable}.get("ot_faas_entitle_tf_state")' in block, (
            f"{name} reads the wrong metadata variable in the adapter teardown "
            f"(expected {variable})")
        assert f"_ot_faas.destroy(deploy_job_id, {variable})" in block, block


def test_the_adapter_teardown_reports_rather_than_raises():
    for name, variable in _DESTROY_HOSTS.items():
        src = _service_src(name)
        block = _adapter_teardown(src)
        assert 'result["ot_faas_error"]' in block, (
            f"{name} does not report the failure into the job result")
        # A `raise` STATEMENT, not the substring: the neighbouring comment says
        # "never raised", and matching inside a word made this fail on prose.
        raisers = [line.strip() for line in block.splitlines()
                   if not line.lstrip().startswith("#")
                   and line.strip().split(" ")[0] == "raise"]
        assert not raisers, (
            f"{name} can raise out of teardown ({raisers}) — a demo's teardown must "
            f"not be blockable by the identity provider")
        assert 'result["ot_faas_deregistered"]' in block


def test_the_teardown_triggers_on_the_bearer_too_not_only_the_integration():
    """A deploy that minted a bearer and never reached registration still left a
    credential in the config store. Gating only on the integration's state would
    leave it there for the life of the install."""
    for name in _DESTROY_HOSTS:
        src = _service_src(name)
        block = _adapter_teardown(src)
        assert "ot_faas_bearer_key" in block, (
            f"{name} skips teardown for a cell that deployed but never registered")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    sys.exit(1 if failures else 0)
