"""BeyondTrust Gateway registry — the inventory behind the Gateways tab.

The dashboard has always auto-ensured one shared Gateway host per cloud and
reference-counted it. That is still true and unchanged; this adds the other half —
gateways an operator deploys deliberately to carry session load, with no cap and no
reference counting, and one place that answers "what gateways do we have".

Both kinds live in the same table (``database.Gateway``) so the page shows everything
the dashboard put in the cloud. They differ in who owns the lifecycle:

  * managed   — created and removed by ``jumpoint_host_service``'s ensure/idle pair.
                Adopted into a row on first ensure. Not deletable from the UI.
  * requested — created and removed by the jobs below. Nothing counts references to
                it; it lives until someone removes it.

Every gateway host in a cloud uses that cloud's configured deploy key, so they join
the same PRA Gateway as additional cluster nodes rather than becoming separate
Gateways. Nothing here needs to change for resources to use them: a resource targets
the Gateway by name as it always did, and PRA distributes across the cluster.

Dispatched by ``jobs_worker`` as ``gateway_deploy`` / ``gateway_teardown``.
"""
import asyncio
import logging
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

CLOUDS = ("aws", "azure", "gcp")

# Statuses a reconcile pass may overwrite. A row in any other state belongs to the job
# driving it: `provisioning` legitimately has no host yet and `deleting` is about to have
# none, so verifying either would just race its owner. `error` states its own reason and
# keeps it.
VERIFIABLE_STATUSES = ("running", "degraded", "missing")

# Statuses that mean "this gateway is not in the cloud". Expressed as an exclusion rather
# than a list of live states so that a status added later still counts as live — the
# firewall readers below fail toward allowing a gateway through, not toward locking it out.
GONE_STATUSES = ("deleted", "missing")

_RECONCILE_TIMEOUT_S = 12

# Deliberately absent: a cap. "Three in us-central1 and two in us-east-2" is the
# stated use case, and the right number is a function of session load, which the
# dashboard cannot see. Cloud quotas are the real ceiling.


class GatewayError(Exception):
    pass


def _cfg(key: str, fallback: str = "") -> str:
    from . import config_service
    from ..config import settings
    return config_service.get(key) or getattr(settings, key, None) or fallback


# ── registry ──────────────────────────────────────────────────────────────────

def list_gateways(db, cloud: str = "") -> list[dict]:
    """Every gateway row, newest first. Serialised here so the API layer stays thin."""
    from ..database import Gateway
    q = db.query(Gateway).filter(Gateway.status != "deleted")
    if cloud:
        q = q.filter(Gateway.cloud == cloud)
    return [_to_dict(g) for g in q.order_by(Gateway.created_at.desc()).all()]


def get_gateway(db, gateway_id: str):
    from ..database import Gateway
    return db.query(Gateway).filter(Gateway.id == gateway_id).first()


def _to_dict(g) -> dict:
    return {
        "id": g.id, "cloud": g.cloud, "region": g.region, "zone": g.zone,
        "name": g.name, "status": g.status, "managed": bool(g.managed),
        "host_id": g.host_id, "egress_ip": g.egress_ip,
        "deploy_job_id": g.deploy_job_id, "error": g.error,
        "created_by": g.created_by, "created_at": g.created_at.isoformat() if g.created_at else None,
    }


def live_egress_ips(db, cloud: str) -> list[str]:
    """Egress IPs of every live gateway in ``cloud`` — the sources a node firewall must
    admit for a Web Jump to reach a source-restricted node.

    Every gateway host in a cloud joins the same PRA Gateway *cluster*, and PRA
    distributes sessions across its nodes: the broker for any given session may be ANY
    of them, so allowing one remembered IP is a coin flip. Rows for gateways that are
    gone are excluded so a torn-down gateway's /32 leaves the rule — including the
    `missing` ones the reconcile pass found gone without anything having recorded it,
    which is half of why that pass exists."""
    from ..database import Gateway
    rows = (db.query(Gateway)
              .filter(Gateway.cloud == cloud, Gateway.status.notin_(GONE_STATUSES),
                      Gateway.egress_ip.isnot(None)).all())
    return sorted({(r.egress_ip or "").strip() for r in rows if (r.egress_ip or "").strip()})


