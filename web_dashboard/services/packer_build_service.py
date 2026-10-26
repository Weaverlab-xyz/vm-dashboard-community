"""Packer image builds, exports, and the provisioner environment they run with.

Dispatched by ``jobs_worker`` as ``packer_{aws,azure,gcp}_build`` and
``{aws,gcp,azure}_export_image`` jobs. ``api/packer`` keeps only the three route
handlers, which validate a request, persist it on the job and return — the worker
reconstructs the request from that metadata and calls in here.

Lives in ``services/`` because the job runner has to import it, and a worker reaching
into the API package is backwards (see services/ansible_local_run_service for the same
split on the Config-Management side).
"""
import logging
import os
from pathlib import Path

from ..models.packer import (
    AWSPackerBuildRequest,
    AzurePackerBuildRequest,
    GCPPackerBuildRequest,
    OCIPackerBuildRequest,
)
from . import (
    aws_service,
    azure_service,
    gcp_service,
    image_registry_service,
    job_service,
    oci_service,
    packer_service,
    storage_service,
)
from .oci_service import OCIError
from .packer_service import PackerError, PackerCancelled
from .epml_service import EpmlError
from ..utils.clock import utcnow

logger = logging.getLogger(__name__)


# ── Job-runner entry points ───────────────────────────────────────────────────
# The worker used to rebuild the pydantic request itself; keeping that here means
# jobs_worker imports neither the API package nor the request models.

_BUILD_REQUEST = {
    "packer_aws_build":   AWSPackerBuildRequest,
    "packer_azure_build": AzurePackerBuildRequest,
    "packer_gcp_build":   GCPPackerBuildRequest,
    "packer_oci_build":   OCIPackerBuildRequest,
}


async def run_build(job_id: str, job_type: str, meta: dict) -> None:
    """Run one Packer build. ``meta['req']`` is the original request as stored by the
    endpoint — secret *references* only, resolved at build launch."""
    model = _BUILD_REQUEST[job_type]
    req = model(**meta["req"])
    created_by = meta.get("created_by", "system")
    fn = {"packer_aws_build": _run_aws_build,
          "packer_azure_build": _run_azure_build,
          "packer_gcp_build": _run_gcp_build,
          "packer_oci_build": _run_oci_build}[job_type]
    await fn(job_id, req, created_by)


async def run_export(job_id: str, job_type: str, meta: dict) -> None:
    """Run one image export (AMI/disk → the hub bucket)."""
    fn = {"aws_export_image": run_export_aws,
          "gcp_export_image": run_export_gcp,
          "azure_export_image": run_export_azure,
          "oci_export_image": run_export_oci}[job_type]
    await fn(job_id, meta)


def _cfg(key: str, fallback: str = "") -> str:
    from ..services import config_service
    from ..config import settings
    return config_service.get(key) or getattr(settings, key, None) or fallback


def _get_db_session():
    from ..database import SessionLocal
    return SessionLocal()


def _cancel_checker(job_id: str):
    """Return a cheap ``() -> bool`` probe for whether ``job_id`` was cancelled,
    for packer_service.run_build's cancel watcher. Each call opens a fresh
    short-lived session (like job_service.cancel_check) so it always sees the
    API's committed Cancel and never touches the long-held build session."""
    def _check() -> bool:
        s = _get_db_session()
        try:
            return job_service.is_cancelled(s, job_id)
        finally:
            s.close()
    return _check


async def _epml_package_url(family: str, source: str) -> str:
    """A download URL for the EPM-L package of ``family`` ('deb'|'rpm'), from either
    source. Both ends are presigned HTTPS the bt-ready provisioner just curls.

    * ``beyondtrust`` (default) — a fresh link from the EPM-L integration. Always
      current, but BeyondTrust expires them ~30 min after listing, so it is resolved
      at build launch, close to when the provisioner runs.
    * ``storage`` — the copy synced to the asset backend (Storage page → *Sync to
      storage*). Pins the version you tested and doesn't depend on BeyondTrust being
      reachable from the build, at the cost of going stale until you re-sync.
    """
    from ..services import epml_service
    if source != "storage":
        return await epml_service.package_download_url(family)

    from ..services import storage_service
    backend = storage_service.active_backend()
    if not backend:
        raise EpmlError("No active storage backend — pick one on the Storage page.")
    assets = await storage_service.list_assets_in(backend)
    suffix = "." + family
    names = sorted(a["name"] for a in assets if a.get("name", "").lower().endswith(suffix))
    if not names:
        raise EpmlError(
            f"No {suffix} package found on the '{backend}' storage backend. Use "
            f"Storage → BeyondTrust EPM-L packages → Sync to storage first, or set "
            f"the package source back to BeyondTrust.")
    # Newest by name — the EPM-L filenames carry their version, so lexical order
    # tracks release order for a given family.
    key = storage_service.asset_key(backend, names[-1])
    return await storage_service.presigned_url(backend, key, expiry_seconds=3600)


async def _bt_provisioner_env(req) -> dict:
    """BeyondTrust provisioner env (BT_*) built from a build request, injected into
    the shell provisioner's ``environment_vars``. For ``bt_epml`` in ('deb','rpm')
    an EPM-L package download URL is resolved → ``BT_EPML_URL``, from BeyondTrust or
    from asset storage per ``bt_epml_source``. Raises ``EpmlError`` if the package
    can't be resolved."""
    env: dict = {}
    admin = (getattr(req, "bt_admin_user", None) or "").strip()
    if admin:
        env["BT_ADMIN_USER"] = admin
    fam = (getattr(req, "bt_epml", None) or "").strip().lower()
    if fam in ("deb", "rpm"):
        source = (getattr(req, "bt_epml_source", None) or "beyondtrust").strip().lower()
        env["BT_EPML_URL"] = await _epml_package_url(fam, source)
    stage = (getattr(req, "bt_stage_asset", None) or "").strip()
    if stage:
        env["POV_STAGE_URL"] = await _storage_asset_url(stage)
        env["POV_STAGE_NAME"] = stage.rsplit("/", 1)[-1]
    return env


async def _storage_asset_url(name: str) -> str:
    """A presigned URL for one asset on the active storage backend, minted now."""
    backend = storage_service.active_backend()
    if not backend:
        raise PackerError("No active storage backend — pick one on the Storage page.")
    assets = await storage_service.list_assets_in(backend)
    if name not in {a.get("name") for a in assets}:
        raise PackerError(f"'{name}' is not on the '{backend}' storage backend any more.")
    key = storage_service.asset_key(backend, name)
    return await storage_service.presigned_url(backend, key, expiry_seconds=3600)


async def _provisioner_env(req) -> tuple[dict, dict, dict]:
    """Resolve the full provisioner environment for a build request.

    Returns ``(plain_env, sensitive_var_names, pkr_env)``:
      * ``plain_env``   — name→literal value, inlined into the template's
        ``environment_vars`` (includes the BT_* convenience knobs).
      * ``sensitive_var_names`` — name→Packer sensitive-variable name; declared in the
        template, value NOT written to it.
      * ``pkr_env``     — ``PKR_VAR_<var>``→resolved secret value, injected into
        the packer build subprocess so the sensitive variables get their values
        without ever touching the template or the archived copy.

    Generic env vars flagged ``is_secret_ref`` are resolved at build-launch via
    config_service (aws_sm://, azure_kv://, gcp_sm://, bt_safe:// …)."""
    from ..services import config_service

    plain = await _bt_provisioner_env(req)
    sensitive_var_names: dict = {}
    pkr_env: dict = {}
    idx = 0
    for ev in (getattr(req, "provisioner_env_vars", None) or []):
        name = (getattr(ev, "name", "") or "").strip()
        if not name:
            continue
        raw = getattr(ev, "value", "") or ""
        if getattr(ev, "is_secret_ref", False):
            value = config_service.resolve_reference(raw.strip())
            if not value:
                raise PackerError(
                    f"Could not resolve secret reference for {name!r} "
                    f"({raw[:40]}…) — check the reference and that the backend is configured."
                )
            var = f"penv_{idx}"
            idx += 1
            sensitive_var_names[name] = var
            pkr_env[f"PKR_VAR_{var}"] = value
        else:
            plain[name] = raw
    return plain, sensitive_var_names, pkr_env


