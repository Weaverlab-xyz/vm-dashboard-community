"""Authoring lab-platform templates, so a POV has something SaaS-first to be built from.

A POV is a template instantiated whole. Until this module existed the dashboard could only
*read* the catalogue, which left the whole feature downstream of templates authored by hand
in the platform's own console — and those templates were largely built for an on-premises
approach, carrying a full product stack inside the environment. A SaaS-first POV wants the
opposite shape: the customer-like VMs, a broker, and nothing else, because PRA, Password
Safe and Entitle are *tenants* reached from outside.

**A template is immutable, so authoring is a bake, not an edit.** No lab platform offers
"change this template"; the shape is always instantiate → change the environment → save it
back. That is the pipeline here, and it is why a build owns a scratch environment for its
duration.

    create_environment -> power on -> check the contract -> prepare -> shut down ->
    bake -> reap

The one part worth reading twice is **prepare**, because it is the piece with no automation
before this. ``docs/integrations/skytap.md#the-template-contract`` requires the broker VM to
carry a metadata runner: the platform hands ``user_data`` to the guest and *nothing executes
it*, so a template whose broker cannot fetch and run its own bootstrap produces a POV that
comes up, bills, and never enrols an agent. That runner has lived only as an example in a
Markdown file for a human to copy into an image. Here it is generated — from the same marker
constants ``pov_broker`` writes into the payload the runner has to recognise, so the two
cannot drift — and installed over one short-lived SSH session.

Two boundaries this module holds deliberately:

**The Windows Resource Broker VM is checked, never prepared.** Its installer is staged by
the customer (see ``docs/design/pov-resource-broker.md``) and there is no WinRM route from
here into a lab platform's private network. The contract check reports whether a suitable
guest is present; installing on it stays the POV's job, after the broker agent exists.

**A failed prepare does not fail the build.** A template that bakes without the runner is
still a usable template — the operator pastes the script in themselves, which is exactly
what they do today. Failing the build would throw away a correct template over a step whose
manual fallback is the status quo. The reason lands on ``prepare_method`` and
``prepare_detail``, never in ``error_message``, which means "this build is broken".
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from datetime import datetime

from sqlalchemy.orm import Session

from ..database import PovTemplateBuild, SessionLocal
from . import job_service, lab_platforms, pov_broker, pov_credentials

logger = logging.getLogger(__name__)


class TemplateBuildError(Exception):
    """A template build could not proceed. The message is shown to an operator, so it
    names the remedy rather than the symptom."""


# ── status vocabulary ────────────────────────────────────────────────────────

STATUS_BUILDING = "building"      # the scratch environment is being created / powered on
STATUS_PREPARING = "preparing"    # the contract check and the runner install
STATUS_BAKING = "baking"          # saving the environment back as a template
STATUS_READY = "ready"            # the template exists; the scratch environment is gone
STATUS_FAILED = "failed"
STATUS_DISCARDED = "discarded"    # reaped by hand without ever baking

# A build's scratch environment is a running environment and it bills. This is the primary
# guard, and it is deliberately the platform's own timer rather than anything in this
# process: a build whose worker is killed mid-run costs an idle timeout, not a month. The
# job reaps it after a successful bake, and the page offers Discard for every other
# outcome — but only this one survives the worker dying, which is the failure nobody is
# watching for. Note the job deliberately does NOT reap on failure: a build that broke is
# the one whose environment somebody may want to look at, and Discard is one click.
BUILD_SUSPEND_ON_IDLE_S = 1800

# How long to wait for the scratch environment to come up. Longer than a POV power-on
# because a base template's FIRST boot is the unbounded one — a Windows guest pulling
# updates is the reason the POV broker payload is injected after power-on rather than
# before.
BUILD_POWERON_TIMEOUT_S = 2400.0

# **Skytap will not bake a multi-VM environment that is still running.** Its own
# documentation says so obliquely — "if the environment contains multiple VMs, Save as
# Template may generate an error; if this happens, shut down the environment and try
# again" — and what it actually answers is `409 {"error":"The machine was busy. Try again
# later."}`, which reads like a transient and is not one: every retry hits it again for as
# long as the VMs are up. So the shutdown is a pipeline STAGE, not error handling.
#
# Four Windows guests shutting down gracefully is the long pole, and a guest that hangs on
# a shutdown dialog would otherwise wedge the whole build — hence `halted` as the fallback
# below, which is Skytap's documented escape hatch ("forces a transition to stopped … when
# the VM won't shut down due to errors in the guest VM"). Graceful first, because a
# template baked from a hard power-off is a template every future POV boots dirty from.
BUILD_SHUTDOWN_TIMEOUT_S = 900.0

# The guest port published for the prepare step, and how long we will wait to reach it.
_SSH_PORT = 22
_SSH_CONNECT_TIMEOUT_S = 30.0
# A freshly created VM answers TCP before sshd is ready, and a template's first boot can
# run long. Retry rather than fail on the first refusal.
_SSH_ATTEMPTS = 10
_SSH_RETRY_WAIT_S = 15.0

# Where the runner and its unit land on the broker VM.
RUNNER_PATH = "/usr/local/sbin/dashboard-bootstrap-runner"
RUNNER_UNIT_PATH = "/etc/systemd/system/dashboard-bootstrap-runner.service"
RUNNER_MARK_DIR = "/var/lib/dashboard-bootstrap"
# How often the runner re-reads the metadata service. The payload arrives minutes after
# boot and again on every re-broker, so this is a liveness interval, not a poll for work.
_RUNNER_INTERVAL_S = 20


# ── the runner ───────────────────────────────────────────────────────────────

def _marker_stem(marker: str) -> str:
    """The version-independent core of a bootstrap marker.

    Two things make matching the raw constant wrong, and both are silent:

    **The version must not be matched on.** ``BOOTSTRAP_BEGIN`` is
    ``# BEGIN-DASHBOARD-AGENT-BOOTSTRAP v1``. The runner is baked into a template image and
    outlives this dashboard's payload format — a runner pinned to ``v1`` would stop
    recognising a ``v2`` payload on every template already in the field, and the symptom
    would be an agent that never enrols, which is indistinguishable from having no runner
    at all. The marker still says "this is our payload"; the version is the payload
    reader's business, not the runner's.

    **The stem must not contain whitespace.** It is interpolated into a ``case`` pattern,
    and the shell splits an unquoted pattern on spaces — ``*# BEGIN-… v1*)`` parses as the
    pattern ``*#`` followed by a syntax error. Dropping the ``# `` prefix and the version
    leaves a single word, which is also exactly the form the template contract in
    ``docs/integrations/skytap.md`` documents.
    """
    stem = marker.lstrip("#").strip()
    parts = stem.split()
    if len(parts) > 1 and re.fullmatch(r"v\d+", parts[-1]):
        parts = parts[:-1]
    return " ".join(parts)


def render_runner() -> str:
    """The metadata runner the template contract requires, as a ``/bin/sh`` script.

    Four properties are load-bearing, and each is a way the contract is got wrong in
    practice:

    1. **It polls; it does not read once.** The bootstrap arrives *after* the VM is up,
       because an enrolment code lives fifteen minutes and a first boot is not bounded —
       see ``pov_broker``'s ordering note. A runner that reads ``user_data`` once at boot
       finds it empty and stops forever.
    2. **Both markers must be present** before anything executes. A truncated metadata read
       would otherwise run half the payload, and the half at the top is the half that
       deletes the running agent and its state volume.
    3. **The "already ran" marker is the payload's hash, not a flag.** A reboot with
       unchanged ``user_data`` must not re-run; a re-injection with a fresh enrolment code
       must. A boolean gets exactly one of those right.
    4. **It runs as root.** It writes under ``/etc/dashboard-agent`` and calls ``docker``.

    The markers come from ``pov_broker`` rather than being written out again here. They are
    the one string the producer and the consumer must agree on exactly, and a copy is a
    chance for them to stop agreeing.
    """
    begin = _marker_stem(pov_broker.BOOTSTRAP_BEGIN)
    end = _marker_stem(pov_broker.BOOTSTRAP_END)
    return f"""#!/bin/sh
