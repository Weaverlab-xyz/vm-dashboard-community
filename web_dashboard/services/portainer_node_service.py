"""Portainer CE server orchestrator (AWS / Azure / GCP).

Owns the deploy/teardown JOB lifecycle for the managed Portainer server that runs
as a single container on a public (source-restricted) VM. Keeps the per-cloud
service modules pure-cloud and ``portainer_service`` a pure API client; this module
glues them to config + the job queue. Dispatched from ``jobs_worker``
(``portainer_node_deploy`` / ``portainer_node_teardown``) — long ops (VM boot +
first-run bootstrap poll) that the durable worker's heartbeat protects.

One Portainer server manages many Docker hosts (they dial IN as Edge agents), so
there is one node and the cloud is a choice about where it lives — picked per
deploy like the region already is, and persisted (``portainer_node_cloud``) so
teardown and bare redeploys find it again. Redeploying to a different cloud
RELOCATES the node.

Everything shared with the Rancher management node — the ingress allow-list merge,
egress-IP detection, placement resolution, the admin-password generator — lives in
``managed_node_service``; the private helpers here are thin wrappers over it.

A successful deploy writes ``portainer_url`` / ``portainer_pat`` /
``portainer_verify_ssl`` into config, which is exactly what the existing
Portainer integration reads — so the Containers page starts working against the
new server with no manual Settings step.

By default the node is EPHEMERAL: an ephemeral external IP + auto-delete boot disk.
A teardown (or stop/recreate) wipes ``/var/lib/portainer``, so users, environments
and settings are lost and the node must re-bootstrap.

Set ``portainer_data_disk_enabled`` to make it DURABLE instead: /data moves to a
separate persistent disk that outlives the VM. Three consequences worth knowing
before reading the deploy path — a persistent disk is zonal, so an existing disk
pins the node's zone and blocks a region move; Portainer ignores
``--admin-password`` once its DB holds an admin, so the stored credential must be
reused rather than regenerated; and the external IP still changes on every
recreate, so ``portainer_url`` is still rewritten each deploy.
"""
import logging

from . import (aws_service, config_service, gcp_service, job_service,
               managed_node_service, portainer_service, region_catalog)
# Same strength requirements as the Rancher node (Portainer also enforces a 12-char
# minimum), so there is one implementation, in the shared module.
from .managed_node_service import generate_admin_password as _generate_admin_password

logger = logging.getLogger(__name__)

_SPEC = managed_node_service.PORTAINER

# How long to wait for Portainer to start serving after the VM boots. Portainer is
# much lighter than Rancher, but the host still has to pull the image on a fresh VM.
_READY_TIMEOUT_S = 300

# The admin Portainer's first-run init creates.
_ADMIN_USERNAME = "admin"

# Told to the operator when a node has locked itself out. A REDEPLOY alone won't fix it:
# the launcher reuses a RUNNING VM as-is, and the container declaration / user-data
# (with --admin-password) is only read at boot — so the VM has to go.
_LOCKED_NODE_REMEDY = ("Delete the node on the Containers page and deploy again: the "
                       "replacement VM initializes its admin at boot, so it can't hit "
                       "this window.")


def _node_cloud() -> str:
    """Which cloud hosts the node (persisted on deploy; ``gcp`` for installs that
    predate the cloud pick)."""
    return managed_node_service.node_cloud(_SPEC)


def _firewall_name(node_name: str) -> str:
    return managed_node_service.firewall_name(node_name)


def _node_params(region=None, zone=None, cloud=None) -> dict:
    """Resolve the node's deploy knobs for ``cloud``, region-aware, plus the durable
    data-volume fields.

    Blank ``cloud`` means the persisted node cloud, so every existing caller keeps
    reading the node that is actually deployed. The data-volume NAME is blank when
    durable state is off, and that blank is what selects the ephemeral
    boot-disk/root-volume path all the way down in the launcher.
    """
    from ..config import settings
    cloud = cloud or _node_cloud()
    p = managed_node_service.resolve_placement(cloud, _SPEC, region=region, zone=zone)

    default_data = getattr(settings, _SPEC.infra_key(cloud, "data_disk_gb"), 10)
    try:
        data_disk_gb = int(config_service.get(_SPEC.infra_key(cloud, "data_disk_gb"))
                           or default_data)
    except (TypeError, ValueError):
        data_disk_gb = default_data
    p["data_disk_name"] = (f"{p['name']}-data"
                           if config_service.get_bool("portainer_data_disk_enabled", False)
                           else "")
    p["data_disk_gb"] = data_disk_gb
    return p


def _allowed_cidrs() -> list[str]:
    """MANUAL firewall source ranges (CSV), fail-closed. Empty CSV -> [] unless the
    node cloud's allow_open is ticked.

    Deliberately quiet: the CSV is only one input to the merged set (the dashboard's
    own egress CIDR and the gateway /32s join it), so an empty CSV does NOT
    necessarily mean the firewall stays closed. The applied-outcome warnings live in
    :func:`refresh_portainer_firewall`, which sees the FINAL set."""
    return managed_node_service.allowed_cidrs(_SPEC, _node_cloud())


