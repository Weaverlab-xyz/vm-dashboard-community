"""PRA Shell Jump on a cloud VM that already exists — the opt-in half of VM PRA.

A deploy wires a Linux VM into PRA as one of its last steps, and until now that was the
only time it could happen: ``terraform_pra_service`` has ``provision_jump`` and
``remove_jump`` and the four runners called the first inline. This module lets the same
jump be added, re-applied or removed later from the VM's row — the pattern the Kubernetes
"PRA tunnel" action already uses — so PRA does not have to be decided in the deploy form.

The jump's state lives where it always has: the ``bt_shell_jump_id`` / ``bt_tf_state`` /
``bt_jump_group_name`` keys on the VM's DEPLOY job. That is deliberate. Every destroy path
(``aws_vm_service._run_destroy`` and its siblings) already removes a jump it finds there,
so a jump added afterwards is torn down with the VM by code that has never heard of this
module, and ``vm_suspend_policy`` sees it exactly as it sees a deploy-time one.

Gateway references. AWS and OCI count every live deploy against the shared gateway; GCP
and Azure count only rows carrying ``jumpoint_host_id`` (see ``jumpoint_host_service.
_active_gce_count``). A VM deployed with PRA off holds no reference there, so attaching
records the host it ensured — otherwise the next idle sweep would reap the gateway from
under the new jump. Detaching leaves that reference in place: the VM may have been wired
to its private address because of that gateway, and it is released with the VM.

Windows is refused for now. Its jumps (an OpenSSH Shell Jump with a PRA Vault account,
or an RDP jump) are wired by ``windows_server_hook`` from the build-time admin password,
which is not something a later action can recover.

Job type ``vm_pra``, metadata ``{cloud, deploy_job_id, action, jump_group?,
jumpoint_name?, pra_credential_ref?}``.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class VmPraError(Exception):
    """A request this module refuses, with a reason fit to show the operator."""


# Per cloud: the deploy job type, the tag the jump carries, and the config-key prefix of
# the cloud's own jump-group / Gateway overrides (blank = only the shared bt_* keys).
# The tags and key names are the ones each runner already uses.
CLOUDS = {
    "aws":   {"deploy_type": "ec2_deploy",   "tag": "AWS",   "prefix": ""},
    "azure": {"deploy_type": "azure_deploy", "tag": "Azure", "prefix": "azure_"},
    "gcp":   {"deploy_type": "gce_deploy",   "tag": "GCP",   "prefix": "gcp_"},
    "oci":   {"deploy_type": "oci_deploy",   "tag": "OCI",   "prefix": "oci_"},
}

# The metadata keys a Shell Jump owns on the deploy row. Cleared together on removal.
JUMP_KEYS = ("bt_shell_jump_id", "bt_jump_group_name", "bt_tf_state", "bt_error")


def resolve_target(cloud: str, jump_group: Optional[str] = None,
                   jumpoint_name: Optional[str] = None,
                   workgroup: Optional[str] = None) -> tuple[str, str]:
    """The Jump Group and Gateway names a jump for ``cloud`` lands in.

    An explicit value wins, then the VM's workstream default (its profile, see
    ``workgroup_service.PROFILE_KEYS``), then the cloud's own key
    (``gcp_bt_jump_group_name`` …), then the shared ``bt_*`` key — each config key with
    its env setting behind it, as ``config.py`` documents them. Shared with the four
    deploy runners, so a jump added later lands where a deploy-time one would.
    """
    from ..config import settings
    from . import config_service, workgroup_service

    def _key(name: str) -> str:
        return config_service.get(name) or getattr(settings, name, "") or ""

    team = workgroup_service.profile(workgroup) if workgroup else {}
    prefix = CLOUDS[cloud]["prefix"]
    group = ((jump_group or "").strip() or team.get("jump_group")
             or (prefix and _key(f"{prefix}bt_jump_group_name")) or _key("bt_jump_group_name"))
    jumpoint = ((jumpoint_name or "").strip() or team.get("jumpoint_name")
                or (prefix and _key(f"{prefix}jumpoint_name")) or _key("bt_jumpoint_name"))
    return group, jumpoint


def opted_in(enable_pra, workgroup: Optional[str] = None) -> bool:
    """Whether a deploy asked for its jump. An explicit True or False is the answer.
    ``None`` — every request made before the opt-in existed, and any API caller that
    leaves it out — takes the workstream's ``pra_enabled`` default, and with none set
    means yes, so PRA stays on by default while ``pra_enabled`` is.

    Deliberately NOT applied to the Gateway: a VM that opts out still gets the shared
    host ensured, so it is wired to the same address it would have been, and a jump
    added later from its row points where a deploy-time one would have."""
    if enable_pra is not None:
        return bool(enable_pra)
    if workgroup:
        from . import workgroup_service
        return workgroup_service.profile(workgroup).get("pra_enabled", True)
    return True


def client_secret_for(pra_credential_ref: Optional[str],
                      workgroup: Optional[str] = None) -> str:
    """The PRA API secret a per-deploy credential reference resolves to, or "" for the
    configured default. ``workgroup`` is the VM's: a reference into a vault scoped to a
    workstream (secret_vault_service) resolves only for that workstream's VMs."""
    if not (pra_credential_ref or "").strip():
        return ""
    from . import config_service
    return config_service.resolve_reference(pra_credential_ref.strip(), workgroup=workgroup)