# {RUNNER_PATH} - the dashboard's metadata runner.
#
# Generated by the POV template builder. The lab platform hands this VM a bootstrap payload
# as user_data and NOTHING on the platform executes it, so this is what does. It is
# idempotent and costs one request to a link-local address every {_RUNNER_INTERVAL_S}s.
set -eu

MARKDIR={RUNNER_MARK_DIR}
METADATA_URL=http://169.254.169.254/skytap/vms/self/user_data
# Some accounts serve only the whole document. Falling back to it and pulling user_data out
# means one runner works on both rather than a template that boots correctly in one region.
METADATA_DOC_URL=http://169.254.169.254/skytap

mkdir -p "$MARKDIR"

read_payload() {{
  body=$(curl -fsS --max-time 10 "$METADATA_URL" 2>/dev/null || true)
  case "$body" in
    *{begin}*) printf '%s' "$body"; return 0 ;;
  esac
  doc=$(curl -fsS --max-time 10 "$METADATA_DOC_URL" 2>/dev/null || true)
  case "$doc" in
    *{begin}*)
      # Pull the user_data string out of the JSON document without a JSON parser: the
      # payload is a shell script, and python may not be installed on a minimal guest.
      #
      # The capture is \\([^"\\\\]|\\\\.\\)* — a JSON string body — and NOT `.*`. A greedy
      # `.*` runs to the last quote on the line, so every field that happens to follow
      # user_data is appended to a script this runner then executes as root. That is a
      # remote-content-to-root-shell bug, not a formatting nit.
      printf '%s' "$doc" \\
        | sed -n 's/.*"user_data"[[:space:]]*:[[:space:]]*"\\(\\([^"\\\\]\\|\\\\.\\)*\\)".*/\\1/p' \\
        | sed -e 's/\\\\n/\\n/g' -e 's/\\\\"/"/g' -e 's/\\\\\\\\/\\\\/g'
      return 0 ;;
  esac
  return 0
}}

