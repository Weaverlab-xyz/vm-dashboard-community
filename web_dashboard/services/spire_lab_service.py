"""SPIRE trust-domain lab lifecycle, for the Password Safe SPIFFE SVID plugin.

Stands the lab up on a Linux VM **the dashboard already deployed**, and takes it down
again. In order: open tcp/8081 on the cloud's own network ACL, run the four playbooks in
``examples/playbooks/spire/`` against the host over SSH, then read back the two public
values an operator has to carry into BeyondInsight by hand — the trust bundle and the
titles of the admin credential in Secrets Safe.

**It does not create the VM, deliberately.** A SPIRE server is a Go binary and a sqlite
file; the host is an ordinary VM, so it already has an auto-delete timer, ref-counted
NAT, Password Safe VM onboarding and a Destroy button. The host must still be one this
dashboard deployed, because ``resolve_host`` re-derives it from the deploy-job rows
rather than trusting an address it is handed — four privileged playbooks against a host
of the caller's choosing is not something this should accept.

**The connection identity is the operator's to choose.** By default
``ansible_local_run_service`` resolves the SSH key from that VM's own *deploy job*
metadata, which is why this attaches to a dashboard-deployed host. But the build form
also takes a Password Safe managed account or a Secrets-Management SSH-key secret, held
on the row as refs (see ``SpireLab.ansible_secret_ssh_key_source``) and resolved per
stage at run time — so a host whose deploy record carries no usable key is no longer a
host no run can log in to. Choosing nothing keeps the auto-derivation, which now fails
with a named reason rather than by shipping an empty key file to the runner.

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


def batch_id_for(row: SpireLab) -> str:
    """The ``batch_id`` this lab's four stage jobs share, so /jobs can roll them up.

    ``Job.batch_id`` is **``String(32)``** and a row id is a 36-character dashed UUID, so
    the id cannot go in whole — a live PostgreSQL rejects it with
    ``StringDataRightTruncation`` while SQLite, which does not enforce VARCHAR length at
    all, accepts it happily. That is why this needs a function and a test rather than a
    field reference.

    12 hex characters, matching every other batch id in the codebase
    (``uuid.uuid4().hex[:12]`` in the bulk deploy and bulk run paths). Derived from the
    lab id rather than random so the same lab always groups under the same batch.
    """
    return (row.id or "").replace("-", "")[:12]


def stage_jobs(row: SpireLab) -> dict:
    """``{stage key: ansible_local job id}``. Public because the page links each stage's
    Live Output, and a failed stage's Ansible error exists nowhere else."""
    try:
        return json.loads(row.stage_job_ids or "{}")
    except Exception:
        return {}


def managed_ref(row: SpireLab) -> Optional[dict]:
    """The lab's pinned Password Safe managed-account ref, or None.

    A plain dict, which is exactly what ``ansible_credentials.resolve`` consumes — no
    pydantic reaches the worker. Public because ``_stage_meta``, the API's serializer and
    the audit entry all need it, and three ``json.loads`` calls would be three chances to
    disagree about what a malformed value means.
    """
    try:
        return json.loads(row.ansible_managed_account or "null") or None
    except Exception:
        # A row we cannot parse is a row with no chosen account: fall back to
        # auto-derivation rather than failing the build on a storage artefact.
        logger.warning("spire-lab: unparseable managed-account ref on lab %s", row.id)
        return None


def _cred_fields(row: SpireLab, host: str = "spire") -> dict:
    """The connection identity for ONE of the lab's two hosts.

    The two VMs are deployed independently and **do not share an SSH key**, so each keeps
    its own set. A NULL set means auto-derive from THAT host's deploy job — which already
    works, because ``ansible_local_run_service._find_cloud_deploy_meta`` matches the deploy
    job on the *target address*, and each stage targets its own machine.

    It deliberately does NOT fall back to the other host's choice. Inheriting it would
    connect to one VM with another VM's credential, and the failure is
    ``Permission denied (publickey)`` several stages into a run that looked configured.
    """
    if host == "k8s":
        return {"ssh_key_source": row.k8s_ansible_secret_ssh_key_source or "",
                "managed": row.k8s_ansible_managed_account,
                "become_self": row.k8s_ansible_managed_become_self,
                "login_user": row.k8s_login_user or ""}
    return {"ssh_key_source": row.ansible_secret_ssh_key_source or "",
            "managed": row.ansible_managed_account,
            "become_self": row.ansible_managed_become_self,
            "login_user": row.login_user or ""}


def managed_ref_for(row: SpireLab, host: str = "spire") -> Optional[dict]:
    """``managed_ref`` for either host. Same parse, same "unparseable means none" rule."""
    raw = _cred_fields(row, host)["managed"]
    try:
        return json.loads(raw or "null") or None
    except Exception:
        logger.warning("spire-lab: unparseable managed-account ref (%s host) on lab %s",
                       host, row.id)
        return None


def credential_kind_for(row: SpireLab, host: str = "spire") -> str:
    if managed_ref_for(row, host):
        return "managed"
    return "ssh-key-secret" if _cred_fields(row, host)["ssh_key_source"] else "auto"


def credential_kind(row: SpireLab) -> str:
    """Which connection identity this lab was built with — for the page and the audit
    entry. Never the ref itself, and never anything resolvable to a credential."""
    if managed_ref(row):
        return "managed"
    return "ssh-key-secret" if (row.ansible_secret_ssh_key_source or "") else "auto"


