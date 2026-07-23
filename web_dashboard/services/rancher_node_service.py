"""Rancher management-node orchestrator (GCE COS).

Owns the deploy/teardown JOB lifecycle for the central Rancher server that runs
as a single privileged container on a public (source-restricted) GCE COS VM.
Keeps ``gcp_service`` pure-GCE and ``rancher_service`` pure-API; this module
glues them to config + the job queue (mirrors how ``vdesktop_service`` owns its
own job lifecycle). Dispatched from ``jobs_worker`` (``rancher_node_deploy`` /
``rancher_node_teardown``) — long ops (VM boot + Rancher bootstrap poll) that the
durable worker's heartbeat protects.

The node is EPHEMERAL: an ephemeral external IP + auto-delete boot disk. A
stop/recreate reassigns the IP and wipes ``/var/lib/rancher``, so it must
re-bootstrap and downstream clusters must re-import.
"""
import asyncio
import logging

import httpx

from ..database import SessionLocal
from . import (config_service, gcp_service, job_service, rancher_service,
               region_catalog, region_config)

logger = logging.getLogger(__name__)

# How long to wait for Rancher to start serving after the VM boots.
_READY_TIMEOUT_S = 360
_READY_POLL_S = 10

# Plain-HTTP echo endpoints used to learn the dashboard's own public egress IP.
# HTTP (not HTTPS) dodges corp TLS-inspection breakage; best-effort, first-wins.
_IP_ECHO_URLS = (
    "http://checkip.amazonaws.com",
    "http://api.ipify.org",
    "http://ifconfig.me/ip",
)


def _firewall_name(node_name: str) -> str:
    return f"{node_name}-allow-mgmt"


def _generate_admin_password() -> str:
    """A strong admin UI password for Rancher first-run when the operator didn't
    set ``rancher_admin_password``. Rancher enforces ≥12 chars and forbids reusing
    the bootstrap password, so this is a fresh 24-char mix of upper/lower/digits/
    symbols. Persisted + surfaced (job result + login hint) so the operator can
    retrieve it."""
    import secrets
    import string
    symbols = "!@#%^*-_=+"
    alphabet = string.ascii_letters + string.digits + symbols
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(24))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in symbols for c in pw)):
            return pw


def _node_params(region=None, zone=None) -> dict:
    """Resolve the node's deploy knobs, region-aware.

    ``region`` (the operator's pick, else derived from an explicit ``zone`` or the
    persisted node zone, else the configured default) selects the network / subnet /
    zone through :func:`region_config.resolve_region`. For the DEFAULT region this
    returns the flat ``gcp_*`` keys unchanged, so single-region installs behave
    exactly as before. The effective zone may be blank — the launcher then auto-picks
    a valid zone in the region and retries siblings on capacity exhaustion.
    """
    from ..config import settings

    default_region = region_catalog.normalize("gcp", region_catalog.default_region("gcp"))

    # Effective region: explicit pick → derived from an explicit zone → derived from
    # the persisted node zone (gcp_rancher_zone / gcp_zone) → configured default.
    if region:
        eff_region = region_catalog.normalize("gcp", region)
    elif zone:
        eff_region = region_catalog.region_from_zone(zone)
    else:
        persisted = config_service.get("gcp_rancher_zone") or config_service.get("gcp_zone")
        eff_region = (region_catalog.region_from_zone(persisted) if persisted
                      else default_region)

    rc = region_config.resolve_region("gcp", eff_region) or {}
    is_default = (eff_region == default_region)

    def _in_region(z) -> bool:
        z = (z or "").strip()
        return bool(z) and region_catalog.region_from_zone(z) == eff_region

    # Effective zone precedence:
    #   explicit request zone (kept only if it sits in the region) →
    #   for a region pick: the region's configured zone (only if in-region — never
    #     inherit the default region's flat gcp_zone) →
    #   for a bare redeploy: the persisted node zone, else the region-config zone →
    #   "" so the launcher auto-picks the region's first available zone.
    eff_zone = ""
    if zone and _in_region(zone):
        eff_zone = region_catalog.normalize("gcp", zone)
    elif region:
        if _in_region(rc.get("zone")):
            eff_zone = region_catalog.normalize("gcp", rc.get("zone"))
    else:
        for cand in (config_service.get("gcp_rancher_zone"), rc.get("zone")):
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
        boot_disk_gb = int(config_service.get("gcp_rancher_boot_disk_gb") or settings.gcp_rancher_boot_disk_gb)
    except (TypeError, ValueError):
        boot_disk_gb = settings.gcp_rancher_boot_disk_gb
    return {
        "project_id":   config_service.get("gcp_project_id") or settings.gcp_project_id,
        "region":       eff_region,
        "zone":         eff_zone,
        "name":         config_service.get("gcp_rancher_name") or settings.gcp_rancher_name,
        "image":        config_service.get("gcp_rancher_image") or settings.gcp_rancher_image,
        "machine_type": config_service.get("gcp_rancher_machine_type") or settings.gcp_rancher_machine_type,
        "boot_disk_gb": boot_disk_gb,
        "network":      network,
        # gcp_service normalizes a bare subnet name into a regional self-link using
        # the launch zone's region, so a region-correct bare name is all we need.
        "subnetwork":   subnetwork,
        "network_tag":  config_service.get("gcp_rancher_network_tag") or settings.gcp_rancher_network_tag,
    }


