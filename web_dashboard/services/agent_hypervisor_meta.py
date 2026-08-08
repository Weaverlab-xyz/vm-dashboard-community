"""Job metadata for agent-brokered hypervisor operations.

A sibling of :mod:`agent_job_meta`, never a widening of it, so the discovery allowlist
stays exactly as strict as it is today.

The design note this implements says the shape is **a closed verb allowlist plus
scheduled inventory sync — not a generic proxy, which is remote code execution with
extra steps.** :data:`HYPERVISOR_META_KEYS` is that allowlist. Every field is a scalar,
an enum, or a charset-constrained opaque id; there is no command, no script, no URL, no
path, and — the part that is easy to lose — **no host and no credential**.

That last omission is the whole design. The job names a connection by the string it has
in the *agent's own* ``connections.yaml``; the agent resolves it locally. So a dashboard
with a fully compromised database can ask an agent to run a verb against a name the
customer already wrote down, and nothing else. No standing vCenter credential is in the
dashboard, in a job payload, in the agent's memory, or in a TLS-inspecting proxy's log —
envelopes are signed, not encrypted, and ``docs/remote-agents.md`` sells the whole
feature on that property.

Pure and stdlib-only, so the boundary is testable without FastAPI or a database.
"""
import copy
import re

HYPERVISOR_META_KEYS = (
    "verb",            # enum — VALID_VERBS
    "connection_ref",  # str — the name this connection has in the AGENT's own
                       #       connections.yaml. Not a host. Not a URL. Not a credential.
    "connection_id",   # str — the dashboard's uuid, echoed back for correlation only;
                       #       the agent never interprets it
    "kind",            # enum — VALID_KINDS
    "target_id",       # str — the hypervisor's OWN opaque id (vmid / uuid / moref)
    "target_scope",    # str — proxmox node / nutanix cluster / vsphere datacenter
    "target_type",     # enum — qemu|lxc|vm
    "page_size",       # int, clamped
    "cursor",          # str — opaque, agent-issued, echoed back
    "timeout_s",       # int, clamped
)

# Phase 4 ships ONE read-only verb. Shipping the *shape* before granting any *power* is
# the point of splitting this from the write-verb phase.
READ_VERBS = ("inventory_sync",)
WRITE_VERBS = ("power_on", "power_off", "power_reset", "restart", "snapshot")
VALID_VERBS = READ_VERBS + WRITE_VERBS

# `snapshot` was held back from the first cut because it is a *create* verb and a
# created thing needs a name — and a name is a free-form string, which is the first
# crack in the no-free-form-string discipline this module exists to keep.
#
# It lands now because the name is GENERATED rather than supplied: `dash-{job_id}`,
# built server-side from an id the operator does not choose. So there is still no
# field here through which operator text can reach a hypervisor, and there is still
# no `snapshot_name` key. The job id also makes the snapshot traceable back to the
# job row that made it, which an operator-typed name would not be.
SNAPSHOT_NAME_PREFIX = "dash-"


def snapshot_name(job_id: str) -> str:
    """The name a snapshot verb creates. Derived, never supplied.

    Constrained to the same charset as every other id that crosses this boundary, and
    truncated: some hypervisors cap snapshot names well below a uuid plus a prefix.
    """
    clean = re.sub(r"[^A-Za-z0-9-]", "", str(job_id or ""))[:32]
    return f"{SNAPSHOT_NAME_PREFIX}{clean}" if clean else f"{SNAPSHOT_NAME_PREFIX}unknown"

# Deliberately absent, and each for its own reason:
#   delete / deploy / clone / console — these need names, sizes, networks, cloud-init:
#              a payload shape indistinguishable from a config file, and a config file
#              is one step from a script. They stay dashboard-direct.
# power_off and power_reset are separate verbs rather than one verb with a `force`
# boolean, because a boolean flag on a destructive verb gets defaulted wrong exactly once.

# `esxi` is a DISTINCT kind from `vsphere` on purpose. Same product, different
# transport: vCenter serves the Automation REST API the agent speaks directly, while a
# bare ESXi host serves SOAP only and has to go through the sibling runner. Conflating
# them is how someone ends up pointing pyVmomi at a vCenter for no reason.
VALID_KINDS = ("vsphere", "proxmox", "nutanix", "xcpng", "hyperv", "esxi")

# The two with no in-agent transport. Listed here as well as in the agent so the
# dashboard can say "this needs the sibling runner" before queueing anything.
SIBLING_KINDS = ("hyperv", "esxi")
VALID_TARGET_TYPES = ("qemu", "lxc", "vm")

MAX_PAGE_SIZE = 1000
MAX_TIMEOUT_S = 600
# 40 pages x 250 default = 10 000 VMs. The cap is what stops a lying agent making the
# dashboard enqueue follow-on jobs forever.
MAX_SYNC_PAGES = 40

_DEFAULTS = {
    "verb": "inventory_sync",
    "connection_ref": "",
    "connection_id": "",
    "kind": "vsphere",
    "target_id": "",
    "target_scope": "",
    "target_type": "vm",
    "page_size": 250,
    "cursor": "",
    "timeout_s": 120,
}

