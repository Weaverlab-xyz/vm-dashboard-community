"""Virtual-desktop pool lifecycle.

**All three clouds provision.** ``create_pool`` fans out to a per-cloud SEAT
BACKEND (one **private** VM per seat, tagged with the backend's pool-tag key) and
fills ``vm_resource_id``; ``scale_pool`` / ``delete_pool`` provision / terminate
through the same backend. ``PROVISIONING_CLOUDS`` is derived from
``_SEAT_BACKENDS``, so the advertised set cannot drift from the implemented one.

Every seat is then brokered on the PRA Gateway (``pra_jump_id``), and **which kind
of jump item depends on the guest OS, not the cloud**:

  * **Windows** seats get a Remote RDP jump with a generated admin password vaulted
    for credential injection. Azure is the only cloud that can do Windows — EC2 and
    GCE each deliver Windows credentials by a different mechanism (see ``_AwsSeats``
    / ``_GcpSeats``), so those backends refuse a Windows pool outright.
  * **Linux** seats get a Shell Jump (SSH, port 22) with **no credential injection**:
    they authenticate with the pool's SSH key, the dashboard never holds the private
    half, and the provider wrapper has no SSH-key vault resource. The rep supplies
    the key in PRA. Do not describe this as parity with the Windows path.

The backend split is what keeps that from becoming a branch per cloud through the
middle of ``provision_seats``; see the section comment above ``_AzureSeats`` for the
things the three cannot share.

Provisioning + teardown are **async** (``deploy_vm`` / ``terminate_vm`` are slow)
and **durable**: the API enqueues a ``vdesktop_pool_provision`` /
``vdesktop_pool_teardown`` job (args in its metadata) and the in-container job
runner (``jobs_worker``) claims it and calls ``provision_seats`` /
``teardown_seats`` — so a gunicorn worker recycling mid-provision can no longer
strand a ``pending`` seat with an untracked VM (mirrors k8s / clouddb).
"""
import logging
import re
import uuid

from sqlalchemy.orm import Session

from ..database import VirtualDesktop

logger = logging.getLogger(__name__)

# Tag stamped on each backing VM so live pool state is recoverable from the cloud.
POOL_TAG = "dashboard:desktop_pool"

VALID_CLOUDS = ("aws", "azure", "gcp")
# Clouds that provision real VMs. DERIVED from
# `_SEAT_BACKENDS` at the bottom of the backend section, not maintained beside it: a
# cloud listed here with no backend behind it hands an operator seat rows and no VMs,
# which is the exact bug this whole item exists to fix.
PROVISIONING_CLOUDS: tuple = ()

_AZURE_REQUIRED = ("location", "resource_group", "subnet_id", "vm_size")
_AWS_REQUIRED = ("region", "ami_id", "instance_type", "subnet_id")
_GCP_REQUIRED = ("project_id", "zone", "machine_type", "image_self_link", "subnetwork")


class VDesktopError(Exception):
    pass


def _row_to_dict(row: VirtualDesktop) -> dict:
    return {
        "id":             row.id,
        "cloud":          row.cloud,
        "pool_name":      row.pool_name,
        "kind":           row.kind,
        "vm_resource_id": row.vm_resource_id,
        "status":         row.status,
        "assigned_user":  row.assigned_user,
        "pra_jump_id":    row.pra_jump_id,
        "created_by":     row.created_by,
        "created_at":     row.created_at.isoformat() if row.created_at else "",
    }


# ── Reads ─────────────────────────────────────────────────────────────────────

def list_desktops(db: Session) -> list[dict]:
    rows = db.query(VirtualDesktop).order_by(VirtualDesktop.created_at.desc()).all()
    return [_row_to_dict(r) for r in rows]


def jump_kind(row: VirtualDesktop) -> str:
    """``"shell_jump" | "remote_rdp"`` — read off the seat's stored Terraform state.

    Derived rather than stored in a column so it cannot disagree with the state it
    describes, and because nothing needs it for teardown: ``remove_rdp_jump`` is
    state-driven and destroys whichever ``sra_*`` resource the state holds.

    A seat with no state predates Linux brokering, when only Windows (Azure) seats
    were ever registered — hence the RDP default.
    """
    state = row.pra_tunnel_state or ""
    return "shell_jump" if "sra_shell_jump" in state else "remote_rdp"


def get_seat(db: Session, seat_id: str) -> dict | None:
    """One seat, plus its ``jump_kind``.

    The kind is added HERE and not in ``_row_to_dict``, which ``list_desktops`` and
    ``get_pool`` call once per row — reading a state blob per seat to render a
    table would be paid on every page load for a fact only the session view needs.
    """
    row = db.query(VirtualDesktop).filter(VirtualDesktop.id == seat_id).first()
    if row is None:
        return None
    return dict(_row_to_dict(row), jump_kind=jump_kind(row))


def get_pool(db: Session, name: str) -> list[dict]:
    rows = (db.query(VirtualDesktop).filter(VirtualDesktop.pool_name == name)
            .order_by(VirtualDesktop.created_at).all())
    return [_row_to_dict(r) for r in rows]


def list_pools(db: Session) -> list[dict]:
    rows = db.query(VirtualDesktop).all()
    pools: dict[str, dict] = {}
    for r in rows:
        p = pools.setdefault(r.pool_name, {
            "pool_name": r.pool_name, "cloud": r.cloud, "kind": r.kind,
            "count": 0, "statuses": {},
        })
        p["count"] += 1
        p["statuses"][r.status] = p["statuses"].get(r.status, 0) + 1
    return list(pools.values())


# ── Helpers ───────────────────────────────────────────────────────────────────

def _vm_name_for(pool_name: str, seat_id: str) -> str:
    base = re.sub(r"[^a-z0-9-]", "-", (pool_name or "").lower()).strip("-")[:40] or "desktop"
    return f"{base}-{seat_id[:8]}"


def _parse_vm_id(vm_resource_id: str):
    """rg, name from an Azure VM ARM id (or None, name when unparseable)."""
    parts = (vm_resource_id or "").strip("/").split("/")
    rg = None
    for i, p in enumerate(parts):
        if p.lower() == "resourcegroups" and i + 1 < len(parts):
            rg = parts[i + 1]
    return rg, (parts[-1] if parts else None)