def _jumpoint_cidrs(db=None) -> list[str]:
    """/32s for the Gateway hosts that can broker this node's Web Jump.

    A PRA Web Jump reaches the node THROUGH a Gateway, so the source IP hitting the
    firewall is that host's egress IP (never the PRA appliance's). Every live gateway
    in the Web Jump's cloud counts -- see
    :func:`managed_node_service.jumpoint_cidrs` for why."""
    return managed_node_service.jumpoint_cidrs(_SPEC, db)


def _dashboard_cidr() -> list[str]:
    """/32 for the DASHBOARD's own public egress IP.

    The worker bootstraps and polls the node over its PUBLIC IP, so this is the
    source address that hits the node's source-restricted firewall -- without it a
    (re)deploy cannot reach its own node and the readiness poll times out."""
    return managed_node_service.dashboard_cidr(_SPEC)


def _ready_timeout_s() -> int:
    """Readiness poll budget (config ``portainer_ready_timeout_s``, default 300s)."""
    return managed_node_service.ready_timeout_s(_SPEC)


async def _detect_egress_ip() -> str:
    """Best-effort: learn the worker's own public egress IP via a plain-HTTP echo.

    Kept as a module-level name (rather than calling straight through) because it is
    the seam the firewall tests replace -- detection must never fire in a unit test.
    """
    return await managed_node_service.detect_egress_ip("Portainer")


async def _ensure_dashboard_egress_cidr() -> str:
    """Refresh + persist the dashboard's own egress CIDR so the firewall admits the
    worker. An operator-set CIDR that already CONTAINS the detected IP is kept as-is
    (corp proxies egress from a pool) -- see
    :func:`managed_node_service.ensure_dashboard_egress_cidr`."""
    return await managed_node_service.ensure_dashboard_egress_cidr(
        _SPEC, detect=_detect_egress_ip)


async def refresh_portainer_firewall(db=None) -> dict:
    """Recompute the node's firewall source set and re-apply it idempotently.

    The merged set is the manual CSV (``_allowed_cidrs``) plus the dashboard's own
    egress /32 plus a /32 for every Gateway that can broker the Web Jump. Fail-closed
    and idempotent behavior is inherited from the per-cloud apply (empty set → rule
    removed / every ingress permission revoked; ``0.0.0.0/0`` from allow_open dedupes
    harmlessly). No-op safe: returns early when the node's cloud has no account
    configured, so callers can fire it best-effort even when no node is deployed.

    ``db`` is optional so best-effort callers can fire it without a session; it is used
    to read the gateway registry."""
    cloud = _node_cloud()
    p = _node_params(cloud=cloud)
    if not p["account"]:
        return {"skipped": f"no {cloud} account configured"}
    merged = sorted(set(_allowed_cidrs()) | set(_dashboard_cidr()) | set(_jumpoint_cidrs(db)))
    if not merged:
        logger.warning("Portainer node has NO allowed source CIDRs — firewall stays closed "
                       "(node unreachable). Set portainer_allowed_source_cidrs in Settings.")
    elif "0.0.0.0/0" in merged:
        logger.warning("Portainer node firewall opening 0.0.0.0/0 — node reachable from "
                       "anywhere (%s or a manual CSV entry)", _SPEC.allow_open_key(cloud))
    return await managed_node_service.apply_ingress(cloud, _SPEC, p, merged)


def firewall_status(db=None) -> dict:
    """Read-only breakdown of the node's firewall source set (no GCP call) — what
    :func:`refresh_portainer_firewall` would apply, so the operator can see exactly
    which sources are allowed and why."""
    dash = _dashboard_cidr()
    jump = _jumpoint_cidrs(db)
    merged = sorted(set(_allowed_cidrs()) | set(dash) | set(jump))
    csv = config_service.get("portainer_allowed_source_cidrs") or ""
    return {
        "manual_cidrs": [c.strip() for c in csv.split(",") if c.strip()],
        "dashboard_egress_ip": dash[0] if dash else "",
        # Kept singular for the existing Settings panel; gateway_cidrs is the full set.
        "jumpoint_egress_ip": jump[0] if jump else "",
        "gateway_cidrs": jump,
        "merged": merged,
        "cloud": _node_cloud(),
        "allow_open": config_service.get_bool(_SPEC.allow_open_key(_node_cloud()), False),
        "opened": bool(merged),
        "ports": list(_SPEC.ports),
    }


def _pra_configured() -> bool:
    """PRA is usable when the API host, an OAuth client and a Jumpoint are set."""
    return all((config_service.get("bt_api_host"), config_service.get("bt_client_id"),
                config_service.get("bt_jumpoint_name")))


