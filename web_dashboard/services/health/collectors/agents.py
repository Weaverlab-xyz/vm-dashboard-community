"""Remote agents: is each one online, and new enough for what is bound to it?

``agent_service.status_of`` is derived from the last poll, never stored, so this reads
the same answer the Agents page shows. The version check is about the work: an agent
that on-prem directories or hypervisor connections are bound to is only useful if it can
run that work, and an old build fails such jobs with messages that read like dashboard
bugs.
"""
from __future__ import annotations

from datetime import datetime

from .. import core


def agent_checks(agent, *, state: str, directories: int, can_directory: bool,
                 hint: str, now: datetime) -> list:
    """Checks for one agent. Pure: the caller resolves status and version answers."""
    checks = []
    if state == "online":
        checks.append(core.check("online", "Online", core.OK,
                                 f"Polled {core.ago(agent.last_seen_at, now)}."))
    elif state == "offline":
        checks.append(core.check("online", "Online", core.FAIL,
                                 f"Last seen {core.ago(agent.last_seen_at, now)}. Anything "
                                 f"bound to this agent cannot be reached."))
    else:
        checks.append(core.check("online", "Online", core.UNKNOWN,
                                 "Enrolment has not finished."))
    version = agent.agent_version or "unknown"
    if directories and not can_directory:
        checks.append(core.check("version", "Version", core.WARN,
                                 f"{directories} directory(ies) use this agent, and version "
                                 f"{version} cannot reach them. {hint}"))
    else:
        checks.append(core.check("version", "Version", core.OK, f"Version {version}."))
    return checks


async def collect(db) -> list:
    from ... import agent_service
    from ....database import ManagedDirectory, RemoteAgent
    now = datetime.utcnow()
    bound: dict = {}
    for (agent_id,) in (db.query(ManagedDirectory.agent_id)
                        .filter(ManagedDirectory.agent_id.isnot(None),
                                ManagedDirectory.status != "deleted").all()):
        bound[agent_id] = bound.get(agent_id, 0) + 1
    items = []
    for agent in db.query(RemoteAgent).filter(RemoteAgent.is_active.is_(True)).all():
        state = agent_service.status_of(agent, now=now)
        items.append(core.Item(
            object_id=agent.id, name=agent.name, kind="Remote agent",
            href="/agents",
            checks=agent_checks(agent, state=state, directories=bound.get(agent.id, 0),
                                can_directory=agent_service.supports_directory(agent),
                                hint=agent_service.directory_upgrade_hint(agent),
                                now=now)))
    return items
