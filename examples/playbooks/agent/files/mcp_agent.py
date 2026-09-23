#!/usr/bin/env python3
"""mcp_agent — a non-human principal that reads the estate, and can be stopped.

The worker the agent demo cell installs. It does one small thing on a loop, and the
point is not the thing: it is that every loop leaves a line naming **who it is** and
**what it spent**, and that revoking the token ends it while somebody watches.

    spiffe://weaverlab.test/agent/mcp-reader · token "mcp-reader-pat" · 14 jobs, 2 failures · 14:02:11

TWO CREDENTIALS, AND THEY ARE NOT THE SAME ONE. This is the honest shape of the demo
and the code is arranged so nobody can miss it:

  * the **SVID** is the worker's IDENTITY. It is fetched from the SPIRE agent's workload
    API socket at each loop, held in memory, never written down, and re-fetched rather
    than cached — a workload that attests itself has nothing to store.
  * the **PAT** is the worker's AUTHORIZATION to this dashboard. It is what /mcp accepts,
    it is scoped to a user whose RBAC bounds every tool, it expires, and it can be
    revoked from Settings -> API Tokens while the loop is running. The log line carries
    its NAME and no part of its value -- see ``token_label``.

THE SVID DOES NOT AUTHENTICATE TO /mcp, AND NOTHING HERE PRETENDS IT DOES. The MCP
server takes a Bearer PAT (api/mcp_server.py) and has no mTLS path. Bridging the two --
having the SVID mint the PAT -- needs the Password Safe SPIFFE SVID plugin, whose
configuration question services/spire_lab_service.py records as unresolved. So the
worker proves its identity and spends its authorization in the same log line, and the
gap between them stays visible instead of being papered over.

THREE TOKEN SOURCES, AND TWO OF THEM LEAVE NOTHING ON THIS HOST.

  * ``--token-source file`` (default) reads a 0600 file. An env var would be readable
    from /proc/<pid>/environ by anything running as the same user and shows up in a
    `ps e`, so a file read once at startup is the smaller surface -- but it is still a
    static secret sitting on a disk.
  * ``--token-source wlc`` asks the platform for this machine's own identity token,
    presents that to BeyondTrust **Workload Credentials**, and reads its dashboard PAT
    back out of WC's own static store.
  * ``--token-source ps`` goes one hop further, and is the one that generalises. WC holds
    the **Password Safe API client id and secret**; the worker fetches that pair with its
    identity token, signs in to Password Safe with it, and requests the credential from
    the vault. The pair is a standing credential -- Password Safe authenticates an
    application with exactly one (``services/ps_api_service._sign_in``) -- so the question
    was never whether one exists but where it lives. Here it lives in WC, which hands it
    over against an identity the platform vouches for, and the host keeps neither half.

Why the third is worth the extra hop: WC becomes a **bootstrap for the vault** rather than
a second vault beside it. Everything Password Safe already governs governs this too -- the
retrieval is a recorded request with a duration, it can require approval, and it is checked
back in. That is the combination worth showing: an identity the platform vouches for (new),
Workload Credentials brokering on it (new), and the vault that has always held the secret
(old). ``services/workload_credentials_service`` puts its half plainly -- "two auth modes,
and the second one stores nothing".

THE IDENTITY TOKEN IS NOT AZURE-ONLY. Every major cloud issues OIDC tokens to non-human
identities, and ``--identity-platform`` picks which is asked:

  * ``azure``  IMDS, or IDENTITY_ENDPOINT/IDENTITY_HEADER where the runtime injects them.
  * ``gcp``    the metadata server's ``instance/service-accounts/default/identity``
               endpoint, which returns a Google-signed OIDC token as PLAIN TEXT.
  * ``aws``    the projected web-identity token at ``AWS_WEB_IDENTITY_TOKEN_FILE`` (IRSA)
               or ``AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE`` (EKS Pod Identity). Note what
               this is NOT: a plain EC2 instance's IMDS issues SigV4 credentials and a
               signed identity document, not an OIDC JWT. EC2 outside a cluster needs an
               issuer of its own -- which is what ``spire`` below is.
  * ``spire``  a JWT-SVID from the SPIRE agent already on this host. The Workload Lab
               stands up an OIDC discovery provider for its trust domain
               (``services/spire_lab_service``), so the trust domain federates like any
               cloud's issuer -- and this is the branch that works on bare metal.
  * ``file``   any other projected token on disk, the Kubernetes ServiceAccount token
               being the one every cluster already mounts.
  * ``auto``   (default) whichever of the above this host declares. It refuses rather
               than guessing: see ``detect_platform``.

ONE MORE THING IT DOES, AND IT IS A DIFFERENT DEMO. ``--k8s-episode`` runs a single
bounded, approval-gated errand rather than the loop: it asks Password Safe for the
Workload Lab Kubernetes token the dashboard linked to this agent, **waits for a human to
approve**, proves with two reads that the token is SCOPED rather than merely working, and
gives the request slot back. The closing beat there is not a revoke -- it is an agent
that asks for access to a cluster and cannot proceed until somebody says yes.

Exit codes are that demo's punctuation: 0 proved the scope, 3 was never approved, 4 means
a refusal did not refuse -- the one outcome that would otherwise look like success.

AND ONE THAT MINTS RATHER THAN RETRIEVES. ``--cloud-episode`` asks Workload Credentials
for a short-lived AWS or Azure credential against a **dynamic secret**, proves it is
scoped, and then proves it ends. It is the only mode here with no Password Safe in the
chain, no human in the loop, and a **price**: WC bills per issuance, so one run is one
charge and nothing retries.

Its ending is the interesting part and it is not a revoke. An AWS lease cannot be
withdrawn -- STS will not take back a credential it has signed -- so the closing beat is
a real wait for real expiry and a re-probe. The cloud play ships the same rule for the
same reason: a run that faked the clock would prove it can print a failure message, not
that the credential died. ``3`` is never returned by this mode, because nobody was asked.

EXITS NON-ZERO ON 401, DELIBERATELY. The revoke is the demo's closing beat, so the
worker must visibly stop rather than log a warning and keep polling -- systemd then
shows a failed unit, which is the thing to point at.
"""
import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

DEFAULT_SOCKET = "unix:///tmp/spire-agent/public/api.sock"
DEFAULT_TOOL = "dashboard_summary"
# The header the platform requires. Omitting it fails in a way that reads like an auth
# problem, which is why it is pinned here rather than left to a default.
WLC_API_VERSION = "2025-07-01"
# ── Identity sources, one branch per platform ────────────────────────────────
# Azure. The runtime injects IDENTITY_ENDPOINT/IDENTITY_HEADER on Container Apps and App
# Service; a plain VM has neither and uses the metadata service directly. Same two-branch
# shape as services/workload_credentials_service.build_identity_request.
IMDS_URL = "http://169.254.169.254/metadata/identity/oauth2/token"
IMDS_API_VERSION = "2018-02-01"
# GCP. Returns the token as PLAIN TEXT rather than JSON, which is why _get_text exists
# separately from _get_json. `format=full` includes the instance details a federation
# policy can assert on, so the issuer's claim is about this VM and not just the project.
GCP_IDENTITY_URL = ("http://metadata.google.internal/computeMetadata/v1/instance/"
                    "service-accounts/default/identity")
# AWS. There is no OIDC endpoint on IMDS to call: a projected FILE is how an AWS non-human
# identity holds a web-identity token. IRSA sets the first variable, EKS Pod Identity the
# second, and both are already the mechanism `sts:AssumeRoleWithWebIdentity` consumes.
AWS_TOKEN_FILE_VARS = ("AWS_WEB_IDENTITY_TOKEN_FILE",
                       "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE")
# The projected ServiceAccount token every cluster mounts, for the `file` platform.
K8S_TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"  # noqa: S105
IDENTITY_PLATFORMS = ("auto", "azure", "gcp", "aws", "spire", "file")
# Three dot-separated base64url segments. Used to pull a JWT-SVID out of spire-agent's
# human-readable output, and to reject a soft-failure string in a token position.
JWT_RE = r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def read_token(path: str) -> str:
    """The PAT, from a file the operator owns."""
    with open(path, encoding="utf-8") as fh:
        token = fh.read().strip()
    if not token:
        raise SystemExit(f"[agent] FATAL: {path} is empty — no authorization to spend.")
    if not token.startswith("vmcli_"):
        raise SystemExit(
            f"[agent] FATAL: {path} does not hold a dashboard PAT (expected vmcli_…).")
    return token


# Anything token-shaped, for scrubbing error text. A client library that puts its request
# headers in an exception repr would otherwise hand the Authorization header to the log --
# on the one cell whose entire argument is about not leaking a credential.
TOKEN_RE = re.compile(r"vmcli_[0-9a-fA-F]{8,}")
# A ServiceAccount token is a JWT, and an episode handles one. Scrubbed on the same
# principle and in the same place: what a third-party client or an API server puts in an
# error body is not this worker's decision, so anything credential-shaped is removed on
# the way out rather than trusted not to appear.
JWT_SCRUB_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
# An AWS access key ID, which AWS's own error bodies quote back at you. Only the ID:
# a secret access key is forty unmarked base64-ish characters with nothing to anchor a
# pattern on, so it CANNOT be scrubbed and must never reach a string that gets printed.
# That is a structural rule rather than a regex, and a test on captured stdout is what
# enforces it — see tests/test_agentcell_cloud_episode.
AWS_KEY_RE = re.compile(r"\b(?:ASIA|AKIA)[0-9A-Z]{16}\b")


def scrub(text: str) -> str:
    """An exception's text with anything credential-shaped removed.

    `call_once` hands the PAT to an HTTP client as an Authorization header, and the
    cluster probes hand a ServiceAccount token to an API server the same way. What either
    puts in an error repr is not this worker's decision, so the values are removed on the
    way OUT, at the one place error text becomes a log line.

    **This function is a backstop, not the defence.** It can only remove what has a
    shape; an AWS secret access key has none. The cloud episode's rule is that credential
    values never reach a printed string in the first place.
    """
    out = TOKEN_RE.sub("vmcli_<redacted>", text or "")
    out = JWT_SCRUB_RE.sub("<jwt redacted>", out)
    return AWS_KEY_RE.sub("<aws key id redacted>", out)


def token_label(label: str) -> str:
    """What the log line calls this worker's authorization.

    THE PAT'S NAME, NOT ANY PART OF ITS VALUE. An earlier version logged the token's first
    eleven characters, on the theory that it located the row in Settings -> API Tokens.
    It does not: `api/tokens.list_tokens` returns id, name, created_at, expires_at,
    last_used_at and is_active, and no prefix -- so those five hex characters correlated
    with nothing an operator can see, while being five real characters of a live
    credential. The name is what the UI lists, what `agentcell_service.pat_name_for`
    generates, and what the create response hands back beside the once-only token.

    It also keeps the credential away from every logging call in `run`, which is the
    shape CodeQL's clear-text-logging query is right to be suspicious of -- see
    `services/cloud_function_service._record_provenance` for the same reasoning.
    """
    return (label or "").strip() or "(unnamed)"


def _request(url: str, headers: dict, data=None, timeout: int = 15,
             method: str = "") -> str:
    """One HTTP call, stdlib only, returning the body as text.

    urllib rather than httpx or requests deliberately: this worker runs on somebody
    else's VM, and every dependency it needs is something the install play has to put
    there. The MCP client is unavoidable; an HTTP library is not.

    ``method`` is explicit because urllib infers POST from the presence of a body, and
    Password Safe's check-in is a PUT with one.
    """
    import urllib.request

    req = urllib.request.Request(url, headers=headers, data=data,
                                 method=method or None)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read().decode("utf-8")


def _get_text(url: str, headers: dict, timeout: int = 10) -> str:
    """A GET whose body is not JSON. GCP's identity endpoint is the reason."""
    return _request(url, headers, timeout=timeout).strip()


def _get_json(url: str, headers: dict, timeout: int = 10) -> dict:
    return json.loads(_request(url, headers, timeout=timeout))


def _post_json(url: str, headers: dict, body=None, form=None, timeout: int = 15):
    """A POST that returns JSON, with either a form or a JSON body.

    Password Safe needs both shapes in the same session: the token endpoint is
    form-encoded, everything after it is JSON.
    """
    from urllib.parse import urlencode

    headers = dict(headers)
    if form is not None:
        data = urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(body or {}).encode("utf-8")
        headers["Content-Type"] = "application/json"
    text = _request(url, headers, data=data, timeout=timeout)
    return json.loads(text) if text.strip() else {}


def detect_platform(env=None, exists=None) -> str:
    """Which identity source this host declares, or "" if it declares none.

    Markers only -- an env var or a file, checked in cost order. It deliberately does NOT
    probe the link-local metadata service to find out: 169.254.169.254 is the same address
    on Azure and GCP and answers to different headers, so a probe means a request that has
    to time out to say no, once per platform, on every host that is neither. Startup then
    hangs for tens of seconds on bare metal, which is the host most likely to be running
    this worker in a lab.

    A plain VM declares nothing -- no marker distinguishes a bare Azure VM from a bare GCE
    one -- so `auto` returns "" for it and the caller names the choices. An explicit
    --identity-platform is one word and never wrong; a guess that picks the wrong metadata
    flavour fails ten seconds later with a timeout that reads like a network problem.

    `env` and `exists` are injectable so the selftest can exercise every branch from a host
    that is none of them.
    """
    env = os.environ if env is None else env
    exists = os.path.exists if exists is None else exists
    if (env.get("IDENTITY_ENDPOINT") or "").strip() and (env.get("IDENTITY_HEADER") or "").strip():
        return "azure"
    if any((env.get(v) or "").strip() for v in AWS_TOKEN_FILE_VARS):
        return "aws"
    if (env.get("GCE_METADATA_HOST") or "").strip():
        return "gcp"
    if exists(DEFAULT_SOCKET.replace("unix://", "")):
        return "spire"
    if exists(K8S_TOKEN_FILE):
        return "file"
    return ""


