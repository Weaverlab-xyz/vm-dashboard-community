"""Ed25519 request/envelope signing for the remote on-prem agent.

Why signatures and not a bearer token
-------------------------------------
A remote agent polls this dashboard in a loop from inside a customer network, and in
the environments this feature exists for that traffic crosses a TLS-inspecting
corporate proxy. A bearer token in an ``Authorization`` header is replayable and lands
in that proxy's log in the clear on every single poll — the proxy becomes a credential
store. With a signature there is no replayable secret in the request at all: the proxy
sees a value that is valid for one method, one path, one body, and sixty seconds.

That property is also why mTLS is *not* the answer here. An inspecting proxy terminates
TLS and cannot forward a client certificate, so mandatory mTLS would break the agent at
exactly the enterprises it is aimed at. Application-layer signing is the substitute that
survives inspection.

Both directions are signed, with different keys and for different reasons:

* **agent -> dashboard** authenticates the caller.
* **dashboard -> agent** proves *provenance* of a job envelope. This is what makes a
  database-write-only compromise insufficient: an attacker who can ``INSERT`` a job row
  still cannot produce a signature the agent will accept, so the work is never run.
  Signing keys are asymmetric rather than a shared HMAC secret precisely so a leaked
  agent-side key cannot be used to forge work *for* that agent.

Pure and stdlib-only apart from ``cryptography`` (already a dependency —
``config_service`` imports Fernet from it), so the whole module is testable without
FastAPI, a database, or a network.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)

# ── Wire constants ────────────────────────────────────────────────────────────

HEADER_AGENT_ID = "X-Agent-Id"
HEADER_TIMESTAMP = "X-Agent-Timestamp"
HEADER_NONCE = "X-Agent-Nonce"
HEADER_SIGNATURE = "X-Agent-Signature"

# How far a request's timestamp may be from ours. Wide enough to absorb ordinary clock
# skew and a slow proxy hop, narrow enough that the nonce table stays small — the
# sweeper only has to retain nonces for this long.
SIGNATURE_WINDOW_SECONDS = 60

# Ed25519 raw keys are always 32 bytes; the signature is always 64.
_PUBLIC_KEY_BYTES = 32
_SIGNATURE_BYTES = 64


class SignatureError(Exception):
    """Raised by :func:`verify_request` helpers that must explain *why* they refused."""


# ── Canonicalization ──────────────────────────────────────────────────────────

def serialize(payload: dict) -> bytes:
    """The exact bytes to sign, and the exact bytes to send.

    Byte-identical to ``notify_transports.serialize`` — deliberately re-stated here
    rather than imported, because that module pulls in ``httpx`` and this one must stay
    importable with nothing but the standard library plus ``cryptography``. A test pins
    the two against each other so they cannot drift.

    Signing a separately-serialised body and then posting with ``json=`` is the classic
    signing bug: the receiver verifies bytes that were never sent. Callers sign *this*
    and post it with ``content=``.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_request(*, agent_id: str, timestamp: str, nonce: str, audience: str,
                      method: str, path: str, body: bytes) -> bytes:
    """The canonical byte string covered by a request signature.

    Every field that decides what the request *does* is inside the signature:

    * ``method`` + ``path`` so a captured signature cannot be replayed against a
      different endpoint (a lease poll re-aimed at ``/complete``, say);
    * ``sha256(body)`` rather than the body itself, so the canonical form stays a fixed
      small size no matter how large the payload;
    * ``audience`` so a signature captured by one dashboard cannot be replayed against
      another — the classic confused-deputy move when an agent is pointed at a staging
      host that an attacker controls;
    * ``timestamp`` + ``nonce`` for freshness and single-use.

    Built as canonical JSON rather than a delimiter-joined string on purpose: JSON
    escapes the field values, so no value can inject the delimiter and shift the
    meaning of a neighbouring field.
    """
    return serialize({
        "aud": audience,
        "id": agent_id,
        "method": method.upper(),
        "nonce": nonce,
        "path": path,
        "sha256": hashlib.sha256(body or b"").hexdigest(),
        "ts": str(timestamp),
    })


# ── Keys ──────────────────────────────────────────────────────────────────────

def generate_keypair() -> tuple[str, str]:
    """A fresh Ed25519 keypair as ``(private_b64, public_b64)`` of the raw 32-byte
    halves. Raw rather than PEM because both halves are written to a file or a database
    column, and 44 characters beats a multi-line block for both."""
    private = Ed25519PrivateKey.generate()
    return private_key_b64(private), public_key_b64(private)


def private_key_b64(private: Ed25519PrivateKey) -> str:
    from cryptography.hazmat.primitives import serialization
    raw = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.b64encode(raw).decode("ascii")


def public_key_b64(private: Ed25519PrivateKey) -> str:
    from cryptography.hazmat.primitives import serialization
    raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def load_private_key(private_b64: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_b64))


