"""Config-Management execution routes: which agent runs a playbook against an address.

Until this module existed, a Config-Management run against a hypervisor-synced VM had to
be executed by the agent whose connection discovered it. That was never a decision so
much as an artefact: ``inventory_service._hv_item`` set a target's ``agent_id`` to its
connection's, so the two were always equal and the check in ``api/config_mgmt.py`` that
compared them never fired.

It is the wrong rule for the topology agent-bound connections exist to serve. VMware
Workstation's ``vmrest`` binds 127.0.0.1 with no bind-address option, so the brokering
agent must run ON the Windows host under Docker Desktop — whose WSL2 VM can reach no
VMware vmnet. The agent that can read the inventory cannot reach the VMs in it.

**One rule, one implementation.** Both the target picker and the enqueue gate resolve the
executing agent through this module, so they cannot disagree about who will run a job:
the picker names an agent, and the gate accepts exactly that one. See
:func:`RouteTable.executor_for`. Two copies of the rule is the failure this module's
shape exists to prevent, and ``tests/test_config_mgmt_route_single_rule.py`` pins it.

**What a route may not do.** It decides *who executes*, never *what address is targeted*.
The address stays pinned to one the discovering agent itself reported, and
``_resolve_agent_target`` resolves the executor from that already-pinned address — so a
route is an input to the agent decision and can never become an input to the address
decision. That ordering is the anti-substitution control; do not reverse it.

Matching is longest-prefix-first, like a routing table. The unique constraint on
``cidr`` is what makes that order total: two agents claiming one range is the single
ambiguity prefix length cannot settle, so it is refused at write time instead.
"""
import ipaddress
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# EVERY import above is stdlib, and the ORM/model imports below are LOCAL TO THE
# FUNCTIONS THAT NEED THEM. That is deliberate, not untidiness: it keeps
# `normalize_cidr`, `Route` and `RouteTable` — the half that actually decides which agent
# runs a job — importable and unit-testable without the app's dependencies installed,
# which is the difference between that logic being covered and being covered only on CI.
# `tests/test_config_mgmt_routes.py` imports this module directly on that basis.
# Please do not hoist them.


class ConfigRouteError(Exception):
    """An operator-facing refusal, like ``HypervisorConnectionError``.

    Every message names the cause *and* the fix: these are read in a form, by someone who
    has just typed something, and "invalid CIDR" tells them nothing they did not know.
    """


# Ranges every agent denies outright, whatever its policy.yaml says — mirroring
# `_DEFAULT_DENY` in runners/agent/agent.py, which is re-added unconditionally at policy
# load so that even `ansible.targets: 0.0.0.0/0` cannot reach them. A route for one of
# these can only ever produce jobs that fail on the agent's own gate, so it is refused
# here rather than accepted and left to disappoint later.
_ALWAYS_DENIED = tuple(ipaddress.ip_network(c) for c in (
    "127.0.0.0/8", "169.254.0.0/16", "::1/128", "fe80::/10",
))


