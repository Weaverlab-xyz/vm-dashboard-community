"""Job metadata for an ``agent_ansible`` run — Config Management executed by a remote
agent inside a network the dashboard cannot reach.

Two allowlists live here, and **the split between them is the security property.** Both
are closed and asserted by the tests.

:data:`RUN_META_KEYS` is the job *row* spec. It obeys ``ansible_run_meta``'s rule — only
refs, ids and non-secret values are ever written, because job metadata lands in the
database. The resolved playbook bytes, SSH key, DB password and become password are
assembled later, per job, by ``POST /api/agent/jobs/{job_id}/ansible-bundle``.

:data:`ENVELOPE_KEYS` is the far smaller subset that crosses to the agent in the *signed
job envelope*, and it obeys ``agent_job_meta``'s rule instead: every field is a scalar, an
enum or a network address. There is deliberately no asset name, no variable name, no
filename and no free-form string of any kind — nothing through which a compromised
dashboard could name something for an agent to fetch or run. Everything else, including
the playbook, rides the sealed bundle, which the agent fetches over its own signed channel
once the job is ``running``.

That is why this module has two tuples rather than one. A single allowlist would force a
choice between putting the playbook in the envelope (breaking ``agent_job_meta``'s
invariant) and re-deriving the whole run spec agent-side (impossible — the agent holds no
storage backend and no secrets registry).

``run_kind`` and ``transport`` look redundant and are not. ``transport`` selects the *play
shape* (SSH/WinRM to a host, versus a ``hosts: localhost`` play reaching out); ``run_kind``
selects the *sibling image* the operator's policy.yaml names. They coincide today because
there are two kinds; a Kubernetes kind would be ``transport="local"`` with a third image.
Keeping them separate keeps the agent's image choice a lookup in a closed map rather than
an inference from another field.

Pure and stdlib-only, so every round-trip is testable without FastAPI or a database.
"""
import copy

# What the agent runs, and which sibling image the policy must name for it. An enum, so
# no image string ever crosses the wire — the image comes from the operator's policy.yaml.
VALID_RUN_KINDS = ("vm", "database")

# How the play reaches the target. "local" is a ``hosts: localhost, connection: local``
# play that reaches *out* to an endpoint (the database case); the other two SSH/WinRM *to*
# a host. Not a free string: the agent switches on this to pick the command it builds.
VALID_TRANSPORTS = ("ssh", "winrm", "local")

# Which transport a run_kind implies, for the kinds where it is not the operator's choice.
# A database run is always a localhost play; a VM run is ssh or winrm depending on guest OS.
_FORCED_TRANSPORT = {"database": "local"}

# Everything an agent_ansible run needs to be reconstructed, and nothing else.
#
# Mirrors ansible_run_meta.RUN_META_KEYS, and the same boundary holds: secret_vars maps a
# var name to a *source ref*, secret_become_source / secret_ssh_key_source are refs, and
# managed_account / managed_become carry Password Safe ids plus the account name. No
# resolved credential is ever written here.
RUN_META_KEYS = (
    "run_kind",               # one of VALID_RUN_KINDS
    "transport",              # one of VALID_TRANSPORTS
    "target_host",            # str — an address the AGENT reported, or a registered host
    "target_port",            # int
    "connection_id",          # uuid of the agent-bound hypervisor_connections row (vm)
    "target_id",              # hypervisor vm_id (vm) | cloud_databases.id (database)
    "target_label",           # str — display only, for the job description
    "asset",                  # storage KEY, resolved dashboard-side. Never sent to the agent.
    "asset_backend",
    "login_user",             # ansible_user for a vm run
    "extra_vars",
    "secret_vars",
    "secret_become_source",
    "secret_ssh_key_source",
    "managed_account",
    "managed_become",
    # The NAME of the var an EPM-L installation token is bound to, never the token.
    "epml_token_var",
    # A POV environment and one of its VMs, for a run whose login comes from the LAB
    # PLATFORM rather than from this database — the Resource Broker install (slice 5b).
    # Both are ids, so the rule this tuple exists to enforce is untouched: no resolved
    # credential is ever written here. The bundle assembler reads the platform's stored
    # credential at fetch time, which is why nothing has to be stored for it.
    "pov_environment_id",
    "pov_vm_id",
)

# The subset that crosses to the agent in the signed envelope. Scalars, enums and a network
# address — nothing else. Kept as its own tuple, not a slice of the above, so widening the
# job row can never widen the wire by accident.
ENVELOPE_KEYS = ("run_kind", "transport", "target_host", "target_port")