def _pool_cloud(db: Session, seat_ids: list) -> str:
    """The cloud these seats belong to. Every seat in a pool shares one."""
    for sid in seat_ids or []:
        row = db.query(VirtualDesktop).filter(VirtualDesktop.id == sid).first()
        if row is not None:
            return (row.cloud or "").lower()
    return ""


def _pool_spec(db: Session, name: str):
    """The deploy spec + job id stored at create-time, for scale-up. (None, None) if absent."""
    from ..database import Job
    jobs = (db.query(Job).filter(Job.job_type == "vdesktop_pool_provision")
            .order_by(Job.created_at.desc()).all())
    for j in jobs:
        md = j.metadata_dict or {}
        if md.get("pool_name") == name and md.get("spec"):
            return md["spec"], j.id
    return None, None


# ── Per-cloud seat backends ──────────────────────────────────────────────────
# Everything below this comment that differs between clouds lives HERE, so the
# orchestration in `provision_seats` / `teardown_seats` — job lifecycle, progress,
# gateway warm-up ordering, per-seat error collection, PRA registration — is written
# once and does not grow a branch per cloud.
#
# The extraction was deliberately a MOVE: `tests/test_vdesktop_seats.py` was written
# against the pre-extraction Azure code and passed unchanged, which is what made "no
# behaviour change" checkable rather than asserted. AWS and GCP were then added as
# backends without a branch through the middle of `provision_seats`.
#
# Three things the three backends cannot share, found by reading the SDKs rather than
# assumed, and the reason this is an interface rather than a few `if`s:
#
#   * **The pool tag key is not portable.** ``dashboard:desktop_pool`` is a fine Azure tag
#     and a fine AWS tag. It is an INVALID GCP label key — GCP allows lowercase letters,
#     digits, ``-`` and ``_`` only. ``cost_service._GCP_LABEL_KEYS`` already carries both
#     hyphen and underscore forms of ``managed-by`` for exactly this reason, so the key
#     belongs per backend rather than as one module constant.
#   * **Windows is a different mechanism on each cloud.** Azure generates and vaults a
#     password; AWS returns encrypted password data decrypted with the launch key pair;
#     GCP uses ``windows-keys`` metadata. A backend declares whether it can do Windows at
#     all, so a cloud that cannot refuses the pool instead of handing somebody a seat they
#     cannot sign into.
#   * **The deploy signatures do not line up.** Azure takes ``rg``/``vm_size``/``subnet_id``,
#     AWS ``region``/``instance_type``/``security_group_ids``, GCP
#     ``project_id``/``zone``/``machine_type``. Only the RESULT is common, so that is what
#     the interface fixes: ``{"vm_id", "private_ip"}``.


class _AzureSeats:
    """Azure seat provisioning. Extracted verbatim from `provision_seats`."""

    cloud = "azure"
    gateway_cloud = "azure"          # what `ensure_jumpoint_host` is keyed on
    pool_tag_key = POOL_TAG
    supports_windows = True
    default_username = "azureuser"
    pra_tag = "Azure VDI"
    # Config keys for this cloud's PRA targets. "" means "no cloud-specific key —
    # go straight to bt_*"; see `_AwsSeats` for why that is a real answer.
    pra_jump_group_key = "azure_bt_jump_group_name"
    pra_jumpoint_key = "azure_jumpoint_name"
    # Credential injection is Windows-only, and Windows is Azure-only, so this is the
    # one backend with a vault group. The Linux/Shell-Jump path never reads it.
    pra_vault_group_key = "azure_desktops_vault_account_group_id"
    region_spec_key = "location"     # which spec key `gateway_region` reads

    @staticmethod
    def gateway_region(spec: dict) -> str:
        """The region to warm a Gateway in. Azure's spec calls it ``location``."""
        return spec.get("location") or _cfg("azure_location")

    @staticmethod
    def validate_spec(spec: dict) -> dict:
        spec = dict(spec or {})
        missing = [k for k in _AZURE_REQUIRED if not spec.get(k)]
        # Windows seats authenticate with generated per-seat passwords, not SSH keys.
        if (spec.get("os_type") or "Linux").lower() != "windows" and not spec.get("ssh_public_key"):
            missing.append("ssh_public_key")
        if missing:
            raise VDesktopError(f"Azure pool requires: {', '.join(missing)}.")
        has_image = spec.get("image_id") or (
            spec.get("image_publisher") and spec.get("image_offer") and spec.get("image_sku"))
        if not has_image:
            raise VDesktopError("Azure pool requires image_id or a marketplace image (publisher/offer/sku).")
        return spec

    @staticmethod
    def generate_password() -> str:
        from . import azure_service
        return azure_service.generate_windows_admin_password()

    @staticmethod
    async def store_password(vm_name: str, seat_id: str, password: str):
        import asyncio
        from . import azure_service
        return await asyncio.to_thread(
            azure_service.store_windows_admin_password, vm_name, seat_id[:8], password)

    @staticmethod
    async def deploy(spec: dict, vm_name: str, admin_password: str, pool_name: str) -> dict:
        """``{"vm_id", "private_ip"}``. The only shape the orchestration depends on.

        ``pool_name`` is unused here — Azure stamps its pool tag through `tag_pool`
        after the VM exists. GCP needs it at launch, which is why it is in the
        signature at all."""
        from . import azure_service
        return await azure_service.deploy_vm(
            rg=spec["resource_group"], location=spec["location"], vm_name=vm_name,
            vm_size=spec["vm_size"], image_id=spec.get("image_id", "") or "",
            subnet_id=spec["subnet_id"], nsg_ids=spec.get("nsg_ids") or [],
            create_public_ip=bool(spec.get("create_public_ip", False)),
            ssh_username=spec.get("ssh_username") or "azureuser",
            ssh_public_key=spec.get("ssh_public_key") or "",
            image_publisher=spec.get("image_publisher"), image_offer=spec.get("image_offer"),
            image_sku=spec.get("image_sku"), image_version=spec.get("image_version"),
            os_type=spec.get("os_type") or "Linux",
            admin_password=admin_password,
            trusted_launch=bool(spec.get("trusted_launch")),
        )

    @staticmethod
    async def tag_pool(spec: dict, vm_name: str, vm_resource_id: str, pool_name: str) -> None:
        # Azure addresses a VM by resource group + name, so the id is unused here. AWS
        # addresses one by instance id, which is why the id is in the signature at all.
        from . import azure_service
        await azure_service.set_desktop_pool_tag(spec["resource_group"], vm_name, pool_name)

    @staticmethod
    async def terminate(vm_resource_id: str) -> str:
        """Terminate and return the VM's display name for the error message."""
        from . import azure_service
        rg, name = _parse_vm_id(vm_resource_id)
        if rg and name:
            await azure_service.terminate_vm(rg, name)
        return name or vm_resource_id

    @staticmethod
    async def reap_idle_gateway(db) -> None:
        from . import jumpoint_host_service
        await jumpoint_host_service.teardown_jumpoint_host_if_idle(
            db, "azure", _cfg("azure_location"))


