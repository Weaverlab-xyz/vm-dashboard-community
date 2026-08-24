"""Shared half of the managed-node orchestrators (Portainer server, Rancher server).

Both features are the same thing wearing different labels: ONE container on ONE
public-but-source-restricted VM, deployed and torn down by a job, with an ingress
allow-list recomputed from several sources on every relevant event. They were
written as deliberate mirrors of each other -- which meant every fix had to be made
twice, and porting them to two more clouds would have meant writing the same logic
six times.

So the cloud-INDEPENDENT half lives here (allow-list merge, egress-IP detection,
placement resolution, admin-password generation) and the per-cloud host primitives
are dispatched from here into ``aws_service`` / ``azure_service`` / ``gcp_service``.
That is exactly the relationship ``gateway_service`` has with
``jumpoint_host_service`` -- the third managed-container offering on the same
Containers page, and the one that was already multi-cloud.

The feature modules (``rancher_node_service`` / ``portainer_node_service``) keep
their own job lifecycle and their own bootstrap logic, because those genuinely
differ: Rancher runs privileged and mints an API token from a bootstrap password;
Portainer runs unprivileged and must be handed a bcrypt admin hash at boot.
"""
import ipaddress
import logging
from dataclasses import dataclass

import httpx

from . import config_service, region_catalog, region_config

logger = logging.getLogger(__name__)

# Clouds a managed node can be hosted on. Same order as gateway_service.CLOUDS so
# the two pickers list identically.
CLOUDS = ("aws", "azure", "gcp")

# Plain-HTTP echo endpoints used to learn the dashboard's own public egress IP.
# HTTP (not HTTPS) dodges corp TLS-inspection breakage; best-effort, first-wins.
_IP_ECHO_URLS = (
    "http://checkip.amazonaws.com",
    "http://api.ipify.org",
    "http://ifconfig.me/ip",
)


class ManagedNodeError(Exception):
    """A managed-node operation that cannot be attempted as configured."""


@dataclass(frozen=True)
class NodeSpec:
    """Everything that differs between the two features, in one place.

    Config keys are DERIVED rather than listed, because the existing naming is
    already perfectly regular: cloud-neutral behaviour keys are
    ``<feature>_<knob>`` and per-cloud placement keys are
    ``<cloud>_<feature>_<knob>``. Deriving them means a new cloud costs no new
    plumbing here -- only the ``aws_``/``azure_`` fields in ``config.py``.
    """
    feature: str                  # "rancher" | "portainer"
    label: str                    # "Rancher" | "Portainer" -- log + message prefix
    ports: tuple                  # ingress ports the node listens on
    settings_hint: str            # where the operator changes these, for error text
    ready_timeout_default: int

    # -- cloud-neutral behaviour keys -----------------------------------------
    @property
    def node_cloud_key(self) -> str:
        return f"{self.feature}_node_cloud"

    @property
    def manual_cidrs_key(self) -> str:
        return f"{self.feature}_allowed_source_cidrs"

    @property
    def dashboard_cidr_key(self) -> str:
        return f"{self.feature}_dashboard_egress_cidr"

    @property
    def web_jump_enabled_key(self) -> str:
        return f"{self.feature}_ui_web_jump_enabled"

    @property
    def jumpoint_cloud_key(self) -> str:
        return f"{self.feature}_ui_jumpoint_cloud"

    @property
    def jumpoint_egress_key(self) -> str:
        return f"{self.feature}_ui_jumpoint_egress_ip"

    @property
    def ready_timeout_key(self) -> str:
        return f"{self.feature}_ready_timeout_s"

    # -- per-cloud placement keys ---------------------------------------------
    def infra_key(self, cloud: str, knob: str) -> str:
        return f"{cloud}_{self.feature}_{knob}"

    def allow_open_key(self, cloud: str) -> str:
        return self.infra_key(cloud, "allow_open")


