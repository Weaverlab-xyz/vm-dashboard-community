"""
Shared BeyondTrust Gateway host — on-demand EC2 lifecycle.

The tunnel-capable Gateway runs on an ECS-on-EC2 container instance (Fargate
can't do protocol tunneling — see aws_service / config bt_ecs_launch_type). To
avoid a standing ~$15/mo host, the dashboard manages ONE shared host's lifecycle
by reference count:

  * ensure_jumpoint_host(region)          — called when an AWS EC2 instance, a
                                            cloud database, or a managed K8s cluster's
                                            PRA tunnel is provisioned. Creates the host
                                            (idempotent, tag find-or-create) and the
                                            gateway task on it.
  * teardown_jumpoint_host_if_idle(db, …) — called on EC2 destroy / DB decommission /
                                            K8s tunnel removal. Terminates the host only
                                            when nothing (no managed EC2 instance, no
                                            active provisioned DB, no tunneled K8s
                                            cluster) is left using it.

Prereqs (one-time, created by scripts/sandbox/Linux/setup-aws.sh): the
``ecsInstanceRole`` + instance profile — carrying AmazonEC2ContainerServiceforEC2Role,
without which the host's agent cannot join the cluster and no task can ever be placed
— the bt-jumpoint cluster, the public subnet + gateway SG, and the dashboard IAM user's
ssm:GetParameter* / iam:PassRole(ecsInstanceRole) / ecs:*ContainerInstances. Everything
here is best-effort from the caller's perspective; failures log and leave the
DB/EC2 resource intact (the tunnel/jump is just unavailable until fixed).
"""
import asyncio
import logging
import re
import time
from typing import Optional

logger = logging.getLogger(__name__)

_REGISTER_TIMEOUT_S = 180   # wait for the EC2 host to register as an ECS container instance
_REGISTER_POLL_S = 10


class GatewayHostError(Exception):
    """A gateway host could not be made ready to run the Gateway task.

    Local rather than ``aws_service.AWSError`` so this module keeps its property of
    having no import-time cloud dependencies (every cloud import here is
    function-local). Callers catch broadly, so the type costs them nothing — what they
    want from it is the message.
    """


def _cfg(key: str) -> str:
    from . import config_service
    val = config_service.get(key)
    if val:
        return val
    from ..config import settings
    return getattr(settings, key, "") or ""


def _persist_jumpoint_egress_ip(ip: Optional[str], cloud: str = "", name: str = "",
                                managed: bool = True) -> None:
    """Record a Gateway host's egress IP so a node firewall can auto-allow it.

    Two sinks, deliberately:

    * the gateway's own registry row (``Gateway.egress_ip``), which is what
      ``{rancher,portainer}_node_service._jumpoint_cidrs`` reads — EVERY live gateway
      in the cloud, because they all join one PRA Gateway cluster and any of them may
      broker a given session;
    * the ``*_ui_jumpoint_egress_ip`` config keys, the single-slot "last known shared
      gateway" the firewalls have always folded in. Only the MANAGED gateway writes
      these: a user-deployed gateway overwriting them made the two features disagree
      about which host they were configured for.

    Best-effort; only overwrites when we actually learned an IP, so an Azure ensure
    (no public IP) never clobbers a good GCP/AWS value."""
    if not ip:
        return
    if cloud and name:
        try:
            from ..database import SessionLocal
            from . import gateway_service
            db = SessionLocal()
            try:
                gateway_service.record_egress_ip(db, cloud, name, ip)
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("jumpoint-host: recording the gateway's egress IP failed "
                           "(non-fatal): %s", exc)
    if not managed:
        return
    try:
        from . import config_service
        for key, label in (("rancher_ui_jumpoint_egress_ip", "Rancher"),
                           ("portainer_ui_jumpoint_egress_ip", "Portainer")):
            if config_service.get(key) != ip:
                config_service.set(key, ip)
                logger.info("jumpoint-host: recorded Web-Jump Gateway egress IP %s for the %s firewall",
                            ip, label)
    except Exception as exc:
        logger.warning("jumpoint-host: persisting egress IP failed (non-fatal): %s", exc)


def _record_placement(placement: Optional[dict], zone: str, ip: Optional[str]) -> None:
    """Fill a caller's ``placement`` out-dict with where the host landed. Only sets
    what we learned, so a cloud with no zone (AWS) or no public IP (Azure) leaves the
    caller's existing value alone."""
    if placement is None:
        return
    if zone:
        placement["zone"] = zone
    if ip:
        placement["egress_ip"] = ip


def _clear_managed_egress_ip(cloud: str, name: str) -> None:
    """Forget a torn-down managed gateway: mark its registry row deleted and drop the
    remembered shared /32.

    Load-bearing for the firewalls, not tidiness. The remembered IP outlived the host
    it described, so every subsequent refresh kept re-applying a rule that admitted a
    VM that no longer existed — while the gateway that replaced it went unallowed."""
    try:
        from ..database import SessionLocal
        from . import config_service, gateway_service
        db = SessionLocal()
        try:
            gateway_service.mark_deleted(db, cloud, name)
        finally:
            db.close()
        for key in ("rancher_ui_jumpoint_egress_ip", "portainer_ui_jumpoint_egress_ip"):
            if config_service.get(key):
                config_service.set(key, "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("jumpoint-host: clearing the torn-down gateway's egress IP failed "
                       "(non-fatal): %s", exc)


def _ui_jumpoint_region(cloud: str) -> str:
    if cloud == "gcp":
        return _cfg("gcp_region") or ""
    if cloud == "azure":
        return _cfg("azure_location") or ""
    return _cfg("aws_region") or _cfg("aws_default_region") or ""


async def ensure_rancher_ui_jumpoint() -> Optional[str]:
    """Best-effort: ensure the dashboard-managed Gateway host that brokers the
    Rancher-UI Web Jump is up, capture its egress IP into
    ``rancher_ui_jumpoint_egress_ip``, and return it.

    Cloud is picked by ``rancher_ui_jumpoint_cloud`` (default ``gcp`` — the same cloud
    the node has historically run on). All three managed hosts expose a knowable egress
    IP: GCP and AWS via the host's public IP, Azure via a Standard, secure-by-default
    public IP on its NIC. A pre-existing operator Gateway (not dashboard-provisioned)
    still can't be auto-detected — add its IP to ``rancher_allowed_source_cidrs``."""
    from . import config_service
    cloud = (_cfg("rancher_ui_jumpoint_cloud") or "gcp").lower()
    try:
        await ensure_jumpoint_host(cloud, _ui_jumpoint_region(cloud))
    except Exception as exc:
        logger.warning("rancher-ui gateway: ensure failed (non-fatal): %s", exc)
    return config_service.get("rancher_ui_jumpoint_egress_ip") or None


