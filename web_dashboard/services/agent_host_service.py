"""Remote agents the dashboard deploys into a cloud, and the lifecycle of their hosts.

Phase 3 of running Config Management on agents (docs/platform/where-work-runs.md, "Scaling
configuration in the cloud"). Phases 1 and 2 let cloud-VM runs go to an agent inside a VPC
or VNet, several at once; this builds that agent for the operator, the way the Gateways,
Rancher and Portainer are built, and decides when its host should exist at all:

  * ``always`` — up until someone stops or deletes it;
  * ``until``  — up until a time, then stopped;
  * ``window`` — "persistent just in time": started ``lead_minutes`` before each occurrence
                 of a change window and stopped ``grace_minutes`` after it closes.

**What a deployed agent is.** An ordinary ``remote_agents`` row plus an ``agent_hosts`` row.
It enrols or attests, leases and is routed to exactly like an agent an operator built by
hand. Three things differ, and all three are deliberate:

  * **Config Management only.** The dashboard writes this agent's ``policy.yaml`` at launch:
    ``job_types: [agent_ansible]`` and ``ansible.targets`` from the ranges on the deploy
    form, nothing else. It is read-only on the host. Changing it is a redeploy, which a
    window-mode agent gets for free at its next start.
  * **Its identity is single-use and re-minted every start.** A SPIRE join token when the
    dashboard attests agents, otherwise a one-time enrolment code — either way spent on
    first boot. Stopping clears the agent's key, so a destroyed host's identity is dead.
  * **Stopping destroys the host and keeps the agent.** Routes and runs booked into the next
    window stay valid; nothing runs and nothing is at rest in between.

**No ingress, ever.** The host sits in the cloud's VM subnet with no public address and no
inbound rule; it dials out to the dashboard through the same on-demand NAT the VMs use.

Dispatched by ``jobs_worker`` as ``agent_host_start`` / ``agent_host_stop`` /
``agent_host_delete``; scheduled by ``schedule_sweeper``'s agent-host pass (:func:`sweep`).
"""
import asyncio
import base64
import json
import logging
import re
import shlex
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

CLOUDS = ("aws", "azure", "gcp")
MODES = ("always", "until", "window")

# Statuses in which the host is (or is about to be) in the cloud. The NAT reclaim checks
# count these, so egress is never cut from under a live agent.
LIVE_STATUSES = ("starting", "running", "stopping")

PURPOSE = "dashboard-agent"
AGENT_IMAGE = "chrweav/dashboard-agent:latest"
# Beside a SPIRE-attested agent, so the dashboard can upgrade and restart it
# (docs/platform/spiffe/remote-agents/lifecycle.md). It verifies every agent image's
# signature against the identity in the policy.yaml below before it pulls.
UPDATER_IMAGE = "chrweav/dashboard-agent-updater:latest"
ANSIBLE_IMAGE = "chrweav/ansible-winrm:latest"
# The SPIRE pair, pinned by digest exactly as examples/remote-agent/docker-compose.spire.yml
# pins them. A tag here would let a registry change what runs beside the agent.
SPIRE_AGENT_IMAGE = ("ghcr.io/spiffe/spire-agent:1.15.5@sha256:"
                     "3652c732b0f0d36f4dd78394230a1b1cd0ddcc29548a46c67a996b1a78c14481")
SPIFFE_HELPER_IMAGE = ("ghcr.io/spiffe/spiffe-helper:0.12.1@sha256:"
                       "49a7b1aca5b497bb4c1ca1ed1f15397bdf9c9cafb8036df4b963b566bd234f7f")
# The agent image is pulled at boot, so the agent always runs a build at least this new.
# Recorded on the agent row at deploy so the enqueue version gates (Windows over SSH,
# several runs at once) accept a parked agent that has not yet reported itself.
DEPLOYED_AGENT_VERSION = "2.9.0"

ANSIBLE_PORTS = (22, 5985, 5986)
MAX_CONCURRENT_CEILING = 8          # the agent's own ANSIBLE_MAX_CONCURRENT_CEILING
MAX_CIDRS = 16
# About 1 GiB per run, plus the agent: 4 GiB covers the default of 2 runs with headroom.
DEFAULT_SIZES = {"aws": "t3.medium", "azure": "Standard_B2s", "gcp": "e2-medium"}

ONLINE_TIMEOUT_S = 900               # boot, pull two images, enrol or attest
_ONLINE_POLL_S = 10
_STOP_DRAIN_POLL_S = 15
# A window start this close to its stop is not worth a host that would barely boot.
_MIN_USEFUL_UP = timedelta(minutes=10)

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}[a-z0-9]$")
_SIZE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_HOST_DIR = "/var/lib/dashboard-agent-host"


