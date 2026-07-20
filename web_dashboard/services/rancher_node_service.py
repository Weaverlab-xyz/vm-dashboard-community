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
from . import config_service, gcp_service, job_service, rancher_service

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


def _node_params() -> dict:
    """Resolve the node's deploy knobs from config_service (Settings)."""
    from ..config import settings
    zone = (config_service.get("gcp_rancher_zone")
            or config_service.get("gcp_zone") or settings.gcp_zone or "us-central1-a")
    try:
        boot_disk_gb = int(config_service.get("gcp_rancher_boot_disk_gb") or settings.gcp_rancher_boot_disk_gb)
    except (TypeError, ValueError):
        boot_disk_gb = settings.gcp_rancher_boot_disk_gb
    return {
        "project_id":   config_service.get("gcp_project_id") or settings.gcp_project_id,
        "zone":         zone,
        "name":         config_service.get("gcp_rancher_name") or settings.gcp_rancher_name,
        "image":        config_service.get("gcp_rancher_image") or settings.gcp_rancher_image,
        "machine_type": config_service.get("gcp_rancher_machine_type") or settings.gcp_rancher_machine_type,
        "boot_disk_gb": boot_disk_gb,
        "network":      config_service.get("gcp_network") or settings.gcp_network or "default",
        # Custom-mode sandbox VPC needs an explicit subnet. Prefer the jumpoint
        # subnet (Cloud NAT + infra-facing) over the no-egress user-VM subnet;
        # gcp_service normalizes a bare name into a regional self-link.
        "subnetwork":   config_service.get("gcp_jumpoint_subnetwork") or config_service.get("gcp_subnetwork") or "",
        "network_tag":  config_service.get("gcp_rancher_network_tag") or settings.gcp_rancher_network_tag,
    }


def _allowed_cidrs() -> list[str]:
    """Firewall source ranges, fail-closed. Empty CSV → [] unless allow_open."""
    csv = config_service.get("rancher_allowed_source_cidrs") or ""
    cidrs = [c.strip() for c in csv.split(",") if c.strip()]
    if not cidrs:
        if config_service.get_bool("gcp_rancher_allow_open", False):
            logger.warning("Rancher node firewall opening 0.0.0.0/0 (gcp_rancher_allow_open=true)")
            return ["0.0.0.0/0"]
        logger.warning("Rancher node has NO allowed source CIDRs — firewall stays closed (node unreachable). "
                       "Set rancher_allowed_source_cidrs in Settings, provision a cluster, or enable the Web Jump.")
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
    """Refresh + persist the dashboard's own egress /32 so the firewall admits the
    worker. Detection wins when it succeeds (tracks a changed dynamic IP); on failure
    any operator-set ``rancher_dashboard_egress_cidr`` is left intact. Returns the
    CIDR now in effect (``""`` if still unknown)."""
    ip = await _detect_egress_ip()
    if ip:
        cidr = f"{ip}/32"
        if (config_service.get("rancher_dashboard_egress_cidr") or "").strip() != cidr:
            config_service.set("rancher_dashboard_egress_cidr", cidr)
            logger.info("Rancher firewall: dashboard egress IP detected as %s", cidr)
        return cidr
    existing = (config_service.get("rancher_dashboard_egress_cidr") or "").strip()
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
                    | set(_jumpoint_cidr()) | set(_dashboard_cidr()))
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
    merged = sorted(set(_allowed_cidrs()) | set(_auto_cluster_cidrs(db)) | set(jump) | set(dash))
    csv = config_service.get("rancher_allowed_source_cidrs") or ""
    return {
        "manual_cidrs": [c.strip() for c in csv.split(",") if c.strip()],
        "cluster_egress_ips": clusters,
        "jumpoint_egress_ip": jump[0] if jump else "",
        "dashboard_egress_ip": dash[0] if dash else "",
        "merged": merged,
        "allow_open": config_service.get_bool("gcp_rancher_allow_open", False),
        "opened": bool(merged),
    }


async def _wait_ready(url: str, timeout_s: int = _READY_TIMEOUT_S) -> bool:
    """Poll the node until Rancher answers (it needs 1-3 min; expect early 5xx)."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    async with httpx.AsyncClient(verify=False, timeout=10.0) as c:
        while asyncio.get_event_loop().time() < deadline:
            try:
                r = await c.get(f"{url}/ping")
                if r.status_code < 500:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(_READY_POLL_S)
    return False


async def run_deploy(db, *, job_id: str, meta: dict) -> None:
    """Deploy (or reuse) the Rancher node: firewall → COS VM → pin server-url →
    wait ready → bootstrap → mint token → best-effort Entitle register."""
    try:
        job_service.set_running(db, job_id)
        p = _node_params()
        if not p["project_id"]:
            job_service.set_failed(db, job_id, "GCP project is not configured.")
            return
        bootstrap_password = config_service.get("rancher_bootstrap_password")
        if not bootstrap_password:
            job_service.set_failed(db, job_id, "rancher_bootstrap_password is not set (Settings → Kubernetes).")
            return

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

        job_service.update_progress(db, job_id, 30, "Launching COS VM")
        res = await gcp_service.run_gce_rancher(
            p["project_id"], p["zone"], p["name"], p["image"], bootstrap_password,
            network=p["network"], subnetwork=p["subnetwork"],
            machine_type=p["machine_type"],
            boot_disk_gb=p["boot_disk_gb"], network_tag=p["network_tag"],
            create_external_ip=True)
        external_ip = res.get("external_ip") or ""
        url = res.get("url") or ""
        if not external_ip:
            job_service.set_failed(db, job_id, "Rancher VM has no external IP — cannot reach it.")
            return
        config_service.set("rancher_server_url", url)

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
            if not await _wait_ready(url, ready_timeout):
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
            config_service.set("rancher_api_token", token)

        # Best-effort auto-register in Entitle (never fails the deploy).
        if config_service.get_bool("entitle_registration_enabled", False):
            job_service.update_progress(db, job_id, 90, "Registering in Entitle")
            try:
                from . import k8s_service
                await k8s_service.register_rancher_in_entitle("register")
            except Exception as exc:
                logger.warning("Rancher auto Entitle-register failed (continuing): %s", exc)

        job_service.set_completed(db, job_id, {
            "url": url, "external_ip": external_ip, "name": p["name"], "zone": p["zone"],
            "firewall_opened": fw.get("opened", False), "reused": res.get("reused", False),
        })
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
        for key in ("rancher_server_url", "rancher_api_token",
                    "rancher_ui_web_jump_id", "rancher_ui_web_jump_tfstate",
                    "rancher_ui_jumpoint_egress_ip",
                    "entitle_rancher_integration_id", "entitle_rancher_tfstate"):
            config_service.set(key, "")

        job_service.set_completed(db, job_id, {"name": name, "zone": zone})
    except Exception as exc:
        logger.exception("Rancher node teardown failed (job %s)", job_id)
        job_service.set_failed(db, job_id, str(exc))
