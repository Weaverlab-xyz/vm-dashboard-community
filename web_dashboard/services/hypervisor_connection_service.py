"""Hypervisor connections: N per kind, each optionally reached through a remote agent.

Before this, ``config.py`` held one ``proxmox_host``, one ``vsphere_host`` and so on, so
"N sites x M hypervisors" was inexpressible and every service read the singletons
directly. This module is the single reader of a connection's credential and the single
place that decides *which* connection a caller means.

The resolution order in :func:`resolve` is the backwards-compatibility contract, and
step 4 in particular is what makes this a non-breaking change: an install with nothing
in the table still works off the old config keys.

Secrets reuse ``config_service``'s Fernet (``encrypt_value``/``decrypt_value``), which
exists for exactly this case — a secret held in a table other than ``app_config``. One
secret-at-rest story and one rotation hazard rather than two. ``secret_ref`` sits
alongside for operators who want no secrets in the dashboard database at all.
"""
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..database import HypervisorConnection
from . import config_service

logger = logging.getLogger(__name__)

VALID_KINDS = ("proxmox", "vsphere", "nutanix", "xcpng", "hyperv")

DEFAULT_PORTS = {"proxmox": 8006, "vsphere": 443, "nutanix": 9440,
                 "xcpng": 443, "hyperv": 5985}

# Non-secret per-kind extras allowed in `options`. Closed, and pinned by a test: this is
# a JSON blob on a row that holds credentials, so "anything goes" here would be the
# obvious place for a password to end up by accident.
OPTION_KEYS = {
    "proxmox": ("token_id", "sync_interval_minutes"),
    "vsphere": ("datacenter", "sync_interval_minutes"),
    "nutanix": ("sync_interval_minutes",),
    "xcpng":   ("sync_interval_minutes",),
    "hyperv":  ("transport", "use_ssl", "sync_interval_minutes"),
}

_SEED_MARK = "hypervisor_connections_seeded"


class HypervisorConnectionError(Exception):
    """Raised when a connection cannot be resolved or is invalid."""


@dataclass(frozen=True)
class Connection:
    """A resolved connection, credential included.

    Frozen and never serialised: ``secret`` is plaintext, so this object must not reach
    a job's metadata, a log line or an API response. Services take it, use it, drop it.
    """
    id: str
    kind: str
    name: str
    host: str
    port: int
    username: str
    secret: str
    verify_ssl: bool
    options: dict = field(default_factory=dict)
    agent_id: Optional[str] = None
    agent_connection_name: str = ""
    site: str = ""

    @property
    def via_agent(self) -> bool:
        return bool(self.agent_id)

    def __repr__(self) -> str:  # pragma: no cover - defensive
        # Never let a stray repr() in a log line or traceback print the credential.
        return (f"Connection(kind={self.kind!r}, name={self.name!r}, "
                f"host={self.host!r}, agent_id={self.agent_id!r})")


# ── config helpers ────────────────────────────────────────────────────────────

def _cfg(key: str) -> str:
    val = config_service.get(key)
    if val not in (None, ""):
        return str(val)
    return str(getattr(settings, key, "") or "")


def _cfg_bool(key: str) -> bool:
    return config_service.get_bool(key, bool(getattr(settings, key, False)))


# ── secrets ───────────────────────────────────────────────────────────────────

def _resolve_secret(row: HypervisorConnection) -> str:
    """The connection's plaintext credential.

    ``secret_ref`` wins when both are set, so an operator moving a connection to an
    external backend does not have to clear the old ciphertext first.
    """
    if row.secret_ref:
        try:
            return config_service.resolve_reference(row.secret_ref)
        except Exception as exc:  # noqa: BLE001
            raise HypervisorConnectionError(
                f"Connection {row.name!r}: its secret reference could not be "
                f"resolved — check the external secret backend.") from exc
    if row.secret_enc:
        return config_service.decrypt_value(row.secret_enc)
    return ""


# ── resolution ────────────────────────────────────────────────────────────────