def managed_account_name(row: SpireLab) -> str:
    """The chosen account's name, or "". A name is not a credential — it is also what
    becomes ``ansible_user`` — so it is safe to show and to audit."""
    return ((managed_ref(row) or {}).get("account_name") or "")


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
              workgroup: Optional[str] = None,
              secret_ssh_key_source: str = "",
              managed_account: Optional[dict] = None,
              managed_become_self: bool = False,
              login_user: str = "") -> dict:
    """Record the lab and enqueue its build. Returns ``{lab_id, job_id}``.

    The four credential arguments default to "choose nothing", which is the pre-existing
    behaviour: the runner auto-derives this host's keypair from its deploy job. The
    caller-facing permission and runner-capability refusals are the API layer's
    (``services/ansible_run_gate``); what is checked here is the SHAPE of the choice.
    """
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

    secret_ssh_key_source = (secret_ssh_key_source or "").strip()
    login_user = (login_user or "").strip()
    if managed_account and secret_ssh_key_source:
        raise SpireLabError(
            "pick EITHER a Password Safe managed account OR an SSH-key secret, not both "
            "— they are two answers to the same question (who the runner logs in as). A "
            "managed account's name also overrides the login user, so a run carrying "
            "both would connect as one identity holding the other's key.")
    if managed_account and (managed_account.get("system_id") is None
                            or managed_account.get("account_id") is None):
        # A name-only ref is the BULK form: it defers the lookup so each host in a batch
        # resolves its own account. A lab has exactly one host and it is known right
        # now, so accepting a name-only ref would trade a lookup that can fail here for
        # a half-built lab whose fourth stage fails on "no such account".
        raise SpireLabError(
            "a managed account must be picked from this host's own list, so that it "
            "carries both system_id and account_id — an account named without ids is "
            "for bulk runs across many hosts, and a lab has one.")
    if managed_become_self and not managed_account:
        raise SpireLabError(
            "'also use for sudo' needs a managed account to use — there is no separate "
            "become credential on this form.")
    if login_user and (len(login_user) > 104 or any(c.isspace() for c in login_user)):
        # 104 is the column, and a login with whitespace in it is a typo that would
        # otherwise surface as an SSH auth failure four stages deep.
        raise SpireLabError(
            "the login user is a single OS username, at most 104 characters and with no "
            "whitespace.")

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
        # Refs and a username. NULL throughout = auto-derive from the deploy job, which
        # is what every lab built before this did. `sort_keys` so the stored JSON is
        # stable and a row diff means a real change of account.
        ansible_secret_ssh_key_source=secret_ssh_key_source or None,
        ansible_managed_account=(json.dumps(managed_account, sort_keys=True)
                                 if managed_account else None),
        ansible_managed_become_self=bool(managed_become_self) or None,
        login_user=login_user or None,
        workgroup=workgroup, created_by=created_by,
        # NULL would mean "never" and never "inherit the default", so the timer is
        # stamped here, in the provision's own transaction. Extending or pinning it
        # afterwards is the existing /api/expiry/set path.
        expires_at=expiry_policy.default_expiry_for_kind(INVENTORY_KIND))
    db.add(row)
    db.flush()

    if managed_account or secret_ssh_key_source:
        # Audit the USE, not the credential: kinds, the account name and the system id.
        # Reuses Config Management's own action name so one audit query still answers
        # "who used a credential in a run", whichever page they used.
        job_service.log_audit(
            db, created_by, "ansible_secret_use",
            details={
                "kinds": [credential_kind(row)]
                         + (["managed-account become (checkout)"]
                            if managed_become_self else []),
                "managed_accounts": ([{"role": "connection",
                                       "account": managed_account.get("account_name"),
                                       "system_id": managed_account.get("system_id")}]
                                     if managed_account else []),
                "asset": "spire lab (4 playbooks)",
                "target": host_info["name"]})

    job = job_service.create_job(
        db, PROVISION_JOB_TYPE, created_by, workgroup=workgroup,
        metadata={"lab_id": row.id, "name": name, "cloud": cloud,
                  "trust_domain": trust_domain, "host": host_info["name"]})
    row.deploy_job_id = job.id
    db.commit()
    logger.info("spire-lab: queued %s lab %r (trust domain %s) on %s as job %s",
                cloud, name, trust_domain, host_info["name"], job.id)
    return {"lab_id": row.id, "job_id": job.id}


def _ansible_target(row: SpireLab, host: str = "spire") -> str:
    """The address the Ansible runner connects to, for one of the lab's two hosts.

    Public first. The runner is a transient in-cloud task or a local container, and
    neither is reliably in-subnet — a private address works only when it happens to be,
    and when it is not the failure is an SSH timeout that reads as a firewall problem.

    ``host`` is ``"spire"`` or ``"k8s"``. It exists because the Kubernetes half runs
    stages on BOTH machines in one job, and a stage silently landing on the wrong one
    would install a SPIRE server where k3s should be.
    """
    if host == "k8s":
        return row.k8s_public_ip or row.k8s_private_ip or ""
    return row.public_ip or row.private_ip or ""