async def ensure_portainer_ui_jumpoint() -> Optional[str]:
    """Best-effort: ensure the dashboard-managed Gateway host that brokers the
    Portainer-UI Web Jump is up, capture its egress IP into
    ``portainer_ui_jumpoint_egress_ip``, and return it.

    Same shape as :func:`ensure_rancher_ui_jumpoint` — the Gateway host itself is
    SHARED, so when both Web Jumps run on the same cloud this is a no-op that just
    re-reads the (possibly refreshed) IP. Cloud is picked by
    ``portainer_ui_jumpoint_cloud`` (default ``gcp``). All three managed hosts expose a
    knowable egress IP (see :func:`ensure_rancher_ui_jumpoint`); only a Gateway the
    dashboard did not provision has to be added to
    ``portainer_allowed_source_cidrs`` by hand."""
    from . import config_service
    cloud = (_cfg("portainer_ui_jumpoint_cloud") or "gcp").lower()
    try:
        await ensure_jumpoint_host(cloud, _ui_jumpoint_region(cloud))
    except Exception as exc:
        logger.warning("portainer-ui gateway: ensure failed (non-fatal): %s", exc)
    return config_service.get("portainer_ui_jumpoint_egress_ip") or None


async def _resolve_deploy_key() -> str:
    """BeyondTrust Gateway Docker deploy key — direct config field first, then
    the legacy Password-Safe title (same resolution as the EC2/RDS paths)."""
    direct = _cfg("aws_ecs_docker_deploy_key")
    if direct:
        return direct
    title = _cfg("bt_ps_deploy_key_title")
    if title:
        from . import btapi_service
        try:
            return await btapi_service.get_ps_secret(title)
        except Exception as exc:
            logger.warning("jumpoint-host: deploy key fetch from Password Safe failed: %s", exc)
    return ""


def _aws_region_cfg(region: str) -> dict:
    """Per-region gateway settings (cluster, subnet, security group).

    A gateway in us-east-2 needs that region's cluster and subnet, not the flat
    default — ``region_config`` already models this and falls back to the flat
    ``bt_ecs_*`` keys when no per-region config exists, so single-region setups are
    unaffected."""
    from .region_config import resolve_region
    return resolve_region("aws", region)


def _gateway_tasks(tasks: list, family: str) -> list:
    """The live gateway tasks among ``tasks``. Shared by the per-host lookup below and the
    batched map :func:`gateway_tasks_by_host` builds, so the rule for "is this task one of
    ours?" cannot drift between the two — the family match is the whole of that rule, and
    getting it wrong reads as every gateway serving nothing."""
    return [t for t in tasks
            if t.get("lastStatus") in ("PROVISIONING", "PENDING", "RUNNING")
            and f"task-definition/{family}:" in (t.get("taskDefinitionArn") or "")]


async def _live_gateway_tasks(region: str, cluster: str, family: str,
                              host_instance_id: str = "") -> list[dict]:
    """Live gateway tasks in ``cluster``, optionally only those running on one
    container instance.

    Scoping by host is what makes several gateways able to share a cluster: without
    it, "is a task already live?" is answered by *any* host's task, and "stop the
    tasks" stops everybody's.
    """
    from . import aws_service
    tasks = await aws_service.list_ecs_tasks(region, cluster)
    live = _gateway_tasks(tasks, family)
    if not host_instance_id:
        return live
    instances = await aws_service.list_container_instances(region, cluster)
    arns = {c["arn"] for c in instances if c.get("ec2_instance_id") == host_instance_id}
    return [t for t in live if t.get("containerInstanceArn") in arns]


async def _ensure_task(region: str, deploy_key: str, host_instance_id: str = "") -> None:
    """Run the gateway task on ``host_instance_id`` if none is live there (the host
    must already have capacity). With no host given, falls back to the historical
    cluster-wide check — the FARGATE path, which has no container instance."""
    from . import aws_service
    rc = _aws_region_cfg(region)
    cluster = rc["ecs_cluster"]
    family = _cfg("bt_ecs_task_family")
    launch_type = (_cfg("bt_ecs_launch_type") or "EC2").upper()
    live = await _live_gateway_tasks(region, cluster, family, host_instance_id)
    if live:
        logger.info("gateway-host: task already live (%d) in cluster %s%s",
                    len(live), cluster,
                    f" on {host_instance_id}" if host_instance_id else "")
        return
    arn = await aws_service.run_ecs_jumpoint_task(
        region=region, cluster=cluster, task_family=family,
        subnet_id=rc["jumpoint_subnet_id"],
        security_group_ids=[s.strip() for s in (rc["jumpoint_security_group_id"] or "").split(",") if s.strip()],
        deploy_key=deploy_key, cpu=_cfg("bt_ecs_cpu"), memory=_cfg("bt_ecs_memory"),
        execution_role_arn=_cfg("bt_ecs_execution_role_arn"), image=_cfg("bt_ecs_image"),
        launch_type=launch_type,
        host_instance_id=host_instance_id,
    )
    logger.info("gateway-host: started gateway task %s — registers with PRA in ~1-2 min",
                arn.split("/")[-1])


def managed_host_name(cloud: str) -> str:
    """The name of the *auto-ensured* shared gateway for ``cloud``.

    Split out because it is now a boundary, not just a config read: a user-deployed
    gateway must never be given this name, and the idle teardown must never act on a
    host that does not carry it."""
    if cloud == "gcp":
        return _gcp_jumpoint_name()
    if cloud == "azure":
        return _AZURE_JUMPOINT_VM_NAME
    return _cfg("bt_ecs_host_name") or "dashboard-sandbox-jumpoint-host"


async def ensure_jumpoint_host(cloud: str, region: str, name: str = "",
                               placement: Optional[dict] = None) -> Optional[str]:
    """Ensure a tunnel-capable Gateway host is up for ``cloud``; return its
    instance/host id (or None). Dispatches per cloud — AWS uses ECS-on-EC2, GCP uses
    a privileged container on a COS GCE VM. Best-effort for callers.

    ``name`` defaults to the shared auto-ensured gateway, which is what every
    existing caller gets. A caller passing a name is asking for *that* gateway host,
    which is how user-deployed gateways get more than one per cloud.

    Whether a name was given also decides ownership, and so who records it: no name
    means the managed gateway, which is adopted into the registry here because
    nothing else knows it happened. A named gateway was requested through a job that
    already owns its row.

    ``placement``, when given, is filled in with ``{zone, egress_ip}`` — where the host
    actually landed, which the caller can't infer: a blank zone is resolved here and a
    capacity-exhausted zone falls through to a sibling."""
    requested = bool(name)
    # Always collect the placement, even when the caller doesn't want it: the adoption
    # below needs the egress IP, and the managed row is CREATED there — so on a first
    # ensure there is no row for the ensure path's own write to land on.
    placement = {} if placement is None else placement
    if cloud == "gcp":
        host_id = await _ensure_jumpoint_host_gcp(region, name, placement=placement)
    elif cloud == "azure":
        host_id = await _ensure_jumpoint_host_azure(region, name, placement=placement)
    else:
        host_id = await _ensure_jumpoint_host_aws(region, name, placement=placement)
    if host_id and not requested:
        _adopt_managed_row(cloud, region, host_id,
                           egress_ip=placement.get("egress_ip", ""))
    return host_id


