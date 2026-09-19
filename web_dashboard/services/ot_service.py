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

  The cell's own KubeSolo API is one of these presets: the baked image runs its
  simulators on single-node Kubernetes, so brokered ``kubectl`` into the plant is
  the same kind of jump item as brokered Modbus.

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
import hashlib
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
# Canonical TCP ports for the endpoints a PRA protocol tunnel is most often demoed
# against on a plant network. "custom" (any port) is accepted everywhere a preset
# key is.
#
# ``cell`` marks what the baked ``ot-sim`` image actually SERVES
# (provisioners/ot/ot-sim-debian.sh runs a simulator per marked protocol, and
# KubeSolo itself serves the Kubernetes API). The rest are still offered for
# standalone tunnels to real lab gear — but a cell form must not offer one,
# because a tunnel to a port with no listener is a session failure
# indistinguishable from a firewall block. tests/test_ot_ports.py holds this table
# and what the image runs to each other.
#
# ``plc`` separates the fieldbus protocols from the cell's platform endpoints. Both
# kinds are brokered exactly the same way — one generic-TCP tunnel jump each, so a
# Jump Group policy can grant the Rockwell PLC and the cluster API to different
# people — but they are not the same claim, and a form that lists "Kubernetes API"
# under "PLC protocols" invites the wrong one.

OT_PORT_PRESETS = {
    "modbus":      {"port": 502,   "label": "Modbus TCP",      "cell": True,  "plc": True},
    "opcua":       {"port": 4840,  "label": "OPC UA",          "cell": True,  "plc": True},
    "dnp3":        {"port": 20000, "label": "DNP3",            "cell": False, "plc": True},
    "s7":          {"port": 102,   "label": "Siemens S7comm",  "cell": True,  "plc": True},
    "ethernet-ip": {"port": 44818, "label": "EtherNet/IP",     "cell": True,  "plc": True},
    # The cell's own single-node Kubernetes (KubeSolo), which is what runs the
    # simulators above. Brokered like everything else here: a rep gets kubectl into
    # the plant cluster through a recorded PRA session and no other way in exists.
    "kubesolo":    {"port": 6443,  "label": "Kubernetes API (KubeSolo)",
                    "cell": True,  "plc": False},
}

# The default a cell deploys with when the form sends nothing.
DEFAULT_CELL_PROTOCOLS = ("modbus",)


def cell_protocols() -> list:
    """Preset keys the baked cell image serves, in table order."""
    return [k for k, v in OT_PORT_PRESETS.items() if v.get("cell")]


def plc_protocols() -> list:
    """The fieldbus half of the table — everything a PLC actually speaks."""
    return [k for k, v in OT_PORT_PRESETS.items() if v.get("plc")]


def resolve_cell_protocols(ot_params: dict) -> list:
    """The protocols a cell should have tunnels for, de-duplicated and ordered.

    Reads ``protocols`` (multi-protocol cells) and falls back to the singular
    ``protocol`` — which is what every cell deployed before multi-protocol
    carries, so those keep re-wiring and tearing down unchanged."""
    ot = ot_params or {}
    raw = ot.get("protocols")
    if not raw:
        single = (ot.get("protocol") or "").strip().lower()
        raw = [single] if single else list(DEFAULT_CELL_PROTOCOLS)
    out = []
    for name in raw:
        key = (name or "").strip().lower()
        if key and key not in out:
            out.append(key)
    return out


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
        # Role-tagged since the DMZ broker arrived. A cell deployed before that has one
        # untagged child, and that child is the cell — so the default keeps every
        # existing parent replayable.
        child_id = next((c.get("job_id") for c in children
                         if (c.get("role") or "cell") == "cell"), "") or ""
        broker_id = next((c.get("job_id") for c in children
                          if c.get("role") == "broker"), "") or ""
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
        vm_service = importlib.import_module(
            f".{_CELL_VM_SERVICE[cloud]}", package=__package__)

        # ── The plant's own identity broker, before the plant floor ──────────────
        # Order matters twice over. The token has to exist before the CELL deploys,
        # because the cell's Entitle registration runs inside that deploy and names the
        # agent that will broker it. And the broker has to be up before the cell is
        # wired, because a cell registered against an agent whose host does not exist
        # is a demo that half-works in the direction nobody checks.
        if broker_id:
            problem = in_plant_agent_problem(
                (job_service.get_job(db, broker_id).metadata_dict or {}).get(
                    "image_self_link", ""),
                (job_service.get_job(db, broker_id).metadata_dict or {}).get(
                    "machine_type", ""), cloud)
            if problem:
                job_service.set_cancelled(db, child_id)
                job_service.set_cancelled(db, broker_id)
                job_service.set_failed(db, job_id, problem)
                return

            job_service.update_progress(db, job_id, 9,
                                        "Minting this plant's own Entitle agent token…")
            token_name = agent_token_name(vm_label or "")
            try:
                await ensure_agent_token(child_id, vm_label or "")
            except Exception as exc:  # noqa: BLE001
                job_service.set_cancelled(db, child_id)
                job_service.set_cancelled(db, broker_id)
                job_service.set_failed(db, job_id, (
                    f"The plant's Entitle agent token could not be minted: {exc}. The "
                    f"cell registers against its OWN agent, so there is nothing to "
                    f"register into until this works. Check the Entitle API key and "
                    f"that no agent named {token_name} already exists. No VM was launched."))
                return
            # Both rows carry the name: the cell's deploy reads it to register against
            # this agent, and the broker's is where an operator looks to find out which
            # agent this host runs.
            job_service.update_metadata(db, child_id, {
                "ot_agent_token_name": token_name,
                "ot_agent_token_key": agent_token_config_key(child_id),
                "ot_broker_job_id": broker_id})
            job_service.update_metadata(db, broker_id, {
                "ot_agent_token_name": token_name, "ot_cell_job_id": child_id})
            child_meta = (job_service.get_job(db, child_id).metadata_dict or child_meta)

            broker_row = job_service.get_job(db, broker_id)
            broker_label = (broker_row.metadata_dict or {}).get("instance_name") or "broker"
            job_service.update_progress(db, job_id, 11,
                                        f"Deploying the plant's DMZ broker ({broker_label})…")
            try:
                await vm_service.run(broker_id, child_job_type, broker_row.metadata_dict)
            except Exception as exc:  # noqa: BLE001
                job_service.set_cancelled(db, broker_id)
                job_service.set_cancelled(db, child_id)
                raise OTCellError(
                    f"The DMZ broker deploy could not start: {exc} — both VM jobs were "
                    f"cancelled and nothing was created. Deploy a new cell.")
            db.expire_all()
            broker_row = job_service.get_job(db, broker_id)
            if broker_row is None or broker_row.status != "completed":
                err = (broker_row.error_message if broker_row else "") or f"see job {broker_id}"
                job_service.set_cancelled(db, child_id)
                job_service.set_failed(db, job_id, (
                    f"The DMZ broker deploy failed: {err} — the cell was not launched, "
                    f"because a cell whose Entitle agent has nowhere to run would "
                    f"register access nobody can use (job {broker_id} has the detail)."))
                return

        job_service.update_progress(db, job_id, 12,
                                    f"Deploying the OT cell VM ({vm_label})…")
        # The child gets everything a normal VM deploy on its cloud gets — Shell
        # Jump, Password Safe onboarding, the shared-gateway reference, the expiry
        # stamp — because it IS a normal gce/ec2/azure deploy, just driven from
        # here. _run_deploy owns the child's terminal status; the only way run()
        # can RAISE is before the child ever leaves `queued` (e.g. a malformed
        # stored request), and a queued row nothing will drive again must be
        # cancelled, not abandoned — the reconciler skips queued by design.
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

        summary = await _wire_cell(db, job_id, child_id, child_row.metadata_dict, cloud,
                                   broker_id=broker_id)
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
async def cell_resource_alive(cloud: str, cmeta: dict) -> Optional[bool]:
    """Does this cell's VM still exist in its cloud? ``True`` / ``False`` /
    ``None`` when the question could not be answered.

    Read by the "clear a failed cell" path, which must never hide a billable
    orphan. The job row cannot answer it on its own: a deploy that died *inside*
    the create call never got a ``vm_id`` / ``instance_id`` written, whether or
    not the VM it was creating survived — so an absent id means "we don't know",
    not "nothing was created".

    ``None`` (no creds, no Reader on the group, an API hiccup, a row too old to
    carry its placement) is a third state on purpose, the same shape as
    ``azure_service._sku_trusted_launch_capable``: the caller must treat it as
    unknown and make the operator say so explicitly, never silently as "gone".
    """
    name = (cmeta.get("instance_name") or cmeta.get("vm_name") or "").strip()
    if not name:
        return None
    try:
        if cloud == "azure":
            from . import azure_service
            rg = (cmeta.get("resource_group") or "").strip()
            return bool(await azure_service.get_vm(rg, name)) if rg else None
        if cloud == "gcp":
            from . import gcp_service
            project = (cmeta.get("project_id") or "").strip()
            zone = (cmeta.get("zone") or "").strip()
            if not (project and zone):
                return None
            return bool(await gcp_service.describe_instances(project, zone, [name]))
        if cloud == "aws":
            from . import aws_service
            region = (cmeta.get("region") or "").strip()
            if not region:
                return None
            # By NAME tag, not instance id — the id is exactly what a deploy that
            # failed inside RunInstances does not have. Terminated instances are
            # excluded: a terminated row is not a billable orphan.
            found = await aws_service.find_instances_by_tag(
                region, name_tag=name,
                states=["pending", "running", "stopping", "stopped"])
            return bool(found)
    except Exception as exc:  # noqa: BLE001
        logger.warning("OT cell liveness probe failed (cloud=%s name=%s): %s",
                       cloud, name, exc)
        return None
    return None


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
            "ingress_agent": f"{vm}-ot-ingress-agent",
            "ingress_deny":  f"{vm}-ot-ingress-deny"}