RANCHER = NodeSpec(
    feature="rancher", label="Rancher", ports=("80", "443"),
    settings_hint="Settings -> Kubernetes", ready_timeout_default=360,
)
PORTAINER = NodeSpec(
    feature="portainer", label="Portainer", ports=("9443", "8000"),
    settings_hint="Settings -> Containers", ready_timeout_default=300,
)


def firewall_name(node_name: str) -> str:
    """The ingress rule / security group / NSG name for a node.

    One name across all three clouds on purpose: it is what a teardown looks for,
    and what an operator greps for in a console.
    """
    return f"{node_name}-allow-mgmt"


def node_cloud(spec: NodeSpec) -> str:
    """Which cloud hosts this feature's node.

    Defaults to ``gcp`` -- not because GCP is special, but because every node that
    exists today is a GCE VM, and a default of anything else would send an existing
    install looking for its node in a cloud it was never deployed to. Written on
    every deploy, so it tracks the node rather than the default.
    """
    val = (config_service.get(spec.node_cloud_key) or "").strip().lower()
    return val if val in CLOUDS else "gcp"


def set_node_cloud(spec: NodeSpec, cloud: str) -> None:
    """Persist where the node actually landed, so teardown and bare redeploys
    (which are given no cloud) go back to the same place -- the same reason the
    launched zone is persisted."""
    cloud = (cloud or "").strip().lower()
    if cloud in CLOUDS:
        config_service.set(spec.node_cloud_key, cloud)


def generate_admin_password() -> str:
    """A strong admin password for a node's first run when the operator didn't set
    one. Both Rancher and Portainer enforce a 12-char minimum, so this is a fresh
    24-char mix of upper/lower/digits/symbols. Persisted + surfaced (job result +
    login hint) so the operator can retrieve it."""
    import secrets
    import string
    symbols = "!@#%^*-_=+"
    alphabet = string.ascii_letters + string.digits + symbols
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(24))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in symbols for c in pw)):
            return pw


# -- allow-list sources ------------------------------------------------------

def allowed_cidrs(spec: NodeSpec, cloud: str) -> list:
    """MANUAL ingress source ranges (CSV), fail-closed. Empty CSV -> [] unless
    allow_open is ticked for this cloud.

    Deliberately quiet: the CSV is only ONE input to the merged set (the dashboard's
    own egress, cluster egress /32s and gateway /32s join it), so an empty CSV
    usually does NOT mean the node stays closed. The applied-outcome warnings belong
    to the caller that sees the FINAL set.
    """
    csv = config_service.get(spec.manual_cidrs_key) or ""
    cidrs = [c.strip() for c in csv.split(",") if c.strip()]
    if not cidrs:
        if config_service.get_bool(spec.allow_open_key(cloud), False):
            return ["0.0.0.0/0"]
        logger.debug("%s is empty - relying on auto-discovered sources",
                     spec.manual_cidrs_key)
    return cidrs


def jumpoint_cidrs(spec: NodeSpec, db=None) -> list:
    """/32s for the Gateway hosts that can broker this node's Web Jump.

    A PRA Web Jump reaches the node THROUGH a Gateway, so the source IP hitting the
    ingress rule is that host's egress IP (never the PRA appliance's).

    EVERY live gateway in the Web Jump's cloud counts, not just the shared one: they
    all join the same PRA Gateway cluster and PRA distributes sessions across its
    nodes, so the broker for a given session may be any of them. Allowing only the
    remembered shared /32 left a user-deployed gateway locked out of the node it was
    deployed to carry.

    Sources: the gateway registry (``Gateway.egress_ip``, authoritative and
    per-gateway) plus the legacy ``<feature>_ui_jumpoint_egress_ip`` key, which still
    covers a shared host recorded before that table existed. Empty for a pre-existing
    operator Gateway the dashboard did not provision -- add its IP to the CSV
    manually.
    """
    if not config_service.get_bool(spec.web_jump_enabled_key, False):
        return []
    ips = set()
    ip = (config_service.get(spec.jumpoint_egress_key) or "").strip()
    if ip:
        ips.add(ip)
    cloud = (config_service.get(spec.jumpoint_cloud_key) or "gcp").strip().lower()
    try:
        from . import gateway_service
        if db is not None:
            ips.update(gateway_service.live_egress_ips(db, cloud))
        else:
            # Callers on the read-only path have a session; the refresh path may not,
            # so open a short-lived one rather than skip the registry and quietly
            # narrow the rule.
            from ..database import SessionLocal
            _db = SessionLocal()
            try:
                ips.update(gateway_service.live_egress_ips(_db, cloud))
            finally:
                _db.close()
    except Exception as exc:  # noqa: BLE001 -- never let an inventory read close the rule
        logger.warning("%s ingress: reading gateway egress IPs failed "
                       "(continuing with %d known): %s", spec.label, len(ips), exc)
    return sorted(f"{i}/32" for i in ips)