def record_egress_ip(db, cloud: str, name: str, egress_ip: str) -> None:
    """Store a gateway's egress IP on its registry row (best-effort).

    Called from the ensure paths for both kinds of gateway, because the IP is
    ephemeral: a reclaim/recreate changes it, and the firewall reads these rows."""
    from ..database import Gateway
    if not egress_ip:
        return
    try:
        row = (db.query(Gateway)
                 .filter(Gateway.cloud == cloud, Gateway.name == name,
                         Gateway.status != "deleted").first())
        if row is None or (row.egress_ip or "") == egress_ip:
            return
        row.egress_ip = egress_ip
        db.commit()
        logger.info("gateway registry: %s gateway %s egress IP = %s", cloud, name, egress_ip)
    except Exception as exc:  # noqa: BLE001
        logger.warning("gateway registry: recording egress IP for %s failed (non-fatal): %s",
                       name, exc)
        db.rollback()


def mark_deleted(db, cloud: str, name: str) -> None:
    """Mark a gateway row deleted after its host is gone (best-effort).

    The idle teardown deletes the managed host directly, so without this the Gateways
    page keeps showing a gateway that no longer exists — and its stale /32 stays in
    every node firewall."""
    from ..database import Gateway
    try:
        row = (db.query(Gateway)
                 .filter(Gateway.cloud == cloud, Gateway.name == name,
                         Gateway.status != "deleted").first())
        if row is None:
            return
        row.status = "deleted"
        row.egress_ip = None
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("gateway registry: marking %s deleted failed (non-fatal): %s", name, exc)
        db.rollback()


def existing_names(db, cloud: str) -> list[str]:
    """Names already taken in ``cloud`` — registry rows plus the managed name, which
    may have no row yet if the auto-ensure has never run.

    A ``missing`` gateway keeps its name reserved even though its host is gone. The name
    is unique per ``(cloud, name)`` in the registry, not just in the cloud: freeing it
    would let a redeploy add a second row under the same name, and the lookups in
    :func:`record_egress_ip` / :func:`mark_deleted` take the first match."""
    from ..database import Gateway
    from . import jumpoint_host_service
    names = [g.name for g in db.query(Gateway)
             .filter(Gateway.cloud == cloud, Gateway.status != "deleted").all()]
    try:
        names.append(jumpoint_host_service.managed_host_name(cloud))
    except Exception:  # noqa: BLE001 — config unreadable; the registry names still apply
        pass
    return names


def adopt_managed(db, cloud: str, region: str, name: str, host_id: str = "",
                  egress_ip: str = "") -> None:
    """Record the auto-ensured gateway, creating the row once and refreshing it after.

    Called from the ensure path, so a deployment that has been running the shared
    gateway for months shows up on the page the first time anything ensures it rather
    than appearing to have none. Best-effort by contract: the ensure path is
    best-effort for its callers, and failing to write an inventory row must not be
    what breaks a deploy."""
    from ..database import Gateway
    try:
        row = (db.query(Gateway)
                 .filter(Gateway.cloud == cloud, Gateway.name == name).first())
        if row is None:
            row = Gateway(id=str(uuid.uuid4()), cloud=cloud, region=region, name=name,
                          managed=True, created_by="system")
            db.add(row)
        row.status = "running"
        row.region = region or row.region
        row.host_id = host_id or row.host_id
        row.egress_ip = egress_ip or row.egress_ip
        row.error = None
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("gateway registry: adopting managed gateway %s failed (non-fatal): %s",
                       name, exc)
        db.rollback()


# ── reconciliation ────────────────────────────────────────────────────────────

async def reconcile(db, cloud: str = "") -> None:
    """Bring registry statuses back in line with what is actually in the cloud.

    Every other writer in this module records what the dashboard *did*. Nothing recorded
    what is *there*, and several things remove a gateway host without going through the
    ensure/teardown pair that owns the row: the Containers tab's own Stop button, an idle
    teardown that found the host already gone, a deletion in the cloud console, a
    capacity reaper. The row then reads ``running`` forever — next to a Containers tab
    correctly showing nothing — and its dead /32 stays in every node firewall.

    Two outcomes, matching the two lifecycles this module is built around:

      * a **managed** gateway whose host is gone becomes ``deleted``. That is what its
        idle teardown would have written; coming and going as resources need it is the
        normal life of that host, not an anomaly to report. The next ensure re-adopts
        the row.
      * a **requested** gateway whose host is gone becomes ``missing``. Nobody asked for
        it to go away, so the row stays on the page with the reason on it.

    A lookup that FAILS leaves every row in that cloud exactly as it was. That property
    is what the whole function rests on: ``missing`` has to mean "we looked and it wasn't
    there", never "we couldn't tell".
    """
    from ..database import Gateway
    q = db.query(Gateway).filter(Gateway.status.in_(VERIFIABLE_STATUSES))
    if cloud:
        q = q.filter(Gateway.cloud == cloud)
    rows = q.all()
    if not rows:
        return

    by_cloud: dict[str, list] = {}
    for row in rows:
        by_cloud.setdefault(row.cloud, []).append(row)
    items = list(by_cloud.items())

    # Per cloud concurrently, but bounded: this runs on a page load, and the cloud SDKs
    # are sync calls on a small thread pool. A cloud that has gone slow must cost the
    # Gateways tab a stale reading, not the request.
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(_live_hosts(c, rs) for c, rs in items),
                           return_exceptions=True),
            timeout=_RECONCILE_TIMEOUT_S)
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("gateway reconcile: timed out after %ds — statuses left as they are",
                       _RECONCILE_TIMEOUT_S)
        return

    changed = False
    for (cloud_name, cloud_rows), live in zip(items, results):
        if isinstance(live, BaseException):
            logger.warning("gateway reconcile: the %s lookup failed — leaving %d row(s) "
                           "as they are: %s", cloud_name, len(cloud_rows), live)
            continue
        for row in cloud_rows:
            changed = _apply_live(row, live.get(row.name)) or changed
    if changed:
        db.commit()


