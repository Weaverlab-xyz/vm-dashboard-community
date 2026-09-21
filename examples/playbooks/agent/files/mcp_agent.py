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


def scrub(text: str) -> str:
    """An exception's text with anything credential-shaped removed.

    `call_once` hands the PAT to an HTTP client as an Authorization header, and the
    cluster probes hand a ServiceAccount token to an API server the same way. What either
    puts in an error repr is not this worker's decision, so the values are removed on the
    way OUT, at the one place error text becomes a log line.
    """
    out = TOKEN_RE.sub("vmcli_<redacted>", text or "")
    return JWT_SCRUB_RE.sub("<jwt redacted>", out)


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

    The identity token is passed in rather than fetched here: the ``ps`` source reads two
    secrets, and one token should serve both rather than making the metadata service
    answer twice for the same machine.
    """
    from urllib.parse import quote, urlencode

    path = f"/site/{quote(site_id)}/secrets/{quote(secret_name)}"
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
    # Applies to BOTH episodes. #912's page already claims the agent "cannot authorise
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
                          "detected_platform": detect_platform() or "none"}))
        return 0
    if args.k8s_episode:
        return run_k8s_episode(args)
    if args.cert_episode:
        return run_cert_episode(args)

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