class AgentHostError(Exception):
    """An operator-facing refusal. Every message names the cause and the fix."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _cfg(key: str) -> str:
    from . import config_service
    return (config_service.get(key) or "").strip()


# ── validation ────────────────────────────────────────────────────────────────

def validate_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not _NAME_RE.match(name):
        raise AgentHostError(
            "Name the agent with 3 to 42 lowercase letters, digits and hyphens, starting "
            "with a letter — it becomes the cloud VM's name, e.g. vpc-a-agent.")
    return name


def normalize_cidrs(raw) -> list:
    """The ranges this agent may configure, normalised, or raise.

    Through the Config-Management route rules (config_mgmt_route_service.normalize_cidr):
    the same refusals for loopback, link-local and host bits, so a range accepted here is
    one a route can name too."""
    from . import config_mgmt_route_service as cmr
    items = raw if isinstance(raw, (list, tuple)) else str(raw or "").replace(",", " ").split()
    out = []
    for item in items:
        try:
            cidr = cmr.normalize_cidr(str(item))
        except cmr.ConfigRouteError as exc:
            raise AgentHostError(str(exc)) from None
        if cidr not in out:
            out.append(cidr)
    if not out:
        raise AgentHostError(
            "Name at least one range this agent may configure — usually the VPC or VNet "
            "it runs in, like 10.40.0.0/16.")
    if len(out) > MAX_CIDRS:
        raise AgentHostError(f"At most {MAX_CIDRS} ranges per agent.")
    return out


def validate_size(cloud: str, size: str) -> str:
    size = (size or "").strip()
    if not size:
        return DEFAULT_SIZES[cloud]
    if not _SIZE_RE.match(size):
        raise AgentHostError(f"{size!r} is not an instance size this cloud would accept.")
    return size


def validate_lifecycle(db, *, mode: str, until_at=None, change_window_id: str = "",
                       lead_minutes=20, grace_minutes=15) -> dict:
    mode = (mode or "always").strip().lower()
    if mode not in MODES:
        raise AgentHostError(f"Lifecycle must be one of {', '.join(MODES)}.")
    try:
        lead = int(lead_minutes)
        grace = int(grace_minutes)
    except (TypeError, ValueError):
        raise AgentHostError("Lead and grace are whole minutes.") from None
    if not 5 <= lead <= 240:
        raise AgentHostError("Start the agent between 5 and 240 minutes before a window.")
    if not 0 <= grace <= 240:
        raise AgentHostError("Keep the agent up between 0 and 240 minutes after a window.")
    out = {"mode": mode, "until_at": None, "change_window_id": None,
           "lead_minutes": lead, "grace_minutes": grace}
    if mode == "until":
        if not isinstance(until_at, datetime):
            raise AgentHostError("An 'until' agent needs the time it should go away.")
        if until_at <= _utcnow():
            raise AgentHostError("That time has already passed.")
        out["until_at"] = until_at
    if mode == "window":
        from . import change_window_service
        window = change_window_service.get(db, change_window_id or "")
        if window is None or window.enabled is False:
            raise AgentHostError("Choose an enabled change window for this agent to follow.")
        out["change_window_id"] = window.id
    return out


# ── what the host is told ─────────────────────────────────────────────────────

def policy_yaml(cidrs: list, max_concurrent: int, *, upgrades: bool = False) -> str:
    """The agent's policy.yaml: Config Management only, for these ranges.

    ``upgrades`` (SPIRE hosts, which get the updater) opts the host into central upgrades,
    accepting only images the project's release workflow signed (keyless).

    Built as data and dumped by yaml.safe_dump, never formatted as text, so no value can
    add a key. Every range was normalised by :func:`normalize_cidrs` already; this re-checks
    rather than trusts, because the row could have been edited around it."""
    import yaml
    ranges = normalize_cidrs(cidrs)
    slots = int(max_concurrent)
    if not 1 <= slots <= MAX_CONCURRENT_CEILING:
        raise AgentHostError(f"Runs at once must be 1 to {MAX_CONCURRENT_CEILING}.")
    doc = {
        "job_types": ["agent_ansible"],
        # The top-level list is what every probe is checked against; the agent requires a
        # target to be allowed here AND in ansible.targets.
        "targets": [{"cidr": c, "ports": list(ANSIBLE_PORTS)} for c in ranges],
        "ansible": {
            "enabled": True,
            "vm_image": ANSIBLE_IMAGE,
            "max_concurrent": slots,
            "targets": [{"cidr": c, "ports": list(ANSIBLE_PORTS)} for c in ranges],
        },
    }
    if upgrades:
        from .agent_lifecycle_service import (DEFAULT_REPOSITORY, SIGNING_IDENTITY,
                                              SIGNING_ISSUER)
        doc["upgrades"] = {
            "enabled": True,
            "repository": DEFAULT_REPOSITORY,
            "verify": {"keyless": {"identity": SIGNING_IDENTITY, "issuer": SIGNING_ISSUER}},
        }
    return yaml.safe_dump(doc, sort_keys=False)


def agent_image(db) -> str:
    """The agent image a new host starts: the newest recorded release, by digest, when
    there is one (what the fleet is being moved to), else the latest tag."""
    from . import agent_release_service
    from .agent_lifecycle_service import DEFAULT_REPOSITORY
    # Only a release of the project's repository: that is what the generated policy pins.
    row = agent_release_service.newest(db, DEFAULT_REPOSITORY)
    return f"{DEFAULT_REPOSITORY}@{row.digest}" if row else AGENT_IMAGE


def dashboard_url_problem() -> str:
    """Why a deployed agent could not dial this dashboard, or "" if it can.

    A return value, not an exception, so the deploy form can show it: CodeQL
    ``py/stack-trace-exposure`` fires on any caught exception stringified into a response
    body (see skytap_service.verify)."""
    from . import agent_service
    url = _cfg(agent_service.AUDIENCE_CONFIG).rstrip("/")
    if not url:
        return ("This dashboard has no pinned agent address yet. Set Public base URL under "
                "Settings → Integrations → Remote Agents and issue one enrolment code (or "
                "press Reset there), then deploy.")
    if not url.startswith("https://"):
        return (f"The agent address is {url}, and a deployed agent will not send signed "
                f"requests over plain HTTP. Put TLS in front of the dashboard first.")
    return ""


def dashboard_url() -> str:
    """Where the host's agent dials: the pinned agent audience, which must be HTTPS.

    Refused rather than guessed: an agent in a cloud subnet reaches the dashboard over the
    internet, and a deploy that bakes in the wrong URL boots an agent that can never enrol."""
    from . import agent_service
    problem = dashboard_url_problem()
    if problem:
        raise AgentHostError(problem)
    return _cfg(agent_service.AUDIENCE_CONFIG).rstrip("/")


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def bootstrap_script(*, url: str, policy: str, material: dict,
                     image: str = AGENT_IMAGE) -> str:
    """The Bash every cloud runs at first boot to start the agent.

    One script for all three clouds — ECS-optimised Amazon Linux, Ubuntu with docker.io, and
    Container-Optimized OS all have Docker and Bash — mirroring
    ``examples/remote-agent/docker-compose.yml`` with the sibling and (when attesting) SPIRE
    overlays, as plain ``docker run`` because none of the three images ships compose.

    Every value that came from a person or a token reaches the script base64-encoded and is
    decoded INTO A FILE, never interpolated into a command line, so no value can become
    shell. The few interpolated values (the URL, the SPIRE address and trust domain) are
    validated and quoted. Re-running it is a no-op once the agent container exists.

    A SPIRE host also gets the updater (examples/remote-agent/docker-compose.updater.yml):
    the control directory it shares with the agent, and the label marking the one
    container it may replace. A code-mode host does not: its identity is a file in the
    container, so a replacement would be a different agent.
    """
    mode = material.get("mode")
    if mode not in ("spire", "code"):
        raise AgentHostError("No identity to give the agent.")
    q = shlex.quote
    d = _HOST_DIR
    if not re.match(r"^[a-z0-9./_-]+(:[A-Za-z0-9._-]+|@sha256:[0-9a-f]{64})$", image):
        raise AgentHostError("The agent image is not a plain reference.")
    lines = [
        "#!/bin/bash",
        "set -euo pipefail",
        f"D={d}",
        'if docker inspect dashboard-agent >/dev/null 2>&1; then exit 0; fi',
        "for _ in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 5; done",
        'mkdir -p "$D/etc" "$D/code" "$D/state"',
        f'echo {_b64(policy)} | base64 -d > "$D/etc/policy.yaml"',
        'chmod 0444 "$D/etc/policy.yaml"',
        'chown 10001:10001 "$D/state" && chmod 0700 "$D/state"',
        'GID=$(stat -c %g /var/run/docker.sock)',
        f"docker pull {image}",
        f"docker pull {ANSIBLE_IMAGE}",
    ]
    agent_env = [
        f"-e DASHBOARD_URL={q(url)}",
        "-e AGENT_POLL_INTERVAL=5",
        "-e AGENT_DOCKER_SOCKET=/var/run/docker.sock",
        "-e AGENT_DRAIN_SECONDS=25",
    ]
    agent_vols = [
        '-v "$D/state:/var/lib/dashboard-agent"',
        '-v "$D/etc/policy.yaml:/etc/dashboard-agent/policy.yaml:ro"',
        "-v /var/run/docker.sock:/var/run/docker.sock",
    ]
    if mode == "code":
        lines += [
            f'echo {_b64(material["code"])} | base64 -d > "$D/code/enroll.code"',
            'chown 10001:10001 "$D/code/enroll.code" && chmod 0400 "$D/code/enroll.code"',
        ]
        # The DIRECTORY is mounted, not the file: once the file is deleted below, Docker
        # would recreate a missing single-file bind source as a directory on restart.
        agent_env.append("-e AGENT_ENROLLMENT_CODE_FILE=/etc/dashboard-agent/code/enroll.code")
        agent_vols.append('-v "$D/code:/etc/dashboard-agent/code:ro"')
    else:
        td = str(material["trust_domain"])
        server = str(material["server_address"])
        port = int(material["server_port"])
        if not re.match(r"^[A-Za-z0-9.-]+$", td) or not re.match(r"^[A-Za-z0-9.:-]+$", server):
            raise AgentHostError("The SPIRE trust domain or server address is not plain.")
        # join_token (a one-use code in this script), or the cloud's own instance identity
        # (aws_iid / azure_imds / gcp_iit): no secret here at all, the VM is the proof.
        attestor = str(material.get("node_attestor") or "join_token")
        if attestor == "join_token":
            node_attestor = '    NodeAttestor "join_token" { plugin_data {} }'
        elif attestor in ("aws_iid", "gcp_iit"):
            node_attestor = f'    NodeAttestor "{attestor}" {{ plugin_data {{}} }}'
        elif attestor == "azure_imds":
            domain = str(material.get("tenant_domain") or "")
            if not re.match(r"^[A-Za-z0-9.-]+$", domain):
                raise AgentHostError("Azure node attestation needs the tenant domain "
                                     "(Settings → SPIFFE and SPIRE).")
            node_attestor = (f'    NodeAttestor "azure_imds" {{ plugin_data '
                             f'{{ tenant_domain = "{domain}" }} }}')
        else:
            raise AgentHostError(f"Unknown node attestor {attestor!r}.")
        token = attestor == "join_token"
        agent_conf = "\n".join([
            "agent {",
            '    data_dir = "/opt/spire/data"',
            '    log_level = "INFO"',
            f'    server_address = "{server}"',
            f'    server_port = "{port}"',
            f'    trust_domain = "{td}"',
            '    trust_bundle_path = "/opt/spire/conf/bootstrap.crt"',
            *(['    join_token = "${SPIRE_JOIN_TOKEN}"'] if token else []),
            '    socket_path = "/run/spire/sockets/agent.sock"',
            "}",
            "plugins {",
            node_attestor,
            # "disk", not "memory": every attestor here attests a node ONCE (a join token
            # is single-use and deleted below; aws_iid, azure_imds and gcp_iit are
            # trust-on-first-use), so the node key must survive a reboot or a SPIRE agent
            # restart, or the host can never attest again (agent-and-human-identity.md).
            '    KeyManager "disk" { plugin_data { directory = "/opt/spire/data" } }',
            '    WorkloadAttestor "unix" { plugin_data {} }',
            "}", ""])
        helper_conf = "\n".join([
            'agent_address = "/run/spire/sockets/agent.sock"',
            'cert_dir = "/run/spiffe/jwt"',
            "daemon_mode = true",
            f'jwt_svids = [{{jwt_audience="{url}/api/agent/attest", '
            'jwt_svid_file_name="attest.jwt"}]',
            "jwt_svid_file_mode = 0600", ""])
        lines += [
            'mkdir -p "$D/spire/conf" "$D/spire/data" "$D/spire/sock" "$D/helper"',
            f'echo {_b64(agent_conf)} | base64 -d > "$D/spire/conf/agent.conf"',
            f'echo {_b64(material["bootstrap_pem"])} | base64 -d > "$D/spire/conf/bootstrap.crt"',
            f'echo {_b64(helper_conf)} | base64 -d > "$D/helper/helper.conf"',
            *([f'echo {_b64(material["join_token"])} | base64 -d > "$D/spire/join.token"']
              if token else []),
            'chmod 0444 "$D/spire/conf/agent.conf" "$D/spire/conf/bootstrap.crt"',
            'chown 1000:1000 "$D/spire/data" "$D/spire/sock" && chmod 0700 "$D/spire/data"',
            *(['chown 1000:1000 "$D/spire/join.token" && chmod 0400 "$D/spire/join.token"']
              if token else []),
            'chown 10001:10001 "$D/helper" "$D/helper/helper.conf"',
            'chmod 0700 "$D/helper" && chmod 0400 "$D/helper/helper.conf"',
            "docker volume create --driver local --opt type=tmpfs --opt device=tmpfs "
            "--opt o=size=1m,uid=10001,gid=10001,mode=0700 spiffe_jwt >/dev/null",
            f"docker pull {SPIRE_AGENT_IMAGE}",
            f"docker pull {SPIFFE_HELPER_IMAGE}",
            # Join token: read into the environment from its file, so it never sits in this
            # script's command line or in `docker inspect` of the agent. Cloud identity: the
            # SPIRE agent reads the instance metadata service, so it shares the host's
            # network (IMDSv2's default hop limit of 1 does not reach a bridged container).
            # It listens on nothing either way: its API is the unix socket below.
            'docker run -d --name dashboard-agent-spire --restart unless-stopped '
            '--user 1000:1000 --read-only --cap-drop ALL '
            '--security-opt no-new-privileges:true --pids-limit 128 '
            '--tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m '
            + ('-e SPIRE_JOIN_TOKEN="$(cat "$D/spire/join.token")" ' if token
               else '--network host ') +
            '-v "$D/spire/conf/agent.conf:/opt/spire/conf/agent.conf:ro" '
            '-v "$D/spire/conf/bootstrap.crt:/opt/spire/conf/bootstrap.crt:ro" '
            '-v "$D/spire/data:/opt/spire/data" -v "$D/spire/sock:/run/spire/sockets" '
            f'{SPIRE_AGENT_IMAGE} -config /opt/spire/conf/agent.conf'
            + (' -expandEnv' if token else ''),
            # Spent on first use; nothing on disk can attest again.
            *(['rm -f "$D/spire/join.token"'] if token else []),
            'docker run -d --name dashboard-agent-spiffe-helper --restart unless-stopped '
            '--user 10001:10001 --read-only --cap-drop ALL '
            '--security-opt no-new-privileges:true --pids-limit 64 '
            '--pid container:dashboard-agent-spire '
            '-v "$D/helper:/etc/spiffe-helper:ro" -v "$D/spire/sock:/run/spire/sockets:ro" '
            '-v spiffe_jwt:/run/spiffe/jwt '
            f'{SPIFFE_HELPER_IMAGE} -config /etc/spiffe-helper/helper.conf',
        ]
        agent_env.append("-e AGENT_SPIFFE_JWT_FILE=/run/spiffe/jwt/attest.jwt")
        agent_vols.append("-v spiffe_jwt:/run/spiffe/jwt:ro")
        lines += [
            'mkdir -p "$D/control"',
            'chown 10001:10001 "$D/control" && chmod 0700 "$D/control"',
            f"docker pull {UPDATER_IMAGE}",
            'docker run -d --name dashboard-agent-updater --restart unless-stopped '
            '--user 10001:10001 --group-add "$GID" --read-only --cap-drop ALL '
            '--security-opt no-new-privileges:true --pids-limit 64 '
            '--tmpfs /tmp:noexec,nosuid,nodev,size=16m '
            '--log-driver json-file --log-opt max-size=10m --log-opt max-file=3 '
            '-e UPDATER_STOP_TIMEOUT=40 '
            '-v "$D/control:/var/lib/dashboard-agent-control" '
            '-v "$D/etc/policy.yaml:/etc/dashboard-agent/policy.yaml:ro" '
            f'-v /var/run/docker.sock:/var/run/docker.sock {UPDATER_IMAGE}',
        ]
        agent_vols.append('-v "$D/control:/var/lib/dashboard-agent-control"')
        agent_env.append("--label dashboard-agent.role=agent")
    lines.append(
        "docker run -d --name dashboard-agent --restart unless-stopped --stop-timeout 40 "
        "--user 10001:10001 --group-add \"$GID\" --read-only --cap-drop ALL "
        "--security-opt no-new-privileges:true --pids-limit 200 "
        "--tmpfs /tmp:noexec,nosuid,nodev,size=16m "
        "--log-driver json-file --log-opt max-size=10m --log-opt max-file=3 "
        + " ".join(agent_env) + " " + " ".join(agent_vols) + f" {image}")
    if mode == "code":
        # Read once by the agent at enrolment; useless after, so not left on disk.
        lines += ["sleep 120", 'rm -f "$D/code/enroll.code"']
    return "\n".join(lines) + "\n"


def _azure_cloud_init(script: str) -> str:
    """Base64 cloud-init for Ubuntu: install Docker, then run the bootstrap script."""
    doc = "\n".join([
        "#cloud-config",
        "package_update: true",
        "packages: [docker.io]",
        "write_files:",
        f"  - path: {_HOST_DIR}/bootstrap.sh",
        "    permissions: '0700'",
        "    encoding: b64",
        f"    content: {_b64(script)}",
        "runcmd:",
        "  - [systemctl, enable, --now, docker]",
        f"  - [bash, {_HOST_DIR}/bootstrap.sh]",
        ""])
    return _b64(doc)


# ── registry ──────────────────────────────────────────────────────────────────

def get(db, host_id: str):
    from ..database import AgentHost
    return db.query(AgentHost).filter(AgentHost.id == host_id).first()


def for_agent(db, agent_id: str):
    from ..database import AgentHost
    return db.query(AgentHost).filter(AgentHost.agent_id == agent_id).first()


def cidrs_of(host) -> list:
    try:
        return list(json.loads(host.ansible_cidrs or "[]"))
    except ValueError:
        return []


def serialize(host, agent=None, window=None) -> dict:
    return {
        "id": host.id, "agent_id": host.agent_id,
        "agent_name": agent.name if agent is not None else "",
        "name": host.name, "cloud": host.cloud, "region": host.region,
        "zone": host.zone or "", "subnet": host.subnet or "",
        "network": host.network or "",
        "instance_size": host.instance_size or DEFAULT_SIZES.get(host.cloud, ""),
        "ansible_cidrs": cidrs_of(host), "max_concurrent": host.max_concurrent,
        "mode": host.mode,
        "until_at": host.until_at.isoformat() if host.until_at else None,
        "change_window_id": host.change_window_id or "",
        "change_window_name": window.name if window is not None else "",
        "lead_minutes": host.lead_minutes, "grace_minutes": host.grace_minutes,
        "status": host.status, "host_id": host.host_id or "", "error": host.error or "",
        "last_job_id": host.last_job_id or "",
        "next_start_at": host.next_start_at.isoformat() if host.next_start_at else None,
        "next_stop_at": host.next_stop_at.isoformat() if host.next_stop_at else None,
        "started_at": host.started_at.isoformat() if host.started_at else None,
        "created_by": host.created_by or "",
        "created_at": host.created_at.isoformat() if host.created_at else None,
    }


def live_count(db, cloud: str, region: Optional[str] = None) -> int:
    """Hosts in ``cloud`` (and ``region``) that are or are about to be in the cloud — for
    the NAT reclaim checks, which must not cut egress from under a live agent."""
    from ..database import AgentHost
    q = db.query(AgentHost).filter(AgentHost.cloud == cloud,
                                   AgentHost.status.in_(LIVE_STATUSES))
    if region:
        q = q.filter(AgentHost.region == region)
    return q.count()


def create(db, *, name: str, cloud: str, region: str, cidrs, max_concurrent=2,
           subnet: str = "", instance_size: str = "", lifecycle: dict,
           created_by: str = ""):
    """Register the agent and its host record. Nothing is launched here — the caller
    queues ``agent_host_start`` (or leaves a window-mode agent for the sweep)."""
    from ..database import AgentHost
    from . import agent_service

    cloud = (cloud or "").strip().lower()
    if cloud not in CLOUDS:
        raise AgentHostError(f"Deploy to one of {', '.join(CLOUDS)}.")
    region = (region or "").strip()
    if not region:
        raise AgentHostError("Choose the region the agent runs in.")
    name = validate_name(name)
    ranges = normalize_cidrs(cidrs)
    try:
        slots = int(max_concurrent)
    except (TypeError, ValueError):
        slots = 0
    if not 1 <= slots <= MAX_CONCURRENT_CEILING:
        raise AgentHostError(f"Runs at once must be 1 to {MAX_CONCURRENT_CEILING}.")
    size = validate_size(cloud, instance_size)
    dashboard_url()                       # refuse now, not at boot, if it cannot work

    try:
        agent, _code = agent_service.create_agent(
            db, name=name, site=f"{cloud}/{region}",
            description="Deployed by the dashboard for Config Management.",
            created_by=created_by)
    except agent_service.AgentError as exc:
        raise AgentHostError(str(exc)) from None
    # The enrolment code minted with the row is discarded unused: every start mints its
    # own. Grant exactly the one job type its policy allows.
    agent.enroll_code_hash = None
    agent.agent_version = DEPLOYED_AGENT_VERSION
    db.commit()
    agent_service.set_allowed_job_types(db, agent, ["agent_ansible"])

    host = AgentHost(agent_id=agent.id, name=name, cloud=cloud, region=region,
                     subnet=(subnet or "").strip() or None, instance_size=size,
                     ansible_cidrs=json.dumps(ranges), max_concurrent=slots,
                     status="parked", created_by=created_by or None, **lifecycle)
    db.add(host)
    db.commit()
    db.refresh(host)
    return host, agent


def busy(db, host) -> bool:
    """Whether a start/stop/delete for this host is queued or running."""
    from ..database import Job
    if not host.last_job_id:
        return False
    job = db.query(Job).filter(Job.id == host.last_job_id).first()
    return job is not None and job.status in ("pending", "running")


def enqueue(db, host, action: str, *, created_by: str = "system") -> str:
    """Queue ``agent_host_<action>`` for this host and return the job id."""
    from . import job_service
    if action not in ("start", "stop", "delete"):
        raise AgentHostError(f"Unknown action {action!r}.")
    if busy(db, host):
        raise AgentHostError(
            "A start or stop for this agent is already in progress. Wait for it to finish.")
    job = job_service.create_job(
        db, job_type=f"agent_host_{action}", created_by=created_by,
        metadata={"host_id": host.id, "action": action,
                  "description": f"Agent host {host.name}: {action}"})
    host.last_job_id = job.id
    if action == "start" and host.status in ("parked", "error"):
        host.status = "provisioning"
    db.commit()
    return job.id


# ── identity ──────────────────────────────────────────────────────────────────

def identity_material(db, agent, cloud: str = "") -> dict:
    """What the host's agent proves itself with, minted for this start and single-use.

    SPIRE when the dashboard attests agents and its server is in use — the same entries
    "Migrate to SPIRE" creates, so the signing key never touches the host's disk. A one-time
    enrolment code otherwise.

    Under SPIRE the HOST proves itself with its cloud's instance identity when the operator
    ticked that cloud (dashboard_spire.cloud_attestor): no join token in the user-data, and
    the node entry pinning the VM is registered once it exists (register_node). A join
    token otherwise."""
    from . import agent_service, config_service, dashboard_spire
    if config_service.get_bool("spire_attest_enabled") and dashboard_spire.server_in_use():
        attestor = dashboard_spire.cloud_attestor(cloud)
        try:
            out = dashboard_spire.migrate_agent(db, agent, join_token=not attestor)
            address = dashboard_spire.server_address()
            port = dashboard_spire.server_port()
        except Exception as exc:  # noqa: BLE001 — named for the operator
            raise AgentHostError(
                f"SPIRE attestation is on, but the dashboard's SPIRE server could not mint "
                f"a join token: {exc}") from exc
        if not address:
            raise AgentHostError(
                "SPIRE attestation is on, but there is no SPIRE server address for agents. "
                "Set it under Settings → Integrations → Platform → SPIFFE and SPIRE.")
        host, _, maybe_port = address.partition(":")
        material = {"mode": "spire", "join_token": out["join_token"],
                    "node_attestor": attestor or "join_token",
                    "node_spiffe_id": out["node_spiffe_id"],
                    "trust_domain": out["trust_domain"], "bootstrap_pem": out["bootstrap_pem"],
                    "server_address": host, "server_port": int(maybe_port or port)}
        if attestor == "azure_imds":
            material["tenant_domain"] = (config_service.get("spire_azure_tenant_domain")
                                         or "").strip()
            if not material["tenant_domain"]:
                raise AgentHostError(
                    "Azure node attestation is ticked but has no tenant domain. Set it "
                    "under Settings → SPIFFE and SPIRE, or untick Azure there.")
        return material
    return {"mode": "code", "code": agent_service.reissue_enroll_code(db, agent)}


def register_node(host, material: dict, launched: dict) -> None:
    """For a host attesting with its cloud's identity: make the agent's node a node entry
    pinning the VM just launched. Raises AgentHostError. A no-op for a join token."""
    from . import config_service, dashboard_spire
    if material.get("node_attestor") in (None, "", "join_token"):
        return
    if host.cloud == "aws":
        facts = {"instance_id": launched.get("host_id") or ""}
    elif host.cloud == "gcp":
        p = _placement(host)
        facts = {"project": p["project"], "zone": launched.get("zone") or p["zone"],
                 "name": host.name}
    else:
        facts = {"subscription": (config_service.get("azure_subscription_id") or "").strip(),
                 "resource_group": _placement(host)["resource_group"], "name": host.name}
    try:
        dashboard_spire.register_cloud_node(
            material["node_spiffe_id"],
            dashboard_spire.cloud_node_selectors(host.cloud, **facts))
    except dashboard_spire.DashboardSpireError as exc:
        raise AgentHostError(f"Could not register the host with the SPIRE server: {exc}") from exc


def forget_node(agent) -> None:
    """Evict a stopped host's attested VM and drop the node entry pinning it, so nothing
    attested as that VM keeps an identity. Best-effort; nothing for a join-token host."""
    from . import dashboard_spire
    parts = (agent.spiffe_id or "").split("/")
    if len(parts) != 5 or not dashboard_spire.server_in_use():
        return
    node = dashboard_spire.ids_for(parts[2], agent.id)[1]
    try:
        problems = dashboard_spire.forget_cloud_node(node)
    except Exception as exc:  # noqa: BLE001 - a stop must not fail on SPIRE
        problems = [str(exc)]
    if problems:
        logger.warning("agent host for %s: SPIRE node cleanup left: %s", agent.name,
                       "; ".join(problems))


def park_identity(db, agent) -> None:
    """Make a destroyed host's identity dead: no key and no live enrolment code. The
    SPIFFE binding stays, so the next start's join token attests as the same agent."""
    agent.public_key = None
    agent.enroll_code_hash = None
    agent.enroll_expires_at = None
    db.commit()


