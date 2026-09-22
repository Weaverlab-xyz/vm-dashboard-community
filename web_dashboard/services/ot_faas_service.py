"""The plant's function runtime, from the dashboard's side.

The OT broker carries the Entitle agent; ``provisioners/ot/ot-sim-debian.sh``
(``OT_FAAS=openfaas``) gives it a function runtime beside it. This module puts a
function ON that runtime and registers it with Entitle, so an Entitle "REST API"
integration's HTTP server lives inside the plant boundary and the agent — a pod on
the same cluster — is what calls it.

Three decisions shape everything here, and each is a road not taken:

**The image is generic; the CODE travels at wire time.** The bake channel is a single
shell script (``packer_service`` emits one ``provisioner "shell"``), so there is no way
to put the repo's ``functions/`` tree on a build VM — a baked adapter would be a
heredoc twin of ~45 KB of security-relevant Python, drifting from the real thing and
needing a re-bake per fix. So the baked image is a loader, and this module sends it the
deterministic zip ``cloud_function_package`` already builds, base64'd into the
OpenFaaS ``Function`` CR's ``environment``. An adapter fix then ships in the dashboard
image and a broker baked weeks ago runs it.

**There is no ``CloudFunction`` row.** ``cloud_function_service`` is terraform and
object-store all the way down: ``_TEMPLATE_DIRS`` has one directory per cloud,
terraform has no path to a private broker (which is exactly why the agent install goes
through ``ansible_local``), ``_upload_package`` needs a bucket the plant cannot reach,
and ``invoke()`` would have to dial a cluster DNS name in a subnet whose only ingress
is the PRA Gateway and the runner on 22. A row whose Invoke button always errors is
worse than no row. What IS shared is shared a layer down — ``cloud_function_package``
via its ``_LAYOUT`` row, ``fnruntime``, and ``entitle_registration_service`` — and the
key names here deliberately match that feature's, so a read-only row could be added
later without renaming anything.

**Credentials travel by reference, and land as FILES.** ``extra_vars`` is persisted to
the job row, so it carries only the NAMES of the variables a secret is bound to — the
same thing ``epml_token_var`` does in ``ansible_run_meta.RUN_META_KEYS``. The values
ride ``secret_vars``, which the runner resolves at run time and adds to the scrub list.
On the far side each becomes its own Kubernetes Secret whose single key is its own name,
because that is what makes OpenFaaS mount it at ``/var/openfaas/secrets/<name>`` as a
plain file — which ``fnruntime.secretref``'s file channel reads directly, so nothing in
``fnruntime.auth`` had to change to run off-cloud.

Shaped like ``ot_service``: module-level functions, ``_cfg`` reading config_service
first, artifacts recorded on the CELL's job row (the row the destroy sweep and the
expiry reaper both read).
"""
import base64
import hashlib
import logging
import secrets
from types import SimpleNamespace
from typing import Optional

logger = logging.getLogger(__name__)


class OTFaasError(Exception):
    """A function-runtime step failed. The message is written verbatim to the job's
    ``error_message`` — the only field the failed-job page renders — so it carries
    the remedy, not just the symptom."""


# The play that puts a function on the broker. A repo sample has to be uploaded to a
# storage backend before a run can resolve it by bare filename, which is the same
# contract every other Config-Management run has.
FAAS_DEPLOY_PLAYBOOK = "openfaas-function-deploy.yml"

FUNCTION_NAME = "ot-entitle-adapter"
NAMESPACE = "openfaas-fn"
# Baked by the provisioner and never pulled: the manifest says imagePullPolicy: Never,
# so this name is a statement about this host's containerd, not about a registry.
FUNCTION_IMAGE = "ot-faas-python:baked"

# In-cluster, and that is the whole point. Entitle never learns an address outside the
# plant: this resolves only inside the broker's KubeSolo, and the agent is what dials
# it. `_split_base_url` keeps the `/function/<name>` prefix on every route field, which
# is the machinery that was built for Azure's `/api` doing the same job here.
GATEWAY_BASE = "http://gateway.openfaas.svc.cluster.local:8080"