def resolve(db: Session, kind: str, connection_id: Optional[str] = None) -> Connection:
    """The connection a caller means, in five steps.

    1. an explicit ``connection_id`` — wrong kind, inactive or missing is an error,
       never a silent fallback to something else;
    2. the ``is_default`` row for this kind;
    3. the only active row for this kind, if there is exactly one;
    4. **the legacy singleton config keys**, when the table holds no row for this kind
       at all. This is what makes the change non-breaking, and it also catches the
       install where the seed marked itself done before someone set ``PROXMOX_HOST`` by
       environment. ``# COMPAT:`` — removable once every install has run the seed;
    5. otherwise an error naming the fix, because guessing between three configured
       vCenters is worse than refusing.
    """
    kind = (kind or "").strip().lower()
    if kind not in VALID_KINDS:
        raise HypervisorConnectionError(f"unknown hypervisor kind {kind!r}")

    if db is None:
        # No session — a caller outside a request (a script, a background helper that
        # was never given one). Go straight to the legacy keys rather than raising:
        # that is what such a caller could see before this table existed, and it is a
        # far better outcome than an AttributeError swallowed by someone's except.
        legacy = _from_settings(kind)
        if legacy is not None:
            return legacy
        raise HypervisorConnectionError(
            f"no {kind} connection is configured and no database session was supplied")

    if connection_id:
        row = db.query(HypervisorConnection).filter(
            HypervisorConnection.id == connection_id).first()
        if row is None:
            raise HypervisorConnectionError("that connection no longer exists")
        if row.kind != kind:
            raise HypervisorConnectionError(
                f"connection {row.name!r} is a {row.kind} connection, not {kind}")
        if not row.is_active:
            raise HypervisorConnectionError(f"connection {row.name!r} is disabled")
        return to_connection(row)

    rows = db.query(HypervisorConnection).filter(
        HypervisorConnection.kind == kind,
        HypervisorConnection.is_active.is_(True)).all()
    if not rows:
        # COMPAT: remove one release after the seed ships everywhere.
        legacy = _from_settings(kind)
        if legacy is not None:
            return legacy
        raise HypervisorConnectionError(
            f"no {kind} connection is configured — add one on the Connections page")

    for row in rows:
        if row.is_default:
            return to_connection(row)
    if len(rows) == 1:
        return to_connection(rows[0])
    raise HypervisorConnectionError(
        f"{len(rows)} {kind} connections are configured and none is the default — "
        f"pass a connection, or set one as the default on the Connections page")


def to_connection(row: HypervisorConnection) -> Connection:
    return Connection(
        id=row.id, kind=row.kind, name=row.name,
        host=row.host or "", port=int(row.port or DEFAULT_PORTS.get(row.kind, 443)),
        username=row.username or "", secret=_resolve_secret(row),
        verify_ssl=bool(row.verify_ssl), options=row.options_dict,
        agent_id=row.agent_id, agent_connection_name=row.agent_connection_name or "",
        site=row.site or "")


# ── the legacy singletons ─────────────────────────────────────────────────────
#
# One spec per kind, used by BOTH the COMPAT branch of resolve() and the seed, so the
# two cannot disagree about what the old configuration meant.

_SINGLETON_SPEC = {
    "proxmox": {"host": "proxmox_host", "port": "proxmox_port", "user": "proxmox_user",
                # Token secret first: token auth is the documented preference and a stale
                # password may still be sitting in config beside it.
                "secrets": ("proxmox_token_secret", "proxmox_password"),
                "verify": "proxmox_verify_ssl",
                "options": {"token_id": "proxmox_token_id"}},
    "vsphere": {"host": "vsphere_host", "port": "vsphere_port", "user": "vsphere_user",
                "secrets": ("vsphere_password",), "verify": "vsphere_verify_ssl",
                "options": {"datacenter": "vsphere_datacenter"}},
    "nutanix": {"host": "nutanix_host", "port": "nutanix_port", "user": "nutanix_username",
                "secrets": ("nutanix_password",), "verify": "nutanix_verify_ssl",
                "options": {}},
    "xcpng":   {"host": "xcpng_host", "port": None, "user": "xcpng_username",
                "secrets": ("xcpng_password",), "verify": "xcpng_verify_ssl",
                "options": {}},
    "hyperv":  {"host": "hyperv_host", "port": "hyperv_port", "user": "hyperv_username",
                "secrets": ("hyperv_password",), "verify": "hyperv_verify_ssl",
                "options": {"transport": "hyperv_transport", "use_ssl": "hyperv_use_ssl"}},
}


def _from_settings(kind: str) -> Optional[Connection]:
    spec = _SINGLETON_SPEC[kind]
    host = _cfg(spec["host"])
    if not host:
        return None
    secret = ""
    for key in spec["secrets"]:
        secret = _cfg(key)
        if secret:
            break
    options = {}
    for opt_key, cfg_key in spec["options"].items():
        value = _cfg_bool(cfg_key) if opt_key == "use_ssl" else _cfg(cfg_key)
        if value not in (None, "", False):
            options[opt_key] = value
    port = _cfg(spec["port"]) if spec["port"] else ""
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = DEFAULT_PORTS[kind]
    return Connection(
        id="", kind=kind, name="default", host=host, port=port,
        username=_cfg(spec["user"]), secret=secret,
        verify_ssl=_cfg_bool(spec["verify"]), options=options)


# ── seeding ───────────────────────────────────────────────────────────────────

