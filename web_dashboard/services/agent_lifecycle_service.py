"""Remote-agent lifecycle: upgrade, restart, pause and canary-first rollouts.

The trust model is in docs/platform/spiffe/remote-agents/lifecycle.md, and it is why this
module is mostly refusals. The dashboard may only CHOOSE which signed release an agent
runs; the host decides whether to take it:

1. **Opt-in on the host.** The agent's own policy.yaml carries ``upgrades:``, and the
   agent reports that on its lease (``control_upgrades``). The dashboard cannot set it.
2. **The agent checks** the request against that policy: the repository, a digest, no
   downgrade.
3. **The updater verifies** the image's cosign signature against the identity the policy
   pins, before it pulls.

Upgrade and restart run on the host, so they also require SPIRE attestation: under SPIRE
the agent's identity survives a new container (the same SPIFFE ID attests to the same
row with a fresh key), which is what lets the SUCCESSOR finish its predecessor's job. An
enrolment-code agent's identity is a file in its container; a replacement is a new
agent, and the job would strand. Pause is dashboard-only and allowed for every agent.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from sqlalchemy.orm import Session

from ..database import AgentRelease, AgentRollout, Job, RemoteAgent
from ..utils.clock import utcnow
from . import agent_service, job_service

#: Run on the agent host, through the updater. Outside AGENT_JOB_TYPES on purpose: the
#: per-agent job-type grant is about work, and these are gated here instead.
CONTROL_JOB_TYPES = ("agent_upgrade", "agent_restart")

#: The agent build that first carried the control handlers and the updater handoff.
MIN_LIFECYCLE_VERSION = (2, 10)

#: Where agents come from unless a release says otherwise. The agent refuses any other
#: repository than the one its policy.yaml names, so this is the common case, not a grant.
DEFAULT_REPOSITORY = "chrweav/dashboard-agent"

#: The keyless signing identity of this project's release workflow
#: (.github/workflows/publish-images.yml), as a policy.yaml `upgrades.verify.keyless` pins
#: it. Written into the policy of every SPIRE host the dashboard deploys.
SIGNING_IDENTITY = ("^https://github.com/Weaverlab-xyz/vm-dashboard-community/"
                    ".github/workflows/publish-images.yml@refs/tags/v")
SIGNING_ISSUER = "https://token.actions.githubusercontent.com"

RINGS = ("canary", "default")

_ACTIVE = ("queued", "running")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


class LifecycleError(Exception):
    """Refusal with an operator-readable reason."""


def version_tuple(version: str) -> tuple:
    match = re.match(r"\s*(\d+)\.(\d+)\.(\d+)", str(version or ""))
    return tuple(int(p) for p in match.groups()) if match else (0, 0, 0)


def ring(agent: RemoteAgent) -> str:
    return agent.upgrade_ring if agent.upgrade_ring in RINGS else "default"


def pending_control_job(db: Session, agent: RemoteAgent) -> Optional[Job]:
    return (db.query(Job)
            .filter(Job.agent_id == agent.id, Job.job_type.in_(CONTROL_JOB_TYPES),
                    Job.status.in_(_ACTIVE))
            .first())


def host_gate(db: Optional[Session], agent: RemoteAgent) -> Optional[str]:
    """Why this agent cannot be upgraded or restarted from here, or None if it can.

    One reason, the first that applies, worded as what the operator does next. Order
    matters: "migrate to SPIRE" before "upgrade by hand", because the second is wasted
    effort until the first is done. ``db`` None skips the one check that needs a query
    (another control job in flight), for the Agents page's per-row hint.
    """
    if not agent.is_active:
        return "This agent is revoked."
    if (agent.auth_mode or "ed25519") != "spiffe":
        return ("Migrate to SPIRE first: upgrade and restart need an agent attested "
                "through SPIRE, whose identity survives a new container.")
    if not agent_service._version_at_least(agent, MIN_LIFECYCLE_VERSION):
        return (f"Upgrade to {'.'.join(str(p) for p in MIN_LIFECYCLE_VERSION)} by hand "
                f"once: this agent reports {agent.agent_version or 'no version'}, which "
                "predates central upgrades.")
    if not agent.control_upgrades:
        return ("Enable upgrades in this agent's policy.yaml (the `upgrades:` block); "
                "the host decides, not the dashboard.")
    if not agent.control_updater:
        return ("Add the updater container beside this agent "
                "(examples/remote-agent/docker-compose.updater.yml).")
    if agent.paused:
        return "This agent is paused. Resume it first."
    if db is not None and pending_control_job(db, agent) is not None:
        return "Another upgrade or restart is already queued or running for this agent."
    return None


def _enqueue(db: Session, agent: RemoteAgent, job_type: str, meta: dict, *,
             actor: str, schedule: Optional[dict] = None) -> Job:
    reason = host_gate(db, agent)
    if reason:
        raise LifecycleError(reason)
    return job_service.create_job(db, job_type=job_type, created_by=actor,
                                  workgroup="agents", metadata=meta,
                                  agent_id=agent.id, **(schedule or {}))


def upgrade(db: Session, agent: RemoteAgent, release: AgentRelease, *, actor: str,
            schedule: Optional[dict] = None, force: bool = False,
            rollout_id: str = "") -> Job:
    """Queue an upgrade of ``agent`` to ``release``. Raises :class:`LifecycleError`."""
    current = version_tuple(agent.agent_version)
    target = version_tuple(release.version)
    if target == current:
        raise LifecycleError(f"{agent.name} already runs {release.version}.")
    if target < current and not force:
        raise LifecycleError(
            f"{release.version} is older than {agent.name} ({agent.agent_version}). A "
            "downgrade needs force, and the agent's policy.yaml must allow it too.")
    meta = {"repository": DEFAULT_REPOSITORY, "digest": release.digest,
            "version": release.version, "release_id": release.id,
            "from_version": agent.agent_version or ""}
    if rollout_id:
        meta["rollout_id"] = rollout_id
    return _enqueue(db, agent, "agent_upgrade", meta, actor=actor, schedule=schedule)


def restart(db: Session, agent: RemoteAgent, *, actor: str,
            schedule: Optional[dict] = None) -> Job:
    """Queue a restart. Under SPIRE a restart also rotates the agent's key: every start
    attests afresh with a key that lives only in memory."""
    return _enqueue(db, agent, "agent_restart", {"version": agent.agent_version or ""},
                    actor=actor, schedule=schedule)


def envelope_payload(job_type: str, meta: dict) -> dict:
    """What the agent is told for a control job: only what its policy check reads."""
    if job_type == "agent_upgrade":
        return {"repository": str(meta.get("repository") or ""),
                "digest": str(meta.get("digest") or ""),
                "version": str(meta.get("version") or "")}
    if job_type == "agent_restart":
        return {}
    raise ValueError(f"not a control job type: {job_type!r}")


def check_completion(job: Job, status: str, result: dict, error: str) -> tuple:
    """The status and error to record when an agent finishes a control job.

    The successor reports its own version; an upgrade that "completed" on any other
    version did not happen, whatever the agent says, so it is recorded as failed.
    """
    if job.job_type != "agent_upgrade" or status != "completed":
        return status, error
    target = str((job.metadata_dict or {}).get("version") or "")
    got = str((result or {}).get("version") or "")
    if got != target:
        return "failed", (f"the agent reported {got or 'no version'} after the upgrade, "
                          f"not {target}")
    return status, error


def set_paused(db: Session, agent: RemoteAgent, paused: bool) -> None:
    agent.paused = bool(paused) or None
    db.commit()


def set_ring(db: Session, agent: RemoteAgent, value: str) -> None:
    if value not in RINGS:
        raise LifecycleError(f"ring must be one of {', '.join(RINGS)}")
    agent.upgrade_ring = None if value == "default" else value
    db.commit()


# ── Rollouts ─────────────────────────────────────────────────────────────────

def _ids(raw) -> list:
    try:
        out = json.loads(raw or "[]")
    except ValueError:
        return []
    return out if isinstance(out, list) else []


def _queue_ring(db: Session, rollout: AgentRollout, release: AgentRelease, which: str,
                *, actor: str, schedule: Optional[dict]) -> list:
    skipped = json.loads(rollout.skipped or "{}")
    queued = []
    agents = (db.query(RemoteAgent).filter(RemoteAgent.is_active.is_(True))
              .order_by(RemoteAgent.name).all())
    for agent in agents:
        if ring(agent) != which:
            continue
        if version_tuple(agent.agent_version) >= version_tuple(release.version):
            skipped[agent.name] = f"already on {agent.agent_version}"
            continue
        try:
            job = upgrade(db, agent, release, actor=actor, schedule=schedule,
                          rollout_id=rollout.id)
        except LifecycleError as exc:
            # Listed with the reason, never silently left behind.
            skipped[agent.name] = str(exc)
            continue
        skipped.pop(agent.name, None)
        queued.append(job.id)
    rollout.skipped = json.dumps(skipped)
    return queued


def _preview_default(db: Session, rollout: AgentRollout, release: AgentRelease) -> None:
    """Name the default ring's ineligible agents up front, so the operator can fix them
    while the canary runs; the default pass re-checks and clears any that were fixed."""
    skipped = json.loads(rollout.skipped or "{}")
    for agent in db.query(RemoteAgent).filter(RemoteAgent.is_active.is_(True)).all():
        if ring(agent) != "default":
            continue
        if version_tuple(agent.agent_version) >= version_tuple(release.version):
            continue
        reason = host_gate(None, agent)
        if reason:
            skipped[agent.name] = reason
    rollout.skipped = json.dumps(skipped)


def start_rollout(db: Session, release: AgentRelease, *, actor: str,
                  schedule: Optional[dict] = None,
                  change_window_id: str = "") -> AgentRollout:
    """Queue ``release`` for the canary ring now (or in the window); the sweep queues
    the default ring once every canary upgrade has completed."""
    rollout = AgentRollout(release_id=release.id, change_window_id=change_window_id or None,
                           status="canary", created_by=actor, skipped="{}")
    db.add(rollout)
    db.flush()
    canary = _queue_ring(db, rollout, release, "canary", actor=actor, schedule=schedule)
    rollout.canary_job_ids = json.dumps(canary)
    if not canary:
        # No canary to wait for: straight to everyone else.
        _advance_to_default(db, rollout, release, schedule=schedule)
    else:
        _preview_default(db, rollout, release)
    db.commit()
    return rollout


def _advance_to_default(db: Session, rollout: AgentRollout, release: AgentRelease, *,
                        schedule: Optional[dict] = None) -> None:
    rollout.default_job_ids = json.dumps(
        _queue_ring(db, rollout, release, "default", actor=rollout.created_by or "rollout",
                    schedule=schedule))
    rollout.status = "default"


def _ring_state(db: Session, job_ids: list) -> tuple:
    """(all done, the first failed job or None)."""
    if not job_ids:
        return True, None
    jobs = db.query(Job).filter(Job.id.in_(job_ids)).all()
    for job in jobs:
        if job.status in ("failed", "cancelled"):
            return False, job
    return all(j.status == "completed" for j in jobs) and len(jobs) == len(job_ids), None


def _schedule_for_default(db: Session, rollout: AgentRollout) -> dict:
    """The default ring goes into the rollout's change window too, if it had one."""
    if not rollout.change_window_id:
        return {}
    from . import change_window_service
    return change_window_service.schedule_kwargs(db, change_window_id=rollout.change_window_id)


