"""The cloud episode — the fourth control surface, and the first one that costs money.

The agent cell now demonstrates four credentials, and the arc is still the point:

  * the **PAT** is revocable — pull it and the loop stops mid-poll;
  * the **cluster token** is gated at retrieval — a person decides, and once released it
    lives out its TTL;
  * a **certificate** is neither: nothing on that path checks a CRL, so it stops when it
    expires and not when somebody takes it away;
  * a **cloud credential** did not exist until the worker asked, and on AWS **nothing can
    shorten its life** — STS will not withdraw a credential it has already signed.

This episode is different from its three siblings in ways that each have a failure mode
leaving the demo *looking* right, which is what these tests are for:

  * **It MINTS rather than retrieves, and the mint is billed.** Exactly one `generate`
    per run, and nothing retries it. A change that minted twice would be correct-looking
    and twice the price.
  * **A hand-rolled SigV4 signature that is wrong fails as HTTP 403** — which is exactly
    what a successful refusal looks like. Three defences are pinned here: the allow beat
    runs first, the deny beat requires a *specific* error code, and the one part of the
    signer with a published AWS test vector is checked against it.
  * **The credential values must never reach a printed string.** An AWS secret access key
    is forty unmarked characters, so `scrub` cannot catch one — the defence is structural
    and a test on captured stdout is what makes it real.
  * **The closing beat is a real wait, never a faked clock.** `docs/workload-lab/cloud.md`
    says a play that faked it "would prove it can print a failure message, not that the
    credential died"; the same rule applies one level over.
  * **The worker's WC client must not drift from the dashboard's.** It restates
    `parse_generated`, the path grammar and `_REVOCABLE_CLOUDS` rather than importing
    them, because it takes no dashboard dependency — so the restatements are pinned
    against the originals here.

Runs under pytest, or standalone:
    python tests/test_agentcell_cloud_episode.py
"""
import argparse
import contextlib
import http.server
import importlib.util
import io
import json
import os
import re
import sys
import threading
import urllib.parse
from datetime import timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-agentcell-cloud")

_WORKER = os.path.join(_ROOT, "examples", "playbooks", "agent", "files", "mcp_agent.py")

from web_dashboard.services import workload_cloud_service as WCS  # noqa: E402
from web_dashboard.services import workload_credentials_service as WLC  # noqa: E402

# A credential-shaped set of values, so "did any of these reach stdout" is answerable.
_AWS_VALUES = {"access_key_id": "ASIAIOSFODNN7EXAMPLE",
               "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
               "session_token": "FwoGZXIvYXdzEB-the-session-token"}
_AZURE_VALUES = {"client_id": "11111111-2222-3333-4444-555555555555",
                 "client_secret": "the-service-principal-secret",
                 "tenant_id": "99999999-8888-7777-6666-555555555555",
                 "key_id": "kid-1"}


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