# Opaque ids and cursors are echoed between two systems and end up in log lines and a
# browser. Constrained rather than sanitised: an id that does not match is refused, not
# repaired, because a "repaired" hypervisor id addresses a different VM.
_ID_RE = re.compile(r"^[A-Za-z0-9:_.-]{0,128}$")
_CURSOR_RE = re.compile(r"^[A-Za-z0-9:_.=+/-]{0,128}$")
_REF_RE = re.compile(r"^[A-Za-z0-9_.-]{0,64}$")


def hypervisor_meta(payload, *, description: str) -> dict:
    """Job metadata for an agent_hypervisor run, from the request payload."""
    meta = {"description": description}
    for key in HYPERVISOR_META_KEYS:
        meta[key] = getattr(payload, key, _DEFAULTS[key])
    return normalize(meta)


def hypervisor_kwargs(meta: dict) -> dict:
    """Arguments reconstructed from job metadata, for the signed lease envelope."""
    meta = meta or {}
    out = {}
    for key in HYPERVISOR_META_KEYS:
        out[key] = meta[key] if key in meta else copy.deepcopy(_DEFAULTS[key])
    return out


def normalize(meta: dict) -> dict:
    """Coerce and clamp to the declared shape.

    Applied at enqueue so the stored row is already valid, and again by the agent on
    arrival — both, because they defend different things: the server pass stops an
    operator typo, the agent pass stops a compromised dashboard.

    Enums fall back to their default; **the opaque ids do not**. An unrecognised verb
    becoming `inventory_sync` is harmless, but a `target_id` that fails the charset
    check is emptied rather than guessed at, because a mangled id names a different VM.
    """
    out = dict(meta or {})

    verb = str(out.get("verb") or _DEFAULTS["verb"]).strip().lower()
    out["verb"] = verb if verb in VALID_VERBS else _DEFAULTS["verb"]

    kind = str(out.get("kind") or _DEFAULTS["kind"]).strip().lower()
    out["kind"] = kind if kind in VALID_KINDS else _DEFAULTS["kind"]

    target_type = str(out.get("target_type") or _DEFAULTS["target_type"]).strip().lower()
    out["target_type"] = (target_type if target_type in VALID_TARGET_TYPES
                          else _DEFAULTS["target_type"])

    out["connection_ref"] = _constrained(out.get("connection_ref"), _REF_RE)
    out["connection_id"] = _constrained(out.get("connection_id"), _ID_RE)
    out["target_id"] = _constrained(out.get("target_id"), _ID_RE)
    out["target_scope"] = _constrained(out.get("target_scope"), _ID_RE)
    out["cursor"] = _constrained(out.get("cursor"), _CURSOR_RE)

    out["page_size"] = _clamp(out.get("page_size"), _DEFAULTS["page_size"], 1, MAX_PAGE_SIZE)
    out["timeout_s"] = _clamp(out.get("timeout_s"), _DEFAULTS["timeout_s"], 1, MAX_TIMEOUT_S)
    return out


def _constrained(value, pattern) -> str:
    text = str(value if value is not None else "").strip()
    return text if pattern.match(text) else ""


def _clamp(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


# ── The result, on its way back into the dashboard ────────────────────────────

# One inventory row. Closed, like the request side, and sanitised on arrival for the
# same reason agent_job_meta's FINDING_KEYS are: these values were typed by whoever
# named the VM, on a network the dashboard cannot see, and they end up in a browser.
VM_KEYS = (
    "vm_id",         # str — the hypervisor's own id
    "name",          # str
    "power_state",   # str
    "vcpus",         # int
    "mem_mib",       # int
    "ip_addresses",  # list[str]
    "scope",         # str — node / cluster / datacenter
    "vm_type",       # str
    "tags",          # list[str]
)

MAX_VM_TEXT = 256
MAX_VMS_PER_PAGE = MAX_PAGE_SIZE
MAX_IPS = 16
MAX_TAGS = 32


def sync_page(result: dict) -> dict:
    """The allowlisted projection of one inventory_sync page."""
    result = result or {}
    raw = result.get("vms")
    vms = []
    if isinstance(raw, list):
        for item in raw[:MAX_VMS_PER_PAGE]:
            if isinstance(item, dict):
                vms.append(_clean_vm(item))
    return {
        "vms": vms,
        "next_cursor": _constrained(result.get("next_cursor"), _CURSOR_RE),
        "complete": bool(result.get("complete", True)),
        "scanned": _non_negative(result.get("scanned")),
    }


def _clean_vm(vm: dict) -> dict:
    out = {}
    for key in VM_KEYS:
        if key not in vm:
            continue
        value = vm[key]
        if key in ("vcpus", "mem_mib"):
            out[key] = _non_negative(value)
        elif key == "ip_addresses":
            out[key] = [_clean_text(v) for v in (value or [])[:MAX_IPS] if _clean_text(v)]
        elif key == "tags":
            out[key] = [_clean_text(v) for v in (value or [])[:MAX_TAGS] if _clean_text(v)]
        else:
            text = _clean_text(value)
            if text:
                out[key] = text
    return out


def _clean_text(value) -> str:
    """Strip C0/C1 control characters (except tab) and truncate.

    Mirrors ``agent_job_meta._clean_text`` rather than importing it — same four-line
    rule, same reason both modules are stdlib-only.
    """
    text = str(value if value is not None else "")
    cleaned = "".join(
        ch for ch in text
        if ch == "\t" or (ord(ch) >= 32 and not (0x7F <= ord(ch) <= 0x9F))
    ).strip()
    return cleaned[:MAX_VM_TEXT]


def _non_negative(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