def is_windows(meta: dict) -> bool:
    """Whether a deploy row is a Windows server. No runner records one flag for it, so
    this reads every trace a Windows build leaves: GCP's ``os_type``, Azure's request,
    and the admin-password keys only a Windows build writes."""
    if (meta.get("os_type") or "").lower() == "windows":
        return True
    if ((meta.get("req") or {}).get("os_type") or "").lower() == "windows":
        return True
    return bool(meta.get("admin_password_custody") or meta.get("windows_access"))


def _vm_name(cloud: str, meta: dict) -> str:
    if cloud == "azure":
        return meta.get("vm_name") or (meta.get("req") or {}).get("vm_name") or ""
    return meta.get("instance_name") or meta.get("instance_id") or ""


def _region(cloud: str, meta: dict) -> str:
    if cloud == "azure":
        return meta.get("location") or (meta.get("req") or {}).get("location") or ""
    if cloud == "gcp":
        from .gcp_vm_service import _region_from_zone
        return _region_from_zone(meta.get("zone") or "")
    return meta.get("region") or ""


def _hostname(meta: dict) -> str:
    """The address the jump points at. ``wired_address`` is the one the deploy handed to
    PRA, Entitle and Password Safe, and the one ``vm_suspend_policy`` re-checks, so a jump
    added later must use it too; the IPs cover rows written before it was recorded."""
    return meta.get("wired_address") or meta.get("private_ip") or meta.get("public_ip") or ""


def state(meta: dict) -> dict:
    """What a row shows about the VM's jump: ``{attached, jump_id, jump_group, error}``."""
    return {
        "attached": bool(meta.get("bt_shell_jump_id")),
        "jump_id": meta.get("bt_shell_jump_id") or None,
        "jump_group": meta.get("bt_jump_group_name") or None,
        "error": meta.get("bt_error") or None,
    }


async def _quiet(pct: int, msg: str) -> None:
    return None


def check_attachable(cloud: str, meta: dict) -> None:
    """Refuse up front what the job would refuse, so the API answers 400 instead of
    queueing a job that can only fail."""
    from . import config_service
    if cloud not in CLOUDS:
        raise VmPraError(f"Unknown cloud '{cloud}'")
    if not config_service.get_bool("pra_enabled"):
        raise VmPraError("BeyondTrust PRA is not enabled in Settings.")
    if is_windows(meta):
        raise VmPraError(
            "PRA access for a Windows server is wired at deploy time, from its build-time "
            "administrator password; it cannot be added afterwards yet.")
    if not _hostname(meta):
        raise VmPraError("The deploy recorded no address for this VM to point a jump at.")


async def attach(db: Session, deploy_job, *, jump_group: Optional[str] = None,
                 jumpoint_name: Optional[str] = None,
                 pra_credential_ref: Optional[str] = None,
                 cloud: str, progress=None) -> dict:
    """Create — or re-create with new options — the VM's Shell Jump.

    Idempotent: a VM that already has a jump has it removed first and created again, so
    "Re-apply" with a different Jump Group moves it rather than leaving a second one.
    Returns the metadata written onto the deploy row.
    """
    from . import job_service, jumpoint_host_service, terraform_pra_service
    say = progress or _quiet
    meta = deploy_job.metadata_dict or {}
    check_attachable(cloud, meta)

    updates: dict = {}
    region = _region(cloud, meta)
    await say(20, f"Ensuring the shared BeyondTrust Gateway host for {region or cloud}…")
    try:
        host_id = await jumpoint_host_service.ensure_jumpoint_host(cloud, region)
        if host_id and not meta.get("jumpoint_host_id"):
            updates["jumpoint_host_id"] = host_id
    except Exception as exc:  # noqa: BLE001 — best-effort, as at deploy time
        logger.warning("vm-pra: gateway ensure failed (non-fatal): %s", exc)
        await say(30, f"Gateway host ensure failed (non-fatal): {exc}")

    if meta.get("bt_tf_state"):
        await say(40, f"Removing the existing Shell Jump {meta.get('bt_shell_jump_id')}…")
        await terraform_pra_service.remove_jump(meta["bt_tf_state"])
        # Recorded straight away: if the create below fails, the row must not go on
        # naming a jump that no longer exists — destroy would try to remove it again.
        job_service.update_metadata(db, deploy_job.id, {k: None for k in JUMP_KEYS})

    group, jumpoint = resolve_target(cloud, jump_group, jumpoint_name,
                                     workgroup=deploy_job.workgroup)
    hostname = _hostname(meta)
    await say(60, f"Creating a Shell Jump to {hostname} in '{group}' via '{jumpoint}'…")
    result = await terraform_pra_service.provision_jump(
        vm_name=_vm_name(cloud, meta),
        hostname=hostname,
        jump_group_name=group,
        jumpoint_name=jumpoint,
        tag=CLOUDS[cloud]["tag"],
        client_secret=client_secret_for(pra_credential_ref, deploy_job.workgroup),
    )
    updates.update({
        "bt_shell_jump_id": result.get("shell_jump_id"),
        "bt_jump_group_name": result.get("jump_group_name"),
        "bt_tf_state": result.get("tf_state_json"),
        "bt_error": None,
    })
    job_service.update_metadata(db, deploy_job.id, updates)
    return updates