# ── the cloud half ────────────────────────────────────────────────────────────

def _placement(host) -> dict:
    from .region_config import resolve_region
    rc = resolve_region(host.cloud, host.region)
    if host.cloud == "aws":
        subnet = host.subnet or rc.get("default_subnet_id") or ""
        sg = rc.get("default_security_group_id") or ""
        if not subnet or not sg:
            raise AgentHostError(
                f"No VM subnet or security group is configured for AWS {host.region}. "
                f"Set them under Settings → Clouds → AWS, or name a subnet on the agent.")
        return {"subnet": subnet, "security_groups": [s for s in sg.split(",") if s]}
    if host.cloud == "azure":
        subnet = host.subnet or rc.get("default_subnet_id") or ""
        rg = rc.get("resource_group") or ""
        if not subnet or not rg:
            raise AgentHostError(
                f"No VM subnet or resource group is configured for Azure {host.region}.")
        return {"subnet": subnet, "resource_group": rg}
    subnet = host.subnet or rc.get("subnetwork") or ""
    project = _cfg("gcp_project_id")
    if not project or not (subnet or rc.get("network")):
        raise AgentHostError(f"No GCP project or VM subnetwork for {host.region}.")
    tags = [t.strip() for t in (rc.get("default_network_tag") or "").split(",") if t.strip()]
    return {"project": project, "subnetwork": subnet, "network": rc.get("network") or "",
            "zone": host.zone or rc.get("zone") or "", "tag": tags[0] if tags else ""}


