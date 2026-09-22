"""The self-hosted function target: the HTTP adapter pair and the entry shim.

The three cloud shims are exercised through fixture objects, because nobody can run
Lambda in a unit test. This one is different and the difference is worth using: the
self-hosted target IS an ordinary HTTP server, so it can be started on a real socket
and driven with ``urllib`` — with nothing installed and no cluster. So these tests
prove the thing the other entry points can only assert about:

  request on a socket → shim → adapters → dispatch → auth → workload → response

Four properties are pinned because each of them, wrong, is a failure that reads as
something else:

1. **The health route needs no credential and reaches no workload.** A readiness
   probe that needed the bearer would restart the pod forever; one that reached the
   workload could mint an account.
2. **The gate still bites.** OpenFaaS's basic auth covers ``/system/*``, not
   ``/function/*``, so ``fnruntime.auth`` is the ONLY gate in front of a
   credential-minting endpoint on a cluster where every pod can reach the gateway.
3. **The path reaches the workload.** For an Entitle Remote Adapter the verb IS the
   path, so a shim that collapsed paths would make every operation identical —
   ``give_access`` and ``revoke_access`` would be indistinguishable.
4. **Keep-alive survives an oversized body.** HTTP/1.1 is on, and answering a
   refused request without closing the connection leaves the next read starting
   mid-body: the caller gets a 400 for a request it never sent.
"""
import importlib.util
import json
import os
import sys
import threading
import types
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from web_dashboard import functions  # noqa: F401  (puts fnruntime on sys.path)
from fnruntime import adapters
from fnruntime.contract import Request, Response

_SECRET = "test-shared-secret"
_ENTRY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                           "web_dashboard", "functions", "fnentry",
                           "openfaas_entry.py")


# ── adapters.from_http / to_http ──────────────────────────────────────────────

def test_from_http_sets_the_openfaas_source():
    req = adapters.from_http("post", "/give_access", {"X-Trace": "1"}, b"{}")
    assert req.source == "openfaas", req.source
    assert req.method == "POST", req.method
    assert req.path == "/give_access", req.path


def test_from_http_lowercases_headers_like_every_other_adapter():
    req = adapters.from_http("GET", "/", {"Authorization": "Bearer x"}, b"")
    assert req.header("AUTHORIZATION") == "Bearer x"
    assert "authorization" in req.headers


def test_from_http_splits_a_query_string_left_on_the_path():
    # of-watchdog hands over the raw request line, so the path still carries it.
    req = adapters.from_http("GET", "/get_assets?fail=500&blank=", None, None)
    assert req.path == "/get_assets", req.path
    assert req.query == {"fail": "500", "blank": ""}, req.query


def test_from_http_prefers_an_already_parsed_query():
    req = adapters.from_http("GET", "/x?a=1", None, None, query={"b": "2"})
    assert req.query == {"b": "2"}, req.query
    assert req.path == "/x"


def test_from_http_accepts_a_str_body():
    req = adapters.from_http("POST", "/", None, '{"a": 1}')
    assert req.json() == {"a": 1}


def test_from_http_tolerates_no_body_and_no_headers():
    req = adapters.from_http("GET", "/", None, None)
    assert req.body == b""
    assert req.json() == {}
    assert req.headers == {}


def test_to_http_returns_bytes_and_an_accurate_content_length():
    status, headers, payload = adapters.to_http(Response(200, {"error": "nope"}))
    assert status == 200, status
    assert isinstance(payload, bytes), type(payload)
    assert headers["content-length"] == str(len(payload)), headers
    assert json.loads(payload.decode("utf-8")) == {"error": "nope"}


def test_to_http_content_length_counts_bytes_not_characters():
    """The case that makes ``to_http`` return bytes rather than text.

    Content-Length is a BYTE count. A body built from ``len(str)`` would understate
    it for anything non-ASCII, and the caller then reads a truncated body — which
    surfaces as a connection reset, not as a malformed response, and so sends you
    looking at the network.

    A *str* body is what reaches this path: ``Response.rendered()`` serializes a
    dict with ``json.dumps``, whose default ``ensure_ascii=True`` escapes non-ASCII
    away, so only a workload returning text directly (an error message, a log
    excerpt) can carry multibyte characters through.
    """
    text = "café ☕"
    status, headers, payload = adapters.to_http(Response(200, text))
    assert status == 200, status
    assert payload == text.encode("utf-8"), payload
    assert len(payload) > len(text), "the fixture is no longer multibyte"
    assert headers["content-length"] == str(len(payload)), (
        f"content-length {headers['content-length']} is not the byte length "
        f"{len(payload)} (character length is {len(text)})")


