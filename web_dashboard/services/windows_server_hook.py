"""What a Windows server build does after its VM exists, and undoes at destroy.

Shared by ``azure_vm_service`` and ``aws_vm_service`` so the two clouds cannot drift.
Linux builds have their own per-cloud steps (Shell Jump, Entitle SSH, the SSH-rotation
Password Safe plugins); none of those apply to a Windows guest, which until this module
existed simply got none of them.

:func:`wire` runs, in order:

1. **Password Safe** (per-build opt-in + global flag): onboard the VM as a
   password-managed system, seeded with the build-time administrator password and then
   rotated (``ps_vm_hook.register_windows``). Once Password Safe holds a working
   credential, the build-time copy in the secret manager is deleted — after a rotation
   it is wrong, and a wrong password that LOOKS authoritative is worse than none. If
   onboarding fails, the copy stays as the break-glass credential.
2. **PRA**: a Remote RDP jump item. When Password Safe owns the credential the jump gets
   no PRA Vault copy (PRA's Password Safe integration injects the current one, and a copy
   would go stale at the first rotation); otherwise the build-time password is vaulted
   in PRA for injection, as VDI seats do.

Everything here is best-effort: the VM exists and its password is stored before any of
it runs, so a failure is recorded on the job and never fails the deploy.

Job-metadata keys written: ``admin_password_custody`` ("secret_manager" |
``"passwordsafe_managed"``), ``admin_password_retired``, ``bt_rdp_jump_id``,
``bt_rdp_tf_state``, ``bt_rdp_error``, ``windows_secret_cleanup_error`` and the
``ps_*`` keys ``ps_vm_hook`` writes.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

CUSTODY_SECRET_MANAGER = "secret_manager"
CUSTODY_PASSWORD_SAFE = "passwordsafe_managed"


def _cfg(key: str) -> str:
    from . import config_service
    from ..config import settings
    return str(config_service.get(key) or getattr(settings, key, "") or "").strip()


def _vault_group_id():
    raw = _cfg("pra_windows_vault_account_group_id")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


async def wire(db, job_id: str, *, vm_name: str, hostname: str, username: str,
               password: str, result: dict, tag: str, register_in_passwordsafe: bool,
               pra_enabled: bool, jump_group: str, jumpoint_name: str,
               client_secret: str = "") -> None:
    """Password Safe, then the PRA RDP jump. See the module docstring."""
    from . import job_service, ps_vm_hook, windows_admin_secret

    result["admin_password_custody"] = CUSTODY_SECRET_MANAGER

    if register_in_passwordsafe and ps_vm_hook.registration_enabled():
        await ps_vm_hook.register_windows(
            db, job_id, vm_name, hostname, result=result, tag=tag,
            username=username, password=password)
        if ps_vm_hook.password_safe_holds_credential(result):
            result["admin_password_custody"] = CUSTODY_PASSWORD_SAFE
            backend = result.get("admin_password_backend") or ""
            ref = result.get("admin_password_ref") or ""
            err = windows_admin_secret.delete(backend, ref)
            if err:
                # Not fatal, and not silent: an orphaned copy that no longer matches the
                # account is something an operator should go and remove.
                result["windows_secret_cleanup_error"] = (
                    f"Password Safe now manages the account, but the build-time copy "
                    f"{ref!r} in {backend} could not be deleted: {err}")
            else:
                result["admin_password_retired"] = True
                result.pop("admin_password_backend", None)
                result.pop("admin_password_ref", None)
    elif register_in_passwordsafe:
        result["ps_error"] = ("Password Safe registration is disabled globally "
                              "(passwordsafe_registration_enabled).")

    if not pra_enabled:
        return
    if not (jump_group and jumpoint_name):
        result["bt_rdp_error"] = "no PRA Jump Group / Jumpoint configured for this cloud"
        return
    from . import terraform_pra_service
    ps_owned = result["admin_password_custody"] == CUSTODY_PASSWORD_SAFE
    job_service.update_progress(db, job_id, 97, "Creating the PRA Remote RDP jump item…")
    try:
        jump = await terraform_pra_service.provision_rdp_jump(
            name=vm_name, hostname=hostname,
            jump_group_name=jump_group, jumpoint_name=jumpoint_name,
            rdp_username=username, tag=tag,
            admin_password="" if ps_owned else password,
            vault_account_name="" if ps_owned else f"{vm_name}-admin",
            vault_account_group_id=_vault_group_id(),
            client_secret=client_secret,
        )
        result["bt_rdp_jump_id"] = jump.get("rdp_jump_id") or None
        result["bt_rdp_vault_account_id"] = jump.get("vault_account_id")
        result["bt_rdp_tf_state"] = jump.get("tf_state_json")
        result["bt_jump_group_name"] = jump.get("jump_group_name")
    except Exception as e:  # noqa: BLE001
        logger.warning("PRA RDP jump for %s failed: %s", vm_name, e)
        result["bt_rdp_error"] = str(e)


def entra_requested(req) -> tuple[bool, bool]:
    """``(join, intune)`` for one Azure Windows deploy: the request's own choice, else the
    ``azure_windows_entra_join`` / ``azure_windows_entra_intune_enroll`` defaults."""
    from . import config_service

    def pick(field, key):
        v = getattr(req, field, None)
        return bool(v) if v is not None else config_service.get_bool(key, False)

    join = pick("entra_join", "azure_windows_entra_join")
    return join, join and pick("entra_intune_enroll", "azure_windows_entra_intune_enroll")


def _group_ids(key: str) -> list:
    return [g.strip() for g in _cfg(key).replace(";", ",").split(",") if g.strip()]


async def entra_join_azure(db, job_id: str, *, rg: str, vm_name: str, location: str,
                           vm_id: str, intune: bool, result: dict) -> None:
    """Finish an Entra ID join on an Azure VM created with a system identity.

    Installs AADLoginForWindows, then grants the configured Entra groups login on THIS
    VM: ``azure_entra_vm_admin_group_ids`` → Virtual Machine Administrator Login,
    ``azure_entra_vm_user_group_ids`` → Virtual Machine User Login. Assignments are
    recorded in ``entra_role_assignments`` so destroy can remove them.

    Never fails the deploy: the local administrator (vaulted / Password Safe-managed)
    still reaches the VM, so a failure is a warning on the job (``entra_error``,
    ``entra_role_errors``)."""
    from . import azure_service, job_service
    job_service.update_progress(db, job_id, 92, "Joining the VM to Entra ID…")
    try:
        result["entra_join"] = await azure_service.enable_entra_login(
            rg, vm_name, location, intune=intune)
    except Exception as e:  # noqa: BLE001
        logger.warning("Entra join of %s failed: %s", vm_name, e)
        result["entra_error"] = str(e)
        return

    assignments, errors = [], []
    for key, role in (("azure_entra_vm_admin_group_ids", "admin"),
                      ("azure_entra_vm_user_group_ids", "user")):
        for gid in _group_ids(key):
            try:
                r = await azure_service.ensure_role_assignment(
                    scope=vm_id, role=azure_service.ENTRA_VM_LOGIN_ROLES[role],
                    principal_id=gid, principal_type="Group")
                assignments.append({"scope": vm_id, "name": r["name"], "role": role,
                                    "group": gid, "created": r.get("created", True)})
            except Exception as e:  # noqa: BLE001
                hint = (" — the dashboard's service principal needs "
                        "Microsoft.Authorization/roleAssignments/write (Role Based Access "
                        "Control Administrator or User Access Administrator) on the VM's "
                        "resource group" if "403" in str(e) else "")
                errors.append(f"{role} login for group {gid}: {e}{hint}")
    if assignments:
        result["entra_role_assignments"] = assignments
    if errors:
        result["entra_role_errors"] = errors
    if not (_group_ids("azure_entra_vm_admin_group_ids")
            or _group_ids("azure_entra_vm_user_group_ids")):
        result["entra_note"] = ("joined, but no Entra groups are configured for login "
                                "(azure_entra_vm_admin_group_ids / _user_group_ids) — "
                                "assign Virtual Machine Administrator/User Login by hand")


async def teardown_entra(meta: dict, result: dict) -> None:
    """Remove the role assignments :func:`entra_join_azure` CREATED. Run before the VM
    is deleted, while their scope still exists. One that already existed (``created``
    False) was someone else's and stays."""
    from . import azure_service
    errors = []
    for a in (meta or {}).get("entra_role_assignments") or []:
        if not a.get("created", True):
            continue
        try:
            await azure_service.delete_role_assignment(a["scope"], a["name"])
        except Exception as e:  # noqa: BLE001
            errors.append(f"{a.get('name')}: {e}")
    if errors:
        result["entra_role_errors"] = errors


async def teardown(meta: dict, result: dict) -> None:
    """Undo :func:`wire` and the stored password. Best-effort, never raises.

    Order: the RDP jump (and its PRA Vault copy) first, then the stored password. The
    Password Safe managed system is NOT handled here — ``ps_vm_hook.deregister``, which
    every destroy path already calls, removes it from the same ``ps_*`` keys."""
    from . import windows_admin_secret
    meta = meta or {}
    state = meta.get("bt_rdp_tf_state")
    if state:
        try:
            from . import terraform_pra_service
            await terraform_pra_service.remove_rdp_jump(state)
            result["bt_rdp_jump_removed"] = meta.get("bt_rdp_jump_id") or True
        except Exception as e:  # noqa: BLE001
            logger.warning("RDP jump removal failed: %s", e)
            result["bt_rdp_error"] = f"RDP jump removal failed: {e}"
    backend, ref = meta.get("admin_password_backend"), meta.get("admin_password_ref")
    if backend and ref:
        err = windows_admin_secret.delete(backend, ref)
        if err:
            result["windows_secret_cleanup_error"] = f"{backend}:{ref}: {err}"
        else:
            result["admin_password_deleted"] = ref