async def register_portainer_ui_web_jump(db) -> dict:
    """Ensure a PRA **Web Jump** to the managed Portainer UI exists. Idempotent:
    returns the stored id if already provisioned, EXCEPT when the node has moved to a
    new URL, which is re-pointed (destroy + recreate). OPT-IN
    (``portainer_ui_web_jump_enabled``): lets an operator whose IP isn't in
    ``portainer_allowed_source_cidrs`` reach the node's UI from the PRA
    representative console — brokered and recorded, with no CIDR change.

    Re-ensures the dashboard-managed Jumpoint host and refreshes the node firewall on
    EVERY call (not just first provisioning): AWS/GCP jumpoint egress IPs are
    ephemeral, so a host reclaim/recreate changes the IP — re-syncing here keeps the
    firewall's jumpoint /32 current even when the Web Jump itself already exists.

    Mirrors ``k8s_service.register_rancher_ui_web_jump``."""
    from . import jumpoint_host_service, terraform_pra_service as pra

    url = config_service.get("portainer_url")
    if not url:
        raise RuntimeError("Portainer node is not running (no portainer_url)")
    if not _pra_configured():
        raise RuntimeError("PRA is not configured (bt_api_host / bt_client_id / bt_jumpoint_name)")

    # The Web Jump connects THROUGH a Jumpoint, so the source hitting the node's
    # firewall is that host's egress IP. Ensure the host is up, capture its (possibly
    # changed) IP, and refresh the firewall so its /32 is allowed — BEFORE the reused
    # early-return, so an ephemeral IP is re-synced on every call.
    try:
        await jumpoint_host_service.ensure_portainer_ui_jumpoint()
    except Exception as exc:
        logger.warning("Portainer UI web-jump: jumpoint egress capture failed (non-fatal): %s", exc)
    try:
        await refresh_portainer_firewall(db)
    except Exception as exc:
        logger.warning("Portainer UI web-jump: firewall refresh failed (non-fatal): %s", exc)

    existing = config_service.get("portainer_ui_web_jump_id")
    if existing:
        # A Web Jump carries the URL it was created with, and this node is EPHEMERAL: a
        # stop/recreate (or a relocation to another region) gives it a new public IP, and
        # `portainer_url` follows while the jump item does not. Left alone, the item keeps
        # dialling an address nothing answers on and every session dies with PRA's
        # "internal timeout starting session" — indistinguishable from a firewall problem,
        # and no redeploy could converge it, because this early-return is the only path a
        # redeploy takes.
        prior_url = pra.web_jump_url_from_state(
            config_service.get("portainer_ui_web_jump_tfstate"))
        if not prior_url or prior_url == url:
            # "" means the state couldn't tell us — reuse rather than destroy something
            # that is probably fine.
            return {"web_jump_id": existing, "reused": True}
        # provision_web_jump starts from empty state, so there is no in-place update to
        # make: re-pointing is destroy + recreate. The remove is best-effort inside, so a
        # PRA-side failure leaves the old item orphaned rather than blocking the new one —
        # the log line below is what ties the two together afterwards.
        logger.info("Portainer UI web-jump: node URL moved %s -> %s — re-pointing the Web Jump",
                    prior_url, url)
        await remove_portainer_ui_web_jump()

    jump_group = config_service.get("portainer_ui_jump_group") or config_service.get("bt_jump_group_name")
    jumpoint = config_service.get("portainer_ui_jumpoint_name") or config_service.get("bt_jumpoint_name")
    # Vault the admin credential for injection when a Vault account group is chosen
    # (deploy form) — the operator never sees the password; PRA injects it into the
    # Portainer login. Falls back to a plain (non-injected) Web Jump when no group is
    # set. The password comes from first-run bootstrap.
    vault_group = (config_service.get("portainer_ui_vault_account_group_id")
                   or config_service.get("bt_vault_account_group_id") or "").strip()
    admin_password = config_service.get("portainer_admin_password") if vault_group else ""
    try:
        vault_group_id = int(vault_group) if vault_group else None
    except ValueError:
        vault_group_id = None

    result = await pra.provision_web_jump(
        name="portainer-ui", url=url,
        jump_group_name=jump_group, jumpoint_name=jumpoint, tag="portainer",
        verify_certificate=config_service.get_bool("portainer_ui_verify_certificate", False),
        client_secret=config_service.get("bt_client_secret"),
        admin_password=admin_password,
        vault_account_name="portainer-ui-admin" if (admin_password and vault_group_id) else "",
        vault_username=_ADMIN_USERNAME, vault_account_group_id=vault_group_id)

    config_service.set("portainer_ui_web_jump_id", str(result.get("web_jump_id") or ""))
    config_service.set("portainer_ui_vault_account_id", str(result.get("vault_account_id") or ""))
    if result.get("tf_state_json"):
        config_service.set("portainer_ui_web_jump_tfstate", result["tf_state_json"])
    return {"web_jump_id": result.get("web_jump_id"),
            "vault_account_id": result.get("vault_account_id"),
            "jump_group": jump_group, "jumpoint": jumpoint, "reused": False}


async def remove_portainer_ui_web_jump() -> None:
    """Destroy the Portainer-UI PRA Web Jump (best-effort) and clear its config.
    Called from node teardown."""
    from . import terraform_pra_service as pra
    state = config_service.get("portainer_ui_web_jump_tfstate")
    if state:
        try:
            await pra.remove_web_jump(state)
        except Exception as exc:
            logger.warning("Portainer UI web-jump removal failed (non-fatal): %s", exc)
    config_service.set("portainer_ui_web_jump_id", "")
    config_service.set("portainer_ui_web_jump_tfstate", "")
    config_service.set("portainer_ui_vault_account_id", "")


def _admin_password_hash(password: str) -> str:
    """bcrypt hash of ``password`` for Portainer's ``--admin-password``.

    Round-tripped before it is handed out: a hash Portainer can't verify would produce
    a node that boots fine and rejects the only password we know, which is worse than
    failing here. Returns ``""`` when bcrypt is unavailable, so the caller falls back
    to the first-run init endpoint."""
    try:
        import bcrypt
    except ImportError:
        logger.warning("bcrypt is not installed — the Portainer node will fall back to "
                       "first-run API initialization, which races Portainer's init window.")
        return ""
    raw = password.encode()
    hashed = bcrypt.hashpw(raw, bcrypt.gensalt()).decode()
    if not bcrypt.checkpw(raw, hashed.encode()):
        raise RuntimeError("bcrypt produced a hash it cannot verify — refusing to launch a "
                           "Portainer node nobody could log into")
    return hashed


