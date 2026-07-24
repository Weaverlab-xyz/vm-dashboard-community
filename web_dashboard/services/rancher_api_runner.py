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

# The whole HTTP response travels as ONE atomic text line:
#   RANCHER_B64:<base64 body>:RC:<http code>
# Two Cloud Logging behaviours force this shape (both bit live 2026-07-21): a log
# line that parses as JSON is ingested as structured jsonPayload — so a raw JSON
# response body VANISHES from textPayload-based log assembly — and lines emitted
# within the same instant can come back reordered. One base64 (never valid JSON,
# no ':' in its alphabet) line sidesteps both.
_B64_MARK = "RANCHER_B64:"
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


def _node_region() -> str:
    """The GCP region the Rancher node lives in, from the persisted node zone.

    Set on every deploy (``gcp_rancher_zone``, e.g. ``us-east1-b`` → ``us-east1``)
    BEFORE the readiness poll, so it's the authoritative node region at every
    runner call. ``""`` when unknown (no node deployed yet, or config unreadable) —
    the caller then keeps the default ``gcp_region``."""
    try:
        from . import config_service
        zone = (config_service.get("gcp_rancher_zone") or "").strip()
    except Exception:
        return ""
    # A zone is region-plus-a-suffix (two hyphens: "us-east1-b"); anything else
    # (a bare region, junk) is not safely splittable → fall back.
    return zone.rsplit("-", 1)[0] if zone.count("-") >= 2 else ""


def _retarget_region(subnet_ref: str, region: str) -> str:
    """Point a subnetwork ref at ``region``. A bare name (``dashboard-sandbox-…``)
    is region-agnostic — Cloud Run resolves it in the job's region — so it's
    returned as-is. A regional self-link (``…/regions/<X>/subnetworks/<name>``) has
    its region segment rewritten so the job's NIC lands in a subnet that actually
    exists in the runner's region."""
    import re
    if not subnet_ref or "/regions/" not in subnet_ref:
        return subnet_ref
    return re.sub(r"/regions/[^/]+/", f"/regions/{region}/", subnet_ref, count=1)


def _resolve():
    """GCP Cloud Run knobs — reuse the k8s runner's resolution (same project /
    region / image / VPC keys) so runner installs need nothing new. Unlike the
    generic k8s runner (which reaches PUBLIC cluster endpoints), the Rancher
    runner dials the node's INTERNAL IP — so VPC reach is REQUIRED: fail fast
    with the exact keys when neither direct VPC egress nor a connector is set
    (without it the job launches, can't route, and burns the whole readiness
    budget before dying with a generic timeout — lived it live 2026-07-21).

    Cloud Run **Direct VPC egress** reaches only **same-region** internal IPs: a
    runner in the primary ``gcp_region`` cannot reach a node deployed in another
    region — the SYN to the node's internal IP is silently dropped and the probe
    just times out (diagnosed live 2026-07-24: a us-central1 runner timed out on a
    us-east1 node's 10.102.x IP, while the same probe from a us-east1 runner
    handshook instantly). Multi-region Rancher (#398) puts the node in any region,
    so PIN the direct-egress runner to the NODE's region. The bare subnet name
    resolves per-region, and the VPC's internal-allow rule (the /12 supernet) admits
    whichever regional jumpoint subnet the runner then lands in. A **VPC Access
    connector** is left on ``gcp_region`` — a connector can reach any region in the
    VPC, and it must stay co-located with the Cloud Run job's region."""
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
    # Direct VPC egress wins over a connector in run_cloud_run_k8s_task (it's used
    # whenever a network/subnet is set), so mirror that test here before overriding.
    using_direct = bool(cfg.get("vpc_network") or cfg.get("vpc_subnetwork"))
    node_region = _node_region()
    if using_direct and node_region and node_region != cfg.get("region"):
        logger.info("Rancher runner: pinning Cloud Run region to the node's region %s "
                    "(was %s) — direct VPC egress is region-locked", node_region, cfg.get("region"))
        cfg = {**cfg, "region": node_region,
               "vpc_subnetwork": _retarget_region(cfg.get("vpc_subnetwork", ""), node_region)}
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
    ]
    if token:
        lines.append(f"header = {_q(f'Authorization: Bearer {token}')}")
    if json_body is not None:
        lines.append(f"header = {_q('Content-Type: application/json')}")
        lines.append(f"data = {_q(_json.dumps(json_body))}")
    return "\n".join(lines) + "\n"


