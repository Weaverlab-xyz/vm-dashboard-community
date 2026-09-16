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
before this. ``docs/profiles/pov/skytap.md#the-template-contract`` requires the broker VM to
carry a metadata runner: the platform hands ``user_data`` to the guest and *nothing executes
it*, so a template whose broker cannot fetch and run its own bootstrap produces a POV that
comes up, bills, and never enrols an agent. That runner has lived only as an example in a
Markdown file for a human to copy into an image. Here it is generated — from the same marker
constants ``pov_broker`` writes into the payload the runner has to recognise, so the two
cannot drift — and installed over one short-lived SSH session.

Two boundaries this module holds deliberately:

**The Windows Resource Broker VM is checked, never prepared.** Its installer is staged by
the customer (see ``docs/profiles/pov/design/resource-broker.md``) and there is no WinRM route from
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

# The container runtime the broker VM must have, and where its packages come from.
#
# **A runner without a runtime is the same failure, one line later.** The bootstrap the
# dashboard injects ends in `docker run`, and everything above that line is `mkdir`, a
# heredoc and two `|| true`s — so `docker run` is the first line in it that can fail. A
# broker VM with a perfect runner and no Docker reads the payload, dies on that line, and
# re-reads it every twenty seconds forever, while the POV page says `enrolling` and names
# nothing. That is indistinguishable from having no runner at all, which is why installing
# one without the other was only ever half a fix.
#
# **Docker CE from Docker's own repo FIRST, and the distro's `podman` + `podman-docker`
# when that does not land.** Preference, not exclusion, and the ordering carries the whole
# argument:
#
# Docker CE leads because the agent does not drive a CLI — it speaks the Docker Engine API
# over the socket directly, and the parts of it most likely to differ under Podman are the
# ones that have already produced a bug here (binary log frames, a privileged sibling
# holding /dev/net/tun). What this feature is tested against is what it should install
# where it can.
#
# Podman follows because "where it can" is not everywhere, and the gap is not exotic: this
# repo is an internet CDN, and a lab guest routinely reaches its own distro mirror and
# nothing else. A Skytap AlmaLinux 8 broker is exactly that guest. The alternative to
# Podman there is not Docker CE, it is a POV that sits at `enrolling` and says nothing —
# and Podman serves the same Engine API on the same socket path, so the agent cannot tell.
# Podman on a lab VM is a decision this project has made deliberately (2026-09-16); it is
# not a silent degradation.
#
# **What is NOT optional either way is the socket.** `command -v docker` and a running
# Engine API are different facts under Podman, and only the second one is the agent's. See
# the socket block in `render_docker_install`.
#
# A base image that already carries either is left alone: reinstalling over a working
# runtime is how a build breaks a template that was fine.
DOCKER_PACKAGES = "docker-ce docker-ce-cli containerd.io"
# The host on its own, because it is named in three places that must agree: the repo URL
# below, the refusal an operator reads when neither runtime lands, and the sentence the
# broker job adds to an enrolment timeout. It is also the one prerequisite on the whole
# Skytap list that nothing on this side can test, so "which host do I have to let out" is
# a question worth being able to answer from one constant.
DOCKER_REPO_HOST = "download.docker.com"
DOCKER_REPO_BASE = f"https://{DOCKER_REPO_HOST}/linux"

# The fallback, from the guest's OWN repository. Reached when Docker CE does not land,
# which on a lab guest is usually not a broken repo but an unreachable one: a Skytap
# AlmaLinux 8 broker resolves `appstream` and `baseos` perfectly and cannot reach Docker's
# CDN at all, and `dnf install podman-docker` there pulls 19 packages without leaving the
# lab. That guest is the common case, not the exotic one.
#
# **`podman-docker` is not optional garnish, it is the half that makes this work.** Podman
# alone gives no `docker` command and, more importantly, no AGENT_SOCKET_PATH: the agent
# speaks the Engine API over that path directly and never runs a CLI. podman-docker ships
# both the shim and the symlink. Neither, however, STARTS anything -- see the socket block
# in `render_docker_install`, which is the part that turns this from a trap into a runtime.
PODMAN_PACKAGES = "podman podman-docker"

# Where the agent expects the Engine API, and where Podman actually puts it.
#
# The first is `pov_broker`'s constant rather than a second spelling of the same path: the
# bootstrap MOUNTS that path into the agent container, so an install that verified a
# different one would pass while the agent found nothing. One literal, two readers.
AGENT_SOCKET_PATH = pov_broker.GUEST_DOCKER_SOCKET
PODMAN_SOCKET_PATH = "/run/podman/podman.sock"

# What the probe writes into `prepare_detail` when the guest has no runtime, and the
# string anything reading that column back has to match on.
#
# A constant because it is now WRITTEN in one place and READ in another: a build records it
# here, and `runtime_gap_for_template` below answers a broker job with it weeks later, on a
# POV whose enrolment timed out. Two spellings of it would mean the read silently never
# matches — which is the same silence this whole path exists to end.
DOCKER_MISSING_MARKER = "docker: MISSING"

