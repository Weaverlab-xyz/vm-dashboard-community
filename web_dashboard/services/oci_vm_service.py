"""OCI compute deploy / destroy execution, run by the job runner.

Dispatched by ``jobs_worker`` as ``oci_deploy`` and ``oci_destroy``. ``api/oci`` keeps
the route handlers, which validate, persist the request on the job and return; the
worker reconstructs the call from that metadata and runs it here, so a gunicorn recycle
mid-deploy no longer strands the job or orphans the instance.

Lives in ``services/`` because the job runner has to import it, and a worker reaching
into the API package is backwards (see services/aws_vm_service for the AWS counterpart).
"""
import logging
from typing import Optional

from ..config import settings
from ..models.oci import OCIDeployRequest
from . import cache_service, job_service, oci_service

logger = logging.getLogger(__name__)


def _oci_cfg(key: str, fallback: str = "") -> str:
    """Own copy rather than an import from ``api.oci`` — that is the dependency this
    module exists to remove."""
    from ..services import config_service
    return config_service.get(key) or getattr(settings, key, None) or fallback


def _region() -> str:
    return _oci_cfg("oci_region") or "us-ashburn-1"


def _get_db_session():
    from ..database import SessionLocal
    return SessionLocal()


def _finish_parent(parent_job_id: str, child_job_ids: list) -> None:
    """Give the batch parent a terminal status once its children are done.

    Own session: `run()` holds none, and the per-child sessions are opened and closed
    inside `_run_deploy`. Best-effort — a parent left `running` is cosmetic next to a
    batch whose VMs all exist."""
    db = _get_db_session()
    try:
        job_service.finish_batch_parent(db, parent_job_id, child_job_ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not close out batch parent %s: %s", parent_job_id, exc)
    finally:
        db.close()


def _ssh_key_secret(payload: OCIDeployRequest) -> str:
    """The Vault secret this launch reads its public key from — per-launch override
    first, then the configured default. Shared by the single and batch paths so they
    can't resolve to different secrets."""
    from ..services import config_service
    return payload.ssh_key_secret_override or config_service.get("oci_ssh_key_secret") or ""


# ── Job-runner entry point ────────────────────────────────────────────────────

async def run(job_id: str, job_type: str, meta: dict) -> None:
    """Run one OCI job. Every argument comes from the metadata the endpoint persisted."""
    if job_type == "oci_deploy":
        await _run_deploy(job_id, OCIDeployRequest(**meta["req"]), meta["compartment_ocid"])
    elif job_type == "oci_bulk_deploy":
        # The children were created `queued` so the runner's claim query (which filters
        # status='pending') cannot pick them up alongside this parent; _run_bulk_deploy
        # drives them and owns their status. Each child carries its own full request.
        job_items = [(c["job_id"], OCIDeployRequest(**c["req"])) for c in meta["children"]]
        await _run_bulk_deploy(job_items, meta["compartment_ocid"])
        # The parent has no terminal status of its own — the worker only marks a job
        # failed when dispatch raises, so without this it stays `running` at 0%.
        _finish_parent(job_id, [c["job_id"] for c in meta["children"]])
    elif job_type == "oci_destroy":
        await _run_destroy(job_id, meta["instance_ocid"], meta.get("deploy_job_id"))


async def _run_deploy(job_id: str, payload: OCIDeployRequest, compartment: str,
                      *, ssh_public_key: Optional[str] = None) -> None:
    """Deploy one OCI compute instance.

    ``ssh_public_key`` is injected by ``_run_bulk_deploy`` so a batch resolves the Vault
    secret once instead of once per VM. Left at None — the single-deploy path — this
    fetches it itself, exactly as before batches existed. Keyword-only so the existing
    ``oci_deploy`` call in ``run()`` is untouched."""
    from ..services import config_service as _cfg_svc
    db = _get_db_session()
    try:
        job_service.set_running(db, job_id)

        # Resolve the SSH public key (per-launch override wins over the default).
        secret = _ssh_key_secret(payload)
        if ssh_public_key is None:
            ssh_public_key = ""
            if secret:
                job_service.update_progress(db, job_id, 15, "Retrieving SSH public key from OCI Vault…")
                try:
                    ssh_public_key = await oci_service.get_ssh_public_key(secret)
                except Exception as exc:
                    logger.warning("Could not fetch OCI SSH key: %s", exc)

        job_service.update_progress(db, job_id, 25, "Launching compute instance…")
        result = await oci_service.launch_instance(
            compartment_id=compartment,
            availability_domain=payload.availability_domain,
            instance_name=payload.instance_name,
            shape=payload.shape,
            image_ocid=payload.image_ocid,
            subnet_ocid=payload.subnet_ocid or _cfg_svc.get("oci_default_subnet_ocid") or "",
            assign_public_ip=payload.assign_public_ip,
            ssh_public_key=ssh_public_key,
            ocpus=payload.ocpus,
            memory_gb=payload.memory_gb,
            boot_volume_gb=payload.boot_volume_gb,
            workgroup=payload.workgroup,
        )

        hostname = result.get("public_ip") or result.get("private_ip") or payload.instance_name
        final_meta = {
            "instance_ocid":  result["ocid"],
            "instance_name":  result["display_name"],
            "shape":          result.get("shape"),
            "ocpus":          result.get("ocpus"),
            "memory_gb":      result.get("memory_gb"),
            "lifecycle_state": result.get("lifecycle_state"),
            "public_ip":      result.get("public_ip"),
            "private_ip":     result.get("private_ip"),
            "availability_domain": result.get("availability_domain"),
            "image_ocid":     payload.image_ocid,
            "image_name":     payload.image_name,
            "region":         _region(),
        }

        # ── BeyondTrust PRA — Shell Jump (optional) ───────────────────────────
        if _cfg_svc.get_bool("beyondtrust_enabled"):
            from ..services import terraform_pra_service
            jump_group = (payload.jump_group or _cfg_svc.get("oci_bt_jump_group_name")
                          or _cfg_svc.get("bt_jump_group_name") or settings.bt_jump_group_name)
            jumpoint_name = (payload.jumpoint_name or _cfg_svc.get("oci_jumpoint_name")
                             or _cfg_svc.get("bt_jumpoint_name") or settings.bt_jumpoint_name)
            job_service.update_progress(db, job_id, 90, f"Instance launched ({hostname}), provisioning Shell Jump…")
            try:
                bt_result = await terraform_pra_service.provision_jump(
                    vm_name=payload.instance_name, hostname=hostname,
                    jump_group_name=jump_group, jumpoint_name=jumpoint_name, tag="OCI",
                )
                final_meta["bt_shell_jump_id"] = bt_result.get("shell_jump_id")
                final_meta["bt_jump_group_name"] = bt_result.get("jump_group_name")
                final_meta["bt_tf_state"] = bt_result.get("tf_state_json")
                job_service.update_progress(db, job_id, 95,
                    f"Shell Jump created (ID: {bt_result.get('shell_jump_id')}, group: {jump_group})")
            except Exception as bt_exc:
                final_meta["bt_error"] = str(bt_exc)
                job_service.update_progress(db, job_id, 95,
                    f"Instance deployed but Shell Jump provisioning failed: {bt_exc}")
        else:
            job_service.update_progress(db, job_id, 95, "Instance launched.")

        # Entitle — register as SSH ephemeral-accounts integration (per-build opt-in).
        from ..services import entitle_vm_hook
        if payload.register_in_entitle and entitle_vm_hook.registration_enabled():
            # Resolved login user (config override → request field → opc default);
            # _cfg_svc.get is store-only so a blank request field falls through.
            await entitle_vm_hook.register(db, job_id, payload.instance_name, hostname,
                                           private=not payload.assign_public_ip,
                                           result=final_meta, tag="OCI",
                                           sudo_user=_cfg_svc.get("oci_ssh_username") or payload.ssh_username or "opc",
                                           ssh_key_secret=secret)

        # Password Safe — onboard as a managed system + account (per-build opt-in).
        # OCI uses the traditional "ssh" method (no cloud-native plugin), so the
        # chosen key secret must carry the VM's private key.
        from ..services import ps_vm_hook
        if payload.register_in_passwordsafe and ps_vm_hook.registration_enabled():
            await ps_vm_hook.register(db, job_id, payload.instance_name, hostname,
                                      result=final_meta, tag="OCI", ssh_key_secret=secret)

        job_service.set_completed(db, job_id, final_meta)
        # Prefix, not an exact key: the instance list is cached per region+compartment
        # (api/oci._cache_key), and this runner has no request context to rebuild the
        # one key with. Dropping every region's entry is also the correct blast radius
        # — a deploy/terminate changes the inventory whichever region it landed in.
        await cache_service.invalidate_prefix("oci_instances")
    except Exception as exc:
        logger.error("OCI deploy failed for job %s: %s", job_id, exc)
        job_service.set_failed(db, job_id, str(exc))
    finally:
        db.close()


async def _run_bulk_deploy(job_items: list, compartment: str) -> None:
    """Deploy a batch of OCI instances, resolving the Vault key once for the whole run.

    ``job_items`` is ``[(job_id, OCIDeployRequest)]`` — each child carries its own
    expanded instance name, everything else is shared.

    The simplest of the four batch runners: OCI provisions no Jumpoint of its own (it
    binds to an existing one named in config), no NAT instance and no quota precheck,
    so the only thing worth hoisting is the key fetch. ``_run_deploy`` owns
    set_running/set_completed/set_failed per child, so one instance failing fails that
    row alone and the batch carries on."""
    if not job_items:
        return
    # Every child shares the payload bar its name, so they resolve to one secret.
    shared_key: Optional[str] = None
    secret = _ssh_key_secret(job_items[0][1])
    if secret:
        try:
            shared_key = await oci_service.get_ssh_public_key(secret)
        except Exception as exc:
            logger.warning("Could not fetch OCI SSH key for the batch: %s", exc)
            shared_key = ""
    else:
        shared_key = ""
    for job_id, payload in job_items:
        await _run_deploy(job_id, payload, compartment, ssh_public_key=shared_key)

async def _run_destroy(job_id: str, instance_ocid: str, deploy_job_id: Optional[str] = None) -> None:
    db = _get_db_session()
    try:
        job_service.set_running(db, job_id)
        result = {"instance_ocid": instance_ocid}

        deploy_meta = {}
        if deploy_job_id:
            deploy_job = job_service.get_job(db, deploy_job_id)
            if deploy_job:
                deploy_meta = deploy_job.metadata_dict

        # Remove the BeyondTrust Shell Jump before terminating the instance.
        if deploy_meta.get("bt_tf_state"):
            job_service.update_progress(db, job_id, 20, "Removing BeyondTrust Shell Jump…")
            try:
                from ..services import terraform_pra_service
                await terraform_pra_service.remove_jump(deploy_meta["bt_tf_state"])
                result["bt_shell_jump_removed"] = deploy_meta.get("bt_shell_jump_id")
            except Exception as e:
                result["bt_error"] = str(e)
                logger.error("OCI Shell Jump removal failed: %s", e)

        if deploy_meta.get("entitle_registration_tf_state"):
            from ..services import entitle_vm_hook
            await entitle_vm_hook.deregister(deploy_meta, result)

        if deploy_meta.get("ps_registration_tf_state"):
            from ..services import ps_vm_hook
            await ps_vm_hook.deregister(deploy_meta, result)

        job_service.update_progress(db, job_id, 50, "Terminating instance…")
        await oci_service.terminate_instance(instance_ocid)

        if deploy_job_id:
            deploy_meta["destroyed"] = True
            if job_service.get_job(db, deploy_job_id):
                job_service.set_completed(db, deploy_job_id, deploy_meta)

        job_service.set_completed(db, job_id, result)
        # Prefix, not an exact key: the instance list is cached per region+compartment
        # (api/oci._cache_key), and this runner has no request context to rebuild the
        # one key with. Dropping every region's entry is also the correct blast radius
        # — a deploy/terminate changes the inventory whichever region it landed in.
        await cache_service.invalidate_prefix("oci_instances")
    except Exception as exc:
        logger.error("OCI destroy failed for job %s: %s", job_id, exc)
        job_service.set_failed(db, job_id, str(exc))
    finally:
        db.close()