def _stage_meta(row: SpireLab, stage: dict, asset_backend: str) -> dict:
    """Job metadata for one stage's ``ansible_local`` run.

    Built through ``ansible_run_meta.run_meta`` rather than by hand so this run obeys the
    same closed allowlist every Config-Management run does — that module is the boundary
    that keeps a credential out of the jobs table, and a second hand-rolled dict beside
    it would be a second place for one to leak.
    """
    from . import ansible_run_meta

    # Which machine this stage runs on. The original four have no `host` key and all mean
    # the SPIRE server, so the default keeps them untouched.
    _host = stage.get("host", "spire")
    _target = _ansible_target(row, _host)
    # Per host, because the two VMs do not share an SSH key. See _cred_fields for why a
    # blank set here must NOT inherit the other host's choice.
    _cred = _cred_fields(row, _host)
    _ref = managed_ref_for(row, _host)

    class _Payload:
        asset = stage["asset"]
        target = _target
        cloud = row.cloud
        # The lab's own login user wins, then the per-cloud config key. A managed
        # account overrides even this at run time, because the account's name IS the
        # login identity (see ansible_credentials.resolve).
        ansible_user = _cred["login_user"].strip() or \
            _cfg(require_backend(row.cloud).default_user_cfg) or \
            _cfg("ansible_default_user", "ubuntu")
        extra_vars = stage["vars_for"](row)
        # A ref per var name, never a value — resolved at run time and scrubbed from the
        # output. The join token rides this channel: it must not be stored on the row (it
        # is one-use and spent within minutes) and must not reach a job log.
        secret_vars = stage["secret_vars_for"](row) if stage.get("secret_vars_for") else None
        secret_become_source = ""
        # Read off the ROW, so all four stages — and a provision resumed after a failed
        # one — use the identical credential. Both are refs; `run_meta`'s closed
        # allowlist is what keeps a value out of the jobs table, and neither of these
        # needed a new key in it.
        secret_ssh_key_source = _cred["ssh_key_source"]
        managed_account = _ref
        # The SAME ref for sudo, because all four plays are `become: true` and this form
        # offers no separate become credential. Password Safe reuses the already-open
        # request rather than opening a second one, and the become checkout forces
        # password mode regardless of the ref's own uses_ssh_key flag.
        managed_become = _ref if (_cred["become_self"] and _ref) else None
        epml_token_var = ""

    return ansible_run_meta.run_meta(
        _Payload(),
        description=f"SPIRE lab ({row.name}): {stage['asset']} → {_target}",
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
        metadata=meta, batch_id=batch_id_for(row))
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
        # THE K3S HALF FIRST, and only when there is one. Closing the ACL stops the SPIRE
        # server minting, which is the kill switch — but it leaves the k3s node with an
        # agent re-attesting to a server that no longer answers and an API server trusting
        # an issuer that no longer resolves. Neither is dangerous; both are confusing, and
        # the Kubernetes tab next to this one deletes its ServiceAccount on teardown, so
        # leaving this half in place was an asymmetry an operator would meet as a mystery.
        #
        # NON-FATAL, and that is the important part. The node may be gone, unreachable, or
        # never have been linked; none of those may stop the ACL from closing, because the
        # ACL is the thing that actually matters. A failure here is reported and the
        # teardown continues.
        if row.k8s_status == "linked" and row.k8s_vm_name:
            await broadcast_progress(job_id, 15, UNLINK_STAGE["label"])
            try:
                status = await _run_stage(
                    db, row=row, stage=UNLINK_STAGE, actor=row.created_by or "system",
                    asset_backend=(_cfg("spire_lab_asset_backend")
                                   or storage_service.active_backend()),
                    parent_job_id=job_id)
                row.k8s_status = "unlinked" if status == "completed" else "failed"
                db.commit()
                if status != "completed":
                    job_service.append_job_log(
                        db, job_id,
                        "the k3s unlink did not complete — the node keeps its SPIRE agent "
                        "and its API server keeps the authentication-config drop-in. Run "
                        "examples/playbooks/k3s/k3s-spiffe-unlink.yml on it by hand. "
                        "Closing the ACL below is unaffected.")
            except Exception as exc:  # noqa: BLE001 — see the note above
                job_service.append_job_log(
                    db, job_id, f"the k3s unlink could not run ({exc}) — the node keeps "
                                f"its agent and drop-in; closing the ACL regardless")
                logger.warning("spire-lab: k3s unlink for %s failed: %s", lab_id, exc)

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


# ── The Kubernetes half: linking a k3s node into the trust domain ─────────────
# The four stages above prove ISSUANCE and GOVERNANCE. They never prove that anything
# accepts the result, because nothing in this dashboard has ever presented a JWT-SVID to a
# relying party. This links a second VM — a small k3s node — into the same trust domain and
# makes its API server accept those tokens, so the lab finally demonstrates the path
# docs/spiffe.md argues for rather than only the vaulted downgrade.
#
# WHY THIS IS A SEPARATE JOB, not extra stages on provision:
#   * the governance half is what most labs are built for and it stands on its own. A k3s
#     failure must not make a working trust domain read as broken, which is why `k8s_status`
#     is a separate column;
#   * an existing lab can gain the capability without being rebuilt.
#
# WHY THE ISSUER IS A HOSTNAME AND NOT THE SPIRE HOST'S IP. `spire-server x509 mint -dns`
# writes a **DNS** SAN, and Go verifies an **IP** SAN for `https://10.0.0.5:8443`. An IP
# issuer therefore fails TLS verification at the API server no matter how correct the trust
# bundle is, and the error reads as a bad CA. So the provider is minted for
# `oidc.<trust-domain>` and `k3s-spiffe-auth.yml` writes the /etc/hosts entry that resolves
# it to the SPIRE host's private address.

K8S_LINK_JOB_TYPE = "spirelab_k8s_link"

# The teardown's one stage, on the k3s host. Kept beside the link's stages rather than
# inside `run_decommission` so it goes through the same `_stage_meta` / `_run_stage`
# machinery — which is what keeps a credential out of the jobs table (see
# `ansible_run_meta.run_meta`) and gives the stage its own readable Ansible output.
def _unlink_vars(row: SpireLab) -> dict:
    """Extra vars for the unlink. Reads only the row, like every other `vars_for`.

    `remove_workload_user` is deliberately absent, which leaves the play's own `false`
    default in force: the account may predate the link and may own other things on the
    node, so deleting it because a lab was closed is a bigger surprise than leaving one
    that can no longer obtain a token.
    """
    return {"workload_user": K8S_WORKLOAD_USER,
            "username_prefix": K8S_USERNAME_PREFIX,
            "workload_spiffe_id": row.k8s_workload_spiffe_id or ""}


