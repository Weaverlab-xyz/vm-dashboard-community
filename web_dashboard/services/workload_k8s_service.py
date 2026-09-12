"""Password-Safe-brokered ServiceAccount tokens for workloads OUTSIDE the cluster.

The Workload Lab's answer for a managed cluster. The SPIRE lab proves a workload can
reach an API server holding a credential stored **nowhere** — it attests itself and
fetches a five-minute JWT-SVID — but that rests on ``--authentication-config``, a
kube-apiserver flag. Which means it works on self-managed k3s and on **none** of EKS, AKS
or GKE, because no managed control plane lets you pass it. Every cluster this dashboard
actually provisions is in that second group.

So this is the path that does work there, and the question it answers is narrow and real:
**how does a machine outside the cluster authenticate to it?** A pod has a good answer
already — a projected ServiceAccount token, short-lived, audience-bound, rotated by the
kubelet, which is what IRSA, GKE Workload Identity and AKS Workload Identity all build
on. A CI runner or a CMDB scanner has none of that, and its options are a long-lived
kubeconfig (no expiry, no revocation, no record of retrieval), a client certificate
(Kubernetes cannot revoke certificates at all), or OIDC (see above). Since 1.24
Kubernetes also stopped auto-creating forever-tokens in Secrets, so the old answer is
gone and nothing replaced it.

**THE PROFILE IS THE DEMONSTRATION, NOT THE TOKEN.** A vaulted token is only interesting
if it is scoped, so this onboards one of two identities and the RBAC is the whole point:

  * ``deployer`` — ClusterRole ``edit`` through a **RoleBinding** in ONE namespace. The
    canonical CI case, and what makes it a demonstration is that it is *refused* in a
    second namespace.
  * ``reader`` — ClusterRole ``view`` through a **ClusterRoleBinding**. Reads the whole
    cluster and **cannot read Secrets**, because upstream ``view`` omits them by design.

Neither is ``cluster-admin``, and that is the difference from the existing ``ps-token``
path on the Kubernetes page. That one onboards a cluster-admin ServiceAccount because it
serves a human's brokered PRA session; a *vaulted* cluster-admin token is a vaulted
skeleton key, and it demonstrates nothing about scoping because there is no scope.

**What this deliberately does NOT do is call ``ps_k8s_token_service.register``.** That
function's step 2 reads the cluster's "current token" as a seed for the managed account,
and on a first registration that path applies
``k8s_service._entitle_k8s_rbac_manifest`` — a **ClusterRoleBinding to cluster-admin** —
purely to obtain a seed which is then discarded anyway, because a bearer token is 800-1200
characters and Password Safe's create API caps a password at 128. So the seed costs a
cluster-admin binding and buys nothing. This skips it: the account is created holding a
placeholder, and the rotation on register is what fills it. Everything else there is
reused rather than reimplemented, including the per-cloud address resolution and the
rotator RBAC.

The PRA Vault mirror and the ``SyncedAccounts`` link are skipped too, and that is not a
half-finished registration — ``ps_k8s_token_service._reconcile_synced_link`` says so
itself: "a PS-managed token with no PRA copy is a valid configuration". There is no
brokered session here. The consumer is a program with a Password Safe API client.

**Bound mode creates no token Secret at all.** The API server mints on demand through the
TokenRequest API, so there is nothing in the cluster to label, nothing for the plugin's
label-scoped sweep to collect, and nothing left behind when this is torn down but the
ServiceAccount and its binding.

Two boundaries the docs state and this module will not pretend away:

  * **Rotation does not revoke.** A token already issued lives out its TTL whatever
    Password Safe does next, so rotation is hygiene and deleting the ServiceAccount is
    containment — every token ever issued is bound to that account's uid.
  * **The vault authenticates whoever can retrieve.** Anyone who can retrieve *is* the
    workload, as far as this mechanism can tell. That is exactly the axis the SPIRE path
    wins on and this one does not, which is why both exist on the same page.

See docs/workload-kubernetes.md.
"""
import json
import logging
import re
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from ..database import K8sCluster, WorkloadK8sToken

logger = logging.getLogger(__name__)

JOB_TYPE = "workload_k8s_token"
INVENTORY_KIND = "workloadk8s"

VALID_ACTIONS = ("register", "rotate", "deregister")
VALID_PROFILES = ("deployer", "reader")
VALID_MODES = ("bound", "longlived")