async def detach(db: Session, deploy_job, *, progress=None) -> dict:
    """Remove the VM's Shell Jump and clear its keys from the deploy row."""
    from . import job_service, terraform_pra_service
    say = progress or _quiet
    meta = deploy_job.metadata_dict or {}
    jump_id = meta.get("bt_shell_jump_id")
    if not jump_id:
        raise VmPraError("This VM has no PRA Shell Jump to remove.")
    if not meta.get("bt_tf_state"):
        raise VmPraError(
            f"Shell Jump {jump_id} was provisioned before the Terraform migration and has "
            "no state to destroy it with; remove it in PRA by hand.")
    await say(40, f"Removing Shell Jump {jump_id}…")
    await terraform_pra_service.remove_jump(meta["bt_tf_state"])
    cleared = {k: None for k in JUMP_KEYS}
    job_service.update_metadata(db, deploy_job.id, cleared)
    return {"bt_shell_jump_removed": jump_id}


def queue(db: Session, *, cloud: str, deploy_job, payload, username: str):
    """Queue the ``vm_pra`` job for one VM. The caller has already resolved the deploy
    row and checked the user may act on it; this refuses what cannot work and returns
    the new job."""
    from . import job_service
    if payload.action == "register":
        check_attachable(cloud, deploy_job.metadata_dict or {})
    elif not (deploy_job.metadata_dict or {}).get("bt_shell_jump_id"):
        raise VmPraError("This VM has no PRA Shell Jump to remove.")
    job = job_service.create_job(
        db, job_type="vm_pra", created_by=username,
        workgroup=deploy_job.workgroup,
        metadata={
            "cloud": cloud, "deploy_job_id": deploy_job.id, "action": payload.action,
            "instance": payload.instance,
            "jump_group": payload.jump_group, "jumpoint_name": payload.jumpoint_name,
            "pra_credential_ref": payload.pra_credential_ref,
        },
    )
    job_service.log_audit(
        db, username, f"vm_pra_{payload.action}",
        details={"cloud": cloud, "instance": payload.instance, "job_id": job.id})
    return job


async def run(db: Session, job_id: str, meta: dict) -> None:
    """Worker entry for a ``vm_pra`` job."""
    from . import job_service
    from ..api.websocket import broadcast_progress

    async def _progress(pct: int, msg: str) -> None:
        await broadcast_progress(job_id, pct, msg)

    job_service.set_running(db, job_id)
    try:
        deploy_job = job_service.get_job(db, meta.get("deploy_job_id") or "")
        if deploy_job is None or (deploy_job.metadata_dict or {}).get("destroyed"):
            raise VmPraError("The VM's deploy record is gone; it may have been destroyed.")
        if meta.get("action") == "remove":
            await _progress(10, "Removing the VM's PRA Shell Jump…")
            out = await detach(db, deploy_job, progress=_progress)
        else:
            await _progress(10, "Adding the VM to PRA…")
            written = await attach(
                db, deploy_job, cloud=meta.get("cloud") or "",
                jump_group=meta.get("jump_group"), jumpoint_name=meta.get("jumpoint_name"),
                pra_credential_ref=meta.get("pra_credential_ref"), progress=_progress)
            out = {"bt_shell_jump_id": written.get("bt_shell_jump_id"),
                   "bt_jump_group_name": written.get("bt_jump_group_name")}
    except Exception as exc:
        job_service.set_failed(db, job_id, str(exc))
        logger.exception("vm-pra job failed job=%s", job_id)
        return
    job_service.set_completed(db, job_id, out)