def _allowed_cidrs() -> list[str]:
    """MANUAL firewall source ranges (CSV), fail-closed. Empty CSV → [] unless allow_open.

    Deliberately quiet: the CSV is only ONE input to the merged set (cluster
    egress /32s, Jumpoint /32, dashboard + runner CIDRs join it), so an empty
    CSV usually does NOT mean the firewall stays closed. The applied-outcome
    warnings live in :func:`refresh_rancher_firewall`, which sees the FINAL set.
    """
    csv = config_service.get("rancher_allowed_source_cidrs") or ""
    cidrs = [c.strip() for c in csv.split(",") if c.strip()]
    if not cidrs:
        if config_service.get_bool("gcp_rancher_allow_open", False):
            return ["0.0.0.0/0"]
        logger.debug("rancher_allowed_source_cidrs is empty — relying on auto-discovered sources")
    return cidrs


def _auto_cluster_cidrs(db) -> list[str]:
    """/32s for every dashboard-PROVISIONED cluster whose egress IP we captured.

    These are the clusters' NAT/outbound IPs — the source address their
    cattle-cluster-agent uses to dial out to this node — so they must be allowed
    for the import to reach ``Active``. Registered clusters have no ``egress_ip``.
    """
    from ..database import K8sCluster
    rows = db.query(K8sCluster).filter(K8sCluster.egress_ip.isnot(None)).all()
    return [f"{r.egress_ip.strip()}/32" for r in rows if (r.egress_ip or "").strip()]


def _jumpoint_cidr() -> list[str]:
    """/32 for the dashboard-managed Web-Jump Jumpoint host, when known + enabled.

    A PRA Web Jump reaches the node THROUGH a Jumpoint, so the source IP hitting
    the firewall is the Jumpoint host's egress IP (never the PRA appliance's).
    Only known when the dashboard provisioned that host (see jumpoint_host_service);
    empty for a pre-existing operator Jumpoint (add its IP to the CSV manually).
    """
    if not config_service.get_bool("rancher_ui_web_jump_enabled", False):
        return []
    ip = (config_service.get("rancher_ui_jumpoint_egress_ip") or "").strip()
    return [f"{ip}/32"] if ip else []