# ── Background task runners ───────────────────────────────────────────────────

async def _run_aws_build(job_id: str, req: AWSPackerBuildRequest, created_by: str = "system") -> None:
    db = _get_db_session()
    build_dir = packer_service.BUILDS_DIR / job_id
    try:
        job_service.set_running(db, job_id)
        build_dir.mkdir(parents=True, exist_ok=True)

        # Build env. A POV build names its region (the POV's, since an AMI is
        # region-scoped and pov_cloud_env.resolve_image refuses a mismatch).
        region = req.region or _cfg("aws_region") or "us-east-2"
        is_windows = req.os_type == "Windows"
        env = _base_env()
        from .workload_credential_lease import aws_subprocess_env as _wlc_aws_env
        _dynamic = _wlc_aws_env()
        if _dynamic:
            env.update(_dynamic)
        elif _cfg("aws_access_key_id") and _cfg("aws_secret_access_key"):
            env["AWS_ACCESS_KEY_ID"] = _cfg("aws_access_key_id")
            env["AWS_SECRET_ACCESS_KEY"] = _cfg("aws_secret_access_key")
        else:
            # The dashboard's own SPIFFE identity, assumed into a role (the session
            # token is what makes the triple work).
            from . import cloud_federation
            try:
                env.update(cloud_federation.aws_subprocess_env() or {})
            except cloud_federation.FederationError as exc:
                raise PackerError(str(exc)) from exc
        env["AWS_DEFAULT_REGION"] = region
        env["PKR_VAR_region"] = region

        # Resolve an OS family to the newest public AMI (the EC2 counterpart of
        # the GCP builder's source_image_family) — a literal source_ami wins.
        source_ami = (req.source_ami or "").strip()
        ssh_username = req.ssh_username
        if not source_ami:
            from . import aws_service
            job_service.update_progress(
                db, job_id, 3, f"Resolving the newest {req.source_ami_family} AMI in {region}…")
            fam = await aws_service.find_ami_by_family(region, req.source_ami_family)
            if (fam.get("os_type") or "Linux") != req.os_type:
                # A shell build waiting on SSH from a Windows AMI hangs for its whole
                # timeout, and the reverse fails as obscurely. Say which is which now.
                raise PackerError(
                    f"source family {req.source_ami_family} is a {fam.get('os_type')} "
                    f"image and this is a {req.os_type} build")
            source_ami = fam["ami_id"]
            # The family's canonical login user wins over an untouched default —
            # Debian images log in as `admin`, and ec2-user would hang the build
            # at SSH. An explicit non-default username is respected.
            if ssh_username == "ec2-user":
                ssh_username = fam["ssh_username"]
            job_service.update_progress(
                db, job_id, 4, f"Source AMI {source_ami} ({fam['name']}, user {ssh_username})")

        # Generate template
        job_service.update_progress(db, job_id, 5, "Generating Packer template…")
        has_script = bool(req.provisioner_script.strip())
        if has_script or req.provisioner_files:
            plain_env, sensitive_var_names, pkr_env = await _provisioner_env(req)
        else:
            plain_env, sensitive_var_names, pkr_env = {}, {}, {}
        env.update(pkr_env)
        scripts = _stage_bundled(build_dir, req.provisioner_files)
        tags = {"pov-recipe": req.recipe} if req.recipe else None
        if is_windows:
            if has_script:
                scripts.append("provision.ps1")
            template = packer_service.generate_aws_windows_template(
                source_ami=source_ami,
                instance_type=req.instance_type,
                image_name=req.image_name,
                scripts=scripts,
                provisioner_env=plain_env,
                provisioner_sensitive_var_names=sensitive_var_names,
                extra_tags=tags,
            )
            (build_dir / "winrm_bootstrap.ps1").write_text(packer_service.AWS_WINRM_BOOTSTRAP)
            _write_ps_provisioner(build_dir, req.provisioner_script)
        else:
            if scripts and has_script:
                # The operator's script goes before the last bundled one: that one
                # seals the image (host keys, machine-id, cloud-init state), and
                # anything run after it would undo the seal.
                scripts.insert(len(scripts) - 1, "provision.sh")
            template = packer_service.generate_aws_template(
                source_ami=source_ami,
                instance_type=req.instance_type,
                ssh_username=ssh_username,
                image_name=req.image_name,
                has_provisioner=has_script,
                provisioner_env=plain_env,
                provisioner_sensitive_var_names=sensitive_var_names,
                scripts=scripts or None,
                extra_tags=tags,
            )
            _write_provisioner(build_dir, req.provisioner_script)
        (build_dir / "build.pkr.hcl").write_text(template)

        def on_progress(pct, msg):
            job_service.update_progress(db, job_id, pct, msg)

        def on_log(line):
            job_service.append_job_log(db, job_id, line)

        result = await packer_service.run_build(
            "aws", build_dir, env, on_progress,
            is_cancelled=_cancel_checker(job_id), on_log=on_log)

        # Archive template
        if req.archive_template:
            bucket = _cfg("packer_aws_s3_bucket")
            if bucket:
                try:
                    uri = await packer_service.archive_to_s3(
                        build_dir / "build.pkr.hcl",
                        job_id, req.image_name, bucket,
                        _archive_credentials(env, region),
                    )
                    result["template_archive"] = uri
                    job_service.update_progress(db, job_id, 98, f"Template archived: {uri}")
                except Exception as e:
                    result["archive_error"] = str(e)

        if req.register_mode == "ami_only":
            result.update(register_aws_ami(
                db, job_id, req.image_name, result.get("artifact_id") or "", region,
                created_by, os_type=req.os_type, recipe=req.recipe))
            if result.get("register_error"):
                # The catalog row IS this build's deliverable: an AMI nobody can pick
                # from the template editor is not a success, whatever Packer said.
                raise PackerError(
                    f"AMI {result.get('artifact_id') or '(unknown)'} was built but not "
                    f"registered: {result['register_error']}")
        else:
            # Export to portable VHD + auto-register (Phase 2)
            export_result = await export_and_register_aws(
                db, job_id, req.image_name, result.get("artifact_id") or "", region, created_by,
            )
            result.update(export_result)

        job_service.set_completed(db, job_id, result)

    except PackerError as e:
        job_service.set_failed(db, job_id, str(e))
    except PackerCancelled:
        # Operator cancelled: packer was signalled and ran its own cleanup, so the
        # temp build instance is torn down. The job is already 'cancelled' (that
        # flip is what the watcher polled for) — leave it, don't mark it failed.
        logger.info("packer build %s cancelled; build instance cleaned up", job_id)
    except Exception as e:
        job_service.set_failed(db, job_id, f"Unexpected error: {e}")
    finally:
        db.close()