async def _bootstrap(db, job_id: str, url: str, password: str,
                     *, state_preexisting: bool = False) -> tuple[str, str]:
    """Sign in as the admin and mint an API token; initialize the admin if needed.

    Returns ``(pat, note)``. ``note`` is a human-readable caveat for the job result
    (empty on the clean path).

    ``state_preexisting`` marks a Portainer DB that outlived the VM (a durable data
    disk). Such a DB may already hold a token minted under the default description, so
    the new one is given a distinct description rather than colliding with it.

    Login comes FIRST because a node launched by this dashboard initializes its admin
    at container start (``--admin-password``) — there is nothing left to init. The
    first-run endpoint stays as the fallback for a node created before that, or one
    launched without a usable bcrypt hash.

    Propagates :class:`PortainerInitWindowClosed`: that node has no admin at all, so a
    hand-supplied PAT can't rescue it and reporting a note would hide a dead node."""
    job_service.update_progress(db, job_id, 75, "Signing in as the admin user")
    try:
        jwt = await portainer_service.login(url, _ADMIN_USERNAME, password)
    except portainer_service.PortainerInitWindowClosed:
        raise
    except portainer_service.PortainerError as exc:
        logger.info("Portainer admin login failed (%s) — trying first-run initialization", exc)
        job_service.update_progress(db, job_id, 78, "Creating the admin user")
        try:
            await portainer_service.init_admin(url, _ADMIN_USERNAME, password)
        except portainer_service.PortainerAlreadyInitialized as already:
            logger.warning("Portainer first-run init unavailable: %s", already)
            return "", ("The node already had an admin user with a different password, so no "
                        "API token could be minted automatically. Add a Portainer API token "
                        "in Settings → Containers.")
        jwt = await portainer_service.login(url, _ADMIN_USERNAME, password)

    job_service.update_progress(db, job_id, 85, "Minting an API token")
    if state_preexisting:
        import time
        pat = await portainer_service.create_access_token(
            url, jwt, password, description=f"vm-dashboard-{int(time.time())}")
    else:
        pat = await portainer_service.create_access_token(url, jwt, password)
    return pat, ""


async def _launch_node(cloud: str, p: dict, *, admin_password_hash: str) -> dict:
    """Create (or reuse/start) the node VM on ``cloud`` and return
    ``{external_ip, internal_ip, url, zone, reused, data_disk_reused}``.

    Per-cloud because the container-launch mechanism has no common API: GCE reads a
    ``gce-container-declaration`` at boot, AWS runs ``docker run`` from EC2 user-data
    on a Docker-bearing AMI, Azure does the same from cloud-init. The *shape* is
    identical everywhere — unprivileged, no Docker socket, host ports 9443/8000, and
    the admin hash handed over at launch so the node comes up already initialized.
    """
    if cloud == "gcp":
        return await gcp_service.run_gce_portainer(
            p["project_id"], p["zone"], p["name"], p["image"],
            network=p["network"], subnetwork=p["subnetwork"],
            machine_type=p["machine_type"], boot_disk_gb=p["boot_disk_gb"],
            network_tag=p["network_tag"], create_external_ip=True, region=p["region"],
            admin_password_hash=admin_password_hash,
            data_disk_name=p["data_disk_name"], data_disk_gb=p["data_disk_gb"])
    if cloud == "aws":
        return await _launch_node_aws(p, admin_password_hash=admin_password_hash)
    raise managed_node_service.unsupported(cloud, _SPEC, "node launch")


async def _node_security_group_id(p: dict) -> str:
    """The id of the node's own security group, which the ingress refresh created just
    before this launch.

    Looked up rather than threaded through because the refresh is reached by several
    paths (deploy, gateway churn, cluster provision) and only one of them is a launch.
    A miss is fatal and says so: launching into the VPC's default group would put the
    node behind whatever rules that group happens to carry, which is the opposite of
    source-restricted.
    """
    name = _firewall_name(p["name"])
    sg_id = await aws_service.find_security_group_id(
        p["region"], vpc_id=p["vpc_id"], name=name)
    if not sg_id:
        raise managed_node_service.ManagedNodeError(
            f"The security group {name!r} does not exist in {p['vpc_id']}, so there is "
            f"no source-restricted group to launch the node into. The ingress refresh "
            f"should have created it -- check the job log above for why it did not.")
    return sg_id


