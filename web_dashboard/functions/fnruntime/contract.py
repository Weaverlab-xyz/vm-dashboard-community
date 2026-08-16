"""Normalized request / response / context types.

This is the entire surface a workload sees: nothing about Lambda event shapes,
``azure.functions``, or Flask leaks through. The adapters (``fnruntime.adapters``)
own the translation in both directions.

Stdlib only.
"""
import base64
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class Request:
    """One inbound HTTP (or synthesized) request, normalized.

    ``headers`` keys are **always lower-cased** by the adapter, so a workload can
    index them directly without worrying about which cloud folded the case.
    ``body`` is **already base64-decoded** — the ``isBase64Encoded`` flag AWS sets
    is the adapter's problem, not the workload's.
    """
    method: str = "GET"
    path: str = "/"
    headers: Mapping[str, str] = field(default_factory=dict)
    query: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    # Which shape this came from — one of: aws_function_url, aws_apigw_v2,
    # aws_apigw_v1, aws_direct, azure, gcp, unknown. Auth uses it to allow the
    # secret in the body for direct (scheduled) invokes, which have no headers.
    source: str = "unknown"

    def json(self) -> dict:
        """The body parsed as a JSON object, or ``{}``.

        Never raises: a malformed, non-UTF-8, empty, or non-object body all yield
        ``{}`` so a workload can do ``req.json().get(...)`` unguarded. A workload
        that needs to distinguish "no body" from "bad body" should check ``body``.
        """
        if not self.body:
            return {}
        try:
            val = json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return val if isinstance(val, dict) else {}

    def header(self, name: str, default: str = "") -> str:
        """Case-insensitive header lookup."""
        return self.headers.get((name or "").lower(), default)


@dataclass
class Response:
    """What a workload returns. ``body`` may be a dict/list (serialized as JSON),
    a str (sent as-is), bytes (base64-encoded where the platform needs it), or
    None (empty body)."""
    status: int = 200
    body: Any = None
    headers: dict = field(default_factory=dict)

    def rendered(self) -> tuple:
        """``(body_text, headers, is_base64)`` with content-type filled in.

        Every adapter funnels through this so the three clouds cannot drift in how
        they serialize a workload's return value.
        """
        headers = {str(k).lower(): str(v) for k, v in (self.headers or {}).items()}
        body = self.body
        if body is None:
            return "", headers, False
        if isinstance(body, (bytes, bytearray)):
            headers.setdefault("content-type", "application/octet-stream")
            return base64.b64encode(bytes(body)).decode("ascii"), headers, True
        if isinstance(body, str):
            headers.setdefault("content-type", "text/plain; charset=utf-8")
            return body, headers, False
        headers.setdefault("content-type", "application/json")
        # default=str so a stray datetime/UUID in a workload's payload degrades to
        # a string instead of 500-ing the whole invocation at serialization time.
        return json.dumps(body, default=str), headers, False


@dataclass(frozen=True)
class Context:
    """Per-invocation metadata, assembled from the environment the module deploys
    with (``FN_*``) plus whatever request id the platform supplies."""
    request_id: str = ""
    workload: str = ""
    cloud: str = ""
    region: str = ""
    function_name: str = ""
    started_ms: int = 0

    @classmethod
    def from_env(cls, req: Optional[Request] = None, *,
                 request_id: str = "", workload: str = "") -> "Context":
        """Build a Context from the ``FN_*`` env vars the Terraform modules set.

        ``request_id`` falls back to the platform's own correlation header and then
        to a fresh uuid4, so a log line always has one to key on.
        """
        rid = request_id or _request_id_from(req)
        return cls(
            request_id=rid,
            workload=workload or os.environ.get("FN_WORKLOAD", ""),
            cloud=os.environ.get("FN_CLOUD", ""),
            region=os.environ.get("FN_REGION", ""),
            function_name=os.environ.get("FN_NAME", ""),
            started_ms=int(time.time() * 1000),
        )

    def elapsed_ms(self) -> int:
        return max(0, int(time.time() * 1000) - self.started_ms)


def _request_id_from(req: Optional[Request]) -> str:
    """Platform correlation id, if the request carries one."""
    if req is not None:
        for name in ("x-amzn-trace-id", "x-azure-functions-invocationid",
                     "x-cloud-trace-context", "traceparent", "x-request-id"):
            val = req.headers.get(name)
            if val:
                return str(val)[:200]
    return str(uuid.uuid4())
