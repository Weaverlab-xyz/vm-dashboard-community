"""Invariants for the remote agent's discovery probes and its policy allow-list.

Two properties matter here and neither is visible from a passing scan.

**The probes never authenticate.** Discovery reads pre-auth protocol banners: a
Postgres SSLRequest reply, a MySQL greeting, a TDS PRELOGIN response, a TNS refusal.
If one ever grew a login attempt it would still "work" — and it would also lock out
service accounts across a customer's estate and read, in their SIEM, exactly like
credential spraying. So there is a static assertion that no probe sends anything
credential-shaped.

**The policy fails closed.** A missing or corrupt policy file must refuse everything.
Fail-open is the classic inversion bug in exactly this kind of code, and it is
invisible until someone reads the file that granted the access.

Runs against real byte fixtures, no network. Needs requests/PyYAML/cryptography (the
agent's own dependencies). Runs under pytest, or standalone:
    python tests/test_agent_probes.py
"""
import importlib.util
import os
import re
import struct
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "runners", "agent", "agent.py")

try:
    _spec = importlib.util.spec_from_file_location("agent_runner", _PATH)
    agent = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(agent)
except Exception as exc:  # pragma: no cover — agent deps missing
    try:
        import pytest
        pytest.skip(f"agent runner not importable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


class FakeSock:
    """A socket that replays a scripted reply and records what was sent."""

    def __init__(self, reply: bytes = b""):
        self.reply = reply
        self.sent = b""

    def sendall(self, data):
        self.sent += data

    def recv(self, _n):
        out, self.reply = self.reply, b""
        return out

    def settimeout(self, _t):
        pass

    def close(self):
        pass


# ── Postgres ──────────────────────────────────────────────────────────────────

def test_postgres_ssl_supported_and_not_supported_both_identify():
    for reply, tls in ((b"S", True), (b"N", False)):
        sock = FakeSock(reply)
        found = agent.probe_postgres(sock)
        assert found["engine"] == "postgres"
        assert found["tls"] is tls
        # The SSLRequest is exactly 8 bytes: length 8, magic 80877103.
        assert sock.sent == struct.pack("!II", 8, 80877103)


def test_postgres_ignores_an_unrelated_service():
    assert agent.probe_postgres(FakeSock(b"HTTP/1.1 200 OK")) is None


# ── MySQL / MariaDB ───────────────────────────────────────────────────────────

def _mysql_greeting(version: bytes) -> bytes:
    payload = b"\x0a" + version + b"\x00" + b"\x00" * 20
    return len(payload).to_bytes(3, "little") + b"\x00" + payload


def test_mysql_greeting_yields_the_version():
    found = agent.probe_mysql(FakeSock(_mysql_greeting(b"8.4.0")))
    assert found == {"engine": "mysql", "server_version": "8.4.0"}


def test_mariadb_is_distinguished_from_mysql():
    found = agent.probe_mysql(FakeSock(_mysql_greeting(b"11.4.2-MariaDB")))
    assert found["engine"] == "mariadb"


def test_mysql_probe_sends_nothing_at_all():
    """The server speaks first, so a MySQL probe is a pure read — there is no packet
    that could be mistaken for a handshake attempt."""
    sock = FakeSock(_mysql_greeting(b"8.4.0"))
    agent.probe_mysql(sock)
    assert sock.sent == b""


def test_mysql_rejects_a_wrong_protocol_version():
    payload = b"\x09" + b"5.7.0" + b"\x00"
    frame = len(payload).to_bytes(3, "little") + b"\x00" + payload
    assert agent.probe_mysql(FakeSock(frame)) is None


def test_mysql_rejects_a_truncated_greeting():
    assert agent.probe_mysql(FakeSock(b"\x05\x00")) is None


# ── SQL Server ────────────────────────────────────────────────────────────────

def _tds_prelogin_response(major, minor, build) -> bytes:
    options = b"\x00\x00\x06\x00\x06\xff"
    data = bytes([major, minor]) + build.to_bytes(2, "big") + b"\x00\x00"
    payload = options + data
    return struct.pack("!BBHHBB", 0x04, 0x01, 8 + len(payload), 0, 0, 0) + payload


def test_mssql_prelogin_yields_the_version():
    found = agent.probe_mssql(FakeSock(_tds_prelogin_response(16, 0, 4135)))
    assert found == {"engine": "sqlserver", "server_version": "16.0.4135"}


def test_mssql_request_is_a_prelogin_not_a_login():
    """0x12 is PRELOGIN. 0x10 would be LOGIN7 — an authentication attempt."""
    sock = FakeSock(_tds_prelogin_response(16, 0, 1))
    agent.probe_mssql(sock)
    assert sock.sent[0] == 0x12, "the probe must send PRELOGIN (0x12), never LOGIN7"
    assert struct.unpack("!H", sock.sent[2:4])[0] == len(sock.sent)


def test_mssql_ignores_a_non_tds_reply():
    assert agent.probe_mssql(FakeSock(b"SSH-2.0-OpenSSH_9.6\r\n")) is None


# ── Oracle ────────────────────────────────────────────────────────────────────

def test_tns_connect_packet_is_well_formed():
    """The connect-data offset field must match the real offset or the listener
    silently drops the packet and Oracle looks like a closed port."""
    data = b"(CONNECT_DATA=(COMMAND=ping))"
    pkt = agent._tns_connect_packet(data)
    assert struct.unpack("!H", pkt[0:2])[0] == len(pkt), "declared length must match"
    assert pkt[4] == 1, "packet type must be CONNECT"
    declared_offset = struct.unpack("!H", pkt[26:28])[0]
    assert declared_offset == 58
    assert pkt[declared_offset:] == data, "connect data must start at the declared offset"


def test_oracle_accepts_refuse_and_resend_as_identification():
    for packet_type in (2, 4, 11):        # Accept, Refuse, Resend
        reply = b"\x00\x20\x00\x00" + bytes([packet_type]) + b"\x00" * 10
        assert agent.probe_oracle(FakeSock(reply))["engine"] == "oracle"


def test_oracle_ignores_anything_else():
    assert agent.probe_oracle(FakeSock(b"\x00\x20\x00\x00\x63" + b"\x00" * 10)) is None


# ── The no-credentials guarantee ──────────────────────────────────────────────

def _code_of(name: str) -> str:
    """A function's executable code with its docstring and comments removed.

    Scanning raw text would flag the docstrings, which exist precisely to explain that
    these probes do not authenticate — prose about a rule must not read as a breach of
    it.
    """
    import ast
    tree = ast.parse(open(_PATH, encoding="utf-8").read())
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == name)
    body = node.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return "\n".join(ast.unparse(stmt) for stmt in body)