UNLINK_STAGE = {
    "key": "unlink", "asset": "k3s-spiffe-unlink.yml", "vars_for": _unlink_vars,
    "host": "k8s", "pct": 15,
    "label": "Unlinking the k3s node from the trust domain…",
}

# The OIDC Discovery Provider's port on the SPIRE host. Opened to the k3s node only — the
# k3s node itself needs nothing inbound, because the workload runs on it.
OIDC_PORT = 8443
OIDC_RULE_NAME = "allow-spire-oidc"

# The Secrets Safe title the join token is written to, under this lab's own folder. Named
# here beside SECRET_TITLES because it is the same value channel, but it is NOT in that
# dict: those four are read back after a provision, and this one is consumed by the very
# next stage and is worthless minutes later.
JOIN_TOKEN_TITLE = "k8s-join-token"

# Defaults for a link. Recorded on the row once it succeeds, so the panel can show what has
# to agree and a re-link cannot quietly change one of them.
K8S_AUDIENCE = "k8s"
K8S_WORKLOAD_USER = "deploy-bot"
K8S_WORKLOAD_UID = 1010
K8S_WORKLOAD_PATH = "/ns/kube-system/sa/deploy-bot"
K8S_NODE_PATH = "/node/k3s-01"
K8S_WORKLOAD_ROLE = "view"
K8S_USERNAME_PREFIX = "spiffe:"
# Seconds. The whole point of the pattern, so it is explicit rather than inherited.
K8S_JWT_SVID_TTL = 300


def oidc_domain_for(row: SpireLab) -> str:
    """The hostname the k3s API server fetches JWKS from. See the section note above."""
    return f"oidc.{row.trust_domain}"


def issuer_url_for(row: SpireLab) -> str:
    return f"https://{oidc_domain_for(row)}:{OIDC_PORT}"


def workload_spiffe_id_for(row: SpireLab) -> str:
    return f"spiffe://{row.trust_domain}{K8S_WORKLOAD_PATH}"


def _join_token_ref(row: SpireLab) -> str:
    """`bt_safe://` ref to the join token the entry stage just wrote."""
    folder = (row.admin_secret_folder or "").strip("/")
    return f"bt_safe://{(row.ps_safe or '').strip('/')}/{folder}/{JOIN_TOKEN_TITLE}"


def _k3s_install_vars(row: SpireLab) -> dict:
    # Pinned, not latest: k3s-spiffe-auth.yml refuses anything below 1.34 because a v1
    # AuthenticationConfiguration stops a pre-1.34 API server, and on a single node there
    # is then no API left to repair it through. Blank means "whatever get.k3s.io serves",
    # which is fine while that is >= 1.34 and is why the guard stays in the play.
    return {"k3s_version": _cfg("spire_lab_k3s_version", "")}


def _oidc_vars(row: SpireLab) -> dict:
    return {"trust_domain": row.trust_domain,
            "oidc_domain": oidc_domain_for(row),
            "oidc_port": OIDC_PORT,
            "spire_version": _cfg("spire_lab_version", "1.15.3")}


def _k8s_entry_vars(row: SpireLab) -> dict:
    return {"trust_domain": row.trust_domain,
            "audience": row.k8s_audience or K8S_AUDIENCE,
            "workload_uid": row.k8s_workload_uid or K8S_WORKLOAD_UID,
            "workload_path": K8S_WORKLOAD_PATH,
            "node_path": K8S_NODE_PATH,
            "jwt_svid_ttl": K8S_JWT_SVID_TTL,
            # Stored rather than printed. Without this the token is in the job log, and a
            # job's output IS a captured log — anyone who can read it can attest a host
            # into this trust domain until the token is spent.
            "node_token_secret": f"{(row.admin_secret_folder or '').strip('/')}/{JOIN_TOKEN_TITLE}",
            "token_safe": row.ps_safe or ""}


def _agent_vars(row: SpireLab) -> dict:
    return {"trust_domain": row.trust_domain,
            # The PRIVATE address: the agent dials the server from inside the network, and
            # `spire-open-ports.yml` plus the cloud ACL is what lets it.
            "spire_server_address": row.private_ip or row.public_ip or "",
            "spire_server_port": row.bind_port or BIND_PORT,
            "trust_bundle_pem": row.trust_bundle_pem or "",
            "spire_version": _cfg("spire_lab_version", "1.15.3"),
            "workload_user": K8S_WORKLOAD_USER,
            "workload_uid": row.k8s_workload_uid or K8S_WORKLOAD_UID,
            "verify_audience": row.k8s_audience or K8S_AUDIENCE}


def _agent_secret_vars(row: SpireLab) -> dict:
    """The join token, as a ref. Never a value — see _stage_meta."""
    return {"join_token": _join_token_ref(row)}


def _auth_vars(row: SpireLab) -> dict:
    return {"oidc_issuer_url": issuer_url_for(row),
            "trust_bundle_pem": row.trust_bundle_pem or "",
            "workload_spiffe_id": workload_spiffe_id_for(row),
            "audience": row.k8s_audience or K8S_AUDIENCE,
            "username_prefix": K8S_USERNAME_PREFIX,
            "workload_role": row.k8s_workload_role or K8S_WORKLOAD_ROLE,
            "workload_user": K8S_WORKLOAD_USER,
            # Resolves the issuer hostname to the SPIRE host. Without it the API server
            # cannot reach JWKS at all, and see the section note for why the issuer is not
            # simply this address.
            "spire_oidc_host_ip": row.private_ip or row.public_ip or ""}


