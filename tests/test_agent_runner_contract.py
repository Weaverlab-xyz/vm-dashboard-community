"""The contract between the dashboard and the agent container.

``runners/agent/agent.py`` ships and versions independently of the dashboard — it is a
separate image with its own build context, exactly like ``runners/promote/entrypoint.py``
— so it vendors its own copy of the request canonicalization instead of importing
``web_dashboard.services.agent_signing``.

A vendored copy can drift, and this particular drift is silent in the worst way: every
signature would still be produced and every one would fail to verify, which looks like
a revoked agent rather than a code bug. These assertions pin the copy against the
original, byte for byte, and pin the rest of the wire contract the same way:

  * the canonical form and the header names are identical on both sides;
  * every path the agent calls is a route the dashboard actually declares;
  * the agent's handler table matches ``agent_service.AGENT_JOB_TYPES``;
  * the agent's dependency list stays small enough to audit;
  * a hand-written enrolment-code file survives whatever encoding the operator's editor
    chose — UTF-16 is explained rather than raising, a BOM is stripped rather than sent;
  * the SELinux advice is withheld where it cannot possibly apply;
  * the shipped examples keep the SELinux relabel flag on the policy mount, without which
    the agent cannot read its own policy on any RHEL-family host;
  * the opt-in sibling overlay keys its override on the same service as the base compose
    file, without which the documented merge command cannot apply the Docker socket.

Runs under pytest, or standalone:
    python tests/test_agent_runner_contract.py
"""
import ast
import contextlib
import importlib.util
import os
import re
import stat
import sys

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXAMPLES = os.path.join(_ROOT, "examples", "remote-agent")
_AGENT = os.path.join(_ROOT, "runners", "agent", "agent.py")
_SIGNING = os.path.join(_ROOT, "web_dashboard", "services", "agent_signing.py")
_SERVICE = os.path.join(_ROOT, "web_dashboard", "services", "agent_service.py")
_API = os.path.join(_ROOT, "web_dashboard", "api", "agent.py")
_DOCKERFILE = os.path.join(_ROOT, "runners", "agent", "Dockerfile")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


try:
    agent = _load("agent_runner", _AGENT)
    signing = _load("agent_signing", _SIGNING)
