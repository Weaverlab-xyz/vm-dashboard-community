"""SPIRE trust-domain lab lifecycle, for the Password Safe SPIFFE SVID plugin.

Stands the lab up on a Linux VM **the dashboard already deployed**, and takes it down
again. In order: open tcp/8081 on the cloud's own network ACL, run the four playbooks in
``examples/playbooks/spire/`` against the host over SSH, then read back the two public
values an operator has to carry into BeyondInsight by hand — the trust bundle and the
titles of the admin credential in Secrets Safe.

**It does not create the VM, deliberately.** A SPIRE server is a Go binary and a sqlite
file; the host is an ordinary VM, so it already has an auto-delete timer, ref-counted
NAT, Password Safe VM onboarding and a Destroy button. Re-implementing three clouds'
worth of VM creation here would also break the thing that makes the Ansible runs work at
all: ``ansible_local_run_service`` resolves the SSH key from the *deploy job's* metadata,
so a VM this module created out of band would be a host the runner cannot log in to.

**It does not write the Password Safe managed system or functional account, deliberately
and for now.** The plugin takes its whole configuration from BeyondInsight *attributes*,
and whether the gateway populates those for a plugin action is unresolved — see
``docs/runbooks/spire-lab-standup.md`` §5. Writing an attribute writer before that
question is answered would be betting on the answer. So this feature gets an operator to
the point where §5 takes two minutes, and stops.

**Why a row and a timer.** Not cost — the VM's own timer covers that. A forgotten trust
domain keeps minting: 8081 is an API that issues identities, so a lab nobody remembers is
an identity provider nobody is watching. The timer's teardown closes the ACL, which is
the reachability kill switch; deregistering the managed system is the other one, and the
plugin implements no Enable/Disable Managed Account.

Per-cloud differences are confined to :data:`_HOST_BACKENDS`. The playbooks are
cloud-agnostic — they configure a Linux host over SSH — so the only thing that varies is
how the network ACL is opened, which is a genuinely different API and a genuinely
different resource on each of the three.
"""

import json
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from ..database import Job, SpireLab
from . import expiry_policy, job_service

logger = logging.getLogger(__name__)

PROVISION_JOB_TYPE = "spirelab_provision"
DECOMMISSION_JOB_TYPE = "spirelab_decommission"

# The inventory kind. Mirrors "certlab": a first-class reapable resource with a row of
# its own, not a Job row like a VM.
INVENTORY_KIND = "spirelab"

VALID_CLOUDS = ("azure", "gcp", "aws")

# The SPIRE server API. gRPC over mutual TLS — which is why nothing may terminate TLS in
# front of it: a terminating proxy strips the client certificate and the server rejects
# the call as unauthenticated, a symptom that reads as a credential problem.
BIND_PORT = 8081

# The name of the one ingress rule this feature owns, on every cloud. Deliberately not
# the managed-node rule name: opening 8081 for a lab must not disturb the management
# ingress on a VM that is also a Rancher or Portainer node.
RULE_NAME = "allow-spire-api"

# What the seed puts in, and what discovery should return. **The count is the assertion,
# not "discovery succeeded".** The plugin shipped with discovery defaulting its path
# filter to the MINTABLE prefix, so configuring minting silently narrowed the inventory
# to 2 accounts while the action still reported success. The three exclusions are one
# node/agent entry, one ``-admin`` and one ``-downstream``.
ENTRIES_SEEDED = 11
DISCOVERY_EXPECTED = 8


class SpireLabError(Exception):
    """Raised when a SPIRE lab cannot be built or torn down."""


def _cfg(key: str, default: str = "") -> str:
    try:
        from . import config_service
        val = config_service.get(key)
        if val not in (None, ""):
            return str(val)
    except Exception:
        pass
    from ..config import settings
    val = getattr(settings, key, None)
    return default if val in (None, "") else str(val)


def source_cidrs() -> list:
    """The sources allowed to reach tcp/8081, from ``spire_lab_source_cidrs``.

    **Blank means no ACL change is made at all** — not "open to the world". 8081 mints
    identities, so the only safe reading of "I did not say who may reach it" is "do not
    let anyone new reach it". `run_provision` says so in the job output rather than
    failing, because a broker already inside the VNet needs no rule and that lab is
    perfectly valid.

    Pin every address the caller actually egresses from. Corporate egress commonly
    rotates between two addresses, and a rule holding one of them fails on about half the
    connections — which presents as an intermittent credential fault, not a network one.
    """
    raw = _cfg("spire_lab_source_cidrs")
    return [c.strip() for c in raw.replace(";", ",").split(",") if c.strip()]


