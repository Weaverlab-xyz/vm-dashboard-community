"""Remote agents: is each one online, and new enough for what is bound to it?

``agent_service.status_of`` is derived from the last poll, never stored, so this reads
the same answer the Agents page shows. The version check is about the work: an agent
that on-prem directories or hypervisor connections are bound to is only useful if it can
run that work, and an old build fails such jobs with messages that read like dashboard
bugs.

The identity check is a recommendation, never a failure: an agent that enrolled with a
code keeps a long-lived Ed25519 signing key in ``identity.json`` (``auth_mode`` NULL or
"ed25519"), one attested by SPIRE holds its key only in memory ("spiffe"). Both are
supported, so both are OK; :func:`identity_note` adds the fleet count above the list.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .. import core
from ....utils.clock import utcnow

# The anchor is the "### Remote agents" heading. Linked only once the page is in this
# build's docs/, so the note never points at a 404.
DOCS_HREF = "/docs/platform/workload-identity#remote-agents"
_DOCS_PAGE = Path(__file__).resolve().parents[4] / "docs" / "access" / "workload-identity.md"


def identity_mode(agent) -> str:
    """"spiffe" for a SPIRE-attested agent, else "ed25519" (NULL is the enrolment default)."""
    return "spiffe" if (getattr(agent, "auth_mode", None) or "").lower() == "spiffe" else "ed25519"


def identity_check(agent) -> dict:
    mode = identity_mode(agent)
    if mode == "spiffe":
        detail = "SPIRE-attested: its signing key lives only in the agent's memory."
    else:
        detail = ("Long-lived signing key on disk (identity.json). SPIRE attestation is "
                  "recommended; see Migrate to SPIRE on the Agents page.")
    # Always OK: a key on disk is supported, so it must not turn the page amber or red.
    # ``mode`` rides along in the stored check so identity_note can count it.
    return dict(core.check("identity", "Identity", core.OK, detail), mode=mode)


def identity_note(items: list) -> dict | None:
    """The fleet line: how many agents still keep a signing key on disk.

    ``items`` are the stored rows as the snapshot serialises them. Revoked agents are
    never collected, so they are not counted.
    """
    modes = [c.get("mode") for it in items for c in it.get("checks") or []
             if c.get("key") == "identity"]
    if not modes:
        return None
    on_disk, attested = modes.count("ed25519"), modes.count("spiffe")
    if on_disk:
        text = (f"{on_disk} of {len(modes)} agent(s) still keep a long-lived signing key "
                f"on disk; {attested} are SPIRE-attested. Attestation is recommended, not "
                f"required, so this does not change any status.")
    else:
        text = f"All {attested} agent(s) are SPIRE-attested: no signing key on disk."
    note = {"text": text, "href": "", "link": ""}
    if _DOCS_PAGE.is_file():
        note.update(href=DOCS_HREF, link="Workload identity for remote agents")
    return note


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
    checks.append(identity_check(agent))
    return checks


async def collect(db) -> list:
    from ... import agent_service
    from ....database import ManagedDirectory, RemoteAgent
    now = utcnow()
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