async def _live_hosts(cloud: str, rows: list) -> dict:
    """``{name: info}`` for the gateways in ``rows`` that exist, with AWS rows annotated
    by whether the gateway task is actually running on the host.

    The annotation is asked per REGION, not per gateway: the ECS calls behind it are
    cluster-wide, so one round trip answers for every gateway sharing that cluster."""
    from . import jumpoint_host_service
    live = await jumpoint_host_service.live_gateway_hosts(
        cloud, [(r.region or "", r.name) for r in rows])
    if cloud != "aws":
        return live

    by_region: dict[str, list] = {}
    for row in rows:
        if row.name in live:
            by_region.setdefault(row.region or "", []).append(row)
    for region, region_rows in by_region.items():
        serving = await jumpoint_host_service.gateway_tasks_by_host(
            region, [live[r.name]["host_id"] for r in region_rows])
        if serving is None:
            continue    # unanswerable for this region; leave `serving` unset
        for row in region_rows:
            live[row.name]["serving"] = serving.get(live[row.name]["host_id"])
    return live


_MISSING_REASON = (
    "The host for this gateway is no longer in the cloud, and nothing asked for it to be "
    "removed. The row was kept so the gap is visible — remove it here, or deploy a "
    "replacement to take over its share of the session load.")

_DEGRADED_REASON = (
    "The host is up but no gateway task is running on it, so this gateway cannot broker a "
    "session. The Containers tab's ECS Tasks list shows the same thing. The next deploy "
    "that needs this gateway starts the task.")


def _apply_live(row, info: Optional[dict]) -> bool:
    """Reconcile one row against its live host (``None`` when it has none). Returns
    whether the row changed, so the caller can commit once for the whole pass."""
    if info is None:
        want = "deleted" if row.managed else "missing"
        reason = None if row.managed else _MISSING_REASON
        if row.status == want:
            return False
        logger.info("gateway reconcile: %s gateway %s has no host in the cloud — %s",
                    row.cloud, row.name, want)
        row.status = want
        # The address outlived the host it described; drop it so the node firewalls stop
        # re-applying a /32 that admits nothing.
        row.egress_ip = None
        row.error = reason
        return True

    # `serving` is None on the clouds (and configurations) where it can't be determined,
    # and unknown must never downgrade a row — only a definite No does.
    want = "degraded" if info.get("serving") is False else "running"
    reason = _DEGRADED_REASON if want == "degraded" else None

    # A recreated gateway host keeps its NAME and takes a FRESH address, so the row's
    # remembered /32 can point at a host that is gone while this one goes unallowed. Only
    # overwrite when we actually learned an address: a cloud that reports none (an Azure
    # gateway has no public IP) must not blank a good value.
    ip = info.get("egress_ip") or ""
    ip_changed = bool(ip) and ip != (row.egress_ip or "")
    if ip_changed:
        logger.info("gateway reconcile: %s gateway %s egress IP %s → %s",
                    row.cloud, row.name, row.egress_ip, ip)
        row.egress_ip = ip

    if row.status == want and (row.error or None) == reason and not ip_changed:
        return False
    if row.status != want:
        logger.info("gateway reconcile: %s gateway %s is %s", row.cloud, row.name, want)
    row.status = want
    row.error = reason
    row.host_id = info.get("host_id") or row.host_id
    return True


# ── naming ────────────────────────────────────────────────────────────────────