# ── The four playbooks, in the only order that works ──────────────────────────
# `key` is what lands in `stages_done`, so it is a stable identifier and not a label.
# `vars_for` returns the run's extra_vars; everything it reads comes from the row or
# from config, never from the request, so a resumed job builds the identical run.

def _install_vars(row: SpireLab) -> dict:
    out = {"trust_domain": row.trust_domain,
           "bind_port": row.bind_port or BIND_PORT,
           "spire_version": _cfg("spire_lab_version", "1.15.3"),
           # ca_ttl CAPS every SVID the server issues, including the admin credential:
           # `-ttl 720h` against the 168h default yields ~7 days and SPIRE says so
           # rather than failing. Raising it here is how a lab outlives a week.
           "ca_ttl": _cfg("spire_lab_ca_ttl", "168h")}
    if row.admin_spiffe_id:
        out["admin_spiffe_id"] = row.admin_spiffe_id
    return out


def _ports_vars(row: SpireLab) -> dict:
    # The same source set the cloud ACL got, so the two gates cannot disagree. They fail
    # identically — a gRPC timeout on Verify Functional Account — so a lab where one is
    # narrower than the other is the hardest version of this to debug.
    return {"bind_port": row.bind_port or BIND_PORT,
            "spire_source_cidrs": _row_cidrs(row)}


def _seed_vars(row: SpireLab) -> dict:
    return {"trust_domain": row.trust_domain}


def _identity_vars(row: SpireLab) -> dict:
    out = {"trust_domain": row.trust_domain,
           "admin_secret_folder": row.admin_secret_folder or "",
           "ps_safe": row.ps_safe or _cfg("spire_lab_ps_safe", "Automation"),
           "admin_ttl": _cfg("spire_lab_admin_ttl", "720h")}
    if row.admin_spiffe_id:
        out["admin_spiffe_id"] = row.admin_spiffe_id
    return out


STAGES = (
    {"key": "install", "asset": "spire-server-install.yml", "vars_for": _install_vars,
     "pct": 25, "label": "Installing the SPIRE server…"},
    {"key": "ports", "asset": "spire-open-ports.yml", "vars_for": _ports_vars,
     "pct": 45, "label": "Opening the host firewall…"},
    {"key": "seed", "asset": "spire-seed-entries.yml", "vars_for": _seed_vars,
     "pct": 65, "label": "Seeding the registration entries…"},
    {"key": "identity", "asset": "spire-admin-identity.yml", "vars_for": _identity_vars,
     "pct": 85, "label": "Minting the administrative credential…"},
)

STAGE_ASSETS = tuple(s["asset"] for s in STAGES)

# The titles `spire-admin-identity.yml` writes into Secrets Safe, under the lab's folder.
# The first two are the credential — never read by this service, only named. The second
# two are public values the playbook publishes so the dashboard has a VALUE channel out
# of the run instead of parsing an Ansible log.
SECRET_TITLES = {
    "pfx": "admin-pfx-b64",
    "passphrase": "admin-pfx-pass",
    "bundle": "trust-bundle-pem",
    "expires": "admin-svid-expires",
}


# ── Per-cloud host backends ───────────────────────────────────────────────────
# Everything that differs between clouds lives HERE, so the orchestration in
# `run_provision` — job lifecycle, stage sequencing, progress, artifact read-back — is
# written once and does not grow a branch per cloud.
#
# The interface is narrow because only one thing actually varies: opening a port to an
# existing VM. It is a different resource on each cloud (an NSG rule, a VPC firewall rule
# keyed on a network tag, a security-group permission), which is why this is an interface
# rather than three `if`s.
#
#   * `deploy_job_type` — the job type whose completed rows ARE the host inventory for
#     this cloud. There is no separate VM table; `/cloud-targets` reads the same rows.
#   * `placement(meta)` — the identifiers the ingress call needs, from that job's
#     metadata. Raises if the metadata predates something required.
#   * `apply_ingress(placement, ports, source_cidrs)` — converge one rule. FAIL-CLOSED
#     on an empty source set on every cloud: `opened` is False, and callers key off that
#     rather than off the absence of an error.