async def _launch_node_aws(p: dict, *, admin_password_hash: str) -> dict:
    """Find-or-create the Portainer node as an EC2 instance running one container.

    The durable-state path is the interesting half. An existing EBS volume cannot be
    attached by ``run_instances``, so the order is: ensure the volume (which pins the
    AZ), launch with user-data that WAITS for it, attach once the instance is running.
    The wait is what makes it safe -- without it Portainer would start against an
    unmounted /data and write its database to the root volume, losing it on the next
    recreate. That is the same guarantee konlet gives on GCE, reconstructed.
    """
    region = p["region"]
    existing = await aws_service.list_ec2_container_nodes(region, _SPEC.feature)
    live = [i for i in existing if i.get("status") in ("PENDING", "RUNNING")]

    volume = {}
    if p["data_disk_name"]:
        volume = await aws_service.find_node_data_volume(region, p["data_disk_name"])

    if live:
        node = live[0]
        logger.info("Portainer node: reusing EC2 instance %s (%s)",
                    node.get("instance_id"), node.get("name"))
        # A reused instance already has whatever volume it was launched with; the
        # attach is idempotent and returns immediately when it is already ours.
        if volume.get("volume_id"):
            await aws_service.attach_node_data_volume(
                region, volume_id=volume["volume_id"],
                instance_id=node["instance_id"])
        return {**node, "reused": True, "data_disk_reused": bool(volume)}

    data_disk_reused = bool(volume)
    if p["data_disk_name"] and not volume:
        # A fresh volume needs an AZ, and the subnet's is the only one the instance can
        # land in -- asking the subnet is what keeps the two in the same zone.
        az = await aws_service.subnet_availability_zone(region, p["subnet_id"])
        volume = await aws_service.ensure_node_data_volume(
            region, name=p["data_disk_name"], az=az, size_gb=p["data_disk_gb"])

    volumes = [(aws_service.NODE_DATA_MOUNT, "/data")] if volume else []
    user_data = aws_service.container_node_user_data(
        managed_node_service.docker_run_command(
            p["image"], name=_SPEC.feature, ports=_SPEC.ports,
            volumes=volumes,
            args=(["--admin-password", admin_password_hash]
                  if admin_password_hash else ())),
        data_volume_id=volume.get("volume_id", ""))
    ami_id = await aws_service.get_ssm_parameter(region, aws_service.ECS_OPTIMIZED_AMI_SSM)
    inst = await aws_service.run_ec2_container_node(
        region, ami_id=ami_id, instance_type=p["instance_type"],
        subnet_id=p["subnet_id"], security_group_ids=[await _node_security_group_id(p)],
        user_data=user_data, name_tag=p["name"], purpose=_SPEC.feature,
        root_disk_gb=p["boot_disk_gb"])
    if volume.get("volume_id"):
        await aws_service.attach_node_data_volume(
            region, volume_id=volume["volume_id"], instance_id=inst["instance_id"])
    node = await aws_service.await_container_node(region, inst["instance_id"],
                                                 _SPEC.feature)
    return {**node, "reused": False, "data_disk_reused": data_disk_reused}


async def _stop_node(cloud: str, p: dict, *, name: str, zone: str,
                     delete_firewall: bool = False, data_disk_name: str = "",
                     delete_data_disk: bool = False) -> None:
    """Delete the node VM on ``cloud``, optionally taking its ingress rule and its
    durable data volume with it."""
    if cloud == "gcp":
        await gcp_service.stop_gce_portainer(
            p["project_id"], zone, name,
            delete_firewall=delete_firewall,
            firewall_name=_firewall_name(name) if delete_firewall else None,
            data_disk_name=data_disk_name, delete_data_disk=delete_data_disk)
        return
    if cloud == "aws":
        for node in await aws_service.list_ec2_container_nodes(p["region"], _SPEC.feature):
            if node.get("name") == name and node.get("instance_id"):
                await aws_service.terminate_instance(p["region"], node["instance_id"])
        if delete_firewall:
            # Revoke first (immediate, and what closes the node), then reclaim the empty
            # group once the terminating instance releases its ENI -- see the Rancher
            # teardown for why an orphaned group is not merely untidy.
            await aws_service.ensure_node_security_group(
                p["region"], vpc_id=p["vpc_id"], name=_firewall_name(name),
                ports=list(_SPEC.ports), source_cidrs=[])
            await aws_service.release_node_security_group(
                p["region"], vpc_id=p["vpc_id"], name=_firewall_name(name))
        # The volume delete comes LAST and only when asked: it cannot happen until the
        # instance has released it, and it is the one part of a teardown that cannot
        # be undone.
        if delete_data_disk and data_disk_name:
            vol = await aws_service.find_node_data_volume(p["region"], data_disk_name)
            if vol.get("volume_id"):
                await aws_service.delete_node_data_volume(p["region"], vol["volume_id"])
        return
    raise managed_node_service.unsupported(cloud, _SPEC, "node teardown")


async def _find_data_disk(cloud: str, p: dict) -> dict:
    """The node's durable data volume if it already exists, else ``{}``.

    Read BEFORE launching: a volume that already holds an initialized Portainer DB
    ignores ``--admin-password``, so a deploy that has no stored password for it
    would produce a node nobody can sign into.
    """
    if cloud == "gcp":
        return await gcp_service.find_portainer_data_disk(
            p["project_id"], p["data_disk_name"])
    if cloud == "aws":
        return await aws_service.find_node_data_volume(p["region"], p["data_disk_name"])
    raise managed_node_service.unsupported(cloud, _SPEC, "durable data volumes")