def _adopt_managed_row(cloud: str, region: str, host_id: str, egress_ip: str = "") -> None:
    """Record the auto-ensured gateway in the registry, so the Gateways page shows it
    beside the requested ones. Owns its own session — callers of ensure_jumpoint_host
    are mid-deploy and hold sessions of their own. Best-effort by contract: an
    inventory write must never be what fails a deploy."""
    try:
        from ..database import SessionLocal
        from . import gateway_service
        db = SessionLocal()
        try:
            gateway_service.adopt_managed(db, cloud, region, managed_host_name(cloud),
                                          host_id=host_id, egress_ip=egress_ip)
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("gateway-host: registering the managed gateway failed "
                       "(non-fatal): %s", exc)


async def _await_ecs_registration(region: str, host_id: str) -> None:
    """Block until ``host_id`` is an ACTIVE container instance, or the timeout.

    Both the create and the REUSE path need this. A tag lookup matches instances in
    `pending` as well as `running`, and an EC2 host that is merely running has not
    necessarily had its ECS agent join the cluster yet — so "found a host" is not the
    same as "the cluster can place a task". Calling RunTask in that window fails with
    `InvalidParameterException: No Container Instances were found in your cluster`.
    Reuse skipped this wait and was the common way to hit it: any deploy landing while
    another had just launched the host got no Gateway at all.

    Scoped to the named host rather than "any ACTIVE instance in the cluster": with
    several gateways sharing a cluster, a neighbour being ready says nothing about
    this host, and the task is placement-constrained to this host anyway — so waiting
    on someone else's readiness would just move the RunTask failure later.

    Costs nothing on the healthy path — an already-registered host returns ACTIVE on
    the first poll.

    Raises :class:`GatewayHostError` on timeout instead of letting the caller fire a
    RunTask that cannot succeed. Timing out here is not a race that one more attempt
    might win: the host is up, the cluster is readable, and the host still is not in
    it — overwhelmingly because the agent's `RegisterContainerInstance` was denied,
    which the agent treats as terminal and exits on. Trying anyway just swapped a
    diagnosable condition for `InvalidParameterException: No Container Instances`,
    which names neither the host nor the permission.

    A *listing* that never succeeds is the one case that still falls through to the
    attempt: not knowing whether the host registered is different from knowing it did
    not, and RunTask may well work. Transient listing failures no longer end the wait
    at all — returning on the first one meant one throttled call sent the deploy
    straight to the doomed RunTask it was supposed to prevent.
    """
    from . import aws_service
    cluster = _aws_region_cfg(region)["ecs_cluster"]
    deadline = time.monotonic() + _REGISTER_TIMEOUT_S
    listed = False        # did any poll actually read the cluster?
    last_exc = None
    while True:
        try:
            ci = await aws_service.list_container_instances(region, cluster)
            listed, last_exc = True, None
            if any(c.get("status") == "ACTIVE" and c.get("ec2_instance_id") == host_id
                   for c in ci):
                return
        except Exception as exc:  # noqa: BLE001 — keep polling; the deadline is the bound
            last_exc = exc
            logger.warning("gateway-host: listing container instances failed "
                           "(still waiting on %s): %s", host_id, exc)
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(_REGISTER_POLL_S)

    if not listed:
        logger.warning("gateway-host: could not read cluster %s to confirm %s registered "
                       "(last error: %s) — attempting the task anyway",
                       cluster, host_id, last_exc)
        return
    raise GatewayHostError(
        f"Gateway host {host_id} did not register with the ECS cluster {cluster!r} "
        f"within {_REGISTER_TIMEOUT_S}s, so no Gateway task can be placed on it. The "
        f"usual cause is the host's instance profile "
        f"({_cfg('bt_ecs_host_instance_profile') or 'ecsInstanceRole'}) missing "
        f"ecs:RegisterContainerInstance — attach the AWS-managed "
        f"AmazonEC2ContainerServiceforEC2Role policy to that role. The ECS agent treats "
        f"the denial as terminal and exits, so the host stays out of the cluster "
        f"permanently; /var/log/ecs/ecs-agent.log on the host confirms it.")


async def _ensure_jumpoint_host_aws(region: str, name: str = "",
                                    placement: Optional[dict] = None) -> Optional[str]:
    """Ensure an AWS Gateway host (and its task) is up; return its instance id (or
    None on the FARGATE escape hatch / when nothing was created). Raises AWSError on
    failure — callers treat this as best-effort."""
    from . import aws_service
    deploy_key = await _resolve_deploy_key()
    if not deploy_key:
        logger.warning("gateway-host: aws_ecs_docker_deploy_key not set — cannot start a "
                       "gateway; tunnels/jumps will be unavailable until configured.")
        return None

    launch_type = (_cfg("bt_ecs_launch_type") or "EC2").upper()
    if launch_type != "EC2":
        # Legacy Fargate: no host to manage, just run the task.
        await _ensure_task(region, deploy_key)
        return None

    requested = bool(name)
    name = name or managed_host_name("aws")
    existing = await aws_service.find_instances_by_tag(
        region, name_tag=name, states=["pending", "running"])
    if existing:
        logger.info("gateway-host: reusing host %s (%s)", existing[0]["instance_id"], name)
        _record_placement(placement, "", existing[0].get("public_ip"))
        _persist_jumpoint_egress_ip(existing[0].get("public_ip"), "aws", name,
                                    managed=not requested)
        # The tag match includes `pending`, and a running host may still be mid-ECS
        # registration — wait, or RunTask 400s with "No Container Instances".
        await _await_ecs_registration(region, existing[0]["instance_id"])
        await _ensure_task(region, deploy_key, existing[0]["instance_id"])
        return existing[0]["instance_id"]

    # Create the host. Re-check the tag right before launch to shrink the
    # find-or-create race (acceptable residual window for a single-operator lab).
    rc = _aws_region_cfg(region)
    cluster = rc["ecs_cluster"]
    # The ECS-optimized AL2023 AMI (the ECS agent + tun module ship in it). Same
    # image the managed Portainer/Rancher nodes use, so the parameter path has one
    # definition, in aws_service.
    ami_id = await aws_service.get_ssm_parameter(
        region, aws_service.ECS_OPTIMIZED_AMI_SSM)
    user_data = (f"#!/bin/bash\n"
                 f"echo \"ECS_CLUSTER={cluster}\" >> /etc/ecs/ecs.config\n"
                 f"modprobe tun || true\n")
    recheck = await aws_service.find_instances_by_tag(region, name_tag=name, states=["pending", "running"])
    if recheck:
        logger.info("gateway-host: host appeared concurrently (%s) — reusing",
                    recheck[0]["instance_id"])
        _record_placement(placement, "", recheck[0].get("public_ip"))
        _persist_jumpoint_egress_ip(recheck[0].get("public_ip"), "aws", name,
                                    managed=not requested)
        # Losing the race means the winner may still be booting — same wait as above.
        await _await_ecs_registration(region, recheck[0]["instance_id"])
        await _ensure_task(region, deploy_key, recheck[0]["instance_id"])
        return recheck[0]["instance_id"]

    inst = await aws_service.run_container_instance(
        region,
        ami_id=ami_id,
        instance_type=_cfg("bt_ecs_host_instance_type") or "t3.small",
        subnet_id=rc["jumpoint_subnet_id"],
        security_group_ids=[s.strip() for s in (rc["jumpoint_security_group_id"] or "").split(",") if s.strip()],
        instance_profile=_cfg("bt_ecs_host_instance_profile") or "ecsInstanceRole",
        user_data=user_data,
        name_tag=name,
    )
    host_id = inst["instance_id"]
    logger.info("gateway-host: launched host %s (%s) — awaiting ECS registration",
                host_id, _cfg("bt_ecs_host_instance_type") or "t3.small")

    await _await_ecs_registration(region, host_id)
    # Capture the freshly-launched host's (ephemeral) public IP for the Rancher
    # firewall — run_container_instance returns only the id, so look it up by tag.
    try:
        fresh = await aws_service.find_instances_by_tag(region, name_tag=name, states=["pending", "running"])
        if fresh:
            _record_placement(placement, "", fresh[0].get("public_ip"))
            _persist_jumpoint_egress_ip(fresh[0].get("public_ip"), "aws", name,
                                        managed=not requested)
    except Exception as exc:
        logger.warning("gateway-host: capturing host public IP failed (non-fatal): %s", exc)
    await _ensure_task(region, deploy_key, host_id)
    return host_id


