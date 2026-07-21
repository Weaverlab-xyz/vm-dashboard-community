"""Rancher API over an **in-cloud runner** — the corp-TLS-inspection escape hatch.

Corp proxies that TLS-inspect (e.g. Cloudflare Gateway) reject the Rancher node's
self-signed cert at the PROXY's origin-side verification, killing every direct
HTTPS call from the dashboard (readiness poll, bootstrap, import API) — client-side
``verify=False`` cannot bypass a proxy-side block. Live-diagnosed 2026-07-21: the
node answered ``/ping`` over plain HTTP and over an IAP tunnel, while direct HTTPS
died at ClientHello 100% of the time.

Fix: when ``rancher_api_transport = runner``, each Rancher HTTP call executes as a
one-shot ``curl`` inside a **GCP Cloud Run job** (the same corp-CA-dodging pattern
as the Ansible / k8s / promote cloud runners — see ``k8s_runner_service``), which
egresses from GCP with no inspecting proxy in the path. The job rides the shared
k8s-runner plumbing (``gcp_service.run_cloud_run_k8s_task``: stock
``dtzar/helm-kubectl`` image — it ships curl — output via Cloud Logging, secrets in
env not argv) and targets the node's INTERNAL IP through the Cloud Run VPC
connector (its egress annotation is ``private-ranges-only``, so RFC1918 is exactly
what routes through the connector; a public IP would not).

Request marshalling: method/URL/headers/body travel as a **curl config file** on
the runner's stdin (``STDIN_B64`` → ``curl -K -``), so the API token and payload
never appear in the container's argv. The HTTP response is extracted from the job
log between sentinels plus a trailing ``RANCHER_STATUS:<code>`` write-out.
"""
import base64
import json as _json
import logging

logger = logging.getLogger(__name__)

_BEGIN = "RANCHER_RESP_BEGIN"
_STATUS = "RANCHER_STATUS:"
_READY = "RANCHER_READY"
_NOT_READY = "RANCHER_NOT_READY"

# The runner shell always decodes KUBECONFIG_B64; the API calls don't need one.
_DUMMY_KUBECONFIG_B64 = base64.b64encode(b"# unused by the rancher api runner\n").decode()

_POLL_S = 10
# Cloud Run task timeout is 1200s (gcp_service) — cap the in-container readiness
# loop below it so the poll concludes inside the job instead of being killed.
_MAX_READY_S = 900


class RancherRunnerError(Exception):
    """The runner job could not be launched, or its output had no HTTP response."""


def _resolve():
    """GCP Cloud Run knobs — reuse the k8s runner's resolution (same project /
    region / image / VPC keys) so runner installs need nothing new. Unlike the
    generic k8s runner (which reaches PUBLIC cluster endpoints), the Rancher
    runner dials the node's INTERNAL IP — so VPC reach is REQUIRED: fail fast
    with the exact keys when neither direct VPC egress nor a connector is set
    (without it the job launches, can't route, and burns the whole readiness
    budget before dying with a generic timeout — lived it live 2026-07-21)."""
    from . import k8s_runner_service
    try:
        cfg = k8s_runner_service._resolve_gcp()
    except Exception as exc:
        raise RancherRunnerError(
            f"Rancher API runner (Cloud Run) is not configured: {exc}") from exc
    if not (cfg.get("vpc_network") or cfg.get("vpc_subnetwork") or cfg.get("vpc_connector")):
        raise RancherRunnerError(
            "rancher_api_transport=runner needs VPC reach to the node's internal IP: set "
            "gcp_run_network + gcp_run_subnetwork (direct VPC egress — recommended, no "
            "standing infra) or gcp_ansible_vpc_connector (Serverless VPC Access connector).")
    return cfg


def _q(val: str) -> str:
    """Quote a value for a curl config file (double-quoted, backslash escapes)."""
    return '"' + val.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _curl_config(method: str, url: str, *, token: str = "",
                 json_body=None, timeout_s: int = 30) -> str:
    """Build the ``curl -K -`` config for one API call. Everything sensitive
    (Authorization header, body) lives here — delivered via stdin, not argv."""
    lines = [
        f"url = {_q(url)}",
        f"request = {_q(method.upper())}",
        "insecure",       # the node's self-signed cert — trusted by reachability, like verify=False
        "silent",
        "show-error",
        f"max-time = {int(timeout_s)}",
        f'write-out = "\\n{_STATUS}%{{http_code}}"',
    ]
    if token:
        lines.append(f"header = {_q(f'Authorization: Bearer {token}')}")
    if json_body is not None:
        lines.append(f"header = {_q('Content-Type: application/json')}")
        lines.append(f"data = {_q(_json.dumps(json_body))}")
    return "\n".join(lines) + "\n"