def _azure_build_credentials() -> tuple:
    """``(client_id, client_secret, client_jwt, tenant_id, subscription_id)`` for an
    azure-arm build, with exactly one of the secret and the assertion set.

    Order: a Workload Credentials lease (inside the job), the stored secret, then the
    dashboard's SPIFFE identity as a build-scoped client assertion. Raises PackerError
    naming what is missing.
    """
    # Azure credentials
    # The dynamic tier first. Packer gets the credential wired into the template
    # rather than the environment (see the PKR_VAR_* block below), so a lease has to
    # be resolved here explicitly — there is no ambient path it could fall back to.
    from .workload_credential_lease import azure_credentials as _wlc_azure
    _quad = _wlc_azure() or {}
    client_id = _quad.get("client_id") or _cfg("azure_client_id")
    client_secret = _quad.get("client_secret") or _cfg("azure_client_secret")
    tenant_id = _quad.get("tenant_id") or _cfg("azure_tenant_id")
    subscription_id = _quad.get("subscription_id") or _cfg("azure_subscription_id")

    # The azure-arm builder needs the service principal wired into the
    # template (it does NOT read ARM_* env vars) — fail fast with a clear
    # message instead of letting Packer fall back to Managed Identity and
    # time out against IMDS (169.254.169.254) after 30s on this non-Azure host.
    #
    # With no lease and no stored secret, the dashboard's SPIFFE identity: a client
    # assertion minted for this build (client_jwt), long enough to outlast it.
    client_jwt = ""
    if not client_secret and client_id and tenant_id and subscription_id:
        from . import cloud_federation
        if cloud_federation.active("azure"):
            try:
                client_jwt = cloud_federation.azure_build_assertion()
            except cloud_federation.FederationError as exc:
                raise PackerError(str(exc)) from exc
    if not all([client_id, client_secret or client_jwt, tenant_id, subscription_id]):
        from . import cloud_federation
        why = cloud_federation.reason("azure")
        raise PackerError(
            "Azure credentials for the Packer build were not found. It needs "
            "azure_client_id, azure_tenant_id and azure_subscription_id, and one of: a "
            "Workload Credentials lease, a stored azure_client_secret, or the "
            "dashboard's SPIFFE identity federated to that app"
            + (f" (federation is not in use: {why})." if why else ".")
        )
    return client_id, client_secret or "", client_jwt, tenant_id, subscription_id


async def _run_azure_build(job_id: str, req: AzurePackerBuildRequest, created_by: str = "system") -> None:
    db = _get_db_session()
    build_dir = packer_service.BUILDS_DIR / job_id
    try:
        job_service.set_running(db, job_id)
        build_dir.mkdir(parents=True, exist_ok=True)

        client_id, client_secret, client_jwt, tenant_id, subscription_id = (
            _azure_build_credentials())
        resource_group = _cfg("azure_resource_group") or "dashboard-rg"
        # A POV build names its location (the POV's): pov_cloud_env.resolve_image refuses
        # an image registered in another one.
        location = req.region or _cfg("azure_location") or "centralus"
        image_only = req.register_mode == "image_only"
        if image_only:
            # The builder takes its location from build_resource_group_name, so a build
            # in the POV's location needs a group THERE; the dashboard's own may be in
            # any region. One per location, kept: it holds the images POVs boot from.
            resource_group = _pov_image_resource_group(location)
            job_service.update_progress(
                db, job_id, 3, f"Ensuring resource group {resource_group} in {location}…")
            await azure_service.ensure_resource_group(
                resource_group, location, {"BuiltBy": "vm-dashboard", "purpose": "pov-images"})

        env = _base_env()
        # ARM_* kept for any subprocess that honors them; the azure-arm builder
        # itself reads the credentials from the source block via these PKR_VAR_*.
        env["ARM_CLIENT_ID"] = client_id
        env["ARM_TENANT_ID"] = tenant_id
        env["ARM_SUBSCRIPTION_ID"] = subscription_id
        if client_secret:
            env["ARM_CLIENT_SECRET"] = client_secret
        env["PKR_VAR_client_id"] = client_id
        env["PKR_VAR_client_secret"] = client_secret or ""
        # Never logged and never in the template file: a sensitive var, in this
        # subprocess's environment only.
        env["PKR_VAR_client_jwt"] = client_jwt
        env["PKR_VAR_tenant_id"] = tenant_id
        env["PKR_VAR_subscription_id"] = subscription_id
        env["PKR_VAR_resource_group"] = resource_group
        env["PKR_VAR_location"] = location

        is_windows = req.os_type.lower() == "windows"
        is_win_client = is_windows and bool(getattr(req, "trusted_launch", False))
        # Linux builds also publish to a Compute Gallery: a captured managed image
        # references the Packer-deleted source disk (so it can't be exported to VHD,
        # and Azure rejects a managed-image ID in a disk's creationData.imageReference),
        # whereas a gallery version is self-contained and exportable. Windows Server
        # still uses the managed-image path (gallery migration is a follow-up).
        # A POV build (image_only) never exports, and boots a managed image fine
        # (pov_cloud_azure._image_reference), so it skips the gallery on both OSes.
        is_linux_gallery = not is_windows and not image_only
        use_gallery = is_win_client or is_linux_gallery

        # Ensure the gallery + a matching image definition, then hand their
        # coordinates to Packer via PKR_VAR_gallery_*. Win 11 needs Trusted Launch
        # (Azure can't create a managed image from a TL VM); Linux uses a plain
        # Generalized definition.
        if use_gallery:
            from datetime import datetime
            gallery_rg = req.gallery_resource_group or _cfg("azure_gallery_resource_group") or resource_group
            gallery_name = req.gallery_name or _cfg("azure_shared_image_gallery") or "vmDashboardGallery"
            img_def_name = req.gallery_image_name or packer_service._safe_azure_name(req.image_name)
            _now = utcnow()
            # Version components must be integers < 2^31 — YYMMDD.HHMMSS fits and is monotonic.
            img_version = req.gallery_image_version or f"1.{int(_now.strftime('%y%m%d'))}.{int(_now.strftime('%H%M%S'))}"
            job_service.update_progress(
                db, job_id, 4,
                f"Ensuring Compute Gallery '{gallery_name}' + image definition '{img_def_name}'…")
            await azure_service.ensure_gallery(gallery_rg, location, gallery_name)
            if is_win_client:
                await azure_service.ensure_trusted_launch_image_definition(
                    gallery_rg, gallery_name, img_def_name, location)
            else:
                # The image-definition generation must match the built VM's, which
                # follows the source marketplace SKU (…-gen2 → Gen2, else Gen1).
                hyper_v_gen = "V2" if "gen2" in f"{req.image_sku} {req.image_offer}".lower() else "V1"
                await azure_service.ensure_linux_image_definition(
                    gallery_rg, gallery_name, img_def_name, location, hyper_v_gen)
            env["PKR_VAR_gallery_subscription"] = subscription_id
            env["PKR_VAR_gallery_resource_group"] = gallery_rg
            env["PKR_VAR_gallery_name"] = gallery_name
            env["PKR_VAR_gallery_image_name"] = img_def_name
            env["PKR_VAR_gallery_image_version"] = img_version

        job_service.update_progress(db, job_id, 5, "Generating Packer template…")
        has_script = bool(req.provisioner_script.strip())
        if is_win_client:
            generate = packer_service.generate_azure_windows_client_template
        elif is_windows:
            generate = packer_service.generate_azure_windows_template
        elif image_only:
            generate = packer_service.generate_azure_template
        else:
            generate = packer_service.generate_azure_linux_gallery_template
        gen_kwargs = dict(
            image_publisher=req.image_publisher,
            image_offer=req.image_offer,
            image_sku=req.image_sku,
            vm_size=req.vm_size,
            image_name=req.image_name,
            has_provisioner=has_script,
        )
        # The two managed-image generators take bundled scripts and a recipe tag. The
        # gallery ones never see a POV recipe, so they keep their demo-tab signature.
        managed = generate in (packer_service.generate_azure_windows_template,
                               packer_service.generate_azure_template)
        scripts = _stage_bundled(build_dir, req.provisioner_files) if managed else []
        if scripts and has_script:
            if is_windows:
                scripts.append("provision.ps1")
            else:
                # Before the last bundled script, which seals the image (as on AWS).
                scripts.insert(len(scripts) - 1, "provision.sh")
        if managed:
            gen_kwargs["scripts"] = scripts or None
            gen_kwargs["extra_tags"] = {"pov-recipe": req.recipe} if req.recipe else None
        # Provisioner env (BT_* + generic) goes to the shell provisioner and, through the
        # same environment_vars, to the Windows Server PowerShell one — the POV Windows
        # recipe's staged installer arrives as POV_STAGE_URL. The Windows client
        # generator has no env block, so it is still not resolved there.
        if (has_script or scripts) and not is_win_client:
            plain_env, sensitive_var_names, pkr_env = await _provisioner_env(req)
            env.update(pkr_env)
            gen_kwargs["provisioner_env"] = plain_env
            gen_kwargs["provisioner_sensitive_var_names"] = sensitive_var_names
        template = generate(**gen_kwargs)
        (build_dir / "build.pkr.hcl").write_text(template)
        if is_windows:
            _write_ps_provisioner(build_dir, req.provisioner_script)
            if is_win_client and req.vm_size.startswith("Standard_B"):
                job_service.update_progress(
                    db, job_id, 5,
                    f"Warning: {req.vm_size} (B-series) does not support Trusted Launch — "
                    "Windows 11 needs a Gen2/Trusted-Launch size such as Standard_D2s_v3.",
                )
            elif req.vm_size in ("Standard_B1s", "Standard_B2s"):
                job_service.update_progress(
                    db, job_id, 5,
                    f"Note: {req.vm_size} (≤4 GB RAM) makes Windows builds very slow — "
                    "Standard_D2s_v3 or larger is recommended.",
                )
        else:
            _write_provisioner(build_dir, req.provisioner_script)

        def on_progress(pct, msg):
            job_service.update_progress(db, job_id, pct, msg)

        def on_log(line):
            job_service.append_job_log(db, job_id, line)

        result = await packer_service.run_build(
            "azure", build_dir, env, on_progress,
            is_cancelled=_cancel_checker(job_id), on_log=on_log)

        # Archive template
        if req.archive_template:
            storage_account = _cfg("packer_azure_storage_account")
            container = _cfg("packer_azure_archive_container") or "packer-templates"
            if storage_account:
                try:
                    uri = await packer_service.archive_to_azure_blob(
                        build_dir / "build.pkr.hcl",
                        job_id, req.image_name, storage_account, container,
                        {"azure_client_id": client_id, "azure_client_secret": client_secret,
                         "azure_tenant_id": tenant_id},
                    )
                    result["template_archive"] = uri
                    job_service.update_progress(db, job_id, 98, f"Template archived: {uri}")
                except Exception as e:
                    result["archive_error"] = str(e)

        if image_only:
            result.update(register_azure_image(
                db, job_id, req.image_name, result.get("artifact_id") or "", location,
                created_by, os_type=req.os_type, recipe=req.recipe))
            if result.get("register_error"):
                # The catalog row IS this build's deliverable, as on AWS.
                raise PackerError(
                    f"managed image {result.get('artifact_id') or '(unknown)'} was built "
                    f"but not registered: {result['register_error']}")
        elif use_gallery:
            # Gallery builds (Win-client TL + all Linux) publish a gallery image
            # VERSION — there's no managed image to export to VHD. Register the
            # version directly.
            gallery_version_id = (
                f"/subscriptions/{subscription_id}/resourceGroups/{gallery_rg}"
                f"/providers/Microsoft.Compute/galleries/{gallery_name}"
                f"/images/{img_def_name}/versions/{img_version}"
            )
            result["gallery_image_version_id"] = gallery_version_id
            result.update(await _register_gallery_image(
                db, job_id, req.image_name, gallery_version_id, location, created_by,
                os_type=req.os_type, recipe=req.recipe))
        else:
            # Windows Server (managed image): export to portable VHD + auto-register.
            export_result = await export_and_register_azure(
                db, job_id, req.image_name, result.get("artifact_id") or "", resource_group, created_by,
                os_type=req.os_type, recipe=req.recipe, location=location,
            )
            result.update(export_result)

        job_service.set_completed(db, job_id, result)

    except PackerError as e:
        job_service.set_failed(db, job_id, str(e))
    except PackerCancelled:
        # Operator cancelled: packer was signalled and ran its own cleanup, so the
        # temp build instance is torn down. The job is already 'cancelled' (that
        # flip is what the watcher polled for) — leave it, don't mark it failed.
        logger.info("packer build %s cancelled; build instance cleaned up", job_id)
    except Exception as e:
        job_service.set_failed(db, job_id, f"Unexpected error: {e}")
    finally:
        db.close()