def normalize_cidr(raw: str) -> str:
    """The stored form of an operator's range, or raise :class:`ConfigRouteError`.

    Refuses rather than normalises when the input is ambiguous. ``192.168.235.1/24`` is
    almost certainly a typo for the ``.0`` network, but ``192.168.235.0/8`` silently
    widened to ``192.0.0.0/8`` would hand three agents' worth of hosts to one — so host
    bits set is an error that names the network it probably meant, and never an
    assumption.
    """
    text = (raw or "").strip()
    if not text:
        raise ConfigRouteError(
            "Enter a network range like 192.168.235.0/24, or a single host as "
            "192.168.235.99/32.")

    # A bare address is the single-host case, which is common enough for one lab VM that
    # making the operator write /32 would just be pedantry.
    if "/" not in text:
        try:
            addr = ipaddress.ip_address(text)
        except ValueError:
            raise ConfigRouteError(
                f"{text!r} is not an IP address or a network range. Enter a range like "
                f"192.168.235.0/24, or a single host as 192.168.235.99.")
        text = f"{addr}/{addr.max_prefixlen}"

    try:
        network = ipaddress.ip_network(text, strict=True)
    except ValueError:
        # Two very different mistakes arrive as the same exception, and they want
        # different messages: genuinely malformed, versus a real range written against
        # a host address inside it.
        try:
            loose = ipaddress.ip_network(text, strict=False)
        except ValueError:
            raise ConfigRouteError(
                f"{text!r} is not a network range. Enter one like 192.168.235.0/24, or "
                f"a single host as 192.168.235.99/32.")
        host = text.split("/", 1)[0]
        raise ConfigRouteError(
            f"{text!r} is not a network address — did you mean {loose}? A range must "
            f"name the network itself. To route that one host instead, write "
            f"{host}/{loose.max_prefixlen}.")

    for denied in _ALWAYS_DENIED:
        # subnet_of, not overlaps: 0.0.0.0/0 contains loopback and is still a legitimate
        # single-agent lab route, because a narrower row always wins over it.
        if network.version == denied.version and network.subnet_of(denied):
            raise ConfigRouteError(
                f"{network} can never be a Config-Management target: every agent denies "
                f"loopback and link-local ranges outright, whatever its policy.yaml says, "
                f"so a route for it would only produce jobs that fail. Enter the address "
                f"the runner reaches the guest ON instead — the one the hypervisor sync "
                f"reports for it, like 192.168.235.0/24.")

    return str(network)


@dataclass(frozen=True)
class Route:
    """One parsed row. ``cidr`` is kept alongside ``network`` because refusal messages
    quote the operator's own stored string, not ``str(network)``."""
    id: str
    agent_id: str
    network: object          # IPv4Network | IPv6Network
    label: str
    cidr: str


@dataclass(frozen=True)
class RouteTable:
    """Pre-parsed and pre-sorted: longest prefix first, then version, then address.

    Frozen and address-in/agent-out, with no session of its own, so the pure projection
    functions in ``inventory_service`` can take one as an argument. That is what keeps
    the executor resolvable per-VM without a query per VM.
    """
    routes: tuple = ()

    def __bool__(self) -> bool:
        return bool(self.routes)

    def match_for(self, address: str) -> Optional[Route]:
        """The most specific route covering ``address``, or None.

        A non-IP (a hostname, or an empty string from a VM with no synced address) is
        *not* an error here — it simply matches nothing, so the caller falls back to the
        discovering agent. That is the conservative answer and the one that keeps a VM
        with no address pointing at the agent that would give it one.
        """
        try:
            addr = ipaddress.ip_address(str(address or "").strip())
        except ValueError:
            return None
        for route in self.routes:
            # `in` returns False across address families rather than raising, so v4 and
            # v6 rows can share one flat list.
            if addr in route.network:
                return route
        return None

    def executor_for(self, address: str, *, fallback: str = "") -> str:
        """Which agent id executes a run against ``address``.

        ``fallback`` is the brokering agent. With an empty table this returns it
        unchanged, which is the backwards-compatibility contract: an install with no
        routes behaves exactly as it did before this module existed.
        """
        match = self.match_for(address)
        return match.agent_id if match else fallback


EMPTY = RouteTable()