_B64_LINE_RE = None  # compiled lazily (keeps `re` out of the module's hot import)


def _parse_response(output: str) -> tuple:
    """Extract ``(status_code, body_text)`` from the job's combined log output.

    The response is ONE ``RANCHER_B64:<b64 body>:RC:<code>`` line (last occurrence
    wins). A missing line = curl never produced a response (transport failure);
    a present line with an empty code = the request died before an HTTP status."""
    global _B64_LINE_RE
    import re
    if _B64_LINE_RE is None:
        _B64_LINE_RE = re.compile(r"RANCHER_B64:([A-Za-z0-9+/=]*):RC:(\d{3})")
    matches = _B64_LINE_RE.findall(output or "")
    if not matches:
        if _B64_MARK in (output or ""):
            raise RancherRunnerError(
                "Rancher API runner returned no HTTP status — transport failure between "
                f"the runner and the node. Log tail:\n{(output or '').strip()[-1500:]}")
        raise RancherRunnerError(
            "Rancher API runner produced no response marker — the curl call likely "
            f"failed before reaching the node. Log tail:\n{(output or '').strip()[-1500:]}")
    b64_body, code = matches[-1]
    try:
        body = base64.b64decode(b64_body).decode("utf-8", "replace") if b64_body else ""
    except Exception:
        raise RancherRunnerError("Rancher API runner emitted a malformed response body line.")
    return int(code), body


async def request(method: str, url: str, *, token: str = "",
                  json_body=None, timeout_s: int = 30, job_id: str = "") -> tuple:
    """Execute one Rancher API call in a Cloud Run job; return ``(status, body)``.

    ``url`` must be the node's INTERNAL URL (``rancher_internal_url``) — the
    connector only carries private ranges. Raises :class:`RancherRunnerError` on
    launch/transport failure (an HTTP error status is returned, not raised —
    callers keep their own status handling, same as the direct path)."""
    from . import gcp_service
    cfg = _resolve()
    # The runner shell prepends `printf %s "$STDIN_B64" | base64 -d | ` to this
    # command, and a pipe binds to the FIRST simple command only — so the whole
    # thing must be ONE brace group with curl first, so `curl -K -` receives the
    # piped config (an earlier `echo && { curl ...; }` chain fed it to echo —
    # "no URL specified", caught live). The response is then re-emitted as the
    # single atomic RANCHER_B64 line (see the sentinel comment up top).
    command = (
        "{ curl -sS -K - -o /tmp/rancher_body -w '%{http_code}' > /tmp/rancher_code || true; "
        'printf "RANCHER_B64:%s:RC:%s\\n" "$(base64 -w0 /tmp/rancher_body 2>/dev/null)" '
        '"$(cat /tmp/rancher_code)"; }'
    )
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
    # On exhaustion, run one VERBOSE probe whose stderr is kept — so a timeout log
    # shows the ACTUAL reason (e.g. "Connection timed out" = no route/dropped SYN,
    # the cross-region direct-egress signature; "Connection refused" = reachable but
    # not yet serving; a TLS line = up). Without it a runner timeout is silent and
    # indistinguishable from the node merely being slow to boot.
    command = (
        f"for i in $(seq 1 {tries}); do "
        f"curl -sk -m 5 {ping} >/dev/null 2>&1 && {{ echo {_READY}; exit 0; }}; "
        f"sleep {_POLL_S}; done; "
        f"echo {_NOT_READY}; echo '--- final probe ---'; curl -sk -m 8 -v {ping} 2>&1 | tail -8"
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
    logger.warning("Rancher runner readiness timed out from region %s against %s — probe tail:\n%s",
                   cfg.get("region"), ping, (output or "").strip()[-800:])
    return "timeout"
