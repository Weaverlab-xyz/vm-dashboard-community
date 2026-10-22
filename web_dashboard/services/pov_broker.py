"""The POV broker: the agent that runs *inside* the customer's environment.

Slice 3 of the POV feature. Slice 2 leaves a running environment the dashboard can create,
power and destroy but cannot reach into: a POV lives on a lab platform's private network,
and this dashboard has no route to it — the same problem ``ansible_cloud_run_service``
answers with "only this host has a route to the cluster". Every later slice (the Gateway,
the Password Safe Resource Broker, the per-VM wire-up) needs to run something on that
network, so all of them wait on this one.

The answer is the mechanism the remote-agent feature already provides, pointed at a new
place: one VM in the environment is the **broker**, it runs the dashboard agent, and the
agent dials *out*. Nothing here invents a second execution path.

Platform-agnostic on purpose. The only platform-specific step is handing the payload to
the guest, which goes through ``lab_platforms`` — the ``bootstrap_injection`` capability.
Skytap's mechanism is ``"metadata"``: the platform stores ``user_data`` and the guest
fetches it. **Nothing executes it for you.** That is why this module is only half of the
feature; the other half is the template contract in ``docs/profiles/pov/skytap.md``, and a
template without a runner reads the payload and does nothing, silently.

Four orderings are load-bearing here, each wrong in a way that is quiet rather than loud:

**The payload is injected AFTER power-on, never before.** An enrolment code lives fifteen
minutes and a first boot is not bounded — a Windows template pulling updates can eat all
of it. Injecting before power-on hands the guest a code that expired while it booted, and
the symptom is an agent stuck at ``enrolling`` with no request ever reaching the
dashboard. Injecting after means the code starts its clock when the guest is already up
and polling.

**The wait is derived from that TTL, never chosen.** Waiting past the moment the code
expires is waiting for something that cannot happen.

**The agent id is persisted before the wait.** If the process dies mid-wait, a re-run must
know which agent row to re-issue a code for. Minting a second row for the same POV is how
you end up revoking the wrong one at teardown.

**Re-running removes the agent's state volume.** The agent writes its identity there on
first enrolment and never redeems a code again. A re-issued code plus a surviving volume
gives you a container that starts cleanly, signs with a key the dashboard has just
cleared, and 401s forever — which reads as revocation, not as a stale volume.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime

from sqlalchemy.orm import Session

from ..database import PovEnvironment, PovEnvironmentVM, RemoteAgent
from . import agent_service, config_service, job_service, lab_platforms, public_url
from ..utils.clock import utcnow

logger = logging.getLogger(__name__)


class BrokerError(Exception):
    """A refusal carrying the remedy, not just the cause.

    Raised where the operator has something to do about it. The message lands in the
    job's ``error_message`` and on the POV row, which are the only two places anyone
    looks — see docs/notes on why a log line is not a third place.
    """


# The CONVENTIONAL name for the VM that carries the agent. No longer a default that has
# to match: a POV that named nothing is resolved by `resolve_broker_candidate` below, and
# this name serves two narrower jobs — breaking a tie between several Linux guests, and
# being the name the cloud read-back looks for, where the template designates its broker
# by ROLE and names it this.
#
# It stopped being a hard requirement because the dashboard now has a signal it did not
# have when this was written: `PovEnvironmentVM.guest_os`, an operator-correctable OS. On
# a template with one Linux guest that is a better answer than any name, because the
# thing being looked for is "the Docker host", and Linux is a fact about it while its
# name is a convention somebody else's template never agreed to.
DEFAULT_BROKER_VM_NAME = "broker"

# Where the operator-typed name lives. Not a secret — a VM name inside the environment —
# so it goes on the row's metadata, the same split `pov_resource_broker` and
# `pov_entitle_agent` make for theirs.
_META_VM_NAME = "broker_vm_name"

# Both markers must be present before the guest runs anything. A metadata read that
# returns a truncated body would otherwise execute half a script — with the `docker rm -f`
# and the volume delete at the top and the `docker run` that replaces them at the bottom,
# half of this script is precisely the destructive half.
BOOTSTRAP_BEGIN = "# BEGIN-DASHBOARD-AGENT-BOOTSTRAP v1"
BOOTSTRAP_END = "# END-DASHBOARD-AGENT-BOOTSTRAP"

# The image the broker runs. Same one the Agents page emits; kept as one constant so a
# rename cannot ship a page that says one thing and a POV that installs another.
AGENT_IMAGE = "chrweav/dashboard-agent:latest"

# Where the bootstrap writes the agent's files on the broker VM.
GUEST_STATE_DIR = "/etc/dashboard-agent"
# The named volume holding the agent's identity. Removed on every re-run — see the module
# docstring.
GUEST_STATE_VOLUME = "dashboard_agent_state"

# The Docker socket, on the guest and inside the container — one constant because the
# bootstrap mounts it at the same path it reads it from, and the agent's own default
# (``AGENT_DOCKER_SOCKET``) is that path. A mount whose container side differs from what the
# agent looks for is a "cannot reach the Docker socket" that no amount of mounting fixes.
GUEST_DOCKER_SOCKET = "/var/run/docker.sock"

# SPIRE attestation for the broker (opt-in through "Let agents attest through SPIRE"). The
# same two sidecars as examples/remote-agent/docker-compose.spire.yml, pinned to the same
# digests — tests/test_pov_broker_spire.py keeps them in step — run with `docker run`
# because the bootstrap is a shell script, not a compose project.
SPIRE_AGENT_IMAGE = ("ghcr.io/spiffe/spire-agent:1.15.3"
                     "@sha256:41b0dcd8b258a69db9e2768292a060766fb76fd866e4bc925849981ea1b825ff")
SPIFFE_HELPER_IMAGE = ("ghcr.io/spiffe/spiffe-helper:0.12.1"
                       "@sha256:49a7b1aca5b497bb4c1ca1ed1f15397bdf9c9cafb8036df4b963b566bd234f7f")
GUEST_SPIRE_DIR = GUEST_STATE_DIR + "/spire"
# The JWT-SVID lives here: a tmpfs-backed named volume owned by the agent's uid, so the
# token is never on the guest's disk, and the volume definition survives a reboot where a
# tmpfs mounted by hand would not.
GUEST_JWT_VOLUME = "dashboard_agent_spiffe_jwt"
SPIRE_CONTAINERS = ("dashboard-agent-spiffe-helper", "dashboard-agent-spire")
# How long the guest waits for an SVID before it falls back to the enrolment code. A
# reachable server issues one in seconds; this only bounds a port that is blocked or a
# proxy that inspects TLS, which is where a customer's network most often stops SPIRE.
SPIRE_WAIT_SECONDS = 120

# What the broker may reach. Ports rather than "any": the difference between "may look for
# databases in the POV" and "may reach anything in the customer's environment". These are
# the management ports the later wire-up slices actually use.
BROKER_PORTS = (22, 443, 3389, 5985, 5986)

# What a POV broker may be asked to do. Each entry is added by the slice that uses it —
# granting one nothing runs is granting something for no reason.
#
#   agent_discover  the broker reads the POV's own VMs (slice 3)
#   agent_gateway   the broker runs the POV's BeyondTrust Gateway (slice 5)
#   agent_ansible   the broker installs the Password Safe Resource Broker (slice 5b)
#
# `agent_ansible` is the largest of the three by a distance, and the policy example says
# why in as many words: `targets:` grants a port PROBE, this grants a playbook running as
# root on the hosts you name. So it comes with its own `ansible.targets` list, which
# `render_policy` scopes to the POV's WINDOWS guests rather than reusing the discovery one.
#
# **A broker enrolled before a grant was added does not have it.** policy.yaml is written
# once, at enrolment, and the agent reads it at start — so adding a type here changes
# nothing on a running broker until it is re-brokered. `pov_gateway.preflight` refuses
# with that remedy rather than letting the job lease and be refused on the far side.
BROKER_JOB_TYPES = ("agent_discover", "agent_gateway", "agent_ansible")

# The Gateway image a POV broker is allowed to run, and the flag that makes it work.
#
# On a customer-owned agent this comes from a file the customer wrote, and that is the
# trust boundary. On a POV it does not: the dashboard generated the broker VM, the
# bootstrap and this policy, so the boundary is somewhere else entirely. Keeping the same
# SHAPE anyway is still worth it — one code path on the agent, one thing to reason about —
# but nobody should read this block as the customer having agreed to it.
#
# `privileged` is not decoration. A Gateway needs NET_ADMIN, NET_RAW, IPC_LOCK and
# /dev/net/tun to carry protocol tunnels; without them it registers online and every
# tunnel times out, which reads as a firewall problem for days.
GATEWAY_IMAGE = "beyondtrust/sra-jumpoint:latest"

# The Config-Management runner a POV broker may launch. `run_kind="vm"` selects it, and the
# Resource Broker install is a WinRM run — which is exactly what this image is for.
ANSIBLE_VM_IMAGE = "chrweav/ansible-winrm:latest"

# The ports an RB install needs on its target. 5985 is WinRM over HTTP and 5986 over HTTPS;
# both are named because which one a template's guest has enabled is the template's
# choice, and a run refused for the port would read as a firewall.
ANSIBLE_PORTS = (5985, 5986)

# SSH, for a Linux target — the Entitle agent host. A separate constant rather than a
# third entry in ANSIBLE_PORTS: these are granted per target, and a Windows guest has no
# business being reachable on 22 by a playbook just because a Linux one in the same POV is.
ANSIBLE_SSH_PORTS = (22,)

# How often the enrolment wait looks. Sixty seconds of margin below the code's TTL so the
# last poll still has a code to redeem.
ENROLL_POLL_SECONDS = 15.0
_ENROLL_MARGIN_SECONDS = 60.0


def enroll_timeout_seconds() -> float:
    """How long to wait for the agent to appear, derived from the code's own lifetime.

    Never a constant of its own. A wait longer than the TTL spends its tail waiting for
    something that has become impossible, and reports a timeout where the real answer is
    "the code expired" — two different remedies behind one message.
    """
    return max(ENROLL_POLL_SECONDS,
               agent_service.ENROLL_TTL_MINUTES * 60.0 - _ENROLL_MARGIN_SECONDS)


# ── naming ───────────────────────────────────────────────────────────────────

def broker_vm_name(env: PovEnvironment) -> str:
    """The VM name this POV's broker is expected to have.

    Per-environment rather than global: templates come from wherever the SE got them, and
    a single account can hold two whose broker VMs are named differently. Stored on the
    row's metadata at provision time so it survives into every later re-run — a default
    read fresh each time would silently change meaning when the default changed.

    Substitutes the conventional name, so this answers "what name would we look for" and
    NOT "what did the operator ask for". Anything choosing a VM wants
    :func:`stored_broker_vm_name`; the difference is load-bearing — see there.
    """
    return (env.metadata_dict.get(_META_VM_NAME) or "").strip() or DEFAULT_BROKER_VM_NAME


def stored_broker_vm_name(env: PovEnvironment) -> str:
    """What was actually TYPED, or "" — as opposed to :func:`broker_vm_name`, which
    substitutes the conventional name.

    The distinction is the hinge of the whole selection rule. "The operator typed nothing"
    and "the operator typed 'broker'" are different instructions — the first means *infer
    which guest this is*, the second means *match this name exactly and refuse if it is
    missing* — and the defaulted reader above cannot tell them apart. While `broker` was a
    hard requirement that did not matter; now that a blank field means auto-detect, a
    reader that invents a value would switch inference off for every POV.
    """
    return str(env.metadata_dict.get(_META_VM_NAME) or "").strip()


def set_broker_vm_name(env: PovEnvironment, vm_name: str | None) -> None:
    """MERGE the typed name onto the row's metadata. ``None`` leaves it alone, ``""``
    clears it back to auto-detect. No commit: the create path writes this onto a row it
    has not added yet, so the caller owns the transaction.

    A merge, not an assignment. `metadata_dict` also carries `rb_vm_name`, `rb_zone`,
    `rb_asset`, the Entitle host name, the guest-step opt-in list and `broker_error`, so
    replacing the dict drops whichever of those was set first.
    """
    meta = env.metadata_dict
    if vm_name is None:
        return
    value = str(vm_name).strip()
    if value:
        meta[_META_VM_NAME] = value
    else:
        meta.pop(_META_VM_NAME, None)
    env.metadata_dict = meta


def configure(db: Session, env: PovEnvironment, *, vm_name: str | None = None) -> None:
    """:func:`set_broker_vm_name` plus the commit. The post-create writer."""
    set_broker_vm_name(env, vm_name)
    db.commit()


def agent_name(env: PovEnvironment) -> str:
    """The agent row's name. Derived, so a re-run finds the existing one.

    ``RemoteAgent.name`` is 64 characters because ``Job.created_by`` records
    ``agent:{name}`` in a 100-char column. A POV name may be 63 on its own, so the prefix
    is truncated rather than the suffix: an agent called ``pov-really-long-nam`` with no
    ``-broker`` on the end is one nobody can recognise in the console.
    """
    return ("pov-" + (env.name or "unnamed"))[:57] + "-broker"


def dashboard_agent_url() -> str:
    """The URL the broker agent must dial, or "" when the install has not stated one.

    The pinned signing audience wins, because that is the value every agent signature is
    checked against — handing a broker a *different* URL produces an agent that enrols and
    then 401s on every poll. ``public_base_url`` is the fallback for the first agent on a
    fresh install, before anything is pinned.
    """
    pinned = (config_service.get(agent_service.AUDIENCE_CONFIG) or "").rstrip("/")
    return pinned or public_url.configured()


# ── selection ────────────────────────────────────────────────────────────────

def name_matches_broker(name: str, wanted: str) -> bool:
    """Whether a VM name is THE broker name: exact, case-insensitive, trimmed.

    One predicate rather than the same expression written out wherever a broker is picked.
    The rule it encodes is the load-bearing part: "contains broker" also matches a customer
    VM called ``password-broker``, and the cost of that wrong answer is an agent installed
    on a machine nobody expected. It is now one rung of
    :func:`resolve_broker_candidate` rather than the whole rule, and exactness is what
    that rung is for: a name the operator TYPED is an instruction, so matching it loosely
    is how a typo becomes a successful install on the wrong guest.
    """
    return (name or "").strip().lower() == (wanted or "").strip().lower()


# The remedy the POV page can offer. A parameter on the resolver rather than baked into
# its messages, because the template builder's remedy is a different sentence: it holds a
# template nobody has made a POV from yet, so "set the name on this POV" names nothing.
_POV_REMEDY = ("Set the control node VM name on this POV to name one on purpose, then press "
               "Install control node.")


def claimed_vm_names(env: PovEnvironment | None = None) -> tuple[str, ...]:
    """The VM names another POV role has already taken, lowercased.

    Inference must not hand the agent a guest whose job is something else. On a POV with
    one Linux VM, "the only Linux VM" and "the Entitle agent host" can be the same
    machine, and that is a refusal rather than a coincidence: k3s brings its own
    containerd and its own iptables rules onto the one host whose job is keeping the
    agent channel up.

    The DEFAULTED readers, deliberately. A template guest literally named "entitle" is
    the Entitle host by convention whether or not anybody configured it, and inference
    that conscripted it would be overruled by the first person to press that button.

    ``env=None`` gives the two conventional names alone, which is all the template
    builder can know -- it holds a template, not a POV.

    Imported in the function body: both modules reach this one, so a top-level import
    would close a real cycle. Same reason ``ensure_broker`` imports them locally.
    """
    from . import pov_entitle_agent, pov_resource_broker
    if env is None:
        names = (pov_entitle_agent.DEFAULT_HOST_VM_NAME,
                 pov_resource_broker.DEFAULT_RB_VM_NAME)
    else:
        names = (pov_entitle_agent.host_vm_name(env),
                 pov_resource_broker.rb_vm_name(env))
    return tuple(sorted({(n or "").strip().lower() for n in names if (n or "").strip()}))


def _label(cand: dict) -> str:
    """How a candidate is named in a refusal: its name, or its platform id if unnamed."""
    return str(cand.get("name") or cand.get("id") or "").strip() or "unnamed"


def resolve_broker_candidate(candidates: list[dict], *, typed_name: str = "",
                             claimed_names: tuple[str, ...] = (), sticky_id: str = "",
                             pinned_id: str = "", remedy: str = _POV_REMEDY) -> dict:
    """Which of these VMs is the broker. Returns the candidate given; raises
    ``BrokerError`` naming what it found.

    ``candidates`` is the minimal shape both callers already hold --
    ``{"id", "name", "os_family"}``, extra keys ignored and the SAME dict handed back, so
    the template builder can pass its live adapter dicts straight in and keep using the
    NIC list on the one that comes out.

    ONE expression of the rule, over both DB rows and a live platform read, because a
    builder that PREPARES one VM while the adopt path ENROLS another is the failure mode
    two copies of it produce, and neither half would look wrong on its own.

    The ladder, in order. Each rung exists because the one above it cannot answer:

    a. **Pinned** -- ``pinned_id`` wins outright: the platform has just told us which
       instance it built, which is better than any inference about it and better than any
       name, because the dashboard chose that name itself moments earlier.
    b. **Typed** -- a name the operator actually typed is matched exactly, and refused if
       nothing matches. Never widened to inference: a typed name matching nothing is a
       typo or a renamed template, and inferring past it installs an agent somewhere the
       operator did not ask for.
    c. **Sticky** -- ``sticky_id`` still among the candidates. An enrolled broker is not
       moved: the agent's identity lives on that guest, so re-resolving onto a different
       VM leaves a container polling from a machine nothing points at.

       Below the typed rung, deliberately. This exists to stop INFERENCE moving an
       enrolled broker, not to outrank an operator -- above it, naming a VM on a POV that
       had already brokered would be silently ignored, which is exactly the remedy the
       refusals at (f) tell people to use.
    d. **Infer** -- the Linux candidates, minus ``claimed_names``. Exactly one survivor
       is an answer. Uniqueness, never position: "the first Linux VM" is not a decision a
       position in a list can make, but "the only one" is a fact about the template. A
       blank family is NOT Linux -- it means the platform did not say, which is what
       ``PovEnvironmentVM.os_family_override`` exists to end.
    e. **Convention** -- several survivors, one of them named ``DEFAULT_BROKER_VM_NAME``.
    f. **Refuse**, naming the candidates and the remedy.
    """
    if not candidates:
        raise BrokerError(
            "this environment has no VM rows yet, so there is nothing to install the "
            "control node on. Refresh the POV and try again once the platform reports its VMs.")

    found = ", ".join(sorted(_label(c) for c in candidates)) or "none"

    # (a) the instance the platform just handed back.
    pinned = str(pinned_id or "").strip()
    if pinned:
        for cand in candidates:
            if str(cand.get("id") or "").strip() == pinned:
                return cand

    # (b) a name the operator typed is exact, or a refusal. Text kept as it was, so the
    # remedy an operator already knows still reads the same way.
    wanted = str(typed_name or "").strip()
    if wanted:
        for cand in candidates:
            if name_matches_broker(cand.get("name"), wanted):
                return cand
        raise BrokerError(
            f"no VM in this environment is named {wanted.lower()!r}, so there is nowhere "
            f"to install the control node's agent. Found: {found}. Rename the template's "
            f"control node VM, or set a different control node VM name on this POV.")

    # (c) an enrolled broker is not moved BY INFERENCE -- but a typed name above already
    # had its say, so this never overrules an operator.
    sticky = str(sticky_id or "").strip()
    if sticky:
        for cand in candidates:
            if str(cand.get("id") or "").strip() == sticky:
                return cand

    # (d) the only Linux guest that is not already somebody else's host.
    claimed = {(n or "").strip().lower() for n in claimed_names}
    linux = [c for c in candidates
             if str(c.get("os_family") or "").strip().lower() == "linux"]
    free = [c for c in linux if _label(c).lower() not in claimed]
    if len(free) == 1:
        return free[0]

    if not linux:
        blank = [c for c in candidates if not str(c.get("os_family") or "").strip()]
        if blank:
            # The message a BtPocLin01-shaped POV meets, and the reason this rung names
            # the blank guests: the operator is looking at a VMs tab whose OS column
            # reads "-" and has no reason to connect that to a broker that will not
            # install.
            unknown = ", ".join(sorted(_label(c) for c in blank))
            raise BrokerError(
                f"no VM in this environment reports a Linux OS, and {len(blank)} report "
                f"none at all ({unknown}). A blank OS means unknown, never Linux. Set "
                f"the OS on the POV's VMs tab for the guest that runs Docker, or set the "
                f"control node VM name. Found: {found}.")
        raise BrokerError(
            f"no VM in this environment runs Linux, and the control node is a Docker "
            f"host. Found: {found}. {remedy}")

    if not free:
        # Every Linux guest is already claimed. Naming the claim matters: "the only Linux
        # VM is the Entitle host" is a sentence an operator can act on, where "could not
        # resolve the broker" sends them looking at the template instead.
        taken = ", ".join(sorted(_label(c) for c in linux))
        raise BrokerError(
            f"the only Linux VM in this environment ({taken}) is already this POV's "
            f"Entitle agent or Resource Broker host, and those are deliberately "
            f"different machines from the control node. Add a Linux VM to the template, or "
            f"{remedy[0].lower()}{remedy[1:]}")

    # (e) several Linux guests, one conventionally named.
    for cand in free:
        if name_matches_broker(cand.get("name"), DEFAULT_BROKER_VM_NAME):
            return cand

    # (f) genuinely ambiguous, so name them rather than naming a VM that does not exist.
    names = ", ".join(sorted(_label(c) for c in free))
    raise BrokerError(
        f"{len(free)} VMs in this environment could be the control node ({names}) and nothing "
        f"distinguishes them. {remedy}")


def select_broker_vm(db: Session, env: PovEnvironment, *, infer: bool = True,
                     platform_vm_id: str = "") -> PovEnvironmentVM:
    """The VM that will run the agent, or a refusal naming what was actually found.

    The rule is :func:`resolve_broker_candidate`; this adapts the POV's own rows to it
    and maps the answer back. ``guest_os`` rather than ``os_family`` is what makes an
    operator's OS override reach the broker.

    ``infer=False`` restricts this to an exact name match with no inference at all. That
    is the cloud read-back's contract: there the dashboard has just CREATED the broker,
    and a cloud POV whose workload guests are Linux would otherwise bind
    ``broker_vm_id`` to a workload guest -- silently, and then persist it.

    ``platform_vm_id`` short-circuits to the VM the platform said it just built, which is
    strictly better than any name match and is what the cloud path passes.
    """
    rows = (db.query(PovEnvironmentVM)
              .filter(PovEnvironmentVM.environment_id == env.id).all())
    by_id = {r.platform_vm_id: r for r in rows}
    cands = [{"id": r.platform_vm_id, "name": r.name or "", "os_family": r.guest_os}
             for r in rows]
    chosen = resolve_broker_candidate(
        cands,
        # With inference off the conventional name is a requirement again, so the
        # defaulted reader is the right one there and only there.
        typed_name=(stored_broker_vm_name(env) if infer else broker_vm_name(env)),
        claimed_names=(claimed_vm_names(env) if infer else ()),
        # The id the caller was GIVEN outranks everything; the id this POV happens to
        # have brokered before only outranks a guess. Two different claims, so two
        # different rungs -- collapsing them would let a stale broker_vm_id swallow the
        # `name VM` remedy.
        pinned_id=str(platform_vm_id or "").strip(),
        sticky_id=((env.broker_vm_id or "") if infer else ""))
    return by_id[chosen["id"]]


def _broker_targets(db: Session, env: PovEnvironment) -> list[str]:
    """The POV's own VM addresses, as /32s.

    A per-VM allow-list rather than the environment's subnet. The subnet is what the
    platform hands out and it is bigger than the POV — on a shared lab network it can
    contain somebody else's environment entirely. Listing the addresses we read back
    grants exactly this POV and nothing adjacent to it.
    """
    rows = (db.query(PovEnvironmentVM)
              .filter(PovEnvironmentVM.environment_id == env.id).all())
    return sorted({(r.private_ip or "").strip() for r in rows if (r.private_ip or "").strip()})


# ── rendering ────────────────────────────────────────────────────────────────

def render_policy(targets: list[str], ansible_targets: list[str] | None = None,
                  ansible_ssh_targets: list[str] | None = None) -> str:
    """The agent's ``policy.yaml``, generated from this POV's addresses.

    Generated files here are held to a higher bar than usual because **this file fails
    closed**: the agent refuses to start on anything it cannot read as a target, and an
    agent that will not start is indistinguishable from a network fault from the
    dashboard's side. So the shape is boring on purpose — inline lists, no quoting
    decisions, no user-supplied text anywhere in it.
    """
    ports = ", ".join(str(p) for p in BROKER_PORTS)
    ansible_targets = [t for t in (ansible_targets or []) if t]
    # Kept as a SEPARATE list rather than merged, because the two carry different ports:
    # a Windows guest is granted WinRM and a Linux one is granted SSH. Merging them and
    # emitting the union would hand every host in the POV both, which is a widening
    # nobody asked for on the day somebody adds a third kind of target.
    ansible_ssh_targets = [t for t in (ansible_ssh_targets or [])
                           if t and t not in ansible_targets]
    # ASCII only, deliberately. This file is written by a shell heredoc on a guest whose
    # locale nobody chose and parsed by an agent that already has to be told about UTF-16
    # and BOMs; a non-ASCII byte in a comment is a needless way to find that out.
    lines = [
        "# Generated by the dashboard for a POV control node agent.",
        "# Regenerated on every control node run - edit the POV, not this file.",
        "targets:",
    ]
    for ip in targets:
        lines.append(f"  - cidr: {ip}/32")
        lines.append(f"    ports: [{ports}]")
    lines += [
        "deny:",
        # The metadata service the bootstrap itself reads. The agent has no business
        # reaching it, and on every cloud this range is the credential endpoint.
        "  - 169.254.0.0/16",
        "job_types:",
    ]
    lines += [f"  - {t}" for t in BROKER_JOB_TYPES]
    lines += [
        "limits:",
        f"  max_hosts: {max(len(targets), 1)}",
        # The Gateway this POV's broker may run. See GATEWAY_IMAGE for why a generated
        # policy still carries a block whose whole point elsewhere is that the customer
        # wrote it.
        "gateway:",
        "  enabled: true",
        f"  image: {GATEWAY_IMAGE}",
        "  privileged: true",
    ]

    # Config Management, for the Resource Broker install (WinRM) and the Entitle agent
    # install (SSH). Its own target list, NOT the discovery one above — widening a port
    # probe must never widen what may have a playbook applied to it as root. An empty list
    # means "nothing may be configured", which is the correct fail-closed reading and what
    # the agent does with it.
    lines.append("ansible:")
    if ansible_targets or ansible_ssh_targets:
        ansible_ports = ", ".join(str(p) for p in ANSIBLE_PORTS)
        ssh_ports = ", ".join(str(p) for p in ANSIBLE_SSH_PORTS)
        lines += [
            "  enabled: true",
            f"  vm_image: {ANSIBLE_VM_IMAGE}",
            "  targets:",
        ]
        for ip in ansible_targets:
            lines.append(f"    - cidr: {ip}/32")
            lines.append(f"      ports: [{ansible_ports}]")
        for ip in ansible_ssh_targets:
            lines.append(f"    - cidr: {ip}/32")
            lines.append(f"      ports: [{ssh_ports}]")
    else:
        # Rendered disabled rather than omitted, so an operator reading the file on the
        # broker VM sees that the feature exists and that this POV has no guest for it —
        # rather than wondering whether the dashboard forgot.
        lines += [
            "  enabled: false",
            "  # No VM in this POV may have a playbook applied to it.",
        ]

    lines.append("")
    return "\n".join(lines)


_SPIRE_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$|^[0-9A-Fa-f:]{2,45}$")
_SPIRE_TD = re.compile(r"^[a-z0-9.-]{1,255}$")
_SPIRE_TOKEN = re.compile(r"^[A-Za-z0-9-]{8,128}$")
_PEM_LINE = re.compile(r"^(-----(BEGIN|END) CERTIFICATE-----|[A-Za-z0-9+/=]{1,76})$")


def check_spire_offer(spire: dict) -> dict:
    """The SPIRE values the bootstrap writes, or ValueError naming the one that is unsafe.

    Every one of them lands in a root shell script and in a SPIRE config on a guest, so
    each is held to the shape it can legitimately have rather than escaped: an address, a
    trust domain, a join token and a PEM bundle never need a quote, a brace or a newline
    out of place, and a value that has one is a misconfiguration to report, not to quote.
    """
    out = {
        "server_address": str(spire.get("server_address") or "").strip(),
        "trust_domain": str(spire.get("trust_domain") or "").strip(),
        "join_token": str(spire.get("join_token") or "").strip(),
        "bootstrap_pem": str(spire.get("bootstrap_pem") or "").strip(),
    }
    try:
        out["server_port"] = int(spire.get("server_port") or 0)
    except (TypeError, ValueError):
        out["server_port"] = 0
    if not _SPIRE_HOST.match(out["server_address"]):
        raise ValueError("the SPIRE server address for agents is empty or not a host name "
                         "(Settings → Remote Agents, or the agent gateway's Public base URL)")
    if not 0 < out["server_port"] < 65536:
        raise ValueError("the SPIRE server port is not a port number")
    if not _SPIRE_TD.match(out["trust_domain"]):
        raise ValueError("the SPIRE trust domain is not a bare lower-case name")
    if not _SPIRE_TOKEN.match(out["join_token"]):
        raise ValueError("the SPIRE server returned a join token of an unexpected shape")
    lines = out["bootstrap_pem"].splitlines()
    if (not lines or "BEGIN CERTIFICATE" not in lines[0]
            or not all(_PEM_LINE.match(ln.strip()) for ln in lines)):
        raise ValueError("the SPIRE trust bundle is not a PEM certificate")
    return out


def _render_spire_block(spire: dict | None, dashboard_url: str) -> str:
    """The shell that brings up SPIRE on the guest and decides how the agent signs in.

    Always emitted, because its first half is cleanup: a broker re-brokered after SPIRE
    was switched off must lose the sidecars it no longer uses. Its result is
    ``AGENT_AUTH``, ``spiffe`` or ``ed25519``, which the agent's ``docker run`` reads.

    **It falls back, and that is the design rather than a concession.** SPIRE needs mTLS
    to the server's port, untouched; a customer network that blocks the port or inspects
    TLS stops it, and a POV whose broker never comes up is worth less than one on an
    Ed25519 key. So the guest waits ``SPIRE_WAIT_SECONDS`` for an SVID and, without one,
    removes the sidecars and redeems the enrolment code that was written beside them. The
    dashboard sees which happened on the agent row (``auth_mode``) and says so.

    The containers mirror examples/remote-agent/docker-compose.spire.yml exactly — same
    users, the same read-only, capability-free settings, spiffe-helper in spire-agent's PID
    namespace so the unix workload attestor can read its uid (10001, the entry's selector).
    Host directories replace the overlay's init container: this script already runs as
    root, so it can set the ownership itself.
    """
    cleanup = (
        "# SPIRE: remove whatever a previous run left, so a re-broker attests from nothing --\n"
        "# the same reason the agent's state volume goes below.\n"
        'AGENT_AUTH="ed25519"\n'
        f"docker rm -f {' '.join(SPIRE_CONTAINERS)} >/dev/null 2>&1 || true\n"
        f"docker volume rm {GUEST_JWT_VOLUME} >/dev/null 2>&1 || true\n"
        f'rm -rf "{GUEST_SPIRE_DIR}"\n')
    if not spire:
        return cleanup + "\n"
    s = check_spire_offer(spire)
    audience = dashboard_url.rstrip("/") + "/api/agent/attest"
    agent_conf = (
        "agent {\n"
        '    data_dir          = "/opt/spire/data"\n'
        '    log_level         = "INFO"\n'
        f'    server_address    = "{s["server_address"]}"\n'
        f'    server_port       = "{s["server_port"]}"\n'
        f'    trust_domain      = "{s["trust_domain"]}"\n'
        '    trust_bundle_path = "/opt/spire/conf/bootstrap.crt"\n'
        f'    join_token        = "{s["join_token"]}"\n'
        '    socket_path       = "/run/spire/sockets/agent.sock"\n'
        "}\n"
        "plugins {\n"
        '    NodeAttestor "join_token" { plugin_data {} }\n'
        '    KeyManager "disk" { plugin_data { directory = "/opt/spire/data" } }\n'
        '    WorkloadAttestor "unix" { plugin_data {} }\n'
        "}\n")
    helper_conf = (
        'agent_address = "/run/spire/sockets/agent.sock"\n'
        'cert_dir = "/run/spiffe/jwt"\n'
        "daemon_mode = true\n"
        f'jwt_svids = [{{jwt_audience="{audience}", jwt_svid_file_name="attest.jwt"}}]\n'
        # Owner-writable: spiffe-helper renews with os.WriteFile, which applies the mode
        # only on create, so 0400 is written once and every renewal is refused.
        "jwt_svid_file_mode = 0600\n")
    d = GUEST_SPIRE_DIR
    hardening = ("--read-only --cap-drop ALL --security-opt no-new-privileges:true")
    return cleanup + f"""
# SPIRE: attest this agent with a short-lived JWT-SVID, its key held only in memory.
# Server {s["server_address"]}:{s["server_port"]}, trust domain {s["trust_domain"]}.
mkdir -p "{d}/data" "{d}/sock"
cat > "{d}/agent.conf" <<'DASHBOARD_SPIRE_AGENT_EOF'
{agent_conf}DASHBOARD_SPIRE_AGENT_EOF
cat > "{d}/bootstrap.crt" <<'DASHBOARD_SPIRE_BUNDLE_EOF'
{s["bootstrap_pem"]}
DASHBOARD_SPIRE_BUNDLE_EOF
cat > "{d}/helper.conf" <<'DASHBOARD_SPIRE_HELPER_EOF'
{helper_conf}DASHBOARD_SPIRE_HELPER_EOF
chmod 0644 "{d}/agent.conf" "{d}/bootstrap.crt"
# spire-agent runs as 1000, spiffe-helper as the agent's 10001.
chown 1000:1000 "{d}/data" "{d}/sock" && chmod 0700 "{d}/data" && chmod 0755 "{d}/sock"
chown 10001:10001 "{d}/helper.conf" && chmod 0400 "{d}/helper.conf"

if docker volume create --driver local --opt type=tmpfs --opt device=tmpfs \\
     --opt o=size=1m,uid=10001,gid=10001,mode=0700 {GUEST_JWT_VOLUME} >/dev/null \\
   && docker run -d --name dashboard-agent-spire --restart unless-stopped \\
        --user 1000:1000 {hardening} --pids-limit 128 \\
        --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \\
        -v "{d}/agent.conf:/opt/spire/conf/agent.conf:ro,Z" \\
        -v "{d}/bootstrap.crt:/opt/spire/conf/bootstrap.crt:ro,Z" \\
        -v "{d}/data:/opt/spire/data:Z" \\
        -v "{d}/sock:/run/spire/sockets:z" \\
        {SPIRE_AGENT_IMAGE} -config /opt/spire/conf/agent.conf >/dev/null \\
   && docker run -d --name dashboard-agent-spiffe-helper --restart unless-stopped \\
        --user 10001:10001 {hardening} --pids-limit 64 \\
        --pid container:dashboard-agent-spire \\
        -v "{d}/helper.conf:/etc/spiffe-helper/helper.conf:ro,Z" \\
        -v "{d}/sock:/run/spire/sockets:ro,z" \\
        -v {GUEST_JWT_VOLUME}:/run/spiffe/jwt \\
        {SPIFFE_HELPER_IMAGE} -config /etc/spiffe-helper/helper.conf >/dev/null; then
  # The token is in a volume only a container can see; the agent's own image has a shell.
  WAITED=0
  while [ "$WAITED" -lt {SPIRE_WAIT_SECONDS} ]; do
    if docker run --rm --entrypoint sh --user 10001:10001 \\
         -v {GUEST_JWT_VOLUME}:/run/spiffe/jwt:ro {AGENT_IMAGE} \\
         -c 'test -s /run/spiffe/jwt/attest.jwt' >/dev/null 2>&1; then
      AGENT_AUTH="spiffe"
      break
    fi
    sleep 5
    WAITED=$((WAITED + 5))
  done
fi
if [ "$AGENT_AUTH" != "spiffe" ]; then
  echo "WARNING: SPIRE issued no SVID within {SPIRE_WAIT_SECONDS}s. Check that this VM reaches {s["server_address"]}:{s["server_port"]} with no TLS inspection in between. Falling back to the enrolment code." >&2
  docker logs --tail 20 dashboard-agent-spire >&2 2>&1 || true
  docker rm -f {' '.join(SPIRE_CONTAINERS)} >/dev/null 2>&1 || true
  docker volume rm {GUEST_JWT_VOLUME} >/dev/null 2>&1 || true
fi

"""


# What a platform accepts as the bootstrap, when its capability row does not say. Skytap
# refuses user_data over 16384 characters with a 422, and EC2's user data is capped at
# 16 KB before encoding, so the strictest known limit is the default.
DEFAULT_BOOTSTRAP_MAX_CHARS = 16384
_HEREDOC_START = re.compile(r"<<-?\s*'?\"?([A-Za-z_][A-Za-z0-9_]*)'?\"?\s*$")
_GZ_EOF = "DASHBOARD_BOOTSTRAP_GZ_EOF"


def _compact(script: str) -> str:
    """The script without its comment lines and runs of blank lines.

    Heredoc bodies are copied verbatim: they are files (the policy, SPIRE's configs, the
    trust bundle), and a `#` line inside one is data. The shebang and both markers stay,
    because the guest runner matches the markers.
    """
    out: list[str] = []
    terminator = None
    for line in script.splitlines():
        if terminator is not None:
            out.append(line)
            if line == terminator:
                terminator = None
            continue
        bare = line.strip()
        if (bare.startswith("#") and not line.startswith("#!")
                and line not in (BOOTSTRAP_BEGIN, BOOTSTRAP_END)):
            continue
        if not bare and out and not out[-1].strip():
            continue
        out.append(line)
        m = _HEREDOC_START.search(line)
        if m:
            terminator = m.group(1)
    return "\n".join(out) + "\n"


def _compressed(script: str) -> str:
    """The script gzipped and base64-encoded, between the same two markers, with the
    decoder that runs it.

    Decoded to a file and run from there rather than piped into ``sh``: a shell reading its
    script from a pipe shares that pipe as stdin with every command it runs, and one that
    reads stdin would eat the rest of the script. ``base64`` and ``gzip`` are in coreutils
    and BusyBox alike, which covers every guest that can run the agent's Docker.
    """
    import base64
    import gzip
    body = base64.encodebytes(gzip.compress(script.encode("utf-8"), mtime=0)).decode("ascii")
    return (
        "#!/bin/sh\n"
        f"{BOOTSTRAP_BEGIN}\n"
        "# Compressed to fit the platform's user-data limit. The plain script is the\n"
        "# decoded payload below; it runs exactly as if it had been sent as-is.\n"
        "set -eu\n"
        # mktemp makes the file 0600. No umask here: the script it runs inherits one, and
        # it sets its own where a mode matters.
        "T=$(mktemp)\n"
        "trap 'rm -f \"$T\"' EXIT\n"
        f"base64 -d <<'{_GZ_EOF}' | gzip -dc > \"$T\"\n"
        f"{body}{_GZ_EOF}\n"
        "sh \"$T\"\n"
        f"{BOOTSTRAP_END}\n")


def fit_bootstrap(script: str, limit: int) -> tuple[str, str]:
    """``(payload, how)``: the script as it will fit the platform, least changed first.

    ``how`` is ``"plain"`` (unchanged), ``"compact"`` (comments dropped) or
    ``"compressed"``. Raises BrokerError when not even the compressed form fits, naming the
    size, rather than letting the platform refuse it with a 422 that names neither.

    Least changed first because the plain script is the one a human can read on the guest
    when something goes wrong; it is only given up when the platform will not take it.
    """
    if len(script) <= limit:
        return script, "plain"
    compact = _compact(script)
    if len(compact) <= limit:
        return compact, "compact"
    packed = _compressed(compact)
    if len(packed) <= limit:
        return packed, "compressed"
    raise BrokerError(
        f"the control node's bootstrap is {len(packed)} characters even compressed, and this "
        f"platform accepts {limit}. The policy grows with the POV's VMs; a POV this size "
        f"needs a platform with a larger user-data limit.")


def render_bootstrap(*, env_name: str, dashboard_url: str, enroll_code: str,
                     policy_yaml: str, images: tuple = (),
                     now: datetime | None = None, spire: dict | None = None) -> str:
    """The script the guest runs, between its two markers.

    A ``/bin/sh`` script rather than cloud-init: Skytap's mechanism hands bytes to the
    guest and the guest decides what they are, so the simplest thing that every template
    in a POV — Linux with Docker — can run is a shell script.

    ``umask 022`` before the code file is not tidiness. The agent container runs as uid
    10001, and a mode 0600 file owned by root is unreadable inside it; the agent says so
    and exits rather than enrolling. A single-use fifteen-minute secret readable on a
    machine inside the POV is the trade the Agents page already makes for the same reason.

    **The Docker socket is mounted, and that is root on the broker VM.** The Agents page
    deliberately does not emit that mount — applying it there is a separate, considered
    act by the operator, because their agent host is theirs. A POV broker is not: the
    dashboard created that VM from a template for this POV, and the Gateway it has to run
    is a sibling container, so the socket is a prerequisite of the machine's only job. The
    line is worth seeing rather than inferring, which is why it is here and commented
    rather than folded into a shared flag block.

    **And mounting it is not the same as being able to open it.** The socket is typically
    ``srw-rw---- root docker``; the container runs as uid 10001 with no supplementary
    groups, so it gets ``EACCES`` on a socket that is sitting right there. Every POV
    Gateway install failed on this — the agent's refusal reads "the sibling runner needs it
    mounted", which sends you to look at the one thing that was never wrong. So the
    bootstrap resolves the socket's own group on the guest and passes ``--group-add``.

    Adding a group is not a loosening: the socket already *is* root on this host, and that
    is stated above and in the docs. What changes is only whether the process the dashboard
    put there on purpose can use the thing the dashboard mounted on purpose.

    **And before any of that, there has to BE a Docker.** The script installs a container
    runtime if the guest has none — ``pov_template_builder.render_docker_install``, the same
    block a template bake runs, reached rather than copied. A base template with a perfect
    metadata runner and no ``docker`` was the single most common way a POV failed, and it
    failed in the worst possible shape: the payload runs as far as the final ``docker run``,
    ``set -e`` kills it there, the runner re-reads ``user_data`` twenty seconds later and
    dies in the same place forever, and the dashboard sees nothing at all because the thing
    that would have told it is the agent that never started.

    Two properties of putting it *here* rather than only in the bake:

    * **It is idempotent and skips a guest that already has one**, so every POV whose broker
      is healthy — every cloud POV, whose image carries Docker — pays nothing for it.
    * **It reaches a package repository, so it is the one line here that can fail**, and it
      fails as silently as the ``docker run`` it replaces. That is why it ships with
      ``runtime_gap_for_template``: the install being attempted is only half an answer, and
      the other half is the job saying which template baked without a runtime.

    It does NOT make the bake redundant. A runtime installed by the bootstrap lands on one
    POV; the same install at bake time lands on every POV made from the template, before
    anybody is waiting fourteen minutes for it.

    **And naming an image is not the same as having it.** ``images`` are pulled here, before
    the agent that needs them starts. The agent will not pull one for itself and says so in
    three separate refusals -- a rule that is right where it was written: on a customer's
    own agent host a pull is a network fetch of executable content, so ``policy.yaml``
    grants and the operator provisions.

    A POV broker inverts every clause of that. The dashboard built the VM, wrote the policy
    and **chose the image names**, so leaving the fetch to a human who was never told is how
    ``agent_gateway`` failed with "not present on this host" on a machine nobody had ever
    logged into. The pull therefore happens in the script the dashboard already writes,
    under the same argument as the socket mount above -- and the agent's own promise stays
    literally true, because the agent still is not the thing doing it.

    Two properties it depends on, both deliberate:

    * **Before the** ``docker rm -f`` **below.** A slow registry then costs a re-broker
      nothing: the agent already running keeps serving until its replacement is ready.
    * **Non-fatal.** ``set -eu`` is in force, so an unguarded pull failure would abort the
      bootstrap and leave the POV with no agent at all. A POV with an enrolled agent and one
      missing image refuses one job and names it, which is much the better failure.

    **``AGENT_IMAGE`` is pulled too, and it is the one that must never be left out.**
    ``docker run`` fetches an image only when it is *absent* locally, so the tag below is
    resolved exactly once in a broker VM's life -- at first bootstrap. Every re-broker after
    that is ``docker rm -f`` and ``docker run`` against a ``:latest`` that has been sitting
    in the guest's image store since the day the VM was built, and a broker is a long-lived
    VM. The effect is that **an agent fix can never reach a POV that already has an agent**:
    it merges, it is tagged, ``:latest`` moves, the operator re-brokers to pick it up, and
    the guest runs the same old binary and fails in the same old way. Not hypothetical --
    the ``NanoCpus`` clamp shipped in the agent and the very next Config Management run
    still returned "Range of CPUs is from 0.01 to 1.00", because the fixed agent was never
    fetched. A re-broker is the moment the dashboard is entitled to refresh it, so it does,
    under the same argument as everything else here: this VM is the dashboard's.
    """
    # The agent's own image leads, and is why this loop is now unconditional: a POV with
    # no guest opted in for configuration still needs its agent refreshed. The list can
    # therefore never be empty, which is also what settles the empty-`for` syntax error
    # that the previous guard existed for.
    pulls = (
        "# Every image this broker runs, fetched before the agent that needs them. The\n"
        "# AGENT refuses to pull -- right on a customer's own host, but this VM is the\n"
        "# dashboard's: it built it, wrote the policy and chose these names. Its OWN\n"
        "# image is in the list because `docker run` pulls only what is ABSENT, so\n"
        "# without this a re-broker re-runs the copy cached at first boot, forever.\n"
        "# A failure here warns and carries on: one refused job beats no agent at all.\n"
        "for IMAGE in " + " ".join((AGENT_IMAGE,) + tuple(images)
                                   + ((SPIRE_AGENT_IMAGE, SPIFFE_HELPER_IMAGE) if spire else ())
                                   ) + "; do\n"
        "  docker pull \"$IMAGE\" || echo \"WARNING: could not pull $IMAGE\"\n"
        "done\n\n")

    # The container runtime, before anything that needs one. Imported in the function body:
    # `pov_template_builder` imports THIS module at top level for the marker constants and
    # the broker resolver, so a module-scope import here closes a real cycle -- the same
    # reason `ensure_broker` imports its three siblings locally.
    #
    # The builder's renderer verbatim, not a second copy: what a bake installs and what a
    # POV installs when the bake never happened have to be the same runtime, or the feature
    # is tested against one and shipped on the other. Only the enabled-at-boot gate comes
    # off -- see `render_docker_install` for why that gate is about a template and not
    # about this guest.
    from . import pov_template_builder
    runtime = pov_template_builder.render_docker_install(require_enabled_at_boot=False)

    stamp = (now or utcnow()).strftime("%Y-%m-%d %H:%M:%SZ")
    # The name reaches a shell comment. `api/pov` already constrains it to a slug, but a
    # newline here would end the comment and put whatever followed on its own line, so it
    # is flattened at the point of use rather than trusted from two callers away.
    env_name = " ".join(str(env_name or "unnamed").split())
    # After the pulls (it needs the images) and after the agent's own `docker rm`, so the
    # old agent is gone before a new identity is set up for its replacement.
    spire_block = _render_spire_block(spire, dashboard_url)
    return f"""#!/bin/sh
{BOOTSTRAP_BEGIN}
# POV environment: {env_name}
# Generated {stamp} by the dashboard. Re-injection replaces this whole script.
set -eu

STATE={GUEST_STATE_DIR}
mkdir -p "$STATE"

cat > "$STATE/policy.yaml" <<'DASHBOARD_AGENT_POLICY_EOF'
{policy_yaml}DASHBOARD_AGENT_POLICY_EOF
chmod 0644 "$STATE/policy.yaml"

# 022, not 077: the container runs as uid 10001 and cannot read a root-owned 0600 file.
( umask 022 && printf '%s' '{enroll_code}' > "$STATE/enroll-code" )

{runtime}
{pulls}# Replace any previous agent AND its state volume. The volume holds the identity written
# at first enrolment; leaving it means the new code is never redeemed, the container signs
# with a key the dashboard has already cleared, and every poll 401s.
docker rm -f dashboard-agent >/dev/null 2>&1 || true
docker volume rm {GUEST_STATE_VOLUME} >/dev/null 2>&1 || true

{spire_block}# How the agent signs in, decided by the SPIRE block above. With an SVID the code is
# neither mounted nor kept: attesting burns it on the dashboard anyway.
if [ "$AGENT_AUTH" = "spiffe" ]; then
  rm -f "$STATE/enroll-code"
  AGENT_AUTH_ARGS="-e AGENT_SPIFFE_JWT_FILE=/run/spiffe/jwt/attest.jwt -v {GUEST_JWT_VOLUME}:/run/spiffe/jwt:ro"
else
  AGENT_AUTH_ARGS="-v $STATE/enroll-code:/etc/dashboard-agent/enroll-code:ro,Z -e AGENT_ENROLLMENT_CODE_FILE=/etc/dashboard-agent/enroll-code"
fi

# The container runs as uid 10001 and the socket below is typically 0660 root:docker, so
# uid 10001 cannot open it without the socket's GROUP. Mounting it is not enough: the agent
# gets EACCES, and the refusal it prints says "the sibling runner needs it mounted", which
# is the wrong thing to go and check.
#
# Resolved on the guest, not here, because the dashboard cannot know a GID it never chose:
# it varies by distro and by install order. Unset when there is no socket (or no `stat`),
# so the expansion below vanishes rather than passing an empty argument.
DOCKER_GROUP=""
DOCKER_SOCK_GID=$(stat -c '%g' {GUEST_DOCKER_SOCKET} 2>/dev/null || true)
if [ -n "$DOCKER_SOCK_GID" ]; then
  DOCKER_GROUP="--group-add $DOCKER_SOCK_GID"
fi

# $DOCKER_GROUP and $AGENT_AUTH_ARGS are deliberately unquoted: each is words or none,
# and no word in either has a space ($STATE is a constant path).
docker run -d --name dashboard-agent --restart unless-stopped \\
  --read-only --cap-drop ALL --security-opt no-new-privileges:true \\
  --user 10001:10001 --tmpfs /tmp \\
  $DOCKER_GROUP \\
  $AGENT_AUTH_ARGS \\
  -v {GUEST_DOCKER_SOCKET}:{GUEST_DOCKER_SOCKET} \\
  -v {GUEST_STATE_VOLUME}:/var/lib/dashboard-agent \\
  -v "$STATE/policy.yaml:/etc/dashboard-agent/policy.yaml:ro,Z" \\
  -e DASHBOARD_URL="{dashboard_url}" \\
  {AGENT_IMAGE}
{BOOTSTRAP_END}
"""


# ── the agent row ────────────────────────────────────────────────────────────

def _agent_row(db: Session, env: PovEnvironment) -> RemoteAgent | None:
    """This POV's agent, by id first and by derived name second.

    The name lookup is recovery, not discovery: if a crash between the mint and the commit
    left a row the environment does not point at, the next run must adopt it. Minting a
    second row for the same POV would leave one of them enrolled and unreferenced, and
    teardown would revoke the other.
    """
    if env.broker_agent_id:
        row = db.query(RemoteAgent).filter(RemoteAgent.id == env.broker_agent_id).first()
        if row is not None:
            return row
        logger.warning("POV %s: broker_agent_id %s points at no row; falling back to the "
                       "derived name", env.id, env.broker_agent_id)
    return db.query(RemoteAgent).filter(RemoteAgent.name == agent_name(env)).first()


def _mint_code(db: Session, env: PovEnvironment) -> tuple[RemoteAgent, str]:
    """An agent row and a fresh single-use code, whether or not one already exists."""
    existing = _agent_row(db, env)
    if existing is not None:
        # Re-issue rather than create. This also clears the public key, which is what
        # makes the guest's `docker volume rm` and this call two halves of one operation:
        # a new identity on both sides or neither.
        existing.is_active = True
        code = agent_service.reissue_enroll_code(db, existing)
        logger.info("POV %s: re-issued an enrolment code for broker agent %s",
                    env.id, existing.name)
        return existing, code

    agent, code = agent_service.create_agent(
        db, name=agent_name(env),
        site=(env.workgroup or "")[:64],
        description=f"POV control node for {env.name} on {env.platform}",
        created_by=env.created_by or "")
    return agent, code


async def _wait_for_enrolment(db: Session, agent_id: str, *, timeout_s: float,
                              sleep=None, on_tick=None) -> bool:
    """Poll until the agent redeems its code. True if it did.

    Polls the database, not the platform: enrolment is something the agent does *to us*,
    so the only authoritative signal is our own row gaining a public key. Asking the
    platform whether the guest looks healthy would be inferring the thing we can observe
    directly.

    ``on_tick`` is not decoration. This is fourteen minutes with nothing else to say, and
    a job row that stops heartbeating for that long is one ``reconcile_stale_jobs`` would
    fail out from under a live wait on the next app restart — quite apart from a Live
    Output that looks hung to whoever is watching it.
    """
    sleep = sleep or asyncio.sleep
    # Poll count rather than a wall clock, so an injected sleep makes this deterministic
    # in a test instead of spinning against a clock that never moves.
    attempts = max(1, int(timeout_s // ENROLL_POLL_SECONDS))
    for attempt in range(attempts):
        # End the current transaction before each read. The agent enrols against a
        # DIFFERENT process, so this session must take a fresh snapshot; an open read
        # transaction would happily return the same pre-enrolment row for the whole wait
        # and time out on data that changed ten minutes ago.
        db.rollback()
        row = db.query(RemoteAgent).filter(RemoteAgent.id == agent_id).first()
        if row is not None and row.public_key:
            return True
        if attempt + 1 >= attempts:
            break
        if on_tick is not None:
            on_tick(attempt + 1, attempts)
        await sleep(ENROLL_POLL_SECONDS)
    return False


# ── the orchestration ────────────────────────────────────────────────────────

def record_broker_error(db: Session, env: PovEnvironment, message: str) -> None:
    """Record why the broker is not there, on the row the POV page reads.

    Not ``env.error_message``: that field means "this environment is broken", and a POV
    whose broker did not come up is running, billing and reapable. Conflating them would
    put a red row in front of an operator whose environment is fine.
    """
    meta = env.metadata_dict
    if message:
        meta["broker_error"] = message
    else:
        meta.pop("broker_error", None)
    env.metadata_dict = meta
    db.commit()


def spire_offer(db: Session, agent: RemoteAgent) -> tuple[dict | None, str]:
    """``(spire facts for the bootstrap, or None; a line for the job log)``. Never raises.

    Offered whenever this install lets agents attest through SPIRE and its server answers:
    the same four steps as **Migrate to SPIRE** (``dashboard_spire.migrate_agent``), so a
    broker and a hand-migrated agent are bound identically. Anything that stops it leaves
    the broker on its enrolment code, which is how every broker enrolled before this —
    a broker that comes up on Ed25519 is worth more than one that does not come up.

    In the worker on a managed runtime this reaches the SPIRE sidecar through the database
    relay (services/spire_relay), like every other dashboard_spire call made there.
    """
    if not config_service.get_bool("spire_attest_enabled"):
        return None, "SPIRE attestation is off on this dashboard; the control node enrols with an Ed25519 key."
    from . import dashboard_spire
    try:
        # Where the guest will dial, checked BEFORE a token is minted for it. A blank
        # "SPIRE server address for agents" means the agent gateway's host on 8081, and on
        # a Container Apps environment shared with another dashboard that is the OTHER
        # dashboard's server: the guest's TLS fails, it waits two minutes and falls back,
        # and nothing anywhere says why. Found live.
        td = _trust_domain_with_grace(dashboard_spire)
        host, port = dashboard_spire.server_address(), dashboard_spire.server_port()
        verdict, why = dashboard_spire.check_agent_address(
            host, port, dashboard_spire.bootstrap_bundle_pem(), td)
        if verdict is False:
            raise ValueError(why)
        out = dashboard_spire.migrate_agent(db, agent)
        facts = check_spire_offer({
            "server_address": dashboard_spire.server_address(),
            "server_port": dashboard_spire.server_port(),
            "trust_domain": out["trust_domain"],
            "join_token": out["join_token"],
            "bootstrap_pem": out["bootstrap_pem"],
        })
    except (dashboard_spire.DashboardSpireError, agent_service.AgentError, ValueError) as exc:
        logger.warning("POV broker %s: SPIRE not offered: %s", agent.name, exc)
        # A half-done offer must not leave a binding that nothing on the guest will use.
        try:
            agent_service.bind_spiffe_id(db, agent, "")
        except Exception:  # noqa: BLE001 -- best effort; the code path works either way
            db.rollback()
        return None, f"SPIRE not offered ({exc}); the control node enrols with an Ed25519 key."
    checked = ("verified: it presents this dashboard's SPIRE server" if verdict
               else f"not verifiable from the dashboard: {why}")
    return facts, (f"SPIRE join token minted for {out['spiffe_id']} "
                   f"({facts['server_address']}:{facts['server_port']}, {checked}); the "
                   f"control node falls back to its enrolment code if the guest cannot reach the "
                   f"server.")


# How long a Broker run waits for an app process to start serving the SPIRE relay. The
# relay only serves while a SPIRE feature is on, so the first run after ticking one can
# arrive before any app process has noticed (it re-checks every few seconds).
RELAY_GRACE_S = 10.0


def _trust_domain_with_grace(dashboard_spire, sleep=time.sleep) -> str:
    """The server's trust domain, giving a relay that is just starting one more chance."""
    try:
        return dashboard_spire.trust_domain()
    except dashboard_spire.DashboardSpireError as exc:
        if "relay is not running" not in str(exc):
            raise
        sleep(RELAY_GRACE_S)
        return dashboard_spire.trust_domain()


async def ensure_broker(db: Session, env: PovEnvironment, *, job_id: str = "",
                        sleep=None) -> str:
    """Install and enrol this POV's broker agent. Returns a one-line summary.

    Re-runnable from any point, which is the property that matters: every failure below
    leaves the row in a state this function can be called on again.

    Raises :class:`BrokerError` with the remedy in the message. The provision job treats
    that as a warning — the environment is up either way — while the standalone broker job
    treats it as a failure, because there the operator asked for exactly this.
    """
    def _progress(pct: int, msg: str) -> None:
        if job_id:
            job_service.update_progress(db, job_id, pct, msg)

    mechanism = lab_platforms.capabilities(env.platform).get("bootstrap_injection")
    if mechanism not in ("metadata", "cloud_init"):
        # Degrade by saying so. `remote_exec` is a real mechanism this slice has not
        # written, and `None` means the enrol has to be baked into the template — both are
        # answers, and neither is this code path silently doing nothing.
        how = mechanism or "no mechanism at all"
        raise BrokerError(
            f"{env.platform} delivers bootstrap data by {how}, and the control node install "
            f"here is written against the 'metadata' and 'cloud_init' mechanisms. This "
            f"platform needs its own control node path.")

    dashboard_url = dashboard_agent_url()
    if not dashboard_url:
        raise BrokerError(
            "this dashboard does not know its own public URL, so there is no address to "
            "give an agent inside the POV. Set Public base URL in Settings → Integrations "
            "→ Remote Agents to the hostname that serves /api/agent, then run this again.")
    if dashboard_url.startswith("http://"):
        raise BrokerError(
            f"the agent endpoint is {dashboard_url}. The agent refuses to sign over "
            f"plaintext, so a control node installed against it would never enrol. Terminate "
            f"TLS in front of the dashboard and correct Public base URL.")

    _progress(10, "Reading the environment's VMs…")
    # Re-read rather than trust what is on the row: the private IPs this writes into the
    # policy only exist once the VMs are running, and a POV that was suspended and resumed
    # can have new ones.
    from . import pov_env_service
    await pov_env_service.refresh_vms(db, env)

    # On `metadata` the broker VM already exists and is what we inject into. On
    # `cloud_init` it does NOT exist yet — and must not, because the policy about to be
    # rendered names the TARGETS' addresses, which only exist once the targets are up.
    # See pov_cloud_env.vm_specs for the ordering this imposes.
    # Inference lives here and only here: on `metadata` the broker VM already EXISTS and
    # has to be picked out of the template. See the `cloud_init` branch below for why the
    # read-back after a create must not share this behaviour.
    vm = select_broker_vm(db, env) if mechanism == "metadata" else None
    targets = _broker_targets(db, env)
    if not targets:
        raise BrokerError(
            "no VM in this environment reported a private address, so the control node's policy "
            "would grant nothing and the agent would refuse to start. Power the "
            "environment on and run this again.")

    _progress(35, f"Minting an enrolment code for {agent_name(env)}…")
    agent, code = _mint_code(db, env)
    # After the code, never before: re-issuing a code clears the row's SPIFFE binding, so
    # binding first would be undone a line later.
    # Off the event loop: on a managed runtime every call in it is a relayed one, and a
    # down relay costs a grace period plus timeouts that the worker's other jobs (and
    # their heartbeats) must not wait behind. The session is not shared meanwhile.
    spire, spire_note = await asyncio.to_thread(spire_offer, db, agent)
    _progress(40, spire_note)

    # The Config-Management grant is scoped to this POV's Windows guests — or to the one
    # named Resource Broker host, once somebody has named it — plus its Linux guests, or
    # the one named Entitle agent host, plus any guest an operator has opened up for a
    # guest step. Computed here rather than inside render_policy so that function stays
    # pure and testable.
    #
    # Imported in the function body, not at module scope: `pov_gateway` reaches this module
    # and `pov_guest_step` reaches `pov_gateway`, so a top-level import here would close a
    # real cycle. Same reason the other two are local.
    from . import (pov_entitle_agent, pov_guest_step, pov_resource_broker,
                   pov_windows_functional)
    # Unions, not replacements. The grants answer different questions — where the
    # Resource Broker goes, where the Entitle agent goes, which guests an SE wants to
    # configure, and (on AWS with a Password Safe tenant) every Windows guest that needs
    # the functional account — and a POV can want all of them at once. `render_policy`
    # de-duplicates the SSH list against the WinRM one, so an address in both keeps only
    # its WinRM ports.
    win_targets = sorted(set(pov_resource_broker.windows_targets(db, env))
                         | set(pov_guest_step.windows_targets(db, env))
                         | set(pov_windows_functional.windows_targets(db, env)))
    ssh_targets = sorted(set(pov_entitle_agent.linux_targets(db, env))
                         | set(pov_guest_step.linux_targets(db, env)))
    # Recorded so a guest named AFTER this render is refused with "press Broker" instead of
    # failing as a connection timeout twenty minutes later. What is stored is what was
    # written, which is why it happens here and not in the module that reads it.
    pov_guest_step.record_grant(db, env, win_targets + ssh_targets)
    # Exactly the images the policy above names, and no others. Computed here for the same
    # reason the target lists are: `render_policy` and `render_bootstrap` both stay pure,
    # and the two cannot drift into naming an image the other never fetched.
    #
    # The Gateway's is unconditional because its block is (`gateway.enabled: true`, always).
    # The Ansible one follows the same `if` `render_policy` uses, so a POV with no guest
    # opted in yet does not pay for an image it may never run -- and when a guest IS opted
    # in, that takes a re-broker anyway, which is this same code path.
    images = [GATEWAY_IMAGE]
    if win_targets or ssh_targets:
        images.append(ANSIBLE_VM_IMAGE)

    payload = render_bootstrap(
        env_name=env.name, dashboard_url=dashboard_url, enroll_code=code,
        policy_yaml=render_policy(targets, win_targets, ssh_targets),
        images=tuple(images), spire=spire)
    # Fitted before the platform sees it. Skytap answers an oversized user_data with a 422
    # that names neither the payload nor its size, and the policy grows with every VM.
    limit = int(lab_platforms.capabilities(env.platform).get("bootstrap_max_chars")
                or DEFAULT_BOOTSTRAP_MAX_CHARS)
    raw_len = len(payload)
    payload, how = fit_bootstrap(payload, limit)
    if how != "plain":
        _progress(45, f"The bootstrap is {raw_len} characters and {env.platform} accepts "
                      f"{limit}; sending it {how} ({len(payload)} characters).")

    mod = lab_platforms.adapter(env.platform)
    if mechanism == "metadata":
        _progress(50, f"Injecting the bootstrap onto {vm.name or vm.platform_vm_id}…")
        try:
            await mod.inject_bootstrap(env.platform_environment_id, vm.platform_vm_id,
                                       payload)
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(
                f"could not hand the bootstrap to {vm.name or vm.platform_vm_id}: "
                f"{exc}") from exc
    else:
        # cloud_init: the VM is CREATED with the payload. Any previous broker instance is
        # terminated first, which also destroys its agent state volume — so a re-broker is
        # clean by construction rather than by remembering to delete it.
        _progress(50, f"Building the control node {broker_vm_name(env)}…")
        try:
            created = await mod.create_broker_vm(env.platform_environment_id,
                                                 env.template_id, payload)
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(
                f"could not build the control node for {env.name}: {exc}") from exc
        # Read it back so the row below names a VM that exists, and so the POV page shows
        # it alongside the targets.
        await pov_env_service.refresh_vms(db, env)
        # NO inference here, deliberately, and this is the load-bearing half of that
        # distinction. The dashboard just BUILT this VM: the template designates its
        # broker by ROLE (pov_cloud_env.vm_specs) and the driver hands back the instance
        # it created, so there is a better answer than any guess. Inferring instead would
        # pick "the only Linux VM" out of rows that now include this POV's Linux WORKLOAD
        # guests, bind broker_vm_id to one of them, and persist it two lines below --
        # silently, because the bootstrap did reach the real broker via cloud-init and the
        # agent enrols perfectly. Everything that later excludes the broker would then
        # exclude the wrong machine.
        vm = select_broker_vm(db, env, infer=False,
                              platform_vm_id=str((created or {}).get("id") or ""))

    # Persist BEFORE the wait. A crash here must leave the next run re-issuing this row's
    # code rather than minting a second agent for the same POV.
    env.broker_vm_id = vm.platform_vm_id
    env.broker_agent_id = agent.id
    db.commit()

    _progress(65, "Waiting for the control node's agent to enrol…")

    def _tick(done: int, total: int) -> None:
        # Every fourth poll, so a minute of waiting produces one line rather than four.
        if job_id and done % 4 == 0:
            left = int((total - done) * ENROLL_POLL_SECONDS / 60)
            job_service.update_progress(
                db, job_id, min(65 + int(25 * done / max(total, 1)), 95),
                f"Waiting for {agent.name} to enrol — {left} min left…")

    ok = await _wait_for_enrolment(db, agent.id, timeout_s=enroll_timeout_seconds(),
                                   sleep=sleep, on_tick=_tick)
    if not ok:
        minutes = int(enroll_timeout_seconds() / 60)
        where = vm.name or vm.platform_vm_id
        # What this dashboard already knows and has never said. A guest that cannot start
        # the agent cannot report anything, so both messages below can only list
        # candidates — and when the template was baked HERE, one of those candidates is a
        # recorded fact rather than a guess. Led with, because it is the answer.
        #
        # Local import for the cycle, as in `render_bootstrap` above.
        from . import pov_template_builder
        gap = pov_template_builder.runtime_gap_for_template(
            db, platform=env.platform, template_id=env.template_id or "")
        lead = f"{gap} " if gap else ""
        if mechanism == "metadata":
            raise BrokerError(
                f"the bootstrap was written to {where} but no agent enrolled within "
                f"{minutes} minutes. {lead}Nothing executes user_data for you — check that "
                f"VM has the metadata runner from the template contract, that it is on an "
                f"automatic network, and that it can reach {dashboard_url}. Press "
                f"Install control node again to re-issue the code.")
        raise BrokerError(
            f"the control node {where} was built with its bootstrap in user-data but no "
            f"agent enrolled within {minutes} minutes. {lead}Cloud-init runs it on first "
            f"boot, so check the VM's console output, that its image HAS cloud-init and "
            f"Docker, and that it can reach {dashboard_url} outbound. Press "
            f"Install control node again to rebuild it with a fresh code.")

    # Redeemed, so the payload is now a spent secret sitting where anyone with read access
    # to the environment can see it. Clearing it also stops a reboot re-running a
    # bootstrap whose code is gone. Best-effort: the agent is enrolled either way, and
    # failing here would report a working broker as broken.
    #
    # **Only on `metadata`.** A cloud's user-data can only be rewritten while the instance
    # is STOPPED, so there is nothing to clear here without a stop/start of a VM that has
    # just come up. The exposure is smaller than it looks: the code is single-use and
    # fifteen minutes old by now, and the instance requires IMDSv2, so reading it needs a
    # token obtained from on the guest. It is written down in docs/profiles/pov/README.md rather
    # than quietly accepted.
    if mechanism == "metadata":
        try:
            await mod.inject_bootstrap(env.platform_environment_id, vm.platform_vm_id, "")
        except Exception:  # noqa: BLE001
            logger.warning("POV %s: could not clear the spent bootstrap payload", env.id,
                           exc_info=True)

    if mechanism == "cloud_init":
        # One more read now the broker VM has finished booting. Without it the row keeps
        # the aggregate runstate from the moment the instance was still `pending` — which
        # reads as "busy" on the POV page until the next reconcile up to ten minutes
        # later, and gates the power buttons on a value that is already wrong.
        try:
            await pov_env_service.refresh_vms(db, env)
        except Exception:  # noqa: BLE001
            logger.warning("POV %s: post-enrolment VM read failed", env.id, exc_info=True)

    record_broker_error(db, env, "")
    where = vm.name or vm.platform_vm_id
    db.rollback()
    row = db.query(RemoteAgent).filter(RemoteAgent.id == agent.id).first()
    if row is not None and row.auth_mode == "spiffe":
        summary = (f"Control node agent {agent.name} attested through SPIRE on {where}; its "
                   f"signing key is held in memory only.")
    elif spire:
        # Offered and not taken: the guest fell back on its own, which only it can explain.
        summary = (f"Control node agent {agent.name} enrolled on {where} with an Ed25519 key. "
                   f"SPIRE was offered but the guest fell back: check that {where} reaches "
                   f"{spire['server_address']}:{spire['server_port']} with no TLS inspection "
                   f"in between. The guest's bootstrap output names what it saw.")
    else:
        summary = f"Control node agent {agent.name} enrolled on {where}."
    # Replaces the last "Waiting for … to enrol — N min left…" tick. Nothing after this
    # writes the progress line, and set_completed keeps it, so without this a finished
    # job still reads as waiting under a green 100% bar.
    _progress(95, summary)
    return summary


async def run_env_broker(job_id: str, meta: dict) -> None:
    """The standalone job behind the POV page's **Broker** button.

    The same body the provision runs, with the opposite failure disposition: here the
    operator asked for a broker and nothing else, so not getting one is a failed job.
    """
    from ..database import SessionLocal
    from . import pov_env_service

    db = SessionLocal()
    try:
        env = pov_env_service.get(db, meta.get("environment_id", ""))
        if env is None:
            job_service.set_failed(db, job_id, "the POV environment row is gone")
            return
        if not env.platform_environment_id:
            job_service.set_failed(
                db, job_id,
                "this environment was never created on the platform, so there is no VM "
                "to install a control node on.")
            return
        try:
            summary = await ensure_broker(db, env, job_id=job_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("POV %s: broker enrolment failed", env.id, exc_info=True)
            record_broker_error(db, env, str(exc))
            job_service.set_failed(db, job_id, str(exc))
            return
        job_service.set_completed(db, job_id, {
            "environment_id": env.id,
            "broker_vm_id": env.broker_vm_id,
            "broker_agent_id": env.broker_agent_id,
            "summary": summary,
        })
    finally:
        db.close()


def teardown(db: Session, env: PovEnvironment) -> str:
    """Revoke and delete this POV's broker agent. Returns a line for the job log.

    Runs **before** the platform delete, not after. An enrolled agent whose VM has just
    been deleted keeps polling from nowhere and keeps holding whatever job it leased; the
    order here is what makes "the environment is gone" and "its agent is gone" the same
    event rather than two.

    Idempotent: an environment with no broker, or one whose agent row somebody already
    removed, is a no-op rather than an error. Destroy is the path that has to survive
    every kind of half-finished state.
    """
    agent = _agent_row(db, env)
    if agent is None:
        env.broker_agent_id = None
        env.broker_vm_id = None
        db.commit()
        return "No control node agent to revoke."

    name = agent.name
    # Its SPIRE entries go with it. Best-effort, like remove_workload itself: the agent
    # being revoked is what must happen, and it already has by the time a down server
    # would matter.
    spire_note = ""
    if agent.spiffe_id:
        try:
            from . import dashboard_spire
            td = agent.spiffe_id.split("/")[2]
            problems = dashboard_spire.remove_workload(
                agent.spiffe_id, dashboard_spire.ids_for(td, agent.id)[1])
            if problems:
                spire_note = f" SPIRE cleanup left: {'; '.join(problems)}."
        except Exception as exc:  # noqa: BLE001
            spire_note = f" SPIRE cleanup failed: {exc}."
    held = agent_service.revoke_agent(db, agent)
    agent_service.delete_agent(db, agent)
    env.broker_agent_id = None
    env.broker_vm_id = None
    db.commit()
    if held:
        return (f"Revoked and removed control node agent {name}; {held} held job(s) "
                f"released.{spire_note}")
    return f"Revoked and removed control node agent {name}.{spire_note}"


# ── what the UI shows ────────────────────────────────────────────────────────

def describe(db: Session, env: PovEnvironment) -> dict:
    """The broker's state for one POV row.

    ``status`` is ``agent_service.status_of`` — derived, never stored — plus one value it
    does not have: ``"none"``, for a POV with no broker at all. That distinction is the
    whole point of the field: "never installed" and "installed and offline" have different
    remedies, and a single "not working" hides which one you are looking at.
    """
    return _described(env, _agent_row(db, env))


def _described(env: PovEnvironment, agent: RemoteAgent | None) -> dict:
    """``describe``'s projection, once the agent row has been resolved however."""
    return {
        "broker_vm_id": env.broker_vm_id or "",
        "broker_agent_id": env.broker_agent_id or "",
        "broker_agent_name": agent.name if agent is not None else "",
        "broker_status": agent_service.status_of(agent) if agent is not None else "none",
        # How it signs in: "spiffe" (attested, key in memory), "ed25519" (enrolment code,
        # key on the guest's disk), or "" before it has done either.
        "broker_auth": ("" if agent is None or not agent.public_key
                        else "spiffe" if agent.auth_mode == "spiffe" else "ed25519"),
        "broker_error": env.metadata_dict.get("broker_error", ""),
    }


def describe_many(db: Session, envs: list[PovEnvironment]) -> dict[str, dict]:
    """``describe`` for a whole page, in two queries instead of two per row.

    Same resolution as :func:`_agent_row` and deliberately the same *precedence* — id
    first, derived name as recovery — because a list that disagreed with the detail page
    about which row is a POV's broker would be a worse bug than the one this replaces.

    The reason it exists is the fallback. ``_agent_row`` only skips its second query when
    ``broker_agent_id`` is set, so every POV *without* a broker — the common row on a busy
    install — cost a full ``RemoteAgent.name`` lookup, and the POV list ran one per row.
    At a hundred SEs the connection pool is what caps concurrency here, not the platform,
    so this is the read that had to stop scaling with the page.

    Returns ``{env.id: projection}``. A caller must tolerate a missing key rather than
    assume one per input.
    """
    if not envs:
        return {}

    ids = {e.broker_agent_id for e in envs if e.broker_agent_id}
    names = {agent_name(e) for e in envs}
    by_id: dict[str, RemoteAgent] = {}
    by_name: dict[str, RemoteAgent] = {}
    if ids:
        by_id = {a.id: a for a in
                 db.query(RemoteAgent).filter(RemoteAgent.id.in_(sorted(ids))).all()}
    if names:
        by_name = {a.name: a for a in
                   db.query(RemoteAgent).filter(RemoteAgent.name.in_(sorted(names))).all()}

    out: dict[str, dict] = {}
    for env in envs:
        agent = by_id.get(env.broker_agent_id) if env.broker_agent_id else None
        if agent is None:
            if env.broker_agent_id:
                logger.warning("POV %s: broker_agent_id %s points at no row; falling back "
                               "to the derived name", env.id, env.broker_agent_id)
            agent = by_name.get(agent_name(env))
        out[env.id] = _described(env, agent)
    return out
