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
feature; the other half is the template contract in ``docs/integrations/skytap.md``, and a
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
from datetime import datetime

from sqlalchemy.orm import Session

from ..database import PovEnvironment, PovEnvironmentVM, RemoteAgent
from . import agent_service, config_service, job_service, lab_platforms, public_url

logger = logging.getLogger(__name__)


class BrokerError(Exception):
    """A refusal carrying the remedy, not just the cause.

    Raised where the operator has something to do about it. The message lands in the
    job's ``error_message`` and on the POV row, which are the only two places anyone
    looks — see docs/notes on why a log line is not a third place.
    """


# The VM in the template that carries the agent. A convention, not a discovery: the
# dashboard cannot tell which of eight VMs is the one with Docker on it, and guessing
# wrong installs an agent on the customer's domain controller.
DEFAULT_BROKER_VM_NAME = "broker"

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
    """
    return (env.metadata_dict.get("broker_vm_name") or "").strip() or DEFAULT_BROKER_VM_NAME


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

def select_broker_vm(db: Session, env: PovEnvironment) -> PovEnvironmentVM:
    """The VM that will run the agent, or a refusal naming what was actually found.

    Exact name match, case-insensitively. Deliberately not a fuzzy one: "contains
    'broker'" also matches a customer VM called ``password-broker``, and the cost of the
    wrong answer is an agent installed on a machine nobody expected.
    """
    wanted = broker_vm_name(env).strip().lower()
    rows = (db.query(PovEnvironmentVM)
              .filter(PovEnvironmentVM.environment_id == env.id).all())
    if not rows:
        raise BrokerError(
            "this environment has no VM rows yet, so there is nothing to install the "
            "broker on. Refresh the POV and try again once the platform reports its VMs.")

    for row in rows:
        if (row.name or "").strip().lower() == wanted:
            return row

    found = ", ".join(sorted((r.name or r.platform_vm_id) for r in rows)) or "none"
    raise BrokerError(
        f"no VM in this environment is named {wanted!r}, so there is nowhere to install "
        f"the broker agent. Found: {found}. Rename the template's broker VM, or set a "
        f"different broker VM name on this POV.")


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

def render_policy(targets: list[str], ansible_targets: list[str] | None = None) -> str:
    """The agent's ``policy.yaml``, generated from this POV's addresses.

    Generated files here are held to a higher bar than usual because **this file fails
    closed**: the agent refuses to start on anything it cannot read as a target, and an
    agent that will not start is indistinguishable from a network fault from the
    dashboard's side. So the shape is boring on purpose — inline lists, no quoting
    decisions, no user-supplied text anywhere in it.
    """
    ports = ", ".join(str(p) for p in BROKER_PORTS)
    ansible_targets = [t for t in (ansible_targets or []) if t]
    # ASCII only, deliberately. This file is written by a shell heredoc on a guest whose
    # locale nobody chose and parsed by an agent that already has to be told about UTF-16
    # and BOMs; a non-ASCII byte in a comment is a needless way to find that out.
    lines = [
        "# Generated by the dashboard for a POV broker agent.",
        "# Regenerated on every broker run - edit the POV, not this file.",
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

    # Config Management, for the Resource Broker install. Its own target list, NOT the
    # discovery one above — widening a port probe must never widen what may have a
    # playbook applied to it as root. An empty list means "nothing may be configured",
    # which is the correct fail-closed reading and what the agent does with it.
    lines.append("ansible:")
    if ansible_targets:
        ansible_ports = ", ".join(str(p) for p in ANSIBLE_PORTS)
        lines += [
            "  enabled: true",
            f"  vm_image: {ANSIBLE_VM_IMAGE}",
            "  targets:",
        ]
        for ip in ansible_targets:
            lines.append(f"    - cidr: {ip}/32")
            lines.append(f"      ports: [{ansible_ports}]")
    else:
        # Rendered disabled rather than omitted, so an operator reading the file on the
        # broker VM sees that the feature exists and that this POV has no Windows guest
        # for it — rather than wondering whether the dashboard forgot.
        lines += [
            "  enabled: false",
            "  # No Windows VM in this POV, so nothing may have a playbook applied to it.",
        ]

    lines.append("")
    return "\n".join(lines)


def render_bootstrap(*, env_name: str, dashboard_url: str, enroll_code: str,
                     policy_yaml: str, now: datetime | None = None) -> str:
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
    """
    stamp = (now or datetime.utcnow()).strftime("%Y-%m-%d %H:%M:%SZ")
    # The name reaches a shell comment. `api/pov` already constrains it to a slug, but a
    # newline here would end the comment and put whatever followed on its own line, so it
    # is flattened at the point of use rather than trusted from two callers away.
    env_name = " ".join(str(env_name or "unnamed").split())
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

# Replace any previous agent AND its state volume. The volume holds the identity written
# at first enrolment; leaving it means the new code is never redeemed, the container signs
# with a key the dashboard has already cleared, and every poll 401s.
docker rm -f dashboard-agent >/dev/null 2>&1 || true
docker volume rm {GUEST_STATE_VOLUME} >/dev/null 2>&1 || true