class _AwsSeats:
    """EC2 seat provisioning. **Linux only** — see ``supports_windows``."""

    cloud = "aws"
    gateway_cloud = "aws"
    # A colon is a legal character in an EC2 tag key, so the shared constant is usable
    # here unchanged. It is NOT legal in a GCP label key, which is why this is per
    # backend rather than read from the module.
    pool_tag_key = POOL_TAG
    # AWS hands back Windows credentials as password data encrypted to the launch key
    # pair, decrypted client-side — nothing like Azure's "generate one and vault it".
    # Wiring that properly is its own change, so a Windows pool is REFUSED here rather
    # than provisioned into a seat nobody can sign into. `validate_spec` says so.
    supports_windows = False
    default_username = "ec2-user"
    pra_tag = "AWS VDI"
    # **Deliberately empty, and not an oversight.** There is no `aws_bt_jump_group_name`
    # / `aws_jumpoint_name` in config: `aws_vm_service` resolves straight from `bt_*`
    # today, so inventing a pair here would land VDI jump items somewhere the AWS Shell
    # Jump path does not. `ot_service._CLOUD_JUMP_GROUP_KEY` made the same call for the
    # same reason. If those settings are ever added, wire them here — a test asserts
    # this stays "" only for as long as `settings` has no such attribute.
    pra_jump_group_key = ""
    pra_jumpoint_key = ""
    # Linux-only, so there is never a password to vault or inject.
    pra_vault_group_key = ""
    region_spec_key = "region"

    @staticmethod
    def gateway_region(spec: dict) -> str:
        return spec.get("region") or _cfg("aws_region")

    @staticmethod
    def validate_spec(spec: dict) -> dict:
        spec = dict(spec or {})
        if (spec.get("os_type") or "Linux").lower() == "windows":
            raise VDesktopError(
                "AWS desktop pools are Linux-only for now: EC2 returns Windows "
                "credentials as password data encrypted to the launch key pair, which "
                "this does not yet decrypt or vault. Use an Azure pool for Windows.")
        missing = [k for k in _AWS_REQUIRED if not spec.get(k)]
        # Linux-only, so a key is always required — there is no password path to fall
        # back to the way Azure's Windows seats have.
        if not spec.get("ssh_public_key"):
            missing.append("ssh_public_key")
        if missing:
            raise VDesktopError(f"AWS pool requires: {', '.join(missing)}.")
        return spec

    @staticmethod
    def generate_password() -> str:                    # pragma: no cover - unreachable
        raise VDesktopError("AWS desktop pools are Linux-only.")

    @staticmethod
    async def store_password(vm_name, seat_id, password):  # pragma: no cover - unreachable
        raise VDesktopError("AWS desktop pools are Linux-only.")

    @staticmethod
    async def deploy(spec: dict, vm_name: str, admin_password: str, pool_name: str) -> dict:
        # `pool_name` unused: EC2 takes no general tags at launch, so the pool tag is
        # applied by `tag_pool` afterwards.
        from . import aws_service
        res = await aws_service.launch_instance(
            region=spec["region"], ami_id=spec["ami_id"], instance_name=vm_name,
            instance_type=spec["instance_type"],
            public_key=spec.get("ssh_public_key") or "",
            subnet_id=spec["subnet_id"],
            security_group_ids=spec.get("security_group_ids") or [],
            iam_instance_profile=spec.get("iam_instance_profile") or "",
            os_type=spec.get("os_type") or "Linux",
            # No workgroup: desktop seats are listed from `virtual_desktops`, never
            # from `ec2_deploy` jobs, so the tag has no reader and no builder writes
            # the key. A `spec.get()` for a key nothing sets is the drift this
            # module keeps getting caught by.
            workgroup="",
        )
        # `vm_resource_id` carries the REGION as well as the instance id, because
        # teardown gets only this string — it has no spec to read a region from, and an
        # instance id alone does not say which regional endpoint owns it. Azure's ARM id
        # embeds its resource group for exactly the same reason.
        return {"vm_id": f"{spec['region']}/{res['instance_id']}",
                "private_ip": res.get("private_ip")}

    @staticmethod
    def _split(vm_resource_id: str):
        """``region/i-abc`` → ``("region", "i-abc")``."""
        raw = (vm_resource_id or "").strip()
        region, _, instance_id = raw.partition("/")
        return (region, instance_id) if instance_id else ("", raw)

    @staticmethod
    async def tag_pool(spec: dict, vm_name: str, vm_resource_id: str, pool_name: str) -> None:
        from . import aws_service
        region, instance_id = _AwsSeats._split(vm_resource_id)
        if instance_id:
            await aws_service.set_desktop_pool_tag(
                region or spec.get("region", ""), instance_id, pool_name)

    @staticmethod
    async def terminate(vm_resource_id: str) -> str:
        from . import aws_service
        region, instance_id = _AwsSeats._split(vm_resource_id)
        if region and instance_id:
            await aws_service.terminate_instance(region, instance_id)
        return instance_id or vm_resource_id

    @staticmethod
    async def reap_idle_gateway(db) -> None:
        from . import jumpoint_host_service
        await jumpoint_host_service.teardown_jumpoint_host_if_idle(
            db, "aws", _cfg("aws_region"))