# What the post-install probe asks the guest. One constant because it is read on both
# paths — after a success, to name what landed, and after a failure, to name which half.
#
# It asks `is-enabled` as well as `--version` because the state that matters to a TEMPLATE
# is the one that survives a bake and a boot: a `docker` that answers today because
# somebody started it by hand is a template every POV comes up broken from. The unit is
# only asked about when there is one, so a `podman-docker` guest is not reported as
# disabled for having no `docker.service` to enable.
_STATE_PROBE = ("systemctl is-active dashboard-bootstrap-runner 2>/dev/null "
                "|| echo 'runner: INACTIVE'; "
                f"docker --version 2>/dev/null || echo '{DOCKER_MISSING_MARKER}'; "
                # The socket, reported separately from the command, because under Podman
                # they disagree and only this one is the agent's. A Runner detail reading
                # `podman version 4.9.4` beside no socket is a green-looking line about a
                # template whose every POV enrols and then fails every job.
                f"echo \"engine socket: $([ -e {AGENT_SOCKET_PATH} ] "
                f"&& echo present || echo ABSENT)\"; "
                "if systemctl cat docker.service >/dev/null 2>&1; then "
                "echo \"docker at boot: $(systemctl is-enabled docker 2>&1)\"; "
                # Same question for a Podman guest, which has no docker.service to ask
                # about and used to leave this line off the detail entirely.
                "elif systemctl cat podman.socket >/dev/null 2>&1; then "
                "echo \"podman at boot: socket=$(systemctl is-enabled podman.socket 2>&1) "
                "service=$(systemctl is-enabled podman.service 2>&1)\"; fi")


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
    ``docs/profiles/pov/skytap.md`` documents.
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
      # The capture is \\([^"\\\\]|\\\\.\\)* - a JSON string body - and NOT `.*`. A greedy
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