def dashboard_cidr(spec: NodeSpec) -> list:
    """/32 for the DASHBOARD's own public egress IP.

    The worker bootstraps and polls the node over its PUBLIC IP, so this is the
    source address that hits the node's source-restricted ingress rule -- without it
    a (re)deploy cannot reach its own node and the readiness poll times out. Sourced
    from ``<feature>_dashboard_egress_cidr`` (auto-detected + persisted on deploy, or
    set manually); a bare IP is normalized to ``/32``.
    """
    val = (config_service.get(spec.dashboard_cidr_key) or "").strip()
    if not val:
        return []
    return [val if "/" in val else f"{val}/32"]


def ready_timeout_s(spec: NodeSpec) -> int:
    """Readiness poll budget (config ``<feature>_ready_timeout_s``)."""
    from ..config import settings
    try:
        return int(config_service.get(spec.ready_timeout_key)
                   or getattr(settings, spec.ready_timeout_key, spec.ready_timeout_default))
    except (TypeError, ValueError):
        return spec.ready_timeout_default


async def detect_egress_ip(label: str = "") -> str:
    """Best-effort: learn the worker's own public egress IP via a plain-HTTP echo.

    Plain HTTP (not HTTPS) avoids corp TLS-inspection breakage; ``trust_env`` honors
    proxy env vars. Returns a bare IPv4 string, or ``""`` on any failure (no route,
    proxy block, malformed body) -- the caller falls back to the operator-set value.
    """
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
        logger.warning("%s egress-IP detection failed (continuing): %s", label, exc)
    return ""


async def ensure_dashboard_egress_cidr(spec: NodeSpec, detect=None) -> str:
    """Refresh + persist the dashboard's own egress CIDR so the ingress rule admits
    the worker.

    Detection tracks a changed dynamic IP, but an operator-set CIDR that already
    CONTAINS the detected IP is kept as-is: corp proxies (Cloudflare WARP) egress
    from a per-connection POOL of IPs, so pinning whichever /32 detection saw this
    time would still drop the next connection -- the operator sets the pool's CIDR
    once (e.g. ``104.28.182.0/24``) and detection must not clobber it. On detection
    failure any operator-set value is left intact. Returns the CIDR now in effect
    (``""`` if still unknown).

    ``detect`` is injectable so each feature module keeps its own patchable seam.
    """
    detector = detect or detect_egress_ip
    ip = await detector()
    existing = (config_service.get(spec.dashboard_cidr_key) or "").strip()
    if ip:
        if existing:
            try:
                net = ipaddress.ip_network(existing if "/" in existing else f"{existing}/32",
                                           strict=False)
                if ipaddress.ip_address(ip) in net:
                    return existing  # detected IP already covered - keep the broader pin
            except ValueError:
                pass  # malformed stored value - fall through and replace it
        cidr = f"{ip}/32"
        if existing != cidr:
            config_service.set(spec.dashboard_cidr_key, cidr)
            logger.info("%s ingress: dashboard egress IP detected as %s", spec.label, cidr)
        return cidr
    if not existing:
        logger.warning(
            "%s ingress: could not auto-detect the dashboard's public egress IP and %s is "
            "unset - the worker may be unable to reach the node. Set it manually in %s if "
            "the deploy fails.", spec.label, spec.dashboard_cidr_key, spec.settings_hint)
    return existing


