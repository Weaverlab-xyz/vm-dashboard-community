"""The POV broker agent — the orderings and the generated files.

Six properties, each of which fails quietly rather than loudly when it is wrong:

  * **The agent id is persisted before the enrolment wait.** A crash mid-wait must leave
    the next run re-issuing that row's code; minting a second agent for one POV is how
    teardown ends up revoking the wrong one.
  * **A re-run re-issues, never re-mints.** Same property from the other side.
  * **The bootstrap removes the agent's state volume.** A re-issued code plus a surviving
    volume gives a container that starts cleanly and 401s forever — which reads as
    revocation, not as a stale volume, and sends you debugging the wrong thing.
  * **The policy grants a /32 per VM, never a subnet.** The platform's subnet is bigger
    than the POV and on a shared lab network can contain somebody else's environment.
  * **A broker failure does not fail the provision.** The environment is up, billing and
    reapable; failing it would trade a fixable gap for a destroyed environment.
  * **Destroy revokes the agent before deleting the environment.** An enrolled agent whose
    VM has just been deleted keeps polling from nowhere and keeps holding its job.
  * **The agent joins the Docker socket's group.** Mounting the socket is not the same as
    being able to open it: the container runs as uid 10001 and the socket is 0660
    root:docker, so without the group every Gateway install gets EACCES on a socket that
    is sitting right there — while the agent's refusal says "needs it mounted".

Uses a real SQLite database and a fake adapter, so the orchestrator runs for real. No
network, no FastAPI.

Runs under pytest, or standalone:
    python tests/test_pov_broker.py
"""
import asyncio
import os
import subprocess
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-broker")

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.services import (agent_service, job_service, lab_platforms,  # noqa: E402
                                    pov_broker, pov_env_service,
                                    pov_template_builder)

_AGENT_URL = "https://agents.example.test"


class FakeAdapter:
    """A lab platform that records what it was asked to do."""

    def __init__(self, **behaviour):
        self.b = behaviour
        self.injected = []          # (vm_id, payload)
        self.deleted = []
        self.calls = []

    def configured(self):
        return True

    async def create_environment(self, template_id, name="", project_id=""):
        self.calls.append("create")
        return {"id": "sky-900", "runstate": "stopped", "region": "US-West"}

    async def update_environment(self, env_id, changes):
        return {"id": env_id, "runstate": "stopped"}

    async def set_runstate(self, env_id, runstate):
        return {"id": env_id, "runstate": runstate}

    async def wait_for_runstate(self, env_id, target, **kw):
        return {"id": env_id, "runstate": target}

    async def get_environment(self, env_id):
        self.calls.append("get")
        return {"id": env_id, "runstate": "running",
                "vms": self.b.get("vms", [
                    {"id": "vm-1", "name": "broker", "os_family": "linux",
                     "private_ip": "10.9.0.10", "published_services": []},
                    {"id": "vm-2", "name": "dc01", "os_family": "windows",
                     "private_ip": "10.9.0.20", "published_services": []},
                ])}

    async def inject_bootstrap(self, env_id, vm_id, payload):
        self.calls.append("inject")
        if self.b.get("inject_raises") and payload:
            raise RuntimeError(self.b["inject_raises"])
        self.injected.append((vm_id, payload))

    async def delete_environment(self, env_id):
        self.calls.append("delete")
        self.deleted.append(env_id)


def _install(adapter):
    original = lab_platforms.adapter
    lab_platforms.adapter = lambda platform: adapter
    return original


def _restore(original):
    lab_platforms.adapter = original


async def _no_sleep(_seconds):
    return None


def _new_env(**kw):
    db = d.SessionLocal()
    env = d.PovEnvironment(
        platform="skytap", name=kw.get("name", "poc-" + uuid.uuid4().hex[:6]),
        # Overridable, because the build rows that describe a template are keyed on this
        # and the database is shared by every test in this file: two envs on one template
        # id means one test's PovTemplateBuild is read by the next one's broker run.
        template_id=kw.get("template_id", "42"),
        platform_environment_id=kw.get("platform_environment_id", "sky-900"),
        status=kw.get("status", pov_env_service.STATUS_ACTIVE))
    db.add(env)
    db.commit()
    env_id = env.id
    db.close()
    return env_id


def _add_vms(env_id, vms):
    db = d.SessionLocal()
    for vm in vms:
        db.add(d.PovEnvironmentVM(environment_id=env_id, **vm))
    db.commit()
    db.close()


def _reload(env_id):
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    out = (env.broker_vm_id, env.broker_agent_id, env.status,
           env.metadata_dict.get("broker_error", ""))
    db.close()
    return out


def _set_url(value=_AGENT_URL):
    """Point the broker at an agent endpoint, WITHOUT writing config.

    `agent_base_url` is the pinned signing audience: write-once in production and the
    value every agent signature is checked against. A test that stores a fake one leaves
    a developer's own dashboard unable to enrol a real agent, and a test suite has no
    business being able to do that. Patching the one function that reads it gets the same
    coverage and touches nothing.
    """
    pov_broker.dashboard_agent_url = lambda: value


