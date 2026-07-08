"""Rancher management-plane client — **runner-executed** (no app-side network).

The dashboard app has no route into the cluster VPC, and the central Rancher is
exposed only on an INTERNAL endpoint (no public ingress). So every Rancher API
call is made *from inside the management cluster*: this module builds the shell
commands and a caller-supplied ``run`` coroutine executes them via the k8s
runner (``k8s_runner_service.run`` with the mgmt cluster's kubeconfig). Each call
runs as a throwaway ``kubectl run … curl`` pod that hits Rancher's in-cluster
service ``https://rancher.<namespace>`` — always reachable from within the
cluster, independent of the internal-LB / private-DNS wiring the *agents* and the
*operator* use.

``run`` signature: ``async def run(command: str) -> str`` — returns the command's
combined stdout (the curl response body).

⚠️  VERIFICATION GATE: the Rancher v3 API paths/payloads below are the documented
shapes but have NOT been exercised against a live Rancher in this environment.
Confirm each against the target Rancher version before relying on it — they're
isolated here on purpose so corrections are one-liners.
"""
import json
import logging
import shlex
import uuid

logger = logging.getLogger(__name__)


class RancherError(Exception):
    """Raised when a Rancher API call (via the runner) fails or returns junk."""


def _cfg(key: str, default: str = "") -> str:
    try:
        from . import config_service
        val = config_service.get(key)
        if val:
            return val
    except Exception:
        pass
    from ..config import settings
    return getattr(settings, key, default) or default


def _namespace() -> str:
    return _cfg("rancher_namespace", "cattle-system")


def _base() -> str:
    """In-cluster Rancher service base URL (reachable from a pod in the cluster,
    regardless of the external internal-LB / DNS wiring)."""
    return f"https://rancher.{_namespace()}"


def _curl_pod(curl_args: str, *, ns: str = None) -> str:
    """Build a ``kubectl run`` one-shot that curls Rancher from inside the cluster
    and streams the response body to stdout. ``--quiet`` + ``--rm -i`` keep the
    output to just curl's stdout so the caller can parse JSON. curl ``-sk``:
    silent + skip TLS verify (Rancher's cert is the cert-manager self-signed CA)."""
    ns = ns or _namespace()
    pod = f"rancher-api-{uuid.uuid4().hex[:8]}"
    return (
        f"kubectl run {pod} -n {shlex.quote(ns)} --rm -i --restart=Never --quiet "
        f"--image=curlimages/curl:latest --command -- curl -sk {curl_args}"
    )


def _hdr(token: str = "") -> str:
    h = "-H 'Content-Type: application/json'"
    if token:
        h += f" -H 'Authorization: Bearer {token}'"
    return h


def _body(obj: dict) -> str:
    # Single-quote the JSON for the shell; the JSON itself uses double quotes.
    return "-d " + shlex.quote(json.dumps(obj))


def _extract_json(output: str) -> dict:
    """Pull the JSON object out of the runner's stdout. ``kubectl run --quiet``
    should leave only curl's body, but be defensive: take the last {...} span."""
    if not output:
        raise RancherError("empty response from Rancher API pod")
    s, e = output.find("{"), output.rfind("}")
    if s == -1 or e == -1 or e < s:
        raise RancherError(f"no JSON in Rancher API response: {output[:300]}")
    try:
        return json.loads(output[s:e + 1])
    except json.JSONDecodeError as exc:
        raise RancherError(f"bad JSON from Rancher API: {exc}: {output[:300]}") from exc


async def bootstrap(run, *, bootstrap_password: str, server_url: str) -> str:
    """First-run bootstrap: log in with the bootstrap password, pin the public
    ``server-url`` (what Rancher hands to imported cluster-agents), and mint a
    non-expiring API token. Returns the API token (``token-xxxxx:yyyyy``).
    ``run`` executes each command in the mgmt cluster via the k8s runner."""
    base = _base()
    login = _extract_json(await run(_curl_pod(
        f"-X POST {base}/v3-public/localProviders/local?action=login {_hdr()} "
        f"{_body({'username': 'admin', 'password': bootstrap_password, 'responseType': 'json'})}")))
    login_token = login.get("token")
    if not login_token:
        raise RancherError(f"Rancher bootstrap login returned no token: {str(login)[:200]}")

    # Pin server-url (idempotent PUT). Agents dial this; must be the stable
    # internal hostname, NOT the in-cluster service name.
    await run(_curl_pod(
        f"-X PUT {base}/v3/settings/server-url {_hdr(login_token)} "
        f"{_body({'name': 'server-url', 'value': server_url})}"))

    # Mint a non-expiring API token (ttl=0) for subsequent management calls.
    minted = _extract_json(await run(_curl_pod(
        f"-X POST {base}/v3/token {_hdr(login_token)} "
        f"{_body({'type': 'token', 'description': 'vm-dashboard', 'ttl': 0})}")))
    api_token = minted.get("token")
    if not api_token:
        raise RancherError(f"Rancher token mint returned no token: {str(minted)[:200]}")
    return api_token


async def create_import_cluster(run, *, api_token: str, name: str) -> tuple:
    """Create an *imported* cluster in Rancher + fetch its registration manifest
    URL. Returns ``(rancher_cluster_id, manifest_url)``. The caller applies the
    manifest into the downstream cluster (cattle-cluster-agent dials out)."""
    base = _base()
    created = _extract_json(await run(_curl_pod(
        f"-X POST {base}/v3/cluster {_hdr(api_token)} "
        f"{_body({'type': 'cluster', 'name': name})}")))
    cluster_id = created.get("id")
    if not cluster_id:
        raise RancherError(f"Rancher cluster create returned no id: {str(created)[:200]}")

    reg = _extract_json(await run(_curl_pod(
        f"-X POST {base}/v3/clusterregistrationtoken {_hdr(api_token)} "
        f"{_body({'type': 'clusterRegistrationToken', 'clusterId': cluster_id})}")))
    manifest_url = reg.get("manifestUrl") or reg.get("manifest_url")
    if not manifest_url:
        raise RancherError(
            f"Rancher registration token for {cluster_id} had no manifestUrl: {str(reg)[:200]}")
    return cluster_id, manifest_url


async def delete_cluster(run, *, api_token: str, cluster_id: str) -> None:
    """Remove an imported cluster from Rancher (best-effort; caller logs errors)."""
    base = _base()
    await run(_curl_pod(
        f"-X DELETE {base}/v3/cluster/{shlex.quote(cluster_id)} {_hdr(api_token)}"))
