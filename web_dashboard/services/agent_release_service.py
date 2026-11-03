"""The agent releases the dashboard may offer as upgrade targets.

A release is a version and the image digest it is. Added by hand (an air-gapped site
mirrors the image and records the mirror's digest), or read from Docker Hub with
:func:`check_registry`. Neither path verifies a signature, and neither needs to: the
updater on each agent host verifies the cosign signature against the identity its own
policy.yaml pins before it pulls, so a wrong row here offers an upgrade every host
refuses (docs/platform/spiffe/remote-agents/lifecycle.md).
"""
from __future__ import annotations

from typing import Optional

import requests
from sqlalchemy.orm import Session

from ..database import AgentRelease
from .agent_lifecycle_service import (DEFAULT_REPOSITORY, LifecycleError,
                                      validate_release, validate_repository, version_tuple)

#: The label the agent image carries with its own version (runners/agent/Dockerfile).
#: The image TAG is the dashboard release, not the agent version, so the tag cannot say.
VERSION_LABEL = "dashboard-agent.version"

_AUTH = "https://auth.docker.io/token"
_REGISTRY = "https://registry-1.docker.io/v2"
_ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))
_TIMEOUT = 15


def list_releases(db: Session) -> list:
    rows = db.query(AgentRelease).all()
    return sorted(rows, key=lambda r: version_tuple(r.version), reverse=True)


def newest(db: Session, repository: str = "") -> Optional[AgentRelease]:
    rows = [r for r in list_releases(db)
            if not repository or (r.repository or DEFAULT_REPOSITORY) == repository]
    return rows[0] if rows else None


#: How many releases behind the newest this dashboard offers: 0 = n, 1 = n-1, 2 = n-2.
#: One dashboard-wide value, and an optional one per workstream (a JSON object) that an
#: agent follows when it has a workstream.
LAG_CONFIG = "agent_release_lag"
WORKSTREAM_LAG_CONFIG = "agent_release_lag_by_workstream"
MAX_LAG = 2


def _clamp(value) -> int:
    try:
        return min(max(int(value), 0), MAX_LAG)
    except (TypeError, ValueError):
        return 0


def get_lag() -> int:
    from . import config_service
    return _clamp(config_service.get(LAG_CONFIG) or 0)