async def find_gateway_host_id(cloud: str, region: str, name: str) -> str:
    """The cloud id of the gateway host named ``name``, or ``""`` when none exists.

    For recovering what a FAILED deploy already built. On AWS the host id is an EC2
    instance id the launch assigns, so a deploy that raised after ``run_instances`` —
    the ECS-registration wait being the usual place — left a running host whose id the
    caller never learned. GCP and Azure name their hosts, so there the lookup exists
    only to confirm the VM is really there rather than hand back a pointer to something
    that was never created.

    Best-effort by contract: returns ``""`` on any lookup failure, because this runs
    while a job is already failing and must not replace that failure with its own.
    """
    try:
        if cloud == "gcp":
            from . import gcp_service
            found = await gcp_service.describe_instances(
                _gcp_project(), _gcp_jumpoint_zone(region), [name])
            return name if found else ""
        if cloud == "azure":
            from . import azure_service
            from .region_config import resolve_region
            rg = resolve_region("azure", _azure_gateway_location(region))["resource_group"]
            return name if await azure_service.get_vm(rg, name) else ""
        from . import aws_service
        hosts = await aws_service.find_instances_by_tag(
            region, name_tag=name, states=["pending", "running", "stopping", "stopped"])
        return hosts[0]["instance_id"] if hosts else ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("gateway-host(%s): looking up the host for %s failed: %s",
                       cloud, name, exc)
        return ""


async def live_gateway_hosts(cloud: str, targets) -> dict:
    """Which of the gateway hosts in ``targets`` — ``(region, name)`` pairs — are really
    in ``cloud`` right now. Returns ``{name: {"host_id", "state"}}``; a name absent from
    the result does not exist.

    The plural counterpart to :func:`find_gateway_host_id`, with the opposite error
    contract, and that contract is the point. This one RAISES: the reconcile pass reading
    it rewrites registry rows, so "the lookup failed" must never be able to look like
    "the host is gone" — that would retire live inventory and pull a working gateway's
    /32 out of every node firewall on a transient API error.

    ``stopped``/``stopping`` count as present. A stopped host still exists, still bills
    its volume, and can be started again; calling it missing is the one direction of
    error that loses information.

    ``egress_ip`` comes back where the cloud reports one, because it is the other half of
    the same drift: a gateway host that was recreated keeps its NAME and takes a FRESH
    address, so a registry row that only ever learns an IP at ensure time hands the node
    firewalls a /32 that admits the previous host.
    """
    by_region: dict[str, list[str]] = {}
    for region, name in targets:
        by_region.setdefault(region or "", []).append(name)

    if cloud == "gcp":
        # One aggregated list covers every zone and both kinds of gateway: the managed
        # and requested paths share run_gce_jumpoint, so both carry purpose=bt-jumpoint —
        # the same label the Containers tab's GCE list filters on.
        from . import gcp_service
        project = _gcp_project()
        if not project:
            raise GatewayHostError("GCP project is not configured.")
        wanted = {n for names in by_region.values() for n in names}
        return {i["name"]: {"host_id": i["name"], "state": (i.get("status") or "").lower(),
                            "egress_ip": i.get("external_ip") or ""}
                for i in await gcp_service.list_gce_jumpoints(project)
                if i.get("name") in wanted}

    if cloud == "azure":
        # describe_vms, not get_vm: get_vm swallows its lookup failure and returns None,
        # which here would be indistinguishable from "no such VM" — a 403 on the read
        # would retire every Azure gateway. describe_vms raises, and one call per resource
        # group covers every gateway in it. Both kinds carry managed-by=vm-dashboard, so
        # its managed-only filter keeps them.
        from . import azure_service
        from .region_config import resolve_region
        out: dict = {}
        for region, names in by_region.items():
            rg = resolve_region("azure", _azure_gateway_location(region))["resource_group"]
            if not rg:
                raise GatewayHostError("Azure resource group is not configured.")
            found = {vm.get("name"): vm for vm in await azure_service.describe_vms(rg)}
            for name in names:
                vm = found.get(name)
                if vm:
                    out[name] = {"host_id": name, "state": (vm.get("state") or "").lower(),
                                 "egress_ip": vm.get("public_ip") or ""}
        return out

    from . import aws_service
    out = {}
    for region, names in by_region.items():
        found = await aws_service.find_instances_by_name_tags(
            region or _ui_jumpoint_region("aws"), name_tags=names,
            states=["pending", "running", "stopping", "stopped"])
        for name, info in found.items():
            out[name] = {"host_id": info["instance_id"], "state": info["state"],
                         "egress_ip": info.get("public_ip") or ""}
    return out