class _AzureHost:
    cloud = "azure"
    deploy_job_type = "azure_deploy"
    default_user_cfg = "ansible_azure_user"
    # An inbound NSG rule. Azure evaluates the NIC's NSG before the subnet's, so the rule
    # has to go on whichever one governs the VM — writing it elsewhere produces a rule
    # that exists and does nothing.
    acl_label = "Network Security Group"

    @staticmethod
    def placement(meta: dict) -> dict:
        rg = meta.get("resource_group") or _cfg("azure_resource_group", "vm-cli-rg")
        vm_name = meta.get("vm_name") or ""
        if not vm_name:
            raise SpireLabError("the Azure deploy job records no vm_name")
        return {"resource_group": rg, "vm_name": vm_name,
                "location": meta.get("location") or ""}

    @staticmethod
    async def apply_ingress(placement: dict, ports: list, cidrs: list) -> dict:
        from . import azure_service
        res = await azure_service.ensure_vm_inbound_rule(
            placement["resource_group"], placement["vm_name"],
            rule_name=RULE_NAME, ports=ports, source_cidrs=cidrs,
            location=placement.get("location") or "")
        return {"opened": bool(res.get("opened")), "name": res.get("nsg") or "",
                "detail": res}


class _GcpHost:
    cloud = "gcp"
    deploy_job_type = "gce_deploy"
    default_user_cfg = "ansible_gcp_user"
    # A VPC firewall rule targeting a network TAG, because GCE firewall rules cannot
    # name an instance. `apply_ingress` adds the tag to the instance as part of opening
    # the port — which is why a rule alone, applied by hand, does nothing.
    acl_label = "VPC firewall rule"

    @staticmethod
    def placement(meta: dict) -> dict:
        project = meta.get("project_id") or _cfg("gcp_project_id")
        zone = meta.get("zone") or _cfg("gcp_zone")
        name = meta.get("instance_name") or ""
        if not (project and zone and name):
            raise SpireLabError(
                "the GCE deploy job records no project/zone/instance_name — the "
                "firewall rule and the instance tag both need all three")
        return {"project_id": project, "zone": zone, "instance_name": name}

    @staticmethod
    async def apply_ingress(placement: dict, ports: list, cidrs: list) -> dict:
        from . import gcp_service
        res = await gcp_service.ensure_instance_inbound_rule(
            placement["project_id"], placement["zone"], placement["instance_name"],
            rule_name=RULE_NAME, ports=ports, source_cidrs=cidrs)
        return {"opened": bool(res.get("opened")), "name": res.get("firewall") or "",
                "detail": res}


class _AwsHost:
    cloud = "aws"
    deploy_job_type = "ec2_deploy"
    default_user_cfg = "ansible_aws_user"
    # An inbound permission on the instance's own security group. Unlike the other two
    # there is no separate resource to create — which also means an empty source set
    # REVOKES rather than deletes, since an in-use security group cannot be removed.
    acl_label = "security group"

    @staticmethod
    def placement(meta: dict) -> dict:
        region = meta.get("region") or _cfg("aws_region")
        instance_id = meta.get("instance_id") or ""
        if not (region and instance_id):
            raise SpireLabError(
                "the EC2 deploy job records no region/instance_id — an instance id "
                "alone does not say which regional endpoint owns it")
        return {"region": region, "instance_id": instance_id}

    @staticmethod
    async def apply_ingress(placement: dict, ports: list, cidrs: list) -> dict:
        from . import aws_service
        res = await aws_service.ensure_instance_inbound_rule(
            placement["region"], placement["instance_id"],
            ports=ports, source_cidrs=cidrs, description=RULE_NAME)
        return {"opened": bool(res.get("opened")), "name": res.get("group_id") or "",
                "detail": res}


# Keyed by cloud. A cloud in VALID_CLOUDS but absent here has no way to open its ACL, so
# it cannot host a lab — which is what PROVISIONING_CLOUDS is DERIVED from rather than
# maintained beside, so the two cannot drift and no operator is handed a lab row whose
# server nothing can reach.
_HOST_BACKENDS = {
    "azure": _AzureHost,
    "gcp": _GcpHost,
    "aws": _AwsHost,
}

PROVISIONING_CLOUDS = tuple(c for c in VALID_CLOUDS if c in _HOST_BACKENDS)


