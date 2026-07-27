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
    elif job_type == "gce_bulk_deploy":
        # The children were created `queued` so the runner's claim query (which filters
        # status='pending') cannot pick them up alongside this parent; _run_bulk_deploy
        # drives them and owns their status. Each child carries its own full request.
        job_items = [(c["job_id"], GCPDeployRequest(**c["req"])) for c in meta["children"]]
        await _run_bulk_deploy(job_items, meta["project_id"], meta["zone"])
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
class _JumpointRef:
    """How one GCE deploy reached its BeyondTrust Jumpoint.

    ``mode`` is the discriminator persisted on the deploy job and read back by
    ``_run_destroy``. A ``paired`` ref OWNS a dedicated ``bt-jumpoint-<vm>`` VM and has
    to delete it once no sibling deploy references it. A ``shared`` ref BORROWS the
    ref-counted host that ``jumpoint_host_service`` owns on behalf of cloud databases,
    k8s tunnels and VDI seats, and must only release a reference — the teardown
    decision is that service's to make, not this one's.

    A value object rather than a widened tuple for the same reason as
    ``aws_vm_service._BulkItem``: the shape is read back in three places and a bare
    tuple makes the shared/paired distinction invisible at the call site.
    """
    __slots__ = ("mode", "name", "zone", "region", "error", "reused")

    def __init__(self, mode: str, name: str = "", zone: str = "",
                 region: str = "", error: str = "", reused: bool = False):
        self.mode, self.name, self.zone = mode, name, zone
        self.region, self.error, self.reused = region, error, reused

    def record(self, meta: dict) -> None:
        """Write this reference onto the deploy job's result metadata.

        A shared host is NEVER recorded under ``jumpoint_name``. That key is what
        triggers the paired teardown in ``_run_destroy``, whose sibling count looks at
        ``gce_deploy`` rows only — so recording a shared host there would delete it
        when the last GCE VM went, taking every cloud-database, k8s and VDI tunnel
        routed through the same host down with it.
        """
        if self.error:
            meta["jumpoint_error"] = self.error
        if not self.name:
            return
        meta["jumpoint_mode"] = self.mode
        if self.mode == "paired":
            meta["jumpoint_name"] = self.name
            meta["jumpoint_zone"] = self.zone
        else:
            meta["jumpoint_host_id"] = self.name
            meta["jumpoint_region"] = self.region


async def _acquire_paired_jumpoint(db, job_id: str, payload: GCPDeployRequest,
                                   project_id: str, zone: str, subnet: str) -> _JumpointRef:
    """Start (or reuse) the dedicated ``bt-jumpoint-<vm>`` VM for a single deploy.

    Best-effort, unchanged from before this was extracted: a failure is recorded on the
    job and the VM launch continues, because the operator may already have a Jumpoint
    reaching that subnet by other means."""
    from ..services import config_service as _cfg_svc
    name = _jumpoint_name(payload.instance_name)
    image = _cfg_svc.get("gcp_jumpoint_image") or "beyondtrust/sra-jumpoint:latest"
    machine = _cfg_svc.get("gcp_jumpoint_machine_type") or "e2-micro"
    jp_zone = _cfg_svc.get("gcp_jumpoint_zone") or zone
    job_service.update_progress(db, job_id, 5, f"Starting BeyondTrust Jumpoint {name}…")
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
        meta = await gcp_service.run_gce_jumpoint(
            project_id=project_id,
            zone=jp_zone,
            name=name,
            container_image=image,
            deploy_key=deploy_key,
            subnetwork=subnet or "",
            machine_type=machine,
            create_external_ip=True,
        )
        ref = _JumpointRef("paired", name=meta.get("name", ""),
                           zone=meta.get("zone", jp_zone),
                           reused=bool(meta.get("reused")))
        job_service.update_progress(
            db, job_id, 15,
            f"Jumpoint {name} {'reused' if ref.reused else 'started'}, launching VM…"
        )
        return ref
    except Exception as e:
        # Non-fatal — continue to VM launch; user may already have a Jumpoint elsewhere.
        logger.warning("GCP Jumpoint provisioning failed (non-fatal): %s", e)
        job_service.update_progress(
            db, job_id, 15,
            f"Jumpoint provisioning failed (non-fatal): {e} — continuing with VM launch…"
        )
        return _JumpointRef("paired", error=str(e))


