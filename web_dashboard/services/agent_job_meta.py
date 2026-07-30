"""Job metadata for remote-agent jobs.

This is the boundary between the dashboard and a network the dashboard does not own,
and it is the reason a compromised dashboard cannot simply tell an agent to run
something. :data:`DISCOVER_META_KEYS` is a **closed allowlist**, asserted by the tests,
and every field in it is a scalar, an enum, or a list of network addresses. There is
deliberately no free-form string anywhere in a discovery payload: no command, no
script, no URL to fetch, no filename. A field that could carry executable content
cannot be added by accident, only by editing this tuple and the test that pins it.

That is the same discipline ``ansible_run_meta`` already applies for a different
reason — there, to keep credentials out of the database; here, to keep code out of the
customer's LAN. Both work because the allowlist is the funnel, not a convention.

Pure and stdlib-only, so the round-trip is testable without FastAPI or a database.
"""
import copy

# Everything an agent_discover run needs, and nothing else.
DISCOVER_META_KEYS = (
    "scan_kind",            # "k8s" | "database" | "both"
    "cidrs",                # list[str] — CIDRs to sweep, intersected with agent policy
    "hostnames",            # list[str] — explicit hosts, checked against agent policy
    "ports",                # {"k8s": [int], "database": [int]}
    "use_local_kubeconfig",  # bool — read a kubeconfig MOUNTED INTO the agent
    "timeout_s",            # int — per-probe connect timeout
    "max_hosts",            # int — hard cap on hosts expanded from cidrs
    "concurrency",          # int — parallel probes
)

# The `both` default keeps a payload written by an older build behaving like one that
# never had the field, matching ansible_run_meta's reason for having defaults at all.
_DEFAULTS = {
    "scan_kind": "both",
    "cidrs": [],
    "hostnames": [],
    # Ports a *service* listens on, not ports we guess at. 6443 kubeadm/k3s/rke2,
    # 8443 some managed and older distros, 443 clusters fronted by a load balancer.
    "ports": {"k8s": [6443, 8443, 443],
              "database": [5432, 3306, 1433, 1521]},
    "use_local_kubeconfig": True,
    "timeout_s": 3,
    "max_hosts": 1024,
    "concurrency": 32,
}

VALID_SCAN_KINDS = ("k8s", "database", "both")

# Caps applied server-side at enqueue AND agent-side before scanning. Both, because
# they defend different things: the server cap stops an operator typo from queueing a
# /8 sweep, the agent cap stops a compromised dashboard from doing it on purpose.
MAX_HOSTS_CEILING = 4096
MAX_CONCURRENCY = 128
MAX_TIMEOUT_S = 30


def discover_meta(payload, *, description: str) -> dict:
    """Job metadata for an agent_discover run, from the request payload.

    Read with ``getattr`` so a Pydantic request model and a plain object behave the
    same — the tests use the latter.
    """
    meta = {"description": description}
    for key in DISCOVER_META_KEYS:
        meta[key] = getattr(payload, key, _DEFAULTS[key])
    return normalize(meta)


def discover_kwargs(meta: dict) -> dict:
    """Discovery arguments reconstructed from job metadata.

    Missing keys fall back to :data:`_DEFAULTS` rather than raising: a job queued by an
    older build predates some of these, and refusing to run it would be worse than
    running it the way that build would have.
    """
    meta = meta or {}
    out = {}
    for key in DISCOVER_META_KEYS:
        if key in meta:
            out[key] = meta[key]
        else:
            # deepcopy, not dict()/list(): `ports` nests lists inside a dict, so a
            # shallow copy still hands back the default's inner lists and a caller that
            # appends to one poisons every later reconstruction in the process.
            out[key] = copy.deepcopy(_DEFAULTS[key])
    return out


def normalize(meta: dict) -> dict:
    """Coerce and clamp a payload to the declared shape.

    Applied at enqueue so the stored row is already valid, and applied again by the
    agent on arrival. Anything unrecognised is replaced by its default rather than
    rejected — the caps are the safety property, and a job that silently scans a
    smaller range is a better outcome than one that fails on a typo.
    """
    out = dict(meta or {})

    kind = str(out.get("scan_kind") or _DEFAULTS["scan_kind"]).strip().lower()
    out["scan_kind"] = kind if kind in VALID_SCAN_KINDS else _DEFAULTS["scan_kind"]

    for key in ("cidrs", "hostnames"):
        value = out.get(key) or []
        out[key] = [str(v).strip() for v in value if str(v).strip()] \
            if isinstance(value, (list, tuple)) else []

    ports = out.get("ports")
    if not isinstance(ports, dict):
        ports = dict(_DEFAULTS["ports"])
    out["ports"] = {
        family: _valid_ports(ports.get(family), _DEFAULTS["ports"][family])
        for family in ("k8s", "database")
    }

    out["use_local_kubeconfig"] = bool(out.get("use_local_kubeconfig", True))
    out["timeout_s"] = _clamp(out.get("timeout_s"), _DEFAULTS["timeout_s"], 1, MAX_TIMEOUT_S)
    out["max_hosts"] = _clamp(out.get("max_hosts"), _DEFAULTS["max_hosts"], 1, MAX_HOSTS_CEILING)
    out["concurrency"] = _clamp(out.get("concurrency"), _DEFAULTS["concurrency"], 1, MAX_CONCURRENCY)
    return out


def _valid_ports(value, fallback) -> list:
    if not isinstance(value, (list, tuple)):
        return list(fallback)
    ports = []
    for item in value:
        try:
            port = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 65535:
            ports.append(port)
    return ports or list(fallback)


def _clamp(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))
