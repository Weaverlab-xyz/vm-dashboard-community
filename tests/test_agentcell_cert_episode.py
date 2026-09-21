"""The certificate episode — the third control surface, and the one that does not stop.

The agent cell now demonstrates three credentials, and the arc is the point:

  * the **PAT** is revocable — pull it and the loop stops mid-poll;
  * the **cluster token** is gated at retrieval — a person decides, and once released it
    lives out its TTL;
  * a **certificate** is neither. `docs/workload-lab/certificates.md`:
    *"No revocation checking. The plugin consults neither CRLs nor OCSP. Short lifetimes
    are the mitigation, and that is a deliberate design position."*

So the demo's closing beat is uncomfortable on purpose — revoke the certificate and the
agent keeps working — and these tests exist to stop that honesty being quietly lost.

What they pin, each of which has a way of failing that leaves the demo *looking* right:

  * **Both halves, and neither usable alone.** The passphrase is a managed-account
    credential; the bundle is a Secrets Safe **file** secret, downloaded over the API's
    own file endpoint. A change that fetched one and faked the other would still print
    a success line.
  * **The bundle arrives as BYTES.** Not through ps-cli, which cannot carry them:
    every route it offers hands back `response.text`, so a PEM bundle survives and a
    `.pfx` is *corrupted rather than refused*. The worker calls
    `GET Secrets-Safe/Secrets/{id}/file/download` on the session the passphrase already
    opened, and a real PKCS#12 is compared byte for byte here.
  * **The bundle, the certificate and the key touch disk in exactly one guarded place**,
    which the EPISODE owns and removes — on the failure path too. The bundle arrives as a
    file, so this is not avoidable; it is bounded instead, and these tests are what keeps
    the bound real.
  * **The CN assertion is real.** An endpoint that answers 200 without seeing the
    certificate must not read as proof.

Runs under pytest, or standalone:
    python tests/test_agentcell_cert_episode.py
"""
import http.server
import importlib.util
import json
import os
import re
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import types
import urllib.parse

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-agentcell-cert")

_WORKER = os.path.join(_ROOT, "examples", "playbooks", "agent", "files", "mcp_agent.py")
_API = os.path.join(_ROOT, "web_dashboard", "api", "agentcell.py")
_DOC = os.path.join(_ROOT, "docs", "profiles", "demo", "agent-demo-cell.md")

from web_dashboard.services import agentcell_service as A  # noqa: E402,F401

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