# -- the container command (AWS + Azure; GCP has konlet instead) -------------

def docker_run_command(image: str, *, name: str, ports=(), env=None, args=(),
                       privileged: bool = False, volumes=()) -> str:
    """A ``docker run`` line for a managed node, for whichever cloud runs it from
    user-data / cloud-init.

    Written once and shared because the container is the SAME container on every
    cloud -- only the mechanism that starts it differs (GCE reads a
    ``gce-container-declaration``; AWS and Azure run this). Two details are
    load-bearing rather than stylistic:

    * Values are single-quoted. A bcrypt hash (Portainer's ``--admin-password``) is
      full of ``$``, and an unquoted one would be expanded by the shell into
      something Portainer cannot verify -- producing a node that boots fine and
      rejects the only password anybody has.
    * ``--restart always`` matches konlet's ``restartPolicy: Always``, so a reboot
      brings the node back rather than leaving a running VM serving nothing.

    Ports are PUBLISHED rather than run on the host network. COS happens to use host
    networking, but ``-p`` is what both upstreams document, and the observable result
    (those ports answering on the VM) is identical.
    """
    parts = ["docker run -d --restart always", f"--name {name}"]
    if privileged:
        parts.append("--privileged")
    for p in ports:
        parts.append(f"-p {p}:{p}")
    for host_path, container_path in volumes:
        parts.append(f"-v {host_path}:{container_path}")
    for key, val in (env or {}).items():
        parts.append(f"-e {key}='{val}'")
    parts.append(image)
    # Container ARGS go after the image, and are quoted for the same reason as env.
    parts += [f"'{a}'" for a in args]
    return " ".join(parts)


# -- placement ---------------------------------------------------------------

def unsupported(cloud: str, spec: NodeSpec, what: str) -> ManagedNodeError:
    return ManagedNodeError(
        f"The managed {spec.label} node is not supported on {cloud!r} yet ({what}). "
        f"Set {spec.node_cloud_key} to one of {', '.join(CLOUDS)} that is implemented, "
        f"or redeploy from {spec.settings_hint}.")