def render_docker_install(*, require_enabled_at_boot: bool = True,
                          socket_path: str = "") -> str:
    """The container runtime install, as a ``/bin/sh`` block the install script appends.

    Its own renderer rather than more lines inside ``render_install_script`` because it is
    the half that can actually fail — it reaches a package repository over the network from
    inside the guest — and a thing that can fail is a thing to be able to test on its own.
    It is now also the half ``pov_broker.render_bootstrap`` emits into the payload itself,
    which is the reason this is a renderer and not a section of a script: one expression of
    the install, reached from a bake and from a POV that never had one.

    Three properties, and each is a way a build produces a template that looks fine:

    1. **It is idempotent, and "already present" means left alone.** A base image with
       Docker, or with ``podman`` + ``podman-docker`` aliasing it, satisfies the contract.
       Reinstalling over either is how a build breaks a template that worked.
    2. **It verifies the daemon runs, and separately that it is enabled at boot.** A
       runtime installed and not started fails the bootstrap in exactly the same place as
       one never installed — and `dnf install docker-ce` leaves the unit *disabled* on the
       RHEL family, so "running now" is worth nothing for a template. A template is baked
       and then booted, for every POV, so enabled-at-boot is the property that survives the
       bake and the only one worth gating on. Found the hard way on a guest that was
       running because somebody had just started it by hand.
    3. **Docker CE first, the distro's Podman second, and a refusal that names both.**
       Every step of the Docker CE install is non-fatal, because failing it is the normal
       case on a lab guest with no route to ``download.docker.com`` — and the answer there
       is ``podman`` + ``podman-docker`` out of the guest's own repository, which serves the
       same Engine API on the same socket. Only when neither lands does this exit non-zero,
       naming the distro and both attempts, so the Runner detail says the thing to fix
       rather than a package manager's last error.
    4. **It verifies the SOCKET, not just the command.** This is the one that bites: under
       Podman ``docker`` is a shim that answers happily while nothing is listening, because
       installing ``podman-docker`` does not start ``podman.socket`` and the
       ``/var/run/docker.sock`` symlink it ships is created by ``systemd-tmpfiles`` at the
       next boot. A guest in that state passes every CLI check, enrols, comes up green, and
       then fails every Gateway and Config-Management job on a socket nobody started. So
       the socket is brought up here and then checked as its own gate.

       **Enabled AND started, and neither word is redundant.** ``systemctl enable`` writes
       a symlink and starts nothing — a guest enabled and not started has no API until it
       is rebooted. ``start`` without ``enable`` is the mirror image and is the trap
       ``docker-ce`` sets on the RHEL family. Socket activation is tried first because it
       is the idiomatic shape; ``podman.service`` is the fallback for a guest where that
       produced no listening socket, and is what an operator reaches for by hand.

       The boot gate asks the same question of Podman, which it previously skipped
       entirely: ``systemctl cat docker.service`` is false on a podman-docker guest, so the
       whole check fell through and a template could bake with a socket that answers only
       because the install had just started it. Either ``podman.socket`` or
       ``podman.service`` being enabled satisfies it — an operator who fixed a guest by
       hand usually has the second, and failing that guest would be wrong.

    ``require_enabled_at_boot=False`` drops property 2's *second* gate, and only the second.
    That gate is a statement about a TEMPLATE — baked now, booted later, for every POV — and
    it is the wrong question to fail a live POV on. The bootstrap runs on a guest that is
    already up and needs a runtime in the next ten minutes; refusing it because
    ``systemctl enable`` did not take would abort an install that was about to work and
    leave the POV with no agent, which is strictly worse than a broker that comes back
    after a power cycle. The verify above it still runs on both paths: "it works" is never
    the part that gets skipped.

    ``socket_path`` is where the caller's bootstrap will MOUNT the Engine API from, and
    defaults to the one constant ``pov_broker`` mounts (:data:`AGENT_SOCKET_PATH`). It is
    a parameter for one further reason worth stating plainly: property 4 is a gate with two
    sides, and the machines this repo is developed on cannot create an ``AF_UNIX`` socket
    at all, so a test that could only ever see the failing side would pin half a rule.

    Written without ``${...}`` parameter expansion on purpose: every variable it reads
    from ``/etc/os-release`` is initialised to empty first, because the script runs under
    ``set -u`` and a Debian guest has no ``VERSION_ID`` field to speak of.
    """
    # Named here rather than interpolated as an expression, so the f-string below reads as
    # the shell it is.
    sock = str(socket_path or "").strip() or AGENT_SOCKET_PATH
    return f"""# --- the container runtime ----------------------------------------
# The injected bootstrap ends in `docker run`, and that is the first line in it that can
# fail. A broker with a runner and no runtime re-runs the payload every {_RUNNER_INTERVAL_S}s forever
# while the POV page says `enrolling` and names nothing.
if command -v docker >/dev/null 2>&1; then
  echo "docker: already present, left alone"
else
  ID=""
  ID_LIKE=""
  VERSION_ID=""
  VERSION_CODENAME=""
  if [ -r /etc/os-release ]; then
    . /etc/os-release
  fi

  case " $ID $ID_LIKE " in
    *" ubuntu "*|*" debian "*)
      case " $ID $ID_LIKE " in
        *" ubuntu "*) DOCKER_REPO_DIR=ubuntu ;;
        *) DOCKER_REPO_DIR=debian ;;
      esac
      export DEBIAN_FRONTEND=noninteractive
      apt-get -y -q update || true
      # Every step from here is non-fatal, and the podman fallback after this `case` is
      # why: no VERSION_CODENAME, no route to Docker's CDN and no gpg key are three
      # different ways to end up without Docker CE, and the answer to all three is the
      # distro's own container tools rather than a guest nobody can use.
      if [ -n "$VERSION_CODENAME" ]; then
        apt-get -y -q install ca-certificates curl gnupg || true
        install -m 0755 -d /etc/apt/keyrings
        if curl -fsSL "{DOCKER_REPO_BASE}/$DOCKER_REPO_DIR/gpg" -o /etc/apt/keyrings/docker.asc; then
          chmod a+r /etc/apt/keyrings/docker.asc
          echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] {DOCKER_REPO_BASE}/$DOCKER_REPO_DIR $VERSION_CODENAME stable" \\
            > /etc/apt/sources.list.d/docker.list
          apt-get -y -q update || true
          apt-get -y -q install {DOCKER_PACKAGES} || true
        else
          echo "could not fetch Docker's apt key from {DOCKER_REPO_BASE}; trying the distro's own container tools." >&2
        fi
      else
        echo "this guest's /etc/os-release names no VERSION_CODENAME and Docker's apt repository is per-release; trying the distro's own container tools." >&2
      fi
      ;;
    *" rhel "*|*" centos "*|*" fedora "*)
      # ID before ID_LIKE: an AlmaLinux guest's ID_LIKE is "rhel centos fedora", so a
      # family match that looked for fedora first would send it to the wrong repo.
      case "$ID" in
        fedora) DOCKER_REPO_DIR=fedora ;;
        *) DOCKER_REPO_DIR=centos ;;
      esac
      DOCKER_MAJOR=$(echo "$VERSION_ID" | cut -d. -f1)
      if [ -z "$DOCKER_MAJOR" ]; then
        echo "this guest's /etc/os-release names no VERSION_ID and Docker's yum repository is per-major-release; trying the distro's own container tools." >&2
      else
      cat > /etc/yum.repos.d/docker-ce.repo <<DASHBOARD_DOCKER_REPO_EOF
[docker-ce-stable]
name=Docker CE Stable
baseurl={DOCKER_REPO_BASE}/$DOCKER_REPO_DIR/$DOCKER_MAJOR/$(uname -m)/stable
enabled=1
gpgcheck=1
gpgkey={DOCKER_REPO_BASE}/$DOCKER_REPO_DIR/gpg
DASHBOARD_DOCKER_REPO_EOF
      # --allowerasing: on a RHEL-family guest carrying the distro's own container stack,
      # containerd.io replaces runc, and without this dnf reports a dependency conflict
      # rather than resolving it.
      #
      # Non-fatal, because the fallback below is a real answer. This repo reaches
      # download.docker.com, which a lab guest very often cannot: an AlmaLinux 8 broker
      # with perfectly good access to `appstream` and none to Docker's CDN is the exact
      # shape that leaves a POV at `enrolling`.
      if command -v dnf >/dev/null 2>&1; then
        dnf -y install {DOCKER_PACKAGES} --allowerasing || true
      else
        yum -y install {DOCKER_PACKAGES} || true
      fi
      fi
      ;;
    *)
      echo "no Docker CE repository for '$ID'; trying the distro's own container tools." >&2
      ;;
  esac

  # {PODMAN_PACKAGES} from the DISTRO's repository, when Docker CE did not land. Podman
  # serves the same Engine API, and `podman-docker` provides both the `docker` command and
  # the /var/run/docker.sock symlink the agent's socket path depends on -- so this is a
  # substitution the agent cannot tell apart, not a downgrade it has to cope with.
  #
  # It is the FALLBACK and not the default on purpose: Docker CE is what this feature is
  # tested against, and the places Podman differs (binary log frames, a privileged sibling
  # holding /dev/net/tun) are places that have already produced bugs here. But a lab guest
  # that cannot reach Docker's CDN and can reach its own distro mirror is common, and a
  # working broker on Podman beats a POV that sits at `enrolling` saying nothing.
  if ! command -v docker >/dev/null 2>&1; then
    if command -v dnf >/dev/null 2>&1; then
      dnf -y install {PODMAN_PACKAGES} || true
    elif command -v yum >/dev/null 2>&1; then
      yum -y install {PODMAN_PACKAGES} || true
    elif command -v apt-get >/dev/null 2>&1; then
      apt-get -y -q install {PODMAN_PACKAGES} || true
    fi
  fi

  if ! command -v docker >/dev/null 2>&1; then
    echo "could not install a container runtime on this guest ('$ID'): neither Docker CE from {DOCKER_REPO_HOST} nor {PODMAN_PACKAGES} from the distro's own repository. The injected bootstrap ends in 'docker run', so this broker will sit at 'enrolling' with nothing to say why. Install one by hand on this VM." >&2
    exit 1
  fi
fi

# Enable and start it whether this script installed it or found it. Neither is an error
# worth stopping on here: the checks below are what actually decide.
if command -v systemctl >/dev/null 2>&1; then
  systemctl enable docker >/dev/null 2>&1 || true
  systemctl start docker >/dev/null 2>&1 || true
else
  service docker start >/dev/null 2>&1 || true
fi

# **Clear the debris a previous run left, before anything tries to link over it.**
# `docker run -v {sock}:...` against a host whose daemon is not running does not fail --
# the runtime CREATES the source as an empty DIRECTORY. The bootstrap has done exactly
# that on a guest whose podman was installed but whose socket had not been started yet,
# and every later run then mounts an empty directory into the agent.
#
# `rmdir` and never `rm -rf`: it removes the directory only if it is EMPTY, so debris goes
# and anything with real content in it stays and is reported below. `-L` first, because a
# working socket symlink also answers `-d` when it points at a directory, and removing
# that would be the opposite of the repair.
if [ -d {sock} ] && [ ! -L {sock} ]; then
  rmdir {sock} >/dev/null 2>&1 || true
fi

# **The socket, which is the part a CLI check cannot see.** The agent does not run `docker`:
# it speaks the Engine API over {sock} directly. Under Podman the `docker`
# command is a shim that works perfectly while NOTHING IS LISTENING -- `podman.socket` is
# not enabled by installing podman-docker, and the symlink podman-docker drops is created
# by systemd-tmpfiles at boot. So a guest can pass every CLI check here, enrol, come up
# green, and then fail every Gateway and Config-Management job on a socket that was never
# started. That failure reads as a permission or firewall problem and has cost days before.
if command -v systemctl >/dev/null 2>&1 && [ ! -S {sock} ]; then
  # ENABLE *and* START, and both words are load-bearing. `enable` only writes a symlink
  # into sockets.target.wants -- it starts nothing -- so a guest enabled and not started
  # has no API until it is rebooted, while `docker version` answers the whole time. The
  # reverse is the trap docker-ce sets on the RHEL family: started by hand, dead after the
  # next boot. Neither alone is a working broker.
  systemctl enable podman.socket >/dev/null 2>&1 || true
  systemctl start podman.socket >/dev/null 2>&1 || true
  # Now rather than at the next boot, and only ever as the symlink podman-docker itself
  # ships -- this does not invent a path, it stops waiting for a reboot to create one.
  systemd-tmpfiles --create >/dev/null 2>&1 || true
  # Socket activation is the idiomatic path and is tried first, above. This is the fallback
  # for a guest where it did not produce a listening socket -- an older podman, a
  # sockets.target that has already run, a unit whose activation is masked. `podman.service`
  # runs the same API persistently rather than on demand, which is the shape an operator
  # reaches for by hand (`systemctl enable podman; systemctl start podman`) when the socket
  # alone leaves them with nothing listening.
  if [ ! -S {PODMAN_SOCKET_PATH} ]; then
    systemctl enable podman.service >/dev/null 2>&1 || true
    systemctl start podman.service >/dev/null 2>&1 || true
  fi
  # `! -e` and not `! -S`, which is the difference between a link and a mess. `ln -s X DIR`
  # puts the link INSIDE the directory -- and `-n` does not save you, it only treats a
  # SYMLINK to a directory as a file. So a directory still standing here would get
  # `{sock}/podman.sock` created in it, which is invisible, fixes nothing, and makes the
  # `rmdir` this script's own refusal recommends fail with "Directory not empty".
  if [ ! -e {sock} ] && [ -S {PODMAN_SOCKET_PATH} ]; then
    ln -s {PODMAN_SOCKET_PATH} {sock} || true
  fi
  # `--restart unless-stopped` is in the bootstrap's `docker run`, and under Podman that
  # flag only survives a reboot when this unit is enabled. Best-effort: the metadata runner
  # brings the agent back anyway, just more slowly.
  systemctl enable podman-restart.service >/dev/null 2>&1 || true
fi

# Working, not merely installed. A package that landed beside a daemon that will not start
# fails the bootstrap in the same place as a guest that never had one.
if ! docker version >/dev/null 2>&1; then
  echo "the broker VM still has no working 'docker' after this script. The injected bootstrap ends in 'docker run', so every POV built from this template would sit at 'enrolling' with nothing in the job to say why. Fix the runtime on this VM before baking it." >&2
  exit 1
fi

# AND the socket answers, which is the one the AGENT depends on. Checked separately from
# the command above because they can disagree, and when they do it is always this one that
# is wrong -- see the socket block above.
#
# Stated as the two ways it is WRONG rather than as one `-S` assertion, because the two
# have different remedies and an operator reading either needs to be told which one they
# have. A dangling podman-docker symlink and a leftover mount directory both fail a single
# `-S` with one message that fits neither.
if [ -d {sock} ]; then
  echo "{sock} on this broker VM is a DIRECTORY, not a socket, and it is NOT EMPTY so this script would not remove it. A 'docker run -v {sock}:...' on a host whose daemon was not running creates one, and every later run then mounts an empty directory into the agent. Look at what is inside it, remove it, make sure the daemon is up, and press Broker again." >&2
  exit 1
fi
if [ ! -e {sock} ]; then
  echo "'docker' works on this broker VM but nothing is listening at {sock}. The agent speaks the Engine API over that socket directly and never runs the command, so it would start, enrol, come up green, and then fail every Gateway and Config-Management job. Under Podman run 'systemctl enable --now podman.socket'; under Docker check that dockerd is running." >&2
  exit 1
fi

""" + (_DOCKER_BOOT_GATE if require_enabled_at_boot else _DOCKER_BOOT_NOTE)