def test_to_http_empty_body_is_zero_length_not_absent():
    status, headers, payload = adapters.to_http(Response(204, None))
    assert status == 204
    assert payload == b""
    assert headers["content-length"] == "0"


def test_to_http_preserves_the_status_a_workload_chose():
    for code in (200, 400, 401, 403, 500):
        status, _headers, _payload = adapters.to_http(Response(code, {"x": 1}))
        assert status == code, code


# ── the shim, over a real socket ──────────────────────────────────────────────

def _load_shim():
    """Import the shim the way the baked loader does — as a top-level module.

    ``fnentry`` is deliberately not a package (its contents land at the zip root),
    so this mirrors ``runpy.run_module("openfaas_entry")`` rather than inventing an
    import path that only exists in the repo. The fake ``workload`` has to be in
    ``sys.modules`` first, because the shim imports it at module level exactly as
    the three cloud shims do.
    """
    seen = []

    def handle(req, ctx):
        seen.append((req.method, req.path, req.json(), req.source))
        if req.path == "/boom":
            raise RuntimeError("workload exploded")
        return Response(200, {"saw": req.path, "source": req.source})

    fake = types.ModuleType("workload")
    fake.NAME = "fake_workload"
    fake.DESCRIPTION = "test double"
    fake.handle = handle
    sys.modules["workload"] = fake

    spec = importlib.util.spec_from_file_location("openfaas_entry", _ENTRY_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["openfaas_entry"] = module
    spec.loader.exec_module(module)
    return module, seen


class _Server:
    """The shim's own handler on an ephemeral port, in a background thread."""

    def __init__(self, shim):
        from http.server import ThreadingHTTPServer
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), shim._Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=15)