def _clear_url():
    pov_broker.dashboard_agent_url = lambda: ""


# ── the generated files ──────────────────────────────────────────────────────

def test_the_bootstrap_carries_both_markers_and_the_code():
    """Both markers, because the guest runner must refuse a truncated read: the top half
    of this script is the destructive half."""
    script = pov_broker.render_bootstrap(
        env_name="poc-1", dashboard_url=_AGENT_URL, enroll_code="agte_deadbeef",
        policy_yaml=pov_broker.render_policy(["10.0.0.5"]))
    assert pov_broker.BOOTSTRAP_BEGIN in script
    assert pov_broker.BOOTSTRAP_END in script
    assert script.index(pov_broker.BOOTSTRAP_BEGIN) < script.index(pov_broker.BOOTSTRAP_END)
    assert "agte_deadbeef" in script
    assert _AGENT_URL in script


def test_the_bootstrap_removes_the_agent_state_volume():
    """The whole point of a re-run. Leaving the volume means the re-issued code is never
    redeemed and every poll 401s, which looks exactly like a revoked agent."""
    script = pov_broker.render_bootstrap(
        env_name="poc-1", dashboard_url=_AGENT_URL, enroll_code="agte_x",
        policy_yaml=pov_broker.render_policy(["10.0.0.5"]))
    assert f"docker volume rm {pov_broker.GUEST_STATE_VOLUME}" in script
    assert "docker rm -f dashboard-agent" in script


def test_the_code_file_is_written_world_readable():
    """022, not 077. The container runs as uid 10001 and cannot read a root-owned 0600
    file — the agent says so and exits rather than enrolling."""
    script = pov_broker.render_bootstrap(
        env_name="poc-1", dashboard_url=_AGENT_URL, enroll_code="agte_x",
        policy_yaml=pov_broker.render_policy(["10.0.0.5"]))
    assert "umask 022" in script
    assert "umask 077" not in script


def _bootstrap() -> str:
    return pov_broker.render_bootstrap(
        env_name="poc-1", dashboard_url=_AGENT_URL, enroll_code="agte_x",
        policy_yaml=pov_broker.render_policy(["10.0.0.5"]))


def test_the_bootstrap_is_valid_shell():
    """It is generated shell that runs as root on a guest nobody will log into to find out
    it did not parse. `$DOCKER_GROUP` is deliberately unquoted, which is exactly the kind of
    line worth having a parser confirm."""
    script = _bootstrap()
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        p = subprocess.run(["sh", "-n", path], capture_output=True, text=True)
        assert p.returncode == 0, f"the bootstrap is not valid /bin/sh: {p.stderr[:300]}"
    finally:
        os.unlink(path)


def test_the_bootstrap_pulls_the_images_the_policy_names():
    """Naming an image is not the same as having it. The agent refuses to pull -- correct on
    a customer's host, wrong on a VM the dashboard built and whose image names it chose --
    so `agent_gateway` failed with "not present on this host" on a machine nobody had logged
    into. The bootstrap fetches them instead."""
    script = pov_broker.render_bootstrap(
        env_name="poc-01", dashboard_url="https://d", enroll_code="a",
        policy_yaml="v: 1\n",
        images=(pov_broker.GATEWAY_IMAGE, pov_broker.ANSIBLE_VM_IMAGE))
    assert pov_broker.GATEWAY_IMAGE in script
    assert pov_broker.ANSIBLE_VM_IMAGE in script
    assert "docker pull" in script


def test_the_pull_runs_before_the_agent_is_replaced():
    """A slow registry must not cost a re-broker its running agent. Pull first, and the one
    already serving keeps serving until its replacement is ready."""
    script = pov_broker.render_bootstrap(
        env_name="poc-01", dashboard_url="https://d", enroll_code="a",
        policy_yaml="v: 1\n", images=(pov_broker.GATEWAY_IMAGE,))
    assert script.index("docker pull") < script.index("docker rm -f dashboard-agent")


def test_the_bootstrap_installs_a_runtime_before_anything_needs_one():
    """The failure this whole payload used to end in. A base template with a perfect
    metadata runner and no `docker` runs as far as the final `docker run`, `set -e` kills it
    there, the runner re-reads `user_data` twenty seconds later and dies in the same place
    forever — and the dashboard sees nothing, because the thing that would report it is the
    agent that never started.

    Ordering is the whole test: the install has to precede the pulls (which need a runtime)
    and precede the `docker rm -f` (so a slow or failed install costs a re-broker nothing).
    """
    script = pov_broker.render_bootstrap(
        env_name="poc-01", dashboard_url="https://d", enroll_code="a",
        policy_yaml="v: 1\n", images=(pov_broker.GATEWAY_IMAGE,))
    runtime_at = script.index("the container runtime")
    assert runtime_at < script.index("docker pull"), \
        "a pull needs a runtime, so the install cannot come after it"
    assert runtime_at < script.index("docker rm -f dashboard-agent"), \
        "an install that reaches a package repo must not cost a re-broker its agent"