async def _ensure_egress(host) -> None:
    """The VMs' on-demand egress, which a private agent needs to reach the dashboard.
    Best-effort like the VM deploy's: an install with its own NAT reports None."""
    try:
        if host.cloud == "aws":
            from . import nat_instance_service
            await nat_instance_service.ensure_nat_instance(host.region)
        elif host.cloud == "gcp":
            from . import gcp_nat_service
            await gcp_nat_service.ensure_vm_nat(host.region)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent host %s: egress ensure failed (continuing): %s", host.name, exc)


async def _release_egress(db, host) -> None:
    try:
        if host.cloud == "aws":
            from . import nat_instance_service
            await nat_instance_service.reclaim_nat_instance(db, host.region)
        elif host.cloud == "gcp":
            from . import gcp_nat_service
            await gcp_nat_service.reclaim_vm_nat(db, host.region)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent host %s: egress reclaim failed (non-fatal): %s", host.name, exc)


async def launch(host, script: str) -> dict:
    """Start the host. Returns ``{"host_id", "zone", "network"}`` — ``network`` in the raw
    form its cloud reports it, normalised by the caller. Overridable in tests and the
    end-to-end harness, which is why the cloud SDKs are reached only through here."""
    p = _placement(host)
    size = host.instance_size or DEFAULT_SIZES[host.cloud]
    if host.cloud == "aws":
        from . import aws_service
        ami = await aws_service.get_ssm_parameter(host.region, aws_service.ECS_OPTIMIZED_AMI_SSM)
        inst = await aws_service.run_ec2_container_node(
            host.region, ami_id=ami, instance_type=size, subnet_id=p["subnet"],
            security_group_ids=p["security_groups"], user_data=script, name_tag=host.name,
            purpose=PURPOSE, public_ip=False)
        return {"host_id": inst["instance_id"], "zone": "",
                "network": inst.get("vpc_id") or ""}
    if host.cloud == "azure":
        from . import azure_service
        res = await azure_service.run_vm_container_node(
            p["resource_group"], host.region, subnet_id=p["subnet"], name=host.name,
            vm_size=size, admin_password=azure_service._azure_compliant_password(),
            cloud_init_b64=_azure_cloud_init(script), purpose=PURPOSE,
            container_image=AGENT_IMAGE, public_ip=False)
        return {"host_id": res["name"], "zone": "", "network": p["subnet"]}
    from . import gcp_service
    res = await gcp_service.run_gce_startup_node(
        p["project"], host.region, p["zone"], host.name, startup_script=script,
        subnetwork=p["subnetwork"], network=p["network"], machine_type=size,
        labels={"purpose": PURPOSE}, network_tag=p["tag"])
    return {"host_id": res["name"], "zone": res["zone"], "network": res.get("network") or ""}


