"""Keep checking a remote agent after it attests.

Attestation proves, at each start, that the process holds a SPIFFE identity bound to its
row. What it cannot prove is that nothing changed afterwards. These checks run on what
the dashboard already sees -- each attestation, each signed request, each lease
self-report -- and raise an **attention** item on the agent when something moved that a
person should look at (docs/platform/spiffe/remote-agents/spire-attestation.md, "Ongoing
checks"):

* ``clone``: a request signed by the key the last attestation replaced. Two copies of the
  agent are running; the older one is still polling. The request is refused either way.
* ``policy_changed``: the agent's policy.yaml hash moved. The file is the host owner's to
  change, which is exactly why a change is worth a look.
* ``unsigned_version``: the agent reports a version (2.10 or later, the signed releases)
  that is not a release recorded on this dashboard.
* ``stale_key``: an attested agent that says it re-attests every N hours has not done so in
  twice that, while online.

Each kind is raised once until an operator acknowledges it, audited, and sent as an
``agent.attention`` notification. Nothing here refuses work by itself: the item says what
moved, and revoking stays the operator's call.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Optional

from sqlalchemy.orm import Session

from ..database import RemoteAgent
from ..utils.clock import utcnow

logger = logging.getLogger(__name__)

KINDS = {
    "clone": "Two copies are running",
    "policy_changed": "policy.yaml changed",
    "unsigned_version": "Not a recorded release",
    "stale_key": "Key not rotated",
}

#: How long the key an attestation replaced is remembered. Long enough for an old copy
#: to poll (every few seconds) and be caught; short enough that a key the agent itself
#: retired long ago is not held onto.
PREVIOUS_KEY_WINDOW = timedelta(minutes=15)

#: The first agent build that re-attests while running, and reports how often.
MIN_REATTEST_VERSION = (2, 11)

#: The first agent builds that are signed releases; older ones predate signing.
MIN_SIGNED_VERSION = (2, 10)

MAX_ITEMS = 20


def items(agent: RemoteAgent) -> list:
    try:
        out = json.loads(agent.attention or "[]")
    except ValueError:
        return []
    return out if isinstance(out, list) else []


def flag(db: Session, agent: RemoteAgent, kind: str, detail: str) -> bool:
    """Raise ``kind`` on ``agent`` unless it is already raised. Returns whether it was new."""
    current = items(agent)
    if any(i.get("kind") == kind for i in current):
        return False
    now = utcnow()
    current.append({"kind": kind, "title": KINDS.get(kind, kind), "detail": detail,
                    "at": now.isoformat()})
    agent.attention = json.dumps(current[-MAX_ITEMS:])
    db.commit()
    logger.warning("agent %s needs attention (%s): %s", agent.name, kind, detail)
    from . import agent_service
    agent_service.audit(db, agent, "agent.attention", details={"kind": kind, "detail": detail})
    try:
        from . import notification_service, notify_policy
        notification_service.emit_safe(db, notify_policy.NotificationEvent(
            event_type="agent.attention",
            title=f"Agent {agent.name}: {KINDS.get(kind, kind)}",
            body=detail,
            resource_kind="agent",
            resource_id=f"agent:{agent.id}",
            resource_name=agent.name,
            url="/agents",
            dedupe_bucket=f"agent.attention:{agent.id}:{kind}:{now.date().isoformat()}",
            fields={"Agent": agent.name, "Check": KINDS.get(kind, kind)},
        ))
    except Exception:  # noqa: BLE001 - the item is recorded; delivery is best-effort
        logger.warning("could not notify agent attention", exc_info=True)
    return True


def acknowledge(db: Session, agent: RemoteAgent) -> list:
    """Clear every item. Returns what was cleared, for the audit."""
    cleared = items(agent)
    agent.attention = None
    db.commit()
    return cleared


# ── the checks ────────────────────────────────────────────────────────────────

def note_attest(db: Session, agent: RemoteAgent, *, new_key: str, policy_hash: str,
                ip: str) -> None:
    """Call BEFORE the attestation overwrites the row: remembers the key it replaces and
    compares the policy hash against the one on record. Does not commit."""
    now = utcnow()
    old = agent.public_key or ""
    if old and old != new_key:
        agent.previous_public_key = old
        agent.previous_key_until = now + PREVIOUS_KEY_WINDOW
    agent.last_attest_at = now
    agent.last_attest_ip = (ip or "")[:45] or None
    _check_policy(db, agent, policy_hash, commit=False)


def caught_previous_key(db: Session, agent: RemoteAgent, verify) -> bool:
    """For a request whose signature did NOT verify with the current key: whether it
    verifies with the key the last attestation replaced, which means a second copy of the
    agent is still running on it. ``verify(public_key) -> bool`` checks the request."""
    old, until = agent.previous_public_key, agent.previous_key_until
    if not old or not until or utcnow() > until:
        return False
    if not verify(old):
        return False
    flag(db, agent, "clone",
         "A request was signed with the key this agent's last attestation replaced, so "
         "another copy attested as this agent and the older one is still running. It was "
         "refused. If you did not start a second copy, revoke the agent and find the "
         f"other one (it last attested from {agent.last_attest_ip or 'an unknown address'}).")
    return True


def _check_policy(db: Session, agent: RemoteAgent, reported: str, *, commit: bool) -> None:
    reported = (reported or "").strip()[:64]
    if not reported:
        return
    before = agent.policy_hash or ""
    agent.policy_hash = reported
    if commit:
        db.commit()
    if before and before != reported:
        flag(db, agent, "policy_changed",
             f"The agent's policy.yaml hash changed from {before[:12]}… to {reported[:12]}…. "
             "The file is the host owner's to edit; if nobody meant to, check the host.")


def check_policy(db: Session, agent: RemoteAgent, reported: str) -> None:
    """A policy hash from a lease self-report (agent 2.11+)."""
    if (reported or "").strip()[:64] != (agent.policy_hash or ""):
        _check_policy(db, agent, reported, commit=True)


def check_version(db: Session, agent: RemoteAgent) -> None:
    """Flag a signed-era version that is not a recorded release (when any are recorded)."""
    from . import agent_release_service, agent_service
    if not agent_service._version_at_least(agent, MIN_SIGNED_VERSION):
        return
    releases = agent_release_service.list_releases(db)
    if not releases or any(r.version == agent.agent_version for r in releases):
        return
    flag(db, agent, "unsigned_version",
         f"It reports {agent.agent_version}, which is not a release recorded under "
         "Lifecycle. A build from a release is signed; this one may be a local build, or "
         "a release nobody recorded yet.")


def sweep(db: Session, now=None) -> int:
    """Flag attested agents that stopped rotating their key. Returns how many were new."""
    from . import agent_service
    now = now or utcnow()
    online_since = now - timedelta(seconds=agent_service.ONLINE_WINDOW_SECONDS)
    new = 0
    for agent in (db.query(RemoteAgent)
                  .filter(RemoteAgent.is_active.is_(True), RemoteAgent.auth_mode == "spiffe",
                          RemoteAgent.reported_reattest_hours > 0,
                          RemoteAgent.last_seen_at >= online_since).all()):
        if not agent_service._version_at_least(agent, MIN_REATTEST_VERSION):
            continue
        hours = int(agent.reported_reattest_hours)
        last = agent.last_attest_at
        if last is None or now - last > timedelta(hours=2 * hours):
            if flag(db, agent, "stale_key",
                    f"It re-attests every {hours} h but last did "
                    f"{'never' if last is None else last.isoformat(timespec='minutes') + ' UTC'}"
                    ". Its key is older than it should be; restart it, and check its log for "
                    "attestation errors."):
                new += 1
    return new


def has_work(db: Session) -> bool:
    return db.query(RemoteAgent.id).filter(
        RemoteAgent.is_active.is_(True), RemoteAgent.auth_mode == "spiffe",
        RemoteAgent.reported_reattest_hours > 0).first() is not None


def item_dicts(agent: RemoteAgent) -> list:
    return [{"kind": i.get("kind", ""), "title": i.get("title", ""),
             "detail": i.get("detail", ""), "at": i.get("at", "")} for i in items(agent)]


def first(agent: RemoteAgent) -> Optional[dict]:
    got = items(agent)
    return got[0] if got else None
