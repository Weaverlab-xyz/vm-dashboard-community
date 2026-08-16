"""Cloud Functions: the portable request/response contract.

The adapters are the only code in the runtime that knows what cloud it is on, so
every normalization rule that a workload depends on is pinned here. These run with
NOTHING installed — fnruntime is stdlib-only and the Azure/GCP requests are
duck-typed — which is the point: the contract is provable without a cloud account.
"""
import base64
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from web_dashboard import functions  # noqa: F401  (puts fnruntime on sys.path)
from fnruntime import adapters
from fnruntime.contract import Request, Response


# ── AWS: all four event shapes ────────────────────────────────────────────────

def test_aws_function_url_shape():
    event = {
        "version": "2.0",
        "rawPath": "/invoke",
        "headers": {"authorization": "Bearer s3cret", "content-type": "application/json"},
        "queryStringParameters": {"fail": "500"},
        "body": '{"action":"grant"}',
        "isBase64Encoded": False,
        "requestContext": {
            "http": {"method": "POST"},
            "domainName": "abc123.lambda-url.us-east-1.on.aws",
            "stage": "$default",
        },
    }
    req = adapters.from_aws(event)
    assert req.source == "aws_function_url", req.source
    assert req.method == "POST"
    assert req.path == "/invoke"
    assert req.json() == {"action": "grant"}
    assert req.query == {"fail": "500"}
    assert req.header("Authorization") == "Bearer s3cret"


def test_aws_apigw_v2_is_the_same_branch_but_strips_the_stage():
    event = {
        "version": "2.0",
        "rawPath": "/prod/invoke",
        "headers": {},
        "requestContext": {
            "http": {"method": "GET"},
            "domainName": "abc.execute-api.us-east-1.amazonaws.com",
            "stage": "prod",
        },
    }
    req = adapters.from_aws(event)
    assert req.source == "aws_apigw_v2", req.source
    assert req.path == "/invoke", req.path


def test_aws_apigw_v1_lowercases_headers_and_survives_null_query():
    event = {
        "httpMethod": "POST",
        "path": "/invoke",
        # v1 does NOT fold header case, and sends null (not {}) for no query.
        "headers": {"Authorization": "Bearer abc", "X-Custom": "v"},
        "queryStringParameters": None,
        "body": "hello",
        "isBase64Encoded": False,
    }
    req = adapters.from_aws(event)
    assert req.source == "aws_apigw_v1"
    assert req.header("authorization") == "Bearer abc"
    assert req.header("x-custom") == "v"
    assert req.query == {}
    assert req.body == b"hello"


def test_aws_v1_base64_body_is_decoded():
    event = {
        "httpMethod": "POST",
        "path": "/",
        "headers": {},
        "body": base64.b64encode(b'{"a":1}').decode("ascii"),
        "isBase64Encoded": True,
    }
    req = adapters.from_aws(event)
    assert req.json() == {"a": 1}, req.body


def test_aws_direct_invoke_becomes_a_post_with_the_event_as_body():
    """EventBridge / `aws lambda invoke` send a bare document with no
    requestContext. Without this branch every scheduled invocation crashes."""
    event = {"probe": [{"host": "db.internal", "port": 5432}], "secret": "s"}
    req = adapters.from_aws(event)
    assert req.source == "aws_direct"
    assert req.method == "POST"
    assert req.path == "/"
    assert req.json()["probe"][0]["host"] == "db.internal"


def test_aws_garbage_event_does_not_raise():
    for event in (None, [], "text", 7):
        req = adapters.from_aws(event)
        assert req.source == "aws_direct"


def test_to_aws_always_emits_the_full_envelope():
    """A bare dict returned from a Function URL is treated by Lambda as a 200
    body — which would silently swallow a 401."""
    out = adapters.to_aws(Response(401, {"error": "unauthorized"}))
    assert set(out) == {"statusCode", "headers", "body", "isBase64Encoded"}
    assert out["statusCode"] == 401
    assert json.loads(out["body"]) == {"error": "unauthorized"}
    assert out["isBase64Encoded"] is False
    assert out["headers"]["content-type"] == "application/json"


def test_to_aws_base64_encodes_bytes():
    out = adapters.to_aws(Response(200, b"\x00\x01binary"))
    assert out["isBase64Encoded"] is True
    assert base64.b64decode(out["body"]) == b"\x00\x01binary"


def test_to_aws_empty_body():
    out = adapters.to_aws(Response(204, None))
    assert out["statusCode"] == 204 and out["body"] == ""


# ── Azure / GCP: duck-typed, no SDK installed ─────────────────────────────────

class _FakeAzureRequest:
    def __init__(self, method="POST", url="https://app.azurewebsites.net/api/invoke",
                 headers=None, params=None, body=b""):
        self.method = method
        self.url = url
        self.headers = headers or {}
        self.params = params or {}
        self._body = body

    def get_body(self):
        return self._body


def test_from_azure_strips_the_api_route_prefix_and_lowercases_headers():
    req = adapters.from_azure(_FakeAzureRequest(
        headers={"Authorization": "Bearer x"},
        params={"fail": "slow"},
        body=b'{"action":"revoke"}'))
    assert req.source == "azure"
    assert req.path == "/invoke", req.path
    assert req.header("authorization") == "Bearer x"
    assert req.query == {"fail": "slow"}
    assert req.json() == {"action": "revoke"}


def test_from_azure_survives_a_body_that_raises():
    class Broken(_FakeAzureRequest):
        def get_body(self):
            raise RuntimeError("no body")
    req = adapters.from_azure(Broken())
    assert req.body == b""


class _FakeFlaskRequest:
    def __init__(self, method="POST", path="/", headers=None, args=None, data=b""):
        self.method = method
        self.path = path
        self.headers = headers or {}
        self.args = args or {}
        self._data = data

    def get_data(self):
        return self._data


def test_from_gcp_duck_types_flask():
    req = adapters.from_gcp(_FakeFlaskRequest(
        headers={"Authorization": "Bearer g"}, data=b'{"ok":1}'))
    assert req.source == "gcp"
    assert req.header("authorization") == "Bearer g"
    assert req.json() == {"ok": 1}


def test_to_gcp_returns_the_flask_triple():
    body, status, headers = adapters.to_gcp(Response(200, {"a": 1}))
    assert status == 200
    assert json.loads(body) == {"a": 1}
    assert headers["content-type"] == "application/json"


# ── Request/Response semantics workloads rely on ──────────────────────────────

def test_request_json_never_raises():
    for body in (b"", b"not json", b"[1,2,3]", b"\xff\xfe", b"null"):
        assert Request(body=body).json() == {}


def test_response_renders_str_and_dict_differently():
    text, headers, _ = Response(200, "plain").rendered()
    assert text == "plain" and headers["content-type"].startswith("text/plain")
    text, headers, _ = Response(200, {"a": 1}).rendered()
    assert json.loads(text) == {"a": 1} and headers["content-type"] == "application/json"


def test_response_explicit_content_type_wins():
    _text, headers, _ = Response(200, "<html>", {"Content-Type": "text/html"}).rendered()
    assert headers["content-type"] == "text/html"


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