K8S_STAGES = (
    {"key": "k3s", "asset": "k3s-server-init.yml", "vars_for": _k3s_install_vars,
     "host": "k8s", "pct": 25, "label": "Installing k3s on the second host…"},
    {"key": "oidc", "asset": "spire-oidc-provider.yml", "vars_for": _oidc_vars,
     "host": "spire", "pct": 42, "label": "Publishing the trust domain as an OIDC issuer…"},
    # ALWAYS re-runs. A join token is one-use and expires in ten minutes, so a link resumed
    # after a later stage failed cannot reuse the one the first attempt minted — it would
    # fail attestation with a message about an unknown token, which reads like a broken
    # agent. Re-running mints a fresh token and another node entry, which is how
    # re-attestation works and is excluded from discovery either way.
    {"key": "entry", "asset": "spire-k8s-entry.yml", "vars_for": _k8s_entry_vars,
     "host": "spire", "pct": 58, "always": True,
     "label": "Minting a join token and the workload entry…"},
    {"key": "agent", "asset": "spire-agent-install.yml", "vars_for": _agent_vars,
     "secret_vars_for": _agent_secret_vars, "host": "k8s", "pct": 76,
     "label": "Attesting the SPIRE agent on the k3s node…"},
    {"key": "auth", "asset": "k3s-spiffe-auth.yml", "vars_for": _auth_vars,
     "host": "k8s", "pct": 92,
     "label": "Teaching the API server to accept JWT-SVIDs…"},
)

K8S_STAGE_ASSETS = tuple(s["asset"] for s in K8S_STAGES)


def k8s_stages_done(row: SpireLab) -> list:
    return [s for s in (row.k8s_stages_done or "").split(",") if s]


def k8s_stage_jobs(row: SpireLab) -> dict:
    try:
        return json.loads(row.k8s_stage_job_ids or "{}")
    except Exception:
        return {}


def start_k8s_link(db: Session, *, lab_id: str, created_by: str, host: str,
                   audience: str = "", workload_role: str = "",
                   secret_ssh_key_source: str = "",
                   managed_account: Optional[dict] = None,
                   managed_become_self: bool = False,
                   login_user: str = "") -> dict:
    """Enqueue the Kubernetes link for an existing lab.

    ``host`` is a VM NAME or IP for the k3s node, re-derived through ``resolve_host``
    against this dashboard's own deploy rows — exactly as ``provision`` treats the SPIRE
    host, and for the same reason: accepting an arbitrary address here would be accepting
    a request to run privileged playbooks against a host of the caller's choosing.

    The refusals below are the ones worth making before a job row exists, because every
    one of them otherwise fails several minutes into a run.
    """
    row = get_lab(db, lab_id)
    if not row:
        raise SpireLabError(f"SPIRE lab {lab_id} not found")
    if row.status != "available":
        raise SpireLabError(
            f"{row.name} is {row.status}. The trust domain has to be built and its bundle "
            f"read back before a k3s node can be attested into it")
    if row.k8s_status == "linking":
        raise SpireLabError(f"{row.name} is already being linked to a k3s node")
    if not (row.trust_bundle_pem or "").strip():
        raise SpireLabError(
            "this lab has no trust bundle recorded, so the agent would have nothing to "
            "verify the server with and the API server nothing to verify tokens with. "
            "Re-run the build, or read the bundle back from the row's Bundle button")
    # The k3s node's OWN credential. Same either/or rule as the build form, because it is
    # the same question: who does the runner log in as. Checked here rather than in the API
    # for the same reason provision does it — the API owns caller-facing refusals, this owns
    # the SHAPE of the choice.
    secret_ssh_key_source = (secret_ssh_key_source or "").strip()
    login_user = (login_user or "").strip()
    if managed_account and secret_ssh_key_source:
        raise SpireLabError(
            "pick EITHER a Password Safe managed account OR an SSH-key secret for the k3s "
            "node, not both — they are two answers to the same question. A managed "
            "account's name also overrides the login user, so a run carrying both would "
            "connect as one identity holding the other's key.")
    if managed_account and (managed_account.get("system_id") is None
                            or managed_account.get("account_id") is None):
        raise SpireLabError(
            "a managed account must be picked from the k3s host's own list, so that it "
            "carries both system_id and account_id — a name-only ref is for bulk runs "
            "across many hosts, and this is one host.")
    if managed_become_self and not managed_account:
        raise SpireLabError(
            "'also use this account for sudo' needs a managed account to use — there is no "
            "separate become credential for the k3s node.")
    if login_user and (len(login_user) > 104 or any(c.isspace() for c in login_user)):
        raise SpireLabError(
            "the k3s node's login user is a single OS username, at most 104 characters "
            "and with no whitespace.")

    host_info = resolve_host(db, row.cloud, host)
    placement = json.dumps(require_backend(row.cloud).placement(host_info["meta"]),
                           sort_keys=True)
    if not (host_info["public_ip"] or host_info["private_ip"]):
        raise SpireLabError(
            "the chosen k3s host reports no address, so the Ansible runner cannot reach "
            "it. Its deploy job's metadata predates address capture, or the VM is stopped")
    if not (host_info["private_ip"] or "").strip():
        # The SPIRE host's ACL is opened to THIS address and nothing else. Widening the
        # rule instead would undo the only thing keeping tcp/8443 off the rest of the
        # subnet, so it is refused rather than quietly substituted.
        raise SpireLabError(
            "the chosen k3s host reports no private address. The SPIRE host's firewall is "
            "opened to that address specifically, and widening it instead is not "
            "something this should do silently")
    if placement == (row.vm_resource_id or ""):
        raise SpireLabError(
            "the k3s node and the SPIRE server are the same VM. Two hosts is the point: "
            "an agent attesting over loopback proves the mechanism but not that it works "
            "across a network, which is the half worth demonstrating")

    row.k8s_vm_resource_id = placement
    row.k8s_vm_name = host_info["name"]
    row.k8s_private_ip = host_info["private_ip"]
    row.k8s_public_ip = host_info["public_ip"]
    row.k8s_status = "linking"
    row.k8s_error_message = None
    # Cleared so a re-link after a failure starts from the top rather than trusting the
    # stages a previous attempt claimed. The entry stage re-runs regardless (see K8S_STAGES).
    row.k8s_stages_done = None
    row.k8s_audience = (audience or "").strip() or K8S_AUDIENCE
    row.k8s_workload_role = (workload_role or "").strip() or K8S_WORKLOAD_ROLE
    row.k8s_workload_uid = K8S_WORKLOAD_UID
    row.k8s_workload_spiffe_id = workload_spiffe_id_for(row)
    row.k8s_issuer_url = issuer_url_for(row)
    # NULL throughout = auto-derive from the K3S host's own deploy job, which is what the
    # runner does when nothing is set and is the correct default: the deploy job is matched
    # on the target address, and these stages target this VM. Never inherited from the SPIRE
    # host's set — see _cred_fields.
    row.k8s_ansible_secret_ssh_key_source = secret_ssh_key_source or None
    row.k8s_ansible_managed_account = (json.dumps(managed_account, sort_keys=True)
                                       if managed_account else None)
    row.k8s_ansible_managed_become_self = bool(managed_become_self) or None
    row.k8s_login_user = login_user or None
    row.updated_at = datetime.utcnow()
    if managed_account or secret_ssh_key_source:
        # Audit the USE, not the credential. Same action name as Config Management's, so one
        # audit query still answers "who used a credential in a run" whichever page it was.
        job_service.log_audit(
            db, created_by, "ansible_secret_use",
            details={"kinds": [credential_kind_for(row, "k8s")]
                              + (["managed-account become (checkout)"]
                                 if managed_become_self else []),
                     "account": (managed_account or {}).get("account_name", ""),
                     "system_id": (managed_account or {}).get("system_id"),
                     "target": row.k8s_vm_name, "lab": row.name,
                     "why": "spire lab kubernetes link"})
    job = job_service.create_job(
        db, K8S_LINK_JOB_TYPE, created_by, workgroup=row.workgroup,
        metadata={"lab_id": row.id, "name": row.name, "cloud": row.cloud,
                  "trust_domain": row.trust_domain, "k8s_host": row.k8s_vm_name})
    db.commit()
    logger.info("spire-lab: queued k8s link for %r (k3s node %s) as job %s",
                row.name, row.k8s_vm_name, job.id)
    return {"lab_id": row.id, "job_id": job.id}


