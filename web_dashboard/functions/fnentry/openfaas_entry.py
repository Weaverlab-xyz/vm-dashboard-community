"""Self-hosted entry point — OpenFaaS, Nuclio, or a plain Kubernetes Deployment.

Packaged at the zip root as ``openfaas_entry.py``. Unlike the three cloud shims this
one owns a real HTTP server, because there is no platform to own it: of-watchdog runs
in ``mode=http`` and forwards each request to ``upstream_url``, which is this process.

Routes — and, as on Azure, **the verb IS the path**:

  ANY  /<anything>     the workload. An Entitle Remote Adapter distinguishes
                       ``/give_access`` from ``/revoke_access`` by path alone, so a
                       single fixed route would make every operation identical. The
                       OpenFaaS gateway strips ``/function/<name>`` and forwards the
                       residual path, so a workload sees what it sees on the clouds.
  GET  /_/health       liveness. Answered HERE, before ``dispatch`` and therefore
                       before the shared-secret gate, and it touches no workload
                       state. That is deliberate: a readiness probe must not need a
                       credential, and it must never be able to mint an account.
                       The ``/_/`` prefix is not a path any Entitle route uses, so
                       it cannot shadow one.

Two bindings that are security decisions rather than defaults:

* **Loopback only.** of-watchdog is in the same container, so ``127.0.0.1`` is the
  whole reachable surface. Binding ``0.0.0.0`` would publish this port directly the
  moment anything put a Service in front of the pod — and this port has no
  front door at all, only the inner shared-secret gate.
* **The gate is unchanged.** OpenFaaS's basic auth protects ``/system/*``, not
  ``/function/*``; anything on the cluster can reach the gateway. So the inner gate
  in ``fnruntime.auth`` is the ONLY gate, which is exactly what it is built to be
  (see its module docstring) — nothing here may bypass or soften it.

Stdlib only, like everything it imports.
"""
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import workload
from fnruntime import adapters, dispatch

# of-watchdog's ``upstream_url`` default in the baked image. Overridable so the same
# shim runs behind Nuclio or a plain Deployment, which choose their own port.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5000

HEALTH_PATH = "/_/health"

# A body larger than this is refused unread. The largest thing any Entitle route
# sends is a ``create_actor`` payload carrying one Asset, i.e. kilobytes — so this is
# three orders of magnitude of headroom, and its only job is to stop an unauthorized
# caller (the gate runs AFTER the body is read, because the credential may be in a
# header of a request whose body we must still consume) from making the function
# allocate whatever it claims in Content-Length.
MAX_BODY_BYTES = 1 << 20


def serve_http(method, path, headers, body):
    """One request: translate → dispatch → render. The entire entry point.

    Factored out of the handler so it can be driven from a test with three strings
    and no socket. It holds no decisions of its own — ``dispatch.handle_request`` is
    the single decision point on every platform, which is what stops the four
    runtimes drifting apart.
    """
    request = adapters.from_http(method, path, headers, body)
    response = dispatch.handle_request(request, workload)
    return adapters.to_http(response)


class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 so of-watchdog can keep the connection alive between the many calls
    # one Entitle grant makes. It obliges us to send an accurate Content-Length on
    # every response, which ``adapters.to_http`` computes from the encoded bytes.
    protocol_version = "HTTP/1.1"
    server_version = "fnruntime"
    sys_version = ""

    def log_message(self, fmt, *args):  # noqa: A003 - BaseHTTPRequestHandler's name
        """Silence the default access log.

        ``fnruntime.logs`` already emits one structured line per request, with the
        request id, the workload, the status and the duration. The stock line adds
        none of that and interleaves a second format into the same stream, which is
        what makes a function's logs unreadable exactly when they are needed.
        """

    def _content_length(self) -> int:
        try:
            return int(self.headers.get("content-length") or 0)
        except (TypeError, ValueError):
            return 0

    def _read_body(self, length: int) -> bytes:
        return self.rfile.read(length) if length > 0 else b""

    def _respond(self, status: int, headers: dict, payload: bytes) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _serve(self) -> None:
        if self.command == "GET" and self.path.split("?", 1)[0] == HEALTH_PATH:
            payload = b'{"ok":true}'
            self._respond(200, {"content-type": "application/json",
                                "content-length": str(len(payload))}, payload)
            return
        length = self._content_length()
        if length > MAX_BODY_BYTES:
            # Refused UNREAD, so the oversized body is never allocated — and the
            # connection is closed rather than kept alive, because those bytes are
            # still queued on the socket. Answering on a kept-alive connection
            # would leave the next read starting mid-body, which the parser sees as
            # a malformed request line: the caller gets a 400 for a request it
            # never made, on a request it never sent.
            payload = b'{"error":"request too large"}'
            self.close_connection = True
            self._respond(413, {"content-type": "application/json",
                                "content-length": str(len(payload)),
                                "connection": "close"}, payload)
            return
        body = self._read_body(length)
        # ``serve_http`` cannot raise — dispatch turns a workload exception into a
        # 500 with a request id — so there is no except here on purpose. Something
        # that escaped anyway is a bug in dispatch itself, and letting the server's
        # own handler log it is more useful than a 500 that hides which layer broke.
        status, headers, payload = serve_http(self.command, self.path,
                                              self.headers, body)
        self._respond(status, headers, payload)

    # Every verb routes to the same place, because the workload dispatches on the
    # path. Entitle uses GET and POST; PUT and DELETE are here so a workload that
    # adds one later does not need a change in the shim.
    do_GET = _serve
    do_POST = _serve
    do_PUT = _serve
    do_DELETE = _serve


def main(argv=None) -> int:
    host = (os.environ.get("OTFN_BIND") or DEFAULT_HOST).strip() or DEFAULT_HOST
    try:
        port = int(os.environ.get("OTFN_PORT") or DEFAULT_PORT)
    except ValueError:
        port = DEFAULT_PORT
    # Threading, because one Entitle grant is several sequential calls and a single
    # slow FUXA request must not block the health probe behind it — a liveness
    # timeout would restart the pod mid-grant, leaving the account it just minted
    # with nothing to report back.
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