def check_name(cloud: str, name: str, taken) -> str:
    """The naming rules, with the taken-name list passed in.

    Pure on purpose — no DB, no cloud — because these rules are the part worth unit
    testing and the part a mistake in is expensive: the name is what keeps the managed
    and requested hosts apart *in the cloud*, since the managed teardown finds its host
    by name tag. A requested gateway wearing the managed name is one an idle teardown
    would terminate.

    Cloud rules come from ``vm_naming``, which already encodes them: RFC1035 for GCP,
    and 15 characters for Azure because ``_run_vm_jumpoint_sync`` derives the in-guest
    hostname as ``name[:15]`` — two gateways differing only past that point would share
    a hostname."""
    from . import vm_naming
    name = (name or "").strip()
    if not name:
        raise GatewayError("A gateway name is required.")
    try:
        name = vm_naming.validate_base(name, cloud)
        limit = vm_naming.name_limit(cloud)
    except ValueError as exc:
        raise GatewayError(str(exc)) from exc
    if len(name) > limit:
        raise GatewayError(
            f"{cloud.upper()} gateway names must be {limit} characters or fewer — "
            f"{name!r} is {len(name)}.")
    if name.lower() in {str(n).lower() for n in taken}:
        raise GatewayError(
            f"A gateway named {name!r} already exists in {cloud}. Gateway names are "
            "how the managed and requested hosts stay apart in the cloud, so they "
            "must be unique.")
    return name


def next_free_name(cloud: str, region: str, taken) -> str:
    """A free, cloud-legal default. Pure, like :func:`check_name`."""
    from . import vm_naming
    lowered = {str(n).lower() for n in taken}
    base = f"gw-{(region or '').replace('_', '-')}".rstrip("-") or "gw"
    limit = vm_naming.name_limit(cloud)
    for n in range(1, 100):
        suffix = f"-{n:02d}"
        candidate = f"{base[:limit - len(suffix)]}{suffix}".strip("-").lower()
        if candidate not in lowered:
            return candidate
    raise GatewayError(f"Could not find a free gateway name in {cloud}/{region}.")


def validate_name(db, cloud: str, name: str) -> str:
    """:func:`check_name` against the names currently taken in ``cloud``."""
    return check_name(cloud, name, existing_names(db, cloud))


def suggest_name(db, cloud: str, region: str) -> str:
    """:func:`next_free_name` against the names currently taken in ``cloud``."""
    return next_free_name(cloud, region, existing_names(db, cloud))


# ── job-runner entry point ────────────────────────────────────────────────────

async def run(db, *, job_id: str, meta: dict) -> None:
    """Dispatch one gateway job. Arguments come from the metadata the endpoint
    persisted — the runner has no request object."""
    from . import job_service
    job_type = meta.get("job_type") or ""
    try:
        if job_type == "gateway_teardown":
            await _run_teardown(db, job_id, meta)
        else:
            await _run_deploy(db, job_id, meta)
    except Exception as exc:  # noqa: BLE001 — the job owns its own failure
        logger.exception("gateway job %s failed", job_id)
        _mark_error(db, meta.get("gateway_id"), str(exc))
        job_service.set_failed(db, job_id, str(exc))


def _mark_error(db, gateway_id: Optional[str], message: str) -> None:
    if not gateway_id:
        return
    row = get_gateway(db, gateway_id)
    if row is not None:
        row.status = "error"
        row.error = message[:2000]
        db.commit()