async def gateway_tasks_by_host(region: str, host_ids) -> Optional[dict]:
    """``{host_id: bool}`` — whether each of ``host_ids`` has a live gateway task on it —
    or None when that cannot be determined for ``region`` at all, which is NOT the same as
    False.

    AWS is the one cloud where "the host is up" and "the gateway is up" are separate facts
    behind separate APIs: the gateway is an ECS task placed on the container instance, and
    the host outlives the task dying. That gap is exactly what makes a registry row read
    ``running`` next to a Containers tab reporting zero tasks. On GCP and Azure the
    container is konlet/docker inside the VM with no API to ask, so there the host's
    existence is the whole answer.

    Batched because both underlying calls are per-CLUSTER, not per-host: asking once per
    gateway would issue 2N round trips to answer one question about N hosts sharing a
    cluster, and the reconcile pass reading this runs on a page load with a deadline.

    Best-effort by contract — None everywhere it is unsure — because a downgrade is only
    ever safe on a definite No.
    """
    family = _cfg("bt_ecs_task_family")
    if not family:
        # The task filter keys off the family name. Without it every task looks like
        # somebody else's and every host like it is serving nothing.
        return None
    host_ids = [h for h in host_ids if h]
    if not host_ids:
        return {}
    try:
        from . import aws_service
        cluster = _aws_region_cfg(region)["ecs_cluster"]
        if not cluster:
            return None
        live = _gateway_tasks(await aws_service.list_ecs_tasks(region, cluster), family)
        instances = await aws_service.list_container_instances(region, cluster)
        arn_by_host = {c.get("ec2_instance_id"): c.get("arn") for c in instances}
        # Falsy ARNs are dropped on both sides: a Fargate task carries no container
        # instance, and a host whose ECS agent never joined has no ARN to match — without
        # this the two Nones would meet and call that host "serving".
        serving = {t.get("containerInstanceArn") for t in live if t.get("containerInstanceArn")}
        return {h: bool(arn_by_host.get(h)) and arn_by_host[h] in serving for h in host_ids}
    except Exception as exc:  # noqa: BLE001
        logger.warning("gateway-host: checking gateway tasks in %s failed (non-fatal): %s",
                       region, exc)
        return None


async def teardown_gateway(cloud: str, region: str, name: str, zone: str = "") -> None:
    """Delete one named Gateway host unconditionally.

    The counterpart to ``teardown_jumpoint_host_if_idle``, for a gateway an operator
    deployed on purpose: no reference counting, because nothing implicitly depends on
    it — it exists because someone asked for it and goes away when someone asks.

    Refuses the managed gateway. That one's lifecycle belongs to the ensure/idle pair,
    and deleting it here would leave the auto-ensure to silently recreate it while the
    registry row said it was gone."""
    if name == managed_host_name(cloud):
        raise ValueError(
            f"{name!r} is the auto-managed gateway for {cloud}; it is created and "
            "removed by the reference-counted lifecycle, not by request")
    if cloud == "gcp":
        from . import gcp_service
        await gcp_service.stop_gce_jumpoint(_gcp_project(), zone or _gcp_jumpoint_zone(region), name)
    elif cloud == "azure":
        from . import azure_service
        from .region_config import resolve_region
        location = _azure_gateway_location(region)
        await azure_service.stop_vm_jumpoint(resolve_region("azure", location)["resource_group"], name)
    else:
        from . import aws_service
        cluster = _aws_region_cfg(region)["ecs_cluster"]
        family = _cfg("bt_ecs_task_family")
        hosts = await aws_service.find_instances_by_tag(
            region, name_tag=name, states=["pending", "running", "stopping", "stopped"])
        for h in hosts:
            # Stop this host's tasks first so PRA deregisters gracefully, and only
            # this host's — the same scoping the idle teardown uses.
            for t in await _live_gateway_tasks(region, cluster, family, h["instance_id"]):
                await aws_service.stop_ecs_jumpoint_task(region, cluster, t["taskArn"])
            await aws_service.terminate_instance(region, h["instance_id"])
    logger.info("gateway-host(%s): deleted gateway %s", cloud, name)


def _active_db_count(db, cloud: Optional[str] = None) -> int:
    # Only dashboard-PROVISIONED databases hold a reference to the shared Gateway. A
    # registered row is someone else's database — no Terraform state, no PRA tunnel, no
    # jump item — so it never brokers through the host, and counting it pinned the
    # gateway (and, on AWS, the three SSM endpoints) forever. Registered rows are born
    # status='available', so the status gate alone never caught them. NULL means
    # provisioned: the migration in database.py backfills that literal, and reading it
    # NULL-tolerantly is the safe direction — under-counting would reap the gateway from
    # under a live DB.
    from ..database import CloudDatabase
    q = (db.query(CloudDatabase)
           .filter(CloudDatabase.status.in_(["available", "provisioning"])))
    if cloud:
        q = q.filter(CloudDatabase.cloud == cloud)
    return sum(1 for r in q.all() if (r.source or "provisioned") != "registered")


def _active_ec2_count(db) -> int:
    # Mirrors the sibling count in api/aws.py:_run_destroy — completed ec2_deploy
    # jobs not yet marked destroyed.
    from ..database import Job
    jobs = db.query(Job).filter(Job.job_type == "ec2_deploy", Job.status == "completed").all()
    return sum(1 for j in jobs if not (j.metadata_dict or {}).get("destroyed"))


def _active_gce_count(db) -> int:
    """Live GCE VMs that borrowed the SHARED host.

    Deliberately asymmetric with ``_active_ec2_count``, which counts *all* live
    ec2_deploy rows: on AWS every EC2 deploy uses the shared host, so "all" is right
    there. On GCP both shapes coexist — singles follow ``gcp_vm_jumpoint_mode`` (shared
    by default), batches always share, and a deploy carrying its own Gateway deploy key
    is always paired — so counting all of them would let one paired VM pin the shared
    host forever and block a cloud-database reclaim.

    Keys on ``jumpoint_host_id`` (what the deploy actually used) rather than
    ``jumpoint_mode`` (what it intended), so a row whose ensure failed and never
    touched the host doesn't hold a phantom reference."""
    from ..database import Job
    jobs = db.query(Job).filter(Job.job_type == "gce_deploy", Job.status == "completed").all()
    return sum(1 for j in jobs
               if not (j.metadata_dict or {}).get("destroyed")
               and (j.metadata_dict or {}).get("jumpoint_host_id"))


def _active_azure_vm_count(db) -> int:
    """Live Azure VMs that borrowed the SHARED host.

    Shaped like ``_active_gce_count``, not ``_active_ec2_count``, for the same reason:
    on Azure both shapes coexist — singles follow ``azure_vm_jumpoint_mode`` (shared by
    default), and a deploy that asked for ACI or carried its own Gateway deploy key got
    its own container group instead — so counting all ``azure_deploy`` rows would let one
    ACI-brokered VM pin the shared VM forever and block a cloud-database reclaim.

    Keys on ``jumpoint_host_id`` (what the deploy actually used) rather than
    ``jumpoint_mode`` (what it intended), so a row whose ensure failed and never touched
    the host doesn't hold a phantom reference."""
    from ..database import Job
    jobs = db.query(Job).filter(Job.job_type == "azure_deploy", Job.status == "completed").all()
    return sum(1 for j in jobs
               if not (j.metadata_dict or {}).get("destroyed")
               and (j.metadata_dict or {}).get("jumpoint_host_id"))