# The steps, named, so `stages_done` distinguishes a half-failed onboard from one that
# never started. Not playbooks — this runs no Ansible; they are the ordered points at
# which something in the cluster or in Password Safe has changed.
STAGES = ("workload_rbac", "rotator_rbac", "managed_system", "first_rotation")

# Request-supplied and interpolated straight into a YAML manifest, so it is validated
# rather than trusted. A namespace of "default\n  foo: bar" would otherwise inject
# arbitrary keys into the ServiceAccount it renders. Same expression as
# `ps_resource_service._RFC1123_LABEL`, which guards the `ns=` option on the address —
# both ends of the same value, and Kubernetes will refuse anything else anyway.
_RFC1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_LABEL_MAX = 63

# What each profile binds. `role` is an upstream default ClusterRole in both cases,
# deliberately: a hand-written Role would be this feature's opinion about what a CI build
# needs, whereas `edit` and `view` are what every cluster already agrees those words mean,
# and `view`'s omission of Secrets is a documented upstream property rather than something
# asserted here.
#
# `scope` is the security boundary and the two differ on purpose. A RoleBinding grants
# inside its own namespace only, which is what makes the Deployer's refusal elsewhere a
# property of the binding rather than of anybody's care. A ClusterRoleBinding grants
# everywhere, which is what a fleet scan needs and is only acceptable because `view`
# cannot read Secrets.
_PROFILES = {
    "deployer": {
        "role": "edit",
        "scope": "namespace",
        "binding_kind": "RoleBinding",
        "summary": ("deploys into one namespace and is refused in every other — "
                    "ClusterRole/edit through a RoleBinding"),
    },
    "reader": {
        "role": "view",
        "scope": "cluster",
        "binding_kind": "ClusterRoleBinding",
        "summary": ("reads the whole cluster and cannot read Secrets — "
                    "ClusterRole/view through a ClusterRoleBinding"),
    },
}


class WorkloadK8sError(Exception):
    pass


def _cfg(key: str, default: str = "") -> str:
    from . import config_service
    try:
        return config_service.get(key) or default
    except Exception:                                  # pragma: no cover - config layer
        return default


def enabled() -> bool:
    """Both halves have to be on, and neither is a preview flag of its own.

    The tab is a control surface over a REGISTERED cluster and a CONFIGURED Password
    Safe; with either missing there is nothing for it to act on, so it renders as
    unavailable rather than as an empty list. Deliberately NOT a new preview flag —
    Settings owns two toggles for this page, one per lab, and the Kubernetes tab is a
    capability of the page rather than a third lab.

    Both reads pass the `settings` default, which is the convention `feature_flags.flags`
    uses for every flag that has one and is not optional here: `enabled()` defaults to
    False, so omitting it would read an env-var-configured deployment as OFF while the nav
    link and the Settings toggle — which go through `flags()` — read it as ON. That is the
    see-it-but-cannot-use-it split the `enabled` docstring exists to warn about."""
    from ..config import settings
    from . import feature_flags
    return (feature_flags.enabled("k8s_management_enabled",
                                  settings.k8s_management_enabled)
            and feature_flags.enabled("password_safe_enabled",
                                      settings.password_safe_enabled))


# ── reads ─────────────────────────────────────────────────────────────────────

def list_rows(db: Session, workgroup: Optional[str] = None) -> list:
    q = db.query(WorkloadK8sToken).filter(WorkloadK8sToken.status != "deleted")
    if workgroup:
        q = q.filter(WorkloadK8sToken.workgroup == workgroup)
    return q.order_by(WorkloadK8sToken.created_at.desc()).all()


def get_row(db: Session, row_id: str) -> Optional[WorkloadK8sToken]:
    return db.query(WorkloadK8sToken).filter(WorkloadK8sToken.id == row_id).first()


def profile_summary(profile: str) -> str:
    return (_PROFILES.get((profile or "").strip().lower()) or {}).get("summary", "")


def stage_jobs(row: WorkloadK8sToken) -> list:
    """The step job ids, oldest first. Text on the row, list on the wire."""
    try:
        out = json.loads(row.stage_job_ids or "[]")
        return out if isinstance(out, list) else []
    except (ValueError, TypeError):
        return []


# ── the manifests ─────────────────────────────────────────────────────────────