async def run_k8s_link(db: Session, *, lab_id: str, job_id: str) -> None:
    """Worker entry point for ``spirelab_k8s_link``.

    Opens tcp/8443 on the SPIRE host to the k3s node, then runs the five stages in order,
    alternating hosts. Stops at the first failure: every later stage depends on the one
    before, so continuing turns one legible Ansible error into several.

    **The ACL is asymmetric and that is not an oversight.** Only the SPIRE host gains a
    rule: the agent dials the server on 8081 and the API server fetches JWKS on 8443, while
    the k3s node needs nothing inbound because the workload runs on it. Two ports, one
    source — the node's private address.
    """
    from ..api.websocket import broadcast_progress
    from . import storage_service
    row = get_lab(db, lab_id)
    if not row:
        logger.warning("spire-lab: row %s vanished before the k8s link", lab_id)
        return
    job_service.set_running(db, job_id)
    try:
        backend = require_backend(row.cloud)
        placement = json.loads(row.vm_resource_id or "{}")
        node_cidr = f"{row.k8s_private_ip}/32"

        await broadcast_progress(
            job_id, 6, f"Opening tcp/{row.bind_port} and tcp/{OIDC_PORT} on the "
                       f"{backend.acl_label} to {node_cidr}…")
        res = await backend.apply_ingress(
            placement, [row.bind_port or BIND_PORT, OIDC_PORT], [node_cidr])
        if not res.get("opened"):
            raise SpireLabError(
                f"the {backend.acl_label} was not opened to {node_cidr}, so the agent "
                f"cannot reach tcp/{row.bind_port} and the API server cannot reach "
                f"tcp/{OIDC_PORT}")
        await broadcast_progress(
            job_id, 10,
            f"{backend.acl_label} allows tcp/{row.bind_port} and tcp/{OIDC_PORT} from "
            f"{node_cidr}.")

        asset_backend = _cfg("spire_lab_asset_backend") or storage_service.active_backend()
        done = k8s_stages_done(row)
        for stage in K8S_STAGES:
            if stage["key"] in done and not stage.get("always"):
                continue
            status = await _run_k8s_stage(
                db, row=row, stage=stage, actor=row.created_by or "system",
                asset_backend=asset_backend, parent_job_id=job_id)
            if status != "completed":
                raise SpireLabError(
                    f"{stage['asset']} {status} — see job "
                    f"{k8s_stage_jobs(row).get(stage['key'], '')} for the Ansible output")
            if stage["key"] not in done:
                done.append(stage["key"])
            row.k8s_stages_done = ",".join(done)
            db.commit()

        row.k8s_status = "linked"
        row.k8s_error_message = None
        row.updated_at = datetime.utcnow()
        db.commit()
        job_service.set_completed(db, job_id, result={
            "lab_id": row.id,
            "trust_domain": row.trust_domain,
            "k8s_host": row.k8s_vm_name,
            "issuer_url": row.k8s_issuer_url,
            "workload_spiffe_id": row.k8s_workload_spiffe_id,
            "audience": row.k8s_audience,
            # The one command that proves it, and it has to run as the workload: as root it
            # is attested unix:uid:0 and matches no entry.
            "verify": f"sudo -u {K8S_WORKLOAD_USER} kubectl get pods -A",
        })
        logger.info("spire-lab: linked %r to k3s node %s", row.name, row.k8s_vm_name)
    except Exception as exc:  # noqa: BLE001
        # `status` is deliberately untouched: the trust domain is still available and still
        # governs everything it governed before. Only the link failed.
        row = get_lab(db, lab_id)
        if row:
            row.k8s_status = "failed"
            row.k8s_error_message = str(exc)[:2000]
            row.updated_at = datetime.utcnow()
            db.commit()
        logger.error("spire-lab: k8s link failed for %s: %s", lab_id, exc)
        job_service.set_failed(db, job_id, str(exc))