# The second gate, and the only part of the install that is about a TEMPLATE rather than
# about a working runtime. Split out so the broker's copy can omit exactly this and nothing
# else — see `render_docker_install`'s `require_enabled_at_boot`.
_DOCKER_BOOT_GATE = """# AND enabled at boot, which is the check that matters for a TEMPLATE. `dnf install
# docker-ce` leaves the unit disabled on the RHEL family, so a guest can pass the check
# above while being one power cycle from having no runtime at all - and a template is
# baked and then booted, every time, for every POV. "Running now" is worth nothing here.
if command -v systemctl >/dev/null 2>&1 && systemctl cat docker.service >/dev/null 2>&1; then
  if ! systemctl is-enabled docker >/dev/null 2>&1; then
    echo "docker is running on the broker VM but its unit is NOT enabled at boot, and 'systemctl enable docker' did not take. A template is baked and then booted, so every POV built from this one would come up with no daemon and sit at 'enrolling'. Enable it on this VM before baking." >&2
    exit 1
  fi
# The SAME rule for a Podman guest, which used to fall through this gate entirely. The
# `systemctl cat docker.service` above is false on podman-docker -- there is no docker
# unit to enable -- so the whole check was skipped, and a template could bake with a
# socket that answers right now because the install just started it and is not enabled at
# boot. Every POV from that template comes up with a working `docker` command and nothing
# listening: the exact failure this gate exists to stop, one unit name over.
elif command -v systemctl >/dev/null 2>&1 && systemctl cat podman.socket >/dev/null 2>&1; then
  # Either is a real answer: socket activation (podman.socket) or the API service running
  # persistently (podman.service). An operator who fixed this by hand usually has the
  # second, so demanding the first would fail a guest that is genuinely correct.
  if ! systemctl is-enabled podman.socket >/dev/null 2>&1 \\
     && ! systemctl is-enabled podman.service >/dev/null 2>&1; then
    echo "podman answers on this broker VM but NEITHER podman.socket NOR podman.service is enabled at boot. A template is baked and then booted, so every POV built from this one would come up with a working 'docker' command and nothing listening on the socket - which enrols, goes green, and fails every Gateway and Config-Management job. Run 'systemctl enable --now podman.socket' on this VM before baking." >&2
    exit 1
  fi
fi
echo "docker: ready and enabled at boot"
"""