def load_table(db) -> RouteTable:
    """Every active route, in one query. This is the N+1 boundary.

    Callers that annotate many rows — ``inventory_service._hypervisor_items`` — load the
    table once and pass it down, in the same shape as the per-kind bulk workgroup
    override lookup that sits beside it.

    Deliberately does NOT filter on ``RemoteAgent.is_active``. A revoked delegate must
    stay visible so the operator gets told their route points at a revoked agent;
    dropping it here would silently route runs back to an agent with no path to the
    target, which is the original bug wearing a different hat.
    """
    from ..database import ConfigMgmtRoute

    parsed = []
    for row in db.query(ConfigMgmtRoute).filter(ConfigMgmtRoute.is_active.is_(True)).all():
        try:
            network = ipaddress.ip_network(row.cidr, strict=False)
        except ValueError:
            # Only reachable if a row was written around `create` — but a single bad row
            # must not take the whole inventory page down with it.
            logger.warning("config-management route %s holds an unparseable cidr %r",
                           row.id, row.cidr)
            continue
        parsed.append(Route(id=row.id, agent_id=row.agent_id, network=network,
                            label=row.label or "", cidr=row.cidr))
    parsed.sort(key=lambda r: (-r.network.prefixlen, r.network.version,
                               r.network.network_address))
    return RouteTable(tuple(parsed))


def executor_for(db, address: str, *, fallback: str = "") -> str:
    """One-shot convenience for a single run. Same answer as :meth:`RouteTable.executor_for`.

    ``tests/test_config_mgmt_routes.py`` pins that those two agree — two entry points
    into one rule is exactly the drift this module is shaped to avoid.
    """
    return load_table(db).executor_for(address, fallback=fallback)


def match_for(db, address: str) -> Optional[Route]:
    """The route covering ``address``, for a refusal that needs to name it."""
    return load_table(db).match_for(address)


# ── CRUD ──────────────────────────────────────────────────────────────────────

def serialize(row) -> dict:
    """The API/UI projection. There is no secret on this row and there never should be:
    a route names an agent and a range, and nothing that needs protecting."""
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "cidr": row.cidr,
        "label": row.label or "",
        "is_active": bool(row.is_active),
        "created_by": row.created_by or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def list_routes(db) -> list:
    """Most specific first, so the list reads in the order the matcher applies it."""
    from ..database import ConfigMgmtRoute

    def _sort_key(row):
        try:
            net = ipaddress.ip_network(row.cidr, strict=False)
        except ValueError:
            return (1, 9, 0)        # an unparseable row sorts last rather than raising
        return (-net.prefixlen, net.version, int(net.network_address))

    rows = db.query(ConfigMgmtRoute).all()
    return [serialize(r) for r in sorted(rows, key=_sort_key)]


def match_counts(db) -> dict:
    """``{route_id: how many synced VMs currently fall in its range}``, in one pass.

    This is the "did my CIDR actually bind to anything" answer, and it is why a route
    needs no Test button: there is nothing to dial, so the only useful feedback is
    whether the range covers real rows. Counted over every route at once rather than
    per route, because the cache is the largest table on the page.
    """
    from ..database import HypervisorConnection, HypervisorVMCache

    table = load_table(db)
    counts = {r.id: 0 for r in table.routes}
    if not table:
        return counts
    rows = (db.query(HypervisorVMCache.ip_addresses)
            .join(HypervisorConnection,
                  HypervisorConnection.id == HypervisorVMCache.connection_id)
            .filter(HypervisorConnection.is_active.is_(True))
            .all())
    for (raw,) in rows:
        try:
            ips = json.loads(raw or "[]")
        except (TypeError, ValueError):
            continue
        # The FIRST address only: that is the one `_hv_item` publishes as the target, so
        # counting every interface would overstate what a route actually governs.
        first = next((str(i) for i in ips if str(i).strip()), "")
        route = table.match_for(first)
        if route is not None:
            counts[route.id] = counts.get(route.id, 0) + 1
    return counts


def _get(db, route_id: str):
    from ..database import ConfigMgmtRoute

    row = db.query(ConfigMgmtRoute).filter(ConfigMgmtRoute.id == route_id).first()
    if row is None:
        raise ConfigRouteError("No such Config-Management route.")
    return row


