"""
Config Management API — routes, validation and job enqueue.

All endpoints require authentication. A run is **queued**, not executed here: the
endpoint validates the request, persists the run's parameters on the job (see
``services.ansible_run_meta``) and returns a job_id the client polls at
/api/jobs/{id}. Execution belongs to the job runner —
``services.ansible_local_run_service`` for VM (SSH/WinRM) targets, and
``services.ansible_cloud_run_service`` for the Kubernetes / cloud-database localhost
plays.

Asset types supported:
    .yml / .yaml  — Ansible playbooks (run as-is)
    .sh           — Bash scripts (auto-wrapped in a generated playbook)
    .rpm          — RPM packages   (auto-wrapped: copy + dnf install)
    .deb          — DEB packages   (auto-wrapped: copy + apt install)

Target types:
    On-premises group key  — "proxmox", "vsphere", "hyperv", "nutanix", "xcpng"
    Bare IP / hostname     — ad-hoc; cloud field determines SSH key source
"""
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, model_validator
from sqlalchemy.orm import Session

from ..database import Job, User, get_db
from .auth import get_current_user
from ..services import job_service
from ..services import storage_service
from ..services.storage_service import StorageError
from ..services import ansible_local_service
from ..services import ansible_run_meta

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/config-mgmt", tags=["config-mgmt"])


# ── Asset / playbook listing ───────────────────────────────────────────────────

@router.get("/assets")
async def list_assets(current_user: User = Depends(get_current_user)):
    """List all available assets (.yml, .sh, .deb, .rpm) across every configured
    storage backend, each item tagged with the backend it lives on. Issue #16:
    operators can now keep playbooks on local filesystem AND on a cloud backend
    side-by-side — the UI uses the per-asset backend tag to warn when a local
    asset is paired with a cloud target."""
    try:
        return await storage_service.list_all_assets()
    except StorageError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/playbooks")
async def list_playbooks(current_user: User = Depends(get_current_user)):
    """List playbook names (.yml/.yaml) from configured storage — back-compat alias."""
    try:
        return await storage_service.list_playbooks()
    except StorageError as e:
        raise HTTPException(status_code=502, detail=str(e))


class UploadAssetRequest(BaseModel):
    filename: str
    content_b64: str


@router.post("/upload", status_code=201)
async def upload_asset(
    req: UploadAssetRequest,
    current_user: User = Depends(get_current_user),
):
    """Upload a playbook (.yml/.yaml), shell script (.sh), or package (.rpm/.deb) to storage."""
    import base64
    try:
        data = base64.b64decode(req.content_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="content_b64 is not valid base64.")
    # Advisory secret scan (never blocks the upload — a heads-up only).
    findings = []
    from ..services import config_service as cs, secret_scan
    if cs.get_bool("secret_scan_enabled", True):
        findings = secret_scan.scan_bytes(data, req.filename)

    try:
        await storage_service.upload_asset(req.filename, data)
    except StorageError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "filename": req.filename, "size": len(data),
            "secret_findings": findings}


# ── Inventory ─────────────────────────────────────────────────────────────────