def host_backend(cloud: str):
    """The backend for this cloud, or None when the dashboard cannot open its ACL."""
    return _HOST_BACKENDS.get((cloud or "").lower())


def require_backend(cloud: str):
    backend = host_backend(cloud)
    if not backend:
        raise SpireLabError(
            f"no SPIRE lab host backend for cloud {cloud!r} — built for "
            f"{', '.join(PROVISIONING_CLOUDS)}")
    return backend


# ── Reads ─────────────────────────────────────────────────────────────────────

def list_labs(db: Session, workgroup: Optional[str] = None) -> list:
    q = db.query(SpireLab)
    if workgroup:
        q = q.filter(SpireLab.workgroup == workgroup)
    return q.order_by(SpireLab.created_at.desc()).all()


def get_lab(db: Session, lab_id: str) -> Optional[SpireLab]:
    return db.query(SpireLab).filter(SpireLab.id == lab_id).first()


def _row_cidrs(row: SpireLab) -> list:
    return [c for c in (row.source_cidrs or "").split(",") if c]


def _stages_done(row: SpireLab) -> list:
    return [s for s in (row.stages_done or "").split(",") if s]


def stage_jobs(row: SpireLab) -> dict:
    """``{stage key: ansible_local job id}``. Public because the page links each stage's
    Live Output, and a failed stage's Ansible error exists nowhere else."""
    try:
        return json.loads(row.stage_job_ids or "{}")
    except Exception:
        return {}


def secret_refs(row: SpireLab) -> dict:
    """``{role: "<folder>/<title>"}`` for the four artifacts the identity play writes.

    These are the strings an operator pastes into BeyondInsight, and the strings
    ``secrets_backend_service.read_bt_secrets_safe`` takes. The credential's two are
    NAMED here and never read by this service — the functional account's DSS-key field
    is the protected place built for them.
    """
    folder = (row.admin_secret_folder or "").strip("/")
    if not folder:
        return {}
    return {role: f"{folder}/{title}" for role, title in SECRET_TITLES.items()}


def resolve_host(db: Session, cloud: str, host_ref: str) -> dict:
    """The dashboard-deployed VM named by ``host_ref``, as ``{name, private_ip,
    public_ip, meta, deploy_job_id}``.

    ``host_ref`` is a NAME or an IP, and it is re-derived against this dashboard's own
    deploy rows rather than trusted: the caller supplies a proposal, and a request that
    could name an arbitrary address would be a request to run four privileged playbooks
    against a host of the caller's choosing. Same discipline as
    ``api.config_mgmt._resolve_agent_target``.

    Reads completed deploy jobs, which is the same source of truth ``/cloud-targets``
    uses — the cloud tabs' cache is empty on a fresh restart and after every deploy.
    """
    backend = require_backend(cloud)
    ref = (host_ref or "").strip()
    if not ref:
        raise SpireLabError("a host is required — pick a VM this dashboard deployed")
    jobs = (db.query(Job)
            .filter(Job.job_type == backend.deploy_job_type, Job.status == "completed")
            .order_by(Job.created_at.desc()).all())
    for job in jobs:
        meta = job.metadata_dict or {}
        if meta.get("destroyed"):
            continue
        name = meta.get("vm_name") or meta.get("instance_name") or ""
        private_ip = meta.get("private_ip") or ""
        public_ip = meta.get("public_ip") or ""
        if ref not in (name, private_ip, public_ip):
            continue
        if not (private_ip or public_ip):
            continue
        return {"name": name, "private_ip": private_ip, "public_ip": public_ip,
                "meta": meta, "deploy_job_id": job.id}
    raise SpireLabError(
        f"no live {cloud} VM this dashboard deployed matches {ref!r}. The lab attaches "
        f"to an existing VM — deploy one from the {cloud} cloud page first, on a subnet "
        f"the Resource Broker can reach.")


def _slug(text: str) -> str:
    out = "".join(ch if (ch.isalnum() or ch in "-_") else "-"
                  for ch in (text or "").lower()).strip("-")
    return out[:48] or "spire"


# ── Provision ─────────────────────────────────────────────────────────────────

