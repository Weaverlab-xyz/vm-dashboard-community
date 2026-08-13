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
import hashlib
import ipaddress
import json
import logging
import os
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
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)

# 2.x scans for HYPERVISORS. 1.x scanned for Kubernetes API servers and database
# listeners, and handed a 2.x scan request it would probe nothing, complete green and
# report zero findings — indistinguishable from a clean network. The dashboard refuses
# to queue for a 1.x agent (api/agent.queue_discovery), which is why the major matters
# and why the lease body reports it on every poll rather than only at enrolment.
AGENT_VERSION = "2.0.0"

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


class Policy:
    """The customer's allow-list. Fails closed, always.

    Matching happens on the **resolved IP** at connect time, not on the hostname the
    dashboard asked for. A hostname check alone is defeated by DNS rebinding: the name
    resolves inside the allowed range when validated and somewhere else when connected.
    """

    def __init__(self, allow: list, deny: list, job_types: set, limits: dict,
                 digest: str, connection_verbs: dict = None, sibling: dict = None,
                 loopback_connections: set = None):
        self.allow = allow            # [(network, {ports} or None)]
        self.deny = deny              # [network]
        self.job_types = job_types
        self.limits = limits
        self.digest = digest
        self.connection_verbs = connection_verbs or {}   # {connection name: {verb}}
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

    @classmethod
    def load(cls, path: str) -> "Policy":
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError as exc:
            raise AgentFatal(_policy_unreadable(path, exc))
        try:
            doc = yaml.safe_load(raw) or {}
        except yaml.YAMLError as exc:
            raise AgentFatal(f"policy.yaml is not valid YAML: {exc}")
        if not isinstance(doc, dict):
            raise AgentFatal("policy.yaml must be a mapping at the top level.")

        allow = []
        for entry in doc.get("targets") or []:
            if not isinstance(entry, dict):
                continue
            ports = entry.get("ports")
            ports = {int(p) for p in ports} if isinstance(ports, (list, tuple)) else None
            if entry.get("cidr"):
                try:
                    allow.append((ipaddress.ip_network(str(entry["cidr"]), strict=False), ports))
                except ValueError as exc:
                    raise AgentFatal(f"policy.yaml: bad cidr {entry['cidr']!r}: {exc}")
            elif entry.get("fqdn"):
                # Resolved once at load and pinned to the resulting addresses, so the
                # allow-list is always expressed in IPs by the time it is consulted.
                for addr in _resolve_all(str(entry["fqdn"])):
                    allow.append((ipaddress.ip_network(addr + "/32" if ":" not in addr
                                                       else addr + "/128"), ports))
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
        return cls(allow, deny, job_types, limits,
                   hashlib.sha256(raw).hexdigest(), verbs, sibling, loopback)

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

    def _url(self, path: str) -> str:
        url = f"{self.base}{path}"
        # An SSRF-shaped bug elsewhere in this process must not be able to turn the
        # agent into an open proxy into the customer's network.
        if urlparse(url).netloc != self._host:
            raise AgentFatal(f"refusing to call a host other than {self._host}")
        return url

    def _request(self, method: str, path: str, payload: dict, *, signed: bool = True):
        body = serialize(payload)
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


