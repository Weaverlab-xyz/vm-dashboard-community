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


# **No hyphens in these names, ever.** POSIX function names take no hyphen and dash
# refuses one outright — so `apt-get` and `systemd-tmpfiles` cannot be stubbed here, and
# they do not need to be: the empty PATH below makes every unstubbed command not-found,
# and the script calls both of them guarded. If a block ever falls through to a real
# package install, every command in it is not-found and the run fails, which is the point.
#
# This is a Windows/Linux trap and not a theoretical one. `sh` on a developer's Windows
# box is Git Bash, which ALLOWS a hyphen; `sh` in CI is dash, which does not. A stub with
# a hyphen therefore passes locally and takes out every test in this section in CI with
# "Syntax error: Bad function name" — it has done so once already.
# `test_the_stub_preludes_use_only_posix_function_names` pins it on any platform.
#
# `dnf` and `yum` are named because they *can* be, and a named stub says which branch ran
# instead of only that something did.
_NO_PACKAGES = """
PATH=""
dnf() { echo "PACKAGE-MANAGER-RAN" >&2; return 9; }
yum() { echo "PACKAGE-MANAGER-RAN" >&2; return 9; }
systemctl() { return 0; }
service() { return 0; }
ln() { return 0; }
"""


# The Engine API socket the install now gates on, faked as a plain file.
#
# A plain file and not a socket because **this repo's development machines cannot make an
# AF_UNIX socket at all** — Windows Python has no `socket.AF_UNIX` — so a test that needed a
# real one would pass in CI and fail here, which is the same as not having it. The gate is
# written as the two ways the path is WRONG (absent, or a directory) rather than as one
# `-S` assertion, and both of those are creatable anywhere, so what is pinned below is the
# whole rule rather than the half a `-S` would leave testable.
#
# Forward slashes: `sh` on Windows is Git Bash, and a backslash in a `[ -e ]` is an escape,
# not a separator.
_SOCK_DIR = tempfile.mkdtemp(prefix="pov-broker-sock-")
_FAKE_SOCKET = os.path.join(_SOCK_DIR, "docker.sock").replace("\\", "/")
_MISSING_SOCKET = os.path.join(_SOCK_DIR, "absent.sock").replace("\\", "/")
_DIR_SOCKET = os.path.join(_SOCK_DIR, "dir.sock").replace("\\", "/")
with open(_FAKE_SOCKET, "w", encoding="utf-8"):
    pass
os.makedirs(_DIR_SOCKET, exist_ok=True)


def _install(**kw) -> str:
    """The install block, gated on a socket path this test run actually controls."""
    kw.setdefault("socket_path", _FAKE_SOCKET)
    return b.render_docker_install(**kw)


def test_the_stub_preludes_use_only_posix_function_names():
    """The preludes in this file are shell, and they run under whatever `sh` is.

    POSIX function names take no hyphen: dash refuses `systemd-tmpfiles() { ... }` with
    "Syntax error: Bad function name" and abandons the whole script. Git Bash — which is
    `sh` on a Windows workstation — accepts it. So a hyphenated stub passes every local
    run and takes out every test in this section in CI, reported as an assertion about
    podman or sockets rather than as a syntax error in the harness. That has happened.

    Asserted on the TEXT rather than by parsing, deliberately: `sh -n` here is the
    permissive shell, so a parse check is exactly the thing that cannot see this.
    """
    for name, prelude in (("_NO_PACKAGES", _NO_PACKAGES), ("_NO_RUNTIME", _NO_RUNTIME)):
        for line in prelude.splitlines():
            head = line.split("(")[0].strip()
            if "()" in line.replace(" ", "") and head:
                assert "-" not in head, (
                    f"{name} stubs {head!r} as a shell function, and a hyphen in a "
                    f"function name is a dash syntax error that kills the whole prelude. "
                    f"Let PATH='' make it not-found instead.")


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
    p = _run_block(_install(),
                   _NO_PACKAGES + "\ndocker() { return 0; }\n")
    assert p.returncode == 0, f"rc={p.returncode} {p.stderr[:300]}"
    assert "PACKAGE-MANAGER-RAN" not in (p.stdout + p.stderr), \
        "a guest that already has docker must not have packages installed over it"
    assert "already present" in p.stdout, p.stdout


def test_a_runtime_that_does_not_answer_fails_the_install():
    """The regression this exists for: a package that landed beside a daemon that will not
    start fails the bootstrap in exactly the same place as a guest that never had one, and
    the install script is the last moment anything is watching."""
    p = _run_block(_install(),
                   _NO_PACKAGES + "\ndocker() { case \"$1\" in version) return 1 ;; esac; return 0; }\n")
    assert p.returncode != 0, "a runtime that cannot answer `docker version` must fail loudly"
    assert "enrolling" in p.stderr, \
        f"the refusal must name the symptom an SE would otherwise chase: {p.stderr[:300]}"