def provision(db: Session, *, name: str, trust_domain: str, cloud: str, host: str,
              created_by: str, admin_spiffe_id: str = "",
              workgroup: Optional[str] = None) -> dict:
    """Record the lab and enqueue its build. Returns ``{lab_id, job_id}``."""
    cloud = (cloud or "azure").lower()
    require_backend(cloud)                    # fail here, not in the worker
    name = (name or "").strip()
    trust_domain = (trust_domain or "").strip().lower()
    if not name:
        raise SpireLabError("a lab needs a name")
    if not trust_domain:
        raise SpireLabError("a trust domain is required")
    if "/" in trust_domain or trust_domain.startswith("spiffe:"):
        raise SpireLabError(
            "the trust domain is a bare DNS-style name such as 'weaverlab.test', not a "
            "spiffe:// URI. It is baked into the server config, every SPIFFE ID and the "
            "Managed System, and the plugin asserts it on every connect.")

    host_info = resolve_host(db, cloud, host)
    placement = require_backend(cloud).placement(host_info["meta"])

    cidrs = source_cidrs()
    folder_root = _cfg("spire_lab_secret_root", "spire").strip("/")
    row = SpireLab(
        name=name, trust_domain=trust_domain, cloud=cloud,
        region=host_info["meta"].get("location") or host_info["meta"].get("region") or "",
        vm_name=host_info["name"],
        # The private address is what the Resource Broker dials on 8081; the public one is
        # what the Ansible runner SSHes to when it is not in-subnet. Both are kept because
        # confusing them is the whole of the reachability trap.
        private_ip=host_info["private_ip"], public_ip=host_info["public_ip"],
        vm_resource_id=json.dumps(placement, sort_keys=True),
        bind_port=BIND_PORT,
        status="provisioning",
        source_cidrs=",".join(cidrs),
        firewall_name=RULE_NAME,
        entries_seeded=None, discovery_expected=DISCOVERY_EXPECTED,
        admin_secret_folder=f"{folder_root}/{_slug(name)}",
        admin_spiffe_id=(admin_spiffe_id or "").strip()
        or f"spiffe://{trust_domain}/password-safe/admin",
        ps_safe=_cfg("spire_lab_ps_safe", "Automation"),
        workgroup=workgroup, created_by=created_by,
        # NULL would mean "never" and never "inherit the default", so the timer is
        # stamped here, in the provision's own transaction. Extending or pinning it
        # afterwards is the existing /api/expiry/set path.
        expires_at=expiry_policy.default_expiry_for_kind(INVENTORY_KIND))
    db.add(row)
    db.flush()

    job = job_service.create_job(
        db, PROVISION_JOB_TYPE, created_by, workgroup=workgroup,
        metadata={"lab_id": row.id, "name": name, "cloud": cloud,
                  "trust_domain": trust_domain, "host": host_info["name"]})
    row.deploy_job_id = job.id
    db.commit()
    logger.info("spire-lab: queued %s lab %r (trust domain %s) on %s as job %s",
                cloud, name, trust_domain, host_info["name"], job.id)
    return {"lab_id": row.id, "job_id": job.id}


def _ansible_target(row: SpireLab) -> str:
    """The address the Ansible runner connects to.

    Public first. The runner is a transient in-cloud task or a local container, and
    neither is reliably in-subnet — a private address works only when it happens to be,
    and when it is not the failure is an SSH timeout that reads as a firewall problem.
    """
    return row.public_ip or row.private_ip or ""


def _stage_meta(row: SpireLab, stage: dict, asset_backend: str) -> dict:
    """Job metadata for one stage's ``ansible_local`` run.

    Built through ``ansible_run_meta.run_meta`` rather than by hand so this run obeys the
    same closed allowlist every Config-Management run does — that module is the boundary
    that keeps a credential out of the jobs table, and a second hand-rolled dict beside
    it would be a second place for one to leak.
    """
    from . import ansible_run_meta

    class _Payload:
        asset = stage["asset"]
        target = _ansible_target(row)
        cloud = row.cloud
        ansible_user = _cfg(require_backend(row.cloud).default_user_cfg) or \
            _cfg("ansible_default_user", "ubuntu")
        extra_vars = stage["vars_for"](row)
        secret_vars = None
        secret_become_source = ""
        secret_ssh_key_source = ""
        managed_account = None
        managed_become = None
        epml_token_var = ""

    return ansible_run_meta.run_meta(
        _Payload(),
        description=f"SPIRE lab ({row.name}): {stage['asset']} → {_ansible_target(row)}",
        asset_backend=asset_backend)