async def destroy(host) -> None:
    """Remove the host from the cloud. A host already gone is not an error."""
    if host.cloud == "aws":
        from . import aws_service
        ids = {n["instance_id"] for n in await aws_service.list_ec2_container_nodes(
            host.region, PURPOSE) if n.get("name") == host.name and n.get("instance_id")}
        if host.host_id:
            ids.add(host.host_id)
        for iid in ids:
            await aws_service.terminate_instance(host.region, iid)
        return
    if host.cloud == "azure":
        from . import azure_service
        await azure_service.stop_vm_container_node(_placement(host)["resource_group"],
                                                   host.name)
        return
    from . import gcp_service
    try:
        await gcp_service.terminate_instance(_cfg("gcp_project_id"), host.zone or "",
                                             host.name)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if not ("404" in msg or "not found" in msg.lower()):
            raise


# ── the jobs ──────────────────────────────────────────────────────────────────

async def run(db, *, job_id: str, meta: dict) -> None:
    """``agent_host_start`` / ``agent_host_stop`` / ``agent_host_delete``."""
    from . import job_service
    host = get(db, meta.get("host_id") or "")
    if host is None:
        job_service.set_failed(db, job_id, "That deployed agent no longer exists.")
        return
    action = meta.get("action")
    try:
        if action == "start":
            await _start(db, host, job_id)
        elif action == "stop":
            await _stop(db, host, job_id)
        elif action == "delete":
            await _delete(db, host, job_id)
        else:
            raise AgentHostError(f"Unknown action {action!r}.")
    except Exception as exc:  # noqa: BLE001 — recorded on the row and the job
        logger.exception("agent host %s: %s failed", host.name, action)
        db.rollback()
        host = get(db, meta.get("host_id") or "")
        if host is not None and action != "delete":
            host.status = "error"
            host.error = str(exc)[:2000]
            db.commit()
        job_service.set_failed(db, job_id, str(exc)[:2000])


