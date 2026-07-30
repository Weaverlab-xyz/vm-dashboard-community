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
import hashlib
import ipaddress
import json
import logging
import os
import random
import re
import socket
import ssl
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import urlparse

import requests
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)

AGENT_VERSION = "1.0.0"

log = logging.getLogger("agent")

# ── Configuration ─────────────────────────────────────────────────────────────

DASHBOARD_URL = (os.environ.get("DASHBOARD_URL") or "").rstrip("/")
ENROLLMENT_CODE = os.environ.get("AGENT_ENROLLMENT_CODE", "").strip()
STATE_DIR = os.environ.get("AGENT_STATE_DIR", "/var/lib/dashboard-agent")
POLICY_FILE = os.environ.get("AGENT_POLICY_FILE", "/etc/dashboard-agent/policy.yaml")
CA_BUNDLE = os.environ.get("AGENT_CA_BUNDLE", "").strip()
INSECURE_TLS = os.environ.get("AGENT_INSECURE_TLS", "").strip() in ("1", "true", "yes")
# "audit" logs in full what it WOULD do and executes nothing. Run this for a couple of
# weeks and diff intent against policy before letting it act — in practice this is what
# gets an agent approved by a security team.
MODE = (os.environ.get("AGENT_MODE") or "normal").strip().lower()
KUBECONFIG = os.environ.get("KUBECONFIG", "/etc/dashboard-agent/kubeconfig")

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