def _active_k8s_count(db, cloud: Optional[str] = None) -> int:
    # A managed cluster needs the shared Gateway while it has EITHER a live PRA
    # k8s tunnel (pra_jump_id set) OR a live API TCP tunnel (config key
    # k8s_api_tunnel_jump_{id} set — no DB column) routing through it, so neither
    # tunnel's teardown yanks the gateway from under the other. (Registered local
    # clusters carry cloud='local' and never match a real-cloud filter.)
    from ..database import K8sCluster
    from . import config_service
    q = db.query(K8sCluster)
    if cloud:
        q = q.filter(K8sCluster.cloud == cloud)
    return sum(1 for r in q.all()
               if r.pra_jump_id or config_service.get(f"k8s_api_tunnel_jump_{r.id}"))


def _active_vdesktop_count(db, cloud: Optional[str] = None) -> int:
    # A desktop seat needs the shared Gateway only while it has a live PRA Remote
    # RDP jump routing through it — i.e. pra_jump_id is set. Mirrors
    # _active_k8s_count so a VDI pool keeps the gateway alive, and neither a
    # clouddb/k8s teardown yanks it from under running seats nor a VDI teardown
    # from under a DB/cluster.
    from ..database import VirtualDesktop
    q = db.query(VirtualDesktop).filter(VirtualDesktop.pra_jump_id.isnot(None))
    if cloud:
        q = q.filter(VirtualDesktop.cloud == cloud)
    return q.count()


def _active_web_jump_count(cloud: str) -> int:
    """Web Jumps brokered by ``cloud``'s shared gateway — the Rancher/Portainer
    management UIs.

    Load-bearing, like ``_active_gce_count``: a provisioned Web Jump reaches its node
    THROUGH this gateway, so reaping it takes the UI offline and takes the jumpoint /32
    out of the node's firewall with it. Both features already store the ids this counts,
    so an enabled-but-never-provisioned Web Jump correctly holds no reference."""
    from . import config_service
    total = 0
    for enabled, jump_id, cloud_key in (
            ("rancher_ui_web_jump_enabled", "rancher_ui_web_jump_id",
             "rancher_ui_jumpoint_cloud"),
            ("portainer_ui_web_jump_enabled", "portainer_ui_web_jump_id",
             "portainer_ui_jumpoint_cloud")):
        if not config_service.get_bool(enabled, False):
            continue
        if not (config_service.get(jump_id) or "").strip():
            continue
        # Same default as ensure_{rancher,portainer}_ui_jumpoint: gcp.
        if ((config_service.get(cloud_key) or "gcp").strip().lower()) == cloud:
            total += 1
    return total


def _active_ot_tunnel_count(cloud: str = "gcp") -> int:
    """Standalone OT protocol tunnels (api/ot) brokered by ``cloud``'s shared
    gateway.

    Cell-attached OT wiring needs no term here — a cell's gce/ec2/azure deploy row
    already holds its reference via ``_active_gce_count`` and friends. Standalone
    tunnels have no job row, only config keys, so without this a cloud-database
    decommission could reap the gateway from under a tunnel an operator is
    mid-session on (the same class of bug ``_active_web_jump_count`` exists to
    prevent). Exceptions propagate on purpose: the caller's whole pass is
    best-effort, and an error must mean "don't reap", never "count is zero"."""
    from . import ot_service
    return ot_service.active_standalone_tunnel_count(cloud)


async def teardown_jumpoint_host_if_idle(db, cloud: str, region: str) -> None:
    """Terminate the shared Gateway host for ``cloud`` iff nothing is left using
    it. Dispatches per cloud. Best-effort; logs and returns on error."""
    if cloud == "gcp":
        return await _teardown_jumpoint_host_if_idle_gcp(db, region)
    if cloud == "azure":
        return await _teardown_jumpoint_host_if_idle_azure(db, region)
    return await _teardown_jumpoint_host_if_idle_aws(db, region)


async def _teardown_jumpoint_host_if_idle_aws(db, region: str) -> None:
    """Terminate the *managed* AWS host iff nothing is left using it (no managed EC2
    instance, no active AWS cloud database). Best-effort; logs and returns on error.

    Scoped to the managed gateway throughout. The host lookup is already narrow — it
    matches only the managed Name tag — but the task stop was not: it stopped every
    running task in the cluster, which was harmless while one gateway existed and
    would silently kill a user-deployed gateway's task once they share a cluster.
    Both halves now key off the managed host's own instance id.
    """
    from . import aws_service
    try:
        active = (_active_db_count(db, "aws") + _active_ec2_count(db)
                  + _active_k8s_count(db, "aws") + _active_vdesktop_count(db, "aws")
                  + _active_web_jump_count("aws") + _active_ot_tunnel_count("aws"))
        if active > 0:
            logger.info("gateway-host: keeping host (%d active resource(s))", active)
            return
        name = managed_host_name("aws")
        hosts = await aws_service.find_instances_by_tag(
            region, name_tag=name, states=["pending", "running", "stopping", "stopped"])
        if not hosts:
            # Already gone — but the registry may still say otherwise, and this is the
            # only place that would ever have said so. Returning straight out of here is
            # how a row stayed `running` indefinitely, keeping a dead /32 in every node
            # firewall. Nothing left to terminate is still an idle-teardown outcome.
            _clear_managed_egress_ip("aws", name)
            return
        # Stop this host's gateway task(s) first (graceful PRA deregistration), then
        # terminate the host. Never touches a task on any other container instance.
        cluster = _aws_region_cfg(region)["ecs_cluster"]
        family = _cfg("bt_ecs_task_family")
        for h in hosts:
            try:
                for t in await _live_gateway_tasks(region, cluster, family, h["instance_id"]):
                    await aws_service.stop_ecs_jumpoint_task(region, cluster, t["taskArn"])
            except Exception as exc:
                logger.warning("gateway-host: stopping gateway task(s) failed (non-fatal): %s", exc)
            await aws_service.terminate_instance(region, h["instance_id"])
            logger.info("gateway-host: terminated idle host %s", h["instance_id"])
        _clear_managed_egress_ip("aws", name)
    except Exception as exc:
        logger.warning("gateway-host: idle teardown failed (non-fatal): %s", exc)


# ── GCP: privileged BeyondTrust Gateway container on a COS GCE VM ─────────────
# Cloud Run / serverless can't grant NET_ADMIN/NET_RAW/IPC_LOCK + /dev/net/tun,
# so the tunnel host is a Container-Optimised-OS GCE instance running the
# gateway container PRIVILEGED (gcp_service sets securityContext.privileged).
# One shared, ref-counted instance, mirroring the AWS host lifecycle.