async def _record_partial_host(db, gateway_id: Optional[str], cloud: str, region: str,
                               name: str) -> None:
    """Point the registry row at a host a failed deploy already created, so the operator
    can remove it from the Gateways page instead of it becoming a cost nobody can see.

    Only fills a blank ``host_id`` — a row that already names its host knows better than
    a lookup by name does. Best-effort throughout: an inventory write must never be what
    replaces the real failure, so this swallows everything and lets the caller re-raise
    the original exception."""
    if not gateway_id:
        return
    try:
        from . import jumpoint_host_service
        host_id = await jumpoint_host_service.find_gateway_host_id(cloud, region, name)
        if not host_id:
            return
        row = get_gateway(db, gateway_id)
        if row is not None and not row.host_id:
            row.host_id = host_id
            db.commit()
            logger.warning("gateway %s failed but left host %s behind — recorded it on the "
                           "registry row so it can be removed", name, host_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("gateway: recording the partial host for %s failed "
                       "(non-fatal): %s", name, exc)


async def _run_deploy(db, job_id: str, meta: dict) -> None:
    from . import job_service, jumpoint_host_service
    cloud, region = meta["cloud"], meta["region"]
    name, zone = meta["name"], meta.get("zone") or ""
    gateway_id = meta.get("gateway_id")

    job_service.set_running(db, job_id)
    job_service.update_progress(db, job_id, 10, f"Launching {cloud} gateway {name}…")

    # Passing a name is what marks this as a requested gateway: ensure_jumpoint_host
    # then skips the managed-adoption write, because the row below is already ours.
    # ``placement`` comes back filled with where the host actually landed.
    placement: dict = {}
    try:
        if cloud == "gcp" and zone:
            host_id = await jumpoint_host_service._ensure_jumpoint_host_gcp(
                region, name, zone, placement=placement)
        else:
            host_id = await jumpoint_host_service.ensure_jumpoint_host(
                cloud, region, name, placement=placement)

        if not host_id:
            # The ensure paths are best-effort and return None when a prerequisite (deploy
            # key, project, subnet) is missing — they log the specific one.
            raise GatewayError(
                f"The {cloud} gateway could not be started. Check that the {cloud} deploy "
                "key and gateway subnet are configured in Settings → BeyondTrust; the job "
                "log names the missing value.")
    except Exception:
        # A failed ensure can still have built the host: the AWS path launches the EC2
        # instance and only then waits for its ECS agent to register, so anything raised
        # from there on leaves a host running. Record it before the job fails — otherwise
        # the row keeps host_id empty, the Gateways page shows an errored gateway with
        # nothing to remove, and the only thing that can still find the host is the
        # Name-tag lookup inside teardown_gateway.
        await _record_partial_host(db, gateway_id, cloud, region, name)
        raise

    row = get_gateway(db, gateway_id) if gateway_id else None
    if row is not None:
        row.status = "running"
        row.host_id = host_id
        row.error = None
        # Record where it ACTUALLY landed: the launcher resolves a blank zone, and may
        # fall through to a sibling zone on a capacity error, so the requested zone is
        # not necessarily the real one — and teardown deletes by (zone, name).
        row.zone = placement.get("zone") or row.zone
        row.egress_ip = placement.get("egress_ip") or row.egress_ip
        db.commit()

    # A new gateway is a new source address in front of every source-restricted node.
    # Re-applying here is what closes the gap: the ensure path records the IP, but
    # nothing was re-applying the rules, so a Web Jump brokered by this gateway hit a
    # firewall that had never heard of it.
    job_service.update_progress(db, job_id, 90, "Updating node firewalls")
    await refresh_node_firewalls(db, f"gateway {name} deploy")

    job_service.update_progress(db, job_id, 100, f"Gateway {name} is up ({host_id}).")
    job_service.set_completed(db, job_id, {"host_id": host_id, "name": name})


async def refresh_node_firewalls(db, why: str) -> None:
    """Re-apply the Rancher + Portainer node firewalls after the gateway set changes.

    Both nodes are source-restricted and brokered through a gateway, so their allow
    lists are a function of which gateways exist. Best-effort and no-op safe (each
    refresh returns early when its node/cloud isn't configured) — a firewall refresh
    must never be what fails a gateway job."""
    from . import portainer_node_service, rancher_node_service
    for label, refresh in (("rancher", rancher_node_service.refresh_rancher_firewall),
                           ("portainer", portainer_node_service.refresh_portainer_firewall)):
        try:
            await refresh(db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("gateway: %s firewall refresh after %s failed (non-fatal): %s",
                           label, why, exc)


async def _run_teardown(db, job_id: str, meta: dict) -> None:
    from . import job_service, jumpoint_host_service
    cloud, region = meta["cloud"], meta["region"]
    name, zone = meta["name"], meta.get("zone") or ""
    gateway_id = meta.get("gateway_id")

    job_service.set_running(db, job_id)
    job_service.update_progress(db, job_id, 10, f"Removing {cloud} gateway {name}…")

    await jumpoint_host_service.teardown_gateway(cloud, region, name, zone)

    row = get_gateway(db, gateway_id) if gateway_id else None
    if row is not None:
        row.status = "deleted"
        row.egress_ip = None
        db.commit()

    # Drop the departed gateway's /32 from the node firewalls — the row is deleted
    # above, so the recomputed set no longer contains it.
    job_service.update_progress(db, job_id, 90, "Updating node firewalls")
    await refresh_node_firewalls(db, f"gateway {name} teardown")

    job_service.update_progress(db, job_id, 100, f"Gateway {name} removed.")
    job_service.set_completed(db, job_id, {"name": name})