def test_the_bootstraps_runtime_install_is_the_builders_own():
    """One expression of the install, reached rather than copied. What a template BAKE
    installs and what a POV installs when the bake never happened have to be the same
    runtime, or the feature is tested against one and shipped on the other — and the only
    difference permitted is the enabled-at-boot gate, which is a statement about a template
    and not about a guest that is already running."""
    script = pov_broker.render_bootstrap(
        env_name="poc-01", dashboard_url="https://d", enroll_code="a",
        policy_yaml="v: 1\n")
    assert pov_template_builder.render_docker_install(
        require_enabled_at_boot=False) in script, \
        "the bootstrap must carry the builder's own install block verbatim"
    assert pov_template_builder.render_docker_install() not in script, \
        "the boot gate is a template's rule; a live POV must not be refused over it"


def test_the_bootstrap_mounts_the_socket_the_install_verified():
    """Two constants for one path is how an install passes while the agent finds nothing:
    the install gates on a socket and the `docker run` MOUNTS one, and if they ever name
    different paths both halves look correct on their own."""
    assert pov_template_builder.AGENT_SOCKET_PATH == pov_broker.GUEST_DOCKER_SOCKET


def test_a_failed_pull_never_costs_the_pov_its_agent():
    """`set -eu` is in force, so an unguarded pull failure would abort the bootstrap and
    leave no agent at all. A POV with an enrolled agent and one missing image refuses one
    job and names it; that is much the better failure, so the pull is guarded."""
    script = pov_broker.render_bootstrap(
        env_name="poc-01", dashboard_url="https://d", enroll_code="a",
        policy_yaml="v: 1\n", images=(pov_broker.GATEWAY_IMAGE,))
    pull = next(ln for ln in script.splitlines() if "docker pull" in ln)
    assert "||" in pull, pull


def test_the_bootstrap_pulls_the_agent_image_itself():
    """The one image whose staleness is invisible. `docker run` fetches only what is ABSENT
    locally, so a broker VM resolves `dashboard-agent:latest` once -- at first bootstrap --
    and every re-brokering afterwards re-runs that same cached copy. An agent fix then
    cannot reach a POV that already has an agent: the NanoCpus clamp shipped and the next
    Config Management run still returned "Range of CPUs is from 0.01 to 1.00"."""
    script = pov_broker.render_bootstrap(
        env_name="poc-01", dashboard_url="https://d", enroll_code="a",
        policy_yaml="v: 1\n")
    pull_line = next(ln for ln in script.splitlines() if ln.startswith("for IMAGE in "))
    assert pov_broker.AGENT_IMAGE in pull_line.replace(";", " ").split(), pull_line


def test_the_agent_image_is_pulled_even_with_no_policy_images():
    """A POV with no guest opted in for configuration names no extra image, and that used
    to emit no pull loop at all -- which is exactly the POV that would keep running a
    year-old agent. The agent's own image makes the list unconditional, which also settles
    `for IMAGE in ; do`, the syntax error the old empty-list guard existed for."""
    script = pov_broker.render_bootstrap(
        env_name="poc-01", dashboard_url="https://d", enroll_code="a",
        policy_yaml="v: 1\n")
    assert "docker pull" in script
    assert "for IMAGE in ; do" not in script


def test_the_ansible_image_is_pulled_only_when_config_management_is_on():
    """`render_policy` names `ansible.vm_image` only when there are targets, so pulling it
    unconditionally would fetch a large image for a POV that may never configure a guest.
    Opting a guest in takes a re-broker anyway, which is this same code path."""
    assert pov_broker.ANSIBLE_VM_IMAGE not in pov_broker.render_bootstrap(
        env_name="p", dashboard_url="https://d", enroll_code="a",
        policy_yaml="v: 1\n", images=(pov_broker.GATEWAY_IMAGE,))


def test_the_bootstrap_is_ascii_only():
    """It travels as a JSON string through a metadata service, out of it through two `sed`
    passes in the guest runner, and into `sh` — and in the worst case a human retypes it
    into a console with no clipboard. Nothing in a comment here is worth spending any of
    that on, so the em dashes stay in the Python and out of the payload."""
    bad = sorted({c for c in _bootstrap() if ord(c) > 127})
    assert not bad, f"the bootstrap carries non-ASCII: {bad!r}"