def _binding_name(*, profile: str, namespace: str, service_account: str) -> str:
    """A binding name that cannot collide with another row's.

    A ClusterRoleBinding name is CLUSTER-scoped, so a Reader for `ci/deploy-bot` and one
    for `ops/deploy-bot` would otherwise write the same object and the second onboard
    would silently re-point the first. The namespace is in the name for that reason even
    on the RoleBinding, where it is redundant, because the two profiles reading
    differently would be worse than one redundant segment."""
    return f"workload-{profile}-{namespace}-{service_account}"[:_LABEL_MAX].rstrip("-")


def workload_rbac_manifest(*, profile: str, namespace: str, service_account: str) -> str:
    """Namespace + ServiceAccount + the profile's binding. No Secret, and no token.

    The ServiceAccount is the identity Password Safe mints for, and this is the only
    thing this feature creates inside the cluster. In bound mode there is no token Secret
    at all — the API server mints through the TokenRequest API on each rotation — so
    there is nothing here for the plugin's label-scoped sweep to collect and nothing to
    leave behind.

    The Namespace is included so onboarding into a namespace that does not exist yet
    works rather than failing on the ServiceAccount; `kubectl apply` on an existing one
    is a no-op.

    **The roleRef is always a ClusterRole and never `cluster-admin`.** What differs is
    the BINDING: a RoleBinding pointing at a ClusterRole grants that role's verbs inside
    the binding's own namespace only, which is the mechanism behind the Deployer's
    refusal elsewhere. Getting this backwards — a ClusterRoleBinding to `edit` — would
    produce something that passes every test a namespace-scoped one does and grants write
    access to the entire cluster."""
    profile = (profile or "").strip().lower()
    spec = _PROFILES.get(profile)
    if spec is None:
        raise WorkloadK8sError(
            f"unknown profile {profile!r} (expected one of {', '.join(VALID_PROFILES)})")
    _require_label("namespace", namespace)
    _require_label("service account", service_account)
    name = _binding_name(profile=profile, namespace=namespace,
                         service_account=service_account)
    head = (
        "apiVersion: v1\nkind: Namespace\nmetadata:\n"
        f"  name: {namespace}\n---\n"
        "apiVersion: v1\nkind: ServiceAccount\nmetadata:\n"
        f"  name: {service_account}\n  namespace: {namespace}\n---\n"
    )
    subject = (f"subjects:\n- kind: ServiceAccount\n  name: {service_account}\n"
               f"  namespace: {namespace}\n")
    role_ref = ("roleRef:\n  apiGroup: rbac.authorization.k8s.io\n  kind: ClusterRole\n"
                f"  name: {spec['role']}\n")
    if spec["scope"] == "namespace":
        return (
            head + "apiVersion: rbac.authorization.k8s.io/v1\nkind: RoleBinding\n"
            f"metadata:\n  name: {name}\n  namespace: {namespace}\n" + role_ref + subject
        )
    return (
        head + "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding\n"
        f"metadata:\n  name: {name}\n" + role_ref + subject
    )


def _require_label(what: str, value: str) -> None:
    value = (value or "").strip()
    if not value or len(value) > _LABEL_MAX or not _RFC1123_LABEL.match(value):
        raise WorkloadK8sError(
            f"the {what} must be a DNS-1123 label — lowercase letters, digits and "
            f"dashes, starting and ending with a letter or digit, at most {_LABEL_MAX} "
            f"characters. Kubernetes refuses anything else, and this value is written "
            f"into a manifest, so it is checked before the run rather than after.")


# ── onboard ───────────────────────────────────────────────────────────────────