while :; do
  body=$(read_payload)
  # BOTH markers, or nothing runs. See property 2 in the generator's docstring: half of
  # this payload is the destructive half.
  case "$body" in
    *{begin}*{end}*)
      # Everything after the end marker is cut off BEFORE anything is executed. Belt and
      # braces with the extraction above: whatever a metadata document carries after the
      # payload, it does not reach a root shell. Truncating also makes the hash below
      # cover exactly the bytes that run.
      payload=$(printf '%s\\n' "$body" | sed -n '1,/{end}/p')
      sum=$(printf '%s' "$payload" | sha256sum | cut -d' ' -f1)
      if [ "$sum" != "$(cat "$MARKDIR/last" 2>/dev/null || true)" ]; then
        (umask 077 && printf '%s' "$payload" > /run/dashboard-bootstrap.sh)
        # The marker is written only on success, so a failed run is retried on the next
        # tick rather than latched as done.
        if sh /run/dashboard-bootstrap.sh; then
          printf '%s' "$sum" > "$MARKDIR/last"
        fi
        rm -f /run/dashboard-bootstrap.sh
      fi
      ;;
  esac
  sleep {_RUNNER_INTERVAL_S}
done
"""


def render_runner_unit() -> str:
    """The systemd unit that keeps the runner running.

    ``Restart=always`` rather than a oneshot: the runner's whole job is to be there when
    the payload arrives, which is minutes after boot and again on every re-broker.
    """
    return f"""[Unit]
Description=Dashboard bootstrap runner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={RUNNER_PATH}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""


def render_install_script() -> str:
    """Runner + unit + enable, as one script an operator can paste into a root shell.

    This is the fallback path, and it is offered on every build rather than only on a
    failed one: an SE baking a template on a platform with no published-service capability,
    or from a network with no route to a NAT-ed high port, needs it as the *primary* route
    and should not have to fail once to find it.
    """
    runner = render_runner()
    unit = render_runner_unit()
    return f"""#!/bin/sh
# Install the dashboard's metadata runner. Run as root on the broker VM, then bake the
# environment into a template.
set -eu

cat > {RUNNER_PATH} <<'DASHBOARD_RUNNER_EOF'
{runner}DASHBOARD_RUNNER_EOF
chmod 0755 {RUNNER_PATH}

cat > {RUNNER_UNIT_PATH} <<'DASHBOARD_UNIT_EOF'
{unit}DASHBOARD_UNIT_EOF

mkdir -p {RUNNER_MARK_DIR}
systemctl daemon-reload
systemctl enable --now dashboard-bootstrap-runner
systemctl is-active dashboard-bootstrap-runner
"""


