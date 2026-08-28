"""Invariants for the remote agent's hypervisor probes and its policy allow-list.

Two properties matter here and neither is visible from a passing scan.

**The probes never authenticate.** Discovery reads what a management endpoint says
before any credential is offered: a VMware service descriptor, an XAPI ``Server``
header, a Prism 401. If one ever grew a login attempt it would still "work" — and it
would also lock out service accounts across a customer's estate and read, in their
SIEM, exactly like credential spraying. So there is a static assertion that no probe
sends anything credential-shaped, and it explicitly bans the NTLM negotiate token that
would otherwise be the tempting way to fingerprint a Windows host.

**The policy fails closed.** A missing or corrupt policy file must refuse everything.
Fail-open is the classic inversion bug in exactly this kind of code, and it is
invisible until someone reads the file that granted the access.

Identification is tested as a pure function over captured response bytes — no sockets,
no fake TLS — which is what keeps this suite as good as the byte-fixture tests it
replaced. Needs requests/PyYAML/cryptography (the agent's own dependencies). Runs under
pytest, or standalone:
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


def _resp(status: str = "200 OK", headers: dict = None, body: str = "") -> bytes:
    """An HTTP response as bytes, the way _identify receives it."""
    lines = [f"HTTP/1.1 {status}"]
    for key, value in (headers or {}).items():
        lines.append(f"{key}: {value}")
    return ("\r\n".join(lines) + "\r\n\r\n" + body).encode()


# ── Product identification (pure, against captured-shape responses) ────────────

def test_vcenter_is_identified_with_its_version():
    body = ("<ServiceContent><about><fullName>VMware vCenter Server 8.0.3 "
            "build-24022515</fullName><apiType>VirtualCenter</apiType>"
            "<apiVersion>8.0.3.0</apiVersion><build>24022515</build></about>"
            "</ServiceContent>")
    found = agent._identify(_resp(body=body), None, "10.0.0.5", 443)
    assert found["product"] == "vsphere"
    assert found["confidence"] == "confirmed"
    assert "8.0.3" in found["server_version"]
    assert found["build"] == "24022515"
    assert found["endpoint"] == "https://10.0.0.5:443"


def test_a_bare_esxi_host_is_told_apart_from_vcenter():
    """apiType is the discriminator, and it matters for more than labelling: the agent
    transport needs vCenter's Automation REST API, which ESXi does not serve."""
    body = ("<about><fullName>VMware ESXi 8.0.2 build-22380479</fullName>"
            "<apiType>HostAgent</apiType></about>")
    found = agent._identify(_resp(body=body), None, "10.0.0.6", 443)
    assert found["product"] == "esxi"


def test_xcpng_is_identified_from_the_server_header():
    found = agent._identify(_resp(headers={"Server": "Xapi/25.6.0"}), None, "10.0.0.7", 443)
    assert found["product"] == "xcpng"
    assert found["server_version"] == "25.6.0"
    assert found["confidence"] == "confirmed"


def test_proxmox_is_identified_but_reports_no_version():
    """/api2/json/version is auth-gated, so there is no version to report and we do not
    invent one."""
    found = agent._identify(
        _resp(headers={"Server": "pve-api-daemon/3.0"},
              body="<title>pve1 - Proxmox Virtual Environment</title>"),
        None, "10.0.0.8", 8006)
    assert found["product"] == "proxmox"
    assert found["server_version"] == ""
    assert found["confidence"] == "confirmed"


def test_nutanix_prism_is_identified_from_its_refusal():
    """A 401 is a refusal, not a login attempt — no credential is ever sent."""
    found = agent._identify(
        _resp("401 Unauthorized", {"WWW-Authenticate": 'Basic realm="Nutanix"'}),
        None, "10.0.0.9", 9440)
    assert found["product"] == "nutanix"
    assert found["confidence"] == "confirmed"


def test_winrm_is_reported_as_possible_not_confirmed():
    """The honest limit of the whole probe set: this identifies WinRM on Windows, and
    nearly every domain-joined Windows Server has WinRM on. Claiming "Hyper-V" here
    would send operators to add connections to file servers."""
    found = agent._identify(
        _resp("401 Unauthorized", {"Server": "Microsoft-HTTPAPI/2.0",
                                   "WWW-Authenticate": "Negotiate"}),
        None, "10.0.0.10", 5985)
    assert found["product"] == "winrm"
    assert found["confidence"] == "possible"