def test_the_agent_joins_the_docker_sockets_group():
    """The bug this exists for. The socket is mounted and the container runs as uid 10001,
    so without the socket's group the agent gets EACCES and every POV Gateway install is
    refused — with a message that says the socket needs mounting, which it already is."""
    script = _bootstrap()
    assert "--group-add" in script, \
        "the agent cannot open a 0660 root:docker socket without its group"
    # Resolved from the socket itself rather than from `getent group docker`: what has to
    # be opened is the socket, and on a rootless install its group is not `docker` at all.
    assert f"stat -c '%g' {pov_broker.GUEST_DOCKER_SOCKET}" in script, script
    assert f"-v {pov_broker.GUEST_DOCKER_SOCKET}:{pov_broker.GUEST_DOCKER_SOCKET}" in script, \
        "the mount's container side must be the path the agent actually reads"


def _run_group_resolution(script: str, stat_stub: str) -> str:
    """Run just the group-resolution block out of the generated bootstrap.

    Sliced rather than reimplemented, because a copy of these four lines in a test would
    keep passing after the real ones changed. The slice boundaries are asserted, so a
    rename fails here loudly instead of silently testing nothing.
    """
    start = script.index('DOCKER_GROUP=""')
    # Searched FROM `start`, not from the top. The payload now carries a container-runtime
    # install above this block whose comments quote `docker run` while explaining why it is
    # there, so the first match in the whole script is prose several hundred lines early —
    # and the slice it produced ran backwards and tested nothing.
    end = script.index("docker run ", start)
    return subprocess.run(
        ["sh", "-s"],
        input="set -eu\n" + stat_stub + "\n" + script[start:end] + '\necho "[$DOCKER_GROUP]"\n',
        capture_output=True, text=True, encoding="utf-8", errors="replace").stdout


def test_the_group_is_resolved_on_the_guest_not_guessed_here():
    """The GID varies by distro and by install order, and the dashboard never chose it —
    so it is read on the VM. 992 here stands for "whatever this guest happens to use"."""
    out = _run_group_resolution(_bootstrap(), "stat() { echo 992; }")
    assert out.strip() == "[--group-add 992]", out


def test_no_socket_means_no_flag_rather_than_an_empty_argument():
    """A guest with no socket must still get a runnable `docker run`. An empty but present
    `--group-add` argument is a container that never starts, and the bootstrap's own
    `docker rm -f` has already removed the agent that was working."""
    out = _run_group_resolution(_bootstrap(), "stat() { return 1; }")
    assert out.strip() == "[]", out


def test_the_policy_grants_a_slash_32_per_vm_not_a_subnet():
    policy = pov_broker.render_policy(["10.9.0.10", "10.9.0.20"])
    assert "- cidr: 10.9.0.10/32" in policy
    assert "- cidr: 10.9.0.20/32" in policy
    assert "/24" not in policy and "/16" not in policy.split("deny:")[0]


def test_the_policy_denies_the_metadata_range():
    """The agent has no business at 169.254.169.254 — on every cloud that is the
    credential endpoint, and here it is where the bootstrap itself came from."""
    assert "169.254.0.0/16" in pov_broker.render_policy(["10.0.0.1"]).split("deny:")[1]


def test_the_agent_name_fits_the_column():
    """RemoteAgent.name is 64 because Job.created_by records `agent:{name}` in 100."""
    env = d.PovEnvironment(platform="skytap", name="p" * 63)
    assert len(pov_broker.agent_name(env)) <= 64
    assert pov_broker.agent_name(env).endswith("-broker")


def test_the_enrolment_wait_cannot_outlive_the_code():
    assert pov_broker.enroll_timeout_seconds() < agent_service.ENROLL_TTL_MINUTES * 60


# ── selection ────────────────────────────────────────────────────────────────

def test_a_typed_broker_vm_name_is_matched_exactly_not_fuzzily():
    """'contains broker' also matches a customer VM called password-broker, and the cost
    of the wrong answer is an agent installed on a machine nobody expected.

    Exactness now lives on the TYPED rung, which is the only place it means anything: a
    name the operator wrote down is an instruction. Note the VM is Linux here and the
    name IS typed -- without both, this passes for an unrelated reason (the blank-OS
    refusal below) and stops testing fuzziness at all.
    """
    env_id = _new_env()
    _add_vms(env_id, [{"platform_vm_id": "v1", "name": "password-broker",
                       "os_family": "linux", "private_ip": "10.0.0.9"}])
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    pov_broker.configure(db, env, vm_name="broker")
    try:
        pov_broker.select_broker_vm(db, env)
        raise AssertionError("a fuzzy match was accepted")
    except pov_broker.BrokerError as exc:
        assert "password-broker" in str(exc), "the refusal must name what it did find"
        assert "named 'broker'" in str(exc), "the refusal must name what it looked for"
    finally:
        db.close()


def test_the_only_linux_vm_is_the_broker_whatever_it_is_called():
    """The feature. A template whose Docker host is called BtPocLin01 matches no broker
    name anybody would guess, and its OS is a fact about it where its name is a
    convention somebody else's template never agreed to."""
    env_id = _new_env()
    _add_vms(env_id, [{"platform_vm_id": "v1", "name": "BtPocLin01",
                       "os_family": "linux", "private_ip": "10.0.0.9"},
                      {"platform_vm_id": "v2", "name": "BtPocWin01",
                       "os_family": "windows", "private_ip": "10.0.0.10"}])
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    try:
        assert pov_broker.select_broker_vm(db, env).platform_vm_id == "v1"
    finally:
        db.close()


