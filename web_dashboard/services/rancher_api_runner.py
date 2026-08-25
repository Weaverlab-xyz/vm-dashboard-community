"""Rancher API over an **in-cloud runner** — the corp-TLS-inspection escape hatch.

Corp proxies that TLS-inspect (e.g. Cloudflare Gateway) reject the Rancher node's
self-signed cert at the PROXY's origin-side verification, killing every direct
HTTPS call from the dashboard (readiness poll, bootstrap, import API) — client-side
``verify=False`` cannot bypass a proxy-side block. Live-diagnosed 2026-07-21: the
node answered ``/ping`` over plain HTTP and over an IAP tunnel, while direct HTTPS
died at ClientHello 100% of the time.

Fix: when ``rancher_api_transport = runner``, each Rancher HTTP call executes as a
one-shot ``curl`` inside an in-cloud job (the same corp-CA-dodging pattern as the
Ansible / k8s / promote cloud runners — see ``k8s_runner_service``), which egresses
from the cloud with no inspecting proxy in the path. The job rides the shared
k8s-runner plumbing (stock ``dtzar/helm-kubectl`` image — it ships curl — output
through the cloud's own log, secrets in env not argv) and targets the node's
INTERNAL address, so the runner needs VPC reach to it:

  * **GCP** — a Cloud Run job, over direct VPC egress or a Serverless VPC Access
    connector. Its egress is private-ranges-only, so RFC1918 is exactly what routes;
    a public IP would not.
  * **AWS** — an ECS Fargate task in the node's own VPC, reaching its private IP.
  * **Azure** — an ACI container group on a VNet-delegated subnet in the node's VNet.

The backend is NOT configured separately: it follows the cloud the node is in,
because the whole point is to reach that node's private address from inside its own
network. A node on a cloud with no runner implementation says so rather than
launching a job that cannot route.

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


def _node_cloud() -> str:
    """Which cloud the Rancher node is in. The runner has to be there too."""
    try:
        from . import managed_node_service
        return managed_node_service.node_cloud(managed_node_service.RANCHER)
    except Exception:  # noqa: BLE001 — config unreadable; the GCP default is the
        return "gcp"   # only node that can exist on an install this old


def _node_region(cloud: str = "") -> str:
    """The region the Rancher node lives in, derived from the zone the deploy
    persisted for that cloud.

    Recorded on every deploy BEFORE the readiness poll, so it is the authoritative
    node region at every runner call. ``""`` when unknown (no node deployed yet, or
    config unreadable) — the caller then keeps the runner's default region.

    The two clouds spell a zone differently, and neither split is safe on the other:
    GCP is ``<region>-<letter>`` (``us-east1-b``), AWS is ``<region><letter>``
    (``us-east-1a``), so ``us-east-1a`` naively rsplit on ``-`` would yield ``us-east``.
    """
    cloud = cloud or _node_cloud()
    try:
        from . import config_service
        zone = (config_service.get(f"{cloud}_rancher_zone") or "").strip()
    except Exception:
        return ""
    if not zone:
        return ""
    if cloud == "gcp":
        # A zone is region-plus-a-suffix (two hyphens: "us-east1-b"); anything else
        # (a bare region, junk) is not safely splittable → fall back.
        return zone.rsplit("-", 1)[0] if zone.count("-") >= 2 else ""
    if cloud == "aws":
        return zone[:-1] if zone[-1:].isalpha() else zone
    return ""


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
    """GCP Cloud Run knobs (the ``gcp`` backend of :func:`_run`) — reuse the k8s runner's resolution (same project /
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
    node_region = _node_region("gcp")
    if using_direct and node_region and node_region != cfg.get("region"):
        logger.info("Rancher runner: pinning Cloud Run region to the node's region %s "
                    "(was %s) — direct VPC egress is region-locked", node_region, cfg.get("region"))
        cfg = {**cfg, "region": node_region,
               "vpc_subnetwork": _retarget_region(cfg.get("vpc_subnetwork", ""), node_region)}
    return cfg


def _resolve_ecs():
    """ECS Fargate knobs, re-pointed at the NODE's region.

    Reuses the k8s runner's ECS plumbing so a runner install needs nothing new, then
    overrides the network from the node's own per-region config: a task in the default
    region's VPC has no route to a node's private IP in another region, and the
    failure is a silent dropped SYN rather than an error — the same defect that had to
    be fixed for Cloud Run's region-locked direct egress.

    The node's ingress rule must admit the task's private address, which is what
    ``rancher_runner_source_cidr`` is for (set it to the runner subnet's or the VPC's
    CIDR). It is auto-merged into the allow-list while the transport is ``runner``.
    """
    from . import k8s_runner_service, region_config
    try:
        cfg = k8s_runner_service._resolve_ecs()
    except Exception as exc:
        raise RancherRunnerError(
            f"Rancher API runner (ECS) is not configured: {exc}") from exc
    node_region = _node_region("aws")
    if node_region and node_region != cfg.get("region"):
        rc = region_config.resolve_region("aws", node_region) or {}
        subnet = (rc.get("ecs_subnet_id") or "").strip()
        if not subnet:
            raise RancherRunnerError(
                f"The Rancher node is in {node_region} but no runner subnet is configured "
                f"there (ansible_ecs_subnet_id for that region), so an ECS task has no "
                f"route to the node's private IP. Add a per-region config, or move the "
                f"node.")
        sgs = [s.strip() for s in (rc.get("ecs_security_group_ids") or "").split(",")
               if s.strip()]
        logger.info("Rancher runner: pinning the ECS task to the node's region %s "
                    "(was %s) — a task in another VPC cannot reach the node",
                    node_region, cfg.get("region"))
        cfg = {**cfg, "region": node_region, "subnet_id": subnet,
               "security_group_ids": sgs or cfg.get("security_group_ids", []),
               "cluster": (rc.get("ecs_cluster") or cfg.get("cluster"))}
    return cfg