def _gcp_jumpoint_name() -> str:
    """GCE *instance* name for the shared gateway VM — must be a valid GCE
    resource name (RFC1035: lowercase, leading letter, hyphens). This is NOT the
    PRA Gateway the tunnel binds to: that comes from the form's gateway picker,
    and which PRA Gateway the container joins is set by its deploy key. So
    sanitize any configured value — a PRA display name like 'GCP Run' (space +
    uppercase) otherwise 400s the Compute API as an invalid instance name."""
    raw = (_cfg("gcp_jumpoint_name") or "clouddb-shared-jumpoint").strip().lower()
    name = re.sub(r"[^a-z0-9-]+", "-", raw).strip("-")
    if not name or not name[0].isalpha():
        name = f"clouddb-{name}".strip("-")
    return name[:63].rstrip("-") or "clouddb-shared-jumpoint"


def _gcp_project() -> str:
    return _cfg("gcp_project") or _cfg("gcp_project_id")


def _gcp_jumpoint_zone(region: str) -> str:
    """The zone a GCP gateway host runs in for ``region``.

    ``gcp_jumpoint_zone`` is the gateway's zone override, but it only applies where
    it belongs: a zone from another region would place the host — and the subnet
    ``_gcp_jumpoint_subnetwork`` derives from that zone — outside the region the
    operator asked for, silently ignoring their choice. Everything else is
    ``region_config``'s zone resolution (the region's own ``zone``, then the flat
    ``gcp_zone`` when it is in-region, then ``<region>-b``).

    A blank region keeps the historical chain exactly, so single-region installs are
    unaffected.
    """
    from .region_config import resolve_zone_for_region, zone_in_region
    override = _cfg("gcp_jumpoint_zone")
    if not region:
        return override or _cfg("gcp_zone") or ""
    if override and zone_in_region(override, region):
        return override
    return resolve_zone_for_region(region)


def _gcp_jumpoint_subnetwork(project: str, zone: str) -> str:
    """Regional self-link for the gateway's subnet. Prefer gcp_jumpoint_subnetwork
    (the sandbox's Cloud-NAT subnet) over gcp_subnetwork (the user-VM subnet, which
    the sandbox leaves without internet egress — a gateway there can't reach PRA to
    register). The sandbox emits a bare name, but GCE's networkInterfaces.subnetwork
    needs projects/<p>/regions/<r>/subnetworks/<name>; a value already containing "/"
    is passed through unchanged."""
    from . import region_catalog
    from .region_config import resolve_region
    # Resolve per the gateway's region (derived from its zone). The region-config
    # jumpoint_subnetwork falls back to gcp_subnetwork, then the flat keys.
    region = region_catalog.region_from_zone(zone)
    sub = resolve_region("gcp", region)["jumpoint_subnetwork"]
    if not sub or "/" in sub:
        return sub
    return f"projects/{project}/regions/{region}/subnetworks/{sub}" if region else sub


async def _resolve_gcp_deploy_key() -> str:
    """BeyondTrust Gateway deploy key for GCP launches — resolved through whichever
    secrets backend the user picked on /secrets (same keys the GCP deploy flow uses)."""
    from . import config_service
    return (config_service.get("gcp_cloud_run_docker_deploy_key")
            or config_service.get("gcp_jumpoint_docker_deploy_key")
            or config_service.get("gcp_jumpoint_deploy_key")
            or "")


async def _ensure_jumpoint_host_gcp(region: str, name: str = "", zone: str = "",
                                    placement: Optional[dict] = None) -> Optional[str]:
    """Ensure a COS GCE Gateway VM is up (idempotent on name); return its name.
    Best-effort — logs and returns None when prerequisites are missing."""
    from . import gcp_service, region_catalog
    from .region_config import resolve_region
    project = _gcp_project()
    if not project:
        logger.warning("gateway-host(gcp): gcp_project not set — cannot start a gateway.")
        return None
    deploy_key = await _resolve_gcp_deploy_key()
    if not deploy_key:
        logger.warning("gateway-host(gcp): gateway deploy key not set "
                       "(gcp_cloud_run_docker_deploy_key) — tunnels unavailable until configured.")
        return None
    requested = bool(name)
    name = name or _gcp_jumpoint_name()
    zone = zone or _gcp_jumpoint_zone(region)
    # The launcher's zone fallback is region-scoped, so a blank region (single-region
    # installs pass one through, but a zone-only caller may not) is derived from the zone.
    region = region or region_catalog.region_from_zone(zone)
    try:
        meta = await gcp_service.run_gce_jumpoint(
            project_id=project,
            zone=zone,
            name=name,
            container_image=_cfg("gcp_jumpoint_image") or "beyondtrust/sra-jumpoint:latest",
            deploy_key=deploy_key,
            # Per-region DB network (region-config db_network → gcp_network flat).
            network=resolve_region("gcp", region)["db_network"],
            subnetwork=_gcp_jumpoint_subnetwork(project, zone),
            machine_type=_cfg("gcp_jumpoint_machine_type") or "e2-micro",
            create_external_ip=True,
            # Lets the launcher try the region's other zones when this one is out of
            # capacity, instead of leaving the deployment with no gateway at all.
            region=region,
        )
        landed_zone = meta.get("zone") or zone
        logger.info("gateway-host(gcp): gateway %s %s in %s",
                    name, "reused" if meta.get("reused") else "started", landed_zone)
        if placement is not None:
            placement.update({"zone": landed_zone, "egress_ip": meta.get("external_ip") or ""})
        _persist_jumpoint_egress_ip(meta.get("external_ip"), "gcp", name,
                                    managed=not requested)
        return name
    except Exception as exc:
        logger.warning("gateway-host(gcp): ensure failed (non-fatal): %s", exc)
        return None


async def _teardown_jumpoint_host_if_idle_gcp(db, region: str) -> None:
    """Delete the *managed* GCE Gateway VM iff nothing is left using it — no active
    GCP cloud database, k8s tunnel, VDI seat, or batch-deployed GCE VM. Best-effort;
    logs and returns on error.

    Safe against user-deployed gateways by construction: it deletes one VM by the
    managed name, which no user gateway is allowed to take."""
    from . import gcp_service
    try:
        # _active_gce_count is load-bearing, not tidiness: without it a cloud-database
        # decommission would delete the host out from under every running GCE VM that
        # borrowed it. The AWS counterpart has always included its _active_ec2_count
        # term for exactly this reason.
        active = (_active_db_count(db, "gcp") + _active_k8s_count(db, "gcp")
                  + _active_vdesktop_count(db, "gcp") + _active_gce_count(db)
                  + _active_web_jump_count("gcp") + _active_ot_tunnel_count("gcp"))
        if active > 0:
            logger.info("gateway-host(gcp): keeping gateway (%d active resource(s))", active)
            return
        project = _gcp_project()
        if not project:
            # Without the project nothing can be deleted OR verified, so the registry row
            # is deliberately left alone: `gateway_service.reconcile` retires a gateway
            # only on a lookup that succeeded and came back empty, never on one it could
            # not perform. Logged because a silent return here reads as "nothing to do".
            logger.warning("gateway-host(gcp): no project configured — cannot tear down "
                           "the idle gateway")
            return
        name = managed_host_name("gcp")
        await gcp_service.stop_gce_jumpoint(project, _gcp_jumpoint_zone(region), name)
        logger.info("gateway-host(gcp): deleted idle gateway %s", name)
        _clear_managed_egress_ip("gcp", name)
    except Exception as exc:
        logger.warning("gateway-host(gcp): idle teardown failed (non-fatal): %s", exc)