# The plant's HMI adapter. Safe as a default because its own dry run is ON unless an
# operator turns it off (ot_faas_dry_run): it deploys, registers, serves every route
# and reports exactly what it would do, without touching the HMI.
#
# `entitle_webhook_echo` — the no-op reference adapter, every route plus fault
# injection — stays available through ot_faas_workload, and is the right choice when
# the question is "does the chain work at all" rather than "does the HMI grant work".
DEFAULT_WORKLOAD = "fuxa_hmi_access"
ECHO_WORKLOAD = "entitle_webhook_echo"

# Where OpenFaaS mounts a secret listed in a Function's `secrets:`.
SECRET_MOUNT_DIR = "/var/openfaas/secrets"

# The shared-secret gate's variable, and the suffix that tells fnruntime to read it
# from a file rather than the environment.
#
# Spelled here rather than imported from ``fnruntime.secretref.file_env_for``: that
# package is ZIP MATERIAL and importing it mutates ``sys.path`` (its ``__init__``
# inserts its own directory so ``import fnruntime`` resolves in-repo exactly as it
# does inside the zip). Nothing in the dashboard process does that today —
# ``api/pov_accessor_rest`` copies three things out of a workload rather than import
# them, for the same reason — and a two-word constant is not worth being the first.
# ``tests/test_ot_faas_service.py`` holds this against ``file_env_for`` so the
# duplication cannot drift.
SHARED_SECRET_ENV = "FN_SHARED_SECRET"
SECRET_FILE_SUFFIX = "_FILE"

# A hard ceiling with a reason. `extra_vars` reaches ansible as ONE inline
# `--extra-vars <json>` argv element (ansible_vm_cmd), and Linux caps a single argv
# element at MAX_ARG_STRLEN = 131072 bytes. So this is not a style limit: past it the
# run dies with an unhelpful E2BIG in the runner. 90 KB leaves room for the rest of
# the JSON document and refuses at the click instead, with somewhere to go. A
# stdlib-only workload plus the whole fnruntime tree measures ~44 KB; one that vendors
# a driver (pymysql, python-tds) will not fit and needs the mounted-Secret route.
PACKAGE_BUDGET_B64 = 90_000
MAX_ARG_STRLEN = 131_072


def _cfg(key: str) -> str:
    from . import config_service
    from ..config import settings
    return config_service.get(key) or getattr(settings, key, "") or ""


# ── Per-cell keys ────────────────────────────────────────────────────────────
# Namespaced under `ot/<vm_job_id>/` like the cell's own agent token, and for the same
# reason: one per cell, destroyed with it. A shared key would mean every cell's adapter
# could be reached with every other cell's credential.

def bearer_config_key(vm_job_id: str) -> str:
    return f"ot/{vm_job_id}/faas_bearer"


def secret_name(vm_job_id: str, env_var: str) -> str:
    """The Kubernetes Secret backing ``env_var`` for this cell's function.

    A Secret name is a DNS-1123 label, so BOTH halves are normalised — not just the
    variable. Job ids are lower-case uuids today, which is exactly why the id half is
    easy to forget: an uppercase or underscored id would be rejected by the API server
    outright, and only whoever changed id generation would find out.

    Suffixed with the cell's job id so two cells sharing a cluster could never
    collide. They do not today, but a name that works only because of that is a trap.
    """
    def _label(text: str) -> str:
        kept = "".join(c if c.isalnum() else "-" for c in (text or "").lower())
        return kept.strip("-")

    stem = _label(env_var) or "fn-secret"
    return f"{stem}-{_label(vm_job_id)[:8] or 'cell'}"


def secret_file_path(vm_job_id: str, env_var: str) -> str:
    return f"{SECRET_MOUNT_DIR}/{secret_name(vm_job_id, env_var)}"


