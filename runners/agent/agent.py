#!/usr/bin/env python3
"""Remote on-prem agent for the infrastructure dashboard.

Runs inside a private network, dials OUT to the dashboard over HTTPS, and asks for
work. Nothing listens; no inbound firewall rule is needed anywhere.

What this process will and will not do
--------------------------------------
It polls, it validates, it probes, it reports. It does **not** execute anything the
dashboard sends it. A discovery job names networks and ports; there is no field in the
protocol that can carry a command, a script, a URL or a filename, and the handler table
below is a closed dict rather than a dispatch on a string from the wire. That is
deliberate and it is the whole security argument: an agent is a dashboard-controlled
endpoint inside someone else's network, so a compromise of the dashboard must not
become code execution on the LAN.

Two independent gates enforce it:

* **The envelope signature.** Every job is signed by the dashboard with a key this
  agent pinned at enrolment. An attacker who can write to the dashboard's database
  still cannot produce a signature, so the job is refused before it is even parsed.
* **policy.yaml.** Mounted read-only, owned by whoever runs the agent, and reachable by
  no dashboard API. It names the networks and ports that may be touched. Missing,
  unreadable or unparseable means *refuse everything* — never "allow everything".

Probes never authenticate. Not once. Discovery is a TCP connect and a pre-auth
protocol banner, because authenticated probing of unknown hosts is how a discovery
feature becomes a credential-spray tool and how service accounts get locked out.

Dependencies are deliberately few: requests, PyYAML, cryptography. No Docker socket,
no kubectl, no ansible — this image is structurally incapable of running a playbook.
"""
from __future__ import annotations

import base64
import errno
import getpass
import hashlib
import ipaddress
import json
import logging
import os
import posixpath
import random
import re
import http.client as http_client
import socket
import ssl
import stat
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
import urllib.error as _urlerror
import urllib.request as _urlrequest
from urllib.parse import quote as _quote, urlparse

import requests
import yaml
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# 2.x scans for HYPERVISORS. 1.x scanned for Kubernetes API servers and database
# listeners, and handed a 2.x scan request it would probe nothing, complete green and
# report zero findings — indistinguishable from a clean network. The dashboard refuses
# to queue for a 1.x agent (api/agent.queue_discovery), which is why the major matters
# and why the lease body reports it on every poll rather than only at enrolment.
# 2.1 added `dashboard_secret`: the credential is fetched per job from the dashboard and
# opened with `open_sealed`, so nothing sensitive sits in connections.yaml. A 2.0 agent
# does not know the key and would fall through to a local password the operator has just
# deleted — sending an empty one — so the dashboard refuses to QUEUE for an agent below
# this (agent_service.supports_dashboard_secret) rather than let it fail against the
# hypervisor, where an empty password reads as a wrong one and retries risk a lockout.
# 2.2 added `password_sealed` / `client_secret_sealed`: a credential the customer keeps on
# THIS host, encrypted against a key in the state volume rather than sitting in their YAML
# as text. Deliberately NOT gated from the dashboard, and it is worth knowing why the
# asymmetry is correct rather than an omission — `dashboard_secret` could be gated because
# the dashboard holds that credential and therefore knows the connection uses it, whereas a
# sealed value exists only in the customer's own file and the dashboard cannot see it at
# all. What replaces the gate: `seal` ships in the same image that reads the key, so a
# sealed value cannot be produced by a build that would ignore it; and `_secret_for` now
# REFUSES a connection with no credential instead of sending an empty password, which is
# the failure an ungated old agent would otherwise produce.
# 2.3 added `agent_ansible`: Config Management executed on THIS host, in a one-shot sibling
# container, against a VM or database the dashboard has no route to. Gated from the
# dashboard (agent_service.supports_ansible) because an older agent has no such entry in
# HANDLERS and would refuse the job by name — a refusal that lands in Live Output, where it
# reads as a policy.yaml problem and sends the operator to edit the wrong file.
# Two things about this release are load-bearing rather than incidental:
#   * The signed job envelope carries FOUR SCALARS and no playbook. Executable content is
#     never merely signed; it arrives sealed, from /jobs/{id}/ansible-bundle, bound by AAD
#     to this agent, this job and this endpoint.
#   * The agent renders the Ansible inventory ITSELF from those four scalars, and refuses
#     any `ansible_*` extra var. A dashboard-supplied inventory could set
#     `ansible_connection: local`, which would run the operator's playbook inside the runner
#     container on this network instead of against the target it names.
AGENT_VERSION = "2.4.0"

log = logging.getLogger("agent")

# ── Configuration ─────────────────────────────────────────────────────────────

DASHBOARD_URL = (os.environ.get("DASHBOARD_URL") or "").rstrip("/")
ENROLLMENT_CODE = os.environ.get("AGENT_ENROLLMENT_CODE", "").strip()
# Preferred over the variable above. An env var is baked into the container's config and
# stays readable via `docker inspect` for the container's whole life, long after the code
# has been spent; a mounted file leaves nothing durable in Docker's metadata and can be
# deleted the moment enrolment succeeds.
ENROLLMENT_CODE_FILE = os.environ.get("AGENT_ENROLLMENT_CODE_FILE", "").strip()
STATE_DIR = os.environ.get("AGENT_STATE_DIR", "/var/lib/dashboard-agent")
POLICY_FILE = os.environ.get("AGENT_POLICY_FILE", "/etc/dashboard-agent/policy.yaml")
CA_BUNDLE = os.environ.get("AGENT_CA_BUNDLE", "").strip()
INSECURE_TLS = os.environ.get("AGENT_INSECURE_TLS", "").strip() in ("1", "true", "yes")
# "audit" logs in full what it WOULD do and executes nothing. Run this for a couple of
# weeks and diff intent against policy before letting it act — in practice this is what
# gets an agent approved by a security team.
MODE = (os.environ.get("AGENT_MODE") or "normal").strip().lower()
# Ports a hypervisor MANAGEMENT endpoint listens on. Mirrors agent_job_meta
# _DEFAULTS["ports"] server-side; both sides clamp, for the two different reasons
# that module documents.
_PORT_DEFAULTS = {"vmware": [443], "proxmox": [8006], "nutanix": [9440],
                  "xcpng": [443], "winrm": [5985, 5986]}

_IDENTITY_FILE = os.path.join(STATE_DIR, "identity.json")
_HTTP_TIMEOUT = 30
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_FINDINGS = 200


class AgentFatal(Exception):
    """Misconfiguration the operator has to fix; the process exits rather than
    spinning in a crash loop that hides the message."""


class PolicyRefusal(Exception):
    """The agent declined the work. Reported back as a job failure, and logged
    locally in full — the local log is the audit record the dashboard cannot edit."""


class Throttled(Exception):
    """The dashboard answered 429 and said when to come back.

    Distinct from ``requests.RequestException`` so the retry uses the interval the server
    asked for rather than this process's own doubling — and distinct from ``AgentFatal``
    because being asked to slow down is not a reason to exit.
    """

    def __init__(self, retry_after: float):
        self.retry_after = max(1.0, float(retry_after))
        super().__init__(f"throttled for {self.retry_after:.0f}s")


def _read_secret_file(path: str) -> str:
    """Read one small operator-written secret — a code, a password — as text.

    ``utf-8-sig`` rather than plain ``utf-8`` for one reason: **a byte-order mark is not
    whitespace.** A UTF-8 BOM survives ``.strip()``, so the reads this replaced handed back
    ``"\\ufeffagte_…"`` and the only symptom was the dashboard rejecting a code that looks
    correct in every editor. ``utf-8-sig`` decodes ASCII and BOM-less UTF-8 byte for byte
    identically, so it costs nothing in the ordinary case.

    Raises ``ValueError`` — of which ``UnicodeDecodeError`` is a subclass — for a file
    that is not UTF-8 at all. In practice that means UTF-16, which is what PowerShell
    5.1's ``>`` and ``Out-File`` write by default and what Notepad's "Unicode" option
    saves. The two forms fail differently and both are handled here: a BOM'd UTF-16 file
    fails to decode on the ``\\xff\\xfe`` itself, while a BOM-less UTF-16LE one *decodes
    cleanly* — its high bytes are NUL, which is legal UTF-8 — and would otherwise be
    returned as an interleaved-NUL string that is rejected as invalid with nothing
    pointing at why. Callers turn either into :func:`_secret_file_undecodable`.
    """
    with open(path, encoding="utf-8-sig") as fh:
        text = fh.read()
    if "\x00" in text:
        raise ValueError("embedded NUL: decoded as UTF-8 but is not UTF-8 text")
    return text.strip()


def _secret_file_undecodable(what: str, path: str) -> str:
    """Explain a secret file whose *encoding* is wrong, in terms of what wrote it.

    Worth its own message because both halves of this failure mislead, in opposite
    directions. ``UnicodeDecodeError`` is not an ``OSError``, so before this existed a
    UTF-16 file escaped the handler below and killed the container with a raw traceback —
    throwing away the one explanation the operator was ever going to get, since the agent
    reads this file before its first network call and nothing about it reaches the
    dashboard. The BOM case is the quiet opposite: it reads fine, enrolment is refused as
    a bad code, and the file looks perfect in every editor that hides the mark.

    Almost always Windows, and almost always PowerShell 5.1. The dashboard's emitted
    PowerShell command writes the file with ``Set-Content -Encoding ascii -NoNewline``
    precisely to avoid this, so a file in this state was written by hand — which is why
    the remedy names the encoding flags rather than telling the operator to re-copy the
    command.
    """
    return (
        f"{what} is set to {path}, and the file exists and is readable, but its contents "
        f"are not text this agent can decode. This is an encoding problem, not a "
        f"permissions one: it looks like UTF-16, which is what PowerShell 5.1's `>` and "
        f"`Out-File` write by default and what Notepad's \"Unicode\" option saves. Write "
        f"it as ASCII or UTF-8 with no byte-order mark — in PowerShell that is "
        f"`Set-Content -Encoding ascii -NoNewline -Path <file> -Value '<value>'`.")


def enrollment_code() -> str:
    """The one-time enrolment code, from a mounted file when one is configured.

    The file wins when both are set: an operator who went to the trouble of mounting one
    meant it, and silently preferring the environment variable would quietly undo the
    reason they did. Only read when there is no stored identity, so deleting the file
    after a successful enrolment — which is the point of using one — does not stop the
    agent from restarting.
    """
    if ENROLLMENT_CODE_FILE:
        try:
            return _read_secret_file(ENROLLMENT_CODE_FILE)
        except ValueError:
            raise AgentFatal(
                _secret_file_undecodable(
                    "AGENT_ENROLLMENT_CODE_FILE", ENROLLMENT_CODE_FILE)
                + " Or pass the code as AGENT_ENROLLMENT_CODE instead. Nothing has "
                  "reached the dashboard yet, so the code itself is still unspent.")
        except OSError as exc:
            raise AgentFatal(
                f"AGENT_ENROLLMENT_CODE_FILE is set to {ENROLLMENT_CODE_FILE} but it "
                f"cannot be read ({exc}). The container runs as uid 10001, so a file "
                f"mode 0600 owned by another user is unreadable inside it — mount it "
                f"world-readable, or pass AGENT_ENROLLMENT_CODE instead.")
    return ENROLLMENT_CODE


def _retry_after_seconds(resp, default: float) -> float:
    """The server's ``Retry-After`` in seconds, or ``default``.

    Only the delta-seconds form is handled, because that is the only form this dashboard
    sends. An HTTP-date would fall through to the default rather than being parsed
    wrongly, which is the safe direction to be wrong in.
    """
    try:
        return max(1.0, float(int((resp.headers.get("Retry-After") or "").strip())))
    except (TypeError, ValueError):
        return default


# ── Canonical signing (mirrors web_dashboard/services/agent_signing.py) ───────
# Vendored rather than imported: this container ships and versions independently of
# the dashboard, exactly like runners/promote/entrypoint.py. tests/test_agent_runner_
# contract.py asserts these produce bytes identical to the dashboard's implementation,
# so the copy cannot drift into a signature that silently never verifies.

HEADER_AGENT_ID = "X-Agent-Id"
HEADER_TIMESTAMP = "X-Agent-Timestamp"
HEADER_NONCE = "X-Agent-Nonce"
HEADER_SIGNATURE = "X-Agent-Signature"


def serialize(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_request(*, agent_id: str, timestamp: str, nonce: str, audience: str,
                      method: str, path: str, body: bytes) -> bytes:
    return serialize({
        "aud": audience,
        "id": agent_id,
        "method": method.upper(),
        "nonce": nonce,
        "path": path,
        "sha256": hashlib.sha256(body or b"").hexdigest(),
        "ts": str(timestamp),
    })


# ── Sealed credentials (mirrors web_dashboard/services/agent_sealing.py) ──────
# Vendored for the same reason the signing block above is, and pinned by the same
# contract test. Read that module's header for why a just-in-time credential is
# encrypted to a per-fetch key rather than trusted to TLS: an inspecting proxy sees
# response bodies, and this feature is sold on the property that it learns nothing.
#
# The ephemeral private half below never touches disk and dies with the fetch, so a
# stolen identity.json yields no decryption key for traffic captured earlier.

SEAL_VERSION = 1
SEAL_ALG = "X25519-HKDF-SHA256-AES256GCM"
SEAL_INFO = b"vm-dashboard/agent-secret-seal/v1"
SEAL_NONCE_BYTES = 12
_SEAL_KEY_BYTES = 32


class SealError(Exception):
    """A sealed credential that could not be opened, and why."""


def seal_aad(*, agent_id: str, audience: str, epk: str, job_id: str, ref: str) -> bytes:
    """Rebuilt locally from trusted state, never read off the wire — the binding is
    vacuous otherwise. `ref` is the load-bearing field: without it a credential can be
    relabelled as one for a connection pointing at a host the attacker controls."""
    return serialize({
        "agent_id": str(agent_id),
        "aud": str(audience),
        "epk": str(epk),
        "job_id": str(job_id),
        "ref": str(ref),
        "v": SEAL_VERSION,
    })


def generate_reply_keypair() -> tuple:
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


def _seal_b64(envelope: dict, name: str, expect: int = 0) -> bytes:
    try:
        raw = base64.b64decode(str(envelope.get(name) or "").encode("ascii"),
                               validate=True)
    except Exception as exc:  # noqa: BLE001
        raise SealError(f"sealed envelope field {name!r} is not valid base64") from exc
    if not raw:
        raise SealError(f"sealed envelope field {name!r} is missing or empty")
    if expect and len(raw) != expect:
        raise SealError(
            f"sealed envelope {name!r} is {len(raw)} bytes, not {expect}")
    return raw


def open_sealed(private_b64: str, envelope: dict, *, agent_id: str, audience: str,
                job_id: str, ref: str) -> str:
    if not isinstance(envelope, dict):
        raise SealError("sealed envelope is not an object")
    if envelope.get("v") != SEAL_VERSION:
        raise SealError(
            f"the dashboard sealed this credential as version {envelope.get('v')!r}, "
            f"but this agent only understands version {SEAL_VERSION} — pull a newer "
            f"chrweav/dashboard-agent image and restart")
    if envelope.get("alg") != SEAL_ALG:
        raise SealError(
            f"sealed envelope algorithm {envelope.get('alg')!r} is not {SEAL_ALG}")

    private_raw = _seal_b64({"k": private_b64}, "k", _SEAL_KEY_BYTES)
    epk_raw = _seal_b64(envelope, "epk", _SEAL_KEY_BYTES)
    nonce = _seal_b64(envelope, "nonce", SEAL_NONCE_BYTES)
    ciphertext = _seal_b64(envelope, "ct")

    try:
        private = X25519PrivateKey.from_private_bytes(private_raw)
        shared = private.exchange(X25519PublicKey.from_public_bytes(epk_raw))
    except Exception as exc:  # noqa: BLE001
        raise SealError("X25519 key agreement failed") from exc

    recipient_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=SEAL_INFO).derive(epk_raw + recipient_raw + shared)
    aad = seal_aad(agent_id=agent_id, audience=audience,
                   epk=base64.b64encode(epk_raw).decode("ascii"),
                   job_id=job_id, ref=ref)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad)
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


# ── Locally sealed credentials (a secret this host keeps, but not in cleartext) ─
#
# For the customer who will not move a credential to the dashboard — that being the
# point of an on-prem agent for them — but does not want it sitting in a YAML file as
# text. `seal` encrypts one value against a key in this container's state volume and
# prints it; the operator pastes the result into `password_sealed:` and deletes the
# plaintext. `_secret_for` opens it per job.
#
# BE PRECISE ABOUT WHAT THIS DEFENDS AGAINST, because docs/remote-agents.md argues the
# opposite for identity.json and both statements have to stay true. This is NOT
# protection from root on this host: the key is on the same machine, which is unavoidable
# for a process that must restart unattended. What it protects is the file TRAVELLING.
# connections.yaml is authored, edited, versioned and copied — into a git repo, an
# Ansible role, a runbook, a support ticket, a screenshot, a backup — and identity.json
# is not. The key lives in the state volume, which none of those copies carry, so a copy
# of the config is worthless anywhere else.
#
# The AAD binds the connection's HOST, for the same reason `seal_aad` binds `ref`.
# HypervisorConnections.load() runs per job, so an edit takes effect with no restart and
# without Docker access; unbound, somebody who can write connections.yaml but not read
# the state volume could move a sealed vCenter password onto an entry whose host they
# control, with verify_ssl: false, and read the plaintext off the next sync. The cost is
# that changing `host` means re-sealing, and the refusal below says so.

LOCAL_SEAL_VERSION = 1
LOCAL_SEAL_PREFIX = f"sealed.v{LOCAL_SEAL_VERSION}."
LOCAL_PURPOSE_PASSWORD = "connection.password"
LOCAL_PURPOSE_PS_CLIENT = "passwordsafe.client_secret"
_LOCAL_KEY_FILE = os.path.join(STATE_DIR, "sealing.key")
_LOCAL_KEY_BYTES = 32
_LOCAL_NONCE_BYTES = 12
_MOUNTINFO = "/proc/self/mountinfo"
# Two roles, two labels, one file. The bytes in `sealing.key` are never used directly as
# either the AES key or the fingerprint — both are HKDF'd out of them under distinct `info`
# strings, the same domain separation `SEAL_INFO` gives the transport seal. It costs two
# hash calls and buys the thing that matters once a customer has sealed a value: the key
# file's contents stop being load-bearing on their own, so the id derivation can change, and
# a future operator-supplied passphrase can go in the same file without either role
# borrowing the other's material.
_LOCAL_CIPHER_INFO = b"vm-dashboard/agent-local-seal/v1/cipher"
_LOCAL_KEYID_INFO = b"vm-dashboard/agent-local-seal/v1/keyid"
# Lab escape hatch, in the shape of AGENT_INSECURE_TLS: seal against a key that will not
# survive the container. Case-sensitive for the same reason.
SEAL_EPHEMERAL_OK = os.environ.get(
    "AGENT_SEAL_EPHEMERAL_KEY_OK", "").strip() in ("1", "true", "yes")

# Read once per process. `_secret_for` has thirteen call sites and `_vmrest` reaches it on
# EVERY HTTP call — the same fact that made JobSecrets.dashboard_secret memoise — so an
# uncached read is one file open per API request. A module-level global is right here and
# not the usual per-worker hazard: the agent is a single process, not `gunicorn -w 2`.
_LOCAL_KEY_CACHE: Optional[bytes] = None


class LocalSealError(Exception):
    """A locally sealed value that could not be sealed or opened, and why.

    Separate from ``SealError`` — the dashboard-to-agent transport seal — because the two
    have nothing in common operationally: that one means "pull a newer image", this one
    means "you sealed against a different key, or moved the value, or changed the host".
    """


def _local_derive(material: bytes, info: bytes, length: int = 32) -> bytes:
    """HKDF-SHA256 one role out of the key file's bytes. See the two ``_INFO`` labels."""
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None,
                info=info).derive(material)


def _key_id(material: bytes) -> str:
    """A short fingerprint of the sealing key, derived under its own label.

    Carried in the token in the clear, and it earns its place: the mistake this feature
    will actually produce is running ``seal`` without mounting the same state volume, so
    the value is sealed against a key that dies with that container. Without an id the
    symptom is "decryption failed"; with one the agent can say which key sealed it and
    which key is here, which names the cause.

    Four bytes. It only has to distinguish two keys an operator might confuse, not resist
    a search — and it must not be a handle worth attacking, which is what the separate
    ``info`` guarantees: it shares no derivation with the key that does the encrypting.
    """
    return _local_derive(material, _LOCAL_KEYID_INFO, 4).hex()


def seal_host_key(host: str) -> str:
    """One address, one binding, however it was typed.

    Case and surrounding whitespace are obvious. The trailing dot is not: ``vc.lab.local.``
    is the same name as ``vc.lab.local`` to DNS, and a refusal over it would be
    indistinguishable from the relabelling this binding exists to catch.
    """
    return str(host or "").strip().rstrip(".").lower()


def bundle_ref(*, run_kind: str, transport: str, host: str, port) -> str:
    """The AAD ``ref`` for a Config-Management run bundle, bound to its target.

    Byte-identical to ``web_dashboard/services/agent_sealing.py::bundle_ref``, mirrored here
    because this file cannot import from that package; a parity test pins the two. If they
    ever disagree the symptom is "the sealed bundle did not authenticate", which points an
    operator at keys rather than at normalisation — so the mirror matters more than it looks.

    Rebuilt from the job envelope this agent has ALREADY signature-checked, never from the
    response that carried the ciphertext. That is the whole point: a dashboard which returned
    a bundle sealed for some other host cannot make the tag verify here. A bundle carries an
    SSH private key and a become password, so this is the most valuable instance in the
    protocol of the relabelling attack the ``ref`` binding exists to stop.
    """
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 0
    return f"ansible:{run_kind}:{transport}:{seal_host_key(host)}:{port}"


def local_seal_aad(*, host: str, purpose: str) -> bytes:
    """Rebuilt from the config file on both sides, never carried in the token.

    ``purpose`` keeps a sealed hypervisor password from being pasted in as a Password Safe
    client secret; ``host`` is the anti-redirect binding. Uses the same canonical
    ``serialize`` as everything else here so the byte encoding cannot drift. ``v`` is
    ``LOCAL_SEAL_VERSION``, the same number the token prefix is built from —
    ``tests/test_agent_local_seal.py`` pins them equal, because two independent "version 1"s
    in one construction is how a format change ships half-applied.
    """
    return serialize({"host": seal_host_key(host), "purpose": str(purpose),
                      "v": LOCAL_SEAL_VERSION})


def ps_seal_host(api_url: str) -> str:
    """The host component of a Password Safe ``api_url``, whichever form it was written in.

    passwordsafe.example.yaml accepts both the bare host and the full
    ``/BeyondTrust/api/public/v3`` path, and ``PasswordSafe.__init__`` normalises by
    appending. Binding the raw string would mean a value sealed against one form failing
    against the other for no reason the operator can see.

    ``hostname`` rather than ``netloc``, and the scheme is supplied when it is missing, for
    two failures that both look like tampering. ``netloc`` keeps the port, so
    ``https://ps:443`` and ``https://ps`` — the same server, written two legitimate ways —
    would not open each other's values. And with no scheme, ``urlparse`` reads
    ``ps.example.com:443/…`` as scheme ``ps.example.com``, leaving a *port number* as the
    binding: every such host would bind to ``443`` and the anti-relabel property would be
    gone for that form.
    """
    raw = str(api_url or "").strip()
    if raw and "://" not in raw:
        raw = "https://" + raw
    try:
        return seal_host_key(urlparse(raw).hostname or "")
    except ValueError:
        return ""  # an unparseable authority binds to nothing, and `local_seal` refuses it


def _state_dir_is_ephemeral() -> bool:
    """Whether a key written to STATE_DIR would die with this container.

    Two questions, because neither alone is the one that matters. ``st_dev`` against ``/``
    answers *"is this a different filesystem from the container's own layer"*, which catches
    the common case — no volume mounted at all. It does not answer *"will it still be there
    tomorrow"*: a ``--tmpfs`` mount is a different device and is gone on exit, and losing the
    key is unrecoverable, since the operator has been told to delete the plaintext. So the
    fstype is checked too.

    **Stat the directory, never a file inside it.** On overlayfs a non-directory inode can
    report the *lower* layer's ``st_dev``, which is the whole reason ``xino`` exists — so
    stat'ing ``sealing.key`` would sometimes report the image layer and conclude the opposite
    of the truth.

    ``st_dev`` is the primary check rather than ``/proc/self/mountinfo`` because it needs no
    parsing and cannot get the subdirectory-of-a-mount case wrong: with
    ``-v vol:/data -e AGENT_STATE_DIR=/data/agent`` the subdirectory inherits the volume's
    device, whereas an exact-match on mountinfo would report no mount. (A longest-prefix
    match on mountinfo would get that right too — this is a simplicity argument, not a
    correctness one.) The fstype lookup below *does* use mountinfo, with a longest-prefix
    match, and fails **open**: a wrong refusal here blocks a legitimate operator, which is
    the more expensive direction to be wrong in.
    """
    try:
        if os.stat(STATE_DIR).st_dev != os.stat("/").st_dev:
            # realpath here rather than inside _mount_fstype: symlink resolution is an OS
            # question and belongs with the stat calls, while the parser below is pure.
            return _mount_fstype(os.path.realpath(STATE_DIR)) in ("tmpfs", "ramfs")
        return True
    except OSError:
        return False  # cannot tell — never block on a question we could not answer