async def _run_gcp_build(job_id: str, req: GCPPackerBuildRequest, created_by: str = "system") -> None:
    db = _get_db_session()
    build_dir = packer_service.BUILDS_DIR / job_id
    creds_file = None
    try:
        job_service.set_running(db, job_id)
        build_dir.mkdir(parents=True, exist_ok=True)

        project_id = _cfg("gcp_project_id")
        zone = _cfg("gcp_zone") or "us-central1-a"
        sa_json = _cfg("gcp_service_account_json")

        if not project_id:
            raise PackerError("GCP project_id not configured. Go to Setup → GCP.")

        env = _base_env()
        env["PKR_VAR_project_id"] = project_id
        env["PKR_VAR_zone"] = zone

        # Write service account key to a temp file; set ADC env var
        if sa_json:
            creds_file = build_dir / "credentials.json"
            creds_file.write_text(sa_json)
            env["GOOGLE_APPLICATION_CREDENTIALS"] = str(creds_file)
        else:
            # The dashboard's own SPIFFE identity: the external_account config beside
            # its token, which googlecompute reads like any ADC file.
            from . import cloud_federation
            env.update({k: v for k, v in (cloud_federation.gcp_subprocess_env() or {}).items()
                        if k == "GOOGLE_APPLICATION_CREDENTIALS"})

        # Name the source in the job log. A provisioner that refuses the image it
        # was given (the network cell's vyatta check) otherwise fails against a
        # source only Packer's own "Using image:" line records.
        src_desc = (f"image {req.source_image_name}" if req.source_image_name
                    else f"family {req.source_image}")
        if req.source_image_project:
            src_desc += f" in project {req.source_image_project}"
        job_service.update_progress(
            db, job_id, 5, f"Generating Packer template — source {src_desc}…")
        if req.provisioner_script.strip():
            plain_env, sensitive_var_names, pkr_env = await _provisioner_env(req)
        else:
            plain_env, sensitive_var_names, pkr_env = {}, {}, {}
        env.update(pkr_env)
        template = packer_service.generate_gcp_template(
            source_image=req.source_image,
            source_image_name=req.source_image_name,
            source_image_project=req.source_image_project,
            machine_type=req.machine_type,
            ssh_username=req.ssh_username,
            image_name=req.image_name,
            project_id=project_id,
            zone=zone,
            has_provisioner=bool(req.provisioner_script.strip()),
            provisioner_env=plain_env,
            provisioner_sensitive_var_names=sensitive_var_names,
            disk_type=req.disk_type,
            disk_size=req.disk_size_gb,
        )
        (build_dir / "build.pkr.hcl").write_text(template)
        _write_provisioner(build_dir, req.provisioner_script)

        def on_progress(pct, msg):
            job_service.update_progress(db, job_id, pct, msg)

        def on_log(line):
            job_service.append_job_log(db, job_id, line)

        result = await packer_service.run_build(
            "gcp", build_dir, env, on_progress,
            is_cancelled=_cancel_checker(job_id), on_log=on_log)

        # Archive template
        if req.archive_template:
            bucket = _cfg("packer_gcs_bucket")
            if bucket and sa_json:
                try:
                    uri = await packer_service.archive_to_gcs(
                        build_dir / "build.pkr.hcl",
                        job_id, req.image_name, bucket,
                        {"gcp_service_account_json": sa_json},
                    )
                    result["template_archive"] = uri
                    job_service.update_progress(db, job_id, 98, f"Template archived: {uri}")
                except Exception as e:
                    result["archive_error"] = str(e)

        # Export to portable VHD + auto-register (Phase 2)
        export_result = await export_and_register_gcp(
            db, job_id, req.image_name, result.get("artifact_id") or "", project_id, created_by,
        )
        result.update(export_result)

        job_service.set_completed(db, job_id, result)

    except PackerError as e:
        job_service.set_failed(db, job_id, str(e))
    except PackerCancelled:
        # Operator cancelled: packer was signalled and ran its own cleanup, so the
        # temp build instance is torn down. The job is already 'cancelled' (that
        # flip is what the watcher polled for) — leave it, don't mark it failed.
        logger.info("packer build %s cancelled; build instance cleaned up", job_id)
    except Exception as e:
        job_service.set_failed(db, job_id, f"Unexpected error: {e}")
    finally:
        # Always remove the service account key from disk
        if creds_file and creds_file.exists():
            try:
                creds_file.unlink()
            except Exception:
                pass
        db.close()