async def _run_stage(db: Session, *, row: SpireLab, stage: dict, actor: str,
                     asset_backend: str, parent_job_id: str) -> str:
    """Run ONE playbook as its own ``ansible_local`` job row, and return its status.

    Its own row, rather than four phases inside this job, because a failed stage's
    Ansible output is the only place its error exists — and the operator already knows
    how to read a Config-Management job's Live Output.

    Created ``queued``, not ``pending``: the worker claims on ``pending`` under a handled
    type, so a pending child would be claimed and run a SECOND time, concurrently with
    this call. ``job_service.create_job`` documents that exact case.
    """
    from ..api.websocket import broadcast_progress
    from . import ansible_local_run_service

    meta = _stage_meta(row, stage, asset_backend)
    child = job_service.create_job(
        db, "ansible_local", actor, workgroup="ansible", status="queued",
        metadata=meta, batch_id=row.id)
    jobs = stage_jobs(row)
    jobs[stage["key"]] = child.id
    row.stage_job_ids = json.dumps(jobs)
    db.commit()

    await broadcast_progress(parent_job_id, stage["pct"],
                             f"{stage['label']} (job {child.id[:8]})")
    await ansible_local_run_service.run(db, job_id=child.id, meta=meta)

    db.expire_all()
    fresh = db.query(Job).filter(Job.id == child.id).first()
    return (fresh.status if fresh else "failed") or "failed"


async def ensure_secret_folder(row: SpireLab) -> dict:
    """Create ``<safe>/<folder tree>`` for this lab's credential, if it is not there.

    **The identity playbook writes into a folder and never creates one** — the same rule
    the ``k3s-kubeconfig.yml`` it was modelled on states outright. Without this the fourth
    and last stage fails on a folder-not-found *after* the server is installed and seeded,
    which reads like a credential problem and is not. The Certificate Lab already
    established that the folder is the dashboard's job; this is the same call.

    Run as a PRE-FLIGHT, before the cloud ACL and before any playbook, because a lab whose
    credential has nowhere to go is one we should not have started. It is also the cheapest
    possible check: two list calls against a tenant we already talk to.

    The safe is never created — it carries its own ACL, and a safe that appeared because
    an automation asked for one is an access boundary nobody chose.
    """
    import asyncio
    from . import secrets_backend_service
    folder = (row.admin_secret_folder or "").strip("/")
    safe = (row.ps_safe or "").strip("/")
    if not (safe and folder):
        raise SpireLabError(
            "the lab has no Secrets Safe destination — set spire_lab_ps_safe and "
            "spire_lab_secret_root under Settings → SPIRE Lab")
    try:
        return await asyncio.to_thread(
            secrets_backend_service.ensure_bt_folder_path, f"{safe}/{folder}")
    except ValueError as exc:
        raise SpireLabError(
            f"the administrative credential has nowhere to land: {exc}") from exc


