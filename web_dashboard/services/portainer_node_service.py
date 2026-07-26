"""Portainer CE server orchestrator (GCE COS).

Owns the deploy/teardown JOB lifecycle for the managed Portainer server that runs
as a single container on a public (source-restricted) GCE COS VM. Keeps
``gcp_service`` pure-GCE and ``portainer_service`` a pure API client; this module
glues them to config + the job queue (mirrors ``rancher_node_service``, which is
the model for this whole layer). Dispatched from ``jobs_worker``
(``portainer_node_deploy`` / ``portainer_node_teardown``) — long ops (VM boot +
first-run bootstrap poll) that the durable worker's heartbeat protects.

A successful deploy writes ``portainer_url`` / ``portainer_pat`` /
``portainer_verify_ssl`` into config, which is exactly what the existing
Portainer integration reads — so the Containers page starts working against the
new server with no manual Settings step.

The node is EPHEMERAL: an ephemeral external IP + auto-delete boot disk. A
teardown (or stop/recreate) wipes ``/var/lib/portainer``, so users, environments
and settings are lost and the node must re-bootstrap.
"""
import logging

import httpx

from . import (config_service, gcp_service, job_service, portainer_service,
               region_catalog, region_config)
# Reused verbatim from the Rancher orchestrator — same strength requirements
# (Portainer also enforces a 12-char minimum), so there is one implementation.
from .rancher_node_service import _generate_admin_password

logger = logging.getLogger(__name__)

# How long to wait for Portainer to start serving after the VM boots. Portainer is
# much lighter than Rancher, but COS still has to pull the image on a fresh VM.
_READY_TIMEOUT_S = 300

# Plain-HTTP echo endpoints used to learn the dashboard's own public egress IP.
# HTTP (not HTTPS) dodges corp TLS-inspection breakage; best-effort, first-wins.
_IP_ECHO_URLS = (
    "http://checkip.amazonaws.com",
    "http://api.ipify.org",
    "http://ifconfig.me/ip",
)

# The admin Portainer's first-run init creates.
_ADMIN_USERNAME = "admin"


def _firewall_name(node_name: str) -> str:
    return f"{node_name}-allow-mgmt"


def _node_params(region=None, zone=None) -> dict:
    """Resolve the node's deploy knobs, region-aware.

    ``region`` (the operator's pick, else derived from an explicit ``zone`` or the
    persisted node zone, else the configured default) selects the network / subnet /
    zone through :func:`region_config.resolve_region`. For the DEFAULT region this
    returns the flat ``gcp_*`` keys unchanged, so single-region installs behave
    exactly as before. The effective zone may be blank — the launcher then auto-picks
    a valid zone in the region and retries siblings on capacity exhaustion.

    Mirrors ``rancher_node_service._node_params``; only the config key prefix differs.
    """
    from ..config import settings

    default_region = region_catalog.normalize("gcp", region_catalog.default_region("gcp"))

    # Effective region: explicit pick → derived from an explicit zone → derived from
    # the persisted node zone (gcp_portainer_zone / gcp_zone) → configured default.
    if region:
        eff_region = region_catalog.normalize("gcp", region)
    elif zone:
        eff_region = region_catalog.region_from_zone(zone)
    else:
        persisted = config_service.get("gcp_portainer_zone") or config_service.get("gcp_zone")
        eff_region = (region_catalog.region_from_zone(persisted) if persisted
                      else default_region)

    rc = region_config.resolve_region("gcp", eff_region) or {}
    is_default = (eff_region == default_region)

    def _in_region(z) -> bool:
        z = (z or "").strip()
        return bool(z) and region_catalog.region_from_zone(z) == eff_region

    # Effective zone precedence: explicit request zone (kept only if in-region) →
    # for a region pick: the region's configured zone (only if in-region) →
    # for a bare redeploy: the persisted node zone, else the region-config zone →
    # "" so the launcher auto-picks the region's first available zone.
    eff_zone = ""
    if zone and _in_region(zone):
        eff_zone = region_catalog.normalize("gcp", zone)
    elif region:
        if _in_region(rc.get("zone")):
            eff_zone = region_catalog.normalize("gcp", rc.get("zone"))
    else:
        for cand in (config_service.get("gcp_portainer_zone"), rc.get("zone")):
            if _in_region(cand):
                eff_zone = region_catalog.normalize("gcp", cand)
                break

    # Network is a global VPC name (region-agnostic); the SUBNET is regional. For a
    # non-default region take the subnet from the region entry only — never fall back
    # to the default region's flat subnet name (it wouldn't exist in this region).
    network = rc.get("network") or settings.gcp_network or "default"
    if is_default:
        subnetwork = rc.get("jumpoint_subnetwork") or rc.get("subnetwork") or ""
    else:
        entry = region_config.load_region_configs("gcp").get(eff_region, {})
        subnetwork = (str(entry.get("jumpoint_subnetwork") or "").strip()
                      or str(entry.get("subnetwork") or "").strip()
                      or rc.get("jumpoint_subnetwork") or rc.get("subnetwork") or "")

    try:
        boot_disk_gb = int(config_service.get("gcp_portainer_boot_disk_gb")
                           or settings.gcp_portainer_boot_disk_gb)
    except (TypeError, ValueError):
        boot_disk_gb = settings.gcp_portainer_boot_disk_gb
    return {
        "project_id":   config_service.get("gcp_project_id") or settings.gcp_project_id,
        "region":       eff_region,
        "zone":         eff_zone,
        "name":         config_service.get("gcp_portainer_name") or settings.gcp_portainer_name,
        "image":        config_service.get("gcp_portainer_image") or settings.gcp_portainer_image,
        "machine_type": (config_service.get("gcp_portainer_machine_type")
                         or settings.gcp_portainer_machine_type),
        "boot_disk_gb": boot_disk_gb,
        "network":      network,
        # gcp_service normalizes a bare subnet name into a regional self-link using
        # the launch zone's region, so a region-correct bare name is all we need.
        "subnetwork":   subnetwork,
        "network_tag":  (config_service.get("gcp_portainer_network_tag")
                         or settings.gcp_portainer_network_tag),
    }