async def _relocate_across_clouds(db, job_id: str, target_cloud: str) -> None:
    """Delete the node in its PREVIOUS cloud when the operator moved it.

    One Portainer server is the whole model, so two live nodes is never the intent —
    and a stranded one keeps billing while Edge agents keep dialling an address the
    dashboard has stopped tracking. Best-effort: if the old cloud cannot be reached we
    say so and carry on, because refusing would leave the operator unable to move.
    """
    prev_cloud = _node_cloud()
    if prev_cloud == target_cloud:
        return
    logger.info("Relocating Portainer: %s to %s", prev_cloud, target_cloud)
    job_service.update_progress(db, job_id, 20, f"Relocating from {prev_cloud}")
    try:
        prev = _node_params(cloud=prev_cloud)
        for node in await managed_node_service.list_nodes(prev_cloud, _SPEC, prev):
            await _stop_node(prev_cloud, prev, name=node.get("name") or prev["name"],
                             zone=node.get("zone") or prev["zone"], delete_firewall=True,
                             data_disk_name=prev["data_disk_name"], delete_data_disk=False)
    except Exception as exc:  # noqa: BLE001 -- never block a move on the old cloud
        logger.warning("Portainer cross-cloud relocation: deleting the %s node failed "
                       "(continuing; it may need removing by hand): %s", prev_cloud, exc)