docker run -d --name dashboard-agent --restart unless-stopped \\
  --read-only --cap-drop ALL --security-opt no-new-privileges:true \\
  --user 10001:10001 --tmpfs /tmp \\
  -v /var/run/docker.sock:/var/run/docker.sock \\
  -v {GUEST_STATE_VOLUME}:/var/lib/dashboard-agent \\
  -v "$STATE/policy.yaml:/etc/dashboard-agent/policy.yaml:ro,Z" \\
  -v "$STATE/enroll-code:/etc/dashboard-agent/enroll-code:ro,Z" \\
  -e DASHBOARD_URL="{dashboard_url}" \\
  -e AGENT_ENROLLMENT_CODE_FILE=/etc/dashboard-agent/enroll-code \\
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
        description=f"POV broker for {env.name} on {env.platform}",
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
    if mechanism != "metadata":
        # Degrade by saying so. `remote_exec` is a real mechanism this slice has not
        # written, and `None` means the enrol has to be baked into the template — both are
        # answers, and neither is this code path silently doing nothing.
        how = mechanism or "no mechanism at all"
        raise BrokerError(
            f"{env.platform} delivers bootstrap data by {how}, and the broker install "
            f"here is written against the 'metadata' mechanism. This platform needs its "
            f"own broker path.")

    dashboard_url = dashboard_agent_url()
    if not dashboard_url:
        raise BrokerError(
            "this dashboard does not know its own public URL, so there is no address to "
            "give an agent inside the POV. Set Public base URL in Settings → Integrations "
            "→ Remote Agents to the hostname that serves /api/agent, then run this again.")
    if dashboard_url.startswith("http://"):
        raise BrokerError(
            f"the agent endpoint is {dashboard_url}. The agent refuses to sign over "
            f"plaintext, so a broker installed against it would never enrol. Terminate "
            f"TLS in front of the dashboard and correct Public base URL.")

    _progress(10, "Reading the environment's VMs…")
    # Re-read rather than trust what is on the row: the private IPs this writes into the
    # policy only exist once the VMs are running, and a POV that was suspended and resumed
    # can have new ones.
    from . import pov_env_service
    await pov_env_service.refresh_vms(db, env)

    vm = select_broker_vm(db, env)
    targets = _broker_targets(db, env)
    if not targets:
        raise BrokerError(
            "no VM in this environment reported a private address, so the broker's policy "
            "would grant nothing and the agent would refuse to start. Power the "
            "environment on and run this again.")

    _progress(35, f"Minting an enrolment code for {agent_name(env)}…")
    agent, code = _mint_code(db, env)

    # The Config-Management grant is scoped to this POV's Windows guests — or to the one
    # named Resource Broker host, once somebody has named it. Computed here rather than
    # inside render_policy so that function stays pure and testable.
    from . import pov_resource_broker
    payload = render_bootstrap(
        env_name=env.name, dashboard_url=dashboard_url, enroll_code=code,
        policy_yaml=render_policy(targets,
                                  pov_resource_broker.windows_targets(db, env)))

    _progress(50, f"Injecting the bootstrap onto {vm.name or vm.platform_vm_id}…")
    mod = lab_platforms.adapter(env.platform)
    try:
        await mod.inject_bootstrap(env.platform_environment_id, vm.platform_vm_id, payload)
    except Exception as exc:  # noqa: BLE001
        raise BrokerError(
            f"could not hand the bootstrap to {vm.name or vm.platform_vm_id}: {exc}") from exc

    # Persist BEFORE the wait. A crash here must leave the next run re-issuing this row's
    # code rather than minting a second agent for the same POV.
    env.broker_vm_id = vm.platform_vm_id
    env.broker_agent_id = agent.id
    db.commit()

    _progress(65, "Waiting for the broker agent to enrol…")

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
        raise BrokerError(
            f"the bootstrap was written to {vm.name or vm.platform_vm_id} but no agent "
            f"enrolled within {int(enroll_timeout_seconds() / 60)} minutes. Nothing "
            f"executes user_data for you — check that VM has the metadata runner from the "
            f"template contract, that it is on an automatic network, and that it can "
            f"reach {dashboard_url}. Press Broker again to re-issue the code.")

    # Redeemed, so the payload is now a spent secret sitting where anyone with read access
    # to the environment can see it. Clearing it also stops a reboot re-running a
    # bootstrap whose code is gone. Best-effort: the agent is enrolled either way, and
    # failing here would report a working broker as broken.
    try:
        await mod.inject_bootstrap(env.platform_environment_id, vm.platform_vm_id, "")
    except Exception:  # noqa: BLE001
        logger.warning("POV %s: could not clear the spent bootstrap payload", env.id,
                       exc_info=True)

    record_broker_error(db, env, "")
    return f"Broker agent {agent.name} enrolled on {vm.name or vm.platform_vm_id}."


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
                "to install a broker on.")
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
        return "No broker agent to revoke."

    name = agent.name
    held = agent_service.revoke_agent(db, agent)
    agent_service.delete_agent(db, agent)
    env.broker_agent_id = None
    env.broker_vm_id = None
    db.commit()
    if held:
        return f"Revoked and removed broker agent {name}; {held} held job(s) released."
    return f"Revoked and removed broker agent {name}."


# ── what the UI shows ────────────────────────────────────────────────────────

def describe(db: Session, env: PovEnvironment) -> dict:
    """The broker's state for one POV row.

    ``status`` is ``agent_service.status_of`` — derived, never stored — plus one value it
    does not have: ``"none"``, for a POV with no broker at all. That distinction is the
    whole point of the field: "never installed" and "installed and offline" have different
    remedies, and a single "not working" hides which one you are looking at.
    """
    agent = _agent_row(db, env)
    return {
        "broker_vm_id": env.broker_vm_id or "",
        "broker_agent_id": env.broker_agent_id or "",
        "broker_agent_name": agent.name if agent is not None else "",
        "broker_status": agent_service.status_of(agent) if agent is not None else "none",
        "broker_error": env.metadata_dict.get("broker_error", ""),
    }