def _allowed_cidrs() -> list[str]:
    """MANUAL firewall source ranges (CSV), fail-closed. Empty CSV → [] unless allow_open.

    Deliberately quiet: the CSV is only one input to the merged set (the dashboard's
    own egress CIDR joins it), so an empty CSV does NOT necessarily mean the firewall
    stays closed. The applied-outcome warnings live in
    :func:`refresh_portainer_firewall`, which sees the FINAL set."""
    csv = config_service.get("portainer_allowed_source_cidrs") or ""
    cidrs = [c.strip() for c in csv.split(",") if c.strip()]
    if not cidrs:
        if config_service.get_bool("gcp_portainer_allow_open", False):
            return ["0.0.0.0/0"]
        logger.debug("portainer_allowed_source_cidrs is empty — relying on the "
                     "auto-detected dashboard egress CIDR")
    return cidrs


def _dashboard_cidr() -> list[str]:
    """/32 for the DASHBOARD's own public egress IP.

    The worker bootstraps and polls the node over its PUBLIC IP, so this is the
    source address that hits the node's source-restricted firewall — without it a
    (re)deploy can't reach its own node and the readiness poll times out. Sourced
    from ``portainer_dashboard_egress_cidr`` (auto-detected + persisted on deploy, or
    set manually); a bare IP is normalized to ``/32``."""
    val = (config_service.get("portainer_dashboard_egress_cidr") or "").strip()
    if not val:
        return []
    return [val if "/" in val else f"{val}/32"]


def _ready_timeout_s() -> int:
    """Readiness poll budget (config ``portainer_ready_timeout_s``, default 300s)."""
    from ..config import settings
    try:
        return int(config_service.get("portainer_ready_timeout_s")
                   or settings.portainer_ready_timeout_s)
    except (TypeError, ValueError):
        return _READY_TIMEOUT_S


