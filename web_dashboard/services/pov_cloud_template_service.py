"""Cloud POV templates: the topology a cloud POV environment is built from.

On Skytap a template is something the platform holds and the dashboard picks from. No
public cloud has one, so on a cloud the dashboard holds it — and this module is the CRUD
for that, plus the validation that keeps a template buildable.

Two rules, both of them about failing at the form rather than in the job:

**Everything checkable is checked here.** A VM with no image, a broker VM that is really
two broker VMs, an empty instance type, a CIDR that will not parse — all of them are a
refusal on save. The alternative is a template that saves cleanly and fails eleven
minutes into a provision with half a VPC already built.

**Nothing here talks to a cloud.** Whether an AMI exists, whether the account has quota
for an `m5.2xlarge`, whether the region is enabled — none of that is knowable without
credentials a template outlives, and writing a template before its image is promoted is a
legitimate thing to want. Those failures belong to the provision job, which resolves
images late for exactly this reason (see ``pov_cloud_env.resolve_image``).

The sibling to read alongside this is ``pov_blueprint_service``: a blueprint is a saved
set of *form answers* and names a template. This is the template.
"""
from __future__ import annotations

import ipaddress
import logging
import re

from sqlalchemy.orm import Session

from ..database import PovCloudTemplate, PovCloudTemplateVM
from . import lab_platforms, pov_cloud_env

logger = logging.getLogger(__name__)

# The same shape a POV name and a blueprint name take. A template name reaches the create
# form's dropdown and job output.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")

# A VM name becomes a resource tag and, on Linux, the guest hostname. No dots: a dotted
# name reads as an FQDN to half the tooling downstream and as a label to the other half.
_VM_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

VALID_ROLES = ("target", "broker")
VALID_OS_FAMILIES = ("linux", "windows")

# A ceiling, not a quota. A POV is a demonstration; twenty VMs is well past the point
# where the real limit is the account's vCPU allowance and the customer's attention, and a
# template with a typo'd repeat is otherwise a very expensive save.
MAX_VMS = 20


class CloudTemplateError(Exception):
    """A cloud template could not be saved. The message names the remedy."""


def normalize_name(name: str) -> str:
    value = (name or "").strip().lower()
    if not _NAME_RE.match(value):
        raise CloudTemplateError(
            "name must be 2-63 characters of lowercase letters, digits and hyphens, "
            "starting with a letter or digit")
    return value


def get(db: Session, template_id: str) -> PovCloudTemplate | None:
    return db.query(PovCloudTemplate).filter(
        PovCloudTemplate.id == (template_id or "").strip()).first()


def list_for(db: Session, cloud: str = "") -> list:
    q = db.query(PovCloudTemplate)
    if cloud:
        q = q.filter(PovCloudTemplate.cloud == cloud.strip().lower())
    return q.order_by(PovCloudTemplate.name.asc()).all()


def vms_of(db: Session, template_id: str) -> list:
    return (db.query(PovCloudTemplateVM)
              .filter(PovCloudTemplateVM.template_id == template_id)
              .order_by(PovCloudTemplateVM.sort_order,
                        PovCloudTemplateVM.name).all())


def _validate_cloud(cloud: str) -> str:
    value = (cloud or "").strip().lower()
    if value not in lab_platforms.CLOUD_PLATFORMS:
        raise CloudTemplateError(
            f"{cloud!r} is not a cloud this build can run a POV on; supported today: "
            f"{', '.join(lab_platforms.CLOUD_PLATFORMS)}")
    return value


def _validate_cidr(cidr: str) -> str:
    """Refuse a network that will not build, or is too small to be useful."""
    raw = (cidr or "").strip()
    if not raw:
        return ""
    try:
        net = ipaddress.ip_network(raw, strict=False)
    except ValueError as exc:
        raise CloudTemplateError(f"{raw!r} is not a valid network: {exc}") from None
    if net.version != 4:
        raise CloudTemplateError(
            "the POV network must be IPv4; the guests, the Gateway and the Password Safe "
            "managed systems are all addressed by their IPv4 private address")
    if not net.is_private:
        raise CloudTemplateError(
            f"{net} is a public range. A POV network must be private — each VM gets its "
            f"own public address for egress, and nothing listens on it.")
    if net.prefixlen > 24:
        raise CloudTemplateError(
            f"{net} leaves room for {net.num_addresses} addresses; use /24 or larger")
    return str(net)