def _parse_response(output: str) -> tuple:
    """Extract ``(status_code, body_text)`` from the job's combined log output.

    The response sits between the LAST ``RANCHER_RESP_BEGIN`` line and the
    ``RANCHER_STATUS:<code>`` write-out (last occurrence wins — Cloud Logging can
    interleave unrelated lines around, but not inside, the curl output)."""
    text = output or ""
    begin = text.rfind(_BEGIN)
    if begin < 0:
        raise RancherRunnerError(
            "Rancher API runner produced no response marker — the curl call likely "
            f"failed before reaching the node. Log tail:\n{text.strip()[-1500:]}")
    chunk = text[begin + len(_BEGIN):]
    at = chunk.rfind(_STATUS)
    if at < 0:
        raise RancherRunnerError(
            "Rancher API runner returned no HTTP status — transport failure between "
            f"the runner and the node. Log tail:\n{chunk.strip()[-1500:]}")
    status_str = chunk[at + len(_STATUS):].strip().split()[0] if chunk[at + len(_STATUS):].strip() else ""
    try:
        status = int(status_str[:3])
    except ValueError:
        raise RancherRunnerError(f"Rancher API runner emitted a malformed status: {status_str!r}")
    body = chunk[:at].strip("\n")
    return status, body


async def request(method: str, url: str, *, token: str = "",
                  json_body=None, timeout_s: int = 30, job_id: str = "") -> tuple:
    """Execute one Rancher API call in a Cloud Run job; return ``(status, body)``.

    ``url`` must be the node's INTERNAL URL (``rancher_internal_url``) — the
    connector only carries private ranges. Raises :class:`RancherRunnerError` on
    launch/transport failure (an HTTP error status is returned, not raised —
    callers keep their own status handling, same as the direct path)."""
    from . import gcp_service
    cfg = _resolve()
    command = (f"echo {_BEGIN} && " + "{ curl -sS -K - || true; } && echo")
    stdin_b64 = base64.b64encode(
        _curl_config(method, url, token=token, json_body=json_body,
                     timeout_s=timeout_s).encode()).decode()
    exit_code, output = await gcp_service.run_cloud_run_k8s_task(
        project_id=cfg["project_id"], region=cfg["region"], image=cfg["image"],
        command=command, kubeconfig_b64=_DUMMY_KUBECONFIG_B64,
        stdin_b64=stdin_b64, job_id=job_id, vpc_connector=cfg["vpc_connector"],
        vpc_network=cfg.get("vpc_network", ""), vpc_subnetwork=cfg.get("vpc_subnetwork", ""))
    # curl is ||-guarded so the job exits 0 even on transport failure; the parse
    # below is what distinguishes an HTTP response from a dead transport.
    if exit_code != 0:
        raise RancherRunnerError(
            f"Rancher API runner job exited {exit_code}. Log tail:\n{(output or '').strip()[-1500:]}")
    return _parse_response(output)


async def wait_ready(url: str, timeout_s: int, *, job_id: str = "") -> str:
    """Poll ``<url>/ping`` from INSIDE one Cloud Run job (a single job runs the
    whole retry loop — one job per probe would burn ~30s of cold-start each).
    Returns ``"ready"`` or ``"timeout"``."""
    from . import gcp_service
    cfg = _resolve()
    tries = max(1, min(int(timeout_s), _MAX_READY_S) // _POLL_S)
    ping = f"{url.rstrip('/')}/ping"
    command = (
        f"for i in $(seq 1 {tries}); do "
        f"curl -sk -m 5 {ping} >/dev/null 2>&1 && {{ echo {_READY}; exit 0; }}; "
        f"sleep {_POLL_S}; done; echo {_NOT_READY}"
    )
    exit_code, output = await gcp_service.run_cloud_run_k8s_task(
        project_id=cfg["project_id"], region=cfg["region"], image=cfg["image"],
        command=command, kubeconfig_b64=_DUMMY_KUBECONFIG_B64,
        stdin_b64="", job_id=job_id, vpc_connector=cfg["vpc_connector"],
        vpc_network=cfg.get("vpc_network", ""), vpc_subnetwork=cfg.get("vpc_subnetwork", ""))
    if _READY in (output or ""):
        return "ready"
    if exit_code != 0 and _NOT_READY not in (output or ""):
        raise RancherRunnerError(
            f"Rancher readiness runner job exited {exit_code}. Log tail:\n{(output or '').strip()[-1500:]}")
    return "timeout"