# The same place in the script when the caller is a live POV. It still says what it found,
# because this text reaches a guest console an operator reads, but a disabled unit is a
# note here rather than a refusal: this VM is already running, and the agent it is about to
# start is worth more than the power cycle it may not survive.
_DOCKER_BOOT_NOTE = """if command -v systemctl >/dev/null 2>&1 && systemctl cat docker.service >/dev/null 2>&1; then
  if ! systemctl is-enabled docker >/dev/null 2>&1; then
    echo "NOTE: docker is running but its unit is not enabled at boot; this broker will have no runtime after a power cycle. Run 'systemctl enable docker' on this VM."
  fi
fi
echo "docker: ready"
"""


def render_install_script() -> str:
    """Runner + unit + enable + the container runtime, for one paste into a root shell.

    This is the fallback path, and it is offered on every build rather than only on a
    failed one: an SE baking a template on a platform with no published-service capability,
    or from a network with no route to a NAT-ed high port, needs it as the *primary* route
    and should not have to fail once to find it.

    **The runner is installed before the runtime, and that ordering is deliberate.** The
    runner is local, costs nothing and cannot really fail; the Docker install reaches a
    package repository from inside the guest and can. Landing the cheap half first means a
    guest with no route to ``download.docker.com`` still ends up with a runner — the
    template is one manual ``dnf install`` from correct instead of needing this script run
    again — while the exit status still says the script failed.

    Note for anyone tempted to put a timeout around this later: the package install makes
    it a multi-minute script, where the runner alone was a multi-second one.
    """
    runner = render_runner()
    unit = render_runner_unit()
    docker = render_docker_install()
    return f"""#!/bin/sh
# Install the dashboard's metadata runner and the container runtime it needs. Run as root
# on the broker VM, then bake the environment into a template.
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

{docker}"""


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

    * **A resolvable broker VM.** Without one the POV comes up, bills, and the Broker
      column reads ``none`` forever. Resolved by ``pov_broker.resolve_broker_candidate``,
      the same ladder the POV itself uses, so a template that verifies is a template the
      POV can broker. A blank ``broker_vm_name`` means auto-detect (the only Linux VM),
      not the conventional name.

      One caveat this cannot close: the operator's per-VM OS override lives on a POV's
      rows, and a template read has no POV, so a template whose only Linux guest reports
      no OS fails here while the POV built from it resolves fine once the OS is set on
      its VMs tab. One rule, two different worlds.
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
    # Blank stays blank: it means auto-detect, and coercing it to the conventional name
    # here would make every Verify demand a name the template never agreed to.
    wanted = (broker_vm_name or "").strip()

    broker = None
    try:
        broker = pov_broker.resolve_broker_candidate(
            vms, typed_name=wanted,
            # No env: this is a template, so only the conventional role names are known.
            claimed_names=pov_broker.claimed_vm_names(),
            remedy=("Rename the VM in the template, or build with the name this "
                    "template actually uses."))
    except pov_broker.BrokerError as exc:
        # The resolver already names what it found and what to do about it, so this
        # passes the sentence straight through rather than composing a second one.
        out.append(_result("broker VM", CHECK_FAIL, str(exc)))
    if broker is not None:
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

    # By identity, not by name. The broker may have been INFERRED rather than named, so a
    # name comparison would count it as a workload VM and report a bare broker template
    # as having something to demonstrate. Identity also settles the case of two VMs that
    # share a name, which a name comparison silently got wrong before.
    workload = [v for v in vms if v is not broker]
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