def _dashboard_cidr() -> list[str]:
    """/32 for the DASHBOARD's own public egress IP.

    The worker bootstraps and polls the node over its PUBLIC IP, so this is the
    source address that hits the node's source-restricted firewall — without it a
    (re)deploy can't reach its own node and the readiness poll times out. Sourced
    from ``rancher_dashboard_egress_cidr`` (auto-detected + persisted on deploy, or
    set manually); a bare IP is normalized to ``/32``.
    """
    val = (config_service.get("rancher_dashboard_egress_cidr") or "").strip()
    if not val:
        return []
    return [val if "/" in val else f"{val}/32"]


def _runner_cidr() -> list[str]:
    """Source range for the in-cloud API runner (``rancher_api_transport=runner``).

    The Cloud Run runner reaches the node's INTERNAL IP through the VPC connector,
    and GCE ingress firewalls apply to internal traffic too — so the connector's
    /28 (``rancher_runner_source_cidr``) must be admitted. Private RFC1918 range;
    adds no public exposure. Empty when the transport is direct."""
    if (config_service.get("rancher_api_transport") or "direct").strip().lower() != "runner":
        return []
    val = (config_service.get("rancher_runner_source_cidr") or "").strip()
    if not val:
        logger.warning("rancher_api_transport=runner but rancher_runner_source_cidr is unset — "
                       "the runner can only reach the node if the VPC's default internal-allow "
                       "rule covers the connector range. Set it in Settings → Kubernetes.")
        return []
    return [val if "/" in val else f"{val}/32"]


def _ready_timeout_s() -> int:
    """Readiness poll budget (config ``rancher_ready_timeout_s``, default 360s)."""
    from ..config import settings
    try:
        return int(config_service.get("rancher_ready_timeout_s") or settings.rancher_ready_timeout_s)
    except (TypeError, ValueError):
        return _READY_TIMEOUT_S


async def _detect_egress_ip() -> str:
    """Best-effort: learn the worker's own public egress IP via a plain-HTTP echo.

    Plain HTTP (not HTTPS) avoids corp TLS-inspection breakage; ``trust_env`` honors
    proxy env vars. Returns a bare IPv4 string, or ``""`` on any failure (no route,
    proxy block, malformed body) — the caller falls back to the operator-set value.
    """
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
        logger.warning("Rancher egress-IP detection failed (continuing): %s", exc)
    return ""


async def _ensure_dashboard_egress_cidr() -> str:
    """Refresh + persist the dashboard's own egress CIDR so the firewall admits the
    worker. Detection tracks a changed dynamic IP, but an operator-set CIDR that
    already CONTAINS the detected IP is kept as-is: corp proxies (Cloudflare WARP)
    egress from a per-connection POOL of IPs, so pinning whichever /32 detection saw
    this time would still drop the next connection — the operator sets the pool's
    CIDR once (e.g. ``104.28.182.0/24``) and detection must not clobber it. On
    detection failure any operator-set value is left intact. Returns the CIDR now
    in effect (``""`` if still unknown)."""
    import ipaddress
    ip = await _detect_egress_ip()
    existing = (config_service.get("rancher_dashboard_egress_cidr") or "").strip()
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
            config_service.set("rancher_dashboard_egress_cidr", cidr)
            logger.info("Rancher firewall: dashboard egress IP detected as %s", cidr)
        return cidr
    if not existing:
        logger.warning("Rancher firewall: could not auto-detect the dashboard's public egress IP "
                       "and rancher_dashboard_egress_cidr is unset — the worker may be unable to "
                       "reach the node. Set it manually in Settings → Kubernetes if the deploy fails.")
    return existing


