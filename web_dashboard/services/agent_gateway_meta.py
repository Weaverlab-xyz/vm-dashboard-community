"""Job metadata for an ``agent_gateway`` run — a BeyondTrust Gateway started inside a
network the dashboard cannot reach.

Slice 5 of the POV feature. A POV's VMs are reachable only from inside the customer's
environment, so every PRA jump item a later slice creates needs a Gateway *in there*. The
broker VM from slice 3 already runs an agent that dials out; this is the job that makes it
start the Gateway container next to itself.

The allowlist here is the same funnel ``agent_job_meta.DISCOVER_META_KEYS`` is, and for
the same reason: this crosses into somebody else's network, so **every field is a scalar
or an enum and there is no free-form string anywhere.** No image name, no container name,
no command, no URL, no path. A field that could carry executable content cannot be added
by accident, only by editing this tuple and the test that pins it.

Two things a reader will look for and not find:

**The image is not here.** It comes from the agent's own ``policy.yaml``
(``gateway.image``), exactly as the Config-Management and hypervisor sibling images do. A
job says only *what kind of thing to do*; a compromised dashboard cannot choose what runs
on the host. See ``examples/remote-agent/policy.example.yaml``.

**The deploy key is not here either.** Job metadata lands in the database and the envelope
crosses the wire, and a PRA deploy key is neither single-use nor short-lived — every
Gateway node registered with it joins the same Gateway. It rides the sealed per-job
channel instead (``POST /api/agent/jobs/{id}/gateway-key``), fetched once the job is
``running`` and bound to a key that exists only for that fetch.

Pure and stdlib-only, so every round-trip is testable without FastAPI or a database.
"""
import copy

# Everything an agent_gateway run needs, and nothing else.
GATEWAY_META_KEYS = (
    "gateway_action",   # one of VALID_ACTIONS
    "timeout_s",        # int — how long the agent waits for the container to settle
)

# What the agent may be asked to do. An enum, and deliberately a small one: "start the
# Gateway" and "take it away again" are the whole vocabulary. `remove` exists because a
# POV teardown has to reach the container, and reaching it through some general-purpose
# verb would be a much larger grant than this feature needs.
VALID_ACTIONS = ("install", "remove")

_DEFAULTS = {
    "gateway_action": "install",
    # A container start plus a short settle. Not a provisioning timeout: the agent is
    # only waiting for docker to report the thing running, and PRA-side registration is
    # confirmed by the DASHBOARD against the tenant's API, not by the agent.
    "timeout_s": 120,
}

_TIMEOUT_MIN = 30
_TIMEOUT_MAX = 600


def gateway_meta(payload, *, description: str) -> dict:
    """The job row's metadata for an ``agent_gateway`` run.

    ``description`` is stored for the Jobs page and is NOT part of the envelope — the
    same split ``agent_job_meta.discover_meta`` makes. An operator-typed string is
    exactly what must not cross to the agent.
    """
    meta = normalize(payload)
    meta["description"] = str(description or "")[:200]
    return meta


def envelope_payload(meta: dict) -> dict:
    """The subset that crosses to the agent in the signed job envelope.

    Everything in :data:`GATEWAY_META_KEYS` and nothing else — notably not
    ``description``, which is the one free-form string on the row.
    """
    clean = normalize(meta)
    return {key: clean[key] for key in GATEWAY_META_KEYS}


def gateway_kwargs(meta: dict) -> dict:
    """Keyword arguments for the agent's ``run_gateway`` handler."""
    clean = normalize(meta)
    return {"action": clean["gateway_action"], "timeout_s": clean["timeout_s"]}


def normalize(meta: dict) -> dict:
    """Coerce anything into the closed shape, falling back rather than raising.

    Falling back is deliberate. This runs on a job row that may have been written by an
    older build, and refusing to normalise one would strand a job nobody can now cancel.
    An unrecognised action becomes the default, which is the safe direction: ``install``
    is idempotent, whereas silently upgrading a typo to ``remove`` would delete a working
    Gateway.
    """
    src = meta if isinstance(meta, dict) else {}
    out = copy.deepcopy(_DEFAULTS)

    action = str(src.get("gateway_action") or "").strip().lower()
    if action in VALID_ACTIONS:
        out["gateway_action"] = action

    out["timeout_s"] = _clamp(src.get("timeout_s"), _DEFAULTS["timeout_s"],
                              _TIMEOUT_MIN, _TIMEOUT_MAX)
    return out


def _clamp(value, default: int, low: int, high: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def check(meta: dict) -> str:
    """Why this metadata could not be run, or "" if it can.

    Separate from :func:`normalize` on purpose: normalize is forgiving so an old row
    stays actionable, while this is what the queueing path calls to refuse a request
    nobody has made yet.
    """
    action = str((meta or {}).get("gateway_action") or "").strip().lower()
    if action and action not in VALID_ACTIONS:
        return (f"{action!r} is not a Gateway action this build knows "
                f"(known: {', '.join(VALID_ACTIONS)})")
    return ""