def advance_rollouts(db: Session) -> int:
    """One sweep pass: move each live rollout on, or stop it. Returns how many changed."""
    changed = 0
    for rollout in db.query(AgentRollout).filter(
            AgentRollout.status.in_(("canary", "default"))).all():
        ring_ids = _ids(rollout.canary_job_ids if rollout.status == "canary"
                        else rollout.default_job_ids)
        done, failed = _ring_state(db, ring_ids)
        if failed is not None:
            name = (db.query(RemoteAgent.name).filter(RemoteAgent.id == failed.agent_id)
                    .scalar() or failed.agent_id)
            rollout.status = "stopped"
            rollout.detail = (f"stopped: the upgrade of {name} {failed.status}"
                              f"{': ' + failed.error_message if failed.error_message else ''}")
            rollout.finished_at = utcnow()
            changed += 1
        elif done and rollout.status == "canary":
            release = db.query(AgentRelease).filter(
                AgentRelease.id == rollout.release_id).first()
            if release is None:
                rollout.status = "stopped"
                rollout.detail = "stopped: the release was deleted"
                rollout.finished_at = utcnow()
            else:
                try:
                    schedule = _schedule_for_default(db, rollout)
                except Exception as exc:  # noqa: BLE001 - e.g. the window was deleted
                    schedule = {}
                    rollout.detail = f"default ring queued now: {exc}"
                _advance_to_default(db, rollout, release, schedule=schedule)
            changed += 1
        elif done:
            rollout.status = "done"
            rollout.finished_at = utcnow()
            changed += 1
    if changed:
        db.commit()
    return changed