class _GcpSeats:
    """GCE seat provisioning. **Linux only** — see ``supports_windows``."""

    cloud = "gcp"
    gateway_cloud = "gcp"
    # NOT ``POOL_TAG``. A colon is legal in an Azure tag key and an EC2 tag key and is
    # ILLEGAL in a GCP label key, which allows a leading lowercase letter followed by
    # lowercase letters, digits, ``-`` and ``_`` only. Inheriting the shared constant
    # here would have produced instances the API rejects — and it is the kind of thing
    # that reads fine in review, which is why `test_the_pool_tag_key_is_legal_for_its_cloud`
    # exists. `cost_service._GCP_LABEL_KEYS` carries both forms of `managed-by` for the
    # same reason.
    pool_tag_key = "dashboard_desktop_pool"
    # GCE delivers a Windows password through `windows-keys` instance metadata and an
    # RSA exchange — a third mechanism again, after Azure's vaulted password and AWS's
    # key-pair-encrypted password data. Linux only until that is built.
    supports_windows = False
    default_username = "gcpuser"
    pra_tag = "GCP VDI"
    pra_jump_group_key = "gcp_bt_jump_group_name"
    pra_jumpoint_key = "gcp_jumpoint_name"
    pra_vault_group_key = ""
    # A GCP spec commits to a ZONE, but `ensure_jumpoint_host` and
    # `teardown_jumpoint_host_if_idle` are keyed on a REGION. `gateway_region` below is
    # where that conversion happens; handing either one a zone silently mis-places the
    # gateway rather than failing.
    region_spec_key = "zone"

    @staticmethod
    def gateway_region(spec: dict) -> str:
        from . import region_catalog
        return region_catalog.region_from_zone(spec.get("zone") or _cfg("gcp_zone"))

    @staticmethod
    def _label_value(raw: str) -> str:
        """A GCP label VALUE has the same character rules as a key.

        Pool names do not: ``create_pool`` accepts any string. `_vm_name_for` already
        sanitizes for the instance name; the label needs the same treatment or the whole
        launch is rejected over the pool's capitalisation.
        """
        cleaned = re.sub(r"[^a-z0-9_-]", "-", (raw or "").lower()).strip("-_")
        return cleaned[:63] or "pool"

    @staticmethod
    def validate_spec(spec: dict) -> dict:
        spec = dict(spec or {})
        if (spec.get("os_type") or "Linux").lower() == "windows":
            raise VDesktopError(
                "GCP desktop pools are Linux-only for now: GCE delivers Windows "
                "credentials through windows-keys instance metadata, which this does "
                "not yet perform. Use an Azure pool for Windows.")
        missing = [k for k in _GCP_REQUIRED if not spec.get(k)]
        if not spec.get("ssh_public_key"):
            missing.append("ssh_public_key")
        if missing:
            raise VDesktopError(f"GCP pool requires: {', '.join(missing)}.")
        return spec

    @staticmethod
    def generate_password() -> str:                    # pragma: no cover - unreachable
        raise VDesktopError("GCP desktop pools are Linux-only.")

    @staticmethod
    async def store_password(vm_name, seat_id, password):  # pragma: no cover - unreachable
        raise VDesktopError("GCP desktop pools are Linux-only.")

    @staticmethod
    async def deploy(spec: dict, vm_name: str, admin_password: str, pool_name: str) -> dict:
        from . import gcp_service
        res = await gcp_service.launch_instance(
            project_id=spec["project_id"], zone=spec["zone"], instance_name=vm_name,
            machine_type=spec["machine_type"],
            image_self_link=spec["image_self_link"],
            subnetwork=spec["subnetwork"],
            create_external_ip=bool(spec.get("create_external_ip", False)),
            ssh_username=spec.get("ssh_username") or _GcpSeats.default_username,
            ssh_public_key=spec.get("ssh_public_key") or "",
            disk_size_gb=int(spec.get("disk_size_gb") or 20),
            network_tags=spec.get("network_tags") or None,
            # The pool label goes on AT LAUNCH rather than through a follow-up call, and
            # `_launch_instance_sync` merges it over `managed-by` rather than replacing
            # it. That makes GCP the only one of the three where a crash between create
            # and tag cannot leave an unattributable seat — so `tag_pool` below has
            # nothing left to do.
            labels={_GcpSeats.pool_tag_key: _GcpSeats._label_value(pool_name)},
        )
        # project/zone/name: terminate needs all three and gets only this string.
        return {"vm_id": f"{spec['project_id']}/{spec['zone']}/{res['instance_name']}",
                "private_ip": res.get("private_ip")}

    @staticmethod
    def _split(vm_resource_id: str):
        """``project/zone/name`` → the three parts, or ``("", "", raw)``."""
        parts = (vm_resource_id or "").strip("/").split("/")
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
        return "", "", (parts[-1] if parts else "")

    @staticmethod
    async def tag_pool(spec: dict, vm_name: str, vm_resource_id: str, pool_name: str) -> None:
        """No-op: the label was applied at launch. See `deploy`."""
        return None

    @staticmethod
    async def terminate(vm_resource_id: str) -> str:
        from . import gcp_service
        project, zone, name = _GcpSeats._split(vm_resource_id)
        if project and zone and name:
            await gcp_service.terminate_instance(project, zone, name)
        return name or vm_resource_id

    @staticmethod
    async def reap_idle_gateway(db) -> None:
        # REGION, not zone: `teardown_jumpoint_host_if_idle` passes this to
        # `_gcp_jumpoint_zone`, whose `zone_in_region(override, region)` check is False
        # for a zone-shaped argument — so a raw `gcp_zone` here quietly reaped against
        # the wrong placement instead of erroring.
        from . import jumpoint_host_service
        await jumpoint_host_service.teardown_jumpoint_host_if_idle(
            db, "gcp", _GcpSeats.gateway_region({}))


# Keyed by cloud. All three of VALID_CLOUDS are implemented; a cloud listed there and
# absent here would create seat RECORDS ONLY, which is why PROVISIONING_CLOUDS is
# derived from this rather than maintained beside it: the two cannot drift. What the
# derivation does NOT reach is the create form — see tests/test_vdesktop_form_clouds.py.
_SEAT_BACKENDS = {
    "azure": _AzureSeats,
    "aws": _AwsSeats,
    "gcp": _GcpSeats,
}