def _placement_gcp(spec: NodeSpec, region=None, zone=None) -> dict:
    """GCE placement: project + regional subnet + a zone the launcher can use.

    ``region`` (the operator's pick, else derived from an explicit ``zone`` or the
    persisted node zone, else the configured default) selects the network / subnet /
    zone through :func:`region_config.resolve_region`. For the DEFAULT region this
    returns the flat ``gcp_*`` keys unchanged, so single-region installs behave
    exactly as before. The effective zone may be blank -- the launcher then auto-picks
    a valid zone in the region and retries siblings on capacity exhaustion.
    """
    from ..config import settings
    f = spec.feature

    default_region = region_catalog.normalize("gcp", region_catalog.default_region("gcp"))

    # Effective region: explicit pick -> derived from an explicit zone -> derived from
    # the persisted node zone (gcp_<f>_zone / gcp_zone) -> configured default.
    if region:
        eff_region = region_catalog.normalize("gcp", region)
    elif zone:
        eff_region = region_catalog.region_from_zone(zone)
    else:
        persisted = (config_service.get(spec.infra_key("gcp", "zone"))
                     or config_service.get("gcp_zone"))
        eff_region = (region_catalog.region_from_zone(persisted) if persisted
                      else default_region)

    rc = region_config.resolve_region("gcp", eff_region) or {}
    is_default = (eff_region == default_region)

    def _in_region(z) -> bool:
        z = (z or "").strip()
        return bool(z) and region_catalog.region_from_zone(z) == eff_region

    # Effective zone precedence:
    #   explicit request zone (kept only if it sits in the region) ->
    #   for a region pick: the region's configured zone (only if in-region - never
    #     inherit the default region's flat gcp_zone) ->
    #   for a bare redeploy: the persisted node zone, else the region-config zone ->
    #   "" so the launcher auto-picks the region's first available zone.
    eff_zone = ""
    if zone and _in_region(zone):
        eff_zone = region_catalog.normalize("gcp", zone)
    elif region:
        if _in_region(rc.get("zone")):
            eff_zone = region_catalog.normalize("gcp", rc.get("zone"))
    else:
        for cand in (config_service.get(spec.infra_key("gcp", "zone")), rc.get("zone")):
            if _in_region(cand):
                eff_zone = region_catalog.normalize("gcp", cand)
                break

    # Network is a global VPC name (region-agnostic); the SUBNET is regional. For a
    # non-default region take the subnet from the region entry only -- never fall back
    # to the default region's flat subnet name (it would not exist in this region).
    network = rc.get("network") or settings.gcp_network or "default"
    if is_default:
        subnetwork = rc.get("jumpoint_subnetwork") or rc.get("subnetwork") or ""
    else:
        entry = region_config.load_region_configs("gcp").get(eff_region, {})
        subnetwork = (str(entry.get("jumpoint_subnetwork") or "").strip()
                      or str(entry.get("subnetwork") or "").strip()
                      or rc.get("jumpoint_subnetwork") or rc.get("subnetwork") or "")

    default_boot = getattr(settings, spec.infra_key("gcp", "boot_disk_gb"), 20)
    try:
        boot_disk_gb = int(config_service.get(spec.infra_key("gcp", "boot_disk_gb"))
                           or default_boot)
    except (TypeError, ValueError):
        boot_disk_gb = default_boot
    project_id = config_service.get("gcp_project_id") or settings.gcp_project_id
    return {
        "cloud":        "gcp",
        # The cloud-neutral name for "the account this node lives in", so callers and
        # API responses do not have to know which cloud they are looking at.
        "account":      project_id,
        "project_id":   project_id,
        "region":       eff_region,
        "zone":         eff_zone,
        "name":         (config_service.get(spec.infra_key("gcp", "name"))
                         or getattr(settings, spec.infra_key("gcp", "name"), f"{f}-server")),
        "image":        (config_service.get(spec.infra_key("gcp", "image"))
                         or getattr(settings, spec.infra_key("gcp", "image"), "")),
        "machine_type": (config_service.get(spec.infra_key("gcp", "machine_type"))
                         or getattr(settings, spec.infra_key("gcp", "machine_type"), "")),
        "boot_disk_gb": boot_disk_gb,
        "network":      network,
        # gcp_service normalizes a bare subnet name into a regional self-link using
        # the launch zone's region, so a region-correct bare name is all we need.
        "subnetwork":   subnetwork,
        "network_tag":  (config_service.get(spec.infra_key("gcp", "network_tag"))
                         or getattr(settings, spec.infra_key("gcp", "network_tag"), f)),
    }


