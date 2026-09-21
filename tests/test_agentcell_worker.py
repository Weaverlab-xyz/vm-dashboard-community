"""The worker's contract: what it logs, what it holds, and how it ends.

The worker is the only part of this feature an audience actually looks at, and three of
its properties carry the whole demo. Each is easy to lose to a well-meaning edit:

  * **One line, both halves.** The SPIFFE ID it proved and the token it spent, together.
    Split them across two lines and the point — that these are two different things —
    stops being visible.
  * **The token is never logged in full.** A hint locates the row in Settings → API
    Tokens; the value would hand it to anyone watching the screen share.
  * **A refusal ends the process.** Exit 2 on 401, and a unit that does not restart on
    it. A worker that logged a warning and kept polling would turn the demo's closing
    beat into a scroll of failures.

Also pinned: the worker re-fetches its SVID rather than caching it, because "an attested
workload holds nothing" is the claim being made, and a cached ID would survive the
registration entry being deleted.

Runs under pytest, or standalone:
    python tests/test_agentcell_worker.py
"""
import os
import re
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-agentcell-worker")

_DIR = os.path.join(_ROOT, "examples", "playbooks", "agent")
_WORKER = os.path.join(_DIR, "files", "mcp_agent.py")
_INSTALL = os.path.join(_DIR, "agent-install.yml")
_ENTRY = os.path.join(_DIR, "agent-spiffe-entry.yml")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _code(path):
    """Source with docstrings and comments stripped — the worker explains its own design
    at length, so an absence check on the raw file finds the prose."""
    src = re.sub(r'"""[\s\S]*?"""', "", _read(path))
    return "\n".join(ln for ln in src.splitlines()
                      if ln.strip() and not ln.lstrip().startswith("#"))


def _yaml_code(path):
    """A playbook with its comment lines dropped.

    Same trap as _code, in YAML. These playbooks argue for their own settings in comments
    — "NOT Restart=always", "an Environment= line is world-readable" — so a raw scan finds
    the argument and reports it as the violation it is arguing against.
    """
    return "\n".join(ln for ln in _read(path).splitlines()
                      if ln.strip() and not ln.lstrip().startswith("#"))


# -- it runs at all ------------------------------------------------------------