async def _start(db, host, job_id: str) -> None:
    from ..database import RemoteAgent
    from . import agent_service, job_service
    job_service.set_running(db, job_id)
    agent = db.query(RemoteAgent).filter(RemoteAgent.id == host.agent_id).first()
    if agent is None or not agent.is_active:
        raise AgentHostError("This agent was revoked. Delete it and deploy a new one.")
    if host.host_id:
        raise AgentHostError(
            f"A host ({host.host_id}) is already recorded for this agent. Stop it first.")
    host.status = "starting"
    host.error = None
    db.commit()

    job_service.update_progress(db, job_id, 10, "Minting a single-use identity")
    url = dashboard_url()
    material = identity_material(db, agent, host.cloud)
    spire = material.get("mode") == "spire"
    script = bootstrap_script(url=url, policy=policy_yaml(cidrs_of(host),
                                                         host.max_concurrent,
                                                         upgrades=spire),
                              material=material, image=agent_image(db))

    job_service.update_progress(db, job_id, 20, "Opening egress for the subnet")
    await _ensure_egress(host)

    job_service.update_progress(db, job_id, 30, f"Launching {host.cloud} host {host.name}")
    try:
        res = await launch(host, script)
        # The VM exists, so its identity can be pinned: before this its SPIRE agent may
        # already have attested, but nothing hands it the agent's SVID until it is.
        register_node(host, material, res)
    except Exception:
        # A failed launch may still have left a VM behind (Azure's create can fail after
        # the NIC exists; AWS after run_instances). Remove anything by name before failing.
        try:
            await destroy(host)
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent host %s: cleanup after a failed launch failed: %s",
                           host.name, exc)
        forget_node(agent)
        park_identity(db, agent)
        raise
    host.host_id = res.get("host_id") or host.name
    host.zone = res.get("zone") or host.zone
    from . import config_mgmt_route_service as cmr
    host.network = cmr.normalize_network(host.cloud, res.get("network") or "") or host.network
    db.commit()
    _narrow_routes_to_network(db, host)

    job_service.update_progress(db, job_id, 50, "Waiting for the agent to come online")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ONLINE_TIMEOUT_S
    while True:
        db.expire_all()
        agent = db.query(RemoteAgent).filter(RemoteAgent.id == host.agent_id).first()
        if agent is not None and agent_service.status_of(agent) == "online":
            break
        if loop.time() > deadline:
            attestor = material.get("node_attestor")
            raise AgentHostError(
                f"The host started but the agent did not come online within "
                f"{ONLINE_TIMEOUT_S // 60} minutes. Check that the subnet reaches "
                f"{url} (and, when attesting, the SPIRE server), then stop and start it."
                + (f" It attests with its cloud's identity: check that the SPIRE server "
                   f"enables NodeAttestor \"{attestor}\" with the access it needs (Settings "
                   f"→ SPIFFE and SPIRE; docs: deploying an agent, Identity)."
                   if attestor and attestor != "join_token" else ""))
        await asyncio.sleep(_ONLINE_POLL_S)

    host = get(db, host.id)
    host.status = "running"
    host.started_at = _utcnow()
    if host.mode == "window":
        from . import change_window_service
        window = change_window_service.get(db, host.change_window_id or "")
        span = window_span(host, window, host.started_at) if window is not None else None
        host.next_stop_at = span[1] if span else None
    db.commit()
    job_service.update_progress(db, job_id, 100, f"Agent {agent.name} is online.")
    job_service.set_completed(db, job_id, {"host_id": host.host_id})


