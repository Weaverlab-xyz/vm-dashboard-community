"""Sealing a single secret to a remote agent, for one job, over its own poll channel.

Why this exists
---------------
``agent_signing`` bought *authentication* the hard way — asymmetric signatures rather
than a bearer token — because the environments this feature targets put a TLS-inspecting
corporate proxy between the agent and the dashboard, and a replayable credential would
land in that proxy's log on every poll. See that module's header; the whole remote-agent
feature is sold on the property that an inspecting proxy learns nothing reusable.

A just-in-time credential fetch runs in the *other* direction, and TLS alone would not
keep that promise: the response body is exactly what an inspecting proxy, a
TLS-terminating ingress, or an APM tool with body capture records in the clear. Declining
to protect confidentiality on the same channel that went to such lengths for
authenticity — and for a strictly higher-value secret, a hypervisor administrator
password — would be the inconsistency, not the encryption.

Why the recipient key is ephemeral, per fetch
---------------------------------------------
The agent generates an X25519 keypair for each fetch and puts the public half in the
request body. ``agent_signing.canonical_request`` already covers ``sha256(body)``, so
that key arrives **already authenticated by the agent's Ed25519 identity** — there is
nothing new to verify, no new column, no enrolment change, and no key-rotation protocol
to get wrong. The private half never touches disk and dies with the fetch, so unlike an
enrolment-bound encryption key there is no stored decryption key an attacker could lift
and use against previously captured traffic.

(Ed25519 keys cannot be reused for this. ``cryptography`` exposes no birational map to
X25519, so key agreement needs its own key type — which, given the above, is a feature.)

The construction
----------------
HPKE base mode, spelled out with primitives ``cryptography`` already ships, so no
dependency is added and the agent image's dependency audit in
``tests/test_agent_runner_contract.py`` stays green unchanged::

    shared = ephemeral_private.exchange(reply_key)
    ikm    = ephemeral_public || reply_key || shared
    key    = HKDF-SHA256(ikm, salt=None, info=SEAL_INFO)
    ct     = AES-256-GCM(key, random 96-bit nonce, plaintext, aad=seal_aad(...))

Both public keys go into the IKM rather than hashing the raw DH output alone. That
mirrors HPKE's ``kem_context = enc || pkRm``; deriving from ``shared`` by itself is the
classic omission and it loses the cryptographic binding between a ciphertext and the
recipient it was built for.

Pure and stdlib-only apart from ``cryptography``, like its sibling, so it is testable
with no FastAPI, no database and no network.
"""
from __future__ import annotations

import base64
import json
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# ── Wire constants ────────────────────────────────────────────────────────────

SEAL_VERSION = 1

# Compared for exact literal equality and never parsed into parameters. An ``alg`` field
# that *selects* an algorithm is the JWT ``alg:none`` mistake; this one exists only so a
# version mismatch produces a readable refusal instead of a bad-tag error.
SEAL_ALG = "X25519-HKDF-SHA256-AES256GCM"

# Domain separation only. Every *variable* value lives in the AAD instead, so there is
# exactly one canonicalization of variable data rather than two that would have to agree.
SEAL_INFO = b"vm-dashboard/agent-secret-seal/v1"

# AES-GCM's standard 96-bit nonce, random and transmitted.
#
# A fixed zero nonce would be safe *today*, because a fresh ephemeral keypair per seal
# means every derived key is used exactly once. That is the problem: it makes a
# single-use invariant load-bearing and invisible, so the first caller who ever seals
# twice under one derived key gets catastrophic nonce reuse with no signal. Random 96-bit
# nonces degrade gracefully instead, and sixteen base64 characters is a fair price.
NONCE_BYTES = 12

_KEY_BYTES = 32          # X25519 raw public and private keys are both 32 bytes
_DERIVED_KEY_BYTES = 32  # AES-256


class SealError(Exception):
    """Raised when a sealed envelope cannot be opened, carrying *why*.

    A deliberate deviation from ``agent_signing.verify_bytes``, which answers every
    failure identically so a prober learns nothing. Here both ends are already
    authenticated and the distinction is not attacker-useful, while the operator very
    much needs to know whether they are looking at a version mismatch or a wrong key —
    a failed job renders this text and nothing else.
    """


# ── Canonicalization ──────────────────────────────────────────────────────────