def test_a_blank_os_is_never_read_as_linux():
    """Slice 3's invariant, and the reason the OS override exists. A guest the platform
    would not classify must refuse and SAY SO, not be adopted because it is the only
    candidate left -- the bootstrap is a shell script and a Windows guest fails partway
    through it."""
    env_id = _new_env()
    _add_vms(env_id, [{"platform_vm_id": "v1", "name": "BtPocLin01",
                       "private_ip": "10.0.0.9"}])
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    try:
        pov_broker.select_broker_vm(db, env)
        raise AssertionError("a blank os_family was read as Linux")
    except pov_broker.BrokerError as exc:
        assert "BtPocLin01" in str(exc), "the refusal must name the unclassified guest"
        assert "VMs tab" in str(exc), "the refusal must carry the remedy that works"
    finally:
        db.close()


def test_an_operator_set_os_makes_the_only_linux_vm_resolvable():
    """The two halves together, and the motivating case end to end: the platform said
    nothing, an operator said linux, and the broker resolves."""
    env_id = _new_env()
    _add_vms(env_id, [{"platform_vm_id": "v1", "name": "BtPocLin01",
                       "private_ip": "10.0.0.9"}])
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    try:
        pov_env_service.set_vm_os(db, env, "v1", "linux")
        assert pov_broker.select_broker_vm(db, env).platform_vm_id == "v1"
    finally:
        db.close()


def test_two_linux_vms_are_refused_by_naming_both():
    """Uniqueness, never position. "The first Linux VM" is not a decision a position in a
    list can make, so ambiguity refuses -- and names the candidates, because the operator
    has to pick one and the old message named a VM that did not exist."""
    env_id = _new_env()
    _add_vms(env_id, [{"platform_vm_id": "v1", "name": "app01",
                       "os_family": "linux", "private_ip": "10.0.0.9"},
                      {"platform_vm_id": "v2", "name": "db01",
                       "os_family": "linux", "private_ip": "10.0.0.10"}])
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    try:
        pov_broker.select_broker_vm(db, env)
        raise AssertionError("an ambiguous POV was resolved anyway")
    except pov_broker.BrokerError as exc:
        assert "app01" in str(exc) and "db01" in str(exc), "both candidates must be named"
    finally:
        db.close()


def test_the_conventional_name_breaks_a_tie_between_two_linux_vms():
    env_id = _new_env()
    _add_vms(env_id, [{"platform_vm_id": "v1", "name": "app01",
                       "os_family": "linux", "private_ip": "10.0.0.9"},
                      {"platform_vm_id": "v2", "name": "broker",
                       "os_family": "linux", "private_ip": "10.0.0.10"}])
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    try:
        assert pov_broker.select_broker_vm(db, env).platform_vm_id == "v2"
    finally:
        db.close()


def test_the_entitle_host_is_not_conscripted_as_the_broker():
    """k3s brings its own containerd and its own iptables rules, so putting it on the one
    host whose job is keeping the agent channel up means the install can sever the
    connection it arrives over. A coincidence of naming must not decide that."""
    env_id = _new_env()
    _add_vms(env_id, [{"platform_vm_id": "v1", "name": "entitle",
                       "os_family": "linux", "private_ip": "10.0.0.9"},
                      {"platform_vm_id": "v2", "name": "dc01",
                       "os_family": "windows", "private_ip": "10.0.0.10"}])
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    try:
        pov_broker.select_broker_vm(db, env)
        raise AssertionError("the Entitle host was conscripted as the broker")
    except pov_broker.BrokerError as exc:
        assert "entitle" in str(exc), "the refusal must name the claim"
    finally:
        db.close()


def test_an_enrolled_broker_is_never_moved_by_inference():
    """The sticky rung. The agent's identity lives on the guest it enrolled from, so
    re-resolving onto a different VM leaves a container polling from a machine nothing
    points at -- and adding a Linux VM to a running POV must not do that."""
    env_id = _new_env()
    _add_vms(env_id, [{"platform_vm_id": "v1", "name": "lin01",
                       "os_family": "linux", "private_ip": "10.0.0.9"},
                      {"platform_vm_id": "v2", "name": "dc01",
                       "os_family": "windows", "private_ip": "10.0.0.10"}])
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    env.broker_vm_id = "v2"
    db.commit()
    try:
        assert pov_broker.select_broker_vm(db, env).platform_vm_id == "v2"
    finally:
        db.close()


