"""OT (operational technology) demo features: protocol-tunnel presets and the
one-click OT demo cell orchestrator.

Two surfaces share this module:

* **Standalone OT protocol tunnels** — a thin, preset-carrying wrapper over
  ``terraform_pra_service.provision_api_tunnel`` (the generic ``tunnel_type="tcp"``
  jump the k8s API tunnel already uses). State lives in config_service keys
  (``ot_tunnel_{jump,state,meta}_<slug>``), mirroring the k8s API tunnel's
  ``k8s_api_tunnel_*`` precedent — no DB column. These tunnels hold a reference to
  the shared GCP gateway via ``active_standalone_tunnel_count()``, which
  ``jumpoint_host_service`` adds to its idle-teardown sum.

* **The OT demo cell** (job type ``ot_cell_deploy``, dispatched by ``jobs_worker``)
  — drives one ``queued`` VM-deploy child (``gce_deploy`` / ``ec2_deploy`` /
  ``azure_deploy``, per the parent's ``cloud``) through that cloud's vm service
  ``run`` (so the VM gets the Shell Jump, Password Safe onboarding, shared-gateway
  reference, expiry stamp and inventory row exactly as any VM deploy does), then
  wires the OT layer on top: a Web Jump to the HMI and a protocol tunnel to the
  PLC port. Every wiring artifact is written into the CHILD's metadata the moment
  it exists (``ot_web_jump_tf_state`` / ``ot_tunnel_tf_state``), because the child
  row is the cell's inventory record: each cloud's ``_run_destroy`` removes
  whatever of the wiring is present, so the Destroy button and the expiry reaper
  both clean the whole cell with no extra teardown path.
"""
import json
import logging
import re
from datetime import datetime
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class OTError(Exception):
    """Invalid OT request (bad preset, duplicate tunnel, …)."""


class OTCellError(Exception):
    """A cell orchestration step failed. The message is written verbatim to the
    parent job's ``error_message`` — the ONLY field the failed-job page renders —
    so it must carry the remedy, not just the symptom."""


# ── Protocol presets ──────────────────────────────────────────────────────────
# Canonical TCP ports for the OT protocols a PRA protocol tunnel is most often
# demoed against. "custom" (any port) is accepted everywhere a preset key is.

OT_PORT_PRESETS = {
    "modbus":      {"port": 502,   "label": "Modbus TCP"},
    "opcua":       {"port": 4840,  "label": "OPC UA"},
    "dnp3":        {"port": 20000, "label": "DNP3"},
    "s7":          {"port": 102,   "label": "Siemens S7comm"},
    "ethernet-ip": {"port": 44818, "label": "EtherNet/IP"},
}

# A Web Jump renders headless Chromium ON the PRA gateway host. Below ~2 GB the
# renderer is OOM-killed and the session error is indistinguishable from a blocked
# firewall, so the cell deploy refuses to start against an undersized gateway.
MIN_WEB_JUMP_GATEWAY_MB = 2048


def resolve_ports(protocol: str, remote_port: Optional[int] = None,
                  local_port: Optional[int] = None) -> Tuple[int, int]:
    """(local_port, remote_port) for a preset key or ``custom``. The local (rep-side)
    port defaults to the remote port so the operator's Modbus/OPC-UA client config
    reads naturally (127.0.0.1:502 → plc:502)."""
    key = (protocol or "").strip().lower()
    if key == "custom":
        if not remote_port:
            raise OTError("protocol 'custom' requires remote_port")
        rp = int(remote_port)
    else:
        preset = OT_PORT_PRESETS.get(key)
        if not preset:
            raise OTError(
                f"unknown OT protocol '{protocol}' — one of "
                f"{', '.join(sorted(OT_PORT_PRESETS))} or 'custom'")
        rp = int(remote_port or preset["port"])
    lp = int(local_port or rp)
    return lp, rp


def tunnel_slug(name: str) -> str:
    """Config-key-safe slug for a tunnel name (same normalisation the PRA HCL uses)."""
    return re.sub(r"[^a-z0-9_]", "_", (name or "").strip().lower())


def _cfg(key: str) -> str:
    from ..config import settings
    from . import config_service
    return config_service.get(key) or str(getattr(settings, key, "") or "")


# Per-cloud PRA default keys — the same fallback chain each cloud's own Shell Jump
# uses (override → cloud-specific key → bt_*). AWS has no cloud-specific key on
# purpose: aws_vm_service resolves straight from bt_* today, and inventing one here
# would make the cell's jump items land somewhere its Shell Jump does not.
_CLOUD_JUMP_GROUP_KEY = {"gcp": "gcp_bt_jump_group_name", "aws": "", "azure": "azure_bt_jump_group_name"}
_CLOUD_JUMPOINT_KEY = {"gcp": "gcp_jumpoint_name", "aws": "", "azure": "azure_jumpoint_name"}

# The VM-deploy child each cloud's cell parent drives, and the vm service module
# (under web_dashboard.services) whose run() executes it. jobs_worker dispatches the
# PARENT; the child never leaves `queued` except through run_cell_deploy below.
CELL_CHILD_JOB_TYPE = {"gcp": "gce_deploy", "aws": "ec2_deploy", "azure": "azure_deploy"}
_CELL_VM_SERVICE = {"gcp": "gcp_vm_service", "aws": "aws_vm_service", "azure": "azure_vm_service"}


def cell_cloud_for_job_type(job_type: str) -> str:
    """The cloud a cell child job type belongs to, or "" when it is not one."""
    for cloud, jt in CELL_CHILD_JOB_TYPE.items():
        if jt == job_type:
            return cloud
    return ""


def resolve_jump_targets(jump_group: Optional[str], jumpoint_name: Optional[str],
                         cloud: str = "gcp") -> Tuple[str, str]:
    """Resolve the PRA Jump Group / Jumpoint display names with the same fallback
    chain the cloud's own Shell Jump uses (override → cloud-specific → bt_*)."""
    jg_key = _CLOUD_JUMP_GROUP_KEY.get(cloud, "")
    jp_key = _CLOUD_JUMPOINT_KEY.get(cloud, "")
    jg = ((jump_group or "").strip() or (_cfg(jg_key) if jg_key else "")
          or _cfg("bt_jump_group_name"))
    jp = ((jumpoint_name or "").strip() or (_cfg(jp_key) if jp_key else "")
          or _cfg("bt_jumpoint_name"))
    return jg, jp


def jumpoint_overridden(ot_params: dict, cloud: str = "gcp") -> bool:
    """True when the cell names a Gateway other than the configured default — the
    case where the gateway sizing guard must step aside, because it can only reason
    about the dashboard-managed shared gateway (live host or its size config key),
    and refusing an operator-managed Gateway on OUR config default would be a false
    refusal."""
    override = ((ot_params or {}).get("jumpoint_name") or "").strip()
    if not override:
        return False
    _, default_jp = resolve_jump_targets(None, None, cloud)
    return override != default_jp