def _narrow_routes_to_network(db, host) -> list:
    """Narrow this agent's own deploy-created routes to the network its host runs in.

    The agent can reach only that VPC or VNet, so a route of its for a range elsewhere in
    the same region could only ever hand it VMs it cannot reach. Only routes the deploy
    created (label ``"<name> (deployed)"``) with no network yet are touched; one the
    operator narrowed or added by hand is theirs. A clash with an existing route for that
    network is left as it is and logged. Returns the CIDRs narrowed."""
    from . import config_mgmt_route_service as cmr
    if not host.network:
        return []
    narrowed = []
    for route in cmr.list_routes(db):
        if (route["agent_id"] != host.agent_id or route["scope"] != host.cloud
                or route["network"] or route["label"] != f"{host.name} (deployed)"):
            continue
        try:
            cmr.update(db, route["id"], network=host.network)
            narrowed.append(route["cidr"])
        except cmr.ConfigRouteError as exc:
            db.rollback()
            logger.info("agent host %s: route %s left unnarrowed: %s",
                        host.name, route["cidr"], exc)
    return narrowed


async def _drain(db, host, job_id: str) -> None:
    """Let this agent's running Config-Management jobs finish, for up to the grace period.

    A run already going when its window closes is left to finish, the same rule the change
    window sweep applies to the dashboard's own jobs. Past the grace it is stopped with the
    host, and the dashboard fails it when its heartbeat goes stale."""
    from ..database import Job
    from . import job_service
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0, int(host.grace_minutes or 0)) * 60
    while True:
        db.expire_all()
        running = (db.query(Job)
                   .filter(Job.agent_id == host.agent_id, Job.status == "running").count())
        if running == 0 or loop.time() > deadline:
            return
        job_service.update_progress(db, job_id, 20,
                                    f"Waiting for {running} running job(s) to finish")
        await asyncio.sleep(_STOP_DRAIN_POLL_S)


async def _stop(db, host, job_id: str, *, final: bool = False) -> None:
    from ..database import RemoteAgent
    from . import job_service
    job_service.set_running(db, job_id)
    host.status = "stopping"
    db.commit()
    await _drain(db, host, job_id)

    job_service.update_progress(db, job_id, 60, f"Removing host {host.name}")
    await destroy(host)
    agent = db.query(RemoteAgent).filter(RemoteAgent.id == host.agent_id).first()
    if agent is not None:
        forget_node(agent)
        park_identity(db, agent)
    host = get(db, host.id)
    host.host_id = None
    host.started_at = None
    host.next_stop_at = None             # re-planned by the next sweep
    host.status = "parked"
    host.error = None
    if host.mode == "until" and host.until_at and host.until_at <= _utcnow():
        # Its time is up: it stays parked for good, rather than being started again.
        host.mode = "always"
        host.until_at = None
    db.commit()
    await _release_egress(db, host)
    if not final:
        job_service.update_progress(db, job_id, 100, f"Agent host {host.name} is parked.")
        job_service.set_completed(db, job_id, {"name": host.name})


