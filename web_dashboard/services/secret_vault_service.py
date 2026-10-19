"""Registered secret vaults, and which workstream may use each.

A reference can name a vault: ``azure_kv://<vault-id>/<secret>`` routes the read to that
vault's endpoint (``config_service._parse_ref``). Until now the registry had no API, and
its ``workgroup`` column — "only this workstream resolves here" — was never read. This
module is the administrator's way to register vaults and scope one to a workstream; the
scope is enforced where references resolve (``config_service._resolve_external``), so a
reference into a team's vault resolves only for that team's deploys.

Which callers pass a workstream: the per-deploy resolution of a cloud VM (its PRA
credential and Gateway deploy key) and Add to PRA. Every other resolution — global
Settings keys, Config-Management secret variables, notifications — passes none, so a
scoped vault is simply not usable there. That is the point: a team vault holds that
team's secrets.
"""
from __future__ import annotations

import re
from typing import Optional

from sqlalchemy.orm import Session

BACKENDS = ("aws_sm", "azure_kv", "gcp_sm", "bt_secrets_safe")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class VaultError(ValueError):
    """Bad input, or a vault still in use (400/409 at the API edge)."""


def _row(v) -> dict:
    return {"id": v.id, "backend": v.backend, "endpoint": v.endpoint,
            "workgroup": v.workgroup or None}


def list_vaults(db: Session) -> list:
    from ..database import SecretVault
    return [_row(v) for v in db.query(SecretVault).order_by(SecretVault.id).all()]


def upsert(db: Session, vault_id: str, backend: str, endpoint: str,
           workgroup: Optional[str], by_user_id: Optional[str] = None) -> dict:
    from ..database import SecretVault
    from . import workgroup_service
    vault_id = (vault_id or "").strip()
    if not ID_RE.match(vault_id):
        raise VaultError("A vault id is lowercase letters, digits and dashes, at most 63.")
    if backend not in BACKENDS:
        raise VaultError(f"backend must be one of {', '.join(BACKENDS)}")
    endpoint = (endpoint or "").strip()
    if not endpoint or len(endpoint) > 1024:
        raise VaultError("endpoint is required (a Key Vault URL, an AWS region, a GCP "
                         "project, or a Secrets Safe host)")
    try:
        canonical = workgroup_service.canonical_or_none(db, workgroup)
    except workgroup_service.WorkgroupError as e:
        raise VaultError(str(e)) from e
    row = db.query(SecretVault).filter(SecretVault.id == vault_id).first()
    if row is None:
        row = SecretVault(id=vault_id, backend=backend, endpoint=endpoint,
                          created_by_user_id=by_user_id)
        db.add(row)
    elif row.backend != backend:
        raise VaultError(f"Vault '{vault_id}' is a {row.backend} vault; its backend cannot "
                         "change (references to it name that backend).")
    row.endpoint = endpoint
    row.workgroup = canonical
    db.commit()
    _flush_resolution_cache()
    return _row(row)


def delete(db: Session, vault_id: str) -> None:
    from ..database import SecretVault
    row = db.query(SecretVault).filter(SecretVault.id == vault_id).first()
    if row is None:
        raise LookupError(f"Vault '{vault_id}' not found.")
    users = bound_by(db, vault_id)
    if users:
        raise VaultError(f"Vault '{vault_id}' is bound to workstream(s) "
                         f"{', '.join(users)}; unbind it there first.")
    db.delete(row)
    db.commit()
    _flush_resolution_cache()


def vault_of(ref: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """``(vault_id, workgroup)`` a reference resolves through, or ``(None, None)`` for a
    reference into the configured store (or not a reference at all)."""
    from . import config_service
    raw = (ref or "").strip()
    for prefix, backend in config_service._EXT_PREFIXES.items():
        if raw.startswith(prefix):
            vault_id, _ = config_service._parse_ref(raw, backend)
            if vault_id is None:
                return None, None
            return vault_id, config_service._vault_workgroup(vault_id, backend)
    return None, None


def bound_by(db: Session, vault_id: str) -> list:
    """Workstreams whose bound PRA credential reads from this vault."""
    from ..database import Workgroup
    from . import workgroup_service
    out = []
    for wg in db.query(Workgroup).all():
        ref = workgroup_service.bindings(wg.name, db).get("pra_credential_ref")
        if ref and vault_of(ref)[0] == vault_id:
            out.append(wg.name)
    return out


def _flush_resolution_cache() -> None:
    # A changed scope must apply now, not after the resolved-value cache expires.
    from . import config_service
    with config_service._ext_cache_lock:
        config_service._ext_cache.clear()
