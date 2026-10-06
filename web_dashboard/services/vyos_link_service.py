"""The VyOS site link: an on-prem Active Directory on a GCP VPC over WireGuard, cheaply.

A ``managed_directories`` row with provider ``gcp_vyos_link`` that extends a registered
on-prem AD (``linked_directory_id``), as a DNS link and an AD Connector do. What it
builds is the network path those two assume:

* **The cloud end, built here.** ``terraform/directory/gcp_vyos_peer``: one small VyOS VM
  (the ``vyos-cell`` bake) with a static external IP, ``can_ip_forward``, a VPC route per
  on-prem subnet pointing at it, and the firewall rules for WireGuard, configuration,
  DNS and the on-prem agent's WinRM. The GCP Ansible runner then configures it over SSH
  to its internal address with :data:`PLAYBOOK` -- WireGuard, the routes, and DNS
  forwarding for the AD domain, so a DNS link can target an address inside the VPC.
* **The on-prem end, done by the operator.** They generate a key pair on their own
  router and paste only its PUBLIC key; :func:`onprem_commands` gives them the lines to
  paste back. The on-prem private key never leaves the router.

**Where the secret lives.** The cloud peer's WireGuard private key is generated here,
written to GCP Secret Manager (``wg_secret_ref`` in ``link_config``), and dropped from
memory. Each configuration run receives it through the Cloud Run runner's secret-env
channel (``cloud_ansible_secrets``): never in instance metadata, Terraform state, the
job record or this row. Destroy deletes it.

**Status means the tunnel works.** The row is ``available`` only when the peer pinged a
domain controller through the tunnel (``VMDASH-VYOS-LINK:UP``). Built and configured but
not answered yet is ``awaiting_onprem``, and **Check link** runs the play again. A run
that ends without a sentinel is a failure, never success.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import logging
import os
import re
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

PROVIDER = "gcp_vyos_link"
STATUS_AWAITING = "awaiting_onprem"
PLAYBOOK = os.path.join(os.path.dirname(__file__), "builtin_playbooks",
                        "vyos-wireguard-peer.yml")
SENTINEL = "VMDASH-VYOS-LINK:"
SECRET_PREFIX = "vyos-link-wg"
WG_PORT = 51820
WG_ADDRESS = "10.255.255.1/30"          # the cloud peer's tunnel address
ONPREM_TUNNEL_IP = "10.255.255.2"       # the on-prem router's
DEFAULT_MACHINE_TYPE = "e2-small"
DEFAULT_SSH_USER = "adminuser"          # vyos-cell.sh's VYOS_ADMIN_USER default
# A raw private key in `set interfaces wireguard` and `name-server` in DNS forwarding.
REQUIRED_RELEASE = "1.4"

_SENTINEL_RE = re.compile(re.escape(SENTINEL) + r"(CONFIGURED|UP|WAITING)")


class VyosLinkError(Exception):
    """A request that cannot be built; the message is the remedy."""


def _cfg(key: str, default: str = "") -> str:
    from . import config_service
    from ..config import settings
    return str(config_service.get(key) or getattr(settings, key, "") or default).strip()


def link_config(row) -> dict:
    try:
        out = json.loads(getattr(row, "link_config", None) or "{}")
        return out if isinstance(out, dict) else {}
    except (TypeError, ValueError):
        return {}


def _set_config(row, cfg: dict) -> None:
    row.link_config = json.dumps(cfg, sort_keys=True)


# ── keys ──────────────────────────────────────────────────────────────────────

def generate_keypair() -> tuple[str, str]:
    """``(private_b64, public_b64)``: a WireGuard (X25519) key pair, as ``wg genkey``
    and ``wg pubkey`` print them."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    key = X25519PrivateKey.generate()
    raw = serialization.Encoding.Raw
    priv = key.private_bytes(raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())
    pub = key.public_key().public_bytes(raw, serialization.PublicFormat.Raw)
    return base64.b64encode(priv).decode(), base64.b64encode(pub).decode()


def public_key_ok(value: str) -> bool:
    """A WireGuard public key: base64 of exactly 32 bytes (44 characters)."""
    v = (value or "").strip()
    if len(v) != 44:
        return False
    try:
        return len(base64.b64decode(v, validate=True)) == 32
    except (ValueError, TypeError):
        return False


# ── validation ────────────────────────────────────────────────────────────────

def _cidrs(values, what: str) -> list:
    out, bad = [], []
    for v in values or []:
        v = (v or "").strip()
        if not v:
            continue
        try:
            out.append(str(ipaddress.ip_network(v, strict=False)))
        except ValueError:
            bad.append(v)
    if bad:
        raise VyosLinkError(f"{what}: not CIDR ranges: {', '.join(bad)}")
    return out