def _validate_vms(vms: list) -> list:
    """Normalise and refuse the VM list. Returns clean dicts, in order.

    The broker check is the one worth reading: at most one VM may carry the role. It is
    where the dashboard agent, the Gateway and the Resource Broker all land, and two of
    them would mean two agents enrolled for one POV each holding half the wire-up — which
    presents as an intermittently broken POV rather than as a bad template.
    """
    if not vms:
        raise CloudTemplateError(
            "a template needs at least one VM; a POV with nothing in it has nothing to "
            "demonstrate")
    if len(vms) > MAX_VMS:
        raise CloudTemplateError(
            f"{len(vms)} VMs is past the {MAX_VMS} this supports in one POV")

    clean, seen, brokers = [], set(), 0
    for index, raw in enumerate(vms):
        name = (raw.get("name") or "").strip().lower()
        if not _VM_NAME_RE.match(name):
            raise CloudTemplateError(
                f"VM name {raw.get('name')!r} must be 1-63 characters of lowercase "
                f"letters, digits and hyphens, starting and ending with one")
        if name in seen:
            raise CloudTemplateError(
                f"two VMs are both called {name!r}; names become resource tags and guest "
                f"hostnames, so they have to differ")
        seen.add(name)

        role = (raw.get("role") or "target").strip().lower()
        if role not in VALID_ROLES:
            raise CloudTemplateError(
                f"{role!r} is not a VM role; use {' or '.join(VALID_ROLES)}")
        if role == "broker":
            brokers += 1

        os_family = (raw.get("os_family") or "linux").strip().lower()
        if os_family not in VALID_OS_FAMILIES:
            raise CloudTemplateError(
                f"{os_family!r} is not an OS family; use "
                f"{' or '.join(VALID_OS_FAMILIES)}")

        image_ref = (raw.get("image_ref") or "").strip()
        image_id = (raw.get("image_id") or "").strip()
        if bool(image_ref) == bool(image_id):
            which = "both" if image_ref else "neither"
            raise CloudTemplateError(
                f"VM {name!r} needs exactly one of a catalog image or a literal image "
                f"id, and has {which}")

        instance_type = (raw.get("instance_type") or "").strip()
        if not instance_type:
            raise CloudTemplateError(f"VM {name!r} has no instance type")

        disk_raw = raw.get("disk_gb")
        disk_gb = int(disk_raw) if disk_raw else pov_cloud_env.DEFAULT_DISK_GB
        if disk_gb < 8:
            raise CloudTemplateError(
                f"VM {name!r} asks for a {disk_gb} GB disk; nothing boots in that")

        clean.append({
            "name": name, "role": role, "os_family": os_family,
            "image_ref": image_ref or None, "image_id": image_id or None,
            "instance_type": instance_type, "disk_gb": disk_gb,
            "sort_order": index,
        })

    if brokers > 1:
        raise CloudTemplateError(
            f"{brokers} VMs are marked as the broker. Exactly one VM carries the "
            f"dashboard agent, the Gateway and the Resource Broker.")
    if brokers == 0:
        # Not a refusal: a template of pure targets is a legitimate thing to build, and a
        # warning in a JSON response is a warning nobody reads. The broker install refuses
        # by name later, which is the visible version of this.
        logger.info("cloud template saved with no broker VM; its POV will have no agent")
    return clean


def _replace_vms(db: Session, template_id: str, specs: list) -> None:
    """Swap the whole VM list for a new one, in one transaction with its template.

    Replaced wholesale rather than diffed by id. A template's VM rows are a *description*
    with no identity of their own — nothing references them, and no environment is built
    from them after the fact — so a diff would be machinery in exchange for nothing, and
    would silently preserve a row the operator meant to remove.
    """
    (db.query(PovCloudTemplateVM)
       .filter(PovCloudTemplateVM.template_id == template_id)
       .delete(synchronize_session=False))
    for spec in specs:
        db.add(PovCloudTemplateVM(template_id=template_id, **spec))


