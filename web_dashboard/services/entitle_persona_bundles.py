"""Persona × cloud access bundles in Entitle. **This module grants access**; it is not
``personas``.

``services/personas`` is curation only by contract: a persona may reorder the dashboard
and never hide or grant anything, which is what lets it be chosen from a cookie. A bundle
is the opposite — requesting one puts a person in groups and cloud roles — so the
catalog lives here, imports nothing from ``personas``, and only shares its persona KEYS
(``tests/test_entitle_persona_bundles`` pins that every key here is a real persona).

Each bundle is one Entitle request for one job function on one cloud, built from three
components, each resolved against the live tenant:

  ``group``      the virtual-app role over the directory group ``ent-<cloud>-<key>`` —
                 the group the cloud's SSO maps to a permission set / role / binding.
                 Found among active virtual apps (``entitle_virtual_app_service``).
  ``cloud``      roles in the cloud's own Entitle integration (AWS, Azure, GCP), pinned
                 per cloud in Settings (``entitle_cloud_integration_<cloud>``), matched by
                 role name or external id. OCI has none: Entitle grants OCI through IAM
                 group membership, which is the ``group`` component — the catalog's
                 ``oci_policy`` lines say what that group's policy should allow.
  ``dashboard``  the dashboard's own ``dashboard-<scope>-<level>`` groups that
                 ``bootstrap_entitle_groups`` created, matched tenant-wide by group name.

A component is ``resolved`` (every part found), ``absent`` (nothing configured or nothing
found — left out), or ``partial`` (some parts found, some not). **A partial component
blocks its bundle**: a bundle that silently granted three of the four things its name
promises is worse than no bundle. A bundle also needs cloud access — the ``group`` or
``cloud`` component resolved — or it would grant only dashboard permissions under a
cloud's name. Nothing is ever guessed: an ambiguous match (one role name on several
subscriptions, one group name in two directories) is reported, not picked.

Durations are the tier's own (:mod:`entitle_tiers`), capped per bundle, because the tier
workflows' single rule stops at the tier's longest duration — a longer bundle request
would match no rule.
"""
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from . import entitle_tiers

logger = logging.getLogger(__name__)

JOB_TYPE = "entitle_persona_bundles"
CLOUDS = ("aws", "azure", "gcp", "oci")
CLOUD_LABELS = {"aws": "AWS", "azure": "Azure", "gcp": "GCP", "oci": "OCI"}
CATEGORY = "Cloud access"
GROUP_PREFIX = "ent"


@dataclass(frozen=True)
class PersonaAccess:
    """One job function's access, on every cloud.

    ``key`` is a persona key, or ``<persona>-<variant>`` for a second level of the same
    role (``sre-breakglass``); :attr:`persona` is the part before the dash. Role names per
    cloud are what that cloud's Entitle integration calls them — AWS managed policy or
    permission-set names, Azure built-in role names, GCP role ids (matched against the
    role's external id as well as its name).
    """
    key: str
    label: str
    purpose: str
    tier: str
    max_duration: int
    aws: tuple = ()
    azure: tuple = ()
    gcp: tuple = ()
    oci_policy: tuple = ()          # documentation for the ent-oci-<key> group's policy
    dashboard: tuple = ()           # "{cloud}" is replaced by the cloud key

    @property
    def persona(self) -> str:
        return self.key.split("-", 1)[0]

    def cloud_roles(self, cloud: str) -> tuple:
        return getattr(self, cloud) if cloud in ("aws", "azure", "gcp") else ()

    def dashboard_groups(self, cloud: str) -> tuple:
        return tuple(g.format(cloud=cloud) for g in self.dashboard)

    def durations(self) -> tuple:
        return tuple(d for d in entitle_tiers.TIER_DURATIONS[self.tier]
                     if d <= self.max_duration)