async def _detect_egress_ip() -> str:
    """Best-effort: learn the worker's own public egress IP via a plain-HTTP echo.

    Plain HTTP (not HTTPS) avoids corp TLS-inspection breakage; ``trust_env`` honors
    proxy env vars. Returns a bare IPv4 string, or ``""`` on any failure."""
    import ipaddress
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=True, follow_redirects=True) as c:
            for url in _IP_ECHO_URLS:
                try:
                    r = await c.get(url)
                    ip = (r.text or "").strip()
                    ipaddress.ip_address(ip)  # validate; raises on junk/HTML
                    return ip
                except Exception:
                    continue
    except Exception as exc:  # client construction / proxy env issues
        logger.warning("Portainer egress-IP detection failed (continuing): %s", exc)
    return ""


async def _ensure_dashboard_egress_cidr() -> str:
    """Refresh + persist the dashboard's own egress CIDR so the firewall admits the
    worker. An operator-set CIDR that already CONTAINS the detected IP is kept as-is:
    corp proxies egress from a per-connection POOL, so pinning whichever /32 detection
    saw this time would still drop the next connection. On detection failure any
    operator-set value is left intact. Returns the CIDR now in effect."""
    import ipaddress
    ip = await _detect_egress_ip()
    existing = (config_service.get("portainer_dashboard_egress_cidr") or "").strip()
    if ip:
        if existing:
            try:
                net = ipaddress.ip_network(existing if "/" in existing else f"{existing}/32",
                                           strict=False)
                if ipaddress.ip_address(ip) in net:
                    return existing  # detected IP already covered — keep the broader pin
            except ValueError:
                pass  # malformed stored value — fall through and replace it
        cidr = f"{ip}/32"
        if existing != cidr:
            config_service.set("portainer_dashboard_egress_cidr", cidr)
            logger.info("Portainer firewall: dashboard egress IP detected as %s", cidr)
        return cidr
    if not existing:
        logger.warning("Portainer firewall: could not auto-detect the dashboard's public egress "
                       "IP and portainer_dashboard_egress_cidr is unset — the worker may be "
                       "unable to reach the node. Set it manually in Settings → Containers.")
    return existing


async def refresh_portainer_firewall(db=None) -> dict:
    """Recompute the node's firewall source set and re-apply it idempotently.

    The merged set is the manual CSV (``_allowed_cidrs``) plus the dashboard's own
    egress /32. Fail-closed and idempotent behavior is inherited from
    ``gcp_service.ensure_portainer_firewall`` (empty set → rule deleted; ``0.0.0.0/0``
    from allow_open dedupes harmlessly). No-op safe: returns early when no GCP project
    is configured, so callers can fire it best-effort even when no node is deployed.

    ``db`` is accepted (and unused) to match ``refresh_rancher_firewall``'s signature —
    Portainer has no per-cluster egress IPs to fold in."""
    p = _node_params()
    if not p["project_id"]:
        return {"skipped": "no gcp project configured"}
    merged = sorted(set(_allowed_cidrs()) | set(_dashboard_cidr()))
    if not merged:
        logger.warning("Portainer node has NO allowed source CIDRs — firewall stays closed "
                       "(node unreachable). Set portainer_allowed_source_cidrs in Settings.")
    elif "0.0.0.0/0" in merged:
        logger.warning("Portainer node firewall opening 0.0.0.0/0 — node reachable from "
                       "anywhere (gcp_portainer_allow_open or a manual CSV entry)")
    return await gcp_service.ensure_portainer_firewall(
        p["project_id"], p["network"], p["network_tag"], merged, _firewall_name(p["name"]))


def firewall_status(db=None) -> dict:
    """Read-only breakdown of the node's firewall source set (no GCP call) — what
    :func:`refresh_portainer_firewall` would apply, so the operator can see exactly
    which sources are allowed and why."""
    dash = _dashboard_cidr()
    merged = sorted(set(_allowed_cidrs()) | set(dash))
    csv = config_service.get("portainer_allowed_source_cidrs") or ""
    return {
        "manual_cidrs": [c.strip() for c in csv.split(",") if c.strip()],
        "dashboard_egress_ip": dash[0] if dash else "",
        "merged": merged,
        "allow_open": config_service.get_bool("gcp_portainer_allow_open", False),
        "opened": bool(merged),
        "ports": ["9443", "8000"],
    }