async def _delete(db, host, job_id: str) -> None:
    """Stop the host if it is up, then remove the agent, its routes and this record."""
    from ..database import RemoteAgent
    from . import agent_service, job_service
    if host.host_id or host.status in LIVE_STATUSES:
        await _stop(db, host, job_id, final=True)
    else:
        job_service.set_running(db, job_id)
    host = get(db, host.id)
    agent = db.query(RemoteAgent).filter(RemoteAgent.id == host.agent_id).first()
    name = host.name
    db.delete(host)
    db.commit()
    if agent is not None:
        agent_service.revoke_agent(db, agent)
        agent_service.delete_agent(db, agent)
    job_service.update_progress(db, job_id, 100, f"Deployed agent {name} deleted.")
    job_service.set_completed(db, job_id, {"name": name})


# ── the schedule ──────────────────────────────────────────────────────────────

def plan(host, window, now: datetime) -> tuple:
    """``(action, next_start, next_stop)`` for one host at ``now`` — pure, so the schedule
    is testable against any clock.

    ``action`` is ``"start"``, ``"stop"`` or ``None``. Only the scheduled modes act:
    ``always`` is up or down because a person said so. A window-mode host should exist from
    ``lead_minutes`` before an occurrence to ``grace_minutes`` after it; an until-mode host
    until ``until_at``."""
    if host.mode == "until":
        if host.until_at and now >= host.until_at and _up(host):
            return "stop", None, host.until_at
        return None, None, host.until_at
    if host.mode != "window" or window is None or window.enabled is False:
        return None, None, None
    span = window_span(host, window, now)
    if span is None:
        return None, None, None
    up_from, up_until = span
    if host.status == "running":
        # Pinned when the host came up (see _start), so a host started by hand ahead of
        # its window stays up through that window rather than being stopped at once.
        stop_at = host.next_stop_at or up_until
        return ("stop" if now >= stop_at else None), up_from, stop_at
    if _up(host) and not up_from <= now < up_until:
        # Failed part-way with a host left behind, and outside its window: take it down.
        return "stop", up_from, up_until
    if host.next_start_at and up_from < host.next_start_at and now < host.next_start_at:
        # Stopped by hand during this window (see hold_until_next_window): stay down.
        return None, host.next_start_at, None
    if up_from <= now < up_until and host.status == "parked" \
            and up_until - now >= _MIN_USEFUL_UP:
        return "start", up_from, up_until
    return None, up_from, up_until


def hold_until_next_window(db, host, now: Optional[datetime] = None) -> None:
    """A person stopped a window-mode host during its window: keep it down until the next
    occurrence, rather than letting the next sweep start it again."""
    from . import change_window_service
    if host.mode != "window":
        return
    window = change_window_service.get(db, host.change_window_id or "")
    now = now or _utcnow()
    span = window_span(host, window, now) if window is not None else None
    if span is None or not span[0] <= now < span[1]:
        return
    following = window_span(host, window, span[1])
    host.next_start_at = following[0] if following else None
    db.commit()


def _up(host) -> bool:
    """Running, or failed with a host still in the cloud — either way, one to take down
    when its time is over. (A failed host with nothing in the cloud waits for a person:
    retrying a broken start every sweep would only repeat the failure.)"""
    return host.status == "running" or (host.status == "error" and bool(host.host_id))


def window_span(host, window, now: datetime) -> Optional[tuple]:
    """``(up_from, up_until)`` of the occurrence that is current or next at ``now``:
    the window widened by the host's lead and grace. ``None`` if it never recurs."""
    from . import change_window
    lead = timedelta(minutes=int(host.lead_minutes or 0))
    grace = timedelta(minutes=int(host.grace_minutes or 0))
    try:
        start, end = change_window.next_occurrence(window, now - grace)
    except change_window.ScheduleError:
        return None
    return start - lead, end + grace


def will_be_up(db, host, at: datetime) -> bool:
    """Whether this host is scheduled to be up at ``at`` — for booking a run ahead into a
    window while the agent is parked."""
    if host.mode == "always":
        return host.status in LIVE_STATUSES or host.status == "provisioning"
    if host.mode == "until":
        return bool(host.until_at) and at < host.until_at and host.status != "parked"
    if host.mode != "window":
        return False
    from . import change_window_service
    window = change_window_service.get(db, host.change_window_id or "")
    if window is None or window.enabled is False:
        return False
    span = window_span(host, window, at)
    return span is not None and span[0] <= at < span[1]


def sweep(db, now: Optional[datetime] = None) -> list:
    """One pass: queue the starts and stops the schedule calls for, and record the next
    times for the Agents page. Returns ``[(host name, action), …]`` queued."""
    from ..database import AgentHost
    from . import change_window_service
    now = now or _utcnow()
    queued = []
    for host in db.query(AgentHost).filter(AgentHost.mode.in_(("until", "window"))).all():
        window = (change_window_service.get(db, host.change_window_id)
                  if host.change_window_id else None)
        action, next_start, next_stop = plan(host, window, now)
        host.next_start_at, host.next_stop_at = next_start, next_stop
        db.commit()
        if action and not busy(db, host):
            try:
                enqueue(db, host, action)
                queued.append((host.name, action))
            except AgentHostError as exc:
                logger.info("agent host %s: %s not queued: %s", host.name, action, exc)
    return queued


def booking_refusal(db, agent, at: Optional[datetime]) -> str:
    """Why a run cannot be booked onto this offline agent for ``at``, or "" if it can.

    An agent the dashboard deployed is offline between windows by design, so the enqueue
    gate's "not online" would refuse exactly the runs a window-mode agent exists for. A
    run scheduled into a time its host will be up is accepted; anything else is refused
    naming when the agent will next be up, so the operator can book into that."""
    host = for_agent(db, agent.id)
    if host is None:
        return ""
    if at is not None and will_be_up(db, host, at):
        return ""
    if host.status in ("provisioning", "starting"):
        return (f"Agent '{agent.name}' is starting in {host.cloud} and is not online yet. "
                f"Run again once it is, or schedule the run for later.")
    if host.mode == "window":
        from . import change_window_service
        window = change_window_service.get(db, host.change_window_id or "")
        if window is None or window.enabled is False:
            return (f"Agent '{agent.name}' follows a change window that is gone or "
                    f"disabled, so it will not start. Start it on the Agents page.")
        _, up_from, up_until = plan(host, window, at or _utcnow())
        when = (f" It is next up from {up_from:%Y-%m-%d %H:%M} to {up_until:%H:%M} UTC."
                if up_from and up_until else
                f" It is next up from {up_from:%Y-%m-%d %H:%M} UTC." if up_from else "")
        return (f"Agent '{agent.name}' is parked until its change window '{window.name}'. "
                f"Schedule this run into that window, or start the agent now.{when}")
    return (f"Agent '{agent.name}' is a deployed agent whose host is {host.status}. Start "
            f"it on the Agents page, then run again. Nothing was queued.")