def onboard(db: Session, *, name: str, cluster_id: str, profile: str, namespace: str,
            service_account: str, created_by: str, mode: str = "bound",
            ttl_seconds: int = 0, expires_at: Optional[datetime] = None,
            workgroup: Optional[str] = None, cluster_name: str = "",
            resource_group: str = "", location: str = "") -> dict:
    """Validate, insert the row, and enqueue the registration job.

    Everything that can be decided without touching a cloud is decided HERE, so a
    mistake is a 400 while the form is still open rather than a failed job the operator
    has to go and read. That includes the address: it is composed and validated now,
    because an address the plugin cannot parse fails inside Password Safe at the first
    rotation, where this dashboard sees only a generic failure.

    ``workgroup`` is a parameter and defaults to None rather than being read off the
    cluster: ``K8sCluster`` HAS NO WORKGROUP COLUMN. Taking one from there looks obvious
    and raises AttributeError on every call — which is how this was found. NULL is also
    what the SPIRE lab's rows carry, since its API does not pass one either, so the
    inventory row and the RBAC filter behave identically for both labs."""
    from . import job_service, ps_api_service, ps_k8s_token_service, ps_resource_service

    if not enabled():
        raise WorkloadK8sError(
            "the Workload Lab's Kubernetes tab needs both k8s_management_enabled and "
            "password_safe_enabled")
    name = (name or "").strip()
    if not name:
        raise WorkloadK8sError("a name is required")
    profile = (profile or "").strip().lower()
    if profile not in VALID_PROFILES:
        raise WorkloadK8sError(
            f"unknown profile {profile!r} (expected one of {', '.join(VALID_PROFILES)})")
    mode = (mode or "bound").strip().lower()
    if mode not in VALID_MODES:
        raise WorkloadK8sError(
            f"unknown mode {mode!r} (expected one of {', '.join(VALID_MODES)})")
    namespace = (namespace or "").strip()
    service_account = (service_account or "").strip()
    _require_label("namespace", namespace)
    _require_label("service account", service_account)

    cluster = db.query(K8sCluster).filter(K8sCluster.id == cluster_id).first()
    if cluster is None:
        raise WorkloadK8sError(f"cluster {cluster_id} is not registered")
    if not cluster.kubeconfig_ref:
        raise WorkloadK8sError(
            f"{cluster.name} has no stored kubeconfig, so nothing here can reach its API "
            f"server to create the ServiceAccount")
    if not ps_api_service.configured():
        raise WorkloadK8sError(
            "Password Safe is not configured — set pscli_api_url, pscli_client_id, "
            "pscli_client_secret and pscli_api_account_name")

    # One identity per (cluster, namespace, ServiceAccount). A second row on the same
    # triple would onboard a second managed account against the same account name, and
    # Password Safe would hold two rotating credentials for one ServiceAccount — each
    # invalidating nothing but each believing it owns the account.
    dup = (db.query(WorkloadK8sToken)
           .filter(WorkloadK8sToken.cluster_id == cluster_id,
                   WorkloadK8sToken.namespace == namespace,
                   WorkloadK8sToken.service_account == service_account,
                   WorkloadK8sToken.status != "deleted").first())
    if dup is not None:
        raise WorkloadK8sError(
            f"{namespace}/{service_account} on {cluster.name} is already onboarded as "
            f"{dup.name!r} ({dup.profile}). Two managed accounts for one ServiceAccount "
            f"both rotate it, and each would serve a credential the other had replaced.")

    ttl = int(ttl_seconds or 0)
    # `_address_for` rather than `build_address`, and rather than a per-cloud branch here.
    # It resolves the region / subscription / project / location from the cluster row, its
    # deploy job's Terraform variables and config — including the GKE zone-versus-region
    # distinction that is the documented cause of a 404 on the cluster lookup. A private
    # helper on another module (noqa below) is worth it: a second copy of that resolution
    # would drift, and the failure it drifts into is opaque.
    #
    # `cluster_name`/`resource_group`/`location` are passed through for the case that
    # cannot be derived: a REGISTERED cluster has no deploy job, so there are no Terraform
    # variables to read the cloud's own name for the cluster out of, and `K8sCluster`
    # stores neither a resource group nor a GKE location. `_address_for` falls back to
    # config keys and then fails with a message naming what is missing — which is the right
    # behaviour at the click, but an operator has to be ABLE to supply it, so the request
    # can. Blank is correct for a dashboard-provisioned cluster, where the deploy job has
    # all three.
    address = ps_k8s_token_service._address_for(          # noqa: SLF001 — see above
        db, cluster, mode=mode, ttl_seconds=ttl, namespace=namespace,
        cluster_name=cluster_name, resource_group=resource_group, location=location)
    ps_resource_service._validate_k8ssa_dns_name(address)  # noqa: SLF001 — the authority

    row = WorkloadK8sToken(
        name=name, cluster_id=cluster.id, cluster_name=cluster.name, cloud=cluster.cloud,
        profile=profile, namespace=namespace, service_account=service_account,
        mode=mode, ttl_seconds=ttl or None, ps_address=address,
        ps_account_name=f"{namespace}/{service_account}",
        status="onboarding", workgroup=workgroup, created_by=created_by,
        expires_at=expires_at)
    db.add(row)
    db.flush()
    job = job_service.create_job(
        db, JOB_TYPE, created_by, workgroup=row.workgroup,
        metadata={"row_id": row.id, "action": "register"})
    row.stage_job_ids = json.dumps([job.id])
    db.commit()
    logger.info("workload-k8s: queued %s onboard for %r on cluster %s as job %s",
                profile, name, cluster.id, job.id)
    return {"id": row.id, "job_id": job.id, "address": address,
            "account_name": row.ps_account_name}