# ── the template contract ────────────────────────────────────────────────────

# Reported per check rather than collapsed into one verdict. "no broker VM" and "the broker
# is on a manual network" are different failures with different fixes, and a single
# not-working badge is how an operator ends up rebuilding the wrong thing — the same
# reasoning the POV page's Broker column already follows.
CHECK_PASS = "pass"
CHECK_WARN = "warn"
CHECK_FAIL = "fail"


def _result(check: str, status: str, detail: str) -> dict:
    return {"check": check, "status": status, "detail": detail}


def check_contract(vms: list[dict], broker_vm_name: str) -> list[dict]:
    """Does this set of VMs satisfy the template contract?

    ``vms`` is the adapter's shape — ``{id, name, os_family, private_ip, interfaces}`` —
    so this works identically against a live template read and against the scratch
    environment mid-build. Pure, so the outcomes are testable without a platform.

    What is checked, and what each failure actually costs:

    * **A broker VM by name.** Without it the POV comes up, bills, and the Broker column
      reads ``none`` forever. Exact match — see ``pov_broker.name_matches_broker``.
    * **The broker is on an automatic network.** The metadata service answers *only* on
      VMs attached to one. On a manual network the guest gets no metadata at all, which
      looks exactly like a missing runner and sends the operator to rewrite a runner that
      was fine.
    * **A Windows guest for the Resource Broker.** A warning, not a failure: plenty of POVs
      wire only PRA and Entitle, and refusing to bake a Linux-only template would be
      inventing a requirement the POV flow does not have.
    * **The environment has other VMs.** A template that is nothing but a broker builds a
      POV with nothing to demonstrate.

    Whether the runner itself is present cannot be answered from a platform read — it is a
    file inside the guest. The build answers it by installing one; a bare Verify says so
    rather than guessing.
    """
    out: list[dict] = []
    wanted = (broker_vm_name or pov_broker.DEFAULT_BROKER_VM_NAME).strip()
    names = [str(v.get("name") or "") for v in vms]

    broker = next((v for v in vms
                   if pov_broker.name_matches_broker(v.get("name"), wanted)), None)
    if broker is None:
        found = ", ".join(sorted(n for n in names if n)) or "none"
        out.append(_result(
            "broker VM", CHECK_FAIL,
            f"no VM is named {wanted!r}, so a POV from this template has nowhere to run "
            f"its agent. Found: {found}. Rename the VM in the template, or build with the "
            f"name this template actually uses."))
    else:
        out.append(_result("broker VM", CHECK_PASS,
                           f"{broker.get('name')} will run the agent."))

        # An automatic network is what the metadata service answers on. `nic_type` is the
        # adapter card; the network's own type is what matters, so a missing value is
        # reported as unknown rather than assumed good — an assumed-good network is how a
        # template ships that cannot bootstrap.
        nics = broker.get("interfaces") or []
        kinds = {str(n.get("network_type") or "").strip().lower() for n in nics}
        kinds.discard("")
        if not nics:
            out.append(_result(
                "broker network", CHECK_WARN,
                "the platform reported no network interfaces for the broker VM, so "
                "whether it can reach the metadata service is unknown."))
        elif "automatic" in kinds:
            out.append(_result("broker network", CHECK_PASS,
                               "the broker VM is on an automatic network."))
        elif kinds:
            out.append(_result(
                "broker network", CHECK_FAIL,
                f"the broker VM's network is {', '.join(sorted(kinds))}, not automatic. "
                f"The metadata service answers only on automatic networks, so the guest "
                f"would receive no bootstrap at all."))
        else:
            out.append(_result(
                "broker network", CHECK_WARN,
                "the platform did not report a network type for the broker VM. Confirm it "
                "is on an automatic network before relying on this template."))

    windows = [v for v in vms if str(v.get("os_family") or "") == "windows"]
    if windows:
        out.append(_result(
            "Resource Broker host", CHECK_PASS,
            f"{len(windows)} Windows guest(s): {', '.join(sorted(str(v.get('name') or '') for v in windows))}."))
    else:
        out.append(_result(
            "Resource Broker host", CHECK_WARN,
            "no Windows guest, so a POV from this template cannot install a Password Safe "
            "Resource Broker. Fine for a PRA-and-Entitle POV."))

    workload = [n for n in names if not pov_broker.name_matches_broker(n, wanted)]
    if workload:
        out.append(_result("workload VMs", CHECK_PASS,
                           f"{len(workload)} VM(s) besides the broker."))
    else:
        out.append(_result(
            "workload VMs", CHECK_WARN,
            "this template contains only the broker, so a POV built from it has nothing "
            "to demonstrate."))

    return out