async def run_deploy(db, *, job_id: str, meta: dict) -> None:
    """Deploy (or reuse) the Portainer node: firewall → VM → wait ready →
    first-run admin → mint token → wire the integration config."""
    try:
        job_service.set_running(db, job_id)
        # Deploy-time cloud + region/zone pick (blank → the persisted node cloud /
        # region, else the configured default). Selects the node's region-specific
        # subnet/zone within the chosen cloud.
        cloud = (meta.get("cloud") or "").strip().lower() or _node_cloud()
        if cloud not in managed_node_service.CLOUDS:
            job_service.set_failed(
                db, job_id,
                f"{cloud!r} is not a cloud the Portainer node can run on — "
                f"choose one of {', '.join(managed_node_service.CLOUDS)}.")
            return
        p = _node_params(region=meta.get("region"), zone=meta.get("zone"), cloud=cloud)
        if not p["account"]:
            job_service.set_failed(
                db, job_id,
                f"{cloud.upper()} is not configured, so the Portainer node has nowhere to "
                f"go. Set it up under Settings → {cloud.upper()} (or pick a different "
                f"cloud on the deploy form).")
            return

        # Persist the deploy-form PRA choices to config FIRST, so the firewall merge
        # (_jumpoint_cidrs gates on portainer_ui_web_jump_enabled) and the later
        # Web-Jump provisioning all honor this deploy's picks. Only keys the operator
        # actually sent are written, so a bare redeploy keeps the existing Settings.
        if "web_jump_enabled" in meta:
            config_service.set("portainer_ui_web_jump_enabled",
                               "1" if meta["web_jump_enabled"] else "0")
        if meta.get("jump_group"):
            config_service.set("portainer_ui_jump_group", str(meta["jump_group"]))
        if meta.get("jumpoint_name"):
            config_service.set("portainer_ui_jumpoint_name", str(meta["jumpoint_name"]))
        if meta.get("vault_account_group_id"):
            config_service.set("portainer_ui_vault_account_group_id",
                               str(meta["vault_account_group_id"]))

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
                f"portainer_allowed_source_cidrs in Settings → Containers — or enable "
                f"{_SPEC.allow_open_key(cloud)} — then redeploy.")
            return

        # Moving the node to a DIFFERENT cloud deletes the old one first, for the same
        # reason a region move does: one server, and a stranded node keeps billing
        # while answering on an address nothing tracks any more.
        await _relocate_across_clouds(db, job_id, cloud)

        # Single relocatable node. If a node already lives in the TARGET region, reuse
        # that exact zone (the launcher starts/returns it). If one lives in a DIFFERENT
        # region, delete it first so we never strand a duplicate there (the node is
        # ephemeral — state re-bootstraps).
        target_region = p["region"]
        try:
            existing_nodes = await managed_node_service.list_nodes(cloud, _SPEC, p)
        except Exception as exc:
            logger.warning("Portainer relocation check failed (continuing): %s", exc)
            existing_nodes = []
        for node in existing_nodes:
            nzone = node.get("zone") or ""
            if region_catalog.region_from_zone(nzone) == target_region:
                p["zone"] = nzone   # reuse the live in-region node's exact zone
            elif p["data_disk_name"]:
                # A persistent disk is zonal: it cannot follow the node to another
                # region. Deleting the VM here would leave the launcher to pin straight
                # back to the disk's zone, so the "relocation" would silently not
                # happen. Refuse instead of pretending.
                job_service.set_failed(
                    db, job_id,
                    f"The Portainer node is in {region_catalog.region_from_zone(nzone)} "
                    f"and durable state is enabled, so it cannot be moved to "
                    f"{target_region}: its data disk '{p['data_disk_name']}' is zonal and "
                    f"cannot be attached in another region. Move it with a disk snapshot, "
                    f"or tear the node down with 'delete the data disk' to rebuild in "
                    f"{target_region} from scratch.")
                return
            else:
                logger.info("Relocating Portainer: deleting node '%s' in %s → region %s",
                            node.get("name"), nzone, target_region)
                job_service.update_progress(db, job_id, 25, f"Relocating to {target_region}")
                try:
                    await _stop_node(cloud, p, name=node.get("name") or p["name"],
                                     zone=nzone)
                except Exception as exc:
                    logger.warning("Failed to delete old-region Portainer node (continuing): %s", exc)

        # Settle the admin credential BEFORE the VM exists. Portainer only accepts its
        # first-run init endpoint for a short window after the container starts and then
        # fences off the entire API ("administrator initialization timeout") — a race the
        # deploy loses whenever the image pull or readiness poll runs long, leaving a node
        # NOBODY can log into. Passing the hash at launch makes the node come up already
        # initialized, so there is no window to lose.
        password = config_service.get("portainer_admin_password") or ""
        generated = False
        if not password:
            password = _generate_admin_password()
            generated = True
        pw_hash = _admin_password_hash(password)

        # With a persistent data disk the DB can outlive the VM, and Portainer ignores
        # --admin-password once an admin exists. Generating one here for a disk that
        # already holds an admin would bake in a password that was never real, so check
        # BEFORE launching and fail with the remedy instead of deploying a node we
        # cannot sign into.
        if p["data_disk_name"] and generated:
            prior = await _find_data_disk(cloud, p)
            if prior:
                job_service.set_failed(
                    db, job_id,
                    f"The Portainer data disk '{p['data_disk_name']}' already exists in "
                    f"{prior['zone']} and holds an admin user, but no admin password is "
                    f"stored in Settings — Portainer ignores --admin-password on a disk "
                    f"that is already initialized, so the node would come up with a "
                    f"password nobody knows. Set portainer_admin_password in Settings → "
                    f"Containers to the password that disk was created with, or delete "
                    f"the disk to start clean.")
                return

        job_service.update_progress(db, job_id, 30, "Launching the node VM")
        res = await _launch_node(cloud, p, admin_password_hash=pw_hash)
        # True when Portainer's DB pre-dates this launch: a reused VM, or a fresh VM
        # that picked up an existing data disk. Both mean "do not treat the credential
        # we just computed as authoritative".
        state_preexisting = bool(res.get("reused") or res.get("data_disk_reused"))
        external_ip = res.get("external_ip") or ""
        url = res.get("url") or ""
        if not external_ip:
            job_service.set_failed(db, job_id, "Portainer VM has no external IP — cannot reach it.")
            return

        # Persist the ACTUAL deployed zone so teardown + bare redeploys stay sticky to
        # the (possibly relocated / auto-picked) region.
        managed_node_service.set_node_cloud(_SPEC, cloud)
        deployed_zone = res.get("zone") or p["zone"]
        if deployed_zone:
            config_service.set(_SPEC.infra_key(cloud, "zone"), deployed_zone)
        # portainer_url is the key the EXISTING integration reads, so writing it here
        # is what makes the Containers page work with no manual Settings step. The node
        # serves a self-signed cert on :9443, so TLS verification must be off for it.
        config_service.set("portainer_url", url)
        config_service.set("portainer_verify_ssl", "0")

        # A fresh VM was created WITH this password baked in, so persist it now — the
        # credential is real from the moment the container starts, and losing it would
        # leave a node we can't log into even though its admin exists.
        if pw_hash and not state_preexisting:
            config_service.set("portainer_admin_password", password)
            config_service.set("portainer_admin_password_generated", "1" if generated else "0")

        job_service.update_progress(db, job_id, 55, "Waiting for Portainer to start")
        try:
            ready = await portainer_service.wait_ready(url, _ready_timeout_s())
        except portainer_service.PortainerInitWindowClosed as exc:
            job_service.set_failed(db, job_id, f"{exc} {_LOCKED_NODE_REMEDY}")
            return
        if not ready:
            job_service.set_failed(
                db, job_id,
                f"Portainer did not start serving at {url} within {_ready_timeout_s()}s. "
                f"The VM is running — check that the firewall admits the dashboard's egress "
                f"IP, then redeploy.")
            return

        note = ""
        existing_pat = config_service.get("portainer_pat")
        if state_preexisting and existing_pat:
            # The DB that issued this token is still there — a reused VM, or a fresh VM
            # back on its data disk — so the token is still valid and there is nothing
            # to mint. Preferring it also avoids re-minting under the same fixed
            # description on every durable redeploy.
            job_service.update_progress(db, job_id, 85, "Reusing the existing API token")
            pat = existing_pat
        else:
            try:
                pat, note = await _bootstrap(db, job_id, url, password,
                                             state_preexisting=state_preexisting)
            except portainer_service.PortainerInitWindowClosed as exc:
                job_service.set_failed(db, job_id, f"{exc} {_LOCKED_NODE_REMEDY}")
                return

        if pat:
            config_service.set("portainer_pat", pat)

        # Provision the PRA Web Jump AFTER bootstrap, so the admin password exists to
        # vault. Best-effort — a PRA hiccup must not fail the node deploy. Runs on
        # fresh + reused nodes so an ephemeral jumpoint IP is re-synced either way.
        if config_service.get_bool("portainer_ui_web_jump_enabled", False):
            job_service.update_progress(db, job_id, 92, "Provisioning PRA Web Jump")
            try:
                await register_portainer_ui_web_jump(db)
            except Exception as exc:
                logger.warning("Portainer Web Jump provisioning failed (non-fatal): %s", exc)

        completion = {
            "url": url,
            "external_ip": external_ip,
            "internal_ip": res.get("internal_ip") or "",
            "cloud": cloud,
            "zone": deployed_zone,
            "region": p["region"],
            "name": p["name"],
            "reused": bool(res.get("reused")),
            "token_configured": bool(pat),
        }
        # Surface the generated password ONLY when it isn't vaulted — with a PRA Vault
        # account the operator uses the portainer-ui Web Jump and never sees it.
        if config_service.get("portainer_ui_vault_account_id"):
            completion["admin_credential"] = (
                "stored in PRA Vault — use the portainer-ui Web Jump")
        elif pat and config_service.get_bool("portainer_admin_password_generated", False):
            completion["admin_username"] = _ADMIN_USERNAME
            completion["admin_password"] = config_service.get("portainer_admin_password") or ""
        if note:
            completion["note"] = note
        job_service.set_completed(db, job_id, completion)
    except managed_node_service.ManagedNodeError as exc:
        # A placement/support problem, not a crash: the message already names the
        # config key to set, so a traceback would only bury it.
        logger.warning("Portainer node deploy: %s", exc)
        job_service.set_failed(db, job_id, str(exc))
    except Exception as exc:
        logger.exception("Portainer node deploy failed")
        job_service.set_failed(db, job_id, str(exc))


