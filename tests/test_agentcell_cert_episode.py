"""The certificate episode — the third control surface, and the one that does not stop.

The agent cell now demonstrates three credentials, and the arc is the point:

  * the **PAT** is revocable — pull it and the loop stops mid-poll;
  * the **cluster token** is gated at retrieval — a person decides, and once released it
    lives out its TTL;
  * a **certificate** is neither. `docs/integrations/certificates.md`:
    *"No revocation checking. The plugin consults neither CRLs nor OCSP. Short lifetimes
    are the mitigation, and that is a deliberate design position."*

So the demo's closing beat is uncomfortable on purpose — revoke the certificate and the
agent keeps working — and these tests exist to stop that honesty being quietly lost.

What they pin, each of which has a way of failing that leaves the demo *looking* right:

  * **Both halves, and neither usable alone.** The passphrase is a managed-account
    credential; the bundle is a Secrets Safe file secret. A change that fetched one and
    faked the other would still print a success line.
  * **The client pair reaches ps-cli through the ENVIRONMENT, never argv.**
    `/proc/<pid>/cmdline` is world-readable; that one slip would undo the whole argument.
  * **The bundle and key touch disk in exactly one guarded place**, and it is cleaned up
    on the failure path too.
  * **The CN assertion is real.** An endpoint that answers 200 without seeing the
    certificate must not read as proof.

Runs under pytest, or standalone:
    python tests/test_agentcell_cert_episode.py
"""
import http.server
import importlib.util
import os
import re
import ssl
import subprocess
import sys
import tempfile
import threading

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-agentcell-cert")

_WORKER = os.path.join(_ROOT, "examples", "playbooks", "agent", "files", "mcp_agent.py")
_API = os.path.join(_ROOT, "web_dashboard", "api", "agentcell.py")
_DOC = os.path.join(_ROOT, "docs", "profiles", "demo", "agent-demo-cell.md")

from web_dashboard.services import agentcell_service as A  # noqa: E402

_CN = "svc-deploy-pipeline"
_PASS = "s3cret"


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _code(path):
    src = re.sub(r'"""[\s\S]*?"""', "", _read(path))
    return "\n".join(ln for ln in src.splitlines()
                     if ln.strip() and not ln.lstrip().startswith("#"))