def contract_ok(report: list[dict]) -> bool:
    """Whether a report has no hard failures. Warnings never block a bake — they are
    choices about what a template is for, not defects in it."""
    return not any(r.get("status") == CHECK_FAIL for r in report or [])


# ── the prepare step ─────────────────────────────────────────────────────────

async def _ssh_install(host: str, port: int, username: str, password: str) -> str:
    """Install the runner over SSH. Returns a short summary line.

    **``known_hosts=None``.** There is no host key to pin: this VM was created minutes ago
    by the same API call that told us where to reach it, and it is destroyed at the end of
    this job. The trust here is the platform API's, over the same credentials the rest of
    this integration already relies on. That is an acceptable trade for one connection to a
    machine with a lifetime measured in minutes — and it is exactly why this path is not
    reused for anything that outlives a build. POV wiring reaches VMs through a Gateway
    inside the environment for precisely this reason.
    """
    try:
        import asyncssh
    except ImportError as exc:  # pragma: no cover - asyncssh is in requirements.txt
        raise TemplateBuildError(
            "asyncssh is not installed, so the runner cannot be installed automatically. "
            "Use the install script from the builder page instead.") from exc

    script = render_install_script()
    last: Exception | None = None
    for attempt in range(_SSH_ATTEMPTS):
        try:
            async with asyncssh.connect(
                    host, port=port, username=username, password=password,
                    known_hosts=None, connect_timeout=_SSH_CONNECT_TIMEOUT_S) as conn:
                # Piped to `sh -s` rather than written and executed: no file is left on a
                # VM that is about to become a template, and nothing depends on a writable
                # path the guest may not have.
                result = await conn.run("sudo -n sh -s || sh -s", input=script,
                                        check=False)
                if result.exit_status != 0:
                    stderr = (result.stderr or "").strip()[:400]
                    raise TemplateBuildError(
                        f"the runner install exited {result.exit_status} on the broker VM"
                        f"{': ' + stderr if stderr else ''}. The template can still be "
                        f"baked and the script pasted in by hand.")
                check = await conn.run(
                    "systemctl is-active dashboard-bootstrap-runner; "
                    "docker --version 2>/dev/null || echo 'docker: MISSING'",
                    check=False)
                detail = " ".join((check.stdout or "").split())[:300]
                return f"runner installed over SSH; {detail}" if detail else \
                    "runner installed over SSH"
        except TemplateBuildError:
            raise
        except Exception as exc:  # noqa: BLE001
            # A brand-new guest answers TCP before sshd is ready, and a first boot can run
            # long. Retry rather than fail on the first refusal.
            last = exc
            if attempt < _SSH_ATTEMPTS - 1:
                logger.info("template build: SSH to %s:%s not ready (%s); retrying",
                            host, port, exc)
                await asyncio.sleep(_SSH_RETRY_WAIT_S)

    raise TemplateBuildError(
        f"could not reach the broker VM over SSH at {host}:{port} after "
        f"{_SSH_ATTEMPTS} attempts ({last}). This connects to a NAT-ed high port on the "
        f"lab platform, not to the API host — an egress rule that allows only HTTPS to the "
        f"API URL will block it. Use the install script from the builder page instead.")