def _placement_aws(spec: NodeSpec, region=None, zone=None) -> dict:
    """EC2 placement: a public subnet in the chosen region, plus its VPC for the
    security group.

    The node needs exactly the network shape a Gateway host needs, so it reuses the
    same per-region fields (``jumpoint_subnet_id``, falling back to the sandbox's
    public ``ecs_subnet_id``) rather than introducing a second set that could drift
    out of agreement with what the sandbox actually created.

    Missing prerequisites RAISE, naming the key -- following ``_resolve_ecs``. A
    placement that quietly fell back would put the node in the default region's
    network while the form, the job and the row all said otherwise; that exact defect
    is what the Gateways region picker exists to prevent.

    ``zone`` is not accepted: an EC2 subnet already pins the availability zone, so a
    zone here could only contradict it.
    """
    from ..config import settings
    f = spec.feature

    eff_region = (region_catalog.normalize("aws", region) if region
                  else region_catalog.normalize("aws", region_catalog.default_region("aws")))
    rc = region_config.resolve_region("aws", eff_region) or {}
    subnet_id = (rc.get("jumpoint_subnet_id") or rc.get("ecs_subnet_id") or "").strip()
    vpc_id = (rc.get("vpc_id") or "").strip()
    missing = [k for k, v in (("aws_jumpoint_subnet_id (or ansible_ecs_subnet_id)", subnet_id),
                              ("aws_vpc_id", vpc_id)) if not v]
    if missing:
        raise ManagedNodeError(
            f"The managed {spec.label} node cannot be placed in AWS {eff_region}: "
            f"{', '.join(missing)} is not set for that region. Run the AWS sandbox setup "
            f"for it, or add a per-region config under Settings -> Multi-region.")

    default_boot = getattr(settings, spec.infra_key("aws", "boot_disk_gb"), 30)
    try:
        boot_disk_gb = int(config_service.get(spec.infra_key("aws", "boot_disk_gb"))
                           or default_boot)
    except (TypeError, ValueError):
        boot_disk_gb = default_boot
    return {
        "cloud":         "aws",
        # The VPC is the AWS answer to "where does this live": it is regional, it is
        # what the security group is created in, and without it the node has nowhere
        # to go -- so it is also the honest truthiness check.
        "account":       vpc_id,
        "vpc_id":        vpc_id,
        "subnet_id":     subnet_id,
        "region":        eff_region,
        # Filled in from the launched instance -- an AZ is a property of the subnet,
        # not something the operator picks.
        "zone":          "",
        "name":          (config_service.get(spec.infra_key("aws", "name"))
                          or getattr(settings, spec.infra_key("aws", "name"),
                                     f"{f}-server")),
        "image":         (config_service.get(spec.infra_key("aws", "image"))
                          or getattr(settings, spec.infra_key("aws", "image"), "")),
        "instance_type": (config_service.get(spec.infra_key("aws", "instance_type"))
                          or getattr(settings, spec.infra_key("aws", "instance_type"), "")),
        "boot_disk_gb":  boot_disk_gb,
    }


def _placement_azure(spec: NodeSpec, region=None, zone=None) -> dict:
    """Azure VM placement: a subnet in the chosen location, inside a resource group.

    Reuses the Gateway host's per-region ``jumpoint_subnet_id`` for the same reason the
    AWS side does -- the node wants exactly that network shape, and a second field would
    only drift out of agreement with what the sandbox actually created.

    Blank ``region`` falls back to the location the node was last deployed to (recorded
    in ``azure_<feature>_zone``) before the configured default, so a bare redeploy stays
    where the node is rather than quietly relocating it -- the same stickiness
    ``gcp_<feature>_zone`` gives the GCE path.

    ``zone`` is accepted but ignored: Azure availability zones are not modelled for
    these nodes, and the gateway VM they are built from does not use them either.
    """
    from ..config import settings
    f = spec.feature

    persisted = (config_service.get(spec.infra_key("azure", "zone")) or "").strip()
    eff_region = region_catalog.normalize(
        "azure", region or persisted or region_catalog.default_region("azure"))
    rc = region_config.resolve_region("azure", eff_region) or {}
    rg = (rc.get("resource_group") or "").strip()
    subnet_id = (rc.get("jumpoint_subnet_id") or "").strip()
    missing = [k for k, v in (("azure_resource_group", rg),
                              ("azure_jumpoint_subnet_id", subnet_id)) if not v]
    if missing:
        raise ManagedNodeError(
            f"The managed {spec.label} node cannot be placed in Azure {eff_region}: "
            f"{', '.join(missing)} is not set for that location. Run the Azure sandbox "
            f"setup for it, or add a per-region config under Settings -> Multi-region.")

    default_boot = getattr(settings, spec.infra_key("azure", "boot_disk_gb"), 30)
    try:
        boot_disk_gb = int(config_service.get(spec.infra_key("azure", "boot_disk_gb"))
                           or default_boot)
    except (TypeError, ValueError):
        boot_disk_gb = default_boot
    return {
        "cloud":          "azure",
        # The resource group is the Azure answer to "where does this live": it is what
        # every resource is created in, and without it the node has nowhere to go -- so
        # it is also the honest truthiness check.
        "account":        rg,
        "resource_group": rg,
        "subnet_id":      subnet_id,
        "region":         eff_region,
        # Azure has one location per node and no separate zone, so the zone column
        # shows the location rather than sitting blank.
        "zone":           eff_region,
        "name":           (config_service.get(spec.infra_key("azure", "name"))
                           or getattr(settings, spec.infra_key("azure", "name"),
                                      f"{f}-server")),
        "image":          (config_service.get(spec.infra_key("azure", "image"))
                           or getattr(settings, spec.infra_key("azure", "image"), "")),
        "vm_size":        (config_service.get(spec.infra_key("azure", "vm_size"))
                           or getattr(settings, spec.infra_key("azure", "vm_size"), "")),
        "boot_disk_gb":   boot_disk_gb,
    }


