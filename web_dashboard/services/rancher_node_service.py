"""Rancher management-node orchestrator (AWS / Azure / GCP).

Owns the deploy/teardown JOB lifecycle for the central Rancher server that runs
as a single privileged container on a public (source-restricted) VM. Keeps the
per-cloud service modules pure-cloud and ``rancher_service`` pure-API; this module
glues them to config + the job queue (mirrors how ``vdesktop_service`` owns its
own job lifecycle). Dispatched from ``jobs_worker`` (``rancher_node_deploy`` /
``rancher_node_teardown``) — long ops (VM boot + Rancher bootstrap poll) that the
durable worker's heartbeat protects.

Rancher stays a SINGLE management plane, so there is one node and the cloud is a
choice about where it lives — picked per deploy like the region already is, and
persisted (``rancher_node_cloud``) so teardown and bare redeploys find it again.
Redeploying to a different cloud RELOCATES the node.

Everything shared with the managed Portainer server — the ingress allow-list merge,
egress-IP detection, placement resolution, the admin-password generator — lives in
``managed_node_service``; the private helpers here are thin wrappers over it, kept
so the seams tests patch stay in one obvious place.

The node is EPHEMERAL on GCP and AWS: an ephemeral external IP + auto-delete boot
disk, so a stop/recreate reassigns the IP and wipes ``/var/lib/rancher`` (Rancher
re-bootstraps and downstream clusters must re-import). On Azure the Standard public
IP is Static, so the address survives a recreate even though the state does not.
"""
import asyncio
import logging

import httpx

from . import (config_service, gcp_service, job_service, managed_node_service,
               rancher_service, region_catalog)

logger = logging.getLogger(__name__)

_SPEC = managed_node_service.RANCHER

# How long to wait for Rancher to start serving after the VM boots.
_READY_TIMEOUT_S = 360
_READY_POLL_S = 10


def _node_cloud() -> str:
    """Which cloud hosts the node (persisted on deploy; ``gcp`` for installs that
    predate the cloud pick)."""
    return managed_node_service.node_cloud(_SPEC)


def _firewall_name(node_name: str) -> str:
    return managed_node_service.firewall_name(node_name)


def _generate_admin_password() -> str:
    """A strong admin UI password for Rancher first-run when the operator didn't
    set ``rancher_admin_password``. Rancher enforces ≥12 chars and forbids reusing
    the bootstrap password, so a fresh distinct one is required."""
    return managed_node_service.generate_admin_password()


def _node_params(region=None, zone=None, cloud=None) -> dict:
    """Resolve the node's deploy knobs for ``cloud``, region-aware.

    Blank ``cloud`` means the persisted node cloud, so every existing caller keeps
    reading the node that is actually deployed.
    """
    return managed_node_service.resolve_placement(
        cloud or _node_cloud(), _SPEC, region=region, zone=zone)


def _allowed_cidrs() -> list[str]:
    """MANUAL firewall source ranges (CSV), fail-closed. Empty CSV → [] unless the
    node cloud's allow_open is ticked.

    Deliberately quiet: the CSV is only ONE input to the merged set (cluster
    egress /32s, Jumpoint /32, dashboard + runner CIDRs join it), so an empty
    CSV usually does NOT mean the firewall stays closed. The applied-outcome
    warnings live in :func:`refresh_rancher_firewall`, which sees the FINAL set.
    """
    return managed_node_service.allowed_cidrs(_SPEC, _node_cloud())


def _auto_cluster_cidrs(db) -> list[str]:
    """/32s for every dashboard-PROVISIONED cluster whose egress IP we captured.

    These are the clusters' NAT/outbound IPs — the source address their
    cattle-cluster-agent uses to dial out to this node — so they must be allowed
    for the import to reach ``Active``. Registered clusters have no ``egress_ip``.
    """
    from ..database import K8sCluster
    rows = db.query(K8sCluster).filter(K8sCluster.egress_ip.isnot(None)).all()
    return [f"{r.egress_ip.strip()}/32" for r in rows if (r.egress_ip or "").strip()]


def _jumpoint_cidrs(db=None) -> list[str]:
    """/32s for the Gateway hosts that can broker this node's Web Jump.

    A PRA Web Jump reaches the node THROUGH a Gateway, so the source IP hitting
    the firewall is that host's egress IP (never the PRA appliance's). Every live
    gateway in the Web Jump's cloud counts — see
    :func:`managed_node_service.jumpoint_cidrs` for why.
    """
    return managed_node_service.jumpoint_cidrs(_SPEC, db)