def pra_preflight_problem(cloud: str = "gcp") -> str:
    """"" when PRA is usable, else the remedy string for the failed-job page.
    Mirrors ``portainer_node_service._pra_configured`` (host + OAuth client +
    Jumpoint), which is the set every terraform PRA apply needs."""
    from . import config_service
    if not config_service.get_bool("pra_enabled"):
        return ("PRA integration is disabled (pra_enabled) — the OT cell exists to "
                "demo PRA-brokered access, so enable it in Settings → Integrations "
                "and redeploy. No VM was launched.")
    missing = [k for k in ("bt_api_host", "bt_client_id") if not _cfg(k)]
    _, jumpoint = resolve_jump_targets(None, None, cloud)
    if not jumpoint:
        missing.append("bt_jumpoint_name")
    if missing:
        return (f"PRA is not fully configured ({', '.join(missing)} missing) — set "
                "the PRA API host, OAuth client and Gateway name in Settings, then "
                "redeploy the cell. No VM was launched.")
    return ""


# ── Gateway sizing guard ──────────────────────────────────────────────────────

_KNOWN_MACHINE_MB = {
    "e2-micro": 1024, "e2-small": 2048, "e2-medium": 4096,
    "f1-micro": 614, "g1-small": 1740,
}

# Conservative per-vCPU minimums across GCE families (n1 is the smallest of each
# family class), so an unknown-generation type is judged pessimistically.
_FAMILY_MB_PER_VCPU = {"standard": 3840, "highmem": 6656, "highcpu": 900}


def gateway_mem_mb(machine_type: str) -> Optional[int]:
    """Approximate RAM for a GCE machine type; None = unknown (treated as OK,
    because refusing to deploy over a type this map hasn't met would be a worse
    failure than a documented risk)."""
    mt = (machine_type or "").strip().lower()
    if not mt:
        return None
    if mt in _KNOWN_MACHINE_MB:
        return _KNOWN_MACHINE_MB[mt]
    m = re.search(r"custom-(\d+)-(\d+)", mt)          # e2-custom-2-4096, n2-custom-…
    if m:
        return int(m.group(2))
    m = re.match(r"[a-z0-9]+-(standard|highmem|highcpu)-(\d+)$", mt)
    if m:
        return _FAMILY_MB_PER_VCPU[m.group(1)] * int(m.group(2))
    return None


# AWS: the shared gateway is an ECS container instance sized by
# bt_ecs_host_instance_type (default t3.small = 2 GB, exactly the minimum). The
# burstable families are the only ones with sub-2GB shapes, so they are pinned
# exactly; everything else parses by size suffix with a conservative floor.
_KNOWN_AWS_INSTANCE_MB = {
    "t2.nano": 512, "t3.nano": 512, "t3a.nano": 512, "t4g.nano": 512,
    "t2.micro": 1024, "t3.micro": 1024, "t3a.micro": 1024, "t4g.micro": 1024,
    "t2.small": 2048, "t3.small": 2048, "t3a.small": 2048, "t4g.small": 2048,
    "t2.medium": 4096, "t3.medium": 4096, "t3a.medium": 4096, "t4g.medium": 4096,
}

# Smallest RAM any current family offers at that size (c* is the floor for
# .medium/.large), so an unknown family is judged pessimistically — same
# reasoning as the GCE per-vCPU floors above.
_AWS_SIZE_FLOOR_MB = {"nano": 512, "micro": 1024, "small": 2048,
                      "medium": 4096, "large": 4096}


def aws_gateway_mem_mb(instance_type: str) -> Optional[int]:
    """Approximate RAM for an EC2 instance type; None = unknown (treated as OK,
    for the same reason as ``gateway_mem_mb``)."""
    it = (instance_type or "").strip().lower()
    if not it:
        return None
    if it in _KNOWN_AWS_INSTANCE_MB:
        return _KNOWN_AWS_INSTANCE_MB[it]
    size = it.split(".", 1)[1] if "." in it else ""
    if size.endswith("xlarge"):
        return 8192   # every current *.xlarge and up is ≥8 GB
    return _AWS_SIZE_FLOOR_MB.get(size)


# Azure: the shared gateway is a VM sized by azure_jumpoint_vm_size (default
# Standard_B2s = 4 GB). The B-series holds every sub-2GB shape an operator is
# likely to pick to save cost, so it is pinned exactly; other families are only
# mapped where unambiguous, unknown = not blocked.
_KNOWN_AZURE_VM_MB = {
    "standard_b1ls": 512, "standard_b1s": 1024, "standard_b1ms": 2048,
    "standard_b2s": 4096, "standard_b2ms": 8192, "standard_b4ms": 16384,
    "standard_b2ats_v2": 1024, "standard_b2als_v2": 4096, "standard_b2as_v2": 8192,
    "standard_a1_v2": 2048, "standard_a2_v2": 4096,
    "standard_d2s_v3": 8192, "standard_d2s_v4": 8192, "standard_d2s_v5": 8192,
}


def azure_gateway_mem_mb(vm_size: str) -> Optional[int]:
    """Approximate RAM for an Azure VM size; None = unknown (treated as OK)."""
    sz = (vm_size or "").strip().lower()
    if not sz:
        return None
    return _KNOWN_AZURE_VM_MB.get(sz)


# Per-cloud guard wiring: the memory model, the size config key the remedy names,
# and that key's minimum/preferred examples. The Settings pointer is shared — the
# three keys sit together under the PRA panel's per-cloud overrides.
_GUARD = {
    "gcp": {"mem": gateway_mem_mb, "key": "gcp_jumpoint_machine_type",
            "minimum": "e2-small", "preferred": "e2-medium",
            "panel": "Settings → Integrations → Privileged Remote Access (GCP overrides)"},
    "aws": {"mem": aws_gateway_mem_mb, "key": "bt_ecs_host_instance_type",
            "minimum": "t3.small", "preferred": "t3.medium",
            "panel": "Settings → Integrations → Privileged Remote Access (AWS overrides)"},
    "azure": {"mem": azure_gateway_mem_mb, "key": "azure_jumpoint_vm_size",
              "minimum": "Standard_B1ms", "preferred": "Standard_B2s",
              "panel": "Settings → Integrations → Privileged Remote Access (Azure overrides)"},
}


def gateway_size_remedy(machine_type: str, gateway_name: str, source: str,
                        cloud: str = "gcp") -> str:
    """"" when the gateway can render a Web Jump, else the full remedy string."""
    g = _GUARD.get(cloud, _GUARD["gcp"])
    mem = g["mem"](machine_type)
    if mem is None or mem >= MIN_WEB_JUMP_GATEWAY_MB:
        return ""
    return (
        f"Gateway sizing guard: {source} is {machine_type} (~{mem} MB RAM). A PRA "
        "Web Jump renders headless Chromium ON the gateway and is OOM-killed below "
        "2 GB — the resulting session failure looks identical to a blocked "
        f"firewall. Set {g['key']} to {g['minimum']} (minimum) or "
        f"{g['preferred']} (preferred) in {g['panel']}, "
        f"delete the gateway VM {gateway_name} "
        "so the next deploy recreates it at the new size, then retry this cell. "
        "No VM was launched."
    )