def _resolve_aci():
    """ACI knobs, reusing the k8s runner's resolution.

    The container group must sit on a VNet-DELEGATED subnet in the node's VNet, or it
    has no route to the node's private address -- the same requirement the k8s runner
    already has for private cluster APIs, which is why its subnet fallback chain is
    reused rather than a second one invented. Azure has no cross-region wrinkle to undo
    here: the resource group and location already come from the node's own placement.

    The node's NSG must admit the container group's address, which is what
    ``rancher_runner_source_cidr`` is for (set it to the runner subnet's CIDR). It is
    auto-merged into the allow-list while the transport is ``runner``.
    """
    from . import k8s_runner_service
    try:
        cfg = k8s_runner_service._resolve_aci()
    except Exception as exc:
        raise RancherRunnerError(
            f"Rancher API runner (ACI) is not configured: {exc}") from exc
    if not cfg.get("subnet_id"):
        raise RancherRunnerError(
            "rancher_api_transport=runner needs VNet reach to the node's internal IP: "
            "set ansible_aci_subnet_id (or azure_aci_subnet_id) to a VNet-delegated "
            "subnet in the node's VNet. Without it the container group runs with a "
            "public address and cannot route to the node.")
    return cfg


async def _run(command: str, *, stdin_b64: str = "", job_id: str = "") -> tuple:
    """Run one shell command in an in-cloud runner job; return ``(exit_code, output)``.

    The COMMANDS are cloud-independent (a shell string in a stock helm-kubectl image),
    so only the launcher differs — which is why both callers below share this rather
    than each carrying its own per-cloud fan-out.
    """
    cloud = _node_cloud()
    if cloud == "gcp":
        from . import gcp_service
        cfg = _resolve()
        return await gcp_service.run_cloud_run_k8s_task(
            project_id=cfg["project_id"], region=cfg["region"], image=cfg["image"],
            command=command, kubeconfig_b64=_DUMMY_KUBECONFIG_B64,
            stdin_b64=stdin_b64, job_id=job_id, vpc_connector=cfg["vpc_connector"],
            vpc_network=cfg.get("vpc_network", ""),
            vpc_subnetwork=cfg.get("vpc_subnetwork", ""))
    if cloud == "aws":
        from . import aws_service
        cfg = _resolve_ecs()
        return await aws_service.run_ecs_k8s_task(
            region=cfg["region"], cluster=cfg["cluster"],
            task_family=cfg["task_family"], image=cfg["image"],
            cpu=cfg["cpu"], memory=cfg["memory"], subnet_id=cfg["subnet_id"],
            security_group_ids=cfg["security_group_ids"],
            execution_role_arn=cfg["execution_role_arn"],
            command=command, kubeconfig_b64=_DUMMY_KUBECONFIG_B64,
            stdin_b64=stdin_b64, job_id=job_id)
    if cloud == "azure":
        from . import azure_service
        cfg = _resolve_aci()
        return await azure_service.run_aci_k8s_task(
            rg=cfg["rg"], location=cfg["location"], subnet_id=cfg["subnet_id"],
            image=cfg["image"], command=command,
            kubeconfig_b64=_DUMMY_KUBECONFIG_B64, stdin_b64=stdin_b64, job_id=job_id,
            acr_server=cfg["acr_server"], acr_username=cfg["acr_username"],
            acr_password=cfg["acr_password"])
    raise RancherRunnerError(
        f"rancher_api_transport=runner is not implemented for a node on {cloud!r}. "
        f"Use rancher_api_transport=direct, or host the node on a cloud that has a "
        f"runner (aws, azure, gcp).")


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
    exit_code, output = await _run(command, stdin_b64=stdin_b64, job_id=job_id)
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
    exit_code, output = await _run(command, job_id=job_id)
    if _READY in (output or ""):
        return "ready"
    if exit_code != 0 and _NOT_READY not in (output or ""):
        raise RancherRunnerError(
            f"Rancher readiness runner job exited {exit_code}. Log tail:\n{(output or '').strip()[-1500:]}")
    logger.warning("Rancher runner readiness timed out against %s (node cloud %s) — "
                   "probe tail:\n%s", ping, _node_cloud(), (output or "").strip()[-800:])
    return "timeout"
