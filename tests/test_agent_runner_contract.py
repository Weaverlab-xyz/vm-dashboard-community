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
  * the shipped examples keep the SELinux relabel flag on the policy mount, without which
    the agent cannot read its own policy on any RHEL-family host.

Runs under pytest, or standalone:
    python tests/test_agent_runner_contract.py
"""
import ast
import importlib.util
import os
import re
import stat
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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

    with tempfile.TemporaryDirectory() as tmp:
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
        body = _read(os.path.join(_ROOT, "examples", "remote-agent", name))
        mounts = [ln for ln in body.splitlines()
                  if "/etc/dashboard-agent/" in ln and ":ro" in ln]
        assert mounts, f"{name}: no agent config mount found"
        for line in mounts:
            assert ":ro,Z" in line, (
                f"{name}: this mount needs the SELinux relabel flag: {line.strip()}")

    # `docker run -v` rejects a relative source path outright, so the documented command
    # has to be absolute or it cannot be pasted at all.
    policy_example = _read(os.path.join(_ROOT, "examples", "remote-agent",
                                        "policy.example.yaml"))
    assert "-v ./policy.yaml" not in policy_example


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
