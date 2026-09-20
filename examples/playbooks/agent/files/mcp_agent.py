#!/usr/bin/env python3
"""mcp_agent — a non-human principal that reads the estate, and can be stopped.

The worker the agent demo cell installs. It does one small thing on a loop, and the
point is not the thing: it is that every loop leaves a line naming **who it is** and
**what it spent**, and that revoking the token ends it while somebody watches.

    spiffe://weaverlab.test/agent/mcp-reader · token vmcli_9f3c… · 14 jobs, 2 failures · 14:02:11

TWO CREDENTIALS, AND THEY ARE NOT THE SAME ONE. This is the honest shape of the demo
and the code is arranged so nobody can miss it:

  * the **SVID** is the worker's IDENTITY. It is fetched from the SPIRE agent's workload
    API socket at each loop, held in memory, never written down, and re-fetched rather
    than cached — a workload that attests itself has nothing to store.
  * the **PAT** is the worker's AUTHORIZATION to this dashboard. It is what /mcp accepts,
    it is scoped to a user whose RBAC bounds every tool, it expires, and it can be
    revoked from Settings -> API Tokens while the loop is running.

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


def token_hint(token: str) -> str:
    """Enough of the token to correlate a log line with a row in Settings -> API Tokens,
    and not enough to use. The full value never reaches a log."""
    return token[:11] + "…"


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


def password_safe_credential(*, api_url: str, client_id: str, client_secret: str,
                             account_id: int, system_id: int = 0,
                             duration_min: int = 30,
                             reason: str = "mcp-agent credential fetch") -> str:
    """One managed credential, with a pair this host was handed rather than keeps.

    Mirrors ``services/ps_api_service`` step for step, because a second dialect of the
    same API is how the two drift apart:

      * ``POST Auth/Connect/Token`` (form-encoded) for a Bearer token, then
        ``POST Auth/SignAppIn`` to establish the session the retrieval endpoints need;
      * ``POST Requests`` -> a request id, then ``GET Credentials/{id}`` -> the value;
      * ``PUT Requests/{id}/Checkin`` to release it. Plain check-in ONLY -- never the
        rotate-on-release variant, for the reason ``ps_api_service._checkin`` records.

    The check-in is in a ``finally``: an open request holds the account's concurrent slot
    for its whole duration, so a worker that crashed between retrieval and release would
    make the NEXT fetch fail on the cap and report the wrong cause.
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

    body = _post_json(base + "Requests", headers, body={
        "AccessType": "View", "SystemID": int(system_id or 0),
        "AccountID": int(account_id), "DurationMinutes": int(duration_min),
        "Reason": reason, "ConflictOption": "reuse"})
    request_id = body if isinstance(body, int) else (
        (body or {}).get("RequestID") or (body or {}).get("RequestId")
        or (body or {}).get("id"))
    if not request_id:
        raise SystemExit("[agent] FATAL: Password Safe returned no request id.")
    try:
        got = _get_json(base + f"Credentials/{int(request_id)}", headers)
        if isinstance(got, dict):
            got = got.get("Credentials") or got.get("Password") or ""
        value = str(got or "").strip().strip('"')
        if not value.startswith("vmcli_"):
            # Password Safe can return a soft-failure STRING in the credential position
            # ("It was not possible to get a credential for Request ID: 5") -- the case
            # ps_api_service._looks_like_sa_token guards for the k8s tunnel. The same
            # guard here, because a worker that polls with that string as its bearer
            # gets a 401 and reports the demo's closing beat for the wrong reason.
            raise SystemExit(
                "[agent] FATAL: Password Safe did not release a dashboard PAT for "
                f"account {int(account_id)} — the request may be awaiting approval, or "
                "the access policy may not auto-release.")
        return value
    finally:
        try:
            _request(base + f"Requests/{int(request_id)}/Checkin",
                     dict(headers, **{"Content-Type": "application/json"}),
                     data=json.dumps({"Reason": reason}).encode("utf-8"),
                     method="PUT")
        except Exception:  # noqa: BLE001 — best effort; the duration expires it anyway
            print("[agent] note: the credential check-in was refused; the request "
                  "expires on its own duration.", flush=True)


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
        token_source: str = "file") -> int:
    hint = token_hint(token)
    held = SOURCE_HOLDS.get(token_source, "a static secret on this host")
    print(f"[agent] polling {url} every {interval}s as {hint} "
          f"(token from {token_source}: {held})", flush=True)
    while True:
        spiffe_id = fetch_spiffe_id(socket_path)
        try:
            payload = asyncio.run(call_once(url, token, tool))
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            # 401 is the demo's closing beat, not an error to ride out.
            if "401" in text or "Unauthorized" in text or "unauthorized" in text:
                print(f"[agent] {spiffe_id} · token {hint} · REFUSED — the token "
                      f"is revoked or expired · {_now()}", flush=True)
                print("[agent] stopping: the identity is still valid, the authorization "
                      "is not.", flush=True)
                return 2
            print(f"[agent] {spiffe_id} · token {hint} · call failed: {text} · "
                  f"{_now()}", flush=True)
            time.sleep(interval)
            continue
        print(f"[agent] {spiffe_id} · token {hint} · {summarise(payload)} · "
              f"{_now()}", flush=True)
        time.sleep(interval)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=os.environ.get("AGENT_MCP_URL", ""),
                    help="the dashboard's MCP endpoint, e.g. https://host/mcp")
    ap.add_argument("--token-file", default=os.environ.get("AGENT_TOKEN_FILE",
                                                           "/etc/mcp-agent/token"))
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
    ap.add_argument("--selftest", action="store_true",
                    help="check the argument wiring and exit, touching nothing")
    args = ap.parse_args(argv)

    if args.selftest:
        print("[agent] selftest ok:",
              json.dumps({"tool": args.tool, "interval": args.interval,
                          "spiffe_socket": args.spiffe_socket,
                          "token_source": args.token_source,
                          "identity_platform": args.identity_platform,
                          "detected_platform": detect_platform() or "none"}))
        return 0
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
               token_source=args.token_source)


if __name__ == "__main__":
    sys.exit(main())