CATALOG = (
    PersonaAccess(
        key="cloudops", label="Cloud Ops",
        purpose="Run and fix compute: start, stop, resize and rebuild instances, read "
                "everything else.",
        tier="single_approver", max_duration=43200,
        aws=("AmazonEC2FullAccess", "ReadOnlyAccess"),
        azure=("Virtual Machine Contributor", "Reader"),
        gcp=("roles/compute.instanceAdmin.v1", "roles/viewer"),
        oci_policy=("manage instance-family in compartment <c>",
                    "read all-resources in compartment <c>"),
        dashboard=("dashboard-{cloud}-write", "dashboard-images-write"),
    ),
    PersonaAccess(
        key="devops", label="DevOps",
        purpose="Ship to non-production: clusters, registries and pipelines, without "
                "identity administration.",
        tier="single_approver", max_duration=43200,
        aws=("PowerUserAccess",),
        azure=("Contributor", "Azure Kubernetes Service Cluster User Role"),
        gcp=("roles/container.developer", "roles/artifactregistry.writer"),
        oci_policy=("manage cluster-family in compartment <c>",
                    "manage repos in compartment <c>"),
        dashboard=("dashboard-{cloud}-read", "dashboard-k8s-write",
                   "dashboard-cloud_function-write", "dashboard-config_mgmt-write"),
    ),
    PersonaAccess(
        key="dba", label="DBA",
        purpose="Administer managed databases. Pairs with, not instead of, the "
                "per-database ephemeral accounts the dashboard registers.",
        tier="single_approver", max_duration=43200,
        aws=("AmazonRDSFullAccess",),
        azure=("SQL DB Contributor", "SQL Server Contributor"),
        gcp=("roles/cloudsql.admin",),
        oci_policy=("manage database-family in compartment <c>",
                    "manage autonomous-database-family in compartment <c>"),
        dashboard=("dashboard-{cloud}-read", "dashboard-cloud_database-write"),
    ),
    PersonaAccess(
        key="security", label="Security / IAM",
        purpose="Audit configuration and identity across the account, read-only.",
        tier="auto_approve", max_duration=3600,
        aws=("SecurityAudit",),
        azure=("Security Reader",),
        gcp=("roles/iam.securityReviewer",),
        oci_policy=("inspect all-resources in tenancy", "read audit-events in tenancy"),
        dashboard=("dashboard-{cloud}-read", "dashboard-audit-read",
                   "dashboard-inventory-read"),
    ),
    PersonaAccess(
        key="sre", label="SRE",
        purpose="Operate: read everything, own monitoring and alerting.",
        tier="auto_approve", max_duration=3600,
        aws=("ReadOnlyAccess", "CloudWatchFullAccess"),
        azure=("Monitoring Contributor", "Reader"),
        gcp=("roles/monitoring.editor", "roles/logging.viewer"),
        oci_policy=("read all-resources in compartment <c>",
                    "manage alarms in compartment <c>"),
        dashboard=("dashboard-{cloud}-read", "dashboard-k8s-read",
                   "dashboard-inventory-read"),
    ),
    PersonaAccess(
        key="sre-breakglass", label="SRE break-glass",
        purpose="Incident-only administrator access, one hour at most, two approvers.",
        tier="two_approver", max_duration=3600,
        aws=("AdministratorAccess",),
        azure=("Contributor",),
        gcp=("roles/editor",),
        oci_policy=("manage all-resources in compartment <c>",),
        dashboard=("dashboard-{cloud}-delete",),
    ),
    PersonaAccess(
        key="netadmin", label="Network admin",
        purpose="VPCs / VNets, routing, firewalls and load balancers.",
        tier="single_approver", max_duration=43200,
        aws=("NetworkAdministrator",),
        azure=("Network Contributor",),
        gcp=("roles/compute.networkAdmin", "roles/compute.securityAdmin"),
        oci_policy=("manage virtual-network-family in compartment <c>",),
        dashboard=("dashboard-{cloud}-read", "dashboard-gateways-write"),
    ),
    PersonaAccess(
        key="finops", label="FinOps",
        purpose="Cost, budgets and recommendations; no resource changes.",
        tier="auto_approve", max_duration=3600,
        aws=("Billing", "AWSBudgetsReadOnlyAccess"),
        azure=("Cost Management Contributor",),
        gcp=("roles/billing.viewer", "roles/recommender.viewer"),
        oci_policy=("read usage-reports in tenancy", "manage usage-budgets in tenancy"),
        dashboard=("dashboard-{cloud}-read", "dashboard-costs-write"),
    ),
    PersonaAccess(
        key="aiops", label="AI platform",
        purpose="Build on the cloud's managed AI services.",
        tier="single_approver", max_duration=43200,
        aws=("AmazonBedrockFullAccess",),
        azure=("Azure AI Developer",),
        gcp=("roles/aiplatform.user",),
        oci_policy=("manage generative-ai-family in compartment <c>",),
        dashboard=("dashboard-{cloud}-read", "dashboard-cloud_function-write"),
    ),
)