def ensure_bearer(vm_job_id: str) -> str:
    """This cell's function bearer, minted on first use.

    Minted here rather than in the play so it survives a re-run: the play is
    idempotent only because the value it writes is already decided.
    """
    from . import config_service

    key = bearer_config_key(vm_job_id)
    existing = (config_service.get(key) or "").strip()
    if existing:
        return existing
    value = secrets.token_urlsafe(32)
    config_service.set(key, value)
    return value


def fuxa_admin_config_key(vm_job_id: str) -> str:
    """Where this cell's FUXA admin password lives, when it has one.

    It may not. The cell's FUXA ships with authentication OFF, and in that state FUXA
    applies NO authorization to its user endpoints at all — an anonymous caller is
    handed administrator — so the adapter works with no credential and
    ``check_config`` says so in as many words. Rotating that default belongs with the
    bake change that turns ``secureEnabled`` on; until then this key is how an
    operator supplies a password they set themselves.
    """
    return f"ot/{vm_job_id}/fuxa_admin_password"


# ── Per-workload wiring ──────────────────────────────────────────────────────
# A workload needs its own environment and its own credentials, and the service
# should not grow a branch per workload inside queue_deploy. One function each,
# dispatched by name — the idiom cloud_function_service and ot_service already use.

def _fuxa_env(child_id: str, cmeta: dict) -> dict:
    """What ``fuxa_hmi_access`` needs, all of it from the cell's own record."""
    from . import config_service

    vm = cmeta.get("instance_name") or cmeta.get("vm_name") or "ot-cell"
    hmi_url = cmeta.get("ot_hmi_url") or ""
    env = {
        "FN_FUXA_URL": hmi_url,
        "FN_FUXA_HMI_URL": hmi_url,
        "FN_FUXA_ASSET_ID": f"fuxa:{vm}:hmi",
        "FN_FUXA_ASSET_NAME": f"FUXA HMI - {vm} (plant floor)",
        "FN_FUXA_CELL": vm,
        # The minted credential is useless without this. The cell admits the PRA
        # Gateway and the broker and nothing else, so a requester's browser cannot
        # route to the HMI: the grant has to hand over the name of the Web Jump that
        # can. ot_service provisions it as ot-<vm>-hmi.
        "FN_FUXA_JUMP_ITEM": cmeta.get("ot_web_jump_name") or f"ot-{vm}-hmi",
        "FN_FUXA_USER": _cfg("ot_faas_fuxa_user") or "admin",
        # Plain HTTP on a private address inside the plant; there is no certificate to
        # verify and nothing in the path to present one.
        "FN_FUXA_VERIFY_SSL": "0",
        # Tri-state on purpose: the adapter defaults its own dry run to ON when the
        # variable is ABSENT, so this must always be sent explicitly or the config
        # key would be unable to turn it off.
        "FN_FUXA_DRY_RUN": "1" if config_service.get_bool("ot_faas_dry_run", True) else "0",
    }
    if _cfg("ot_faas_fuxa_role_mode"):
        env["FN_FUXA_ROLE_MODE"] = _cfg("ot_faas_fuxa_role_mode")
    return env


def _fuxa_secrets(child_id: str) -> dict:
    """``{env_var: config_key}`` for the credentials this workload may have.

    Only what is actually set: mounting an empty Secret would make the adapter read a
    blank credential rather than none, and ``secretref`` refuses an empty file — so a
    cell whose FUXA has no password would fail closed instead of working as it does
    today.
    """
    from . import config_service

    out = {}
    if (config_service.get(fuxa_admin_config_key(child_id)) or "").strip():
        out["FN_FUXA_PASSWORD"] = fuxa_admin_config_key(child_id)
    return out


_WORKLOAD_ENV = {"fuxa_hmi_access": _fuxa_env}
_WORKLOAD_SECRETS = {"fuxa_hmi_access": _fuxa_secrets}