def test_no_probe_sends_anything_credential_shaped():
    """Static scan of the probe functions. A probe that grew a username, a password or
    a login packet would still pass a functional test — this is the assertion that
    catches it."""
    banned = re.compile(r"(password|passwd|credential|username|LOGIN7|auth_plugin)", re.I)
    for name in ("probe_postgres", "probe_mysql", "probe_mssql", "probe_oracle"):
        hits = banned.findall(_code_of(name))
        assert not hits, f"{name} references {hits} — probes must never authenticate"


def test_kubernetes_probe_sends_only_an_unauthenticated_get():
    code = _code_of("probe_kubernetes")
    assert "GET /version" in code
    assert "Authorization" not in code, "the k8s probe must not send a credential"


def test_kubernetes_probe_refuses_deprecated_tls_versions():
    """The probe turns verification off — it is identifying an unknown host whose CA it
    has never seen — and that also relaxes the context's security level enough to
    negotiate TLS 1.0/1.1.

    Asserted against the live context rather than the source, because the default
    minimum depends on the OpenSSL the image was built against: it happens to be
    TLS 1.2 on some builds and is not guaranteed to be on others. Kubernetes has
    required TLS 1.2 for years, so pinning the floor costs no discovery reach.
    """
    import ssl as _ssl
    created = []
    original = _ssl.create_default_context

    def _capture(*a, **kw):
        ctx = original(*a, **kw)
        created.append(ctx)
        return ctx

    _ssl.create_default_context = _capture
    try:
        # No listener on this port, so the probe builds its context and then returns
        # None at the connect — which is all we need to inspect it.
        agent.probe_kubernetes("127.0.0.1", 1, 0.05)
    finally:
        _ssl.create_default_context = original

    assert created, "probe_kubernetes did not build an SSL context"
    ctx = created[0]
    assert ctx.minimum_version >= _ssl.TLSVersion.TLSv1_2, (
        f"the probe would negotiate {ctx.minimum_version!r}; pin TLSv1_2 or higher")
    # And the verification posture is still deliberately off — if this ever flips, the
    # probe stops working against every self-signed cluster CA out there.
    assert ctx.verify_mode == _ssl.CERT_NONE and ctx.check_hostname is False


