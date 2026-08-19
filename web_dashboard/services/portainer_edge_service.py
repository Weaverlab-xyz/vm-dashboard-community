"""Register an Edge-agent environment and hand back the command that joins it.

This is the answer to "do I have to wire up the connections again". A managed
Portainer node cannot reach a workstation or a LAN Docker host: it runs
unprivileged with no Docker socket of its own, and it sits on a public IP behind a
fail-closed firewall with no route into anyone's network. No import can change
that, which is why the migration bundle records environments as reference data and
never replays them.

An Edge agent inverts the direction. The agent runs ON the Docker host and polls
OUT to the node's tunnel port, so the only connectivity required is outbound from
the operator's network — nothing inbound, no VPN, no firewall change on their side.

The node's firewall already opens 8000 for exactly this
(``gcp_service._ensure_portainer_firewall_sync``), so registration is the missing
half rather than a new network design.

**The one sharp edge:** an Edge key is derived from the node's URL, its tunnel host
and the new endpoint id. The managed node takes an EPHEMERAL external IP, so every
recreate changes the URL and silently invalidates every key minted before it. Keys
are therefore never cached — each registration mints a fresh one against the
node's current URL, and :func:`stale_keys_warning` is what tells an operator their
existing agents need re-joining.
"""
import logging
import uuid

from . import config_service, portainer_service

logger = logging.getLogger(__name__)

#: Default Edge agent image. Matched to the server image's channel — a CE server
#: talks to the CE agent.
DEFAULT_AGENT_IMAGE = "portainer/agent:latest"

#: Portainer's Edge tunnel port. Fixed by the node's firewall rule, which opens
#: 9443 (UI/API) and 8000 (this).
EDGE_TUNNEL_PORT = 8000

#: How often the agent checks in, seconds. Portainer's own default.
DEFAULT_CHECKIN_INTERVAL = 5


def _node_url() -> str:
    return (config_service.get("portainer_url") or "").strip().rstrip("/")


def generate_edge_id() -> str:
    """A fresh agent identity.

    Portainer only assigns one itself when its ``EnforceEdgeID`` setting is on, and
    a blank EDGE_ID makes the agent register as a duplicate of whatever else has a
    blank one — so the dashboard always supplies a UUID.
    """
    return str(uuid.uuid4())


def join_command(edge_id: str, edge_key: str, *,
                 image: str = "", insecure_poll: bool = True) -> str:
    """The ``docker run`` an operator pastes on the Docker host they want managed.

    ``insecure_poll`` defaults to True because the managed node serves a SELF-SIGNED
    certificate on 9443 (the deploy turns ``portainer_verify_ssl`` off for the same
    reason). Without ``EDGE_INSECURE_POLL=1`` the agent's very first poll fails
    certificate verification and the environment simply never comes up — with no
    error anywhere the operator would think to look.
    """
    image = image or DEFAULT_AGENT_IMAGE
    lines = [
        "docker run -d \\",
        "  -v /var/run/docker.sock:/var/run/docker.sock \\",
        "  -v /var/lib/docker/volumes:/var/lib/docker/volumes \\",
        "  -v /:/host \\",
        "  -v portainer_agent_data:/data \\",
        "  --restart always \\",
        "  -e EDGE=1 \\",
        f"  -e EDGE_ID={edge_id} \\",
        f"  -e EDGE_KEY={edge_key} \\",
    ]
    if insecure_poll:
        lines.append("  -e EDGE_INSECURE_POLL=1 \\")
    lines += [
        "  --name portainer_edge_agent \\",
        f"  {image}",
    ]
    return "\n".join(lines)


def stale_keys_warning(nodes: list) -> str:
    """Whether previously issued Edge keys can still be valid, as a sentence or ``""``.

    An Edge key encodes the node URL it was minted against. The managed node takes an
    ephemeral external IP, so a recreate changes that URL and every existing agent
    stops being able to check in — which presents as environments going quietly
    offline, not as an error. Comparing the configured URL against the live node is
    the only way to notice.
    """
    configured = _node_url()
    if not configured or not nodes:
        return ""
    live = {(n.get("url") or "").strip().rstrip("/") for n in nodes if n.get("url")}
    if live and configured not in live:
        return (f"The stored Portainer URL ({configured}) does not match the running "
                f"node ({', '.join(sorted(live))}). Edge keys are derived from the "
                f"node URL, so any agent joined before the address changed can no "
                f"longer check in and must be re-joined with a new key.")
    return ""


async def register(name: str, *, image: str = "",
                   checkin_interval: int = DEFAULT_CHECKIN_INTERVAL) -> dict:
    """Create an Edge environment and return everything needed to join it.

    Runs inline rather than as a job: it is two HTTP calls against a node the
    dashboard already holds a token for, with nothing to poll and nothing to reap.

    Returns ``{endpoint_id, name, edge_id, edge_key, join_command, tunnel_port,
    server_url}``.
    """
    server_url = _node_url()
    endpoint = await portainer_service.create_edge_endpoint(
        name, server_url, checkin_interval=checkin_interval)
    edge_key = str(endpoint.get("EdgeKey") or "")
    # Portainer sets EdgeID itself only under EnforceEdgeID; otherwise the agent
    # brings its own and we must be the one to choose it.
    edge_id = str(endpoint.get("EdgeID") or "") or generate_edge_id()
    if not edge_key:
        # The environment exists but cannot be joined. Say so rather than returning a
        # command with an empty key, which would fail on the operator's host with a
        # message about the agent, not about this.
        raise portainer_service.PortainerError(
            f"Portainer created the Edge environment '{name}' (id "
            f"{endpoint.get('Id')}) but returned no Edge key, so it cannot be joined. "
            f"Retrieve the key from the Portainer UI, or delete the environment and "
            f"try again.")
    return {
        "endpoint_id": int(endpoint.get("Id") or 0),
        "name": endpoint.get("Name") or name,
        "edge_id": edge_id,
        "edge_key": edge_key,
        "server_url": server_url,
        "tunnel_port": EDGE_TUNNEL_PORT,
        "join_command": join_command(edge_id, edge_key, image=image),
    }