def _mount_fstype(path: str) -> str:
    """The filesystem type backing ``path``, or ``""`` when it cannot be determined.

    Longest-prefix match over ``/proc/self/mountinfo``, which is the only place a container
    can see this. Absent or unparseable — a non-Linux host, a restricted ``/proc`` — returns
    ``""``, and every caller treats that as "no objection".

    Takes an already-resolved path and normalises it with ``posixpath``, never
    ``os.path`` — mount points are POSIX whatever the interpreter is running on, and
    ``os.path`` on Windows rewrites ``/var/lib`` as ``C:\\var\\lib``, so every comparison
    here would silently fail to match. That is only reachable from a test, since ``_MOUNTINFO``
    does not exist on Windows, and a test that can only pass on one platform is the thing
    ``tests/test_open_encoding.py`` exists to stamp out.

    The source path is a module constant rather than a literal so a test can drive the parser
    against a real mountinfo fixture; there is nowhere else this file runs.
    """
    try:
        with open(_MOUNTINFO, encoding="utf-8") as fh:
            entries = fh.read().splitlines()
    except OSError:
        return ""
    target = posixpath.normpath(path)
    best_len, best_type = -1, ""
    for line in entries:
        head, sep, tail = line.partition(" - ")
        fields = head.split()
        if not sep or len(fields) < 5 or not tail.split():
            continue
        point = fields[4]
        if (target == point or target.startswith(point.rstrip("/") + "/")) \
                and len(point) > best_len:
            best_len, best_type = len(point), tail.split()[0]
    return best_type