def _addresses(values, what: str) -> list:
    out, bad = [], []
    for v in values or []:
        v = (v or "").strip()
        if not v:
            continue
        try:
            out.append(str(ipaddress.ip_address(v)))
        except ValueError:
            bad.append(v)
    if bad:
        raise VyosLinkError(f"{what}: not IP addresses: {', '.join(bad)}")
    return out


def provision_problem(*, image_name: str, image_self_link: str, release: str,
                      onprem_public_key: str) -> str:
    """Everything that can be refused before anything is built. ``""`` = fine."""
    from . import netcell_service, windows_admin_secret
    problem = netcell_service.image_problem(image_name, image_self_link)
    if problem:
        return problem
    if (release or "").strip() != REQUIRED_RELEASE:
        return (f"The site link needs a VyOS {REQUIRED_RELEASE} image (got "
                f"{release or 'none'!r}): 1.3 cannot take a WireGuard private key as a "
                "value, and names DNS forwarders differently.")
    if not public_key_ok(onprem_public_key):
        return ("Paste the on-prem router's WireGuard PUBLIC key: 44 characters of base64, "
                "as `generate pki wireguard key-pair` prints it. Never its private key.")
    if not windows_admin_secret._gcp_sm_configured():
        return ("The cloud peer's WireGuard private key is kept in GCP Secret Manager, "
                "which the Cloud Run runner reads it from. Set gcp_project_id (or "
                "secrets_gcp_project) first.")
    if not _cfg("gcp_ssh_key_secret_name"):
        return ("The GCP Ansible runner configures the peer with the key in "
                "gcp_ssh_key_secret_name, which is not set. Bake its public half into the "
                "image as VYOS_RUNNER_PUBKEY (provisioners/net/vyos-cell.sh).")
    return ""


def provision(db: Session, *, onprem_directory_id: str, project: str, zone: str,
              network: str, subnetwork: str, image_name: str, image_self_link: str,
              release: str, onprem_public_key: str, onprem_subnets: list,
              cloud_networks: list, dns_ips: list, machine_type: str = "",
              wireguard_source_ranges: Optional[list] = None, ssh_user: str = "",
              created_by: str, workgroup: Optional[str] = None) -> dict:
    """Validate, store the peer's key, record and enqueue a VyOS site link."""
    from ..database import ManagedDirectory
    from . import directory_service, job_service, secrets_backend_service
    onprem = directory_service.get_directory(db, onprem_directory_id)
    if not onprem or onprem.cloud != "local" or onprem.provider != "onprem_ad":
        raise VyosLinkError("a site link extends a registered on-premises Active "
                            "Directory — register the domain on this page first")
    project = (project or "").strip() or _cfg("gcp_project") or _cfg("gcp_project_id")
    zone = (zone or "").strip() or _cfg("gcp_zone")
    network = (network or "").strip() or _cfg("gcp_network")
    subnetwork = (subnetwork or "").strip() or _cfg("gcp_subnetwork")
    missing = [n for n, v in (("project", project), ("zone", zone), ("network", network),
                              ("subnetwork", subnetwork)) if not v]
    if missing:
        raise VyosLinkError(f"name the GCP {', '.join(missing)} for the peer")
    problem = provision_problem(image_name=image_name, image_self_link=image_self_link,
                                release=release, onprem_public_key=onprem_public_key)
    if problem:
        raise VyosLinkError(problem)
    onprem_subnets = _cidrs(onprem_subnets, "on-prem subnets")
    cloud_networks = _cidrs(cloud_networks, "cloud networks")
    dns_ips = _addresses(dns_ips, "domain controllers")
    sources = _cidrs(wireguard_source_ranges or ["0.0.0.0/0"], "WireGuard source ranges")
    if not onprem_subnets:
        raise VyosLinkError("list the on-prem subnets the cloud must reach — at least "
                            "the domain controllers' subnet")
    if not cloud_networks:
        raise VyosLinkError("list the VPC ranges the on-prem side must reach")
    if not dns_ips:
        raise VyosLinkError("list the domain controllers' addresses: the peer forwards "
                            "the domain's DNS to them")
    tunnel = ipaddress.ip_network(WG_ADDRESS, strict=False)
    for net in onprem_subnets + cloud_networks:
        if ipaddress.ip_network(net).overlaps(tunnel):
            raise VyosLinkError(f"{net} overlaps the tunnel's own {tunnel}")
    outside = [ip for ip in dns_ips
               if not any(ipaddress.ip_address(ip) in ipaddress.ip_network(n)
                          for n in onprem_subnets)]
    if outside:
        raise VyosLinkError(f"{', '.join(outside)} is not inside any on-prem subnet "
                            f"listed, so the cloud would have no route to it")
    clash = db.query(ManagedDirectory).filter(
        ManagedDirectory.linked_directory_id == onprem.id,
        ManagedDirectory.provider == PROVIDER, ManagedDirectory.project == project,
        ManagedDirectory.status != "deleted").first()
    if clash:
        raise VyosLinkError(f"{onprem.name} already has a site link in {project}")

    row = ManagedDirectory(
        name=onprem.name, cloud="gcp", provider=PROVIDER, source="provisioned",
        status="provisioning", project=project, networks=json.dumps([network]),
        dns_ips=json.dumps(dns_ips), linked_directory_id=onprem.id,
        workgroup=workgroup, created_by=created_by, expires_at=None)
    db.add(row)
    db.flush()

    private, public = generate_keypair()
    try:
        secret_ref = secrets_backend_service.write_sync(
            "gcp_sm", f"{SECRET_PREFIX}-{row.id[:8]}", private)
    except Exception as e:  # noqa: BLE001 — every SDK raises its own type
        db.rollback()
        raise VyosLinkError(f"could not store the peer's WireGuard key in GCP Secret "
                            f"Manager: {e}") from e
    finally:
        private = ""
    _set_config(row, {
        "zone": zone, "network": network, "subnetwork": subnetwork,
        "image_name": image_name, "image_self_link": image_self_link,
        "machine_type": (machine_type or "").strip() or DEFAULT_MACHINE_TYPE,
        "onprem_subnets": onprem_subnets, "cloud_networks": cloud_networks,
        "wireguard_source_ranges": sources, "onprem_public_key": onprem_public_key.strip(),
        "peer_public_key": public, "wg_secret_ref": secret_ref,
        "wg_port": WG_PORT, "wg_address": WG_ADDRESS, "onprem_tunnel_ip": ONPREM_TUNNEL_IP,
        "ssh_user": (ssh_user or "").strip() or DEFAULT_SSH_USER,
    })
    job = job_service.create_job(
        db, directory_service.PROVISION_JOB_TYPE, created_by, workgroup=workgroup,
        metadata={"directory_id": row.id, "name": row.name, "cloud": "gcp",
                  "kind": PROVIDER, "linked_directory_id": onprem.id})
    row.deploy_job_id = job.id
    db.commit()
    return {"directory_id": row.id, "job_id": job.id}


