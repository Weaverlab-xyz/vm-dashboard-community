"""Shared OPA (Open Policy Agent) invoker.

Action-level admission control ([`admission_service`](admission_service.py),
query ``data.admission``) shells the bundled ``opa`` binary over a Rego policy
directory. This module is the single subprocess seam.

The binary ships in the image (Dockerfile ``ADD .../opa``); ``opa_available()``
lets callers degrade or skip when it's absent (e.g. a non-rebuilt dev container).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

OPA_BIN = os.environ.get("OPA_BINARY", "opa")


class OpaError(Exception):
    pass


def opa_available() -> bool:
    return shutil.which(OPA_BIN) is not None or os.path.isfile(OPA_BIN)


def list_packages(policy_dir: str) -> list[str]:
    """The rego filenames (without extension) in ``policy_dir`` — one per
    rule by convention. ``[]`` if the directory is absent."""
    p = Path(policy_dir)
    if not p.is_dir():
        return []
    return sorted(f.stem for f in p.glob("*.rego"))


def eval_query(input_doc: dict, *, data_dir: str, query: str, timeout: int = 30) -> dict:
    """Run ``opa eval`` for ``query`` over the Rego in ``data_dir`` with
    ``input_doc`` on stdin; return the query's value object (``{}`` if no
    result). Raises :class:`OpaError` if opa is missing, the dir is absent, or
    eval fails — the admission caller treats that as a denial (fail closed)."""
    if not opa_available():
        raise OpaError(
            f"OPA binary {OPA_BIN!r} not found — the image bundles it at "
            "/usr/local/bin/opa; in a non-rebuilt dev container, install it first."
        )
    if not Path(data_dir).is_dir():
        raise OpaError(f"policy dir not found: {data_dir}")

    try:
        proc = subprocess.run(
            [OPA_BIN, "eval", "--format", "json", "--data", data_dir,
             "--stdin-input", query],
            input=json.dumps(input_doc), capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise OpaError(f"opa eval timed out: {exc}") from exc
    if proc.returncode != 0:
        raise OpaError(f"opa eval failed: {proc.stderr.strip() or proc.stdout.strip()}")

    try:
        out = json.loads(proc.stdout)
        results = out.get("result") or []
        return results[0]["expressions"][0]["value"] if results else {}
    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        raise OpaError(f"could not parse opa output: {exc}") from exc