def get_workstream_lags() -> dict:
    import json
    from . import config_service
    try:
        raw = json.loads(config_service.get(WORKSTREAM_LAG_CONFIG) or "{}")
    except ValueError:
        raw = {}
    return {str(k): _clamp(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def lag_for(workstream: Optional[str] = None) -> int:
    """The track for a workstream, else the dashboard-wide one."""
    own = get_workstream_lags().get((workstream or "").strip().lower())
    return get_lag() if own is None else own


def set_lag(lag: int, workstream: str = "") -> int:
    """Set the dashboard-wide track, or one workstream's. ``lag`` -1 clears a workstream's
    (it follows the dashboard-wide track again)."""
    import json
    from . import config_service
    workstream = (workstream or "").strip().lower()
    if workstream:
        lags = get_workstream_lags()
        if lag == -1:
            lags.pop(workstream, None)
        elif lag in range(MAX_LAG + 1):
            lags[workstream] = lag
        else:
            raise LifecycleError("lag must be 0 (n), 1 (n-1) or 2 (n-2), or -1 to follow the default")
        config_service.set(WORKSTREAM_LAG_CONFIG, json.dumps(lags))
        return lag
    if lag not in range(MAX_LAG + 1):
        raise LifecycleError("lag must be 0 (n), 1 (n-1) or 2 (n-2)")
    config_service.set(LAG_CONFIG, str(lag))
    return lag


def target(db: Session, repository: str = "", agent=None) -> Optional[AgentRelease]:
    """The release to offer: the track's lag behind the newest (the agent's workstream's
    track when it has one), or the oldest there is when fewer are recorded -- never
    nothing, while something is."""
    rows = [r for r in list_releases(db)
            if not repository or (r.repository or DEFAULT_REPOSITORY) == repository]
    lag = lag_for(getattr(agent, "workstream", None)) if agent is not None else get_lag()
    return rows[min(lag, len(rows) - 1)] if rows else None


def get(db: Session, release_id: str) -> Optional[AgentRelease]:
    return db.query(AgentRelease).filter(AgentRelease.id == release_id).first()


def add(db: Session, *, version: str, digest: str, notes: str = "", actor: str,
        source: str = "manual", repository: str = "") -> AgentRelease:
    version, digest = validate_release(version, digest)
    repository = validate_repository(repository)
    existing = db.query(AgentRelease).filter(AgentRelease.version == version).first()
    if existing is not None:
        if existing.digest == digest and existing.repository == repository:
            return existing
        if source == "registry" and existing.source == "registry":
            # Docker Hub is the authority for a row it wrote: a rebuilt tag moves the
            # digest, and refusing made "Check Docker Hub" fail from then on.
            existing.digest = digest
            existing.repository = repository
            db.commit()
            return existing
        raise LifecycleError(f"{version} is already recorded with digest {existing.digest}. "
                             "Delete it first if that one is wrong.")
    row = AgentRelease(version=version, repository=repository, digest=digest,
                       notes=(notes or "")[:2000] or None,
                       source=source, created_by=actor)
    db.add(row)
    db.commit()
    return row


def delete(db: Session, release: AgentRelease) -> int:
    """Remove a release and the finished rollouts that point at it; returns how many.

    ``agent_rollouts.release_id`` is a foreign key, so deleting a release that was ever
    rolled out raised an IntegrityError (a 500) and the row could never be removed. A
    rollout still in flight refuses instead: it is reading this row.
    """
    from ..database import AgentRollout
    rollouts = db.query(AgentRollout).filter(AgentRollout.release_id == release.id).all()
    if any(r.status in ("canary", "default") for r in rollouts):
        raise LifecycleError(f"{release.version} has a rollout in progress. Wait for it to "
                             "finish (or stop), then remove it.")
    for r in rollouts:
        db.delete(r)
    db.delete(release)
    db.commit()
    return len(rollouts)


def release_dict(row: AgentRelease) -> dict:
    return {"id": row.id, "version": row.version, "digest": row.digest,
            "repository": row.repository or DEFAULT_REPOSITORY,
            "notes": row.notes or "", "source": row.source,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "created_by": row.created_by or ""}


def _get(session, url: str, token: str, accept: str = _ACCEPT, method: str = "GET"):
    resp = session.request(method, url, timeout=_TIMEOUT,
                           headers={"Authorization": f"Bearer {token}", "Accept": accept})
    resp.raise_for_status()
    return resp


def read_registry(repository: str = DEFAULT_REPOSITORY, tag: str = "latest",
                  session=None) -> dict:
    """``{version, digest}`` for ``repository:tag`` on Docker Hub, read anonymously.

    The digest is the manifest (index) digest -- what the updater pulls and verifies.
    The version is the image's ``dashboard-agent.version`` label, read from the config
    blob of its first platform manifest (every platform is built from one source).
    """
    session = session or requests.Session()
    token = session.get(_AUTH, timeout=_TIMEOUT, params={
        "service": "registry.docker.io",
        "scope": f"repository:{repository}:pull"}).json().get("token", "")
    top = _get(session, f"{_REGISTRY}/{repository}/manifests/{tag}", token)
    digest = top.headers.get("Docker-Content-Digest", "")
    doc = top.json()
    manifest = doc
    if doc.get("manifests"):
        # An index: the first real platform (attestation manifests say "unknown").
        plat = next((m for m in doc["manifests"]
                     if (m.get("platform") or {}).get("os") not in (None, "unknown")),
                    doc["manifests"][0])
        manifest = _get(session, f"{_REGISTRY}/{repository}/manifests/{plat['digest']}",
                        token).json()
    config_digest = (manifest.get("config") or {}).get("digest", "")
    config = _get(session, f"{_REGISTRY}/{repository}/blobs/{config_digest}", token,
                  accept="*/*").json()
    labels = (config.get("config") or {}).get("Labels") or {}
    return {"version": str(labels.get(VERSION_LABEL) or ""), "digest": digest}


def check_registry(db: Session, *, actor: str, tag: str = "latest",
                   session=None) -> AgentRelease:
    """Record the release Docker Hub serves for ``tag``. Raises LifecycleError."""
    try:
        found = read_registry(DEFAULT_REPOSITORY, tag, session=session)
    except (requests.RequestException, ValueError, KeyError, StopIteration) as exc:
        raise LifecycleError(
            f"could not read {DEFAULT_REPOSITORY}:{tag} from Docker Hub ({exc}). Where this "
            "dashboard has no route to Docker Hub, add the release by hand.") from exc
    if not found["version"] or found["version"] == "dev":
        raise LifecycleError(f"{DEFAULT_REPOSITORY}:{tag} carries no `{VERSION_LABEL}` "
                             "label; it predates central upgrades.")
    return add(db, version=found["version"], digest=found["digest"], actor=actor,
               source="registry", notes=f"Docker Hub {DEFAULT_REPOSITORY}:{tag}")


_HUB_TAGS = "https://hub.docker.com/v2/repositories/{repo}/tags"
_MAX_TAGS = 20


def list_hub_tags(repository: str = DEFAULT_REPOSITORY, session=None) -> list:
    """Names of the most recently pushed tags on Docker Hub, newest first."""
    session = session or requests.Session()
    resp = session.get(_HUB_TAGS.format(repo=repository), timeout=_TIMEOUT,
                       params={"page_size": _MAX_TAGS, "ordering": "last_updated"})
    resp.raise_for_status()
    return [t["name"] for t in resp.json().get("results", []) if t.get("name")]


def sync_registry(db: Session, *, actor: str, session=None) -> dict:
    """Record every release Docker Hub publishes: ``{added, updated, unchanged, skipped}``.

    Reads the recent tags, then each one's ``dashboard-agent.version`` label, so a
    community user never types a version or a digest. Tags with no label (they predate
    central upgrades) or an unreadable manifest are skipped, not fatal. Raises
    LifecycleError only when Docker Hub cannot be reached at all.
    """
    session = session or requests.Session()
    try:
        tags = list_hub_tags(DEFAULT_REPOSITORY, session=session)
    except (requests.RequestException, ValueError, KeyError) as exc:
        raise LifecycleError(
            f"could not list {DEFAULT_REPOSITORY} on Docker Hub ({exc}). This dashboard needs "
            "outbound HTTPS to hub.docker.com and registry-1.docker.io.") from exc
    if "latest" not in tags:
        tags.append("latest")
    out = {"added": [], "updated": [], "unchanged": [], "skipped": []}
    seen = set()
    for tag in tags:
        try:
            found = read_registry(DEFAULT_REPOSITORY, tag, session=session)
        except (requests.RequestException, ValueError, KeyError, StopIteration):
            out["skipped"].append(tag)
            continue
        version = found["version"]
        if not version or version == "dev" or version in seen:
            if not version or version == "dev":
                out["skipped"].append(tag)
            continue
        seen.add(version)
        before = db.query(AgentRelease).filter(AgentRelease.version == version).first()
        prior = before.digest if before is not None else None
        try:
            add(db, version=version, digest=found["digest"], actor=actor, source="registry",
                notes=f"Docker Hub {DEFAULT_REPOSITORY}:{tag}")
        except LifecycleError:
            out["skipped"].append(tag)     # a hand-recorded row with another digest
            continue
        out["added" if prior is None else
            "unchanged" if prior == found["digest"] else "updated"].append(version)
    return out