async def _ssh_install(host: str, port: int, logins: list[tuple[str, str]], *,
                       sleep=None) -> str:
    """Install the runner and the runtime over SSH, each login in turn. Returns a summary.

    **Two loops, because two different failures wear the same costume.** A brand-new guest
    answers TCP before sshd is ready, so *unreachable* has to be retried on a ladder — ten
    attempts, fifteen seconds apart. A login sshd actively refused is not that: it will be
    refused just as firmly in fifteen seconds, and the next login may not be. So the outer
    loop is readiness and the inner one is identity, and a refusal moves straight to the
    next login without spending any of the ladder. Nesting the ladder inside the logins
    instead would multiply a seven-minute worst case by however many credentials the VM
    happens to carry.

    A guest that answers and refuses **every** login therefore fails at once. That is the
    one thing a rejected password tells us for certain: waiting will not fix it.

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

    if not logins:
        # candidates() cannot return an empty list, but a caller that hand-rolled one would
        # otherwise spend the whole ladder discovering it had nothing to try.
        raise TemplateBuildError(
            "no usable login was found for the broker VM, so the runner cannot be "
            "installed over SSH. Use the install script from the builder page instead.")

    wait = sleep or asyncio.sleep
    script = render_install_script()
    last: Exception | None = None
    for attempt in range(_SSH_ATTEMPTS):
        refused: list[str] = []
        for username, password in logins:
            try:
                async with asyncssh.connect(
                        host, port=port, username=username, password=password,
                        known_hosts=None,
                        connect_timeout=_SSH_CONNECT_TIMEOUT_S) as conn:
                    # Piped to `sh -s` rather than written and executed: no file is left on
                    # a VM that is about to become a template, and nothing depends on a
                    # writable path the guest may not have.
                    result = await conn.run("sudo -n sh -s || sh -s", input=script,
                                            check=False)
                    if result.exit_status != 0:
                        stderr = (result.stderr or "").strip()[:400]
                        # Which half landed is the useful part. The script installs the
                        # runner first and the container runtime second, so the common
                        # failure leaves a template with a runner and no Docker — which
                        # dies at `docker run` on every POV built from it, not at
                        # enrolment. Probing says that; an exit status does not.
                        probe = await conn.run(_STATE_PROBE, check=False)
                        landed = " ".join((probe.stdout or "").split())[:200]
                        raise TemplateBuildError(
                            f"the install exited {result.exit_status} on the broker VM as "
                            f"{username}{': ' + stderr if stderr else ''}. State on the VM "
                            f"now: {landed or 'unknown'}. It installs as root, so a "
                            f"credential without sudo gets exactly this far. The template "
                            f"can still be baked and the script pasted in by hand.")
                    check = await conn.run(_STATE_PROBE, check=False)
                    detail = " ".join((check.stdout or "").split())[:300]
                    # Naming the login that worked is the whole point of trying several: the
                    # build row is where an SE finds out which one the VM accepted.
                    return f"runner and runtime installed over SSH as {username}; " \
                           f"{detail}" if detail \
                        else f"runner and runtime installed over SSH as {username}"
            except TemplateBuildError:
                # This login worked and the install did not. Another login does not fix a
                # script that ran and failed, and re-running it under one would re-apply a
                # half-applied install.
                raise
            except asyncssh.PermissionDenied as exc:
                # sshd was there and said no. Move to the next login immediately: this one
                # will not start working later, and the ladder is not for this.
                last = exc
                refused.append(username)
                continue
            except Exception as exc:  # noqa: BLE001
                # Nothing answered. Trying the other logins against a port that is not
                # listening only multiplies the wait, so start the whole set over.
                last = exc
                break
        else:
            # Every login was refused by a server that was there to refuse them.
            if refused and len(refused) == len(logins):
                many = (f"all {len(refused)} stored credentials" if len(refused) > 1
                        else "the stored credential")
                raise TemplateBuildError(
                    f"the broker VM at {host}:{port} refused {many} "
                    f"(tried: {', '.join(refused)}). SSH answered, so this is the login and "
                    f"not the route — correct the credential on that VM in the lab platform "
                    f"and build again, or install the runner by hand from the builder page.")

        if attempt < _SSH_ATTEMPTS - 1:
            logger.info("template build: SSH to %s:%s not ready (%s); retrying",
                        host, port, last)
            await wait(_SSH_RETRY_WAIT_S)

    raise TemplateBuildError(
        f"could not reach the broker VM over SSH at {host}:{port} after "
        f"{_SSH_ATTEMPTS} attempts ({last}). This connects to a NAT-ed high port on the "
        f"lab platform, not to the API host — an egress rule that allows only HTTPS to the "
        f"API URL will block it. Use the install script from the builder page instead.")


async def prepare_broker_vm(mod, env_id: str, vm: dict) -> str:
    """Publish SSH, install the runner and Docker, revoke the service. Returns a summary.

    ``docs/profiles/pov/skytap.md`` rules published services out for POV *wiring*, because a
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
        # `candidates`, not `pick`: this is the one credential consumer that authenticates
        # in process, so a VM carrying two logins is a question SSH answers rather than an
        # ambiguity to refuse -- and it takes them in the same ranked order `pick` decides
        # by, so the answer usually arrives on the first attempt. See
        # pov_credentials.candidates.
        logins = pov_credentials.candidates(
            entries, vm_label=f"the broker VM {vm.get('name')!r}",
            # A broker VM is Linux by construction — this function reaches it over SSH —
            # so `root` sorts to the front and the usual case authenticates first try.
            os_family="linux",
            # A build has no login field to fall back on, so the only remedy is on the
            # platform side. See pov_credentials.DEFAULT_REMEDY.
            remedy=("Add one on that VM in the lab platform and build again, or install "
                    "the runner by hand — the build carries no login of its own."))
        return await _ssh_install(published["external_ip"],
                                  int(published["external_port"]), logins)
    finally:
        with contextlib.suppress(Exception):
            await mod.delete_published_service(env_id, vm.get("id"), nic.get("id"),
                                               published["id"])