PROVISIONING_CLOUDS = tuple(c for c in VALID_CLOUDS if c in _SEAT_BACKENDS)


def seat_backend(cloud: str):
    """The backend for this cloud, or None for a cloud with no backend.

    None is the unknown-cloud answer, not a records-only one: every cloud in
    ``VALID_CLOUDS`` is implemented. ``VirtualDesktop.cloud`` is an unconstrained
    ``String(20)``, so a row can still name something else.
    """
    return _SEAT_BACKENDS.get((cloud or "").lower())


# ── Create / scale / delete (sync DB part; API schedules the async cloud work) ──

def create_pool(db: Session, *, cloud: str, name: str, count: int, created_by: str,
                spec: dict = None) -> dict:
    """Create a pool: validate the deploy spec against the cloud's seat backend and
    enqueue a ``vdesktop_pool_provision`` job whose metadata carries everything the
    worker handler needs (``pool_name`` + ``seat_ids`` + ``spec``) — so the job is
    self-contained from creation and the job runner can claim it without racing a
    follow-up metadata write.

    A cloud with no seat backend creates pending rows only. That is the unknown-cloud
    path; all of ``VALID_CLOUDS`` is implemented."""
    name = (name or "").strip()
    if cloud not in VALID_CLOUDS:
        raise VDesktopError(f"Unknown cloud '{cloud}'. Valid: {', '.join(VALID_CLOUDS)}.")
    if not name:
        raise VDesktopError("pool name is required.")
    if count < 1:
        raise VDesktopError("count must be >= 1.")
    if get_pool(db, name):
        raise VDesktopError(f"Pool '{name}' already exists.")

    backend = seat_backend(cloud)
    provision = backend is not None
    if provision:
        spec = backend.validate_spec(spec)

    # Generate the seat ids first so the provision job's metadata can name them.
    seat_ids = []
    for _ in range(count):
        sid = str(uuid.uuid4())
        db.add(VirtualDesktop(id=sid, cloud=cloud, pool_name=name, kind="vm_pool",
                              status="pending", created_by=created_by))
        seat_ids.append(sid)

    job_id = None
    if provision:
        from . import job_service
        job = job_service.create_job(
            db, job_type="vdesktop_pool_provision", created_by=created_by,
            metadata={"pool_name": name, "cloud": cloud, "count": count,
                      "spec": spec, "seat_ids": seat_ids},
        )
        job_id = job.id

    db.commit()
    logger.info("Created desktop pool %s (%s x%d)%s", name, cloud, count,
                " — provisioning" if provision else " — no backend, records only")
    return {
        "pool_name": name, "cloud": cloud, "count": count, "seats": get_pool(db, name),
        "job_id": job_id,
        "to_provision": seat_ids if provision else [],
        "spec": spec if provision else None,
    }


def scale_pool(db: Session, name: str, count: int) -> dict:
    """Resize a pool to ``count`` seats: returns ids to provision (up) or tear down
    (down); the caller schedules the cloud work."""
    if count < 0:
        raise VDesktopError("count must be >= 0.")
    seats = (db.query(VirtualDesktop).filter(VirtualDesktop.pool_name == name)
             .order_by(VirtualDesktop.created_at).all())
    if not seats:
        raise VDesktopError(f"Pool '{name}' not found.")
    cloud = seats[0].cloud
    cur = len(seats)
    out = {"pool_name": name, "count": cur, "to_provision": [], "to_teardown": [], "spec": None}

    if count > cur:
        spec = None
        if cloud in PROVISIONING_CLOUDS:
            spec, _ = _pool_spec(db, name)
            if not spec:
                raise VDesktopError("Pool has no stored deploy spec; cannot scale up.")
        new_ids = []
        for _ in range(count - cur):
            sid = str(uuid.uuid4())
            db.add(VirtualDesktop(id=sid, cloud=cloud, pool_name=name, kind=seats[0].kind,
                                  status="pending", created_by=seats[0].created_by))
            new_ids.append(sid)
        db.commit()
        if cloud in PROVISIONING_CLOUDS:
            out["to_provision"] = new_ids
            out["spec"] = spec
    elif count < cur:
        # Shrink: drop the newest seats. For Azure, terminate them first.
        removable = list(reversed(seats))[: cur - count]
        if cloud in PROVISIONING_CLOUDS:
            for s in removable:
                s.status = "deprovisioning"
            db.commit()
            out["to_teardown"] = [s.id for s in removable]
        else:
            for s in removable:
                db.delete(s)
            db.commit()
    out["count"] = len(get_pool(db, name))
    return out


def delete_pool(db: Session, name: str) -> dict:
    """Delete a pool: mark seats deprovisioning + return ids to tear down (the caller
    schedules teardown, which terminates the VMs then drops the rows).

    A pool whose cloud has no backend has no VMs, so its rows drop immediately. That
    is the unknown-cloud path, not an AWS/GCP one — all three provision."""
    seats = db.query(VirtualDesktop).filter(VirtualDesktop.pool_name == name).all()
    n = len(seats)
    if n == 0:
        return {"deleted_seats": 0, "to_teardown": []}
    cloud = seats[0].cloud
    if cloud in PROVISIONING_CLOUDS:
        for s in seats:
            s.status = "deprovisioning"
        db.commit()
        return {"deleted_seats": n, "to_teardown": [s.id for s in seats]}
    for s in seats:
        db.delete(s)
    db.commit()
    logger.info("Deleted desktop pool %s (%d records)", name, n)
    return {"deleted_seats": n, "to_teardown": []}


# ── PRA brokering helpers ────────────────────────────────────────────────────

def _cfg(key: str, fallback: str = "") -> str:
    from ..config import settings
    from . import config_service
    return config_service.get(key) or getattr(settings, key, fallback)


def _pra_configured() -> bool:
    """True when the PRA API creds are present (mirror cloud_database_service)."""
    return bool(_cfg("bt_api_host") and _cfg("bt_client_id") and _cfg("bt_client_secret"))