def start_rotate(db: Session, *, row_id: str, created_by: str) -> dict:
    """Enqueue one rotation. Hygiene, not revocation — see the module docstring."""
    row = get_row(db, row_id)
    if row is None:
        raise WorkloadK8sError(f"workload identity {row_id} not found")
    if not row.ps_account_id:
        raise WorkloadK8sError(
            f"{row.name} has no Password Safe managed account, so there is nothing to "
            f"rotate — the onboard did not get that far. Its status is {row.status!r}.")
    from . import job_service
    job = job_service.create_job(
        db, JOB_TYPE, created_by, workgroup=row.workgroup,
        metadata={"row_id": row.id, "action": "rotate"})
    row.stage_job_ids = json.dumps(stage_jobs(row) + [job.id])
    db.commit()
    return {"id": row.id, "job_id": job.id}


def start_decommission(db: Session, *, row_id: str, created_by: str) -> dict:
    """Enqueue teardown: delete the ServiceAccount and binding, then the PS objects.

    Also the reaper's entry point, which is why it is a queued job rather than a direct
    call — the expiry sweep creates exactly the row a human pressing Delete does."""
    row = get_row(db, row_id)
    if row is None:
        raise WorkloadK8sError(f"workload identity {row_id} not found")
    from . import job_service
    job = job_service.create_job(
        db, JOB_TYPE, created_by, workgroup=row.workgroup,
        metadata={"row_id": row.id, "action": "deregister"})
    row.status = "decommissioning"
    row.stage_job_ids = json.dumps(stage_jobs(row) + [job.id])
    db.commit()
    return {"id": row.id, "job_id": job.id}


# ── the worker ────────────────────────────────────────────────────────────────

def _mark(row: WorkloadK8sToken, stage: str) -> None:
    done = [s for s in (row.stages_done or "").split(",") if s]
    if stage not in done:
        done.append(stage)
    row.stages_done = ",".join(done)


async def run(db: Session, *, row_id: str, job_id: str, action: str = "register") -> None:
    """Worker entry point for ``workload_k8s_token``."""
    if action not in VALID_ACTIONS:
        raise WorkloadK8sError(f"unknown action {action!r}")
    row = get_row(db, row_id)
    if row is None:
        logger.warning("workload-k8s: row %s vanished before the %s", row_id, action)
        return
    from . import job_service
    job_service.set_running(db, job_id)
    try:
        if action == "register":
            result = await _run_register(db, row, job_id)
        elif action == "rotate":
            result = await _run_rotate(db, row, job_id)
        else:
            result = await _run_deregister(db, row, job_id)
        job_service.set_completed(db, job_id, result=result)
    except Exception as exc:
        # The type and the message, never a traceback and never a chained cause: this
        # string reaches a browser through the row (CodeQL py/stack-trace-exposure, and
        # the same reason ansible_run_gate gives).
        row.error_message = str(exc)[:2000]
        row.status = "failed"
        row.updated_at = datetime.utcnow()
        db.commit()
        logger.error("workload-k8s: %s failed for %s: %s", action, row_id, exc)
        job_service.set_failed(db, job_id, str(exc))