@router.get("/inventory")
async def get_inventory(db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    """
    Return the dynamic Ansible inventory.

    Only on-premises hypervisors that are both enabled (feature flag) and have
    a host address configured appear.  The response includes:
      targets   — simplified list for the UI target picker
      inventory — full Ansible JSON inventory (groups + hostvars)
    """
    return {
        "targets":   ansible_local_service.get_configured_targets(db),
        "inventory": ansible_local_service.build_inventory(db),
    }


# ── Cloud targets ─────────────────────────────────────────────────────────────

@router.get("/cloud-targets")
async def get_cloud_targets(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Return cloud VM targets (EC2 + Azure VMs + GCE instances) with IPs for the
    Config Mgmt page's target picker.

    Source of truth is the ``jobs`` table: every successful cloud deploy lands
    a completed Job whose ``metadata_dict`` carries ``instance_id``/``vm_name``,
    ``private_ip``, and ``public_ip``. We enumerate those directly instead of
    relying on the cache populated by the cloud tabs — the cache may be empty
    on a freshly-restarted server, after a cache-invalidation following a
    deploy, or when the user has never opened the relevant cloud tab. Previously
    those cases left this endpoint returning empty lists even though the
    instances clearly existed (issue #12).

    Destroyed instances are excluded (``metadata_dict['destroyed'] == True``
    after the destroy job runs).

    Response shape:
        {
          "aws":   [{name, ip, instance_id}, ...],
          "azure": [{name, ip}, ...],
          "gcp":   [{name, ip, zone}, ...],
        }
    """
    targets: dict = {"aws": [], "azure": [], "gcp": []}

    # Pull completed deploys for all three clouds in one trip.
    deploy_jobs = (
        db.query(Job)
        .filter(
            Job.job_type.in_(("ec2_deploy", "azure_deploy", "gce_deploy")),
            Job.status == "completed",
        )
        .order_by(Job.created_at.desc())
        .all()
    )

    for job in deploy_jobs:
        meta = job.metadata_dict
        if meta.get("destroyed"):
            continue
        ip = meta.get("public_ip") or meta.get("private_ip")
        if not ip:
            continue

        if job.job_type == "ec2_deploy":
            iid = meta.get("instance_id")
            name = meta.get("instance_name") or iid or ""
            targets["aws"].append({"name": name, "ip": ip, "instance_id": iid})
        elif job.job_type == "azure_deploy":
            targets["azure"].append({"name": meta.get("vm_name", ""), "ip": ip})
        elif job.job_type == "gce_deploy":
            targets["gcp"].append({
                "name": meta.get("instance_name", ""),
                "ip": ip,
                "zone": meta.get("zone", ""),
            })

    # Per-cloud default SSH user — surfaced as a *suggestion* the run-asset
    # form pre-fills when the operator picks a cloud target. Not a secret;
    # logged-in user is sufficient auth.
    default_user = _cfg("ansible_default_user") or "ec2-user"
    return {
        **targets,
        "default_users": {
            "aws":   _cfg("ansible_aws_user")   or default_user,
            "azure": _cfg("ansible_azure_user") or default_user,
            "gcp":   _cfg("ansible_gcp_user")   or default_user,
        },
    }


# ── Agent-reachable targets (on-prem VMs + databases behind a remote agent) ─────

@router.get("/agent-targets")
async def get_agent_targets(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """On-prem VMs and databases a **remote agent** can configure.

    The third target family, alongside ``/cloud-targets`` and ``/localhost-targets``, and the
    only one for resources the dashboard has no route to at all. A cloud-hosted dashboard has
    no other way to configure a hypervisor guest or an on-prem database: the runner it would
    otherwise launch is a sibling container on its own host.

    Built from ``inventory_service.collect`` + ``_target_spec`` rather than its own query, so
    a row that appears here is one the run endpoint will accept — the two cannot disagree
    about what is targetable, which is the failure mode a second implementation would have.
    RBAC is the inventory page's own filter, so a synced VM nobody has tagged stays
    admin-only.

    ``reason`` is populated instead of the target fields when a row is *nearly* targetable —
    almost always a VM with no address yet — because "why is my VM not in this list" is the
    question this feature will actually generate.

    Response shape::

        {"vms": [{id, name, agent_id, agent_name, connection_id, target_id, ip,
                  transport, cloud, reason}, …],
         "databases": [{id, name, agent_id, agent_name, target_id, host, engine, reason}, …]}
    """
    from ..database import RemoteAgent
    from ..services import inventory_service

    agent_names = {a.id: a.name for a in db.query(RemoteAgent).all()}
    accessible = inventory_service.accessible_workgroups(current_user)
    out: dict = {"vms": [], "databases": []}
    for item in inventory_service.collect(db):
        if not item.get("agent_id"):
            continue
        if not inventory_service.visible_to(item, accessible, current_user.username):
            continue
        spec = inventory_service._target_spec(item)
        row = {"id": item["id"], "name": item.get("name") or item["id"],
               "agent_id": item["agent_id"],
               "agent_name": agent_names.get(item["agent_id"], "(unknown agent)"),
               "reason": spec if isinstance(spec, str) else ""}
        if item.get("kind") == "database":
            row.update({"engine": item.get("engine") or "",
                        "host": item.get("private_host") or "",
                        "target_id": item["id"].split(":", 1)[1]})
            out["databases"].append(row)
        else:
            row.update({"cloud": item.get("cloud") or "",
                        "connection_id": item.get("connection_id") or "",
                        "target_id": item["id"].split(":")[-1],
                        "ip": item.get("ip") or "",
                        "transport": spec.get("transport") if isinstance(spec, dict) else ""})
            out["vms"].append(row)
    return out


# ── Localhost targets (Kubernetes clusters + databases) ─────────────────────────

@router.get("/localhost-targets")
async def get_localhost_targets(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Kubernetes clusters + databases selectable as **localhost** Ansible
    targets (the run reaches out via kubeconfig / DB login vars). Ids + display fields
    only — never a secret. Parallels ``/cloud-targets`` for VMs, and is served here so
    the Config-Management page doesn't need the separate k8s / cloud_database feature
    permissions just to populate its picker.

    Only resources an Ansible runner can actually reach + configure appear: aws/azure/gcp
    (in-cloud runner), plus — for both clusters and databases — those registered with
    cloud="local", which run on the dashboard's local runner. See ``K8S_TARGET_CLOUDS``
    and ``DB_TARGET_CLOUDS`` for the two lists.

    Response shape:
        {"k8s": [{id, name, cloud, status}, …],
         "databases": [{id, engine, cloud, status}, …]}
    """
    from ..database import K8sCluster, CloudDatabase
    from ..services import ansible_cloud_run_service as acr

    clusters = []
    for c in db.query(K8sCluster).order_by(K8sCluster.created_at.desc()).all():
        if (c.cloud or "").lower() in acr.K8S_TARGET_CLOUDS:
            clusters.append({"id": c.id, "name": c.name, "cloud": c.cloud,
                             "status": c.status})
    databases = []
    for d in db.query(CloudDatabase).order_by(CloudDatabase.created_at.desc()).all():
        if (d.cloud or "").lower() in acr.DB_TARGET_CLOUDS and d.engine in acr.ANSIBLE_DB_ENGINES:
            databases.append({"id": d.id, "engine": d.engine, "cloud": d.cloud,
                              "status": d.status})
    return {"k8s": clusters, "databases": databases}


# ── Playbook / asset run ───────────────────────────────────────────────────────

class ManagedAccountRef(BaseModel):
    """A BeyondTrust Password Safe managed account. Never carries a credential.

    Two forms, because a single run and a bulk run identify an account differently:

    * PINNED — ``system_id`` + ``account_id``, picked from the live list for one
      host. Drives the just-in-time checkout directly.
    * BY NAME — ``account_name`` only, used by a bulk run. Both ids are specific to
      one managed system, so a pinned ref cannot be reused across a fleet: it would
      check out one machine's credential and connect to every host with it. A
      name-only ref is instead resolved against each job's OWN target host at run
      time (see ``services.ansible_credentials.resolve_managed_ref``).

    ``account_name`` is non-secret and becomes ``ansible_user``.
    """
    system_id: int | None = None
    account_id: int | None = None
    account_name: str = ""
    uses_ssh_key: bool = False   # DSSAutoManagementFlag → checkout as -t dsskey

    @model_validator(mode="after")
    def _pinned_or_named(self):
        if self.account_id is not None and self.system_id is not None:
            return self
        if (self.account_name or "").strip():
            return self
        raise ValueError(
            "a managed account needs either both system_id and account_id (a pinned "
            "account for one host) or an account_name (resolved per host at run time)")


class RunRequest(BaseModel):
    asset: str           # filename of any supported type (.yml, .sh, .deb, .rpm)
    target: str = ""     # on-prem group key OR bare IP/hostname for cloud/ad-hoc (unused for k8s/database)
    cloud: str = ""      # "" | "aws" | "azure" | "gcp" — drives SSH key retrieval
    ansible_user: str = ""  # SSH user for cloud runner targets; falls back to ansible_default_user
    # Target family. "vm" (default) = the SSH/WinRM path below (target is an IP/host
    # or on-prem group key). "k8s"/"database" = a Kubernetes cluster / cloud database:
    # a localhost play whose connection material is auto-injected server-side and which
    # ALWAYS runs on the in-cloud transient runner (see ansible_cloud_run_service). For
    # those, target/cloud/ansible_user/secret_ssh_key_source/managed_account are ignored.
    target_kind: str = "vm"  # "vm" | "k8s" | "database"
    target_id: str = ""      # K8sCluster.id / CloudDatabase.id when target_kind != "vm"
    extra_vars: dict = {}
    # Use Secrets-Management secrets in the run WITHOUT ever seeing the value.
    # Requires the `secrets:use` permission (admins bypass). A "source" is a
    # config-secret registry key or a raw vault ref (bt_safe:// …). Resolved
    # values are scrubbed from job output and never stored on the job.
    secret_vars: dict = {}            # {ansible_var: source} — named vars; LOCAL runner only
    secret_become_source: str = ""    # source → ansible_become_password (no_log); LOCAL runner only
    secret_ssh_key_source: str = ""   # source → the connection SSH private key; LOCAL + cloud runner
    # Which storage backend the asset should be fetched from. Empty = active
    # backend (back-compat). With multi-backend support (issue #16), the UI
    # passes the backend explicitly because the same asset name may exist on
    # multiple backends.
    asset_backend: str = ""
    # Bind a freshly-minted BeyondTrust EPM-L installation token to this Ansible
    # variable. The NAME only — the token is minted server-side at run time and rides
    # the scrubbed secret channel, so it never reaches the browser or job metadata.
    epml_token_var: str = ""
    # Groups the jobs of one bulk run (see /run-bulk). A descriptive label only —
    # nothing authorizes off it — stored on the job's indexed `batch_id` column so
    # /jobs can filter to the batch and roll up its status.
    batch_id: str = ""
    # BeyondTrust Password Safe managed-account checkout (LOCAL runner only). The
    # credential is checked out just-in-time at run time — the operator never sees
    # it. managed_account is the connection identity; managed_become is an optional
    # separate account for the become/sudo password.
    managed_account: ManagedAccountRef | None = None
    managed_become: ManagedAccountRef | None = None
    # ── Agent-executed runs ───────────────────────────────────────────────────
    # Set when the target sits on a network the dashboard has no route to, so the run must
    # be queued for a remote agent instead of a runner the dashboard launches. The inventory
    # page fills these from `inventory_service._target_spec`; the run form does not offer
    # them as free text.
    #
    # **Every one of these is RE-DERIVED server-side before a job is created** — see
    # ``_resolve_agent_target``. They are a proposal the endpoint checks against its own
    # rows, never an instruction. A client that could name an address and an agent could
    # aim someone else's agent at a host of its choosing, which is the same reason the
    # dashboard may never set an agent-bound connection's `host`.
    agent_id: str = ""        # RemoteAgent.id that can reach this target
    connection_id: str = ""   # the agent-bound hypervisor connection a VM was synced from
    transport: str = ""       # "ssh" | "winrm" | "local"
    port: int = 0             # the port on the target; 0 = derive from the transport


def _cfg(key: str) -> str:
    # Same two-line delegator the run service keeps; both resolve through
    # ansible_local_service._cfg, so there is no logic to drift.
    return ansible_local_service._cfg(key)





def _can_use_secrets(user) -> bool:
    """True if the user may use a secret in a run (without ever seeing it): an
    admin, an unrestricted (NULL-permission) legacy user, or one granted
    ``secrets:use``."""
    if getattr(user, "is_effective_admin", False):
        return True
    perms = user.effective_permissions_dict  # {} / NULL → unrestricted (legacy)
    if not perms:
        return True
    return "use" in perms.get("secrets", [])


# ── Cloud-runner secret injection (hardened per provider) ───────────────────────
# The per-provider resolution is pure (services/cloud_ansible_secrets); here we
# inject the real config_service / secrets-backend callables and map the module's
# StoreMismatch to an actionable HTTP 400.
def _effective_runner(cloud: str) -> str:
    """The Ansible runner backend that will actually handle a run for this target
    cloud — per-cloud override (ansible_runner_<cloud>) falling back to global."""
    runner = _cfg("ansible_runner") or "local"
    if cloud in ("aws", "azure", "gcp"):
        runner = _cfg(f"ansible_runner_{cloud}") or runner
    return runner


def _validate_cloud_secret_stores(runner: str, secret_vars: dict | None,
                                  secret_become_source: str) -> None:
    """For the ECS/GCP runners, require every named/become secret to reference that
    cloud's store. Raises HTTPException(400) otherwise; no-op for ACI/local. Pure
    prefix check (no backend I/O) so it can gate the request synchronously."""
    from ..services import config_service as cs, cloud_ansible_secrets as _cas
    try:
        _cas.validate_stores(runner, secret_vars, secret_become_source,
                             is_reference=cs.is_reference, get_raw=cs.get_raw)
    except _cas.StoreMismatch as exc:
        raise HTTPException(status_code=400, detail=str(exc))




def _resolve_agent_target(payload: "RunRequest", db) -> dict:
    """Verify an agent-executed run against the dashboard's own rows, and return the job's
    resolved target fields.

    The request may *propose* an agent and an address; this decides whether that is true.
    Everything returned is read from a row here, not copied from the body — so a caller who
    rewrites ``target`` or ``agent_id`` gets a 400 rather than a job that points an agent
    somewhere it was never told about. The agent's own ``policy.yaml`` is the second, and
    final, gate on the same question.

    Raises ``HTTPException(400/404)``; returns the overrides for
    ``agent_ansible_meta.run_meta``.
    """
    from ..database import CloudDatabase, HypervisorConnection, HypervisorVMCache, RemoteAgent
    from ..services import agent_ansible_meta, agent_service

    agent = db.query(RemoteAgent).filter(RemoteAgent.id == payload.agent_id).first()
    if not agent or not agent.is_active:
        raise HTTPException(status_code=404, detail="That remote agent is not registered.")
    if "agent_ansible" not in agent_service.allowed_job_types(agent):
        raise HTTPException(
            status_code=400,
            detail=(f"Agent '{agent.name}' is not granted the Config-Management job type. "
                    f"Grant it on the Agents page — this is the dashboard operator's half "
                    f"of the permission; the agent's own policy.yaml is the other half."))
    # Version gate at ENQUEUE, not at run: an older agent has no handler for this job type
    # and would refuse it into Live Output, where the message reads as a policy problem.
    if not agent_service.supports_ansible(agent):
        raise HTTPException(status_code=400,
                            detail=agent_service.ansible_upgrade_hint(agent))
    if agent_service.status_of(agent) != "online":
        raise HTTPException(
            status_code=400,
            detail=(f"Agent '{agent.name}' is not online, so this run would sit queued "
                    f"indefinitely. Nothing was queued."))

    if payload.target_kind == "k8s":
        # Out of scope for now, and refused by name rather than falling through to the VM
        # branch — which would fail on a missing hypervisor connection and read as a wiring
        # problem. An on-prem cluster has the identical shape to an on-prem database and
        # wants the same treatment; it just is not wired yet.
        raise HTTPException(
            status_code=400,
            detail=("Kubernetes clusters cannot yet be configured through a remote agent. "
                    "An on-premises cluster still runs on the dashboard's own runner, which "
                    "needs a route to the cluster's API server."))

    if payload.target_kind == "database":
        row = (db.query(CloudDatabase)
               .filter(CloudDatabase.id == payload.target_id).first())
        if not row:
            raise HTTPException(status_code=404, detail="No such database.")
        if (row.agent_id or "") != agent.id:
            raise HTTPException(
                status_code=400,
                detail=f"That database is not reachable through agent '{agent.name}'.")
        if not row.private_host:
            raise HTTPException(
                status_code=400,
                detail="That database has no endpoint recorded, so there is nothing for the "
                       "agent to connect to.")
        return {"run_kind": "database", "transport": "local",
                "target_host": row.private_host, "target_port": row.port or 0,
                "target_id": row.id, "connection_id": "",
                "target_label": f"{row.engine}/{row.id[:8]}"}

    # A VM: the connection must be bound to this agent, and the address must be one the
    # agent itself reported for that VM. That second check is what stops an arbitrary
    # address being substituted for a legitimately-synced one.
    conn = (db.query(HypervisorConnection)
            .filter(HypervisorConnection.id == payload.connection_id).first())
    if not conn or not conn.is_active:
        raise HTTPException(status_code=404, detail="No such hypervisor connection.")
    if (conn.agent_id or "") != agent.id:
        raise HTTPException(
            status_code=400,
            detail=f"That connection is not brokered by agent '{agent.name}'.")
    vm = (db.query(HypervisorVMCache)
          .filter(HypervisorVMCache.connection_id == conn.id,
                  HypervisorVMCache.vm_id == payload.target_id).first())
    if not vm:
        raise HTTPException(
            status_code=404,
            detail="That VM is not in this connection's synced inventory. Sync and retry.")
    try:
        ips = json.loads(vm.ip_addresses or "[]")
    except ValueError:
        ips = []
    ips = [str(i) for i in ips if str(i).strip()]
    if not ips:
        raise HTTPException(
            status_code=400,
            detail=("This VM reports no address, so there is nothing to run against. An "
                    "address needs the guest powered on, guest tools installed, and "
                    "`sync_guest_details: true` on this connection in the agent's "
                    "connections.yaml."))
    # The body's address must be one the AGENT reported. Equal, not merely plausible.
    host = payload.target if payload.target in ips else ips[0]
    transport = (payload.transport
                 if payload.transport in ("ssh", "winrm")
                 else agent_ansible_meta.transport_for_guest_os(vm.guest_os))
    return {"run_kind": "vm", "transport": transport, "target_host": host,
            "target_port": payload.port or 0, "target_id": vm.vm_id,
            "connection_id": conn.id,
            "target_label": vm.name or vm.vm_id}


async def _run_agent_ansible(payload: "RunRequest", db, current_user):
    """Enqueue a Config-Management run for a remote agent to execute.

    A distinct job type rather than an ``ansible_local`` row with a different runner, and
    that is forced rather than chosen: ``agent_service.AGENT_JOB_TYPES`` must stay disjoint
    from ``jobs_worker.HANDLED_TYPES`` or the local worker would race the agent for the same
    row, and ``create_job(agent_id=…)`` forces ``status="queued"`` where the local worker
    claims ``pending``.

    Only refs reach the job row. The playbook and every credential are assembled and sealed
    later, when the agent asks — see ``services/agent_ansible_bundle``.
    """
    from ..services import agent_ansible_bundle, agent_ansible_meta

    if ansible_local_service.asset_type(payload.asset) not in (
            "playbook", "script", "powershell", "rpm", "deb"):
        raise HTTPException(status_code=400, detail=f"Unsupported asset {payload.asset!r}.")

    # Refused here as well as on the agent so an operator who typed one gets a clean 400 now
    # rather than a puzzling refusal in Live Output half a minute later.
    offending = agent_ansible_bundle.reserved_vars(payload.extra_vars)
    if offending:
        raise HTTPException(
            status_code=400,
            detail=(f"{', '.join(offending)} cannot be set as extra vars: Ansible reads "
                    f"ansible_* variables as connection configuration, so one of them could "
                    f"redirect the play into the runner container instead of the target. "
                    f"Use the run form's own user / key / become fields instead."))

    if payload.secret_vars and not _can_use_secrets(current_user):
        raise HTTPException(
            status_code=403,
            detail="Using a Secrets-Management secret in a run requires the 'secrets:use' permission.")

    overrides = _resolve_agent_target(payload, db)
    asset_backend = payload.asset_backend or storage_service.active_backend()
    # NOT gated on local-filesystem storage, unlike the in-cloud runners: the DASHBOARD reads
    # the asset and puts the bytes in the sealed bundle, so a local/UNC backend works here
    # even though a Fargate task could never reach it.
    meta = agent_ansible_meta.run_meta(
        payload,
        description=f"Ansible (agent): {payload.asset} → {overrides['target_label']}",
        asset_backend=asset_backend, **overrides)
    problem = agent_ansible_meta.check(meta)
    if problem:
        raise HTTPException(status_code=400, detail=problem)

    job = job_service.create_job(
        db, job_type="agent_ansible", created_by=current_user.username,
        workgroup="ansible", metadata=meta, batch_id=payload.batch_id,
        agent_id=payload.agent_id)
    if payload.secret_vars:
        job_service.log_audit(
            db, current_user.username, "ansible_secret_use",
            details={"vars": sorted(payload.secret_vars.keys()), "asset": payload.asset,
                     "target": f"agent:{overrides['target_host']}"})
    return {"job_id": job.id, "status": "queued"}


async def _run_cloud_localhost(payload: "RunRequest", db, current_user):
    """Enqueue a Kubernetes-cluster / cloud-database Config-Management run.

    These are localhost plays that reach out via a kubeconfig / DB login vars, so
    the SSH-oriented request fields are ignored. Connection material is resolved
    server-side at launch (never here, never stored on the job). The run executes on
    the in-cloud transient runner, or — for a cloud="local" Kubernetes cluster, which
    only this host can reach — the local sibling container (jobs_worker →
    ansible_cloud_run_service.resolve_runner).
    Returns ``{job_id, status: "queued"}``; the client polls /api/jobs/{id}."""
    from ..services import (k8s_service, cloud_database_service,
                            ansible_cloud_run_service as acr)

    kind = payload.target_kind
    if not payload.target_id:
        raise HTTPException(status_code=400, detail=f"target_id is required for a {kind} run.")

    # A localhost play must be a real playbook — no auto-wrapped script/rpm/deb.
    if ansible_local_service.asset_type(payload.asset) != "playbook":
        raise HTTPException(
            status_code=400,
            detail=f"{kind} targets run a localhost play — supply a .yml/.yaml playbook.")

    # Resolve the target row (→ 404) and derive its cloud.
    if kind == "k8s":
        try:
            cluster = k8s_service.get_cluster(db, payload.target_id)
        except k8s_service.K8sError as e:
            raise HTTPException(status_code=404, detail=str(e))
        cloud = (cluster.get("cloud") or "").lower()
        target_label = cluster.get("name") or payload.target_id[:8]
    else:  # database
        try:
            info = cloud_database_service.connection_info(db, payload.target_id)
        except cloud_database_service.CloudDatabaseError as e:
            raise HTTPException(status_code=404, detail=str(e))
        cloud = (info.get("cloud") or "").lower()
        engine = info.get("engine")
        if engine not in acr.ANSIBLE_DB_ENGINES:
            raise HTTPException(
                status_code=400,
                detail=(f"engine {engine!r} is not supported for Ansible runs "
                        f"(supported: {', '.join(acr.ANSIBLE_DB_ENGINES)})."))
        if not info.get("private_host"):
            raise HTTPException(
                status_code=400,
                detail="database has no endpoint yet — wait for provisioning to finish.")
        target_label = f"{engine}/{payload.target_id[:8]}"

    # Targetable cloud + reachable asset storage. Both conditions turn on where the
    # runner executes, so they're resolved together in the service (unit-tested there).
    asset_backend = payload.asset_backend or storage_service.active_backend()
    problem = acr.check_target(kind, cloud, asset_backend, payload.asset)
    if problem:
        raise HTTPException(status_code=400, detail=problem)

    # Only operator-picked named secret_vars apply to a localhost play (no SSH key /
    # become / managed-account). Using one requires the secrets:use permission.
    wants_secret = bool(payload.secret_vars)
    if wants_secret and not _can_use_secrets(current_user):
        raise HTTPException(
            status_code=403,
            detail="Using a Secrets-Management secret in a run requires the 'secrets:use' permission.")

    description = f"Ansible ({kind}): {payload.asset} → {target_label}"
    job = job_service.create_job(
        db,
        job_type="ansible_cloud_run",
        created_by=current_user.username,
        workgroup="ansible",
        # Refs only — no resolved credential is ever written to job metadata.
        metadata={
            "description": description,
            "target_kind": kind,
            "target_id": payload.target_id,
            "cloud": cloud,
            "asset": payload.asset,
            "asset_backend": asset_backend,
            "extra_vars": payload.extra_vars or {},
            "secret_vars": payload.secret_vars or {},
        },
        batch_id=payload.batch_id,
    )
    if wants_secret:
        job_service.log_audit(
            db, current_user.username, "ansible_secret_use",
            details={"kinds": [f"{len(payload.secret_vars)} var(s)"],
                     "vars": sorted(payload.secret_vars.keys()),
                     "asset": payload.asset, "target": f"{kind}:{payload.target_id}"})
    return {"job_id": job.id, "status": "queued"}


@router.post("/run")
async def run_playbook(
    payload: RunRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Run an asset against a target as a background job.

    target must be one of the configured hypervisor group keys returned by
    /api/config-mgmt/inventory, or a bare IP / hostname for ad-hoc cloud runs.
    For cloud targets, set cloud="aws"|"azure"|"gcp" to enable SSH key retrieval.

    When target_kind is "k8s" or "database", target_id selects a managed Kubernetes
    cluster / cloud database; the run is a localhost play on the in-cloud runner and
    the SSH-oriented fields are ignored (see _run_cloud_localhost).
    """
    # Checked FIRST, and before the k8s/database split, because it is the reachability
    # question rather than the target-family one: an on-prem database bound to an agent is
    # target_kind="database" and still cannot use any runner the dashboard launches.
    if payload.agent_id:
        return await _run_agent_ansible(payload, db, current_user)

    if payload.target_kind in ("k8s", "database"):
        return await _run_cloud_localhost(payload, db, current_user)

    targets = ansible_local_service.get_configured_targets(db)
    valid_keys = {t["key"] for t in targets}

    # Bare IP/hostname targets (contain a dot or colon) are allowed ad-hoc.
    is_adhoc = "." in payload.target or ":" in payload.target
    if not is_adhoc and payload.target not in valid_keys:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Target '{payload.target}' is not a configured hypervisor. "
                f"Configured: {sorted(valid_keys) or '(none — enable integrations in Settings)'}."
            ),
        )

    # Issue #16: with multi-backend storage, the same asset name can exist on
    # local *and* on a cloud backend. Cloud-side ansible runners (ECS task,
    # ACI, Cloud Run) cannot reach the dashboard's local filesystem, so refuse
    # the local-asset + cloud-target combo up front with an actionable error
    # rather than letting the runner blow up partway through.
    asset_backend = payload.asset_backend or storage_service.active_backend()
    is_cloud_target = bool(payload.cloud) or (is_adhoc and not payload.target.startswith(("10.", "192.168.", "172.")))
    runner = _cfg("ansible_runner") or "local"
    runs_in_cloud_runner = runner in ("ecs", "aci", "gcp")
    if asset_backend == "local" and (is_cloud_target or runs_in_cloud_runner):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Asset '{payload.asset}' lives on local filesystem storage, "
                f"which the cloud-side ansible runner cannot reach. Open the "
                f"Storage page and use the Move action to copy this asset to "
                f"a cloud backend (S3 / Azure Blob / GCS), then re-run the job."
            ),
        )

    # Using a Secrets-Management secret in a run requires the `secrets:use`
    # permission (admins bypass) — the operator never sees the value. Named-var and
    # become-password secrets work on both the local and the cloud runners; on the
    # cloud they are injected via the provider's secret channel (ECS valueFrom /
    # Cloud Run secret-env / ACI secure_value).
    has_managed = bool(payload.managed_account or payload.managed_become)
    wants_secret = bool(payload.secret_vars or payload.secret_become_source
                        or payload.secret_ssh_key_source or has_managed)
    if wants_secret and not _can_use_secrets(current_user):
        raise HTTPException(
            status_code=403,
            detail="Using a Secrets-Management secret in a run requires the 'secrets:use' permission.")

    # A managed-account checkout needs BeyondTrust Password Safe enabled.
    if has_managed:
        from ..services import config_service as cs
        if not cs.get_bool("password_safe_enabled"):
            raise HTTPException(
                status_code=400,
                detail="Managed-account checkout requires BeyondTrust Password Safe to be enabled in Settings.")

    atype = ansible_local_service.asset_type(payload.asset)

    # A cloud run only actually uses the cloud runner for bare-IP playbook targets;
    # otherwise it falls back to local. When it will run on ECS/GCP, every named/
    # become secret must already live in that cloud's store (fail fast with an
    # actionable move-it message rather than a mid-job failure). ACI takes the value
    # inline, so no store requirement.
    eff_runner = _effective_runner(payload.cloud)
    if (wants_secret and eff_runner in ("ecs", "aci", "gcp")
            and is_adhoc and atype == "playbook"):
        _validate_cloud_secret_stores(
            eff_runner, payload.secret_vars, payload.secret_become_source)

    # Managed-account checkout works on the local and ACI runners (both inject the
    # credential inline). ECS / Cloud Run reference a store secret, so a JIT-checked-
    # out credential needs an ephemeral, RBAC-locked store copy — gated behind an
    # explicit opt-in (it copies a PAM-vaulted credential into the cloud store for
    # the run). Rejected up front when that isn't enabled.
    from ..services import managed_accounts as _ma, config_service as _cs2
    if _ma.requires_ephemeral_store(has_managed, eff_runner, is_adhoc, atype == "playbook"):
        if not _cs2.get_bool("ansible_cloud_ephemeral_secrets_enabled"):
            raise HTTPException(
                status_code=400,
                detail=("Managed-account checkout on the ECS / Cloud Run runners requires "
                        "'Ephemeral cloud secrets' to be enabled in Settings (it briefly copies "
                        "the credential into the cloud store, RBAC-locked). Otherwise use the "
                        "local or Azure (ACI) runner."))
        if eff_runner == "gcp" and not _cfg("gcp_ansible_runner_service_account"):
            raise HTTPException(
                status_code=400,
                detail=("GCP ephemeral secrets require 'gcp_ansible_runner_service_account' to be "
                        "set — the Cloud Run job runs as that SA and read access to the ephemeral "
                        "secret is locked to it."))
    description = f"Ansible ({atype}): {payload.asset} → {payload.target}"

    # Everything the run needs, persisted so the durable runner can reconstruct it.
    # Refs and ids only — see services/ansible_run_meta for what may go in here.
    job = job_service.create_job(
        db,
        job_type="ansible_local",
        created_by=current_user.username,
        workgroup="ansible",
        metadata=ansible_run_meta.run_meta(
            payload, description=description, asset_backend=asset_backend),
        batch_id=payload.batch_id,
    )
    if wants_secret:
        # Audit the use — kinds + var names only, never the source refs or values.
        kinds = []
        if payload.secret_vars:
            kinds.append(f"{len(payload.secret_vars)} var(s)")
        if payload.secret_become_source:
            kinds.append("become-password")
        if payload.secret_ssh_key_source:
            kinds.append("ssh-key")
        # Managed-account use — record kind + account name(s) + system, never the credential.
        managed_accts = []
        if payload.managed_account:
            kinds.append("managed-account (checkout)")
            managed_accts.append({"role": "connection",
                                  "account": payload.managed_account.account_name,
                                  "system_id": payload.managed_account.system_id})
        if payload.managed_become:
            kinds.append("managed-account become (checkout)")
            managed_accts.append({"role": "become",
                                  "account": payload.managed_become.account_name,
                                  "system_id": payload.managed_become.system_id})
        job_service.log_audit(
            db, current_user.username, "ansible_secret_use",
            details={"kinds": kinds, "vars": sorted(payload.secret_vars.keys()),
                     "managed_accounts": managed_accts,
                     "asset": payload.asset, "target": payload.target})
    # No background task: the job is a queued row now, claimed by jobs_worker. Its
    # parameters live in the metadata written above, so a worker restart resumes it
    # instead of stranding it — see services/ansible_run_meta.py.
    return {"job_id": job.id, "status": "queued"}


class BulkRunRequest(BaseModel):
    """A run against several inventory rows at once. The targets are named by
    INVENTORY ID (``job:…`` / ``k8s:…`` / ``clouddb:…``), never by address — the
    server resolves each one from its own records, so a client cannot point a run at
    a host it doesn't own by supplying an IP."""
    inventory_ids: list[str] = []
    asset: str
    asset_backend: str = ""
    extra_vars: dict = {}
    secret_vars: dict = {}
    # VM-only connection fields; ignored for k8s/database rows (localhost plays).
    ansible_user: str = ""
    secret_become_source: str = ""
    secret_ssh_key_source: str = ""
    managed_account: ManagedAccountRef | None = None
    managed_become: ManagedAccountRef | None = None


@router.post("/run-bulk")
async def run_playbook_bulk(
    payload: BulkRunRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Run one asset against several inventory resources — one job per target.

    Validation happens at two levels, and they fail differently on purpose:

    * SELECTION problems — mixed kinds, a kind with no Config-Management path, a row
      that isn't individually targetable, an id the caller can't see — are checked
      before any job exists and refuse the whole request with a 400.
    * PER-TARGET problems are found only when a target is dispatched, and they do not
      necessarily apply to the rest of the batch: several checks in ``/run`` turn on
      the target's cloud (``_effective_runner`` and everything downstream of it), so
      a mixed-cloud VM selection can be fine for one host and not another. Those
      targets are reported in ``failed`` and the remaining ones still run, rather
      than being silently dropped or aborting a batch that is already part-queued.

    Each target is dispatched through the ordinary ``/run`` path, so every permission
    check, secret-store validation and runner decision behaves exactly as it does for
    a single run — this endpoint adds selection, not a second code path. Jobs share a
    ``batch_id``, so /jobs can filter to the batch and roll up how it is going.

    Returns ``{batch_id, kind, count, jobs: [...], failed: [...]}``; 400 if every
    target failed.
    """
    from ..services import inventory_service

    # Resolved from a FRESH collect(), not the page's cache: this enqueues work, so
    # it must not act on a resource that was destroyed since the page loaded. The
    # RBAC filter is the inventory page's own — an id outside what this user can see
    # comes back as "unknown" rather than as a target.
    accessible = inventory_service.accessible_workgroups(current_user)
    visible = [i for i in inventory_service.collect(db)
               if inventory_service.visible_to(i, accessible, current_user.username)]
    try:
        plan = inventory_service.plan_bulk_run(visible, payload.inventory_ids)
    except inventory_service.BulkSelectionError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # A k8s/database run is a localhost play with no SSH connection; the run path
    # silently ignores the connection-identity fields. Across a batch that silence
    # would be misleading, so refuse instead.
    wrong_fields = inventory_service.reject_connection_fields(plan["kind"], {
        "secret_ssh_key_source": payload.secret_ssh_key_source,
        "secret_become_source": payload.secret_become_source,
        "managed_account": payload.managed_account,
        "managed_become": payload.managed_become,
    })
    if wrong_fields:
        raise HTTPException(status_code=400, detail=wrong_fields)

    batch_id = uuid.uuid4().hex[:12]
    jobs, failed = [], []
    for target in plan["targets"]:
        req = RunRequest(
            asset=payload.asset,
            asset_backend=payload.asset_backend,
            extra_vars=payload.extra_vars,
            secret_vars=payload.secret_vars,
            ansible_user=payload.ansible_user,
            secret_become_source=payload.secret_become_source,
            secret_ssh_key_source=payload.secret_ssh_key_source,
            managed_account=payload.managed_account,
            managed_become=payload.managed_become,
            batch_id=batch_id,
            **target["spec"],
        )
        try:
            result = await run_playbook(req, db, current_user)
        except HTTPException as e:
            failed.append({"inventory_id": target["id"], "name": target["name"],
                           "error": str(e.detail)})
            continue
        jobs.append({"inventory_id": target["id"], "name": target["name"],
                     "job_id": result["job_id"]})

    if not jobs:
        # Nothing queued — surface the first reason rather than a misleading success.
        detail = failed[0]["error"] if failed else "No targets could be run."
        raise HTTPException(
            status_code=400,
            detail=f"No jobs were queued. First target failed with: {detail}")

    job_service.log_audit(
        db, current_user.username, "ansible_bulk_run",
        details={"batch_id": batch_id, "kind": plan["kind"], "asset": payload.asset,
                 "count": len(jobs), "targets": [j["name"] for j in jobs],
                 "failed": [f["name"] for f in failed]})
    return {"batch_id": batch_id, "kind": plan["kind"], "count": len(jobs),
            "jobs": jobs, "failed": failed}


@router.get("/secret-options")
async def list_secret_options(current_user: User = Depends(get_current_user)):
    """Secret sources the operator can use in a run — **names only, never
    values**. Requires ``secrets:use`` (admins bypass); the run form uses this to
    populate the secret picker."""
    if not _can_use_secrets(current_user):
        raise HTTPException(status_code=403, detail="The 'secrets:use' permission is required.")
    from ..services import config_service as cs
    from .secrets import _SECRET_REGISTRY

    out = []
    for key, desc in _SECRET_REGISTRY:
        # get_raw() reads the correctly-keyed global row; the cache is keyed on
        # (key, None) tuples, so a bare-key _cache.get(key) always misses.
        has = bool(cs.get_raw(key))
        out.append({"key": key, "description": desc, "has_value": has})
    return out


@router.get("/managed-accounts")
async def list_managed_accounts(
    host: str,
    name: str = "",
    current_user: User = Depends(get_current_user),
):
    """Live BeyondTrust Password Safe managed-account list for a target host —
    **ids + names only, never credentials**. Requires ``secrets:use`` (using a
    managed account = checking out a credential without seeing it). The run form
    calls this on target change to populate the account picker.

    ``host`` is the connection address (a cloud VM's IP or a free-text on-prem
    host); ``name`` is an optional system-name hint the run form passes for a cloud
    VM (its deploy name). Cloud-native onboarding — e.g. the AWS Systems Manager
    Password Safe plugin — registers the managed system keyed on the instance name
    with a placeholder IP, so an IP-only lookup never finds it; the name hint does.

    Returns ``{"enabled": false, "systems": []}`` when BeyondTrust is off (no
    ps-cli call), and never 500s a lookup — a ps-cli error yields an ``error`` note
    with an empty list so the UI can surface it inline."""
    if not _can_use_secrets(current_user):
        raise HTTPException(status_code=403, detail="The 'secrets:use' permission is required.")

    from ..services import config_service as cs, btapi_service, managed_accounts as ma

    # ephemeral_enabled tells the UI that managed accounts can run on ECS/GCP (via
    # the ephemeral store copy) and to nudge on change-after-release for those.
    ephemeral_enabled = cs.get_bool("ansible_cloud_ephemeral_secrets_enabled")
    if not cs.get_bool("password_safe_enabled"):
        return {"enabled": False, "ephemeral_enabled": ephemeral_enabled, "systems": []}

    host = (host or "").strip()
    if not host:
        return {"enabled": True, "ephemeral_enabled": ephemeral_enabled, "systems": []}

    ip, name = ma.lookup_args(host, name)
    try:
        systems = await btapi_service.list_ps_managed_systems_by_ip_or_name(ip, name)
        accounts_by_system: dict = {}
        for s in systems:
            sid = s.get("ManagedSystemID") or s.get("SystemId") or s.get("SystemID")
            if sid is None:
                continue
            accounts_by_system[int(sid)] = \
                await btapi_service.list_ps_managed_accounts_with_fallback(int(sid))
        return {"enabled": True, "ephemeral_enabled": ephemeral_enabled,
                "systems": ma.normalize_managed_systems(systems, accounts_by_system)}
    except btapi_service.BTAPIError as exc:
        # Log the real ps-cli error server-side; return a generic reason. A raw
        # BTAPIError string carries ps-cli stderr, so returning it here would leak
        # internal detail to the caller — CodeQL py/stack-trace-exposure.
        logger.warning("managed-account lookup for %r failed: %s", host, exc)
        return {"enabled": True, "ephemeral_enabled": ephemeral_enabled, "systems": [],
                "error": "Password Safe lookup failed — check the BeyondTrust configuration and server logs."}


@router.get("/drift")
async def config_drift_report(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Per-target config-drift signals for the Ansible stream: **unverified**
    (last apply older than ``config_drift_stale_days``) and **changed** (the
    stored playbook's current content differs from what was applied). Read-only —
    computed from the ``config_apply_state`` rows recorded on each successful run."""
    from ..services import config_drift

    return await config_drift.collect(db)