# ── The DMZ broker's zone ─────────────────────────────────────────────────────
# The broker is the plant's industrial DMZ host: it runs the BeyondTrust Entitle agent
# INSIDE the plant, which is the only arrangement in which "Entitle manages access to
# plant resources" is true as stated. It is also the only machine in the demo with any
# way out, and that way out is two ports to one destination.
#
#   <broker>-dmz-egress-entitle-<hash>  790  EGRESS  ALLOW tcp 443,8080 → the Entitle set
#   <broker>-dmz-egress-dns-udp / -tcp  790  EGRESS  ALLOW 53 → the metadata resolver
#   <broker>-dmz-egress-deny            800  EGRESS  DENY  all
#   <broker>-dmz-ingress-allow          800  INGRESS ALLOW tcp 22 ← Gateway + runner
#   <broker>-dmz-ingress-deny           810  INGRESS DENY  all
#
# 790 outranks the 800 deny for those destinations and for nothing else. The cell's own
# rules are untouched apart from one new line admitting the broker on :22 — so the two
# rule sets together ARE the Purdue diagram, which is the point: in a demo you can read
# them out of `gcloud compute firewall-rules list` instead of drawing them on a slide.
OT_DMZ_NETWORK_TAG = "ot-dmz"
_DMZ_EGRESS_ALLOW_PRIORITY = 790
# The agent's channel. 8080 is not telemetry and not optional — it carries
# ENTITLE_PROXY_URL, the agent's primary channel, in plain HTTP (docs/kubesolo.md).
ENTITLE_AGENT_PORTS = ("443", "8080")
# A cloud VM resolves through the link-local metadata server, and the 800 deny covers
# it like everything else. One rule carries one protocol, so DNS costs two.
_METADATA_RESOLVER_CIDR = "169.254.169.254/32"


def _dmz_rule_names(vm: str, digest: str = "") -> dict:
    return {"egress_entitle": f"{vm}-dmz-egress-entitle-{digest or 'none'}",
            "egress_dns_udp": f"{vm}-dmz-egress-dns-udp",
            "egress_dns_tcp": f"{vm}-dmz-egress-dns-tcp",
            "egress_deny":    f"{vm}-dmz-egress-deny",
            "ingress_allow":  f"{vm}-dmz-ingress-allow",
            "ingress_deny":   f"{vm}-dmz-ingress-deny"}


def entitle_agent_endpoint() -> str:
    """The hostname the agent dials home on, for the configured tenant's region."""
    from . import entitle_egress
    return f"agent.{entitle_egress.region()}.entitle.io"