def _worker():
    spec = importlib.util.spec_from_file_location("mcp_agent", _WORKER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _have_openssl():
    try:
        subprocess.run(["openssl", "version"], capture_output=True, timeout=10)
        return True
    except Exception:  # noqa: BLE001
        return False


_PKI = {}


def _pki():
    """A real CA, a real server certificate and a real client PKCS#12.

    Generated rather than faked: the probe's whole job is to open a bundle and complete a
    mutual-TLS handshake, and a stub of either would test nothing that matters. Built once
    and cached — `openssl req` is the slow part of this file.
    """
    if _PKI:
        return _PKI
    d = tempfile.mkdtemp(prefix="cert-episode-test-")

    def _ssl(*args):
        r = subprocess.run(["openssl", *args], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"openssl {args[0]} failed: {r.stderr[:300]}"

    p = lambda n: os.path.join(d, n)  # noqa: E731
    _ssl("req", "-x509", "-newkey", "rsa:2048", "-keyout", p("ca.key"), "-out",
         p("ca.crt"), "-days", "1", "-nodes", "-subj", "/CN=demo-ca")
    _ssl("req", "-newkey", "rsa:2048", "-keyout", p("srv.key"), "-out", p("srv.csr"),
         "-nodes", "-subj", "/CN=localhost")
    with open(p("ext.cnf"), "w", encoding="utf-8") as fh:
        fh.write("subjectAltName=IP:127.0.0.1\n")
    _ssl("x509", "-req", "-in", p("srv.csr"), "-CA", p("ca.crt"), "-CAkey", p("ca.key"),
         "-CAcreateserial", "-out", p("srv.crt"), "-days", "1", "-extfile", p("ext.cnf"))
    _ssl("req", "-newkey", "rsa:2048", "-keyout", p("cli.key"), "-out", p("cli.csr"),
         "-nodes", "-subj", f"/CN={_CN}")
    _ssl("x509", "-req", "-in", p("cli.csr"), "-CA", p("ca.crt"), "-CAkey", p("ca.key"),
         "-CAcreateserial", "-out", p("cli.crt"), "-days", "1")
    _ssl("pkcs12", "-export", "-out", p("bundle.pfx"), "-inkey", p("cli.key"),
         "-in", p("cli.crt"), "-passout", f"pass:{_PASS}")
    _PKI.update({"dir": d, "bundle": open(p("bundle.pfx"), "rb").read(),
                 "srv_crt": p("srv.crt"), "srv_key": p("srv.key"), "ca": p("ca.crt")})
    return _PKI


class _MtlsEcho(http.server.BaseHTTPRequestHandler):
    """Echoes the client certificate's CN, as `nginx-mtls-endpoint.yml` does."""
    blind = False          # answer 200 without looking at the certificate

    def do_GET(self):
        cn = ""
        if not self.blind:
            for rdn in (self.connection.getpeercert() or {}).get("subject", ()):
                for k, v in rdn:
                    if k == "commonName":
                        cn = v
        self.send_response(200)
        self.end_headers()
        self.wfile.write(f"client CN={cn}\n".encode())

    def log_message(self, *a):
        pass


def _serve_mtls(handler=_MtlsEcho):
    pki = _pki()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(pki["srv_crt"], pki["srv_key"])
    ctx.load_verify_locations(pki["ca"])
    ctx.verify_mode = ssl.CERT_REQUIRED
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"https://127.0.0.1:{srv.server_address[1]}/"


# -- the probe proves the IDENTITY, not that a request succeeded ---------------

def test_the_probe_opens_a_real_bundle_and_completes_a_real_handshake():
    if not _have_openssl():
        print("   (skipped: no openssl)")
        return
    m = _worker()
    r = m.cert_mtls_probe(endpoint=_serve_mtls(), bundle=_pki()["bundle"],
                          passphrase=_PASS, expect_cn=_CN, verify=False)
    assert r["status"] == 200 and r["echoed"] and r["proved"], r
    assert _CN in m.cert_probe_summary(r)


def test_an_endpoint_that_answers_without_seeing_the_certificate_proves_nothing():
    """The failure that would otherwise read as success: 200, but the CN never echoed.
    The certificate reached the handshake and the application ignored it."""
    if not _have_openssl():
        print("   (skipped: no openssl)")
        return
    m = _worker()
    blind = type("Blind", (_MtlsEcho,), {"blind": True})
    r = m.cert_mtls_probe(endpoint=_serve_mtls(blind), bundle=_pki()["bundle"],
                          passphrase=_PASS, expect_cn=_CN, verify=False)
    assert r["status"] == 200
    assert r["proved"] is False, "a 200 with no CN echoed was accepted as proof"
    assert "did not echo" in m.cert_probe_summary(r)


def test_a_mismatched_passphrase_fails_loudly_and_names_the_cause():
    """The two halves are one identity. Out of step is the likeliest real failure, and a
    generic openssl error sends somebody to the wrong place."""
    if not _have_openssl():
        print("   (skipped: no openssl)")
        return
    m = _worker()
    try:
        m.cert_mtls_probe(endpoint=_serve_mtls(), bundle=_pki()["bundle"],
                          passphrase="wrong", expect_cn=_CN, verify=False)
    except SystemExit as exc:
        assert "two halves of one identity" in str(exc)
    else:
        raise AssertionError("a wrong passphrase was accepted")


# -- the bundle and key touch disk in exactly one guarded place ----------------

def test_the_bundle_never_outlives_the_probe():
    if not _have_openssl():
        print("   (skipped: no openssl)")
        return
    m = _worker()
    before = set(os.listdir(tempfile.gettempdir()))
    m.cert_mtls_probe(endpoint=_serve_mtls(), bundle=_pki()["bundle"],
                      passphrase=_PASS, expect_cn=_CN, verify=False)
    leaked = [n for n in set(os.listdir(tempfile.gettempdir())) - before
              if n.startswith("mcp-agent-cert-")]
    assert not leaked, f"the probe left its working directory behind: {leaked}"


def test_the_working_directory_goes_even_when_the_probe_fails():
    """A `finally` is not enough on its own — the cleanup has to cover the openssl
    failure path too, which is the one most likely to be hit in a lab."""
    if not _have_openssl():
        print("   (skipped: no openssl)")
        return
    m = _worker()
    before = set(os.listdir(tempfile.gettempdir()))
    try:
        m.cert_mtls_probe(endpoint=_serve_mtls(), bundle=_pki()["bundle"],
                          passphrase="wrong", expect_cn=_CN, verify=False)
    except SystemExit:
        pass
    leaked = [n for n in set(os.listdir(tempfile.gettempdir())) - before
              if n.startswith("mcp-agent-cert-")]
    assert not leaked, f"a failed probe left its working directory behind: {leaked}"


def test_the_key_is_written_private_and_the_passphrase_never_reaches_argv():
    code = _code(_WORKER)
    body = code.split("def cert_mtls_probe(", 1)[1].split("\ndef ")[0]
    assert "TemporaryDirectory" in body, "the probe writes outside a managed directory"
    assert "0o600" in body and "0o700" in body, "the key or its directory is not private"
    assert "env:PFXPASS" in body, \
        "the passphrase is not passed to openssl through the environment"
    assert "-passin" in body and "passphrase" not in body.split("-passin")[1][:80], \
        "the passphrase looks like it is on openssl's command line — /proc/<pid>/cmdline "\
        "is world-readable"


# -- ps-cli: the environment, never argv ---------------------------------------

def test_the_client_pair_reaches_ps_cli_through_the_environment():
    """The single slip that would undo the whole argument. /proc/<pid>/cmdline is
    world-readable; /proc/<pid>/environ is readable only by the same user, which here is
    root — which already holds everything this worker has."""
    code = _code(_WORKER)
    body = code.split("def secrets_safe_file(", 1)[1].split("\ndef ")[0]
    argv = body[body.index("argv = ["):body.index("]", body.index("argv = ["))]
    for leaked in ("client_secret", "client_id", "PSCLI_CLIENT"):
        assert leaked not in argv, f"the ps-cli argv carries {leaked!r}"
    assert "PSCLI_CLIENT_SECRET" in body and "env.update" in body, \
        "the pair is not put in the subprocess environment"
    assert "env=env" in body, "the environment is built and then not passed"


def test_the_ps_cli_argv_names_a_service_then_a_real_verb():
    """`beyondtrust-bips-cli` is unpinned, and argparse only rejects a bare verb at
    RUNTIME — six calls once shipped wrong and sat unnoticed for months. The service
    module has tests/test_pscli_grammar.py for this; the worker builds its own argv, so
    it is checked against the same table."""
    from tests.test_pscli_grammar import VERBS
    m = _worker()
    assert m.PSCLI_SERVICE in VERBS, f"{m.PSCLI_SERVICE!r} is not a ps-cli service"
    assert m.PSCLI_VERB in VERBS[m.PSCLI_SERVICE], \
        f"{m.PSCLI_VERB!r} is not a verb of the {m.PSCLI_SERVICE} service"


def test_a_missing_ps_cli_says_what_to_install():
    m = _worker()
    body = _code(_WORKER).split("def secrets_safe_file(", 1)[1].split("\ndef ")[0]
    assert "FileNotFoundError" in body and "beyondtrust-bips-cli" in body, \
        "a host without ps-cli gets a traceback rather than an instruction"


def test_something_that_is_not_a_bundle_is_refused_before_openssl_sees_it():
    """Handing openssl a non-bundle makes it complain about the passphrase, which sends
    somebody to debug the wrong half of a two-half identity."""
    body = _code(_WORKER).split("def secrets_safe_file(", 1)[1].split("\ndef ")[0]
    # On the EXPRESSION, not on "0x30" appearing somewhere: the refusal message mentions
    # it too, so a text match passed happily when the check itself was removed. That is
    # the same string-blind mistake this file's other absence checks are written to avoid.
    assert "not blob.startswith(" in body, \
        "nothing checks that the payload is DER before it is treated as a PKCS#12 — " \
        "openssl would then complain about the passphrase, sending somebody to debug " \
        "the wrong half of a two-half identity"
    assert "download-secret-file" in body, \
        "the refusal does not name the alternative ps-cli verb for a file attachment"


# -- the human in the loop must be real, not assumed ---------------------------

def test_an_ungated_release_is_refused_rather_than_reported_as_approved():
    """The one way this demo can mislead.

    The worker cannot MAKE Password Safe require approval — that is the account's access
    policy, set in BeyondInsight. What it can do is refuse to pretend there was a person
    when there was not. Without this, an auto-releasing policy fetches, probes and prints
    a success line indistinguishable from the approved one: the operator concludes a gate
    is in force, and the audit trail shows a request nobody was asked about.
    """
    m = _worker()
    assert m.approval_problem(0, True), \
        "a credential released on the first ask was accepted as approved"
    msg = m.approval_problem(0, True)
    assert "no person was consulted" in msg
    assert "access policy" in msg, "the refusal does not say how to fix it"
    assert "--no-require-approval" in msg, "the refusal does not name the opt-out"


def test_a_real_approval_is_not_refused():
    m = _worker()
    assert m.approval_problem(1, True) == ""
    assert m.approval_problem(9, True) == ""


def test_the_opt_out_exists_and_is_off_by_default():
    """Somebody without an approver still needs to run the episode — but they should have
    to say so, rather than get an approved-looking line for free."""
    m = _worker()
    assert m.approval_problem(0, False) == ""
    code = _code(_WORKER)
    assert '"--no-require-approval"' in code, "there is no way to opt out"
    assert 'dest="require_approval"' in code and "default=True" in code, \
        "the requirement is not on by default"


def test_both_episodes_check_it():
    """#912's page already claims the agent "cannot authorise its own access". On an
    auto-releasing policy that was silently untrue for the cluster episode too, so this
    is a correctness fix to an existing claim rather than a rule for one episode."""
    code = _code(_WORKER)
    for fn in ("run_k8s_episode", "run_cert_episode"):
        body = code.split(f"def {fn}(", 1)[1].split("\ndef ")[0]
        assert "approval_problem(" in body, f"{fn} does not check that a human was asked"
        assert "return 5" in body, f"{fn} has no distinct exit for an ungated release"


def test_the_refusal_returns_the_slot():
    """Refusing still has to give the request back — an abandoned one holds the account's
    concurrent slot and the next attempt reports the cap instead of the cause."""
    code = _code(_WORKER)
    for fn in ("run_k8s_episode", "run_cert_episode"):
        body = code.split(f"def {fn}(", 1)[1].split("\ndef ")[0]
        block = body[body.index("approval_problem("):]
        block = block[:block.index("return 5")]
        assert "_checkin(" in block, \
            f"{fn} refuses without releasing the request it opened"


def test_the_certificate_episode_says_why_the_human_matters_most_here():
    """A certificate cannot be revoked out from under the agent, so the approval is the
    only moment anybody gets a say. That is a stronger argument than the cluster token's
    and the episode should make it."""
    body = _read(_WORKER)
    body = body[body.index("def run_cert_episode("):body.index("\ndef main(")]
    assert "only moment a person gets a say" in body


# -- both halves, and the arc they complete ------------------------------------

def test_the_episode_fetches_both_halves():
    code = _code(_WORKER)
    body = code.split("def run_cert_episode(", 1)[1].split("\ndef ")[0]
    assert "password_safe_episode(" in body, "the passphrase is not a recorded request"
    assert "secrets_safe_file(" in body, "the bundle is never fetched"
    assert body.index("password_safe_episode(") < body.index("secrets_safe_file("), \
        "the bundle is fetched before the passphrase is released — a bundle nobody can " \
        "open is not worth retrieving"


def test_the_passphrase_is_not_validated_as_a_jwt():
    """The cluster episode's shape check would reject a perfectly good PKCS#12
    passphrase, and the rejection would read as Password Safe misbehaving."""
    code = _code(_WORKER)
    body = code.split("def run_cert_episode(", 1)[1].split("\ndef ")[0]
    assert "validate=" in body, "the episode takes the default JWT check"
    episode = code.split("def password_safe_episode(", 1)[1].split("\ndef ")[0]
    assert "check = validate or _looks_like_jwt" in episode, \
        "the shape check is hardcoded again, so one of the two callers must break"


def test_the_episode_says_that_revocation_will_not_stop_it():
    """The closing beat, and the one somebody will otherwise assume away."""
    body = _read(_WORKER)
    body = body[body.index("def run_cert_episode("):body.index("\ndef main(")]
    assert "CRL" in body and "OCSP" in body
    assert "only its expiry does" in body, \
        "the episode does not say what DOES stop the agent"


def test_the_page_carries_the_three_shapes():
    doc = _read(_DOC)
    assert "revocable" in doc.lower() and "expires" in doc
    assert "CRL" in doc and "OCSP" in doc, \
        "the page does not state that nothing on this path checks revocation"


def test_the_cert_link_needs_a_built_ca():
    """An identity against a CA that is not built yet produces a managed system that
    fails every rotation — cert_lab_service refuses for that reason, and a link promising
    the agent a certificate that cannot be issued would be the same mistake one level up.
    """
    api = _code(_API)
    branch = api.split('elif mechanism == "certificates":', 1)[1].split("else:", 1)[0]
    assert "available" in branch, "the link accepts a CA that is not built"
    assert "status_code=400" in branch


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        # SystemExit is a BaseException and this worker raises it as its fatal path —
        # `except Exception` would let one end the run mid-file, which reads as a pass.
        except (Exception, SystemExit) as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