async def _bootstrap(db, job_id: str, url: str) -> tuple[str, str]:
    """First-run the node: create the admin, then mint an API token.

    Returns ``(pat, note)``. ``note`` is a human-readable caveat for the job result
    (empty on the clean path). A node whose init window has closed yields
    ``("", note)`` rather than raising — the VM is up and usable, the operator just
    has to supply a PAT by hand."""
    password = config_service.get("portainer_admin_password")
    generated = False
    if not password:
        password = _generate_admin_password()
        generated = True

    job_service.update_progress(db, job_id, 75, "Creating the admin user")
    try:
        await portainer_service.init_admin(url, _ADMIN_USERNAME, password)
    except portainer_service.PortainerAlreadyInitialized as exc:
        logger.warning("Portainer first-run init unavailable: %s", exc)
        return "", ("The node already had an admin user, so no API token could be minted "
                    "automatically. Add a Portainer API token in Settings → Containers.")

    # Only persist the password once init actually succeeded, so a failed deploy
    # doesn't leave a credential that doesn't open anything.
    config_service.set("portainer_admin_password", password)
    config_service.set("portainer_admin_password_generated", "1" if generated else "0")

    job_service.update_progress(db, job_id, 85, "Minting an API token")
    jwt = await portainer_service.login(url, _ADMIN_USERNAME, password)
    pat = await portainer_service.create_access_token(url, jwt, password)
    return pat, ""


async def run_deploy(db, *, job_id: str, meta: dict) -> None:
    """Deploy (or reuse) the Portainer node: firewall → COS VM → wait ready →
    first-run admin → mint token → wire the integration config."""
    try:
        job_service.set_running(db, job_id)
        # Deploy-time region/zone pick (blank → the persisted node region, else the
        # configured default). Selects the node's region-specific subnet/zone.
        p = _node_params(region=meta.get("region"), zone=meta.get("zone"))
        if not p["project_id"]:
            job_service.set_failed(db, job_id, "GCP project is not configured.")
            return

        job_service.update_progress(db, job_id, 10, "Configuring firewall")
        # Learn (best-effort) + persist the dashboard's OWN public egress IP first:
        # the worker bootstraps + polls the node over its public IP, so that source
        # must be in the firewall or the deploy can't reach its own node.
        await _ensure_dashboard_egress_cidr()
        fw = await refresh_portainer_firewall(db)
        if not fw.get("opened"):
            # Fail closed AND fast: polling a node no source can reach just burns the
            # readiness timeout and reports a misleading "not ready".
            job_service.set_failed(
                db, job_id,
                "The Portainer node's firewall is closed — no allowed source CIDRs, and the "
                "dashboard couldn't auto-detect its own public egress IP to open it. Set "
                "portainer_dashboard_egress_cidr (the dashboard's egress IP) or "
                "portainer_allowed_source_cidrs in Settings → Containers — or enable "
                "gcp_portainer_allow_open — then redeploy.")
            return

        # Single relocatable node. If a node already lives in the TARGET region, reuse
        # that exact zone (the launcher starts/returns it). If one lives in a DIFFERENT
        # region, delete it first so we never strand a duplicate there (the node is
        # ephemeral — state re-bootstraps).
        target_region = p["region"]
        try:
            existing_nodes = await gcp_service.list_gce_portainer(p["project_id"])
        except Exception as exc:
            logger.warning("Portainer relocation check failed (continuing): %s", exc)
            existing_nodes = []
        for node in existing_nodes:
            nzone = node.get("zone") or ""
            if region_catalog.region_from_zone(nzone) == target_region:
                p["zone"] = nzone   # reuse the live in-region node's exact zone
            else:
                logger.info("Relocating Portainer: deleting node '%s' in %s → region %s",
                            node.get("name"), nzone, target_region)
                job_service.update_progress(db, job_id, 25, f"Relocating to {target_region}")
                try:
                    await gcp_service.stop_gce_portainer(
                        p["project_id"], nzone, node.get("name") or p["name"])
                except Exception as exc:
                    logger.warning("Failed to delete old-region Portainer node (continuing): %s", exc)

        job_service.update_progress(db, job_id, 30, "Launching COS VM")
        res = await gcp_service.run_gce_portainer(
            p["project_id"], p["zone"], p["name"], p["image"],
            network=p["network"], subnetwork=p["subnetwork"],
            machine_type=p["machine_type"], boot_disk_gb=p["boot_disk_gb"],
            network_tag=p["network_tag"], create_external_ip=True, region=p["region"])
        external_ip = res.get("external_ip") or ""
        url = res.get("url") or ""
        if not external_ip:
            job_service.set_failed(db, job_id, "Portainer VM has no external IP — cannot reach it.")
            return

        # Persist the ACTUAL deployed zone so teardown + bare redeploys stay sticky to
        # the (possibly relocated / auto-picked) region.
        deployed_zone = res.get("zone") or p["zone"]
        if deployed_zone:
            config_service.set("gcp_portainer_zone", deployed_zone)
        # portainer_url is the key the EXISTING integration reads, so writing it here
        # is what makes the Containers page work with no manual Settings step. The node
        # serves a self-signed cert on :9443, so TLS verification must be off for it.
        config_service.set("portainer_url", url)
        config_service.set("portainer_verify_ssl", "0")

        job_service.update_progress(db, job_id, 55, "Waiting for Portainer to start")
        ready = await portainer_service.wait_ready(url, _ready_timeout_s())
        if not ready:
            job_service.set_failed(
                db, job_id,
                f"Portainer did not start serving at {url} within {_ready_timeout_s()}s. "
                f"The VM is running — check that the firewall admits the dashboard's egress "
                f"IP, then redeploy.")
            return

        note = ""
        existing_pat = config_service.get("portainer_pat")
        if res.get("reused") and existing_pat:
            # Node already bootstrapped and we still hold a token — nothing to mint.
            job_service.update_progress(db, job_id, 85, "Reusing the existing API token")
            pat = existing_pat
        else:
            pat, note = await _bootstrap(db, job_id, url)

        if pat:
            config_service.set("portainer_pat", pat)

        completion = {
            "url": url,
            "external_ip": external_ip,
            "internal_ip": res.get("internal_ip") or "",
            "zone": deployed_zone,
            "region": p["region"],
            "name": p["name"],
            "reused": bool(res.get("reused")),
            "token_configured": bool(pat),
        }
        if pat and config_service.get_bool("portainer_admin_password_generated", False):
            completion["admin_username"] = _ADMIN_USERNAME
            completion["admin_password"] = config_service.get("portainer_admin_password") or ""
        if note:
            completion["note"] = note
        job_service.set_completed(db, job_id, completion)
    except Exception as exc:
        logger.exception("Portainer node deploy failed")
        job_service.set_failed(db, job_id, str(exc))