async def run_provision(db: Session, *, lab_id: str, job_id: str) -> None:
    """Worker entry point for ``spirelab_provision``.

    Ensures the credential's Secrets Safe folder, opens the cloud ACL, then runs the four
    playbooks in order, then reads back the two public artifacts. Stops at the first stage
    that fails: every later stage asserts the server is up, so continuing would turn one
    legible Ansible error into four.
    """
    from ..api.websocket import broadcast_progress
    from . import storage_service
    row = get_lab(db, lab_id)
    if not row:
        logger.warning("spire-lab: row %s vanished before provision", lab_id)
        return
    job_service.set_running(db, job_id)
    try:
        backend = require_backend(row.cloud)
        placement = json.loads(row.vm_resource_id or "{}")
        cidrs = _row_cidrs(row)

        # ── where the credential will land ────────────────────────────────────
        # FIRST, before the ACL and before any playbook. This is the one prerequisite
        # that fails at the very LAST stage if it is missing, by which point the server
        # is installed and seeded on somebody's VM and the error reads like a credential
        # fault. Two list calls to find out is a bargain against that.
        await broadcast_progress(
            job_id, 4, f"Checking the Secrets Safe folder "
                       f"{row.ps_safe}/{row.admin_secret_folder}…")
        made = await ensure_secret_folder(row)
        if made.get("created"):
            await broadcast_progress(
                job_id, 6,
                f"Created Secrets Safe folder(s) {'/'.join(made['created'])} under "
                f"{row.ps_safe}. The safe itself is never created — it carries its "
                f"own ACL.")

        # ── the cloud gate ────────────────────────────────────────────────────
        await broadcast_progress(job_id, 8, f"Opening tcp/{row.bind_port} on the "
                                            f"{backend.acl_label}…")
        if cidrs:
            res = await backend.apply_ingress(placement, [row.bind_port], cidrs)
            if not res.get("opened"):
                raise SpireLabError(
                    f"the {backend.acl_label} was not opened, so nothing can reach "
                    f"tcp/{row.bind_port}")
            row.firewall_name = res.get("name") or RULE_NAME
            db.commit()
            await broadcast_progress(
                job_id, 12,
                f"{backend.acl_label} {row.firewall_name} allows tcp/{row.bind_port} "
                f"from {', '.join(cidrs)}.")
        else:
            # Not fail-open: nothing was opened. Said out loud because it is the first
            # thing to check when the plugin later times out, and because a broker
            # already inside the VNet needs no rule at all — that lab is valid.
            await broadcast_progress(
                job_id, 12,
                f"No {backend.acl_label} change was made: spire_lab_source_cidrs is "
                f"unset. If Verify Functional Account later times out, this is why — "
                f"tcp/{row.bind_port} is reachable only from inside the network.")

        # ── the four playbooks ────────────────────────────────────────────────
        asset_backend = _cfg("spire_lab_asset_backend") or storage_service.active_backend()
        done = _stages_done(row)
        for stage in STAGES:
            if stage["key"] in done:
                continue
            status = await _run_stage(
                db, row=row, stage=stage, actor=row.created_by or "system",
                asset_backend=asset_backend, parent_job_id=job_id)
            if status != "completed":
                raise SpireLabError(
                    f"{stage['asset']} {status} — see job "
                    f"{stage_jobs(row).get(stage['key'], '')} for the Ansible output")
            done.append(stage["key"])
            row.stages_done = ",".join(done)
            if stage["key"] == "seed":
                # What went IN. What comes back out of discovery is the plugin's
                # assertion and belongs to a Verify run, not to this one.
                row.entries_seeded = ENTRIES_SEEDED
            db.commit()

        # ── the two public artifacts ──────────────────────────────────────────
        await broadcast_progress(job_id, 92, "Reading back the trust bundle…")
        read_public_artifacts(db, row)

        row.status = "available"
        row.error_message = None
        row.updated_at = datetime.utcnow()
        db.commit()
        job_service.set_completed(db, job_id, result={
            "lab_id": row.id, "trust_domain": row.trust_domain,
            "entries_seeded": row.entries_seeded,
            "discovery_expected": row.discovery_expected,
            "secrets": secret_refs(row),
            "next": "docs/runbooks/spire-lab-standup.md §5 — onboard the "
                    "functional account and managed system by hand, then read the "
                    "'Attributes received:' line on Verify Functional Account."})
    except Exception as exc:
        row.status = "failed"
        row.error_message = str(exc)[:2000]
        row.updated_at = datetime.utcnow()
        db.commit()
        logger.error("spire-lab: provision failed for %s: %s", lab_id, exc)
        job_service.set_failed(db, job_id, str(exc))


def read_public_artifacts(db: Session, row: SpireLab) -> dict:
    """Fetch the trust bundle and the granted SVID expiry from Secrets Safe.

    Both are public — the bundle is what every consumer of the trust domain has to trust,
    and an expiry is a date. They come through Secrets Safe because it is the only channel
    out of an Ansible run that carries a VALUE: a job's "output" is a captured log, so
    reading them from there would mean parsing Ansible's callback formatting back into
    data. The playbook publishes them; this reads them.

    Never fatal. A lab whose server is up and seeded is a working lab even if the read
    fails, and the values are also printed in the identity stage's own output.
    """
    refs = secret_refs(row)
    if not refs:
        return {}
    out = {}
    try:
        from . import secrets_backend_service
        bundle = secrets_backend_service.read_bt_secrets_safe(refs["bundle"]) or ""
        if bundle.strip():
            row.trust_bundle_pem = bundle.strip()
            out["bundle"] = True
        raw = (secrets_backend_service.read_bt_secrets_safe(refs["expires"]) or "").strip()
        if raw:
            out["expires_raw"] = raw
            parsed = _parse_openssl_date(raw)
            if parsed:
                row.admin_svid_expires_at = parsed
                out["expires_at"] = parsed.isoformat()
        row.updated_at = datetime.utcnow()
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("spire-lab: could not read back the public artifacts for %s: %s",
                       row.id, exc)
    return out


