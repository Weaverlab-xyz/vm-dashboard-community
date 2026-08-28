"""Job metadata for an ``agent_storage`` run — file I/O against a share the dashboard
cannot reach, performed by an agent that can.

The storage abstraction in ``services/storage_service.py`` already speaks to a local
filesystem or an SMB UNC path, but it does so from inside the dashboard container. That is
why the ``local`` backend is pinned to the local Ansible runner: a container in Azure has
no route to a corporate file server. This job type moves the same four operations —
``list``, ``fetch``, ``upload``, ``delete`` — onto the agent, which is already inside the
network, and removes the constraint entirely.

WHERE THE PATH IS NOT
---------------------
There is no path in this allowlist, and that is the load-bearing part of the design. The
agent holds its own ``shares.yaml`` mapping a NAME to a path and, for UNC, a credential;
its ``policy.yaml`` grants share names and read/write separately. A job says *which named
share* and *which file inside it*. It cannot say ``\\\\some-other-server\\c$``, and a
dashboard that has been taken over cannot either. The string in ``share`` is one half of a
join the operator wrote down, exactly like ``agent_connection_name`` on a hypervisor
connection.

Three free-form strings do cross, unlike the gateway and discovery allowlists which have
none: ``share``, ``subpath`` and ``name``. They are unavoidable — a file operation needs to
name a file — so they are constrained here to a shape that cannot escape a directory, and
the agent independently refuses anything that resolves outside the share root rather than
trusting this validation. Two checks that must both fail before a traversal works.

``content_b64`` IS PERSISTED
----------------------------
An upload's bytes have to be in the job row, because ``api/agent._envelope_payload`` builds
the signed envelope from the stored metadata at lease time. ``agent_storage_service.run_op``
therefore scrubs the key once the job is terminal — the bytes were already delivered and
keeping them turns the ``jobs`` table into a second, unmanaged copy of the share. The
256 KB envelope cap bounds what a crash can leave behind.

Pure and stdlib-only, so every round-trip is testable without FastAPI or a database.
"""
import copy
import re

# Everything an agent_storage run needs, and nothing else.
STORAGE_META_KEYS = (
    "op",           # one of VALID_OPS
    "share",        # `name:` of an entry in the agent's shares.yaml
    "subpath",      # optional relative directory within that share ("" = its root)
    "name",         # bare filename; "" for `list`
    "content_b64",  # base64 payload; "" for everything but `upload`
)

# What the agent may be asked to do. The four operations `_BACKEND_OPS` in
# storage_service.py already dispatches for every other backend, and no more. There is
# deliberately no `move`, no `mkdir` and no `exec` — a move is a fetch plus an upload plus
# a delete, each of which the operator has already granted individually.
VALID_OPS = ("list", "fetch", "upload", "delete")

# Operations that write. Split out because policy.yaml grants read and write separately,
# and the dashboard should refuse a write against a read-only share before it costs an
# operator a round trip to be told no by the agent.
WRITE_OPS = ("upload", "delete")

# The subset that crosses to the agent in the signed job envelope. Identical to
# STORAGE_META_KEYS today — there is no `description` here, because unlike a discovery scan
# this job is never something an operator types a label for; it is machinery behind a page.
ENVELOPE_KEYS = STORAGE_META_KEYS

_DEFAULTS = {
    "op": "list",
    "share": "",
    "subpath": "",
    "name": "",
    "content_b64": "",
}

# A share name is an identifier the operator chose in their own file. Letters, digits and
# the three separators people actually use — no dots, so `..` is not expressible at all.
_SHARE_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# A filename, and only a filename. No directory separators of either flavour, no drive
# letter, and a leading dot is refused so `..` and dotfiles both fall out of one rule.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")

# A relative subdirectory. Same character class as a name, one or more segments joined by
# forward slashes; the agent normalises the separator for its own OS. `..` cannot match
# because a segment may not begin with a dot.
_SUBPATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
                         r"(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,127})*$")