def rollout_dict(db: Session, rollout: AgentRollout) -> dict:
    release = db.query(AgentRelease).filter(AgentRelease.id == rollout.release_id).first()
    ids = _ids(rollout.canary_job_ids) + _ids(rollout.default_job_ids)
    jobs = {j.id: j for j in db.query(Job).filter(Job.id.in_(ids)).all()} if ids else {}
    names = dict(db.query(RemoteAgent.id, RemoteAgent.name).all())

    def rows(raw, which):
        return [{"job_id": i, "ring": which,
                 "agent": names.get(jobs[i].agent_id, "") if i in jobs else "",
                 "status": jobs[i].status if i in jobs else "missing"}
                for i in _ids(raw)]

    return {"id": rollout.id, "version": release.version if release else "",
            "status": rollout.status, "detail": rollout.detail or "",
            "jobs": rows(rollout.canary_job_ids, "canary")
            + rows(rollout.default_job_ids, "default"),
            "skipped": json.loads(rollout.skipped or "{}"),
            "created_at": rollout.created_at.isoformat() if rollout.created_at else None,
            "finished_at": rollout.finished_at.isoformat() if rollout.finished_at else None}


def validate_release(version: str, digest: str) -> tuple:
    version = (version or "").strip()
    digest = (digest or "").strip().lower()
    if not _VERSION_RE.match(version):
        raise LifecycleError(f"{version!r} is not a release version (like 2.10.1)")
    if not _DIGEST_RE.match(digest):
        raise LifecycleError("the digest must be sha256: followed by 64 hex characters")
    return version, digest