def _azure_identity_token(audience: str, client_id: str = "") -> str:
    """Azure: IMDS, or the per-replica endpoint the runtime injects."""
    from urllib.parse import urlencode

    params = {"api-version": IMDS_API_VERSION, "resource": audience}
    if client_id:
        # A user-assigned identity. Sending this blank is NOT the same as omitting it --
        # it asks for an identity with no client id and fails.
        params["client_id"] = client_id
    endpoint = (os.environ.get("IDENTITY_ENDPOINT") or "").strip()
    header = (os.environ.get("IDENTITY_HEADER") or "").strip()
    if endpoint and header:
        url, headers = endpoint, {"X-IDENTITY-HEADER": header}
    else:
        url, headers = IMDS_URL, {"Metadata": "true"}
    payload = _get_json(f"{url}?{urlencode(params)}", headers)
    token = payload.get("access_token") or ""
    if not token:
        raise SystemExit("[agent] FATAL: the identity endpoint returned no access_token.")
    return token


def _gcp_identity_token(audience: str) -> str:
    """GCP: the metadata server signs an OIDC token for this service account."""
    from urllib.parse import urlencode

    query = urlencode({"audience": audience, "format": "full"})
    token = _get_text(f"{GCP_IDENTITY_URL}?{query}", {"Metadata-Flavor": "Google"})
    if not re.fullmatch(JWT_RE, token):
        raise SystemExit("[agent] FATAL: the GCP metadata server returned something that "
                         "is not an identity token. Check that the VM has a service "
                         "account attached.")
    return token


def _aws_token_file(env=None) -> str:
    env = os.environ if env is None else env
    for var in AWS_TOKEN_FILE_VARS:
        path = (env.get(var) or "").strip()
        if path:
            return path
    raise SystemExit(
        "[agent] FATAL: --identity-platform aws found neither "
        + " nor ".join(AWS_TOKEN_FILE_VARS)
        + ". AWS hands a non-human identity its OIDC token as a projected file (IRSA or "
          "EKS Pod Identity); a plain EC2 instance has no such issuer, so use "
          "--identity-platform spire there.")


def _projected_token(path: str, what: str) -> str:
    """A token the platform wrote to disk for this workload.

    Not a static secret despite being a file: it is audience-bound, minutes-long, and
    rotated in place by the kubelet or the pod-identity agent. The worker re-reads it
    rather than caching for exactly that reason.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError as exc:
        raise SystemExit(f"[agent] FATAL: cannot read the {what} token at {path}: {exc}")
    if not token:
        raise SystemExit(f"[agent] FATAL: the {what} token at {path} is empty.")
    return token


def _spire_jwt_svid(audience: str, socket_path: str) -> str:
    """SPIRE: a JWT-SVID for this workload, audience-bound.

    The same shell-out as fetch_spiffe_id and for the same reason -- the binary is on any
    host running an agent, and this one is by construction. This is the branch that needs
    no cloud at all: the Workload Lab publishes the trust domain as an OIDC issuer, so a
    bare-metal workload federates on the same mechanism a cloud VM does.
    """
    try:
        out = subprocess.run(
            ["spire-agent", "api", "fetch", "jwt", "-audience", audience,
             "-socketPath", socket_path],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        raise SystemExit("[agent] FATAL: --identity-platform spire needs the spire-agent "
                         "binary on PATH.")
    except subprocess.TimeoutExpired:
        raise SystemExit("[agent] FATAL: the SPIRE workload API did not answer.")
    if out.returncode != 0:
        raise SystemExit("[agent] FATAL: SPIRE would not issue a JWT-SVID — this workload "
                         f"has no registration entry for audience {audience!r}.")
    m = re.search(JWT_RE, out.stdout)
    if not m:
        raise SystemExit("[agent] FATAL: no JWT-SVID in the spire-agent output.")
    return m.group(0)


def fetch_identity_token(audience: str, platform: str = "auto", client_id: str = "",
                         token_file: str = "",
                         socket_path: str = DEFAULT_SOCKET) -> str:
    """This machine's own OIDC token, from whichever issuer vouches for it.

    Nothing is stored to get one, on any branch. There is no Azure requirement in the
    mechanism -- federating a non-human identity against an OIDC issuer is available in
    all three clouds and from SPIRE besides. What is Azure-shaped is which of these the
    dashboard's own Workload Credentials client has been run against, which is an
    implementation gap rather than a property of the design.
    """
    platform = (platform or "auto").strip().lower()
    if platform == "auto":
        platform = detect_platform()
        if not platform:
            raise SystemExit(
                "[agent] FATAL: this host declares no identity source, so --identity-"
                "platform cannot be inferred. Name one: "
                + ", ".join(p for p in IDENTITY_PLATFORMS if p != "auto")
                + ". (A plain cloud VM always needs this — no marker on disk tells an "
                  "Azure VM apart from a GCE one.)")
    if platform == "azure":
        return _azure_identity_token(audience, client_id)
    if platform == "gcp":
        return _gcp_identity_token(audience)
    if platform == "aws":
        return _projected_token(token_file or _aws_token_file(), "AWS web identity")
    if platform == "spire":
        return _spire_jwt_svid(audience, socket_path.replace("unix://", ""))
    if platform == "file":
        return _projected_token(token_file or K8S_TOKEN_FILE, "projected identity")
    raise SystemExit(f"[agent] FATAL: unknown identity platform {platform!r}; choose one "
                     f"of {', '.join(IDENTITY_PLATFORMS)}.")


def read_wlc_secret(*, base_url: str, site_id: str, service_name: str,
                    secret_name: str, identity_token: str, folder: str = "") -> str:
    """One static secret out of Workload Credentials, against an identity token.

    The chain, and why each link is there:

      1. the platform vouches for this machine  -> an identity token, stored nowhere;
      2. Workload Credentials accepts that token in place of a PAT (``entra`` auth mode)
         and ``X-BT-Service-Name`` says which registered Workload Identity it satisfies;
      3. the secret comes back.

    The path grammar mirrors the shipping Terraform provider's BuildPath --
    ``/site/{site-id}/secrets{endpoint}`` with an optional ``?folder=`` -- so this and
    ``services/workload_credentials_service.build_secrets_path`` cannot disagree about
    where a secret lives.

    **The endpoint for a static secret is ``/static/{name}``, and leaving that segment out
    was a real bug rather than a shorthand.** This function built ``/secrets/{name}`` while
    ``workload_credentials_service.read_static`` builds ``/secrets/static/{name}`` -- the
    path a live site actually answered, recorded in
    ``tests/test_workload_credentials.test_the_live_read_response_yields_just_the_secret_map``.
    So every ``--token-source wlc`` and ``ps`` run would have 404'd, and the docstring
    above asserted the two could not disagree while they did. The test that was meant to
    catch it only checked that ``/site/`` and ``/secrets/`` appeared somewhere in the file,
    which any of the three paths satisfies; it now asserts the whole path.

    The identity token is passed in rather than fetched here: the ``ps`` source reads two
    secrets, and one token should serve both rather than making the metadata service
    answer twice for the same machine.
    """
    from urllib.parse import quote, urlencode

    path = f"/site/{quote(site_id)}/secrets/static/{quote(secret_name)}"
    url = base_url.rstrip("/") + path
    if folder:
        url += "?" + urlencode({"folder": folder})
    payload = _get_json(url, {
        "Authorization": f"Bearer {identity_token}",
        "X-BT-Service-Name": service_name,
        "bt-secrets-api-version": WLC_API_VERSION,
    })
    for key in ("value", "secret", "password"):
        got = payload.get(key)
        if isinstance(got, str) and got:
            return got.strip()
    raise SystemExit(
        "[agent] FATAL: Workload Credentials returned no secret value for "
        f"{secret_name!r}. Fields present: {sorted(payload)}")


def fetch_token_from_wlc(*, base_url: str, site_id: str, service_name: str,
                         resource: str, secret_name: str, folder: str = "",
                         client_id: str = "", platform: str = "auto",
                         identity_token_file: str = "",
                         socket_path: str = DEFAULT_SOCKET) -> str:
    """This worker's dashboard PAT, from Workload Credentials' own store."""
    token = fetch_identity_token(resource, platform, client_id, identity_token_file,
                                 socket_path)
    return read_wlc_secret(base_url=base_url, site_id=site_id,
                           service_name=service_name, secret_name=secret_name,
                           identity_token=token, folder=folder)


def fetch_token_via_password_safe(
        *, base_url: str, site_id: str, service_name: str, resource: str,
        client_id_secret: str, client_secret_secret: str, ps_api_url: str,
        account_id: int, system_id: int = 0, folder: str = "", client_id: str = "",
        platform: str = "auto", identity_token_file: str = "",
        socket_path: str = DEFAULT_SOCKET, duration_min: int = 30,
        reason: str = "mcp-agent credential fetch") -> str:
    """This worker's dashboard PAT, out of Password Safe, holding nothing.

    One hop longer than ``fetch_token_from_wlc`` and a different argument. Workload
    Credentials here holds only the **Password Safe API client id and secret** -- the
    OAuth2 pair ``services/ps_api_service._sign_in`` uses -- and the credential itself
    stays where it has always been, in the vault that rotates it. WC is a bootstrap, not
    a second vault.

    What that buys, and it is the reason the extra hop is worth it: the retrieval is a
    **recorded request** with a duration and a reason, it can be made to require approval,
    and it is checked back in. The PAT in a file has none of those properties and never
    will.

    Both WC secrets are read against ONE identity token: two round trips to the metadata
    service for one machine's identity would be noise in the audit log, not caution.
    """
    identity = fetch_identity_token(resource, platform, client_id, identity_token_file,
                                    socket_path)
    wlc = dict(base_url=base_url, site_id=site_id, service_name=service_name,
               identity_token=identity, folder=folder)
    ps_client_id = read_wlc_secret(secret_name=client_id_secret, **wlc)
    ps_client_secret = read_wlc_secret(secret_name=client_secret_secret, **wlc)
    return password_safe_credential(
        api_url=ps_api_url, client_id=ps_client_id, client_secret=ps_client_secret,
        account_id=account_id, system_id=system_id, duration_min=duration_min,
        reason=reason)


# Password Safe can exit SUCCESSFULLY and return this in the credential position when
# the request is not releasable -- the access policy requires approval, or the requestor
# cannot auto-release. services/btapi_service learned it the hard way: the text is handed
# back as the "credential" and only surfaces much later as an opaque failure once
# something tries to authenticate with it.
#
# THIS IS HOW "AWAITING APPROVAL" ARRIVES. Not as a status code -- as a sentence. The
# wait loop below reads it as *not yet* rather than as failure, which is the whole reason
# an approval-gated episode can be waited on at all.
_SOFT_FAILURE = "not possible to get a credential"


def _looks_like_jwt(value: str) -> bool:
    """Three dot-separated segments. What a ServiceAccount token is, in both bound and
    long-lived mode -- the same check ``ps_api_service._looks_like_sa_token`` makes."""
    parts = (value or "").split(".")
    return len(parts) == 3 and all(parts) and not any(c.isspace() for c in value)


def _is_dashboard_pat(value: str) -> bool:
    return (value or "").startswith("vmcli_")


def _ps_session(api_url: str, client_id: str, client_secret: str) -> tuple:
    """``(base, headers)`` for an authenticated Password Safe session.

    ``POST Auth/Connect/Token`` (form-encoded) then ``POST Auth/SignAppIn``, mirroring
    ``services/ps_api_service._sign_in`` step for step -- a second dialect of the same API
    is how the two drift apart.
    """
    base = api_url.rstrip("/") + "/"
    token_body = _post_json(base + "Auth/Connect/Token", {"Accept": "application/json"},
                            form={"grant_type": "client_credentials",
                                  "client_id": client_id,
                                  "client_secret": client_secret})
    bearer = (token_body or {}).get("access_token") or ""
    if not bearer:
        raise SystemExit("[agent] FATAL: Password Safe returned no access_token for the "
                         "client-credentials pair Workload Credentials handed over.")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {bearer}"}
    _post_json(base + "Auth/SignAppIn", headers)
    return base, headers


def _open_request(base: str, headers: dict, *, account_id: int, system_id: int,
                  duration_min: int, reason: str) -> int:
    """``POST Requests`` -> a request id."""
    body = _post_json(base + "Requests", headers, body={
        "AccessType": "View", "SystemID": int(system_id or 0),
        "AccountID": int(account_id), "DurationMinutes": int(duration_min),
        "Reason": reason, "ConflictOption": "reuse"})
    request_id = body if isinstance(body, int) else (
        (body or {}).get("RequestID") or (body or {}).get("RequestId")
        or (body or {}).get("id"))
    if not request_id:
        raise SystemExit("[agent] FATAL: Password Safe returned no request id.")
    return int(request_id)


def _poll_credential(base: str, headers: dict, request_id: int) -> tuple:
    """``(value, pending)`` for one attempt at ``GET Credentials/{id}``.

    ``pending`` true means *not yet* -- the request exists and Password Safe has not
    released it, which on an approval-gated policy means a person has not said yes. Both
    shapes that state arrives in are handled: a non-200, and a 200 carrying the
    soft-failure sentence.
    """
    import urllib.error

    try:
        raw = _request(base + f"Credentials/{int(request_id)}", headers)
    except urllib.error.HTTPError as exc:
        # 403/404/409 here mean the request is not releasable yet; anything else is a
        # problem worth stopping for rather than waiting on.
        if exc.code in (403, 404, 409):
            return "", True
        raise
    try:
        got = json.loads(raw) if raw.strip() else ""
    except ValueError:
        got = raw
    if isinstance(got, dict):
        got = got.get("Credentials") or got.get("Password") or ""
    value = str(got or "").strip().strip('"')
    if not value or _SOFT_FAILURE in value.lower():
        return "", True
    return value, False


def _checkin(base: str, headers: dict, request_id: int, reason: str) -> None:
    """Release the request. Plain check-in ONLY -- never the rotate-on-release variant,
    for the reason ``ps_api_service._checkin`` records: under synced accounts a change on
    either member re-rotates both."""
    try:
        _request(base + f"Requests/{int(request_id)}/Checkin",
                 dict(headers, **{"Content-Type": "application/json"}),
                 data=json.dumps({"Reason": reason}).encode("utf-8"),
                 method="PUT")
    except Exception:  # noqa: BLE001 — best effort; the duration expires it anyway
        print("[agent] note: the credential check-in was refused; the request "
              "expires on its own duration.", flush=True)


def password_safe_credential(*, api_url: str, client_id: str, client_secret: str,
                             account_id: int, system_id: int = 0,
                             duration_min: int = 30,
                             reason: str = "mcp-agent credential fetch",
                             validate=None, expected: str = "a dashboard PAT") -> str:
    """One managed credential, with a pair this host was handed rather than keeps.

    ``validate`` says what shape the credential should be; it defaults to a dashboard PAT
    because that is what this worker fetches for itself. **It is a parameter rather than
    a literal** because the same recorded-request flow serves the cluster token too, and
    a hardcoded ``vmcli_`` check would reject a perfectly good ServiceAccount token. It
    also used to be what caught the soft-failure sentence, by accident -- that guard is
    explicit now (see ``_poll_credential``).

    The check-in is in a ``finally``: an open request holds the account's concurrent slot
    for its whole duration, so a worker that crashed between retrieval and release would
    make the NEXT fetch fail on the cap and report the wrong cause.

    This does NOT wait. It is the fetch for a policy that auto-releases; an
    approval-gated one is ``password_safe_episode``.
    """
    check = validate or _is_dashboard_pat
    base, headers = _ps_session(api_url, client_id, client_secret)
    request_id = _open_request(base, headers, account_id=account_id,
                               system_id=system_id, duration_min=duration_min,
                               reason=reason)
    try:
        value, pending = _poll_credential(base, headers, request_id)
        if pending or not check(value):
            raise SystemExit(
                f"[agent] FATAL: Password Safe did not release {expected} for account "
                f"{int(account_id)} — the request may be awaiting approval, or the "
                "access policy may not auto-release.")
        return value
    finally:
        _checkin(base, headers, request_id, reason)


def approval_problem(polls: int, required: bool) -> str:
    """Why an episode that got its credential should stop anyway. Pure.

    **This worker cannot make Password Safe require approval** -- that is the account's
    access policy, set in BeyondInsight, and nothing here can or should change it. What it
    can do is refuse to PRETEND there was a human when there was not.

    Without this the failure is silent and the demo is a lie: on an auto-releasing policy
    the episode fetches, probes and prints a success line that reads exactly like the
    approved one. The operator concludes an approval gate is in force; the audit trail
    shows a request nobody was asked about. Turning that into a refusal costs one flag and
    removes the only way this demo can mislead.
    """
    if not required or polls > 0:
        return ""
    return ("Password Safe released the credential on the first ask — no person was "
            "consulted. This episode is supposed to demonstrate a human in the loop, so "
            "it refuses rather than printing a success line that reads exactly like an "
            "approved one. Either set the account's access policy to require approval "
            "(BeyondInsight → the managed account → its access policy, with auto-release "
            "off), or pass --no-require-approval to run it as an ungated fetch and say so "
            "when you present it.")


def password_safe_episode(*, api_url: str, client_id: str, client_secret: str,
                          account_id: int, system_id: int = 0,
                          duration_min: int = 15, reason: str,
                          max_wait_seconds: int = 1800, poll_seconds: int = 20,
                          on_wait=None, validate=None,
                          expected: str = "a ServiceAccount token") -> tuple:
    """One approval-gated episode: ask, wait for a person, hand back ``(value, base,
    headers, request_id, waits)`` so the caller can use it and then release.

    ``waits`` is **how many polls went by before the credential came back**, and it is
    returned rather than kept because it is the only evidence available here that a human
    was involved at all. Zero means Password Safe released on the first ask -- the access
    policy auto-releases, no person was consulted, and an episode that reported "approved"
    would be describing something that did not happen. What to DO about that is the
    caller's decision (see ``--require-approval``); knowing it is this function's job.

    **The waiting is the demonstration**, so it is visible: ``on_wait`` is called every
    poll with the seconds elapsed, and the worker prints a line. An agent that cannot
    authorise its own access to a cluster is the argument; a silent sleep would hide it.

    **It does NOT check in while pending**, and that is the difference from
    ``password_safe_credential`` and from ``ps_api_service._request_credential``. Those
    release the slot when the credential does not come back, which is right for an
    auto-release policy and exactly wrong here: it would cancel the very request a human
    is being asked to approve.

    **It does give up.** An abandoned request holds the account's concurrent slot for its
    whole duration, so the next attempt fails on the cap (4035) reporting a cap problem
    instead of the approval it was waiting on -- the confusion
    ``ps_api_service._request_credential`` documents. On timeout the slot is returned and
    the caller is told it expired rather than that anything failed.
    """
    check = validate or _looks_like_jwt
    base, headers = _ps_session(api_url, client_id, client_secret)
    request_id = _open_request(base, headers, account_id=account_id,
                               system_id=system_id, duration_min=duration_min,
                               reason=reason)
    waited = 0
    polls = 0
    while True:
        try:
            value, pending = _poll_credential(base, headers, request_id)
        except Exception:
            _checkin(base, headers, request_id, reason)
            raise
        if not pending:
            # `validate` is a parameter for the same reason it is one on
            # password_safe_credential: this flow serves a cluster token (a JWT) AND a
            # PKCS#12 passphrase (an opaque string). A hardcoded shape would reject one
            # of the two, and the rejection would read as Password Safe misbehaving.
            if not check(value):
                _checkin(base, headers, request_id, reason)
                raise SystemExit(
                    f"[agent] FATAL: Password Safe released something that is not "
                    f"{expected} for account {int(account_id)}.")
            return value, base, headers, request_id, polls
        if waited >= max_wait_seconds:
            _checkin(base, headers, request_id, reason)
            return "", base, headers, request_id, polls
        if on_wait:
            on_wait(waited)
        time.sleep(poll_seconds)
        waited += poll_seconds
        polls += 1


# ── Proving the token is SCOPED, not merely that it works ────────────────────
#
# The two beats examples/playbooks/k8s/ci-*-with-ps-token.yml assert, in the same shape
# and for the same reason. docs/workload-lab/kubernetes.md is explicit that
# steps 3 and 4 -- the REFUSALS -- are the ones that prove something, and that they are
# written as assertions rather than runbook steps because "a step in a runbook gets
# skipped, and an assertion does not".
#
# Each profile gets one read that must succeed and one that must be refused:
PROBES = {
    # ClusterRole `edit` through a RoleBinding in ONE namespace.
    "deployer": {
        "allow": "/api/v1/namespaces/{ns}/pods",
        "deny": "/api/v1/namespaces/{other}/pods",
        "says": "namespace-scoped: it can list pods in {ns} and is refused in {other}",
    },
    # ClusterRole `view` cluster-wide, which upstream omits Secrets from by design.
    "reader": {
        "allow": "/api/v1/pods",
        "deny": "/api/v1/namespaces/{ns}/secrets",
        "says": "cluster-wide read, and Secrets refused — `view` omits them by design",
    },
}


def k8s_probe(*, api_server: str, token: str, profile: str, namespace: str,
              other_namespace: str = "kube-system", verify: bool = True) -> dict:
    """Run one profile's two reads. Returns what happened; raises only on a broken setup.

    **The refusal has to fail with 403 specifically.** A wrong API server, an expired
    token or a typo in the path also fail, and a check that only asserted "it failed"
    would report a passing demonstration on any of them -- the trap
    ``docs/workload-lab/kubernetes.md`` records the plays encoding with
    ``failed_when: false`` rather than ``ignore_errors``.

    ``verify`` is honoured rather than assumed: a lab cluster's API server is often
    self-signed, and turning verification off is a decision the caller makes visibly
    rather than something buried here.
    """
    import ssl
    import urllib.error
    import urllib.request

    spec = PROBES.get(profile)
    if not spec:
        raise SystemExit(f"[agent] FATAL: no probe defined for profile {profile!r}; "
                         f"known: {', '.join(sorted(PROBES))}")
    ctx = None
    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    def _status(path: str) -> int:
        url = api_server.rstrip("/") + path
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:  # noqa: S310
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"[agent] FATAL: the API server at {api_server} did not "
                             f"answer: {exc}")

    fmt = {"ns": namespace, "other": other_namespace}
    allow_code = _status(spec["allow"].format(**fmt))
    deny_code = _status(spec["deny"].format(**fmt))
    allowed = allow_code == 200
    refused = deny_code == 403
    return {
        "profile": profile,
        "allow_path": spec["allow"].format(**fmt), "allow_status": allow_code,
        "deny_path": spec["deny"].format(**fmt), "deny_status": deny_code,
        "allowed": allowed, "refused": refused,
        "proved": allowed and refused,
        "says": spec["says"].format(**fmt),
    }


def probe_summary(result: dict) -> str:
    """One line for the journal. Says which half failed when one did, because "the probe
    failed" sends somebody to the wrong place."""
    if result.get("proved"):
        return f"scope proved — {result['says']}"
    if not result.get("allowed"):
        return (f"the allowed read returned {result['allow_status']} rather than 200 "
                f"({result['allow_path']}) — the token may be wrong or expired, so the "
                "refusal below proves nothing")
    return (f"THE REFUSAL DID NOT REFUSE: {result['deny_path']} returned "
            f"{result['deny_status']}, not 403. The token is broader than the profile "
            "claims, which is the one outcome this probe exists to catch")