async def gateway_size_problem(project_id: str, region: str) -> str:
    """Resolve the effective GCP gateway machine type — the LIVE managed VM when it
    exists (a config change never resizes an existing gateway), else the config
    default — and return the remedy string when it is too small, "" otherwise."""
    from . import gcp_service, jumpoint_host_service as jhs
    name = jhs.managed_host_name("gcp")
    machine, source = "", ""
    try:
        zone = jhs._gcp_jumpoint_zone(region)
        for info in await gcp_service.describe_instances(project_id, zone, [name]):
            if info.get("machine_type") and info.get("status") not in ("", "UNKNOWN", "TERMINATED"):
                machine, source = info["machine_type"], f"the live gateway VM {name}"
                break
    except Exception as exc:  # noqa: BLE001 — the config fallback below still guards
        logger.debug("OT gateway guard: live lookup failed (%s) — using config", exc)
    if not machine:
        # _cfg, not config_service.get: the gateway CREATION path (jumpoint_host_service)
        # falls back through config.py's default (e2-medium) when the key is unset or
        # blank, so the guard must read the same way — a raw row read predicted e2-micro
        # for fresh installs and refused a deploy that would in fact have built e2-medium.
        machine = _cfg("gcp_jumpoint_machine_type") or "e2-micro"
        source = "gcp_jumpoint_machine_type (the configured gateway size)"
    return gateway_size_remedy(machine, name, source, "gcp")


async def aws_gateway_size_problem(region: str) -> str:
    """AWS counterpart of ``gateway_size_problem``: the live managed ECS host's
    instance type when one is running, else bt_ecs_host_instance_type read the way
    the creation path reads it (config → settings default t3.small)."""
    from . import aws_service, jumpoint_host_service as jhs
    name = jhs.managed_host_name("aws")
    machine, source = "", ""
    try:
        hosts = await aws_service.find_instances_by_tag(
            region, name_tag=name, states=["pending", "running"])
        if hosts:
            # The tag lookup returns no instance type — one DescribeInstances
            # round trip by id does.
            for info in await aws_service.describe_instances(region, [hosts[0]["instance_id"]]):
                if info.get("instance_type"):
                    machine = info["instance_type"]
                    source = f"the live gateway host {name}"
                    break
    except Exception as exc:  # noqa: BLE001 — the config fallback below still guards
        logger.debug("OT gateway guard(aws): live lookup failed (%s) — using config", exc)
    if not machine:
        machine = _cfg("bt_ecs_host_instance_type") or "t3.small"
        source = "bt_ecs_host_instance_type (the configured gateway size)"
    return gateway_size_remedy(machine, name, source, "aws")


async def azure_gateway_size_problem(location: str) -> str:
    """Azure counterpart of ``gateway_size_problem``: the live managed gateway VM's
    size when it exists, else azure_jumpoint_vm_size read the way the creation path
    reads it (config → Standard_B2s)."""
    from . import azure_service, jumpoint_host_service as jhs
    from .region_config import resolve_region
    name = jhs.managed_host_name("azure")
    machine, source = "", ""
    try:
        loc = jhs._azure_gateway_location(location)
        rg = resolve_region("azure", loc)["resource_group"]
        vm = await azure_service.get_vm(rg, name) if rg else None
        if vm and vm.get("size"):
            machine, source = vm["size"], f"the live gateway VM {name}"
    except Exception as exc:  # noqa: BLE001 — the config fallback below still guards
        logger.debug("OT gateway guard(azure): live lookup failed (%s) — using config", exc)
    if not machine:
        machine = _cfg("azure_jumpoint_vm_size") or "Standard_B2s"
        source = "azure_jumpoint_vm_size (the configured gateway size)"
    return gateway_size_remedy(machine, name, source, "azure")


# ── Standalone OT protocol tunnels ────────────────────────────────────────────

def _tunnel_keys(slug: str) -> Tuple[str, str, str]:
    return (f"ot_tunnel_jump_{slug}", f"ot_tunnel_state_{slug}", f"ot_tunnel_meta_{slug}")


async def create_standalone_tunnel(*, name: str, hostname: str, protocol: str,
                                   remote_port: Optional[int], local_port: Optional[int],
                                   jump_group: Optional[str], jumpoint_name: Optional[str],
                                   region: str, created_by: str,
                                   cloud: str = "gcp") -> dict:
    """Provision a generic-TCP PRA protocol tunnel to any OT endpoint and record it
    in config_service. The Jump Group + Jumpoint must already exist in PRA.

    ``cloud`` names whose shared gateway host the tunnel rides (and therefore whose
    idle-teardown sum it holds a reference in) — the jump item itself is
    cloud-agnostic PRA config."""
    from . import config_service, terraform_pra_service as pra
    lp, rp = resolve_ports(protocol, remote_port, local_port)
    slug = tunnel_slug(name)
    if not slug:
        raise OTError("tunnel name must contain at least one letter or digit")
    jump_key, state_key, meta_key = _tunnel_keys(slug)
    # get_fresh: a tunnel deleted seconds ago must not block re-creation for the
    # config cache's 5s window, and a just-created one must be seen as a duplicate.
    if (config_service.get_fresh(jump_key) or "").strip():
        raise OTError(f"an OT tunnel named '{name}' already exists — delete it first "
                      "or pick another name")
    jg, jp = resolve_jump_targets(jump_group, jumpoint_name, cloud)
    if not (jg and jp):
        raise OTError("PRA Jump Group / Gateway are not configured "
                      "(bt_jump_group_name / bt_jumpoint_name)")
    # Best-effort, like the k8s API tunnel: the target may be reachable through an
    # operator-managed Gateway the dashboard doesn't run a host for. Skipped
    # entirely on a Gateway override — the tunnel rides the NAMED Gateway, so
    # spinning up the shared host would be a billable VM nothing uses.
    if not jumpoint_overridden({"jumpoint_name": jumpoint_name or ""}, cloud):
        try:
            from . import jumpoint_host_service
            await jumpoint_host_service.ensure_jumpoint_host(cloud, region)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OT tunnel: ensure Gateway host failed (non-fatal): %s", exc)

    result = await pra.provision_api_tunnel(
        name=name, hostname=hostname, jump_group_name=jg, jumpoint_name=jp,
        local_port=lp, remote_port=rp, tag="OT",
        client_secret=config_service.get("bt_client_secret"),
    )
    config_service.set(jump_key, str(result.get("tunnel_jump_id") or ""))
    config_service.set(state_key, result.get("tf_state_json") or "")
    config_service.set(meta_key, json.dumps({
        "name": name, "hostname": hostname, "protocol": (protocol or "").lower(),
        "local_port": lp, "remote_port": rp, "cloud": cloud,
        "created_by": created_by, "created_at": datetime.utcnow().isoformat() + "Z",
    }))
    logger.info("OT tunnel '%s' provisioned (jump id %s, %s -> %s:%s)",
                name, result.get("tunnel_jump_id"), lp, hostname, rp)
    return {"slug": slug, "tunnel_jump_id": str(result.get("tunnel_jump_id") or ""),
            "local_port": lp, "remote_port": rp, "cloud": cloud}


def _tunnel_meta(slug: str) -> dict:
    from . import config_service
    try:
        return json.loads(config_service.get_fresh(f"ot_tunnel_meta_{slug}") or "{}")
    except (ValueError, TypeError):
        return {}


def tunnel_cloud(slug: str) -> str:
    """Whose shared gateway a recorded tunnel rides. Rows written before tunnels
    went multi-cloud carry no ``cloud`` — those were all GCP."""
    return (_tunnel_meta(tunnel_slug(slug)).get("cloud") or "gcp").strip().lower()