def _clean_subpath(value) -> str:
    """Normalise a subpath's separators without changing what it means.

    A TRAILING separator is noise and is stripped. A LEADING one is not: it is the
    difference between "the win/ directory inside the share" and "the root of this host",
    and quietly turning the second into the first would accept an absolute path while
    telling the operator absolute paths are refused. It is left in place so the pattern
    below rejects it and :func:`check` can say so.
    """
    return str(value or "").strip().replace("\\", "/").rstrip("/")


def storage_meta(op: str, *, share: str, subpath: str = "", name: str = "",
                 content_b64: str = "") -> dict:
    """The job row's metadata for an ``agent_storage`` run."""
    return normalize({
        "op": op,
        "share": share,
        "subpath": subpath,
        "name": name,
        "content_b64": content_b64,
    })


def envelope_payload(meta: dict) -> dict:
    """The subset that crosses to the agent in the signed job envelope."""
    clean = normalize(meta)
    return {key: clean[key] for key in ENVELOPE_KEYS}


def storage_kwargs(meta: dict) -> dict:
    """Keyword arguments for the agent's ``run_storage`` handler."""
    clean = normalize(meta)
    return {key: clean[key] for key in ENVELOPE_KEYS}


def normalize(meta: dict) -> dict:
    """Coerce anything into the closed shape, falling back rather than raising.

    Forgiving for the same reason ``agent_gateway_meta.normalize`` is: this runs on a job
    row that an older build may have written, and refusing to normalise one would strand a
    job nobody can now cancel. The fallbacks all point at the harmless end — an
    unrecognised op becomes ``list``, and a name that does not match becomes ``""``, which
    every write path then refuses in :func:`check` rather than acting on a half-parsed one.
    """
    src = meta if isinstance(meta, dict) else {}
    out = copy.deepcopy(_DEFAULTS)

    op = str(src.get("op") or "").strip().lower()
    if op in VALID_OPS:
        out["op"] = op

    share = str(src.get("share") or "").strip()
    if _SHARE_RE.match(share):
        out["share"] = share

    subpath = _clean_subpath(src.get("subpath"))
    if subpath and _SUBPATH_RE.match(subpath):
        out["subpath"] = subpath

    name = str(src.get("name") or "").strip()
    if _NAME_RE.match(name):
        out["name"] = name

    content = src.get("content_b64")
    if isinstance(content, str):
        out["content_b64"] = content

    return out


def check(meta: dict) -> str:
    """Why this metadata could not be run, or "" if it can.

    Separate from :func:`normalize` on purpose: normalize is forgiving so an old row stays
    actionable, while this is what the queueing path calls to refuse a request nobody has
    made yet. It reads the RAW values rather than the normalised ones, so a name that
    failed to parse is reported as the bad name the caller passed instead of as a missing
    one — a traversal attempt should say so, not read as an empty field.
    """
    src = meta if isinstance(meta, dict) else {}

    op = str(src.get("op") or "").strip().lower()
    if op not in VALID_OPS:
        return (f"{op!r} is not a storage operation this build knows "
                f"(known: {', '.join(VALID_OPS)})")

    share = str(src.get("share") or "").strip()
    if not share:
        return "No share name. Set one on /storage — it must match a `name:` in the " \
               "agent's shares.yaml."
    if not _SHARE_RE.match(share):
        return (f"Share name {share!r} is not a valid identifier — letters, digits, "
                f"underscore and hyphen only.")

    subpath = _clean_subpath(src.get("subpath"))
    if subpath and not _SUBPATH_RE.match(subpath):
        return (f"Subpath {subpath!r} is not a relative directory inside the share. "
                f"No absolute paths, no drive letters, and no segment may start with a dot.")

    name = str(src.get("name") or "").strip()
    if op == "list":
        return ""
    if not name:
        return f"A {op} needs a file name."
    if not _NAME_RE.match(name):
        return (f"{name!r} is not a plain file name. A storage job names a file inside "
                f"the share, never a path — no '/', no '\\\\', and no leading dot.")

    if op == "upload" and not isinstance(src.get("content_b64"), str):
        return "An upload needs base64 content."
    return ""