async def prepare_broker_vm(mod, env_id: str, vm: dict) -> str:
    """Publish SSH, install the runner, revoke the published service. Returns a summary.

    ``docs/integrations/skytap.md`` rules published services out for POV *wiring*, because a
    published address changes per environment and per power cycle. A build is the one case
    where that objection does not apply: the address is created, used once and revoked
    inside this function, so there is nothing to churn and nothing that outlives the job.
    The revoke is in a ``finally`` because a published service left behind on a VM that is
    about to become a template would be baked into every POV built from it.
    """
    nics = vm.get("interfaces") or []
    nic = next((n for n in nics if n.get("id")), None)
    if nic is None:
        raise TemplateBuildError(
            f"the broker VM {vm.get('name')!r} has no addressable network interface, so a "
            f"port cannot be published to reach it.")

    published = await mod.publish_service(env_id, vm.get("id"), nic.get("id"), _SSH_PORT)
    try:
        entries = await mod.stored_credentials(env_id, vm.get("id"))
        username, password = pov_credentials.pick(
            entries, vm_label=f"the broker VM {vm.get('name')!r}",
            # A build has no login field to fall back on, so the only remedy is on the
            # platform side. See pov_credentials.DEFAULT_REMEDY.
            remedy=("Leave exactly one usable credential on that VM in the lab platform "
                    "and build again, or install the runner by hand — the build carries "
                    "no login of its own."))
        return await _ssh_install(published["external_ip"],
                                  int(published["external_port"]), username, password)
    finally:
        with contextlib.suppress(Exception):
            await mod.delete_published_service(env_id, vm.get("id"), nic.get("id"),
                                               published["id"])


# ── the build job ────────────────────────────────────────────────────────────

def get(db: Session, build_id: str) -> PovTemplateBuild | None:
    return db.query(PovTemplateBuild).filter(PovTemplateBuild.id == build_id).first()


def _adapter(build: PovTemplateBuild):
    return lab_platforms.adapter(build.platform)


def _fail(db: Session, build: PovTemplateBuild, job_id: str, message: str) -> None:
    """Mark the build failed, keeping the scratch environment id.

    Failing WITHOUT the id is how an orphan is made — the same rule the POV provision
    follows. The row stays visible with a Discard button so the environment can still be
    reaped.
    """
    build.status = STATUS_FAILED
    build.error_message = message
    db.commit()
    job_service.set_failed(db, job_id, message)


async def _reap(db: Session, build: PovTemplateBuild, mod, job_id: str) -> None:
    """Delete the scratch environment. Never raises — it runs after the work is done, and
    a bookkeeping failure here must not turn a successful bake into a failed one."""
    env_id = build.build_environment_id
    if not env_id:
        return
    try:
        await mod.delete_environment(env_id)
        build.build_environment_id = None
        db.commit()
        job_service.append_job_log(db, job_id,
                                   f"Reaped the build environment {env_id}.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("template build %s: could not reap environment %s",
                       build.id, env_id, exc_info=True)
        job_service.append_job_log(
            db, job_id,
            f"WARNING: the build environment {env_id} could not be deleted ({exc}). It is "
            f"still running and still billing — reap it with Discard, or in the platform.")


async def _quiesce(db: Session, build: PovTemplateBuild, mod, job_id: str) -> None:
    """Shut the build environment down so the bake can succeed. Raises if it will not go.

    Unlike `_reap`, a failure here is fatal on purpose: the only thing after this is the
    bake, and baking a running environment is precisely what does not work. Failing with
    "it would not shut down" is a fact the reader can act on; letting it through would
    surface as the 409 that names no cause.

    The graceful stop is tried first and the forced one only after it times out, because
    the difference is visible in every POV that is ever built from the result.
    """
    env_id = build.build_environment_id
    try:
        await mod.set_runstate(env_id, "stopped")
        await mod.wait_for_runstate(env_id, "stopped",
                                    timeout_s=BUILD_SHUTDOWN_TIMEOUT_S)
        job_service.append_job_log(db, job_id,
                                   f"Build environment {env_id} is stopped.")
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("template build %s: graceful shutdown of %s failed",
                       build.id, env_id, exc_info=True)
        job_service.append_job_log(
            db, job_id,
            f"The graceful shutdown did not finish ({exc}). Forcing the VMs off — see "
            f"the build log if the baked template misbehaves on first boot.")

    # `halted` is Skytap's documented force-off, and it settles on `stopped` — so that is
    # still what we wait for. Waiting for 'halted' would time out on a success.
    await mod.set_runstate(env_id, "halted")
    await mod.wait_for_runstate(env_id, "stopped", timeout_s=BUILD_SHUTDOWN_TIMEOUT_S)
    job_service.append_job_log(
        db, job_id, f"Build environment {env_id} was forced off and is stopped.")