async def delete_standalone_tunnel(slug: str) -> dict:
    """TF-destroy a standalone tunnel from its stored state and remove its config
    rows (``delete``, not blanking — a blanked row would still be enumerated).
    Returns the tunnel's recorded cloud so the caller can release the right shared
    gateway — the meta row it was recorded in no longer exists by then."""
    from . import config_service, terraform_pra_service as pra
    slug = tunnel_slug(slug)
    cloud = tunnel_cloud(slug)
    jump_key, state_key, meta_key = _tunnel_keys(slug)
    if not (config_service.get_fresh(jump_key) or "").strip():
        return {"ok": True, "removed": False, "cloud": cloud}
    state = config_service.get_fresh(state_key)
    if state:
        try:
            await pra.remove_api_tunnel(state)
        except Exception as exc:  # noqa: BLE001 — mirror the k8s API tunnel: clear anyway
            logger.warning("OT tunnel %s: TF destroy failed (clearing keys anyway — "
                           "the jump item may need manual removal in PRA): %s", slug, exc)
    for key in (jump_key, state_key, meta_key):
        config_service.delete(key)
    return {"ok": True, "removed": True, "cloud": cloud}


def list_standalone_tunnels(cloud: Optional[str] = None) -> list:
    """All recorded standalone OT tunnels (from their ``ot_tunnel_meta_*`` rows),
    optionally scoped to the ones riding ``cloud``'s shared gateway."""
    from . import config_service
    out = []
    for row in config_service.list_all():
        key = row.get("key") or ""
        if not key.startswith("ot_tunnel_meta_"):
            continue
        slug = key[len("ot_tunnel_meta_"):]
        jump_id = (config_service.get_fresh(f"ot_tunnel_jump_{slug}") or "").strip()
        if not jump_id:
            continue
        try:
            meta = json.loads(config_service.get_fresh(key) or "{}")
        except (ValueError, TypeError):
            meta = {}
        meta.setdefault("cloud", "gcp")   # pre-multi-cloud rows were all GCP
        if cloud and (meta.get("cloud") or "gcp") != cloud:
            continue
        meta.update({"slug": slug, "tunnel_jump_id": jump_id})
        out.append(meta)
    return out


def active_standalone_tunnel_count(cloud: str = "gcp") -> int:
    """Live standalone OT tunnels riding ``cloud``'s shared gateway — a reference
    term in that gateway's idle-teardown sum (``jumpoint_host_service``), so tearing
    down a cloud database can't reap the gateway from under a tunnel an operator is
    mid-session on. Deliberately NOT exception-swallowing: the caller's whole
    teardown pass is best-effort, and an error must mean "don't reap", never
    "count is zero". A live jump key whose meta row is missing or unreadable counts
    for EVERY cloud for the same reason — over-counting keeps a host, under-counting
    reaps one."""
    from . import config_service
    count = 0
    for row in config_service.list_all():
        key = row.get("key") or ""
        if not (key.startswith("ot_tunnel_jump_") and (config_service.get(key) or "").strip()):
            continue
        slug = key[len("ot_tunnel_jump_"):]
        meta_raw = config_service.get(f"ot_tunnel_meta_{slug}") or ""
        try:
            tunnel_cloud_val = (json.loads(meta_raw).get("cloud") or "gcp") if meta_raw else "gcp"
        except (ValueError, TypeError):
            count += 1     # unreadable meta: hold a reference everywhere
            continue
        if tunnel_cloud_val == cloud:
            count += 1
    return count


# ── The OT demo cell orchestrator (job type: ot_cell_deploy) ──────────────────

def _get_db_session():
    from ..database import SessionLocal
    return SessionLocal()


async def _cell_gateway_size_problem(cloud: str, meta: dict) -> str:
    """Dispatch the sizing guard to the parent's cloud, reading the placement the
    parent metadata carries for it (GCP: project+region; AWS: region; Azure:
    location)."""
    if cloud == "aws":
        return await aws_gateway_size_problem(meta.get("region") or "")
    if cloud == "azure":
        return await azure_gateway_size_problem(meta.get("location") or "")
    return await gateway_size_problem(meta["project_id"], meta.get("region") or "")


async def aws_airgap_problem(region: str, subnet_id: str) -> str:
    """Refuse an AWS cell whose subnet would hand it a public IP — "" when fine.

    The cell's whole story is a plant network with no path in except PRA. GCE and
    Azure let the deploy form pin the external IP off per instance, and the OT forms
    do. EC2 has no such switch: MapPublicIpOnLaunch on the subnet decides, so a cell
    dropped into the sandbox's public subnet silently comes up internet-addressable
    and the demo asserts something untrue. Checked here, beside the gateway sizing
    guard, because both are "refuse before launching anything" preflights.

    An unreadable subnet is NOT a refusal: ``subnet_auto_assigns_public_ips`` returns
    None when it cannot tell, and blocking a deploy on a failed describe call would
    make a transient AWS error look like a misconfigured subnet.
    """
    from . import config_service
    if not config_service.get_bool("ot_aws_require_private_subnet", True):
        return ""
    subnet_id = (subnet_id or "").strip()
    if not subnet_id:
        return ""
    from . import aws_service
    public = await aws_service.subnet_auto_assigns_public_ips(region, subnet_id)
    if not public:
        return ""
    return (
        f"Subnet {subnet_id} auto-assigns public IPs, so the cell VM would come up "
        f"addressable from the internet — there would be no air gap for PRA to be the "
        f"only way into. EC2 has no per-instance external-IP switch; the subnet "
        f"decides (MapPublicIpOnLaunch). Redeploy the cell into the private sandbox "
        f"subnet — the OT tab's default — or, if a public subnet is genuinely what you "
        f"want, clear 'OT demo cell (AWS): refuse a subnet that auto-assigns public "
        f"IPs' under Settings → Integrations → Privileged Remote Access. No instance "
        f"was launched.")