def workload_env(workload: str, child_id: str, cmeta: dict) -> dict:
    builder = _WORKLOAD_ENV.get(workload)
    return dict(builder(child_id, cmeta)) if builder else {}


def workload_secrets(workload: str, child_id: str) -> dict:
    builder = _WORKLOAD_SECRETS.get(workload)
    return dict(builder(child_id)) if builder else {}


def base_url() -> str:
    """The integration's endpoint. Overridable, which is what makes the runtime
    swappable: a plain Deployment or Nuclio answers on a different name, and nothing
    else in this module has to know."""
    return (_cfg("ot_faas_base_url").strip()
            or f"{GATEWAY_BASE}/function/{FUNCTION_NAME}")


# ── The package ──────────────────────────────────────────────────────────────

def build_package(workload: str = "") -> tuple:
    """``(base64_text, sha256_hex, workload)`` for the plant runtime.

    Refuses an oversized package rather than letting the runner discover the kernel's
    argv limit. The message names the workload and the route out, because "E2BIG" in
    an ansible wrapper is not a diagnosis anybody can act on.
    """
    from . import cloud_function_package as pkg

    name = (workload or _cfg("ot_faas_workload") or DEFAULT_WORKLOAD).strip()
    try:
        blob, sha256_hex, _b64_digest = pkg.build(cloud="openfaas", workload=name)
    except Exception as exc:  # noqa: BLE001
        raise OTFaasError(
            f"the function package for workload {name!r} could not be built ({exc}). "
            f"Workloads live in web_dashboard/functions/fnworkloads/; "
            f"cloud_function_package.available_workloads() lists them.") from exc

    encoded = base64.b64encode(blob).decode("ascii")
    if len(encoded) > PACKAGE_BUDGET_B64:
        raise OTFaasError(
            f"the {name!r} package is {len(encoded)} bytes once base64-encoded, past "
            f"the {PACKAGE_BUDGET_B64}-byte limit for a Config-Management run: the "
            f"whole extra_vars document travels as a single argv element, which Linux "
            f"caps at {MAX_ARG_STRLEN} bytes. A workload this size has to be delivered "
            f"as a mounted Secret instead of inline — most likely it vendors a "
            f"database driver, which the plant runtime image does not carry anyway.")
    # Recomputed rather than trusted: this is the value the loader checks its payload
    # against before running it, so it has to describe THESE bytes.
    assert hashlib.sha256(blob).hexdigest() == sha256_hex, "packager digest mismatch"
    return encoded, sha256_hex, name


# ── Refusals, before anything is created ─────────────────────────────────────

def skip_reason(cmeta: dict, bmeta: Optional[dict] = None) -> str:
    """"" when this cell can host an Entitle adapter, else the remedy verbatim.

    Same contract as ``ot_service.ps_checkout_skip_reason``: the string is written into
    the job result, so it names what to do. The agent-token check is not cosmetic —
    ``_common_attrs_hcl(private=True)`` RAISES without an agent name, and a Terraform
    traceback is a worse answer than a sentence.
    """
    from . import config_service

    cmeta = cmeta or {}
    if not config_service.get_bool("ot_faas_enabled", False):
        return ("The plant function runtime is off (ot_faas_enabled). Turn it on in "
                "Settings → Integrations → OT to host Entitle REST adapters in the "
                "plant.")
    if not cmeta.get("ot_broker_job_id"):
        return ("This cell has no DMZ broker, and the adapter runs on the broker's "
                "KubeSolo beside the Entitle agent that calls it. Deploy the cell with "
                "Entitle to get one.")
    if not (cmeta.get("ot_agent_token_name") or "").strip():
        return ("This cell has no Entitle agent token recorded, and the integration is "
                "registered agent-brokered — without the agent's name there is nothing "
                "to broker it. Re-wire the cell to mint one.")
    if bmeta is not None and not (bmeta.get("private_ip") or "").strip():
        return ("The DMZ broker reported no private address, so the Config-Management "
                "runner has nothing to reach.")
    return ""