def _resolve_pra_targets(spec: dict, backend) -> dict:
    """Jump Group / Gateway / Vault account-group for a pool's jump items.

    Resolves ``spec override -> the backend's cloud-specific key -> the shared bt_*``,
    the same chain each cloud's own Shell Jump uses in ``*_vm_service``. A backend
    whose key is ``""`` has no cloud-specific setting and goes straight to ``bt_*``
    — see ``_AwsSeats``, where that is the correct answer and not a gap.

    The ``spec`` overrides are read but not yet emitted by any spec builder; they
    cost nothing and are where a per-pool override would land.
    """
    def _key(name: str) -> str:
        k = getattr(backend, name, "") if backend is not None else ""
        return _cfg(k) if k else ""

    jump_group = ((spec.get("jump_group") or "").strip()
                  or _key("pra_jump_group_key") or _cfg("bt_jump_group_name"))
    jumpoint = ((spec.get("jumpoint_name") or "").strip()
                or _key("pra_jumpoint_key") or _cfg("bt_jumpoint_name"))
    raw_group = str(spec.get("vault_account_group_id")
                    or _key("pra_vault_group_key") or "").strip()
    try:
        vault_group_id = int(raw_group) if raw_group else None
    except ValueError:
        vault_group_id = None
    return {"jump_group": jump_group, "jumpoint": jumpoint, "vault_group_id": vault_group_id}


def session_info(db: Session, seat_id: str) -> dict | None:
    """PRA connection info for one seat, or None when the seat is gone.

    Lives here rather than in the router so it is testable without importing
    FastAPI, and so the per-cloud target resolution is the SAME function the
    provisioner used — the endpoint used to hard-code the ``azure_*`` keys, which
    named the wrong Jump Group for an AWS or GCP seat.
    """
    seat = get_seat(db, seat_id)
    if seat is None:
        return None
    vm_name = (seat.get("vm_resource_id") or "").split("/")[-1]
    if not seat.get("pra_jump_id"):
        return {
            "brokered": False, "vm_name": vm_name,
            "note": ("Not brokered — PRA registration is pending or failed, or this seat "
                     "predates brokering for its OS. Confirm PRA is configured, or "
                     "recreate the seat."),
        }
    backend = seat_backend(seat.get("cloud"))
    tgt = _resolve_pra_targets({}, backend)
    kind = seat.get("jump_kind") or "remote_rdp"
    # The username actually deployed, read off the pool's stored spec rather than
    # guessed: a pool created before the per-cloud default was fixed really does log
    # in as whatever it was given.
    spec = _pool_spec(db, seat.get("pool_name") or "")[0] or {}
    username = (spec.get("ssh_username")
                or (backend.default_username if backend is not None else ""))
    if kind == "shell_jump":
        note = (f"Open the auto-registered Shell Jump (SSH, port 22) from your PRA "
                f"representative console, as '{username}'. Linux seats authenticate "
                f"with the SSH key the pool was created with — there is no admin "
                f"password, and nothing is injected from the PRA Vault.")
    else:
        note = ("Open the auto-registered Remote RDP Jump Item from your PRA "
                "representative console. Credentials inject from the PRA Vault when "
                "provisioned; otherwise use the seat's admin password "
                "(Azure → VMs → Password).")
    host = _cfg("bt_api_host")
    return {
        "brokered": True,
        "vm_name": vm_name,
        "cloud": seat.get("cloud"),
        "username": username,
        "pra": {
            "jump_id": seat.get("pra_jump_id"),
            "kind": kind,
            "jump_group": tgt["jump_group"],
            "jumpoint": tgt["jumpoint"],
        },
        "console_url": f"https://{host}/login" if host else "",
        "note": note,
    }


# ── Async cloud work (scheduled by the API as background tasks) ─────────────────