async def run_template_build(job_id: str, meta: dict) -> None:
    """Build one template. The job body; see the module docstring for the pipeline."""
    db = SessionLocal()
    try:
        build = get(db, str(meta.get("build_id") or ""))
        if build is None:
            job_service.set_failed(db, job_id, "the template build row is gone")
            return

        try:
            mod = _adapter(build)
        except lab_platforms.LabPlatformError as exc:
            _fail(db, build, job_id, str(exc))
            return

        try:
            # ── create ───────────────────────────────────────────────────────
            job_service.update_progress(db, job_id, 5,
                                        "Creating the build environment…")
            env = await mod.create_environment(
                build.base_template_id, name=f"build-{build.name}",
                project_id=build.project_id or "")
            # Committed BEFORE anything else can fail. An environment that exists on the
            # platform and not here is the one failure nothing can clean up — and a scratch
            # environment nobody knows about bills until somebody notices.
            build.build_environment_id = str(env["id"])
            build.build_environment_was = str(env["id"])
            db.commit()
            job_service.set_cloud_resource_id(db, job_id, build.build_environment_id)

            # The platform's own idle timer, set before the power-on. This is what makes a
            # build whose worker dies cost an idle timeout instead of a month.
            with contextlib.suppress(Exception):
                await mod.update_environment(
                    build.build_environment_id,
                    {"suspend_on_idle": BUILD_SUSPEND_ON_IDLE_S})

            # ── power on ─────────────────────────────────────────────────────
            job_service.update_progress(db, job_id, 20, "Powering it on…")
            await mod.set_runstate(build.build_environment_id, "running")
            await mod.wait_for_runstate(build.build_environment_id, "running",
                                        timeout_s=BUILD_POWERON_TIMEOUT_S)

            # ── contract ─────────────────────────────────────────────────────
            build.status = STATUS_PREPARING
            db.commit()
            job_service.update_progress(db, job_id, 45,
                                        "Checking the template contract…")
            live = await mod.get_environment(build.build_environment_id)
            vms = live.get("vms") or []
            wanted = (build.broker_vm_name or pov_broker.DEFAULT_BROKER_VM_NAME)
            report = check_contract(vms, wanted)
            build.contract_list = report
            db.commit()
            for row in report:
                job_service.append_job_log(
                    db, job_id, f"[{row['status']}] {row['check']}: {row['detail']}")

            if not contract_ok(report):
                _fail(db, build,
                      job_id,
                      "the build environment does not satisfy the template contract, so "
                      "baking it would produce a template that cannot run a POV. See the "
                      "contract report on the build; press Discard to reap the "
                      "environment.")
                return

            broker = next((v for v in vms
                           if pov_broker.name_matches_broker(v.get("name"), wanted)), None)
            if broker is not None:
                build.broker_vm_id = str(broker.get("id") or "")
                db.commit()

            # ── prepare ──────────────────────────────────────────────────────
            # Never fatal. A template that bakes without the runner is still a usable
            # template — the operator pastes the script in, which is what they do today.
            job_service.update_progress(db, job_id, 60,
                                        "Installing the metadata runner…")
            if not meta.get("install_runner", True):
                build.prepare_method = "skipped"
                build.prepare_detail = ("the runner install was not requested; paste the "
                                        "install script onto the broker VM by hand.")
            elif not lab_platforms.supports(build.platform, "published_services"):
                build.prepare_method = "skipped"
                build.prepare_detail = (
                    f"{build.platform} cannot publish a port, so there is no route to the "
                    f"broker VM from here. Paste the install script in by hand.")
            elif broker is None:
                build.prepare_method = "skipped"
                build.prepare_detail = "no broker VM was resolved."
            else:
                try:
                    summary = await prepare_broker_vm(
                        mod, build.build_environment_id, broker)
                    build.prepare_method = "ssh"
                    build.prepare_detail = summary
                except Exception as exc:  # noqa: BLE001
                    build.prepare_method = "failed"
                    build.prepare_detail = str(exc)
                    logger.warning("template build %s: prepare failed", build.id,
                                   exc_info=True)
            db.commit()
            job_service.append_job_log(
                db, job_id, f"Prepare: {build.prepare_method} — {build.prepare_detail}")

            # ── quiesce ──────────────────────────────────────────────────────
            # See BUILD_SHUTDOWN_TIMEOUT_S: the bake below fails with a 409 that names no
            # cause for as long as these VMs are running.
            build.status = STATUS_BAKING
            db.commit()
            job_service.update_progress(db, job_id, 72,
                                        "Shutting the build environment down…")
            await _quiesce(db, build, mod, job_id)

            # ── bake ─────────────────────────────────────────────────────────
            job_service.update_progress(db, job_id, 80, "Saving it as a template…")
            tpl = await mod.create_template(build.build_environment_id, build.name,
                                            build.description or "")
            build.result_template_id = str(tpl["id"])
            build.result_template_name = tpl.get("name") or build.name
            db.commit()
            job_service.append_job_log(
                db, job_id,
                f"Template {build.result_template_id} ({build.result_template_name}) "
                f"created.")

            # ── reap ─────────────────────────────────────────────────────────
            if build.keep_build_environment:
                job_service.append_job_log(
                    db, job_id,
                    f"Keeping the build environment {build.build_environment_id} as "
                    f"asked. It is stopped, but its storage still bills — Discard "
                    f"reaps it.")
            else:
                job_service.update_progress(db, job_id, 92,
                                            "Reaping the build environment…")
                await _reap(db, build, mod, job_id)

            build.status = STATUS_READY
            db.commit()
            job_service.set_completed(db, job_id, {
                "build_id": build.id,
                "template_id": build.result_template_id,
                "template_name": build.result_template_name,
                "prepare_method": build.prepare_method or "",
            })
        except Exception as exc:  # noqa: BLE001
            logger.error("template build %s failed", build.id, exc_info=True)
            hint = ""
            if build.build_environment_id:
                hint = (f" The build environment {build.build_environment_id} is still "
                        f"running — press Discard to reap it.")
            _fail(db, build, job_id, f"{exc}{hint}")
    finally:
        db.close()