async def run_teardown(db, *, job_id: str, meta: dict) -> None:
    """Delete the Portainer node VM + its firewall rule, then clear the runtime
    config it populated. The node is ephemeral, so this discards all Portainer
    state (users, environments, settings)."""
    try:
        job_service.set_running(db, job_id)
        # Teardown follows the node, not the form: the persisted cloud is where it
        # actually is. An explicit meta cloud is honoured so a stranded node in a
        # cloud we have since moved away from can still be reaped.
        cloud = (meta.get("cloud") or "").strip().lower() or _node_cloud()
        p = _node_params(cloud=cloud)
        if not p["account"]:
            job_service.set_failed(
                db, job_id,
                f"{cloud.upper()} is not configured, so the Portainer node cannot be "
                f"reached to tear it down.")
            return

        name = meta.get("name") or p["name"]
        zone = (meta.get("zone") or config_service.get(_SPEC.infra_key(cloud, "zone"))
                or p["zone"])
        if cloud == "gcp" and not zone:
            # GCE addresses a VM BY zone, so without one there is nothing to delete.
            # Elsewhere the node is found by tag and the zone is only a record, so a
            # blank one is not a reason to refuse.
            job_service.set_failed(
                db, job_id,
                "No zone is known for the Portainer node — pass ?zone= to the stop call.")
            return

        # Remove the PRA Web Jump first (best-effort) — it points at a URL that is
        # about to stop existing.
        if config_service.get("portainer_ui_web_jump_tfstate"):
            job_service.update_progress(db, job_id, 15, "Removing PRA Web Jump")
            try:
                await remove_portainer_ui_web_jump()
            except Exception as exc:
                logger.warning("Portainer PRA web-jump removal failed (continuing): %s", exc)

        # Deleting the data disk is opt-in per teardown: it is the only copy of the
        # node's users, environments and settings, and it is the one part of a teardown
        # that cannot be undone.
        delete_data_disk = bool(meta.get("delete_data_disk"))
        data_disk_name = p["data_disk_name"]

        job_service.update_progress(db, job_id, 30, f"Deleting {name} in {zone}")
        await _stop_node(cloud, p, name=name, zone=zone, delete_firewall=True,
                         data_disk_name=data_disk_name,
                         delete_data_disk=delete_data_disk)

        job_service.update_progress(db, job_id, 80, "Clearing Portainer configuration")
        # Drop everything the deploy populated. The URL goes too: leaving it would point
        # the Containers page at a VM that no longer exists.
        cleared = ["portainer_url", _SPEC.infra_key(cloud, "zone"),
                   "portainer_ui_web_jump_id", "portainer_ui_web_jump_tfstate",
                   "portainer_ui_vault_account_id", "portainer_ui_jumpoint_egress_ip"]
        # The admin password and PAT live in Portainer's DB, so they survive exactly as
        # long as the data disk does. Clearing them alongside a PRESERVED disk is what
        # would break the next deploy: Portainer ignores --admin-password on an
        # already-initialized DB, so a regenerated password could never sign in.
        state_survives = bool(data_disk_name) and not delete_data_disk
        if not state_survives:
            cleared += ["portainer_pat", "portainer_admin_password",
                        "portainer_admin_password_generated"]
        for key in cleared:
            try:
                config_service.set(key, "")
            except Exception as exc:
                logger.warning("Failed to clear config key '%s' (continuing): %s", key, exc)

        result = {"name": name, "zone": zone, "cloud": cloud, "deleted": True,
                  "data_disk_deleted": bool(delete_data_disk and data_disk_name)}
        if state_survives:
            result["note"] = (
                f"The data disk '{data_disk_name}' was kept, so the admin credential and "
                f"API token are still valid — the next deploy reattaches it and comes "
                f"back with its users, environments and settings. It keeps costing "
                f"storage until it is deleted.")
        job_service.set_completed(db, job_id, result)
    except managed_node_service.ManagedNodeError as exc:
        # A placement/support problem, not a crash: the message already names the
        # config key to set, so a traceback would only bury it.
        logger.warning("Portainer node teardown: %s", exc)
        job_service.set_failed(db, job_id, str(exc))
    except Exception as exc:
        logger.exception("Portainer node teardown failed")
        job_service.set_failed(db, job_id, str(exc))