class Policy:
    """The customer's allow-list. Fails closed, always.

    Matching happens on the **resolved IP** at connect time, not on the hostname the
    dashboard asked for. A hostname check alone is defeated by DNS rebinding: the name
    resolves inside the allowed range when validated and somewhere else when connected.
    """

    def __init__(self, allow: list, deny: list, job_types: set, limits: dict, digest: str):
        self.allow = allow            # [(network, {ports} or None)]
        self.deny = deny              # [network]
        self.job_types = job_types
        self.limits = limits
        self.digest = digest

    @classmethod
    def load(cls, path: str) -> "Policy":
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError as exc:
            raise AgentFatal(
                f"Cannot read the policy file at {path}: {exc}. The agent refuses all "
                f"work without one — mount it read-only and restart.")
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
        return cls(allow, deny, job_types, limits,
                   hashlib.sha256(raw).hexdigest())

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
        with os.fdopen(fd, "w") as fh:
            json.dump({"agent_id": self.agent_id, "private_key": self.private_b64,
                       "dashboard_public_key": self.dashboard_public,
                       "audience": self.audience}, fh)
        os.replace(tmp, _IDENTITY_FILE)

    @staticmethod
    def load() -> Optional["Identity"]:
        try:
            with open(_IDENTITY_FILE) as fh:
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
        if resp.status_code != 200:
            raise AgentFatal(f"enrolment refused ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()
        identity = Identity(data["agent_id"], private_b64,
                            data["dashboard_public_key"], data["audience"])
        identity.save()
        log.info("enrolled as %s (%s)", data.get("name"), data["agent_id"])
        return identity

    def lease(self) -> Optional[dict]:
        resp = self._request("POST", "/api/agent/lease", {})
        if resp.status_code == 401:
            raise AgentFatal(
                "the dashboard rejected this agent's signature — it was probably "
                "revoked. Re-enrol with a fresh code.")
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


def probe_postgres(sock: socket.socket) -> Optional[dict]:
    """PostgreSQL SSLRequest. The server answers a single byte, 'S' or 'N', before any
    authentication happens — enough to identify the engine and nothing more."""
    sock.sendall(struct.pack("!II", 8, 80877103))
    reply = sock.recv(1)
    if reply in (b"S", b"N"):
        # The version is only available after a StartupMessage, which is the beginning
        # of an authentication attempt. Not worth a locked-out service account.
        return {"engine": "postgres", "server_version": "", "tls": reply == b"S"}
    return None


def probe_mysql(sock: socket.socket) -> Optional[dict]:
    """MySQL/MariaDB send their greeting first, and the version string is in the clear
    ahead of any credential exchange."""
    data = sock.recv(256)
    if len(data) < 6:
        return None
    payload_len = int.from_bytes(data[0:3], "little")
    if not (0 < payload_len <= 1024) or data[4] != 10:   # protocol version 10
        return None
    end = data.find(b"\x00", 5)
    if end < 0:
        return None
    version = data[5:end].decode("utf-8", "replace")
    engine = "mariadb" if "mariadb" in version.lower() else "mysql"
    return {"engine": engine, "server_version": version}


def probe_mssql(sock: socket.socket) -> Optional[dict]:
    """TDS PRELOGIN — the pre-authentication handshake packet. The response carries the
    server version in its VERSION option."""
    options = b"\x00\x00\x06\x00\x06\xff"          # VERSION token, then terminator
    payload = options + b"\x00" * 6
    header = struct.pack("!BBHHBB", 0x12, 0x01, 8 + len(payload), 0, 0, 0)
    sock.sendall(header + payload)

    reply = sock.recv(512)
    if len(reply) < 9 or reply[0] != 0x04:          # 0x04 = tabular result
        return None
    body = reply[8:]
    version = ""
    idx = 0
    while idx + 5 <= len(body) and body[idx] != 0xFF:
        token = body[idx]
        offset = int.from_bytes(body[idx + 1:idx + 3], "big")
        length = int.from_bytes(body[idx + 3:idx + 5], "big")
        if token == 0x00 and length >= 6 and offset + 6 <= len(body):
            major, minor = body[offset], body[offset + 1]
            build = int.from_bytes(body[offset + 2:offset + 4], "big")
            version = f"{major}.{minor}.{build}"
            break
        idx += 5
    return {"engine": "sqlserver", "server_version": version}


def _tns_connect_packet(connect_data: bytes) -> bytes:
    """A TNS CONNECT packet: 8-byte packet header, a 50-byte CONNECT body, then the
    connect-data string at offset 58. Built field by field because the offsets are
    load-bearing — ``connect_data_offset`` below must equal the real one or the
    listener drops the packet without replying."""
    body = struct.pack(
        "!HHHHHHHH",
        0x013A,      # version
        0x012C,      # minimum compatible version
        0x0000,      # service options
        0x0800,      # session data unit
        0x7FFF,      # transport data unit
        0x860E,      # NT protocol characteristics
        0x0000,      # line turnaround
        0x0001,      # value of 1 in hardware, for byte-order detection
    )
    body += struct.pack("!HH", len(connect_data), 58)   # data length, data offset
    body += struct.pack("!I", 0)                        # max receivable connect data
    body += struct.pack("!BB", 0, 0)                    # connect flags 0/1
    body += b"\x00" * 24                                # trace/xactions, pad to offset 58
    header = struct.pack("!HHBBH", 8 + len(body) + len(connect_data), 0, 1, 0, 0)
    return header + body + connect_data


def probe_oracle(sock: socket.socket) -> Optional[dict]:
    """Oracle TNS. A listener answers a CONNECT with Accept, Refuse or Resend — all
    three positively identify it, and none of them is a login attempt."""
    try:
        sock.sendall(_tns_connect_packet(b"(CONNECT_DATA=(COMMAND=ping))"))
        reply = sock.recv(64)
    except OSError:
        return None
    # Byte 4 of the reply header is the TNS packet type: 2=Accept, 4=Refuse, 11=Resend.
    if len(reply) >= 5 and reply[4] in (2, 4, 11):
        return {"engine": "oracle", "server_version": ""}
    return None


_DB_PROBES = {5432: probe_postgres, 3306: probe_mysql, 1433: probe_mssql,
              1521: probe_oracle}


def probe_database(ip: str, port: int, timeout: float) -> Optional[dict]:
    prober = _DB_PROBES.get(port)
    if prober is None:
        return None
    sock = _connect(ip, port, timeout)
    if sock is None:
        return None
    try:
        sock.settimeout(timeout)
        found = prober(sock)
    except (OSError, struct.error, ValueError):
        return None
    finally:
        sock.close()
    if not found:
        return None
    return {"kind": "database", "host": ip, "port": port,
            "suggested_name": f"{found['engine']}-{ip.replace('.', '-')}", **found}


_GITVERSION = re.compile(r'"gitVersion"\s*:\s*"([^"]+)"')


def probe_kubernetes(ip: str, port: int, timeout: float) -> Optional[dict]:
    """A Kubernetes apiserver answers GET /version with a gitVersion, or with 401/403 —
    and a refusal is itself a positive identification, since almost nothing else on
    6443 rejects an anonymous request that way."""
    context = ssl.create_default_context()
    # Verification off on purpose: we are identifying an unknown host, and its
    # certificate is almost certainly signed by a cluster CA we have never seen. The
    # certificate is read as EVIDENCE, not trusted for anything.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    sock = _connect(ip, port, timeout)
    if sock is None:
        return None
    try:
        sock.settimeout(timeout)
        with context.wrap_socket(sock, server_hostname=None) as tls:
            der = tls.getpeercert(binary_form=True)
            tls.sendall(b"GET /version HTTP/1.1\r\nHost: " + ip.encode() +
                        b"\r\nAccept: application/json\r\nConnection: close\r\n\r\n")
            chunks, total = [], 0
            while total < 65536:
                block = tls.recv(8192)
                if not block:
                    break
                chunks.append(block)
                total += len(block)
            raw = b"".join(chunks).decode("utf-8", "replace")
    except (OSError, ssl.SSLError):
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass

    status = raw.split(" ")[1] if raw.startswith("HTTP/") and " " in raw else ""
    match = _GITVERSION.search(raw)
    if not match and status not in ("401", "403"):
        return None

    version = match.group(1) if match else ""
    return {"kind": "k8s", "host": ip, "port": port,
            "api_server": f"https://{ip}:{port}",
            "server_version": version,
            "distro": _distro_of(version),
            "tls_cn": _cert_cn(der),
            "suggested_name": f"k8s-{ip.replace('.', '-')}"}


def _distro_of(version: str) -> str:
    lowered = (version or "").lower()
    for marker, name in (("+k3s", "k3s"), ("+rke2", "rke2"), ("-eks", "eks"),
                         ("-gke", "gke"), ("+aks", "aks")):
        if marker in lowered:
            return name
    return "kubeadm" if version else ""


def _cert_cn(der: Optional[bytes]) -> str:
    if not der:
        return ""
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        cert = x509.load_der_x509_certificate(der)
        attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
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


def run_discovery(payload: dict, policy: Policy, emit, cancelled) -> dict:
    """Sweep the requested scope with unauthenticated probes and return findings."""
    scan_kind = payload.get("scan_kind") or "both"
    timeout = float(payload.get("timeout_s") or 3)
    ports = payload.get("ports") or {}
    k8s_ports = list(ports.get("k8s") or [6443, 8443, 443])
    db_ports = [p for p in (ports.get("database") or [5432, 3306, 1433, 1521])
                if p in _DB_PROBES]
    workers = max(1, min(int(payload.get("concurrency") or 32),
                         policy.limit("max_concurrency", 128)))

    findings = []
    if payload.get("use_local_kubeconfig") and scan_kind in ("k8s", "both"):
        findings.extend(_from_kubeconfig(policy, emit))

    hosts = _expand(payload, policy, emit)
    checks = []
    refused = 0
    for host in hosts:
        if scan_kind in ("k8s", "both"):
            checks += [(host, p, "k8s") for p in k8s_ports]
        if scan_kind in ("database", "both"):
            checks += [(host, p, "database") for p in db_ports]

    allowed = []
    for host, port, kind in checks:
        if policy.allows(host, port):
            allowed.append((host, port, kind))
        else:
            refused += 1
    if refused:
        emit(f"Policy refused {refused} of {len(checks)} host:port combinations")
    emit(f"Probing {len(allowed)} endpoint(s) across {len(hosts)} host(s) "
         f"with {workers} workers")

    def _one(item):
        host, port, kind = item
        if cancelled():
            return None
        if MODE == "audit":
            log.info("AUDIT would probe %s:%s (%s)", host, port, kind)
            return None
        return (probe_kubernetes(host, port, timeout) if kind == "k8s"
                else probe_database(host, port, timeout))

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for found in pool.map(_one, allowed):
            done += 1
            if done % 64 == 0:
                emit(f"…{done}/{len(allowed)} probed")
            if found and len(findings) < _MAX_FINDINGS:
                emit(f"FOUND {found['kind']} at {found['host']}:{found['port']} "
                     f"{found.get('engine') or found.get('distro') or ''} "
                     f"{found.get('server_version') or ''}".rstrip())
                findings.append(found)

    return {"findings": findings, "scanned": len(allowed),
            "hosts": len(hosts), "refused": refused,
            "truncated": len(findings) >= _MAX_FINDINGS}


def _from_kubeconfig(policy: Policy, emit) -> list:
    """Highest-signal path: a kubeconfig the operator mounted in. Only the API server
    URL and version are ever reported — the credentials in the file stay in the file."""
    if not os.path.exists(KUBECONFIG):
        return []
    try:
        with open(KUBECONFIG) as fh:
            doc = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as exc:
        emit(f"Could not read the mounted kubeconfig: {exc}")
        return []

    findings = []
    for cluster in doc.get("clusters") or []:
        server = ((cluster or {}).get("cluster") or {}).get("server") or ""
        parsed = urlparse(server)
        if not parsed.hostname:
            continue
        port = parsed.port or 443
        for ip in _resolve_all(parsed.hostname):
            if not policy.allows(ip, port):
                emit(f"REFUSED kubeconfig cluster {server}: {ip}:{port} is outside policy")
                break
            found = probe_kubernetes(ip, port, 5)
            if found:
                found["api_server"] = server.rstrip("/")
                found["suggested_name"] = cluster.get("name") or found["suggested_name"]
                found["source"] = "kubeconfig"
                emit(f"FOUND k8s {server} ({found.get('server_version') or 'version unknown'})")
                findings.append(found)
            break
    return findings


HANDLERS = {"agent_discover": run_discovery}


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
                         lambda: reporter.cancelled)
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
        if not ENROLLMENT_CODE:
            log.error("No stored identity and AGENT_ENROLLMENT_CODE is not set. "
                      "Create an agent in the dashboard and pass its code.")
            return 2
        try:
            dashboard.identity = dashboard.enroll(ENROLLMENT_CODE)
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
        except requests.RequestException as exc:
            log.warning("dashboard unreachable (%s); retrying in %.0fs", exc, backoff)
            backoff = min(backoff * 2, 300)
        # Jittered so a fleet restarted together does not poll in lockstep.
        time.sleep(backoff * (0.8 + random.random() * 0.4))


if __name__ == "__main__":
    sys.exit(main())