async def discard(db: Session, build: PovTemplateBuild) -> str:
    """Reap a build's scratch environment and close the row out.

    Allowed from any non-``ready`` state, and from ``ready`` when the environment was
    deliberately kept. A build that broke halfway is exactly the one whose environment most
    needs reaping — the same reason Destroy is allowed from ``failed`` on a POV.

    A failed reap does NOT mark the row discarded. Marking it would hide an environment that
    is still running and still billing.
    """
    env_id = build.build_environment_id
    if env_id:
        mod = _adapter(build)
        await mod.delete_environment(env_id)
        build.build_environment_id = None
    if build.status != STATUS_READY:
        build.status = STATUS_DISCARDED
    build.error_message = None
    db.commit()
    return (f"reaped the build environment {env_id}" if env_id
            else "there was no build environment left to reap")


def serialize(build: PovTemplateBuild) -> dict:
    """The row as the builder page reads it."""
    return {
        "id": build.id,
        "platform": build.platform,
        "name": build.name,
        "description": build.description or "",
        "base_template_id": build.base_template_id or "",
        "base_template_name": build.base_template_name or "",
        "project_id": build.project_id or "",
        "build_environment_id": build.build_environment_id or "",
        "build_environment_was": build.build_environment_was or "",
        "broker_vm_name": build.broker_vm_name or pov_broker.DEFAULT_BROKER_VM_NAME,
        "broker_vm_id": build.broker_vm_id or "",
        "result_template_id": build.result_template_id or "",
        "result_template_name": build.result_template_name or "",
        "status": build.status,
        "contract_report": build.contract_list,
        "prepare_method": build.prepare_method or "",
        "prepare_detail": build.prepare_detail or "",
        "error_message": build.error_message or "",
        "job_id": build.job_id or "",
        "keep_build_environment": bool(build.keep_build_environment),
        "workgroup": build.workgroup or "",
        "created_by": build.created_by or "",
        "created_at": (build.created_at or datetime.utcnow()).isoformat(),
    }


__all__ = [
    "TemplateBuildError", "STATUS_BUILDING", "STATUS_PREPARING", "STATUS_BAKING",
    "STATUS_READY", "STATUS_FAILED", "STATUS_DISCARDED",
    "render_runner", "render_runner_unit", "render_install_script",
    "check_contract", "contract_ok", "prepare_broker_vm", "run_template_build",
    "discard", "get", "serialize", "CHECK_PASS", "CHECK_WARN", "CHECK_FAIL",
]
