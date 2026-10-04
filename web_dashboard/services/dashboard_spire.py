"""The dashboard's OWN SPIRE server: one-click agent migration and its trust domain.

docs/design/agent-and-human-identity.md. ``docker-compose.spire.yml`` runs a SPIRE server
beside the dashboard; this module drives it the way the SPIRE lab drives its Docker-mode
server (``spire_lab_service._CLI_PREFIX``): ``docker exec <container> spire-server …``,
over the Docker socket the app already mounts and with the ``docker`` CLI the image
already ships. No gRPC dependency, and the CLI's own ``-output json`` (protojson with
proto field names, empty fields emitted) is the contract.

Three jobs:

* **Migrate an agent** (``migrate_agent``): a one-use join token for a node entry named
  after the agent, the workload entry ``spiffe://<td>/agent/<id>`` selecting
  ``unix:uid:10001`` under it, the dashboard's trust domain registered so its SVIDs
  verify, and the agent bound — the four manual steps #996 left, in one call.
* **Register the trust domain** (``sync_trust_domain``) from ``bundle show -format
  spiffe``, which is exactly the shape ``SpiffeTrustDomain.bundle_json`` holds.
* **Keep it current** (``sync_if_due``): SPIRE rotates JWT keys within ca_ttl and
  publishes the next one ahead of time, so a daily re-sync keeps verification working.

Cloud node attestation (aws_iid / azure_imds / gcp_iit) is not automated: the node's
SPIFFE ID is derived from the instance and is not known until it attests.

And one for the dashboard itself (``mint_jwt``, ``live_bundle``): JWT-SVIDs for
``spiffe://<td>/dashboard``, minted through the admin API rather than attested through a
SPIRE agent. Attestation proves a workload to a server that does not already trust it, and
the app already administers this one. See services/dashboard_identity.py and
docs/design/dashboard-workload-identity.md.
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from . import config_service

logger = logging.getLogger(__name__)

DEFAULT_CONTAINER = "vmdash-spire-server"
SPIRE_BIN = "/opt/spire/bin/spire-server"
OWNER = "dashboard-spire"           # SpiffeTrustDomain.created_by for the rows we own
AGENT_UID = 10001                    # runners/agent/Dockerfile USER; the helper runs as it
JOIN_TOKEN_TTL_S = 900
JWT_SVID_TTL_S = 300
SYNC_EVERY = timedelta(hours=24)
MIN_AGENT_VERSION = "2.6.0"          # first agent with AGENT_SPIFFE_JWT_FILE
DASHBOARD_PATH = "/dashboard"        # the dashboard's own SPIFFE ID under its trust domain
SERVER_PORT = 8081                   # docker-compose.spire.yml publishes this by default

# Who may hold which path in the dashboard's OWN trust domain. Remote agents and the
# dashboard itself are reserved: a service-account client bound to one of those IDs would
# let whatever can mint them sign in as something else. The other two are where the
# Workload Lab's demos and service-account workloads live (docs/design/
# dashboard-workload-identity.md, L1 and L2).
RESERVED_PREFIXES = ("/agent/", DASHBOARD_PATH)
CELL_PREFIX = "/demo/agent-cell/"
WORKLOAD_PREFIX = "/workload/"


class DashboardSpireError(Exception):
    """The dashboard's SPIRE server could not do what was asked. The message is safe to
    show an operator: it names the cause and never carries a token."""


def container() -> str:
    return (config_service.get("spire_server_container") or "").strip() or DEFAULT_CONTAINER


def _run(argv: list, timeout: int, input: Optional[str] = None) -> subprocess.CompletedProcess:
    """Indirection so tests can stub the subprocess without touching the parsing."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          check=False, input=input)