def run_discovery(payload: dict, policy: Policy, emit, cancelled, job_id: str = "") -> dict:
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
        secret = doc.get("client_secret") or ""
        if doc.get("client_secret_file"):
            try:
                secret = _read_secret_file(doc["client_secret_file"])
            except ValueError:
                raise PolicyRefusal(_secret_file_undecodable(
                    "client_secret_file", str(doc["client_secret_file"])))
            except OSError as exc:
                raise PolicyRefusal(f"cannot read client_secret_file: {exc}")
        return cls(str(doc["api_url"]), str(doc.get("client_id") or ""), str(secret),
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

    def checkout(self, account_id: int, duration_min: int = 30) -> tuple:
        """(request_id, credential). `ConflictOption=reuse` returns an existing active
        request instead of a 409 — the same reason btapi_service passes `-c-op reuse`,
        where a 409 body was once mis-parsed as a request id."""
        resp = self._call("POST", "Requests", json={
            "AccessType": "View", "SystemID": 0, "AccountID": int(account_id),
            "DurationMinutes": int(duration_min), "Reason": "vm-dashboard agent",
            "ConflictOption": "reuse"})
        if resp.status_code not in (200, 201):
            raise PolicyRefusal(
                f"Password Safe refused the credential request ({resp.status_code}). "
                f"The API identity needs the Requestor role and an access policy "
                f"granting View on a Smart Rule containing this account.")
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
        except Exception:  # noqa: BLE001
            log.debug("Password Safe check-in failed for request %s", request_id)


def _secret_for(conn: dict, checkins: list = None) -> str:
    """The connection's password, by whichever of the three means it declares.

    Precedence is deliberate: `ps_managed_account` first, because an operator who has
    moved a connection to Password Safe should not silently keep using a stale literal
    left in the file below it. Then `password_file` (so a Docker/Podman secret can be
    mounted), then an inline `password`.

    When `checkins` is provided, a just-in-time checkout appends its request id so the
    caller can check the credential back in when the job finishes. Without it the
    request simply expires on its own duration — which is safe, just untidy.
    """
    account = conn.get("ps_managed_account")
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
        return credential

    path = conn.get("password_file")
    if path:
        try:
            return _read_secret_file(path)
        except ValueError:
            raise PolicyRefusal(
                f"connection {conn.get('name')!r}: "
                + _secret_file_undecodable("password_file", str(path)))
        except OSError as exc:
            raise PolicyRefusal(
                f"connection {conn.get('name')!r}: cannot read password_file {path}: {exc}")
    return str(conn.get("password") or "")


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
        raise PolicyRefusal(
            f"{parsed.hostname} answered {exc.code} for {method} {parsed.path}")
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
    out = []
    for vm in rows:
        out.append({"vm_id": vm.get("vm"), "name": vm.get("name"),
                    "power_state": vm.get("power_state"),
                    "vcpus": vm.get("cpu_count"), "mem_mib": vm.get("memory_size_MiB"),
                    "vm_type": "vm"})
    return {"vms": out, "next_cursor": "", "complete": True, "scanned": len(out)}


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
    out = []
    for vm in body.get("data") or []:
        out.append({"vm_id": str(vm.get("vmid")), "name": vm.get("name"),
                    "power_state": vm.get("status"), "vcpus": vm.get("maxcpu"),
                    "mem_mib": int((vm.get("maxmem") or 0) / (1024 * 1024)),
                    "scope": vm.get("node"), "vm_type": vm.get("type")})
    emit(f"read {len(out)} guest(s) from Proxmox at {host}")
    return {"vms": out, "next_cursor": "", "complete": True, "scanned": len(out)}


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
            "complete": complete, "scanned": len(out)}


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
    return {"vms": out, "next_cursor": "", "complete": True, "scanned": len(out)}


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
    return {"vms": out, "next_cursor": "", "complete": True, "scanned": len(out)}


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
# stdlib cannot do, so Hyper-V stays dashboard-direct.
_POWER = {
    "power_on":    {"proxmox": "start",    "nutanix": "ON",  "xcpng": "VM.start"},
    "power_off":   {"proxmox": "stop",     "nutanix": "OFF", "xcpng": "VM.hard_shutdown"},
    "power_reset": {"proxmox": "reset",    "nutanix": "OFF", "xcpng": "VM.hard_reboot"},
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
    action = {"power_on": "start", "power_off": "stop",
              "power_reset": "reset", "restart": "reset"}[verb]
    _hv_request(conn, "POST", f"{base}/api/vcenter/vm/{vm}/power?action={action}",
                headers={"vmware-api-session-id": token})
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


def _engine(method: str, path: str, body=None, timeout: float = 60.0):
    """One Docker Engine API call. Returns (status, parsed-or-raw-body)."""
    conn = _UnixHTTP(DOCKER_SOCKET, timeout=timeout)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
    except OSError as exc:
        raise PolicyRefusal(
            f"cannot reach the Docker socket at {DOCKER_SOCKET}: {exc}. The sibling "
            f"runner needs it mounted — see docker-compose.sibling.yml.")
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    try:
        return resp.status, json.loads(raw.decode("utf-8", "replace")) if raw else {}
    except ValueError:
        return resp.status, raw.decode("utf-8", "replace")


def _demux(raw: bytes) -> str:
    """Strip Docker's 8-byte stream framing from a non-TTY log stream."""
    out, i = [], 0
    while i + 8 <= len(raw):
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        out.append(raw[i + 8:i + 8 + size])
        i += 8 + size
    return b"".join(out).decode("utf-8", "replace") if out else raw.decode("utf-8", "replace")


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
        status, raw = _engine(
            "GET", f"/containers/{container}/logs?stdout=1&stderr=1")
        text = _demux(raw.encode() if isinstance(raw, str) else raw)
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
    return env


def _via_sibling(conn, payload, policy, emit, verb, kind, checkins=None):
    host, port = _conn_endpoint(conn, 5985 if kind == "hyperv" else 443)
    # The policy gate applies exactly as it does to an in-process call: the sibling is
    # a different process, not a different trust boundary.
    _check_endpoint(policy, host, port)
    env = _sibling_env(conn, kind, verb, payload, _secret_for(conn, checkins))
    return _run_sibling(policy, env, emit, lambda: False)


def run_hypervisor(payload: dict, policy: Policy, emit, cancelled, job_id: str = "") -> dict:
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
    checkins: list = []
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


HANDLERS = {"agent_discover": run_discovery,
            "agent_hypervisor": run_hypervisor}


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
                         lambda: reporter.cancelled, job_id)
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
        dashboard.complete(job_id, status="failed", error=str(exc)[:1000])


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


def main() -> int:
    global POLICY
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)-7s %(message)s")
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