# ── Azure: privileged BeyondTrust Gateway on an Azure VM ─────────────────────
# ACI (run_aci_jumpoint_task) is serverless and can't grant NET_ADMIN/NET_RAW/
# IPC_LOCK + /dev/net/tun, so the tunnel host is a real Azure VM running the
# gateway container privileged (azure_service.run_vm_jumpoint). One shared,
# ref-counted VM, mirroring the AWS/GCP host lifecycle.

_AZURE_JUMPOINT_VM_NAME = "clouddb-jumpoint"


async def _resolve_azure_deploy_key() -> str:
    """BeyondTrust Gateway deploy key for Azure launches — resolved through
    whichever secrets backend the user picked on /secrets (same keys the ACI
    gateway path uses)."""
    from . import config_service
    return (config_service.get("azure_aci_deploy_key")
            or config_service.get("azure_aci_docker_deploy_key")
            or "")


def _azure_gateway_location(region: str) -> str:
    """The Azure region a gateway VM is created in — and torn down from.

    A requested region wins; ``azure_location`` is the fallback for the *managed*
    host, which is ensured without one. Reading the flat key first (as this used to)
    made a region choice a no-op: the VM came up in the default region while the row,
    the job and the operator all said otherwise, and the teardown then looked for it
    in the wrong resource group.
    """
    from . import region_catalog
    return region_catalog.normalize("azure", region) or _cfg("azure_location")


async def _ensure_jumpoint_host_azure(region: str, name: str = "",
                                      placement: Optional[dict] = None) -> Optional[str]:
    """Ensure an Azure VM Gateway is up (idempotent on name); return its name.
    Best-effort — logs and returns None when prerequisites are missing."""
    from . import azure_service
    from .region_config import resolve_region
    location = _azure_gateway_location(region)
    rc = resolve_region("azure", location)
    rg = rc["resource_group"]
    subnet = rc["jumpoint_subnet_id"] or _cfg("azure_aci_subnet_id")
    requested = bool(name)
    name = name or managed_host_name("azure")
    if not (rg and location and subnet):
        logger.warning("gateway-host(azure): azure_resource_group / azure_location / "
                       "azure_jumpoint_subnet_id not set — cannot start a gateway.")
        return None
    deploy_key = await _resolve_azure_deploy_key()
    if not deploy_key:
        logger.warning("gateway-host(azure): gateway deploy key not set "
                       "(azure_aci_deploy_key) — tunnels unavailable until configured.")
        return None
    # Bake the native DB clients into the VM when Password Safe cloud-DB onboarding
    # is enabled (the "{engine} Azure Run Command Plugin" invokes them in-guest at
    # rotation time). Only takes effect on a fresh VM; the onboarding also ensures
    # them idempotently over Run Command, covering a reused VM.
    from . import config_service
    install_db_clients = (
        config_service.get_bool("clouddb_ps_onboarding_enabled", False)
        and (_cfg("passwordsafe_azure_db_registration_method") or "runcommand").lower() != "off")
    try:
        meta = await azure_service.run_vm_jumpoint(
            rg=rg, location=location, subnet_id=subnet, name=name,
            container_image=_cfg("azure_aci_jumpoint_image") or "beyondtrust/sra-jumpoint:latest",
            deploy_key=deploy_key,
            # Standard_B2s (4 GB), NOT Standard_B1s (1 GB) — Azure's side of the same
            # defect proven on GCP 2026-08-17: a Web Jump renders the target UI in a
            # headless Chromium on the GATEWAY, and 1 GB gets it OOM-killed, dropping
            # every session on the node. See `gcp_jumpoint_machine_type` in config.py.
            vm_size=_cfg("azure_jumpoint_vm_size") or "Standard_B2s",
            admin_password=azure_service._azure_compliant_password(),
            install_db_clients=install_db_clients,
        )
        logger.info("gateway-host(azure): gateway VM %s %s in %s",
                    name, "reused" if meta.get("reused") else "started", location)
        _record_placement(placement, location, meta.get("public_ip"))
        _persist_jumpoint_egress_ip(meta.get("public_ip"), "azure", name,
                                    managed=not requested)
        return name
    except Exception as exc:
        logger.warning("gateway-host(azure): ensure failed (non-fatal): %s", exc)
        return None


async def _teardown_jumpoint_host_if_idle_azure(db, region: str) -> None:
    """Delete the *managed* Azure Gateway VM iff nothing is left using it — no active
    Azure cloud database, k8s tunnel, VDI seat, or Azure VM that borrowed it.
    Best-effort; logs and returns on error.

    Safe against user-deployed gateways by construction: it deletes one VM by the
    managed name, which no user gateway is allowed to take."""
    from . import azure_service
    try:
        # _active_azure_vm_count is load-bearing, not tidiness: without it a
        # cloud-database decommission would delete the host out from under every Azure VM
        # that borrowed it. The AWS and GCP counterparts carry their _active_ec2_count /
        # _active_gce_count terms for exactly this reason.
        active = (_active_db_count(db, "azure") + _active_k8s_count(db, "azure")
                  + _active_vdesktop_count(db, "azure") + _active_azure_vm_count(db)
                  + _active_web_jump_count("azure") + _active_ot_tunnel_count("azure"))
        if active > 0:
            logger.info("gateway-host(azure): keeping gateway (%d active resource(s))", active)
            return
        rg = _cfg("azure_resource_group")
        if not rg:
            # Same reasoning as the GCP guard above: unverifiable is not the same as
            # gone, so the row stays as it is and the reason gets logged.
            logger.warning("gateway-host(azure): no resource group configured — cannot "
                           "tear down the idle gateway")
            return
        name = managed_host_name("azure")
        await azure_service.stop_vm_jumpoint(rg, name)
        logger.info("gateway-host(azure): deleted idle gateway %s", name)
        _clear_managed_egress_ip("azure", name)
    except Exception as exc:
        logger.warning("gateway-host(azure): idle teardown failed (non-fatal): %s", exc)