async def _run_register(db: Session, row: WorkloadK8sToken, job_id: str) -> dict:
    """Apply the workload RBAC, then the rotator's, then onboard and rotate once.

    The order is the correctness argument, and it is not the same as
    ``ps_k8s_token_service.register``'s:

      1. **the workload's own RBAC first.** It is the only thing here that is this
         feature's opinion, and it is the one step whose failure means the identity would
         be worthless — a ServiceAccount Password Safe can mint for but which can do
         nothing. Failing before anything exists in Password Safe leaves nothing to clean
         up;
      2. the ROTATOR's RBAC, so the functional account can actually mint. Non-fatal by
         design in the function this delegates to: Password Safe's own Verify Functional
         Account names every missing verb, whereas refusing to register would leave
         nothing to verify;
      3. the managed system and account, created holding a PLACEHOLDER. A bearer token is
         800-1200 characters and the create API caps a password at 128, so seeding is not
         possible — which is exactly why step 4 is not optional;
      4. rotate once. This is what puts a credential that authenticates into the vault,
         and it proves the whole chain (functional account → cloud control plane → API
         server → RBAC → TokenRequest) at onboarding time rather than at 3am. A row that
         reaches step 3 and stops looks registered and serves a placeholder.
    """
    from ..api.websocket import broadcast_progress
    from . import (job_service, k8s_service, ps_api_service, ps_k8s_token_service,
                   ps_resource_service)

    cluster = db.query(K8sCluster).filter(K8sCluster.id == row.cluster_id).first()
    if cluster is None:
        raise WorkloadK8sError(
            f"cluster {row.cluster_id} is no longer registered, so its API server cannot "
            f"be reached to create the ServiceAccount")

    # 1. The workload's identity and what it may do.
    await broadcast_progress(job_id, 15,
                             f"Creating {row.namespace}/{row.service_account} and its "
                             f"{_PROFILES[row.profile]['binding_kind']}…")
    manifest = workload_rbac_manifest(
        profile=row.profile, namespace=row.namespace,
        service_account=row.service_account)
    await k8s_service._apply_manifest_via_runner(       # noqa: SLF001 — the only applier
        k8s_service.resolve_kubeconfig(db, row.cluster_id), manifest,
        target_cloud=cluster.cloud or "")
    _mark(row, "workload_rbac")
    db.commit()

    # 2. The rotator's. Reused wholesale — it also handles the cloud-side identity
    #    mapping (the EKS access entry, the AKS role assignment) that a rotation needs
    #    and that has nothing to do with this feature.
    await broadcast_progress(job_id, 35, "Applying the rotator RBAC…")
    warnings: list = []
    rbac_note = await ps_k8s_token_service._apply_rbac(   # noqa: SLF001 — see docstring
        db, row.cluster_id, mode=row.mode, warnings=warnings, namespace=row.namespace)
    _mark(row, "rotator_rbac")
    db.commit()

    # 3. The managed system and account. NO `initial_password`: there is nothing to seed
    #    it with that would fit, and obtaining one would cost a cluster-admin binding
    #    (see the module docstring).
    await broadcast_progress(job_id, 55, "Creating the Password Safe managed system…")
    fa = await ps_k8s_token_service._resolve_functional_account(  # noqa: SLF001
        ps_k8s_token_service._functional_account_name(cluster.cloud),  # noqa: SLF001
        "kubernetes", "service account")
    platform_id = await ps_api_service.get_platform_id(
        _cfg("k8s_ps_token_platform", "Kubernetes Service Account Token"))
    workgroup_id = await ps_api_service.get_workgroup_id(
        ps_k8s_token_service._workgroup())             # noqa: SLF001
    system_label = f"workload-{row.profile}-{cluster.name}-{row.service_account}"
    reg = await ps_resource_service.register_managed_system(
        name=system_label, host_name=system_label,
        functional_account_id=fa["id"], platform_id=platform_id,
        workgroup_id=workgroup_id, ip_address="127.0.0.1", port=443,
        managed_account_name=row.ps_account_name, method="k8ssa",
        dns_name=row.ps_address)
    row.ps_system_id = str(reg.get("managed_system_id") or "")
    row.ps_account_id = str(reg.get("managed_account_id") or "")
    # The STATE, not just the ids: it is what `deregister` destroys from. A row holding
    # the ids alone would have nothing able to remove the objects it created.
    row.ps_tf_state = reg.get("tf_state_json")
    _mark(row, "managed_system")
    row.updated_at = datetime.utcnow()
    db.commit()

    # 4. The rotation that makes the account hold something real.
    await broadcast_progress(job_id, 80, "Rotating so the vault holds a real token…")
    await ps_k8s_token_service._rotate_token_once(      # noqa: SLF001 — see docstring
        db, cluster, int(row.ps_account_id), warnings=warnings, namespace=row.namespace)
    row.rotated = True
    _mark(row, "first_rotation")
    row.status = "active"
    row.error_message = None
    row.updated_at = datetime.utcnow()
    db.commit()

    for note in warnings:
        job_service.append_job_log(db, job_id, f"warning: {note}")
    return {"id": row.id, "managed_system_id": row.ps_system_id,
            "managed_account_id": row.ps_account_id, "account_name": row.ps_account_name,
            "address": row.ps_address, "profile": row.profile,
            "binds": _PROFILES[row.profile]["summary"], "rbac": rbac_note,
            "warnings": warnings}