async def refresh_rancher_firewall(db) -> dict:
    """Recompute the node's firewall source set and re-apply it idempotently.

    The merged set is the manual CSV (``_allowed_cidrs``) plus the auto-discovered
    dashboard-provisioned cluster egress /32s plus the dashboard-managed Web-Jump
    Jumpoint /32. Called from every lifecycle event that changes the set (node
    deploy, cluster provision/import/decommission, Web Jump enable). Fail-closed
    and idempotent behavior is inherited from ``gcp_service.ensure_rancher_firewall``
    (empty set → rule deleted; ``0.0.0.0/0`` from allow_open dedupes harmlessly).
    No-op safe: returns early when no GCP project is configured so callers can fire
    it best-effort even when the Rancher node isn't deployed.
    """
    p = _node_params()
    if not p["project_id"]:
        return {"skipped": "no gcp project configured"}
    merged = sorted(set(_allowed_cidrs()) | set(_auto_cluster_cidrs(db))
                    | set(_jumpoint_cidr()) | set(_dashboard_cidr()) | set(_runner_cidr()))
    # Warn on the FINAL merged set only — an empty manual CSV alone is normal
    # (auto-discovered sources usually populate the set on their own).
    if not merged:
        logger.warning("Rancher node has NO allowed source CIDRs — firewall stays closed (node unreachable). "
                       "Set rancher_allowed_source_cidrs in Settings, provision a cluster, or enable the Web Jump.")
    elif "0.0.0.0/0" in merged:
        logger.warning("Rancher node firewall opening 0.0.0.0/0 — node reachable from anywhere "
                       "(gcp_rancher_allow_open or a manual CSV entry)")
    return await gcp_service.ensure_rancher_firewall(
        p["project_id"], p["network"], p["network_tag"], merged, _firewall_name(p["name"]))


def firewall_status(db) -> dict:
    """Read-only breakdown of the node's firewall source set (no GCP call) — what
    :func:`refresh_rancher_firewall` would apply, plus the per-cluster egress IPs so
    the operator can see exactly which sources are allowed and why."""
    from ..database import K8sCluster
    rows = db.query(K8sCluster).filter(K8sCluster.egress_ip.isnot(None)).all()
    clusters = [{"name": r.name, "cloud": r.cloud, "ip": (r.egress_ip or "").strip()}
                for r in rows if (r.egress_ip or "").strip()]
    jump = _jumpoint_cidr()
    dash = _dashboard_cidr()
    runner = _runner_cidr()
    merged = sorted(set(_allowed_cidrs()) | set(_auto_cluster_cidrs(db))
                    | set(jump) | set(dash) | set(runner))
    csv = config_service.get("rancher_allowed_source_cidrs") or ""
    return {
        "manual_cidrs": [c.strip() for c in csv.split(",") if c.strip()],
        "cluster_egress_ips": clusters,
        "jumpoint_egress_ip": jump[0] if jump else "",
        "dashboard_egress_ip": dash[0] if dash else "",
        "runner_source_cidr": runner[0] if runner else "",
        "merged": merged,
        "allow_open": config_service.get_bool("gcp_rancher_allow_open", False),
        "opened": bool(merged),
    }