def test_a_runtime_that_is_running_but_disabled_fails_the_install():
    """The one found live. `dnf install docker-ce` leaves the unit **disabled** on the RHEL
    family, and the guest it was found on was running only because somebody had just typed
    `systemctl start docker`. A template is baked and then booted — for every POV, every
    time — so "running now" is worth nothing here and this must not pass."""
    p = _run_block(_install(), _NO_PACKAGES + """
docker() { return 0; }
systemctl() { case "$1" in is-enabled) return 1 ;; *) return 0 ;; esac; }
""")
    assert p.returncode != 0, \
        "a runtime that will not come back after a reboot must not bake into a template"
    assert "boot" in p.stderr, \
        f"the refusal must say it is about boot, not about now: {p.stderr[:300]}"


def test_a_runtime_that_is_running_and_enabled_passes():
    p = _run_block(_install(), _NO_PACKAGES + """
docker() { return 0; }
systemctl() { return 0; }
""")
    assert p.returncode == 0, f"rc={p.returncode} {p.stderr[:300]}"
    assert "enabled at boot" in p.stdout, p.stdout


def test_a_guest_with_no_docker_unit_is_not_failed_for_not_enabling_one():
    """The `podman` + `podman-docker` guest the contract accepts has no `docker.service` to
    enable, and `is-enabled` on a unit that does not exist is not a finding about it. The
    gate asks whether there is a unit first — otherwise the check that protects RHEL-family
    templates would reject every Podman one."""
    p = _run_block(_install(), _NO_PACKAGES + """
docker() { return 0; }
systemctl() { case "$1" in cat) return 1 ;; is-enabled) return 1 ;; *) return 0 ;; esac; }
""")
    assert p.returncode == 0, \
        f"a guest with no docker.service must not be failed for it: {p.stderr[:300]}"


def test_the_state_probe_reports_the_boot_state_and_not_only_the_version():
    """The Runner column is where an SE reads this, and `Docker version 26.1.0` beside a
    disabled unit is a green-looking line about a template that cannot work."""
    assert "is-enabled docker" in b._STATE_PROBE, b._STATE_PROBE
    assert "cat docker.service" in b._STATE_PROBE, \
        "the probe must ask whether there is a unit before reporting on one"


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


# A guest with NO runtime cannot be simulated by stubbing `docker`, because `command -v`
# finds a shell function as readily as a binary — defining one is the same as the guest
# already having Docker, which is the other branch entirely. So absence is `PATH=""` and no
# function at all, and the package manager DEFINES the function when it "installs",
# which is what makes the `command -v docker` re-check after the fallback mean something.
_NO_RUNTIME = """
PATH=""
systemctl() { return 0; }
service() { return 0; }
ln() { return 0; }
"""


def test_a_guest_with_no_docker_ce_falls_back_to_the_distros_podman():
    """The case this POV feature actually meets. A Skytap AlmaLinux 8 broker resolves
    `appstream` and `baseos` perfectly and cannot reach download.docker.com at all, so
    every step of the Docker CE install has to be non-fatal — otherwise the one guest the
    fallback exists for never reaches it."""
    p = _run_block(_install(), _NO_RUNTIME + """
dnf() {
  for a in "$@"; do
    case "$a" in
      docker-ce) echo "CE-ATTEMPTED" >&2; return 1 ;;
      podman-docker) echo "PODMAN-INSTALLED" >&2; docker() { return 0; }; return 0 ;;
    esac
  done
  return 0
}
""")
    assert "PODMAN-INSTALLED" in p.stderr, \
        f"a guest with no route to Docker's CDN must fall back to the distro's podman: {p.stderr[:400]}"
    assert p.returncode == 0, \
        f"and the fallback landing is a SUCCESS, not a tolerated failure: {p.stderr[:400]}"


def test_docker_ce_is_attempted_before_the_podman_fallback():
    """Ordering, asserted on the text because the branch that proves it behaviourally needs
    an `/etc/os-release` this test cannot write. Docker CE is what the agent's Engine API
    use is tested against; Podman is the answer to a guest that cannot reach Docker's CDN,
    not a preference. Reversing these would quietly change what every future template
    bakes."""
    block = b.render_docker_install()
    assert block.index(b.DOCKER_REPO_HOST) < block.index(b.PODMAN_PACKAGES), \
        "Docker CE must be tried first; podman is the fallback, not the default"
    guard_at = block.index("if ! command -v docker")
    install_at = block.index(f"dnf -y install {b.PODMAN_PACKAGES}")
    assert guard_at < install_at, \
        "the fallback must be guarded on Docker CE having actually failed, or a guest " \
        "that got Docker CE has podman installed over it too"