def resolve_placement(cloud: str, spec: NodeSpec, region=None, zone=None) -> dict:
    """Where this node goes, on ``cloud``.

    Always carries ``cloud``, ``account``, ``region``, ``name`` and the ingress rule
    name, so a caller can report and tear down a node without knowing which cloud it
    is on. Per-cloud keys beyond that are the launcher's business.
    """
    cloud = (cloud or "gcp").strip().lower()
    if cloud == "gcp":
        p = _placement_gcp(spec, region=region, zone=zone)
    elif cloud == "aws":
        p = _placement_aws(spec, region=region, zone=zone)
    elif cloud == "azure":
        p = _placement_azure(spec, region=region, zone=zone)
    else:
        raise unsupported(cloud, spec, "placement")
    p["firewall_name"] = firewall_name(p["name"])
    return p


# -- per-cloud host primitives -----------------------------------------------

async def apply_ingress(cloud: str, spec: NodeSpec, placement: dict,
                        source_cidrs: list) -> dict:
    """Make the node's ingress rule match ``source_cidrs`` exactly, and return
    ``{"name", "opened", ...}``.

    Fail-closed is part of the contract on every cloud: an EMPTY set must leave the
    node unreachable, not untouched. How that is achieved differs -- GCP deletes the
    firewall rule, AWS revokes every ingress permission (an in-use security group
    cannot be deleted), Azure deletes the NSG rule -- but ``opened`` is False either
    way, and callers key off that.
    """
    if cloud == "gcp":
        from . import gcp_service
        if spec is RANCHER:
            return await gcp_service.ensure_rancher_firewall(
                placement["project_id"], placement["network"], placement["network_tag"],
                source_cidrs, placement["firewall_name"])
        return await gcp_service.ensure_portainer_firewall(
            placement["project_id"], placement["network"], placement["network_tag"],
            source_cidrs, placement["firewall_name"])
    if cloud == "aws":
        from . import aws_service
        return await aws_service.ensure_node_security_group(
            placement["region"], vpc_id=placement["vpc_id"],
            name=placement["firewall_name"], ports=list(spec.ports),
            source_cidrs=source_cidrs)
    if cloud == "azure":
        from . import azure_service
        return await azure_service.ensure_node_nsg(
            placement["resource_group"], placement["region"],
            name=placement["firewall_name"], ports=list(spec.ports),
            source_cidrs=source_cidrs)
    raise unsupported(cloud, spec, "ingress rules")


async def list_nodes(cloud: str, spec: NodeSpec, placement: dict) -> list:
    """Every managed node of this feature that actually exists in ``cloud``.

    The cloud is the source of truth -- there is no registry table -- so this is a
    tag/label query, and it is what the relocation check and the UI both read.
    """
    if cloud == "gcp":
        from . import gcp_service
        if spec is RANCHER:
            return await gcp_service.list_gce_rancher(placement["project_id"])
        return await gcp_service.list_gce_portainer(placement["project_id"])
    if cloud == "aws":
        from . import aws_service
        return await aws_service.list_ec2_container_nodes(
            placement["region"], spec.feature)
    if cloud == "azure":
        from . import azure_service
        return await azure_service.list_vm_container_nodes(
            placement["resource_group"], spec.feature)
    raise unsupported(cloud, spec, "node listing")
