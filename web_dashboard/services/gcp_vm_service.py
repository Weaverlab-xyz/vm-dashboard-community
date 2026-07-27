"""GCE instance deploy / destroy / image-capture execution, run by the job runner.

Dispatched by ``jobs_worker`` as ``gce_deploy``, ``gce_capture_image`` and
``gce_destroy``. ``api/gcp`` keeps the route handlers, which validate, persist the
request on the job and return; the worker reconstructs the call from that metadata and
runs it here, so a gunicorn recycle mid-deploy no longer strands the job or orphans the
instance.

Lives in ``services/`` because the job runner has to import it, and a worker reaching
into the API package is backwards (see services/aws_vm_service for the AWS counterpart).
"""
import logging
from typing import Optional

from ..config import settings
from ..database import Job
from ..models.gcp import GCPDeployRequest
from . import cache_service, gcp_service, job_service, region_catalog

logger = logging.getLogger(__name__)


# Own copies rather than imports from ``api.gcp`` — that is the dependency this module
# exists to remove. Same shape as ``aws_vm_service._aws_cfg`` and
# ``packer_build_service._cfg``.

def _gcp_cfg(key: str, fallback: str = "") -> str:
    from ..services import config_service
    return config_service.get(key) or getattr(settings, key, None) or fallback


def _gcp_project() -> str:
    return _gcp_cfg("gcp_project_id")


def _gcp_zone() -> str:
    return _gcp_cfg("gcp_zone") or "us-central1-a"


def _gcp_region() -> str:
    zone = _gcp_zone()
    cfg_region = _gcp_cfg("gcp_region")
    if cfg_region:
        return cfg_region
    parts = zone.rsplit("-", 1)
    return parts[0] if len(parts) == 2 else zone


def _region_from_zone(zone: str) -> str:
    """Derive the region a zone belongs to (us-central1-a → us-central1). Falls
    back to the configured default region (``_gcp_region()``, which derives from the
    configured zone when ``gcp_region`` is unset) if the zone doesn't parse."""
    parts = (zone or "").rsplit("-", 1)
    if len(parts) == 2 and region_catalog.validate("gcp", parts[0]):
        return parts[0]
    return _gcp_region()


# ── Job-runner entry point ────────────────────────────────────────────────────

async def run(job_id: str, job_type: str, meta: dict) -> None:
    """Run one GCP job. Every argument comes from the metadata the endpoint persisted.

    ``project_id`` and ``zone`` are read from the job, never from ``_gcp_project()`` /
    ``_gcp_zone()``: those return whatever is configured *now*, so a destroy would aim
    at the wrong project if the default changed after the deploy."""
    if job_type == "gce_deploy":
        req = GCPDeployRequest(**meta["req"])
        await _run_deploy(job_id, req, meta["project_id"], meta["zone"])
    elif job_type == "gce_capture_image":
        await _run_capture(job_id, meta["project_id"], meta["zone"],
                           meta["instance_name"], meta["image_name"],
                           meta.get("description") or "")
    elif job_type == "gce_destroy":
        await _run_destroy(job_id, meta["project_id"], meta["zone"],
                           meta["instance_name"], meta.get("deploy_job_id") or "")


def _jumpoint_name(vm_name: str) -> str:
    """Deterministic Jumpoint VM name. Each user VM gets its own paired
    Jumpoint, mirroring the AWS ECS pattern. GCE names cap at 63 chars."""
    base = f"bt-jumpoint-{vm_name}".lower()
    return base[:63]
async def _resolve_gcp_jumpoint_deploy_key() -> str:
    """Return the BeyondTrust SRA Jumpoint deploy key for GCP launches.
    Resolves through whichever secrets backend the user picked on /secrets;
    `gcp_cloud_run_docker_deploy_key` is the historical key name."""
    from ..services import config_service
    return (
        config_service.get("gcp_cloud_run_docker_deploy_key")
        or config_service.get("gcp_jumpoint_docker_deploy_key")
        or ""
    )
def _get_db_session():
    from ..database import SessionLocal
    return SessionLocal()