# ── The certificate half: Secrets Safe, on the session already open ──────────
#
# A certificate identity is TWO objects and both are governed: the PKCS#12 passphrase is
# the managed account's credential, and the bundle it opens is a Secrets Safe FILE SECRET.
# Retrieving one without the other yields nothing usable, which is the point of the split.
#
# WHY NOT ps-cli, WHICH IS WHAT THE DASHBOARD USES. Because it cannot carry these bytes.
# docs/integrations/password-safe.md establishes it and services/secrets_backend_service
# REFUSES on it: the endpoint returns application/octet-stream faithfully, but every route
# ps-cli offers decodes the body to text before anyone sees it -- the library hands back
# `response.text`, and `raw` falls through its JSON parse to print `response.text` too. A
# PEM bundle is ASCII and survives. **A .pfx is corrupted rather than refused**, which is
# the worst of the three outcomes: the DER check below would fire and send somebody to
# look for a wrong bundle in Secrets Safe, when what is wrong is the transport.
#
# So the worker calls GET Secrets-Safe/Secrets/{id}/file/download itself and keeps the
# bytes -- which is what password-safe.md prescribes for exactly this case, naming this
# worker: "the way the agent worker already calls Requests and Credentials".
#
# AND IT COSTS NOTHING TO DO SO. Secrets Safe is part of Password Safe: the session opened
# for the passphrase reaches the bundle unchanged, so this is one sign-in for both halves
# rather than a second authentication in a second dialect. It also removes an unpinned pip
# package from the agent host, and removes the one place this cell put a credential into a
# subprocess environment -- a tension against its own rule against env vars that no longer
# has to be argued, because it is gone.

# `Secrets-Safe/Secrets` resolves by TITLE, or by PATH when the reference carries folders.
# The cert lab's references read `cert/<system>/<account>`, so a reference containing a
# separator is sent as a path, exactly as the `beyondtrust.secrets_safe` lookup resolves
# `folder/title`. UNVERIFIED AGAINST A TENANT, and the single place to change if a live
# one disagrees; the refusal below says which of the two lookups came back empty.
SECRETS_PATH_SEPARATOR = "/"


def _get_bytes(url: str, headers: dict, timeout: int = 60) -> bytes:
    """A GET whose body is BYTES rather than text.

    The one call in this worker that must not decode. Everything else here is JSON and
    goes through ``_request``; a PKCS#12 that went through ``_request`` would come back
    mangled by ``.decode("utf-8")`` in precisely the way ps-cli mangles it.
    """
    import urllib.request

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


def secrets_safe_file(title: str, *, dest_dir: str, base: str, headers: dict,
                      timeout: int = 60) -> str:
    """Download one Secrets Safe FILE secret into ``dest_dir``. Returns its path.

    Two calls, because the download endpoint takes an id and the operator knows a title:
    resolve, then fetch. ``base`` and ``headers`` are the session ``password_safe_episode``
    already opened -- Secrets Safe is part of Password Safe, so nothing is signed in twice.

    **The bundle lands on disk here, and that is the design rather than a slip.** The
    episode owns a single 0700 temporary directory: the bundle arrives in it, openssl
    opens it there, and the whole directory goes at the end. One guarded place beats a
    blob in memory that has to be written out for openssl anyway.
    """
    import urllib.error
    import urllib.parse

    key = "path" if SECRETS_PATH_SEPARATOR in title else "title"
    query = {key: title}
    if key == "path":
        query["separator"] = SECRETS_PATH_SEPARATOR
    lookup = base + "Secrets-Safe/Secrets?" + urllib.parse.urlencode(query)
    try:
        found = _get_json(lookup, headers, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"[agent] FATAL: Secrets Safe rejected the lookup for {title!r} "
                         f"by {key}: HTTP {exc.code}.")
    if isinstance(found, dict):
        found = [found]
    secret_id = ""
    for entry in found or []:
        if isinstance(entry, dict) and (entry.get("Id") or entry.get("id")):
            secret_id = str(entry.get("Id") or entry.get("id"))
            break
    if not secret_id:
        raise SystemExit(
            f"[agent] FATAL: Secrets Safe has no secret at {title!r} (looked up by "
            f"{key}). A reference containing {SECRETS_PATH_SEPARATOR!r} is treated as a "
            "folder path; one without it as a bare title.")

    dest = os.path.join(dest_dir, "bundle.pfx")
    # Written with 0600 ALREADY SET rather than chmod-ed afterwards: between the two there
    # is a window where the bundle is on disk at the umask's discretion.
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        download = base + f"Secrets-Safe/Secrets/{secret_id}/file/download"
        try:
            blob = _get_bytes(download, dict(headers, Accept="application/octet-stream"),
                              timeout=timeout)
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"[agent] FATAL: Secrets Safe would not release the bundle "
                             f"{title!r}: HTTP {exc.code}. The API registration needs "
                             "Secrets Safe read on the folder holding it.")
        os.write(fd, blob)
    finally:
        os.close(fd)

    # A PKCS#12 is DER: it starts with a SEQUENCE tag. Checking beats handing openssl
    # something that is not a bundle and reading its error as a passphrase problem --
    # which sends somebody to debug the wrong half of a two-half identity.
    if not blob.startswith(b"\x30"):
        hint = ""
        if blob.lstrip()[:5] == b"-----":
            hint = (" It looks like PEM, which is a perfectly good thing to keep in a "
                    "file secret — but this episode opens a PKCS#12, so the lab's "
                    "Change Password has to have written one.")
        raise SystemExit(
            f"[agent] FATAL: {title!r} downloaded, but it does not look like a PKCS#12 "
            f"bundle (DER starts 0x30).{hint}")
    return dest