except Exception as exc:  # pragma: no cover — deps missing
    try:
        import pytest
        pytest.skip(f"modules not importable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


# ── The vendored copy must not drift ──────────────────────────────────────────

def test_serialize_is_byte_identical():
    for payload in ({"b": 1, "a": 2}, {"z": [1, {"n": None}], "a": "x"}, {},
                    {"unicode": "café", "esc": 'a"b\\c'}):
        assert agent.serialize(payload) == signing.serialize(payload), payload


def test_canonical_request_is_byte_identical():
    """The single most important assertion in this file. If these disagree, every
    signature the agent makes is rejected and the symptom looks like a revoked key."""
    cases = [
        dict(agent_id="a-1", timestamp="1700000000", nonce="deadbeef",
             audience="https://d.example.com", method="POST",
             path="/api/agent/lease", body=b""),
        dict(agent_id="a-2", timestamp="1", nonce="n", audience="https://x",
             method="post", path="/api/agent/jobs/abc/complete", body=b'{"x":1}'),
        dict(agent_id="ünïcode", timestamp="0", nonce="z", audience="https://x/",
             method="DELETE", path="/api/agent/p", body=b"\x00\xff"),
    ]
    for kwargs in cases:
        assert agent.canonical_request(**kwargs) == signing.canonical_request(**kwargs)


def test_a_signature_made_by_the_agent_verifies_on_the_dashboard():
    """End to end through both implementations, which is the property that actually
    matters at runtime."""
    private, public = signing.generate_keypair()
    identity = agent.Identity("a-1", private, public, "https://d.example.com")
    body = b'{"hello":"world"}'
    message = agent.canonical_request(
        agent_id="a-1", timestamp="1700000000", nonce="abc",
        audience="https://d.example.com", method="POST",
        path="/api/agent/lease", body=body)
    assert signing.verify_request(
        public, agent_id="a-1", timestamp="1700000000", nonce="abc",
        signature=identity.sign(message), audience="https://d.example.com",
        method="POST", path="/api/agent/lease", body=body, now=1700000000)


def test_the_agent_verifies_an_envelope_the_dashboard_signed():
    """The other direction: job provenance."""
    private, public = signing.generate_keypair()
    identity = agent.Identity("a-1", "", public, "https://d")
    envelope = {"job_id": "j1", "job_type": "agent_discover", "payload": {}}
    good = signing.sign_envelope(private, envelope)
    assert identity.verify_dashboard(agent.serialize(envelope), good)
    tampered = dict(envelope, payload={"cidrs": ["0.0.0.0/0"]})
    assert not identity.verify_dashboard(agent.serialize(tampered), good)


def test_header_names_match():
    for name in ("HEADER_AGENT_ID", "HEADER_TIMESTAMP", "HEADER_NONCE",
                 "HEADER_SIGNATURE"):
        assert getattr(agent, name) == getattr(signing, name), name


# ── The wire contract ─────────────────────────────────────────────────────────

def _declared_routes() -> set:
    src = _read(_API)
    prefix = re.search(r'APIRouter\(prefix="([^"]+)"', src).group(1)
    return {prefix + p for p in
            re.findall(r'@router\.(?:get|post|delete|patch)\("([^"]*)"', src)}


def _agent_called_paths() -> set:
    """Every literal and f-string path the agent passes to _request()."""
    tree = ast.parse(_read(_AGENT))
    paths = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == "_request"):
            continue
        if len(node.args) < 2:
            continue
        arg = node.args[1]
        if isinstance(arg, ast.Constant):
            paths.add(arg.value)
        elif isinstance(arg, ast.JoinedStr):
            # Rebuild the f-string with each placeholder as a path parameter.
            out = ""
            for part in arg.values:
                out += part.value if isinstance(part, ast.Constant) else "{p}"
            paths.add(out)
    return paths


def test_every_path_the_agent_calls_is_a_declared_route():
    declared = _declared_routes()
    # Normalise both sides' path params to a single placeholder so /{job_id} and the
    # agent's f-string hole compare equal.
    norm = lambda p: re.sub(r"\{[^}]*\}", "{p}", p)  # noqa: E731
    declared_norm = {norm(p) for p in declared}
    called = _agent_called_paths()
    assert called, "no agent HTTP calls found — did _request get renamed?"
    missing = {p for p in called if norm(p) not in declared_norm}
    assert not missing, (
        f"the agent calls paths the dashboard does not declare: {sorted(missing)}; "
        f"declared: {sorted(declared)}")