# ── Version / distro parsing ──────────────────────────────────────────────────

def test_distro_is_derived_from_the_version_string():
    cases = {"v1.31.3+k3s1": "k3s", "v1.30.5+rke2r1": "rke2", "v1.31.0-eks-a1b2c3": "eks",
             "v1.31.3": "kubeadm", "": ""}
    for version, want in cases.items():
        assert agent._distro_of(version) == want, version


# ── Policy ────────────────────────────────────────────────────────────────────

def _policy(text: str) -> "agent.Policy":
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(text)
        path = fh.name
    try:
        return agent.Policy.load(path)
    finally:
        os.unlink(path)


_BASIC = """
targets:
  - cidr: 10.20.0.0/24
    ports: [6443, 5432]
"""


def test_policy_allows_only_what_it_names():
    p = _policy(_BASIC)
    assert p.allows("10.20.0.5", 6443)
    assert p.allows("10.20.0.5", 5432)
    assert not p.allows("10.20.0.5", 22), "a port outside the list must be refused"
    assert not p.allows("10.99.0.5", 6443), "a host outside the CIDR must be refused"


def test_link_local_is_denied_even_when_a_target_covers_it():
    """Cloud instance metadata. An operator who writes a wide CIDR has not opted into
    169.254.169.254 — they just wrote a wide CIDR."""
    p = _policy("targets:\n  - cidr: 0.0.0.0/0\n")
    assert not p.allows("169.254.169.254", 80)
    assert not p.allows("127.0.0.1", 6443)
    assert p.allows("10.20.0.5", 6443)


def test_an_explicit_deny_list_does_not_drop_the_mandatory_ones():
    p = _policy("targets:\n  - cidr: 0.0.0.0/0\ndeny:\n  - 10.99.0.0/16\n")
    assert not p.allows("10.99.0.1", 443), "the operator's own deny must apply"
    assert not p.allows("169.254.169.254", 80), "and the mandatory ones must survive it"


def test_a_missing_policy_file_refuses_everything():
    """Fail closed. The inverse of this test is the bug that makes the whole design
    worthless."""
    try:
        agent.Policy.load("/nonexistent/policy.yaml")
        raise AssertionError("a missing policy file must be fatal, not permissive")
    except agent.AgentFatal:
        pass


def test_a_corrupt_policy_file_refuses_everything():
    try:
        _policy("targets: [ this is not: valid: yaml")
        raise AssertionError("unparseable YAML must be fatal")
    except agent.AgentFatal:
        pass


def test_a_policy_with_no_targets_is_rejected():
    """An empty allow-list is almost certainly a mistake, and silently reaching
    nothing is a confusing way to find out."""
    for text in ("job_types: [agent_discover]\n", "targets: []\n"):
        try:
            _policy(text)
            raise AssertionError("a policy with no targets must be rejected")
        except agent.AgentFatal:
            pass


def test_a_bad_cidr_is_fatal_rather_than_ignored():
    """Skipping an unparseable entry would silently shrink the allow-list; being loud
    is the only safe direction when the file is the security boundary."""
    try:
        _policy("targets:\n  - cidr: 10.20.0.0/99\n")
        raise AssertionError("a malformed CIDR must be fatal")
    except agent.AgentFatal:
        pass


