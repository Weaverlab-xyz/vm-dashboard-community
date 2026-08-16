"""Connectivity and placement probe.

Answers the question every VPC-attached-serverless debugging session opens with:
*is this function actually where I think it is, and if it can't reach the target,
is that DNS, routing, or the security group?* Today that means correlating three
different cloud consoles.

It is also this feature's own end-to-end verifier — steps 4-8 of the build order
are all "deploy echo_diag and curl it":

    curl -sS -X POST "$FN_URL" -H "Authorization: Bearer $SECRET" \
         -H 'content-type: application/json' \
         -d '{"probe":[{"host":"mydb.internal","port":5432}]}'

Reading the result:

  dns=failed              the resolver answered, and said no such name
  dns=timeout             the resolver never answered — on Azure this is almost
                          always a missing WEBSITE_DNS_SERVER (private DNS zones
                          don't resolve without it, even when routing is correct)
  dns=ok, connect=timeout routing or the security group / NSG
  connect=refused         you got there; nothing is listening on that port
  connect=ok              the path works
  egress.connect=timeout  no outbound internet — a VPC-attached Lambda needs NAT

``dns_ms`` and ``connect_ms`` are reported separately because slow-but-working is a
different problem from broken, and the two are constantly confused.

Stdlib only.
"""
import errno
import os
import socket
import threading
import time

from fnruntime import logs
from fnruntime.contract import Context, Request, Response

NAME = "echo_diag"
DESCRIPTION = "Connectivity and placement probe: what can this function reach?"

_DEFAULT_TIMEOUT = 3.0
_MAX_TIMEOUT = 10.0
_MAX_PROBES = 20
# Exercises DNS and outbound routing in one shot. Overridable for air-gapped
# estates where reaching the public internet is not the expected outcome.
_DEFAULT_EGRESS = os.environ.get("FN_EGRESS_PROBE", "example.com:443")


def _resolve(host: str, port: int, timeout: float) -> tuple:
    """``(addrinfo_list, error)`` — ``getaddrinfo`` with a real time bound.

    The system resolver ignores socket timeouts entirely, so a VPC whose DNS is
    misrouted (exactly the Azure "no WEBSITE_DNS_SERVER" case this workload exists
    to diagnose) would otherwise hang for the function's WHOLE invocation budget
    and return nothing useful. Resolving on a daemon thread and joining with a
    timeout turns that into a reported ``dns=timeout`` in a bounded time; the
    orphaned thread cannot keep the process alive.
    """
    result = {}

    def _run():
        try:
            result["infos"] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except Exception as exc:                       # gaierror and friends
            result["error"] = str(getattr(exc, "strerror", None) or exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return None, f"no answer within {timeout:g}s (resolver unreachable)"
    return result.get("infos"), result.get("error")


def _ipv4_first(infos: list) -> list:
    """Prefer A records over AAAA. Most private estates are IPv4-only, and picking
    an unroutable IPv6 address first turns a working path into a timeout."""
    return sorted(infos, key=lambda info: 0 if info[0] == socket.AF_INET else 1)


def _probe(host: str, port: int, timeout: float) -> dict:
    """Resolve, then TCP-connect, timing and reporting the two **separately** —
    which is the entire point: "it doesn't work" is not an actionable answer."""
    out = {"host": host, "port": port}

    started = time.time()
    infos, error = _resolve(host, port, timeout)
    out["dns_ms"] = int((time.time() - started) * 1000)

    if error and infos is None and "within" in error:
        out["dns"] = "timeout"
        out["error"] = error
        return out
    if error or not infos:
        out["dns"] = "failed"
        out["error"] = error or "no addresses returned"
        return out

    infos = _ipv4_first(infos)
    out["dns"] = "ok"
    out["resolved"] = [str(info[4][0]) for info in infos]

    family, socktype, proto, _canon, sockaddr = infos[0]
    out["tried"] = str(sockaddr[0])
    connect_started = time.time()
    sock = socket.socket(family, socktype, proto)
    sock.settimeout(timeout)
    try:
        sock.connect(sockaddr)
        out["connect"] = "ok"
    except (socket.timeout, TimeoutError):
        out["connect"] = "timeout"
        out["error"] = f"no response within {timeout:g}s (routing or security group)"
    except OSError as exc:
        if exc.errno == errno.ECONNREFUSED:
            # Reached the host — the network path is fine, nothing is listening.
            out["connect"] = "refused"
        else:
            out["connect"] = "failed"
        out["error"] = str(exc.strerror or exc)
    finally:
        try:
            sock.close()
        except OSError:
            pass

    out["connect_ms"] = int((time.time() - connect_started) * 1000)
    return out


def _parse_target(raw) -> tuple:
    """Accept ``{"host": h, "port": p}`` or the ``"host:port"`` shorthand."""
    if isinstance(raw, dict):
        host = str(raw.get("host") or "").strip()
        try:
            port = int(raw.get("port") or 443)
        except (TypeError, ValueError):
            port = 443
        return host, port
    text = str(raw or "").strip()
    if ":" in text:
        host, _, port_text = text.rpartition(":")
        try:
            return host.strip(), int(port_text)
        except ValueError:
            return text, 443
    return text, 443


def _placement() -> dict:
    """Where the platform thinks this function is, from the env the Terraform
    modules inject plus each cloud's own runtime variables."""
    env = os.environ
    return {
        "network_mode": env.get("FN_NETWORK_MODE", ""),
        "declared_subnets": env.get("FN_SUBNETS", ""),
        "declared_network": env.get("FN_NETWORK", ""),
        "aws_region": env.get("AWS_REGION", ""),
        "aws_execution_env": env.get("AWS_EXECUTION_ENV", ""),
        "azure_website_name": env.get("WEBSITE_SITE_NAME", ""),
        "azure_dns_server": env.get("WEBSITE_DNS_SERVER", ""),
        "azure_vnet_route_all": env.get("WEBSITE_VNET_ROUTE_ALL", ""),
        "gcp_service": env.get("K_SERVICE", ""),
        "hostname": socket.gethostname(),
    }


def handle(req: Request, ctx: Context) -> Response:
    payload = req.json()

    try:
        timeout = min(float(payload.get("timeout") or _DEFAULT_TIMEOUT), _MAX_TIMEOUT)
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT

    targets = payload.get("probe") or []
    if isinstance(targets, (str, dict)):
        targets = [targets]
    probes = []
    for raw in list(targets)[:_MAX_PROBES]:
        host, port = _parse_target(raw)
        if host:
            probes.append(_probe(host, port, timeout))

    # On by default: a VPC-attached Lambda without NAT loses outbound internet, and
    # that surprises people far more often than the inbound path does.
    egress = None
    if payload.get("egress", True) and _DEFAULT_EGRESS:
        host, port = _parse_target(_DEFAULT_EGRESS)
        if host:
            egress = _probe(host, port, min(timeout, 2.0))

    return Response(200, {
        "ok": True,
        "context": {
            "request_id": ctx.request_id,
            "workload": ctx.workload or NAME,
            "cloud": ctx.cloud,
            "region": ctx.region,
            "function_name": ctx.function_name,
        },
        "request": {
            "method": req.method,
            "path": req.path,
            "source": req.source,
            # redact() masks `authorization` unconditionally, so echoing headers
            # back is safe — and seeing exactly what survived the front door is
            # most of the value of this workload.
            "headers": logs.redact(dict(req.headers)),
            "query": logs.redact(dict(req.query)),
            "body_bytes": len(req.body),
        },
        "placement": _placement(),
        "probes": probes,
        "egress": egress,
        "duration_ms": ctx.elapsed_ms(),
    })
