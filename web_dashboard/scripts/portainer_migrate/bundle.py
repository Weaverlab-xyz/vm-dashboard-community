"""Build, scrub and validate the Portainer migration bundle.

The bundle is the reviewable artifact in the middle: a plain JSON file the
operator opens, reads and edits before the dashboard writes anything into a live
Portainer. Same rationale as ``config_migrate.bundle`` — a migration that is a
black box does not get trusted, and "what did NOT come across, and why" is the
half that usually goes unrecorded.

Everything here is pure except :func:`write`, so the importer running inside the
app can share :func:`scrub` and :func:`validate` without importing a CLI.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

#: Default home for bundles: outside the repo, mirroring ~/.dashboard-migrate/.
DEFAULT_DIR = Path.home() / ".portainer-migrate"

#: Collections the importer knows how to replay, in dependency order. Teams before
#: memberships, users before memberships; stacks last because they need a live
#: environment to deploy onto.
SECTIONS = ("users", "teams", "team_memberships", "registries", "stacks")

#: Recorded for reference but NEVER replayed. An endpoint in a workstation's
#: Portainer points at a local Docker socket or a LAN address; a cloud node has no
#: route to either, so importing one would only manufacture a dead environment.
#: Re-establishing these means an Edge agent (see the Edge registration flow).
REFERENCE_SECTIONS = ("endpoints", "endpoint_groups", "tags", "settings")

#: Field names dropped from every object before the bundle is written. Portainer's
#: API is not supposed to return these, but a bundle is a file an operator emails
#: to themselves — so it is scrubbed on the way out rather than trusted on the way
#: in. Comparison is case-insensitive; Portainer's casing is inconsistent.
_SECRET_FIELDS = frozenset({
    "password", "passwordhash", "apikey", "rawapikey", "token", "jwt",
    "secret", "accesstoken", "privatekey", "tlscert", "tlskey", "tlscacert",
})


def scrub(value):
    """Recursively drop credential-shaped fields from an API payload.

    Returns a new structure; the input is untouched. Applied to reference sections
    too — ``settings`` in particular carries LDAP and OAuth client secrets.
    """
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()
                if k.replace("_", "").lower() not in _SECRET_FIELDS}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def default_path(label: str = "portainer") -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    return DEFAULT_DIR / f"{label}-{stamp}.json"


def build(*, source_url: str, source_version: str, data: dict,
          reference: dict, warnings: list | None = None) -> dict:
    """Assemble the bundle document. Pure — unit-testable without a Portainer."""
    counts = {name: len(data.get(name) or []) for name in SECTIONS}
    return {
        "schema": SCHEMA_VERSION,
        "meta": {
            "source_url": source_url,
            "source_version": source_version,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "counts": counts,
        },
        # Replayable, in dependency order.
        "data": {name: scrub(data.get(name) or []) for name in SECTIONS},
        # Informational only — the importer must not read this back into Portainer.
        "reference": {name: scrub(reference.get(name)) for name in REFERENCE_SECTIONS
                      if reference.get(name) is not None},
        "warnings": list(warnings or []),
        "not_migrated": [
            "Environment (endpoint) connections - they address a local Docker socket "
            "or a LAN host that a cloud Portainer cannot route to. Recorded under "
            "'reference' so you can see what existed; re-establish them as Edge agents.",
            "User and registry passwords - Portainer's API does not return them, and "
            "this bundle is scrubbed of credential-shaped fields regardless. Imported "
            "users are created with a fresh generated password.",
            "Anything deployed ON those environments (containers, volumes, images). A "
            "Portainer backup never covered these either.",
        ],
    }


def validate(doc) -> list:
    """Structural problems with a bundle, as human-readable strings.

    Returns ``[]`` for a good document. Used by both the CLI (before writing) and
    the import job (before touching a live Portainer), so a hand-edited bundle
    fails with a sentence rather than a KeyError halfway through a replay.
    """
    problems = []
    if not isinstance(doc, dict):
        return ["the bundle is not a JSON object"]
    schema = doc.get("schema")
    if schema != SCHEMA_VERSION:
        problems.append(
            f"unsupported bundle schema {schema!r} (this build reads {SCHEMA_VERSION})")
    data = doc.get("data")
    if not isinstance(data, dict):
        problems.append("the bundle has no 'data' object")
        return problems
    for name in SECTIONS:
        section = data.get(name)
        if section is None:
            continue          # an absent section is simply nothing to import
        if not isinstance(section, list):
            problems.append(f"data.{name} must be a list, got {type(section).__name__}")
            continue
        for idx, item in enumerate(section):
            if not isinstance(item, dict):
                problems.append(f"data.{name}[{idx}] must be an object")
    if all(not (data.get(n) or []) for n in SECTIONS):
        problems.append("the bundle is empty - every importable section has no entries")
    return problems


def write(path: Path, doc: dict) -> Path:
    """Write the bundle mode 0600 AT CREATION, not chmod'ed afterwards.

    It carries no passwords by construction, but it does carry a full picture of
    who has access to what — the same treatment ``config_migrate`` gives its
    bundle.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=False)
        fh.write("\n")
    return path


def read(path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