def _check_agent(db, agent_id: str):
    """The agent must exist, be active, and be granted the job type it is about to own.

    Checked at write time rather than left to the run: a route naming an agent that
    refuses ``agent_ansible`` resolves *every* run in its range to a refusal, and the
    operator would meet that as a failed job rather than as a form error.
    """
    from ..database import RemoteAgent
    from . import agent_service

    agent = db.query(RemoteAgent).filter(RemoteAgent.id == (agent_id or "")).first()
    if agent is None:
        raise ConfigRouteError("That remote agent is not registered.")
    if not agent.is_active:
        raise ConfigRouteError(
            f"Agent '{agent.name}' is revoked, so a route naming it would send every run "
            f"in that range to an agent that cannot lease it. Re-enrol it first.")
    if "agent_ansible" not in agent_service.allowed_job_types(agent):
        raise ConfigRouteError(
            f"Agent '{agent.name}' is not granted the Config-Management job type, so this "
            f"route would resolve every run in its range to an agent that refuses it. "
            f"Grant `agent_ansible` on the Agents tab first, then add this route.")
    return agent


def _refuse_duplicate(db, cidr: str, *, excluding: str = "") -> None:
    from ..database import ConfigMgmtRoute, RemoteAgent

    query = db.query(ConfigMgmtRoute).filter(ConfigMgmtRoute.cidr == cidr)
    if excluding:
        query = query.filter(ConfigMgmtRoute.id != excluding)
    clash = query.first()
    if clash is None:
        return
    owner = db.query(RemoteAgent).filter(RemoteAgent.id == clash.agent_id).first()
    name = owner.name if owner else clash.agent_id
    label = f" ('{clash.label}')" if clash.label else ""
    raise ConfigRouteError(
        f"{cidr} is already routed to agent '{name}'{label}. One address range can name "
        f"only one executing agent, or the dashboard would have to guess which. Edit that "
        f"route, or add a narrower range — the most specific range wins.")


def create(db, *, agent_id: str, cidr: str, label: str = "",
           created_by: str = "") -> dict:
    from sqlalchemy.exc import IntegrityError

    from ..database import ConfigMgmtRoute

    _check_agent(db, agent_id)
    normalized = normalize_cidr(cidr)
    _refuse_duplicate(db, normalized)
    row = ConfigMgmtRoute(
        id=str(uuid.uuid4()), agent_id=agent_id, cidr=normalized,
        label=(label or "").strip() or None, is_active=True,
        created_by=created_by or None)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # The unique constraint, reached by two operators saving at once. Rolled back and
        # re-raised as the operator message so a race reads the same as a clash.
        db.rollback()
        _refuse_duplicate(db, normalized)
        raise
    db.refresh(row)
    return serialize(row)


def update(db, route_id: str, **fields) -> dict:
    row = _get(db, route_id)
    if fields.get("agent_id") is not None:
        _check_agent(db, str(fields["agent_id"]))
        row.agent_id = str(fields["agent_id"])
    if fields.get("cidr") is not None:
        normalized = normalize_cidr(str(fields["cidr"]))
        _refuse_duplicate(db, normalized, excluding=row.id)
        row.cidr = normalized
    if fields.get("label") is not None:
        row.label = str(fields["label"]).strip() or None
    if fields.get("is_active") is not None:
        row.is_active = bool(fields["is_active"])
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return serialize(row)


def delete(db, route_id: str) -> None:
    """Deleting reverts to the discovering agent. Nothing is promoted in its place —
    unlike a default hypervisor connection, an absent route has a correct meaning."""
    row = _get(db, route_id)
    db.delete(row)
    db.commit()


def delete_for_agent(db, agent_id: str) -> int:
    """Drop every route naming this agent. Called from ``agent_service.delete_agent``.

    Explicit rather than left to the FK's CASCADE, for the same reason the job null-out
    beside it is explicit: SQLite does not enforce foreign keys unless
    ``PRAGMA foreign_keys=ON`` is set per connection, and nothing sets it. A surviving
    route would resolve runs to an agent id that no longer exists.
    """
    from ..database import ConfigMgmtRoute

    return (db.query(ConfigMgmtRoute)
            .filter(ConfigMgmtRoute.agent_id == agent_id)
            .delete(synchronize_session=False))