async def _run_k8s_stage(db: Session, *, row: SpireLab, stage: dict, actor: str,
                         asset_backend: str, parent_job_id: str) -> str:
    """``_run_stage`` for the link, writing into the k8s-side stage-job map.

    A separate function rather than a flag on ``_run_stage`` because the two write to
    different columns, and a boolean that decides which column a function writes to is the
    kind of parameter that eventually gets passed wrong.
    """
    from ..api.websocket import broadcast_progress
    from . import ansible_local_run_service

    meta = _stage_meta(row, stage, asset_backend)
    child = job_service.create_job(
        db, "ansible_local", actor, workgroup="ansible", status="queued",
        metadata=meta, batch_id=batch_id_for(row))
    jobs = k8s_stage_jobs(row)
    jobs[stage["key"]] = child.id
    row.k8s_stage_job_ids = json.dumps(jobs)
    db.commit()

    await broadcast_progress(parent_job_id, stage["pct"],
                             f"{stage['label']} (job {child.id[:8]})")
    await ansible_local_run_service.run(db, job_id=child.id, meta=meta)

    db.expire_all()
    fresh = db.query(Job).filter(Job.id == child.id).first()
    return (fresh.status if fresh else "failed") or "failed"


# ── Password Safe onboarding ──────────────────────────────────────────────────
# The half of section 5 of the standup runbook that is deterministic, and it is the
# reason this lab exists: a SPIRE trust domain nothing governs is a demo of SPIRE, not a
# demo of governing machine identities. Both sibling tabs onboard their identities to
# Password Safe (the Certificate Lab through `cert_ps_service`, the Kubernetes tab through
# `workload_k8s_service`), and this was the one that still asked an operator to paste.
#
# WHAT THE BUTTON DOES: create the managed system on the "SPIFFE SVID" platform, pointed
# at this lab's SPIRE server at its API port, in the configured workgroup, referencing the
# functional account whose NAME is the administrative SPIFFE ID.
#
# WHAT IT DELIBERATELY LEAVES TO A HUMAN, and why neither is an oversight:
#
#   * THE FUNCTIONAL ACCOUNT. Its DSS-key field holds the administrative PKCS#12, and
#     creating it here would mean this process reading that credential out of Secrets Safe
#     to push it back in. Every other plugin in this codebase looks a functional account up
#     BY NAME for exactly that reason, and `spire-admin-identity.yml` already writes the
#     PKCS#12 into Secrets Safe under `no_log` without it ever passing through here.
#   * THE `SpiffeTrustDomain` ATTRIBUTE. The plugin takes its whole configuration from
#     BeyondInsight attributes; there is no attribute API in this codebase, and whether the
#     gateway populates attributes for a plugin ACTION has never been observed. That is the
#     open question this lab was built to answer, so writing an attribute writer now would
#     be betting on the answer. `onboarding_gaps` names it, the result reports it, and the
#     page shows it — which is what makes the first live run answer the question instead of
#     hiding it.

PS_REGISTER_JOB_TYPE = "spirelab_ps_register"

# The platform the imported .psplugin presents. Config-overridable because a Password Safe
# admin renaming an imported platform is a real event that has silently switched onboarding
# off before (ps_k8s_token_service._resolve_functional_account documents the case).
_PS_PLATFORM_DEFAULT = "SPIFFE SVID"


def ps_platform() -> str:
    return _cfg("spire_ps_platform", _PS_PLATFORM_DEFAULT)


def onboarding_gaps(row: SpireLab) -> list:
    """What still needs a human after the button, each with its remedy.

    Returned to the page rather than buried in a docstring, because every item here is a
    thing that makes the plugin fail an ACTION while the managed system looks correctly
    onboarded — and the failure mode reads as a credential problem in all three cases.
    """
    gaps = [
        {"what": f"the {ps_platform()!r} functional account",
         "why": ("its DSS-key field holds the administrative PKCS#12, and this dashboard "
                 "never reads that credential — it is written straight into Secrets Safe "
                 "by the identity playbook"),
         "remedy": (f"create a functional account named {row.admin_spiffe_id or '<the admin SPIFFE ID>'} "
                    f"on the {ps_platform()!r} platform and upload the PKCS#12 from "
                    f"{(secret_refs(row) or {}).get('pfx') or 'the lab folder'} as its DSS key")},
        {"what": "the SpiffeTrustDomain attribute",
         "why": ("the plugin reads its configuration from BeyondInsight attributes, and "
                 "whether the gateway populates them for a plugin action is the open "
                 "question this lab exists to answer — so nothing here guesses at it"),
         "remedy": (f"add attribute SpiffeTrustDomain = {row.trust_domain} to the managed "
                    f"system, then run Test Functional Account")},
    ]
    return gaps