async def run_cell_deploy(job_id: str, meta: dict) -> None:
    """Run one ``ot_cell_deploy`` job (deploy mode or rewire mode).

    Deploy mode: metadata carries ``cloud`` (absent on pre-multi-cloud rows = gcp)
    and ``children`` = [{job_id, instance_name}] — one queued VM-deploy child this
    parent drives — plus the cloud's placement keys (GCP: project_id/zone/region;
    AWS: region; Azure: location/resource_group).
    Rewire mode: metadata carries ``rewire_child_job_id`` — re-run only the wiring
    steps whose ``*_tf_state`` is absent on an existing, completed cell."""
    import importlib
    from . import job_service
    db = _get_db_session()
    try:
        rewire_child = (meta.get("rewire_child_job_id") or "").strip()
        if rewire_child:
            await _run_rewire(db, job_id, rewire_child)
            return

        cloud = (meta.get("cloud") or "gcp").strip().lower()
        child_job_type = CELL_CHILD_JOB_TYPE.get(cloud)
        if not child_job_type:
            job_service.set_failed(db, job_id, f"unknown OT cell cloud {cloud!r}")
            return
        children = meta.get("children") or []
        child_id = (children[0].get("job_id") if children else "") or ""
        if not child_id:
            job_service.set_failed(db, job_id, "OT cell parent has no child VM job — "
                                               "deploy the cell again from the OT tab.")
            return

        problem = pra_preflight_problem(cloud)
        if problem:
            job_service.set_cancelled(db, child_id)
            job_service.set_failed(db, job_id, problem)
            return

        child_row = job_service.get_job(db, child_id)
        if child_row is None:
            job_service.set_failed(db, job_id, f"child VM job {child_id} not found")
            return
        child_meta = child_row.metadata_dict

        ot_params = child_meta.get("ot_params") or {}
        if jumpoint_overridden(ot_params, cloud):
            job_service.update_progress(
                db, job_id, 5,
                f"Gateway override '{(ot_params.get('jumpoint_name') or '').strip()}' — "
                f"skipping the shared-gateway size check; the host behind that Gateway "
                f"needs ≥2 GB RAM for the Web Jump.")
        else:
            job_service.update_progress(db, job_id, 5,
                                        "Checking the PRA gateway size (a Web Jump needs ≥2 GB)…")
            remedy = await _cell_gateway_size_problem(cloud, meta)
            if remedy:
                job_service.set_cancelled(db, child_id)
                job_service.set_failed(db, job_id, remedy)
                return

        if cloud == "aws":
            job_service.update_progress(
                db, job_id, 8,
                "Checking the cell's subnet is private (EC2 has no per-instance "
                "external-IP switch)…")
            remedy = await aws_airgap_problem(meta.get("region") or "",
                                              child_meta.get("subnet_id") or "")
            if remedy:
                job_service.set_cancelled(db, child_id)
                job_service.set_failed(db, job_id, remedy)
                return

        vm_label = child_meta.get("instance_name") or child_meta.get("vm_name")
        job_service.update_progress(db, job_id, 12,
                                    f"Deploying the OT cell VM ({vm_label})…")
        # The child gets everything a normal VM deploy on its cloud gets — Shell
        # Jump, Password Safe onboarding, the shared-gateway reference, the expiry
        # stamp — because it IS a normal gce/ec2/azure deploy, just driven from
        # here. _run_deploy owns the child's terminal status; the only way run()
        # can RAISE is before the child ever leaves `queued` (e.g. a malformed
        # stored request), and a queued row nothing will drive again must be
        # cancelled, not abandoned — the reconciler skips queued by design.
        vm_service = importlib.import_module(
            f".{_CELL_VM_SERVICE[cloud]}", package=__package__)
        try:
            await vm_service.run(child_id, child_job_type, child_meta)
        except Exception as exc:  # noqa: BLE001
            job_service.set_cancelled(db, child_id)
            raise OTCellError(
                f"The cell VM deploy could not start: {exc} — the VM job was "
                f"cancelled and nothing was created. Deploy a new cell.")

        db.expire_all()
        child_row = job_service.get_job(db, child_id)
        if child_row is None or child_row.status != "completed":
            err = (child_row.error_message if child_row else "") or f"see job {child_id}"
            job_service.set_failed(db, job_id,
                f"The cell VM deploy failed: {err} — nothing was wired. Fix the cause "
                f"and deploy a new cell (job {child_id} holds the VM detail).")
            return

        summary = await _wire_cell(db, job_id, child_id, child_row.metadata_dict, cloud)
        job_service.set_completed(db, job_id, summary)
    except OTCellError as exc:
        job_service.set_failed(db, job_id, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("ot_cell_deploy %s failed", job_id)
        job_service.set_failed(db, job_id, f"OT cell orchestration error: {exc}")
    finally:
        db.close()


async def _run_rewire(db, job_id: str, child_id: str) -> None:
    from . import job_service
    child = job_service.get_job(db, child_id)
    cloud = cell_cloud_for_job_type(child.job_type) if child else ""
    if child is None or not cloud or not child.metadata_dict.get("ot_cell"):
        job_service.set_failed(db, job_id, f"{child_id} is not an OT cell VM job — "
                                           "nothing to re-wire.")
        return
    cmeta = child.metadata_dict
    if cmeta.get("destroyed"):
        job_service.set_failed(db, job_id, "This cell has been destroyed — deploy a "
                                           "new one instead of re-wiring.")
        return
    if child.status != "completed":
        job_service.set_failed(db, job_id, f"The cell's VM job is {child.status} — "
                                           "re-wire applies only to a deployed cell.")
        return
    problem = pra_preflight_problem(cloud)
    if problem:
        job_service.set_failed(db, job_id, problem)
        return
    summary = await _wire_cell(db, job_id, child_id, cmeta, cloud)
    summary["rewired"] = True
    job_service.set_completed(db, job_id, summary)


# How each cloud's cell VM row marks "this deploy already brokered its own way to
# PRA" (so the gateway repair below must not start the shared host), and how a
# repaired shared reference is recorded. The record shapes mirror each vm service's
# own writer — gcp `_JumpointRef.record` (mode "shared", never jumpoint_name, which
# would trigger the paired-delete branch), azure `_AciRef.record` (mode "host"),
# aws `_BatchResources.record` (informational id only: `_active_ec2_count` counts
# every live row, so the reference needs no mode key).
def _cell_has_gateway_ref(cmeta: dict, cloud: str) -> bool:
    if cmeta.get("jumpoint_host_id"):
        return True
    if cloud == "gcp":
        return bool(cmeta.get("jumpoint_mode") == "paired" and cmeta.get("jumpoint_name"))
    if cloud == "azure":
        return bool(cmeta.get("aci_group_name"))
    return False


def _cell_gateway_repair_record(cloud: str, host: str, region: str) -> dict:
    if cloud == "gcp":
        return {"jumpoint_mode": "shared", "jumpoint_host_id": host, "jumpoint_region": region}
    if cloud == "azure":
        return {"jumpoint_mode": "host", "jumpoint_host_id": host, "jumpoint_region": region}
    return {"jumpoint_host_id": host}


# The gateway deploy key the repair's remedy should name, per cloud (the ensure
# reads it through jumpoint_host_service / azure's resolver).
_GATEWAY_DEPLOY_KEY_NAME = {"gcp": "gcp_cloud_run_docker_deploy_key",
                            "aws": "aws_ecs_docker_deploy_key",
                            "azure": "azure_aci_deploy_key"}


# ── Purdue-zone firewalling ───────────────────────────────────────────────────
# The GCP cell has always carried the `ot-sim` network tag, described in the docs as
# "the forward hook for Purdue-zone firewalling" — nothing consumed it, so the cell's
# isolation was really just the sandbox's posture: no NAT on the VM subnet, no public
# IP. That posture is one settings toggle from evaporating. `gcp_vm_nat_enabled` adds
# a priority-900 EGRESS ALLOW on the VM tag every cell also carries, so turning on
# on-demand egress for ONE ordinary VM silently gives every plant cell in the sandbox
# a route to the internet, with nothing in the UI saying so.
#
# These rules make the cell its own zone, independent of that toggle:
#
#   <cell>-ot-egress-deny   800  EGRESS  DENY  all → 0.0.0.0/0
#   <cell>-ot-ingress-allow 800  INGRESS ALLOW tcp ← source_tags=[bt-jumpoint]
#   <cell>-ot-ingress-deny  810  INGRESS DENY  all ← 0.0.0.0/0
#
# 800 is deliberate: it outranks the on-demand egress ALLOW at 900 and the sandbox's
# standing VM-tag DENY at 1000, so the air gap holds whatever those are set to.
#
# The ingress pair uses source_tags, NOT the gateway's address. The shared Gateway is
# ref-counted and recreated on demand; a pinned /32 would silently stop matching the
# day it comes back with a new internal IP, and the symptom — a Web Jump that times
# out — is the one the troubleshooting table already teaches operators to read as an
# undersized gateway. A tag survives recreation.
_PURDUE_EGRESS_PRIORITY = 800
_PURDUE_INGRESS_ALLOW_PRIORITY = 800
_PURDUE_INGRESS_DENY_PRIORITY = 810
OT_CELL_NETWORK_TAG = "ot-sim"
# The network tag the managed GCP Gateway VM carries (gcp_service._JUMPOINT_LABEL).
GATEWAY_NETWORK_TAG = "bt-jumpoint"


def purdue_firewall_enabled() -> bool:
    from . import config_service
    return config_service.get_bool("ot_purdue_firewall_enabled", False)


def _purdue_rule_names(vm: str) -> dict:
    return {"egress_deny":   f"{vm}-ot-egress-deny",
            "ingress_allow": f"{vm}-ot-ingress-allow",
            "ingress_deny":  f"{vm}-ot-ingress-deny"}


def purdue_cell_ports(cmeta: dict) -> list:
    """Every port the cell legitimately serves through the Gateway.

    22 (Shell Jump) and the HMI are always there; the PLC port is whatever the deploy
    chose. The remaining preset ports ride along because the baked image answers OPC UA
    and EtherNet/IP too, and a standalone tunnel to this cell on one of them is a
    supported demo — an allow-list that only knew about the cell's OWN tunnel would
    make those quietly fail.
    """
    ports = {22, int(cmeta.get("ot_hmi_port") or 1881)}
    ot_params = cmeta.get("ot_params") or {}
    if ot_params.get("plc_port"):
        ports.add(int(ot_params["plc_port"]))
    for preset in OT_PORT_PRESETS.values():
        ports.add(int(preset["port"]))
    return sorted(ports)


async def _wire_purdue_firewall(db, parent_id: str, child_id: str, cmeta: dict) -> str:
    """Fence the GCP cell into its own zone. Returns a one-line note for the summary.

    Best-effort by design: a cell that deployed and wired correctly must not be failed
    over a hardening extra. Every rule that IS created is recorded on the child the
    moment it exists, so destroy removes exactly what is there and a re-wire creates
    only what is missing — the same contract as the Web Jump and tunnel above.
    """
    from . import config_service, gcp_service, job_service

    vm = cmeta.get("instance_name") or cmeta.get("vm_name") or ""
    project = cmeta.get("project_id") or _cfg("gcp_project_id")
    network = (cmeta.get("network") or config_service.get("gcp_network")
               or "default")
    if not vm or not project:
        return "Purdue rules skipped (no VM name or project on the cell)"

    names = _purdue_rule_names(vm)
    created = list(cmeta.get("ot_firewall_rules") or [])

    def _record(rule_name):
        if rule_name not in created:
            created.append(rule_name)
        job_service.update_metadata(db, child_id, {"ot_firewall_rules": created})
        cmeta["ot_firewall_rules"] = created

    job_service.update_progress(db, parent_id, 92,
                                "Applying the cell's Purdue-zone firewall rules…")
    try:
        if names["egress_deny"] not in created:
            await gcp_service.ensure_segmentation_rule(
                project=project, name=names["egress_deny"], network=network,
                direction="EGRESS", action="deny",
                priority=_PURDUE_EGRESS_PRIORITY,
                destination_ranges=["0.0.0.0/0"],
                target_tags=[OT_CELL_NETWORK_TAG], protocol="all",
                description="vm-dashboard OT cell: the plant network has no route out, "
                            "whatever gcp_vm_nat_enabled is set to")
            _record(names["egress_deny"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("OT cell %s: egress-deny rule failed: %s", vm, exc)
        return f"Purdue rules incomplete: egress deny failed ({exc})"

    # The ingress DENY is only ever created once its paired ALLOW exists. Reversing
    # that order, or keeping the deny after a failed allow, leaves a cell nothing can
    # reach — including the Gateway brokering the session meant to fix it.
    try:
        if names["ingress_allow"] not in created:
            await gcp_service.ensure_segmentation_rule(
                project=project, name=names["ingress_allow"], network=network,
                direction="INGRESS", action="allow",
                priority=_PURDUE_INGRESS_ALLOW_PRIORITY,
                source_tags=[GATEWAY_NETWORK_TAG],
                target_tags=[OT_CELL_NETWORK_TAG], protocol="tcp",
                ports=purdue_cell_ports(cmeta),
                description="vm-dashboard OT cell: only the PRA Gateway may reach the "
                            "plant cell")
            _record(names["ingress_allow"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("OT cell %s: gateway ingress-allow rule failed: %s", vm, exc)
        return ("Purdue rules partial: no path out of the cell, but the "
                f"Gateway allow-list was not applied ({exc}) — ingress is unchanged")

    try:
        if names["ingress_deny"] not in created:
            await gcp_service.ensure_segmentation_rule(
                project=project, name=names["ingress_deny"], network=network,
                direction="INGRESS", action="deny",
                priority=_PURDUE_INGRESS_DENY_PRIORITY,
                source_ranges=["0.0.0.0/0"],
                target_tags=[OT_CELL_NETWORK_TAG], protocol="all",
                description="vm-dashboard OT cell: everything except the PRA Gateway "
                            "is denied at the plant boundary")
            _record(names["ingress_deny"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("OT cell %s: ingress-deny rule failed: %s", vm, exc)
        return ("Purdue rules partial: no path out of the cell and the Gateway is "
                f"allowed in, but the catch-all ingress deny failed ({exc})")

    return f"Purdue rules applied ({len(created)} firewall rules)"


async def _wire_cell(db, parent_id: str, child_id: str, cmeta: dict,
                     cloud: str = "gcp") -> dict:
    """Provision the OT access layer for a deployed cell VM, skipping any step whose
    Terraform state already exists (which is what makes the rewire path idempotent).
    Every artifact is persisted onto the CHILD's metadata before the next step runs,
    so a failure part-way leaves nothing untracked for the destroy path."""
    from . import config_service, job_service, terraform_pra_service as pra

    # gce/ec2 rows carry instance_name; azure rows carry vm_name.
    vm = cmeta.get("instance_name") or cmeta.get("vm_name") or "ot-cell"
    ip = cmeta.get("private_ip") or cmeta.get("public_ip")
    if not ip:
        raise OTCellError(
            f"VM {vm} reported no IP address — it may have landed outside the expected "
            f"subnet. Destroy the cell and redeploy; job {child_id} has the VM detail.")

    ot = cmeta.get("ot_params") or {}
    hmi_port = int(ot.get("hmi_port") or 1881)
    protocol = (ot.get("protocol") or "modbus").lower()
    local_port, remote_port = resolve_ports(protocol, ot.get("plc_port"),
                                            ot.get("tunnel_local_port"))
    jump_group, jumpoint = resolve_jump_targets(ot.get("jump_group"),
                                                ot.get("jumpoint_name"), cloud)
    client_secret = config_service.get("bt_client_secret")
    rewire_hint = (f"Fix the cause, then use the cell's Re-wire button (POST "
                   f"/api/ot/cell/{child_id}/rewire) — it retries only the missing "
                   f"pieces. The VM was left running.")

    # The Web Jump and tunnel connect THROUGH the shared gateway host. The child
    # normally holds the host reference from its own deploy; if that ensure failed
    # mid-deploy, repair it now — otherwise an idle-teardown from an unrelated
    # feature could reap the gateway from under this cell.
    if not _cell_has_gateway_ref(cmeta, cloud):
        from . import jumpoint_host_service
        job_service.update_progress(db, parent_id, 78, "Ensuring the shared BeyondTrust Gateway host…")
        region = cmeta.get("region") or cmeta.get("location") or ""
        host = None
        try:
            host = await jumpoint_host_service.ensure_jumpoint_host(cloud, region)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OT cell %s: gateway ensure failed: %s", vm, exc)
        if not host:
            raise OTCellError(
                f"The cell VM {vm} is deployed, but the shared BeyondTrust Gateway "
                f"host could not be started (check the {cloud} credentials and the "
                f"gateway deploy key, {_GATEWAY_DEPLOY_KEY_NAME.get(cloud, 'the gateway deploy key')}). "
                f"{rewire_hint}")
        record = _cell_gateway_repair_record(cloud, host, region)
        job_service.update_metadata(db, child_id, record)
        cmeta.update(record)

    hmi_url = f"http://{ip}:{hmi_port}"
    if not cmeta.get("ot_web_jump_tf_state"):
        job_service.update_progress(db, parent_id, 84,
                                    f"Provisioning the PRA Web Jump to the HMI ({hmi_url})…")
        try:
            res = await pra.provision_web_jump(
                name=f"ot-{vm}-hmi", url=hmi_url, jump_group_name=jump_group,
                jumpoint_name=jumpoint, tag="OT", verify_certificate=False,
                client_secret=client_secret)
        except Exception as exc:  # noqa: BLE001
            raise OTCellError(
                f"The cell VM {vm} is deployed (Shell Jump / Password Safe as "
                f"selected), but the HMI Web Jump failed: {exc}. {rewire_hint}")
        wired = {"ot_web_jump_id": str(res.get("web_jump_id") or ""),
                 "ot_web_jump_tf_state": res.get("tf_state_json") or "",
                 "ot_hmi_url": hmi_url}
        job_service.update_metadata(db, child_id, wired)
        cmeta.update(wired)

    if not cmeta.get("ot_tunnel_tf_state"):
        job_service.update_progress(
            db, parent_id, 92,
            f"Provisioning the PRA protocol tunnel ({protocol} → {ip}:{remote_port})…")
        try:
            res = await pra.provision_api_tunnel(
                name=f"ot-{vm}-{protocol}", hostname=ip, jump_group_name=jump_group,
                jumpoint_name=jumpoint, local_port=local_port, remote_port=remote_port,
                tag="OT", client_secret=client_secret)
        except Exception as exc:  # noqa: BLE001
            raise OTCellError(
                f"The cell VM {vm} and its HMI Web Jump are in place, but the "
                f"{protocol} protocol tunnel failed: {exc}. {rewire_hint}")
        wired = {"ot_tunnel_jump_id": str(res.get("tunnel_jump_id") or ""),
                 "ot_tunnel_tf_state": res.get("tf_state_json") or "",
                 "ot_tunnel_protocol": protocol,
                 "ot_tunnel_local_port": local_port,
                 "ot_tunnel_remote_port": remote_port}
        job_service.update_metadata(db, child_id, wired)
        cmeta.update(wired)

    ps_note = ps_checkout_skip_reason(cmeta)
    if not ps_note:
        ps_note = await _wire_ps_checkout(db, parent_id, child_id, cmeta,
                                          jump_group=jump_group,
                                          client_secret=client_secret,
                                          rewire_hint=rewire_hint,
                                          cloud=cloud)

    # GCP only: AWS security groups and Azure NSGs would each need their own shape of
    # this, and both clouds' cells are still awaiting their first live E2E — adding an
    # untested network restriction to an untested deploy would make any failure
    # tomorrow ambiguous. The tag hook and the rules are GCP's today.
    firewall_note = ""
    if cloud == "gcp" and purdue_firewall_enabled():
        firewall_note = await _wire_purdue_firewall(db, parent_id, child_id, cmeta)

    return {
        "vm_job_id": child_id,
        "instance_name": vm,
        "private_ip": ip,
        "hmi_url": cmeta.get("ot_hmi_url") or hmi_url,
        "web_jump_id": cmeta.get("ot_web_jump_id") or "",
        "tunnel_jump_id": cmeta.get("ot_tunnel_jump_id") or "",
        "tunnel_protocol": protocol,
        "tunnel_local_port": local_port,
        "tunnel_remote_port": remote_port,
        "shell_jump_id": cmeta.get("bt_shell_jump_id") or "",
        "vault_account_id": cmeta.get("ot_vault_account_id") or "",
        "vault_account_name": cmeta.get("ot_vault_account_name") or "",
        "ps_checkout": ps_note,
        "purdue_firewall": firewall_note,
    }


def ps_checkout_skip_reason(cmeta: dict) -> str:
    """"" when the PRA-checkout pair should be wired for this cell, else why not.
    The reason lands verbatim in the parent job's result, so it says what would
    make the step apply rather than just that it didn't."""
    from . import config_service
    if not config_service.get_bool("ot_ps_pra_checkout_enabled"):
        return "skipped — disabled (ot_ps_pra_checkout_enabled)"
    if not cmeta.get("ps_managed_account_id"):
        detail = cmeta.get("ps_error") or "Password Safe onboarding was not selected"
        return (f"skipped — the cell has no Password Safe managed account ({detail}); "
                "the PRA checkout account is a SyncedAccounts subscriber of it")
    return ""


# The per-cloud "trigger a Change Password right after onboarding" flag — (key,
# default) exactly as ps_vm_hook.register reads them. Kept only to NAME the cloud's
# rotation posture in logs and progress text; the post-link converge below no longer
# reads it. See _wire_ps_checkout for why the two are different questions.
_PS_CHANGE_FLAG = {
    "gcp": ("passwordsafe_gcp_change_password_on_register", True),
    "aws": ("passwordsafe_ssm_change_password_on_register", False),
    "azure": ("passwordsafe_azure_change_password_on_register", True),
}


async def _wire_ps_checkout(db, parent_id: str, child_id: str, cmeta: dict, *,
                            jump_group: str, client_secret: str,
                            rewire_hint: str, cloud: str = "gcp") -> str:
    """Make the cell's admin credential checkout-able in PRA. Three artifacts, each
    persisted onto the CHILD the moment it exists (so destroy removes exactly what
    is there and rewire retries only what is missing, like the Web Jump/tunnel):

      1. ``ot_vault_tf_state`` — a PRA Vault username/password account, associated
         to the cell's Jump Group for injection, seeded with a placeholder;
      2. ``ot_ps_mirror_tf_state`` — a managed system + account on the "PRA Vault
         Username Password" plugin, named exactly like the Vault account (the
         plugin resolves its PRA-side target by NAME);
      3. ``ot_ps_synced`` — the SyncedAccounts link making the mirror a subscriber
         of the cell's adminuser account, then one Change on the parent so PRA
         holds a real credential now instead of after the next scheduled rotation
         (the deploy-time initial mint ran BEFORE this link existed).
    """
    from . import config_service, job_service, ps_api_service, ps_resource_service, \
        ps_vm_hook, terraform_pra_service as pra

    vm = cmeta.get("instance_name") or cmeta.get("vm_name") or "ot-cell"
    admin_user = _cfg("passwordsafe_managed_account_name") or "adminuser"
    vault_name = cmeta.get("ot_vault_account_name") or f"{vm}-{admin_user}"
    platform_name = (_cfg("ot_ps_pravault_platform")
                     or _cfg("clouddb_ps_pravault_platform")
                     or "PRA Vault Username Password")

    if not cmeta.get("ot_vault_tf_state"):
        job_service.update_progress(
            db, parent_id, 95,
            f"Creating the PRA Vault account {vault_name} (checkout/injection)…")
        group_raw = (_cfg("bt_vault_account_group_id") or "").strip()
        try:
            res = await pra.provision_vault_account(
                name=vault_name, username=admin_user, jump_group_name=jump_group,
                vault_account_group_id=int(group_raw) if group_raw.isdigit() else None,
                client_secret=client_secret)
        except Exception as exc:  # noqa: BLE001
            raise OTCellError(
                f"The cell VM {vm} and its jump items are in place, but the PRA Vault "
                f"checkout account failed: {exc} — check the PRA OAuth client's Vault "
                f"account-management permission. {rewire_hint}")
        wired = {"ot_vault_account_id": str(res.get("vault_account_id") or ""),
                 "ot_vault_account_name": vault_name,
                 "ot_vault_tf_state": res.get("tf_state_json") or ""}
        job_service.update_metadata(db, child_id, wired)
        cmeta.update(wired)

    if not cmeta.get("ot_ps_mirror_tf_state"):
        job_service.update_progress(
            db, parent_id, 96,
            f"Onboarding the {platform_name} mirror into Password Safe…")
        fa_name = (_cfg("ot_ps_pravault_functional_account")
                   or _cfg("clouddb_ps_pravault_functional_account"))
        if not fa_name:
            raise OTCellError(
                f"The PRA Vault account {vault_name} exists, but no functional account "
                f"is configured for the {platform_name!r} plugin — create one in "
                f"Password Safe (username = the PRA OAuth client id, password = its "
                f"secret) and set ot_ps_pravault_functional_account (or "
                f"clouddb_ps_pravault_functional_account). {rewire_hint}")
        try:
            fa = await ps_api_service.get_functional_account(fa_name)
            pname = fa.get("platform_name") or ""
            if not ps_vm_hook._platform_name_ok(pname, "pra vault"):
                raise OTCellError(
                    f"functional account {fa_name!r} is on platform {pname!r}, not a "
                    f"'PRA Vault' plugin platform — the mirror would land on the wrong "
                    f"platform and never write into PRA. {rewire_hint}")
            platform_id = await ps_api_service.get_platform_id(platform_name)
            workgroup_id = await ps_api_service.get_workgroup_id(_cfg("passwordsafe_workgroup"))
            pra_url = _cfg("bt_api_host")
            if not pra_url.lower().startswith("http"):
                pra_url = f"https://{pra_url}"
            reg = await ps_resource_service.register_managed_system(
                name=f"{vm}-pravault", host_name=pra_url, ip_address="127.0.0.1",
                port=443, functional_account_id=fa["id"], platform_id=platform_id,
                workgroup_id=workgroup_id, managed_account_name=vault_name,
                method="pravault")
        except OTCellError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise OTCellError(
                f"The PRA Vault account {vault_name} exists, but its Password Safe "
                f"mirror (the {platform_name!r} managed account) failed: {exc}. "
                f"{rewire_hint}")
        wired = {"ot_ps_mirror_tf_state": reg.get("tf_state_json") or "",
                 "ot_ps_mirror_system_id": str(reg.get("managed_system_id") or ""),
                 "ot_ps_mirror_account_id": str(reg.get("managed_account_id") or "")}
        job_service.update_metadata(db, child_id, wired)
        cmeta.update(wired)

    if not cmeta.get("ot_ps_synced"):
        sub = str(cmeta.get("ot_ps_mirror_account_id") or "").strip()
        if not sub.isdigit():
            raise OTCellError(
                f"The Password Safe mirror system for {vault_name} exists but recorded "
                f"no managed-account id — remove managed system {vm}-pravault in "
                f"Password Safe, then re-wire to recreate it. {rewire_hint}")
        job_service.update_progress(
            db, parent_id, 97,
            f"Syncing {vault_name} to the cell's {admin_user} account…")
        try:
            link = await ps_api_service.link_synced_account(
                parent_account_id=int(cmeta["ps_managed_account_id"]),
                synced_account_id=int(sub),
                expect_subscriber_platform=platform_name)
        except Exception as exc:  # noqa: BLE001
            raise OTCellError(
                f"The PRA Vault account and its Password Safe mirror exist, but the "
                f"SyncedAccounts link failed: {exc} — without it rotations never reach "
                f"PRA. {rewire_hint}")
        if not link.get("confirmed"):
            raise OTCellError(
                f"Password Safe accepted the sync of account {sub} to "
                f"{cmeta['ps_managed_account_id']} but the subscriber is not in the "
                f"parent's synced list — rotations would not reach PRA. {rewire_hint}")
        job_service.update_metadata(db, child_id, {"ot_ps_synced": True})
        cmeta["ot_ps_synced"] = True
        # Converge now rather than at the next scheduled rotation: the deploy-time
        # initial mint ran BEFORE the link existed, so PRA still holds the
        # placeholder. Best-effort — the link guarantees the next change lands.
        #
        # Deliberately NOT the cloud's change-on-register flag. That flag answers
        # "rotate the credential when we first onboard it?"; this answers "a
        # subscriber appeared after the mint, so push one change through it". They
        # only looked alike on GCP/Azure, where the flag defaults on. On AWS
        # (passwordsafe_ssm_change_password_on_register defaults OFF, because SSM
        # auto-management rotates on its own schedule) reading it here left every
        # fresh cell's Vault account holding the placeholder — a checkout that hands
        # the rep a password which does not log in, until some later rotation. The
        # change this triggers is the same one SSM's own schedule performs.
        if config_service.get_bool("ot_ps_checkout_converge", True):
            try:
                await ps_api_service.change_managed_account_password(
                    int(cmeta["ps_managed_account_id"]))
                job_service.update_metadata(db, child_id, {"ot_ps_change_triggered": True})
                cmeta["ot_ps_change_triggered"] = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("OT cell %s: post-link Change Password failed (the pair "
                               "converges at the next scheduled rotation): %s", vm, exc)
        else:
            change_key, change_default = _PS_CHANGE_FLAG.get(cloud, _PS_CHANGE_FLAG["gcp"])
            logger.info("OT cell %s: ot_ps_checkout_converge is off — PRA holds the "
                        "placeholder until the next rotation of %s (%s=%s)",
                        vm, admin_user, change_key,
                        config_service.get_bool(change_key, change_default))

    return (f"{vault_name} synced"
            + (" (rotation triggered)" if cmeta.get("ot_ps_change_triggered")
               else " — PRA holds the placeholder until the next rotation"))