async def provision_seats(pool_name: str, job_id: str, seat_ids: list, spec: dict) -> None:
    """Provision one VM per seat through the pool's cloud backend (private), fill
    ``vm_resource_id`` + ``running``, and stamp the backend's pool tag.

    Everything here is cloud-NEUTRAL: the job lifecycle, the progress reporting, the
    gateway warm-up that must happen before any seat registers, the per-seat error
    collection that keeps one bad seat from aborting the pool, and the PRA brokering.
    The cloud calls themselves are the backend's — see ``_SEAT_BACKENDS``.

    Windows pools get a generated per-seat admin password, vaulted via the
    secrets backend before the seat's VM is created; the (backend, ref) pairs
    are merged into the pool provision job's ``seat_passwords`` map so
    ``GET /api/azure/vms/{name}/admin-password`` can resolve them. The spec
    itself stays credential-free (it is persisted in job metadata for
    scale-up; see ``_pool_spec``)."""
    from ..database import SessionLocal
    from . import job_service
    db = SessionLocal()
    # The cloud comes from the seats themselves rather than a new parameter: every seat
    # in a pool shares one, `jobs_worker` calls this with the metadata it was given at
    # create time, and adding an argument would strand jobs enqueued by a running
    # deployment before the upgrade.
    cloud = _pool_cloud(db, seat_ids)
    backend = seat_backend(cloud)
    if backend is None:
        # Unknown cloud. Every cloud in VALID_CLOUDS has a backend, so reaching here
        # means either the pool's rows were all deleted between enqueue and claim
        # (`_pool_cloud` returns "") or a row names a cloud that no longer exists.
        # Worth a log line rather than an AttributeError inside the job worker.
        logger.warning("desktop pool %s: no seat backend for cloud %r; nothing provisioned",
                       pool_name, cloud)
        db.close()
        return
    is_windows = (spec.get("os_type") or "Linux").lower() == "windows"
    if is_windows and not backend.supports_windows:
        # `validate_spec` refuses this at create time, so reaching here means a spec
        # stored before a backend's Windows support changed. Fail loudly rather than
        # call a `generate_password` that raises halfway through the pool.
        logger.warning("desktop pool %s: %s seats cannot be Windows; refusing",
                       pool_name, cloud)
        if job_id:
            job_service.set_failed(
                db, job_id, f"{cloud} desktop pools are Linux-only.")
        db.close()
        return
    seat_passwords: dict = {}
    errors: list = []
    try:
        if job_id:
            job_service.set_running(db, job_id)
        # Bring the shared PRA Gateway node online BEFORE the seats register their
        # jump items — otherwise the items register but read "Unavailable" (no
        # Gateway to broker them). Idempotent find-or-create + best-effort, mirroring
        # clouddb/k8s (jumpoint_host_service). It warms in parallel with the seats' VM
        # creates (usually reused -> instant; ~1-2 min only the first time), so it is
        # typically online by the first registration.
        #
        # NOT gated on Windows any more: a Linux seat's Shell Jump needs a Gateway
        # exactly as much as a Windows seat's RDP jump, and this gate was half of why
        # no AWS or GCP seat was ever brokered.
        #
        # The region comes from the BACKEND rather than ``spec["location"]`` — that
        # key exists only on an Azure spec, so the other two were warming against a
        # blank region. The gateway lands in that region's gateway subnet; a seat in a
        # different region needs the networks peered (per-region gateways TODO).
        if _pra_configured():
            if job_id:
                job_service.update_progress(db, job_id, 0, "Ensuring PRA Gateway host is online…")
            try:
                from . import jumpoint_host_service
                await jumpoint_host_service.ensure_jumpoint_host(
                    backend.gateway_cloud, backend.gateway_region(spec))
            except Exception as jp_err:
                logger.warning("desktop pool %s: ensure gateway host failed (non-fatal): %s",
                               pool_name, jp_err)
        ok = 0
        for sid in seat_ids:
            row = db.query(VirtualDesktop).filter(VirtualDesktop.id == sid).first()
            if row is None:
                continue
            vm_name = _vm_name_for(pool_name, sid)
            try:
                admin_password = ""
                if is_windows:
                    # Vaulted BEFORE the VM exists, so the password survives a failure
                    # anywhere after this point. `pw_backend` is the SECRETS backend
                    # name, not the seat backend — they were both called `backend`
                    # before this refactor, in the same scope.
                    admin_password = backend.generate_password()
                    pw_backend, ref = await backend.store_password(vm_name, sid, admin_password)
                res = await backend.deploy(spec, vm_name, admin_password, pool_name)
                row.vm_resource_id = res.get("vm_id") or vm_name
                row.status = "running"
                db.commit()
                private_ip = res.get("private_ip")
                # Broker the seat over PRA. Best-effort: a running seat with no jump
                # item is debuggable; never fail the seat over brokering.
                #
                # The KIND of jump follows the guest OS, not the cloud. Windows gets a
                # Remote RDP item with the generated password vaulted for injection;
                # Linux gets a Shell Jump on 22. Until this branch existed the whole
                # block was gated on ``is_windows``, so every Linux seat — which is
                # every AWS and GCP seat — provisioned a VM and was never brokered.
                if private_ip and not row.pra_jump_id and _pra_configured():
                    try:
                        from . import terraform_pra_service as pra, config_service
                        tgt = _resolve_pra_targets(spec, backend)
                        cred_ref = spec.get("pra_credential_ref")
                        client_secret = config_service.resolve_reference(cred_ref) if cred_ref else ""
                        if is_windows:
                            jump = await pra.provision_rdp_jump(
                                name=vm_name, hostname=private_ip,
                                jump_group_name=tgt["jump_group"], jumpoint_name=tgt["jumpoint"],
                                rdp_username=spec.get("ssh_username") or backend.default_username,
                                tag=backend.pra_tag,
                                admin_password=admin_password,
                                vault_account_name=f"{vm_name}-admin",
                                vault_account_group_id=tgt["vault_group_id"],
                                client_secret=client_secret,
                            )
                            jump_id = jump.get("rdp_jump_id")
                            state = jump.get("tf_state_json")
                        else:
                            # A Linux seat authenticates with an SSH KEY. There is no
                            # password to vault, the dashboard never holds the private
                            # half, and this provider wrapper has no SSH-key vault
                            # resource (only sra_vault_username_password_account) — so
                            # the item registers with NO credential injection and the
                            # rep supplies the key in PRA. ``tgt["vault_group_id"]`` is
                            # deliberately unused here rather than passed as None.
                            jump = await pra.provision_jump(
                                vm_name=vm_name, hostname=private_ip,
                                jump_group_name=tgt["jump_group"], jumpoint_name=tgt["jumpoint"],
                                port=22, tag=backend.pra_tag,
                                client_secret=client_secret,
                            )
                            jump_id = jump.get("shell_jump_id")
                            # ``provision_jump`` does not scrub its own state the way
                            # the RDP path does. A shell jump holds no secret attribute,
                            # so this is a no-op on success; it is here because the
                            # column's contract says scrubbed. It fails CLOSED to None,
                            # which only happens on unparseable state — state the
                            # destroy could not have used either way.
                            state = pra._scrub_tf_state(jump.get("tf_state_json") or "")
                        # ``or None`` is load-bearing: both provisioners return "" when the
                        # Terraform output is missing. A non-NULL "" makes
                        # ``_active_vdesktop_count`` (pra_jump_id.isnot(None)) pin the shared
                        # gateway forever while the UI still greys out Open session — two
                        # symptoms and no cause.
                        row.pra_jump_id = jump_id or None
                        row.pra_tunnel_state = state
                        db.commit()
                    except Exception as pra_err:
                        logger.warning("desktop seat PRA jump registration failed pool=%s seat=%s: %s",
                                       pool_name, sid, pra_err)
                if is_windows:
                    seat_passwords[vm_name] = {
                        "backend": pw_backend, "ref": ref,
                        "username": spec.get("ssh_username") or backend.default_username,
                    }
                try:
                    await backend.tag_pool(spec, vm_name, row.vm_resource_id, pool_name)
                except Exception as tag_err:
                    logger.warning("desktop pool tag failed vm=%s: %s", vm_name, tag_err)
                ok += 1
                if job_id:
                    job_service.update_progress(
                        db, job_id, int((ok + len(errors)) * 100 / max(len(seat_ids), 1)),
                        f"Provisioned {vm_name} ({ok}/{len(seat_ids)})")
            except Exception as exc:
                row.status = "failed"
                db.commit()
                errors.append(f"{vm_name}: {exc}")
                logger.warning("desktop seat provision failed pool=%s seat=%s: %s", pool_name, sid, exc)
                if job_id:
                    job_service.update_progress(
                        db, job_id, int((ok + len(errors)) * 100 / max(len(seat_ids), 1)),
                        f"Seat {vm_name} failed: {exc}")
        if seat_passwords:
            # Merge into the pool's provision job (scale-ups run with job_id=None,
            # so fall back to the create-time job that _pool_spec resolves).
            from ..database import Job
            pool_job_id = job_id or _pool_spec(db, pool_name)[1]
            j = db.get(Job, pool_job_id) if pool_job_id else None
            if j is not None:
                md = j.metadata_dict
                merged = dict(md.get("seat_passwords") or {})
                merged.update(seat_passwords)
                md["seat_passwords"] = merged
                j.metadata_dict = md
                db.commit()
        if job_id:
            total = len(seat_ids)
            if ok == total:
                job_service.set_completed(db, job_id, {"provisioned": ok, "requested": total})
            else:
                # A provision where seats failed must read as FAILED on the Jobs
                # screen, with the reason — not a green job whose failure only shows
                # on the Desktops seat rows.
                detail = "; ".join(errors[:3]) if errors else "see per-seat status on the Desktops page"
                job_service.set_failed(
                    db, job_id, f"Provisioned {ok}/{total} seat(s). {detail}")
        logger.info("desktop pool %s provisioned %d/%d seats", pool_name, ok, len(seat_ids))
    finally:
        db.close()