def test_the_worker_parses_and_selftests():
    r = subprocess.run([sys.executable, _WORKER, "--selftest"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"--selftest failed: {r.stderr}"
    assert "selftest ok" in r.stdout


def test_the_worker_refuses_to_start_without_a_url():
    r = subprocess.run([sys.executable, _WORKER], capture_output=True, text=True, timeout=30)
    assert r.returncode != 0, "the worker started with no MCP endpoint"
    assert "--url" in (r.stderr + r.stdout)


# -- the log line is the demo --------------------------------------------------

def _worker_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("mcp_agent", _WORKER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_the_log_line_carries_both_the_identity_and_the_token():
    code = _code(_WORKER)
    line = [ln for ln in code.splitlines() if "spiffe_id" in ln and "named" in ln]
    assert line, (
        "no log statement carries both the SPIFFE ID and the token's name. Splitting "
        "them across lines hides that identity and authorization are two different "
        "things, which is the whole argument of the cell")


def test_no_part_of_the_token_is_ever_logged():
    """Stronger than the rule this replaces, and for a reason CodeQL found first.

    The worker used to print the token's first eleven characters, on the theory that
    they located the row in Settings -> API Tokens. `api/tokens.list_tokens` returns id,
    name, created_at, expires_at, last_used_at and is_active -- no prefix -- so those
    five hex characters correlated with nothing while being five real characters of a
    live credential. Now the NAME is logged and the value never is.

    Scoped to PRINT statements: the worker legitimately puts the token in an
    Authorization header; what must never happen is it reaching stdout.
    """
    code = _code(_WORKER)
    prints = [ln for ln in code.splitlines() if "print(" in ln]
    assert prints, "the worker prints nothing at all"
    for ln in prints:
        assert "{token}" not in ln, f"a print statement carries the raw token: {ln.strip()}"
        assert "{hint}" not in ln, (
            "a print statement carries a slice of the token. The PAT's name is the "
            "non-secret handle; no part of the value is")
    assert "def token_label" in code, "nothing names the token without showing it"
    assert "def token_hint" not in code, \
        "the truncating hint is back — it correlates with nothing the UI shows"

    # The property the two checks above miss: a slice taken at the ASSIGNMENT still
    # reaches a print through an innocent-looking name. So inside `run`, the token may
    # appear in exactly one place — the call that spends it.
    run_body = code.split("def run(", 1)[1].split("\ndef ")[0]
    # Past the signature (which names the parameter), and with string literals emptied so
    # the word "token" inside a log message is not mistaken for a use of the value.
    run_body = run_body.split("\n", 2)[2]
    run_body = re.sub(r'"[^"]*"|\'[^\']*\'', '""', run_body)
    uses = [ln.strip() for ln in run_body.splitlines()
            if re.search(r"\btoken\b", ln)]
    assert uses == ["payload = asyncio.run(call_once(url, token, tool))"], (
        "the token is used inside run() somewhere other than the call that spends it: "
        f"{uses}. It is spent, never displayed")


def test_the_label_is_the_pats_name_not_its_value():
    m = _worker_module()
    raw = "vmcli_" + ("a" * 64)
    assert m.token_label("mcp-reader-pat") == "mcp-reader-pat"
    assert raw not in m.token_label(""), "the empty label falls back to the token"
    assert m.token_label("") == "(unnamed)", \
        "an unlabelled worker should say so rather than inventing a handle"


def test_an_error_is_scrubbed_before_it_is_logged():
    """`call_once` hands the token to a third-party HTTP client as an Authorization
    header, and what that client puts in its error repr is not this worker's decision.
    On the one cell whose whole argument is about not leaking a credential, the value is
    removed on the way out."""
    m = _worker_module()
    raw = "vmcli_" + ("9f3c" * 16)
    leaked = f"Connection failed: headers={{'Authorization': 'Bearer {raw}'}}"
    out = m.scrub(leaked)
    assert raw not in out, "an error carrying the token reaches the log intact"
    assert "redacted" in out, "the scrub leaves no sign that something was removed"
    code = _code(_WORKER)
    run_body = code.split("def run(", 1)[1]
    assert "scrub(str(exc))" in run_body, "the error text is logged unscrubbed"


# -- identity is re-proved, never cached --------------------------------------

def test_the_svid_is_fetched_every_loop():
    code = _code(_WORKER)
    loop = code.split("def run(", 1)[1]
    assert "fetch_spiffe_id" in loop, (
        "the worker no longer fetches its SVID inside the loop. A cached ID would "
        "survive its registration entry being deleted, which is the opposite of what "
        "'an attested workload holds nothing' means")


def test_an_unattested_worker_says_so_rather_than_guessing():
    code = _code(_WORKER)
    assert '"unattested"' in code, \
        "the worker no longer reports being unattested; it would log a blank or a stale ID"


# -- the ending ----------------------------------------------------------------

def test_a_refused_token_ends_the_process():
    code = _code(_WORKER)
    run_body = code.split("def run(", 1)[1]
    assert "return 2" in run_body, (
        "the worker no longer exits on a refused token — the revoke would become a "
        "scroll of failures rather than the demo's closing beat")
    assert "401" in run_body, "the worker does not recognise a 401"


def test_the_unit_does_not_restart_on_a_revoke():
    unit = _yaml_code(_INSTALL)
    assert "SuccessExitStatus=2" in unit, (
        "the systemd unit does not treat exit 2 as a stop, so a revoked worker would be "
        "restarted in a loop and the demo's ending would be invisible")
    assert "Restart=always" not in unit, "the unit restarts unconditionally"


def test_the_unit_keeps_tmp_shared():
    """The SPIRE workload API socket lives under /tmp — the trap spire-agent-install.yml
    documents for the agent itself."""
    assert "PrivateTmp=no" in _yaml_code(_INSTALL), \
        "the unit gets a private /tmp, so the SPIRE socket would be invisible"


# -- the second token source, which stores nothing -----------------------------

def test_the_worker_offers_a_source_that_holds_nothing():
    code = _code(_WORKER)
    assert "def fetch_token_from_wlc" in code, \
        "the worker can only read its token from a file, so it always holds a static secret"
    assert "def fetch_identity_token" in code, \
        "nothing asks the platform to vouch for this machine"


def test_the_identity_request_handles_both_runtimes():
    """IMDS on a VM; IDENTITY_ENDPOINT/IDENTITY_HEADER where the runtime injects them.
    The same two-branch shape workload_credentials_service.build_identity_request has,
    and a worker that only knew one would fail on the other with an auth-shaped error."""
    code = _code(_WORKER)
    assert "IDENTITY_ENDPOINT" in code and "IDENTITY_HEADER" in code
    assert "169.254.169.254" in code, "no IMDS fallback for a plain VM"


def test_the_wlc_request_names_its_workload_identity():
    """Without X-BT-Service-Name the platform holds a valid token and no statement of
    which registered Workload Identity it is meant to satisfy."""
    code = _code(_WORKER)
    assert "X-BT-Service-Name" in code, "the WC call does not name its Workload Identity"
    assert "bt-secrets-api-version" in code, \
        "the mandatory API-version header is missing; it fails looking like an auth problem"


def test_the_wlc_path_matches_the_providers_grammar():
    code = _code(_WORKER)
    assert "/site/" in code and "/secrets/" in code, \
        "the secrets path no longer mirrors the provider's BuildPath"


def test_the_worker_refuses_wlc_mode_with_missing_configuration():
    r = subprocess.run([sys.executable, _WORKER, "--url", "https://x/mcp",
                        "--token-source", "wlc"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode != 0, "wlc mode started with nothing to identify itself to"
    out = r.stdout + r.stderr
    for flag in ("--wlc-base-url", "--wlc-site-id", "--wlc-service-name"):
        assert flag in out, (
            f"the refusal does not name {flag}. Every one of these is NON-SECRET, so "
            "there is no reason to be vague about which is absent")


def test_the_worker_says_which_source_it_used():
    """Which mode is in play must never be in doubt -- the whole claim is about what is
    or is not sitting on the host."""
    code = _code(_WORKER)
    run_body = code.split("def run(", 1)[1]
    assert "token_source" in run_body, "the worker never reports where its token came from"
    assert "nothing on this host" in code, \
        "the worker does not say plainly when it is holding nothing"


def test_the_worker_takes_no_http_dependency_for_this():
    """It runs on somebody else's VM; every dependency is something the play must put
    there. The MCP client is unavoidable, an HTTP library is not."""
    code = _code(_WORKER)
    for dep in ("import httpx", "import requests", "from httpx", "from requests"):
        assert dep not in code, f"the worker imports {dep!r} just to fetch a token"
    assert "urllib.request" in code, "no stdlib HTTP path"


# -- the third source: Password Safe, bootstrapped by Workload Credentials ------

def test_the_worker_reaches_password_safe_through_wc():
    """The correction this source exists for. Password Safe authenticates an application
    with a client-credentials pair, so that pair is a standing credential and always was
    -- the question is where it lives. In WC it makes WC a bootstrap for the vault rather
    than a second vault beside it, and the worker reaches anything Password Safe governs
    rather than only what was copied across."""
    code = _code(_WORKER)
    assert "def fetch_token_via_password_safe" in code, \
        "there is no path from a workload identity to a credential in the vault"
    assert "def password_safe_credential" in code
    assert "Auth/Connect/Token" in code and "Auth/SignAppIn" in code, \
        "the worker does not sign in the way ps_api_service._sign_in does"


def test_the_password_safe_pair_is_read_from_wc_not_the_host():
    """Both halves come out of Workload Credentials. A worker that took either as an
    argument would be holding the standing credential this source exists to remove."""
    code = _code(_WORKER)
    body = code.split("def fetch_token_via_password_safe", 1)[1].split("\ndef ")[0]
    assert body.count("read_wlc_secret") == 2, \
        "the Password Safe client id and secret are not both fetched from WC"
    assert "fetch_identity_token" in body, "nothing vouches for the machine first"


def test_one_identity_token_serves_both_secrets():
    """Two round trips to the metadata service for one machine's identity would be noise
    in the audit log, not caution."""
    code = _code(_WORKER)
    body = code.split("def fetch_token_via_password_safe", 1)[1].split("\ndef ")[0]
    assert body.count("fetch_identity_token") == 1, \
        "the identity token is fetched more than once for a single credential fetch"
    assert "identity_token=identity" in body


def test_the_retrieval_is_a_recorded_request():
    """The reason the extra hop is worth it. A request has a duration and a reason, it can
    require approval, and it is checked back in -- a PAT in a file has none of those."""
    code = _code(_WORKER)
    # The flow lives in three small helpers now, because the approval-gated episode
    # reuses every step but the check-in placement. Assert on the whole seam.
    seam = code[code.index("def _ps_session("):code.index("def fetch_spiffe_id(")]
    for part in ("Auth/Connect/Token", "Auth/SignAppIn", "Requests", "Credentials/",
                 "Checkin", "DurationMinutes", "Reason"):
        assert part in seam, f"the credential fetch does not {part!r}"


def test_the_request_is_checked_in_even_when_the_fetch_fails():
    """An open request holds the account's concurrent slot for its whole duration, so a
    worker that died between retrieval and release would make the NEXT fetch fail on the
    cap and report the wrong cause."""
    code = _code(_WORKER)
    body = code.split("def password_safe_credential", 1)[1].split("\ndef ")[0]
    assert "finally:" in body, "the credential request is not released on a failure path"
    assert body.index("finally:") < body.index("_checkin("), \
        "the check-in is not inside the finally"


def test_the_check_in_never_rotates_on_release():
    """ps_api_service._checkin records why: under synced accounts a change on either
    member re-rotates both, so rotate-on-release would rotate the real credential every
    time the worker read it, with a dead-credential window each time."""
    code = _code(_WORKER)
    assert "CheckinAndRotate" not in code and "checkin_and_rotate" not in code.lower(), \
        "the worker names the rotate-on-release endpoint"
    assert 'method="PUT"' in code, \
        "the check-in is not a PUT; urllib would infer POST from the body"


def test_a_soft_failure_string_is_not_spent_as_a_token():
    """Password Safe can return a soft-failure STRING in the credential position -- the
    case ps_api_service._looks_like_sa_token guards for the k8s tunnel. A worker that
    polled with that as its bearer would get a 401 and report the demo's closing beat for
    entirely the wrong reason."""
    code = _code(_WORKER)
    # Explicit now rather than incidental. It used to be caught by the hardcoded
    # `vmcli_` prefix check, which had to go: the same flow fetches a ServiceAccount
    # token, and a JWT is not a PAT. The soft-failure sentence needs its own guard.
    assert "_SOFT_FAILURE" in code, \
        "nothing recognises Password Safe's soft-failure sentence"
    poll = code.split("def _poll_credential(", 1)[1].split("\ndef ")[0]
    assert "_SOFT_FAILURE in value.lower()" in poll, \
        "the soft-failure sentence is not detected where the credential is read"
    body = code.split("def password_safe_credential", 1)[1].split("\ndef ")[0]
    assert "validate or _is_dashboard_pat" in body, \
        "the expected credential shape is no longer a parameter, so this flow can only "\
        "ever fetch a PAT"


# -- the identity is not Azure-only --------------------------------------------

def test_every_cloud_can_vouch_for_the_machine():
    """The mechanism is OIDC federation of a non-human identity, which all three clouds
    offer. Treating it as an Azure application identity is the narrower claim, and it is
    the one that makes the cell look Azure-shaped when it is not."""
    code = _code(_WORKER)
    for fn in ("_azure_identity_token", "_gcp_identity_token", "_projected_token",
               "_spire_jwt_svid"):
        assert f"def {fn}" in code, f"no identity branch for {fn}"
    for platform in ("azure", "gcp", "aws", "spire", "file"):
        assert f'"{platform}"' in code, f"{platform} is not a selectable platform"


def test_gcp_is_not_parsed_as_json():
    """The GCP metadata server returns the token as PLAIN TEXT. Parsing it as JSON fails
    on a valid token, which reads like a broken service account."""
    code = _code(_WORKER)
    body = code.split("def _gcp_identity_token", 1)[1].split("\ndef ")[0]
    assert "_get_text" in body and "_get_json" not in body
    assert "Metadata-Flavor" in body, "the GCP metadata server refuses without the header"


def test_aws_reads_a_projected_file_rather_than_imds():
    """There is no OIDC endpoint on EC2's IMDS: it issues SigV4 credentials and a signed
    identity document, not a JWT. Reaching for IMDS here would fail in a way that looks
    like a permissions problem."""
    code = _code(_WORKER)
    body = code.split("def _aws_token_file", 1)[1].split("\ndef ")[0]
    assert "AWS_WEB_IDENTITY_TOKEN_FILE" in code and \
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE" in code, \
        "neither IRSA nor Pod Identity is honoured"
    assert "169.254.169.254" not in body, "the AWS branch reaches for IMDS"
    assert "spire" in body, \
        "the refusal does not point at the platform that works on plain EC2"


def test_spire_can_be_the_issuer_without_a_cloud():
    """The row that ties this back to the lab: a bare-metal host has no metadata service,
    and the Workload Lab already publishes its trust domain as an OIDC issuer."""
    code = _code(_WORKER)
    body = code.split("def _spire_jwt_svid", 1)[1].split("\ndef ")[0]
    assert "fetch" in body and "jwt" in body and "-audience" in body, \
        "the SPIRE branch does not fetch an audience-bound JWT-SVID"


def test_auto_refuses_rather_than_guessing():
    """No marker distinguishes a bare Azure VM from a bare GCE one, and 169.254.169.254 is
    both their metadata addresses. A guess means a request that has to time out to say no,
    on the host most likely to be neither."""
    m = _worker_module()
    assert m.detect_platform(env={}, exists=lambda p: False) == "", \
        "auto guesses a platform on a host that declares none"
    assert m.detect_platform(env={"IDENTITY_ENDPOINT": "http://x", "IDENTITY_HEADER": "h"},
                             exists=lambda p: False) == "azure"
    assert m.detect_platform(env={"AWS_WEB_IDENTITY_TOKEN_FILE": "/t"},
                             exists=lambda p: False) == "aws"
    assert m.detect_platform(env={}, exists=lambda p: p == m.K8S_TOKEN_FILE) == "file"


def test_the_refusal_to_guess_names_the_choices():
    r = subprocess.run([sys.executable, _WORKER, "--url", "https://x/mcp",
                        "--token-source", "wlc", "--wlc-base-url", "https://a",
                        "--wlc-site-id", "s", "--wlc-service-name", "n",
                        "--wlc-resource", "r", "--wlc-secret-name", "k"],
                       capture_output=True, text=True, timeout=30,
                       env=dict(os.environ, AGENT_IDENTITY_PLATFORM="auto"))
    assert r.returncode != 0
    out = r.stdout + r.stderr
    for platform in ("azure", "gcp", "aws", "spire"):
        assert platform in out, f"the refusal does not offer {platform}"


# -- the play ------------------------------------------------------------------

def test_the_play_refuses_a_ps_worker_with_no_way_into_the_vault():
    unit = _yaml_code(_INSTALL)
    assert "agent_token_source == 'ps'" in unit, "the ps source has no guard of its own"
    block = unit.split("Refuse a Password-Safe-sourced worker", 1)[1][:900]
    for var in ("agent_ps_client_id_secret", "agent_ps_client_secret_secret",
                "agent_ps_api_url", "agent_ps_account_id"):
        assert var in block, f"the ps guard does not require {var}"


def test_the_exec_start_survives_every_mode():
    """It did not, once. The unit's ExecStart was built from backslash continuations with
    `{% if %}` blocks between them, and the `file` branch ended without a trailing
    backslash -- silently truncating the command so --spiffe-socket and --interval never
    reached the worker, and leaving systemd an orphan line it reads as an unknown
    directive. One folded line has no continuations to get wrong."""
    import yaml
    with open(_INSTALL, encoding="utf-8") as fh:
        play = yaml.safe_load(fh)[0]
    unit = [t for t in play["tasks"] if t["name"] == "Install the systemd unit"][0]
    exec_line = [ln for ln in unit["ansible.builtin.copy"]["content"].splitlines()
                 if ln.startswith("ExecStart=")]
    assert len(exec_line) == 1, "ExecStart is not a single line"
    assert not exec_line[0].rstrip().endswith("\\"), \
        "ExecStart ends in a continuation, which is how the arguments were lost before"
    assert "{{ agent_args }}" in exec_line[0]

    # Render the real thing for every mode, rather than reading the template. This is the
    # check that would have caught the truncation: the arguments have to SURVIVE, and
    # whether they do depends on the branch taken.
    from jinja2 import Template
    import re as _re
    ctx = dict(play["vars"], agent_mcp_url="https://dash/mcp",
               agent_identity_platform="gcp", agent_wlc_base_url="https://api.bt",
               agent_wlc_site_id="s1", agent_wlc_service_name="svc",
               agent_wlc_resource="aud", agent_wlc_secret_name="pat",
               agent_ps_client_id_secret="ps-id", agent_ps_client_secret_secret="ps-sec",
               agent_ps_api_url="https://ps/api/public/v3", agent_ps_account_id=42,
               agent_token_label="mcp-reader-pat")
    template = ctx.pop("agent_args")
    for mode in ("file", "wlc", "ps"):
        rendered = _re.sub(r"\s+", " ",
                           Template(template).render(dict(ctx, agent_token_source=mode))
                           ).strip()
        for flag in ("--url", "--token-source", "--spiffe-socket", "--interval",
                     "--token-label"):
            assert flag in rendered, f"{mode} mode lost {flag}"
        assert f"--token-source {mode}" in rendered
    # And each mode reaches only its own arguments.
    def _r(mode):
        return Template(template).render(dict(ctx, agent_token_source=mode))
    assert "--token-file" in _r("file") and "--ps-api-url" not in _r("file")
    assert "--wlc-secret-name" in _r("wlc") and "--ps-api-url" not in _r("wlc")
    assert "--ps-api-url" in _r("ps") and "--wlc-secret-name" not in _r("ps")


def test_the_play_removes_a_token_when_moving_to_wlc():
    """Re-running to move a host from `file` to `wlc` must leave nothing behind, or the
    claim that nothing is stored is contradicted by a file in /etc."""
    unit = _yaml_code(_INSTALL)
    assert "state: absent" in unit, \
        "the play does not remove a token left by a previous file-sourced install"


def test_the_play_writes_no_token_in_wlc_mode():
    unit = _yaml_code(_INSTALL)
    block = unit.split("Write the PAT where only the worker can read it", 1)[1][:400]
    assert "agent_token_source == 'file'" in block, \
        "the token file is written regardless of source"


# -- the token's handling on the host -----------------------------------------

def test_the_token_lands_in_a_file_not_the_environment():
    unit = _yaml_code(_INSTALL)
    assert "mode: \"0600\"" in unit, "the token file is not 0600"
    assert "no_log: true" in unit, "the token would be echoed in the Ansible run output"
    assert "Environment=" not in unit, (
        "the token is passed through the systemd environment, which is readable via "
        "`systemctl show` and /proc/<pid>/environ")


def test_the_install_refuses_without_a_token_or_a_url():
    unit = _read(_INSTALL)
    assert "assert" in unit and "agent_token is defined" in unit, \
        "the install playbook does not refuse a run that would produce a useless worker"


# -- the registration entry ----------------------------------------------------

def test_the_entry_is_selected_by_uid_not_by_path():
    entry = _yaml_code(_ENTRY)
    assert "unix:uid:" in entry, "the registration entry no longer selects on uid"
    assert "unix:path:" not in entry, (
        "the entry selects on a path, so anything able to write there inherits the "
        "worker's identity")


def test_the_entry_play_is_rerunnable():
    entry = _read(_ENTRY)
    assert "similar entry already exists" in entry, \
        "a second run of the entry play would fail on a duplicate rather than no-op"


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