async def _run_rotate(db: Session, row: WorkloadK8sToken, job_id: str) -> dict:
    """One rotation. Does NOT revoke — the previous token lives out its TTL."""
    from ..api.websocket import broadcast_progress
    from . import ps_k8s_token_service

    cluster = db.query(K8sCluster).filter(K8sCluster.id == row.cluster_id).first()
    if cluster is None:
        raise WorkloadK8sError(f"cluster {row.cluster_id} is no longer registered")
    await broadcast_progress(job_id, 40, "Rotating the managed account…")
    warnings: list = []
    await ps_k8s_token_service._rotate_token_once(      # noqa: SLF001
        db, cluster, int(row.ps_account_id), warnings=warnings, namespace=row.namespace)
    row.rotated = True
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"id": row.id, "rotated": True, "warnings": warnings,
            "note": ("a token already retrieved keeps working until its TTL expires — "
                     "rotation is hygiene, deleting the ServiceAccount is containment")}


async def _run_deregister(db: Session, row: WorkloadK8sToken, job_id: str) -> dict:
    """Remove the Password Safe objects, then the in-cluster identity.

    **This order, and the cluster LAST.** Deleting the ServiceAccount is what actually
    kills every token ever issued, because each is bound to that account's uid — so doing
    it first would leave Password Safe holding and serving a credential that authenticates
    to nothing, which is a worse state than either end of the teardown.

    Both halves are best-effort and report rather than raise. A cluster that is already
    gone, or a managed system an operator removed by hand, must not leave this row stuck
    in `decommissioning` forever — the record of the identity is what the inventory entry
    is for, and it cannot be retired while the teardown refuses to finish."""
    from ..api.websocket import broadcast_progress
    from . import k8s_service, ps_resource_service

    notes = []
    if row.ps_tf_state:
        await broadcast_progress(job_id, 25, "Removing the Password Safe objects…")
        try:
            await ps_resource_service.deregister(row.ps_tf_state)
            notes.append("Password Safe managed system and account removed")
        except Exception as exc:                        # noqa: BLE001 — see docstring
            notes.append(f"could not remove the managed system: {exc}")
            logger.warning("workload-k8s: PS teardown for %s failed: %s", row.id, exc)
    else:
        # Either the onboard never reached step 3, or an operator removed the objects by
        # hand. Both are states the teardown has to finish from, so this is a note and
        # not a failure — and the in-cluster half below is the part that actually
        # invalidates tokens.
        notes.append("no Password Safe Terraform state was recorded, so nothing there "
                     "was removed by this teardown")

    await broadcast_progress(job_id, 60,
                             f"Deleting {row.namespace}/{row.service_account}…")
    try:
        cluster = db.query(K8sCluster).filter(K8sCluster.id == row.cluster_id).first()
        manifest = workload_rbac_manifest(
            profile=row.profile, namespace=row.namespace,
            service_account=row.service_account)
        # The NAMESPACE is deliberately left behind: this manifest created it only if it
        # was missing, and deleting it would take every unrelated workload in it with it.
        manifest = "---\n".join(
            doc for doc in manifest.split("---\n")
            if "kind: Namespace" not in doc)
        await k8s_service._delete_manifest_via_runner(  # noqa: SLF001
            k8s_service.resolve_kubeconfig(db, row.cluster_id), manifest,
            target_cloud=(cluster.cloud if cluster is not None else "") or "")
        notes.append("ServiceAccount and binding deleted — every token issued to it dies "
                     "with the account's uid")
    except Exception as exc:                            # noqa: BLE001 — see docstring
        notes.append(f"could not delete the in-cluster identity: {exc}")
        logger.warning("workload-k8s: cluster teardown for %s failed: %s", row.id, exc)

    row.status = "deleted"
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"id": row.id, "notes": notes}