def _dashboard_cidr() -> list[str]:
    """/32 for the DASHBOARD's own public egress IP.

    The worker bootstraps and polls the node over its PUBLIC IP, so this is the
    source address that hits the node's source-restricted firewall — without it a
    (re)deploy can't reach its own node and the readiness poll times out.
    """
    return managed_node_service.dashboard_cidr(_SPEC)


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
    return managed_node_service.ready_timeout_s(_SPEC)


async def _detect_egress_ip() -> str:
    """Best-effort: learn the worker's own public egress IP via a plain-HTTP echo.

    Kept as a module-level name (rather than calling straight through) because it is
    the seam the firewall tests replace — detection must never fire in a unit test.
    """
    return await managed_node_service.detect_egress_ip("Rancher")


async def _ensure_dashboard_egress_cidr() -> str:
    """Refresh + persist the dashboard's own egress CIDR so the firewall admits the
    worker. An operator-set CIDR that already CONTAINS the detected IP is kept as-is
    (corp proxies egress from a pool) — see
    :func:`managed_node_service.ensure_dashboard_egress_cidr`."""
    return await managed_node_service.ensure_dashboard_egress_cidr(
        _SPEC, detect=_detect_egress_ip)


async def refresh_rancher_firewall(db) -> dict:
    """Recompute the node's firewall source set and re-apply it idempotently.

    The merged set is the manual CSV (``_allowed_cidrs``) plus the auto-discovered
    dashboard-provisioned cluster egress /32s plus a /32 for every Gateway that can
    broker the Web Jump. Called from every lifecycle event that changes the set (node
    deploy, cluster provision/import/decommission, Web Jump enable). Fail-closed
    and idempotent behavior is inherited from the per-cloud apply (empty set → rule
    removed / every ingress permission revoked; ``0.0.0.0/0`` from allow_open dedupes
    harmlessly). No-op safe: returns early when the node's cloud has no account
    configured, so callers can fire it best-effort even when the node isn't deployed.
    """
    cloud = _node_cloud()
    p = _node_params(cloud=cloud)
    if not p["account"]:
        return {"skipped": f"no {cloud} account configured"}
    merged = sorted(set(_allowed_cidrs()) | set(_auto_cluster_cidrs(db))
                    | set(_jumpoint_cidrs(db)) | set(_dashboard_cidr()) | set(_runner_cidr()))
    # Warn on the FINAL merged set only — an empty manual CSV alone is normal
    # (auto-discovered sources usually populate the set on their own).
    if not merged:
        logger.warning("Rancher node has NO allowed source CIDRs — firewall stays closed (node unreachable). "
                       "Set rancher_allowed_source_cidrs in Settings, provision a cluster, or enable the Web Jump.")
    elif "0.0.0.0/0" in merged:
        logger.warning("Rancher node firewall opening 0.0.0.0/0 — node reachable from anywhere "
                       "(%s or a manual CSV entry)", _SPEC.allow_open_key(cloud))
    return await managed_node_service.apply_ingress(cloud, _SPEC, p, merged)