def _parse_openssl_date(raw: str) -> Optional[datetime]:
    """``openssl x509 -enddate`` format, e.g. ``Sep 15 12:04:31 2026 GMT``.

    Returned as a naive UTC datetime to match every other timestamp on these rows. Parse
    failure returns None rather than raising: the raw string is still shown, and a lab is
    not broken by a date this could not read.
    """
    text = (raw or "").strip().replace(" GMT", "").replace(" UTC", "")
    for fmt in ("%b %d %H:%M:%S %Y", "%b %d %H:%M:%S %Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


# ── Teardown ──────────────────────────────────────────────────────────────────

def start_decommission(db: Session, *, lab_id: str, created_by: str) -> dict:
    """Enqueue teardown. The same entry point the auto-delete sweep calls, so a timer
    that runs out ends in exactly the teardown the button runs — no second code path."""
    row = get_lab(db, lab_id)
    if not row:
        raise SpireLabError(f"SPIRE lab {lab_id} not found")
    if row.status == "decommissioning":
        raise SpireLabError(f"{row.name} is already being torn down")
    row.status = "decommissioning"
    # Cleared in the same transaction that starts the teardown: at-most-once, and it stops
    # the next sweep pass from enqueueing a second teardown for the same row.
    row.expires_at = None
    row.updated_at = datetime.utcnow()
    job = job_service.create_job(
        db, DECOMMISSION_JOB_TYPE, created_by, workgroup=row.workgroup,
        metadata={"lab_id": row.id, "name": row.name, "cloud": row.cloud,
                  "trust_domain": row.trust_domain})
    db.commit()
    logger.info("spire-lab: queued teardown of %r (trust domain %s) as job %s",
                row.name, row.trust_domain, job.id)
    return {"lab_id": row.id, "job_id": job.id}


async def run_decommission(db: Session, *, lab_id: str, job_id: str) -> None:
    """Worker entry point for ``spirelab_decommission``.

    **Closing the ACL is the teardown**, and it is why this feature has a timer at all:
    it is the one action that stops a forgotten trust domain from minting for anybody.
    The VM is deliberately left alone — it is an ordinary VM with its own timer and its
    own Destroy, and destroying somebody's host because a lab expired is a bigger
    surprise than leaving a server nothing can reach.
    """
    from ..api.websocket import broadcast_progress
    row = get_lab(db, lab_id)
    if not row:
        logger.warning("spire-lab: row %s vanished before teardown", lab_id)
        return
    job_service.set_running(db, job_id)
    try:
        backend = require_backend(row.cloud)
        placement = json.loads(row.vm_resource_id or "{}")
        await broadcast_progress(job_id, 30, f"Closing tcp/{row.bind_port} on the "
                                             f"{backend.acl_label}…")
        # An empty source set is the fail-closed contract on all three clouds: the rule
        # is removed (or every permission revoked), and `opened` comes back False.
        res = await backend.apply_ingress(placement, [row.bind_port], [])
        if res.get("opened"):
            raise SpireLabError(
                f"the {backend.acl_label} still allows tcp/{row.bind_port}")
        row.source_cidrs = ""
        row.status = "deleted"
        row.error_message = None
        row.updated_at = datetime.utcnow()
        db.commit()
        await broadcast_progress(
            job_id, 95,
            f"tcp/{row.bind_port} is closed. The VM {row.vm_name} is untouched — it "
            f"has its own auto-delete timer and its own Destroy. The SPIRE server is "
            f"still installed and still holds its CA key, so a lab that is being retired "
            f"for good should have its host destroyed too.")
        job_service.set_completed(db, job_id, result={
            "lab_id": row.id, "trust_domain": row.trust_domain,
            "vm_name": row.vm_name, "vm_destroyed": False})
    except Exception as exc:
        # Left "failed" with the timer still cleared, on purpose: re-arming would have
        # the sweep retry a teardown that already failed once, on a loop, silently.
        row.status = "failed"
        row.error_message = str(exc)[:2000]
        row.updated_at = datetime.utcnow()
        db.commit()
        logger.error("spire-lab: teardown failed for %s: %s", lab_id, exc)
        job_service.set_failed(db, job_id, str(exc))