async def _run_deploy(job_id: str, payload: GCPDeployRequest, project_id: str, zone: str) -> None:
    from ..services import config_service as _cfg_svc
    from ..services.region_config import resolve_region
    db = _get_db_session()
    # Per-region SSH key secret + default network tag (blank fields / the default
    # region fall back to the flat gcp_* config keys).
    _rc = resolve_region("gcp", _region_from_zone(zone))
    # Default the target subnetwork to the sandbox vm-subnet (gcp_subnetwork) when the
    # form leaves it blank — otherwise GCE silently drops the VM into the project's
    # `default` VPC, where the peered/co-located Entitle agent has no route to it
    # (the "Failed to fetch resources / SSH timeout" trap). Explicit form value wins.
    subnet = payload.subnetwork or _rc["subnetwork"]
    bt_enabled = _cfg_svc.get_bool("beyondtrust_enabled")
    jumpoint_name = ""
    jumpoint_zone = zone
    jumpoint_meta: dict = {}
    try:
        job_service.set_running(db, job_id)

        # ── Step 1: Start BT Jumpoint on COS-on-GCE first (BeyondTrust only) ──
        if bt_enabled:
            jumpoint_name = _jumpoint_name(payload.instance_name)
            jumpoint_image = _cfg_svc.get("gcp_jumpoint_image") or "beyondtrust/sra-jumpoint:latest"
            jumpoint_machine = _cfg_svc.get("gcp_jumpoint_machine_type") or "e2-micro"
            jumpoint_zone = _cfg_svc.get("gcp_jumpoint_zone") or zone
            job_service.update_progress(db, job_id, 5, f"Starting BeyondTrust Jumpoint {jumpoint_name}…")
            try:
                if getattr(payload, "docker_deploy_key_ref", None):
                    deploy_key = _cfg_svc.resolve_reference(payload.docker_deploy_key_ref.strip())
                else:
                    deploy_key = await _resolve_gcp_jumpoint_deploy_key()
                if not deploy_key:
                    raise RuntimeError(
                        "Jumpoint deploy key not configured "
                        "(gcp_cloud_run_docker_deploy_key) — set it in the wizard."
                    )
                jumpoint_meta = await gcp_service.run_gce_jumpoint(
                    project_id=project_id,
                    zone=jumpoint_zone,
                    name=jumpoint_name,
                    container_image=jumpoint_image,
                    deploy_key=deploy_key,
                    subnetwork=subnet or "",
                    machine_type=jumpoint_machine,
                    create_external_ip=True,
                )
                job_service.update_progress(
                    db, job_id, 15,
                    f"Jumpoint {jumpoint_name} {'reused' if jumpoint_meta.get('reused') else 'started'}, launching VM…"
                )
            except Exception as e:
                # Non-fatal — continue to VM launch; user may already have a Jumpoint elsewhere.
                jumpoint_meta = {"error": str(e)}
                logger.warning("GCP Jumpoint provisioning failed (non-fatal): %s", e)
                job_service.update_progress(
                    db, job_id, 15,
                    f"Jumpoint provisioning failed (non-fatal): {e} — continuing with VM launch…"
                )

        # Retrieve SSH public key (per-launch override wins over the region default)
        secret_name = getattr(payload, "ssh_key_secret_override", None) or _rc["ssh_key_secret"]
        ssh_username = _cfg_svc.get("gcp_ssh_username") or payload.ssh_username or "gcp-user"
        ssh_public_key = ""
        if secret_name:
            job_service.update_progress(db, job_id, 18, "Retrieving SSH public key from Secret Manager…")
            try:
                ssh_public_key = await gcp_service.get_ssh_public_key(
                    project_id=project_id, secret_name=secret_name
                )
            except Exception as exc:
                logger.warning("Could not fetch SSH key from Secret Manager: %s", exc)

        job_service.update_progress(db, job_id, 20, "Launching Compute Engine instance…")

        # Merge config-driven default network tags (used by sandbox firewall
        # rules) with any tags the user supplied on the deploy form.
        default_tag_csv = _rc["default_network_tag"]
        default_tags = [t.strip() for t in default_tag_csv.split(",") if t.strip()]
        merged_tags = list(dict.fromkeys((payload.network_tags or []) + default_tags))

        wg = getattr(payload, "workgroup", "") or ""
        result = await gcp_service.launch_instance(
            project_id=project_id,
            zone=zone,
            instance_name=payload.instance_name,
            machine_type=payload.machine_type,
            image_self_link=payload.image_self_link,
            subnetwork=subnet,
            create_external_ip=payload.create_external_ip,
            ssh_username=ssh_username,
            ssh_public_key=ssh_public_key,
            disk_size_gb=payload.disk_size_gb,
            network_tags=merged_tags,
            labels={"workgroup": wg} if wg else None,
        )

        hostname = result.get("private_ip") or result.get("public_ip") or payload.instance_name

        final_meta = {
            "instance_name": result["instance_name"],
            "zone":          result["zone"],
            "machine_type":  result["machine_type"],
            "status":        result["status"],
            "public_ip":     result.get("public_ip"),
            "private_ip":    result.get("private_ip"),
            "self_link":     result.get("self_link", ""),
            "image_self_link": payload.image_self_link,
            "image_name":    payload.image_name,
        }
        if bt_enabled:
            if jumpoint_meta.get("error"):
                final_meta["jumpoint_error"] = jumpoint_meta["error"]
            elif jumpoint_meta.get("name"):
                final_meta["jumpoint_name"] = jumpoint_meta["name"]
                final_meta["jumpoint_zone"] = jumpoint_meta.get("zone", jumpoint_zone)

        # ── BeyondTrust PRA — Shell Jump (optional) ───────────────────────────
        if _cfg_svc.get_bool("beyondtrust_enabled"):
            from ..services import terraform_pra_service
            jump_group = ((payload.jump_group or "").strip() or _cfg_svc.get("gcp_bt_jump_group_name")
                          or _cfg_svc.get("bt_jump_group_name") or settings.bt_jump_group_name)
            jumpoint_name = ((payload.jumpoint_name or "").strip() or _cfg_svc.get("gcp_jumpoint_name")
                             or _cfg_svc.get("bt_jumpoint_name") or settings.bt_jumpoint_name)
            job_service.update_progress(db, job_id, 90, f"Instance launched ({hostname}), provisioning Shell Jump…")
            try:
                bt_result = await terraform_pra_service.provision_jump(
                    vm_name=payload.instance_name,
                    hostname=hostname,
                    jump_group_name=jump_group,
                    jumpoint_name=jumpoint_name,
                    tag="GCP",
                )
                final_meta["bt_shell_jump_id"] = bt_result.get("shell_jump_id")
                final_meta["bt_jump_group_name"] = bt_result.get("jump_group_name")
                final_meta["bt_tf_state"] = bt_result.get("tf_state_json")
                job_service.update_progress(
                    db, job_id, 95,
                    f"Shell Jump created (ID: {bt_result.get('shell_jump_id')}, group: {jump_group})"
                )
            except Exception as bt_exc:
                final_meta["bt_error"] = str(bt_exc)
                job_service.update_progress(
                    db, job_id, 95,
                    f"Instance deployed but Shell Jump provisioning failed: {bt_exc}"
                )
        else:
            job_service.update_progress(db, job_id, 95, "Instance launched.")

        # Entitle — register as SSH ephemeral-accounts integration (per-build opt-in).
        from ..services import entitle_vm_hook
        if getattr(payload, "register_in_entitle", False) and entitle_vm_hook.registration_enabled():
            await entitle_vm_hook.register(db, job_id, payload.instance_name, hostname,
                                           private=not payload.create_external_ip,
                                           result=final_meta, tag="GCP",
                                           # Use the RESOLVED login user (gcp_ssh_username →
                                           # gcp-user default), the same account the VM's launch
                                           # key was injected for — not the raw (often-blank)
                                           # payload field, which would register an empty user.
                                           sudo_user=ssh_username,
                                           ssh_key_secret=secret_name)

        # Password Safe — onboard as a managed system + account (per-build opt-in).
        # GCP defaults to the cloud-native "GCP VM SSH Rotation" plugin (managed system
        # address = projectId/zone/instanceName), so pass the project + zone.
        from ..services import ps_vm_hook
        if getattr(payload, "register_in_passwordsafe", False) and ps_vm_hook.registration_enabled():
            await ps_vm_hook.register(db, job_id, payload.instance_name, hostname,
                                      result=final_meta, tag="GCP", ssh_key_secret=secret_name,
                                      project=_gcp_project(), zone=result["zone"])

        job_service.set_completed(db, job_id, final_meta)
        await cache_service.invalidate(cache_service.key_global("gcp_instances"))

    except Exception as exc:
        logger.error("GCE deploy failed for job %s: %s", job_id, exc)
        job_service.set_failed(db, job_id, str(exc))
    finally:
        db.close()
