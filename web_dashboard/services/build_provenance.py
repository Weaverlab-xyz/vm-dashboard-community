"""Where the code in a deployed cloud function came from.

Cloud Functions run privileged code — a database admin credential, or rights to
write Azure role assignments — from inside your network. Which makes one question
worth being able to answer precisely, months later: **what exactly is running in
that function, and where did it come from?**

Workloads are repository files shipped inside the dashboard image (there is
deliberately no upload path), so the answer is knowable. This module captures it at
deploy time and records it on the function's row, so the answer survives independently
of whether anyone still has the image that produced it.

Three layers, most-reliable first:

``source_tree_sha256``  a hash of the handler source that produced the package.
                        ALWAYS available — computed from the files on disk — and it
                        cannot be wrong. Two functions with the same value are
                        running the same handler code, full stop.
``package_sha256``      the exact deployed artifact (already on the row). Narrower:
                        it covers one workload plus its vendored dependencies.
``source_commit`` etc.  git provenance. BEST EFFORT: an image built without the
                        build arg, or from a tarball, simply has none — which is
                        recorded honestly rather than guessed at.

The distinction matters. A git commit tells you *who wrote and reviewed* the code; a
tree hash tells you *what is actually running*. They answer different questions and
the second one is the one you can always get.

Stdlib only, and never raises: provenance is metadata, and failing to collect it must
never fail a deploy.
"""
from __future__ import annotations

import logging
import os
import subprocess

logger = logging.getLogger(__name__)

# Set at image build (see the Dockerfile's ARG/ENV block). Absent in a source
# checkout, where the git fallback below fills them in instead.
_ENV_COMMIT = "DASHBOARD_GIT_SHA"
_ENV_REF = "DASHBOARD_GIT_REF"
_ENV_ORIGIN = "DASHBOARD_GIT_ORIGIN"
_ENV_BUILT_AT = "DASHBOARD_BUILT_AT"

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _env(name: str) -> str:
    return (os.environ.get(name, "") or "").strip()


def _git(*args: str) -> str:
    """A git command in the repo, or ``""``.

    Only reached in a source checkout — a built image has the env vars and no git
    directory. Short timeout and a swallowed failure, because this is metadata.
    """
    try:
        out = subprocess.run(("git", *args), cwd=_REPO_ROOT, capture_output=True,
                             text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _git_provenance() -> dict:
    commit = _git("rev-parse", "HEAD")
    if not commit:
        return {}
    # A dirty tree means the running code does NOT match the commit, which is the
    # single most misleading thing provenance can omit.
    dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))
    return {
        "commit": commit,
        "ref": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "origin": _git("config", "--get", "remote.origin.url"),
        "dirty": dirty,
        "source": "git",
    }


def _image_provenance() -> dict:
    commit = _env(_ENV_COMMIT)
    if not commit:
        return {}
    return {
        "commit": commit,
        "ref": _env(_ENV_REF),
        "origin": _env(_ENV_ORIGIN),
        "built_at": _env(_ENV_BUILT_AT),
        # An image built from a dirty tree should have said so at build time; there
        # is no way to detect it here, so it is reported as unknown rather than
        # asserted false.
        "dirty": None,
        "source": "image",
    }


def collect(source_root: str = "") -> dict:
    """Everything known about where the handler source came from.

    Never raises. Missing git metadata is recorded as absent, not invented.
    """
    provenance: dict = {"commit": "", "ref": "", "origin": "", "dirty": None,
                        "source": "unknown", "tree_sha256": ""}
    try:
        from . import cloud_function_package
        provenance["tree_sha256"] = cloud_function_package.source_tree_sha256(source_root)
    except Exception as exc:
        logger.debug("provenance: could not hash the handler source tree: %s", exc)

    # The image's own build metadata wins over a git call: in a container the git
    # directory is absent, and where both exist the build arg is the authoritative
    # statement of what was shipped.
    #
    # Wrapped for the same reason the hash above is. "Never raises" is the whole
    # contract — this runs on the deploy path, and metadata must not be able to fail
    # a deploy. _git_provenance already swallows subprocess errors internally, but
    # relying on that would make the guarantee depend on a detail of a helper.
    try:
        provenance.update(_image_provenance() or _git_provenance())
    except Exception as exc:
        logger.debug("provenance: could not resolve build metadata: %s", exc)
    return provenance


def describe(provenance: dict) -> str:
    """A one-line human summary, for a log line or the UI."""
    if not provenance:
        return "unknown"
    commit = str(provenance.get("commit") or "")
    tree = str(provenance.get("tree_sha256") or "")
    parts = []
    if commit:
        parts.append(commit[:12] + ("-dirty" if provenance.get("dirty") else ""))
        ref = str(provenance.get("ref") or "")
        if ref and ref != "HEAD":
            parts.append(f"({ref})")
    if tree:
        parts.append(f"tree:{tree[:12]}")
    return " ".join(parts) or "unknown"


def env_for_function(provenance: dict) -> dict:
    """Provenance passed into the function's own environment.

    So a RUNNING function can report what it is, rather than you having to trust the
    dashboard row that claims to describe it. `echo_diag` echoes these back, which
    makes "is this function running the code I think it is?" a single request.
    """
    if not provenance:
        return {}
    env = {}
    if provenance.get("commit"):
        env["FN_SOURCE_COMMIT"] = str(provenance["commit"])[:40]
    if provenance.get("tree_sha256"):
        env["FN_SOURCE_TREE"] = str(provenance["tree_sha256"])[:64]
    return env