def test_unrelated_services_are_not_mislabelled():
    """Proxmox Backup Server and oVirt both answer on ports we probe. Returning None
    for them is better than a finding an operator would act on."""
    cases = [
        (_resp(headers={"Server": "nginx/1.24.0"}), "10.0.0.11", 443),
        (_resp(headers={"Server": "proxmox-backup-api/3.0"}), "10.0.0.12", 8007),
        (_resp("401 Unauthorized", {"Server": "Apache"},
               "<html>oVirt Engine</html>"), "10.0.0.13", 443),
        (_resp(), "10.0.0.14", 443),
    ]
    for raw, ip, port in cases:
        assert agent._identify(raw, None, ip, port) is None, f"{ip}:{port} mislabelled"


def test_every_finding_declares_a_confidence():
    """The UI renders `possible` differently, so a finding without one would silently
    read as confirmed."""
    samples = [
        (_resp(body="<namespaces><name>urn:vim25</name></namespaces>"), 443),
        (_resp(headers={"Server": "Xapi/25.6.0"}), 443),
        (_resp(headers={"Server": "pve-api-daemon/3.0"}), 8006),
        (_resp("401 Unauthorized", {"WWW-Authenticate": 'Basic realm="Nutanix"'}), 9440),
        (_resp("401 Unauthorized", {"Server": "Microsoft-HTTPAPI/2.0"}), 5985),
    ]
    for raw, port in samples:
        found = agent._identify(raw, None, "10.0.0.20", port)
        assert found and found["confidence"] in ("confirmed", "possible")
        assert found["kind"] == "hypervisor"


def test_headers_are_matched_case_insensitively():
    found = agent._identify(_resp(headers={"server": "Xapi/25.6.0"}), None, "10.0.0.21", 443)
    assert found and found["product"] == "xcpng"


# ── The probes never authenticate ─────────────────────────────────────────────

def _code_of(name: str) -> str:
    import ast
    with open(_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    node = next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    body = node.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return "\n".join(ast.unparse(stmt) for stmt in body)


def test_no_probe_sends_anything_credential_shaped():
    """Static scan of the probe functions. A probe that grew a username, a password or
    an auth token would still pass every functional test above — this is the assertion
    that catches it.

    ``NTLM``/``Negotiate``/``Basic`` are banned explicitly. There IS a way to read a
    Windows host's OS build, NetBIOS name and DNS domain anonymously: send an NTLM
    type-1 negotiate token, which carries no credential, and parse the AV pairs out of
    the type-2 challenge. It is tempting, because it would turn `possible` into
    something much better. We do not do it: it initiates an authentication exchange and
    lands in the Windows Security log as a logon event, and "probes never authenticate,
    not once" is the sentence this whole feature is sold on.
    """
    banned = re.compile(
        r"(password|passwd|credential|username|LOGIN7|auth_plugin|NTLM|Negotiate|Basic )")
    for name in ("_https_probe", "probe_hypervisor", "_identify"):
        hits = banned.findall(_code_of(name))
        assert not hits, f"{name} references {hits} — probes must never authenticate"


def test_every_probe_sends_only_an_unauthenticated_get():
    code = _code_of("_https_probe")
    assert "GET " in code
    assert "Authorization" not in code, "the probe must not send a credential"


def test_the_probe_refuses_deprecated_tls_versions():
    """The probe turns verification off — it is identifying an unknown host whose CA it
    has never seen — and that also relaxes the context's security level enough to
    negotiate TLS 1.0/1.1.

    Asserted against the live context rather than the source, because the default
    minimum depends on the OpenSSL the image was built against. Every management
    endpoint we probe has required TLS 1.2 for years, so pinning the floor costs no
    discovery reach.
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
        agent._https_probe("127.0.0.1", 1, 0.05)
    finally:
        _ssl.create_default_context = original

    assert created, "_https_probe did not build an SSL context"
    ctx = created[0]
    assert ctx.minimum_version >= _ssl.TLSVersion.TLSv1_2, (
        f"the probe would negotiate {ctx.minimum_version!r}; pin TLSv1_2 or higher")
    # And the verification posture is still deliberately off — if this ever flips, the
    # probe stops working against every self-signed management endpoint out there.
    assert ctx.verify_mode == _ssl.CERT_NONE and ctx.check_hostname is False


def test_port_443_asks_the_vmware_question_first():
    """vSphere and XCP-ng share 443. One probe per host:port that classifies whichever
    answered, not two connects."""
    code = _code_of("probe_hypervisor")
    assert "vimServiceVersions" in code
    assert code.count("_identify") >= 1


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
    list; getattr() on a string from the wire can.

    Spelled out rather than derived, so adding a handler is a deliberate edit here as well
    as there — this set is the list of everything a dashboard can ask this agent to do.
    """
    assert set(agent.HANDLERS) == {"agent_discover", "agent_hypervisor", "agent_ansible",
                                   "agent_gateway", "agent_storage"}
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