async def _run_capture(
    job_id: str,
    project_id: str,
    zone: str,
    instance_name: str,
    image_name: str,
    description: str,
) -> None:
    db = _get_db_session()
    try:
        job_service.set_running(db, job_id)
        job_service.update_progress(db, job_id, 20, "Creating image from instance disk…")
        result = await gcp_service.create_image_from_instance(
            project_id=project_id, zone=zone,
            instance_name=instance_name, image_name=image_name, description=description,
        )
        job_service.set_completed(db, job_id, result)
        await cache_service.invalidate(cache_service.key_global("gcp_custom_images"))
    except Exception as exc:
        logger.error("GCE image capture failed for job %s: %s", job_id, exc)
        job_service.set_failed(db, job_id, str(exc))
    finally:
        db.close()
async def _run_destroy(
    job_id: str, project_id: str, zone: str, instance_name: str,
    deploy_job_id: Optional[str] = None,
) -> None:
    db = _get_db_session()
    try:
        job_service.set_running(db, job_id)
        result = {"instance_name": instance_name, "zone": zone}

        # Remove BeyondTrust Shell Jump before terminating the instance
        deploy_meta = {}
        if deploy_job_id:
            deploy_job = job_service.get_job(db, deploy_job_id)
            if deploy_job:
                deploy_meta = deploy_job.metadata_dict

        bt_shell_jump_id = deploy_meta.get("bt_shell_jump_id")
        if bt_shell_jump_id:
            job_service.update_progress(
                db, job_id, 20,
                f"Removing BeyondTrust Shell Jump {bt_shell_jump_id}…"
            )
            try:
                tf_state = deploy_meta.get("bt_tf_state")
                if tf_state:
                    from ..services import terraform_pra_service
                    await terraform_pra_service.remove_jump(tf_state)
                    result["bt_shell_jump_removed"] = bt_shell_jump_id
                    job_service.update_progress(
                        db, job_id, 35,
                        f"Shell Jump {bt_shell_jump_id} removed from PRA."
                    )
                else:
                    msg = (
                        f"Shell Jump {bt_shell_jump_id} requires manual removal from PRA "
                        "(provisioned before Terraform migration — no tf_state stored)"
                    )
                    logger.warning(msg)
                    result["bt_error"] = msg
                    job_service.update_progress(db, job_id, 35, msg)
            except Exception as e:
                err = f"Shell Jump removal failed: {e}"
                logger.error("bt_shell_jump_id=%s destroy error: %s", bt_shell_jump_id, e)
                result["bt_error"] = err
                job_service.update_progress(db, job_id, 35, err)

        # Remove the Entitle SSH integration if this deploy registered one.
        if deploy_meta.get("entitle_registration_tf_state"):
            from ..services import entitle_vm_hook
            await entitle_vm_hook.deregister(deploy_meta, result)

        # Off-board the Password Safe managed system if this deploy registered one.
        if deploy_meta.get("ps_registration_tf_state"):
            from ..services import ps_vm_hook
            await ps_vm_hook.deregister(deploy_meta, result)

        job_service.update_progress(db, job_id, 50, f"Deleting instance {instance_name}…")
        await gcp_service.terminate_instance(project_id=project_id, zone=zone, instance_name=instance_name)

        # Clean up paired Jumpoint VM, but only if no other live deploy still references it
        # (multiple VMs may share the same Jumpoint via deploy_key — sibling-aware cleanup).
        jumpoint_name = deploy_meta.get("jumpoint_name") if deploy_job_id else None
        if jumpoint_name:
            sibling_count = sum(
                1 for j in db.query(Job).filter(
                    Job.job_type == "gce_deploy", Job.status == "completed"
                ).all()
                if j.id != deploy_job_id
                and not j.metadata_dict.get("destroyed")
                and j.metadata_dict.get("jumpoint_name") == jumpoint_name
            )
            if sibling_count == 0:
                jumpoint_zone = deploy_meta.get("jumpoint_zone", zone)
                job_service.update_progress(
                    db, job_id, 75, f"Stopping paired Jumpoint {jumpoint_name}…"
                )
                try:
                    await gcp_service.stop_gce_jumpoint(
                        project_id=project_id, zone=jumpoint_zone, name=jumpoint_name
                    )
                    result["jumpoint_stopped"] = jumpoint_name
                except Exception as e:
                    logger.warning("Jumpoint cleanup failed for %s: %s", jumpoint_name, e)
                    result["jumpoint_error"] = f"cleanup failed: {e}"
            else:
                result["jumpoint_shared"] = jumpoint_name
                logger.info(
                    "Leaving Jumpoint %s running — %d other active deploy(s) reference it",
                    jumpoint_name, sibling_count,
                )

        if deploy_job_id:
            deploy_meta["destroyed"] = True
            deploy_job = job_service.get_job(db, deploy_job_id)
            if deploy_job:
                job_service.set_completed(db, deploy_job_id, deploy_meta)

        job_service.set_completed(db, job_id, result)
        await cache_service.invalidate(cache_service.key_global("gcp_instances"))
    except Exception as exc:
        logger.error("GCE destroy failed for job %s: %s", job_id, exc)
        job_service.set_failed(db, job_id, str(exc))
    finally:
        db.close()