async def _run_oci_build(job_id: str, req: OCIPackerBuildRequest, created_by: str = "system") -> None:
    db = _get_db_session()
    build_dir = packer_service.BUILDS_DIR / job_id
    try:
        job_service.set_running(db, job_id)
        build_dir.mkdir(parents=True, exist_ok=True)

        tenancy = _cfg("oci_tenancy_ocid")
        user = _cfg("oci_user_ocid")
        fingerprint = _cfg("oci_fingerprint")
        private_key = _cfg("oci_private_key")
        passphrase = _cfg("oci_private_key_passphrase")
        region = _cfg("oci_region") or "us-ashburn-1"
        compartment = _cfg("oci_compartment_ocid") or tenancy
        subnet = req.subnet_ocid or _cfg("oci_default_subnet_ocid")

        # Fail loudly and by name. The oracle-oci plugin reads no OCI_* env vars, so
        # an unconfigured credential surfaces as an opaque config-provider error from
        # deep inside the plugin — worth pre-empting the way the Azure runner does.
        missing = [n for n, v in (
            ("oci_tenancy_ocid", tenancy), ("oci_user_ocid", user),
            ("oci_fingerprint", fingerprint), ("oci_private_key", private_key),
        ) if not v]
        if missing:
            raise PackerError(
                "OCI is not configured — missing " + ", ".join(missing)
                + ". Go to Setup → OCI.")
        if not subnet:
            raise PackerError(
                "No subnet for the build instance. Pick one on the build form, or set "
                "oci_default_subnet_ocid in Setup → OCI. It must be a public subnet that "
                "allows inbound TCP 22 from the dashboard — Packer connects over the internet.")

        # Re-checked here, not just at the API route: a job can be re-queued
        # directly, and the route's copy of this check fails open when OCI is
        # briefly unreachable. Cheap (two list calls) against a build that runs
        # for minutes, and it is the difference between a named cause and
        # LaunchInstance's unattributed 404 NotAuthorizedOrNotFound.
        try:
            await oci_service.check_launch_placement(
                availability_domain=req.availability_domain,
                image_ocid=req.base_image_ocid, shape=req.shape,
                compartment_id=compartment, region=region)
        except OCIError as exc:
            raise PackerError(str(exc)) from exc

        env = _base_env()
        env["PKR_VAR_tenancy_ocid"] = tenancy
        env["PKR_VAR_user_ocid"] = user
        env["PKR_VAR_fingerprint"] = fingerprint
        env["PKR_VAR_oci_key"] = private_key
        env["PKR_VAR_oci_pass"] = passphrase or ""
        env["PKR_VAR_region"] = region

        job_service.update_progress(db, job_id, 5, "Generating Packer template…")
        # sensitive_var_names holds env-var name → Packer variable NAME, not the
        # secrets themselves; the resolved values live only in pkr_env, which goes
        # onto the subprocess environment and never into the template.
        if req.provisioner_script.strip():
            plain_env, sensitive_var_names, pkr_env = await _provisioner_env(req)
        else:
            plain_env, sensitive_var_names, pkr_env = {}, {}, {}
        env.update(pkr_env)
        template = packer_service.generate_oci_template(
            base_image_ocid=req.base_image_ocid,
            shape=req.shape,
            availability_domain=req.availability_domain,
            compartment_ocid=compartment,
            subnet_ocid=subnet,
            ssh_username=req.ssh_username,
            image_name=req.image_name,
            has_provisioner=bool(req.provisioner_script.strip()),
            provisioner_env=plain_env,
            provisioner_sensitive_var_names=sensitive_var_names,
            ocpus=req.ocpus,
            memory_gb=req.memory_gb,
            boot_volume_gb=req.boot_volume_gb,
        )
        (build_dir / "build.pkr.hcl").write_text(template)
        _write_provisioner(build_dir, req.provisioner_script)

        def on_progress(pct, msg):
            job_service.update_progress(db, job_id, pct, msg)

        def on_log(line):
            job_service.append_job_log(db, job_id, line)

        result = await packer_service.run_build(
            "oci", build_dir, env, on_progress,
            is_cancelled=_cancel_checker(job_id), on_log=on_log)

        # Archive template
        if req.archive_template:
            bucket = _cfg("packer_oci_bucket") or _cfg("storage_oci_bucket")
            if bucket:
                try:
                    uri = await packer_service.archive_to_oci(
                        build_dir / "build.pkr.hcl", job_id, req.image_name, bucket)
                    result["template_archive"] = uri
                    job_service.update_progress(db, job_id, 98, f"Template archived: {uri}")
                except Exception as e:
                    result["archive_error"] = str(e)

        # Export to portable VHD + auto-register
        export_result = await export_and_register_oci(
            db, job_id, req.image_name, result.get("artifact_id") or "", created_by,
        )
        result.update(export_result)

        job_service.set_completed(db, job_id, result)

    except PackerError as e:
        job_service.set_failed(db, job_id, str(e))
    except PackerCancelled:
        # Operator cancelled: packer was signalled and ran its own cleanup, so the
        # temp build instance is torn down. The job is already 'cancelled' (that
        # flip is what the watcher polled for) — leave it, don't mark it failed.
        logger.info("packer build %s cancelled; build instance cleaned up", job_id)
    except Exception as e:
        job_service.set_failed(db, job_id, f"Unexpected error: {e}")
    finally:
        db.close()


# ── Shared helpers ────────────────────────────────────────────────────────────

def _archive_credentials(env: dict, region: str) -> dict:
    """boto3 credentials for the template archive, from the env the build ran with.

    The build env may carry no keys at all: on Workload Credentials it is an AWS profile
    whose credential_process the Packer plugin runs. The archive is uploaded in-process,
    so it asks the lease directly instead (still inside the job, so the provisioning
    lease). The session token rides along either way: a dynamic key without it is
    refused as InvalidClientTokenId.
    """
    key_id = env.get("AWS_ACCESS_KEY_ID") or ""
    secret = env.get("AWS_SECRET_ACCESS_KEY") or ""
    token = env.get("AWS_SESSION_TOKEN") or ""
    if not key_id and env.get("AWS_PROFILE"):
        from .workload_credential_lease import credentials
        values = credentials("aws") or {}
        key_id = values.get("access_key_id", "")
        secret = values.get("secret_access_key", "")
        token = values.get("session_token", "")
    # None, not "": boto3 then falls back to its own chain rather than signing with blanks.
    return {"aws_access_key_id": key_id or None, "aws_secret_access_key": secret or None,
            "aws_session_token": token or None, "aws_region": region}


def _base_env() -> dict:
    """Start from the current process env so PATH, HOME, etc. are inherited."""
    return dict(os.environ)


def _write_provisioner(build_dir: Path, script: str) -> None:
    """Write provision.sh. Always creates the file even if empty."""
    content = script.strip()
    if not content:
        content = "#!/bin/sh\necho 'Build complete.'"
    if not content.startswith("#!"):
        content = "#!/bin/sh\n" + content
    path = build_dir / "provision.sh"
    path.write_text(content)
    path.chmod(0o755)