def _call(server, path, *, method="GET", body=None, secret=_SECRET):
    """``(status, parsed_body)``. A 4xx/5xx is a result here, not an exception."""
    data = body.encode("utf-8") if isinstance(body, str) else body
    request = urllib.request.Request(server.url(path), data=data, method=method)
    if secret is not None:
        request.add_header("Authorization", f"Bearer {secret}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, (json.loads(raw) if raw else None)


def _with_server(fn):
    """Run ``fn(server, seen)`` with the secret configured and cleaned up after."""
    from fnruntime import secretref

    previous = os.environ.get("FN_SHARED_SECRET")
    os.environ["FN_SHARED_SECRET"] = _SECRET
    secretref.clear_cache()
    shim, seen = _load_shim()
    server = _Server(shim)
    try:
        fn(server, seen, shim)
    finally:
        server.close()
        if previous is None:
            os.environ.pop("FN_SHARED_SECRET", None)
        else:
            os.environ["FN_SHARED_SECRET"] = previous


def test_health_needs_no_credential_and_reaches_no_workload():
    def check(server, seen, _shim):
        status, body = _call(server, "/_/health", secret=None)
        assert status == 200, status
        assert body == {"ok": True}, body
        assert seen == [], f"the health route invoked the workload: {seen}"
    _with_server(check)


def test_a_request_with_no_credential_is_401_and_never_reaches_the_workload():
    def check(server, seen, _shim):
        status, body = _call(server, "/get_assets", secret=None)
        assert status == 401, status
        assert body == {"error": "unauthorized"}, body
        assert seen == [], f"an unauthenticated request reached the workload: {seen}"
    _with_server(check)


def test_a_wrong_credential_is_byte_identical_to_a_missing_one():
    def check(server, seen, _shim):
        missing_status, missing_body = _call(server, "/get_assets", secret=None)
        wrong_status, wrong_body = _call(server, "/get_assets", secret="nope")
        assert (missing_status, missing_body) == (wrong_status, wrong_body), (
            "probing must not distinguish no credential from a wrong one: "
            f"{missing_status}/{missing_body} vs {wrong_status}/{wrong_body}")
        assert seen == []
    _with_server(check)


def test_the_path_reaches_the_workload_because_the_verb_is_the_path():
    def check(server, seen, _shim):
        for path in ("/get_assets", "/give_access", "/revoke_access", "/delete_actor"):
            status, body = _call(server, path, method="POST", body="{}")
            assert status == 200, (path, status)
            assert body["saw"] == path, (path, body)
        assert [p for _m, p, _b, _s in seen] == [
            "/get_assets", "/give_access", "/revoke_access", "/delete_actor"], seen
    _with_server(check)


def test_the_workload_sees_the_openfaas_source_and_its_json_body():
    def check(server, seen, _shim):
        status, body = _call(server, "/create_actor", method="POST",
                             body='{"role_code": "operator"}')
        assert status == 200, status
        assert body["source"] == "openfaas", body
        method, path, parsed, source = seen[0]
        assert (method, path, source) == ("POST", "/create_actor", "openfaas")
        assert parsed == {"role_code": "operator"}, parsed
    _with_server(check)


def test_a_workload_exception_is_a_500_with_a_request_id_not_a_stack_trace():
    def check(server, seen, _shim):
        status, body = _call(server, "/boom", method="POST", body="{}")
        assert status == 500, status
        assert body.get("error") == "internal error", body
        assert body.get("request_id"), body
        assert "traceback" not in body and "exploded" not in json.dumps(body), body
    _with_server(check)


def test_an_oversized_body_is_refused_unread_and_closes_the_connection():
    """Declares a huge Content-Length and sends NO body.

    That is the precise shape of the property: the refusal must come from the declared
    length, before the server allocates or waits for anything. A test that actually
    sent the bytes would prove less and flake more — the server answers and closes
    while the client is still writing, so the client's own send fails with a
    connection reset instead of returning the 413.
    """
    import http.client

    def check(server, seen, shim):
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
        try:
            conn.putrequest("POST", "/create_actor")
            conn.putheader("Authorization", f"Bearer {_SECRET}")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(shim.MAX_BODY_BYTES + 1))
            conn.endheaders()  # and deliberately no body
            resp = conn.getresponse()
            payload = resp.read()
            assert resp.status == 413, resp.status
            assert json.loads(payload) == {"error": "request too large"}, payload
            assert resp.getheader("connection", "").lower() == "close", (
                "the refusal kept the connection alive, but the declared body is "
                "still queued on the socket: the next read would start mid-body and "
                "the caller would get a 400 for a request it never sent")
        finally:
            conn.close()
        assert seen == [], "an oversized body reached the workload"
        # A fresh connection must still be served normally.
        status, body = _call(server, "/get_assets", method="POST", body="{}")
        assert status == 200, f"the listener did not survive the refusal: {status}"
        assert body["saw"] == "/get_assets", body
    _with_server(check)


def test_keep_alive_serves_several_requests_on_one_connection():
    # One Entitle grant is several sequential calls, and HTTP/1.1 is on precisely so
    # they share a connection. This is also what an inaccurate Content-Length breaks
    # first: the second response would be read as a continuation of the first.
    def check(server, seen, _shim):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
        try:
            for path in ("/get_assets", "/get_all_permissions", "/get_assets"):
                conn.request("POST", path, body=b"{}",
                             headers={"Authorization": f"Bearer {_SECRET}",
                                      "Content-Type": "application/json"})
                resp = conn.getresponse()
                payload = resp.read()
                assert resp.status == 200, (path, resp.status)
                assert json.loads(payload)["saw"] == path, (path, payload)
        finally:
            conn.close()
        assert len(seen) == 3, seen
    _with_server(check)


def test_an_unconfigured_secret_fails_closed_rather_than_serving_openly():
    from fnruntime import secretref

    previous = os.environ.pop("FN_SHARED_SECRET", None)
    secretref.clear_cache()
    shim, seen = _load_shim()
    server = _Server(shim)
    try:
        status, body = _call(server, "/get_assets", secret=None)
        assert status == 500, f"a function with no secret must not answer 200: {status}"
        assert body == {"error": "function not configured"}, body
        assert seen == [], "a function with no secret reached the workload"
        # ...and the health route still answers, so the pod stays up and diagnosable
        # instead of crash-looping on a configuration mistake.
        assert _call(server, "/_/health", secret=None)[0] == 200
    finally:
        server.close()
        if previous is not None:
            os.environ["FN_SHARED_SECRET"] = previous


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    sys.exit(1 if failures else 0)