def test_ports_omitted_means_any_port_within_the_cidr():
    p = _policy("targets:\n  - cidr: 10.20.0.0/24\n")
    assert p.allows("10.20.0.5", 6443) and p.allows("10.20.0.5", 1521)


def test_job_types_default_to_discovery_only():
    p = _policy(_BASIC)
    assert p.job_types == {"agent_discover"}


def test_the_policy_digest_changes_with_the_content():
    """The dashboard displays this hash; if it did not move when the file did, an
    operator could not tell that the policy had been edited."""
    assert _policy(_BASIC).digest != _policy(_BASIC + "  # a comment\n").digest


def test_check_reports_a_reason():
    p = _policy(_BASIC)
    try:
        p.check("169.254.169.254", 80)
        raise AssertionError("expected a refusal")
    except agent.PolicyRefusal as exc:
        assert "denied" in str(exc)


# ── Handler table ─────────────────────────────────────────────────────────────

def test_handlers_are_a_closed_dict_not_a_dynamic_dispatch():
    """A dict lookup cannot be talked into reaching a function the author did not
    list; getattr() on a string from the wire can."""
    assert set(agent.HANDLERS) == {"agent_discover"}
    src = open(_PATH, encoding="utf-8").read()
    assert "getattr(sys.modules" not in src and "eval(" not in src and "exec(" not in src


# ── pre-flight ────────────────────────────────────────────────────────────────

def test_a_writable_state_dir_passes_preflight():
    import tempfile
    original = agent.STATE_DIR
    agent.STATE_DIR = tempfile.mkdtemp()
    try:
        agent._check_state_dir_writable()
    finally:
        agent.STATE_DIR = original


def test_an_unwritable_state_dir_fails_before_enrolment():
    """The enrolment code is spent on the SERVER the moment /enroll returns 200, and the
    identity is written only after that. So a bad volume used to fail *past* the point of
    no return: raw PermissionError, exit 1, code burnt, and under a restart policy a
    crash loop that walks straight into the per-address enrolment throttle.

    This must raise AgentFatal (exit 2, handled) rather than escaping as an OSError, and
    it must say the code is still usable — otherwise the operator's next move is to
    re-issue one they did not need to.
    """
    import os
    import tempfile
    original = agent.STATE_DIR
    # A path whose parent is a regular file: makedirs fails on every platform, unlike
    # chmod, which Windows ignores.
    blocker = os.path.join(tempfile.mkdtemp(), "blocker")
    open(blocker, "w", encoding="utf-8").close()
    agent.STATE_DIR = os.path.join(blocker, "state")
    try:
        agent._check_state_dir_writable()
        raise AssertionError("an unwritable state dir must be fatal")
    except agent.AgentFatal as exc:
        msg = str(exc)
        assert "still good" in msg, "must tell the operator the code was not burnt"
        assert "10001" in msg, "must name the uid the volume has to be writable by"
    finally:
        agent.STATE_DIR = original


def test_preflight_checks_the_state_dir_before_any_network_call():
    """Ordering is the whole point. If this moved after enrolment it would be useless —
    asserted on the source because the failure it prevents cannot be reproduced without
    a live dashboard to spend a code against."""
    code = _code_of("_preflight")
    assert "_check_state_dir_writable" in code, \
        "_preflight must check the state directory"
    src = open(_PATH, encoding="utf-8").read()
    assert src.index("def _preflight(") < src.index("def main("), \
        "preflight must be defined before main"
    main_code = _code_of("main")
    assert main_code.index("_preflight()") < main_code.index("Identity.load()"), \
        "preflight must run before the identity/enrolment path"


def test_the_agent_imports_no_execution_machinery():
    """Structurally incapable of running a playbook or shelling out. If one of these
    ever appears, the security argument in the module docstring stops being true."""
    src = open(_PATH, encoding="utf-8").read()
    for banned in ("import subprocess", "import docker", "from subprocess",
                   "os.system", "os.popen", "shutil.which"):
        assert banned not in src, f"agent.py must not use {banned}"


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
