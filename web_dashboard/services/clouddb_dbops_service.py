"""Deploy the "bt-dbops" Cloud Run service the Password Safe GCP plugin talks to.

The ``cloud-run`` channel of the GCP Cloud SQL plugin needs a small service inside the
VPC that holds the database drivers and opens the actual connection, so that a
credential can travel in a request body instead of being mirrored into Secret Manager.
Until now that was an operator step, and three places in the repo said the dashboard
could not do it. It can: what it cannot build is the plugin repo's .NET image, which it
does not need — the plugin depends on an HTTP contract, and
``fnworkloads/ps_dbops.py`` serves it through the same Cloud Functions gen2
source-deploy path that already builds, VPC-attaches and deploys ``db_grant``.

**One service per (project, region), not per database.** Unlike ``db_grant``, this
service is stateless with respect to the database: the instance, the catalog, the TLS
choice and the credential all arrive in the request, because that is the contract. So
there is nothing per-database to bind, and three things argue against binding anyway —
Direct VPC egress is region-locked (which also rules out one per project), the warm
instance the service needs for correctness bills continuously, and Cloud Run reserves
subnet IPs in /28 blocks. See docs/design/ps-dbops-cloud-run.md.

The deploy is TWO applies, and the reason is a genuine ordering problem rather than an
oversight: the service's audience is its own URL, which does not exist until the first
apply has finished. So the first apply creates it and the second stamps
``FN_DBOPS_AUDIENCE`` from the recorded ``invoke_url``. In the window between, the
service is deployed and refuses every request — visible, safe, and better than a
deploy that cannot be made at all.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from ..database import CloudFunction
from . import config_service, job_service

logger = logging.getLogger(__name__)

WORKLOAD = "ps_dbops"

# Deterministic, and needs no suffix: Cloud Run service names are unique per project
# and region, so one name per region is already one service per region — and a
# redeploy converges on the same service instead of accumulating one per attempt.
SERVICE_NAME = "bt-dbops"

# What the plugin article calls load-bearing, and why each one is (see the module note
# and the settings panel). These are DEFAULTS: every one is overridable by config.
_DEFAULT_MIN_INSTANCES = 1
_DEFAULT_CONCURRENCY = 8
_DEFAULT_TIMEOUT_SECONDS = 120
_DEFAULT_MAX_INSTANCES = 5

_INGRESS = {"all": "ALLOW_ALL", "internal": "ALLOW_INTERNAL_AND_GCLB"}


class DbOpsDeployError(Exception):
    pass


def _cfg(key: str) -> str:
    return (config_service.get(key) or "").strip()


def _int_cfg(key: str, default: int) -> int:
    try:
        value = int(str(config_service.get(key) or default).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def invoker_members() -> list:
    """The Resource Broker identities granted ``roles/run.invoker``.

    Accepts bare service-account emails as well as fully-qualified IAM members,
    because an operator copying an email out of the GCP console has no reason to know
    Terraform wants a ``serviceAccount:`` prefix — and a missing prefix is an apply
    error 90 seconds in, not a validation error at the click.
    """
    members = []
    for raw in _cfg("clouddb_ps_gcp_dbops_invokers").split(","):
        entry = raw.strip()
        if not entry:
            continue
        if ":" not in entry:
            entry = f"serviceAccount:{entry}"
        members.append(entry)
    return members


def ingress_setting() -> str:
    return _INGRESS.get(_cfg("clouddb_ps_gcp_dbops_ingress").lower() or "all",
                        _INGRESS["all"])


# ── Lookup ───────────────────────────────────────────────────────────────────

def find_for_region(db: Session, region: str) -> Optional[CloudFunction]:
    """The dbops service for ``region``, or ``None``.

    Keyed on (workload, cloud, region) rather than on the name, so a hand-renamed
    service is still found and a second one cannot be deployed beside it unnoticed.
    """
    if not region:
        return None
    return (db.query(CloudFunction)
            .filter(CloudFunction.workload == WORKLOAD,
                    CloudFunction.cloud == "gcp",
                    CloudFunction.region == region,
                    CloudFunction.status != "deleted")
            .order_by(CloudFunction.created_at.desc())
            .first())


def origin(url: str) -> str:
    """``scheme://host`` from a URL, or ``""``.

    The plugin uses address field 4 verbatim as BOTH the request target and the token
    audience and rejects a path, query or fragment
    (ps_resource_service._validate_dbgcp_dns_name), so a trailing slash from a
    Terraform output is not cosmetic — it is an address Password Safe will refuse.
    """
    raw = (url or "").strip()
    if "://" not in raw:
        return ""
    scheme, _, rest = raw.partition("://")
    host = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    return f"{scheme}://{host}" if host else ""


def audience_for_region(db: Session, region: str) -> str:
    """The audience a managed system in ``region`` should carry, or ``""``.

    Only an AVAILABLE service counts. A half-deployed one has a URL that may still
    change, and stamping it into a managed-system address would leave Password Safe
    holding an address for a service that never finished.
    """
    row = find_for_region(db, region)
    if not row or row.status != "available":
        return ""
    return origin(row.invoke_url or "")


# ── Eligibility ──────────────────────────────────────────────────────────────

def deploy_ineligible_reason(db: Session, region: str) -> Optional[str]:
    """Why a dbops service cannot be deployed to ``region``, or ``None``.

    One source of truth for the button, the API pre-flight and :func:`start_deploy`,
    so the button can never offer what the endpoint refuses.
    """
    from . import cloud_function_service

    if not region:
        return "a region is required — the service must live in the database's region"
    if not (_cfg("gcp_project") or _cfg("gcp_project_id")):
        return "no GCP project configured — set gcp_project in Settings → GCP"
    if not cloud_function_service.terraform_available():
        return "terraform is not available in this image, so nothing can be deployed"
    existing = find_for_region(db, region)
    if existing is not None and existing.status not in ("failed",):
        return (f"a DB-Ops service already exists in {region} "
                f"({existing.name}, {existing.status}) — one per region is the design")
    try:
        cloud_function_service._resolved_network(
            "gcp", region, network_mode="vpc", subnet_ids=None, subnet_id="",
            vpc_connector="", security_group_ids=None)
    except cloud_function_service.CloudFunctionError as exc:
        # Re-raised as a reason rather than swallowed: without a VPC the service
        # deploys and then cannot reach a single private-IP instance.
        return str(exc)
    return None


# ── Deploy ───────────────────────────────────────────────────────────────────

def start_deploy(db: Session, *, region: str, created_by: str = "") -> dict:
    """Queue a ``clouddb_dbops_deploy`` job. Validates first, so an impossible
    deploy fails at the click rather than three minutes into an apply."""
    reason = deploy_ineligible_reason(db, region)
    if reason:
        raise DbOpsDeployError(reason)
    job = job_service.create_job(
        db, job_type="clouddb_dbops_deploy", created_by=created_by,
        metadata={"region": region, "workload": WORKLOAD, "name": SERVICE_NAME})
    return {"ok": True, "region": region, "job_id": job.id}


def _environment(db: Session, region: str) -> dict:
    """The service's non-secret settings.

    ``FN_DBOPS_AUDIENCE`` is absent on the first pass — it is the service's own URL,
    which does not exist yet. Everything else is known up front.
    """
    return {
        # Fails closed when empty, by design: one service per region means IAM
        # authenticates the caller but does not scope the target, and this is what
        # puts the boundary back. Seeded from the instances already onboarded in this
        # region so a redeploy does not silently narrow an existing service.
        "FN_DBOPS_ALLOWED_INSTANCES": ",".join(onboarded_instances(db, region)) or "",
        # The same list the invoker bindings use, checked a second time inside the
        # container. The binding is the authentication; this is the authorization, and
        # they are in different trust domains on purpose.
        "FN_DBOPS_ALLOWED_INVOKERS": ",".join(
            m.split(":", 1)[1] for m in invoker_members() if ":" in m),
        # OFF, now that the contract is implemented. It was on while capturing the
        # real request was the point of the build; leaving it on would write a request
        # body -- redacted, but still -- into Cloud Logging on every rotation forever,
        # which is the opposite of this channel's whole argument. Turn it on for as
        # long as it takes to compare one real request against the parser:
        #   gcloud run services update <svc> --set-env-vars FN_DBOPS_CAPTURE=1
        # and expect a redeploy from here to put it back.
        "FN_DBOPS_CAPTURE": "0",
    }


def onboarded_instances(db: Session, region: str) -> list:
    """``project:region:instance`` for every GCP database already onboarded in
    ``region`` on the cloud-run channel.

    Read from the CloudDatabase rows rather than accumulated in the function's own
    settings, so the allowlist is derived from inventory and a redeploy reconstructs
    it exactly. Sorted, so an unchanged fleet produces an unchanged environment and
    terraform sees no diff.
    """
    from ..database import CloudDatabase

    project = _cfg("gcp_project") or _cfg("gcp_project_id")
    if not project:
        return []
    rows = (db.query(CloudDatabase)
            .filter(CloudDatabase.cloud == "gcp",
                    CloudDatabase.region == region,
                    CloudDatabase.status != "deleted")
            .all())
    names = {f"{project}:{region}:{row.instance_id}"
             for row in rows if row.instance_id}
    return sorted(names)


async def run_deploy(db: Session, *, region: str, job_id: str) -> None:
    """Deploy the service, then stamp its own URL into it as the audience.

    Two applies, one job: an operator watching a half-finished deploy cannot tell
    whether to retry or clean up, and the service is useless between the two.
    """
    from . import cloud_function_service

    job_service.set_running(db, job_id)
    try:
        reason = deploy_ineligible_reason(db, region)
        if reason:
            raise DbOpsDeployError(reason)

        members = invoker_members()
        if not members:
            # Not fatal. A service nobody can call is a legitimate intermediate state
            # — the broker identities are often not known yet — and refusing the
            # deploy would make the operator do it in the other order for no reason.
            job_service.update_progress(
                db, job_id, 5,
                "No invokers configured — the service will deploy but NO Resource "
                "Broker will be able to call it until "
                "clouddb_ps_gcp_dbops_invokers is set.")

        job_service.update_progress(db, job_id, 10, "Deploying the DB-Ops service…")
        deployed = cloud_function_service.deploy(
            db, cloud="gcp", region=region, name=SERVICE_NAME, workload=WORKLOAD,
            created_by="clouddb-dbops",
            # vpc, always: the service exists to reach a private-IP instance.
            # _check_front_door refuses anything else for this workload, but passing
            # it explicitly keeps the intent readable here.
            network_mode="vpc",
            auth_mode="run_invoker",
            environment=_environment(db, region),
            ingress_settings=ingress_setting(),
            invoker_members=members,
            min_instances=_int_cfg("clouddb_ps_gcp_dbops_min_instances",
                                   _DEFAULT_MIN_INSTANCES),
            concurrency=_int_cfg("clouddb_ps_gcp_dbops_concurrency",
                                 _DEFAULT_CONCURRENCY),
            timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
            max_instances=_DEFAULT_MAX_INSTANCES)
        fn_id = deployed["fn_id"]
        await cloud_function_service.run_deploy_apply(
            db, fn_id=fn_id, job_id=deployed["job_id"],
            tf_variables=deployed["tf_variables"])

        row = cloud_function_service.get_function(db, fn_id)
        if not row or row.status != "available":
            raise DbOpsDeployError(
                f"the DB-Ops service did not deploy (status: "
                f"{getattr(row, 'status', 'missing')}) — see its job for the "
                "terraform output")

        audience = origin(row.invoke_url or "")
        if not audience:
            raise DbOpsDeployError(
                "the DB-Ops service deployed but reported no invoke URL, so there is "
                "no audience to record — a managed-system address cannot be built")

        job_service.update_progress(db, job_id, 70,
                                    f"Recording the audience {audience}…")
        update = cloud_function_service.update_environment(
            db, fn_id=fn_id, environment={"FN_DBOPS_AUDIENCE": audience},
            created_by="clouddb-dbops")
        await cloud_function_service.run_update_apply(
            db, fn_id=fn_id, job_id=update["job_id"],
            tf_variables=update["tf_variables"])

        db.refresh(row)
        if row.status != "available":
            raise DbOpsDeployError(
                "the DB-Ops service deployed but recording its audience failed — it "
                "will refuse every request until FN_DBOPS_AUDIENCE is set. Re-run "
                "this deploy")

        job_service.set_completed(db, job_id, {
            "fn_id": fn_id, "region": region, "audience": audience,
            "invokers": len(members),
            "allowed_instances": len(onboarded_instances(db, region)),
            "contract_implemented": False,
        })
        logger.info("clouddb dbops deployed region=%s fn_id=%s audience=%s invokers=%d",
                    region, fn_id, audience, len(members))
    except Exception as exc:
        logger.error("clouddb dbops deploy failed region=%s: %s", region, exc)
        job_service.set_failed(db, job_id, str(exc))


def refresh_allowlist(db: Session, *, region: str, created_by: str = "") -> dict:
    """Bring the service's instance allowlist back in line with inventory.

    Called after a GCP database is onboarded on the cloud-run channel. A no-op when
    nothing changed — the list is derived and sorted, so an unchanged fleet produces
    an identical environment and no update job at all.
    """
    from . import cloud_function_service

    row = find_for_region(db, region)
    if not row or row.status != "available":
        return {"ok": False, "reason": f"no DB-Ops service available in {region}"}
    wanted = ",".join(onboarded_instances(db, region))
    import json as _json
    current = (_json.loads(row.env_ref or "{}") or {}).get("FN_DBOPS_ALLOWED_INSTANCES")
    if current == wanted:
        return {"ok": True, "changed": False, "fn_id": row.id}
    result = cloud_function_service.update_environment(
        db, fn_id=row.id, environment={"FN_DBOPS_ALLOWED_INSTANCES": wanted},
        created_by=created_by or "clouddb-dbops")
    return {"ok": True, "changed": True, "fn_id": row.id, "job_id": result["job_id"],
            "tf_variables": result["tf_variables"], "allowed_instances": wanted}