_BY_KEY = {p.key: p for p in CATALOG}


def bundle_key(access_key: str, cloud: str) -> str:
    return f"{access_key}-{cloud}"


def group_name(access_key: str, cloud: str) -> str:
    return f"{GROUP_PREFIX}-{cloud}-{access_key}"


def bundle_name(p: PersonaAccess, cloud: str) -> str:
    return f"{p.label} — {CLOUD_LABELS[cloud]}"


def catalog() -> list:
    """Every bundle the catalog defines, flat, for the settings card."""
    out = []
    for p in CATALOG:
        for cloud in CLOUDS:
            out.append({
                "key": bundle_key(p.key, cloud), "access": p.key, "persona": p.persona,
                "cloud": cloud, "name": bundle_name(p, cloud), "label": p.label,
                "purpose": p.purpose, "tier": p.tier,
                "tier_label": entitle_tiers.TIER_LABELS[p.tier],
                "durations": list(p.durations()), "group": group_name(p.key, cloud),
                "cloud_roles": list(p.cloud_roles(cloud)),
                "oci_policy": list(p.oci_policy) if cloud == "oci" else [],
                "dashboard_groups": list(p.dashboard_groups(cloud)),
            })
    return out


def suggest_for_group(group: str) -> Optional[dict]:
    """Name a virtual-app role for a directory group that follows ``ent-<cloud>-<key>``.
    ``None`` for any other group — no guessing at groups named some other way."""
    g = (group or "").strip().lower()
    for cloud in CLOUDS:
        prefix = f"{GROUP_PREFIX}-{cloud}-"
        if g.startswith(prefix) and g[len(prefix):] in _BY_KEY:
            p = _BY_KEY[g[len(prefix):]]
            return {"name": bundle_name(p, cloud)[:50], "resource": CLOUD_LABELS[cloud],
                    "tier": p.tier, "persona": p.key, "cloud": cloud}
    return None


# ── Resolution against the tenant ────────────────────────────────────────────

def _cfg(key: str) -> str:
    from . import config_service
    from ..config import settings
    return str(config_service.get(key) or getattr(settings, key, "") or "")


def cloud_settings(cfg=None) -> dict:
    cfg = cfg or _cfg
    return {c: {"integration_id": (cfg(f"entitle_cloud_integration_{c}") or "").strip(),
                "resource": (cfg(f"entitle_cloud_resource_{c}") or "").strip()}
            for c in CLOUDS}


def _match_role(roles: list, wanted: str, resource: str) -> tuple:
    """``(role, "")`` or ``(None, reason)``. Exact, case-insensitive on name or external
    id, narrowed to ``resource`` (a resource name or id) when one is set."""
    w = wanted.casefold()
    hits = [r for r in roles
            if r["name"].casefold() == w or (r.get("external_id") or "").casefold() == w]
    if resource:
        hits = [r for r in hits if resource in (r["resource_id"], r["resource_name"])]
    if not hits:
        return None, f"{wanted}: not found" + (f" on {resource}" if resource else "")
    if len({r["id"] for r in hits}) > 1:
        where = ", ".join(sorted({r["resource_name"] or r["resource_id"] for r in hits})[:4])
        return None, (f"{wanted}: on {len(hits)} resources ({where}) — set the resource "
                      "for this cloud to choose one")
    return hits[0], ""


def _component(parts: list) -> dict:
    """``parts`` = ``[(label, role_or_None, reason)]`` → a component summary."""
    found = [{"label": lbl, "role_id": r["id"], "role": r["name"],
              "resource": r["resource_name"]} for lbl, r, _ in parts if r]
    missing = [{"label": lbl, "reason": why} for lbl, r, why in parts if not r]
    state = ("absent" if not found else "resolved" if not missing else "partial")
    return {"state": state, "found": found, "missing": missing}


