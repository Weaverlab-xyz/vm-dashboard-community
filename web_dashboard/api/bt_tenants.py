"""The BeyondTrust tenant registry — CRUD and a credential check.

  GET    /api/pov/tenants                — every registered tenant, grouped-ready
  POST   /api/pov/tenants                — register one
  PATCH  /api/pov/tenants/{id}           — edit; a blank secret keeps the stored one
  POST   /api/pov/tenants/{id}/default   — make it the default for its kind
  POST   /api/pov/tenants/{id}/verify    — check the credential against the product
  DELETE /api/pov/tenants/{id}           — remove it, unless a live POV points at it

Mounted **under the /api/pov prefix and behind the same feature gate**, which is a
decision rather than a filing convenience. The registry exists because a POV instance has
many tenants and a demo instance has one; on a demo instance the singletons are the right
answer and a second place to configure a tenant would be a second answer to "which
tenant?" — the exact ambiguity ``install_profile`` was introduced to remove. So on demo
these routes 404 like the rest of the POV feature, and ``bt_tenant_service.resolve``'s
step-4 fallback is what keeps every existing call site working there untouched.

Writes are **admin-only**. Reads are not: the POV create form needs the list to build its
pickers, and what it gets back never contains a secret — only whether one is stored.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..services import bt_tenant_service, bt_tenant_verify
from .auth import get_current_user, require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pov/tenants", tags=["pov"])


class TenantCreate(BaseModel):
    kind: str
    name: str
    base_url: str
    client_id: str = ""
    # One of these, never both. The service refuses both rather than picking, because the
    # choice decides where a customer's credential lives.
    secret: str = ""
    secret_ref: str = ""
    options: dict = {}
    is_default: bool = False


class TenantUpdate(BaseModel):
    """Every field optional. ``None`` means "not supplied" and the row keeps what it has.

    ``secret`` is the one that matters: the form cannot render what is stored, so a blank
    string has to mean *keep*, not *clear*. Any other rule means saving a Jump Group name
    wipes the credential.
    """
    name: str | None = None
    base_url: str | None = None
    client_id: str | None = None
    secret: str | None = None
    secret_ref: str | None = None
    options: dict | None = None
    is_active: bool | None = None
    is_default: bool | None = None


def _refuse(exc: bt_tenant_service.BTTenantError) -> HTTPException:
    """A tenant refusal is a 409, never a 500.

    Every message this service raises is something an operator can act on — a name clash,
    a tenant still in use, a secret reference that is not one. Returning 500 would file
    all of them as "the dashboard broke" and lose the remedy that is already in the text.
    """
    return HTTPException(status_code=409, detail=str(exc))


@router.get("")
async def list_tenants(kind: str = "", db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    """Every tenant, plus the registry's own shape so the UI does not hardcode it.

    ``kinds`` carries the labels, the allowlisted option keys and which are verifiable —
    the same reason ``/api/pov/platforms`` serves its capability table. A UI that hardcodes
    "Entitle has no Verify button" is a UI that keeps hiding it after Entitle gains one.
    """
    try:
        rows = bt_tenant_service.list_tenants(db, kind)
    except bt_tenant_service.BTTenantError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "tenants": [bt_tenant_service.serialize(db, r) for r in rows],
        "kinds": [
            {"kind": k,
             "label": bt_tenant_service.LABELS[k],
             "option_keys": list(bt_tenant_service.OPTION_KEYS.get(k, ())),
             "option_labels": {key: bt_tenant_service.OPTION_LABELS.get(key, key)
                               for key in bt_tenant_service.OPTION_KEYS.get(k, ())},
             "required_options": list(bt_tenant_service.REQUIRED_OPTIONS.get(k, ())),
             "verifiable": k in bt_tenant_service.VERIFIABLE_KINDS}
            for k in bt_tenant_service.VALID_KINDS
        ],
    }


@router.post("", status_code=201)
async def create_tenant(payload: TenantCreate, db: Session = Depends(get_db),
                        current_user: User = Depends(require_admin)):
    try:
        return {"tenant": bt_tenant_service.create(
            db, kind=payload.kind, name=payload.name, base_url=payload.base_url,
            client_id=payload.client_id, secret=payload.secret,
            secret_ref=payload.secret_ref, options=payload.options,
            is_default=payload.is_default,
            created_by=getattr(current_user, "username", ""))}
    except bt_tenant_service.BTTenantError as exc:
        raise _refuse(exc) from exc


@router.patch("/{tenant_id}")
async def update_tenant(tenant_id: str, payload: TenantUpdate,
                        db: Session = Depends(get_db),
                        current_user: User = Depends(require_admin)):
    # exclude_unset, not exclude_none: the service distinguishes "not supplied" from
    # "supplied as empty", and clearing a secret_ref is a real thing an operator does.
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="nothing to update")
    try:
        return {"tenant": bt_tenant_service.update(db, tenant_id, **fields)}
    except bt_tenant_service.BTTenantError as exc:
        raise _refuse(exc) from exc


@router.post("/{tenant_id}/default")
async def make_default(tenant_id: str, db: Session = Depends(get_db),
                       current_user: User = Depends(require_admin)):
    try:
        return {"tenant": bt_tenant_service.set_default(db, tenant_id)}
    except bt_tenant_service.BTTenantError as exc:
        raise _refuse(exc) from exc


@router.post("/{tenant_id}/verify")
async def verify_tenant(tenant_id: str, db: Session = Depends(get_db),
                        current_user: User = Depends(require_admin)):
    """Check the credential against the product, and record what happened.

    Synchronous rather than a job: it is one token handshake, it is what an operator is
    sitting and waiting for, and a job row per credential check would bury the POV's real
    jobs. The result is stored either way — a failure an operator navigates away from is
    still a failure the row should show.

    **A failed check is a 200 with ``ok: false``**, not an error status. The request
    succeeded; the credential is what did not work, and that is an answer. It is also why
    no message here is ever built from a caught exception: ``verify`` returns its outcome,
    so there is no exception at this boundary to accidentally echo.
    """
    try:
        tenant = bt_tenant_service.resolve_by_id(db, tenant_id)
        # Raises only for "this kind cannot be checked" and "there is no URL" — refusals
        # about the request rather than outcomes, so they 409 and nothing is recorded
        # against the row.
        ok, detail = await bt_tenant_verify.verify(tenant)
    except bt_tenant_service.BTTenantError as exc:
        raise _refuse(exc) from exc
    except Exception as exc:  # noqa: BLE001
        # A bug, not a tenant problem, and deliberately not echoed: a bug's `str()` is
        # written for a developer reading a traceback and can carry local paths, a
        # resolved address, or a chained cause from somewhere unrelated. The traceback
        # goes to the log; the operator gets the type and a pointer to it.
        logger.warning("tenant %s: verify raised unexpectedly", tenant_id, exc_info=True)
        detail = (f"the check failed unexpectedly ({type(exc).__name__}). This is a "
                  f"dashboard fault rather than a tenant one — see the dashboard log.")
        row = bt_tenant_service.record_result(db, tenant_id, error=detail)
        return {"ok": False, "detail": detail, "tenant": row}

    # Recorded either way. The next person to open this page needs to see that it was
    # checked and failed, not an empty column.
    row = bt_tenant_service.record_result(db, tenant_id, error="" if ok else detail)
    return {"ok": ok, "detail": detail, "tenant": row}


@router.delete("/{tenant_id}", status_code=204)
async def delete_tenant(tenant_id: str, db: Session = Depends(get_db),
                        current_user: User = Depends(require_admin)):
    try:
        bt_tenant_service.delete(db, tenant_id)
    except bt_tenant_service.BTTenantError as exc:
        raise _refuse(exc) from exc