def test_a_typed_name_moves_a_broker_that_already_enrolled():
    """The sticky rung must sit BELOW the typed one. Sticky exists to stop INFERENCE
    moving an enrolled broker, not to outrank an operator -- and naming a VM is precisely
    the remedy the ambiguity refusals tell people to use, so a stale broker_vm_id
    swallowing it would make that advice false."""
    env_id = _new_env()
    _add_vms(env_id, [{"platform_vm_id": "v1", "name": "lin01",
                       "os_family": "linux", "private_ip": "10.0.0.9"},
                      {"platform_vm_id": "v2", "name": "lin02",
                       "os_family": "linux", "private_ip": "10.0.0.10"}])
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    env.broker_vm_id = "v1"
    db.commit()
    try:
        # Inference alone keeps it where it is...
        assert pov_broker.select_broker_vm(db, env).platform_vm_id == "v1"
        # ...and naming the other one moves it.
        pov_broker.configure(db, env, vm_name="lin02")
        assert pov_broker.select_broker_vm(db, env).platform_vm_id == "v2"
    finally:
        db.close()


def test_the_cloud_read_back_never_infers():
    """`infer=False` is the cloud path's contract. There the dashboard has just BUILT the
    broker, so a POV whose workload guests are Linux must not bind broker_vm_id to one of
    them -- which would be silent, because the bootstrap did reach the real broker."""
    env_id = _new_env()
    _add_vms(env_id, [{"platform_vm_id": "v1", "name": "workload01",
                       "os_family": "linux", "private_ip": "10.0.0.9"}])
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    try:
        pov_broker.select_broker_vm(db, env, infer=False)
        raise AssertionError("the cloud read-back inferred a workload guest")
    except pov_broker.BrokerError as exc:
        assert "workload01" in str(exc)
    # ...but the id the driver reported is taken, name or no name.
    try:
        assert pov_broker.select_broker_vm(
            db, env, infer=False, platform_vm_id="v1").platform_vm_id == "v1"
    finally:
        db.close()


def test_a_per_pov_broker_vm_name_overrides_the_default():
    env_id = _new_env()
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    env.metadata_dict = {"broker_vm_name": "jump01"}
    db.commit()
    assert pov_broker.broker_vm_name(env) == "jump01"
    db.close()


def test_a_typed_broker_vm_name_is_distinguishable_from_none_typed():
    """The property the whole design rests on. "Typed nothing" means infer and "typed
    'broker'" means match exactly, so a reader that substitutes the convention cannot
    tell the two instructions apart -- and would switch inference off for every POV."""
    env_id = _new_env()
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    try:
        assert pov_broker.stored_broker_vm_name(env) == "", "nothing was typed"
        assert pov_broker.broker_vm_name(env) == "broker", "but a name is still offered"
        pov_broker.configure(db, env, vm_name="broker")
        assert pov_broker.stored_broker_vm_name(env) == "broker", "now it was typed"
    finally:
        db.close()


def test_naming_the_broker_vm_does_not_forget_the_resource_broker_host():
    """The write MERGES. `metadata_dict` also carries rb_vm_name, rb_zone, the Entitle
    host and broker_error, so an assignment drops whichever was set first."""
    env_id = _new_env()
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    try:
        meta = env.metadata_dict
        meta["rb_vm_name"] = "rb01"
        env.metadata_dict = meta
        db.commit()
        pov_broker.configure(db, env, vm_name="jump01")
        assert env.metadata_dict.get("rb_vm_name") == "rb01", "the RB host survived"
        assert env.metadata_dict.get("broker_vm_name") == "jump01"
        pov_broker.configure(db, env, vm_name="")
        assert "broker_vm_name" not in env.metadata_dict, "blank clears to auto-detect"
        assert env.metadata_dict.get("rb_vm_name") == "rb01", "and still survived"
    finally:
        db.close()


# ── ensure_broker ────────────────────────────────────────────────────────────

def test_the_agent_id_is_persisted_before_the_wait():
    """The ordering that matters most. The wait here never succeeds, so the columns can
    only be set if they were written before it."""
    _set_url()
    original = _install(FakeAdapter())
    env_id = _new_env()
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    try:
        asyncio.run(pov_broker.ensure_broker(db, env, sleep=_no_sleep))
        raise AssertionError("the wait should have timed out")
    except pov_broker.BrokerError as exc:
        assert "no agent enrolled" in str(exc)
    finally:
        db.close()
        _restore(original)

    vm_id, agent_id, _, _ = _reload(env_id)
    assert vm_id == "vm-1", "the broker VM must be recorded before the wait"
    assert agent_id, "the agent id must be recorded before the wait"