async def resolve(db, keys: list, *, cfg=None, tp=None) -> list:
    """Resolve each requested bundle key; returns the preview rows. Reads Entitle only."""
    from . import entitle_public_api as api
    from . import entitle_virtual_app_service as vapps
    cfg = cfg or _cfg
    wanted = []
    for k in dict.fromkeys(keys):
        access, _, cloud = k.rpartition("-")
        if cloud in CLOUDS and access in _BY_KEY:
            wanted.append((_BY_KEY[access], cloud))
    if not wanted:
        return []

    vroles = {}
    for r in vapps.active_roles(db):
        if r.get("persona") and r.get("cloud"):
            vroles.setdefault((r["persona"], r["cloud"]), []).append(r)

    clouds = cloud_settings(cfg)
    native: dict = {}
    for cloud in {c for _, c in wanted}:
        iid = clouds[cloud]["integration_id"]
        if iid and cloud != "oci":
            native[cloud] = await api.list_roles(integration_id=iid, cfg=cfg, tp=tp,
                                                 max_pages=10)

    # The dashboard-* groups live in whichever directory integration user-JIT adopted
    # them from. With that integration pinned (entitle_dashboard_groups_integration_id)
    # every role in it is read once; without it each group is searched for by name
    # across the tenant, and only an exact group-name match counts either way.
    dash_names = sorted({g for p, c in wanted for g in p.dashboard_groups(c)})
    dash: dict = {}
    dash_iid = (cfg("entitle_dashboard_groups_integration_id") or "").strip()
    pool = (await api.list_roles(integration_id=dash_iid, cfg=cfg, tp=tp, max_pages=10)
            if dash_iid and dash_names else None)
    for g in dash_names:
        rows = pool if pool is not None else await api.list_roles(
            search=g, cfg=cfg, tp=tp, max_pages=1)
        dash[g] = [r for r in rows if r["resource_name"].casefold() == g.casefold()]

    flows = entitle_tiers.resolve_workflows(cfg)
    out = []
    for p, cloud in wanted:
        # group: the virtual-app role an operator tagged with this persona key + cloud
        vr = vroles.get((p.key, cloud), [])
        if len(vr) > 1:
            group = _component([(group_name(p.key, cloud), None,
                                 f"{len(vr)} virtual-app roles are tagged {p.key}/{cloud}")])
        elif vr:
            r = vr[0]
            group = _component([(group_name(p.key, cloud),
                                 {"id": r["role_id"], "name": r["name"],
                                  "resource_name": r.get("app_name") or ""}, "")])
        else:
            group = _component([])

        names = p.cloud_roles(cloud)
        if cloud in native and names:
            res = clouds[cloud]["resource"]
            cloud_c = _component([(n, *_match_role(native[cloud], n, res)) for n in names])
        else:
            cloud_c = _component([])

        dparts = []
        for g in p.dashboard_groups(cloud):
            hits = dash.get(g) or []
            member = [h for h in hits if "member" in h["name"].casefold()] or hits
            if len({h["id"] for h in member}) == 1:
                dparts.append((g, member[0], ""))
            else:
                dparts.append((g, None, "not found" if not member else
                               f"{len(member)} matching roles"))
        dash_c = _component(dparts)

        comps = {"group": group, "cloud": cloud_c, "dashboard": dash_c}
        blockers = [f"{n} is partial" for n, c in comps.items() if c["state"] == "partial"]
        if group["state"] != "resolved" and cloud_c["state"] != "resolved":
            blockers.append("no cloud access resolved — create the virtual-app role for "
                            f"{group_name(p.key, cloud)}, or pin the {CLOUD_LABELS[cloud]} "
                            "integration")
        if not flows[p.tier]["id"]:
            blockers.append(f"no workflow for the {entitle_tiers.TIER_LABELS[p.tier]} tier")
        role_ids = [f["role_id"] for c in comps.values() for f in c["found"]]
        out.append({
            "key": bundle_key(p.key, cloud), "name": bundle_name(p, cloud),
            "persona": p.persona, "access": p.key, "cloud": cloud, "tier": p.tier,
            "durations": list(p.durations()), "components": comps,
            "role_ids": list(dict.fromkeys(role_ids)),
            "workflow_id": flows[p.tier]["id"],
            "workflow_fallback": flows[p.tier]["fallback"],
            "ready": not blockers, "blockers": blockers,
        })
    return out


