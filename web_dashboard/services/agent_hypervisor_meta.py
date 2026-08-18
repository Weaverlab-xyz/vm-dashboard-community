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
WRITE_VERBS = ("power_on", "power_off", "power_reset", "restart", "shutdown",
               "reboot", "snapshot")
VALID_VERBS = READ_VERBS + WRITE_VERBS

# `shutdown` and `reboot` are the GRACEFUL pair, added because the five verbs before
# them could not express either one and the pages offer both. They are separate verbs
# rather than a `graceful` flag on `power_off`/`power_reset` for the same reason those
# two are separate verbs: a boolean on a destructive op gets defaulted wrong once and
# then nobody can tell which button did what.
#
# Adding a verb is a THREE-file change, and landing part of it is worse than landing
# none of it: `normalize()` below falls an unrecognised verb back to `inventory_sync`,
# so a verb this allowlist grants but an agent does not implement would enqueue a
# discovery scan that completes green while the VM never moves. The three:
#
#   1. here — the allowlist, and PAGE_OPS below;
#   2. runners/agent/agent.py — `_POWER`, and `_power_vsphere`'s own table;
#   3. runners/hypervisor/run.py — `VALID_VERBS` and `_PS_POWER`, for Hyper-V/ESXi.
#
# Version skew across those three is safe in the one direction it actually occurs: an
# agent is deployed separately and lags. An OLD agent given a NEW verb refuses it out
# loud — it reads `payload["verb"]` raw and never normalises, so the refusal comes first
# from `policy.check_verb` (the customer's policy.yaml grants no such verb, and the
# message names the file and the line to add) and then from the per-kind dispatch. The
# dangerous direction — an agent that quietly does something else — is not reachable.

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
#   suspend / resume / pause / save — a suspend writes guest RAM to the datastore, so
#              it is a storage operation wearing a power button's clothes, and the four
#              products disagree about what resuming one even means. Still refused; see
#              _NO_EQUIVALENT.
# power_off and power_reset are separate verbs rather than one verb with a `force`
# boolean, because a boolean flag on a destructive verb gets defaulted wrong exactly once.


# ── Page op -> verb, per hypervisor kind ──────────────────────────────────────

# The hypervisor pages speak the vocabulary of the hypervisor's own UI (`shutdown`,
# `reboot`, `hard_reboot`); the agent speaks the verbs above. This is the map between
# them, and it is **per kind on purpose**.
#
# It has to be, because `restart` is not one operation. Each kind resolves it
# differently: Proxmox `/status/shutdown` (a graceful shutdown), vSphere `?action=reset`
# (a HARD reset), XCP-ng `VM.clean_reboot` (a reboot). One table shared across the
# routers therefore cannot be correct for more than one of them — and one *was* shared,
# by copy, into proxmox.py, vsphere.py and xcpng.py, mapping every page's `shutdown`
# onto `restart`. All three results shipped:
#
#   * agent-bound vCenter **Shutdown** hard-reset the guest, risking the data loss the
#     operator pressed Shutdown to avoid;
#   * agent-bound XCP-ng **Shutdown** cleanly rebooted it, so it came back up;
#   * Proxmox **Reboot** gracefully shut it down and left it off — the same fault
#     mirrored, since there `restart` really is the shutdown.
#
# Checking each mapped verb against WRITE_VERBS (which `agent_power_job` does, and
# should keep doing) cannot catch any of that: `restart` is a perfectly real verb. Only
# asking what it *resolves to on this kind* catches it, which is what
# tests/test_hypervisor_power_routing.py::AGENT_OP does.
#
# So the rule this table exists to enforce: a page op maps to a verb only when the
# verb's resolved operation *for that kind* is the operation the button promises.
# Nothing is approximated onto a neighbouring verb — an op with no honest equivalent is
# absent here and refused, because "close enough" on a power button is how a graceful
# shutdown becomes a hard reset.
#
# `shutdown` and `reboot` are the verbs that fixed the four inversions above. Note that
# they did NOT do it by making `restart` mean one thing: `restart` is still XCP-ng's
# clean reboot and nothing else's, and the one remaining reading of it below is that
# one. The alternative — redefining `restart` as "graceful reboot" everywhere — was
# rejected because a deployed agent already implements `restart` and a deployed
# policy.yaml already grants it, so redefining it would silently change what an
# un-upgraded agent does with a button, which is the exact failure this whole table
# exists to prevent. A NEW verb name is refused out loud by an old agent instead.
PAGE_OPS = {
    "proxmox": {
        "start": "power_on",
        "stop": "power_off",       # /status/stop — the force-stop the page offers
        "shutdown": "shutdown",    # /status/shutdown
        "reboot": "reboot",        # /status/reboot
        # `shutdown` was `restart` until the `shutdown` verb existed, because on Proxmox
        # alone `restart` really does resolve to /status/shutdown. That mapping was
        # correct and it still moved: leaving it would have kept "restart means shutdown
        # here" alive as a per-kind special case, which is the reading that made Reboot
        # unmappable. The cost is that Proxmox Shutdown — the one button in this table
        # that worked before this change — refuses until its agent is re-pulled.
    },
    "vsphere": {
        "start": "power_on",
        "stop": "power_off",       # ?action=stop — a hard power off, which is the op
        "reset": "power_reset",    # ?action=reset
        "shutdown": "shutdown",    # NOT a power action: /guest/power?action=shutdown,
                                   # a different endpoint, and it needs VMware Tools
                                   # running in the guest. See _power_vsphere.
        # `suspend` is absent: there is no suspend verb.
    },
    "xcpng": {
        "start": "power_on",
        "stop": "power_off",           # VM.hard_shutdown
        "shutdown": "shutdown",        # VM.clean_shutdown
        "reboot": "restart",           # VM.clean_reboot — the one place `restart` is
                                       # still read, and the only kind where it is a
                                       # reboot. Deliberately NOT moved to the new
                                       # `reboot` verb: this button works on every
                                       # agent already deployed, and moving it would
                                       # break it until each one is re-pulled, for no
                                       # change in what the VM does.
        "hard_reboot": "power_reset",  # VM.hard_reboot
        # `suspend`, `resume`, `pause` and `unpause` are absent — no verb for any.
    },
    "hyperv": {
        "start": "power_on",           # Start-VM
        "stop": "power_off",           # Stop-VM -TurnOff -Force, the force-stop
        "shutdown": "shutdown",        # Stop-VM, bare — Microsoft's own example for it
                                       # is "shuts down … through the guest operating
                                       # system". NO -Force: on Stop-VM that switch is
                                       # not the prompt suppressor it is on Restart-VM,
                                       # it means "regardless of any unsaved application
                                       # data", which is a different promise.
        "restart": "power_reset",      # Restart-VM -Force. Documented as a "hard"
                                       # restart — "like powering the computer down,
                                       # then back up again" — so `power_reset` is the
                                       # honest verb for it; -Force only suppresses the
                                       # confirmation prompt, which a non-interactive
                                       # WinRM session could not answer.
        # `reboot` is absent, and it is the one op refused for a reason that is not the
        # dashboard's: Hyper-V has no graceful-reboot cmdlet at all. Restart-VM is the
        # hard one (above), so there is no script to point a `reboot` verb at here.
        # `pause`, `resume` and `save` are absent; the runner implements no script.
    },
}