# ── the build job ────────────────────────────────────────────────────────────

def get(db: Session, build_id: str) -> PovTemplateBuild | None:
    return db.query(PovTemplateBuild).filter(PovTemplateBuild.id == build_id).first()


def runtime_gap_for_template(db: Session, *, platform: str, template_id: str) -> str:
    """One sentence when the template this POV came from baked with **no container
    runtime**, or ``""`` when it did not, or when nothing here knows.

    The point is whose knowledge this is. A broker that never enrols is silent by
    construction — the guest has no way to talk to the dashboard until the agent it cannot
    start has started — so the timeout message can only ever list *candidates*, and it lists
    three, of which two are usually fine. But this dashboard **baked that template** and
    wrote down what it found on the broker VM at the time: ``prepare_detail`` already says
    ``docker: MISSING``. Reading it back turns the one failure nobody can see into the one
    the job names first.

    Deliberately a WARNING and never a refusal, which is the whole reason it is read here
    and not in a preflight. The record is a fact about a bake that may be weeks old, and
    installing a runtime by hand on the broker VM is exactly what an operator does about
    it — so a preflight that refused would block the remedy for the problem it detected.
    The bootstrap now carries its own install (``pov_broker.render_bootstrap``), which makes
    this rarer still: by the time anybody reads this sentence, that install has also been
    tried and also failed, and the pair of facts is the diagnosis.

    ``""`` on anything unknown — no build row, a template somebody else authored, a build
    whose probe never ran. Silence is right there: this exists to add a fact, and inventing
    one about a template this dashboard never touched would be worse than the three
    candidates it is trying to improve on.
    """
    template_id = str(template_id or "").strip()
    if not template_id:
        return ""
    # The most recent build for this template. A template can be rebuilt under the same id
    # on some platforms, and an older row's probe describes a guest that no longer exists.
    build = (db.query(PovTemplateBuild)
               .filter(PovTemplateBuild.platform == str(platform or "").strip(),
                       PovTemplateBuild.result_template_id == template_id)
               .order_by(PovTemplateBuild.created_at.desc())
               .first())
    if build is None or DOCKER_MISSING_MARKER not in (build.prepare_detail or ""):
        return ""
    return (f"This dashboard built that template ({build.name}) on "
            f"{build.created_at:%Y-%m-%d} and recorded '{DOCKER_MISSING_MARKER}' on its "
            f"broker VM, so it almost certainly has no container runtime: the payload runs "
            f"as far as 'docker run' and dies there, every {_RUNNER_INTERVAL_S} seconds, "
            f"saying nothing. The bootstrap tries to install one now, which needs the guest "
            f"to reach either {DOCKER_REPO_HOST} or its own distro repository. Install a "
            f"container runtime on the broker VM and re-bake the template to fix it for "
            f"every POV.")


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
            # Blank stays blank -- auto-detect. See check_contract.
            wanted = (build.broker_vm_name or "")
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

            # The SAME resolver the contract check and the POV itself use. Two
            # expressions of this rule would let the builder PREPARE one VM while a POV
            # from the baked template ENROLS another -- and neither half would look
            # wrong on its own. Suppressed rather than fatal: contract_ok above has
            # already decided whether a refusal blocks the bake, and the
            # `broker is None` path below degrades to prepare_method = "skipped".
            broker = None
            with contextlib.suppress(pov_broker.BrokerError):
                broker = pov_broker.resolve_broker_candidate(
                    vms, typed_name=wanted,
                    claimed_names=pov_broker.claimed_vm_names())
            if broker is not None:
                build.broker_vm_id = str(broker.get("id") or "")
                db.commit()

            # ── prepare ──────────────────────────────────────────────────────
            # Never fatal. A template that bakes without the runner is still a usable
            # template — the operator pastes the script in, which is what they do today.
            job_service.update_progress(db, job_id, 60,
                                        "Installing the metadata runner and Docker…")
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
        # What was typed, so "" means auto-detect and the page can say so.
        "broker_vm_name": build.broker_vm_name or "",
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
    "render_runner", "render_runner_unit", "render_docker_install",
    "render_install_script",
    "check_contract", "contract_ok", "prepare_broker_vm", "run_template_build",
    "discard", "get", "serialize", "CHECK_PASS", "CHECK_WARN", "CHECK_FAIL",
]