def test_the_timeout_names_a_template_that_baked_with_no_runtime():
    """The gap that cost a live POV most of an afternoon. A broker that never enrols is
    silent by construction — the guest cannot reach the dashboard until the agent it cannot
    start has started — so this message can only list candidates, and it listed three, two
    of which were fine.

    But the dashboard BAKED that template and wrote down what it found: `docker: MISSING`,
    sitting in `prepare_detail` the whole time. Reading it back turns the one cause nobody
    can see into the one the job names first.
    """
    _set_url()
    original = _install(FakeAdapter())
    env_id = _new_env()
    db = d.SessionLocal()
    db.add(d.PovTemplateBuild(
        platform="skytap", name="weaver", result_template_id="42",
        status="ready", prepare_method="ssh",
        prepare_detail=f"runner installed over SSH as root; active - "
                       f"{pov_template_builder.DOCKER_MISSING_MARKER}"))
    db.commit()
    env = pov_env_service.get(db, env_id)
    try:
        asyncio.run(pov_broker.ensure_broker(db, env, sleep=_no_sleep))
        raise AssertionError("the wait should have timed out")
    except pov_broker.BrokerError as exc:
        message = str(exc)
        assert pov_template_builder.DOCKER_MISSING_MARKER in message, \
            f"the timeout must name the recorded runtime gap: {message}"
        assert "weaver" in message, "and the template it is a fact about"
        # Still the whole message: the other candidates did not stop being candidates.
        assert "metadata runner" in message, message
    finally:
        db.close()
        _restore(original)


def test_the_timeout_invents_nothing_for_a_template_this_dashboard_never_built():
    """Silence is right here. A POV built from a template somebody else authored has no
    build row to read, and a sentence about a runtime nobody probed would be worse than the
    three honest candidates it was meant to improve on."""
    _set_url()
    original = _install(FakeAdapter())
    # Its own template id: the test above leaves a build row on "42", and reading that one
    # here would prove the opposite of what this asserts while still passing.
    env_id = _new_env(template_id="no-build-99")
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    try:
        asyncio.run(pov_broker.ensure_broker(db, env, sleep=_no_sleep))
        raise AssertionError("the wait should have timed out")
    except pov_broker.BrokerError as exc:
        message = str(exc)
        assert pov_template_builder.DOCKER_MISSING_MARKER not in message, message
        assert "metadata runner" in message, "the ordinary message still stands"
    finally:
        db.close()
        _restore(original)


def test_a_re_run_re_issues_the_same_agent_rather_than_minting_a_second():
    """Two rows for one POV is how teardown revokes the wrong one."""
    _set_url()
    original = _install(FakeAdapter())
    env_id = _new_env()
    for _ in range(2):
        db = d.SessionLocal()
        env = pov_env_service.get(db, env_id)
        try:
            asyncio.run(pov_broker.ensure_broker(db, env, sleep=_no_sleep))
        except pov_broker.BrokerError:
            pass
        finally:
            db.close()
    _restore(original)

    _, agent_id, _, _ = _reload(env_id)
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    rows = db.query(d.RemoteAgent).filter(
        d.RemoteAgent.name == pov_broker.agent_name(env)).all()
    db.close()
    assert len(rows) == 1, "a second run minted a second agent row"
    assert rows[0].id == agent_id


def test_a_successful_enrolment_clears_the_spent_payload():
    """user_data is readable by anyone who can read the environment, and a reboot would
    otherwise re-run a bootstrap whose code is gone."""
    _set_url()
    adapter = FakeAdapter()
    original = _install(adapter)
    env_id = _new_env()
    holder = {"id": None}

    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)

    async def sleeper(_seconds):
        holder["id"] = env.broker_agent_id
        inner = d.SessionLocal()
        row = inner.query(d.RemoteAgent).filter(
            d.RemoteAgent.id == holder["id"]).first()
        row.public_key = "fake-public-key"
        inner.commit()
        inner.close()

    try:
        summary = asyncio.run(pov_broker.ensure_broker(db, env, sleep=sleeper))
    finally:
        db.close()
        _restore(original)

    assert "enrolled" in summary
    assert len(adapter.injected) == 2, "the payload was never cleared"
    assert adapter.injected[0][1], "the first injection must carry the script"
    assert adapter.injected[1][1] == "", "the second must clear it"
    assert _reload(env_id)[3] == "", "a success must clear any previous broker error"


def test_a_plaintext_agent_url_is_refused_before_anything_is_minted():
    """The agent refuses to sign over plaintext, so a broker installed against http://
    would never enrol — and the operator would debug the POV instead of the proxy."""
    _set_url("http://agents.example.test")
    adapter = FakeAdapter()
    original = _install(adapter)
    env_id = _new_env()
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    try:
        asyncio.run(pov_broker.ensure_broker(db, env, sleep=_no_sleep))
        raise AssertionError("a plaintext audience was accepted")
    except pov_broker.BrokerError as exc:
        assert "plaintext" in str(exc)
    finally:
        db.close()
        _restore(original)
        _set_url()
    assert adapter.injected == []


def test_an_unknown_agent_url_is_refused_with_the_remedy():
    _clear_url()
    original = _install(FakeAdapter())
    env_id = _new_env()
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    try:
        asyncio.run(pov_broker.ensure_broker(db, env, sleep=_no_sleep))
        raise AssertionError("a missing audience was accepted")
    except pov_broker.BrokerError as exc:
        assert "Public base URL" in str(exc)
    finally:
        db.close()
        _restore(original)
        _set_url()