async def _wait_ready(url: str, timeout_s: int = _READY_TIMEOUT_S) -> str:
    """Poll the node until Rancher answers (it needs 1-3 min; expect early 5xx).

    Returns ``"ready"`` when HTTPS ``/ping`` answers. On budget exhaustion it
    probes plain-HTTP ``/ping`` (port 80, no certificates involved) once to
    DISCRIMINATE the failure: ``"tls_blocked"`` = the node is UP and serving but
    the HTTPS handshake never completes — the classic corp TLS-inspection
    signature (an inspecting proxy, e.g. Cloudflare Gateway, rejects the node's
    self-signed cert at ITS origin-side verification, which ``verify=False``
    cannot bypass); ``"timeout"`` = nothing answered at all (container still
    initialising, or the firewall doesn't admit this worker's egress).
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    async with httpx.AsyncClient(verify=False, timeout=10.0) as c:
        while asyncio.get_event_loop().time() < deadline:
            try:
                r = await c.get(f"{url}/ping")
                if r.status_code < 500:
                    return "ready"
            except httpx.HTTPError:
                pass
            await asyncio.sleep(_READY_POLL_S)
    # HTTPS never made it — is the node actually up? /ping carries no secrets,
    # so a plain-HTTP probe is safe and passes TLS inspection untouched.
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{url.replace('https://', 'http://', 1)}/ping")
            if r.status_code == 200:
                return "tls_blocked"
    except httpx.HTTPError:
        pass
    return "timeout"


async def run_deploy(db, *, job_id: str, meta: dict) -> None:
    """Deploy (or reuse) the Rancher node: firewall → COS VM → pin server-url →
    wait ready → bootstrap → mint token → best-effort Entitle register."""
    try:
        job_service.set_running(db, job_id)
        # Deploy-time region/zone pick (blank → the persisted node region, else the
        # configured default). Selects the node's region-specific subnet/zone.
        p = _node_params(region=meta.get("region"), zone=meta.get("zone"))
        if not p["project_id"]:
            job_service.set_failed(db, job_id, "GCP project is not configured.")
            return
        bootstrap_password = config_service.get("rancher_bootstrap_password")
        if not bootstrap_password:
            job_service.set_failed(db, job_id, "rancher_bootstrap_password is not set (Settings → Kubernetes).")
            return

        # Persist the deploy-form PRA choices to config FIRST, so the firewall
        # merge (_jumpoint_cidr gates on rancher_ui_web_jump_enabled) and the later
        # Web-Jump provisioning (register_rancher_ui_web_jump reads _cfg) all honor
        # this deploy's picks. Only keys the operator actually sent are written, so a
        # bare redeploy keeps the existing Settings.
        if "web_jump_enabled" in meta:
            config_service.set("rancher_ui_web_jump_enabled", "1" if meta["web_jump_enabled"] else "0")
        if meta.get("jump_group"):
            config_service.set("rancher_ui_jump_group", str(meta["jump_group"]))
        if meta.get("jumpoint_name"):
            config_service.set("rancher_ui_jumpoint_name", str(meta["jumpoint_name"]))
        if meta.get("vault_account_group_id"):
            config_service.set("rancher_ui_vault_account_group_id", str(meta["vault_account_group_id"]))

        job_service.update_progress(db, job_id, 10, "Configuring firewall")
        # Learn (best-effort) + persist the dashboard's OWN public egress IP first:
        # the worker bootstraps + polls the node over its public IP, so that source
        # must be in the firewall or the deploy can't reach its own node. Then merge
        # it with manual CIDRs + provisioned-cluster egress /32s + the Web-Jump
        # Jumpoint /32 so a (re)deploy always reflects the current source set.
        await _ensure_dashboard_egress_cidr()
        fw = await refresh_rancher_firewall(db)
        if not fw.get("opened"):
            # Fail closed AND fast: polling a node no source can reach just burns the
            # readiness timeout and reports a misleading "not ready". Tell the operator
            # exactly what to set instead. (allow_open would have opened 0.0.0.0/0.)
            job_service.set_failed(
                db, job_id,
                "The Rancher node's firewall is closed — no allowed source CIDRs, and the "
                "dashboard couldn't auto-detect its own public egress IP to open it. Set "
                "rancher_dashboard_egress_cidr (the dashboard's egress IP) or "
                "rancher_allowed_source_cidrs in Settings → Kubernetes — or enable "
                "gcp_rancher_allow_open — then redeploy.")
            return

        # Single relocatable node. If a node already lives in the TARGET region,
        # reuse that exact zone (launcher starts/returns it). If one lives in a
        # DIFFERENT region, delete it first so we never strand a duplicate
        # "rancher-server" there (the node is ephemeral — state re-bootstraps).
        target_region = p["region"]
        try:
            existing_nodes = await gcp_service.list_gce_rancher(p["project_id"])
        except Exception as exc:
            logger.warning("Rancher relocation check failed (continuing): %s", exc)
            existing_nodes = []
        for node in existing_nodes:
            nzone = node.get("zone") or ""
            if region_catalog.region_from_zone(nzone) == target_region:
                p["zone"] = nzone   # reuse the live in-region node's exact zone
            else:
                logger.info("Relocating Rancher: deleting node '%s' in %s → region %s",
                            node.get("name"), nzone, target_region)
                job_service.update_progress(
                    db, job_id, 25, f"Relocating to {target_region}")
                try:
                    await gcp_service.stop_gce_rancher(
                        p["project_id"], nzone, node.get("name") or p["name"])
                except Exception as exc:
                    logger.warning("Failed to delete old-region Rancher node (continuing): %s", exc)

        job_service.update_progress(db, job_id, 30, "Launching COS VM")
        res = await gcp_service.run_gce_rancher(
            p["project_id"], p["zone"], p["name"], p["image"], bootstrap_password,
            network=p["network"], subnetwork=p["subnetwork"],
            machine_type=p["machine_type"],
            boot_disk_gb=p["boot_disk_gb"], network_tag=p["network_tag"],
            create_external_ip=True, region=p["region"])
        external_ip = res.get("external_ip") or ""
        url = res.get("url") or ""
        if not external_ip:
            job_service.set_failed(db, job_id, "Rancher VM has no external IP — cannot reach it.")
            return
        # Persist the ACTUAL deployed zone so teardown + bare redeploys stay sticky to
        # the (possibly relocated / auto-picked) region.
        deployed_zone = res.get("zone") or p["zone"]
        if deployed_zone:
            config_service.set("gcp_rancher_zone", deployed_zone)
        config_service.set("rancher_server_url", url)
        # Internal URL: what the in-cloud API runner dials (rancher_api_transport=
        # runner) — its VPC-connector egress is private-ranges-only, so the public
        # IP is unroutable from it. Persist unconditionally so flipping the
        # transport later doesn't need a redeploy.
        internal_ip = res.get("internal_ip") or ""
        if internal_ip:
            config_service.set("rancher_internal_url", f"https://{internal_ip}")

        fr = None  # first-run completion result (fresh deploy only)
        existing_token = config_service.get("rancher_api_token")
        if res.get("reused") and existing_token and res.get("status") == "RUNNING":
            # Node already bootstrapped + alive — just re-pin server-url (the
            # ephemeral IP may have changed across a stop/start).
            job_service.update_progress(db, job_id, 70, "Re-pinning server-url on the live node")
            try:
                await rancher_service.set_server_url_direct(server_url=url, api_token=existing_token)
            except Exception as exc:
                logger.warning("Rancher re-pin server-url failed (continuing): %s", exc)
            token = existing_token
        else:
            ready_timeout = _ready_timeout_s()
            job_service.update_progress(db, job_id, 55, "Waiting for Rancher to start")
            transport = (config_service.get("rancher_api_transport") or "direct").strip().lower()
            if transport == "runner":
                # The runner probes from INSIDE GCP (internal IP) — the whole point
                # is that the worker's own path may be TLS-inspected/blocked.
                from . import rancher_api_runner
                ready = await rancher_api_runner.wait_ready(
                    f"https://{internal_ip}" if internal_ip else url, ready_timeout, job_id=job_id)
            else:
                ready = await _wait_ready(url, ready_timeout)
            if ready == "tls_blocked":
                # The node IS serving (plain-HTTP /ping answered) but the HTTPS
                # handshake never completes from here — a TLS-inspecting corp proxy
                # (e.g. Cloudflare Gateway) rejecting the node's self-signed cert.
                # Nothing on the node/firewall side will fix that path.
                job_service.set_failed(
                    db, job_id,
                    f"Rancher IS up at {url} (plain-HTTP /ping answers) but the HTTPS handshake "
                    f"is being terminated in transit — this network TLS-inspects and rejects the "
                    f"node's self-signed certificate. Set rancher_api_transport=runner in "
                    f"Settings → Kubernetes (runs the API calls from an in-cloud runner) and "
                    f"redeploy, or add a Do-Not-Inspect rule for the node in your proxy.")
                return
            if ready != "ready":
                # Two common causes, so name both: (a) the node is up but the worker's
                # egress isn't actually permitted (auto-detect missed / stale IP), or
                # (b) the container is still initialising (cold image pull).
                dash = (config_service.get("rancher_dashboard_egress_cidr") or "").strip() or "unknown"
                allowed = ", ".join(firewall_status(db).get("merged") or []) or "none"
                job_service.set_failed(
                    db, job_id,
                    f"Rancher did not become ready at {url} within {ready_timeout}s. "
                    f"If the node is RUNNING, its firewall must allow the dashboard's egress IP "
                    f"({dash}); currently allowed: {allowed}. Otherwise the container may still be "
                    f"initialising — raise rancher_ready_timeout_s and redeploy, or check the node's "
                    f"container logs in GCP (google-logging-enabled is on).")
                return
            job_service.update_progress(db, job_id, 75, "Bootstrapping Rancher admin")
            token = await rancher_service.bootstrap_direct(
                bootstrap_password=bootstrap_password, server_url=url)
            # Store the token FIRST: a retry then takes the reuse branch above and
            # won't re-run first-run (which would fail on the now-changed password).
            config_service.set("rancher_api_token", token)

            # Complete Rancher's first-run wizard so the operator lands on a
            # ready, logged-in UI (not the "enter your bootstrap password" screen).
            # FRESH-deploy only; best-effort (the node is usable regardless).
            if config_service.get_bool("rancher_auto_first_run", True):
                job_service.update_progress(db, job_id, 85, "Completing Rancher first-run")
                # Rancher FORBIDS reusing the bootstrap password ("must not be the
                # same as the current password"), so the admin password must differ.
                # Use the operator's rancher_admin_password if set, else auto-generate
                # a strong one and persist it so it can be surfaced (login hint + job
                # result) — the operator logs in with it.
                new_pw = config_service.get("rancher_admin_password")
                if not new_pw:
                    new_pw = _generate_admin_password()
                    config_service.set("rancher_admin_password", new_pw)
                    config_service.set("rancher_admin_password_generated", "1")
                try:
                    fr = await rancher_service.complete_first_run_direct(
                        api_token=token, server_url=url,
                        current_password=bootstrap_password, new_password=new_pw)
                except Exception as exc:
                    logger.warning("Rancher first-run completion failed (non-fatal): %s", exc)
                    fr = {"password_changed": False, "reason": str(exc)}

        # Eagerly provision the PRA Web Jump (+ vault the admin credential into the
        # chosen account group) NOW, when it's enabled — so it's ready the moment
        # deploy finishes, using this deploy's Jump Group / Jumpoint / Vault group.
        # Best-effort (a PRA hiccup must not fail the node deploy); the lazy
        # open_console path is the fallback. Runs on fresh + reused.
        if config_service.get_bool("rancher_ui_web_jump_enabled", False):
            job_service.update_progress(db, job_id, 92, "Provisioning PRA Web Jump")
            try:
                from . import k8s_service
                await k8s_service.register_rancher_ui_web_jump(db)
            except Exception as exc:
                logger.warning("Rancher Web Jump provisioning failed (non-fatal): %s", exc)

        # Best-effort auto-register in Entitle (never fails the deploy).
        if config_service.get_bool("entitle_registration_enabled", False):
            job_service.update_progress(db, job_id, 90, "Registering in Entitle")
            try:
                from . import k8s_service
                await k8s_service.register_rancher_in_entitle("register")
            except Exception as exc:
                logger.warning("Rancher auto Entitle-register failed (continuing): %s", exc)

        completion = {
            "url": url, "external_ip": external_ip, "name": p["name"],
            "zone": deployed_zone, "region": target_region,
            "firewall_opened": fw.get("opened", False), "reused": res.get("reused", False),
            "first_run_completed": bool(fr and fr.get("password_changed")),
            "first_run_note": (fr or {}).get("reason", ""),
        }
        # Surface the admin login once, in the job result, when first-run set an
        # AUTO-GENERATED password AND it wasn't vaulted (the operator has no other
        # way to learn it). If it's vaulted for Web-Jump injection, or they set
        # rancher_admin_password themselves, don't echo the secret.
        if (fr and fr.get("password_changed")
                and config_service.get_bool("rancher_admin_password_generated", False)
                and not config_service.get("rancher_ui_vault_account_id")):
            completion["admin_username"] = "admin"
            completion["admin_password"] = config_service.get("rancher_admin_password")
        elif config_service.get("rancher_ui_vault_account_id"):
            completion["admin_credential"] = "stored in PRA Vault — use the rancher-ui Web Jump"
        job_service.set_completed(db, job_id, completion)
    except Exception as exc:
        logger.exception("Rancher node deploy failed (job %s)", job_id)
        job_service.set_failed(db, job_id, str(exc))


async def run_teardown(db, *, job_id: str, meta: dict) -> None:
    """Tear down the Rancher node: soft-guard on active imports → delete VM +
    firewall → deregister Entitle → remove PRA web jump → clear runtime config."""
    try:
        job_service.set_running(db, job_id)
        p = _node_params()
        name = meta.get("name") or p["name"]
        zone = meta.get("zone") or p["zone"]

        # Soft central-guard: warn about orphaned imports unless forced.
        if not meta.get("force"):
            try:
                from . import k8s_service
                n = k8s_service.count_rancher_imports(db)
                if n:
                    job_service.set_failed(
                        db, job_id,
                        f"{n} cluster(s) are imported into this Rancher — decommission or unmanage "
                        f"them first, or force teardown to orphan them.")
                    return
            except Exception as exc:
                logger.warning("Rancher import count check failed (continuing): %s", exc)

        # Deregister Entitle (best-effort) before the node goes away.
        if config_service.get("entitle_rancher_tfstate"):
            job_service.update_progress(db, job_id, 20, "Deregistering from Entitle")
            try:
                from . import k8s_service
                await k8s_service.register_rancher_in_entitle("deregister")
            except Exception as exc:
                logger.warning("Rancher Entitle deregister failed (continuing): %s", exc)

        # Remove the PRA web jump (best-effort).
        if config_service.get("rancher_ui_web_jump_tfstate"):
            job_service.update_progress(db, job_id, 40, "Removing PRA web jump")
            try:
                from . import k8s_service
                await k8s_service.remove_rancher_ui_web_jump()
            except Exception as exc:
                logger.warning("Rancher PRA web-jump removal failed (continuing): %s", exc)

        job_service.update_progress(db, job_id, 70, "Deleting COS VM + firewall")
        await gcp_service.stop_gce_rancher(
            p["project_id"], zone, name,
            delete_firewall=True, firewall_name=_firewall_name(name))

        # Clear runtime config so a fresh deploy re-bootstraps cleanly.
        for key in ("rancher_server_url", "rancher_internal_url", "rancher_api_token",
                    "rancher_ui_web_jump_id", "rancher_ui_web_jump_tfstate",
                    "rancher_ui_vault_account_id",
                    "rancher_ui_jumpoint_egress_ip",
                    "entitle_rancher_integration_id", "entitle_rancher_tfstate"):
            config_service.set(key, "")
        # An AUTO-GENERATED admin password belongs to the torn-down node instance —
        # clear it (+ the marker) so the next fresh deploy generates a new one. An
        # operator-set rancher_admin_password (no marker) is preserved.
        if config_service.get_bool("rancher_admin_password_generated", False):
            config_service.set("rancher_admin_password", "")
            config_service.set("rancher_admin_password_generated", "")

        job_service.set_completed(db, job_id, {"name": name, "zone": zone})
    except Exception as exc:
        logger.exception("Rancher node teardown failed (job %s)", job_id)
        job_service.set_failed(db, job_id, str(exc))