def _write_ps_provisioner(build_dir: Path, script: str) -> None:
    """Write provision.ps1 for Windows builds. Always creates the file."""
    content = script.strip() or "Write-Output 'Build complete.'"
    (build_dir / "provision.ps1").write_text(content)


# The scripts shipped in the image (the Dockerfile COPYs provisioners/). A standard
# POV recipe names its scripts here rather than asking anyone to upload them first.
PROVISIONERS_DIR = Path(__file__).parent.parent.parent / "provisioners"


def _stage_bundled(build_dir: Path, files: list) -> list:
    """Copy bundled provisioner scripts into the build directory; their names, in order.

    Each name is checked to resolve INSIDE ``PROVISIONERS_DIR`` as well as matching the
    request model's pattern, and a missing one fails the build here, naming it — an
    image whose Dockerfile forgot the COPY would otherwise build a bare base AMI and
    register it as the standard one.
    """
    staged = []
    root = PROVISIONERS_DIR.resolve()
    for rel in files or []:
        src = (PROVISIONERS_DIR / rel).resolve()
        if root not in src.parents:
            raise PackerError(f"provisioner file {rel!r} is outside provisioners/")
        if not src.is_file():
            raise PackerError(
                f"bundled provisioner {rel} is not in this image ({src}). The image was "
                f"built without provisioners/ — rebuild it from a current Dockerfile.")
        name = src.name
        if name in staged:
            raise PackerError(f"two bundled provisioners are both named {name}")
        # Bytes, not text: a .ps1 keeps its CRLF and a .sh its LF exactly as shipped.
        dest = build_dir / name
        dest.write_bytes(src.read_bytes())
        if name.endswith(".sh"):
            dest.chmod(0o755)
        staged.append(name)
    return staged


# ── Export + auto-register helpers (Phase 2/3 — build-once, promote-many) ────
#
# Each helper runs after the cloud-native Packer build succeeds. The flow:
#
#   1. Native export to same-cloud storage (only cloud-native APIs can pull
#      bytes out of a freshly built AMI / managed image / GCE image, so the
#      first hop is always same-cloud — S3 for AWS, Blob for Azure, GCS for
#      GCP).
#   2. If the hub backend (`storage_service.hub_backend()`) is the same as
#      same-cloud storage, register with the URL the native export already
#      produced — done.
#   3. Else, copy the artefact from same-cloud staging to the hub backend
#      (Phase 3 cross-backend copy), delete the same-cloud staging blob, and
#      register with the hub URL.
#
# Result: `RegisteredImage.artefact_url` always points at the hub regardless
# of which cloud built the image, satisfying the "hub holds the source of
# truth" design contract. Per-cloud storage is still required for the export
# step (AWS only exports to S3, Azure to Blob, GCS to GCS) but it's used as
# transient staging when the operator's hub is on a different cloud.

def _versioned_blob_name(image_name: str, ext: str = "vhd") -> str:
    from datetime import datetime
    ts = utcnow().strftime("%Y%m%dT%H%M%SZ")
    return f"images/{image_name}-{ts}.{ext}"


async def _land_on_hub(
    db,
    job_id: str,
    *,
    build_backend: str,
    build_key: str,
    image_name: str,
    image_ext: str = "vhd",
) -> tuple[str, str]:
    """Ensure the artefact at `build_key` on `build_backend` ends up on the
    hub backend. If the hub IS the build backend, no-op (returns the input).
    Otherwise copies to the hub, deletes the build-side staging copy, and
    returns the new (hub_backend, hub_key) pair.

    Returns: (final_backend, final_key) for use in artefact_url generation.
    Raises StorageError on copy failure — caller decides whether to surface
    it to the build job as an export_error or fall back to the build-side URL.
    """
    hub = storage_service.hub_backend()
    if not hub:
        # No usable hub — treat the build-side staging as the artefact home.
        # This matches pre-Phase-3 behavior for installs that haven't set up
        # any backend at all.
        return (build_backend, build_key)
    if hub == build_backend:
        return (build_backend, build_key)

    hub_key = storage_service.image_key(hub, image_name, ext=image_ext)
    job_service.update_progress(
        db, job_id, 97,
        f"Copying artefact to hub backend '{hub}': {build_backend}://{build_key} → {hub}://{hub_key}",
    )
    await storage_service.copy(
        src_backend=build_backend, src_key=build_key,
        dst_backend=hub, dst_key=hub_key,
        progress_cb=lambda line: job_service.update_progress(db, job_id, 97, line[:200]),
    )
    # Clean up the build-side staging copy so the operator doesn't pay for
    # two copies of every multi-GB VHD. Best-effort — log if it fails but
    # don't fail the whole build, the canonical artefact is already on the
    # hub at this point.
    try:
        job_service.update_progress(db, job_id, 98, f"Cleaning up build-side staging on '{build_backend}'")
        await storage_service.delete_image_in(build_backend, build_key)
    except Exception as e:
        logger.warning(
            "Failed to delete build-side staging copy %s://%s after hub copy: %s",
            build_backend, build_key, e,
        )
    return (hub, hub_key)


# ── Worker entrypoints for manual export/register jobs ──────────────────────
# Wrap the export_and_register_* helpers with the job lifecycle the API
# endpoint's BackgroundTask closure used to own. The worker (jobs_worker.py)
# calls these after claiming the pending job (the claim already set it running),
# reading args from the job metadata. Export errors are FATAL here (unlike the
# non-fatal auto-export inside a build).

async def run_export_aws(job_id: str, meta: dict) -> None:
    d = _get_db_session()
    try:
        result = await export_and_register_aws(
            d, job_id, meta["image_name"], meta["ami_id"], meta["region"],
            meta.get("created_by", "system"),
        )
        if result.get("export_error") or result.get("export_skipped"):
            job_service.set_failed(d, job_id, result.get("export_error") or result["export_skipped"])
        else:
            job_service.set_completed(d, job_id, result)
    except Exception as e:
        job_service.set_failed(d, job_id, f"Export failed: {e}")
    finally:
        d.close()


async def run_export_gcp(job_id: str, meta: dict) -> None:
    d = _get_db_session()
    try:
        result = await export_and_register_gcp(
            d, job_id, meta["registry_name"], meta["image_name"], meta["project_id"],
            meta.get("created_by", "system"),
        )
        if result.get("export_error") or result.get("export_skipped"):
            job_service.set_failed(d, job_id, result.get("export_error") or result["export_skipped"])
        else:
            job_service.set_completed(d, job_id, result)
    except Exception as e:
        job_service.set_failed(d, job_id, f"Export failed: {e}")
    finally:
        d.close()


async def run_export_azure(job_id: str, meta: dict) -> None:
    d = _get_db_session()
    try:
        result = await export_and_register_azure(
            d, job_id, meta["registry_name"], meta["image_name"], meta["resource_group"],
            meta.get("created_by", "system"), os_type=meta.get("os_type", "Linux"),
        )
        if result.get("export_error") or result.get("export_skipped"):
            job_service.set_failed(d, job_id, result.get("export_error") or result["export_skipped"])
        else:
            job_service.set_completed(d, job_id, result)
    except Exception as e:
        job_service.set_failed(d, job_id, f"Export failed: {e}")
    finally:
        d.close()


async def run_export_oci(job_id: str, meta: dict) -> None:
    d = _get_db_session()
    try:
        result = await export_and_register_oci(
            d, job_id, meta["registry_name"], meta["image_ocid"],
            meta.get("created_by", "system"),
        )
        if result.get("export_error") or result.get("export_skipped"):
            job_service.set_failed(d, job_id, result.get("export_error") or result["export_skipped"])
        else:
            job_service.set_completed(d, job_id, result)
    except Exception as e:
        job_service.set_failed(d, job_id, f"Export failed: {e}")
    finally:
        d.close()