_DEFAULTS = {
    "run_kind": "vm",
    "transport": "ssh",
    "target_host": "",
    # 0, not 22: a valid default would survive normalize() and a WinRM run whose caller
    # supplied no port would silently be aimed at 22. 0 is "unspecified", which is what
    # makes normalize() fall through to _DEFAULT_PORT for the transport actually in use.
    "target_port": 0,
    "connection_id": "",
    "target_id": "",
    "target_label": "",
    "asset": "",
    "asset_backend": "",
    "login_user": "",
    # `{}` not None, matching ansible_run_meta: the run path passes extra_vars to
    # config_drift.inputs_hash unguarded, so None would break the drift record.
    "extra_vars": {},
    "secret_vars": None,
    "secret_become_source": "",
    "secret_ssh_key_source": "",
    "managed_account": None,
    "managed_become": None,
    "epml_token_var": "",
    "pov_environment_id": "",
    "pov_vm_id": "",
}

# Every RUN_META_KEYS entry that normalize() coerces with str(). Listed rather than derived
# so a key added to RUN_META_KEYS without a normalization rule shows up as a test failure.
_STRING_KEYS = (
    "connection_id", "target_id", "target_label", "asset", "asset_backend",
    "login_user", "secret_become_source", "secret_ssh_key_source", "epml_token_var",
    "pov_environment_id", "pov_vm_id",
)

# Default port per transport, used when a caller supplies none. WinRM over HTTP (5985) is
# the default rather than 5986: it is what `winrm quickconfig` enables, and the credential
# reaches the run sealed to a per-fetch key rather than riding WinRM's own transport.
_DEFAULT_PORT = {"ssh": 22, "winrm": 5985, "local": 0}


def transport_for_guest_os(guest_os: str) -> str:
    """``"winrm"`` for a Windows guest, ``"ssh"`` otherwise, from whatever the hypervisor
    reported.

    The inputs are not one vocabulary, which is why this is a function and not a dict: the
    Hyper-V KVP exchange gives a marketing string (``Windows Server 2022 Datacenter``),
    vSphere's guest identity gives a family (``WINDOWS``), and vSphere's own guest-OS code
    gives ``windows9Server64Guest``. All three contain ``windows``, and nothing else here
    does — note the substring is deliberately ``windows`` and not ``win``, because
    ``Darwin`` contains the latter.

    ``ssh`` is the fallback rather than a refusal. A guest with no reported OS is the common
    case on a connection without guest-detail sync, and Linux is both the majority and the
    safe guess: an SSH attempt against a Windows box fails cleanly on connect, whereas
    guessing WinRM for a Linux host would look like a firewall problem. The run form's
    transport field is what settles it when the guess is wrong.
    """
    return "winrm" if "windows" in str(guest_os or "").strip().lower() else "ssh"


def _plain(value):
    """A pydantic sub-model (ManagedAccountRef) as a plain dict, so the metadata is
    JSON-serializable. Anything already plain passes through. Mirrors
    ``ansible_run_meta._plain``."""
    dump = getattr(value, "model_dump", None)
    return dump() if callable(dump) else value


def run_meta(payload, *, description: str, asset_backend: str, **overrides) -> dict:
    """Job metadata for an agent_ansible run, from the request payload.

    ``payload`` is read with ``getattr`` so a RunRequest and a plain object behave the
    same — the tests use the latter. ``overrides`` carries the values the *endpoint*
    resolved rather than the operator typed: the target address and port, the transport,
    and the connection/target ids. Those are deliberately not request fields — a client
    that could name the address could aim the agent at a host of its choosing, which is
    the same reason the dashboard may never set a hypervisor connection's ``host``.
    """
    meta = {"description": description}
    for key in RUN_META_KEYS:
        meta[key] = _plain(getattr(payload, key, _DEFAULTS[key]))
    meta["asset_backend"] = asset_backend
    for key, value in overrides.items():
        if key in RUN_META_KEYS:
            meta[key] = _plain(value)
    return normalize(meta)


def run_kwargs(meta: dict) -> dict:
    """The run spec reconstructed from job metadata.

    Missing keys fall back to :data:`_DEFAULTS` rather than raising: a job queued by an
    older build predates some of these, and failing to resume it would be worse than
    resuming it the way that build would have.
    """
    meta = meta or {}
    out = {}
    for key in RUN_META_KEYS:
        if key in meta:
            out[key] = meta[key]
        else:
            # deepcopy, not dict(): never hand back the shared default object itself — a
            # caller that mutated it would poison every later reconstruction.
            out[key] = copy.deepcopy(_DEFAULTS[key])
    return out