def create(db: Session, *, cloud: str, name: str, vms: list, description: str = "",
           region: str = "", network_cidr: str = "", workgroup: str = "",
           created_by: str = "", source_environment_id: str = "") -> PovCloudTemplate:
    cloud = _validate_cloud(cloud)
    slug = normalize_name(name)
    cidr = _validate_cidr(network_cidr)
    specs = _validate_vms(vms)

    # Unique per cloud, not globally: an `aws` and an `azure` template describing the same
    # POV are two rows by design, and making one of them pick a different name would be a
    # rename with no reason an operator could see.
    clash = db.query(PovCloudTemplate).filter(PovCloudTemplate.cloud == cloud,
                                              PovCloudTemplate.name == slug).first()
    if clash:
        raise CloudTemplateError(
            f"a {cloud} template named {slug!r} already exists")

    row = PovCloudTemplate(
        cloud=cloud, name=slug, description=(description or "").strip() or None,
        region=(region or "").strip() or None,
        network_cidr=cidr or None,
        source_environment_id=(source_environment_id or "").strip() or None,
        workgroup=(workgroup or "").strip() or None,
        created_by=created_by or None,
    )
    db.add(row)
    db.flush()                      # need the id before the children
    _replace_vms(db, row.id, specs)
    db.commit()
    return row


def update(db: Session, row: PovCloudTemplate, payload: dict) -> PovCloudTemplate:
    """Apply a partial update. Only keys present in ``payload`` are touched.

    ``cloud`` is deliberately absent: re-pointing a template at another cloud would keep
    instance types and image ids that mean nothing there, producing a template that looks
    saved and cannot build. Copying it to a new one is the honest version of that.
    """
    if "name" in payload:
        slug = normalize_name(payload["name"])
        clash = (db.query(PovCloudTemplate)
                   .filter(PovCloudTemplate.cloud == row.cloud,
                           PovCloudTemplate.name == slug,
                           PovCloudTemplate.id != row.id).first())
        if clash:
            raise CloudTemplateError(
                f"a {row.cloud} template named {slug!r} already exists")
        row.name = slug
    if "description" in payload:
        row.description = (payload["description"] or "").strip() or None
    if "region" in payload:
        row.region = (payload["region"] or "").strip() or None
    if "network_cidr" in payload:
        row.network_cidr = _validate_cidr(payload["network_cidr"]) or None
    if "workgroup" in payload:
        row.workgroup = (payload["workgroup"] or "").strip() or None
    if "vms" in payload:
        _replace_vms(db, row.id, _validate_vms(payload["vms"]))
    db.commit()
    return row


def delete(db: Session, row: PovCloudTemplate) -> None:
    """Remove a template and its VM rows.

    **A live POV built from this template is not a reason to refuse.** ``PovEnvironment``
    keeps ``template_name`` denormalised precisely so a POV still reads sensibly once its
    template is gone, and every platform call is keyed on ``platform_environment_id``
    rather than on the template — so nothing about a running environment depends on this
    row surviving. Refusing would instead mean an operator cannot tidy a mistake until the
    evaluation it seeded finishes weeks later.
    """
    (db.query(PovCloudTemplateVM)
       .filter(PovCloudTemplateVM.template_id == row.id)
       .delete(synchronize_session=False))
    db.delete(row)
    db.commit()


def describe(db: Session, row: PovCloudTemplate) -> dict:
    """The shape the API and the editor both read."""
    vms = vms_of(db, row.id)
    return {
        "id": row.id,
        "cloud": row.cloud,
        "name": row.name,
        "description": row.description or "",
        "region": row.region or "",
        "network_cidr": row.network_cidr or "",
        # Shown so an operator can see what a blank field will actually build, rather than
        # having to know the default.
        "effective_network_cidr": row.network_cidr or pov_cloud_env.DEFAULT_NETWORK_CIDR,
        "source_environment_id": row.source_environment_id or "",
        "workgroup": row.workgroup or "",
        "created_by": row.created_by or "",
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
        "vm_count": len(vms),
        "broker_vm_name": next((v.name for v in vms if v.role == "broker"), ""),
        "vms": [{
            "id": v.id,
            "name": v.name,
            "role": v.role or "target",
            "os_family": v.os_family or "linux",
            "image_ref": v.image_ref or "",
            "image_id": v.image_id or "",
            "instance_type": v.instance_type,
            "disk_gb": v.disk_gb or pov_cloud_env.DEFAULT_DISK_GB,
        } for v in vms],
    }