async def run_teardown(db, *, job_id: str, meta: dict) -> None:
    """Delete the Portainer node VM + its firewall rule, then clear the runtime
    config it populated. The node is ephemeral, so this discards all Portainer
    state (users, environments, settings)."""
    try:
        job_service.set_running(db, job_id)
        p = _node_params()
        if not p["project_id"]:
            job_service.set_failed(db, job_id, "GCP project is not configured.")
            return

        name = meta.get("name") or p["name"]
        zone = meta.get("zone") or config_service.get("gcp_portainer_zone") or p["zone"]
        if not zone:
            job_service.set_failed(
                db, job_id,
                "No zone is known for the Portainer node — pass ?zone= to the stop call.")
            return

        job_service.update_progress(db, job_id, 30, f"Deleting {name} in {zone}")
        await gcp_service.stop_gce_portainer(
            p["project_id"], zone, name,
            delete_firewall=True, firewall_name=_firewall_name(p["name"]))

        job_service.update_progress(db, job_id, 80, "Clearing Portainer configuration")
        # Drop everything the deploy populated. The URL/PAT go too: leaving them would
        # point the Containers page at a VM that no longer exists.
        for key in ("portainer_url", "portainer_pat", "gcp_portainer_zone",
                    "portainer_admin_password", "portainer_admin_password_generated"):
            try:
                config_service.set(key, "")
            except Exception as exc:
                logger.warning("Failed to clear config key '%s' (continuing): %s", key, exc)

        job_service.set_completed(db, job_id, {"name": name, "zone": zone, "deleted": True})
    except Exception as exc:
        logger.exception("Portainer node teardown failed")
        job_service.set_failed(db, job_id, str(exc))