def resolve_entitle_destinations() -> Tuple[list, str]:
    """``(cidrs, provenance)`` for the plant's one outbound hole.

    The operator's list wins, because that is what a real plant has: a firewall ticket
    naming addresses. Failing that, the endpoint is resolved here and the provenance
    says exactly that — an honest answer, not a contract, and the difference is
    recorded on the job so nobody quotes it as one.

    ``entitle_egress.cidrs()`` is deliberately NOT reused: it holds the addresses
    Entitle connects FROM, for an ingress allow-list. Pointing an egress rule at them
    would be a guess in the wrong direction.

    ``([], "")`` means neither source could answer, which upstream turns into a
    refusal — never a silent widening to 0.0.0.0/0.
    """
    raw = _cfg("ot_entitle_egress_cidrs")
    listed = [c.strip() for c in raw.replace(";", ",").split(",") if c.strip()]
    if listed:
        return sorted(set(listed)), "ot_entitle_egress_cidrs"
    host = entitle_agent_endpoint()
    try:
        import socket
        addrs = {info[4][0] for info in socket.getaddrinfo(
            host, 443, socket.AF_INET, socket.SOCK_STREAM)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("OT DMZ: %s did not resolve (%s)", host, exc)
        addrs = set()
    if not addrs:
        return [], ""
    stamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    return sorted(f"{a}/32" for a in addrs), f"resolved {host} at {stamp}"


def entitle_destination_digest(cidrs: list) -> str:
    """Eight hex characters of the destination set, for the rule's name.

    ``gcp_service.ensure_segmentation_rule`` is create-only — a rule that already
    exists is left alone, never reconciled — so the SET has to be part of the name.
    Without that, a changed address list would leave the old rule in place and report
    success, which is the failure mode this whole feature exists to avoid."""
    return hashlib.sha256(",".join(sorted(cidrs)).encode()).hexdigest()[:8]


def dmz_egress_open_ports() -> bool:
    from . import config_service
    return config_service.get_bool("ot_dmz_egress_open_ports", False)


def dmz_egress_problem() -> str:
    """"" when the plant's one hole can be drawn, else the remedy for the job page."""
    if dmz_egress_open_ports():
        return ""
    cidrs, _ = resolve_entitle_destinations()
    if cidrs:
        return ""
    return (
        f"The plant's outbound path to Entitle cannot be drawn: {entitle_agent_endpoint()} "
        f"did not resolve and no addresses are configured. A firewall rule takes "
        f"addresses, not names, so set ot_entitle_egress_cidrs (Settings → Integrations "
        f"→ Privileged Remote Access) to the ranges BeyondTrust gave you. If you cannot "
        f"get a list, ot_dmz_egress_open_ports allows the broker 443/8080 to anywhere "
        f"instead — a weaker claim the demo then has to own. No VM was launched.")


def purdue_cell_ports(cmeta: dict) -> list:
    """Every port the cell legitimately serves through the Gateway.

    22 (Shell Jump) and the HMI are always there; the PLC port is whatever the deploy
    chose. The remaining preset ports ride along because the baked image answers OPC UA
    and EtherNet/IP too — and, on the KubeSolo runtime, the cluster API on 6443 — and a
    standalone tunnel to this cell on one of them is a supported demo; an allow-list
    that only knew about the cell's OWN tunnel would make those quietly fail.
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

    # The plant's own identity broker, and the only other thing allowed to speak to
    # the cell at all: one port, from one zone. Its own rule rather than another
    # source on the Gateway's, so the audit line reads as the sentence it is — "the
    # DMZ host may SSH here" — and so a cell with no broker has no such line.
    if cmeta.get("ot_broker_job_id"):
        try:
            if names["ingress_agent"] not in created:
                await gcp_service.ensure_segmentation_rule(
                    project=project, name=names["ingress_agent"], network=network,
                    direction="INGRESS", action="allow",
                    priority=_PURDUE_INGRESS_ALLOW_PRIORITY,
                    source_tags=[OT_DMZ_NETWORK_TAG],
                    target_tags=[OT_CELL_NETWORK_TAG], protocol="tcp", ports=[22],
                    description="vm-dashboard OT cell: the plant's own Entitle agent, "
                                "on the DMZ host, may mint ephemeral accounts here")
                _record(names["ingress_agent"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("OT cell %s: DMZ ingress-allow rule failed: %s", vm, exc)
            return ("Purdue rules partial: the Gateway is allowed in, but the plant's "
                    f"Entitle agent is not ({exc}) — its grants would not log in")

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


# The play that puts the agent on the broker. A repo sample has to be uploaded to a
# storage backend before a run can resolve it by bare filename, which is the same
# contract every other Config-Management run has.
ENTITLE_AGENT_PLAYBOOK = "entitle-agent-install.yml"
# Baked by provisioners/ot/ot-sim-debian.sh (OT_ROLE=broker). Naming the archive rather
# than the repo is what keeps the plant's egress allow-list down to one destination.
BROKER_CHART_PATH = "/opt/entitle/charts/entitle-agent.tgz"


async def _install_plant_agent(db, parent_id: str, child_id: str, cmeta: dict,
                               broker_id: str, bmeta: dict, cloud: str = "gcp") -> str:
    """Queue the Config-Management run that installs the Entitle agent on the broker.

    Queued rather than run inline, and as an ordinary ``ansible_local`` job: it then
    gets the durable runner, the job page its output belongs on, and the secret
    handling that binds the token BY REFERENCE — the job row carries the config key,
    never the token. The run reaches a private broker only from an in-cloud runner,
    which ``config_runner_problem`` refused the whole deploy without.
    """
    from . import ansible_run_meta, job_service, storage_service
    from types import SimpleNamespace

    if cmeta.get("ot_agent_install_job_id"):
        return f"agent install already queued (job {cmeta['ot_agent_install_job_id']})"
    broker_ip = (bmeta.get("private_ip") or "").strip()
    if not broker_ip:
        return ("agent install skipped: the DMZ broker reported no private address, so "
                "there is nothing for the runner to reach")

    parent = job_service.get_job(db, parent_id)
    payload = SimpleNamespace(
        asset=ENTITLE_AGENT_PLAYBOOK,
        target=broker_ip,
        cloud=cloud,
        ansible_user="",
        extra_vars={
            # A baked chart, not the repo: anycred.github.io is a CDN and no honest
            # allow-list can name it, so the plant carries the chart instead.
            "entitle_agent_chart": BROKER_CHART_PATH,
            "entitle_agent_chart_repo": "",
            "entitle_agent_replicas": 1,
            # Prove the path from a POD before helm runs. The endpoint is what the
            # firewall was opened to; the cell is what the agent must reach to mint an
            # ephemeral account. Both failures are cheap here and expensive later.
            "entitle_probe_endpoint": entitle_agent_endpoint(),
            "entitle_probe_ssh_target": (cmeta.get("private_ip") or ""),
        },
        secret_vars={"entitle_agent_token": agent_token_config_key(child_id)},
        secret_become_source="",
        secret_ssh_key_source="",
        managed_account=None,
        managed_become=None,
        epml_token_var="",
    )
    try:
        job = job_service.create_job(
            db,
            job_type="ansible_local",
            created_by=(parent.created_by if parent else "system"),
            workgroup="ansible",
            metadata=ansible_run_meta.run_meta(
                payload,
                description=f"Entitle agent → {bmeta.get('instance_name') or broker_ip} "
                            f"(the plant's own broker)",
                asset_backend=storage_service.active_backend()),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("OT cell: agent install could not be queued: %s", exc)
        return f"agent install could not be queued ({exc})"

    job_service.update_metadata(db, child_id, {"ot_agent_install_job_id": job.id})
    job_service.update_metadata(db, broker_id, {"ot_agent_install_job_id": job.id})
    cmeta["ot_agent_install_job_id"] = job.id
    job_service.update_progress(db, parent_id, 96,
                                "Installing the Entitle agent on the plant's broker…")
    return (f"agent install queued as job {job.id} — it runs the same "
            f"{ENTITLE_AGENT_PLAYBOOK} an on-prem site would")


async def _wire_cell(db, parent_id: str, child_id: str, cmeta: dict,
                     cloud: str = "gcp", broker_id: str = "") -> dict:
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
    protocols = resolve_cell_protocols(ot)
    # plc_port / tunnel_local_port are single-protocol overrides (the "custom"
    # case), so they only apply when the cell has exactly one protocol —
    # applying one port to every tunnel would point them all at the same target.
    single = len(protocols) == 1
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
                client_secret=client_secret,
                comments=f"Auto-provisioned by Infrastructure Management Dashboard "
                         f"(OT demo cell {vm}, FUXA HMI)")
        except Exception as exc:  # noqa: BLE001
            raise OTCellError(
                f"The cell VM {vm} is deployed (Shell Jump / Password Safe as "
                f"selected), but the HMI Web Jump failed: {exc}. {rewire_hint}")
        wired = {"ot_web_jump_id": str(res.get("web_jump_id") or ""),
                 "ot_web_jump_tf_state": res.get("tf_state_json") or "",
                 "ot_hmi_url": hmi_url}
        job_service.update_metadata(db, child_id, wired)
        cmeta.update(wired)

    # One protocol tunnel per selected protocol. Each is appended to ot_tunnels
    # the moment it exists, so a failure part-way leaves nothing untracked for
    # the destroy path and a Re-wire retries exactly the missing ones.
    #
    # A cell deployed before multi-protocol carries the singular ot_tunnel_*
    # keys instead. _cell_tunnels projects those into the list shape (same
    # tf_state, so the existing jump item is adopted rather than duplicated),
    # which is also how every destroy path keeps honouring them.
    existing = _cell_tunnels(cmeta)
    done = {t.get("protocol") for t in existing}
    for protocol in protocols:
        if protocol in done:
            continue
        local_port, remote_port = resolve_ports(
            protocol,
            ot.get("plc_port") if single else None,
            ot.get("tunnel_local_port") if single else None)
        job_service.update_progress(
            db, parent_id, 92,
            f"Provisioning the PRA protocol tunnel ({protocol} → {ip}:{remote_port})…")
        try:
            res = await pra.provision_api_tunnel(
                name=f"ot-{vm}-{protocol}", hostname=ip, jump_group_name=jump_group,
                jumpoint_name=jumpoint, local_port=local_port, remote_port=remote_port,
                tag="OT", client_secret=client_secret,
                comments=f"Auto-provisioned by Infrastructure Management Dashboard "
                         f"(OT demo cell {vm}, {protocol})")
        except Exception as exc:  # noqa: BLE001
            raise OTCellError(
                f"The cell VM {vm} and its HMI Web Jump are in place, but the "
                f"{protocol} protocol tunnel failed: {exc}. {rewire_hint}")
        entry = {"protocol": protocol,
                 "jump_id": str(res.get("tunnel_jump_id") or ""),
                 "tf_state": res.get("tf_state_json") or "",
                 "local_port": local_port,
                 "remote_port": remote_port}
        existing.append(entry)
        done.add(protocol)
        wired = {"ot_tunnels": existing}
        # Mirror the FIRST tunnel into the singular keys, and only ever the first:
        # they are what an older reader (and OTCellInfo's back-compat fields) looks
        # at. Keyed on "this is the only tunnel so far" rather than on the loop
        # index, so extending a legacy cell cannot repoint them at a later
        # protocol while the original tunnel keeps living in the list.
        if len(existing) == 1:
            wired.update({"ot_tunnel_jump_id": entry["jump_id"],
                          "ot_tunnel_tf_state": entry["tf_state"],
                          "ot_tunnel_protocol": protocol,
                          "ot_tunnel_local_port": local_port,
                          "ot_tunnel_remote_port": remote_port})
        job_service.update_metadata(db, child_id, wired)
        cmeta.update(wired)

    ps_note = ps_checkout_skip_reason(cmeta)
    if not ps_note:
        ps_note = await _wire_ps_checkout(db, parent_id, child_id, cmeta,
                                          jump_group=jump_group,
                                          client_secret=client_secret,
                                          rewire_hint=rewire_hint,
                                          cloud=cloud)

    # All three clouds now, each through its own primitive (see "The same two zones"
    # below). AWS and Azure converge the cell's and the broker's zones in ONE call
    # because the cell's rule names the broker's group/address as a source, so the two
    # cannot be written independently the way GCP's tag-based rules can.
    broker_id = broker_id or (cmeta.get("ot_broker_job_id") or "")
    bmeta: dict = {}
    if broker_id:
        broker_row = job_service.get_job(db, broker_id)
        bmeta = (broker_row.metadata_dict if broker_row else None) or {}

    # On AWS and Azure the zone also requires a BROKER, which GCP's does not. That is
    # deliberate rather than incidental: those two clouds' cells have live miles on
    # them, and zoning them means replacing an instance's security groups or a NIC's
    # NSG — not adding an independent rule the way GCP does. Arriving only with the
    # in-plant agent keeps that out of every deploy that did not ask for it, and the
    # agent path refuses loudly without its prerequisites. An operator who wants the
    # zoning alone on those clouds deploys a cell with Entitle.
    firewall_note = ""
    dmz_note = ""
    if purdue_firewall_enabled():
        if cloud == "gcp":
            firewall_note = await _wire_purdue_firewall(db, parent_id, child_id, cmeta)
        elif cloud == "aws" and broker_id:
            firewall_note = await _wire_zones_aws(db, parent_id, child_id, cmeta,
                                                  broker_id, bmeta)
        elif cloud == "azure" and broker_id:
            firewall_note = await _wire_zones_azure(db, parent_id, child_id, cmeta,
                                                    broker_id, bmeta)

    # The DMZ zone, then the agent — in that order, so what the probe proves is the
    # narrow path itself rather than a hole that is about to close behind it. On GCP
    # the DMZ zone is its own call; on the other two it was written above, with the
    # cell's, for the source-ordering reason in the comment there.
    agent_note = ""
    if broker_id:
        if cloud == "gcp" and purdue_firewall_enabled():
            dmz_note = await _wire_dmz_firewall(db, parent_id, broker_id, bmeta)
        agent_note = await _install_plant_agent(db, parent_id, child_id, cmeta,
                                                broker_id, bmeta, cloud=cloud)

    return {
        "vm_job_id": child_id,
        "instance_name": vm,
        "private_ip": ip,
        "hmi_url": cmeta.get("ot_hmi_url") or hmi_url,
        "web_jump_id": cmeta.get("ot_web_jump_id") or "",
        # Read back off the cell rather than off the loop above: a Re-wire whose
        # tunnels all already exist never enters that loop, and reporting from
        # loop variables raised UnboundLocalError instead of summarising the cell.
        "tunnels": [{"protocol": t.get("protocol") or "",
                     "jump_id": t.get("jump_id") or "",
                     "local_port": t.get("local_port") or 0,
                     "remote_port": t.get("remote_port") or 0}
                    for t in _cell_tunnels(cmeta)],
        "tunnel_jump_id": cmeta.get("ot_tunnel_jump_id") or "",
        "tunnel_protocol": cmeta.get("ot_tunnel_protocol") or "",
        "tunnel_local_port": int(cmeta.get("ot_tunnel_local_port") or 0),
        "tunnel_remote_port": int(cmeta.get("ot_tunnel_remote_port") or 0),
        "shell_jump_id": cmeta.get("bt_shell_jump_id") or "",
        "vault_account_id": cmeta.get("ot_vault_account_id") or "",
        "vault_account_name": cmeta.get("ot_vault_account_name") or "",
        "ps_checkout": ps_note,
        "purdue_firewall": firewall_note,
        "dmz_zone": dmz_note,
        "plant_agent": agent_note,
        "broker_job_id": broker_id,
    }


# ── Can this cell carry its own Entitle agent? ────────────────────────────────
# Every one of these is a refusal BEFORE anything is launched, with the remedy in the
# message, because the alternative is a demo that deploys green and fails at the only
# moment that matters — the vendor's login. Same posture as pra_preflight_problem and
# the gateway sizing guard.
#
# 8 GB, not 4: the agent alone requests 1Gi and KubeSolo idles at ~200 MB. The estimate
# comes from gateway_mem_mb, which is deliberately pessimistic for families it has not
# met, so the floor is set below e2-standard-2's true 8192 MB rather than at it.
MIN_BROKER_MEM_MB = 7000


def broker_instance_name(cell_name: str) -> str:
    """The DMZ broker's VM name for a cell. `-dmz`, because that is what it is."""
    return f"{(cell_name or 'ot-cell').strip()}-dmz"[:62]


def config_runner_problem(cloud: str = "gcp") -> str:
    """"" when the agent can be installed on a private broker, else the remedy.

    The dashboard has no route to a private cell — the worker reaches managed nodes
    over public addresses (rancher_node_service._dashboard_cidr) — so the install has
    to run from a runner inside the cloud, and the broker's firewall has to admit it.
    """
    mode = (_cfg(f"ansible_runner_{cloud}") or _cfg("ansible_runner") or "local").strip().lower()
    if mode in ("", "local"):
        return (f"The Entitle agent is installed by a Config-Management run against the "
                f"broker's PRIVATE address, and the dashboard host has no route to it. "
                f"Set ansible_runner_{cloud} to this cloud's in-cloud runner (Settings → "
                f"Integrations → Config Management) so the run executes inside the VPC. "
                f"No VM was launched.")
    if cloud == "gcp" and not (_cfg("gcp_run_subnetwork") or _cfg("gcp_ansible_vpc_connector")):
        return ("The Cloud Run Ansible runner has no VPC egress configured, so it cannot "
                "reach the broker's private address: set gcp_run_subnetwork (direct VPC "
                "egress) or gcp_ansible_vpc_connector. No VM was launched.")
    if not _cfg("ot_config_runner_source_cidr").strip():
        return ("The DMZ broker admits only the PRA Gateway and the Config-Management "
                "runner, and the runner's source range is not configured: set "
                "ot_config_runner_source_cidr to the runner's subnet/connector range, or "
                "the agent could never be installed or repaired. No VM was launched.")
    return ""


def broker_shape_problem(machine_type: str, cloud: str = "gcp") -> str:
    """"" when the broker can hold the agent, else the remedy.

    The estimate comes from the same per-cloud tables the Gateway sizing guard uses, so
    a family none of them has met returns None and is allowed through rather than
    refused on a guess.
    """
    sizer = {"aws": aws_gateway_mem_mb, "azure": azure_gateway_mem_mb}.get(
        cloud, gateway_mem_mb)
    mem = sizer(machine_type)
    if mem is None or mem >= MIN_BROKER_MEM_MB:
        return ""
    bigger = {"aws": "t3.large", "azure": "Standard_D2s_v3"}.get(cloud, "e2-standard-2")
    return (f"The DMZ broker is {machine_type} (~{mem} MB). The Entitle agent requests "
            f"1Gi on its own and KubeSolo idles at ~200 MB on top, so the pod would sit "
            f"Pending with no other symptom. Pick {bigger} (8 GB) or larger for the "
            f"broker. No VM was launched.")


def in_plant_agent_problem(broker_image: str = "", broker_machine_type: str = "",
                           cloud: str = "gcp") -> str:
    """"" when this cell can broker its own identity, else the remedy.

    Registering an OT cell in Entitle means the agent runs IN the plant, on the cell's
    DMZ broker. The alternative — an agent in some cluster outside it — is both a
    false claim for the demo and, once the Purdue zoning is on, silently broken: the
    cell admits the PRA Gateway and nothing else, so that agent's SSH is dropped while
    the registration and the grant both still report success.
    """
    from . import config_service
    if cloud not in ("gcp", "aws", "azure"):
        return (f"The in-plant Entitle agent has no zoning for {cloud.upper()}. Deploy "
                f"this cell without Entitle.")
    if not config_service.get_bool("entitle_registration_enabled", False):
        return ("Entitle resource registration is off (entitle_registration_enabled), so "
                "there is nothing for the plant's agent to register into. Enable it in "
                "Settings → Integrations → Entitle, or deploy the cell without Entitle.")
    if not (broker_image or "").strip():
        return ("The cell's Entitle agent runs on a DMZ broker, which needs its own "
                "image: bake one with OT_ROLE=broker (provisioners/ot/README.md) and "
                "pick it in the form's 'DMZ broker image'. No VM was launched.")
    if not purdue_firewall_enabled():
        return ("The in-plant Entitle agent needs the Purdue zoning turned on "
                "(ot_purdue_firewall_enabled): the agent's one way out is a hole in the "
                "plant boundary, and without the boundary there is nothing to make a "
                "hole in — the cell would simply have whatever egress the subnet does. "
                "No VM was launched.")
    problem = dmz_egress_problem()
    if problem:
        return problem
    problem = config_runner_problem(cloud)
    if problem:
        return problem
    # Each zone has to be able to name the PRA Gateway as a source, or applying it
    # would fence the cell away from the one thing brokering access to it. GCP names a
    # network tag, which always exists; the other two name a group or an address that
    # an operator has to have configured.
    if cloud == "aws" and not aws_gateway_source_groups():
        return ("The cell's zone allows the PRA Gateway in by SECURITY GROUP, and "
                "bt_ecs_jumpoint_security_group_id is unset — so the zone would deny "
                "the Gateway along with everything else and the demo would have no way "
                "in. Set it to the Gateway host's security group. No VM was launched.")
    if cloud == "azure" and not (_cfg("azure_jumpoint_name") and _cfg("azure_resource_group")):
        return ("The cell's NSG allows the PRA Gateway in by address, resolved from "
                "azure_jumpoint_name in azure_resource_group, and one of those is unset "
                "— so the zone would deny the Gateway along with everything else. No VM "
                "was launched.")
    return broker_shape_problem(broker_machine_type, cloud)


# ── The plant's own Entitle agent token ──────────────────────────────────────
# One token per cell, minted in the install's own tenant and destroyed with the cell.
# Not `ensure_agent_token()`: that one is the install-wide singleton, written into the
# global config keys, and every cell sharing it would mean every cell's agent could
# broker every other cell's resources. The POV path (pov_entitle_agent) draws the same
# line for the same reason, with the same key shape.
def agent_token_config_key(vm_job_id: str) -> str:
    return f"ot/{vm_job_id}/entitle_agent_token"


def agent_token_state_key(vm_job_id: str) -> str:
    return f"ot/{vm_job_id}/entitle_agent_tf_state"


def agent_token_name(cell_name: str) -> str:
    """The Entitle-side name of this cell's agent token.

    Derived from the cell, so an operator reading the agent list in Entitle can tell
    which plant a token belongs to — and so a leftover from a destroyed cell is
    recognisable rather than anonymous."""
    slug = re.sub(r"[^a-z0-9-]+", "-", (cell_name or "cell").strip().lower()).strip("-")
    slug = slug or "cell"
    # Most cells are already named ot-something; "ot-ot-cell-01" reads like a bug.
    return (slug if slug.startswith("ot-") else f"ot-{slug}")[:60]


async def ensure_agent_token(vm_job_id: str, cell_name: str) -> str:
    """Mint this cell's agent token once and remember it; return the value.

    Stored through config_service (encrypted at rest, resolvable from an external
    vault) and never written into job metadata — the job carries the KEY, which is
    what every run and every teardown needs."""
    from . import config_service, entitle_registration_service as ent
    existing = (config_service.get(agent_token_config_key(vm_job_id)) or "").strip()
    if existing:
        return existing
    minted = await ent.mint_agent_token(agent_token_name(cell_name))
    config_service.set(agent_token_config_key(vm_job_id), minted["token"])
    if minted.get("tf_state_json"):
        # The state is the only way to destroy the token later, and minting is
        # create-only in Entitle — losing it strands the token in the tenant.
        config_service.set(agent_token_state_key(vm_job_id), minted["tf_state_json"])
    return minted["token"]


async def destroy_agent_token(vm_job_id: str) -> str:
    """Destroy this cell's agent token. Returns a one-line note; never raises.

    Teardown of a demo must not be blockable by the identity provider, so a failure
    here is reported and the stash is KEPT — a token we could not destroy is one an
    operator still needs to be able to find."""
    from . import config_service, entitle_registration_service as ent
    state = (config_service.get(agent_token_state_key(vm_job_id)) or "").strip()
    if not state:
        config_service.delete(agent_token_config_key(vm_job_id))
        return ""
    try:
        await ent.deregister(state)
    except Exception as exc:  # noqa: BLE001
        logger.warning("OT cell %s: agent token not destroyed: %s", vm_job_id, exc)
        return (f"the plant's Entitle agent token was not destroyed ({exc}) — remove it "
                f"in Entitle → Org Settings → Agents")
    config_service.delete(agent_token_config_key(vm_job_id))
    config_service.delete(agent_token_state_key(vm_job_id))
    return ""


async def _wire_dmz_firewall(db, parent_id: str, broker_id: str, bmeta: dict) -> str:
    """Fence the DMZ broker: one destination out, the Gateway and the runner in.

    Mirrors ``_wire_purdue_firewall`` deliberately — same recording discipline (each
    rule lands in the row's ``ot_firewall_rules`` the moment it exists, so the cloud's
    own destroy path cleans it), same best-effort posture, same ordering rule that an
    allow is never left behind a deny that outranks it.

    The one asymmetry is the egress ALLOW, which carries a digest of its destination
    set in its name: ``ensure_segmentation_rule`` never reconciles an existing rule,
    so a changed address list has to arrive as a differently-named rule or it would
    not arrive at all.
    """
    from . import config_service, gcp_service, job_service

    vm = bmeta.get("instance_name") or ""
    project = bmeta.get("project_id") or _cfg("gcp_project_id")
    network = (bmeta.get("network") or config_service.get("gcp_network") or "default")
    if not vm or not project:
        return "DMZ rules skipped (no broker name or project)"

    cidrs, provenance = resolve_entitle_destinations()
    open_ports = dmz_egress_open_ports()
    if not cidrs and not open_ports:
        return ("DMZ rules skipped: no Entitle destination addresses "
                "(ot_entitle_egress_cidrs) and the open-ports escape hatch is off")
    if not cidrs:
        cidrs, provenance = ["0.0.0.0/0"], "ot_dmz_egress_open_ports"

    names = _dmz_rule_names(vm, entitle_destination_digest(cidrs))
    created = list(bmeta.get("ot_firewall_rules") or [])

    def _record(rule_name):
        if rule_name not in created:
            created.append(rule_name)
        job_service.update_metadata(db, broker_id, {"ot_firewall_rules": created})
        bmeta["ot_firewall_rules"] = created

    job_service.update_progress(db, parent_id, 93,
                                "Opening the plant's one outbound path (Entitle only)…")
    # Every stale sibling goes first: a re-wire after the address set changed must not
    # leave the previous allow in place beside the new one, or the hole is the union of
    # both and nobody can tell from the rule list which one is live.
    for stale in list(created):
        if stale.startswith(f"{vm}-dmz-egress-entitle-") and stale != names["egress_entitle"]:
            try:
                await gcp_service.delete_firewall_rule(project, stale)
                created.remove(stale)
                job_service.update_metadata(db, broker_id, {"ot_firewall_rules": created})
                bmeta["ot_firewall_rules"] = created
            except Exception as exc:  # noqa: BLE001
                logger.warning("OT DMZ %s: stale rule %s not removed: %s", vm, stale, exc)

    try:
        if names["egress_entitle"] not in created:
            await gcp_service.ensure_segmentation_rule(
                project=project, name=names["egress_entitle"], network=network,
                direction="EGRESS", action="allow", priority=_DMZ_EGRESS_ALLOW_PRIORITY,
                destination_ranges=cidrs, target_tags=[OT_DMZ_NETWORK_TAG],
                protocol="tcp", ports=list(ENTITLE_AGENT_PORTS),
                description=f"vm-dashboard OT broker: the Entitle agent's channel "
                            f"({provenance}) — the plant's only way out")
            _record(names["egress_entitle"])
        for key, proto in (("egress_dns_udp", "udp"), ("egress_dns_tcp", "tcp")):
            if names[key] not in created:
                await gcp_service.ensure_segmentation_rule(
                    project=project, name=names[key], network=network,
                    direction="EGRESS", action="allow",
                    priority=_DMZ_EGRESS_ALLOW_PRIORITY,
                    destination_ranges=[_METADATA_RESOLVER_CIDR],
                    target_tags=[OT_DMZ_NETWORK_TAG], protocol=proto, ports=[53],
                    description="vm-dashboard OT broker: DNS, without which the "
                                "channel above is a name that resolves to nothing")
                _record(names[key])
    except Exception as exc:  # noqa: BLE001
        logger.warning("OT DMZ %s: egress allow failed: %s", vm, exc)
        return f"DMZ rules incomplete: the Entitle path was not opened ({exc})"

    # Only now the deny: the allows above outrank it, but a deny created first would
    # strand the agent for however long the next call takes.
    try:
        if names["egress_deny"] not in created:
            await gcp_service.ensure_segmentation_rule(
                project=project, name=names["egress_deny"], network=network,
                direction="EGRESS", action="deny", priority=_PURDUE_EGRESS_PRIORITY,
                destination_ranges=["0.0.0.0/0"], target_tags=[OT_DMZ_NETWORK_TAG],
                protocol="all",
                description="vm-dashboard OT broker: everything except the Entitle "
                            "channel stops at the plant boundary")
            _record(names["egress_deny"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("OT DMZ %s: egress deny failed: %s", vm, exc)
        return (f"DMZ rules partial: the Entitle path is open but the catch-all egress "
                f"deny failed ({exc}) — the broker can still reach the internet")

    sources = [GATEWAY_NETWORK_TAG]
    runner_cidr = _cfg("ot_config_runner_source_cidr").strip()
    try:
        if names["ingress_allow"] not in created:
            await gcp_service.ensure_segmentation_rule(
                project=project, name=names["ingress_allow"], network=network,
                direction="INGRESS", action="allow",
                priority=_PURDUE_INGRESS_ALLOW_PRIORITY,
                source_tags=sources,
                source_ranges=[runner_cidr] if runner_cidr else None,
                target_tags=[OT_DMZ_NETWORK_TAG], protocol="tcp", ports=[22],
                description="vm-dashboard OT broker: the PRA Gateway and the "
                            "Config-Management runner may reach the DMZ host")
            _record(names["ingress_allow"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("OT DMZ %s: ingress allow failed: %s", vm, exc)
        return (f"DMZ rules partial: the broker has its outbound path but nothing may "
                f"reach it ({exc}) — the agent cannot be installed or repaired")

    try:
        if names["ingress_deny"] not in created:
            await gcp_service.ensure_segmentation_rule(
                project=project, name=names["ingress_deny"], network=network,
                direction="INGRESS", action="deny", priority=_PURDUE_INGRESS_DENY_PRIORITY,
                source_ranges=["0.0.0.0/0"], target_tags=[OT_DMZ_NETWORK_TAG],
                protocol="all",
                description="vm-dashboard OT broker: nothing else reaches the DMZ host")
            _record(names["ingress_deny"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("OT DMZ %s: ingress deny failed: %s", vm, exc)
        return ("DMZ rules partial: the Gateway is allowed in, but the catch-all "
                f"ingress deny failed ({exc})")

    where = "anywhere on 443/8080" if provenance == "ot_dmz_egress_open_ports" else \
            f"{len(cidrs)} destination(s) from {provenance}"
    return f"DMZ zone applied ({len(created)} rules; the agent may reach {where})"


# ── The same two zones, in each cloud's own vocabulary ───────────────────────
# The GCP rules above are the reference shape, not a template to translate. Each cloud
# says the same thing with a different primitive, and glossing over the differences is
# how a demo ends up claiming a boundary it does not actually have:
#
#   GCP    VPC firewall rules on network tags. Priorities, explicit DENY, and rules
#          that exist independently of the instance.
#   AWS    Security groups: pure allow-lists. No priorities and no deny — "denied" is
#          expressed by absence, which reads BETTER in a demo (`describe-security-
#          groups` is the whole boundary) but has two traps. Groups UNION their allows,
#          so a zone binds only when it REPLACES the instance's groups; and a new group
#          is created allowing ALL egress, so the plant's air gap has to be made true
#          by revoking that rule rather than by not adding one.
#   Azure  NSG rules: priorities and real Deny, the closest of the three to GCP. The
#          difference is the starting posture — Azure's default outbound access lets a
#          VM with no public IP reach the internet anyway — so here the outbound Deny
#          is not hardening ON TOP of an air gap, it IS the air gap. Until this runs,
#          an Azure cell's isolation was a claim in the docs and nothing else.
#
# The DNS hole is each platform's own resolver, never a public one: link-local on AWS,
# the AzurePlatformDNS service tag on Azure, the metadata address on GCP.
_AWS_RESOLVER_CIDR = "169.254.169.253/32"
_AZURE_RESOLVER_TAG = "AzurePlatformDNS"
# Azure NSG priorities. Unique per direction, and the outbound allows have to outrank
# the outbound deny the same way the GCP 790s outrank the 800.
_AZ_PRIO = {"egress_entitle": 790, "egress_dns_udp": 791, "egress_dns_tcp": 792,
            "egress_deny": 800, "ingress_allow": 800, "ingress_agent": 810,
            "ingress_deny": 900}


def _aws_zone_names(vm: str) -> dict:
    """The cell's and broker's security-group names. Same words as the GCP rules."""
    return {"cell": f"{vm}-ot-zone"[:255], "dmz": f"{vm}-dmz-zone"[:255]}


def _azure_zone_names(vm: str) -> dict:
    return {"cell": f"{vm}-ot-zone"[:80], "dmz": f"{vm}-dmz-zone"[:80]}


def aws_gateway_source_groups() -> list:
    """The PRA Gateway host's security groups — the AWS analogue of `bt-jumpoint`.

    A group id rather than an address, for the reason the GCP rules use a tag: the
    shared Gateway host is ref-counted and recreated on demand, and a pinned /32 would
    stop matching the day it comes back.
    """
    raw = _cfg("bt_ecs_jumpoint_security_group_id")
    return [g.strip() for g in raw.replace(";", ",").split(",") if g.strip()]


async def azure_gateway_source_cidrs() -> list:
    """The Azure Gateway VM's private address, as a one-entry /32 list.

    Azure's honest analogue of a network tag is an Application Security Group, which
    would have to be attached to the Gateway VM's own NIC — a change to the one Azure
    path that has live miles on it. So the address is resolved here instead and the
    zone is repaired by *Re-wire* if the Gateway is ever rebuilt, which the
    troubleshooting table says in as many words.
    """
    from . import azure_service
    name = _cfg("azure_jumpoint_name")
    rg = _cfg("azure_resource_group")
    if not name or not rg:
        return []
    try:
        vm = await azure_service.get_vm(rg, name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("OT zone: could not resolve the Azure Gateway %s (%s)", name, exc)
        return []
    ip = ((vm or {}).get("internal_ip") or (vm or {}).get("private_ip") or "").strip()
    return [f"{ip}/32"] if ip else []


def _entitle_egress_targets() -> Tuple[list, str]:
    """``(cidrs, provenance)`` for the broker's one hole, escape hatch included."""
    if dmz_egress_open_ports():
        return ["0.0.0.0/0"], "ot_dmz_egress_open_ports"
    return resolve_entitle_destinations()


# ── AWS ───────────────────────────────────────────────────────────────────────

async def _wire_zones_aws(db, parent_id: str, child_id: str, cmeta: dict,
                          broker_id: str = "", bmeta: Optional[dict] = None) -> str:
    """Fence an AWS cell — and its broker, when it has one — into their zones.

    Best-effort like the GCP one, and recorded the same way: every group that exists is
    written onto its own job row the moment it does, so teardown removes exactly what is
    there and a Re-wire converges only what is missing.

    The broker's group is converged FIRST, because it is the ingress source the cell's
    group names — the AWS spelling of "tcp 22 from the ot-dmz tag".
    """
    from . import aws_service, job_service

    region = (cmeta.get("region") or _cfg("aws_region") or "").strip()
    vpc_id = (cmeta.get("vpc_id") or "").strip()
    vm = cmeta.get("instance_name") or cmeta.get("vm_name") or ""
    if not (region and vpc_id and vm):
        return "OT zones skipped (no region, VPC or instance name on the cell)"

    gateway_groups = aws_gateway_source_groups()
    if not gateway_groups:
        return ("OT zones skipped: bt_ecs_jumpoint_security_group_id is unset, so the "
                "Gateway has no group to allow in and the zone would lock the cell away "
                "from the thing brokering access to it")

    names = _aws_zone_names(vm)
    dmz_group_id = ""
    notes = []

    if broker_id and bmeta:
        cidrs, provenance = _entitle_egress_targets()
        runner_cidr = _cfg("ot_config_runner_source_cidr").strip()
        try:
            zone = await aws_service.ensure_ot_zone_security_group(
                region, vpc_id=vpc_id, name=names["dmz"],
                ingress=[{"port": 22,
                          "group_ids": gateway_groups,
                          "cidrs": [runner_cidr] if runner_cidr else []}],
                egress=(
                    [{"protocol": "tcp", "port": int(p), "cidrs": cidrs}
                     for p in ENTITLE_AGENT_PORTS if cidrs]
                    + [{"protocol": "udp", "port": 53, "cidrs": [_AWS_RESOLVER_CIDR]},
                       {"protocol": "tcp", "port": 53, "cidrs": [_AWS_RESOLVER_CIDR]}]),
            )
            dmz_group_id = zone["id"]
            job_service.update_metadata(db, broker_id, {
                "ot_zone_group": names["dmz"], "ot_zone_group_id": dmz_group_id,
                "ot_entitle_destinations": cidrs,
                "ot_entitle_destination_source": provenance})
            instance_id = (bmeta.get("instance_id") or "").strip()
            if instance_id:
                previous = await aws_service.set_instance_security_groups(
                    region, instance_id=instance_id, group_ids=[dmz_group_id])
                job_service.update_metadata(db, broker_id,
                                            {"ot_zone_groups_replaced": previous})
            where = ("anywhere on 443/8080" if provenance == "ot_dmz_egress_open_ports"
                     else f"{len(cidrs)} destination(s) from {provenance}")
            notes.append(f"DMZ zone applied (the agent may reach {where})")
        except Exception as exc:  # noqa: BLE001
            logger.warning("OT cell %s: DMZ security group failed: %s", vm, exc)
            notes.append(f"DMZ zone failed ({exc}) — the agent has no way out")

    try:
        ingress = [{"port": int(p), "group_ids": gateway_groups}
                   for p in purdue_cell_ports(cmeta)]
        if dmz_group_id:
            ingress.append({"port": 22, "group_ids": [dmz_group_id]})
        zone = await aws_service.ensure_ot_zone_security_group(
            region, vpc_id=vpc_id, name=names["cell"], ingress=ingress, egress=[])
        job_service.update_metadata(db, child_id, {
            "ot_zone_group": names["cell"], "ot_zone_group_id": zone["id"]})
        cmeta["ot_zone_group"] = names["cell"]
        instance_id = (cmeta.get("instance_id") or "").strip()
        if instance_id:
            previous = await aws_service.set_instance_security_groups(
                region, instance_id=instance_id, group_ids=[zone["id"]])
            job_service.update_metadata(db, child_id,
                                        {"ot_zone_groups_replaced": previous})
        notes.append("plant zone applied (no egress at all; inbound only from the "
                     "Gateway" + (" and the plant's broker" if dmz_group_id else "") + ")")
    except Exception as exc:  # noqa: BLE001
        logger.warning("OT cell %s: cell security group failed: %s", vm, exc)
        notes.append(f"plant zone failed ({exc})")

    return "; ".join(notes)


# ── Azure ─────────────────────────────────────────────────────────────────────

async def _wire_zones_azure(db, parent_id: str, child_id: str, cmeta: dict,
                            broker_id: str = "", bmeta: Optional[dict] = None) -> str:
    """Fence an Azure cell — and its broker — into their zones.

    Note what the cell's rule list contains that no Azure cell had before: an outbound
    Deny. Without it the cell reaches the internet through Azure's default outbound
    access despite having no public IP, so this is the first code that makes the demo's
    air-gap claim true on Azure rather than merely documented.
    """
    from . import azure_service, job_service

    rg = (cmeta.get("resource_group") or _cfg("azure_resource_group") or "").strip()
    location = (cmeta.get("region") or cmeta.get("location") or "").strip()
    vm = cmeta.get("instance_name") or cmeta.get("vm_name") or ""
    if not (rg and location and vm):
        return "OT zones skipped (no resource group, location or VM name on the cell)"

    gateway_cidrs = await azure_gateway_source_cidrs()
    if not gateway_cidrs:
        return ("OT zones skipped: the Azure Gateway's address could not be resolved "
                "(azure_jumpoint_name), and a zone written without it would lock the "
                "cell away from the thing brokering access to it")

    names = _azure_zone_names(vm)
    broker_ip = ((bmeta or {}).get("private_ip") or "").strip()
    notes = []

    if broker_id and bmeta:
        cidrs, provenance = _entitle_egress_targets()
        runner_cidr = _cfg("ot_config_runner_source_cidr").strip()
        rules = [
            {"name": "ingress-allow", "priority": _AZ_PRIO["ingress_allow"],
             "direction": "Inbound", "access": "Allow", "protocol": "Tcp",
             "sources": gateway_cidrs + ([runner_cidr] if runner_cidr else []),
             "ports": [22],
             "description": "OT DMZ broker: the PRA Gateway and the Config-Management "
                            "runner, and nothing else"},
            {"name": "ingress-deny", "priority": _AZ_PRIO["ingress_deny"],
             "direction": "Inbound", "access": "Deny", "protocol": "*",
             "description": "OT DMZ broker: everything else is denied at the boundary"},
            {"name": "egress-dns-udp", "priority": _AZ_PRIO["egress_dns_udp"],
             "direction": "Outbound", "access": "Allow", "protocol": "Udp",
             "destinations": [_AZURE_RESOLVER_TAG], "ports": [53]},
            {"name": "egress-dns-tcp", "priority": _AZ_PRIO["egress_dns_tcp"],
             "direction": "Outbound", "access": "Allow", "protocol": "Tcp",
             "destinations": [_AZURE_RESOLVER_TAG], "ports": [53]},
            {"name": "egress-deny", "priority": _AZ_PRIO["egress_deny"],
             "direction": "Outbound", "access": "Deny", "protocol": "*",
             "description": "OT DMZ broker: no way out but the Entitle allow above"},
        ]
        if cidrs:
            rules.insert(0, {
                "name": "egress-entitle", "priority": _AZ_PRIO["egress_entitle"],
                "direction": "Outbound", "access": "Allow", "protocol": "Tcp",
                "destinations": cidrs, "ports": list(ENTITLE_AGENT_PORTS),
                "description": f"OT DMZ broker: the Entitle agent's channel "
                               f"({provenance})"})
        try:
            zone = await azure_service.ensure_ot_zone_nsg(
                rg, location, name=names["dmz"], rules=rules)
            job_service.update_metadata(db, broker_id, {
                "ot_zone_nsg": names["dmz"], "ot_zone_nsg_id": zone["id"],
                "ot_entitle_destinations": cidrs,
                "ot_entitle_destination_source": provenance})
            broker_vm = (bmeta or {}).get("instance_name") or ""
            if broker_vm and zone.get("id"):
                await azure_service.attach_nsg_to_vm(rg, broker_vm, nsg_id=zone["id"])
            where = ("anywhere on 443/8080" if provenance == "ot_dmz_egress_open_ports"
                     else f"{len(cidrs)} destination(s) from {provenance}")
            notes.append(f"DMZ zone applied (the agent may reach {where})")
        except Exception as exc:  # noqa: BLE001
            logger.warning("OT cell %s: DMZ NSG failed: %s", vm, exc)
            notes.append(f"DMZ zone failed ({exc}) — the agent has no way out")

    rules = [
        {"name": "ingress-allow", "priority": _AZ_PRIO["ingress_allow"],
         "direction": "Inbound", "access": "Allow", "protocol": "Tcp",
         "sources": gateway_cidrs, "ports": purdue_cell_ports(cmeta),
         "description": "OT cell: only the PRA Gateway may reach the plant"},
        {"name": "ingress-deny", "priority": _AZ_PRIO["ingress_deny"],
         "direction": "Inbound", "access": "Deny", "protocol": "*",
         "description": "OT cell: everything except the Gateway is denied"},
        {"name": "egress-deny", "priority": _AZ_PRIO["egress_deny"],
         "direction": "Outbound", "access": "Deny", "protocol": "*",
         "description": "OT cell: the plant network has no route out — this rule is "
                        "what makes that true, because Azure's default outbound access "
                        "gives a VM with no public IP one anyway"},
    ]
    if broker_ip:
        rules.insert(1, {
            "name": "ingress-agent", "priority": _AZ_PRIO["ingress_agent"],
            "direction": "Inbound", "access": "Allow", "protocol": "Tcp",
            "sources": [f"{broker_ip}/32"], "ports": [22],
            "description": "OT cell: the plant's own Entitle agent, on the DMZ host, "
                           "may mint ephemeral accounts here"})
    try:
        zone = await azure_service.ensure_ot_zone_nsg(
            rg, location, name=names["cell"], rules=rules)
        job_service.update_metadata(db, child_id, {
            "ot_zone_nsg": names["cell"], "ot_zone_nsg_id": zone["id"]})
        cmeta["ot_zone_nsg"] = names["cell"]
        if zone.get("id"):
            await azure_service.attach_nsg_to_vm(rg, vm, nsg_id=zone["id"])
        notes.append("plant zone applied (outbound denied outright; inbound only from "
                     "the Gateway" + (" and the plant's broker" if broker_ip else "") + ")")
    except Exception as exc:  # noqa: BLE001
        logger.warning("OT cell %s: cell NSG failed: %s", vm, exc)
        notes.append(f"plant zone failed ({exc})")

    return "; ".join(notes)


def _cell_tunnels(cmeta: dict) -> list:
    """This cell's protocol tunnels as a list of dicts.

    Reads ``ot_tunnels``; a cell deployed before multi-protocol has only the
    singular ``ot_tunnel_*`` keys, which are projected into the same shape here
    so every caller sees one representation. Returns a fresh list — callers
    append to it and persist the whole thing."""
    raw = (cmeta or {}).get("ot_tunnels")
    if isinstance(raw, list) and raw:
        return [dict(t) for t in raw if isinstance(t, dict)]
    state = (cmeta or {}).get("ot_tunnel_tf_state")
    if state:
        return [{"protocol": (cmeta.get("ot_tunnel_protocol") or "").lower(),
                 "jump_id": str(cmeta.get("ot_tunnel_jump_id") or ""),
                 "tf_state": state,
                 "local_port": int(cmeta.get("ot_tunnel_local_port") or 0),
                 "remote_port": int(cmeta.get("ot_tunnel_remote_port") or 0)}]
    return []


def cell_tunnels(cmeta: dict) -> list:
    """Public view of :func:`_cell_tunnels` for the API and destroy paths."""
    return _cell_tunnels(cmeta)


def cell_wiring_complete(cmeta: dict) -> bool:
    """True when this cell has every artifact its own deploy asked for.

    The single definition on purpose: the cells endpoint and the home-page tile
    both render this, and when each carried its own copy they answered
    differently the moment a cell had more than one protocol tunnel (both went
    green on the first one). Same drift class as a cache warmer holding its own
    copy of route logic."""
    meta = cmeta or {}
    if not meta.get("ot_web_jump_tf_state"):
        return False
    wanted = set(resolve_cell_protocols(meta.get("ot_params") or {}))
    have = {t.get("protocol") for t in _cell_tunnels(meta) if t.get("tf_state")}
    if wanted - have:
        return False
    checkout_pending = (not ps_checkout_skip_reason(meta)
                        and not meta.get("ot_ps_synced"))
    return not checkout_pending


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