def envelope_payload(meta: dict) -> dict:
    """What the agent is told, projected through :data:`ENVELOPE_KEYS` and re-normalized.

    Called by ``api.agent._envelope_payload`` on the way into the signed envelope. Starts
    from an allowlist rather than filtering a denylist, so a field added to the job row
    later cannot start crossing the wire on its own.
    """
    full = normalize(meta or {})
    return {key: full[key] for key in ENVELOPE_KEYS}


def normalize(meta: dict) -> dict:
    """Coerce and clamp a payload to the declared shape.

    Applied at enqueue so the stored row is already valid, and applied again by the agent
    on arrival — the same both-ends discipline ``agent_job_meta.normalize`` uses, and for
    the same reason: the server pass stops an operator typo, the agent pass stops a
    compromised dashboard.

    Unlike the discovery normalizer this does **not** silently substitute a default for an
    unrecognised ``run_kind`` or ``transport``; it blanks them so :func:`check` refuses.
    A discovery job that quietly scans a smaller range is a better outcome than one that
    fails, but a config-management job that quietly runs the *wrong play shape* against a
    host is not.
    """
    out = dict(meta or {})

    kind = str(out.get("run_kind") or "").strip().lower()
    out["run_kind"] = kind if kind in VALID_RUN_KINDS else ""

    transport = _FORCED_TRANSPORT.get(out["run_kind"])
    if transport is None:
        transport = str(out.get("transport") or "").strip().lower()
    out["transport"] = transport if transport in VALID_TRANSPORTS else ""

    out["target_host"] = str(out.get("target_host") or "").strip()

    port = out.get("target_port")
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 0
    if not 1 <= port <= 65535:
        port = _DEFAULT_PORT.get(out["transport"], 0)
    out["target_port"] = port

    for key in _STRING_KEYS:
        out[key] = str(out.get(key) or "").strip()

    extra = out.get("extra_vars")
    out["extra_vars"] = extra if isinstance(extra, dict) else {}

    secret_vars = out.get("secret_vars")
    out["secret_vars"] = secret_vars if isinstance(secret_vars, dict) else None

    for key in ("managed_account", "managed_become"):
        value = _plain(out.get(key))
        out[key] = value if isinstance(value, dict) else None

    return out


def check(meta: dict) -> str:
    """The reason this run cannot be dispatched, or ``""`` when it can.

    One funnel that both the enqueue endpoint and the agent read, so the two cannot
    disagree about what a valid run is. Returns a string rather than raising, because the
    endpoint turns it into a 400 and the agent turns it into a job error — neither wants
    an exception type from the other's world.
    """
    # The raw values, read before normalize() blanks an unrecognised one — otherwise the
    # refusal reads "got ''" for a caller who sent "shell", which names nothing.
    raw = meta or {}
    raw_kind = str(raw.get("run_kind") or "")
    raw_transport = str(raw.get("transport") or "")
    meta = normalize(meta)
    if meta["run_kind"] not in VALID_RUN_KINDS:
        return (f"run_kind must be one of {'/'.join(VALID_RUN_KINDS)} "
                f"(got {raw_kind!r}).")
    if meta["transport"] not in VALID_TRANSPORTS:
        return (f"transport must be one of {'/'.join(VALID_TRANSPORTS)} "
                f"(got {raw_transport!r}).")
    if not meta["target_host"]:
        return ("This target has no address the agent could reach. A synced VM only "
                "reports one when it is powered on with guest tools installed and its "
                "connection has guest-detail sync enabled.")
    if not meta["target_port"]:
        return f"transport {meta['transport']!r} needs an explicit target_port."
    if meta["run_kind"] == "vm" and not meta["connection_id"]:
        # A POV run is the one VM run with no hypervisor connection behind it: its target
        # came from the LAB PLATFORM's VM read, not from an agent-brokered sync, and its
        # login comes from the platform too. So it names an environment and a VM instead —
        # which is the same thing this clause is really asking for, namely that the
        # address was resolved by the dashboard rather than supplied by a caller.
        if not (meta["pov_environment_id"] and meta["pov_vm_id"]):
            return ("A VM run must name the agent-bound connection its target was synced "
                    "from, or the POV environment and VM it belongs to.")
    if meta["run_kind"] == "database" and not meta["target_id"]:
        return "A database run must name the database it targets."
    return ""