# ── Deploying the function onto the broker ───────────────────────────────────

async def queue_deploy(db, parent_id: str, child_id: str, cmeta: dict, *,
                       broker_id: str, bmeta: dict, cloud: str = "gcp",
                       workload: str = "", created_by: str = "") -> str:
    """Queue the Config-Management run that puts the function on the broker.

    Queued as an ordinary ``ansible_local`` job rather than run inline, exactly as
    ``ot_service._install_plant_agent`` is: it then gets the durable runner, the job
    page its output belongs on, and the secret channel that binds credentials by
    reference. The run reaches a private broker only from an in-cloud runner, which
    ``ot_service.config_runner_problem`` refused the whole deploy without.
    """
    from . import ansible_run_meta, job_service, ot_service, storage_service

    problem = skip_reason(cmeta, bmeta)
    if problem:
        return f"adapter deploy skipped: {problem}"
    if cmeta.get("ot_faas_job_id"):
        return f"adapter deploy already queued (job {cmeta['ot_faas_job_id']})"

    broker_ip = (bmeta.get("private_ip") or "").strip()
    package_b64, package_sha, name = build_package(workload)
    bearer = ensure_bearer(child_id)
    if not bearer:
        raise OTFaasError("the function's bearer could not be minted or read back.")

    # One Secret per credential, each named after the variable it backs, so OpenFaaS
    # mounts it as a single FILE at /var/openfaas/secrets/<name> rather than as a
    # directory of keys — which is what fnruntime.secretref's file channel reads.
    #
    # The gate's secret is always there; a workload's own credentials are whatever it
    # declares AND the operator has actually set. Every one of them contributes both a
    # Secret and a `*_FILE` pointer, derived from the same helper, so a name can never
    # be right in one place and wrong in the other.
    env = {
        "FN_CLOUD": "openfaas",
        "FN_WORKLOAD": name,
        "FN_NAME": FUNCTION_NAME,
    }
    env.update(workload_env(name, child_id, cmeta))

    secret_vars = {"otfn_bearer": bearer_config_key(child_id)}
    secrets_map = {secret_name(child_id, SHARED_SECRET_ENV): "otfn_bearer"}
    env[SHARED_SECRET_ENV + SECRET_FILE_SUFFIX] = secret_file_path(
        child_id, SHARED_SECRET_ENV)
    for index, (env_var, config_key) in enumerate(
            sorted(workload_secrets(name, child_id).items())):
        # The ansible variable is positional rather than named after the credential:
        # extra_vars is persisted to the job row, and a variable called
        # `otfn_fuxa_password` would put the credential's PURPOSE in the database even
        # though its value stays out.
        var = f"otfn_secret_{index}"
        secret_vars[var] = config_key
        secrets_map[secret_name(child_id, env_var)] = var
        env[env_var + SECRET_FILE_SUFFIX] = secret_file_path(child_id, env_var)

    parent = job_service.get_job(db, parent_id)
    payload = SimpleNamespace(
        asset=FAAS_DEPLOY_PLAYBOOK,
        target=broker_ip,
        cloud=cloud,
        ansible_user="",
        extra_vars={
            "otfn_name": FUNCTION_NAME,
            "otfn_namespace": NAMESPACE,
            "otfn_image": FUNCTION_IMAGE,
            "otfn_pkg_b64": package_b64,
            "otfn_pkg_sha256": package_sha,
            "otfn_env": env,
            # The NAMES of the bound variables, never their values — extra_vars is
            # persisted to the job row. Same discipline as epml_token_var.
            "otfn_secrets": secrets_map,
            # Prove the function answers through the gateway before the run is called a
            # success. From a POD, because the caller will be one.
            "otfn_probe": True,
        },
        secret_vars=secret_vars,
        secret_become_source="",
        secret_ssh_key_source="",
        managed_account=None,
        managed_become=None,
        epml_token_var="",
    )
    try:
        job = job_service.create_job(
            db,
            job_type="ansible_local",
            created_by=(created_by or (parent.created_by if parent else "system")),
            workgroup="ansible",
            metadata=ansible_run_meta.run_meta(
                payload,
                description=f"Entitle adapter ({name}) → "
                            f"{bmeta.get('instance_name') or broker_ip} "
                            f"(the plant's function runtime)",
                asset_backend=storage_service.active_backend()),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("OT cell: adapter deploy could not be queued: %s", exc)
        return f"adapter deploy could not be queued ({exc})"

    recorded = {"ot_faas_job_id": job.id,
                "ot_faas_workload": name,
                "ot_faas_package_sha256": package_sha,
                "ot_faas_base_url": base_url(),
                "ot_faas_bearer_key": bearer_config_key(child_id)}
    job_service.update_metadata(db, child_id, recorded)
    job_service.update_metadata(db, broker_id, {"ot_faas_job_id": job.id})
    cmeta.update(recorded)
    if parent_id:
        job_service.update_progress(db, parent_id, 97,
                                    "Deploying the Entitle adapter on the plant's "
                                    "function runtime…")
    return (f"adapter deploy queued as job {job.id} ({name}, "
            f"{len(package_b64)} b64 bytes)")


async def queue_probe(db, child_id: str, cmeta: dict, bmeta: dict,
                      created_by: str = "", cloud: str = "gcp") -> str:
    """Re-run the play's probe only: no package, no secrets, no change.

    The twin of ``ot_service.queue_egress_probe``, and safe against a live function —
    it asks "does the deployed adapter still answer through the gateway, from a pod"
    without touching what is deployed. That is the diagnostic when Entitle reports an
    integration unhealthy and nobody knows which layer moved.
    """
    from . import ansible_run_meta, job_service, storage_service

    broker_ip = (bmeta or {}).get("private_ip") or ""
    if not broker_ip:
        raise OTFaasError("the DMZ broker has no private address to probe.")

    payload = SimpleNamespace(
        asset=FAAS_DEPLOY_PLAYBOOK,
        target=broker_ip.strip(),
        cloud=cloud,
        ansible_user="",
        extra_vars={"otfn_name": cmeta.get("ot_faas_function") or FUNCTION_NAME,
                    "otfn_namespace": NAMESPACE,
                    "otfn_probe_only": True},
        secret_vars={},
        secret_become_source="",
        secret_ssh_key_source="",
        managed_account=None,
        managed_become=None,
        epml_token_var="",
    )
    job = job_service.create_job(
        db,
        job_type="ansible_local",
        created_by=created_by or "system",
        workgroup="ansible",
        metadata=ansible_run_meta.run_meta(
            payload,
            description=f"Adapter probe → {(bmeta or {}).get('instance_name') or broker_ip}",
            asset_backend=storage_service.active_backend()),
    )
    return job.id


# ── Registering it with Entitle ──────────────────────────────────────────────

async def register(db, child_id: str, cmeta: dict) -> str:
    """Register the plant's adapter as an Entitle REST integration, agent-brokered.

    ``private=True`` is the load-bearing argument: it attaches the cell's own agent
    token, and the Entitle agent — designed to reach private resources and report back
    to the tenant's API — is then what performs the adapter's calls. So ``base_url``
    can be a name that resolves only inside the plant, Entitle needs no route in, and
    the broker needs no new egress destination.

    Idempotent on the artifact, which is what makes Re-wire safe: the state key is
    written the moment the integration exists, and a second call with it present is a
    no-op rather than a second integration nobody is tracking.
    """
    from . import entitle_registration_service as entitle, job_service

    problem = skip_reason(cmeta)
    if problem:
        return f"Entitle registration skipped: {problem}"
    if cmeta.get("ot_faas_entitle_tf_state"):
        return (f"already registered in Entitle (integration "
                f"{cmeta.get('ot_faas_entitle_integration_id') or '?'})")

    bearer = (_read_bearer(child_id) or "").strip()
    if not bearer:
        raise OTFaasError(
            "the function's bearer is not in the config store, so the integration "
            "would be registered with a credential the function does not accept. "
            "Re-run the adapter deploy, which mints it.")

    vm = cmeta.get("instance_name") or cmeta.get("vm_name") or "ot-cell"
    try:
        result = await entitle.register_rest(
            name=f"ot-{vm}-adapter"[:50],
            base_url=cmeta.get("ot_faas_base_url") or base_url(),
            shared_secret=bearer,
            private=True,
            ephemeral=True,
            auth_header="Authorization",
            ctx=entitle.local_tenant_ctx(
                agent_token_name=cmeta.get("ot_agent_token_name") or ""),
        )
    except Exception as exc:  # noqa: BLE001
        raise OTFaasError(
            f"registering the plant's adapter in Entitle failed: {exc}. The function "
            f"itself is unaffected — re-run this step once the cause is fixed.") from exc

    recorded = {
        "ot_faas_entitle_integration_id": str(result.get("integration_id") or ""),
        "ot_faas_entitle_tf_state": result.get("tf_state_json") or "",
    }
    job_service.update_metadata(db, child_id, recorded)
    cmeta.update(recorded)
    return (f"registered in Entitle as integration "
            f"{recorded['ot_faas_entitle_integration_id'] or '(unnamed)'} — "
            f"agent-brokered, so its endpoint resolves only inside the plant")


def _read_bearer(vm_job_id: str) -> str:
    from . import config_service
    return config_service.get(bearer_config_key(vm_job_id)) or ""


async def destroy(vm_job_id: str, cmeta: dict) -> str:
    """Deregister the integration and clear this cell's stashed bearer.

    Best-effort and never raising, like every other OT teardown step: the teardown of a
    demo must not be blockable by the identity provider. Returns "" when there was
    nothing to do or it succeeded, else the problem for the job result.

    On FAILURE the stash is KEPT. The Terraform state is the only handle left on a
    tenant-side integration, so deleting it would strand the integration with no way
    to find it again — the same argument ``ot_service.destroy_agent_token`` makes.
    """
    from . import config_service, entitle_registration_service as entitle

    state = (cmeta or {}).get("ot_faas_entitle_tf_state") or ""
    if not state:
        # Still clear the bearer: a deploy that never reached registration leaves one.
        config_service.delete(bearer_config_key(vm_job_id))
        return ""
    try:
        await entitle.deregister(
            state,
            ctx=entitle.local_tenant_ctx(
                agent_token_name=(cmeta or {}).get("ot_agent_token_name") or ""),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("OT cell %s: adapter integration not deregistered: %s",
                       vm_job_id, exc)
        return (f"the plant adapter's Entitle integration was not removed ({exc}) — "
                f"its state is kept on the job row so this can be retried")
    # Only now: while the integration existed, the bearer was the credential it
    # authenticates with, and dropping it first would leave a live integration
    # pointing at a function nobody can call.
    config_service.delete(bearer_config_key(vm_job_id))
    return ""


def describe(cmeta: dict) -> dict:
    """What the cell card shows. Reads only what was recorded, never re-derives."""
    cmeta = cmeta or {}
    return {
        "enabled": not skip_reason(cmeta),
        "workload": cmeta.get("ot_faas_workload") or "",
        "deploy_job_id": cmeta.get("ot_faas_job_id") or "",
        "package_sha256": (cmeta.get("ot_faas_package_sha256") or "")[:12],
        "base_url": cmeta.get("ot_faas_base_url") or "",
        "integration_id": cmeta.get("ot_faas_entitle_integration_id") or "",
        # The artifact, not the queued job: a deploy job that is still running, or that
        # failed, must not read as registered. This is the `cell_wiring_complete`
        # lesson — gate on the thing that exists, never on the step that was started.
        "registered": bool(cmeta.get("ot_faas_entitle_tf_state")),
    }