async def export_and_register_oci(
    db, job_id: str, image_name: str, artefact_id: str, created_by: str,
) -> dict:
    """Export an OCI custom image to VHD on the hub and register.

    `artefact_id` is the source image OCID. `image_name` is the registry name.

    VHD rather than OCI's default QCOW2 on purpose: it is the hub's canonical
    format, so the result needs no qemu-img conversion and is promotable to every
    other cloud (Azure's import accepts vhd only)."""
    from . import oci_service

    result: dict = {}
    if not artefact_id:
        result["export_skipped"] = "no image OCID provided"
        return result

    bucket = _cfg("storage_oci_bucket")
    if not bucket:
        msg = (
            "Export skipped: no OCI Object Storage bucket configured (set storage_oci_bucket "
            "on /storage). OCI native export only writes to Object Storage — required even "
            "when the hub is on another cloud."
        )
        job_service.update_progress(db, job_id, 99, msg)
        result["export_skipped"] = msg
        return result

    object_path = _versioned_blob_name(image_name)

    def _on_progress(line: str) -> None:
        job_service.update_progress(db, job_id, 96, line[:200])

    try:
        job_service.update_progress(
            db, job_id, 96, f"Exporting {artefact_id} to oci://{bucket}/{object_path}")
        export = await oci_service.export_image_to_object_storage(
            image_ocid=artefact_id,
            bucket=bucket,
            object_name=object_path,
            export_format="VHD",
            progress_cb=_on_progress,
        )
        result["export"] = export

        final_backend, final_key = await _land_on_hub(
            db, job_id,
            build_backend="oci_object_storage", build_key=object_path,
            image_name=image_name, image_ext="vhd",
        )
        artefact_url = storage_service.image_url(final_backend, final_key)

        registered = image_registry_service.register_image(
            db,
            name=image_name,
            version=object_path.split("/")[-1].rsplit(".", 1)[0],
            source_cloud="oci",
            created_by=created_by,
            description=f"Registered by job {job_id}",
            source_image_id=artefact_id,
            source_region=_cfg("oci_region") or "",
            artefact_url=artefact_url,
            artefact_format="vhd",
        )
        result["registered_image_id"] = registered["id"]
        result["artefact_backend"] = final_backend
        job_service.update_progress(db, job_id, 99, f"Image registered: {registered['id']}")
    except Exception as e:
        msg = f"Export/register failed: {e}"
        logger.exception("OCI export/register failed for job %s", job_id)
        job_service.update_progress(db, job_id, 99, msg)
        result["export_error"] = str(e)
    return result


async def export_and_register_aws(
    db, job_id: str, image_name: str, artefact_id: str,
    region: str, created_by: str,
) -> dict:
    """Export an AMI to VHD on the hub backend and register the result.

    Returns a dict with keys among {export, registered_image_id,
    artefact_backend, export_error, export_skipped}. Used by both the
    Packer post-build flow and the manual /api/aws/images/{id}/export
    endpoint."""
    result: dict = {}
    if not artefact_id:
        result["export_skipped"] = "no AMI ID provided"
        return result

    # AWS ec2 export-image only writes to S3 — the operator must have an S3
    # bucket configured even if their hub backend is Azure/GCS. After the
    # native export, _land_on_hub() handles the cross-backend copy when the
    # hub isn't S3.
    if not _cfg("storage_s3_bucket"):
        msg = (
            "Export skipped: no S3 bucket configured (set storage_s3_bucket on /storage). "
            "AWS native export only writes to S3 — required even when the hub is on another cloud."
        )
        job_service.update_progress(db, job_id, 99, msg)
        result["export_skipped"] = msg
        return result

    bucket = _cfg("storage_s3_bucket")
    s3_prefix = (_cfg("storage_s3_prefix") or "config-mgmt").rstrip("/") + "/images/"
    role_name = _cfg("aws_vmimport_role_name") or "vmimport"

    def _on_progress(line: str) -> None:
        job_service.update_progress(db, job_id, 96, line[:200])

    try:
        job_service.update_progress(db, job_id, 96, f"Exporting {artefact_id} to VHD on s3://{bucket}/{s3_prefix}")
        export = await aws_service.export_image_to_vhd(
            region=region,
            ami_id=artefact_id,
            s3_bucket=bucket,
            s3_prefix=s3_prefix,
            role_name=role_name,
            description=f"Exported by job {job_id}",
            progress_cb=_on_progress,
        )
        result["export"] = export

        # AWS export_image picks the object name (`<task-id>.vhd`). Derive the
        # S3 key from the returned URL so _land_on_hub knows what to copy.
        s3_url = export["s3_url"]
        build_key = s3_url[len(f"s3://{bucket}/"):]
        final_backend, final_key = await _land_on_hub(
            db, job_id,
            build_backend="s3", build_key=build_key,
            image_name=image_name, image_ext="vhd",
        )
        artefact_url = storage_service.image_url(final_backend, final_key)

        registered = image_registry_service.register_image(
            db,
            name=image_name,
            version=export["task_id"],
            source_cloud="aws",
            created_by=created_by,
            description=f"Registered by job {job_id}",
            source_image_id=artefact_id,
            source_region=region,
            artefact_url=artefact_url,
            artefact_format="vhd",
        )
        result["registered_image_id"] = registered["id"]
        result["artefact_backend"] = final_backend
        job_service.update_progress(db, job_id, 99, f"Image registered: {registered['id']}")
    except Exception as e:
        msg = f"Export/register failed: {e}"
        logger.exception("AWS export/register failed for job %s", job_id)
        job_service.update_progress(db, job_id, 99, msg)
        result["export_error"] = str(e)
    return result


def register_aws_ami(
    db, job_id: str, image_name: str, ami_id: str, region: str, created_by: str,
    os_type: str = "Linux", recipe: str = None,
) -> dict:
    """Register a freshly built AMI as itself: no VHD export, no S3 bucket.

    The POV builder's path. A POV boots the AMI in the region it was built in and never
    wants a portable copy, and ``export_and_register_aws`` registers nothing at all on an
    install with no ``storage_s3_bucket`` — which would leave a finished build that the
    template editor can never pick. Same shape as ``_register_gallery_image``.
    """
    if not ami_id:
        msg = "Build finished but no AMI id was found in Packer's output; nothing registered."
        job_service.update_progress(db, job_id, 99, msg)
        return {"register_error": msg}
    try:
        registered = image_registry_service.register_image(
            db,
            name=image_name,
            version=utcnow().strftime("%Y%m%d-%H%M%S"),
            source_cloud="aws",
            created_by=created_by,
            description=f"Built by job {job_id}",
            source_image_id=ami_id,
            source_region=region,
            os_type=os_type,
            recipe=recipe,
        )
        job_service.update_progress(db, job_id, 99, f"Image registered: {registered['id']}")
        return {"registered_image_id": registered["id"]}
    except Exception as e:
        logger.exception("AMI registration failed for job %s", job_id)
        job_service.update_progress(db, job_id, 99, f"Image registration failed: {e}")
        return {"register_error": str(e)}


def _pov_image_resource_group(location: str) -> str:
    """The resource group a POV image build runs in and leaves its managed image in."""
    return f"pov-images-{location}"