@contextlib.contextmanager
def _serve_wlc(*, payload=None, status=200, body=b'{"error":"nope"}'):
    """``(base, seen)`` — a stand-in Workload Credentials and what reached it.

    A CONTEXT MANAGER, because the version that just returned the base URL leaked a
    listening socket and a thread per test. Thirty of those, plus the rest of the suite
    running alongside, produced a `WinError 10053` mid-request on a test about the WC
    path grammar — which reads as that path being wrong rather than as socket
    exhaustion. A flake that accuses the wrong thing is worse than a slow teardown.
    """
    seen = []

    class _H(http.server.BaseHTTPRequestHandler):
        def _send(self, code, blob=b"", ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            if blob:
                self.wfile.write(blob)

        def _log(self, method):
            parsed = urllib.parse.urlparse(self.path)
            seen.append({"method": method, "path": parsed.path,
                         "query": urllib.parse.parse_qs(parsed.query),
                         "auth": self.headers.get("Authorization", ""),
                         "service": self.headers.get("X-BT-Service-Name", ""),
                         "api_version": self.headers.get("bt-secrets-api-version", "")})

        def do_POST(self):
            self._log("POST")
            if status != 200:
                return self._send(status, body, "application/json")
            return self._send(200, json.dumps(payload or {}).encode())

        def do_DELETE(self):
            self._log("DELETE")
            if status != 200:
                return self._send(status, body, "application/json")
            return self._send(204)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _cloud_args(**over):
    """The namespace `run_cloud_episode` reads, with every required field filled."""
    base = dict(cloud_dynamic_name="ci-aws", cloud_dynamic_folder="",
                wlc_base_url="https://wc.example", wlc_site_id="SITE",
                wlc_service_name="mcp-agent", wlc_resource="api://wc",
                wlc_folder="", wlc_client_id="", identity_platform="auto",
                identity_token_file="", spiffe_socket="unix:///dev/null",
                cloud_deny_probe="auto", cloud_scope="sub-1",
                cloud_region="us-east-1", cloud_end_with="expiry",
                cloud_max_wait=4200, cloud_insecure=False, prove_ending=True)
    base.update(over)
    return argparse.Namespace(**base)


def _stub_episode(m, *, minted=None, probes=None, revoke=(True, "released")):
    """Replace everything that would leave the machine. Returns a call log.

    The probes are a LIST, consumed in order: the episode runs one before the ending and
    one after, and the difference between them is the whole demonstration.
    """
    log = {"generate": 0, "probe": [], "revoke": 0}
    minted = minted or {"values": dict(_AWS_VALUES), "lease_id": "lease-1",
                        "expires_at": "2020-01-01T00:00:00Z",
                        "expires_epoch": 1577836800.0, "cloud": "aws"}
    queue = list(probes or [])

    def _generate(**kw):
        log["generate"] += 1
        return minted

    def _probe(args, mint):
        log["probe"].append(args.cloud_deny_probe)
        return queue.pop(0) if queue else {"cloud": mint["cloud"], "authenticated": True,
                                           "proved": True, "deny_probe": "none"}

    def _revoke(**kw):
        log["revoke"] += 1
        return revoke

    m.fetch_spiffe_id = lambda *a, **k: "spiffe://weaverlab.test/agent/mcp-reader"
    m.fetch_identity_token = lambda *a, **k: "identity-token"
    m.generate_wlc_credential = _generate
    m._cloud_probe = _probe
    m.revoke_wlc_lease = _revoke
    return log


def _run(m, args):
    """``(exit_code, stdout)``."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = m.run_cloud_episode(args)
    return code, buf.getvalue()


_DEAD = {"cloud": "aws", "authenticated": False, "allow_status": 403,
         "allow_code": "ExpiredToken", "proved": False, "deny_probe": "none"}
_ALIVE = {"cloud": "aws", "authenticated": True, "proved": True,
          "deny_probe": "iam-list-users", "deny_code": "AccessDenied",
          "identity": "arn:aws:sts::1:assumed-role/ci/x", "says": "scoped"}


# ── the WC dynamic client, against the dashboard's own grammar ───────────────

def test_the_generate_path_matches_the_providers_grammar():
    """The WHOLE path, equal to what the dashboard builds — not two substrings of it.

    A substring check is how `read_wlc_secret` shipped reading `/secrets/{name}` where
    the live API answers `/secrets/static/{name}`, under a docstring saying the two could
    not disagree. The dynamic path gets the stronger check from the start.
    """
    m = _worker()
    with _serve_wlc(payload={"secret": {"accessKeyId": "ASIAIOSFODNN7EXAMPLE",
                                        "secretAccessKey": "s",
                                        "sessionToken": "t"}}) as (base, seen):
        m.generate_wlc_credential(base_url=base, site_id="SITE", service_name="svc",
                                  dynamic_name="ci-aws", identity_token="tok")
    assert seen[0]["path"] == WLC.build_secrets_path("SITE", "/dynamic/ci-aws/generate"), (
        f"the worker posted to {seen[0]['path']!r}, the dashboard builds "
        f"{WLC.build_secrets_path('SITE', '/dynamic/ci-aws/generate')!r}")
    assert seen[0]["method"] == "POST", "generate is a POST; a GET would not mint"


def test_the_generate_call_names_its_workload_identity():
    """Without X-BT-Service-Name the platform holds a valid token and no statement of
    which registered Workload Identity it is meant to satisfy. Without the version header
    it fails looking like an auth problem."""
    m = _worker()
    with _serve_wlc(payload={"secret": {"clientId": "c", "clientSecret": "s",
                                        "tenantId": "t"}}) as (base, seen):
        m.generate_wlc_credential(base_url=base, site_id="SITE",
                                  service_name="mcp-agent", dynamic_name="ci-azure",
                                  identity_token="the-token", folder="lab")
    got = seen[0]
    assert got["service"] == "mcp-agent"
    assert got["api_version"] == m.WLC_API_VERSION
    assert got["auth"] == "Bearer the-token", \
        "the mint did not present the machine's identity token"
    assert got["query"].get("folder") == ["lab"], "the folder was dropped"


def test_the_worker_and_the_dashboard_agree_on_a_generate_payload():
    """The worker MIRRORS `parse_generated` rather than importing it, because it takes no
    dashboard dependency. A mirror nobody checks is a guess, so both run the same table:
    camelCase, PascalCase, and the credential at the response root."""
    m = _worker()
    fixtures = [
        {"secret": {"accessKeyId": "ASIAIOSFODNN7EXAMPLE", "secretAccessKey": "s",
                    "sessionToken": "t", "leaseId": "L", "expiration": "2030-01-01T00:00:00Z"}},
        {"secret": {"AccessKeyId": "ASIAIOSFODNN7EXAMPLE", "SecretAccessKey": "s",
                    "SessionToken": "t"}, "leaseId": "root-lease"},
        {"accessKeyId": "ASIAIOSFODNN7EXAMPLE", "secretAccessKey": "s",
         "sessionToken": "t"},
        {"secret": {"clientId": "c", "clientSecret": "s", "tenantId": "t",
                    "keyId": "k", "Expiration": "2030-01-01T00:00:00+00:00"}},
        {"secret": {"ClientId": "c", "ClientSecret": "s", "TenantId": "t"}},
    ]
    for payload in fixtures:
        mine = m.parse_generated_payload(payload)
        theirs = WLC.parse_generated(payload)
        assert mine["values"] == theirs["values"], \
            f"the two parsers disagree about the values in {sorted(payload)}"
        assert mine["lease_id"] == theirs["lease_id"], \
            f"the two parsers disagree about the lease id in {sorted(payload)}"
        # The dashboard keeps a naive-UTC datetime to match its own columns; the worker
        # keeps an epoch because all it ever does is wait. Same instant either way.
        expected = theirs["expires_at"]
        if expected is None:
            assert mine["expires_epoch"] == 0.0, \
                f"the worker read an expiry the dashboard could not, in {sorted(payload)}"
        else:
            assert abs(mine["expires_epoch"]
                       - expected.replace(tzinfo=timezone.utc).timestamp()) < 1, \
                f"the two parsers disagree about the expiry in {sorted(payload)}"


def test_a_payload_with_no_credential_fields_names_keys_not_values():
    """The refusal must be safe to print. Naming the values would put a credential in a
    message whose whole job is to reach a log."""
    m = _worker()
    try:
        m.parse_generated_payload({"secret": {"surprise": "super-secret-value"}})
    except SystemExit as exc:
        assert "surprise" in str(exc)
        assert "super-secret-value" not in str(exc), \
            "the refusal quoted the value it could not recognise"
    else:
        raise AssertionError("an unrecognised payload was accepted")


def test_the_cloud_is_derived_from_the_payload_not_a_flag():
    """A flag could disagree with the payload, and signing an Azure secret as an AWS key
    fails as SignatureDoesNotMatch — the one failure this episode must never read as a
    refusal."""
    m = _worker()
    aws = m.parse_generated_payload({"secret": {"accessKeyId": "A", "secretAccessKey": "s",
                                                "sessionToken": "t"}})
    azure = m.parse_generated_payload({"secret": {"clientId": "c", "clientSecret": "s",
                                                  "tenantId": "t"}})
    assert aws["cloud"] == "aws" and azure["cloud"] == "azure"
    assert set(m._CREDENTIAL_SHAPE) == set(WCS._CREDENTIAL_SHAPE), \
        "the worker and the dashboard disagree about what a mint returns per cloud"


def test_the_worker_and_the_dashboard_agree_on_which_clouds_are_revocable():
    """Restated rather than imported, so it has to be pinned. Getting this wrong means
    offering a release on AWS, which the provider refuses — and the episode would report
    a withdrawal that never happened."""
    m = _worker()
    assert m._REVOCABLE_CLOUDS == WCS._REVOCABLE_CLOUDS
    assert m.cloud_revocable("azure") and not m.cloud_revocable("aws")


def test_revoke_does_not_swallow_a_provider_refusal():
    """The deliberate divergence from `workload_credentials_service.revoke_lease`, which
    swallows `lease_not_revocable` because its callers "revoke unconditionally and let
    the provider decide". Here the refusal IS the finding."""
    m = _worker()
    with _serve_wlc(status=400, body=b'{"error":"lease_not_revocable"}') as (base, _):
        released, detail = m.revoke_wlc_lease(
            base_url=base, site_id="S", service_name="svc",
            lease_id="L1", identity_token="tok")
    assert released is False, "a refused revoke was reported as a release"
    assert "not_revocable" in detail or "will not withdraw" in detail, \
        f"the detail does not say why the release failed: {detail!r}"


# ── the signer ───────────────────────────────────────────────────────────────

def test_the_signing_key_matches_aws_published_vector():
    """The one part of a hand-rolled SigV4 with a published vector. If the four-step
    derivation drifts, every probe returns 403 SignatureDoesNotMatch — which on this
    path is indistinguishable by status alone from the refusal being demonstrated."""
    m = _worker()
    got = m._sigv4_signing_key("wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
                               "20120215", "us-east-1", "iam").hex()
    assert got == "f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d", \
        f"the derived signing key is {got}, not AWS's documented value"


def test_the_canonical_request_is_the_documented_shape():
    """Asserted as exact text rather than through the signature it produces: a canonical
    request that is subtly wrong signs perfectly and is rejected as a 403."""
    m = _worker()
    text, signed = m._sigv4_canonical(method="POST", host="iam.amazonaws.com",
                                      stamp="20150830T123600Z",
                                      body=b"Action=ListUsers&Version=2010-05-08")
    lines = text.split("\n")
    assert lines[0] == "POST" and lines[1] == "/" and lines[2] == "", \
        "method, canonical URI and an empty query string come first"
    assert lines[3] == "content-type:application/x-www-form-urlencoded; charset=utf-8"
    assert lines[4] == "host:iam.amazonaws.com"
    assert lines[5] == "x-amz-date:20150830T123600Z"
    assert lines[6] == "", "the canonical headers block ends with a blank line"
    assert lines[7] == signed == "content-type;host;x-amz-date"
    assert re.fullmatch(r"[0-9a-f]{64}", lines[8]), "the payload hash is missing"


def test_the_session_token_is_inside_the_signature():
    """A dynamic-secret credential is always an assumed-role triple, so the security
    token is not optional. Sent beside the signature rather than inside it, every call
    fails as SignatureDoesNotMatch."""
    m = _worker()
    text, signed = m._sigv4_canonical(method="POST", host="sts.amazonaws.com",
                                      stamp="20150830T123600Z", body=b"x",
                                      session_token="TOK")
    assert signed == "content-type;host;x-amz-date;x-amz-security-token"
    assert "x-amz-security-token:TOK" in text
    headers = m._sigv4_headers(method="POST", host="sts.amazonaws.com",
                               region="us-east-1", service="sts", body=b"x",
                               values=dict(_AWS_VALUES))
    assert headers["X-Amz-Security-Token"] == _AWS_VALUES["session_token"]
    assert "x-amz-security-token" in headers["Authorization"]


# ── the episode ──────────────────────────────────────────────────────────────

def test_exactly_one_issuance_per_episode():
    """WC bills per issuance. This is the first path in the cell that costs money when it
    runs, and a second mint is a correct-looking doubling of the price."""
    m = _worker()
    log = _stub_episode(m, probes=[_ALIVE, _DEAD])
    code, out = _run(m, _cloud_args())
    assert code == 0, out
    assert log["generate"] == 1, f"{log['generate']} mints for one episode"
    assert "ONE issuance" in out, "the episode does not say it was billed"


def test_the_lease_id_is_never_printed():
    """This dashboard's own position, not a scanner's.

    `workload_credentials_service.generate` states it: *"a lease id is a correlation
    handle to a LIVE credential. The lease id belongs in the lease row and in Workload
    Credentials' own audit log"* — which is why it logs the request and not the result.
    The worker's stdout is a wider sink than that comment was written about: an operator
    tails it, screenshots it in a demo and pastes it into a ticket.

    What the line has to carry instead is whether a lease id came back at all, because
    without one the issuance can be neither revoked nor inspected.
    """
    m = _worker()
    _stub_episode(m, minted={"values": dict(_AWS_VALUES),
                             "lease_id": "lease-7f3c-correlates-to-a-live-credential",
                             "expires_at": "2020-01-01T00:00:00Z",
                             "expires_epoch": 1577836800.0, "cloud": "aws"},
                  probes=[_ALIVE, _DEAD])
    code, out = _run(m, _cloud_args())
    assert code == 0, out
    assert "lease-7f3c" not in out, "the lease id reached stdout"
    assert "revoked or inspected" in out, \
        "the line does not say whether this issuance can be revoked at all"

    m2 = _worker()
    _stub_episode(m2, minted={"values": dict(_AWS_VALUES), "lease_id": "",
                              "expires_at": "2020-01-01T00:00:00Z",
                              "expires_epoch": 1577836800.0, "cloud": "aws"},
                  probes=[_ALIVE, _DEAD])
    _, out2 = _run(m2, _cloud_args())
    assert "NO lease id" in out2, \
        "an issuance with no lease id does not say that its TTL is the only control"


def test_the_printed_expiry_is_the_one_the_wait_uses():
    """Rebuilt from `expires_epoch`, never echoed from the provider's raw string.

    If the two disagree — an unparseable format, a dropped timezone — printing the raw
    string shows the value that is NOT being waited on, so the wait looks wrong rather
    than the parse. Going through a float is also what keeps a string read out of the
    credential envelope away from a sink an operator pastes into tickets.
    """
    m = _worker()
    _stub_episode(m, minted={"values": dict(_AWS_VALUES), "lease_id": "L",
                             # A raw string the parser could not read, deliberately
                             # disagreeing with the epoch beside it.
                             "expires_at": "whenever-o'clock",
                             "expires_epoch": 1577836800.0, "cloud": "aws"},
                  probes=[_ALIVE, _DEAD])
    _, out = _run(m, _cloud_args())
    assert "whenever-o'clock" not in out, \
        "the provider's raw expiry was echoed instead of the parsed one"
    assert "2020-01-01" in out, "the printed expiry is not the epoch the wait counts down"


def test_no_credential_value_reaches_stdout():
    """`scrub` cannot save this one. An AWS secret access key is forty unmarked
    base64-ish characters with nothing to anchor a pattern on, so the rule is that values
    never reach a printed string — and only a test on the printed string enforces it."""
    m = _worker()
    _stub_episode(m, probes=[_ALIVE, _DEAD])
    _, out = _run(m, _cloud_args())
    for value in _AWS_VALUES.values():
        assert value not in out, "a credential value was printed"
    m2 = _worker()
    _stub_episode(m2, minted={"values": dict(_AZURE_VALUES), "lease_id": "L",
                              "expires_at": "2020-01-01T00:00:00Z",
                              "expires_epoch": 1577836800.0, "cloud": "azure"},
                  probes=[dict(_ALIVE, cloud="azure"), dict(_DEAD, cloud="azure")])
    _, out2 = _run(m2, _cloud_args(cloud_dynamic_name="ci-azure"))
    for value in _AZURE_VALUES.values():
        assert value not in out2, "an Azure credential value was printed"


def test_a_failed_allow_beat_proves_nothing():
    """The signer's safety net. A credential that never authenticated cannot be shown to
    stop working, so the run must not read as either a proof or a refusal that failed."""
    m = _worker()
    _stub_episode(m, probes=[dict(_DEAD, deny_probe="iam-list-users")])
    code, out = _run(m, _cloud_args())
    assert code == 1, f"expected 1 for a broken run, got {code}\n{out}"
    assert "proves nothing" in out


def test_a_deny_probe_that_succeeds_exits_4():
    """The outcome that would otherwise look like success: the credential is broader than
    the dynamic secret's definition was assumed to be."""
    m = _worker()
    loose = dict(_ALIVE, proved=False, deny_status=200, deny_code="")
    _stub_episode(m, probes=[loose, _DEAD])
    code, out = _run(m, _cloud_args())
    assert code == 4, f"a refusal that did not refuse exited {code}\n{out}"
    assert "THE REFUSAL DID NOT REFUSE" in out


def test_a_refusal_for_the_wrong_reason_is_not_proof():
    """A 403 carrying ExpiredToken or SignatureDoesNotMatch is a broken credential, not a
    scoped one. It is the failure mode a hand-rolled signer introduces, and reporting it
    as scope would make the demo argue for a signer bug."""
    m = _worker()
    broken = dict(_ALIVE, proved=False, broken=True, deny_status=403,
                  deny_code="SignatureDoesNotMatch")
    _stub_episode(m, probes=[broken, _DEAD])
    code, out = _run(m, _cloud_args())
    assert code == 4
    assert "WRONG REASON" in out and "SignatureDoesNotMatch" in out


def test_a_credential_that_outlives_its_ending_exits_4():
    """The other half of exit 4, and the same category: something that should have
    refused did not. Here it is the credential itself, after its expiry passed."""
    m = _worker()
    _stub_episode(m, probes=[_ALIVE, dict(_ALIVE, deny_probe="none")])
    code, out = _run(m, _cloud_args())
    assert code == 4, f"a credential that survived its expiry exited {code}\n{out}"
    assert "STILL WORKS" in out


def test_the_ending_reprobes_with_the_deny_probe_off():
    """Re-running the deny probe against a dead credential produces a refusal for the
    wrong reason that looks like the right one. Only the allow beat is repeated."""
    m = _worker()
    log = _stub_episode(m, probes=[_ALIVE, _DEAD])
    _run(m, _cloud_args())
    assert log["probe"] == ["auto", "none"], \
        f"the closing probe ran {log['probe'][-1]!r} rather than allow-only"


def test_the_expiry_wait_uses_the_providers_expiry():
    """AWS clamps a role-chained credential at an hour, so a wait computed from the
    requested TTL would re-probe a credential that is still alive and report impatience
    as a failure. And there is no injectable clock — a faked one would prove nothing."""
    m = _worker()
    code = _code(_WORKER)
    body = code.split("def _wait_out_the_lease(", 1)[1].split("\ndef ", 1)[0]
    assert "expires_epoch" in body, "the wait does not use the provider's own expiry"
    assert "time.time()" in body, "the wait does not consult the real clock"
    for faked in ("now=", "clock=", "_now_override"):
        assert faked not in body, f"the wait takes {faked!r} — a faked clock proves nothing"


def test_no_expiry_means_the_ending_cannot_be_proved():
    """A lease with no readable expiry has lost the only control it had. Reporting that
    as a pass would claim an ending nobody can observe."""
    m = _worker()
    _stub_episode(m, minted={"values": dict(_AWS_VALUES), "lease_id": "L",
                             "expires_at": "", "expires_epoch": 0.0, "cloud": "aws"},
                  probes=[_ALIVE])
    code, out = _run(m, _cloud_args())
    # 5, not 4. Nothing refused wrongly — the ending could not be watched. Reporting 4
    # would invent a scope finding about a credential that was never re-tested.
    assert code == 5, f"an unobservable ending exited {code}\n{out}"
    assert "no readable expiry" in out


def test_a_lease_longer_than_the_wait_is_refused_rather_than_slept_through():
    """The refusal names the remedy — a shorter TTL — because on a cloud whose credential
    cannot be revoked the TTL is the whole control."""
    m = _worker()
    import time as _t
    future = _t.time() + 7200
    _stub_episode(m, minted={"values": dict(_AWS_VALUES), "lease_id": "L",
                             "expires_at": "later", "expires_epoch": future,
                             "cloud": "aws"},
                  probes=[_ALIVE])
    code, out = _run(m, _cloud_args(cloud_max_wait=60))
    assert code == 5, f"a lease too long to wait out exited {code}\n{out}"
    assert "--cloud-max-wait" in out and "TTL" in out


def test_a_release_asked_for_on_aws_is_downgraded_loudly_not_silently():
    """Two things have to be true at once here, and only one of them is obvious.

    An AWS lease cannot be released — `revoke_lease` comes back `lease_not_revocable`,
    and a run that reported a release would tell a room a live credential had been
    withdrawn. So the release must not happen.

    But this is only discoverable AFTER the mint, because the cloud is derived from the
    payload rather than taken as a flag — and by then the issuance has been billed.
    Exiting would throw away a credential somebody paid for to punish a flag. So the
    episode ends with the expiry instead and **says so**; what it must never do is
    switch endings quietly.
    """
    m = _worker()
    log = _stub_episode(m, probes=[_ALIVE, _DEAD])
    code, out = _run(m, _cloud_args(cloud_end_with="release"))
    assert code == 0, out
    assert log["revoke"] == 0, "a release was attempted against AWS"
    assert "CANNOT be released" in out, "the downgrade is silent"
    assert "Ending with the expiry instead" in out, \
        "the run does not say which ending it actually used"
    assert "TTL is the only control" in out, \
        "the downgrade reads as a workaround rather than as the provider's limit"


def test_no_deny_probe_is_not_a_failed_refusal():
    """`--cloud-deny-probe none` is a supported choice: this worker cannot read the
    dynamic secret's role, so an operator may have no limit to assert.

    Exit 4 means "something that should have refused did not". Returning it for a probe
    that was never run would report that about nothing — and `cloud_probe_summary`
    already says, in the run's own output, that it proves authentication and not scope.
    """
    m = _worker()
    quiet = {"cloud": "aws", "authenticated": True, "proved": False,
             "deny_probe": "none", "identity": "arn:aws:sts::1:assumed-role/ci/x"}
    _stub_episode(m, probes=[quiet, _DEAD])
    code, out = _run(m, _cloud_args(cloud_deny_probe="none"))
    assert code == 0, f"a run with no refusal asked for exited {code}\n{out}"
    assert "ALL this run proves" in out, \
        "the run passed without saying it proved authentication and not scope"


def test_the_azure_release_reprobes_with_a_fresh_token():
    """Revoking the lease deletes the service principal's secret; it does NOT invalidate
    an ARM token already issued, which lives out its own hour. A re-probe on a cached
    token would prove the release did nothing — and look exactly like the truth."""
    m = _worker()
    azure = {"values": dict(_AZURE_VALUES), "lease_id": "L",
             "expires_at": "2030-01-01T00:00:00Z", "expires_epoch": 1893456000.0,
             "cloud": "azure"}
    log = _stub_episode(m, minted=azure,
                        probes=[dict(_ALIVE, cloud="azure"), dict(_DEAD, cloud="azure")])
    code, out = _run(m, _cloud_args(cloud_dynamic_name="ci-azure",
                                    cloud_end_with="release"))
    assert code == 0, out
    assert log["revoke"] == 1
    assert "not the one already issued" in out, \
        "the release beat does not state what it failed to kill"
    # The proof that it is fresh: `_entra_token` is called per probe and caches nothing.
    body = _code(_WORKER).split("def _entra_token(", 1)[1].split("\ndef ", 1)[0]
    assert "cache" not in body.lower(), "the Entra token is cached; the release beat lies"


def test_no_prove_ending_exits_5_and_names_the_flag():
    """A run that mints, proves the scope and stops prints a line indistinguishable from
    a full one — on a demo whose whole argument is that the credential ends. The same
    reasoning `approval_problem` encodes one level down."""
    m = _worker()
    _stub_episode(m, probes=[_ALIVE])
    code, out = _run(m, _cloud_args(prove_ending=False))
    assert code == 5, f"skipping the ending exited {code}\n{out}"
    assert "--no-prove-ending" in out
    assert "ONE issuance" in out, "a refused run was still billed and must say so"


def test_a_deny_probe_from_the_wrong_cloud_is_refused():
    """`--cloud-deny-probe` offers both clouds' probes, because argparse cannot know
    which cloud the mint will return. Neither probe function acts on the value beyond
    `none` — each runs its own cloud's call — so a mismatched choice would run one call
    and label it with the OTHER one's sentence, printing `scope proved … cannot read
    the Entra directory` about a run that tested `iam:ListUsers`. An assertion
    attributed to a probe that never ran is worse than no assertion at all.
    """
    m = _worker()
    aws = {"values": dict(_AWS_VALUES), "lease_id": "L", "expires_at": "x",
           "expires_epoch": 1.0, "cloud": "aws"}
    args = _cloud_args(cloud_deny_probe="graph-directory-read")
    try:
        m._cloud_probe(args, aws)
    except SystemExit as exc:
        assert "not an aws probe" in str(exc)
        assert "iam-list-users" in str(exc), "the refusal does not name the right probe"
    else:
        raise AssertionError("an Azure deny probe was accepted against an AWS mint")
    # The two that must still pass: the cloud's own probe, and `none`.
    for ok in ("iam-list-users", "none", "auto"):
        m._cloud_probe(_cloud_args(cloud_deny_probe=ok), aws)


def test_a_transport_failure_is_fatal_with_the_bill_named():
    """`k8s_probe`'s rule, and it matters more here. By the time a probe runs a
    credential has been minted and BILLED, so a `URLError` escaping as a traceback would
    end the process having spent money and never mentioned it."""
    m = _worker()
    # Port 1 on loopback refuses immediately — a transport failure, not an HTTP one.
    try:
        m._arm_get("http://127.0.0.1:1/subscriptions/x", "tok", timeout=2)
    except SystemExit as exc:
        assert "could not be reached" in str(exc)
        assert "billed" in str(exc), \
            "the fatal message does not say a credential was already paid for"
    else:
        raise AssertionError("an unreachable endpoint returned a result")


def test_the_billing_note_survives_a_probe_that_raises():
    """The `finally` is the only place the run says it was billed, so the first probe
    has to be INSIDE the try. It was not, and a transport failure there ended the
    process having charged the operator without telling them."""
    m = _worker()
    _stub_episode(m)

    def _boom(args, mint):
        raise SystemExit("[agent] FATAL: sts.amazonaws.com could not be reached")

    m._cloud_probe = _boom
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            m.run_cloud_episode(_cloud_args())
        except SystemExit:
            pass
    assert "ONE issuance" in buf.getvalue(), \
        "a probe that raised skipped the note saying the mint had been billed"


# -- the probes themselves, against HTTP rather than a hand-built result dict --

def test_the_aws_probe_reads_a_refusal_apart_from_a_broken_signature():
    """Every episode test above stubs `_cloud_probe` wholesale, so without this the
    branch logic here has no test at all — and inverting one would still leave the file
    green while reporting a broken credential as proof of scope."""
    m = _worker()
    calls = []

    def fake(*, host, service, region, action, version, values, verify=True,
             timeout=15):
        calls.append(action)
        return _AWS_RESPONSES.pop(0)

    m._aws_query_call = fake
    ok = {"status": 200, "code": "", "arn": "arn:aws:sts::1:assumed-role/ci/x",
          "body": ""}

    # Refused for the right reason.
    _AWS_RESPONSES[:] = [ok, {"status": 403, "code": "AccessDenied", "arn": "",
                              "body": ""}]
    r = m.aws_cloud_probe(values=dict(_AWS_VALUES), deny_probe="iam-list-users")
    assert r["proved"] and r["authenticated"] and not r.get("broken")
    assert calls == ["GetCallerIdentity", "ListUsers"], \
        "the allowed call did not run first — a broken signer would read as scope"

    # Refused for the WRONG reason: a bad signature is a 403 too.
    _AWS_RESPONSES[:] = [ok, {"status": 403, "code": "SignatureDoesNotMatch", "arn": "",
                              "body": ""}]
    r = m.aws_cloud_probe(values=dict(_AWS_VALUES), deny_probe="iam-list-users")
    assert r.get("broken") and not r["proved"], \
        "a bad signature was reported as proof of scope"

    # Not refused at all.
    _AWS_RESPONSES[:] = [ok, {"status": 200, "code": "", "arn": "", "body": ""}]
    assert not m.aws_cloud_probe(values=dict(_AWS_VALUES),
                                 deny_probe="iam-list-users")["proved"]

    # The allowed call itself failed: nothing below it means anything.
    _AWS_RESPONSES[:] = [{"status": 403, "code": "ExpiredToken", "arn": "", "body": ""}]
    r = m.aws_cloud_probe(values=dict(_AWS_VALUES), deny_probe="iam-list-users")
    assert not r.get("authenticated") and not r["proved"]
    assert "deny_status" not in r, "the deny probe ran on a credential that never worked"


def test_the_azure_probe_tells_a_missing_role_from_a_bad_token():
    """A Graph 403 with `Authorization_RequestDenied` is the role being absent, which is
    the refusal. A 401 is the token being wrong, which proves nothing — and an Entra
    that will not issue a Graph token at all is neither."""
    m = _worker()
    tokens, gets = [], []

    def fake_token(*, values, scope, verify=True, timeout=15):
        tokens.append(scope)
        return _AZ_TOKENS.pop(0)

    def fake_get(url, token, verify=True, timeout=15):
        gets.append(url)
        return _AZ_GETS.pop(0)

    m._entra_token = fake_token
    m._arm_get = fake_get
    sub = {"status": 200, "code": "", "data": {"displayName": "lab-sub"}, "body": ""}

    _AZ_TOKENS[:] = [("arm-tok", ""), ("graph-tok", "")]
    _AZ_GETS[:] = [sub, {"status": 403, "code": "Authorization_RequestDenied",
                         "data": {}, "body": ""}]
    r = m.azure_cloud_probe(values=dict(_AZURE_VALUES), scope="sub-1",
                            deny_probe="graph-directory-read")
    assert r["proved"] and r["identity"] == "lab-sub"
    assert tokens == ["https://management.azure.com/.default",
                      "https://graph.microsoft.com/.default"], \
        "the Graph beat reused the ARM token rather than asking for its own"

    _AZ_TOKENS[:] = [("arm-tok", ""), ("graph-tok", "")]
    _AZ_GETS[:] = [sub, {"status": 401, "code": "InvalidAuthenticationToken",
                         "data": {}, "body": ""}]
    r = m.azure_cloud_probe(values=dict(_AZURE_VALUES), scope="sub-1",
                            deny_probe="graph-directory-read")
    assert r.get("broken") and not r["proved"], \
        "a 401 was read as authorisation; it says the token is wrong, not the scope"

    _AZ_TOKENS[:] = [("", "invalid_client")]
    r = m.azure_cloud_probe(values=dict(_AZURE_VALUES), scope="sub-1",
                            deny_probe="graph-directory-read")
    assert not r.get("authenticated") and r["allow_code"] == "invalid_client"


_AWS_RESPONSES: list = []
_AZ_TOKENS: list = []
_AZ_GETS: list = []


def test_deny_probe_none_says_it_proves_only_authentication():
    """`--cloud-deny-probe none` is allowed, because this worker cannot read the dynamic
    secret's role and an operator may have no limit to assert. What it must not do is
    print a line that reads like scope."""
    m = _worker()
    summary = m.cloud_probe_summary({"cloud": "aws", "authenticated": True,
                                     "deny_probe": "none", "identity": "arn:x"})
    assert "ALL this run proves" in summary
    assert "scope proved" not in summary


def test_exit_3_is_never_returned():
    """There is no approval on this path — WC's generate has no gate — so a 3 here would
    name a human who was never asked. The other two episodes return it and this one must
    not, which is why it is asserted rather than assumed."""
    body = _code(_WORKER).split("def run_cloud_episode(", 1)[1].split("\ndef ", 1)[0]
    assert "return 3" not in body, "the cloud episode returns 3, which claims an approval"
    doc = _read(_WORKER).split("def run_cloud_episode(", 1)[1].split('"""')[1]
    assert "never returned" in doc and "never asked" in doc, \
        "the docstring does not say that 3 is absent, or why"


def test_the_episode_never_reaches_password_safe():
    """The one episode with no vault in the chain, and the difference is the argument:
    WC mints here rather than bootstrapping a retrieval of something already held."""
    body = _code(_WORKER).split("def run_cloud_episode(", 1)[1].split("\ndef ", 1)[0]
    for ps in ("password_safe", "ps_api_url", "_open_request", "_checkin"):
        assert ps not in body, f"the cloud episode reaches {ps}"


def test_the_flags_are_wired_and_selftest_reports_them():
    """The wiring test every episode here has, so a flag family that parses but never
    dispatches is caught without a network."""
    import subprocess
    r = subprocess.run([sys.executable, _WORKER, "--selftest"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout.split("selftest ok:", 1)[1])
    for key in ("cloud_episode", "cloud_end_with", "cloud_deny_probe"):
        assert key in payload, f"--selftest does not report {key}"
    assert payload["cloud_end_with"] == "expiry", \
        "the default ending is not the one that exists on both clouds"


def test_the_refusal_names_every_missing_flag():
    """Each of these is NON-SECRET, so there is no reason to be vague about which is
    absent — the rule the `wlc` token source already follows."""
    import subprocess
    r = subprocess.run([sys.executable, _WORKER, "--cloud-episode"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    out = r.stdout + r.stderr
    for flag in ("--cloud-dynamic-name", "--wlc-base-url", "--wlc-site-id",
                 "--wlc-service-name", "--wlc-resource"):
        assert flag in out, f"the refusal does not name {flag}"


def test_the_worker_takes_no_cloud_sdk_dependency():
    """The reason the signer is hand-rolled at all: this runs on somebody else's VM, and
    the AWS CLI is a ~60 MB install on every agent host. The shipped cloud PLAY may use
    it because it runs on the dashboard's runner image, which is a different machine."""
    code = _code(_WORKER)
    for dep in ("import boto3", "import botocore", "from azure", "import azure",
                "google.cloud"):
        assert dep not in code, f"the worker imports {dep!r}"
    for shell in ('"aws"', "'aws'", '"az"', "'az'"):
        assert f"[{shell}" not in code, f"the worker shells out to {shell}"


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