def load_public_key(public_b64: str) -> Ed25519PublicKey:
    """Parse a base64 raw public key, raising :class:`SignatureError` on anything that
    is not one. Callers store this value in a database column, so 'malformed' has to be
    a handled outcome rather than a 500."""
    try:
        raw = base64.b64decode(public_b64, validate=True)
    except Exception as exc:  # noqa: BLE001 — any decode failure is the same refusal
        raise SignatureError("public key is not valid base64") from exc
    if len(raw) != _PUBLIC_KEY_BYTES:
        raise SignatureError(
            f"public key must be {_PUBLIC_KEY_BYTES} raw bytes, got {len(raw)}")
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except Exception as exc:  # noqa: BLE001
        raise SignatureError("public key is not a valid Ed25519 key") from exc


# ── Signing / verification ────────────────────────────────────────────────────

def sign_bytes(private_b64: str, message: bytes) -> str:
    """Base64 Ed25519 signature over ``message``."""
    return base64.b64encode(load_private_key(private_b64).sign(message)).decode("ascii")


def verify_bytes(public_b64: str, signature_b64: str, message: bytes) -> bool:
    """``True`` iff the signature is valid. Never raises — a malformed key, a malformed
    signature and a wrong signature are all one answer, ``False``, because the caller's
    response to each is identical and distinguishing them in an error message would tell
    an attacker which part they got right."""
    try:
        public = load_public_key(public_b64)
        signature = base64.b64decode(signature_b64, validate=True)
    except Exception:  # noqa: BLE001
        return False
    if len(signature) != _SIGNATURE_BYTES:
        return False
    try:
        public.verify(signature, message)
        return True
    except InvalidSignature:
        return False
    except Exception:  # noqa: BLE001 — defensive; verify() should only raise the above
        return False


def sign_request(private_b64: str, *, agent_id: str, audience: str, method: str,
                 path: str, body: bytes, timestamp: Optional[str] = None,
                 nonce: Optional[str] = None) -> dict:
    """Sign one request and return the headers to send with it.

    Returns the timestamp and nonce it used, so the caller sends exactly what was
    signed rather than regenerating them (a regenerated timestamp is a signature that
    can never verify, and it is a genuinely annoying bug to find).
    """
    ts = timestamp or str(int(time.time()))
    nonce = nonce or new_nonce()
    message = canonical_request(agent_id=agent_id, timestamp=ts, nonce=nonce,
                                audience=audience, method=method, path=path, body=body)
    return {
        HEADER_AGENT_ID: agent_id,
        HEADER_TIMESTAMP: ts,
        HEADER_NONCE: nonce,
        HEADER_SIGNATURE: sign_bytes(private_b64, message),
    }


def verify_request(public_b64: str, *, agent_id: str, timestamp: str, nonce: str,
                   signature: str, audience: str, method: str, path: str,
                   body: bytes, now: Optional[float] = None,
                   window: int = SIGNATURE_WINDOW_SECONDS) -> bool:
    """Verify a signed request, including its freshness window.

    Does **not** check nonce reuse — that needs storage, so it lives in
    ``agent_service`` next to the database. This function is the pure half.
    """
    if not timestamp_fresh(timestamp, now=now, window=window):
        return False
    message = canonical_request(agent_id=agent_id, timestamp=timestamp, nonce=nonce,
                                audience=audience, method=method, path=path, body=body)
    return verify_bytes(public_b64, signature, message)


def timestamp_fresh(timestamp: str, *, now: Optional[float] = None,
                    window: int = SIGNATURE_WINDOW_SECONDS) -> bool:
    """Freshness check, tolerant in both directions.

    A request from the *future* is rejected as firmly as a stale one: an agent whose
    clock runs fast would otherwise mint signatures that stay valid long after the
    window should have closed.
    """
    try:
        ts = int(str(timestamp).strip())
    except (TypeError, ValueError):
        return False
    return abs((time.time() if now is None else now) - ts) <= window


def new_nonce() -> str:
    """128 bits. Collision probability across the retention window is not a real risk;
    the length is chosen so the nonce is also unguessable, which matters because a
    predictable nonce would let an attacker pre-poison the replay table and deny an
    agent its own next request."""
    return secrets.token_hex(16)


# ── Job envelopes (dashboard -> agent) ────────────────────────────────────────

def sign_envelope(private_b64: str, envelope: dict) -> str:
    """Sign a job envelope. The signature covers the canonical serialization of the
    envelope itself, so the agent verifies the same structure it is about to act on."""
    return sign_bytes(private_b64, serialize(envelope))


def verify_envelope(public_b64: str, envelope: dict, signature: str) -> bool:
    return verify_bytes(public_b64, signature, serialize(envelope))
