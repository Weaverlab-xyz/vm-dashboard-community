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

TWO TOKEN SOURCES, AND THE SECOND ONE STORES NOTHING.

  * ``--token-source file`` (default) reads a 0600 file. An env var would be readable
    from /proc/<pid>/environ by anything running as the same user and shows up in a
    `ps e`, so a file read once at startup is the smaller surface -- but it is still a
    static secret sitting on a disk.
  * ``--token-source wlc`` has no secret on the host at all. The worker asks the
    platform for its own identity token, presents that to BeyondTrust **Workload
    Credentials**, and reads its dashboard PAT back out. Everything it is configured
    with -- site id, service name, resource, base URL -- is non-secret.

The second is the point of the whole cell. A non-human principal that holds no standing
credential is the argument; a PAT in a file is that argument with an asterisk. This is
the suite answering it with pieces that already exist: an identity the platform vouches
for (new), Workload Credentials brokering on it (new), and the vault behind it (old).
``services/workload_credentials_service`` puts it plainly -- "two auth modes, and the
second one stores nothing".

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
# IMDS. The runtime injects IDENTITY_ENDPOINT/IDENTITY_HEADER on Container Apps and App
# Service; a plain VM has neither and uses the metadata service directly. Same two-branch
# shape as services/workload_credentials_service.build_identity_request.
IMDS_URL = "http://169.254.169.254/metadata/identity/oauth2/token"
IMDS_API_VERSION = "2018-02-01"


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


def _get_json(url: str, headers: dict, timeout: int = 10) -> dict:
    """One GET, stdlib only.

    urllib rather than httpx or requests deliberately: this worker runs on somebody
    else's VM, and every dependency it needs is something the install play has to put
    there. The MCP client is unavoidable; an HTTP library is not.
    """
    import urllib.request

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def fetch_identity_token(resource: str, client_id: str = "") -> str:
    """The platform's own token for this machine. Nothing is stored to get it."""
    params = {"api-version": IMDS_API_VERSION, "resource": resource}
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
    from urllib.parse import urlencode

    payload = _get_json(f"{url}?{urlencode(params)}", headers)
    token = payload.get("access_token") or ""
    if not token:
        raise SystemExit("[agent] FATAL: the identity endpoint returned no access_token.")
    return token


def fetch_token_from_wlc(*, base_url: str, site_id: str, service_name: str,
                         resource: str, secret_name: str, folder: str = "",
                         client_id: str = "") -> str:
    """This worker's dashboard PAT, from Workload Credentials, holding nothing.

    The chain, and why each link is there:

      1. the platform vouches for this machine  -> an identity token, stored nowhere;
      2. Workload Credentials accepts that token in place of a PAT (``entra`` auth mode)
         and ``X-BT-Service-Name`` says which registered Workload Identity it satisfies;
      3. the secret comes back.

    The path grammar mirrors the shipping Terraform provider's BuildPath --
    ``/site/{site-id}/secrets{endpoint}`` with an optional ``?folder=`` -- so this and
    ``services/workload_credentials_service.build_secrets_path`` cannot disagree about
    where a secret lives.
    """
    from urllib.parse import quote, urlencode

    token = fetch_identity_token(resource, client_id)
    path = f"/site/{quote(site_id)}/secrets/{quote(secret_name)}"
    url = base_url.rstrip("/") + path
    if folder:
        url += "?" + urlencode({"folder": folder})
    payload = _get_json(url, {
        "Authorization": f"Bearer {token}",
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


def run(url: str, token: str, tool: str, socket_path: str, interval: int,
        token_source: str = "file") -> int:
    hint = token_hint(token)
    held = "nothing on this host" if token_source == "wlc" else "a file on this host"
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
    ap.add_argument("--token-source", choices=("file", "wlc"),
                    default=os.environ.get("AGENT_TOKEN_SOURCE", "file"),
                    help="where the dashboard PAT comes from. 'wlc' holds no secret on "
                         "this host: the platform vouches for the machine and Workload "
                         "Credentials hands the token back.")
    # All non-secret. That is the whole point -- everything needed to reach Workload
    # Credentials in `entra` mode is configuration, and the credential is not.
    ap.add_argument("--wlc-base-url", default=os.environ.get("AGENT_WLC_BASE_URL", ""))
    ap.add_argument("--wlc-site-id", default=os.environ.get("AGENT_WLC_SITE_ID", ""))
    ap.add_argument("--wlc-service-name",
                    default=os.environ.get("AGENT_WLC_SERVICE_NAME", ""))
    ap.add_argument("--wlc-resource", default=os.environ.get("AGENT_WLC_RESOURCE", ""))
    ap.add_argument("--wlc-secret-name",
                    default=os.environ.get("AGENT_WLC_SECRET_NAME", ""))
    ap.add_argument("--wlc-folder", default=os.environ.get("AGENT_WLC_FOLDER", ""))
    ap.add_argument("--wlc-client-id", default=os.environ.get("AGENT_WLC_CLIENT_ID", ""))
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
                          "token_source": args.token_source}))
        return 0
    if not args.url:
        raise SystemExit("[agent] FATAL: --url (or AGENT_MCP_URL) is required.")

    if args.token_source == "wlc":
        missing = [n for n, v in (("--wlc-base-url", args.wlc_base_url),
                                  ("--wlc-site-id", args.wlc_site_id),
                                  ("--wlc-service-name", args.wlc_service_name),
                                  ("--wlc-resource", args.wlc_resource),
                                  ("--wlc-secret-name", args.wlc_secret_name))
                   if not v]
        if missing:
            # Named individually rather than "check your config": every one of these is
            # non-secret, so there is no reason to be vague about which is absent.
            raise SystemExit("[agent] FATAL: --token-source wlc needs "
                             + ", ".join(missing))
        token = fetch_token_from_wlc(
            base_url=args.wlc_base_url, site_id=args.wlc_site_id,
            service_name=args.wlc_service_name, resource=args.wlc_resource,
            secret_name=args.wlc_secret_name, folder=args.wlc_folder,
            client_id=args.wlc_client_id)
    else:
        token = read_token(args.token_file)

    return run(args.url, token, args.tool, args.spiffe_socket, args.interval,
               token_source=args.token_source)


if __name__ == "__main__":
    sys.exit(main())