def start_ps_register(db: Session, *, lab_id: str, created_by: str,
                      action: str = "register") -> dict:
    """Enqueue onboarding this trust domain as a Password Safe managed system.

    Refused before the job exists when the lab cannot possibly be governed yet, because
    each of these produces a managed system that onboards green and then fails every
    action — which is the failure this whole path is meant to remove, not relocate.
    """
    if action not in ("register", "deregister"):
        raise SpireLabError(f"unknown action {action!r}")
    row = get_lab(db, lab_id)
    if not row:
        raise SpireLabError(f"SPIRE lab {lab_id} not found")

    if action == "register":
        if row.status != "available":
            raise SpireLabError(
                f"{row.name} is {row.status}, not available — onboarding a trust domain "
                f"whose server is not up yet produces a managed system that fails every "
                f"action, and the failure reads as a credential problem")
        if not (row.private_ip or row.public_ip):
            raise SpireLabError(
                f"{row.name} has no recorded address, so nothing could tell Password Safe "
                f"where to reach the SPIRE API")
        if not row.admin_spiffe_id:
            raise SpireLabError(
                f"{row.name} has no administrative SPIFFE ID recorded, and that string is "
                f"the functional account's NAME — without it the managed system would "
                f"inherit the wrong platform. Re-run the identity stage.")
        if row.ps_system_id:
            raise SpireLabError(
                f"{row.name} is already onboarded as managed system {row.ps_system_id}. "
                f"Remove it first if you need to re-create it — a second managed system "
                f"for one trust domain would discover the same entries twice.")
    elif not row.ps_system_id:
        raise SpireLabError(f"{row.name} has no Password Safe managed system recorded")

    job = job_service.create_job(
        db, PS_REGISTER_JOB_TYPE, created_by, workgroup=row.workgroup,
        metadata={"lab_id": row.id, "action": action,
                  "trust_domain": row.trust_domain})
    db.commit()
    logger.info("spire-lab: queued Password Safe %s for %r as job %s",
                action, row.name, job.id)
    return {"lab_id": row.id, "job_id": job.id, "action": action}


async def run_ps_register(db: Session, *, lab_id: str, job_id: str,
                          action: str = "register") -> None:
    """Worker entry point for ``spirelab_ps_register``."""
    from ..api.websocket import broadcast_progress
    from . import ps_api_service, ps_resource_service

    row = get_lab(db, lab_id)
    if not row:
        logger.warning("spire-lab: row %s vanished before the Password Safe %s",
                       lab_id, action)
        return
    job_service.set_running(db, job_id)
    try:
        if action == "deregister":
            await broadcast_progress(job_id, 30, "Removing the Password Safe objects…")
            if row.ps_tf_state:
                await ps_resource_service.deregister(row.ps_tf_state)
            # Cleared whether or not the destroy found anything: the point of clearing is
            # that this dashboard no longer claims to have onboarded the trust domain, and
            # a row still naming a managed system an operator removed by hand is a worse
            # record than one naming none.
            row.ps_system_id = row.ps_tf_state = None
            row.updated_at = datetime.utcnow()
            db.commit()
            job_service.set_completed(db, job_id, result={
                "lab_id": row.id, "deregistered": True})
            return

        if not ps_api_service.configured():
            raise SpireLabError(
                "Password Safe is not configured — set pscli_api_url, pscli_client_id, "
                "pscli_client_secret and pscli_api_account_name")

        await broadcast_progress(job_id, 20, "Resolving the functional account…")
        # The account's NAME is a SPIFFE ID, not a username, and the managed system
        # INHERITS ITS PLATFORM from it — so an account on the wrong platform onboards
        # green and then fails every action. Resolved by name and its platform checked,
        # the same guard ps_k8s_token_service applies for the same reason.
        fa = await ps_api_service.get_functional_account(row.admin_spiffe_id)
        pname = (fa.get("platform_name") or "")
        if pname and ps_platform().lower() not in pname.lower():
            raise SpireLabError(
                f"functional account {row.admin_spiffe_id!r} is on platform {pname!r}, "
                f"not {ps_platform()!r}. The managed system inherits the functional "
                f"account's platform, so this would onboard against the wrong plugin.")
        platform_id = await ps_api_service.get_platform_id(ps_platform())
        workgroup_id = await ps_api_service.get_workgroup_id(
            _cfg("spire_ps_workgroup") or _cfg("passwordsafe_workgroup"))

        await broadcast_progress(job_id, 55, "Creating the Password Safe managed system…")
        # `host_name` is the TRUST DOMAIN (what an operator recognises, and what the plugin
        # governs); the address is the server's. No managed account and no seed — the
        # plugin discovers its accounts as registration entries, which is the count this
        # lab asserts on. See ps_resource_service, method="spiffesvid".
        reg = await ps_resource_service.register_managed_system(
            name=f"spire-{row.trust_domain}", host_name=row.trust_domain,
            functional_account_id=fa["id"], platform_id=platform_id,
            workgroup_id=workgroup_id,
            ip_address=(row.private_ip or row.public_ip),
            port=int(row.bind_port or 8081),
            managed_account_name=row.admin_spiffe_id, method="spiffesvid")
        row.ps_system_id = str(reg.get("managed_system_id") or "")
        row.ps_tf_state = reg.get("tf_state_json")
        row.updated_at = datetime.utcnow()
        db.commit()

        gaps = onboarding_gaps(row)
        for gap in gaps:
            job_service.append_job_log(
                db, job_id, f"still needs a human: {gap['what']} — {gap['remedy']}")
        await broadcast_progress(
            job_id, 95,
            "Managed system created. Two steps still need a human — see the job log and "
            "the lab's onboarding panel.")
        job_service.set_completed(db, job_id, result={
            "lab_id": row.id, "managed_system_id": row.ps_system_id,
            "platform": ps_platform(), "trust_domain": row.trust_domain,
            "discovery_expected": row.discovery_expected,
            "gaps": [g["what"] for g in gaps]})
    except Exception as exc:
        # The message, never a traceback and never a chained cause: it reaches a browser
        # through the row (CodeQL py/stack-trace-exposure, and the reason ansible_run_gate
        # gives at length).
        row.error_message = str(exc)[:2000]
        row.updated_at = datetime.utcnow()
        db.commit()
        logger.error("spire-lab: Password Safe %s failed for %s: %s", action, lab_id, exc)
        job_service.set_failed(db, job_id, str(exc))
