"""The template builder: the runner it generates, the contract it checks, the job it runs.

The runner is the reason this feature exists. `docs/integrations/skytap.md`'s template
contract requires the broker VM to carry one, because Skytap hands `user_data` to the guest
and *nothing executes it* — and until now it lived only as an example in a Markdown file
for a human to copy. Four of its properties are load-bearing and each is silent when wrong,
so they are pinned here against the generated text:

  * **It is valid `/bin/sh`.** The markers are interpolated into `case` patterns, and
    `BOOTSTRAP_BEGIN` carries a version and a space — pasting the raw constant in produces
    a script that does not parse, on a VM nobody will ever SSH into to find out.
  * **It matches the marker STEM, not the version.** A runner baked into a template outlives
    this dashboard's payload format; one pinned to `v1` stops recognising a `v2` payload on
    every template already in the field.
  * **Both markers are required.** Half a payload is the half that deletes the running agent
    and its state volume.
  * **The already-ran marker is the payload's HASH.** A reboot must not re-run; a
    re-injection with a fresh enrolment code must. A boolean gets one of those right.

And the container runtime it installs beside it, which was the other half of the same
failure: the injected bootstrap ends in `docker run`, so a broker VM with a perfect runner
and no Docker re-reads the payload every twenty seconds forever while the POV page says
`enrolling` and names nothing. Found live on an AlmaLinux 8 broker carrying neither Docker
nor Podman.

And the job's own rules, which mirror the POV provision's for the same reasons:

  * **The scratch environment id is committed before anything else can fail.** An
    environment on the platform and not in this database is the one failure nothing can
    clean up — and a scratch one bills until somebody notices.
  * **The published service is revoked even when the install raises.** One left behind is
    baked into every POV built from the template.
  * **A failed prepare does not fail the build.** A template that bakes without the runner
    is still usable; the operator pastes the script in, which is what they do today.

No network, no app, no database: the platform adapter is a stub and the job is driven
against SQLite in a temp file.

Runs under pytest, or standalone:
    python tests/test_pov_template_builder.py
"""
import asyncio
import contextlib
import os
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-template-builder")

from web_dashboard.services import pov_broker  # noqa: E402
from web_dashboard.services import pov_template_builder as b  # noqa: E402


# ── the runner ───────────────────────────────────────────────────────────────

def _sh_parses(script: str) -> tuple[bool, str]:
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write(script)
        path = fh.name
    try:
        p = subprocess.run(["sh", "-n", path], capture_output=True, text=True)
        return p.returncode == 0, (p.stderr or "").strip()
    finally:
        os.unlink(path)


def test_the_runner_is_valid_shell():
    """The regression this exists for: BOOTSTRAP_BEGIN is `# BEGIN-… v1`, and the shell
    splits an unquoted `case` pattern on spaces. Interpolating the raw constant produces a
    script that does not parse — on a VM nobody will ever log into to find out."""
    ok, err = _sh_parses(b.render_runner())
    assert ok, f"the generated runner is not valid /bin/sh: {err}"


def test_the_install_script_is_valid_shell_inside_and_out():
    script = b.render_install_script()
    ok, err = _sh_parses(script)
    assert ok, f"the install script is not valid /bin/sh: {err}"
    # The heredoc hides the runner from `sh -n`, so check the embedded copy too — that is
    # the text that actually lands on the VM.
    inner = script.split("DASHBOARD_RUNNER_EOF")[1]
    ok, err = _sh_parses(inner)
    assert ok, f"the runner embedded in the install script does not parse: {err}"


# ── the container runtime ────────────────────────────────────────────────────
#
# A runner without a runtime is the same failure one line later: the injected bootstrap
# ends in `docker run`, everything above it is a mkdir, a heredoc and two `|| true`s, and a
# broker VM with no Docker therefore re-reads the payload every twenty seconds forever
# while the POV page says `enrolling` and names nothing. This is the half of the install
# that can actually fail — it reaches a package repository from inside the guest — so it
# gets run, not just read.