def test_the_handler_table_matches_the_servers_allowed_types():
    """An agent that cannot handle a type the dashboard will queue for it means jobs
    that fail on arrival; the reverse means dead code in the image."""
    tree = ast.parse(_read(_SERVICE))
    server_types = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "AGENT_JOB_TYPES" for t in node.targets):
            server_types = {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    assert server_types, "AGENT_JOB_TYPES not found"
    assert set(agent.HANDLERS) == server_types, (
        f"agent handlers {sorted(agent.HANDLERS)} != server types {sorted(server_types)}")


def test_the_agent_reports_its_policy_hash_at_enrolment():
    """The operator's only signal that the file changed under them."""
    src = _read(_AGENT)
    assert '"policy_hash"' in src and "POLICY.digest" in src


@contextlib.contextmanager
def _pretend_host(*, docker_desktop: bool):
    """Pin `_in_docker_desktop_vm`, which otherwise reads the machine running the tests.

    Without this the SELinux assertions below pass on CI's bare Ubuntu runner and fail on a
    developer's box, because the container these tests usually run in *is* Docker Desktop's
    Linux VM on Windows and macOS — the probe would be answering correctly about the wrong
    host. Both messages need asserting on regardless of where pytest happens to be.
    """
    original = agent._in_docker_desktop_vm
    agent._in_docker_desktop_vm = lambda: docker_desktop
    try:
        yield
    finally:
        agent._in_docker_desktop_vm = original


def test_an_unreadable_but_world_readable_policy_names_selinux():
    """The container log is the *only* place this failure is visible.

    The policy is loaded before the first network call, so on an SELinux-enforcing host the
    agent exits 2 having sent the dashboard nothing at all — no request, no 4xx, no log
    line — while the mode bits say the file is perfectly readable. Whatever the message
    says here is the entire diagnosis, so it has to name the label rather than repeat the
    uid-10001 advice that applies to the other case.
    """
    import errno as _errno
    import tempfile

    with tempfile.TemporaryDirectory() as tmp, _pretend_host(docker_desktop=False):
        path = os.path.join(tmp, "policy.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("targets: []\n")
        os.chmod(path, 0o644)

        msg = agent._policy_unreadable(
            path, PermissionError(_errno.EACCES, "Permission denied"))
        assert "SELinux" in msg and ":ro,Z" in msg and "chcon" in msg, msg
        # The uid/mode explanation is actively misleading here — the mode is fine.
        assert "chmod" not in msg, msg

        # ENOENT is the ordinary missing-mount case and keeps the plain message.
        assert "SELinux" not in agent._policy_unreadable(
            path, FileNotFoundError(_errno.ENOENT, "No such file or directory"))

        # A genuinely owner-only file is the uid-10001 case, not the label one. Windows
        # ignores chmod's group/other bits, so only assert this where it takes effect.
        os.chmod(path, 0o600)
        if not (os.stat(path).st_mode & stat.S_IROTH):
            owner_only = agent._policy_unreadable(
                path, PermissionError(_errno.EACCES, "Permission denied"))
            assert "chmod" in owner_only and "SELinux" not in owner_only, owner_only


def test_the_selinux_advice_is_withheld_inside_a_docker_desktop_vm():
    """`chcon` is not merely unhelpful on a Windows or macOS host — it is unfollowable.

    The image is Linux-only and runs on those hosts in Docker Desktop's Linux VM, which has
    no SELinux to relabel anything in. An unreadable bind mount there is file sharing: an
    unshared drive, a UNC path, or the Hyper-V backend materialising an unreachable bind
    source as an empty *directory* inside the container. Emitting the label message anyway
    sends the operator after `chcon` for an afternoon, and `docs/remote-agents.md` marks the
    same troubleshooting rows Linux-only for exactly this reason.
    """
    import errno as _errno
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "policy.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("targets: []\n")
        os.chmod(path, 0o644)
        denied = PermissionError(_errno.EACCES, "Permission denied")

        with _pretend_host(docker_desktop=True):
            msg = agent._policy_unreadable(path, denied)
            # Not one runnable label remedy survives. SELinux may still be *named* — ruling
            # it out is the useful part for anyone who already found that advice — but no
            # command an operator could copy out of the log and try.
            for absent in ("chcon", "ls -lZ", "ausearch", "user_home_t", ":ro,Z"):
                assert absent not in msg, f"{absent!r} cannot apply in this VM: {msg}"
            assert "File sharing" in msg and "Docker Desktop" in msg, msg
            # Still says the code is unspent — the reason an operator can retry at all.
            assert "unspent" in msg, msg

            # Even the stat-was-refused branch stops naming a label it cannot have. Restore
            # the mode before asserting, or TemporaryDirectory's cleanup masks the failure.
            os.chmod(tmp, 0o000)
            try:
                blind = (agent._policy_unreadable(path, denied)
                         if not os.access(path, os.R_OK) else "")
            finally:
                os.chmod(tmp, 0o700)
            if blind:  # skipped for root, and on Windows where chmod does not bite
                assert "SELinux" not in blind and "File sharing" in blind, blind

        # And off, the Linux message comes back — qualified as Linux, not unqualified.
        with _pretend_host(docker_desktop=False):
            linux = agent._policy_unreadable(path, denied)
            assert "SELinux" in linux and "chcon" in linux, linux


def test_the_docker_desktop_probe_fails_closed():
    """It reads /proc/version, which is absent on Windows and may be anything at all.

    False has to be the answer to every surprise, because False selects the message that
    names *both* causes; True selects one that flatly denies SELinux is involved. Being
    wrong in that direction on a Fedora host would be the original bug with a new coat.
    """
    assert agent._in_docker_desktop_vm() in (True, False)  # never raises

    lowered = [m.lower() for m in agent._DESKTOP_KERNEL_MARKERS]
    assert "microsoft" in lowered and "linuxkit" in lowered, agent._DESKTOP_KERNEL_MARKERS
    # Matching is done on a lowercased read, so an uppercase marker could never fire.
    assert all(m == m.lower() for m in agent._DESKTOP_KERNEL_MARKERS)


# ── The enrolment code file ───────────────────────────────────────────────────
# An operator writes this one by hand — that is the whole point of the file form, since it
# keeps the code out of `docker inspect` and out of shell history. So its encoding is
# whatever their editor chose, and on Windows that is very often not UTF-8.


def _code_file(tmp: str, data: bytes) -> str:
    path = os.path.join(tmp, "code.txt")
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _read_code(path: str) -> str:
    """Call `enrollment_code()` with the module globals it reads pointed at `path`."""
    saved = (agent.ENROLLMENT_CODE_FILE, agent.ENROLLMENT_CODE)
    try:
        agent.ENROLLMENT_CODE_FILE, agent.ENROLLMENT_CODE = path, "from-the-env"
        return agent.enrollment_code()
    finally:
        agent.ENROLLMENT_CODE_FILE, agent.ENROLLMENT_CODE = saved


_CODE = "agte_" + "a" * 64


def test_a_utf16_code_file_is_explained_rather_than_a_traceback():
    """PowerShell 5.1's `>` and `Out-File` write UTF-16LE, and Notepad calls it "Unicode".

    `UnicodeDecodeError` is a `ValueError`, not an `OSError`, so it used to sail past the
    handler and kill the container with a raw traceback — discarding the only diagnosis the
    operator was ever going to get, because this file is read before the first network call
    and nothing about the failure reaches the dashboard.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        for label, raw in (
            ("utf-16-le with BOM", _CODE.encode("utf-16")),        # PowerShell 5.1 `>`
            ("utf-16-be with BOM", b"\xfe\xff" + _CODE.encode("utf-16-be")),
            # No BOM: this one *decodes* as UTF-8, because the high bytes are NUL and NUL
            # is legal UTF-8. It is the quiet half of the fault and needs its own check.
            ("utf-16-le without BOM", _CODE.encode("utf-16-le")),
        ):
            path = _code_file(tmp, raw)
            try:
                got = _read_code(path)
            except agent.AgentFatal as exc:
                msg = str(exc)
                assert "UTF-16" in msg, f"{label}: does not name the cause: {msg}"
                assert "Set-Content -Encoding ascii" in msg, (
                    f"{label}: does not name the remedy: {msg}")
                assert "AGENT_ENROLLMENT_CODE" in msg, msg
            else:
                raise AssertionError(f"{label}: returned {got!r} instead of explaining")


def test_a_utf8_bom_is_stripped_rather_than_sent_as_part_of_the_code():
    """The quietest form of the same fault: a BOM is not whitespace.

    It survives `.strip()`, so the dashboard is handed "\\ufeffagte_…" and refuses a code
    that looks correct in every editor — and the codes are single-use, so each attempt
    burns one.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        assert _read_code(_code_file(tmp, _CODE.encode("utf-8-sig"))) == _CODE
        # The ordinary cases have to keep working byte for byte.
        assert _read_code(_code_file(tmp, _CODE.encode("ascii"))) == _CODE
        assert _read_code(_code_file(tmp, (_CODE + "\r\n").encode("utf-8"))) == _CODE


def test_an_unreadable_code_file_still_names_uid_10001():
    """The pre-existing OSError message is the other half and must not have been traded
    away for the encoding one — a mode 0600 file is still the commonest failure here."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        try:
            _read_code(os.path.join(tmp, "absent.txt"))
        except agent.AgentFatal as exc:
            assert "10001" in str(exc) and "UTF-16" not in str(exc), exc
        else:
            raise AssertionError("a missing code file must be fatal")


def test_the_other_hand_written_secret_files_decode_the_same_way():
    """`password_file` and `client_secret_file` are written by the same operator, in the
    same shell, on the same host — so they carry the same fault and get the same message.

    These raise `PolicyRefusal` rather than `AgentFatal` because they are read during a
    job rather than at startup, but an undecodable one still has to say *why* instead of
    surfacing a decode traceback out of a job handler.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        utf16 = _code_file(tmp, "s3cret".encode("utf-16"))
        try:
            agent._secret_for({"name": "wk1", "password_file": utf16})
        except agent.PolicyRefusal as exc:
            assert "UTF-16" in str(exc) and "wk1" in str(exc), exc
        else:
            raise AssertionError("an undecodable password_file must be refused")

        # And a BOM'd one yields the secret itself, not the secret with a mark on the front.
        bom = os.path.join(tmp, "pw-bom.txt")
        with open(bom, "wb") as fh:
            fh.write("s3cret".encode("utf-8-sig"))
        assert agent._secret_for({"name": "wk1", "password_file": bom}) == "s3cret"

        # safe_dump rather than an f-string: the path is a Windows one under test here, and
        # its backslashes have to reach the agent as written.
        ps_client = os.path.join(tmp, "passwordsafe.yaml")
        with open(ps_client, "w", encoding="utf-8") as fh:
            yaml.safe_dump({"api_url": "https://ps.example.com", "client_id": "cid",
                            "client_secret_file": utf16}, fh)
        try:
            agent.PasswordSafe.from_file(ps_client)
        except agent.PolicyRefusal as exc:
            assert "UTF-16" in str(exc), exc
        else:
            raise AssertionError("an undecodable client_secret_file must be refused")


# ── The shipped examples ──────────────────────────────────────────────────────

def test_the_example_mounts_carry_the_selinux_relabel_flag():
    """Same bug as the emitted `docker run`, in the files an operator copies instead.

    `tests/test_agent_guard.py` pins the API's install command; these are the other two
    paths to a running agent, and a `:ro` here fails exactly as confusingly.
    """
    # Every /etc/dashboard-agent mount, not just policy.yaml: connections.yaml joined
    # it later and would have shipped with a bare `:ro` had this stayed name-specific.
    for name in ("docker-compose.yml", "policy.example.yaml",
                 "connections.example.yaml"):
        body = _read(os.path.join(_EXAMPLES, name))
        mounts = [ln for ln in body.splitlines()
                  if "/etc/dashboard-agent/" in ln and ":ro" in ln]
        assert mounts, f"{name}: no agent config mount found"
        for line in mounts:
            assert ":ro,Z" in line, (
                f"{name}: this mount needs the SELinux relabel flag: {line.strip()}")

    # `docker run -v` rejects a relative source path outright, so the documented command
    # has to be absolute or it cannot be pasted at all.
    policy_example = _read(os.path.join(_EXAMPLES, "policy.example.yaml"))
    assert "-v ./policy.yaml" not in policy_example


def test_the_sibling_overlay_targets_the_base_files_service_key():
    """Compose merges by service key, not by `container_name`.

    The base file declares the service `agent:` and merely *names* the container
    `dashboard-agent`. An overlay keyed on the container name does not add the socket to
    the agent — it declares a second service with no image, and the documented
    `-f docker-compose.yml -f docker-compose.sibling.yml` fails validation outright.
    Shipped that way, so the whole opt-in path was unreachable.
    """
    base = yaml.safe_load(_read(os.path.join(_EXAMPLES, "docker-compose.yml")))
    overlay = yaml.safe_load(_read(os.path.join(_EXAMPLES,
                                               "docker-compose.sibling.yml")))
    extra = set(overlay["services"]) - set(base["services"])
    assert not extra, (
        f"the overlay declares service(s) {sorted(extra)} that the base file does not "
        f"define; Compose merges by service key, so this adds a new imageless service "
        f"instead of mounting the socket on the agent")

    # The socket has to land on the service, and the variable has to name that mount's
    # container-side path — the overlay's own comment promises exactly this.
    for name, svc in overlay["services"].items():
        target = svc["environment"]["AGENT_DOCKER_SOCKET"]
        assert any(v.endswith(f":{target}") for v in svc["volumes"]), (
            f"{name}: AGENT_DOCKER_SOCKET={target} matches no mount target")


def test_the_base_example_mounts_no_container_socket():
    """The promise that makes the overlay a separate file: without it the agent holds no
    socket, launches nothing, and cannot be made to."""
    base = yaml.safe_load(_read(os.path.join(_EXAMPLES, "docker-compose.yml")))
    for name, svc in base["services"].items():
        for vol in svc.get("volumes") or []:
            assert "docker.sock" not in vol and "podman.sock" not in vol, (
                f"{name}: the base file must not mount a container socket: {vol}")


# ── Image hygiene ─────────────────────────────────────────────────────────────

_ALLOWED_THIRD_PARTY = {"requests", "yaml", "cryptography"}


def test_the_agent_depends_on_almost_nothing():
    """A small dependency set is what keeps this image auditable, and it is also the
    proof that the agent cannot run a playbook: no ansible, no kubectl, no docker."""
    tree = ast.parse(_read(_AGENT))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    third_party = imported - set(sys.stdlib_module_names)
    assert third_party <= _ALLOWED_THIRD_PARTY, (
        f"new third-party dependency in the agent: "
        f"{sorted(third_party - _ALLOWED_THIRD_PARTY)}")


def test_the_dockerfile_runs_as_a_non_root_user():
    body = _dockerfile_instructions()
    assert re.search(r"^USER\s+\d+", body, re.M), (
        "the agent Dockerfile must declare a numeric non-root USER")
    assert "10001" in body


def _dockerfile_instructions() -> str:
    """The Dockerfile with comment lines removed.

    Its comments name the things this image deliberately does NOT carry, so scanning
    the raw text would flag the very statement of the rule.
    """
    return "\n".join(line for line in _read(_DOCKERFILE).splitlines()
                     if not line.lstrip().startswith("#"))


def test_the_image_installs_only_the_allowed_dependencies():
    body = _dockerfile_instructions()
    installed = set(re.findall(r'"([a-zA-Z0-9_-]+)[><=]', body))
    unexpected = {p.lower() for p in installed} - {"requests", "pyyaml", "cryptography"}
    assert not unexpected, f"unexpected pip package in the agent image: {sorted(unexpected)}"
    for banned in ("ansible", "kubectl", "helm", "docker.io", "docker-ce"):
        assert banned not in body.lower(), (
            f"the agent image must not contain {banned} — it is a supervisor, not a runner")


def test_the_published_image_is_wired_into_ci():
    workflow = _read(os.path.join(_ROOT, ".github", "workflows", "publish-images.yml"))
    assert "chrweav/dashboard-agent" in workflow
    assert "runners/agent/Dockerfile" in workflow


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