def start_check(db: Session, *, directory_id: str, created_by: str) -> dict:
    """Run the configuration play again: re-applies the config and probes the tunnel."""
    from . import directory_service, job_service
    row = directory_service.get_directory(db, directory_id)
    if not row or row.provider != PROVIDER:
        raise VyosLinkError("not a site link")
    if row.status not in (STATUS_AWAITING, "available", "failed") or not link_config(
            row).get("peer_internal_ip"):
        raise VyosLinkError(f"{row.name}'s peer is {row.status} — wait for it to be built")
    job = job_service.create_job(
        db, directory_service.PROVISION_JOB_TYPE, created_by, workgroup=row.workgroup,
        metadata={"directory_id": row.id, "name": row.name, "cloud": "gcp",
                  "kind": PROVIDER, "check_only": True})
    db.commit()
    return {"directory_id": row.id, "job_id": job.id}


# ── Terraform ─────────────────────────────────────────────────────────────────

def tf_variables(row) -> dict:
    cfg = link_config(row)
    return {
        "project": row.project, "zone": cfg.get("zone"), "network": cfg.get("network"),
        "subnetwork": cfg.get("subnetwork"),
        "image": cfg.get("image_self_link") or cfg.get("image_name"),
        "machine_type": cfg.get("machine_type") or DEFAULT_MACHINE_TYPE,
        "directory_row_id": row.id,
        "onprem_subnets": cfg.get("onprem_subnets") or [],
        "cloud_networks": cfg.get("cloud_networks") or [],
        "wireguard_port": int(cfg.get("wg_port") or WG_PORT),
        "wireguard_source_ranges": cfg.get("wireguard_source_ranges") or ["0.0.0.0/0"],
    }


def read_outputs(row, outputs: dict) -> None:
    def val(key):
        v = outputs.get(key)
        return v.get("value") if isinstance(v, dict) and "value" in v else v
    cfg = link_config(row)
    cfg["peer_public_ip"] = val("public_ip")
    cfg["peer_internal_ip"] = val("internal_ip")
    row.resource_name = val("instance_name")
    _set_config(row, cfg)