def _run_block(block: str, prelude: str) -> subprocess.CompletedProcess:
    """Run a generated shell block with commands stubbed as shell functions.

    Functions rather than executables on ``PATH``: a function shadows a real command for
    ``command -v`` as well as for a call, and it needs no temp directory, no execute bit
    and no POSIX-path conversion — so this test reads the same on a Windows workstation as
    it does in CI.
    """
    return subprocess.run(["sh", "-s"], input=prelude + "\n" + block,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


# `apt-get` cannot be a shell function — POSIX function names take no hyphen and dash
# refuses one outright — so the Debian branch is fenced off with an empty PATH instead:
# if this block ever falls through to a package install, every command in it is
# not-found and the run fails. `dnf` and `yum` are named because they *can* be, and a
# named stub says which branch ran instead of only that something did.
_NO_PACKAGES = """
PATH=""
dnf() { echo "PACKAGE-MANAGER-RAN" >&2; return 9; }
yum() { echo "PACKAGE-MANAGER-RAN" >&2; return 9; }
systemctl() { return 0; }
service() { return 0; }
"""


def test_the_docker_install_is_valid_shell():
    ok, err = _sh_parses(b.render_docker_install())
    assert ok, f"the generated docker install is not valid /bin/sh: {err}"


def test_the_generated_scripts_are_ascii_only():
    """Because the operator of last resort types this into the platform's own console,
    where there is no clipboard. An em dash or a box-drawing rule in a comment is a
    character a console keymap may not offer and a guest codepage may mangle, and it buys
    nothing: none of this is prose anyone reads for pleasure. The section rules and dashes
    belong in the Python around it, which is only ever read in an editor."""
    for name, text in (("runner", b.render_runner()),
                       ("unit", b.render_runner_unit()),
                       ("docker install", b.render_docker_install()),
                       ("install script", b.render_install_script())):
        bad = sorted({c for c in text if ord(c) > 127})
        assert not bad, f"the generated {name} carries non-ASCII: {bad!r}"


def test_a_guest_that_already_has_a_runtime_is_left_alone():
    """Reinstalling over a working runtime is how a build breaks a template that was
    fine — including the `podman` + `podman-docker` guest the contract accepts."""
    p = _run_block(b.render_docker_install(),
                   _NO_PACKAGES + "\ndocker() { return 0; }\n")
    assert p.returncode == 0, f"rc={p.returncode} {p.stderr[:300]}"
    assert "PACKAGE-MANAGER-RAN" not in (p.stdout + p.stderr), \
        "a guest that already has docker must not have packages installed over it"
    assert "already present" in p.stdout, p.stdout


def test_a_runtime_that_does_not_answer_fails_the_install():
    """The regression this exists for: a package that landed beside a daemon that will not
    start fails the bootstrap in exactly the same place as a guest that never had one, and
    the install script is the last moment anything is watching."""
    p = _run_block(b.render_docker_install(),
                   _NO_PACKAGES + "\ndocker() { case \"$1\" in version) return 1 ;; esac; return 0; }\n")
    assert p.returncode != 0, "a runtime that cannot answer `docker version` must fail loudly"
    assert "enrolling" in p.stderr, \
        f"the refusal must name the symptom an SE would otherwise chase: {p.stderr[:300]}"


def test_the_install_script_installs_the_runner_before_the_runtime():
    """Ordering, and it is deliberate. The runner is local and cannot really fail; the
    runtime reaches a package repository and can. Landing the cheap half first leaves a
    guest with no route to Docker's repo one manual install from correct, rather than
    needing the whole script run again."""
    script = b.render_install_script()
    runner_at = script.index(b.RUNNER_PATH)
    docker_at = script.index("download.docker.com")
    assert runner_at < docker_at, \
        "the runtime install must come after the runner, or a repo with no route costs " \
        "the runner too"


def test_the_install_script_covers_both_package_families():
    """A POV broker template may be RHEL-family or Debian-family, and one that installs on
    only one of them is a builder that works for half the catalogue."""
    script = b.render_install_script()
    for needle in ("apt-get", "docker-ce", "download.docker.com",
                   "/etc/apt/sources.list.d/docker.list",
                   "/etc/yum.repos.d/docker-ce.repo"):
        assert needle in script, f"the install script never mentions {needle}"
    assert "dnf -y install" in script and "yum -y install" in script, \
        "a RHEL 7-era guest has no dnf; both package managers are named on purpose"


def test_the_rhel_repo_is_chosen_by_id_and_not_by_id_like():
    """AlmaLinux — the distro this was found on — has `ID_LIKE="rhel centos fedora"`. A
    family match that looked for fedora first would send every Alma and Rocky guest to a
    repository directory that does not carry its major version."""
    block = b.render_docker_install()
    fedora_by_id = block.index('case "$ID" in')
    assert block.index("fedora) DOCKER_REPO_DIR=fedora", fedora_by_id) > fedora_by_id, \
        "the fedora repo must be selected from ID, never from ID_LIKE"


def test_an_unsupported_distro_names_itself_and_says_the_runner_landed():
    """The build's Runner detail is the only place anyone reads this. "Unsupported" without
    the distro's own name is a line an SE cannot act on, and without "the runner is already
    in place" they re-run a script that had in fact done half its job."""
    block = b.render_docker_install()
    tail = block[block.index("cannot install a container runtime automatically"):]
    assert "$ID" in tail[:200], "the refusal must name the distro it found"
    assert "already in place" in tail[:400], \
        "the refusal must say the runner half landed, or the operator redoes it"


def test_the_runner_matches_the_marker_stem_not_the_version():
    runner = b.render_runner()
    assert "BEGIN-DASHBOARD-AGENT-BOOTSTRAP" in runner, runner[:400]
    # A runner baked into an image outlives the payload format's version number.
    assert "BOOTSTRAP v1" not in runner, \
        "the runner pins the payload version; a v2 payload would stop being recognised"


def test_the_stem_survives_a_renamed_or_reversioned_marker():
    assert b._marker_stem("# BEGIN-DASHBOARD-AGENT-BOOTSTRAP v1") == \
        "BEGIN-DASHBOARD-AGENT-BOOTSTRAP"
    assert b._marker_stem("# BEGIN-DASHBOARD-AGENT-BOOTSTRAP v27") == \
        "BEGIN-DASHBOARD-AGENT-BOOTSTRAP"
    assert b._marker_stem("# END-DASHBOARD-AGENT-BOOTSTRAP") == \
        "END-DASHBOARD-AGENT-BOOTSTRAP"


def test_the_runner_requires_both_markers():
    """Half a payload is the destructive half: the top removes the agent and its state
    volume, the bottom is the `docker run` that replaces them."""
    runner = b.render_runner()
    begin = b._marker_stem(pov_broker.BOOTSTRAP_BEGIN)
    end = b._marker_stem(pov_broker.BOOTSTRAP_END)
    assert f"*{begin}*{end}*)" in runner, \
        "the run guard does not require both markers"


def test_the_runner_keys_on_a_hash_not_a_flag():
    runner = b.render_runner()
    assert "sha256sum" in runner, runner
    assert '"$MARKDIR/last"' in runner, runner


def test_the_runner_polls_rather_than_reading_once():
    """The payload arrives AFTER the VM is up — an enrolment code lives fifteen minutes and
    a first boot is not bounded. A runner that reads once finds nothing and stops."""
    runner = b.render_runner()
    assert "while :; do" in runner, runner
    assert f"sleep {b._RUNNER_INTERVAL_S}" in runner, runner


def test_the_runner_truncates_at_the_end_marker():
    """The metadata document can carry fields after user_data, and this script runs as
    root. Nothing past the end marker may reach a shell."""
    runner = b.render_runner()
    end = b._marker_stem(pov_broker.BOOTSTRAP_END)
    assert f"sed -n '1,/{end}/p'" in runner, runner


def test_the_json_fallback_is_not_a_greedy_match():
    """A greedy `.*` runs to the last quote on the line, appending every field after
    user_data to a script this runner executes as root."""
    runner = b.render_runner()
    assert '"user_data"' in runner, runner
    assert 's/.*"user_data"[[:space:]]*:[[:space:]]*"\\(.*\\)".*/' not in runner, \
        "the user_data extraction is greedy and will append trailing JSON to the payload"


def test_the_unit_restarts_always():
    unit = b.render_runner_unit()
    assert "Restart=always" in unit, unit
    assert b.RUNNER_PATH in unit, unit


# ── the contract check ───────────────────────────────────────────────────────

def _vm(name, os_family="linux", network_type="automatic", nics=True):
    return {
        "id": name, "name": name, "os_family": os_family,
        "interfaces": ([{"id": f"nic-{name}", "ip": "10.0.0.1",
                         "network_type": network_type, "services": []}]
                       if nics else []),
    }


def _status(report, check):
    for row in report:
        if row["check"] == check:
            return row["status"]
    raise AssertionError(f"no {check!r} row in {report}")


def test_a_template_with_a_broker_and_a_workload_passes():
    report = b.check_contract([_vm("broker"), _vm("app")], "broker")
    assert _status(report, "broker VM") == b.CHECK_PASS, report
    assert _status(report, "broker network") == b.CHECK_PASS, report
    assert _status(report, "workload VMs") == b.CHECK_PASS, report
    assert b.contract_ok(report), report


def test_no_broker_vm_is_a_failure_that_names_what_it_found():
    report = b.check_contract([_vm("app"), _vm("db")], "broker")
    assert _status(report, "broker VM") == b.CHECK_FAIL, report
    assert not b.contract_ok(report), report
    detail = next(r["detail"] for r in report if r["check"] == "broker VM")
    assert "app" in detail and "db" in detail, detail


def test_the_broker_match_is_exact_not_fuzzy():
    """'contains broker' also matches a customer VM called password-broker, and the cost of
    that wrong answer is an agent installed on a machine nobody expected."""
    report = b.check_contract([_vm("password-broker")], "broker")
    assert _status(report, "broker VM") == b.CHECK_FAIL, report


def test_the_broker_match_is_case_insensitive():
    report = b.check_contract([_vm("Broker"), _vm("app")], "broker")
    assert _status(report, "broker VM") == b.CHECK_PASS, report


def test_a_manual_network_is_a_failure():
    """The metadata service answers ONLY on automatic networks. On a manual one the guest
    receives no bootstrap at all, which looks exactly like a missing runner."""
    report = b.check_contract([_vm("broker", network_type="manual"), _vm("app")], "broker")
    assert _status(report, "broker network") == b.CHECK_FAIL, report
    assert not b.contract_ok(report), report


def test_an_unknown_network_type_warns_rather_than_assuming_good():
    report = b.check_contract([_vm("broker", network_type=""), _vm("app")], "broker")
    assert _status(report, "broker network") == b.CHECK_WARN, report
    # A warning must not block a bake.
    assert b.contract_ok(report), report


def test_no_windows_guest_is_a_warning_not_a_failure():
    """Plenty of POVs wire only PRA and Entitle. Refusing to bake a Linux-only template
    would invent a requirement the POV flow does not have."""
    report = b.check_contract([_vm("broker"), _vm("app")], "broker")
    assert _status(report, "Resource Broker host") == b.CHECK_WARN, report
    assert b.contract_ok(report), report


def test_a_windows_guest_passes_the_resource_broker_check():
    report = b.check_contract([_vm("broker"), _vm("rb", os_family="windows")], "broker")
    assert _status(report, "Resource Broker host") == b.CHECK_PASS, report


def test_a_broker_only_template_warns_about_having_nothing_to_demonstrate():
    report = b.check_contract([_vm("broker")], "broker")
    assert _status(report, "workload VMs") == b.CHECK_WARN, report
    assert b.contract_ok(report), report


def test_the_default_broker_name_is_used_when_none_is_given():
    report = b.check_contract([_vm(pov_broker.DEFAULT_BROKER_VM_NAME), _vm("app")], "")
    assert _status(report, "broker VM") == b.CHECK_PASS, report


# ── prepare_broker_vm ────────────────────────────────────────────────────────

class _StubPlatform:
    """The adapter surface prepare_broker_vm uses, recording what it was asked to do."""

    def __init__(self, *, credentials=None, publish=None):
        self.published = []
        self.deleted = []
        self._credentials = credentials if credentials is not None else [
            {"text": "root / Passw0rd", "notes": ""}]
        self._publish = publish or {"id": "svc-1", "external_ip": "203.0.113.9",
                                    "external_port": 40022}

    async def publish_service(self, env_id, vm_id, iface_id, port):
        self.published.append((env_id, vm_id, iface_id, port))
        return dict(self._publish)

    async def delete_published_service(self, env_id, vm_id, iface_id, svc_id):
        self.deleted.append((env_id, vm_id, iface_id, svc_id))

    async def stored_credentials(self, env_id, vm_id):
        return list(self._credentials)


_BROKER = {"id": "vm-2", "name": "broker",
           "interfaces": [{"id": "nic-3", "ip": "10.0.0.5", "services": []}]}


def test_prepare_publishes_uses_and_revokes_the_service():
    mod = _StubPlatform()
    saved = b._ssh_install
    seen = {}

    async def _fake(host, port, logins, **kw):
        seen.update(host=host, port=port, logins=list(logins))
        return "installed"

    b._ssh_install = _fake
    try:
        out = asyncio.run(b.prepare_broker_vm(mod, "env-1", _BROKER))
    finally:
        b._ssh_install = saved

    assert out == "installed", out
    assert mod.published == [("env-1", "vm-2", "nic-3", 22)], mod.published
    assert mod.deleted == [("env-1", "vm-2", "nic-3", "svc-1")], mod.deleted
    assert seen == {"host": "203.0.113.9", "port": 40022,
                    "logins": [("root", "Passw0rd")]}, seen


def test_the_published_service_is_revoked_even_when_the_install_raises():
    """One left behind is baked into every POV built from the template."""
    mod = _StubPlatform()
    saved = b._ssh_install

    async def _boom(*a, **kw):
        raise b.TemplateBuildError("no route")

    b._ssh_install = _boom
    try:
        asyncio.run(b.prepare_broker_vm(mod, "env-1", _BROKER))
    except b.TemplateBuildError:
        pass
    else:
        raise AssertionError("the install failure should propagate")
    finally:
        b._ssh_install = saved
    assert mod.deleted == [("env-1", "vm-2", "nic-3", "svc-1")], \
        "the published service was not revoked after a failed install"


def test_prepare_hands_every_usable_credential_to_the_install():
    """A broker VM with two logins used to fail here rather than reach SSH at all."""
    mod = _StubPlatform(credentials=[{"text": "root / Passw0rd"},
                                     {"text": "administrator:Hunter2"}])
    saved = b._ssh_install
    seen = {}

    async def _fake(host, port, logins, **kw):
        seen["logins"] = list(logins)
        return "installed"

    b._ssh_install = _fake
    try:
        asyncio.run(b.prepare_broker_vm(mod, "env-1", _BROKER))
    finally:
        b._ssh_install = saved
    assert seen["logins"] == [("root", "Passw0rd"),
                              ("administrator", "Hunter2")], seen


# ── _ssh_install: several logins, one readiness ladder ───────────────────────
#
# The loop has two axes and they cost wildly different amounts of time, so both are pinned
# here. `asyncssh` is imported INSIDE `_ssh_install`, so swapping `sys.modules` is enough to
# drive it — and it is the only way to exercise authentication without a real guest.

class _Result:
    def __init__(self, exit_status, stdout="", stderr=""):
        self.exit_status = exit_status
        self.stdout = stdout
        self.stderr = stderr


class _FakeConn:
    def __init__(self, outcome):
        self._outcome = outcome

    async def run(self, command, input=None, check=False):
        if command.startswith("sudo -n sh -s"):
            if isinstance(self._outcome, int):
                return _Result(self._outcome, "",
                               "mkdir: /usr/local/sbin: Permission denied")
            return _Result(0)
        return _Result(0, "active\nDocker version 24.0.7")


class _FakeConnect:
    def __init__(self, outcome):
        self._outcome = outcome

    async def __aenter__(self):
        if self._outcome == "denied":
            raise _FakeSSH.PermissionDenied("Permission denied")
        if self._outcome == "unreachable":
            raise OSError("[Errno 111] Connect call failed")
        return _FakeConn(self._outcome)

    async def __aexit__(self, *exc):
        return False


class _FakeSSH:
    """Just enough of `asyncssh` for `_ssh_install`, keyed by username.

    An outcome is "ok", "denied" (an authentication refusal), "unreachable" (no answer at
    all), or an int exit status for an install that ran and failed.
    """

    class PermissionDenied(Exception):
        pass

    def __init__(self, outcomes):
        self._outcomes = outcomes
        self.attempts = []

    def connect(self, host, *, port=None, username=None, password=None,
                known_hosts=None, connect_timeout=None):
        self.attempts.append(username)
        return _FakeConnect(self._outcomes.get(username, "denied"))


@contextlib.contextmanager
def _fake_asyncssh(**outcomes):
    fake = _FakeSSH(outcomes)
    saved = sys.modules.get("asyncssh")
    sys.modules["asyncssh"] = fake
    try:
        yield fake
    finally:
        if saved is None:
            sys.modules.pop("asyncssh", None)
        else:
            sys.modules["asyncssh"] = saved


def _recorder():
    """A stand-in for `asyncio.sleep` that records instead of waiting. Every second this
    records is a second a real build would have spent."""
    waits = []

    async def _sleep(seconds):
        waits.append(seconds)

    return waits, _sleep


def test_the_second_login_is_tried_when_the_first_is_refused():
    """The whole point: a VM carrying a stale credential and a good one now builds."""
    waits, sleep = _recorder()
    with _fake_asyncssh(stale="denied", root="ok") as ssh:
        out = asyncio.run(b._ssh_install(
            "203.0.113.9", 40022, [("stale", "Sekrit1"), ("root", "Sekrit2")],
            sleep=sleep))
    assert "runner and runtime installed over SSH as root" in out, out
    assert ssh.attempts == ["stale", "root"], ssh.attempts
    assert waits == [], "a refused login must not spend the readiness ladder"


def test_the_first_working_login_wins_and_the_rest_are_never_tried():
    """Two credentials that both work is not a decision — it is one connection."""
    waits, sleep = _recorder()
    with _fake_asyncssh(root="ok", administrator="ok") as ssh:
        out = asyncio.run(b._ssh_install(
            "203.0.113.9", 40022, [("root", "Sekrit1"), ("administrator", "Sekrit2")],
            sleep=sleep))
    assert ssh.attempts == ["root"], ssh.attempts
    assert "as root" in out, out
    assert waits == [], "a login that worked cost a wait"


def test_every_login_refused_fails_at_once():
    """A rejected password does not become right in fifteen seconds. Burning the ladder
    twice over would turn a seven-minute worst case into fourteen for no new information."""
    waits, sleep = _recorder()
    with _fake_asyncssh(stale="denied", older="denied") as ssh:
        try:
            asyncio.run(b._ssh_install(
                "203.0.113.9", 40022, [("stale", "Sekrit1"), ("older", "Sekrit2")],
                sleep=sleep))
        except b.TemplateBuildError as exc:
            msg = str(exc)
        else:
            raise AssertionError("every login was refused and it did not fail")
    assert ssh.attempts == ["stale", "older"], ssh.attempts
    assert waits == [], f"the ladder was spent on a login that will never work: {waits}"
    assert "all 2 stored credentials" in msg, msg
    # The usernames are the useful part and the only safe part.
    assert "tried: stale, older" in msg, msg
    assert "Sekrit1" not in msg and "Sekrit2" not in msg, "the refusal leaked a password"


def test_an_unreachable_host_still_retries_the_whole_set():
    """The ladder is for a guest that answers TCP before sshd is ready. A port that is not
    listening does not care who is knocking, so the other logins are not tried per pass."""
    saved = b._SSH_ATTEMPTS
    b._SSH_ATTEMPTS = 3
    waits, sleep = _recorder()
    try:
        with _fake_asyncssh(root="unreachable", administrator="unreachable") as ssh:
            try:
                asyncio.run(b._ssh_install(
                    "203.0.113.9", 40022,
                    [("root", "Sekrit1"), ("administrator", "Sekrit2")], sleep=sleep))
            except b.TemplateBuildError as exc:
                msg = str(exc)
            else:
                raise AssertionError("an unreachable host must fail")
    finally:
        b._SSH_ATTEMPTS = saved
    assert "could not reach the broker VM" in msg, msg
    assert len(waits) == 2, f"expected attempts-1 waits, got {waits}"
    assert ssh.attempts == ["root", "root", "root"], ssh.attempts


def test_a_failed_install_does_not_fall_through_to_the_next_login():
    """This login worked and the script did not. Re-running it under another would re-apply
    a half-applied install, so the exit status is fatal — and it names the likely cause,
    because the runner needs root and an unprivileged login gets exactly this far."""
    waits, sleep = _recorder()
    with _fake_asyncssh(appuser=1, root="ok") as ssh:
        try:
            asyncio.run(b._ssh_install(
                "203.0.113.9", 40022, [("appuser", "Sekrit1"), ("root", "Sekrit2")],
                sleep=sleep))
        except b.TemplateBuildError as exc:
            msg = str(exc)
        else:
            raise AssertionError("a non-zero install must fail the prepare")
    assert ssh.attempts == ["appuser"], ssh.attempts
    assert "exited 1" in msg and "as appuser" in msg, msg
    assert "installs as root" in msg, msg
    assert waits == [], "the ladder is not for a script that ran and failed"


def test_no_logins_at_all_is_refused_without_spending_the_ladder():
    """`candidates` cannot return an empty list, but a hand-rolled caller would otherwise
    spend two and a half minutes discovering it had nothing to try."""
    waits, sleep = _recorder()
    with _fake_asyncssh():
        try:
            asyncio.run(b._ssh_install("203.0.113.9", 40022, [], sleep=sleep))
        except b.TemplateBuildError as exc:
            assert "no usable login" in str(exc), exc
        else:
            raise AssertionError("an empty login list must be refused")
    assert waits == [], waits


def test_the_published_service_is_revoked_when_the_credential_is_unusable():
    """The refusal happens between publish and install, which is the gap a `finally`
    placed around only the install would miss."""
    mod = _StubPlatform(credentials=[{"text": "no separator here", "notes": ""}])
    try:
        asyncio.run(b.prepare_broker_vm(mod, "env-1", _BROKER))
    except Exception:
        pass
    else:
        raise AssertionError("an unparseable credential should refuse")
    assert mod.deleted == [("env-1", "vm-2", "nic-3", "svc-1")], mod.deleted


def test_a_broker_with_no_interface_is_refused_before_anything_is_published():
    mod = _StubPlatform()
    try:
        asyncio.run(b.prepare_broker_vm(mod, "env-1",
                                        {"id": "vm-2", "name": "broker",
                                         "interfaces": []}))
    except b.TemplateBuildError as exc:
        assert "network interface" in str(exc), exc
    else:
        raise AssertionError("a broker with no NIC must be refused")
    assert mod.published == [], "nothing should have been published"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