def firewall_status(db) -> dict:
    """Read-only breakdown of the node's firewall source set (no GCP call) — what
    :func:`refresh_rancher_firewall` would apply, plus the per-cluster egress IPs so
    the operator can see exactly which sources are allowed and why."""
    from ..database import K8sCluster
    rows = db.query(K8sCluster).filter(K8sCluster.egress_ip.isnot(None)).all()
    clusters = [{"name": r.name, "cloud": r.cloud, "ip": (r.egress_ip or "").strip()}
                for r in rows if (r.egress_ip or "").strip()]
    jump = _jumpoint_cidrs(db)
    dash = _dashboard_cidr()
    runner = _runner_cidr()
    merged = sorted(set(_allowed_cidrs()) | set(_auto_cluster_cidrs(db))
                    | set(jump) | set(dash) | set(runner))
    csv = config_service.get("rancher_allowed_source_cidrs") or ""
    return {
        "manual_cidrs": [c.strip() for c in csv.split(",") if c.strip()],
        "cluster_egress_ips": clusters,
        # Kept singular for the existing Settings panel; gateway_cidrs is the full set.
        "jumpoint_egress_ip": jump[0] if jump else "",
        "gateway_cidrs": jump,
        "dashboard_egress_ip": dash[0] if dash else "",
        "runner_source_cidr": runner[0] if runner else "",
        "merged": merged,
        "cloud": _node_cloud(),
        "ports": list(_SPEC.ports),
        "allow_open": config_service.get_bool(_SPEC.allow_open_key(_node_cloud()), False),
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


async def _launch_node(cloud: str, p: dict, bootstrap_password: str) -> dict:
    """Create (or reuse/start) the node VM on ``cloud`` and return
    ``{external_ip, internal_ip, url, zone, reused, status}``.

    Per-cloud because the container-launch mechanism has no common API: GCE reads a
    ``gce-container-declaration`` at boot, AWS runs ``docker run`` from EC2 user-data
    on a Docker-bearing AMI, Azure does the same from cloud-init. The *shape* is
    identical everywhere though — privileged container, host ports 80/443, no
    instance identity attached (the node needs no cloud API access).
    """
    if cloud == "gcp":
        return await gcp_service.run_gce_rancher(
            p["project_id"], p["zone"], p["name"], p["image"], bootstrap_password,
            network=p["network"], subnetwork=p["subnetwork"],
            machine_type=p["machine_type"],
            boot_disk_gb=p["boot_disk_gb"], network_tag=p["network_tag"],
            create_external_ip=True, region=p["region"])
    raise managed_node_service.unsupported(cloud, _SPEC, "node launch")


async def _stop_node(cloud: str, p: dict, *, name: str, zone: str,
                     delete_firewall: bool = False) -> None:
    """Delete the node VM on ``cloud`` (delete, not stop — the node is disposable),
    optionally taking its ingress rule with it."""
    if cloud == "gcp":
        await gcp_service.stop_gce_rancher(
            p["project_id"], zone, name,
            delete_firewall=delete_firewall,
            firewall_name=_firewall_name(name) if delete_firewall else None)
        return
    raise managed_node_service.unsupported(cloud, _SPEC, "node teardown")


async def _relocate_across_clouds(db, job_id: str, target_cloud: str) -> None:
    """Delete the node in its PREVIOUS cloud when the operator moved it.

    Rancher is a single management plane, so two live nodes is never the intent —
    and a stranded one keeps billing and keeps answering on an IP the dashboard has
    stopped tracking. Best-effort: if the old cloud can't be reached we say so and
    carry on, because refusing would leave the operator unable to move at all.
    """
    prev_cloud = _node_cloud()
    if prev_cloud == target_cloud:
        return
    logger.info("Relocating Rancher: %s → %s", prev_cloud, target_cloud)
    job_service.update_progress(db, job_id, 20, f"Relocating from {prev_cloud}")
    try:
        prev = _node_params(cloud=prev_cloud)
        for node in await managed_node_service.list_nodes(prev_cloud, _SPEC, prev):
            await _stop_node(prev_cloud, prev, name=node.get("name") or prev["name"],
                             zone=node.get("zone") or prev["zone"], delete_firewall=True)
    except Exception as exc:  # noqa: BLE001 — never block a move on the old cloud
        logger.warning("Rancher cross-cloud relocation: deleting the %s node failed "
                       "(continuing; it may need removing by hand): %s", prev_cloud, exc)


async def run_deploy(db, *, job_id: str, meta: dict) -> None:
    """Deploy (or reuse) the Rancher node: firewall → VM → pin server-url →
    wait ready → bootstrap → mint token → best-effort Entitle register."""
    try:
        job_service.set_running(db, job_id)
        # Deploy-time cloud + region/zone pick (blank → the persisted node cloud /
        # region, else the configured default). Selects the node's region-specific
        # subnet/zone within the chosen cloud.
        cloud = (meta.get("cloud") or "").strip().lower() or _node_cloud()
        if cloud not in managed_node_service.CLOUDS:
            job_service.set_failed(
                db, job_id,
                f"{cloud!r} is not a cloud the Rancher node can run on — "
                f"choose one of {', '.join(managed_node_service.CLOUDS)}.")
            return
        p = _node_params(region=meta.get("region"), zone=meta.get("zone"), cloud=cloud)
        if not p["account"]:
            job_service.set_failed(
                db, job_id,
                f"{cloud.upper()} is not configured, so the Rancher node has nowhere to go. "
                f"Set it up under Settings → {cloud.upper()} (or pick a different cloud on "
                f"the deploy form).")
            return
        bootstrap_password = config_service.get("rancher_bootstrap_password")
        if not bootstrap_password:
            job_service.set_failed(db, job_id, "rancher_bootstrap_password is not set (Settings → Kubernetes).")
            return

        # Persist the deploy-form PRA choices to config FIRST, so the firewall
        # merge (_jumpoint_cidrs gates on rancher_ui_web_jump_enabled) and the later
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
                f"rancher_allowed_source_cidrs in Settings → Kubernetes — or enable "
                f"{_SPEC.allow_open_key(cloud)} — then redeploy.")
            return

        # Moving the node to a DIFFERENT cloud deletes the old one first, for the same
        # reason a region move does: one management plane, and a stranded node keeps
        # billing while answering on an address nothing tracks any more.
        await _relocate_across_clouds(db, job_id, cloud)

        # Single relocatable node. If a node already lives in the TARGET region,
        # reuse that exact zone (launcher starts/returns it). If one lives in a
        # DIFFERENT region, delete it first so we never strand a duplicate
        # "rancher-server" there (the node is ephemeral — state re-bootstraps).
        target_region = p["region"]
        try:
            existing_nodes = await managed_node_service.list_nodes(cloud, _SPEC, p)
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
                    await _stop_node(cloud, p, name=node.get("name") or p["name"],
                                     zone=nzone)
                except Exception as exc:
                    logger.warning("Failed to delete old-region Rancher node (continuing): %s", exc)

        job_service.update_progress(db, job_id, 30, "Launching the node VM")
        res = await _launch_node(cloud, p, bootstrap_password)
        external_ip = res.get("external_ip") or ""
        url = res.get("url") or ""
        if not external_ip:
            job_service.set_failed(db, job_id, "Rancher VM has no external IP — cannot reach it.")
            return
        # Persist the ACTUAL deployed cloud + zone so teardown + bare redeploys stay
        # sticky to the (possibly relocated / auto-picked) placement.
        managed_node_service.set_node_cloud(_SPEC, cloud)
        deployed_zone = res.get("zone") or p["zone"]
        if deployed_zone:
            config_service.set(_SPEC.infra_key(cloud, "zone"), deployed_zone)
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
                # Name the causes for the transport actually used — the two paths fail
                # for different reasons, so a one-size message misleads (the runner path
                # never touches the dashboard's egress IP, yet the old message blamed it).
                if transport == "runner":
                    internal = config_service.get("rancher_internal_url") or (
                        f"https://{internal_ip}" if internal_ip else "(unknown internal IP)")
                    job_service.set_failed(
                        db, job_id,
                        f"Rancher did not become ready within {ready_timeout}s. The in-cloud runner "
                        f"probes the node's INTERNAL address {internal} from region {p['region']} "
                        f"(Cloud Run direct VPC egress reaches only SAME-region internal IPs, so the "
                        f"runner is pinned there). Likely causes: the container is still initialising "
                        f"(cold rancher/rancher pull — raise rancher_ready_timeout_s and redeploy), or "
                        f"the runner can't route to the internal IP (confirm gcp_run_network / "
                        f"gcp_run_subnetwork reach the node's VPC and that a subnet of that name exists "
                        f"in {p['region']}). See the worker log's runner probe tail for the exact curl "
                        f"error, or the node's container logs in GCP (google-logging-enabled is on).")
                    return
                # Direct transport: the worker dials the node's PUBLIC IP, so its own
                # egress must be in the node firewall (auto-detect can miss a pooled /
                # stale IP), else the container may still be initialising.
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
            "cloud": cloud, "zone": deployed_zone, "region": target_region,
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
        # Teardown follows the node, not the form: the persisted cloud is where it
        # actually is. An explicit meta cloud is honoured so a stranded node in a
        # cloud we've since moved away from can still be reaped.
        cloud = (meta.get("cloud") or "").strip().lower() or _node_cloud()
        p = _node_params(cloud=cloud)
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

        job_service.update_progress(db, job_id, 70, "Deleting the node VM + ingress rule")
        await _stop_node(cloud, p, name=name, zone=zone, delete_firewall=True)

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

        job_service.set_completed(db, job_id, {"name": name, "zone": zone, "cloud": cloud})
    except Exception as exc:
        logger.exception("Rancher node teardown failed (job %s)", job_id)
        job_service.set_failed(db, job_id, str(exc))