def _strays(before):
    """Working directories the code under test left behind in the system temp dir."""
    return [n for n in set(os.listdir(tempfile.gettempdir())) - before
            if n.startswith("mcp-agent-cert-")]


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
    with open(p("bundle.pfx"), "rb") as fh:
        blob = fh.read()
    _PKI.update({"dir": d, "bundle_path": p("bundle.pfx"), "bundle": blob,
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
    # Pinned rather than left to the build's default, which is what `runners/agent/`
    # does in five places for the same reason: PROTOCOL_TLS_SERVER *permits* TLSv1 and
    # TLSv1.1 by contract even where the local OpenSSL happens to start at 1.2, so a
    # static reading of this line is right to call it insecure. The lab endpoint this
    # stands in for should not be reachable over either.
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(pki["srv_crt"], pki["srv_key"])
    ctx.load_verify_locations(pki["ca"])
    ctx.verify_mode = ssl.CERT_REQUIRED
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"https://127.0.0.1:{srv.server_address[1]}/"


# A Secrets Safe that serves the two endpoints the bundle comes down ---------------------
#
# Both halves of this identity arrive over the SAME session, so the stub is an HTTP server
# rather than a fake binary: `Secrets-Safe/Secrets` resolves the reference to an id, and
# `Secrets-Safe/Secrets/{id}/file/download` serves the bytes. It records what it was asked
# for, so the assertions below are made against what the worker actually sent.

def _serve_secrets(*, payload=None, entries=None, lookup_status=200,
                   download_status=200):
    """``(base, seen)`` — a stand-in Secrets Safe and the log of what reached it."""
    seen = []
    blob = _pki()["bundle"] if payload is None else payload

    class _H(http.server.BaseHTTPRequestHandler):
        def _send(self, status, body=b"", ctype="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            seen.append({"path": parsed.path,
                         "query": urllib.parse.parse_qs(parsed.query),
                         "auth": self.headers.get("Authorization", ""),
                         "accept": self.headers.get("Accept", "")})
            if parsed.path.endswith("/file/download"):
                if download_status != 200:
                    return self._send(download_status, b"denied", "text/plain")
                return self._send(200, blob, "application/octet-stream")
            if lookup_status != 200:
                return self._send(lookup_status, b"no", "text/plain")
            rows = [{"Id": "9f1c-the-guid", "Title": "bundle"}] \
                if entries is None else entries
            return self._send(200, json.dumps(rows).encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}/", seen


_BEARER = "Bearer the-session-this-episode-already-opened"


def _fetch(m, dest_dir, base, title="cert/demo-system/svc-deploy-pipeline"):
    return m.secrets_safe_file(title, dest_dir=dest_dir, base=base,
                               headers={"Accept": "application/json",
                                        "Authorization": _BEARER})


def _cert_args(**over):
    """The namespace `run_cert_episode` reads, with every required field filled."""
    base = dict(cert_endpoint="https://127.0.0.1:1/", cert_cn=_CN,
                cert_bundle_title="cert/lab/svc", cert_account_id=7, cert_system_id=3,
                cert_duration=15, cert_max_wait=60, cert_insecure=True,
                require_approval=True, spiffe_socket="/tmp/none.sock",
                identity_platform="auto", identity_token_file="",
                wlc_base_url="https://wlc.example", wlc_site_id="s1",
                wlc_service_name="svc", wlc_resource="api://wlc", wlc_folder="",
                wlc_client_id="", ps_client_id_secret="ps-id",
                ps_client_secret_secret="ps-secret",
                ps_api_url="https://ps.example/BeyondTrust/api/public/v3")
    base.update(over)
    return types.SimpleNamespace(**base)


def _episode(m, *, download, polls=1):
    """Everything around the certificate work, stubbed — so the test observes the part
    this file is about: which directory the episode opens, and whether it goes."""
    m.fetch_spiffe_id = lambda _s: "spiffe://demo/agent"
    m.fetch_identity_token = lambda *a, **k: "identity-token"
    m.read_wlc_secret = lambda **k: "pair-" + k["secret_name"]
    m.password_safe_episode = lambda **k: (_PASS, "https://ps", {}, 42, polls)
    m._checkin = lambda *a, **k: None
    m.secrets_safe_file = download


# -- the probe proves the IDENTITY, not that a request succeeded ---------------

def test_the_probe_opens_a_real_bundle_and_completes_a_real_handshake():
    if not _have_openssl():
        print("   (skipped: no openssl)")
        return
    m = _worker()
    with tempfile.TemporaryDirectory() as work:
        r = m.cert_mtls_probe(endpoint=_serve_mtls(), bundle_path=_pki()["bundle_path"],
                              passphrase=_PASS, expect_cn=_CN, work_dir=work,
                              verify=False)
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
    with tempfile.TemporaryDirectory() as work:
        r = m.cert_mtls_probe(endpoint=_serve_mtls(blind),
                              bundle_path=_pki()["bundle_path"], passphrase=_PASS,
                              expect_cn=_CN, work_dir=work, verify=False)
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
        with tempfile.TemporaryDirectory() as work:
            m.cert_mtls_probe(endpoint=_serve_mtls(), bundle_path=_pki()["bundle_path"],
                              passphrase="wrong", expect_cn=_CN, work_dir=work,
                              verify=False)
    except SystemExit as exc:
        assert "two halves of one identity" in str(exc)
    else:
        raise AssertionError("a wrong passphrase was accepted")


# -- one guarded directory, owned by the episode -------------------------------

def test_the_probe_writes_only_into_the_directory_it_is_given():
    """The probe no longer makes its own temporary directory — it is handed the one the
    episode already opened for the bundle. That is only an improvement if it actually
    stays inside it, so this watches the system temp dir while it runs."""
    if not _have_openssl():
        print("   (skipped: no openssl)")
        return
    m = _worker()
    before = set(os.listdir(tempfile.gettempdir()))
    with tempfile.TemporaryDirectory(prefix="probe-scope-") as work:
        m.cert_mtls_probe(endpoint=_serve_mtls(), bundle_path=_pki()["bundle_path"],
                          passphrase=_PASS, expect_cn=_CN, work_dir=work, verify=False)
        wrote = sorted(os.listdir(work))
        mode = stat.S_IMODE(os.stat(os.path.join(work, "client.key")).st_mode)
    assert wrote == ["client.crt", "client.key"], \
        f"the probe wrote something unexpected into the episode's directory: {wrote}"
    assert mode == 0o600, f"the private key is mode {mode:o}, not 0600"
    assert not _strays(before), "the probe opened a directory of its own after all"


def test_the_episode_owns_the_directory_and_it_does_not_outlive_the_episode():
    """The bundle is a FILE secret: the worker writes it, openssl opens it, and both live in
    one 0700 directory the episode opens and closes. Observed end to end rather than read
    out of the source, because this is where a credential touches disk."""
    if not _have_openssl():
        print("   (skipped: no openssl)")
        return
    m = _worker()
    seen = {}

    def _download(title, *, dest_dir, **kw):
        seen["dir"] = dest_dir
        seen["mode"] = stat.S_IMODE(os.stat(dest_dir).st_mode)
        dest = os.path.join(dest_dir, "bundle.pfx")
        with open(dest, "wb") as fh:
            fh.write(_pki()["bundle"])
        os.chmod(dest, 0o600)
        return dest

    _episode(m, download=_download)
    before = set(os.listdir(tempfile.gettempdir()))
    rc = m.run_cert_episode(_cert_args(cert_endpoint=_serve_mtls()))
    assert rc == 0, f"the episode did not prove the identity: {rc}"
    assert seen["mode"] == 0o700, \
        f"the bundle was downloaded into a {seen['mode']:o} directory"
    assert not os.path.exists(seen["dir"]), "the bundle outlived the episode"
    assert not _strays(before)


def test_the_directory_goes_even_when_the_download_fails():
    """A `finally` around the check-in is not enough — the cleanup has to cover the
    failure paths too, and the download is the one most likely to be hit in a lab."""
    m = _worker()
    seen = {}

    def _explode(title, *, dest_dir, **kw):
        seen["dir"] = dest_dir
        with open(os.path.join(dest_dir, "bundle.pfx"), "wb") as fh:
            fh.write(b"\x30partial")
        raise SystemExit("[agent] FATAL: Secrets Safe would not release the bundle")

    _episode(m, download=_explode)
    before = set(os.listdir(tempfile.gettempdir()))
    try:
        m.run_cert_episode(_cert_args())
    except SystemExit:
        pass
    else:
        raise AssertionError("a failed download did not stop the episode")
    assert not os.path.exists(seen["dir"]), \
        "a half-written bundle was left behind when the download failed"
    assert not _strays(before)


def test_the_key_is_written_private_and_the_passphrase_never_reaches_argv():
    code = _code(_WORKER)
    probe = code.split("def cert_mtls_probe(", 1)[1].split("\ndef ")[0]
    assert "0o600" in probe, "the private key is not written private"
    assert "TemporaryDirectory" not in probe, \
        "the probe opens a directory of its own again — the episode owns exactly one, " \
        "which is what makes 'the bundle touches disk in one guarded place' true"
    assert "env:PFXPASS" in probe, \
        "the passphrase is not passed to openssl through the environment"
    assert "-passin" in probe and "passphrase" not in probe.split("-passin")[1][:80], \
        "the passphrase looks like it is on openssl's command line — /proc/<pid>/cmdline "\
        "is world-readable"
    episode = code.split("def run_cert_episode(", 1)[1].split("\ndef ")[0]
    assert "TemporaryDirectory" in episode and "0o700" in episode, \
        "the episode does not open one private directory for the whole of it"


# -- Secrets Safe: the bytes, unmangled, on the session already open -----------
#
# The whole reason this path is not ps-cli. `docs/integrations/password-safe.md` and
# `services/secrets_backend_service._read_bt_file_secret` both establish it: the endpoint
# returns application/octet-stream faithfully, and every ps-cli route decodes the body to
# text before anyone sees it, so a PEM bundle survives and a .pfx is CORRUPTED RATHER THAN
# REFUSED. These tests exist because that failure is silent at the transport and loud
# three steps later, in a place that sends somebody to look at the wrong thing.

def test_the_bundle_arrives_byte_for_byte():
    """The headline, and the property ps-cli could not give. A real PKCS#12 is binary and
    is not valid UTF-8 — anything that decoded it on the way would corrupt it here."""
    if not _have_openssl():
        print("   (skipped: no openssl)")
        return
    m = _worker()
    base, _ = _serve_secrets()
    with tempfile.TemporaryDirectory() as d:
        path = _fetch(m, d, base)
        with open(path, "rb") as fh:
            got = fh.read()
    assert got == _pki()["bundle"], \
        f"the bundle changed in transit: {len(got)} bytes, expected " \
        f"{len(_pki()['bundle'])}"
    try:
        got.decode("utf-8")
    except UnicodeDecodeError:
        pass          # what we want: this payload could not have survived a text route
    else:
        raise AssertionError(
            "this PKCS#12 happens to be valid UTF-8, so it cannot prove the transport "
            "keeps bytes — regenerate the fixture rather than trusting this test")


def test_the_bundle_is_private_from_the_moment_it_exists():
    """Created 0600, not chmod-ed to 0600 afterwards: between the two there is a window
    where a credential sits on disk at the umask's discretion."""
    if not _have_openssl():
        print("   (skipped: no openssl)")
        return
    m = _worker()
    base, _ = _serve_secrets()
    with tempfile.TemporaryDirectory() as d:
        path = _fetch(m, d, base)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600, "the bundle is not private"
        assert os.path.dirname(path) == d, "the bundle landed outside the episode's dir"
    body = _code(_WORKER).split("def secrets_safe_file(", 1)[1].split("\ndef ")[0]
    assert "0o600" in body.split("os.open(")[1][:120], \
        "the bundle is not created 0600 — a chmod afterwards leaves a window"


def test_the_bundle_comes_down_the_session_already_open():
    """Secrets Safe is part of Password Safe. A second sign-in here would be a second
    dialect of the same API, which is how two callers drift apart."""
    if not _have_openssl():
        print("   (skipped: no openssl)")
        return
    m = _worker()
    base, seen = _serve_secrets()
    with tempfile.TemporaryDirectory() as d:
        _fetch(m, d, base)
    assert len(seen) == 2, f"expected a lookup then a download, got {len(seen)}"
    assert all(r["auth"] == _BEARER for r in seen), \
        "the bundle was fetched with something other than the episode's own session"
    assert not any("Auth/" in r["path"] for r in seen), \
        "the worker signed in again for the second half of one identity"
    assert seen[1]["path"].endswith("/file/download")
    assert "octet-stream" in seen[1]["accept"], \
        "the download does not ask for bytes"


def test_a_reference_with_folders_is_looked_up_as_a_path():
    if not _have_openssl():
        print("   (skipped: no openssl)")
        return
    m = _worker()
    base, seen = _serve_secrets()
    with tempfile.TemporaryDirectory() as d:
        _fetch(m, d, base, title="cert/demo-system/svc")
    q = seen[0]["query"]
    assert q.get("path") == ["cert/demo-system/svc"], \
        f"a foldered reference was not resolved as a path: {q}"
    assert q.get("separator") == ["/"], "the path lookup carries no separator"


def test_a_bare_reference_is_looked_up_as_a_title():
    if not _have_openssl():
        print("   (skipped: no openssl)")
        return
    m = _worker()
    base, seen = _serve_secrets()
    with tempfile.TemporaryDirectory() as d:
        _fetch(m, d, base, title="just-a-bundle")
    q = seen[0]["query"]
    assert q.get("title") == ["just-a-bundle"], \
        f"a bare reference was not resolved by title: {q}"
    assert "separator" not in q, "a title lookup should not carry a path separator"


def test_a_reference_that_resolves_to_nothing_says_which_lookup_it_tried():
    """Resolving by the wrong one of the two is the likeliest operator error, and an
    error that does not say which was used cannot be acted on."""
    m = _worker()
    base, _ = _serve_secrets(entries=[])
    with tempfile.TemporaryDirectory() as d:
        try:
            _fetch(m, d, base, title="cert/nope/nothing")
        except SystemExit as exc:
            assert "no secret" in str(exc) and "path" in str(exc), str(exc)
        else:
            raise AssertionError("an unresolvable reference was treated as a download")


def test_a_refused_download_names_the_permission_rather_than_the_bundle():
    """403 here is a grant, not a missing secret — the lookup already found it."""
    m = _worker()
    base, _ = _serve_secrets(download_status=403)
    with tempfile.TemporaryDirectory() as d:
        try:
            _fetch(m, d, base)
        except SystemExit as exc:
            assert "403" in str(exc) and "read on the folder" in str(exc), str(exc)
        else:
            raise AssertionError("a refused download was treated as a bundle")


def test_a_pem_payload_is_refused_and_says_why_it_is_the_wrong_shape():
    """PEM is a perfectly good file secret and a perfectly useless one here. Handing it
    to openssl makes it complain about the passphrase, which sends somebody to debug the
    wrong half of a two-half identity."""
    m = _worker()
    pem = b"-----BEGIN CERTIFICATE-----\nnope\n-----END CERTIFICATE-----\n"
    base, _ = _serve_secrets(payload=pem)
    with tempfile.TemporaryDirectory() as d:
        try:
            _fetch(m, d, base)
        except SystemExit as exc:
            assert "PKCS#12" in str(exc) and "looks like PEM" in str(exc), str(exc)
        else:
            raise AssertionError("a PEM was accepted as a PKCS#12 bundle")


def test_nothing_on_this_path_decodes_the_bytes():
    """The regression that would be invisible: `_request` decodes UTF-8, so routing the
    bundle through it corrupts a .pfx in exactly the way ps-cli does."""
    code = _code(_WORKER)
    body = code.split("def secrets_safe_file(", 1)[1].split("\ndef ")[0]
    assert "_get_bytes(" in body, "the bundle is fetched through a text helper"
    fetch = code.split("def _get_bytes(", 1)[1].split("\ndef ")[0]
    assert ".decode(" not in fetch, "_get_bytes decodes, which is the whole bug"
    assert "resp.read()" in fetch


def test_the_bundle_does_not_go_back_through_ps_cli():
    """It cannot. Every ps-cli route hands back `response.text`, so a .pfx is corrupted
    rather than refused — `secrets_backend_service._read_bt_file_secret` refuses binary
    payloads for this exact reason and names the endpoint as the way out. A later change
    that reached for the binary again would be re-introducing a known defect."""
    body = _code(_WORKER).split("def secrets_safe_file(", 1)[1].split("\ndef ")[0]
    for gone in ("ps-cli", "subprocess", "PSCLI"):
        assert gone not in body, \
            f"the bundle is back on {gone!r} — see docs/integrations/password-safe.md, " \
            "every ps-cli route decodes the body to text and corrupts a PKCS#12"


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


def test_the_refusal_happens_before_the_bundle_is_fetched():
    """Refusing after downloading the bundle would still have spent both halves — the
    point is that the ungated release stops the episode, not that it prints differently.
    """
    m = _worker()
    called = []

    def _never(title, *, dest_dir, **kw):
        called.append(title)
        raise AssertionError("the bundle was fetched despite an ungated release")

    _episode(m, download=_never, polls=0)
    assert m.run_cert_episode(_cert_args()) == 5
    assert not called


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
    assert body.index("password_safe_episode(") < body.index(
        "secrets_safe_file("), \
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


def test_the_page_says_where_the_bundle_lands():
    """The bundle is a file secret, so it reaches disk. Saying so on the page is the
    difference between a bounded exception and something somebody discovers."""
    doc = _read(_DOC)
    assert "0700" in doc and re.search(r"touch(es)? disk", doc), \
        "the page does not say that the bundle and key land in one guarded directory"


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