def test_the_docker_ce_install_is_never_fatal_on_its_own():
    """Structural, because the behaviour above depends on it and a single missing `|| true`
    puts it back: an `exit 1` or an unguarded install inside the Docker CE branches means a
    guest that cannot reach download.docker.com never reaches the podman fallback at all.
    Both of the guards that used to `exit 1` here — no VERSION_CODENAME, no VERSION_ID —
    are now the fallback's reason to run rather than the script's reason to stop."""
    block = b.render_docker_install()
    ce = block[block.index('case " $ID $ID_LIKE " in'):block.index(b.PODMAN_PACKAGES)]
    for line in ce.splitlines():
        stripped = line.strip()
        if stripped.startswith(("dnf -y install", "yum -y install", "apt-get -y -q install")):
            assert stripped.endswith("|| true"), \
                f"a fatal package install starves the podman fallback: {stripped}"
    assert "exit 1" not in ce, \
        "nothing in the Docker CE half may exit: the fallback below it is the answer"


def test_neither_runtime_landing_refuses_and_names_both():
    """The build's Runner detail is the only place anyone reads this. A refusal that named
    only Docker would send an SE to a CDN their guest cannot reach, when the distro's own
    package was the answer all along — so it names both attempts and the distro it found."""
    p = _run_block(_install(), _NO_RUNTIME + """
dnf() { return 1; }
yum() { return 1; }
""")
    assert p.returncode != 0, "a guest with no runtime at all must fail loudly"
    # The two ATTEMPTS by name, which is the property — an SE reading this needs to know
    # both were tried, so that "no route to Docker's CDN" does not read as the whole story
    # when the distro package was the other half. Deliberately not a containment check
    # against the repo hostname: `x in y` with a host literal is what CodeQL reads as URL
    # sanitization, and it is the wrong assertion anyway. Which host Docker CE comes from
    # is pinned on the rendered block by
    # `test_docker_ce_is_attempted_before_the_podman_fallback`.
    assert "Docker CE" in p.stderr and b.PODMAN_PACKAGES in p.stderr, \
        f"the refusal must name BOTH attempts, or half the remedy is invisible: {p.stderr[:400]}"
    # And it names the distro. Asserted on the text: `$ID` is read from an /etc/os-release
    # this test has no way to write, so it is empty in the run above.
    block = b.render_docker_install()
    refusal = block[block.index("could not install a container runtime"):]
    assert "$ID" in refusal[:160], "the refusal must name the distro it found"


def test_a_runtime_whose_socket_is_absent_is_refused_and_names_podman_socket():
    """The trap `podman-docker` sets, and the reason this gate exists at all. The shim
    answers `docker version` happily while NOTHING IS LISTENING — installing it does not
    start `podman.socket`. The agent never runs the command; it speaks the Engine API over
    the socket directly. So a guest in this state enrols, goes green, and fails every
    Gateway and Config-Management job on a socket nobody started."""
    p = _run_block(_install(socket_path=_MISSING_SOCKET), _NO_PACKAGES + """
docker() { return 0; }
""")
    assert p.returncode != 0, \
        "a working `docker` command over a dead socket must not pass as a working runtime"
    assert "podman.socket" in p.stderr, \
        f"the refusal must name the command that fixes it: {p.stderr[:400]}"


def test_a_socket_path_that_is_a_directory_gets_its_own_refusal():
    """A `docker run -v /var/run/docker.sock:...` against a host with no daemon creates a
    DIRECTORY there, and every later run then mounts an empty one into the agent. It fails
    the same gate as an absent socket and has a completely different remedy, so it gets its
    own message rather than being folded into one `-S`."""
    p = _run_block(_install(socket_path=_DIR_SOCKET), _NO_PACKAGES + """
docker() { return 0; }
""")
    assert p.returncode != 0, "a directory where the socket belongs must not pass"
    assert "rmdir" in p.stderr, \
        f"this refusal must name its own remedy, not the socket one: {p.stderr[:400]}"


def test_the_boot_gate_comes_off_for_a_live_pov_but_the_verify_never_does():
    """`require_enabled_at_boot=False` is for `pov_broker.render_bootstrap`, where the guest
    is already up and needs a runtime in the next ten minutes. Dropping the boot gate there
    is right — refusing an install that was about to work leaves the POV with no agent at
    all. Dropping the VERIFY would be a different thing entirely, and this pins that the
    parameter does exactly one of them."""
    running_but_disabled = _NO_PACKAGES.replace(
        "systemctl() { return 0; }",
        'systemctl() { case "$1" in is-enabled) return 1 ;; *) return 0 ;; esac; }')
    p = _run_block(_install(require_enabled_at_boot=False),
                   running_but_disabled + "\ndocker() { return 0; }\n")
    assert p.returncode == 0, \
        f"a live POV must not be refused over a unit that is not enabled at boot: {p.stderr[:300]}"
    assert "power cycle" in p.stdout, \
        f"it still has to SAY the broker will not survive a reboot: {p.stdout[:300]}"

    # ...and the same call still refuses a runtime that does not answer at all.
    p = _run_block(_install(require_enabled_at_boot=False), _NO_PACKAGES + """
docker() { case "$1" in version) return 1 ;; esac; return 0; }
""")
    assert p.returncode != 0, \
        "the boot gate is optional; a runtime that does not work is never optional"


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