def _hcl_bundle(row: dict) -> dict:
    p = _BY_KEY[row["access"]]
    return {
        "key": row["key"], "name": row["name"],
        "description": f"{p.purpose} ({entitle_tiers.TIER_LABELS[p.tier]}; created by "
                       "VM Dashboard from its persona catalog.)",
        "category": CATEGORY,
        "tags": ["vm-dashboard", f"persona:{p.persona}", f"cloud:{row['cloud']}",
                 f"tier:{p.tier}"],
        "workflow_id": row["workflow_id"],
        "allowed_durations": row["durations"],
        "role_ids": row["role_ids"],
    }


# ── Lifecycle ────────────────────────────────────────────────────────────────

def _loads(raw):
    try:
        v = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return v if isinstance(v, list) else []


def set_to_dict(row) -> dict:
    return {"id": row.id, "status": row.status, "bundles": _loads(row.bundles_json),
            "error": row.error or "", "created_by": row.created_by or "",
            "created_at": row.created_at.isoformat() if row.created_at else None}


def list_sets(db) -> list:
    from ..database import EntitleBundleSet
    rows = (db.query(EntitleBundleSet).filter(EntitleBundleSet.status != "deleted")
            .order_by(EntitleBundleSet.created_at.desc()).all())
    return [set_to_dict(r) for r in rows]


def start_apply(db, rows: list, *, created_by: str):
    """Record a set of READY preview rows and enqueue its apply. Returns ``(set, job)``."""
    from ..database import EntitleBundleSet
    from . import job_service
    ready = [r for r in rows if r.get("ready")]
    if not ready:
        raise ValueError("none of the selected bundles is ready to apply")
    bset = EntitleBundleSet(status="applying", bundles_json=json.dumps(ready),
                            created_by=created_by)
    db.add(bset)
    db.commit()
    db.refresh(bset)
    job = job_service.create_job(db, job_type=JOB_TYPE, created_by=created_by,
                                 metadata={"action": "apply", "set_id": bset.id})
    return bset, job


def start_delete(db, bset, *, created_by: str):
    from . import job_service
    if bset.status in ("applying", "destroying"):
        raise ValueError(f"this bundle set is {bset.status}")
    bset.status, bset.error = "destroying", None
    db.commit()
    return job_service.create_job(db, job_type=JOB_TYPE, created_by=created_by,
                                  metadata={"action": "delete", "set_id": bset.id})


async def run_job(db, *, job_id: str, meta: dict) -> None:
    """Worker entry for an ``entitle_persona_bundles`` job (``action`` apply | delete)."""
    from ..api.websocket import broadcast_progress
    from ..database import EntitleBundleSet
    from . import entitle_registration_service as reg
    from . import job_service
    job_service.set_running(db, job_id)
    bset = db.query(EntitleBundleSet).filter(EntitleBundleSet.id == meta.get("set_id")).first()
    if not bset:
        job_service.set_failed(db, job_id, "bundle set not found")
        return
    action = meta.get("action") or "apply"
    try:
        if action == "delete":
            await broadcast_progress(job_id, 20, "Removing the bundles from Entitle…")
            if bset.tf_state_json:
                await reg.deregister(bset.tf_state_json)
            bset.status, bset.tf_state_json = "deleted", None
            bset.updated_at = datetime.utcnow()
            db.commit()
            job_service.set_completed(db, job_id, {"action": "delete", "set_id": bset.id})
            return
        rows = _loads(bset.bundles_json)
        await broadcast_progress(job_id, 20, f"Creating {len(rows)} bundle(s) in Entitle…")
        result = await reg.register_bundles(bundles=[_hcl_bundle(r) for r in rows])
        ids = (result.get("outputs") or {}).get("bundle_ids") or {}
        for r in rows:
            r["bundle_id"] = str(ids.get(reg._safe_name(r["key"])) or "")
        bset.bundles_json = json.dumps(rows)
        bset.tf_state_json = result.get("tf_state_json")
        bset.status, bset.error = "active", None
        bset.updated_at = datetime.utcnow()
        db.commit()
        job_service.set_completed(db, job_id, {"action": "apply", "set_id": bset.id,
                                               "bundles": len(rows)})
    except Exception as exc:  # noqa: BLE001 — every failure is recorded on the row
        logger.exception("entitle persona bundles job failed set=%s", bset.id)
        bset.status, bset.error = "failed", str(exc)[:4000]
        bset.updated_at = datetime.utcnow()
        db.commit()
        job_service.set_failed(db, job_id, str(exc))