def serialize(payload: dict) -> bytes:
    """The exact bytes of the AAD, and of the sealed plaintext.

    Byte-identical to ``agent_signing.serialize``, and re-stated rather than imported for
    the same reason that one re-states it from ``notify_transports``: the vendored
    agent-side copy of this module is loaded by file path in tests, where a relative
    import cannot resolve. A test pins all of them against each other.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def seal_aad(*, agent_id: str, audience: str, epk: str, job_id: str, ref: str) -> bytes:
    """The additionally-authenticated data binding a ciphertext to one delivery.

    Each field closes a specific substitution:

    * ``ref`` — the highest-value one, and the least obvious. Without it a ciphertext
      released for ``dc1-vcenter`` can be relabelled as the credential for a connection
      that points at a host the attacker controls, and the agent then presents a vCenter
      administrator password to them. That turns credential *confusion* into credential
      *exfiltration*, so it is bound into the tag rather than merely checked.
    * ``job_id`` — a credential released for an ``inventory_sync`` cannot be replayed
      into a later ``power_off`` the operator never authorised it for.
    * ``agent_id`` — a ciphertext captured from one agent's response cannot be rebound
      to another agent.
    * ``audience`` — the same confused-deputy case ``canonical_request`` guards: a seal
      produced by a staging dashboard must not open against production.
    * ``epk`` — the transported ephemeral key is covered by the tag, so a ciphertext
      cannot be re-paired with a different one.
    * ``v`` — a v1 ciphertext can never be reinterpreted under v2 rules.

    **Neither side transmits this.** Both rebuild it from state they already trust: the
    dashboard from the job row, the agent from its own identity plus the job envelope it
    has already signature-checked. Sending the AAD alongside the ciphertext would let
    the sender declare the context and the receiver merely confirm the declaration,
    which is a binding that binds nothing — and is the usual way this design is got
    wrong.

    Canonical JSON, so no field value can inject the delimiter and shift the meaning of
    its neighbour.
    """
    return serialize({
        "agent_id": str(agent_id),
        "aud": str(audience),
        "epk": str(epk),
        "job_id": str(job_id),
        "ref": str(ref),
        "v": SEAL_VERSION,
    })


# ── Keys ──────────────────────────────────────────────────────────────────────

def generate_reply_keypair() -> tuple[str, str]:
    """A fresh X25519 keypair as ``(private_b64, public_b64)`` of the raw 32-byte halves.

    Raw encoding, base64 — the same shape ``agent_signing.generate_keypair`` produces, so
    both key types read and store identically.
    """
    private = X25519PrivateKey.generate()
    raw_private = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption())
    raw_public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    return (base64.b64encode(raw_private).decode("ascii"),
            base64.b64encode(raw_public).decode("ascii"))


def _raw_key(value: str, label: str) -> bytes:
    """Decode a base64 raw 32-byte key, or raise ``SealError`` naming the field."""
    try:
        raw = base64.b64decode(str(value or "").encode("ascii"), validate=True)
    except Exception as exc:  # noqa: BLE001 — one answer for every malformed encoding
        raise SealError(f"{label} is not valid base64") from exc
    if len(raw) != _KEY_BYTES:
        raise SealError(
            f"{label} is {len(raw)} bytes, not {_KEY_BYTES} — not an X25519 key")
    return raw


def check_reply_key(reply_key_b64: str) -> None:
    """Raise ``SealError`` unless this is a usable X25519 public key.

    Exists so a caller can refuse a malformed request *before* doing the expensive,
    side-effectful work of obtaining the credential. That ordering matters more than it
    looks: for a Password Safe managed account, acquiring means opening a real request that
    is then checked in and — by default — rotated on release, so answering a request that
    could never be sealed anyway would let a caller burn checkouts and rotations at will.
    """
    raw = _raw_key(reply_key_b64, "reply key")
    try:
        X25519PublicKey.from_public_bytes(raw)
    except Exception as exc:  # noqa: BLE001
        raise SealError("reply key is not a usable X25519 public key") from exc


def _b64_field(envelope: dict, name: str) -> bytes:
    try:
        raw = base64.b64decode(str(envelope.get(name) or "").encode("ascii"),
                               validate=True)
    except Exception as exc:  # noqa: BLE001
        raise SealError(f"sealed envelope field {name!r} is not valid base64") from exc
    if not raw:
        raise SealError(f"sealed envelope field {name!r} is missing or empty")
    return raw


def _derive(shared: bytes, epk_raw: bytes, recipient_raw: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=_DERIVED_KEY_BYTES,
                salt=None, info=SEAL_INFO).derive(epk_raw + recipient_raw + shared)


# ── Seal / open ───────────────────────────────────────────────────────────────

def seal(reply_key_b64: str, secret: str, *, agent_id: str, audience: str,
         job_id: str, ref: str) -> dict:
    """Seal one secret to ``reply_key_b64`` for exactly this agent, job and connection.

    The plaintext is a JSON *object* rather than a bare string so a second field can be
    added later without a version bump forcing every deployed agent to upgrade in
    lockstep.
    """
    recipient_raw = _raw_key(reply_key_b64, "reply key")
    try:
        recipient = X25519PublicKey.from_public_bytes(recipient_raw)
    except Exception as exc:  # noqa: BLE001
        raise SealError("reply key is not a usable X25519 public key") from exc

    ephemeral = X25519PrivateKey.generate()
    epk_raw = ephemeral.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    try:
        shared = ephemeral.exchange(recipient)
    except Exception as exc:  # noqa: BLE001
        # `cryptography` refuses an all-zero shared secret, which is what a small-order
        # peer key produces. Treated as an ordinary refusal: distinguishing it would
        # confirm to whoever supplied that key that they had hit the check.
        raise SealError("X25519 key agreement failed") from exc

    epk_b64 = base64.b64encode(epk_raw).decode("ascii")
    aad = seal_aad(agent_id=agent_id, audience=audience, epk=epk_b64,
                   job_id=job_id, ref=ref)
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(_derive(shared, epk_raw, recipient_raw)).encrypt(
        nonce, serialize({"secret": str(secret)}), aad)
    return {
        "v": SEAL_VERSION,
        "alg": SEAL_ALG,
        "epk": epk_b64,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ct": base64.b64encode(ciphertext).decode("ascii"),
    }


def open_sealed(private_b64: str, envelope: dict, *, agent_id: str, audience: str,
                job_id: str, ref: str) -> str:
    """Open an envelope sealed to ``private_b64`` and return the secret.

    Every context value is supplied by the caller from its own trusted state and never
    read out of ``envelope`` — see :func:`seal_aad`. A tag failure therefore means the
    ciphertext was built for a different agent, job or connection, and it is refused
    rather than decrypted.

    Returns the secret exactly as sealed, including an empty string. Whether an empty
    credential is acceptable is the caller's policy, not this module's.
    """
    if not isinstance(envelope, dict):
        raise SealError("sealed envelope is not an object")
    if envelope.get("v") != SEAL_VERSION:
        raise SealError(
            f"sealed envelope is version {envelope.get('v')!r}, "
            f"but this build only understands version {SEAL_VERSION}")
    if envelope.get("alg") != SEAL_ALG:
        raise SealError(
            f"sealed envelope algorithm {envelope.get('alg')!r} is not {SEAL_ALG}")

    private_raw = _raw_key(private_b64, "reply private key")
    epk_raw = _b64_field(envelope, "epk")
    if len(epk_raw) != _KEY_BYTES:
        raise SealError(
            f"sealed envelope 'epk' is {len(epk_raw)} bytes, not {_KEY_BYTES}")
    nonce = _b64_field(envelope, "nonce")
    if len(nonce) != NONCE_BYTES:
        raise SealError(
            f"sealed envelope 'nonce' is {len(nonce)} bytes, not {NONCE_BYTES}")
    ciphertext = _b64_field(envelope, "ct")

    try:
        private = X25519PrivateKey.from_private_bytes(private_raw)
        shared = private.exchange(X25519PublicKey.from_public_bytes(epk_raw))
    except Exception as exc:  # noqa: BLE001
        raise SealError("X25519 key agreement failed") from exc

    recipient_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    aad = seal_aad(agent_id=agent_id, audience=audience,
                   epk=base64.b64encode(epk_raw).decode("ascii"),
                   job_id=job_id, ref=ref)
    try:
        plaintext = AESGCM(_derive(shared, epk_raw, recipient_raw)).decrypt(
            nonce, ciphertext, aad)
    except Exception as exc:  # noqa: BLE001
        raise SealError(
            "the sealed credential did not authenticate — it was sealed to a different "
            "key, or for a different agent, job or connection") from exc

    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SealError("the sealed payload is not valid JSON") from exc
    if not isinstance(payload, dict) or "secret" not in payload:
        raise SealError("the sealed payload carries no 'secret' field")
    return str(payload["secret"])