def test_an_environment_with_no_private_addresses_is_refused():
    """An empty policy grants nothing and the agent refuses to start — which from here
    is indistinguishable from a network fault."""
    _set_url()
    original = _install(FakeAdapter(vms=[
        {"id": "vm-1", "name": "broker", "os_family": "linux", "private_ip": ""},
    ]))
    env_id = _new_env()
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    try:
        asyncio.run(pov_broker.ensure_broker(db, env, sleep=_no_sleep))
        raise AssertionError("an addressless environment was accepted")
    except pov_broker.BrokerError as exc:
        assert "private address" in str(exc)
    finally:
        db.close()
        _restore(original)


# ── the provision and destroy paths ──────────────────────────────────────────

def test_a_broker_failure_leaves_the_environment_active():
    """The environment is up, billing and reapable. Failing the provision over the broker
    would trade a fixable gap for a destroyed environment."""
    _set_url()
    original = _install(FakeAdapter(inject_raises="user_data is not available"))
    db = d.SessionLocal()
    env = d.PovEnvironment(platform="skytap", name="poc-" + uuid.uuid4().hex[:6],
                           template_id="42",
                           status=pov_env_service.STATUS_PROVISIONING)
    db.add(env)
    db.commit()
    env_id = env.id
    job = job_service.create_job(db, job_type="pov_env_provision", created_by="tester",
                                 metadata={"environment_id": env_id})
    job_id = job.id
    db.close()
    try:
        asyncio.run(pov_env_service.run_env_provision(job_id, {"environment_id": env_id}))
    finally:
        _restore(original)

    _, _, status, broker_error = _reload(env_id)
    assert status == pov_env_service.STATUS_ACTIVE
    assert "user_data is not available" in broker_error, \
        "the reason must reach the row the POV page reads"

    db = d.SessionLocal()
    assert db.query(d.Job).filter(d.Job.id == job_id).first().status == "completed"
    db.close()


def test_teardown_revokes_and_removes_the_agent_and_is_idempotent():
    _set_url()
    original = _install(FakeAdapter())
    env_id = _new_env()
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    try:
        asyncio.run(pov_broker.ensure_broker(db, env, sleep=_no_sleep))
    except pov_broker.BrokerError:
        pass
    _restore(original)

    env = pov_env_service.get(db, env_id)
    agent_id = env.broker_agent_id
    assert agent_id
    pov_broker.teardown(db, env)
    assert db.query(d.RemoteAgent).filter(d.RemoteAgent.id == agent_id).first() is None
    assert env.broker_agent_id is None
    # Destroy has to survive every kind of half-finished state, so a second call is a
    # no-op rather than an error.
    assert "No broker agent" in pov_broker.teardown(db, env)
    db.close()


def test_destroy_revokes_the_agent_before_deleting_the_environment():
    """Order, not just presence. An enrolled agent whose VM has just been deleted keeps
    polling from nowhere and keeps holding whatever job it leased."""
    _set_url()
    adapter = FakeAdapter()
    original = _install(adapter)
    env_id = _new_env()
    db = d.SessionLocal()
    env = pov_env_service.get(db, env_id)
    try:
        asyncio.run(pov_broker.ensure_broker(db, env, sleep=_no_sleep))
    except pov_broker.BrokerError:
        pass
    agent_id = pov_env_service.get(db, env_id).broker_agent_id
    job = job_service.create_job(db, job_type="pov_env_destroy", created_by="tester",
                                 metadata={"environment_id": env_id})
    job_id = job.id
    db.close()

    try:
        asyncio.run(pov_env_service.run_env_destroy(job_id, {"environment_id": env_id}))
    finally:
        _restore(original)

    db = d.SessionLocal()
    assert db.query(d.RemoteAgent).filter(d.RemoteAgent.id == agent_id).first() is None
    env = pov_env_service.get(db, env_id)
    assert env.status == pov_env_service.STATUS_DESTROYED
    db.close()
    assert adapter.deleted == ["sky-900"]
    assert adapter.calls.index("delete") > 0


# ── the capability contract ──────────────────────────────────────────────────

def test_skytap_declares_the_mechanism_this_module_implements():
    """`bootstrap_injection` is an intent with more than one mechanism. This module is
    written against exactly one of them, and says so rather than failing late."""
    assert lab_platforms.capabilities("skytap")["bootstrap_injection"] == "metadata"


def test_the_adapter_exposes_inject_bootstrap():
    """It is in WRITE_CONTRACT, and this slice is the one that makes it real."""
    assert "inject_bootstrap" in lab_platforms.WRITE_CONTRACT
    mod = lab_platforms.adapter("skytap")
    assert callable(getattr(mod, "inject_bootstrap", None))


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