# Why an unmapped op cannot simply be passed through: `normalize()` falls an
# unrecognised verb back to `inventory_sync`, so a page op that reached it unmapped
# became a *discovery scan* that completed green. The operator clicked Suspend, the job
# said success, and nothing was suspended. `agent_verb` therefore returns None rather
# than defaulting, and its caller raises rather than enqueues.

# An op whose nearest verb would do something DIFFERENT rather than nothing, and what
# that something is. Only for those: the generic reason below is the right answer for an
# op that is merely absent, and a message that over-explains an ordinary gap trains the
# operator to skim the one that matters.
#
# Empty, and that is the point of this change rather than an oversight. Every entry it
# held — vSphere/XCP-ng/Hyper-V `shutdown` and Proxmox `reboot` — described `restart`
# resolving to the wrong operation on that kind, and all four now have a verb that
# resolves to the right one. What remains refused (`suspend`, `resume`, `pause`,
# `unpause`, `save`) is refused for the ordinary reason: no verb, no near-neighbour
# quietly standing in for one. Kept rather than deleted because the next verb-shaped gap
# will want it, and because a stale entry here is a lie the operator reads at the moment
# they are least able to check it — which is what
# tests/…::test_no_stated_reason_outlives_the_gap_it_described exists to catch.
_NO_EQUIVALENT: dict = {}


def agent_verb(kind: str, op: str):
    """The agent verb for a page op on this kind of hypervisor, or None.

    None means refuse. It never means "pass the op through": see the note above on
    :func:`normalize` turning an unknown verb into ``inventory_sync``.
    """
    return (PAGE_OPS.get(str(kind or "").strip().lower()) or {}).get(
        str(op or "").strip().lower())


def no_verb_reason(kind: str, op: str) -> str:
    """Why this op is refused for an agent-bound connection, and what is available.

    This string is the operator's whole diagnosis — they are looking at a page whose
    button just failed — so it names the buttons that do work, and, where one exists,
    the substitution that would have been wrong.
    """
    kind = str(kind or "").strip().lower()
    op = str(op or "").strip().lower()
    available = sorted(PAGE_OPS.get(kind) or {})
    detail = _NO_EQUIVALENT.get((kind, op), "the agent has no verb for it")
    return (f"'{op}' is not available on an agent-bound {kind} connection: {detail}. "
            f"Available here: {', '.join(available) or 'none'}. For {op}, use the "
            f"hypervisor's own console, or a directly-reachable connection.")

# `esxi` is a DISTINCT kind from `vsphere` on purpose. Same product, different
# transport: vCenter serves the Automation REST API the agent speaks directly, while a
# bare ESXi host serves SOAP only and has to go through the sibling runner. Conflating
# them is how someone ends up pointing pyVmomi at a vCenter for no reason.
VALID_KINDS = ("vsphere", "proxmox", "nutanix", "xcpng", "hyperv", "esxi",
               "workstation")

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
