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
        raise LifecycleError(f"{version} is already recorded with digest {existing.digest}. "
                             "Delete it first if that one is wrong.")
    row = AgentRelease(version=version, repository=repository, digest=digest,
                       notes=(notes or "")[:2000] or None,
                       source=source, created_by=actor)
    db.add(row)
    db.commit()
    return row


def delete(db: Session, release: AgentRelease) -> None:
    db.delete(release)
    db.commit()


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