def _cli(*args: str, timeout: int = 30, input: Optional[str] = None) -> str:
    name = container()
    # `-i` only when there is something to send: a federation's seed bundle goes in on
    # stdin (`-trustDomainBundlePath /dev/stdin`) because the container's filesystem is
    # read-only and nothing the app writes is mounted into it.
    argv = ["docker", "exec", *(["-i"] if input is not None else []), name, SPIRE_BIN, *args]
    try:
        proc = _run(argv, timeout) if input is None else _run(argv, timeout, input=input)
    except FileNotFoundError as exc:
        raise DashboardSpireError("the docker CLI is not available to the dashboard") from exc
    except subprocess.TimeoutExpired as exc:
        raise DashboardSpireError(f"spire-server {args[0]} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = err[-1][:300] if err else f"exit code {proc.returncode}"
        if "No such container" in (proc.stderr or "") or "is not running" in (proc.stderr or ""):
            raise DashboardSpireError(
                f"the SPIRE server container {name!r} is not running. Start it with "
                f"docker compose -f docker-compose.yml -f docker-compose.spire.yml up -d, "
                f"or set its name under Settings → Remote agents.")
        # Only the subcommand is named, never the full command line.
        raise DashboardSpireError(f"spire-server {' '.join(args[:2])} failed: {tail}")
    return proc.stdout


def _json(*args: str, timeout: int = 30, input: Optional[str] = None) -> dict:
    out = _cli(*args, "-output", "json", timeout=timeout, input=input)
    try:
        data = json.loads(out)
    except ValueError as exc:
        raise DashboardSpireError(f"spire-server {args[0]} returned output that is not "
                                  f"JSON — is the container running SPIRE 1.15?") from exc
    if not isinstance(data, dict):
        raise DashboardSpireError(f"spire-server {args[0]} returned an unexpected shape")
    return data


# ── the trust domain ──────────────────────────────────────────────────────────

def trust_domain(timeout: int = 30) -> str:
    td = str(_json("bundle", "show", timeout=timeout).get("trust_domain") or "").strip().lower()
    if not td:
        raise DashboardSpireError("the SPIRE server reported no trust domain")
    return td


def bootstrap_bundle_pem() -> str:
    """The X.509 trust bundle an agent host saves as spire/bootstrap.crt."""
    pem = _cli("bundle", "show").strip()
    if "BEGIN CERTIFICATE" not in pem:
        raise DashboardSpireError("the SPIRE server returned no X.509 trust bundle")
    return pem + "\n"


def sync_trust_domain(db: Session, td: Optional[str] = None) -> dict:
    """Register (or refresh) the dashboard's trust domain from its SPIRE server.

    Only ever writes a row this module owns. A row a SPIRE lab registered, or one an
    admin added by hand, is someone else's statement of whose keys to trust under that
    name; overwriting it would silently change whose SVIDs verify, so this refuses.
    """
    from ..database import SpiffeTrustDomain
    from . import spiffe_assertion
    td = td or trust_domain()
    raw = _cli("bundle", "show", "-format", "spiffe")
    try:
        bundle = json.loads(raw)
    except ValueError as exc:
        raise DashboardSpireError("the SPIRE server's SPIFFE bundle is not JSON") from exc
    if not spiffe_assertion.bundle_keys(json.dumps(bundle)):
        raise DashboardSpireError("the SPIRE server's bundle has no JWT-SVID keys")
    rec = db.query(SpiffeTrustDomain).filter(SpiffeTrustDomain.trust_domain == td).first()
    if rec and (rec.spire_lab_id or rec.created_by != OWNER):
        raise DashboardSpireError(
            f"the trust domain {td} is already registered "
            f"({'by a SPIRE lab' if rec.spire_lab_id else 'by hand'}); not overwriting its "
            f"keys. Remove that registration, or give this SPIRE server another name.")
    now = datetime.utcnow()
    if rec is None:
        rec = SpiffeTrustDomain(trust_domain=td, created_by=OWNER)
        db.add(rec)
    rec.bundle_json = json.dumps(bundle)
    rec.bundle_captured_at = now
    rec.jwks_url = None
    rec.updated_at = now
    db.commit()
    spiffe_assertion.clear_state()
    return {"trust_domain": td, "captured_at": now}


def server_in_use() -> bool:
    """Whether anything on this install relies on the dashboard's SPIRE server: agent
    attestation, or the dashboard's own workload identity. Either needs the trust domain
    registered and current, independently of the other."""
    return (config_service.get_bool("spire_attest_enabled")
            or config_service.get_bool("dashboard_spiffe_identity_enabled"))


def registered(db: Session):
    """The SpiffeTrustDomain row this module owns, or None."""
    from ..database import SpiffeTrustDomain
    return (db.query(SpiffeTrustDomain)
            .filter(SpiffeTrustDomain.created_by == OWNER).first())


def sync_if_due(db: Session, now: Optional[datetime] = None) -> bool:
    """Re-sync the stored bundle once a day while the server is in use — or once anything
    has registered its trust domain, because agent cells and service-account clients bound
    to it verify against those keys whichever switches are on. Never raises: this runs from
    the background refresh loop, and a SPIRE server that is down today must not take the
    loop with it."""
    now = now or datetime.utcnow()
    try:
        rec = registered(db)
        if rec is None and not server_in_use():
            return False
        if rec and rec.bundle_captured_at and now - rec.bundle_captured_at < SYNC_EVERY:
            return False
        sync_trust_domain(db, rec.trust_domain if rec else None)
        return True
    except Exception as exc:  # noqa: BLE001 -- background loop: log and carry on
        logger.warning("dashboard SPIRE: could not re-sync the trust bundle: %s", exc)
        return False


# ── migrating one agent ───────────────────────────────────────────────────────

def ids_for(td: str, agent_id: str) -> tuple:
    """(workload SPIFFE ID, node SPIFFE ID) for an agent row."""
    return f"spiffe://{td}/agent/{agent_id}", f"spiffe://{td}/node/{agent_id}"


def _entry_exists(spiffe_id: str) -> bool:
    return bool(_json("entry", "show", "-spiffeID", spiffe_id).get("entries"))


def register_workload(node: str, spiffe_id: str, uid: int,
                      jwt_ttl_s: int = JWT_SVID_TTL_S, federates_with=()) -> str:
    """A one-use join token for ``node``, and the workload entry ``spiffe_id`` selecting
    ``unix:uid:<uid>`` under it — created only if absent, so a second call just mints a new
    token. Returns the token; the caller shows it once and never stores it."""
    token = str(_json("token", "generate", "-spiffeID", node,
                      "-ttl", str(JOIN_TOKEN_TTL_S)).get("value") or "")
    if not token:
        raise DashboardSpireError("the SPIRE server minted no join token")

    if not _entry_exists(spiffe_id):
        fed = [arg for td in federates_with for arg in ("-federatesWith", f"spiffe://{td}")]
        res = _json("entry", "create", "-parentID", node, "-spiffeID", spiffe_id,
                    "-selector", f"unix:uid:{int(uid)}",
                    "-jwtSVIDTTL", str(jwt_ttl_s), *fed)
        results = res.get("results") or []
        status = (results[0].get("status") or {}) if results else {}
        if not results or int(status.get("code") or 0) != 0:
            raise DashboardSpireError(
                f"the SPIRE server refused the workload entry: "
                f"{status.get('message') or 'no result returned'}")
    return token


def remove_workload(spiffe_id: str, node: str) -> list:
    """Delete the workload's entries and evict its node. Best-effort: returns what could
    not be done rather than raising, so revoking a credential never fails because the SPIRE
    server is down — the credential is the thing that must stop, and it already has."""
    problems = []
    try:
        for entry in _json("entry", "show", "-spiffeID", spiffe_id).get("entries") or []:
            res = _json("entry", "delete", "-entryID", str(entry.get("id") or ""))
            results = res.get("results") or []
            status = (results[0].get("status") or {}) if results else {}
            if results and int(status.get("code") or 0) != 0:
                problems.append(f"entry {entry.get('id')}: {status.get('message')}")
    except DashboardSpireError as exc:
        problems.append(f"entries for {spiffe_id}: {exc}")
    try:
        _cli("agent", "evict", "-spiffeID", node)
    except DashboardSpireError as exc:
        # A node whose agent never attested has nothing to evict; that is not a problem.
        if "not found" not in str(exc).lower():
            problems.append(f"node {node}: {exc}")
    return problems


def server_address() -> str:
    """Where an agent host reaches this SPIRE server: the pinned agent audience's host,
    because that is the name the install already publishes for agents."""
    from urllib.parse import urlparse
    from . import agent_service
    return urlparse(config_service.get(agent_service.AUDIENCE_CONFIG) or "").hostname or ""


def install_facts(td: str) -> dict:
    """What spire-agent-install.yml needs besides the join token. None of it is secret."""
    return {"trust_domain": td, "server_address": server_address(),
            "server_port": SERVER_PORT, "bootstrap_pem": bootstrap_bundle_pem()}


def in_trust_domain(spiffe_id: str, td: str) -> bool:
    return (spiffe_id or "").startswith(f"spiffe://{td}/")


def path_of(spiffe_id: str, td: str) -> str:
    return spiffe_id[len(f"spiffe://{td}"):] if in_trust_domain(spiffe_id, td) else ""


def reserved_path(spiffe_id: str, td: str) -> bool:
    """Whether ``spiffe_id`` is one only the dashboard itself may hold in its own trust
    domain — a remote agent's or the dashboard's."""
    path = path_of(spiffe_id, td)
    return any(path == base or path.startswith(base + "/")
               for base in (p.rstrip("/") for p in RESERVED_PREFIXES))


def migrate_agent(db: Session, agent) -> dict:
    """Everything an agent needs to attest through the dashboard's SPIRE server.

    Idempotent: a second run reuses the workload entry and only mints a new join token
    (the old one is spent, or expires in fifteen minutes). Binding does not cut the
    agent's current key off — that happens when it first attests (agent_service.attest).
    """
    from . import agent_service
    td = trust_domain()
    sid, node = ids_for(td, agent.id)
    token = register_workload(node, sid, AGENT_UID)
    sync_trust_domain(db, td)
    agent_service.bind_spiffe_id(db, agent, sid)
    return {"trust_domain": td, "spiffe_id": sid, "node_spiffe_id": node,
            "join_token": token, "join_token_ttl_s": JOIN_TOKEN_TTL_S,
            "bootstrap_pem": bootstrap_bundle_pem()}


# ── the dashboard's own identity ──────────────────────────────────────────────

def dashboard_id(td: str) -> str:
    """The dashboard's SPIFFE ID. One for app and worker alike: they run the same code with
    the same configuration, and a cloud trust policy that must name two subjects for one
    service is a policy somebody gets half right."""
    return f"spiffe://{td}{DASHBOARD_PATH}"


def mint_jwt(audience: str, ttl_s: int, td: Optional[str] = None) -> str:
    """A JWT-SVID for the dashboard, for one audience, from the server's admin API.

    ``jwt mint -output json`` prints the MintJWTSVIDResponse (``{"svid": {"token": …}}``);
    ``-ttl`` is a Go duration. The caller checks the claims — this only refuses output that
    is not a JWT at all. The token is never put into an error message.
    """
    td = td or trust_domain()
    data = _json("jwt", "mint", "-spiffeID", dashboard_id(td), "-audience", audience,
                 "-ttl", f"{int(ttl_s)}s")
    token = str((data.get("svid") or {}).get("token") or "")
    if token.count(".") != 2:
        raise DashboardSpireError("the SPIRE server minted no JWT-SVID")
    return token


def live_bundle() -> dict:
    """The SPIFFE bundle as the server holds it right now, including a JWT key it has
    prepared but not yet signed with — which is why discovery reads this rather than the
    daily copy in SpiffeTrustDomain."""
    raw = _cli("bundle", "show", "-format", "spiffe")
    try:
        bundle = json.loads(raw)
    except ValueError as exc:
        raise DashboardSpireError("the SPIRE server's SPIFFE bundle is not JSON") from exc
    if not isinstance(bundle, dict):
        raise DashboardSpireError("the SPIRE server's SPIFFE bundle has an unexpected shape")
    return bundle


# ── SPIFFE federation with a Workload Lab ─────────────────────────────────────
# docs/design/dashboard-workload-identity.md, L4. This server FETCHES a federated trust
# domain's bundle from that domain's bundle endpoint (https_spiffe: TLS authenticated by
# the endpoint server's own SVID), and keeps it current through rotation by itself. The
# relationship needs a SEED bundle to authenticate that first fetch; the caller passes the
# bundle the dashboard already captured from the lab. Everything here is public keys.

FEDERATION_PORT = 8082               # server.conf federation.bundle_endpoint.port
SERVER_PATH = "/spire/server"        # the SPIFFE ID a SPIRE server's bundle endpoint presents


def federation_url(host: str) -> str:
    return f"https://{host}:{FEDERATION_PORT}"


def server_id(td: str) -> str:
    return f"spiffe://{td}{SERVER_PATH}"


def _federation_exists(td: str) -> bool:
    try:
        _json("federation", "show", "-trustDomain", td)
        return True
    except DashboardSpireError as exc:
        if "is not running" in str(exc):
            raise
        return False


def federate(td: str, url: str, endpoint_id: str, bundle_json: str) -> dict:
    """Create (or update) this server's federation relationship with ``td`` and refresh it
    once, so a relationship that cannot fetch is an error now, not a silent stale bundle.

    The caller derives every argument from the lab row and the captured bundle; nothing
    here comes from a request.
    """
    td = (td or "").strip().lower()
    if not td or "/" in td or ":" in td:
        raise DashboardSpireError(f"{td!r} is not a bare trust domain name")
    if not url.startswith("https://"):
        raise DashboardSpireError("a bundle endpoint URL must be https")
    if endpoint_id != server_id(td):
        raise DashboardSpireError(
            f"the endpoint's SPIFFE ID must be {server_id(td)} — a SPIRE server's bundle "
            f"endpoint presents its own SVID")
    if '"keys"' not in (bundle_json or ""):
        raise DashboardSpireError(
            f"there is no captured bundle for {td} to seed the relationship with. Press "
            f"Refresh keys on the lab first")
    verb = "update" if _federation_exists(td) else "create"
    res = _json("federation", verb, "-trustDomain", td, "-bundleEndpointURL", url,
                "-bundleEndpointProfile", "https_spiffe", "-endpointSpiffeID", endpoint_id,
                "-trustDomainBundleFormat", "spiffe", "-trustDomainBundlePath", "/dev/stdin",
                input=bundle_json)
    results = res.get("results") or []
    status = (results[0].get("status") or {}) if results else {}
    if not results or int(status.get("code") or 0) != 0:
        raise DashboardSpireError(
            f"the SPIRE server refused the federation relationship with {td}: "
            f"{status.get('message') or 'no result returned'}")
    try:
        _cli("federation", "refresh", "-id", td, timeout=60)
    except DashboardSpireError as exc:
        raise DashboardSpireError(
            f"the relationship with {td} is set, but this server could not fetch its "
            f"bundle from {url}: {exc}. Check that tcp/{FEDERATION_PORT} on the lab host is "
            f"reachable from the dashboard host") from exc
    return {"trust_domain": td, "bundle_endpoint_url": url, "action": verb}


def unfederate(td: str) -> list:
    """Remove the relationship and the federated bundle. Each step is attempted on its own
    so a half-removed federation still finishes; returns what was removed."""
    td = (td or "").strip().lower()
    removed = []
    for what, args in (("relationship", ("federation", "delete", "-id", td)),
                       ("bundle", ("bundle", "delete", "-id", f"spiffe://{td}"))):
        try:
            _cli(*args)
            removed.append(what)
        except DashboardSpireError as exc:
            if "is not running" in str(exc):
                raise
            logger.info("dashboard-spire: no %s to remove for %s (%s)", what, td, exc)
    return removed


def _id_str(sid) -> str:
    """``{"trust_domain": "td", "path": "/p"}`` (entry JSON) or a string -> spiffe://td/p."""
    if isinstance(sid, dict):
        return f"spiffe://{sid.get('trust_domain', '')}{sid.get('path', '')}"
    return str(sid or "")


def _bare_td(td: str) -> str:
    return str(td or "").strip().lower().removeprefix("spiffe://").rstrip("/")


def set_federates_with(spiffe_id: str, td: str, present: bool = True) -> int:
    """Add (or remove) ``td`` in the ``federatesWith`` of every entry for ``spiffe_id``,
    so the workload's SVID response carries — or stops carrying — that trust domain's
    bundle. Returns how many entries changed.

    ``entry update`` REPLACES the entry, so every field the entry has is written back
    from what the server just reported; only federatesWith differs. An entry already in
    the wanted state is left alone.
    """
    td = _bare_td(td)
    entries = _json("entry", "show", "-spiffeID", spiffe_id).get("entries") or []
    changed = 0
    for e in entries:
        have = [_bare_td(x) for x in (e.get("federates_with") or [])]
        want = sorted(set(have) | {td}) if present else [x for x in have if x != td]
        if sorted(have) == sorted(want):
            continue
        args = ["entry", "update", "-entryID", str(e["id"]),
                "-parentID", _id_str(e.get("parent_id")),
                "-spiffeID", _id_str(e.get("spiffe_id"))]
        for sel in e.get("selectors") or []:
            args += ["-selector", f"{sel['type']}:{sel['value']}"]
        if e.get("x509_svid_ttl"):
            args += ["-x509SVIDTTL", str(int(e["x509_svid_ttl"]))]
        if e.get("jwt_svid_ttl"):
            args += ["-jwtSVIDTTL", str(int(e["jwt_svid_ttl"]))]
        for dns in e.get("dns_names") or []:
            args += ["-dns", dns]
        if e.get("admin"):
            args.append("-admin")
        if e.get("downstream"):
            args.append("-downstream")
        if e.get("hint"):
            args += ["-hint", str(e["hint"])]
        for other in want:
            args += ["-federatesWith", f"spiffe://{other}"]
        res = _json(*args)
        results = res.get("results") or []
        status = (results[0].get("status") or {}) if results else {}
        if not results or int(status.get("code") or 0) != 0:
            raise DashboardSpireError(
                f"the SPIRE server refused to update the entry for {spiffe_id}: "
                f"{status.get('message') or 'no result returned'}")
        changed += 1
    return changed