def _generate_local_key() -> str:
    """Create the sealing key, once, refusing when it would not survive the container.

    ``O_EXCL`` rather than the write-temp-and-rename ``Identity.save`` uses, because this
    file is only ever created and never replaced: two ``seal`` runs racing must not have
    the second silently overwrite the key the first already sealed a value against. The
    loser re-reads instead. Nothing recovers a lost key — the values sealed with it are
    gone too — so this is worth four lines.

    Returns the key text, or ``""`` when another process created it first.
    """
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    if _state_dir_is_ephemeral() and not SEAL_EPHEMERAL_OK:
        raise LocalSealError(
            f"{STATE_DIR} is not on a volume that outlives this container — either nothing "
            f"is mounted there, or what is mounted is a tmpfs. A sealing key created here "
            f"would be destroyed when the container exits, and anything sealed with it "
            f"could never be opened again. Mount the SAME volume the agent uses and run "
            f"this again: -v dashboard_agent_state:{STATE_DIR}. For a throwaway lab, set "
            f"AGENT_SEAL_EPHEMERAL_KEY_OK=1.")
    text = base64.b64encode(os.urandom(_LOCAL_KEY_BYTES)).decode("ascii")
    try:
        fd = os.open(_LOCAL_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return ""
    except OSError as exc:
        raise LocalSealError(
            f"cannot create the sealing key at {_LOCAL_KEY_FILE} ({exc}). The volume has "
            f"to be writable by uid 10001.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return text


def local_key(*, create: bool = False) -> bytes:
    """The sealing key file's bytes — created on first use by ``seal``, then only read.

    Not the AES key: that and the fingerprint are both HKDF'd out of this under separate
    labels. See ``_LOCAL_CIPHER_INFO``.

    ``create`` is False on every job path on purpose. An agent that generated a key
    because it could not find one would turn "the wrong volume is mounted" into a silent
    new key and a pile of values it can no longer open; a refusal naming the file is the
    useful answer.

    Cached for the life of the process, which is right for one agent on one volume but means
    redirecting ``_LOCAL_KEY_FILE`` does nothing until the cache is cleared — worth knowing
    if you are writing a test.
    """
    global _LOCAL_KEY_CACHE
    if _LOCAL_KEY_CACHE is not None:
        return _LOCAL_KEY_CACHE

    try:
        text = _read_secret_file(_LOCAL_KEY_FILE)
    except FileNotFoundError:
        if not create:
            raise LocalSealError(
                f"a sealed value needs the sealing key, and there is none at "
                f"{_LOCAL_KEY_FILE}. That path is the state volume — check the agent is "
                f"running with the same -v the value was sealed with, and that the volume "
                f"has not been recreated.")
        text = _generate_local_key()
        if not text:
            # Another process won the O_EXCL race. Re-read inside its own guard, so a
            # surprise here still arrives as a LocalSealError rather than a raw chained
            # traceback thrown from inside an except block.
            try:
                text = _read_secret_file(_LOCAL_KEY_FILE)
            except (OSError, ValueError) as exc:
                raise LocalSealError(
                    f"another process created {_LOCAL_KEY_FILE} at the same moment and it "
                    f"cannot be read back ({exc}). Run this again.")
    except ValueError:
        raise LocalSealError(_secret_file_undecodable("the sealing key",
                                                     _LOCAL_KEY_FILE))
    except OSError as exc:
        raise LocalSealError(
            f"cannot read the sealing key at {_LOCAL_KEY_FILE} ({exc}). The container "
            f"runs as uid 10001 and the file is mode 0600, so a key owned by another user "
            f"is unreadable inside it.")

    try:
        key = base64.b64decode(text.encode("ascii"), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise LocalSealError(
            f"{_LOCAL_KEY_FILE} is not base64. It should be one line holding "
            f"{_LOCAL_KEY_BYTES} base64-encoded random bytes.") from exc
    if len(key) != _LOCAL_KEY_BYTES:
        raise LocalSealError(
            f"{_LOCAL_KEY_FILE} decodes to {len(key)} bytes, not {_LOCAL_KEY_BYTES}. If "
            f"nothing has been sealed with it yet, delete it and seal again; if something "
            f"has, restore the file — a truncated key cannot open it.")
    _LOCAL_KEY_CACHE = key
    return key


def local_seal(value: str, *, host: str, purpose: str) -> str:
    """Seal one operator-supplied value. Creates the key if this is the first one."""
    if not str(value):
        raise LocalSealError(
            "there is nothing to seal — the value read back empty. If you ran `docker run` "
            "without `-it`, the container had no stdin to read from and nothing was typed.")
    if not seal_host_key(host):
        # A seal bound to nothing has no anti-relabel property, which is most of the reason
        # to seal at all. Unreachable through `seal` (it requires an address) and through a
        # job (`_conn_endpoint` refuses a hostless connection first), so this is the guard
        # for a future caller rather than for today's.
        raise LocalSealError("a sealed value must be bound to an address, and none was "
                             "given.")
    material = local_key(create=True)
    nonce = os.urandom(_LOCAL_NONCE_BYTES)
    ciphertext = AESGCM(_local_derive(material, _LOCAL_CIPHER_INFO)).encrypt(
        nonce, str(value).encode("utf-8"), local_seal_aad(host=host, purpose=purpose))
    blob = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
    return f"{LOCAL_SEAL_PREFIX}{_key_id(material)}.{blob}"


def looks_locally_sealed(value) -> bool:
    """Is this a sealed token? Used to refuse one that is in the wrong field.

    A string starting with the prefix is not proof of anything, which is exactly why
    finding one somewhere it does not belong is answered with a refusal naming the right
    key rather than an attempt to open it.
    """
    return isinstance(value, str) and value.strip().startswith(LOCAL_SEAL_PREFIX)


def local_unseal(token: str, *, host: str, purpose: str, what: str = "value") -> str:
    """Open a sealed value, or say which of the five things went wrong."""
    text = str(token or "").strip()
    if not looks_locally_sealed(text):
        # The likeliest mistake in a paste-it-in workflow is pasting the wrong thing, so this
        # must not be reported as a truncated token. It used to say "starts with sealed.v1.
        # but is not a sealed value" about a value that plainly does not, and sent the
        # operator after a truncation that never happened.
        raise LocalSealError(
            f"this {what} is not a sealed value: it has to be the whole "
            f"{LOCAL_SEAL_PREFIX}… line that `seal` printed, on one line. If that is the "
            f"credential itself, either seal it — which is the point of this field — or put "
            f"it back in the plaintext field it belongs to.")
    sealed_id, _, blob = text[len(LOCAL_SEAL_PREFIX):].partition(".")
    if not sealed_id or not blob:
        raise LocalSealError(
            f"this {what} begins as a sealed value but is not a complete one — the form is "
            f"{LOCAL_SEAL_PREFIX}<key id>.<data>. Most likely it was truncated on the way "
            f"into the file.")
    try:
        raw = base64.urlsafe_b64decode(blob.encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise LocalSealError(
            f"this sealed {what} is not valid base64 — most likely it was wrapped onto a "
            f"second line, or characters were dropped pasting it in. It has to be one "
            f"unbroken line.") from exc
    if len(raw) <= _LOCAL_NONCE_BYTES:
        raise LocalSealError(f"this sealed {what} is too short to be one.")

    material = local_key()
    here = _key_id(material)
    if sealed_id != here:
        raise LocalSealError(
            f"this {what} was sealed with key {sealed_id}, but the key in "
            f"{_LOCAL_KEY_FILE} is {here}. Almost always that means `seal` was run "
            f"WITHOUT mounting the agent's state volume, so it sealed against a key that "
            f"no longer exists. Seal it again with "
            f"-v dashboard_agent_state:{STATE_DIR} and paste the new value in.")
    try:
        plaintext = AESGCM(_local_derive(material, _LOCAL_CIPHER_INFO)).decrypt(
            raw[:_LOCAL_NONCE_BYTES], raw[_LOCAL_NONCE_BYTES:],
            local_seal_aad(host=host, purpose=purpose))
    except Exception as exc:  # noqa: BLE001
        raise LocalSealError(
            f"this sealed {what} did not authenticate. It is bound to "
            f"{seal_host_key(host)!r}, so either that address has changed "
            f"since it was sealed — in which case seal it again against the new one — or "
            f"the value was moved here from another entry, which is what the binding "
            f"exists to stop.") from exc
    return plaintext.decode("utf-8")


# ── Policy ────────────────────────────────────────────────────────────────────

_DESKTOP_KERNEL_MARKERS = ("microsoft", "wsl", "linuxkit")


def _in_docker_desktop_vm() -> bool:
    """Whether this container's kernel is a Docker Desktop / WSL2 Linux VM.

    ``/proc/version`` names the kernel the container shares with whatever is running it,
    which is about the only thing a container can learn about its host without asking.
    Docker Desktop brands both of its backends: ``-microsoft-standard-WSL2`` on the WSL2
    one and ``-linuxkit`` on the Hyper-V one (and on macOS). Neither VM has SELinux and
    neither can, so where this is true every ``chcon`` instruction is not merely unhelpful
    but impossible — and the real cause of an unreadable bind mount is file sharing.

    ``False`` is the safe answer, which is why every failure path returns it: it produces
    the message that names both causes rather than the one that names the wrong cause, so
    a mis-detection costs a longer message and never a misleading one.
    """
    try:
        with open("/proc/version", encoding="utf-8", errors="replace") as fh:
            version = fh.read().lower()
    except OSError:
        return False
    return any(marker in version for marker in _DESKTOP_KERNEL_MARKERS)


def _policy_unreadable(path: str, exc: OSError) -> str:
    """Explain an unreadable policy file in terms of its most likely actual cause.

    Worth the effort because this is the least diagnosable failure in the whole system.
    The agent refuses to start without a policy, and it loads it *before* the first
    network call — so nothing reaches the dashboard at all: no request, no 4xx, nothing in
    the app or ingress logs, and an agent row that sits at ``enrolling`` with no source IP
    and no policy hash. Every symptom visible from the dashboard says "DNS or TLS", and
    the container log is the only place the truth exists.

    The interesting case is ``EACCES`` on a file whose mode is already world-readable. On
    an SELinux-enforcing host — Fedora, RHEL, CentOS, Rocky, Alma, which is a large share
    of on-prem Linux and therefore of agent hosts — a bind mount keeps its host label, and
    ``container_t`` may not read the ``user_home_t`` of a file in the operator's home
    directory whatever its mode says. Mode bits are only half of "readable" there, so the
    stock advice about uid 10001 sends the reader down the wrong path. This function is the
    one place that can see both halves of the evidence, so it says which one is wrong.

    That label advice is **Linux-only**, and unqualified it is worse than saying nothing at
    all: a Windows or macOS agent host runs this image in a Docker Desktop VM that has no
    SELinux to relabel in, so ``chcon`` and ``ls -lZ`` cannot be followed even in
    principle, while the real cause — Docker Desktop file sharing — goes unmentioned.
    :func:`_in_docker_desktop_vm` is what lets the message pick, and it is the runtime
    counterpart of the Linux-only qualification on the same rows in
    ``docs/remote-agents.md``.
    """
    base = (f"Cannot read the policy file at {path}: {exc}. The agent refuses all "
            f"work without one — mount it read-only and restart.")
    if getattr(exc, "errno", None) != errno.EACCES:
        return base
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        # Even stat was refused, so the file's own mode is not what is being enforced:
        # either the label, or a parent directory this uid cannot search.
        where = ("check that every parent directory is searchable, and that Docker "
                 "Desktop shares this drive (Settings → Resources → File sharing)"
                 if _in_docker_desktop_vm() else
                 "on a Linux host check its SELinux label (`ls -lZ` on the host, and "
                 "mount it `:ro,Z`), and check that every parent directory is searchable")
        return (f"{base} Permission was denied without the file's mode even being "
                f"readable, which points at the mount rather than at the file: {where}.")
    if mode & stat.S_IROTH:
        if _in_docker_desktop_vm():
            # The SELinux message below is the single most misleading thing this process can
            # say on a Windows or macOS host. The label advice there is not merely wrong, it
            # is unfollowable — there is no SELinux in the VM to relabel anything in — and an
            # operator who trusts it spends the afternoon on `chcon`.
            return (
                f"Cannot read the policy file at {path}: {exc}. Its mode is {mode:04o}, so "
                f"it is world-readable and the mode is not the problem. This container is "
                f"running in a Docker Desktop / WSL2 Linux VM, which has no SELinux — so "
                f"the relabelling advice written for a RHEL-family host cannot be what is "
                f"wrong here, whatever you may have read, and there is nothing on this host "
                f"to relabel. The cause is file sharing: check that Settings → Resources → "
                f"File sharing covers the drive, and note that a UNC path (\\\\server\\share) "
                f"or a mapped network drive cannot be bind-mounted at all. On the Hyper-V "
                f"backend a bind source Docker Desktop cannot reach is materialised as an "
                f"empty directory inside the container rather than failing outright, so the "
                f"path can look perfectly present from the host — keep policy.yaml under "
                f"your user profile and this does not arise. The agent refuses all work "
                f"without a readable policy, but it has not contacted the dashboard yet, "
                f"so your enrolment code is still unspent.")
        return (
            f"Cannot read the policy file at {path}: {exc}. Its mode is {mode:04o}, so it "
            f"is world-readable and the mode is not the problem — on an SELinux-enforcing "
            f"host (Fedora, RHEL, CentOS, Rocky, Alma) a bind-mounted file keeps its host "
            f"label, and this container may not read a file labelled user_home_t no matter "
            f"how permissive its mode is. Add the relabel flag to the mount — "
            f'-v "$PWD/policy.yaml:{path}:ro,Z" — or relabel the host file in place with '
            f"`chcon -t container_file_t policy.yaml`; confirm with `ls -lZ policy.yaml` "
            f"and `ausearch -m avc -ts recent`. The agent refuses all work without a "
            f"readable policy, but it has not contacted the dashboard yet, so your "
            f"enrolment code is still unspent — fix the mount and start again.")
    return (f"Cannot read the policy file at {path}: {exc}. Its mode is {mode:04o} and "
            f"this container runs as uid {os.getuid()}, so a file readable only by its "
            f"owner on the host is unreadable here — make it world-readable (`chmod 644`) "
            f"and restart. The agent refuses all work without a readable policy.")


class _DuplicatePolicyKey(yaml.constructor.ConstructorError):
    """A key written twice in policy.yaml. Carries its own finished message."""


class _PolicyLoader(yaml.SafeLoader):
    """``yaml.SafeLoader``, except that a repeated key is an error.

    PyYAML's default is last-wins, silently: an operator who writes two ``targets:``
    blocks — or, having been told to add a range, adds a second ``cidr:`` line to an
    entry that already had one — keeps only the second, with nothing logged and no
    shape error to notice afterwards. Everywhere else that is a merge conflict waiting
    to happen; in the file that IS the security boundary it is a grant the operator
    wrote, believes they have, and does not have.
    """

    def construct_mapping(self, node, deep=False):
        # Checked BEFORE flatten_mapping, so the keys compared are the ones actually
        # typed in the file: a mapping merged in with `<<:` is *meant* to be overridden
        # by the keys beside it, and that is not the mistake being caught here.
        seen = {}
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            try:
                key = self.construct_object(key_node, deep=deep)
                first = seen.get(key)
            except TypeError:
                continue        # unhashable key; SafeConstructor refuses it below
            if first is not None:
                # A duplicate whose key begins with a dash is not really a duplicate:
                # it is the same missing space after the dash, twice, and telling that
                # operator to merge the two would send them the wrong way entirely.
                fix = (f"Merge them into a single `{key}:` with every entry under it."
                       if not str(key).startswith("-") else
                       f"A key beginning with `-` is a missing space after the dash: "
                       f"`{key}: ...` is a key named `{key}`, while "
                       f"`{str(key)[0]} {str(key)[1:]}: ...` is a list entry. Add the "
                       f"space to each of them.")
                raise _DuplicatePolicyKey(
                    None, None,
                    f"policy.yaml: `{key}` is written twice in the same block (line "
                    f"{first}, and again at line {key_node.start_mark.line + 1}). YAML "
                    f"keeps only the LAST one, so everything under the earlier copy is "
                    f"discarded without a word. {fix}", None)
            seen[key] = key_node.start_mark.line + 1
        return super().construct_mapping(node, deep=deep)


class Policy:
    """The customer's allow-list. Fails closed, always.

    Matching happens on the **resolved IP** at connect time, not on the hostname the
    dashboard asked for. A hostname check alone is defeated by DNS rebinding: the name
    resolves inside the allowed range when validated and somewhere else when connected.
    """

    def __init__(self, allow: list, deny: list, job_types: set, limits: dict,
                 digest: str, connection_verbs: dict = None, sibling: dict = None,
                 loopback_connections: set = None, ansible: dict = None,
                 ansible_allow: list = None, gateway: dict = None):
        self.allow = allow            # [(network, {ports} or None)]
        self.deny = deny              # [network]
        self.job_types = job_types
        self.limits = limits
        self.digest = digest
        self.connection_verbs = connection_verbs or {}   # {connection name: {verb}}
        # Config Management, off unless the customer turns it on AND names an image.
        #
        # This has its own block and its own target list rather than riding `targets:` and
        # `sibling:`, and the separation is the point: "may be TCP-probed for a management
        # endpoint" and "may have an arbitrary playbook applied to it as root" are different
        # grants, and an operator who widened the first to run a discovery sweep has not
        # agreed to the second. Reusing one list would silently conflate them.
        #
        # Both images come from HERE and never from a job. A job says only which KIND of run
        # it is (`vm` / `database`, a closed enum), and that selects between these two.
        ansible = ansible or {}
        self.ansible_enabled = bool(ansible.get("enabled"))
        self.ansible_vm_image = str(ansible.get("vm_image") or "")
        self.ansible_db_image = str(ansible.get("db_image") or "")
        # Defaults to the sibling network so an operator who already configured one does not
        # have to say it twice; `none` is refused before create because it makes every run
        # fail with an unreachable host and no explanation.
        self.ansible_network = str(ansible.get("network")
                                   or (sibling or {}).get("network") or "bridge")
        try:
            self.ansible_max_runtime_minutes = int(ansible.get("max_runtime_minutes") or 30)
        except (TypeError, ValueError):
            self.ansible_max_runtime_minutes = 30
        # Empty means "nothing may be configured", which is the correct fail-closed reading
        # of an `ansible:` block with no targets — not "fall back to the discovery list".
        self.ansible_allow = ansible_allow or []
        # The BeyondTrust Gateway, off unless the customer turns it on AND names an image.
        #
        # Its own block rather than a third `ansible.*_image`, because what it grants is
        # different in kind: not "run a playbook and exit" but "run a LONG-LIVED container,
        # privileged, that brokers sessions into this network". `privileged` is the whole
        # reason it is spelled out here — a Gateway needs NET_ADMIN, NET_RAW, IPC_LOCK and
        # /dev/net/tun for protocol tunnelling, and without them it registers online and
        # then times out on tunnel data, which reads as a firewall problem for days.
        #
        # A JOB CANNOT ASK FOR ANY OF THIS. There is no image field, no privileged field
        # and no network field in an agent_gateway envelope — the same property
        # `_run_sibling` documents. The customer opts in here or it does not happen.
        gateway = gateway or {}
        self.gateway_enabled = bool(gateway.get("enabled"))
        self.gateway_image_name = str(gateway.get("image") or "")
        # Default FALSE. An operator who enables the block without reading has granted a
        # container, not a privileged one, and gets a Gateway that registers and cannot
        # tunnel — which the refusal below names, rather than leaving them to find it.
        self.gateway_privileged = bool(gateway.get("privileged"))
        self.gateway_network = str(gateway.get("network")
                                   or (sibling or {}).get("network") or "bridge")

        # The sibling runner is off unless the customer turns it on AND names an image.
        # The image comes from here and never from a job, so a compromised dashboard
        # cannot choose what gets run on the host.
        sibling = sibling or {}
        self.sibling_enabled = bool(sibling.get("enabled"))
        self.sibling_image = str(sibling.get("image") or "")
        self.sibling_network = str(sibling.get("network") or "bridge")
        # Connections allowed to reach a LOOPBACK endpoint. Opt-in, per connection, and
        # deliberately NOT a way to edit the deny list: `check()` still refuses
        # 127.0.0.0/8 unconditionally, which is what keeps a discovery sweep from
        # probing the agent's own container or a cloud metadata service. The exception
        # applies only on the named-connection path (`_check_endpoint`), where the
        # operator already wrote the name, the host and the port in their own files.
        #
        # It exists for one real case: VMware Workstation's `vmrest` binds 127.0.0.1,
        # so a genuinely co-located agent cannot reach it any other way.
        self.loopback_connections = loopback_connections or set()

    def allows_loopback(self, connection_ref: str) -> bool:
        return bool(connection_ref) and connection_ref in self.loopback_connections

    def check_verb(self, connection_ref: str, verb: str) -> None:
        """Raise :class:`PolicyRefusal` unless policy.yaml grants this verb here.

        Names the file and the line to add, because the operator seeing this message
        is looking at Live Output on a dashboard that cannot fix it for them.
        """
        granted = self.connection_verbs.get(connection_ref)
        if granted is None:
            raise PolicyRefusal(
                f"policy.yaml grants no verbs for connection {connection_ref!r}. Add it "
                f"under `connections:` with a `verbs:` list and restart the agent.")
        if verb not in granted:
            raise PolicyRefusal(
                f"policy.yaml does not grant {verb!r} on {connection_ref!r} "
                f"(granted: {', '.join(sorted(granted)) or 'none'}). Add it to that "
                f"connection's `verbs:` list and restart the agent.")

    def ansible_image(self, run_kind: str) -> str:
        """The sibling image for this kind of run, or raise :class:`PolicyRefusal`.

        A closed dict rather than a lookup on the wire string, for the same reason
        ``HANDLERS`` is: the job selects between two values the CUSTOMER wrote down, and can
        never name an image of its own.
        """
        if not self.ansible_enabled:
            raise PolicyRefusal(
                "this agent's policy.yaml does not enable Config Management. Add an "
                "`ansible:` block with `enabled: true`, the image(s) for the kinds of run "
                "you want, and a `targets:` list — see "
                "docs/remote-agents.md#agent-executed-ansible. Note this is a separate "
                "grant from `targets:` and `sibling:` on purpose: it allows a playbook to "
                "be applied to a host, not merely a port to be probed.")
        images = {"vm": ("vm_image", self.ansible_vm_image),
                  "database": ("db_image", self.ansible_db_image)}
        if run_kind not in images:
            raise PolicyRefusal(
                f"{run_kind!r} is not a kind of Config-Management run this agent build "
                f"knows (known: {', '.join(sorted(images))}).")
        key, image = images[run_kind]
        if not image:
            raise PolicyRefusal(
                f"policy.yaml enables Config Management but names no `ansible.{key}`, "
                f"which is the image a {run_kind!r} run needs. Add it and pull it — the "
                f"agent will not pull an image for you.")
        return image

    def gateway_image(self) -> str:
        """The Gateway image, or raise :class:`PolicyRefusal` naming what is missing.

        Three separate refusals rather than one, because they have three different fixes
        and the operator reading this is looking at Live Output on a dashboard that cannot
        edit their file for them.
        """
        if not self.gateway_enabled:
            raise PolicyRefusal(
                "this agent's policy.yaml does not enable the BeyondTrust Gateway. Add a "
                "`gateway:` block with `enabled: true`, the image, and "
                "`privileged: true` — see docs/remote-agents.md#the-beyondtrust-gateway. "
                "It needs the Docker socket, like the other runners.")
        if not self.gateway_image_name:
            raise PolicyRefusal(
                "policy.yaml enables the Gateway but names no `gateway.image`. Add it and "
                "pull it — the agent will not pull an image for you.")
        if not self.gateway_privileged:
            raise PolicyRefusal(
                "policy.yaml enables the Gateway but does not set "
                "`gateway.privileged: true`. A Gateway needs NET_ADMIN, NET_RAW, IPC_LOCK "
                "and /dev/net/tun to carry protocol tunnels; without them it registers "
                "online and every tunnel silently times out, which looks like a firewall "
                "for a long time. This agent will not start one it knows cannot work.")
        return self.gateway_image_name

    def check_ansible(self, ip: str, port: int) -> None:
        """Raise :class:`PolicyRefusal` unless a playbook may be run against this endpoint.

        Deliberately NOT :meth:`check`. The deny list is shared — loopback and
        cloud-metadata are never reachable however the policy is written — but the allow
        list is `ansible.targets`, which the customer writes separately from `targets`.
        Falling back to `targets` when `ansible.targets` is empty would turn "I widened the
        scan range" into "I authorised root on that subnet", which is the whole reason these
        are two lists.
        """
        # Re-checked here rather than assumed from the caller having already asked for an
        # image. Two gates that are each independently sufficient beat two that are only
        # sufficient in the right order — the ordering is exactly what a later caller breaks.
        if not self.ansible_enabled:
            raise PolicyRefusal(
                "this agent's policy.yaml does not enable Config Management "
                "(`ansible: {enabled: true}`), so no endpoint may be configured.")
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            raise PolicyRefusal(f"{ip} is not an IP address")
        for net in self.deny:
            if addr in net:
                raise PolicyRefusal(f"{ip} is in a denied range ({net})")
        for net, ports in self.ansible_allow:
            if addr in net and (ports is None or port in ports):
                return
        raise PolicyRefusal(
            f"policy.yaml does not allow Config Management against {ip}:{port}. Add it "
            f"under `ansible.targets:` — with the port ({port}) named — and restart the "
            f"agent. The `targets:` list above it does NOT grant this: that one allows a "
            f"port probe, this one allows a playbook to run as root on that host.")

    @classmethod
    def _parse_targets(cls, entries, where: str) -> list:
        """``[(network, {ports} or None)]`` from a list of ``cidr``/``fqdn`` entries.

        One parser for both ``targets:`` and ``ansible.targets:``. They are separate
        *grants* and must never share a list, but they have the same shape — and two
        parsers is how one of them would quietly stop pinning resolved addresses.

        Every shape that is not a target is fatal rather than skipped. A skipped entry
        is the worst failure this file has: the YAML parses, the agent starts, the
        digest matches, and the grant the operator wrote is simply not there — so the
        refusal they eventually read points at a line they can see is correct.
        """
        out = []
        if entries is None:
            return out
        if not isinstance(entries, (list, tuple)):
            found = ""
            if isinstance(entries, dict):
                keys = ", ".join(f"`{k}`" for k in list(entries)[:6])
                found = (f" It parsed as a mapping with the key(s): {keys}, which is what "
                         f"a missing space after the dash does — `-cidr: 10.0.0.0/24` is "
                         f"valid YAML for a key NAMED `-cidr`, not a list entry.")
            raise AgentFatal(
                f"policy.yaml: `{where}` must be a list of `- cidr:` / `- fqdn:` entries "
                f"(a {type(entries).__name__} was written instead).{found} Write "
                f"`- cidr: 10.0.0.0/24`, with the dash, a space, then the key.")
        for entry in entries:
            if not isinstance(entry, dict):
                raise AgentFatal(
                    f"policy.yaml: `{where}` contains {entry!r}, which is not a target. "
                    f"Every item is a mapping naming `cidr:` or `fqdn:` — write "
                    f"`- cidr: {entry}` rather than a bare value.")
            ports = entry.get("ports")
            if ports is None:
                pass                      # omitted means any port in the range
            elif isinstance(ports, (list, tuple)):
                try:
                    ports = {int(p) for p in ports}
                except (TypeError, ValueError):
                    raise AgentFatal(f"policy.yaml: `ports: {list(ports)}` under "
                                     f"`{where}` must be numbers, e.g. `ports: [22, 5985]`.")
            else:
                # Not merely ignored: an unrecognised `ports` used to mean "no ports
                # named", which means EVERY port in the range. The typo widened the
                # grant instead of narrowing it, which is the one direction this file
                # must never fail in.
                raise AgentFatal(
                    f"policy.yaml: `ports: {ports!r}` under `{where}` must be a list — "
                    f"write `ports: [{ports}]`. A bare value would be read as no ports "
                    f"named at all, which allows EVERY port in that range.")
            if entry.get("cidr"):
                try:
                    out.append((ipaddress.ip_network(str(entry["cidr"]), strict=False), ports))
                except ValueError as exc:
                    raise AgentFatal(f"policy.yaml: bad cidr {entry['cidr']!r} "
                                     f"under `{where}`: {exc}")
            elif entry.get("fqdn"):
                # Resolved once at load and pinned to the resulting addresses, so the
                # allow-list is always expressed in IPs by the time it is consulted.
                for addr in _resolve_all(str(entry["fqdn"])):
                    out.append((ipaddress.ip_network(addr + "/32" if ":" not in addr
                                                     else addr + "/128"), ports))
            else:
                keys = ", ".join(f"`{k}`" for k in list(entry)[:6]) or "none"
                raise AgentFatal(
                    f"policy.yaml: an entry under `{where}` names neither `cidr:` nor "
                    f"`fqdn:` (it has: {keys}). One of the two is what makes it a target; "
                    f"an entry with only `ports:` matches no host at all.")
        return out

    @classmethod
    def load(cls, path: str) -> "Policy":
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError as exc:
            raise AgentFatal(_policy_unreadable(path, exc))
        try:
            doc = yaml.load(raw, _PolicyLoader) or {}
        except _DuplicatePolicyKey as exc:
            raise AgentFatal(str(exc))
        except yaml.YAMLError as exc:
            raise AgentFatal(f"policy.yaml is not valid YAML: {exc}")
        if not isinstance(doc, dict):
            raise AgentFatal("policy.yaml must be a mapping at the top level.")

        allow = cls._parse_targets(doc.get("targets"), "targets")
        if not allow:
            raise AgentFatal(
                "policy.yaml declares no targets, so the agent can reach nothing. Add a "
                "`targets:` list with at least one cidr or fqdn.")

        deny = []
        for cidr in doc.get("deny") or _DEFAULT_DENY:
            try:
                deny.append(ipaddress.ip_network(str(cidr), strict=False))
            except ValueError as exc:
                raise AgentFatal(f"policy.yaml: bad deny entry {cidr!r}: {exc}")
        # The link-local and loopback denies are not optional. An operator who omits
        # them is not opting into cloud-metadata access, they just forgot.
        for mandatory in _DEFAULT_DENY:
            net = ipaddress.ip_network(mandatory)
            if net not in deny:
                deny.append(net)

        job_types = set(doc.get("job_types") or ["agent_discover"])
        limits = doc.get("limits") if isinstance(doc.get("limits"), dict) else {}
        # Per-connection verb grants. The CUSTOMER decides what may be done to each
        # target, not the dashboard: a connection absent from this list, or present
        # without a verb, is refused however the job was signed.
        sibling = doc.get("sibling") if isinstance(doc.get("sibling"), dict) else {}
        verbs = {}
        loopback = set()
        for entry in doc.get("connections") or []:
            if isinstance(entry, dict) and entry.get("name"):
                name = str(entry["name"])
                verbs[name] = {str(v) for v in (entry.get("verbs") or [])}
                if entry.get("allow_loopback"):
                    loopback.add(name)
        ansible = doc.get("ansible") if isinstance(doc.get("ansible"), dict) else {}
        ansible_allow = cls._parse_targets(ansible.get("targets"), "ansible.targets")
        gateway = doc.get("gateway") if isinstance(doc.get("gateway"), dict) else {}
        return cls(allow, deny, job_types, limits,
                   hashlib.sha256(raw).hexdigest(), verbs, sibling, loopback,
                   ansible, ansible_allow, gateway)

    def check(self, ip: str, port: int) -> None:
        """Raise :class:`PolicyRefusal` unless this exact address:port is allowed."""
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            raise PolicyRefusal(f"{ip} is not an IP address")
        for net in self.deny:
            if addr in net:
                raise PolicyRefusal(f"{ip} is in a denied range ({net})")
        for net, ports in self.allow:
            if addr in net and (ports is None or port in ports):
                return
        raise PolicyRefusal(f"{ip}:{port} is not in any allowed target range")

    def allows(self, ip: str, port: int) -> bool:
        try:
            self.check(ip, port)
            return True
        except PolicyRefusal:
            return False

    def allowed_networks(self) -> list:
        return [net for net, _ports in self.allow]

    def limit(self, name: str, default: int) -> int:
        try:
            return int(self.limits.get(name, default))
        except (TypeError, ValueError):
            return default


# Cloud instance metadata, loopback, and the IPv6 equivalents. Denied unconditionally.
_DEFAULT_DENY = ["169.254.0.0/16", "127.0.0.0/8", "::1/128", "fe80::/10"]


def _resolve_all(host: str) -> list:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []
    return sorted({i[4][0] for i in infos})


# ── Identity ──────────────────────────────────────────────────────────────────

class Identity:
    def __init__(self, agent_id: str, private_b64: str, dashboard_public: str,
                 audience: str):
        self.agent_id = agent_id
        self.private_b64 = private_b64
        self.dashboard_public = dashboard_public
        self.audience = audience

    def sign(self, message: bytes) -> str:
        key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(self.private_b64))
        return base64.b64encode(key.sign(message)).decode("ascii")

    def verify_dashboard(self, message: bytes, signature: str) -> bool:
        try:
            key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(self.dashboard_public))
            key.verify(base64.b64decode(signature), message)
            return True
        except Exception:  # noqa: BLE001 — every failure mode is one answer
            return False

    def save(self) -> None:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        tmp = _IDENTITY_FILE + ".tmp"
        # Create with 0600 from the start rather than chmod-ing after: a private key
        # must never exist world-readable, not even for an instant.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"agent_id": self.agent_id, "private_key": self.private_b64,
                       "dashboard_public_key": self.dashboard_public,
                       "audience": self.audience}, fh)
        os.replace(tmp, _IDENTITY_FILE)

    @staticmethod
    def load() -> Optional["Identity"]:
        try:
            with open(_IDENTITY_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        try:
            return Identity(data["agent_id"], data["private_key"],
                            data["dashboard_public_key"], data["audience"])
        except KeyError:
            log.warning("identity file is incomplete; re-enrolling")
            return None


# ── Dashboard client ──────────────────────────────────────────────────────────

class Dashboard:
    def __init__(self, base_url: str):
        self.base = base_url
        self._host = urlparse(base_url).netloc
        self.session = requests.Session()
        # trust_env picks up HTTP(S)_PROXY, NO_PROXY and SSL_CERT_FILE, which is how
        # this works behind a corporate proxy without any bespoke configuration.
        self.session.trust_env = True
        if CA_BUNDLE:
            self.session.verify = CA_BUNDLE
        elif INSECURE_TLS:
            self.session.verify = False
        self.identity: Optional[Identity] = None
        # Credentials fetched for the job in flight, held only to scrub them out of
        # anything sent back. Cleared by `execute` when the job ends.
        self._held: set = set()

    def _url(self, path: str) -> str:
        url = f"{self.base}{path}"
        # An SSRF-shaped bug elsewhere in this process must not be able to turn the
        # agent into an open proxy into the customer's network.
        if urlparse(url).netloc != self._host:
            raise AgentFatal(f"refusing to call a host other than {self._host}")
        return url

    def hold_secret(self, value: str) -> None:
        """Remember a fetched credential so it can be scrubbed from anything outbound.

        Registered here rather than at each use site because this object owns the only
        two channels back to the dashboard — Live Output and the job's error string — and
        filtering at the exit is the only approach that actually holds. The alternative is
        auditing every library's exception text, which cannot be done: a URL with embedded
        credentials, an XML-RPC fault echoing the request it failed on, or a driver that
        helpfully prints its connection string all arrive as `str(exc)`, and `execute`
        ships that verbatim into the job row where it renders as the failure reason.
        """
        if value and len(str(value)) >= 4:
            self._held.add(str(value))

    def release_secrets(self) -> None:
        self._held.clear()

    def redact(self, text: str) -> str:
        """``text`` with every credential this job fetched replaced.

        Not a defence against a hostile process — it shares memory with the credential
        either way. It is a defence against the ordinary accident of a secret riding out
        inside somebody else's error message.
        """
        out = str(text)
        for secret in self._held:
            if secret in out:
                out = out.replace(secret, "***redacted***")
        return out

    def _scrub(self, payload):
        if not self._held:
            return payload
        if isinstance(payload, dict):
            return {k: self._scrub(v) for k, v in payload.items()}
        if isinstance(payload, list):
            return [self._scrub(v) for v in payload]
        if isinstance(payload, str):
            return self.redact(payload)
        return payload

    def _request(self, method: str, path: str, payload: dict, *, signed: bool = True):
        # Scrubbed BEFORE serialisation, so the signature covers the bytes actually sent.
        body = serialize(self._scrub(payload))
        headers = {"Content-Type": "application/json"}
        if signed:
            if not self.identity:
                raise AgentFatal("cannot sign a request before enrolment")
            ts = str(int(time.time()))
            nonce = os.urandom(16).hex()
            message = canonical_request(
                agent_id=self.identity.agent_id, timestamp=ts, nonce=nonce,
                audience=self.identity.audience, method=method, path=path, body=body)
            headers.update({
                HEADER_AGENT_ID: self.identity.agent_id,
                HEADER_TIMESTAMP: ts,
                HEADER_NONCE: nonce,
                HEADER_SIGNATURE: self.identity.sign(message),
            })
        # content=body, never json=payload: the signature covers these exact bytes, and
        # letting requests re-serialise would produce a signature over something else.
        resp = self.session.request(
            method, self._url(path), data=body, headers=headers,
            timeout=_HTTP_TIMEOUT, allow_redirects=False)
        if len(resp.content) > _MAX_RESPONSE_BYTES:
            raise AgentFatal("dashboard response exceeded the size cap")
        return resp

    # ── protocol ──

    def enroll(self, code: str) -> Identity:
        private = Ed25519PrivateKey.generate()
        private_b64 = base64.b64encode(private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption())).decode("ascii")
        public_b64 = base64.b64encode(private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw)).decode("ascii")

        resp = self._request("POST", "/api/agent/enroll", {
            "enrollment_code": code,
            "public_key": public_b64,
            "agent_version": AGENT_VERSION,
            "policy_hash": POLICY.digest,
        }, signed=False)
        if resp.status_code == 429:
            wait = _retry_after_seconds(resp, 30)
            raise AgentFatal(
                f"the dashboard is throttling enrolment and asked for a retry in "
                f"{wait:.0f}s. Nothing is wrong with this agent — exiting so the "
                f"container's restart policy retries.")
        if resp.status_code != 200:
            raise AgentFatal(f"enrolment refused ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()
        identity = Identity(data["agent_id"], private_b64,
                            data["dashboard_public_key"], data["audience"])
        identity.save()
        log.info("enrolled as %s (%s)", data.get("name"), data["agent_id"])
        return identity

    def lease(self) -> Optional[dict]:
        # The lease body carries what this agent CURRENTLY is, not what it was when it
        # enrolled. agent_version was written once at enrolment, so an operator who
        # pulled a new image and restarted the container kept the old value forever —
        # and every compatibility decision the dashboard makes reads it. job_types is
        # the honest half: HANDLERS says what this build can run, policy.job_types says
        # what the customer allows, and only the intersection actually runs. The
        # dashboard can then say "granted, but this agent's policy refuses it" instead
        # of pretending a grant it cannot enforce.
        #
        # Free on the wire: _request already signs over the serialized body.
        resp = self._request("POST", "/api/agent/lease", {
            "agent_version": AGENT_VERSION,
            "job_types": sorted(set(HANDLERS) & POLICY.job_types),
        })
        if resp.status_code == 401:
            raise AgentFatal(
                "the dashboard rejected this agent's signature — it was probably "
                "revoked. Re-enrol with a fresh code.")
        if resp.status_code == 429:
            # Never conflated with the 401 above: one means stop for good, the other
            # means come back later, and treating a load spike as a revocation would
            # take a whole fleet down permanently.
            raise Throttled(_retry_after_seconds(resp, 30))
        if resp.status_code == 503:
            log.info("dashboard is not ready yet; backing off")
            return None
        resp.raise_for_status()
        data = resp.json()
        job = data.get("job")
        if not job:
            return None

        # Provenance, checked BEFORE the payload is looked at. Someone who can write a
        # job row into the dashboard's database still cannot make this agent act.
        if not self.identity.verify_dashboard(serialize(job), data.get("signature") or ""):
            raise PolicyRefusal(
                "job envelope signature did not verify — refusing to run it")
        if job.get("agent_id") != self.identity.agent_id:
            raise PolicyRefusal("job envelope is addressed to a different agent")
        if job.get("audience") != self.identity.audience:
            raise PolicyRefusal("job envelope carries the wrong audience")
        return job

    def heartbeat(self, job_id: str, pct: int, message: str) -> bool:
        """Returns True when the operator has asked for this job to stop."""
        resp = self._request("POST", f"/api/agent/jobs/{job_id}/heartbeat",
                             {"progress_pct": pct, "message": message})
        if resp.status_code != 200:
            return False
        return bool(resp.json().get("cancel_requested"))

    def logs(self, job_id: str, lines: list) -> None:
        if lines:
            self._request("POST", f"/api/agent/jobs/{job_id}/logs", {"lines": lines})

    def complete(self, job_id: str, *, status: str, result: Optional[dict] = None,
                 error: str = "") -> None:
        self._request("POST", f"/api/agent/jobs/{job_id}/complete",
                      {"status": status, "result": result or {}, "error": error})

    def job_secret(self, job_id: str, ref: str) -> str:
        """Fetch this job's hypervisor credential from the dashboard, sealed.

        A fresh X25519 keypair per call. The public half rides in the request body, which
        ``canonical_request`` already covers, so it is authenticated by this agent's
        Ed25519 identity with nothing new on the wire — and the private half exists only
        as a local for the length of this method, so there is no decryption key on this
        host for anyone to steal and use on captured traffic.

        The AAD is rebuilt from what this agent already knows — its own identity, and the
        job envelope it verified before running anything — never from the response. Reading
        any of it back out of the same reply that carried the ciphertext would make the
        binding meaningless.
        """
        private_b64, public_b64 = generate_reply_keypair()
        resp = self._request("POST", f"/api/agent/jobs/{job_id}/secret",
                             {"connection_ref": ref, "reply_key": public_b64})
        if resp.status_code != 200:
            detail = ""
            try:
                detail = str((resp.json() or {}).get("detail") or "")
            except Exception:  # noqa: BLE001
                detail = (resp.text or "")[:400]
            # Says which of the three files is NOT the problem. Every other refusal this
            # agent produces points at policy.yaml or connections.yaml, so without this the
            # operator goes and re-reads their own configuration first.
            raise PolicyRefusal(
                f"the dashboard refused to release the credential for {ref!r} "
                f"({resp.status_code}): {detail} — this is a dashboard-side authorization "
                f"or configuration refusal, not a policy.yaml or connections.yaml problem.")
        try:
            envelope = (resp.json() or {}).get("sealed") or {}
        except Exception as exc:  # noqa: BLE001
            raise PolicyRefusal(
                f"the dashboard's credential response for {ref!r} was not JSON") from exc
        try:
            secret = open_sealed(
                private_b64, envelope, agent_id=self.identity.agent_id,
                audience=self.identity.audience, job_id=job_id, ref=ref)
        except SealError as exc:
            raise PolicyRefusal(f"connection {ref!r}: {exc}") from exc
        self.hold_secret(secret)
        return secret

    def gateway_deploy_key(self, job_id: str) -> str:
        """Fetch this job's BeyondTrust Gateway deploy key from the dashboard, sealed.

        The same shape as :meth:`job_secret` — read that one first. The body carries only
        the ephemeral public key, because there is nothing here to select with: the
        dashboard derives the key from the job row, so a stolen agent identity cannot ask
        for another POV's Gateway.
        """
        private_b64, public_b64 = generate_reply_keypair()
        resp = self._request("POST", f"/api/agent/jobs/{job_id}/gateway-key",
                             {"reply_key": public_b64})
        if resp.status_code != 200:
            detail = ""
            try:
                detail = str((resp.json() or {}).get("detail") or "")
            except Exception:  # noqa: BLE001
                detail = (resp.text or "")[:400]
            raise PolicyRefusal(
                f"the dashboard refused to release this POV's Gateway deploy key "
                f"({resp.status_code}): {detail} — this is a dashboard-side refusal, not "
                f"a policy.yaml problem on this host.")
        try:
            envelope = (resp.json() or {}).get("sealed") or {}
        except Exception as exc:  # noqa: BLE001
            raise PolicyRefusal(
                "the dashboard's Gateway deploy key response was not JSON") from exc
        try:
            secret = open_sealed(
                private_b64, envelope, agent_id=self.identity.agent_id,
                audience=self.identity.audience, job_id=job_id,
                ref=GATEWAY_SEAL_REF)
        except SealError as exc:
            raise PolicyRefusal(f"the Gateway deploy key: {exc}") from exc
        self.hold_secret(secret)
        return secret

    def ansible_bundle(self, job_id: str, *, run_kind: str, transport: str,
                       host: str, port) -> tuple:
        """Fetch this job's Config-Management run bundle, sealed. Returns ``(bundle, scrub)``.

        The same shape as :meth:`job_secret` — a fresh X25519 keypair per fetch, the public
        half riding a body the Ed25519 signature already covers, the private half living only
        as a local so there is no decryption key on this host for captured traffic.

        The difference is what the AAD binds. :func:`bundle_ref` names the *endpoint*, and it
        is rebuilt from the job envelope this agent already verified — so the dashboard cannot
        hand back a bundle sealed for a different target, which for a payload carrying an SSH
        private key is the attack worth closing.

        Every credential in the bundle is registered for redaction here, before the caller has
        a chance to emit anything. The dashboard also sends a ``scrub`` list, but it is treated
        as an addition rather than the source of truth: a list is exactly the thing that goes
        stale when a field is added, and the agent knows which of its own fields are secret.
        """
        ref = bundle_ref(run_kind=run_kind, transport=transport, host=host, port=port)
        private_b64, public_b64 = generate_reply_keypair()
        resp = self._request("POST", f"/api/agent/jobs/{job_id}/ansible-bundle",
                             {"reply_key": public_b64})
        if resp.status_code != 200:
            detail = ""
            try:
                detail = str((resp.json() or {}).get("detail") or "")
            except Exception:  # noqa: BLE001
                detail = (resp.text or "")[:400]
            # Says which files are NOT the problem, for the same reason job_secret does:
            # every other refusal this agent produces points at policy.yaml, so without this
            # the operator re-reads their own configuration first.
            raise PolicyRefusal(
                f"the dashboard refused to release this run's playbook and credentials "
                f"({resp.status_code}): {detail} — this is a dashboard-side authorization or "
                f"configuration refusal, not a policy.yaml problem.")
        try:
            envelope = (resp.json() or {}).get("sealed") or {}
        except Exception as exc:  # noqa: BLE001
            raise PolicyRefusal("the dashboard's run-bundle response was not JSON") from exc
        try:
            plain = open_sealed(
                private_b64, envelope, agent_id=self.identity.agent_id,
                audience=self.identity.audience, job_id=job_id, ref=ref)
        except SealError as exc:
            raise PolicyRefusal(f"run bundle: {exc}") from exc
        try:
            payload = json.loads(plain)
            bundle = payload["bundle"]
            scrub = payload.get("scrub") or []
        except (ValueError, KeyError, TypeError) as exc:
            raise PolicyRefusal(
                "the dashboard's run bundle did not decode to the expected shape") from exc

        for value in scrub:
            self.hold_secret(str(value))
        for key in ("login_password", "become_password", "ssh_private_key"):
            if bundle.get(key):
                self.hold_secret(str(bundle[key]))
        # A PEM is multi-line and `redact` replaces within ONE line, so a whole-key hold
        # never matches anything in the log. Hold each interior line as well, above the
        # 4-char floor, or the key's body would pass through unredacted if a play echoed it.
        for line in str(bundle.get("ssh_private_key") or "").splitlines():
            if len(line.strip()) >= 8:
                self.hold_secret(line.strip())
        for value in (bundle.get("db") or {}).values():
            if isinstance(value, str) and len(value) >= 4:
                self.hold_secret(value)
        for value in (bundle.get("env") or {}).values():
            if isinstance(value, str) and len(value) >= 4:
                self.hold_secret(value)
        return bundle, scrub


# ── Probes — never authenticate ───────────────────────────────────────────────

def _connect(ip: str, port: int, timeout: float) -> Optional[socket.socket]:
    try:
        return socket.create_connection((ip, port), timeout=timeout)
    except OSError:
        return None


def _https_probe(ip: str, port: int, timeout: float,
                 path: str = "/") -> Optional[tuple]:
    """One TLS connect and one plain GET. Returns ``(raw_response, peer_cert_der)``.

    Verification is off and the TLS floor is pinned back to 1.2, for the same two
    reasons the Kubernetes probe had: the certificate is read as EVIDENCE about an
    unknown host, never trusted for anything, and dropping verification also drops the
    context's security level enough to negotiate TLS 1.0/1.1 — a probe should not be
    the one thing in the estate still willing to speak a deprecated protocol.

    Everything network-facing lives here, so :func:`_identify` stays pure and the whole
    identification table can be tested against captured bytes with no socket at all.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2

    sock = _connect(ip, port, timeout)
    if sock is None:
        return None
    try:
        sock.settimeout(timeout)
        with context.wrap_socket(sock, server_hostname=None) as tls:
            der = tls.getpeercert(binary_form=True)
            tls.sendall(f"GET {path} HTTP/1.1\r\nHost: ".encode() + ip.encode() +
                        b"\r\nAccept: */*\r\nConnection: close\r\n\r\n")
            chunks, total = [], 0
            while total < 65536:
                block = tls.recv(8192)
                if not block:
                    break
                chunks.append(block)
                total += len(block)
            return b"".join(chunks), der
    except (OSError, ssl.SSLError):
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _header(raw: str, name: str) -> str:
    """One HTTP response header value, case-insensitively, or ""."""
    needle = f"\n{name.lower()}:"
    lowered = raw.lower()
    idx = lowered.find(needle)
    if idx < 0:
        return ""
    line_end = raw.find("\r\n", idx + 1)
    if line_end < 0:
        line_end = len(raw)
    return raw[idx + len(needle):line_end].strip()


def _status(raw: str) -> str:
    return raw.split(" ")[1] if raw.startswith("HTTP/") and " " in raw else ""


def _group(pattern, raw: str) -> str:
    """First capture group, or "". Keeps _identify from searching twice per field."""
    match = pattern.search(raw)
    return match.group(1).strip() if match else ""


_VMWARE_FULLNAME = re.compile(r"<fullName>([^<]+)</fullName>")
_VMWARE_APITYPE = re.compile(r"<apiType>([^<]+)</apiType>")
_VMWARE_APIVERSION = re.compile(r"<apiVersion>([^<]+)</apiVersion>")
_VMWARE_BUILD = re.compile(r"<build>([^<]+)</build>")
_XAPI_SERVER = re.compile(r"xapi/?([0-9][0-9.]*)", re.I)
_PVE_SERVER = re.compile(r"pve-api-daemon/?([0-9][0-9.]*)?", re.I)


def _identify(raw_bytes: bytes, der: Optional[bytes], ip: str, port: int) -> Optional[dict]:
    """What answered, from a response and a certificate. Pure — no sockets.

    Returns None for anything not positively identified. That matters: Proxmox Backup
    Server (8007) and oVirt/RHV both answer on ports we probe, and mislabelling them
    would be worse than missing them.
    """
    raw = raw_bytes.decode("utf-8", "replace")
    server = _header(raw, "Server")
    cn = _cert_cn(der)
    issuer = _cert_issuer(der)
    base = {"kind": "hypervisor", "host": ip, "port": port,
            "endpoint": f"https://{ip}:{port}",
            "tls_cn": cn, "tls_issuer": issuer, "source": "probe"}

    # ── VMware: vCenter vs standalone ESXi, with a version. The best of the five. ──
    full = _group(_VMWARE_FULLNAME, raw)
    api_type = _group(_VMWARE_APITYPE, raw)
    if "vim25" in raw or "vimServiceVersions" in raw or full or api_type:
        # apiType separates them: "VirtualCenter" is vCenter, "HostAgent" a bare ESXi.
        # It matters for more than labelling — the agent transport needs vCenter's
        # Automation REST API, which ESXi does not serve at all. Fall back to the
        # product name when apiType is absent (the service-descriptor document has no
        # apiType, only the SOAP response does).
        if api_type:
            product = "esxi" if api_type == "HostAgent" else "vsphere"
        elif "esx" in full.lower() and "vcenter" not in full.lower():
            product = "esxi"
        else:
            product = "vsphere"
        return {**base, "product": product,
                "server_version": full or _group(_VMWARE_APIVERSION, raw),
                "build": _group(_VMWARE_BUILD, raw), "confidence": "confirmed",
                "suggested_name": f"{product}-{ip.replace('.', '-')}"}

    # ── XCP-ng / XenServer: the Server header carries the XAPI version. ──
    match = _XAPI_SERVER.search(server)
    if match or "xenserver" in cn.lower() or "xcp-ng" in raw.lower():
        return {**base, "product": "xcpng",
                "server_version": match.group(1) if match else "",
                "confidence": "confirmed",
                "suggested_name": f"xcpng-{ip.replace('.', '-')}"}

    # ── Proxmox VE: product yes, version no — /api2/json/version needs auth. ──
    if _PVE_SERVER.search(server) or "proxmox virtual environment" in raw.lower():
        return {**base, "product": "proxmox", "server_version": "",
                "confidence": "confirmed",
                "suggested_name": f"pve-{ip.replace('.', '-')}"}

    # ── Nutanix Prism: a 401 from the v3 API, plus the cert. No AOS version is
    #    available anonymously, so we do not invent one. ──
    if port == 9440 and (_status(raw) == "401" or "nutanix" in raw.lower()
                         or "nutanix" in cn.lower() or "nutanix" in issuer.lower()):
        return {**base, "product": "nutanix", "server_version": "",
                "confidence": "confirmed",
                "suggested_name": f"prism-{ip.replace('.', '-')}"}

    # ── WinRM. This identifies WinRM ON WINDOWS, not Hyper-V: nearly every
    #    domain-joined Windows Server has WinRM enabled and the overwhelming majority
    #    are not hypervisors. Reported as `possible` and labelled as such in the UI.
    #
    #    There IS a way to get the OS build, NetBIOS name and DNS domain anonymously —
    #    send an NTLM type-1 negotiate token (which carries no credential) and read the
    #    AV pairs out of the type-2 challenge. We deliberately do NOT: it initiates an
    #    authentication exchange and lands in the Windows Security log as a logon
    #    event, and "probes never authenticate, not once" is the sentence this whole
    #    feature is sold on. test_agent_probes pins that decision. ──
    if "microsoft-httpapi" in server.lower() and _status(raw) in ("401", "404", "405"):
        return {**base, "product": "winrm", "server_version": "",
                "confidence": "possible",
                "suggested_name": f"hyperv-{ip.replace('.', '-')}"}

    return None


def probe_hypervisor(ip: str, port: int, timeout: float) -> Optional[dict]:
    """Identify a hypervisor management endpoint. Never authenticates.

    Port 443 is shared by vSphere and XCP-ng, so this asks the ONE question that can
    tell them apart rather than running a probe per product: fetch the VMware service
    descriptor, and fall back to the root document, classifying whichever answered.
    """
    if port == 443:
        got = _https_probe(ip, port, timeout, "/sdk/vimServiceVersions.xml")
        if got:
            found = _identify(got[0], got[1], ip, port)
            if found:
                return found
    path = "/api/nutanix/v3/clusters/list" if port == 9440 else (
        "/wsman" if port in (5985, 5986) else "/")
    got = _https_probe(ip, port, timeout, path)
    if not got:
        return None
    return _identify(got[0], got[1], ip, port)


def _cert_cn(der: Optional[bytes]) -> str:
    return _cert_name(der, "subject")


def _cert_issuer(der: Optional[bytes]) -> str:
    return _cert_name(der, "issuer")


def _cert_name(der: Optional[bytes], which: str) -> str:
    if not der:
        return ""
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        cert = x509.load_der_x509_certificate(der)
        source = cert.subject if which == "subject" else cert.issuer
        attrs = source.get_attributes_for_oid(NameOID.COMMON_NAME)
        if not attrs:
            attrs = source.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
        return attrs[0].value if attrs else ""
    except Exception:  # noqa: BLE001 — a cert we cannot parse is not an error
        return ""


# ── Discovery ─────────────────────────────────────────────────────────────────

def _expand(payload: dict, policy: Policy, emit) -> list:
    """Resolve the requested scope into concrete IPs, intersected with policy.

    Intersected, not merely validated: a request naming a network the policy does not
    cover yields the empty set for that network and a logged refusal, rather than
    failing the whole job. The operator sees exactly what was skipped and why.
    """
    max_hosts = min(int(payload.get("max_hosts") or 1024),
                    policy.limit("max_hosts", 4096))
    targets = []

    requested = list(payload.get("cidrs") or [])
    if not requested and not payload.get("hostnames"):
        # No scope given: use what the policy allows, which is the only safe default.
        requested = [str(net) for net in policy.allowed_networks()]
        emit(f"No networks specified — using the {len(requested)} range(s) from policy.yaml")

    for entry in requested:
        try:
            network = ipaddress.ip_network(str(entry), strict=False)
        except ValueError:
            emit(f"REFUSED {entry}: not a valid CIDR")
            continue
        hosts = list(network.hosts()) if network.num_addresses > 2 else list(network)
        if len(hosts) > max_hosts:
            emit(f"{network} has {len(hosts)} hosts; scanning the first {max_hosts}")
            hosts = hosts[:max_hosts]
        targets.extend(str(h) for h in hosts)

    for name in payload.get("hostnames") or []:
        resolved = _resolve_all(str(name))
        if not resolved:
            emit(f"SKIPPED {name}: does not resolve")
            continue
        targets.extend(resolved)

    return targets[:max_hosts]


def run_discovery(payload: dict, policy: Policy, emit, cancelled, job_id: str = "",
                  dashboard=None) -> dict:
    """Sweep the requested scope with unauthenticated probes and return findings."""
    scan_kind = payload.get("scan_kind") or "all"
    timeout = float(payload.get("timeout_s") or 3)
    ports = payload.get("ports") or {}
    workers = max(1, min(int(payload.get("concurrency") or 32),
                         policy.limit("max_concurrency", 128)))

    families = [scan_kind] if scan_kind in _PORT_DEFAULTS else list(_PORT_DEFAULTS)
    wanted = []
    for family in families:
        wanted += list(ports.get(family) or _PORT_DEFAULTS[family])

    hosts = _expand(payload, policy, emit)
    # De-duplicate (host, port): vSphere and XCP-ng share 443, and probing it twice
    # would double the work and produce two findings for one endpoint.
    checks = []
    seen = set()
    for host in hosts:
        for port in wanted:
            if (host, port) not in seen:
                seen.add((host, port))
                checks.append((host, port))

    allowed, refused = [], 0
    for host, port in checks:
        if policy.allows(host, port):
            allowed.append((host, port))
        else:
            refused += 1
    if refused:
        emit(f"Policy refused {refused} of {len(checks)} host:port combinations")
    emit(f"Probing {len(allowed)} endpoint(s) across {len(hosts)} host(s) "
         f"with {workers} workers")

    def _one(item):
        host, port = item
        if cancelled():
            return None
        if MODE == "audit":
            log.info("AUDIT would probe %s:%s", host, port)
            return None
        return probe_hypervisor(host, port, timeout)

    findings = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for found in pool.map(_one, allowed):
            done += 1
            if done % 64 == 0:
                emit(f"…{done}/{len(allowed)} probed")
            if found and len(findings) < _MAX_FINDINGS:
                emit(f"FOUND {found['product']} at {found['host']}:{found['port']} "
                     f"{found.get('server_version') or ''} "
                     f"({found.get('confidence')})".replace("  ", " "))
                findings.append(found)

    return {"findings": findings, "scanned": len(allowed),
            "hosts": len(hosts), "refused": refused,
            "truncated": len(findings) >= _MAX_FINDINGS}


# ── Hypervisor connections: the customer's file, not the dashboard's ──────────
#
# The dashboard never holds these credentials. A job names a connection by the string
# it has HERE, and the agent resolves it locally — so a fully compromised dashboard can
# ask for a granted verb on a name the customer already wrote down, and nothing more.
#
# The secret is pluggable on purpose (`password`, `password_file`, and later a Password
# Safe managed-account id), so agent-side just-in-time checkout becomes a new branch in
# _secret_for rather than a redesign.

CONNECTIONS_FILE = os.environ.get("AGENT_CONNECTIONS_FILE",
                                  "/etc/dashboard-agent/connections.yaml")


class HypervisorConnections:
    """Parsed connections.yaml, keyed by name."""

    def __init__(self, by_name: dict):
        self.by_name = by_name

    @classmethod
    def load(cls, path: str = "") -> "HypervisorConnections":
        path = path or CONNECTIONS_FILE
        if not os.path.exists(path):
            # Absent is fine — an agent that only does discovery has no reason to have
            # one. A hypervisor job then fails with "unknown connection", which names
            # the missing thing rather than the missing file.
            return cls({})
        try:
            with open(path, "rb") as fh:
                doc = yaml.safe_load(fh.read()) or {}
        except OSError as exc:
            raise AgentFatal(
                f"Cannot read the connections file at {path}: {exc}. Mount it :ro,Z — "
                f"see docs/remote-agents.md.")
        except yaml.YAMLError as exc:
            raise AgentFatal(f"connections.yaml is not valid YAML: {exc}")
        if not isinstance(doc, dict):
            raise AgentFatal("connections.yaml must be a mapping at the top level.")

        by_name = {}
        for entry in doc.get("connections") or []:
            if isinstance(entry, dict) and entry.get("name"):
                by_name[str(entry["name"])] = entry
        return cls(by_name)

    def names(self) -> list:
        return sorted(self.by_name)

    def get(self, name: str) -> dict:
        conn = self.by_name.get(name)
        if conn is None:
            known = ", ".join(self.names()) or "none"
            raise PolicyRefusal(
                f"unknown connection {name!r} — this agent's connections.yaml defines: "
                f"{known}. The dashboard holds no credential for it; the name is the "
                f"whole join between the two.")
        return conn


# ── Password Safe just-in-time checkout ───────────────────────────────────────
#
# The end state this design was always heading for. With `ps_managed_account:` in
# connections.yaml, the agent holds NO hypervisor credential at all — only a Password
# Safe OAuth client, whose single power is to ask Password Safe for one. Every checkout
# is then subject to Password Safe's own policy, approval workflow and session
# recording, and the credential exists on this host for the duration of one job.
#
# The OAuth client is mounted by the customer, exactly like the enrolment code and the
# policy file. The dashboard never issues it and never sees it — if it did, the
# dashboard would once again be a place from which every hypervisor could be reached,
# which is the thing all of this exists to prevent.

PS_CLIENT_FILE = os.environ.get("AGENT_PS_CLIENT_FILE",
                                "/etc/dashboard-agent/passwordsafe.yaml")


class PasswordSafe:
    """Minimal Password Safe REST client — enough to check one credential out and back.

    Mirrors services/ps_api_service's flow (OAuth client-credentials -> SignAppIn ->
    request -> credential -> check-in) rather than importing it: this runs in the agent
    image, which has `requests` and nothing else. ps-cli is not available here and
    neither is httpx.
    """

    def __init__(self, api_url: str, client_id: str, client_secret: str,
                 verify: bool = True):
        self.base = api_url.rstrip("/")
        if "/beyondtrust/api/public/" not in self.base.lower():
            self.base = f"{self.base}/BeyondTrust/api/public/v3"
        self._id = client_id
        self._secret = client_secret
        self._verify = verify
        self._session = requests.Session()

    @classmethod
    def from_file(cls, path: str = "") -> "PasswordSafe":
        path = path or PS_CLIENT_FILE
        try:
            with open(path, "rb") as fh:
                doc = yaml.safe_load(fh.read()) or {}
        except OSError as exc:
            raise PolicyRefusal(
                f"a connection asks for a Password Safe checkout but {path} cannot be "
                f"read: {exc}. Mount it :ro,Z — see docs/remote-agents.md.")
        except yaml.YAMLError as exc:
            raise PolicyRefusal(f"{path} is not valid YAML: {exc}")
        if not isinstance(doc, dict) or not doc.get("api_url"):
            raise PolicyRefusal(f"{path} needs api_url, client_id and client_secret")

        # Same three-way precedence as a connection's credential, in the same order and for
        # the same reasons: sealed beats a leftover literal, and a sealed value found in a
        # plaintext field is refused rather than sent. This file is the remaining plaintext
        # credential on a host that has otherwise moved to `ps_managed_account` — the gap
        # docs/remote-agents.md names — so it gets the same treatment, or the strongest
        # local configuration still ships a secret in the clear.
        #
        # Sealed is checked FIRST rather than last, so an unreadable leftover
        # `client_secret_file` underneath it cannot raise before the value actually in use
        # is even looked at.
        api_url = str(doc["api_url"])
        secret = ""
        if doc.get("client_secret_sealed"):
            for stale in ("client_secret", "client_secret_file"):
                if doc.get(stale):
                    log.warning(
                        "%s takes its client secret from 'client_secret_sealed'; the %r "
                        "left in it is IGNORED and is still plaintext on this host — "
                        "delete it", path, stale)
            try:
                secret = local_unseal(str(doc["client_secret_sealed"]),
                                      host=ps_seal_host(api_url),
                                      purpose=LOCAL_PURPOSE_PS_CLIENT,
                                      what=f"'client_secret_sealed' in {path}")
            except LocalSealError as exc:
                raise PolicyRefusal(str(exc))
        elif doc.get("client_secret_file"):
            try:
                secret = _read_secret_file(doc["client_secret_file"])
            except ValueError:
                raise PolicyRefusal(_secret_file_undecodable(
                    "client_secret_file", str(doc["client_secret_file"])))
            except OSError as exc:
                raise PolicyRefusal(f"cannot read client_secret_file: {exc}")
            _refuse_sealed_in_the_wrong_place(
                secret,
                what=f"the contents of client_secret_file {doc['client_secret_file']}",
                belongs_in="client_secret_sealed")
        else:
            secret = str(doc.get("client_secret") or "")
            _refuse_sealed_in_the_wrong_place(
                secret, what=f"'client_secret' in {path}",
                belongs_in="client_secret_sealed")
        return cls(api_url, str(doc.get("client_id") or ""), secret,
                   verify=bool(doc.get("verify_ssl", True)))

    def _call(self, method: str, path: str, **kw):
        try:
            resp = self._session.request(
                method, f"{self.base}/{path}", timeout=30, verify=self._verify, **kw)
        except requests.RequestException as exc:
            raise PolicyRefusal(f"Password Safe unreachable: {exc}")
        return resp

    def sign_in(self) -> None:
        resp = self._call("POST", "Auth/Connect/Token", data={
            "grant_type": "client_credentials",
            "client_id": self._id, "client_secret": self._secret})
        if resp.status_code != 200:
            raise PolicyRefusal(
                f"Password Safe token request failed ({resp.status_code})")
        token = (resp.json() or {}).get("access_token") or ""
        if not token:
            raise PolicyRefusal("Password Safe returned no access token")
        self._session.headers["Authorization"] = f"Bearer {token}"
        sign = self._call("POST", "Auth/SignAppIn")
        if sign.status_code not in (200, 201):
            raise PolicyRefusal(f"Password Safe SignAppIn failed ({sign.status_code})")

    def sign_out(self) -> None:
        try:
            self._call("POST", "Auth/Signout")
        except Exception:  # noqa: BLE001 — best effort; the session expires anyway
            pass

    def _system_id(self, account_id: int) -> int:
        """The managed system that owns the account, or 0 when it cannot be read.

        Best-effort: this only fills in a request field, so a failed read must not turn
        a checkout that would have worked into a refusal."""
        try:
            resp = self._call("GET", f"ManagedAccounts/{int(account_id)}")
            body = resp.json() if resp.status_code == 200 else None
            if not isinstance(body, dict):
                # Some builds serve only the request-scoped collection, which is also
                # the one that lists what this identity may actually request.
                resp = self._call("GET", "ManagedAccounts")
                rows = resp.json() if resp.status_code == 200 else []
                rows = rows.get("Data") if isinstance(rows, dict) else rows
                body = next(
                    (r for r in (rows or []) if isinstance(r, dict) and str(
                        r.get("ManagedAccountID") or r.get("AccountId")
                        or r.get("AccountID") or "") == str(int(account_id))), None)
            if isinstance(body, dict):
                return int(body.get("ManagedSystemID") or body.get("SystemId")
                           or body.get("SystemID") or 0)
        except Exception:  # noqa: BLE001
            log.debug("could not resolve the managed system for a Password Safe account")
        return 0

    def checkout(self, account_id: int, duration_min: int = 30) -> tuple:
        """(request_id, credential).

        `SystemID` is a REQUIRED field and Password Safe authorises the *pair* — the
        account has to be requestable on that system. Sending a hard-coded 0, which no
        managed system owns, 403s with 4031: the same code an ungranted Requestor role
        returns, so the grant looks like the cause and re-granting it changes nothing.
        Kept in step with `services/ps_api_service._checkout`.

        `ConflictOption=reuse` returns an existing active request instead of a 409 — the
        same reason btapi_service passes `-c-op reuse`, where a 409 body was once
        mis-parsed as a request id."""
        system_id = self._system_id(account_id)
        resp = self._call("POST", "Requests", json={
            "AccessType": "View", "SystemID": system_id, "AccountID": int(account_id),
            "DurationMinutes": int(duration_min), "Reason": "vm-dashboard agent",
            "ConflictOption": "reuse"})
        if resp.status_code not in (200, 201):
            raise PolicyRefusal(
                f"Password Safe refused the credential request for account "
                f"{int(account_id)} on managed system "
                f"{system_id or 'UNRESOLVED (the account could not be read)'} "
                f"({resp.status_code}): {resp.text[:200]} — the code in that body says "
                f"which cause it is. 4031: the API identity needs the Requestor role and "
                f"an access policy granting View on a Smart Rule containing this account, "
                f"OR the account is not API-enabled, OR it is not requestable on that "
                f"system. 4034: awaiting approval. 4035: the concurrent-request cap.")
        body = resp.json()
        request_id = body if isinstance(body, int) else (
            body.get("RequestID") or body.get("id"))
        if not request_id:
            raise PolicyRefusal("Password Safe returned no request id")

        got = self._call("GET", f"Credentials/{int(request_id)}")
        if got.status_code != 200:
            raise PolicyRefusal(
                f"Password Safe would not release the credential ({got.status_code}) — "
                f"the request may be awaiting approval.")
        credential = got.json()
        if isinstance(credential, dict):
            credential = credential.get("Credentials") or credential.get("Password") or ""
        return int(request_id), str(credential)

    def checkin(self, request_id: int) -> None:
        """Best effort, and deliberately so: the credential is already used, the request
        expires on its own duration, and failing a completed job because the check-in
        was refused would be the wrong trade."""
        try:
            self._call("PUT", f"Requests/{int(request_id)}/Checkin",
                       json={"Reason": "vm-dashboard agent finished"})
        except Exception:  # noqa: BLE001 — never log the request id (CodeQL taints it)
            # The id is reachable from the same per-job object that holds a fetched
            # credential (see JobSecrets), so taint analysis treats it as secret-derived
            # and a `%s` here is a clear-text-logging error. It is no loss: the id is
            # worth nothing diagnostically, and the dashboard-side twin
            # (ps_api_service._checkin) dropped it for exactly this reason.
            log.debug("a Password Safe credential check-in was refused")


class JobSecrets(list):
    """The per-job credential context, carried by the `checkins` parameter.

    A ``list`` subclass, which is a liberty taken on purpose. ``_secret_for`` needs a job
    id and a dashboard client, and thirteen call sites across the per-kind sync, power and
    snapshot functions already thread ``checkins`` down to it. Subclassing means
    ``checkins.append(request_id)``, ``_checkin_all(checkins)`` and every one of those
    thirteen signatures stay byte-for-byte unchanged — and, more to the point, so do the
    fourteen tests in ``tests/test_agent_ps_checkout.py`` that pin the precedence chain.
    Rewriting the call shape of the file that *proves* the ordering, in the change that
    adds to the ordering, is the diff you least want to review.

    The alternative, if this ever grates: rename the last parameter to ``ctx`` at all
    thirteen sites and take the test churn.

    One consequence of holding both the credential and the Password Safe request ids on
    one object: taint analysis treats anything read out of it as secret-derived, so a
    request id must not be logged. ``PasswordSafe.checkin`` says the same at the point it
    matters. That is a fair price — a request id has no diagnostic value — but it is a
    real constraint rather than a style preference, so it is written down here too.
    """

    def __init__(self, *, job_id: str = "", dashboard=None, ref: str = ""):
        super().__init__()
        self.job_id = job_id
        self.dashboard = dashboard
        self.ref = ref
        self._fetched: Optional[str] = None

    def dashboard_secret(self, conn: dict) -> str:
        """The credential the dashboard holds for this connection, fetched once per job.

        Memoised because a Workstation job reads the credential on *every* HTTP call —
        `_vmrest` calls `_secret_for` each time — and for a `ps_account://` connection each
        of those would otherwise be another Password Safe request to open and close.
        """
        if self._fetched is not None:
            return self._fetched
        if not self.dashboard or not self.job_id:
            raise PolicyRefusal(
                f"connection {conn.get('name')!r} declares 'dashboard_secret: true', but "
                f"this build cannot fetch a dashboard-held credential outside a leased "
                f"job. Remove the key and configure a local credential, or upgrade the "
                f"agent image.")
        self._fetched = self.dashboard.job_secret(self.job_id, self.ref or
                                                  str(conn.get("name") or ""))
        return self._fetched


def _secret_for(conn: dict, checkins: list = None) -> str:
    """The connection's password, by whichever of the five means it declares.

    Precedence is deliberate: a *remote* source first, because an operator who has moved a
    connection off local storage should not silently keep authenticating with a stale
    literal left in the file underneath it. Then `password_sealed`, for the same reason one
    step down — someone who sealed a value has finished with the plaintext. Then
    `password_file` (so a Docker/Podman secret can be mounted), then an inline `password`.

    ``ps_managed_account`` and ``dashboard_secret`` are the two remote sources and they are
    **mutually exclusive** rather than ordered. They are different authorities — one has
    this agent ask Password Safe directly, the other has the dashboard do it — and there is
    no stale-leftover story that makes silently preferring one over the other kind. Picking
    quietly would leave nobody able to say which credential a job actually used.

    ``password_sealed`` is not a fourth authority; it is the same local credential, not
    written down in the clear. So it is *ordered* against the plaintext forms rather than
    exclusive with them, and a leftover is warned about exactly as it is above.

    When `checkins` is provided, a just-in-time checkout appends its request id so the
    caller can check the credential back in when the job finishes. Without it the
    request simply expires on its own duration — which is safe, just untidy.
    """
    account = conn.get("ps_managed_account")
    sealed = conn.get("password_sealed")
    if conn.get("dashboard_secret"):
        if account:
            raise PolicyRefusal(
                f"connection {conn.get('name')!r} declares both 'ps_managed_account' and "
                f"'dashboard_secret: true'. Those are two different authorities and this "
                f"agent will not choose between them — remove one from connections.yaml "
                f"and restart the agent.")
        for stale in ("password", "password_sealed", "password_file"):
            if conn.get(stale):
                # Warned, not refused, so this matches the rule already in force for
                # `ps_managed_account` over a leftover literal — and warned rather than
                # silent because the operator has left a credential on this host believing
                # they had moved it to the dashboard. A sealed leftover is named too: it is
                # better than plaintext, but it is still a credential here.
                log.warning(
                    "connection %r takes its credential from the dashboard; the %r left "
                    "in connections.yaml is IGNORED and should be deleted",
                    conn.get("name"), stale)
        if not isinstance(checkins, JobSecrets):
            raise PolicyRefusal(
                f"connection {conn.get('name')!r} declares 'dashboard_secret: true', but "
                f"this code path has no job context to fetch it with. This is an agent "
                f"bug — please report it.")
        return _held(checkins, checkins.dashboard_secret(conn))
    if account:
        ps = PasswordSafe.from_file()
        ps.sign_in()
        try:
            request_id, credential = ps.checkout(
                int(account), int(conn.get("ps_duration_minutes") or 30))
        finally:
            ps.sign_out()
        if checkins is not None:
            checkins.append(request_id)
        if not credential:
            raise PolicyRefusal(
                f"connection {conn.get('name')!r}: Password Safe released an empty "
                f"credential for account {account}")
        return _held(checkins, credential)

    if sealed:
        for stale in ("password", "password_file"):
            if conn.get(stale):
                log.warning(
                    "connection %r takes its credential from 'password_sealed'; the %r "
                    "left in connections.yaml is IGNORED and is still plaintext on this "
                    "host — delete it", conn.get("name"), stale)
        try:
            # The connection name goes on the outside only. `what` naming it too produced
            # "connection 'dc1': this 'password_sealed' for connection 'dc1' …".
            return _held(checkins, local_unseal(
                str(sealed), host=str(conn.get("host") or ""),
                purpose=LOCAL_PURPOSE_PASSWORD, what="'password_sealed'"))
        except LocalSealError as exc:
            raise PolicyRefusal(f"connection {conn.get('name')!r}: {exc}")

    path = conn.get("password_file")
    if path:
        try:
            secret = _read_secret_file(path)
        except ValueError:
            raise PolicyRefusal(
                f"connection {conn.get('name')!r}: "
                + _secret_file_undecodable("password_file", str(path)))
        except OSError as exc:
            raise PolicyRefusal(
                f"connection {conn.get('name')!r}: cannot read password_file {path}: {exc}")
        _refuse_sealed_in_the_wrong_place(
            secret, what=f"the contents of password_file {path}",
            belongs_in="password_sealed", name=str(conn.get("name") or ""))
        return _held(checkins, secret)

    literal = str(conn.get("password") or "")
    _refuse_sealed_in_the_wrong_place(
        literal, what="'password'", belongs_in="password_sealed",
        name=str(conn.get("name") or ""))
    if not literal:
        # Never an empty string. This used to fall through to `""`, which the hypervisor
        # answers as "wrong username or password" — so a connection that simply declares no
        # credential looks like a bad one, and on the inventory sync schedule it retries
        # until the service account locks out. It is also the shape an agent too old to
        # know `password_sealed` would produce against a file that only declares it, which
        # is what stands in for a dashboard-side version gate that cannot exist here: the
        # dashboard never sees the sealed value, so it has nothing to gate on.
        raise PolicyRefusal(
            f"connection {conn.get('name')!r} declares no credential — none of "
            f"'dashboard_secret', 'ps_managed_account', 'password_sealed', "
            f"'password_file' or 'password' is set on it in connections.yaml. Refusing "
            f"rather than sending an empty password, which the endpoint would report as a "
            f"wrong one and which repeats on every sync until the account locks out. If "
            f"this agent image predates 'password_sealed' (2.2.0), that is the likely "
            f"cause: pull a newer chrweav/dashboard-agent and restart.")
    return _held(checkins, literal)


def _held(checkins, secret: str) -> str:
    """Register a credential for outbound redaction, then hand it back.

    Every source goes through this, not just the dashboard fetch. ``hold_secret`` used to
    have exactly one caller — ``Dashboard.job_secret`` — so a credential from a local file
    or a Password Safe checkout was *not* scrubbed, and a hypervisor that echoes it into an
    error string could carry it into a job log the dashboard stores and renders. Sealing a
    value and then leaking it that way would defeat the point.

    Guarded because ``checkins`` is legitimately ``None`` or a bare ``list`` at several call
    sites — and in tests — and because ``hold_secret`` lives on the dashboard client, which
    is the thing that owns the outbound direction.
    """
    if isinstance(checkins, JobSecrets) and checkins.dashboard:
        checkins.dashboard.hold_secret(secret)
    return secret


def _refuse_sealed_in_the_wrong_place(value, *, what: str, belongs_in: str,
                                      name: str = "") -> None:
    """Refuse a sealed value found in a plaintext field, rather than using it as a literal.

    The one mistake the paste-it-in workflow invites. Sending the ciphertext as a password
    would be answered by the endpoint as a wrong password — the misleading failure this
    whole file works to avoid — and quietly opening it instead would create a second
    undocumented way to configure the same thing, with none of the ordering or leftover
    warnings that the declared key gets.
    """
    if not looks_locally_sealed(value):
        return
    where = f"connection {name!r}: " if name else ""
    raise PolicyRefusal(
        f"{where}{what} is a sealed value, and this is not the field for one. Move it to "
        f"{belongs_in!r} — sent as a literal it would be rejected by the endpoint as a "
        f"wrong password, which reads as the wrong problem entirely.")


def _checkin_all(request_ids: list) -> None:
    """Return every credential this job checked out. Best effort by design."""
    if not request_ids:
        return
    try:
        ps = PasswordSafe.from_file()
        ps.sign_in()
        try:
            for request_id in request_ids:
                ps.checkin(request_id)
        finally:
            ps.sign_out()
    except Exception:  # noqa: BLE001 — a failed check-in must not fail a finished job
        log.warning("could not check %d Password Safe credential(s) back in",
                    len(request_ids))


def _conn_endpoint(conn: dict, default_port: int) -> tuple:
    host = str(conn.get("host") or "")
    if not host:
        raise PolicyRefusal(f"connection {conn.get('name')!r} has no host")
    try:
        port = int(conn.get("port") or default_port)
    except (TypeError, ValueError):
        port = default_port
    return host, port


def _hv_request(conn: dict, method: str, url: str, *, headers=None, body=None,
                timeout: float = 60.0):
    """One HTTPS call to a hypervisor management API, using only the stdlib.

    No new dependency: Prism v3, Proxmox's /api2/json and the vSphere Automation REST
    API are all plain JSON over HTTPS, and XCP-ng's XAPI is stdlib xmlrpc. Fattening
    the agent image with pyVmomi/proxmoxer would trade the two image-audit tests — which
    ARE the security argument, in executable form — for a capability REST already gives.

    Verification follows the connection's own `verify_ssl`, unlike the discovery probe:
    this one carries a credential, so trusting an unverified certificate is a real
    interception risk rather than an identification detail.
    """
    parsed = urlparse(url)
    context = ssl.create_default_context()
    if not conn.get("verify_ssl"):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2

    payload = json.dumps(body).encode() if body is not None else None
    request = _urlrequest.Request(url, data=payload, method=method)
    request.add_header("Accept", "application/json")
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with _urlrequest.urlopen(request, timeout=timeout, context=context) as resp:
            raw = resp.read()
    except _urlerror.HTTPError as exc:
        # The code is carried on the exception as well as in the message: a caller that
        # can explain one particular status (see _power_vsphere and VMware Tools) should
        # not have to parse the sentence back apart to find out which one it got.
        refusal = PolicyRefusal(
            f"{parsed.hostname} answered {exc.code} for {method} {parsed.path}")
        refusal.status = exc.code
        raise refusal from exc
    except (OSError, ssl.SSLError) as exc:
        raise PolicyRefusal(f"could not reach {parsed.hostname}: {exc}")
    try:
        return json.loads(raw.decode("utf-8", "replace")) if raw else {}
    except ValueError:
        return {}


def _check_endpoint(policy: "Policy", host: str, port: int,
                    connection_ref: str = "") -> None:
    """The policy gate, on the RESOLVED address — same rule as a discovery probe.

    Matching the resolved IP rather than the name is what stops DNS rebinding: a name
    that resolves inside the allowed range when checked and elsewhere when connected.

    One exception, and only here: a connection whose policy entry sets `allow_loopback`
    may reach a loopback address. `Policy.check` — which every discovery probe goes
    through — is untouched and still refuses loopback unconditionally. The port needs no
    separate allow-listing because it comes from the operator's own connections.yaml,
    not from anything the dashboard sent.
    """
    addresses = _resolve_all(host)
    if not addresses:
        raise PolicyRefusal(f"{host} does not resolve")
    loopback_ok = policy.allows_loopback(connection_ref)
    for addr in addresses:
        try:
            if loopback_ok and ipaddress.ip_address(addr).is_loopback:
                continue
        except ValueError:
            pass
        policy.check(addr, port)


# ── Inventory, and the `enumerated` flag every one of these sets ──────────────
#
# `enumerated: True` says one thing: this list is what the product's API returned, not
# what was left over after something went wrong. The dashboard needs it because an empty
# `vms` is otherwise unreadable — a zero-VM pass prunes the whole cached inventory for
# the connection, which is how a DELETED VM stops being listed, so a host the agent
# could not read looked exactly like a host with nothing on it and emptied a good cache
# with the sync recorded as a success.
#
# Every function below can set it honestly because every one of them parses a structured
# document and raises when it cannot: a list from vCenter, `data` from Proxmox,
# `entities` from Prism, `get_all_records` from XAPI, a JSON array from vmrest. It is
# NOT a claim that the credential could see everything — nothing here can know that —
# only that an enumeration came back rather than nothing at all.
#
# Absent, it reads as False on the dashboard, which then refuses to empty a populated
# cache. So a new `_sync_*` that forgets it costs an operator stale rows and a stated
# reason, never a silently wiped page — and
# tests/test_hyperv_inventory_envelope.py sweeps every producer here for it.

def _sync_vsphere(conn, payload, policy, emit, checkins=None):
    """vCenter inventory via the vSphere Automation REST API (7.0U2+).

    vCenter, not bare ESXi: ESXi serves only the SOAP API, so an ESXi connection has to
    stay dashboard-direct. The connection form says so.
    """
    host, port = _conn_endpoint(conn, 443)
    _check_endpoint(policy, host, port)
    base = f"https://{host}:{port}"
    user = str(conn.get("username") or "")
    basic = base64.b64encode(f"{user}:{_secret_for(conn, checkins)}".encode()).decode()
    session = _hv_request(conn, "POST", f"{base}/api/session",
                          headers={"Authorization": f"Basic {basic}"})
    token = session if isinstance(session, str) else str(session.get("value") or session)
    emit(f"authenticated to vCenter at {host}")
    vms = _hv_request(conn, "GET", f"{base}/api/vcenter/vm",
                      headers={"vmware-api-session-id": token})
    rows = vms if isinstance(vms, list) else (vms.get("value") or [])
    # Guest details are opt-in per connection: /api/vcenter/vm lists everything in one call,
    # but the guest's ADDRESS needs a second call per VM, and a 500-VM vCenter should not pay
    # that on every 30-minute sync unless those guests are Config-Management targets.
    opts = conn.get("options") if isinstance(conn.get("options"), dict) else conn
    want_guest = bool(opts.get("sync_guest_details"))
    out = []
    for vm in rows:
        row = {"vm_id": vm.get("vm"), "name": vm.get("name"),
               "power_state": vm.get("power_state"),
               "vcpus": vm.get("cpu_count"), "mem_mib": vm.get("memory_size_MiB"),
               "vm_type": "vm"}
        # Only for a running VM: the guest endpoints answer 503 on a powered-off one, and
        # asking anyway would turn a normal inventory into one error per stopped VM.
        if want_guest and str(vm.get("power_state") or "").upper() == "POWERED_ON":
            ident = _vsphere_guest_identity(conn, base, token, vm.get("vm"))
            if ident.get("ip"):
                row["ip_addresses"] = [ident["ip"]]
            if ident.get("family"):
                row["guest_os"] = ident["family"][:64]
        out.append(row)
    return {"vms": out, "next_cursor": "", "complete": True, "scanned": len(out),
            "enumerated": True}


def _vsphere_guest_identity(conn, base: str, token: str, vm_id: str) -> dict:
    """``{ip, family}`` for one running VM, or ``{}``.

    Needs VMware Tools in the guest. Every failure is swallowed to ``{}`` deliberately: a
    VM without Tools, one still booting, and one whose Tools are wedged all answer
    differently, and none of them is a reason to fail the inventory of the whole vCenter —
    a sync that fails is a sync that cannot prune, and a sync that returns nothing empties
    the dashboard's cache. Absent keys leave the last known address in place.
    """
    try:
        ident = _hv_request(conn, "GET", f"{base}/api/vcenter/vm/{vm_id}/guest/identity",
                            headers={"vmware-api-session-id": token})
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(ident, dict):
        return {}
    ident = ident.get("value") if isinstance(ident.get("value"), dict) else ident
    return {"ip": str(ident.get("ip_address") or "").strip(),
            "family": str(ident.get("family") or "").strip()}


def _sync_proxmox(conn, payload, policy, emit, checkins=None):
    """Proxmox inventory via /api2/json/cluster/resources.

    API-token auth only here. A password login would mean posting credentials to
    /access/ticket and holding a CSRF token, which is more surface for no gain — the
    dashboard's own connection form already prefers a token.
    """
    host, port = _conn_endpoint(conn, 8006)
    _check_endpoint(policy, host, port)
    token_id = str(conn.get("token_id") or "")
    if not token_id:
        raise PolicyRefusal(
            f"connection {conn.get('name')!r} needs `token_id` — the agent uses Proxmox "
            f"API-token auth, not a password login.")
    user = str(conn.get("username") or "root@pam")
    header = {"Authorization": f"PVEAPIToken={user}!{token_id}={_secret_for(conn, checkins)}"}
    body = _hv_request(conn, "GET",
                       f"https://{host}:{port}/api2/json/cluster/resources?type=vm",
                       headers=header)
    # Opt-in per connection, as on the other kinds: cluster/resources lists everything in one
    # call, but a guest's address is one guest-agent call per VM.
    opts = conn.get("options") if isinstance(conn.get("options"), dict) else conn
    want_guest = bool(opts.get("sync_guest_details"))
    out = []
    for vm in body.get("data") or []:
        row = {"vm_id": str(vm.get("vmid")), "name": vm.get("name"),
               "power_state": vm.get("status"), "vcpus": vm.get("maxcpu"),
               "mem_mib": int((vm.get("maxmem") or 0) / (1024 * 1024)),
               "scope": vm.get("node"), "vm_type": vm.get("type")}
        # QEMU guests only, and only running ones: the guest agent is a qemu feature, so an
        # LXC container has no such endpoint and a stopped VM's answers 500.
        if (want_guest and vm.get("type") == "qemu"
                and str(vm.get("status") or "") == "running"):
            ips = _proxmox_guest_ips(conn, host, port, header,
                                     vm.get("node"), vm.get("vmid"))
            if ips:
                row["ip_addresses"] = ips
        out.append(row)
    emit(f"read {len(out)} guest(s) from Proxmox at {host}")
    return {"vms": out, "next_cursor": "", "complete": True, "scanned": len(out),
            "enumerated": True}


def _proxmox_guest_ips(conn, host: str, port: int, header: dict, node, vmid) -> list:
    """A running QEMU guest's non-loopback addresses, or ``[]``.

    Needs `qemu-guest-agent` in the guest and `agent: 1` on the VM. Without either, Proxmox
    answers 500 — swallowed to ``[]`` rather than raised, because that is the common case on
    a real cluster and it is not a reason to fail the whole inventory. Loopback is filtered
    because it is never how the agent would reach the guest, and an inventory whose first
    address is 127.0.0.1 would be refused by policy for a confusing reason.
    """
    try:
        body = _hv_request(
            conn, "GET",
            f"https://{host}:{port}/api2/json/nodes/{node}/qemu/{vmid}/agent/"
            f"network-get-interfaces", headers=header)
    except Exception:  # noqa: BLE001
        return []
    out = []
    data = (body or {}).get("data") or {}
    for iface in (data.get("result") if isinstance(data, dict) else data) or []:
        if not isinstance(iface, dict):
            continue
        for addr in iface.get("ip-addresses") or []:
            ip = str((addr or {}).get("ip-address") or "").strip()
            if not ip:
                continue
            try:
                parsed = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if parsed.is_loopback or parsed.is_link_local:
                continue
            if ip not in out:
                out.append(ip)
    return out[:16]          # matches the dashboard's own MAX_IPS cap


def _sync_nutanix(conn, payload, policy, emit, checkins=None):
    """Prism Central v3 inventory. Genuinely paged, so this is where the cursor earns
    its place: the offset rides back to the dashboard and returns on the next job."""
    host, port = _conn_endpoint(conn, 9440)
    _check_endpoint(policy, host, port)
    user = str(conn.get("username") or "admin")
    basic = base64.b64encode(f"{user}:{_secret_for(conn, checkins)}".encode()).decode()
    page = int(payload.get("page_size") or 250)
    try:
        offset = int(payload.get("cursor") or 0)
    except ValueError:
        offset = 0
    body = _hv_request(conn, "POST",
                       f"https://{host}:{port}/api/nutanix/v3/vms/list",
                       headers={"Authorization": f"Basic {basic}"},
                       body={"kind": "vm", "length": page, "offset": offset})
    entities = body.get("entities") or []
    total = ((body.get("metadata") or {}).get("total_matches")
             or (offset + len(entities)))
    out = []
    for vm in entities:
        status = vm.get("status") or {}
        resources = status.get("resources") or {}
        out.append({"vm_id": (vm.get("metadata") or {}).get("uuid"),
                    "name": status.get("name"),
                    "power_state": resources.get("power_state"),
                    "vcpus": resources.get("num_sockets"),
                    "mem_mib": resources.get("memory_size_mib"),
                    "scope": ((status.get("cluster_reference") or {}).get("uuid")),
                    "vm_type": "vm"})
    nxt = offset + len(entities)
    complete = nxt >= int(total or 0) or not entities
    emit(f"read {len(out)} VM(s) from Prism at {host} (offset {offset})")
    return {"vms": out, "next_cursor": "" if complete else str(nxt),
            "complete": complete, "scanned": len(out), "enumerated": True}


def _sync_xcpng(conn, payload, policy, emit, checkins=None):
    """XCP-ng inventory over XAPI. stdlib xmlrpc.client, same as the dashboard uses."""
    import xmlrpc.client

    host, port = _conn_endpoint(conn, 443)
    _check_endpoint(policy, host, port)
    context = ssl.create_default_context()
    if not conn.get("verify_ssl"):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    server = xmlrpc.client.ServerProxy(
        f"https://{host}:{port}/",
        transport=xmlrpc.client.SafeTransport(context=context))
    result = server.session.login_with_password(
        str(conn.get("username") or "root"), _secret_for(conn, checkins), "2.0", "dashboard-agent")
    if result.get("Status") != "Success":
        raise PolicyRefusal(f"XAPI login refused at {host}")
    session = result["Value"]
    try:
        records = server.VM.get_all_records(session).get("Value") or {}
    finally:
        try:
            server.session.logout(session)
        except Exception:  # noqa: BLE001
            pass
    out = []
    for _ref, vm in records.items():
        if vm.get("is_a_template") or vm.get("is_control_domain"):
            continue
        out.append({"vm_id": vm.get("uuid"), "name": vm.get("name_label"),
                    "power_state": vm.get("power_state"),
                    "vcpus": vm.get("VCPUs_max"),
                    "mem_mib": int(int(vm.get("memory_static_max") or 0) / (1024 * 1024)),
                    "vm_type": "vm"})
    emit(f"read {len(out)} VM(s) from XCP-ng at {host}")
    return {"vms": out, "next_cursor": "", "complete": True, "scanned": len(out),
            "enumerated": True}


# ── VMware Workstation Pro, via vmrest ────────────────────────────────────────
#
# Workstation was written off twice as unreachable, on the grounds that it is driven by
# `vmrun` against local VMX paths. That was true of vmrun and wrong overall: Workstation
# Pro ships `vmrest`, a REST daemon that is plain JSON over HTTP. So this needs no new
# dependency and no sibling container — it is an ordinary connection that happens to be
# on the same host as the agent.
#
# Three quirks will otherwise cost a debugging round:
#   * vmrest REJECTS `application/json`. Accept and Content-Type must both be its own
#     vendor type.
#   * the power endpoint takes a BARE STRING body ("on"), not a JSON object.
#   * GET /api/vms returns only `id` and `path` — no name. The display name is a
#     separate per-VM call.
#
# vmrest binds 127.0.0.1 by default, which the agent's mandatory deny would refuse, so a
# co-located connection needs `allow_loopback: true` in policy.yaml. See _check_endpoint.

_VMREST_MEDIA = "application/vnd.vmware.vmw.rest-v1+json"

# vmrest's power actions. `restart`/`power_reset`/`snapshot` are deliberately absent:
# vmrest has no reset, no reboot and no snapshot API, and mapping `restart` onto
# `shutdown` would quietly do something other than what the operator asked for.
_VMREST_POWER = {"power_on": "on", "power_off": "off"}


def _vmrest(conn: dict, method: str, path: str, *, body=None, checkins=None,
            timeout: float = 30.0):
    """One vmrest call. Returns the decoded JSON, or None when the call failed."""
    host, port = _conn_endpoint(conn, 8697)
    scheme = "https" if conn.get("verify_ssl") or conn.get("use_https") else "http"
    url = f"{scheme}://{host}:{port}/api{path}"
    user = str(conn.get("username") or "")
    basic = base64.b64encode(f"{user}:{_secret_for(conn, checkins)}".encode()).decode()
    headers = {"Authorization": f"Basic {basic}",
               "Accept": _VMREST_MEDIA, "Content-Type": _VMREST_MEDIA}

    # A bare string body, not JSON — vmrest's power endpoint wants `on`, not `"on"`.
    payload = body.encode() if isinstance(body, str) else (
        json.dumps(body).encode() if body is not None else None)
    request = _urlrequest.Request(url, data=payload, method=method)
    for key, value in headers.items():
        request.add_header(key, value)
    context = None
    if scheme == "https":
        context = ssl.create_default_context()
        if not conn.get("verify_ssl"):
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
    try:
        with _urlrequest.urlopen(request, timeout=timeout, context=context) as resp:
            raw = resp.read()
    except _urlerror.HTTPError as exc:
        if exc.code in (401, 403):
            raise PolicyRefusal(
                f"vmrest rejected the credential for {conn.get('name')!r} ({exc.code}). "
                f"Set it with `vmrest -C` and check the username in connections.yaml.")
        return None
    except (OSError, ssl.SSLError) as exc:
        raise PolicyRefusal(
            f"could not reach vmrest at {host}:{port}: {exc}. Is `vmrest` running, and "
            f"does policy.yaml allow this connection to reach it?")
    try:
        return json.loads(raw.decode("utf-8", "replace")) if raw else {}
    except ValueError:
        return None


def _vmrest_name(conn, vm_id, path, checkins):
    """displayName, falling back to the VMX filename.

    /api/vms gives only an id and a path, so a name costs one more call per VM. When
    that call fails the basename is a perfectly good name — far better than a blank
    cell, and it is what the file is actually called on disk.
    """
    got = _vmrest(conn, "GET", f"/vms/{vm_id}/params/displayName", checkins=checkins)
    name = (got or {}).get("value") if isinstance(got, dict) else ""
    if name:
        return str(name)
    base = str(path or "").replace("\\", "/").rsplit("/", 1)[-1]
    return base[:-4] if base.lower().endswith(".vmx") else base


def _vmrest_guest_os(conn, vm_id, checkins):
    """The VMX `guestOS` code (e.g. `windows9-64`), or "" when vmrest will not say.

    The RAW code, deliberately: the dashboard owns the label table
    (hypervisor_view_service.guest_os_label). An agent is upgraded separately and lags
    whatever the dashboard runs, so a label shipped from here would freeze at the build
    the host last pulled — and correcting a display string would then need an agent
    rebuild. Same reason every other value in a sync row is an id or an enum.
    """
    got = _vmrest(conn, "GET", f"/vms/{vm_id}/params/guestOS", checkins=checkins)
    value = (got or {}).get("value") if isinstance(got, dict) else ""
    return str(value or "")


def _sync_workstation(conn, payload, policy, emit, checkins=None):
    host, port = _conn_endpoint(conn, 8697)
    _check_endpoint(policy, host, port, str(payload.get("connection_ref") or ""))

    listing = _vmrest(conn, "GET", "/vms", checkins=checkins)
    if not isinstance(listing, list):
        raise PolicyRefusal(
            "vmrest did not return a VM list — check that `vmrest` is running and that "
            "its credentials were set with `vmrest -C`.")

    limit = max(1, int(payload.get("page_size") or 250))
    out = []
    for entry in listing[:limit]:
        vm_id = str(entry.get("id") or "")
        if not vm_id:
            continue
        settings_doc = _vmrest(conn, "GET", f"/vms/{vm_id}", checkins=checkins) or {}
        power = _vmrest(conn, "GET", f"/vms/{vm_id}/power", checkins=checkins) or {}
        state = str(power.get("power_state") or "")
        row = {
            "vm_id": vm_id,
            "name": _vmrest_name(conn, vm_id, entry.get("path"), checkins),
            "power_state": state,
            "vcpus": (settings_doc.get("cpu") or {}).get("processors"),
            "mem_mib": settings_doc.get("memory"),
            # The VMX path rides in `scope`, which is the generic cache's per-kind
            # column. It is display-only: identity is vmrest's opaque id.
            "scope": str(entry.get("path") or ""),
            "vm_type": "vm",
            # A fifth call per VM, on top of /vms/{id}, /power, /params/displayName and
            # (when running) /ip. Affordable: this is a desktop hypervisor with tens of
            # VMs and no cursor, and the OS column is blank without it.
            "guest_os": _vmrest_guest_os(conn, vm_id, checkins),
        }
        # Only a powered-on VM has an address, and only with tools installed. Asking a
        # stopped VM costs a call per VM to learn nothing.
        if state.lower() in ("poweredon", "powered_on", "on"):
            ip = _vmrest(conn, "GET", f"/vms/{vm_id}/ip", checkins=checkins) or {}
            if ip.get("ip"):
                row["ip_addresses"] = [str(ip["ip"])]
        out.append(row)

    emit(f"read {len(out)} VM(s) from Workstation at {host}")
    # One page: a desktop hypervisor does not have 10 000 VMs, and vmrest has no cursor.
    return {"vms": out, "next_cursor": "", "complete": True, "scanned": len(out),
            "enumerated": True}


def _power_workstation(conn, payload, policy, emit, verb, checkins=None):
    action = _VMREST_POWER.get(verb)
    if action is None:
        raise PolicyRefusal(
            f"vmrest has no {verb!r} operation — Workstation supports power_on and "
            f"power_off only (its API offers on/off/shutdown/suspend/pause/unpause, "
            f"with no reset, reboot or snapshot).")
    vm_id = str(payload.get("target_id") or "")
    if not vm_id:
        raise PolicyRefusal("a Workstation power verb needs target_id (the vmrest VM id)")
    host, port = _conn_endpoint(conn, 8697)
    _check_endpoint(policy, host, port, str(payload.get("connection_ref") or ""))
    _vmrest(conn, "PUT", f"/vms/{vm_id}/power", body=action, checkins=checkins)
    emit(f"{verb} issued for {vm_id}")
    return {"verb": verb, "target_id": vm_id, "ok": True}

_SYNC = {"vsphere": _sync_vsphere, "proxmox": _sync_proxmox,
         "nutanix": _sync_nutanix, "xcpng": _sync_xcpng,
         "workstation": _sync_workstation}

# Verb -> (proxmox path fragment, nutanix power state, XAPI method). Hyper-V is absent
# on purpose: WinRM needs a real NTLM/Negotiate stack, which is the one transport the
# stdlib cannot do, so Hyper-V power goes through the sibling runner instead — see
# _SIBLING_KINDS and runners/hypervisor/run.py::_PS_POWER.
#
# The nutanix column is inert — `_power_nutanix` refuses before reading this table — and
# is kept only so the rows stay the same shape. A v3 power change is a full spec PUT, so
# there is no action string for it to hold in the first place.
_POWER = {
    "power_on":    {"proxmox": "start",    "nutanix": "ON",  "xcpng": "VM.start"},
    "power_off":   {"proxmox": "stop",     "nutanix": "OFF", "xcpng": "VM.hard_shutdown"},
    "power_reset": {"proxmox": "reset",    "nutanix": "OFF", "xcpng": "VM.hard_reboot"},
    "shutdown":    {"proxmox": "shutdown", "nutanix": "OFF", "xcpng": "VM.clean_shutdown"},
    "reboot":      {"proxmox": "reboot",   "nutanix": "OFF", "xcpng": "VM.clean_reboot"},
    # `restart` predates `shutdown`/`reboot` and resolves differently per kind, which is
    # exactly why those two were added. It stays for one reason: an agent is upgraded
    # separately from the dashboard, so removing a verb an already-deployed policy.yaml
    # grants would break the button that uses it. Only XCP-ng's Reboot still maps here;
    # the dashboard's PAGE_OPS says so and says why.
    "restart":     {"proxmox": "shutdown", "nutanix": "OFF", "xcpng": "VM.clean_reboot"},
}


def _power_proxmox(conn, payload, policy, emit, verb, checkins=None):
    host, port = _conn_endpoint(conn, 8006)
    _check_endpoint(policy, host, port)
    node = payload.get("target_scope") or ""
    vm_type = payload.get("target_type") or "qemu"
    vmid = payload.get("target_id") or ""
    if not (node and vmid):
        raise PolicyRefusal("a Proxmox power verb needs target_scope (node) and target_id")
    user = str(conn.get("username") or "root@pam")
    header = {"Authorization":
              f"PVEAPIToken={user}!{conn.get('token_id')}={_secret_for(conn, checkins)}"}
    action = _POWER[verb]["proxmox"]
    _hv_request(conn, "POST",
                f"https://{host}:{port}/api2/json/nodes/{node}/{vm_type}/{vmid}"
                f"/status/{action}", headers=header)
    emit(f"{verb} issued for {vm_type}/{vmid} on {node}")
    return {"verb": verb, "target_id": vmid, "ok": True}


def _power_vsphere(conn, payload, policy, emit, verb, checkins=None):
    """vCenter power via the Automation REST API. vCenter only — ESXi serves SOAP."""
    host, port = _conn_endpoint(conn, 443)
    _check_endpoint(policy, host, port)
    base = f"https://{host}:{port}"
    user = str(conn.get("username") or "")
    basic = base64.b64encode(f"{user}:{_secret_for(conn, checkins)}".encode()).decode()
    session = _hv_request(conn, "POST", f"{base}/api/session",
                          headers={"Authorization": f"Basic {basic}"})
    token = session if isinstance(session, str) else str(session.get("value") or session)
    vm = payload.get("target_id") or ""
    if not vm:
        raise PolicyRefusal("a vSphere power verb needs target_id (the VM moref)")
    # Verb -> the sub-path under /api/vcenter/vm/{vm}, not just the ?action= value,
    # because vCenter puts the graceful pair on a DIFFERENT ENDPOINT. `power` is the
    # virtual power button; `guest/power` asks the operating system, through VMware
    # Tools. Holding the whole sub-path here is what lets one table say which of the two
    # a verb uses — a table of bare actions could only have said `shutdown`, and
    # `power?action=shutdown` is not a thing vCenter accepts.
    path = {"power_on":    "power?action=start",
            "power_off":   "power?action=stop",
            "power_reset": "power?action=reset",
            "restart":     "power?action=reset",
            "shutdown":    "guest/power?action=shutdown",
            "reboot":      "guest/power?action=reboot"}[verb]
    try:
        _hv_request(conn, "POST", f"{base}/api/vcenter/vm/{vm}/{path}",
                    headers={"vmware-api-session-id": token})
    except PolicyRefusal as exc:
        # The guest endpoint has one failure the power endpoint cannot have, and its
        # bare status line does not say so: 503 here means VMware Tools is not answering
        # in that guest. Without this the operator reads "answered 503" on a VM that is
        # plainly running and has no reason to suspect the guest agent.
        if path.startswith("guest/") and getattr(exc, "status", None) in (400, 503):
            raise PolicyRefusal(
                f"{exc}. A graceful {verb} on vCenter is not a power action — it asks "
                f"the guest OS through VMware Tools (503: Tools not running in the "
                f"guest; 400: the VM is not powered on). Install or start VMware Tools, "
                f"or use Force Off, which cuts power and needs no guest agent."
            ) from exc
        raise
    emit(f"{verb} issued for {vm}")
    return {"verb": verb, "target_id": vm, "ok": True}


def _power_nutanix(conn, payload, policy, emit, verb, checkins=None):
    raise PolicyRefusal(
        "Nutanix power verbs are not implemented in this agent build — a v3 power "
        "change is a full spec PUT with a metadata version, not a simple action.")


def _power_xcpng(conn, payload, policy, emit, verb, checkins=None):
    import xmlrpc.client

    host, port = _conn_endpoint(conn, 443)
    _check_endpoint(policy, host, port)
    context = ssl.create_default_context()
    if not conn.get("verify_ssl"):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    server = xmlrpc.client.ServerProxy(
        f"https://{host}:{port}/",
        transport=xmlrpc.client.SafeTransport(context=context))
    result = server.session.login_with_password(
        str(conn.get("username") or "root"), _secret_for(conn, checkins), "2.0", "dashboard-agent")
    if result.get("Status") != "Success":
        raise PolicyRefusal(f"XAPI login refused at {host}")
    session = result["Value"]
    try:
        ref = server.VM.get_by_uuid(session, payload.get("target_id") or "").get("Value")
        method = _POWER[verb]["xcpng"]
        obj = server
        for part in method.split("."):
            obj = getattr(obj, part)
        args = (session, ref, False, False) if method == "VM.start" else (session, ref)
        obj(*args)
    finally:
        try:
            server.session.logout(session)
        except Exception:  # noqa: BLE001
            pass
    emit(f"{verb} issued for {payload.get('target_id')}")
    return {"verb": verb, "target_id": payload.get("target_id"), "ok": True}


_POWER_IMPL = {"proxmox": _power_proxmox, "nutanix": _power_nutanix,
               "xcpng": _power_xcpng, "vsphere": _power_vsphere,
               "workstation": _power_workstation}


# Mirrors agent_hypervisor_meta.snapshot_name. Derived from the job id the agent
# already holds, NOT sent in the payload: a snapshot needs a name, and the moment a
# name travels as a field there is a free-form string in a payload whose whole premise
# is that there isn't one. The job id also makes the snapshot traceable to the row
# that created it, which an operator-typed name would not be.
SNAPSHOT_NAME_PREFIX = "dash-"


def _snapshot_name(job_id: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9-]", "", str(job_id or ""))[:32]
    return f"{SNAPSHOT_NAME_PREFIX}{clean}" if clean else f"{SNAPSHOT_NAME_PREFIX}unknown"


def _snapshot_proxmox(conn, payload, policy, emit, name, checkins=None):
    host, port = _conn_endpoint(conn, 8006)
    _check_endpoint(policy, host, port)
    node = payload.get("target_scope") or ""
    vm_type = payload.get("target_type") or "qemu"
    vmid = payload.get("target_id") or ""
    if not (node and vmid):
        raise PolicyRefusal("a Proxmox snapshot needs target_scope (node) and target_id")
    user = str(conn.get("username") or "root@pam")
    header = {"Authorization":
              f"PVEAPIToken={user}!{conn.get('token_id')}={_secret_for(conn, checkins)}"}
    _hv_request(conn, "POST",
                f"https://{host}:{port}/api2/json/nodes/{node}/{vm_type}/{vmid}/snapshot",
                headers=header, body={"snapname": name})
    emit(f"snapshot {name} requested for {vm_type}/{vmid} on {node}")
    return {"verb": "snapshot", "target_id": vmid, "snapshot": name, "ok": True}


def _snapshot_vsphere(conn, payload, policy, emit, name, checkins=None):
    host, port = _conn_endpoint(conn, 443)
    _check_endpoint(policy, host, port)
    base = f"https://{host}:{port}"
    user = str(conn.get("username") or "")
    basic = base64.b64encode(f"{user}:{_secret_for(conn, checkins)}".encode()).decode()
    session = _hv_request(conn, "POST", f"{base}/api/session",
                          headers={"Authorization": f"Basic {basic}"})
    token = session if isinstance(session, str) else str(session.get("value") or session)
    vm = payload.get("target_id") or ""
    if not vm:
        raise PolicyRefusal("a vSphere snapshot needs target_id (the VM moref)")
    _hv_request(conn, "POST", f"{base}/api/vcenter/vm/{vm}/snapshots",
                headers={"vmware-api-session-id": token},
                body={"name": name, "description": "created by vm-dashboard",
                      "memory": False, "quiesce": False})
    emit(f"snapshot {name} requested for {vm}")
    return {"verb": "snapshot", "target_id": vm, "snapshot": name, "ok": True}


def _snapshot_xcpng(conn, payload, policy, emit, name, checkins=None):
    import xmlrpc.client

    host, port = _conn_endpoint(conn, 443)
    _check_endpoint(policy, host, port)
    context = ssl.create_default_context()
    if not conn.get("verify_ssl"):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    server = xmlrpc.client.ServerProxy(
        f"https://{host}:{port}/",
        transport=xmlrpc.client.SafeTransport(context=context))
    result = server.session.login_with_password(
        str(conn.get("username") or "root"), _secret_for(conn, checkins), "2.0", "dashboard-agent")
    if result.get("Status") != "Success":
        raise PolicyRefusal(f"XAPI login refused at {host}")
    session = result["Value"]
    try:
        ref = server.VM.get_by_uuid(session, payload.get("target_id") or "").get("Value")
        server.VM.snapshot(session, ref, name)
    finally:
        try:
            server.session.logout(session)
        except Exception:  # noqa: BLE001
            pass
    emit(f"snapshot {name} requested for {payload.get('target_id')}")
    return {"verb": "snapshot", "target_id": payload.get("target_id"),
            "snapshot": name, "ok": True}


def _snapshot_nutanix(conn, payload, policy, emit, name, checkins=None):
    raise PolicyRefusal(
        "Nutanix snapshots are not implemented in this agent build — a v3 snapshot is "
        "a separate resource with its own spec, not an action on the VM.")


_SNAPSHOT_IMPL = {"proxmox": _snapshot_proxmox, "vsphere": _snapshot_vsphere,
                  "xcpng": _snapshot_xcpng, "nutanix": _snapshot_nutanix}


# The two the agent cannot speak to itself. `esxi` is a distinct kind from `vsphere`
# on purpose: the same product, but a bare host serves SOAP only while vCenter serves
# the Automation REST API the agent uses directly, and conflating them is how someone
# ends up pointing pyVmomi at a vCenter for no reason.
_SIBLING_KINDS = ("hyperv", "esxi")


# ── The sibling runner ────────────────────────────────────────────────────────
#
# Hyper-V (WinRM/NTLM) and bare ESXi (SOAP) are the two transports this agent cannot
# carry itself. Rather than fatten the image — the three-dependency rule is the security
# argument, in executable form, and two tests enforce it — those two run in a one-shot
# container that exits when the work is done. The supervisor stays inert.
#
# THE COST, STATED PLAINLY: launching a container needs the Docker socket, and the
# socket is root on the host. It is NOT mounted by the default deployment, whose compose
# file still promises it launches nothing; it lives in a separate opt-in overlay. So the
# capability needs three independent parties to agree — the dashboard grants the job
# type, the customer's policy.yaml enables the runner and names its image, and the host
# operator physically mounts the socket. Withhold any one and nothing happens.
#
# The Engine API is spoken with the standard library: http.client over an AF_UNIX
# socket, about sixty lines. Adding the `docker` SDK would put a package the audit tests
# ban into the image to do something they already cover.

DOCKER_SOCKET = os.environ.get("AGENT_DOCKER_SOCKET", "/var/run/docker.sock")
SIBLING_LABEL = "com.weaverlab.dashboard-agent"


class _UnixHTTP(http_client.HTTPConnection):
    """HTTPConnection that dials a unix socket instead of a host:port."""

    def __init__(self, path: str, timeout: float = 60.0):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


def _engine(method: str, path: str, body=None, timeout: float = 60.0,
            raw: bool = False):
    """One Docker Engine API call. Returns (status, parsed-or-raw-body).

    `raw=True` hands back the response bytes undecoded, and the container log stream is
    the one caller that needs it. Its 8-byte frame headers carry the payload length as a
    big-endian int32 — binary, not text — so the decode below turns any length byte >=
    0x80 into U+FFFD, and re-encoding then yields three bytes where the frame had one.
    That shifts every subsequent frame boundary and destroys the stream. It is not a rare
    edge: it corrupts half of all payload sizes, and it cost a whole class of runner
    result its error message (see the note in _run_sibling).
    """
    conn = _UnixHTTP(DOCKER_SOCKET, timeout=timeout)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
    except OSError as exc:
        raise PolicyRefusal(
            f"cannot reach the Docker socket at {DOCKER_SOCKET}: {exc}. The sibling "
            f"runner needs it mounted — see docker-compose.sibling.yml.")
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    if raw:
        return resp.status, data
    try:
        return resp.status, json.loads(data.decode("utf-8", "replace")) if data else {}
    except ValueError:
        return resp.status, data.decode("utf-8", "replace")


def _demux_frames(buf: bytearray) -> list:
    """Every COMPLETE frame in ``buf`` as ``[(stream, payload)]``, consumed in place.

    A partial frame — a header split across reads, or a payload that has not all arrived —
    is deliberately LEFT in ``buf`` for the next call. That is the whole difference between
    this and :func:`_demux`, and it is what makes the same walker usable on a follow stream:
    a walker that discards the tail of every read would drop most of the output, and one
    that mistook a partial payload for a header would desync every subsequent frame — the
    exact corruption :func:`_engine`'s docstring records.

    ``stream`` is Docker's first header byte: 1 stdout, 2 stderr. Returned rather than
    discarded so a caller can keep the two apart; concatenating them splices a stderr
    warning into the middle of a half-written stdout line.
    """
    frames = []
    while True:
        if len(buf) < 8:
            return frames
        size = int.from_bytes(buf[4:8], "big")
        if len(buf) < 8 + size:
            return frames          # payload still arriving — keep the header for next time
        stream = buf[0] or 1
        frames.append((stream, bytes(buf[8:8 + size])))
        del buf[:8 + size]


def _demux(raw: bytes) -> str:
    """Strip Docker's 8-byte stream framing from a complete, in-hand log body.

    A thin wrapper over :func:`_demux_frames` so there is one framing implementation. Used
    for the non-follow read, where the whole body genuinely is in hand: the hypervisor
    sibling's single JSON result, and the diagnostic tail after a failed start.

    The fallback matters for that second case: a body with no complete frame at all is
    almost always an unframed error string, so returning it as text beats returning "".
    """
    buf = bytearray(raw)
    frames = _demux_frames(buf)
    if not frames:
        return raw.decode("utf-8", "replace")
    return b"".join(payload for _stream, payload in frames).decode("utf-8", "replace")


def sweep_orphans() -> int:
    """Remove sibling containers a previous agent left behind.

    A crash between create and remove leaves one; without this they accumulate on the
    host forever. Scoped by label, so it can never touch a container this agent did not
    create. Best effort — a socket that is not mounted simply means there is nothing to
    sweep.
    """
    try:
        status, body = _engine(
            "GET", "/containers/json?all=1&filters="
                   + _quote(json.dumps({"label": [SIBLING_LABEL]})))
    except PolicyRefusal:
        return 0
    if status != 200 or not isinstance(body, list):
        return 0
    removed = 0
    for container in body:
        try:
            _engine("DELETE", f"/containers/{container['Id']}?force=1&v=1")
            removed += 1
        except Exception:  # noqa: BLE001
            pass
    if removed:
        log.info("swept %d orphaned sibling container(s)", removed)
    return removed


def _run_sibling(policy: "Policy", env: dict, emit, cancelled,
                 timeout: float = 300.0) -> dict:
    """Run one hypervisor operation in a throwaway container and return its JSON.

    Note what is NOT derived from the job: the image (policy), and every field of
    HostConfig. There is deliberately no path by which a payload can ask for a bind
    mount, a capability, host networking or privileged mode — a fully hostile dashboard
    cannot get `--privileged` out of this agent because there is no field to ask with.
    """
    image = policy.sibling_image
    if not policy.sibling_enabled or not image:
        raise PolicyRefusal(
            "this agent's policy.yaml does not enable the sibling runner. Set "
            "`sibling: {enabled: true, image: chrweav/hypervisor-runner:latest}` and "
            "mount the Docker socket — see docs/remote-agents.md#the-sibling-runner.")

    # The credential rides in Env in the create body: not in argv (where it would show
    # in `ps` on the host) and not in a file (which would need a bind mount).
    spec = {
        "Image": image,
        "Env": [f"{k}={v}" for k, v in env.items()],
        "Labels": {SIBLING_LABEL: "1"},
        "HostConfig": {
            "AutoRemove": False,       # racing the log read; we remove explicitly
            "Binds": [],
            "Privileged": False,
            "CapDrop": ["ALL"],
            "ReadonlyRootfs": True,
            "Tmpfs": {"/tmp": "rw,size=16m"},
            "NetworkMode": policy.sibling_network,
            "PidsLimit": 128,
            "Memory": 256 * 1024 * 1024,
            "SecurityOpt": ["no-new-privileges:true"],
        },
    }
    status, body = _engine("POST", "/containers/create", spec)
    if status == 404:
        raise PolicyRefusal(
            f"the sibling image {image!r} is not present on this host. Pull it first: "
            f"docker pull {image}")
    if status not in (200, 201):
        raise PolicyRefusal(f"could not create the sibling container ({status}): "
                            f"{str(body)[:200]}")
    container = body.get("Id")
    emit(f"running {image} for this operation")

    try:
        status, _ = _engine("POST", f"/containers/{container}/start")
        if status not in (204, 304):
            raise PolicyRefusal(f"the sibling container would not start ({status})")

        if cancelled():
            _engine("POST", f"/containers/{container}/kill")
            raise PolicyRefusal("cancelled while the sibling was starting")

        status, _ = _engine("POST", f"/containers/{container}/wait",
                            timeout=timeout + 15)
        # raw=True, never a str: re-encoding a decoded log stream mangles the frame
        # headers and silently lost half of all results, successes included.
        status, stream = _engine(
            "GET", f"/containers/{container}/logs?stdout=1&stderr=1", raw=True)
        text = _demux(stream)
    finally:
        try:
            _engine("DELETE", f"/containers/{container}?force=1&v=1")
        except Exception:  # noqa: BLE001
            log.warning("could not remove sibling container %s", container[:12])

    # The runner prints exactly one JSON object, and prints a refusal the same way, so
    # a failure is reported rather than inferred from an empty pipe.
    line = next((ln for ln in reversed(text.splitlines()) if ln.strip().startswith("{")),
                "")
    if not line:
        raise PolicyRefusal(f"the sibling runner produced no result: {text[-300:]}")
    try:
        result = json.loads(line)
    except ValueError:
        raise PolicyRefusal("the sibling runner produced unparseable output")
    if not result.get("ok"):
        raise PolicyRefusal(result.get("error") or "the sibling runner failed")
    result.pop("ok", None)
    return result


# ── The Ansible sibling: streamed, cancellable, one-shot ──────────────────────
#
# A hypervisor operation is one API call whose whole result is a line of JSON. An Ansible
# run is minutes of human-readable output an operator watches, so it needs a different
# shape: follow the log stream, forward lines as they arrive, and be killable. That is why
# this lives beside `_run_sibling` rather than inside it — the hypervisor path is correct as
# it is, and bending it to do both would put a `follow` flag through code where the failure
# mode is a corrupted stream.

# Where the run's files are extracted, and a VOLUME rather than a directory in the image.
# Both halves of that were found the hard way against a real dockerd:
#
#   NOT under /tmp, because the tmpfs below is mounted over /tmp when the container starts,
#   which shadows anything placed there beforehand and takes it away with no diagnostic
#   whatsoever.
#
#   A volume, because `PUT /containers/<id>/archive` is refused outright — 400 "container
#   rootfs is marked read-only" — on any container whose HostConfig sets ReadonlyRootfs,
#   unless the extract destination resolves INTO a mount. The daemon decides that from
#   HostConfig alone, so "put the files in before it starts" does not get around it.
#
# A tmpfs mount here would take the PUT and pass every test, and then lose the files: the
# tmpfs is mounted empty at start, over the top of what was just extracted. It has to be a
# volume, which the daemon mounts for the extract as well and which therefore survives.
# Anonymous, so the `?force=1&v=1` removals below take it away with the container.
#
# Must match agent_ansible_bundle.JOB_DIR on the dashboard.
_JOB_DIR = "/opt/job"

# Every value is a CONSTANT. `run_kind` selects the image (from policy) and nothing else, so
# no HostConfig field is derived from anything the dashboard sends — the property
# tests/test_agent_sibling_runner.py::test_no_hostconfig_field_comes_from_the_job asserts.
#
# Each number is here for a reason a real run found:
#   Memory      256 MB SIGKILLs ansible-core before it prints anything — the controller alone
#               is ~150-200 MB once requests/cryptography/pywinrm are imported, and every
#               forked worker dirties copy-on-write pages immediately. MemorySwap equal to it
#               makes the limit a limit rather than an invitation to thrash.
#   PidsLimit   threads count against the pids cgroup, and a run is ansible + N forks + a
#               persistent ansible-connection per host + ssh/sshpass.
#   Tmpfs       the playbook and key fit in 16 MB; ansible's assembled AnsiballZ_* module
#               payloads do not.
#   LogConfig   NOT optional. `GET /logs` answers 501 when the container's logging driver is
#               not file-based, and journald is the default on RHEL, Fedora, Rocky and Alma —
#               so without this the whole feature is dead on those hosts, reported as "the
#               runner produced no output". The rotation is free for a streaming reader.
_ANSIBLE_MEMORY = 1024 * 1024 * 1024
_ANSIBLE_HOSTCONFIG = {
    "AutoRemove": False,        # the log stream and /wait both outlive the container
    "Binds": [],               # no bind mount: nothing of this host is exposed to a run
    # The job dir, and the only writable thing besides /tmp. Anonymous — no Source, so
    # there is still no host path here for a job to influence. See _JOB_DIR above for why
    # the archive PUT does not work without it.
    "Mounts": [{"Type": "volume", "Target": _JOB_DIR}],
    "Privileged": False,
    "CapDrop": ["ALL"],
    "ReadonlyRootfs": True,
    "Tmpfs": {"/tmp": "rw,nosuid,nodev,size=128m,mode=1777"},
    "PidsLimit": 512,
    "Memory": _ANSIBLE_MEMORY,
    "MemorySwap": _ANSIBLE_MEMORY,
    "NanoCpus": 2_000_000_000,
    "SecurityOpt": ["no-new-privileges:true"],
    "LogConfig": {"Type": "json-file",
                  "Config": {"max-size": "16m", "max-file": "2"}},
}

# Ansible writes outside /tmp by default — ~/.ansible/tmp for local temp, ~/.ansible/cp for
# SSH ControlPersist sockets — and both runner images run as root with HOME=/root. On a
# read-only root filesystem that is an immediate `[Errno 30] Read-only file system:
# '/root/.ansible'`, before a single task runs. Relocating them is what lets
# ReadonlyRootfs stay true, which is worth more than the convenience of turning it off.
_ANSIBLE_ENV = {
    "ANSIBLE_HOME": "/tmp/.ansible",
    "ANSIBLE_LOCAL_TEMP": "/tmp/.ansible/tmp",
    "ANSIBLE_SSH_CONTROL_PATH_DIR": "/tmp/.ansible/cp",
    "ANSIBLE_REMOTE_TMP": "/tmp/.ansible/remote",
    "ANSIBLE_RETRY_FILES_ENABLED": "0",
    # The agent has no host-key store and each run is a fresh container, so there is nothing
    # for a known_hosts check to be continuous with — it would refuse every first connection.
    "ANSIBLE_HOST_KEY_CHECKING": "False",
    "ANSIBLE_NOCOLOR": "1",
    # stdout is a pipe, not a tty: without this Python buffers and the "live" output arrives
    # in 4 KB blocks, which reads as a hung run.
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}

# One log line the agent will forward without a newline before giving up on finding one.
# Matches Reporter.emit's own truncation so a line cannot be counted long here and short
# there.
_MAX_STREAM_LINE = 8192


def _archive_put(container: str, dest: str, files: dict) -> None:
    """Write ``{absolute path: bytes}`` into a created-but-not-started container as a tar.

    This is how the playbook, the inventory and the SSH key get in. The alternative —
    base64 in the environment, as the cloud runners use — cannot work here: ``execve``
    caps a *single* environment string at ``MAX_ARG_STRLEN``, which is 128 KB on a
    4 KB-page host and 2 MB on a 64 KB-page arm64 one. So an env-delivered playbook works
    on the machine it was developed on and fails on a customer's with `argument list too
    long`, surfacing as an unexplained container-start failure. There is no size limit here.

    It is also strictly better for the credential: a value in ``Env`` is visible in
    ``docker inspect`` for the life of the container, and these files are not.

    Modes are set in the tar rather than by a shell step afterwards, so the private key is
    never briefly world-readable. ``tarfile`` is stdlib, so the three-dependency rule holds.

    Everything is extracted into ``dest``, which must be a mount — see ``_JOB_DIR``. Members
    are named relative to it, so ``files`` stays keyed by the absolute path each file will
    have inside the container and there is one place that says where a file lands.
    """
    import io
    import tarfile

    prefix = "/" + dest.strip("/") + "/"
    blob = io.BytesIO()
    # No compression: this is a loopback socket, and gzip would only add CPU.
    with tarfile.open(fileobj=blob, mode="w") as tar:
        seen = set()
        for path, content in files.items():
            abs_path = "/" + path.strip("/")
            if not abs_path.startswith(prefix):
                # Refused rather than written: extraction is relative to `dest`, so a file
                # from outside it would silently land somewhere else entirely — and the one
                # place that could happen is a path assembled from a dashboard-supplied
                # asset name.
                raise PolicyRefusal(
                    f"refusing to place {abs_path!r}: the run's files must all be under "
                    f"{dest}")
            rel = abs_path[len(prefix):]
            # Explicit parent directories. Docker's extractor does not create them, and a
            # missing one fails the whole PUT rather than the single entry.
            parts = rel.split("/")[:-1]
            for i in range(len(parts)):
                d = "/".join(parts[:i + 1])
                if d in seen:
                    continue
                seen.add(d)
                info = tarfile.TarInfo(d)
                info.type = tarfile.DIRTYPE
                info.mode = 0o700
                tar.addfile(info)
            info = tarfile.TarInfo(rel)
            info.size = len(content)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(content))
    payload = blob.getvalue()

    conn = _UnixHTTP(DOCKER_SOCKET, timeout=120.0)
    try:
        conn.request("PUT",
                     f"/containers/{container}/archive?path={_quote(dest, safe='')}",
                     body=payload,
                     headers={"Content-Type": "application/x-tar",
                              "Content-Length": str(len(payload))})
        resp = conn.getresponse()
        body = resp.read()
    except OSError as exc:
        raise PolicyRefusal(f"could not write the run's files into the container: {exc}")
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    if resp.status not in (200, 204):
        detail = body[:200].decode("utf-8", "replace")
        if "read-only" in detail:
            detail += (" — the runner is created with a read-only root filesystem, so the "
                       f"Engine only accepts an extract into a mount. {dest} should be one "
                       "of HostConfig.Mounts; if this agent has been patched, that is what "
                       "to put back.")
        raise PolicyRefusal(
            f"could not write the run's files into the container ({resp.status}): {detail}")


def _stream_logs(container: str, on_line, deadline: float) -> None:
    """Follow a container's log stream, calling ``on_line(text)`` per complete line.

    Three things here are not obvious and all three were bugs waiting to happen.

    ``read1`` and never ``read``: ``read(n)`` blocks until *n* bytes accumulate, so a
    64 KB buffer would hold a play's output back for minutes at a time and the run would
    look hung. ``read1`` does one recv and returns what is there.

    ``IncompleteRead`` is a NORMAL end of stream, not an error. When a container is killed
    the socket EOFs mid-chunk and ``http.client`` raises it; uncaught, a cancel would surface
    as a traceback instead of "cancelled".

    Nothing here polls for cancellation. The socket timeout is a generous *ceiling*, not a
    clock — a timeout raised inside ``http.client``'s chunked state machine can leave it
    between a chunk trailer and the next size line, with no defined way to resume, and one
    desynced chunk desyncs every subsequent 8-byte frame header. Cancellation is a separate
    thread that kills the container; the daemon closing the stream is what ends this loop.
    """
    conn = _UnixHTTP(DOCKER_SOCKET, timeout=max(30.0, deadline - time.monotonic() + 60.0))
    try:
        conn.request("GET", f"/containers/{container}/logs?stdout=1&stderr=1&follow=1")
        resp = conn.getresponse()
        if resp.status != 200:
            detail = resp.read()[:200].decode("utf-8", "replace")
            if resp.status == 501:
                raise PolicyRefusal(
                    "this host's Docker logging driver is not file-based, so the run's "
                    "output cannot be read back (the Engine answered 501). The agent asks "
                    "for json-file per container, so this usually means the daemon forbids "
                    "it — check `log-driver` in /etc/docker/daemon.json.")
            raise PolicyRefusal(f"could not read the runner's output ({resp.status}): {detail}")

        buf = bytearray()
        held = {1: bytearray(), 2: bytearray()}
        while True:
            try:
                chunk = resp.read1(65536)
            except (http_client.IncompleteRead, OSError):
                break              # killed, or the daemon went away: an ordinary EOF here
            if not chunk:
                break
            buf += chunk
            for stream, payload in _demux_frames(buf):
                # stdout and stderr are kept apart deliberately: one shared buffer splices a
                # stderr warning into the middle of a half-written stdout line.
                pending = held[2 if stream == 2 else 1]
                pending += payload
                while True:
                    nl = pending.find(b"\n")
                    if nl >= 0:
                        line, del_to = bytes(pending[:nl]), nl + 1
                    elif len(pending) >= _MAX_STREAM_LINE:
                        # No newline in sight. Flush what we have — and delete exactly what
                        # was emitted, with no +1: the +1 is for a newline that is not there,
                        # and it would silently eat one byte every 8 KB.
                        line, del_to = bytes(pending[:_MAX_STREAM_LINE]), _MAX_STREAM_LINE
                    else:
                        break
                    del pending[:del_to]
                    on_line(line.decode("utf-8", "replace"))
        # Whatever each stream ended on without a newline. Ansible's last line usually has
        # one, but a crash mid-write does not, and that is the line worth having.
        for pending in held.values():
            if pending:
                on_line(bytes(pending).decode("utf-8", "replace"))
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _run_ansible_sibling(policy: "Policy", *, image: str, files: dict, env: dict,
                         command: str, emit, cancelled) -> int:
    """Run one playbook in a throwaway container, streaming its output. Returns the exit code.

    Same guarantees as :func:`_run_sibling` — the image comes from policy, every HostConfig
    field is a constant, nothing is bind-mounted — with three additions a long run needs:
    output as it happens, a working Cancel, and a wall-clock ceiling.
    """
    if not policy.ansible_enabled:
        raise PolicyRefusal(
            "this agent's policy.yaml does not enable Config Management "
            "(`ansible: {enabled: true}`).")
    if policy.ansible_network == "none":
        # Worth refusing rather than running: with no network every task fails as
        # "unreachable", which reads as a firewall or credential problem on the target.
        raise PolicyRefusal(
            "policy.yaml sets `ansible.network: none`, so the runner has no network and "
            "every task would fail as unreachable. Use `bridge`, or a network that can "
            "reach the target.")

    spec = {
        "Image": image,
        "Env": [f"{k}={v}" for k, v in {**_ANSIBLE_ENV, **env}.items()],
        # A LIST, because the Engine API's Cmd is an array of strings — a bare string is not
        # shell-parsed for you, it is taken as argv[0]. `caller` already prefixed `exec`, so
        # the container's PID 1 becomes ansible-playbook itself: without that, `sh` is PID 1
        # and does not forward SIGTERM to a child while waiting, so Cancel would do nothing
        # until the SIGKILL ten seconds later — the same reason `docker stop` on `sh -c`
        # always burns the full grace period.
        "Cmd": ["/bin/sh", "-c", command],
        # Cleared explicitly. `chrweav/ansible-winrm` is built on an upstream image that sets
        # its own ENTRYPOINT, which would otherwise be prepended to the Cmd above and turn a
        # valid command into an unrecognised argument.
        "Entrypoint": [],
        "WorkingDir": _JOB_DIR,
        "Tty": False,              # the deframer's precondition, stated rather than assumed
        "Labels": {SIBLING_LABEL: "1"},
        "HostConfig": {**_ANSIBLE_HOSTCONFIG, "NetworkMode": policy.ansible_network},
    }
    status, body = _engine("POST", "/containers/create", spec)
    if status == 404:
        raise PolicyRefusal(
            f"the Ansible runner image {image!r} is not present on this host. Pull it "
            f"first: docker pull {image}")
    if status not in (200, 201):
        # The body is included deliberately — it carries the real reason, and discarding it
        # is what turns a one-line diagnosis into an unsolvable report.
        raise PolicyRefusal(f"could not create the Ansible runner ({status}): "
                            f"{str(body)[:300]}")
    container = body.get("Id")

    timeout = max(60.0, policy.ansible_max_runtime_minutes * 60.0)
    deadline = time.monotonic() + timeout
    done = threading.Event()
    killed = threading.Event()
    expired = threading.Event()

    def _watch():
        """Kill the container on Cancel or on the deadline.

        A separate thread because the reader must not poll: see :func:`_stream_logs`. Its own
        short-timeout Engine calls, so a wedged daemon cannot make the watcher hang too.
        """
        while not done.wait(1.0):
            over = time.monotonic() > deadline
            if not (over or cancelled()):
                continue
            if over:
                expired.set()
            killed.set()
            try:
                _engine("POST", f"/containers/{container}/kill?signal=SIGTERM", timeout=20.0)
            except Exception:  # noqa: BLE001
                pass
            if not done.wait(10.0):
                try:
                    _engine("POST", f"/containers/{container}/kill?signal=SIGKILL",
                            timeout=20.0)
                except Exception:  # noqa: BLE001
                    pass
            return

    watcher = threading.Thread(target=_watch, name="ansible-watch", daemon=True)
    try:
        # The files go in BEFORE start, into the job-dir volume: the tmpfs is mounted over
        # /tmp at start, and the Engine refuses an extract anywhere but a mount on a
        # container whose rootfs is read-only — created or running, it reads HostConfig.
        _archive_put(container, _JOB_DIR, files)

        status, start_body = _engine("POST", f"/containers/{container}/start")
        if status not in (204, 304):
            raise PolicyRefusal(f"the Ansible runner would not start ({status}): "
                                f"{str(start_body)[:300]}")
        emit(f"running {image}")
        watcher.start()
        _stream_logs(container, emit, deadline)
        done.set()

        # /wait AFTER the stream, and it is /wait rather than an inspect on purpose. The
        # container has already exited by the time the follow stream ends, so this returns
        # at once — but if the daemon has not yet recorded the exit, /wait blocks until it
        # can answer, whereas `GET /json` polled a millisecond early answers
        # `Running: true, ExitCode: 0`. That is a FALSE SUCCESS, which for a config run
        # means a failed playbook reported green.
        status, waited = _engine("POST", f"/containers/{container}/wait", timeout=120.0)
        code = int((waited or {}).get("StatusCode", -1)) if isinstance(waited, dict) else -1
        wait_error = ((waited or {}).get("Error") or {}).get("Message") \
            if isinstance(waited, dict) else ""

        if expired.is_set():
            raise PolicyRefusal(
                f"the run exceeded this agent's ceiling of "
                f"{policy.ansible_max_runtime_minutes} minutes and was stopped. Raise "
                f"`ansible.max_runtime_minutes` in policy.yaml if the playbook is "
                f"legitimately this slow.")
        if killed.is_set():
            # An operator cancel, and it RETURNS rather than raising. `execute` checks
            # `reporter.cancelled` before it looks at the result and reports "Cancelled by
            # the operator."; raising here would instead render as "Agent policy refused:
            # cancelled", which reads like the policy stopped something the operator asked
            # for. The exit code is 137 or 143 and is deliberately not reported as a failure.
            emit("cancelled — the runner was stopped")
            return -1
        if code == 137 and _oom_killed(container):
            raise PolicyRefusal(
                f"the runner was killed for exceeding its "
                f"{_ANSIBLE_MEMORY // (1024 * 1024)} MB memory limit. That is a fixed limit "
                f"in this agent, not a setting — a playbook this heavy should do its work "
                f"on the target rather than on the controller.")
        if code < 0 and wait_error:
            raise PolicyRefusal(f"the Ansible runner failed to run: {wait_error}")
        return code
    finally:
        done.set()
        try:
            _engine("DELETE", f"/containers/{container}?force=1&v=1")
        except Exception:  # noqa: BLE001
            log.warning("could not remove Ansible runner container %s", container[:12])


def _oom_killed(container: str) -> bool:
    """Whether the kernel OOM-killed this container.

    Only inspect reports it, and it is the difference between "exit 137" and a message that
    names the memory limit — 137 alone is ambiguous between an OOM and the agent's own
    cancel-kill. Best effort: a container already gone is simply not an OOM we can prove.
    """
    try:
        status, body = _engine("GET", f"/containers/{container}/json", timeout=20.0)
    except Exception:  # noqa: BLE001
        return False
    if status != 200 or not isinstance(body, dict):
        return False
    return bool((body.get("State") or {}).get("OOMKilled"))


def _sibling_env(conn: dict, kind: str, verb: str, payload: dict, secret: str) -> dict:
    host, port = _conn_endpoint(conn, 5985 if kind == "hyperv" else 443)
    opts = conn.get("options") if isinstance(conn.get("options"), dict) else conn
    env = {
        "HV_KIND": kind, "HV_VERB": verb,
        "HV_HOST": host, "HV_PORT": str(port),
        "HV_USERNAME": str(conn.get("username") or ""),
        "HV_PASSWORD": secret,
        "HV_VERIFY_SSL": "1" if conn.get("verify_ssl") else "0",
        "HV_TARGET_ID": str(payload.get("target_id") or ""),
    }
    if kind == "hyperv":
        env["HV_TRANSPORT"] = str(opts.get("transport") or "ntlm")
        env["HV_USE_SSL"] = "1" if opts.get("use_ssl") else "0"
        # Report each guest's addresses and OS as well as the VM list. Off by default: it
        # costs a KVP read per VM on the host, and it is only needed where those guests are
        # Config-Management targets. It reads from YOUR connections.yaml rather than from a
        # dashboard field for the same reason `host` does — how much this agent reads out of
        # your guests is your decision, not the dashboard's.
        if opts.get("sync_guest_details"):
            env["HV_GUEST_DETAILS"] = "1"
    return env


def _via_sibling(conn, payload, policy, emit, verb, kind, checkins=None):
    host, port = _conn_endpoint(conn, 5985 if kind == "hyperv" else 443)
    # The policy gate applies exactly as it does to an in-process call: the sibling is
    # a different process, not a different trust boundary.
    _check_endpoint(policy, host, port)
    env = _sibling_env(conn, kind, verb, payload, _secret_for(conn, checkins))
    return _run_sibling(policy, env, emit, lambda: False)


def run_hypervisor(payload: dict, policy: Policy, emit, cancelled, job_id: str = "",
                   dashboard=None) -> dict:
    """Execute ONE allow-listed verb against ONE named connection.

    Three independent gates, and all three must agree:

    1. the dashboard granted this agent the job type (``allowed_job_types``);
    2. the customer's policy.yaml grants this verb on this connection name;
    3. the customer's connections.yaml actually defines that name, with a credential
       the dashboard has never seen.

    Note what this function never reads out of ``payload``: a host, a port, a URL, a
    username or a password. There is no field for any of them, and a static test
    asserts it — that is the difference between a verb allowlist and a proxy.
    """
    verb = str(payload.get("verb") or "")
    ref = str(payload.get("connection_ref") or "")
    kind = str(payload.get("kind") or "")

    policy.check_verb(ref, verb)
    conn = HypervisorConnections.load().get(ref)
    declared = str(conn.get("kind") or kind)
    if not _kind_matches(declared, kind):
        raise PolicyRefusal(
            f"connection {ref!r} is a {declared} connection; the job asked for {kind}")
    # The AGENT's kind wins from here on. The dashboard has no `esxi` connection kind —
    # from its side a bare host and a vCenter are both "vsphere" — but the two need
    # different transports, and only this file knows which one this endpoint is.
    kind = declared

    emit(f"{verb} on {ref} ({kind}) — granted by policy digest {policy.digest[:12]}")
    if cancelled():
        raise PolicyRefusal("cancelled before the connection was opened")

    if conn.get("ps_managed_account"):
        emit(f"checking a credential out of Password Safe for account "
             f"{conn['ps_managed_account']} — this agent stores none")
    elif conn.get("dashboard_secret"):
        # A statement of fact, never the value. The credential is scrubbed out of anything
        # outbound anyway (Dashboard.hold_secret), but nothing should be relying on that.
        emit(f"fetching the credential for {ref} from the dashboard — "
             f"nothing is stored on this host")
    checkins = JobSecrets(job_id=job_id, dashboard=dashboard, ref=ref)
    try:
        return _run_verb(conn, payload, policy, emit, verb, kind, job_id, checkins)
    finally:
        _checkin_all(checkins)


def _kind_matches(declared: str, asked: str) -> bool:
    """Does the agent's own kind for this connection satisfy what the job asked for?

    Exact match, plus one deliberate specialisation: a job for ``vsphere`` may be served
    by a connection this agent knows is ``esxi``. The dashboard cannot tell them apart —
    it stores no host for an agent-bound connection, let alone a probe of it — and both
    are vSphere as far as its model goes. The distinction is purely local: vCenter
    serves the Automation REST API the agent speaks directly, a bare host serves SOAP
    and has to go through the sibling runner.

    Not symmetric. A job for `esxi` is NOT served by a `vsphere` connection, because
    that direction would silently send SOAP work to a vCenter.
    """
    return declared == asked or (asked == "vsphere" and declared == "esxi")


def _run_verb(conn, payload, policy, emit, verb, kind, job_id, checkins):
    # Hyper-V and bare ESXi have no in-agent transport; they go to the sibling runner.
    if kind in _SIBLING_KINDS:
        if verb == "snapshot":
            raise PolicyRefusal(
                f"snapshot is not available for {kind!r} through the sibling runner")
        if verb == "reboot" and kind == "hyperv":
            # Refused here rather than by the runner's verb list, which would answer
            # "unknown verb 'reboot'" and read as a version mismatch. It is not one:
            # Hyper-V has no graceful-reboot cmdlet to run. Restart-VM is documented as
            # a "hard" restart — "like powering the computer down, then back up again" —
            # so it is what `power_reset` uses, and there is nothing left for `reboot`.
            raise PolicyRefusal(
                "Hyper-V has no graceful reboot: Restart-VM is a hard restart, which is "
                "what the page's Restart button already does. Shut the guest down and "
                "start it again if the reboot has to be graceful.")
        return _via_sibling(conn, payload, policy, emit, verb, kind, checkins)

    if verb == "inventory_sync":
        handler = _SYNC.get(kind)
        if handler is None:
            raise PolicyRefusal(f"no inventory sync for {kind!r} in this agent build")
        return handler(conn, payload, policy, emit, checkins)

    if verb == "snapshot":
        impl = _SNAPSHOT_IMPL.get(kind)
        if impl is None:
            raise PolicyRefusal(f"no snapshot support for {kind!r} in this agent build")
        return impl(conn, payload, policy, emit, _snapshot_name(job_id), checkins)

    impl = _POWER_IMPL.get(kind)
    if impl is None or (verb not in _POWER and kind != "workstation"):
        raise PolicyRefusal(f"{verb!r} is not available for {kind!r} in this agent build")
    return impl(conn, payload, policy, emit, verb, checkins)


# ── Config Management ─────────────────────────────────────────────────────────

# A variable an operator may not set, because Ansible reads it as connection configuration
# rather than data. THIS is the check that matters — the dashboard applies the same filter,
# but a compromised dashboard is precisely the thing that would stop applying it.
#
# What it prevents, concretely: `ansible_connection: local` turns "configure that VM over
# SSH" into "run this playbook inside the runner container", on this network, with the
# runner image's kubectl and helm on PATH. `ansible_python_interpreter` and
# `ansible_ssh_executable` are the same hole in different clothes. And `-e` outranks every
# inventory variable in Ansible's precedence order, so a filter on the inventory alone would
# be worth nothing.
_RESERVED_VAR_PREFIX = "ansible_"

# Ansible's own exit codes. Reported as text because "the run failed (exit 4)" sends an
# operator to a search engine, and every one of these has a specific meaning worth stating.
_ANSIBLE_EXIT = {
    0: "",
    1: "ansible-playbook itself failed — usually a malformed playbook or a bad argument",
    2: "one or more hosts failed a task",
    3: "one or more hosts were unreachable (older ansible reports this as 4)",
    4: "the target was unreachable — check the address, the port and the credential",
    99: "the run was aborted by a callback or a signal",
    250: "an unexpected internal ansible error",
}


def _check_extra_vars(extra_vars: dict) -> None:
    """Raise :class:`PolicyRefusal` if the dashboard tried to send a connection variable."""
    bad = sorted(k for k in (extra_vars or {})
                 if str(k).lower().startswith(_RESERVED_VAR_PREFIX))
    if bad:
        raise PolicyRefusal(
            f"the dashboard sent {', '.join(bad)} as extra vars, and this agent refuses "
            f"them: Ansible reads ansible_* variables as CONNECTION configuration, so one "
            f"of them could redirect this playbook into the runner container instead of the "
            f"target host. Remove them from the run and try again.")


def _vm_inventory(*, ip: str, port: int, transport: str, login_user: str,
                  winrm: dict) -> str:
    """A one-host Ansible inventory, authored HERE rather than received.

    Every value comes from the signed job envelope or from the typed credential fields —
    never from a structure the dashboard composed. That is the difference between this and
    accepting an inventory: there is no key here that the dashboard chose the *name* of.

    ``ansible_host`` is the **resolved IP**, not the name the job used. ``check_ansible``
    validated the resolved address to defeat DNS rebinding, but the connection happens later,
    in another process, in another namespace — so pinning the address here is what makes that
    check mean anything. It goes in the inventory rather than in ``HostConfig.ExtraHosts``,
    which would be a job-derived HostConfig field and would break the property that none are.
    """
    hostvars = {"ansible_host": ip, "ansible_port": int(port)}
    if transport == "winrm":
        hostvars.update({
            "ansible_connection": "winrm",
            "ansible_winrm_scheme": str(winrm.get("scheme") or "http"),
            "ansible_winrm_transport": str(winrm.get("transport") or "ntlm"),
            "ansible_winrm_server_cert_validation":
                str(winrm.get("cert_validation") or "ignore"),
        })
    else:
        hostvars["ansible_connection"] = "ssh"
    if login_user:
        hostvars["ansible_user"] = login_user
    # JSON, which is valid YAML, so ansible's yaml inventory plugin reads it. Written by
    # json.dumps rather than assembled as text so no value can inject structure.
    return json.dumps({"all": {"hosts": {"target": hostvars}}})


def _ansible_argv(*, run_kind: str, transport: str, has_key: bool,
                  has_vars: bool) -> list:
    """The ``ansible-playbook`` argv, mirroring ``services/ansible_vm_cmd.build_vm_argv``.

    Byte-identical to the dashboard's builder for the VM shape, and pinned by a test, for the
    reason that module's docstring gives: the ORDER of the two ``--extra-vars`` is what makes
    a resolved secret win a name conflict, and two copies would eventually disagree about it.

    The database shape is the ``hosts: localhost`` play instead, matching
    ``services/ansible_localhost_cmd.build_localhost_command``.
    """
    if run_kind == "database":
        argv = ["ansible-playbook", "-i", "localhost,", "-c", "local",
                f"{_JOB_DIR}/playbook.yml"]
        if has_vars:
            argv += ["--extra-vars", f"@{_JOB_DIR}/secret_vars.json"]
        return argv
    argv = [
        "ansible-playbook",
        "-i", f"{_JOB_DIR}/inventory.json",
        f"{_JOB_DIR}/playbook.yml",
        "--ssh-common-args",
        "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
    ]
    if has_key:
        argv += ["--private-key", f"{_JOB_DIR}/id_rsa"]
    if has_vars:
        argv += ["--extra-vars", f"@{_JOB_DIR}/secret_vars.json"]
    return argv


def run_ansible(payload: dict, policy: "Policy", emit, cancelled, job_id: str,
                dashboard) -> dict:
    """Run one Config-Management job on this host, in a one-shot container.

    The order of the first four steps is the security model, so it is worth reading as one
    thing rather than four:

    1. Read the FOUR scalars the envelope carries. There is nothing else in it — no playbook,
       no filename, no variable name, no credential.
    2. Resolve the address and check it against ``ansible.targets`` in the customer's
       policy.yaml. This happens before anything is fetched, so a refused target costs the
       dashboard nothing and tells it nothing.
    3. Ask policy which image a run of this kind uses. The dashboard never names an image.
    4. Only now fetch the sealed bundle, whose AAD binds it to the endpoint checked in (2).

    Nothing is written to this host's filesystem: the files go straight into the container
    through the archive API and die with it.
    """
    run_kind = str(payload.get("run_kind") or "")
    transport = str(payload.get("transport") or "")
    host = str(payload.get("target_host") or "")
    try:
        port = int(payload.get("target_port") or 0)
    except (TypeError, ValueError):
        port = 0
    if not host or not port:
        raise PolicyRefusal("this job names no target address, so there is nothing to run "
                            "against.")

    # (3) before (2)'s fetch, and before the network: an agent whose policy has no image for
    # this kind should say so rather than resolving a customer's DNS first.
    image = policy.ansible_image(run_kind)

    # (2) The resolved address is what gets checked AND what gets used, so the name cannot
    # resolve to one thing here and another at connect time.
    addresses = _resolve_all(host)
    if not addresses:
        raise PolicyRefusal(f"{host!r} does not resolve to any address from this agent.")
    ip = addresses[0]
    policy.check_ansible(ip, port)
    emit(f"target {host} resolved to {ip}:{port} and is allowed by policy.yaml")

    if MODE == "audit":
        # Audit mode promises the agent "logs every job it would run, in full, and executes
        # nothing". A playbook is the one job type where breaking that promise would be
        # unrecoverable, so the return is here — before the bundle is even fetched, so no
        # credential is released either.
        emit(f"AUDIT MODE — would run {run_kind} Config Management against {ip}:{port} "
             f"using {image}; nothing was fetched and nothing was executed")
        return {"exit_code": 0, "audit": True}

    # (4) The bundle, sealed and bound to this endpoint. Credentials are registered for
    # redaction inside this call, before anything below can emit.
    bundle, _scrub = dashboard.ansible_bundle(
        job_id, run_kind=run_kind, transport=transport, host=host, port=port)

    extra_vars = bundle.get("extra_vars") or {}
    _check_extra_vars(extra_vars)

    playbook = str(bundle.get("playbook") or "")
    if not playbook.strip():
        # Named here rather than left to ansible, which answers an empty playbook with
        # "Unable to parse … did not contain a list of plays" — a message that reads as a
        # broken playbook rather than as one that never arrived.
        raise PolicyRefusal(
            "the dashboard sent an empty playbook, so there is nothing to run. The asset it "
            "named is empty or could not be read out of storage.")

    files = {f"{_JOB_DIR.strip('/')}/playbook.yml": playbook.encode("utf-8")}

    if bundle.get("asset_name") and bundle.get("asset_b64"):
        files[f"{_JOB_DIR.strip('/')}/assets/{bundle['asset_name']}"] = \
            base64.b64decode(bundle["asset_b64"])

    has_key = False
    if run_kind == "vm":
        files[f"{_JOB_DIR.strip('/')}/inventory.json"] = _vm_inventory(
            ip=ip, port=port, transport=transport,
            login_user=str(bundle.get("login_user") or ""),
            winrm=bundle.get("winrm") or {}).encode("utf-8")
        if bundle.get("ssh_private_key"):
            files[f"{_JOB_DIR.strip('/')}/id_rsa"] = \
                str(bundle["ssh_private_key"]).encode("utf-8")
            has_key = True

    # Everything the play should see as DATA, in one file rather than on the command line so
    # no value lands in `ps` on this host. The connection material the inventory does not
    # carry — a WinRM/SSH password, a become password — goes here as the ansible_* names
    # ANSIBLE expects; that is this agent naming them, not the dashboard, which is the whole
    # reason `_check_extra_vars` runs against the dashboard's dict and not against this one.
    play_vars = dict(extra_vars)
    play_vars.update(bundle.get("db") or {})
    if bundle.get("login_password"):
        play_vars["ansible_password"] = bundle["login_password"]     # WinRM
        play_vars["ansible_ssh_pass"] = bundle["login_password"]     # SSH
    if bundle.get("become_password"):
        play_vars["ansible_become_password"] = bundle["become_password"]
    if play_vars:
        files[f"{_JOB_DIR.strip('/')}/secret_vars.json"] = \
            json.dumps(play_vars).encode("utf-8")

    argv = _ansible_argv(run_kind=run_kind, transport=transport, has_key=has_key,
                         has_vars=bool(play_vars))
    # `exec` so ansible-playbook becomes PID 1 and SIGTERM reaches it. Without it `sh` is
    # PID 1 and does not forward signals while waiting, so Cancel would do nothing until the
    # SIGKILL ten seconds later.
    command = "exec " + " ".join(_shell_quote(a) for a in argv)

    emit(f"running {run_kind} Config Management against {ip}:{port}")
    code = _run_ansible_sibling(
        policy, image=image, files=files, env=bundle.get("env") or {},
        command=command, emit=emit, cancelled=cancelled)

    meaning = _ANSIBLE_EXIT.get(code, "")
    if code == 0:
        emit("ansible-playbook completed successfully")
    elif code < 0:
        # Cancelled. `execute` reads `reporter.cancelled` and reports it; returning quietly
        # is what lets it, and a cancel must not be dressed up as a playbook failure.
        return {"exit_code": code, "cancelled": True}
    else:
        # Raised, not returned: the job must land as FAILED. Returning a non-zero exit code
        # in a result dict would complete the job green, which is the single worst outcome
        # for a config-management run — an operator reads "completed" and believes the host
        # was configured.
        raise PolicyRefusal(
            f"ansible-playbook exited {code}" + (f" — {meaning}" if meaning else ""))
    return {"exit_code": code}


def _shell_quote(value: str) -> str:
    """``shlex.quote`` without importing shlex — POSIX single-quote escaping.

    Written out because the argv it quotes is built from a credential-bearing bundle, and
    the rule is three lines: wrap in single quotes, and end/reopen the quoting around any
    single quote in the value.
    """
    if value and all(c.isalnum() or c in "@%+=:,./-_" for c in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


# The `ref` the dashboard seals this POV's deploy key under. Must match
# `web_dashboard/services/pov_gateway.SEAL_REF` exactly: it is part of the sealed
# envelope's AAD, so a mismatch fails as "did not authenticate" rather than as a typo.
GATEWAY_SEAL_REF = "pov-gateway-deploy-key"

# What the Gateway container is called on this host. GENERATED, never supplied — there is
# no field in the agent_gateway protocol through which a dashboard string could name
# something on this machine, which is the same rule the snapshot verb follows.
GATEWAY_CONTAINER = "pov-gateway"


def _gateway_container_id(name: str = GATEWAY_CONTAINER):
    """The id of the Gateway container on this host, or None.

    Matches on the exact name. Docker's filter is a substring match by default, so an
    unanchored one would also find `pov-gateway-old` — and stopping the wrong container is
    not a mistake this should be able to make.
    """
    status, body = _engine(
        "GET", "/containers/json?all=1&filters="
               + _quote(json.dumps({"name": [name]})))
    if status != 200 or not isinstance(body, list):
        return None
    for row in body:
        for raw in row.get("Names") or []:
            if str(raw).lstrip("/") == name:
                return row.get("Id")
    return None


def _remove_gateway(emit) -> bool:
    """Stop and remove the Gateway container. True if there was one."""
    container = _gateway_container_id()
    if not container:
        return False
    _engine("POST", f"/containers/{container}/stop?t=10")
    status, _ = _engine("DELETE", f"/containers/{container}?force=1")
    if status not in (204, 404):
        raise PolicyRefusal(
            f"the Gateway container would not be removed ({status}). Remove it by hand on "
            f"this host: docker rm -f {GATEWAY_CONTAINER}")
    emit(f"removed the {GATEWAY_CONTAINER} container")
    return True


def run_gateway(payload: dict, policy: "Policy", emit, cancelled, job_id: str = "",
                dashboard=None) -> dict:
    """Start (or remove) the BeyondTrust Gateway container on this host.

    The odd one out among these handlers, and worth saying why: every other job here is a
    one-shot that finishes. This one leaves a **long-lived, privileged container running**,
    because that is what a Gateway is. So the order below is the security model:

    1. Read the two scalars the envelope carries. There is nothing else in it — no image,
       no container name, no privileged flag, no network.
    2. Ask policy for the image, which also asserts the customer opted into privileged. A
       removal skips this: taking a container away must not need the grant that putting
       one there does, or a POV whose policy was narrowed becomes un-teardownable.
    3. Only then fetch the sealed deploy key — so a refused policy costs the dashboard
       nothing and releases no credential.

    The container is recreated rather than reused. A deploy key change is the whole reason
    to run this again, and a running container holds the key it started with.
    """
    action = str(payload.get("gateway_action") or "install")
    try:
        timeout = float(payload.get("timeout_s") or 120)
    except (TypeError, ValueError):
        timeout = 120.0

    if action == "remove":
        if MODE == "audit":
            emit("AUDIT MODE — would remove the Gateway container; nothing was changed")
            return {"removed": False, "audit": True}
        removed = _remove_gateway(emit)
        return {"removed": removed,
                "detail": "removed" if removed else "there was no Gateway container"}

    # (2) Before the network and before the fetch.
    image = policy.gateway_image()

    if MODE == "audit":
        # Audit mode promises this agent "logs every job it would run, in full, and
        # executes nothing". Returning here means no deploy key is released either.
        emit(f"AUDIT MODE — would start {image} as {GATEWAY_CONTAINER} (privileged); "
             f"nothing was fetched and nothing was started")
        return {"started": False, "audit": True}

    if dashboard is None:
        raise PolicyRefusal("no dashboard channel, so the deploy key cannot be fetched")
    # (3) Registered for redaction inside this call, before anything below can emit.
    deploy_key = dashboard.gateway_deploy_key(job_id)
    if not deploy_key.strip():
        raise PolicyRefusal(
            "the dashboard sent an empty Gateway deploy key. Paste the key from the "
            "Gateway you created in PRA onto the POV and try again.")

    # Replace whatever is there. Not an optimisation to skip this: the reason to re-run is
    # usually a new key, and a running container keeps the one it started with.
    if _remove_gateway(emit):
        emit("replaced the previous Gateway container")

    if cancelled():
        raise PolicyRefusal("cancelled before the Gateway was started")

    spec = {
        "Image": image,
        # In Env rather than argv: argv shows in `ps` on the host.
        "Env": [f"DEPLOY_KEY={deploy_key}"],
        "HostConfig": {
            # Every field here is a CONSTANT. Nothing in the job can reach any of them.
            "RestartPolicy": {"Name": "unless-stopped"},
            "Binds": [],
            # The one privileged container this agent will ever start, and only because
            # the customer wrote `privileged: true` in their own file. See
            # Policy.gateway_image for what breaks without it.
            "Privileged": True,
            "NetworkMode": policy.gateway_network,
            "SecurityOpt": [],
        },
    }
    status, body = _engine("POST",
                           f"/containers/create?name={_quote(GATEWAY_CONTAINER)}",
                           spec)
    if status == 404:
        raise PolicyRefusal(
            f"the Gateway image {image!r} is not present on this host. Pull it first: "
            f"docker pull {image}")
    if status not in (200, 201):
        raise PolicyRefusal(f"could not create the Gateway container ({status}): "
                            f"{str(body)[:200]}")
    container = body.get("Id")

    status, _ = _engine("POST", f"/containers/{container}/start")
    if status not in (204, 304):
        raise PolicyRefusal(f"the Gateway container would not start ({status})")
    emit(f"started {image} as {GATEWAY_CONTAINER}")

    # A short settle, then read the state back. Deliberately NOT a registration check:
    # whether PRA accepted this node is a question only the dashboard can ask, against the
    # tenant's own API, and an agent that guessed at it would report green for a Gateway
    # with a bad key. All this proves is that the container did not exit immediately —
    # which is the failure a wrong image or a missing /dev/net/tun produces.
    deadline = time.monotonic() + min(timeout, 60.0)
    state = {}
    while time.monotonic() < deadline:
        if cancelled():
            raise PolicyRefusal("cancelled while the Gateway was starting")
        status, info = _engine("GET", f"/containers/{container}/json")
        state = (info or {}).get("State") or {} if status == 200 else {}
        if not state.get("Running"):
            break
        time.sleep(3)

    if not state.get("Running"):
        raise PolicyRefusal(
            f"the Gateway container exited (code {state.get('ExitCode')}). Check "
            f"`docker logs {GATEWAY_CONTAINER}` on this host — a wrong image tag and a "
            f"kernel without /dev/net/tun both look like this.")

    emit("the Gateway container is running; the dashboard will confirm registration "
         "against the PRA tenant")
    return {"started": True, "container": GATEWAY_CONTAINER, "image": image}


HANDLERS = {"agent_discover": run_discovery,
            "agent_hypervisor": run_hypervisor,
            "agent_ansible": run_ansible,
            "agent_gateway": run_gateway}


# ── Job execution ─────────────────────────────────────────────────────────────

class Reporter:
    """Buffers Live Output and flushes it on a timer, so a fast scan does not make one
    HTTP request per line."""

    def __init__(self, dashboard: Dashboard, job_id: str):
        self.dashboard = dashboard
        self.job_id = job_id
        self.lines = []
        self.cancelled = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def emit(self, line: str) -> None:
        # Redacted before it reaches either destination, including the local log — a
        # credential this job fetched has no business in a line somebody pasted into a
        # ticket, wherever that line came from.
        line = self.dashboard.redact(line)
        log.info("[%s] %s", self.job_id[:8], line)
        with self._lock:
            self.lines.append(str(line)[:8000])

    def start(self):
        self._thread.start()
        return self

    def _loop(self):
        while not self._stop.wait(2.0):
            self.flush()

    def flush(self, pct: int = 50, message: str = "scanning"):
        with self._lock:
            batch, self.lines = self.lines[:500], self.lines[500:]
        try:
            if batch:
                self.dashboard.logs(self.job_id, batch)
            if self.dashboard.heartbeat(self.job_id, pct, message):
                self.cancelled = True
        except Exception as exc:  # noqa: BLE001 — a reporting hiccup must not kill work
            log.warning("could not report progress: %s", exc)

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)
        self.flush(90, "finishing")


def execute(dashboard: Dashboard, policy: Policy, job: dict) -> None:
    job_id = job["job_id"]
    job_type = job.get("job_type") or ""
    reporter = Reporter(dashboard, job_id).start()
    try:
        # Two gates, both closed by default. The handler table is a dict lookup, not a
        # dispatch on a wire string, so an unknown type cannot reach any code path.
        if job_type not in policy.job_types:
            raise PolicyRefusal(f"policy.yaml does not permit job type '{job_type}'")
        handler = HANDLERS.get(job_type)
        if handler is None:
            raise PolicyRefusal(f"this agent has no handler for job type '{job_type}'")

        if MODE == "audit":
            reporter.emit(f"AUDIT MODE — would run {job_type}; nothing will be executed")

        reporter.emit(f"Starting {job_type} (policy {policy.digest[:12]}…)")
        result = handler(job.get("payload") or {}, policy, reporter.emit,
                         lambda: reporter.cancelled, job_id, dashboard)
        reporter.stop()

        if reporter.cancelled:
            dashboard.complete(job_id, status="failed", error="Cancelled by the operator.")
            return
        result["audit_mode"] = MODE == "audit"
        dashboard.complete(job_id, status="completed", result=result)
        log.info("job %s completed: %d finding(s)", job_id[:8],
                 len(result.get("findings") or []))
    except PolicyRefusal as exc:
        # Refusals are logged locally in full. The local log is the record the
        # dashboard cannot rewrite, which matters most when the dashboard is the thing
        # behaving badly.
        log.warning("REFUSED job %s: %s", job_id, exc)
        reporter.stop()
        dashboard.complete(job_id, status="failed", error=f"Agent policy refused: {exc}")
    except Exception as exc:  # noqa: BLE001
        log.exception("job %s failed", job_id)
        reporter.stop()
        # Type name plus a shortened message rather than the raw `str(exc)`. This string
        # lands in the job row and is the ONLY text a failed job renders, and an arbitrary
        # library's exception message is not something this agent can vouch for — a driver
        # that prints its connection string, or an XML-RPC fault echoing the request it
        # failed on, both arrive here. `Dashboard._scrub` removes any credential this job
        # fetched on the way out; the type name is what keeps the line diagnosable.
        dashboard.complete(job_id, status="failed",
                           error=f"{type(exc).__name__}: {str(exc)[:400]}")
    finally:
        # The credential is scoped to the job, so the scrub set is too. Nothing here
        # pretends to wipe it from memory — a Python str cannot be zeroed, and claiming
        # otherwise would be theatre; the bound is scope, not erasure.
        dashboard.release_secrets()


# ── Main loop ─────────────────────────────────────────────────────────────────

POLICY: Optional[Policy] = None


def _preflight() -> None:
    if not DASHBOARD_URL:
        raise AgentFatal("DASHBOARD_URL is not set.")
    scheme = urlparse(DASHBOARD_URL).scheme
    if scheme != "https":
        if not INSECURE_TLS:
            raise AgentFatal(
                f"DASHBOARD_URL is {scheme}://, and this agent will not send signed "
                f"requests over an unencrypted channel. Terminate TLS in front of the "
                f"dashboard, or set AGENT_INSECURE_TLS=1 for a throwaway lab.")
        log.warning("!! DASHBOARD_URL is not https and AGENT_INSECURE_TLS is set — "
                    "traffic is readable in transit. Never do this outside a lab.")
    if INSECURE_TLS:
        log.warning("!! TLS verification is disabled (AGENT_INSECURE_TLS=1)")
    if MODE not in ("normal", "audit"):
        raise AgentFatal(f"AGENT_MODE must be 'normal' or 'audit', not '{MODE}'")
    _check_state_dir_writable()


def _check_state_dir_writable() -> None:
    """Fail before enrolment if the identity cannot be saved afterwards.

    Enrolment spends the code on the SERVER — a successful ``/enroll`` NULLs
    ``enroll_code_hash`` — and only then does the agent write ``identity.json``. So a
    state directory that is not writable does not merely fail: it fails *after* the
    single-use code has been consumed, with a raw ``PermissionError`` traceback and exit
    1. Under ``--restart unless-stopped`` that becomes a crash loop in which every
    restart re-enrols with a dead code, and ten failures inside fifteen minutes trip the
    dashboard's per-address enrolment throttle, locking the host out for fifteen more.

    Checking here turns all of that into one clear message with the code untouched. The
    check is a real create-and-delete rather than ``os.access``, because ``access`` tests
    the *permission bits* and answers wrongly on a read-only filesystem, which is exactly
    the case ``--read-only`` without a mounted volume produces.
    """
    probe = os.path.join(STATE_DIR, ".write-probe")
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("")
        os.unlink(probe)
    except OSError as exc:
        raise AgentFatal(
            f"The state directory {STATE_DIR} is not writable ({exc}). The agent stores "
            f"its identity there after enrolling, and the enrolment code is spent the "
            f"moment the dashboard accepts it — so starting without a writable volume "
            f"would burn the code and leave nothing to show for it. Mount one "
            f"(-v dashboard_agent_state:{STATE_DIR}) and make sure it is writable by "
            f"uid 10001, then start again. The code you have is still good.")


_SEAL_USAGE = """usage:
  seal --host <hypervisor address>        seal a connection's `password_sealed`
  seal --api-url <password safe url>      seal passwordsafe.yaml's `client_secret_sealed`

Mount the SAME state volume the agent runs with, or the key is thrown away with the
container and the value can never be opened:

  docker run --rm -it -v dashboard_agent_state:%(state)s \\
      chrweav/dashboard-agent:latest seal --host vcenter.lab.internal

The address matters: the sealed value is bound to it, so it cannot be moved onto an entry
pointing somewhere else. It must match the entry's `host:` (or `api_url:`), and changing that
address later means sealing the credential again. Seal once per connection ENTRY, not once
per credential — two entries sharing one password need two sealed values.

`-it` is not optional. Without `-i` the container has no stdin, and this reads an empty
value. A piped value has leading and trailing whitespace stripped; a typed one does not."""


def _seal_command(argv: list) -> int:
    """``seal`` — encrypt one credential against this host's key and print the result.

    A subcommand that PRINTS rather than a startup pass that rewrites connections.yaml,
    which is what "the agent encrypts it" would most obviously mean. Three reasons, and the
    first is the one that decides it: that file is mounted `:ro,Z` in every documented
    install command, and an agent able to write it is an agent able to rewrite its own
    `host:` and re-aim the connection — the single property the file exists to hold, and the
    stated difference between this design and a proxy. Also, a PyYAML round-trip would
    discard every comment in a file that is mostly comments; and `os.replace` onto a
    bind-mounted *file* does not work, because the mount is on the inode, so the rewrite
    could not even be atomic.

    Reads the value from a TTY without echo when there is one, and from stdin when there is
    not. Prompting is the point rather than a nicety: on Windows an editor or a prompt is
    the only way to keep the value out of the shell's on-disk history, which is already why
    the enrolment code has a file form.
    """
    host = ""
    purpose = ""
    flags = 0
    i = 0
    while i < len(argv):
        value = argv[i + 1] if i + 1 < len(argv) else ""
        if argv[i] == "--host" and value:
            host, purpose, flags = value, LOCAL_PURPOSE_PASSWORD, flags + 1
        elif argv[i] == "--api-url" and value:
            host, purpose, flags = (ps_seal_host(value), LOCAL_PURPOSE_PS_CLIENT,
                                    flags + 1)
        else:
            log.error("%s", _SEAL_USAGE % {"state": STATE_DIR})
            return 2
        i += 2
    if flags != 1 or not host:
        log.error("%s", _SEAL_USAGE % {"state": STATE_DIR})
        return 2

    what = ("hypervisor password" if purpose == LOCAL_PURPOSE_PASSWORD
            else "Password Safe client secret")
    try:
        if sys.stdin.isatty():
            value = getpass.getpass(f"{what} for {host}: ")
        else:
            value = sys.stdin.read().strip()
    except (EOFError, KeyboardInterrupt):
        log.error("nothing was sealed.")
        return 2

    try:
        token = local_seal(value, host=host, purpose=purpose)
    except LocalSealError as exc:
        log.error("%s", exc)
        return 2

    key = local_key()
    log.info("sealed for %s with key %s. Paste the line below into %s and delete the "
             "plaintext:", host, _key_id(key),
             "connections.yaml as `password_sealed:`"
             if purpose == LOCAL_PURPOSE_PASSWORD
             else "passwordsafe.yaml as `client_secret_sealed:`")
    # stdout, alone, so `seal ... > value.txt` yields the token and nothing else. Every
    # other line this command emits goes to stderr for exactly that reason.
    print(token)
    return 0


def main() -> int:
    global POLICY
    # Before _preflight, which hard-requires DASHBOARD_URL that `seal` has no use for — and
    # on stderr, so the token is the only thing on stdout.
    if sys.argv[1:2] == ["seal"]:
        logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
        return _seal_command(sys.argv[2:])

    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    if sys.argv[1:]:
        log.error("unknown argument %r. This container takes no arguments except the "
                  "`seal` subcommand; everything else is configured by environment "
                  "variable. See runners/agent/README.md.", sys.argv[1])
        return 2
    try:
        _preflight()
        POLICY = Policy.load(POLICY_FILE)
    except AgentFatal as exc:
        log.error("%s", exc)
        return 2

    log.info("agent %s starting; dashboard=%s policy=%s mode=%s",
             AGENT_VERSION, DASHBOARD_URL, POLICY.digest[:12], MODE)
    log.info("policy permits %d network(s) and job types %s",
             len(POLICY.allow), sorted(POLICY.job_types))

    dashboard = Dashboard(DASHBOARD_URL)
    dashboard.identity = Identity.load()
    if dashboard.identity is None:
        try:
            code = enrollment_code()
            if not code:
                raise AgentFatal(
                    "No stored identity, and neither AGENT_ENROLLMENT_CODE nor "
                    "AGENT_ENROLLMENT_CODE_FILE is set. Register an agent in the "
                    "dashboard and pass the code it gives you.")
            dashboard.identity = dashboard.enroll(code)
        except AgentFatal as exc:
            log.error("%s", exc)
            return 2

    poll = float(os.environ.get("AGENT_POLL_INTERVAL", "5"))
    backoff = poll
    while True:
        try:
            job = dashboard.lease()
            backoff = poll
            if job:
                execute(dashboard, POLICY, job)
                continue
        except AgentFatal as exc:
            log.error("%s", exc)
            return 2
        except PolicyRefusal as exc:
            # A bad envelope is not a transient error — something is wrong at the other
            # end. Back off hard rather than hammering, and keep saying so.
            log.error("REFUSED a job envelope: %s", exc)
            backoff = min(backoff * 2, 300)
        except Throttled as exc:
            # Honour the server's interval instead of this process's own doubling. Never
            # shorter than the poll interval, and the jitter below is what keeps a fleet
            # that was throttled together from returning in lockstep and doing it again.
            log.warning("the dashboard asked us to slow down; waiting %.0fs",
                        exc.retry_after)
            backoff = max(exc.retry_after, poll)
        except requests.RequestException as exc:
            log.warning("dashboard unreachable (%s); retrying in %.0fs", exc, backoff)
            backoff = min(backoff * 2, 300)
        # Jittered so a fleet restarted together does not poll in lockstep.
        time.sleep(backoff * (0.8 + random.random() * 0.4))


if __name__ == "__main__":
    sys.exit(main())