async def _acquire_shared_jumpoint(region: str) -> _JumpointRef:
    """Borrow the ref-counted COS Jumpoint host that cloud databases, k8s tunnels and
    VDI seats already share — one for a whole batch instead of one per VM.

    Best-effort like the paired path: a batch still launches its VMs when the jumpoint
    is unavailable, it just has no Shell Jump."""
    from ..services import jumpoint_host_service
    try:
        host = await jumpoint_host_service.ensure_jumpoint_host("gcp", region)
    except Exception as e:
        logger.warning("Shared GCP Jumpoint host unavailable (non-fatal): %s", e)
        return _JumpointRef("shared", region=region, error=str(e))
    if not host:
        return _JumpointRef(
            "shared", region=region,
            error="shared Jumpoint host unavailable — check the GCP project and the "
                  "jumpoint deploy key in the wizard")
    return _JumpointRef("shared", name=host, region=region)


async def _run_deploy(job_id: str, payload: GCPDeployRequest, project_id: str, zone: str,
                      *, jumpoint: Optional[_JumpointRef] = None) -> None:
    """Deploy one GCE instance.

    ``jumpoint`` is injected by ``_run_bulk_deploy`` so a batch acquires ONE shared
    host rather than N paired VMs. Left at None — the single-deploy path — this
    behaves exactly as it did before batches existed, starting its own paired
    ``bt-jumpoint-<vm>``. Keyword-only so the existing ``gce_deploy`` call in ``run()``
    is untouched."""
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
    jp: Optional[_JumpointRef] = None
    try:
        job_service.set_running(db, job_id)

        # ── Step 1: Start BT Jumpoint on COS-on-GCE first (BeyondTrust only) ──
        if bt_enabled:
            if jumpoint is not None:
                jp = jumpoint
                job_service.update_progress(
                    db, job_id, 15,
                    f"Using the shared BeyondTrust Jumpoint {jp.name}, launching VM…"
                    if jp.name else
                    f"Shared Jumpoint unavailable ({jp.error}) — continuing with VM launch…"
                )
            else:
                jp = await _acquire_paired_jumpoint(
                    db, job_id, payload, project_id, zone, subnet)

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
        if jp:
            jp.record(final_meta)

        # ── BeyondTrust PRA — Shell Jump (optional) ───────────────────────────
        if _cfg_svc.get_bool("beyondtrust_enabled"):
            from ..services import terraform_pra_service
            jump_group = ((payload.jump_group or "").strip() or _cfg_svc.get("gcp_bt_jump_group_name")
                          or _cfg_svc.get("bt_jump_group_name") or settings.bt_jump_group_name)
            # NB: the PRA *display* name, not the GCE instance name that _JumpointRef
            # carries. These were one variable until the jumpoint block moved out, and
            # it only worked because the metadata write above had already happened.
            pra_jumpoint_name = ((payload.jumpoint_name or "").strip() or _cfg_svc.get("gcp_jumpoint_name")
                                 or _cfg_svc.get("bt_jumpoint_name") or settings.bt_jumpoint_name)
            job_service.update_progress(db, job_id, 90, f"Instance launched ({hostname}), provisioning Shell Jump…")
            try:
                bt_result = await terraform_pra_service.provision_jump(
                    vm_name=payload.instance_name,
                    hostname=hostname,
                    jump_group_name=jump_group,
                    jumpoint_name=pra_jumpoint_name,
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


async def _run_bulk_deploy(job_items: list, project_id: str, zone: str) -> None:
    """Deploy a batch of GCE instances behind ONE shared Jumpoint.

    ``job_items`` is ``[(job_id, GCPDeployRequest)]`` — each child carries its own
    expanded instance name, everything else is shared.

    Thin on purpose. The AWS and Azure bulk runners are 200-line near-copies of their
    single-deploy counterparts, and that duplication is exactly why the AWS batch path
    quietly lost its PRA overrides. Injecting the jumpoint instead means every feature
    on the single path — PRA Shell Jump, Entitle, Password Safe — is in the batch path
    for free, and stays there.

    ``_run_deploy`` owns set_running/set_completed/set_failed per child, so one VM's
    failure fails that row alone and the batch carries on."""
    from ..services import config_service as _cfg_svc
    jumpoint = None
    if _cfg_svc.get_bool("beyondtrust_enabled"):
        jumpoint = await _acquire_shared_jumpoint(_region_from_zone(zone))
    for job_id, payload in job_items:
        await _run_deploy(job_id, payload, project_id, zone, jumpoint=jumpoint)


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

        # Mark the deploy row destroyed BEFORE releasing the Jumpoint.
        #
        # The paired branch below excludes this job explicitly, so ordering never
        # mattered for it. The shared branch calls teardown_jumpoint_host_if_idle,
        # which counts live rows and takes no "exclude me" argument — leave this until
        # afterwards and the row being destroyed counts itself, so the host is never
        # reclaimed. aws_vm_service._run_destroy orders it the same way for the same
        # reason.
        if deploy_job_id:
            deploy_meta["destroyed"] = True
            deploy_job = job_service.get_job(db, deploy_job_id)
            if deploy_job:
                job_service.set_completed(db, deploy_job_id, deploy_meta)

        # Release the Jumpoint. Two shapes coexist: a PAIRED bt-jumpoint-<vm> VM this
        # deploy owns outright, or a borrowed reference to the SHARED ref-counted host.
        #
        # A row written before jumpoint_mode existed can only be paired: nothing ever
        # wrote jumpoint_host_id on a gce_deploy, and the only writer of jumpoint_name
        # was the paired block. The inference is therefore total, which is why this
        # needs no backfill and old VMs keep tearing down exactly as they do today.
        mode = deploy_meta.get("jumpoint_mode") if deploy_job_id else None
        if not mode and deploy_job_id and deploy_meta.get("jumpoint_name"):
            mode = "paired"

        if mode == "paired":
            jumpoint_name = deploy_meta.get("jumpoint_name") or ""
            from ..services import jumpoint_host_service as _jhs
            if jumpoint_name and jumpoint_name == _jhs._gcp_jumpoint_name():
                # Belt and braces. A VM destroy must never delete the shared host — it
                # also serves cloud databases, k8s tunnels and VDI seats, none of which
                # the gce_deploy-scoped sibling count below can see.
                logger.warning(
                    "Refusing to delete %s from a VM destroy — that is the shared "
                    "ref-counted Jumpoint host, not a paired one", jumpoint_name)
                result["jumpoint_shared"] = jumpoint_name
            elif jumpoint_name:
                # Sibling-aware cleanup: several VMs may share one paired Jumpoint.
                # Shared-mode rows carry no jumpoint_name, so they can never match here.
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

        elif mode == "shared":
            # Drop this VM's reference and let jumpoint_host_service decide. It counts
            # GCE VMs, cloud databases, k8s tunnels and VDI seats, so the host survives
            # as long as anything still needs it.
            region = deploy_meta.get("jumpoint_region") or _region_from_zone(zone)
            job_service.update_progress(
                db, job_id, 75, "Releasing the shared BeyondTrust Jumpoint host…")
            try:
                from ..services import jumpoint_host_service
                await jumpoint_host_service.teardown_jumpoint_host_if_idle(db, "gcp", region)
            except Exception as e:
                logger.warning("Shared Jumpoint host release failed (non-fatal): %s", e)
                result["jumpoint_host_teardown_error"] = str(e)

        job_service.set_completed(db, job_id, result)
        await cache_service.invalidate(cache_service.key_global("gcp_instances"))
    except Exception as exc:
        logger.error("GCE destroy failed for job %s: %s", job_id, exc)
        job_service.set_failed(db, job_id, str(exc))
    finally:
        db.close()