def seed_from_settings(db: Session) -> int:
    """Copy each configured singleton into row #1 for its kind. Returns rows created.

    **Copies, never moves.** The ``*_host`` config keys are left exactly as they were,
    which is what makes rolling back to the previous image a no-op rather than an
    outage: the old build reads the keys it always read, and these rows sit unread.

    Idempotent twice over — a config mark *and* a per-kind existence check — because the
    mark is an ``app_config`` row an operator can delete. Concurrency (two gunicorn
    workers plus the jobs worker starting together) is arbitrated by the unique
    constraint, the same idiom ``agent_service._consume_nonce`` uses for nonces.

    Runs OUTSIDE the advisory-locked DDL block, deliberately. Do not wrap it in a
    session-level advisory lock — that is precisely the QueuePool leak that produced the
    cold co-deploy hang init_db documents.
    """
    try:
        if config_service.get(_SEED_MARK) == "1":
            return 0
    except Exception:  # noqa: BLE001 — a config read must not stop the app booting
        logger.debug("hypervisor seed: config mark unreadable", exc_info=True)
        return 0

    created = 0
    try:
        for kind in VALID_KINDS:
            legacy = _from_settings(kind)
            if legacy is None:
                continue                       # nothing configured for this kind
            if db.query(HypervisorConnection.id).filter(
                    HypervisorConnection.kind == kind).first():
                continue                       # an operator got here first
            db.add(_row_from(legacy, created_by="system:migration"))
            created += 1
        if created:
            db.commit()
    except IntegrityError:
        # Two workers raced past both guards; the unique constraint is the arbiter.
        db.rollback()
        return 0
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.warning("hypervisor connection seed failed", exc_info=True)
        return 0

    try:
        config_service.set(_SEED_MARK, "1")
    except Exception:  # noqa: BLE001
        logger.debug("hypervisor seed: could not write the mark", exc_info=True)
    if created:
        logger.info("seeded %d hypervisor connection(s) from the legacy config keys", created)
    return created


def _row_from(conn: Connection, *, created_by: str) -> HypervisorConnection:
    return HypervisorConnection(
        id=str(uuid.uuid4()), kind=conn.kind, name=conn.name, host=conn.host,
        port=conn.port, username=conn.username,
        secret_enc=config_service.encrypt_value(conn.secret) if conn.secret else None,
        verify_ssl=conn.verify_ssl,
        options=json.dumps(conn.options) if conn.options else None,
        is_default=True, is_active=True,
        created_at=datetime.utcnow(), created_by=created_by)


# ── CRUD ──────────────────────────────────────────────────────────────────────

def _clean_options(kind: str, raw) -> Optional[str]:
    """Keep only the declared non-secret keys for this kind."""
    allowed = OPTION_KEYS.get(kind, ())
    source = raw if isinstance(raw, dict) else {}
    out = {k: v for k, v in source.items() if k in allowed and v not in (None, "")}
    return json.dumps(out, sort_keys=True) if out else None


def list_connections(db: Session, kind: str = "") -> list:
    query = db.query(HypervisorConnection)
    if kind:
        query = query.filter(HypervisorConnection.kind == kind.strip().lower())
    rows = query.order_by(HypervisorConnection.kind, HypervisorConnection.name).all()
    return [serialize(r) for r in rows]