# ── configuration ─────────────────────────────────────────────────────────────

def play_vars(row) -> dict:
    """The play's non-secret vars. ``wg_private_key`` is not among them."""
    cfg = link_config(row)
    dns = json.loads(row.dns_ips or "[]")
    return {
        "wg_port": int(cfg.get("wg_port") or WG_PORT),
        "wg_address": cfg.get("wg_address") or WG_ADDRESS,
        "onprem_tunnel_ip": cfg.get("onprem_tunnel_ip") or ONPREM_TUNNEL_IP,
        "onprem_public_key": cfg.get("onprem_public_key") or "",
        "onprem_subnets": cfg.get("onprem_subnets") or [],
        "cloud_networks": cfg.get("cloud_networks") or [],
        "ad_domain": row.name, "dns_servers": dns,
        "listen_address": cfg.get("peer_internal_ip") or "",
        "probe_address": dns[0] if dns else "",
    }


def render_playbook(row) -> str:
    """The builtin play with this link's vars written in. The Cloud Run runner takes no
    extra vars, so they travel inside the play -- which is why none may be secret."""
    import yaml
    with open(PLAYBOOK, encoding="utf-8") as f:
        plays = yaml.safe_load(f)
    plays[0].setdefault("vars", {}).update(play_vars(row))
    return yaml.safe_dump(plays, sort_keys=False)


def parse_state(output: str) -> str:
    """``"up"`` / ``"waiting"`` / ``"configured"`` from the LAST sentinel, or ``""``."""
    found = _SENTINEL_RE.findall(output or "")
    return found[-1].lower() if found else ""


async def configure(row, job_id: str) -> tuple[str, str]:
    """Run the play on the GCP Ansible runner. ``(state, output)``; never raises for an
    in-play failure, which comes back as state ``""``."""
    from . import ansible_local_service, cloud_ansible_secrets, gcp_service
    cfg = link_config(row)
    pem = await ansible_local_service.fetch_ssh_key("gcp")
    if not pem:
        raise VyosLinkError("no GCP Ansible key (gcp_ssh_key_secret_name) to log on with")
    env_names, manifest_b64 = cloud_ansible_secrets.build_manifest(["wg_private_key"])
    region = (_cfg("gcp_ansible_cloud_run_region") or _cfg("gcp_region")
              or "-".join(str(cfg.get("zone") or "").split("-")[:2]))
    code, output = await gcp_service.run_cloud_run_ansible_task(
        project_id=_cfg("gcp_project_id") or row.project,
        region=region,
        image=_cfg("gcp_ansible_image") or "chrweav/ansible-winrm:latest",
        target_ip=cfg.get("peer_internal_ip") or "",
        ansible_user=cfg.get("ssh_user") or DEFAULT_SSH_USER,
        playbook_b64=base64.b64encode(render_playbook(row).encode()).decode(),
        ssh_key_b64=base64.b64encode(pem.encode()).decode(),
        job_id=job_id,
        vpc_connector=_cfg("gcp_ansible_vpc_connector"),
        vpc_network=_cfg("gcp_run_network"),
        vpc_subnetwork=_cfg("gcp_run_subnetwork"),
        service_account=_cfg("gcp_ansible_runner_service_account"),
        secret_entries=[{"env": env_names[0], "secret_name": cfg.get("wg_secret_ref")}],
        manifest_b64=manifest_b64,
    )
    pem = ""
    state = parse_state(output) if code == 0 else ""
    return state, output


def onprem_commands(row) -> str:
    """The lines the operator pastes into their on-prem VyOS (1.4) configure mode. No
    secret: their own private key is already on the router."""
    cfg = link_config(row)
    peer_ip = cfg.get("peer_public_ip") or "<peer public IP>"
    lines = [
        "# In configure mode on the on-prem VyOS router. Generate its key first with",
        "#   generate pki wireguard key-pair install interface wg0",
        "# and paste the PUBLIC key it prints into the dashboard.",
        f"set interfaces wireguard wg0 address '{cfg.get('onprem_tunnel_ip') or ONPREM_TUNNEL_IP}/30'",
        "set interfaces wireguard wg0 description 'Site link to GCP (vm-dashboard)'",
        f"set interfaces wireguard wg0 peer gcp public-key '{cfg.get('peer_public_key') or ''}'",
        f"set interfaces wireguard wg0 peer gcp address '{peer_ip}'",
        f"set interfaces wireguard wg0 peer gcp port '{cfg.get('wg_port') or WG_PORT}'",
        "set interfaces wireguard wg0 peer gcp persistent-keepalive '25'",
        f"set interfaces wireguard wg0 peer gcp allowed-ips '{(cfg.get('wg_address') or WG_ADDRESS).split('/')[0]}/32'",
    ]
    for net in cfg.get("cloud_networks") or []:
        lines.append(f"set interfaces wireguard wg0 peer gcp allowed-ips '{net}'")
        lines.append(f"set protocols static route {net} interface wg0")
    lines += ["commit", "save"]
    return "\n".join(lines)