async def teardown_seats(seat_ids: list, job_id: str = None) -> None:
    """Terminate the VM behind each seat through its cloud's backend (best-effort),
    then drop the row. A seat whose cloud has no backend has no VM and is just dropped.

    Runs on the durable worker (scale-down / pool-delete schedule a
    ``vdesktop_pool_teardown`` job). When given the claiming ``job_id`` it owns
    that job's running/completed lifecycle so the worker's claim doesn't leak as
    ``running``; called with no ``job_id`` it just does the work (back-compat)."""
    from ..database import SessionLocal
    from . import job_service
    db = SessionLocal()
    try:
        if job_id:
            job_service.set_running(db, job_id)
        dropped = 0
        errors: list = []
        # Remembered for the gateway reap below, which runs AFTER the rows are gone and
        # so cannot read the cloud back off them.
        last_cloud = ""
        for sid in seat_ids:
            row = db.query(VirtualDesktop).filter(VirtualDesktop.id == sid).first()
            if row is None:
                continue
            last_cloud = row.cloud or last_cloud
            # A seat whose cloud has no backend has no VM to terminate — the same
            # seats this code has always skipped, now skipped because there is no
            # backend rather than because the string was not "azure".
            backend = seat_backend(row.cloud)
            if backend is not None and row.vm_resource_id:
                try:
                    name = await backend.terminate(row.vm_resource_id)
                except Exception as exc:
                    name = row.vm_resource_id
                    errors.append(f"{name}: terminate failed: {exc}")
                    logger.warning("desktop seat terminate failed seat=%s vm=%s: %s", sid, name, exc)
            # Remove the seat's PRA jump item (+ vault account for a Windows seat).
            # `remove_rdp_jump` is a misnomer: it delegates to `_destroy_state_only_sync`,
            # which writes a provider-only config and destroys whatever `sra_*` resource
            # the state holds — so it is correct for a Shell Jump too, and NOT
            # `remove_jump`, which regenerates HCL and re-reads `bt_jump_group_name`
            # (the wrong Jump Group for a GCP seat).
            if row.pra_tunnel_state:
                try:
                    from . import terraform_pra_service as pra
                    await pra.remove_rdp_jump(row.pra_tunnel_state)
                except Exception as exc:
                    errors.append(f"{sid[:8]}: PRA jump removal failed: {exc}")
                    logger.warning("desktop seat PRA jump removal failed seat=%s: %s", sid, exc)
            db.delete(row)
            db.commit()
            dropped += 1
        # If no brokered Azure resource is left using the shared PRA Gateway, reap
        # it (ref-counted — now counts remaining VDI seats too). The torn-down rows
        # were deleted above, so the count reflects what remains. Best-effort.
        try:
            reaper = seat_backend(last_cloud) if last_cloud else None
            if reaper is not None:
                await reaper.reap_idle_gateway(db)
        except Exception as jp_err:
            logger.warning("desktop teardown: idle gateway reap failed (non-fatal): %s", jp_err)
        if job_id:
            if errors:
                # Rows are dropped regardless, but a failed terminate can leave an
                # Azure VM behind — surface that as a failed job, not a silent green.
                job_service.set_failed(
                    db, job_id,
                    f"Tore down {dropped}/{len(seat_ids)} seat(s) with cleanup errors: {'; '.join(errors[:3])}")
            else:
                job_service.set_completed(db, job_id, {"torn_down": dropped, "requested": len(seat_ids)})
    finally:
        db.close()


async def drop_seat_by_vm(db: Session, vm_name: str) -> bool:
    """Clean up the desktop-seat row whose backing VM is being destroyed elsewhere
    (e.g. via the Azure tab's Destroy button, which terminates the VM directly).

    Matches a ``virtual_desktops`` row whose ``vm_resource_id`` equals ``vm_name``
    or ends with ``/{vm_name}`` (a full ARM id). Removes the seat's PRA RDP jump
    first (best-effort, state-driven) so it isn't stranded, then deletes the row.
    Does NOT terminate the VM — the caller already does that. Returns True if a
    seat row matched and was dropped."""
    if not vm_name:
        return False
    suffix = "/" + vm_name
    row = (
        db.query(VirtualDesktop)
        .filter(
            (VirtualDesktop.vm_resource_id == vm_name)
            | VirtualDesktop.vm_resource_id.like("%" + suffix)
        )
        .first()
    )
    if row is None:
        return False
    if row.pra_tunnel_state:
        try:
            from . import terraform_pra_service as pra
            await pra.remove_rdp_jump(row.pra_tunnel_state)
        except Exception as exc:
            logger.warning("desktop seat PRA jump removal failed (Azure-tab destroy) seat=%s vm=%s: %s",
                           row.id, vm_name, exc)
    seat_id = row.id
    db.delete(row)
    db.commit()
    logger.info("dropped desktop seat %s (backing VM %s destroyed via Azure tab)", seat_id, vm_name)
    return True