def serialize(row: HypervisorConnection) -> dict:
    """The API/UI projection. **No credential field of any kind** — not the ciphertext,
    not a masked placeholder that a later refactor could accidentally fill in."""
    return {
        "id": row.id, "kind": row.kind, "name": row.name,
        "host": row.host or "", "port": row.port,
        "username": row.username or "", "verify_ssl": bool(row.verify_ssl),
        "options": row.options_dict,
        "has_secret": bool(row.secret_enc or row.secret_ref),
        "secret_ref": row.secret_ref or "",
        "agent_id": row.agent_id, "agent_connection_name": row.agent_connection_name or "",
        "via_agent": bool(row.agent_id),
        "site": row.site or "",
        "is_default": bool(row.is_default), "is_active": bool(row.is_active),
        "last_ok_at": row.last_ok_at.isoformat() if row.last_ok_at else None,
        "last_sync_at": row.last_sync_at.isoformat() if row.last_sync_at else None,
        "last_error": row.last_error or "",
        "created_by": row.created_by or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def create(db: Session, *, kind: str, name: str, created_by: str,
           host: str = "", port=None, username: str = "", secret: str = "",
           secret_ref: str = "", verify_ssl: bool = False, options=None,
           agent_id: str = "", agent_connection_name: str = "", site: str = "",
           is_default: bool = False) -> dict:
    kind = (kind or "").strip().lower()
    if kind not in VALID_KINDS:
        raise HypervisorConnectionError(
            f"unknown hypervisor kind {kind!r} — one of {', '.join(VALID_KINDS)}")
    name = (name or "").strip()
    if not name:
        raise HypervisorConnectionError("a name is required")
    if db.query(HypervisorConnection.id).filter(
            HypervisorConnection.kind == kind, HypervisorConnection.name == name).first():
        raise HypervisorConnectionError(f"a {kind} connection named {name!r} already exists")

    agent_id = (agent_id or "").strip()
    agent_connection_name = (agent_connection_name or "").strip()
    if agent_id:
        # An agent-bound connection is defined ENTIRELY by the name it has in that
        # agent's own file. Accepting a host here would invite someone to fill it in and
        # assume it is what gets dialled, when nothing reads it.
        if not agent_connection_name:
            raise HypervisorConnectionError(
                "an agent-bound connection needs the name it has in that agent's "
                "connections.yaml — the dashboard never holds its credential")
        host, username, secret, secret_ref = "", "", "", ""
    elif not (host or "").strip():
        raise HypervisorConnectionError("a host is required")

    row = HypervisorConnection(
        id=str(uuid.uuid4()), kind=kind, name=name,
        host=(host or "").strip(), port=int(port or DEFAULT_PORTS[kind]),
        username=(username or "").strip(),
        secret_enc=config_service.encrypt_value(secret) if secret else None,
        secret_ref=(secret_ref or "").strip() or None,
        verify_ssl=bool(verify_ssl), options=_clean_options(kind, options),
        agent_id=agent_id or None, agent_connection_name=agent_connection_name or None,
        site=(site or "").strip() or None,
        is_active=True, is_default=False,
        created_at=datetime.utcnow(), created_by=created_by)
    db.add(row)
    db.commit()
    db.refresh(row)

    # First of its kind is the default whether or not anyone asked, so a single-connection
    # install never hits the "none is the default" refusal.
    if is_default or db.query(HypervisorConnection.id).filter(
            HypervisorConnection.kind == kind).count() == 1:
        set_default(db, row.id)
        db.refresh(row)
    return serialize(row)


def update(db: Session, connection_id: str, **fields) -> dict:
    row = _get(db, connection_id)
    for key in ("name", "host", "username", "site", "agent_connection_name"):
        if key in fields and fields[key] is not None:
            setattr(row, key, str(fields[key]).strip() or None)
    if fields.get("port") is not None:
        row.port = int(fields["port"])
    if fields.get("verify_ssl") is not None:
        row.verify_ssl = bool(fields["verify_ssl"])
    if fields.get("options") is not None:
        row.options = _clean_options(row.kind, fields["options"])
    if fields.get("is_active") is not None:
        row.is_active = bool(fields["is_active"])
    if fields.get("agent_id") is not None:
        row.agent_id = str(fields["agent_id"]).strip() or None
    # A blank secret means "leave it alone", never "clear it" — otherwise every edit of
    # an unrelated field through a form that does not echo the password wipes it.
    if fields.get("secret"):
        row.secret_enc = config_service.encrypt_value(str(fields["secret"]))
        row.secret_ref = None
    if fields.get("secret_ref"):
        row.secret_ref = str(fields["secret_ref"]).strip()
        row.secret_enc = None
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return serialize(row)


def set_default(db: Session, connection_id: str) -> dict:
    """Make this the default for its kind, clearing its siblings in the same commit."""
    row = _get(db, connection_id)
    db.query(HypervisorConnection).filter(
        HypervisorConnection.kind == row.kind,
        HypervisorConnection.id != row.id).update({"is_default": False})
    row.is_default = True
    db.commit()
    db.refresh(row)
    return serialize(row)


def delete(db: Session, connection_id: str) -> None:
    row = _get(db, connection_id)
    kind, was_default = row.kind, row.is_default
    db.delete(row)
    db.commit()
    if was_default:
        # Promote a survivor, or the remaining connections become unreachable without an
        # explicit id — a delete must not strand them.
        survivor = db.query(HypervisorConnection).filter(
            HypervisorConnection.kind == kind,
            HypervisorConnection.is_active.is_(True)).first()
        if survivor:
            set_default(db, survivor.id)


def record_result(db: Session, connection_id: str, *, error: str = "",
                  synced: bool = False) -> None:
    """Stamp the outcome of a live call. Best-effort; never raises into a caller."""
    try:
        row = db.query(HypervisorConnection).filter(
            HypervisorConnection.id == connection_id).first()
        if row is None:
            return
        now = datetime.utcnow()
        if error:
            row.last_error = str(error)[:2000]
        else:
            row.last_ok_at = now
            row.last_error = None
            if synced:
                row.last_sync_at = now
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.debug("could not record connection result", exc_info=True)


def _get(db: Session, connection_id: str) -> HypervisorConnection:
    row = db.query(HypervisorConnection).filter(
        HypervisorConnection.id == connection_id).first()
    if row is None:
        raise HypervisorConnectionError("that connection no longer exists")
    return row