async def run(db: Session, row, job_id: str, *, check_only: bool) -> None:
    """``directory_provision`` for a site link: build (unless ``check_only``), then
    configure and probe."""
    from ..api.websocket import broadcast_progress
    from . import directory_service, job_service, terraform, terraform_provider_env
    job_service.set_running(db, job_id)
    try:
        if not check_only:
            await broadcast_progress(job_id, 5, "Building the VyOS peer…")
            outputs = await terraform.apply(
                directory_service._deploy_dir(job_id), tf_variables(row),
                template_dir=directory_service.template_dir("gcp", PROVIDER),
                env=terraform_provider_env.provider_env("gcp"),
                on_line=directory_service._job_stream(job_id, 5, "Building the VyOS peer…",
                                                      "gcp"))
            read_outputs(row, outputs)
            row.updated_at = datetime.utcnow()
            db.commit()
        await broadcast_progress(job_id, 70, "Configuring WireGuard on the peer…")
        state, output = await configure(row, job_id)
    except Exception as exc:  # noqa: BLE001
        row.status = "failed"
        row.error_message = str(exc)[:2000]
        row.updated_at = datetime.utcnow()
        db.commit()
        logger.error("vyos link %s failed: %s", row.id, exc)
        job_service.set_failed(db, job_id, str(exc)[:2000])
        return

    cfg = link_config(row)
    cfg["last_check"] = datetime.utcnow().isoformat()
    cfg["tunnel"] = state or "unknown"
    _set_config(row, cfg)
    result = {"directory_id": row.id, "name": row.name, "tunnel": state or "unknown",
              "peer_public_ip": cfg.get("peer_public_ip"),
              "peer_internal_ip": cfg.get("peer_internal_ip")}
    if not state:
        # Completed without a sentinel: the play did not get as far as committing, or
        # its output was cut short. Never read that as configured.
        row.status = "failed" if not check_only else row.status
        row.error_message = ("the configuration play ended without reporting a result — "
                             "read the job's output")
        row.updated_at = datetime.utcnow()
        db.commit()
        job_service.set_failed(db, job_id, f"{row.error_message}\n\n{output[-4000:]}")
        return
    if state == "up":
        row.status = "available"
        row.error_message = None
    else:
        row.status = STATUS_AWAITING
        row.error_message = (f"the peer is configured, but {play_vars(row)['probe_address']} "
                             "did not answer through the tunnel — paste the on-prem commands "
                             "into your router, then Check link")
    row.updated_at = datetime.utcnow()
    db.commit()
    job_service.set_completed(db, job_id, result=result)


def destroy_problem(db: Session, row) -> str:
    """A DNS link pointed at this peer resolves through it; refuse to pull it out."""
    from ..database import ManagedDirectory
    peer = link_config(row).get("peer_internal_ip")
    if not peer:
        return ""
    for other in db.query(ManagedDirectory).filter(
            ManagedDirectory.provider == "dns_link",
            ManagedDirectory.linked_directory_id == row.linked_directory_id,
            ManagedDirectory.status != "deleted").all():
        if peer in json.loads(other.dns_ips or "[]"):
            return (f"the DNS link in {other.project} forwards {row.name} to this peer "
                    f"({peer}) — destroy that first")
    return ""


def delete_secret(row) -> str:
    """Remove the peer's key from Secret Manager. ``""`` on success, else the error."""
    ref = link_config(row).get("wg_secret_ref")
    if not ref:
        return ""
    from . import secrets_backend_service
    try:
        secrets_backend_service.delete_sync("gcp_sm", ref)
        return ""
    except Exception as e:  # noqa: BLE001
        return str(e)


def to_dict_extra(row) -> dict:
    """What the page shows: never the key reference, only public material."""
    cfg = link_config(row)
    return {k: cfg.get(k) for k in (
        "zone", "network", "machine_type", "onprem_subnets", "cloud_networks",
        "peer_public_key", "peer_public_ip", "peer_internal_ip", "tunnel", "last_check",
        "wg_port")}