def register_azure_image(
    db, job_id: str, image_name: str, image_id: str, location: str, created_by: str,
    os_type: str = "Linux", recipe: str = None,
) -> dict:
    """Register a freshly built managed image as itself: no VHD export, no gallery.

    The Azure twin of ``register_aws_ami``, for the same reason: a POV boots the image
    in the location it was built in, and ``export_and_register_azure`` registers nothing
    on an install with no ``storage_azure_account``. ``image_id`` is Packer's artifact
    id, the managed image's resource id, which ``pov_cloud_azure._image_reference``
    takes as it is.
    """
    if not image_id.startswith("/"):
        msg = ("Build finished but no managed image id was found in Packer's output; "
               "nothing registered.")
        job_service.update_progress(db, job_id, 99, msg)
        return {"register_error": msg}
    try:
        registered = image_registry_service.register_image(
            db,
            name=image_name,
            version=utcnow().strftime("%Y%m%d-%H%M%S"),
            source_cloud="azure",
            created_by=created_by,
            description=f"Built by job {job_id}",
            source_image_id=image_id,
            source_region=location,
            os_type=os_type,
            recipe=recipe,
        )
        job_service.update_progress(db, job_id, 99, f"Image registered: {registered['id']}")
        return {"registered_image_id": registered["id"]}
    except Exception as e:
        logger.exception("Managed image registration failed for job %s", job_id)
        job_service.update_progress(db, job_id, 99, f"Image registration failed: {e}")
        return {"register_error": str(e)}


async def _register_gallery_image(
    db, job_id: str, registry_name: str, gallery_version_id: str,
    location: str, created_by: str, os_type: str = "Linux", recipe: str = None,
) -> dict:
    """Register a Compute Gallery image VERSION in the image registry.

    Gallery builds (Win-client Trusted Launch + all Linux) publish a gallery
    version (not a managed image), so there is no VHD to export — we record the
    version resource id directly. No hub artefact_url (the gallery version *is*
    the artefact)."""
    try:
        registered = image_registry_service.register_image(
            db,
            name=registry_name,
            version=gallery_version_id.rstrip("/").split("/")[-1],
            source_cloud="azure",
            created_by=created_by,
            description=f"Compute Gallery image (job {job_id})",
            source_image_id=gallery_version_id,
            source_region=location,
            artefact_url=None,
            artefact_format=None,
            os_type=os_type,
            recipe=recipe,
        )
        job_service.update_progress(db, job_id, 99, f"Gallery image registered: {registered['id']}")
        return {"registered_image_id": registered["id"]}
    except Exception as e:
        logger.exception("Gallery image registration failed for job %s", job_id)
        job_service.update_progress(db, job_id, 99, f"Gallery image registration failed: {e}")
        return {"register_error": str(e)}


async def export_and_register_azure(
    db, job_id: str, image_name: str, artefact_id: str,
    resource_group: str, created_by: str, os_type: str = "Linux",
    recipe: str = None, location: str = "",
) -> dict:
    """Export an Azure managed image to VHD on the hub and register.

    `artefact_id` is the managed-image full resource path (from Packer's
    `artifact_id`) or a bare name. The last path segment is taken as
    the source image name. `image_name` is the registry name."""
    result: dict = {}
    if not artefact_id:
        result["export_skipped"] = "no managed image ID provided"
        return result

    if not _cfg("storage_azure_account"):
        msg = (
            "Export skipped: no Azure storage account configured "
            "(set storage_azure_account on /storage). "
            "Azure native export only writes to Blob — required even when the hub is on another cloud."
        )
        job_service.update_progress(db, job_id, 99, msg)
        result["export_skipped"] = msg
        return result

    src_image_name = artefact_id.rstrip("/").split("/")[-1]
    storage_account = _cfg("storage_azure_account")
    container = _cfg("storage_azure_container") or "playbooks"
    blob_name = _versioned_blob_name(image_name)

    def _on_progress(line: str) -> None:
        job_service.update_progress(db, job_id, 96, line[:200])

    try:
        job_service.update_progress(db, job_id, 96, f"Exporting {src_image_name} to blob {storage_account}/{container}/{blob_name}")
        export = await azure_service.export_managed_image_to_vhd(
            image_rg=resource_group,
            image_name=src_image_name,
            dest_storage_account=storage_account,
            dest_container=container,
            dest_blob_name=blob_name,
            progress_cb=_on_progress,
        )
        result["export"] = export

        final_backend, final_key = await _land_on_hub(
            db, job_id,
            build_backend="azure_blob", build_key=blob_name,
            image_name=image_name, image_ext="vhd",
        )
        artefact_url = storage_service.image_url(final_backend, final_key)

        registered = image_registry_service.register_image(
            db,
            name=image_name,
            version=blob_name.split("/")[-1].rsplit(".", 1)[0],
            source_cloud="azure",
            created_by=created_by,
            description=f"Registered by job {job_id}",
            source_image_id=artefact_id,
            source_region=location or _cfg("azure_location") or "centralus",
            artefact_url=artefact_url,
            artefact_format="vhd",
            os_type=os_type,
            recipe=recipe,
        )
        result["registered_image_id"] = registered["id"]
        result["artefact_backend"] = final_backend
        job_service.update_progress(db, job_id, 99, f"Image registered: {registered['id']}")
    except Exception as e:
        msg = f"Export/register failed: {e}"
        logger.exception("Azure export/register failed for job %s", job_id)
        job_service.update_progress(db, job_id, 99, msg)
        result["export_error"] = str(e)
    return result


async def export_and_register_gcp(
    db, job_id: str, image_name: str, artefact_id: str,
    project_id: str, created_by: str,
) -> dict:
    """Export a GCP custom image to VHD on the hub and register.

    `artefact_id` is the source custom image name. `image_name` is the
    registry name."""
    result: dict = {}
    if not artefact_id:
        result["export_skipped"] = "no image name provided"
        return result

    if not _cfg("storage_gcs_bucket"):
        msg = (
            "Export skipped: no GCS bucket configured (set storage_gcs_bucket on /storage). "
            "GCP native export only writes to GCS — required even when the hub is on another cloud."
        )
        job_service.update_progress(db, job_id, 99, msg)
        result["export_skipped"] = msg
        return result

    bucket = _cfg("storage_gcs_bucket")
    object_path = _versioned_blob_name(image_name)
    network = _cfg("gcp_export_network") or ""
    subnet = _cfg("gcp_export_subnet") or ""
    # Preferred zone for the export worker VM; the exporter falls back to sibling
    # zones on a capacity error. gcp_export_zone lets an operator pin it separately
    # from the deploy zone (gcp_zone) if one region/zone is chronically tight.
    zone = _cfg("gcp_export_zone") or _cfg("gcp_zone") or "us-central1-a"

    def _on_progress(line: str) -> None:
        job_service.update_progress(db, job_id, 96, line[:200])

    try:
        job_service.update_progress(db, job_id, 96, f"Exporting {artefact_id} to gs://{bucket}/{object_path}")
        export = await gcp_service.export_custom_image_to_vhd(
            project_id=project_id,
            image_name=artefact_id,
            dest_bucket=bucket,
            dest_object=object_path,
            network=network,
            subnet=subnet,
            zone=zone,
            progress_cb=_on_progress,
        )
        result["export"] = export

        final_backend, final_key = await _land_on_hub(
            db, job_id,
            build_backend="gcs", build_key=object_path,
            image_name=image_name, image_ext="vhd",
        )
        artefact_url = storage_service.image_url(final_backend, final_key)

        registered = image_registry_service.register_image(
            db,
            name=image_name,
            version=export.get("build_id") or object_path.split("/")[-1].rsplit(".", 1)[0],
            source_cloud="gcp",
            created_by=created_by,
            description=f"Registered by job {job_id}",
            source_image_id=artefact_id,
            source_region=_cfg("gcp_zone") or "",
            artefact_url=artefact_url,
            artefact_format="vhd",
        )
        result["registered_image_id"] = registered["id"]
        result["artefact_backend"] = final_backend
        job_service.update_progress(db, job_id, 99, f"Image registered: {registered['id']}")
    except Exception as e:
        msg = f"Export/register failed: {e}"
        logger.exception("GCP export/register failed for job %s", job_id)
        job_service.update_progress(db, job_id, 99, msg)
        result["export_error"] = str(e)
    return result
