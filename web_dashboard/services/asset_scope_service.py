"""Stored assets an administrator has limited to one workstream.

A playbook, script or installer on any storage backend is global by default: everyone
with ``storage:read`` sees it and everyone with ``config_mgmt:write`` can run it. An
administrator can scope one to a workstream (``storage_asset_scopes``); from then on
only that workstream's members — and administrators — list, fetch, run or delete it.

Who is a member is the inventory's own rule (``inventory_service.accessible_workgroups``:
``None`` for an administrator, else the user's workgroups), so a change to that rule
reaches assets too.

Enforced at the request edge — the list, fetch, run and delete endpoints — never in the
job runners, which only run what was already authorised when it was queued. An asset
the user may not see is answered as missing (404), the same as one that does not exist.

The backend "" means "the active backend", as it does on ``/run``; every function here
resolves it before keying a row, so a scope set on ``s3`` applies to a run that named
no backend while ``s3`` is active.
"""
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy.orm import Session


class AssetScopeError(ValueError):
    """Bad input to :func:`set_scope` (400 at the API edge)."""


class AssetScopeDenied(LookupError):
    """The asset is scoped to a workstream this user is not in (404 at the API edge)."""


def _backend(backend: Optional[str]) -> str:
    if backend:
        return backend
    from . import storage_service
    return storage_service.active_backend() or ""


def _accessible(user):
    from . import inventory_service
    return inventory_service.accessible_workgroups(user)


def scopes(db: Session) -> dict:
    """``{(backend, name): workgroup}`` for every scoped asset."""
    from ..database import StorageAssetScope
    return {(r.backend, r.name): r.workgroup for r in db.query(StorageAssetScope).all()}


def scope_of(db: Session, backend: Optional[str], name: str) -> Optional[str]:
    from ..database import StorageAssetScope
    row = db.query(StorageAssetScope).filter(
        StorageAssetScope.backend == _backend(backend),
        StorageAssetScope.name == name).first()
    return row.workgroup if row else None


def _allowed(accessible, workgroup: Optional[str]) -> bool:
    return accessible is None or not workgroup or workgroup in accessible


def visible(user, workgroup: Optional[str]) -> bool:
    """Whether ``user`` may see an asset scoped to ``workgroup`` (None = global)."""
    return _allowed(_accessible(user), workgroup)


def filter_items(db: Session, user, items: Iterable[dict],
                 backend: Optional[str] = None) -> list:
    """The listed assets ``user`` may see, each with its ``workgroup`` (None = global).
    An item names its own backend (``/list-all``); otherwise ``backend`` applies."""
    table = scopes(db)
    accessible = _accessible(user)
    default = _backend(backend)
    out = []
    for item in items:
        wg = table.get((item.get("backend") or default, item.get("name")))
        if _allowed(accessible, wg):
            out.append({**item, "workgroup": wg})
    return out


def filter_names(db: Session, user, names: Iterable[str],
                 backend: Optional[str] = None) -> list:
    """:func:`filter_items` for a bare list of names on one backend."""
    table = scopes(db)
    accessible = _accessible(user)
    b = _backend(backend)
    return [n for n in names if _allowed(accessible, table.get((b, n)))]


def check(db: Session, user, backend: Optional[str], name: str) -> None:
    """Raise :class:`AssetScopeDenied` unless ``user`` may use this asset."""
    accessible = _accessible(user)
    if accessible is None:
        return      # an administrator sees every asset; no lookup needed
    if not _allowed(accessible, scope_of(db, backend, name)):
        raise AssetScopeDenied(f"Asset '{name}' not found.")


def set_scope(db: Session, backend: Optional[str], name: str,
              workgroup: Optional[str], by: Optional[str] = None) -> Optional[str]:
    """Scope an asset to ``workgroup``, or make it global again with None/"". Returns the
    canonical workstream name (or None). The caller must be an administrator."""
    from ..database import StorageAssetScope
    from . import workgroup_service
    if not isinstance(name, str) or not name.strip() or len(name) > 255:
        raise AssetScopeError("name must be an asset name")
    name = name.strip()
    if ".." in name or name.startswith("/"):
        raise AssetScopeError(f"'{name[:60]}' is not an asset name")
    b = _backend(backend)
    if not b or len(b) > 32:
        raise AssetScopeError("bad storage backend")
    row = db.query(StorageAssetScope).filter(
        StorageAssetScope.backend == b, StorageAssetScope.name == name).first()
    if not (workgroup or "").strip():
        if row is not None:
            db.delete(row)
            db.commit()
        return None
    try:
        canonical = workgroup_service.canonical_or_none(db, workgroup)
    except workgroup_service.WorkgroupError as e:
        raise AssetScopeError(str(e)) from e
    if row is None:
        row = StorageAssetScope(backend=b, name=name, workgroup=canonical)
        db.add(row)
    row.workgroup = canonical
    row.updated_by = by
    db.commit()
    return canonical


def on_delete(db: Session, backend: Optional[str], name: str) -> None:
    """The asset is gone: drop its scope so a later upload of the same name starts global
    — and so a workstream is not held undeletable by an asset that no longer exists."""
    from ..database import StorageAssetScope
    db.query(StorageAssetScope).filter(
        StorageAssetScope.backend == _backend(backend),
        StorageAssetScope.name == name).delete()
    db.commit()


def on_move(db: Session, name: str, from_backend: str, to_backend: str) -> None:
    """The asset moved backends: its scope moves with it, replacing any on the target."""
    wg = scope_of(db, from_backend, name)
    if wg is None:
        return
    on_delete(db, to_backend, name)
    from ..database import StorageAssetScope
    db.query(StorageAssetScope).filter(
        StorageAssetScope.backend == from_backend,
        StorageAssetScope.name == name).update({"backend": to_backend})
    db.commit()


def count_for(db: Session, workgroup: str) -> int:
    from ..database import StorageAssetScope
    return db.query(StorageAssetScope).filter(StorageAssetScope.workgroup == workgroup).count()


def on_copy(db: Session, name: str, from_backend: str, to_backend: str) -> None:
    """The asset was copied to another backend (a migrate): the copy keeps the scope, or a
    copy would quietly turn a team's asset global."""
    wg = scope_of(db, from_backend, name)
    if wg is not None:
        set_scope(db, to_backend, name, wg)


def check_replace(db: Session, user, backend: Optional[str], name: str) -> None:
    """Raise :class:`AssetScopeDenied` if an upload of ``name`` would overwrite an asset
    scoped to a workstream ``user`` is not in — the scope would stay, so that team's runs
    would execute the uploader's file."""
    accessible = _accessible(user)
    if accessible is None:
        return
    if not _allowed(accessible, scope_of(db, backend, name)):
        raise AssetScopeDenied(
            f"An asset named '{name}' already exists and you cannot replace it.")