def cert_mtls_probe(*, endpoint: str, bundle_path: str, passphrase: str,
                    expect_cn: str, work_dir: str, verify: bool = True,
                    timeout: int = 15) -> dict:
    """Present the client certificate to the lab's mTLS endpoint and read back the CN.

    Takes a PATH and a working directory rather than bytes, because the bundle already
    arrives as a file: ps-cli downloads it, openssl opens it, and Python's ``ssl`` needs
    file paths for a client certificate. Inventing a round trip through memory would add
    a copy of a credential and remove nothing.

    **The caller owns ``work_dir`` and must remove it.** One guarded 0700 directory for
    the whole episode — the bundle, the certificate and the key — is easier to reason
    about, and to clean up, than one per step.

    The passphrase reaches openssl through the ENVIRONMENT rather than argv, exactly as
    ``examples/playbooks/certificates/ci-fetch-cert.yml`` does it and for the same reason.
    """
    import ssl
    import subprocess as _sp
    import urllib.error
    import urllib.request

    crt = os.path.join(work_dir, "client.crt")
    key = os.path.join(work_dir, "client.key")
    env = dict(os.environ, PFXPASS=passphrase)
    for args, out_path in ((["-clcerts", "-nokeys"], crt),
                           (["-nocerts", "-nodes"], key)):
        r = _sp.run(["openssl", "pkcs12", "-in", bundle_path, "-passin", "env:PFXPASS",
                     *args, "-out", out_path],
                    capture_output=True, text=True, timeout=timeout,
                    stdin=_sp.DEVNULL, env=env)
        if r.returncode != 0:
            raise SystemExit(
                "[agent] FATAL: openssl could not open the bundle — the passphrase "
                "and the bundle are two halves of one identity, so this usually "
                f"means they are out of step: {scrub(r.stderr)[:300]}")
    os.chmod(key, 0o600)

    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    ctx.load_cert_chain(certfile=crt, keyfile=key)

    req = urllib.request.Request(endpoint, headers={"Accept": "text/plain"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310
            body = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        body, status = exc.read().decode("utf-8", "replace"), exc.code
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"[agent] FATAL: the mTLS endpoint {endpoint} did not "
                         f"answer: {scrub(str(exc))}")

    echoed = expect_cn and expect_cn in body
    return {"status": status, "expect_cn": expect_cn, "echoed": bool(echoed),
            "proved": status == 200 and bool(echoed),
            "body": body.strip()[:200]}


def cert_probe_summary(result: dict) -> str:
    """One line. Names which half failed, because "the probe failed" sends somebody to
    the wrong place."""
    if result.get("proved"):
        return (f"identity proved — the endpoint answered {result['status']} and echoed "
                f"{result['expect_cn']}")
    if result.get("status") != 200:
        return (f"the endpoint returned {result['status']}, not 200 — the certificate "
                "was not accepted, so nothing below proves anything")
    return (f"the endpoint answered 200 but did not echo {result['expect_cn']!r} — it is "
            "not seeing the certificate this agent presented")


# ── The cloud half: a dynamic secret, minted by the thing that spends it ─────
#
# THE ONE EPISODE HERE THAT NEVER TOUCHES PASSWORD SAFE, and the difference is the whole
# argument. The other two reach the vault: WC hands over the Password Safe client pair
# and the vault releases a credential it has always held. Here Workload Credentials is
# not a bootstrap for anything -- it MINTS, against a dynamic secret, and the credential
# did not exist a second before this worker asked for it.
#
# What that buys over a key in a CI secret store is the three properties
# docs/workload-lab/cloud.md names as the ones nobody chose: it expires, the issuance is
# recorded against THIS workload, and nothing sits on the host between runs.
#
# WHAT IT DOES NOT BUY IS SCOPE. `services/workload_cloud_service` states it plainly --
# "THE SCOPE IS NOT DEFINED HERE" -- because the dynamic secret's own definition in WC
# decides which role is assumed and what it may do. The dashboard cannot widen or narrow
# it and neither can this worker. So unlike `PROBES` above, whose allow/deny pair is
# derived from a profile the dashboard chose, the deny probe here is an ASSERTION THE
# OPERATOR MAKES about a role this code cannot see. `cloud_probe_summary` says so.
#
# AND IT COSTS MONEY. WC bills per issuance. One `generate` per run of this episode, and
# nothing retries it -- a worker looping on this is a cost problem before it is an audit
# one.

# Which clouds can release a lease before it expires. Restated rather than imported,
# because this file takes no dashboard dependency; pinned equal to
# `services/workload_cloud_service._REVOCABLE_CLOUDS` by test.
#
# AWS is absent and that is a provider fact: STS will not withdraw a credential it has
# already signed, so there the TTL is the only control there is.
_REVOCABLE_CLOUDS = frozenset({"azure"})

# What a mint hands back per cloud, as field NAMES. The cloud is DERIVED from this shape
# rather than taken as a flag -- a flag could disagree with the payload, and a run that
# signed an Azure secret as an AWS key would fail as a signature error, which is the one
# failure this episode must never confuse with a refusal.
_CREDENTIAL_SHAPE = {
    "aws": ("access_key_id", "secret_access_key", "session_token"),
    "azure": ("client_id", "client_secret", "tenant_id"),
}


def cloud_revocable(cloud: str) -> bool:
    """Whether this cloud's leases can be released early. See `_REVOCABLE_CLOUDS`."""
    return (cloud or "").strip().lower() in _REVOCABLE_CLOUDS


def _wlc_first(mapping: dict, *names: str):
    """First present, non-empty value among ``names``. Mirrors
    ``workload_credentials_service._first``."""
    for name in names:
        val = mapping.get(name)
        if val not in (None, ""):
            return val
    return None


def _parse_expiry_epoch(value) -> float:
    """A lease expiry as a UNIX epoch, or 0.0 if unreadable.

    The same ISO forms ``workload_credentials_service.parse_expiration`` accepts,
    including a trailing ``Z``. Returns 0.0 rather than raising, and the caller treats
    that as "no expiry to wait for" -- which is a refusal, not a pass, because an episode
    whose closing beat is expiry cannot prove anything without knowing when that is.
    """
    if value in (None, ""):
        return 0.0
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def parse_generated_payload(payload) -> dict:
    """Normalise a ``generate`` response. A MIRROR of
    ``services/workload_credentials_service.parse_generated``, never an import.

    Restated for the same reason `read_wlc_secret` restates the path grammar: this file
    runs on somebody else's VM and takes no dashboard dependency. A test pins the two
    against a shared fixture table, which is what keeps a mirror from drifting into a
    guess.

    Both of the upstream tolerances are reproduced deliberately, because they come from
    BeyondTrust's own GitHub Action rather than from taste: field names arrive in
    **camelCase or PascalCase**, and ``leaseId``/``expiration`` may sit on the ``secret``
    object or at the response root. The published docs disagree with each other on both.

    Returns ``{"values", "lease_id", "expires_at", "expires_epoch", "cloud"}``.
    """
    if not isinstance(payload, dict):
        raise SystemExit("[agent] FATAL: Workload Credentials returned "
                         f"{type(payload).__name__} from generate, expected a JSON object.")

    secret = payload.get("secret")
    if not isinstance(secret, dict):
        # Some shapes put the credential at the root. Accept that, but only when it looks
        # like a credential -- otherwise the refusal below says far more than a dict of
        # metadata masquerading as one would.
        secret = payload if any(
            k in payload for k in ("accessKeyId", "AccessKeyId", "clientId", "ClientId")
        ) else {}

    lease_id = (_wlc_first(secret, "leaseId", "LeaseId")
                or _wlc_first(payload, "leaseId", "LeaseId"))
    expiration = (_wlc_first(secret, "expiration", "Expiration")
                  or _wlc_first(payload, "expiration", "Expiration"))

    access_key = _wlc_first(secret, "accessKeyId", "AccessKeyId")
    client_id = _wlc_first(secret, "clientId", "ClientId")

    if access_key:
        cloud = "aws"
        values = {
            "access_key_id": access_key,
            "secret_access_key": _wlc_first(secret, "secretAccessKey", "SecretAccessKey"),
            "session_token": _wlc_first(secret, "sessionToken", "SessionToken"),
        }
        optional = ()
    elif client_id:
        cloud = "azure"
        values = {
            "client_id": client_id,
            "client_secret": _wlc_first(secret, "clientSecret", "ClientSecret"),
            "tenant_id": _wlc_first(secret, "tenantId", "TenantId"),
            "key_id": _wlc_first(secret, "keyId", "KeyId"),
        }
        # key_id only correlates a revoke; absence is not a failure.
        optional = ("key_id",)
    else:
        # KEYS ONLY, never values -- the same rule the upstream refusal follows, and the
        # one that makes this message safe to print.
        raise SystemExit("[agent] FATAL: the generate response contained no recognised "
                         f"credential fields (saw: {', '.join(sorted(secret)) or 'nothing'}).")

    absent = [k for k, v in values.items() if v in (None, "") and k not in optional]
    if absent:
        raise SystemExit("[agent] FATAL: the generate response is missing "
                         + ", ".join(absent))

    return {"values": values, "lease_id": str(lease_id) if lease_id else "",
            "expires_at": str(expiration or ""),
            "expires_epoch": _parse_expiry_epoch(expiration), "cloud": cloud}


def generate_wlc_credential(*, base_url: str, site_id: str, service_name: str,
                            dynamic_name: str, identity_token: str,
                            folder: str = "") -> dict:
    """Mint one credential from a dynamic secret. **THIS IS THE METERED CALL.**

    The same chain `read_wlc_secret` documents -- the platform vouches for this machine,
    WC accepts that in place of a PAT, ``X-BT-Service-Name`` says which registered
    Workload Identity it satisfies -- pointed at ``/dynamic/{name}/generate`` instead of
    a static read. So the host still holds nothing, and what comes back is a credential
    with an expiry rather than a copy of a standing one.

    Exactly one caller, called exactly once per episode, and nothing retries it. A retry
    here is a second charge for one demonstration; if WC is unreachable the honest answer
    is to fail and say so.
    """
    import urllib.error
    from urllib.parse import quote, urlencode

    path = (f"/site/{quote(site_id)}/secrets/dynamic/{quote(dynamic_name)}/generate")
    url = base_url.rstrip("/") + path
    if folder:
        url += "?" + urlencode({"folder": folder})
    try:
        payload = _post_json(url, {
            "Authorization": f"Bearer {identity_token}",
            "X-BT-Service-Name": service_name,
            "bt-secrets-api-version": WLC_API_VERSION,
        }, body={})
    except urllib.error.HTTPError as exc:
        # The status and the secret's NAME. The body is scrubbed and truncated rather
        # than trusted: it is a provider's error text, and the habit of assuming one
        # carries nothing sensitive is how a credential ends up in a log line.
        detail = scrub((exc.read() or b"").decode("utf-8", "replace"))[:300]
        raise SystemExit(
            f"[agent] FATAL: Workload Credentials refused to mint from dynamic secret "
            f"{dynamic_name!r} ({exc.code}). {detail}") from None
    return parse_generated_payload(payload)


def revoke_wlc_lease(*, base_url: str, site_id: str, service_name: str,
                     lease_id: str, identity_token: str) -> tuple:
    """Release a lease early. Returns ``(released, detail)``.

    **It does NOT swallow ``lease_not_revocable``, and that is the deliberate divergence
    from ``workload_credentials_service.revoke_lease``.** The dashboard's client swallows
    that refusal because its callers "revoke unconditionally and let the provider
    decide", which is right for housekeeping. Here the refusal IS the finding: an
    episode that reported a release AWS never performed would tell a room a live
    credential had been withdrawn.
    """
    import urllib.error
    from urllib.parse import quote

    if not lease_id:
        return False, "no lease id was returned, so there is nothing to release"
    url = (base_url.rstrip("/")
           + f"/site/{quote(site_id)}/secrets/leases/id/{quote(lease_id)}")
    try:
        _request(url, {"Authorization": f"Bearer {identity_token}",
                       "X-BT-Service-Name": service_name,
                       "bt-secrets-api-version": WLC_API_VERSION}, method="DELETE")
    except urllib.error.HTTPError as exc:
        detail = scrub((exc.read() or b"").decode("utf-8", "replace"))[:300]
        if "not_revocable" in detail:
            return False, ("the provider refuses to revoke this lease — STS will not "
                           "withdraw a credential it has already signed, so the TTL is "
                           "the only control there is")
        return False, f"the release failed ({exc.code}). {detail}"
    return True, "the lease was released"


# ── Proving a cloud credential is SCOPED, not merely that it authenticated ───
#
# The same two beats `PROBES` above runs against a cluster, and for the same stated
# reason: docs/workload-lab/kubernetes.md is explicit that THE REFUSALS are the steps
# that prove something. One call the credential must be able to make, one it must be
# refused — and the refusal written as an assertion rather than a runbook step, because
# a step gets skipped and an assertion does not.
#
# NO `aws` OR `az` SHELL-OUT, and this is the same rule `_request` states for HTTP
# libraries: every dependency is something the install play has to put on somebody
# else's VM. `openssl` and `spire-agent` survive that test because both are already on
# the host by construction; the AWS CLI is a ~60 MB install on every agent host and the
# Azure CLI brings a Python of its own. The shipped cloud PLAY can use them because it
# runs on the dashboard's runner image, which is a different machine with a different
# budget. So: a hand-rolled SigV4 signer for AWS, plain OAuth2 for Azure, stdlib only.
#
# THE ORDER IS THE SIGNER'S SAFETY NET. A hand-rolled signature that is wrong fails as
# HTTP 403, which is exactly what a successful refusal looks like. Three defences, and
# all three are needed:
#
#   1. the ALLOW beat runs first, so a broken signer ends the episode saying the
#      allowed call failed rather than being read as a proof of scope;
#   2. the deny beat requires a SPECIFIC error code, not merely a 403 — see
#      `AWS_BROKEN_CODES`, which are the 403s that mean the credential or the signature
#      is wrong;
#   3. the signer is pinned to AWS's published test vectors by test.

# A refusal that proves scope.
AWS_DENY_CODES = ("AccessDenied", "UnauthorizedOperation", "AccessDeniedException")
# A refusal that proves the credential or the signature is broken. Reported as its own
# outcome: it is a 403 like the one above and means the opposite thing.
AWS_BROKEN_CODES = ("ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId",
                    "SignatureDoesNotMatch", "TokenRefreshRequired",
                    "IncompleteSignature", "InvalidSignatureException")
# The Azure equivalents. A Graph 403 with this code is the role being absent, which is
# the refusal; a 401 is the token being wrong, which proves nothing.
AZURE_DENY_CODES = ("Authorization_RequestDenied", "AuthorizationFailed",
                    "InsufficientPrivileges")

# The deny probes on offer. Each is an ASSERTION ABOUT THE DYNAMIC SECRET'S ROLE that
# the operator is choosing to make -- this worker cannot read that role's policy, so it
# cannot pick for them. The defaults are the broadest safe bet per cloud.
CLOUD_DENY_PROBES = {
    # An ARM-scoped dynamic secret has no IAM write. `iam:ListUsers` is a read, so a
    # refusal here is a scope statement rather than a lucky guard-rail.
    "iam-list-users": "an AWS role scoped to its workload cannot enumerate IAM users",
    # The Azure analogue of "`view` omits Secrets by design": a service principal issued
    # for ARM has no Graph application permissions unless somebody granted them. The
    # token still ISSUES -- it just carries no roles -- so the failure lands at the API
    # with a crisp code instead of at the token endpoint with a vague one.
    "graph-directory-read": ("an ARM-scoped service principal cannot read the Entra "
                             "directory"),
    "none": "",
}
_DEFAULT_DENY_PROBE = {"aws": "iam-list-users", "azure": "graph-directory-read"}


def _sigv4_signing_key(secret_key: str, date: str, region: str, service: str) -> bytes:
    """The four-step derived key. Split out because it is the ONE part of this signer
    with a published AWS test vector, and pinning it is worth a function.

    ``20120215``/``us-east-1``/``iam`` against the documented example secret derives
    ``f4780e2d…db404d``; ``tests/test_agentcell_cloud_episode`` asserts exactly that.
    """
    import hashlib
    import hmac

    def _sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _sign(("AWS4" + secret_key).encode("utf-8"), date)
    return _sign(_sign(_sign(k_date, region), service), "aws4_request")


def _sigv4_canonical(*, method: str, host: str, stamp: str, body: bytes,
                     session_token: str = "") -> tuple:
    """The canonical request and its signed-header list. Returns ``(text, headers)``.

    Split out so a test can assert the exact bytes rather than only the signature they
    produce: a canonical request that is subtly wrong signs perfectly and is rejected as
    ``SignatureDoesNotMatch``, which on this path looks exactly like a refusal.
    """
    import hashlib

    signed = "content-type;host;x-amz-date"
    canonical_headers = ("content-type:application/x-www-form-urlencoded; charset=utf-8\n"
                         f"host:{host}\n"
                         f"x-amz-date:{stamp}\n")
    if session_token:
        # The security token is part of the SIGNATURE, not an extra sent beside it.
        signed = "content-type;host;x-amz-date;x-amz-security-token"
        canonical_headers += f"x-amz-security-token:{session_token}\n"
    text = "\n".join([method, "/", "", canonical_headers, signed,
                      hashlib.sha256(body).hexdigest()])
    return text, signed


def _sigv4_headers(*, method: str, host: str, region: str, service: str, body: bytes,
                   values: dict, now=None) -> dict:
    """AWS Signature Version 4, by hand, for one query-protocol POST.

    Short because that is all SigV4 is: a canonical request, a string to sign, a
    four-step derived key, and a header. Nothing here is clever and nothing here should
    be — a signer that is subtly wrong is rejected as ``SignatureDoesNotMatch``, which on
    this path is a 403 that looks exactly like the refusal the episode is trying to
    prove. Hence `AWS_BROKEN_CODES`, hence the allow beat running first, and hence the
    two halves above being separately testable.

    **Only the shape this episode needs**: a query-protocol POST to ``/`` with a body
    that is always present, no query string, and the three (or four) headers AWS
    requires in the signature.
    """
    import hashlib
    import hmac

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    date = stamp[:8]
    token = values.get("session_token") or ""

    canonical_request, signed_headers = _sigv4_canonical(
        method=method, host=host, stamp=stamp, body=body, session_token=token)
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope,
                         hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
    signing_key = _sigv4_signing_key(values["secret_access_key"], date, region, service)
    signature = hmac.new(signing_key, to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()

    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        "Host": host,
        "X-Amz-Date": stamp,
        "Authorization": (f"AWS4-HMAC-SHA256 Credential={values['access_key_id']}/{scope}, "
                          f"SignedHeaders={signed_headers}, Signature={signature}"),
    }
    if token:
        headers["X-Amz-Security-Token"] = token
    return headers


def _xml_field(text: str, tag: str) -> str:
    """One element's text out of an AWS query-protocol response.

    A regex rather than an XML parser, and rather than asking for JSON with an Accept
    header: the query protocol's honouring of that header is not something this repo can
    verify against a live endpoint, and a probe that silently got XML while expecting
    JSON would report "no error code" on a perfectly good refusal.
    """
    found = re.search(rf"<{tag}>([^<]+)</{tag}>", text or "")
    return found.group(1).strip() if found else ""


def _aws_query_call(*, host: str, service: str, region: str, action: str,
                    version: str, values: dict, verify: bool = True,
                    timeout: int = 15) -> dict:
    """One signed query-protocol call. Returns ``{status, code, arn, body}``.

    Never raises on an HTTP error: a refusal IS the expected outcome of half the calls
    here, so the status and the provider's error code are data rather than exceptions.

    **A TRANSPORT failure is a different thing and does raise**, following `k8s_probe`'s
    rule rather than letting a `URLError` out as a traceback. The distinction matters
    more here than there: by the time a probe runs, a credential has been minted and
    billed, so the one outcome to avoid is a stack trace that never mentions either.
    """
    import ssl
    import urllib.error
    import urllib.request

    body = f"Action={action}&Version={version}".encode("utf-8")
    headers = _sigv4_headers(method="POST", host=host, region=region, service=service,
                             body=body, values=values)
    ctx = None
    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(f"https://{host}/", headers=headers, data=body,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310
            text = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        text = (exc.read() or b"").decode("utf-8", "replace")
        status = exc.code
    except Exception as exc:                            # noqa: BLE001 — see the docstring
        raise SystemExit(
            f"[agent] FATAL: {host} could not be reached ({type(exc).__name__}). The "
            "credential was already minted and billed, and it is live until it expires "
            "— check this host's egress to that endpoint.") from None
    return {"status": status, "code": _xml_field(text, "Code"),
            "arn": _xml_field(text, "Arn"), "body": scrub(text)[:400]}


def aws_cloud_probe(*, values: dict, deny_probe: str, region: str = "us-east-1",
                    verify: bool = True) -> dict:
    """One allow beat and one deny beat against AWS.

    **The allow beat is `sts:GetCallerIdentity`, and it proves less than it looks like it
    does.** IAM cannot deny that call, so a 200 proves the credential authenticates and
    names the role the dynamic secret assumed -- the same ARN
    `examples/playbooks/cloud/ci-run-with-dynamic-creds.yml` prints -- and proves nothing
    whatsoever about scope. It runs first anyway, because that is what tells a broken
    signer apart from a real refusal.
    """
    result = {"cloud": "aws", "deny_probe": deny_probe, "proved": False,
              "says": CLOUD_DENY_PROBES.get(deny_probe, "")}
    allow = _aws_query_call(host="sts.amazonaws.com", service="sts", region=region,
                            action="GetCallerIdentity", version="2011-06-15",
                            values=values, verify=verify)
    result["allow_status"] = allow["status"]
    result["allow_code"] = allow["code"]
    result["identity"] = allow["arn"]
    if allow["status"] != 200:
        return result
    result["authenticated"] = True
    if deny_probe == "none":
        return result

    deny = _aws_query_call(host="iam.amazonaws.com", service="iam", region=region,
                           action="ListUsers", version="2010-05-08", values=values,
                           verify=verify)
    result["deny_status"] = deny["status"]
    result["deny_code"] = deny["code"]
    if deny["status"] == 200:
        return result
    if deny["code"] in AWS_BROKEN_CODES:
        result["broken"] = True
        return result
    result["proved"] = deny["code"] in AWS_DENY_CODES
    return result


def _entra_token(*, values: dict, scope: str, verify: bool = True,
                 timeout: int = 15) -> tuple:
    """A client-credentials access token for one resource. Returns ``(token, error)``.

    Fresh every call, and never cached. That matters exactly once -- after a lease is
    released -- where reusing a token issued before the release would prove the release
    did nothing, which is both wrong and indistinguishable from the truth.
    """
    import ssl
    import urllib.error
    import urllib.request
    from urllib.parse import urlencode

    url = (f"https://login.microsoftonline.com/{values['tenant_id']}"
           "/oauth2/v2.0/token")
    form = urlencode({"grant_type": "client_credentials",
                      "client_id": values["client_id"],
                      "client_secret": values["client_secret"],
                      "scope": scope}).encode("utf-8")
    ctx = None
    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        url, data=form, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        text = (exc.read() or b"").decode("utf-8", "replace")
        # The error CODE, never the description: Entra's description echoes the request
        # back, and the request carries the client secret.
        try:
            code = (json.loads(text).get("error") or "") or f"http_{exc.code}"
        except ValueError:
            code = f"http_{exc.code}"
        return "", code
    except Exception as exc:                            # noqa: BLE001
        # Transport, not authorisation. Returning "" here would make an unreachable
        # Entra look like a principal with no roles, which is the refusal the Graph
        # beat exists to detect — a network problem reported as proof of scope.
        raise SystemExit(
            f"[agent] FATAL: login.microsoftonline.com could not be reached "
            f"({type(exc).__name__}). The credential was already minted and billed, "
            "and it is live until its lease expires.") from None
    return payload.get("access_token") or "", ""


def _arm_get(url: str, token: str, verify: bool = True, timeout: int = 15) -> dict:
    """One bearer GET against an Azure endpoint. Returns ``{status, code, body}``."""
    import ssl
    import urllib.error
    import urllib.request

    ctx = None
    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310
            text = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        text = (exc.read() or b"").decode("utf-8", "replace")
        status = exc.code
    except Exception as exc:                            # noqa: BLE001
        raise SystemExit(
            f"[agent] FATAL: {url.split('/')[2]} could not be reached "
            f"({type(exc).__name__}). The credential was already minted and billed, "
            "and it is live until its lease expires.") from None
    code, data = "", {}
    try:
        parsed = json.loads(text)
        data = parsed if isinstance(parsed, dict) else {}
        err = data.get("error") or {}
        code = err.get("code") if isinstance(err, dict) else str(err)
    except ValueError:
        pass
    # `data` is the PARSED body and `body` is a scrubbed, truncated copy for a message.
    # Parsing the truncated one is how a caller ends up reporting "no display name" on a
    # response that had one — it was simply cut off at 400 characters.
    return {"status": status, "code": code or "", "data": data,
            "body": scrub(text)[:400]}


def azure_cloud_probe(*, values: dict, scope: str, deny_probe: str,
                      verify: bool = True) -> dict:
    """One allow beat and one deny beat against Azure.

    Two deny probes were considered and rejected, and the reasons are worth keeping:
    reading **another subscription** returns 404 ``SubscriptionNotFound`` for one you
    cannot see, which is indistinguishable from a typo; and anything **write-shaped** --
    even a validate-only deployment -- is still a call that can change state, which is
    not a thing to run in somebody's tenant to make a point.
    """
    result = {"cloud": "azure", "deny_probe": deny_probe, "proved": False,
              "says": CLOUD_DENY_PROBES.get(deny_probe, "")}
    token, error = _entra_token(values=values, scope="https://management.azure.com/.default",
                                verify=verify)
    if not token:
        result["allow_status"] = 0
        result["allow_code"] = error or "no_token"
        return result
    allow = _arm_get(
        f"https://management.azure.com/subscriptions/{scope}?api-version=2022-12-01",
        token, verify=verify)
    result["allow_status"] = allow["status"]
    result["allow_code"] = allow["code"]
    if allow["status"] != 200:
        return result
    result["identity"] = allow["data"].get("displayName") or ""
    result["authenticated"] = True
    if deny_probe == "none":
        return result

    graph, graph_error = _entra_token(values=values,
                                      scope="https://graph.microsoft.com/.default",
                                      verify=verify)
    if not graph:
        # No token at all is not a refusal by Graph — it is Entra declining to issue,
        # which says nothing about what this principal may read.
        result["deny_status"] = 0
        result["deny_code"] = graph_error or "no_token"
        result["broken"] = True
        return result
    deny = _arm_get("https://graph.microsoft.com/v1.0/users?$top=1", graph, verify=verify)
    result["deny_status"] = deny["status"]
    result["deny_code"] = deny["code"]
    if deny["status"] == 200:
        return result
    if deny["status"] == 401:
        # The token is the problem, not the authorisation. Same category as AWS's
        # `SignatureDoesNotMatch`: a refusal that refused for the wrong reason.
        result["broken"] = True
        return result
    result["proved"] = (deny["status"] == 403
                        and deny["code"] in AZURE_DENY_CODES)
    return result


def cloud_probe_summary(result: dict) -> str:
    """One line, and FOUR outcomes rather than the three its siblings have.

    The fourth is the one this family did not need before: a refusal that refused for the
    wrong reason. `probe_summary` can assume a 403 from an API server means authorisation,
    because nothing else about that request is being computed here. A hand-rolled
    signature can be wrong, and when it is, AWS says 403 — so "it failed" and "it was
    refused" stop being the same sentence.
    """
    cloud = result.get("cloud", "cloud")
    if not result.get("authenticated"):
        return (f"the allowed {cloud} call returned "
                f"{result.get('allow_status')}/{result.get('allow_code') or 'no code'} — "
                "the credential may be wrong, already expired, or unreachable, so "
                "anything below it proves nothing")
    who = result.get("identity") or "an unnamed principal"
    if result.get("deny_probe") == "none":
        return (f"the credential authenticated as {who} — and that is ALL this run "
                "proves. No deny probe was asked for, so nothing here says the "
                "credential is scoped")
    if result.get("broken"):
        return (f"authenticated as {who}, but the refusal refused for the WRONG REASON "
                f"({result.get('deny_code') or result.get('deny_status')}) — that is a "
                "broken credential or a bad signature, not a scoped one, and it proves "
                "nothing")
    if result.get("proved"):
        return (f"scope proved — authenticated as {who}, and "
                f"{result.get('says') or 'the asserted limit held'} "
                f"({result.get('deny_code')})")
    return (f"THE REFUSAL DID NOT REFUSE: authenticated as {who}, and "
            f"{result.get('deny_probe')} returned {result.get('deny_status')}"
            f"/{result.get('deny_code') or 'no error'} rather than a denial. This "
            "credential is broader than the dynamic secret's definition was assumed to "
            "be, which is the one outcome this probe exists to catch")


def cloud_ending_problem(prove_ending: bool, cloud: str) -> str:
    """Refuse a run that would print a proved-looking line without proving the ending.

    The pure analogue of `approval_problem`, one level over. That function exists because
    an ungated fetch prints a line indistinguishable from an approved one; this one
    exists because a run that mints, proves the scope and stops prints a line
    indistinguishable from a full run — on a demo whose entire argument is that the
    credential dies.
    """
    if prove_ending:
        return ""
    cost = ("a real wait of up to an hour, because STS caps a role-chained credential "
            "there and will not withdraw one it has signed"
            if not cloud_revocable(cloud)
            else "a release and a re-probe, which is quick")
    return (f"--no-prove-ending was passed, so this run stops after proving the scope "
            f"and never shows the credential dying — which is the argument. Proving the "
            f"ending on {cloud} costs {cost}.")


def fetch_spiffe_id(socket_path: str) -> str:
    """The worker's own SPIFFE ID, from the SPIRE agent's workload API.

    Shells out to `spire-agent` rather than taking a Python SPIFFE dependency: the binary
    is already on any host running an agent, and the demo host is one by construction.

    Re-fetched every loop instead of cached at startup. That is the property worth
    showing -- an attested workload holds nothing, so if its registration entry is
    deleted the next fetch fails and the line says so.
    """
    try:
        out = subprocess.run(
            ["spire-agent", "api", "fetch", "x509", "-socketPath", socket_path],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        return "no-spire-agent-binary"
    except subprocess.TimeoutExpired:
        return "spire-agent-timeout"
    if out.returncode != 0:
        return "unattested"
    m = re.search(r"SPIFFE ID:\s*(spiffe://\S+)", out.stdout)
    return m.group(1) if m else "unattested"


async def call_once(url: str, token: str, tool: str) -> dict:
    """One MCP tool call. Raises on transport or auth failure."""
    # Imported here so --selftest runs on a host without the mcp client installed.
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    headers = {"Authorization": f"Bearer {token}"}
    async with sse_client(url, headers=headers) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            result = await session.call_tool(tool, {})
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            return json.loads(text)
        except ValueError:
            return {"text": text}
    return {}


def summarise(payload: dict) -> str:
    """One clause about what the call returned. Deliberately shallow: the demo is the
    governance around the call, not the data."""
    if not isinstance(payload, dict) or not payload:
        return "no payload"
    # The keys dashboard_summary actually returns (api/mcp_server.py): active_jobs,
    # failed_today, total_jobs, scope, features. Matched by name rather than guessed,
    # and falling through to a generic rendering if the tool's shape ever changes.
    if "active_jobs" in payload:
        return (f"{payload['active_jobs']} active jobs, "
                f"{payload.get('failed_today', '?')} failed today")
    return ", ".join(f"{k}={v}" for k, v in list(payload.items())[:3])


# What each source leaves behind, said on the first line of every run. The demo turns on
# this distinction, so it is stated rather than implied -- and `ps` and `wlc` differ in
# where the credential came FROM, not in what is left here, which is why they read alike.
SOURCE_HOLDS = {
    "file": "a static secret on this host",
    "wlc": "nothing on this host",
    "ps": "nothing on this host",
}


def run(url: str, token: str, tool: str, socket_path: str, interval: int,
        token_source: str = "file", label: str = "") -> int:
    """The loop. `token` is spent here and never printed -- see `token_label`."""
    named = token_label(label)
    held = SOURCE_HOLDS.get(token_source, "a static secret on this host")
    print(f"[agent] polling {url} every {interval}s as {named} "
          f"(token from {token_source}: {held})", flush=True)
    while True:
        spiffe_id = fetch_spiffe_id(socket_path)
        try:
            payload = asyncio.run(call_once(url, token, tool))
        except Exception as exc:  # noqa: BLE001
            text = scrub(str(exc))
            # 401 is the demo's closing beat, not an error to ride out.
            if "401" in text or "Unauthorized" in text or "unauthorized" in text:
                print(f"[agent] {spiffe_id} · token {named} · REFUSED — the token "
                      f"is revoked or expired · {_now()}", flush=True)
                print("[agent] stopping: the identity is still valid, the authorization "
                      "is not.", flush=True)
                return 2
            print(f"[agent] {spiffe_id} · token {named} · call failed: {text} · "
                  f"{_now()}", flush=True)
            time.sleep(interval)
            continue
        print(f"[agent] {spiffe_id} · token {named} · {summarise(payload)} · "
              f"{_now()}", flush=True)
        time.sleep(interval)


def run_k8s_episode(args) -> int:
    """One bounded, approval-gated cluster-access episode.

    The whole beat, in order, with every step on its own line because the point of this
    is that somebody can watch it happen:

      1. the platform vouches for this machine, Workload Credentials hands over the
         Password Safe client pair -- nothing is stored here to make either happen;
      2. a request is opened against the linked account, naming this agent;
      3. **it waits**, and says so every poll. It cannot approve its own request;
      4. on release, two reads prove the token is SCOPED, not merely that it works;
      5. the slot goes back.

    Exit codes are the demo's punctuation: 0 proved it, 3 was never approved, 4 means a
    refusal did not refuse. That last one is a failure worth its own code -- it is the
    outcome that would otherwise look like success.
    """
    missing = [n for n, v in (("--k8s-api-server", args.k8s_api_server),
                              ("--k8s-profile", args.k8s_profile),
                              ("--k8s-namespace", args.k8s_namespace),
                              ("--k8s-account-id", args.k8s_account_id),
                              ("--wlc-base-url", args.wlc_base_url),
                              ("--wlc-site-id", args.wlc_site_id),
                              ("--wlc-service-name", args.wlc_service_name),
                              ("--wlc-resource", args.wlc_resource),
                              ("--ps-client-id-secret", args.ps_client_id_secret),
                              ("--ps-client-secret-secret", args.ps_client_secret_secret),
                              ("--ps-api-url", args.ps_api_url)) if not v]
    if missing:
        raise SystemExit("[agent] FATAL: --k8s-episode needs " + ", ".join(missing))

    spiffe_id = fetch_spiffe_id(args.spiffe_socket)
    reason = f"mcp-agent cluster access — {spiffe_id}"
    print(f"[agent] {spiffe_id} · requesting {args.k8s_profile} access to "
          f"{args.k8s_api_server} · {_now()}", flush=True)

    # The client pair, against this machine's own identity. Nothing static on this host.
    identity = fetch_identity_token(args.wlc_resource, args.identity_platform,
                                    args.wlc_client_id, args.identity_token_file,
                                    args.spiffe_socket)
    wlc = dict(base_url=args.wlc_base_url, site_id=args.wlc_site_id,
               service_name=args.wlc_service_name, identity_token=identity,
               folder=args.wlc_folder)
    ps_client_id = read_wlc_secret(secret_name=args.ps_client_id_secret, **wlc)
    ps_client_secret = read_wlc_secret(secret_name=args.ps_client_secret_secret, **wlc)
    print("[agent] holding nothing: the Password Safe client pair came from Workload "
          f"Credentials against this machine's own identity · {_now()}", flush=True)

    def _waiting(elapsed: int) -> None:
        print(f"[agent] {spiffe_id} · WAITING for approval ({elapsed}s) — this agent "
              f"cannot authorise its own access · {_now()}", flush=True)

    token, base, headers, request_id, polls = password_safe_episode(
        api_url=args.ps_api_url, client_id=ps_client_id,
        client_secret=ps_client_secret, account_id=args.k8s_account_id,
        system_id=args.k8s_system_id, duration_min=args.k8s_duration,
        reason=reason, max_wait_seconds=args.k8s_max_wait, on_wait=_waiting)

    # THE REQUEST ID IS NOT LOGGED, and this is the second time this repo has reached
    # that conclusion -- `ps_api_service._checkin` carries the same note, "never log the
    # request id (CodeQL taints it)". The taint is real rather than pedantic: the id
    # comes back from the same call as the credential, so an analyser cannot tell them
    # apart and neither, at a glance, can a reader.
    #
    # Nothing is lost. The handle that correlates this with the Password Safe audit row
    # is the SPIFFE ID, which travels in the request's own `reason` and which Password
    # Safe records -- so the log names what the other system shows, rather than an
    # internal id only this process can see. Exactly the lesson the PAT hint taught.
    if token:
        # This episode's page claims the agent "cannot authorise its own access". On an
        # auto-releasing policy that claim is false, and nothing would have said so.
        problem = approval_problem(polls, args.require_approval)
        if problem:
            _checkin(base, headers, request_id, reason)
            print(f"[agent] {spiffe_id} · REFUSING: {problem} · {_now()}", flush=True)
            return 5

    if not token:
        print(f"[agent] {spiffe_id} · the request was never approved within "
              f"{args.k8s_max_wait}s — the slot has been given back · {_now()}",
              flush=True)
        print("[agent] stopping: no approval, no access. That is the mechanism working.",
              flush=True)
        return 3

    try:
        print(f"[agent] {spiffe_id} · approved — a token was released · {_now()}",
              flush=True)
        result = k8s_probe(api_server=args.k8s_api_server, token=token,
                           profile=args.k8s_profile, namespace=args.k8s_namespace,
                           other_namespace=args.k8s_other_namespace,
                           verify=not args.k8s_insecure)
        print(f"[agent] {spiffe_id} · {probe_summary(result)} · {_now()}", flush=True)
    finally:
        _checkin(base, headers, request_id, reason)
        print(f"[agent] {spiffe_id} · the request was checked back in · {_now()}",
              flush=True)
        print("[agent] note: the check-in returns the slot. A token already released "
              "lives out its TTL — rotation does not revoke it, and only deleting the "
              "ServiceAccount does.", flush=True)
    return 0 if result.get("proved") else 4


def run_cert_episode(args) -> int:
    """One certificate episode, and the THIRD control surface this cell demonstrates.

    The arc matters more than this episode does:

      * the **PAT** is revocable — pull it and the loop stops mid-poll;
      * the **cluster token** is gated at retrieval — a person decides, and once released
        it lives out its TTL;
      * a **certificate** is neither. docs/workload-lab/certificates.md is blunt about it:
        "No revocation checking. The plugin consults neither CRLs nor OCSP. Short
        lifetimes are the mitigation, and that is a deliberate design position."

    So the uncomfortable demo is the point. Disable the managed account -- which revokes
    the certificate on a backend that can -- and run this again. **It still works.**
    Nothing on this path checks. The agent stops when the certificate EXPIRES, not when
    somebody takes it away, and showing the mechanism that does not stop on demand is
    what makes the two that do worth having.

    Exit codes: 0 proved the identity, 3 the passphrase was never released, 4 the
    endpoint did not accept or echo the certificate.
    """
    missing = [n for n, v in (("--cert-endpoint", args.cert_endpoint),
                              ("--cert-cn", args.cert_cn),
                              ("--cert-bundle-title", args.cert_bundle_title),
                              ("--cert-account-id", args.cert_account_id),
                              ("--wlc-base-url", args.wlc_base_url),
                              ("--wlc-site-id", args.wlc_site_id),
                              ("--wlc-service-name", args.wlc_service_name),
                              ("--wlc-resource", args.wlc_resource),
                              ("--ps-client-id-secret", args.ps_client_id_secret),
                              ("--ps-client-secret-secret", args.ps_client_secret_secret),
                              ("--ps-api-url", args.ps_api_url)) if not v]
    if missing:
        raise SystemExit("[agent] FATAL: --cert-episode needs " + ", ".join(missing))

    spiffe_id = fetch_spiffe_id(args.spiffe_socket)
    reason = f"mcp-agent certificate use — {spiffe_id}"
    print(f"[agent] {spiffe_id} · requesting the certificate identity behind "
          f"{args.cert_cn} · {_now()}", flush=True)

    identity = fetch_identity_token(args.wlc_resource, args.identity_platform,
                                    args.wlc_client_id, args.identity_token_file,
                                    args.spiffe_socket)
    wlc = dict(base_url=args.wlc_base_url, site_id=args.wlc_site_id,
               service_name=args.wlc_service_name, identity_token=identity,
               folder=args.wlc_folder)
    ps_client_id = read_wlc_secret(secret_name=args.ps_client_id_secret, **wlc)
    ps_client_secret = read_wlc_secret(secret_name=args.ps_client_secret_secret, **wlc)
    print("[agent] holding nothing: the Password Safe client pair came from Workload "
          f"Credentials against this machine's own identity · {_now()}", flush=True)

    def _waiting(elapsed: int) -> None:
        print(f"[agent] {spiffe_id} · WAITING for approval ({elapsed}s) — this agent "
              f"cannot authorise its own access · {_now()}", flush=True)

    # HALF ONE: the passphrase, through the same recorded-request flow the cluster
    # episode uses. Approval-gated wherever the access policy says so.
    passphrase, base, headers, request_id, polls = password_safe_episode(
        api_url=args.ps_api_url, client_id=ps_client_id,
        client_secret=ps_client_secret, account_id=args.cert_account_id,
        system_id=args.cert_system_id, duration_min=args.cert_duration,
        reason=reason, max_wait_seconds=args.cert_max_wait, on_wait=_waiting,
        # A PKCS#12 passphrase is an opaque string -- there is no shape to check beyond
        # "not empty", and _poll_credential has already rejected the soft-failure
        # sentence by the time this runs.
        validate=lambda v: bool(v), expected="a PKCS#12 passphrase")
    if not passphrase:
        print(f"[agent] {spiffe_id} · the request was never approved within "
              f"{args.cert_max_wait}s — the slot has been given back · {_now()}",
              flush=True)
        return 3

    # A CERTIFICATE IS THE ONE THAT CANNOT BE TAKEN BACK, which is why the human matters
    # most here: the approval is the ONLY moment anybody gets a say. Once the passphrase
    # is out, the identity works until it expires whatever anyone does afterwards.
    problem = approval_problem(polls, args.require_approval)
    if problem:
        _checkin(base, headers, request_id, reason)
        print(f"[agent] {spiffe_id} · REFUSING: {problem} · {_now()}", flush=True)
        print("[agent] this matters more here than anywhere else in the cell: a "
              "certificate cannot be revoked out from under this agent, so the approval "
              "is the only moment a person gets a say.", flush=True)
        return 5

    import tempfile

    try:
        # ONE GUARDED DIRECTORY FOR THE WHOLE EPISODE. The bundle is downloaded into it,
        # openssl writes the certificate and key beside it, and it all goes at the end --
        # including on the failure paths below. This is the one place in this worker
        # where a credential touches disk, and saying so beats letting somebody find it.
        with tempfile.TemporaryDirectory(prefix="mcp-agent-cert-") as work:
            os.chmod(work, 0o700)
            # HALF TWO: the bundle. Neither half is usable alone, which is the design.
            print(f"[agent] {spiffe_id} · passphrase released; downloading the bundle "
                  f"from Secrets Safe · {_now()}", flush=True)
            # The SAME session that released the passphrase. Secrets Safe is part of
            # Password Safe, so both halves of this identity come down one sign-in.
            bundle_path = secrets_safe_file(args.cert_bundle_title, dest_dir=work,
                                            base=base, headers=headers)
            result = cert_mtls_probe(endpoint=args.cert_endpoint,
                                     bundle_path=bundle_path, passphrase=passphrase,
                                     expect_cn=args.cert_cn, work_dir=work,
                                     verify=not args.cert_insecure)
            print(f"[agent] {spiffe_id} · {cert_probe_summary(result)} · {_now()}",
                  flush=True)
    finally:
        _checkin(base, headers, request_id, reason)
        print(f"[agent] {spiffe_id} · the request was checked back in · {_now()}",
              flush=True)
        print("[agent] note: nothing here checks a CRL or OCSP. Revoking this "
              "certificate does not stop this agent — only its expiry does. That is the "
              "mechanism this episode exists to show, not a gap in it.", flush=True)
    return 0 if result.get("proved") else 4


def _cloud_probe(args, minted: dict) -> dict:
    """Dispatch to the right probe for the cloud the PAYLOAD declared.

    **The deny probe has to belong to that cloud, and the check is not pedantry.**
    ``--cloud-deny-probe`` offers the union of both clouds' probes, because argparse
    cannot know which cloud the mint will return. Neither probe function acts on the
    value beyond ``none`` -- each runs its own cloud's call -- so a mismatched choice
    runs one call and labels it with the OTHER one's sentence. The episode would then
    print `scope proved ... an ARM-scoped service principal cannot read the Entra
    directory` about a run that tested `iam:ListUsers`, and exit 0. An assertion
    attributed to a probe that never ran is worse than no assertion.
    """
    cloud = minted["cloud"]
    deny = args.cloud_deny_probe
    if deny == "auto":
        deny = _DEFAULT_DENY_PROBE[cloud]
    elif deny != "none" and deny != _DEFAULT_DENY_PROBE[cloud]:
        raise SystemExit(
            f"[agent] FATAL: --cloud-deny-probe {deny} is not an {cloud} probe, and "
            f"{cloud} is what the dynamic secret returned. Use "
            f"{_DEFAULT_DENY_PROBE[cloud]}, or `none` to run no refusal at all — a run "
            f"labelled with a probe it did not make would claim a limit nobody tested.")
    if cloud == "aws":
        return aws_cloud_probe(values=minted["values"], deny_probe=deny,
                               region=args.cloud_region,
                               verify=not args.cloud_insecure)
    return azure_cloud_probe(values=minted["values"], scope=args.cloud_scope,
                             deny_probe=deny, verify=not args.cloud_insecure)


def run_cloud_episode(args) -> int:
    """One cloud-credential episode, and the FOURTH control surface this cell shows.

    The arc, now four long, and each one ends differently:

      * the **PAT** is revocable — pull it and the loop stops mid-poll;
      * the **cluster token** is gated at retrieval — a person decides, and once
        released it lives out its TTL;
      * a **certificate** is neither: nothing on that path checks a CRL, so it stops
        when it expires and not when somebody takes it away;
      * a **cloud credential** did not exist until this worker asked. It expires, and on
        AWS **nothing can shorten that** — STS will not withdraw a credential it has
        already signed.

    That last one is why this episode's closing beat is a real wait rather than a revoke.
    `examples/playbooks/cloud/ci-run-with-dynamic-creds.yml` makes the same call for the
    same reason: a run that faked the clock would prove it can print a failure message,
    not that the credential died.

    **This is the first episode in this cell that costs money when it runs.** Exactly one
    `generate`, and nothing retries it.

    Exit codes:

      * **0** — the scope and the ending were both proved (or no refusal was asked for
        and the ending was still proved; see `--cloud-deny-probe none`).
      * **4** — A REFUSAL DID NOT REFUSE: the deny probe succeeded, or the credential
        still worked after it should have stopped. Reserved for exactly that, because
        it is the outcome a reader will act on.
      * **5** — the ending was NOT PROVED. Either the run was told to skip it, or it
        could not be observed: the lease outlasts ``--cloud-max-wait``, or the provider
        returned no readable expiry. Deliberately not 4 — nothing refused wrongly in
        any of those, and saying so would invent a scope finding.
      * **3** — **never returned.** There is no approval on this path, and a 3 here
        would name a human who was never asked.
    """
    missing = [n for n, v in (("--cloud-dynamic-name", args.cloud_dynamic_name),
                              ("--wlc-base-url", args.wlc_base_url),
                              ("--wlc-site-id", args.wlc_site_id),
                              ("--wlc-service-name", args.wlc_service_name),
                              ("--wlc-resource", args.wlc_resource)) if not v]
    if missing:
        raise SystemExit("[agent] FATAL: --cloud-episode needs " + ", ".join(missing))

    spiffe_id = fetch_spiffe_id(args.spiffe_socket)
    print(f"[agent] {spiffe_id} · requesting a short-lived cloud credential from dynamic "
          f"secret {args.cloud_dynamic_name} · {_now()}", flush=True)

    identity = fetch_identity_token(args.wlc_resource, args.identity_platform,
                                    args.wlc_client_id, args.identity_token_file,
                                    args.spiffe_socket)
    wlc = dict(base_url=args.wlc_base_url, site_id=args.wlc_site_id,
               service_name=args.wlc_service_name, identity_token=identity)
    minted = generate_wlc_credential(
        dynamic_name=args.cloud_dynamic_name,
        folder=args.cloud_dynamic_folder or args.wlc_folder, **wlc)
    cloud = minted["cloud"]
    print("[agent] holding nothing: this machine's own identity token is what Workload "
          "Credentials accepted. No PAT, no Password Safe client pair, and the "
          f"issuance is recorded against this workload rather than the dashboard · "
          f"{_now()}", flush=True)
    print(f"[agent] MINTED — one metered issuance. {cloud} lease "
          f"{minted['lease_id'] or '(none returned)'} expires "
          f"{minted['expires_at'] or '(no expiry reported)'} · {_now()}", flush=True)

    # AFTER the mint, deliberately. The refusal has to name a credential that exists,
    # because an operator who sees it has already been billed and needs to know that.
    problem = cloud_ending_problem(args.prove_ending, cloud)

    # Azure only, and only when asked for. The default on BOTH clouds is to wait out the
    # expiry: it is the one ending that exists everywhere, and having the demo end the
    # same way on both is worth more than the thirty seconds a release saves.
    #
    # DOWNGRADED RATHER THAN REFUSED, and only because of where this can be checked. The
    # cloud is derived from the minted PAYLOAD -- deliberately, so a flag can never
    # disagree with what came back -- which means this is the earliest point it is known,
    # and by now the issuance has happened and been billed. Exiting here would throw away
    # a credential somebody paid for in order to punish a flag, and prove nothing with
    # it. So it says loudly what it is doing instead; the one thing it must not do is
    # switch endings quietly.
    end_with = args.cloud_end_with
    if end_with == "release" and not cloud_revocable(cloud):
        end_with = "expiry"
        print(f"[agent] {spiffe_id} · --cloud-end-with release was asked for, and an "
              f"{cloud} lease CANNOT be released — STS will not withdraw a credential it "
              f"has already signed. Ending with the expiry instead. This is not a "
              f"workaround: on {cloud} the TTL is the only control there is, which is "
              f"why a short one matters more here, not less · {_now()}", flush=True)

    # EVERYTHING AFTER THE MINT GOES INSIDE THE TRY, including the first probe. The
    # `finally` below is the only place the run says it was billed, and the mint has
    # already happened by here — so a probe that raises outside it would end the process
    # having spent money and never mentioned it.
    try:
        result = _cloud_probe(args, minted)
        print(f"[agent] {spiffe_id} · {cloud_probe_summary(result)} · {_now()}",
              flush=True)
        if problem:
            print(f"[agent] {spiffe_id} · REFUSING: {problem} · {_now()}", flush=True)
            return 5
        if not result.get("authenticated"):
            # 1, not 4. A credential that never worked cannot be shown to stop working,
            # so there is no refusal here that failed to refuse — there is a broken run,
            # and the summary above has already said which half broke.
            return 1
        if end_with == "release":
            released, detail = revoke_wlc_lease(lease_id=minted["lease_id"], **wlc)
            print(f"[agent] {spiffe_id} · {detail} · {_now()}", flush=True)
            if not released:
                return 4
            print("[agent] note: the release killed the ability to get ANOTHER token, "
                  "not the one already issued — an ARM access token lives out its own "
                  "hour whatever happens to the service principal behind it. Same shape "
                  "as the cluster episode's 'the approval gates retrieval, not use'.",
                  flush=True)
        elif not _wait_out_the_lease(minted, args.cloud_max_wait, spiffe_id):
            # 5, NOT 4. Nothing failed to refuse here — the ending simply could not be
            # observed, which is what 5 already means. Returning 4 would report "the
            # credential outlived its expiry" about a credential that was never
            # re-tested, and a CI job reading the code would act on a scope finding
            # that does not exist.
            return 5

        # THE BEAT THAT PROVES IT. Re-run the ALLOW probe only — the deny probe already
        # said what it had to say, and running it again against a dead credential would
        # produce a refusal for the wrong reason that looks like the right one.
        after = _cloud_probe(argparse.Namespace(**{**vars(args),
                                                   "cloud_deny_probe": "none"}), minted)
        if after.get("authenticated"):
            print(f"[agent] {spiffe_id} · THE CREDENTIAL STILL WORKS after it should "
                  f"have stopped — {cloud_probe_summary(after)} · {_now()}", flush=True)
            return 4
        print(f"[agent] {spiffe_id} · the credential is dead: the same call now returns "
              f"{after.get('allow_status')}/{after.get('allow_code') or 'no code'} · "
              f"{_now()}", flush=True)
    finally:
        print(f"[agent] this episode billed ONE issuance against {args.cloud_dynamic_name}. "
              "Nothing was left on this host, and nothing here could widen what that "
              "credential was allowed to do — the dynamic secret's own definition in "
              "Workload Credentials decides that, and this worker cannot read it.",
              flush=True)
    # `proved` is False under `--cloud-deny-probe none`, and that is not a failure: no
    # refusal was asked for, so none failing to refuse is not an outcome. Returning 4
    # there would report the one thing 4 means -- something that should have refused did
    # not -- about a probe that was never run. `cloud_probe_summary` has already said
    # the run proves authentication and not scope, which is the honest report of it.
    return 0 if (result.get("proved")
                 or result.get("deny_probe") == "none") else 4


def _wait_out_the_lease(minted: dict, max_wait: int, spiffe_id: str) -> bool:
    """Hold until the provider's expiry passes. False if it cannot be waited out.

    The expiry comes from the PROVIDER'S payload, never from a requested TTL: AWS clamps
    a role-chained credential at an hour, so a wait computed from the ask would re-probe
    a credential that is still alive and report a failure that is really impatience.

    A countdown line every minute, for the reason `_waiting` prints one in the cluster
    episode — a silent process is indistinguishable from a hung one, and this wait is
    long enough for somebody to give up on it.
    """
    margin = 30
    deadline = minted["expires_epoch"]
    if not deadline:
        print(f"[agent] {spiffe_id} · the provider returned no readable expiry, so this "
              "run cannot show the credential dying. That is the only control there "
              f"was, and it is now unobservable · {_now()}", flush=True)
        return False
    remaining = (deadline + margin) - time.time()
    if remaining > max_wait:
        print(f"[agent] {spiffe_id} · the lease has {int(remaining // 60)}m left, longer "
              f"than --cloud-max-wait ({max_wait}s). Shorten the dynamic secret's TTL — "
              "on a cloud whose credential cannot be revoked, a short TTL is the whole "
              f"control · {_now()}", flush=True)
        return False
    while True:
        remaining = (deadline + margin) - time.time()
        if remaining <= 0:
            return True
        print(f"[agent] {spiffe_id} · waiting out the lease — {int(remaining // 60)}m "
              f"{int(remaining % 60)}s remaining. Nothing can shorten this · {_now()}",
              flush=True)
        time.sleep(min(60, remaining))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=os.environ.get("AGENT_MCP_URL", ""),
                    help="the dashboard's MCP endpoint, e.g. https://host/mcp")
    ap.add_argument("--token-file", default=os.environ.get("AGENT_TOKEN_FILE",
                                                           "/etc/mcp-agent/token"))
    ap.add_argument("--token-label", default=os.environ.get("AGENT_TOKEN_LABEL", ""),
                    help="the PAT's NAME, as Settings -> API Tokens lists it and as the "
                         "agent cell's create response returns it. Non-secret, and the "
                         "only thing about the token this worker ever logs.")
    ap.add_argument("--token-source", choices=("file", "wlc", "ps"),
                    default=os.environ.get("AGENT_TOKEN_SOURCE", "file"),
                    help="where the dashboard PAT comes from. 'wlc' reads it out of "
                         "Workload Credentials' own store; 'ps' reads the Password Safe "
                         "API client pair out of WC and requests the credential from the "
                         "vault. Neither leaves a secret on this host.")
    ap.add_argument("--identity-platform", choices=IDENTITY_PLATFORMS,
                    default=os.environ.get("AGENT_IDENTITY_PLATFORM", "auto"),
                    help="which issuer vouches for this machine. Not Azure-only: every "
                         "cloud federates non-human identities over OIDC, and 'spire' "
                         "needs no cloud at all.")
    ap.add_argument("--identity-token-file",
                    default=os.environ.get("AGENT_IDENTITY_TOKEN_FILE", ""),
                    help="override the projected-token path for the aws/file platforms.")
    # All non-secret. That is the whole point -- everything needed to reach Workload
    # Credentials in `entra` mode is configuration, and the credential is not.
    ap.add_argument("--wlc-base-url", default=os.environ.get("AGENT_WLC_BASE_URL", ""))
    ap.add_argument("--wlc-site-id", default=os.environ.get("AGENT_WLC_SITE_ID", ""))
    ap.add_argument("--wlc-service-name",
                    default=os.environ.get("AGENT_WLC_SERVICE_NAME", ""))
    ap.add_argument("--wlc-resource", default=os.environ.get("AGENT_WLC_RESOURCE", ""),
                    help="the audience the identity token is minted for.")
    ap.add_argument("--wlc-secret-name",
                    default=os.environ.get("AGENT_WLC_SECRET_NAME", ""),
                    help="token-source wlc: the WC secret holding the PAT.")
    ap.add_argument("--wlc-folder", default=os.environ.get("AGENT_WLC_FOLDER", ""))
    ap.add_argument("--wlc-client-id", default=os.environ.get("AGENT_WLC_CLIENT_ID", ""),
                    help="Azure only: selects a user-assigned managed identity.")
    # token-source ps. The two WC secrets are the Password Safe OAuth2 pair, and the
    # account is the one whose password IS this worker's PAT.
    ap.add_argument("--ps-client-id-secret",
                    default=os.environ.get("AGENT_PS_CLIENT_ID_SECRET", ""))
    ap.add_argument("--ps-client-secret-secret",
                    default=os.environ.get("AGENT_PS_CLIENT_SECRET_SECRET", ""))
    ap.add_argument("--ps-api-url", default=os.environ.get("AGENT_PS_API_URL", ""),
                    help="e.g. https://ps.example.com/BeyondTrust/api/public/v3")
    ap.add_argument("--ps-account-id", type=int,
                    default=int(os.environ.get("AGENT_PS_ACCOUNT_ID", "0") or 0))
    ap.add_argument("--ps-system-id", type=int,
                    default=int(os.environ.get("AGENT_PS_SYSTEM_ID", "0") or 0),
                    help="optional; Password Safe resolves it from the account when 0.")
    ap.add_argument("--ps-duration", type=int,
                    default=int(os.environ.get("AGENT_PS_DURATION", "30") or 30))
    ap.add_argument("--ps-reason", default=os.environ.get(
        "AGENT_PS_REASON", "mcp-agent credential fetch"))
    ap.add_argument("--tool", default=DEFAULT_TOOL)
    ap.add_argument("--spiffe-socket", default=os.environ.get("AGENT_SPIFFE_SOCKET",
                                                              DEFAULT_SOCKET))
    ap.add_argument("--interval", type=int, default=30)
    # ── One cluster-access episode ───────────────────────────────────────────
    # A MODE, not a loop. The worker's job is the MCP loop; this is a bounded errand it
    # runs once and reports on. Keeping them separate is what makes the episode a single
    # request/check-in pair in the Password Safe audit trail rather than a stream.
    ap.add_argument("--k8s-episode", action="store_true",
                    help="request the linked cluster token, wait for approval, prove "
                         "the token is scoped, and give the slot back. Exits when done.")
    ap.add_argument("--k8s-api-server", default=os.environ.get("AGENT_K8S_API", ""),
                    help="e.g. https://10.0.0.5:6443")
    ap.add_argument("--k8s-profile", default=os.environ.get("AGENT_K8S_PROFILE", ""),
                    choices=("", "deployer", "reader"))
    ap.add_argument("--k8s-namespace", default=os.environ.get("AGENT_K8S_NS", ""))
    ap.add_argument("--k8s-other-namespace",
                    default=os.environ.get("AGENT_K8S_OTHER_NS", "kube-system"),
                    help="the namespace the deployer profile must be REFUSED in.")
    ap.add_argument("--k8s-account-id", type=int,
                    default=int(os.environ.get("AGENT_K8S_ACCOUNT_ID", "0") or 0),
                    help="the WorkloadK8sToken row's ps_account_id.")
    ap.add_argument("--k8s-system-id", type=int,
                    default=int(os.environ.get("AGENT_K8S_SYSTEM_ID", "0") or 0))
    ap.add_argument("--k8s-duration", type=int, default=15,
                    help="minutes the request holds the account's slot.")
    ap.add_argument("--k8s-max-wait", type=int, default=1800,
                    help="seconds to wait for approval before giving the slot back.")
    ap.add_argument("--k8s-insecure", action="store_true",
                    help="skip API server certificate verification (lab clusters).")
    # ── One certificate episode ──────────────────────────────────────────────
    ap.add_argument("--cert-episode", action="store_true",
                    help="request the linked certificate identity's two halves, present "
                         "the client certificate to an mTLS endpoint, and release. "
                         "Exits when done.")
    ap.add_argument("--cert-endpoint", default=os.environ.get("AGENT_CERT_ENDPOINT", ""),
                    help="the lab's mTLS endpoint, e.g. https://api.demo.internal:8443/")
    ap.add_argument("--cert-cn", default=os.environ.get("AGENT_CERT_CN", ""),
                    help="the subject the endpoint should echo back.")
    ap.add_argument("--cert-bundle-title",
                    default=os.environ.get("AGENT_CERT_BUNDLE", ""),
                    help="the bundle's Secrets Safe reference, e.g. "
                         "cert/<system>/<account>. One containing '/' is resolved as a "
                         "folder path, one without it as a bare title.")
    ap.add_argument("--cert-account-id", type=int,
                    default=int(os.environ.get("AGENT_CERT_ACCOUNT_ID", "0") or 0),
                    help="the managed account holding the PKCS#12 passphrase.")
    ap.add_argument("--cert-system-id", type=int,
                    default=int(os.environ.get("AGENT_CERT_SYSTEM_ID", "0") or 0))
    ap.add_argument("--cert-duration", type=int, default=15)
    ap.add_argument("--cert-max-wait", type=int, default=1800)
    ap.add_argument("--cert-insecure", action="store_true",
                    help="skip endpoint certificate verification (lab endpoints).")
    # ── One cloud-credential episode ─────────────────────────────────────────
    # The only episode that MINTS rather than retrieves, the only one with no human in
    # the loop, and the only one that costs money. All three follow from the same fact:
    # Workload Credentials issues against a dynamic secret, so there is nothing standing
    # for a person to gate and nothing already held for a vault to release.
    ap.add_argument("--cloud-episode", action="store_true",
                    help="mint one short-lived AWS or Azure credential against this "
                         "machine's own identity, prove it is scoped, and prove it "
                         "ends. ONE METERED ISSUANCE. Exits when done.")
    ap.add_argument("--cloud-dynamic-name",
                    default=os.environ.get("AGENT_CLOUD_DYNAMIC_NAME", ""),
                    help="the Workload Credentials dynamic secret to mint from. A NAME, "
                         "not a credential — and the thing that decides the scope of "
                         "everything this episode receives.")
    ap.add_argument("--cloud-dynamic-folder",
                    default=os.environ.get("AGENT_CLOUD_DYNAMIC_FOLDER", ""),
                    help="falls back to --wlc-folder when unset.")
    ap.add_argument("--cloud-deny-probe", default=os.environ.get(
                        "AGENT_CLOUD_DENY_PROBE", "auto"),
                    choices=("auto",) + tuple(CLOUD_DENY_PROBES),
                    help="which call the credential must be REFUSED. This worker cannot "
                         "read the dynamic secret's role, so this is an assertion you "
                         "are making about it. 'none' runs no refusal at all and says "
                         "out loud that the run then proves authentication, not scope.")
    ap.add_argument("--cloud-scope", default=os.environ.get("AGENT_CLOUD_SCOPE", ""),
                    help="Azure: the subscription id the credential must be able to "
                         "read. Required there; ignored on AWS.")
    ap.add_argument("--cloud-region", default=os.environ.get("AGENT_CLOUD_REGION",
                                                             "us-east-1"),
                    help="AWS: the region the STS and IAM calls are signed for.")
    # Default `expiry` on BOTH clouds rather than `auto`. A release is available on
    # Azure and is quicker, but the ending that exists everywhere is the one worth
    # making the default -- a demo that ends differently depending on the cloud is a
    # demo somebody has to remember two versions of.
    ap.add_argument("--cloud-end-with", choices=("expiry", "release"),
                    default=os.environ.get("AGENT_CLOUD_END_WITH", "expiry"),
                    help="how the episode shows the credential stopping. 'expiry' waits "
                         "out the provider's own expiry and re-probes — a real wait, "
                         "never a faked clock. 'release' is Azure only and is refused "
                         "on a cloud whose leases cannot be revoked.")
    ap.add_argument("--cloud-max-wait", type=int,
                    default=int(os.environ.get("AGENT_CLOUD_MAX_WAIT", "4200") or 4200),
                    help="seconds this will wait for an expiry. AWS caps a role-chained "
                         "credential at an hour, so the default allows for one.")
    ap.add_argument("--cloud-insecure", action="store_true",
                    help="skip certificate verification on the cloud endpoints (a lab "
                         "behind an intercepting proxy).")
    ap.add_argument("--no-prove-ending", dest="prove_ending",
                    action="store_false", default=True,
                    help="mint and prove the scope, but stop without showing the "
                         "credential die. Off by default — that run prints a line "
                         "indistinguishable from a full one, on a demo whose whole "
                         "argument is that the credential ends.")
    # Applies to BOTH Password Safe episodes. #912's page already claims the agent "cannot authorise
    # its own access"; on an auto-releasing policy that was silently untrue there too, so
    # this is a correctness fix to an existing claim rather than a new rule for one
    # episode. Default on: the ungated case is the one that needs saying out loud.
    ap.add_argument("--no-require-approval", dest="require_approval",
                    action="store_false", default=True,
                    help="run an episode even when Password Safe released without "
                         "consulting anybody. Off by default — an ungated fetch that "
                         "prints an approved-looking line is the one way this demo can "
                         "mislead.")
    ap.add_argument("--selftest", action="store_true",
                    help="check the argument wiring and exit, touching nothing")
    args = ap.parse_args(argv)

    if args.selftest:
        print("[agent] selftest ok:",
              json.dumps({"tool": args.tool, "interval": args.interval,
                          "spiffe_socket": args.spiffe_socket,
                          "token_source": args.token_source,
                          "token_label": token_label(args.token_label),
                          "identity_platform": args.identity_platform,
                          "detected_platform": detect_platform() or "none",
                          "cloud_episode": args.cloud_episode,
                          "cloud_end_with": args.cloud_end_with,
                          "cloud_deny_probe": args.cloud_deny_probe}))
        return 0
    if args.k8s_episode:
        return run_k8s_episode(args)
    if args.cert_episode:
        return run_cert_episode(args)
    if args.cloud_episode:
        return run_cloud_episode(args)

    if not args.url:
        raise SystemExit("[agent] FATAL: --url (or AGENT_MCP_URL) is required.")

    # Named individually rather than "check your config": every one of these is
    # non-secret, so there is no reason to be vague about which is absent.
    common = (("--wlc-base-url", args.wlc_base_url),
              ("--wlc-site-id", args.wlc_site_id),
              ("--wlc-service-name", args.wlc_service_name),
              ("--wlc-resource", args.wlc_resource))
    if args.token_source in ("wlc", "ps"):
        extra = {
            "wlc": (("--wlc-secret-name", args.wlc_secret_name),),
            "ps": (("--ps-client-id-secret", args.ps_client_id_secret),
                   ("--ps-client-secret-secret", args.ps_client_secret_secret),
                   ("--ps-api-url", args.ps_api_url),
                   ("--ps-account-id", args.ps_account_id)),
        }[args.token_source]
        missing = [n for n, v in common + extra if not v]
        if missing:
            raise SystemExit(f"[agent] FATAL: --token-source {args.token_source} needs "
                             + ", ".join(missing))

    wlc = dict(base_url=args.wlc_base_url, site_id=args.wlc_site_id,
               service_name=args.wlc_service_name, resource=args.wlc_resource,
               folder=args.wlc_folder, client_id=args.wlc_client_id,
               platform=args.identity_platform,
               identity_token_file=args.identity_token_file,
               socket_path=args.spiffe_socket)
    if args.token_source == "wlc":
        token = fetch_token_from_wlc(secret_name=args.wlc_secret_name, **wlc)
    elif args.token_source == "ps":
        token = fetch_token_via_password_safe(
            client_id_secret=args.ps_client_id_secret,
            client_secret_secret=args.ps_client_secret_secret,
            ps_api_url=args.ps_api_url, account_id=args.ps_account_id,
            system_id=args.ps_system_id, duration_min=args.ps_duration,
            reason=args.ps_reason, **wlc)
    else:
        token = read_token(args.token_file)

    return run(args.url, token, args.tool, args.spiffe_socket, args.interval,
               token_source=args.token_source, label=args.token_label)


if __name__ == "__main__":
    sys.exit(main())
