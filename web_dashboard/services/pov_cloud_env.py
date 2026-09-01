"""Building a POV environment on a public cloud.

Skytap hands the dashboard an *environment* as a first-class object: one POST against a
template id and N VMs exist, powered, networked and named. No public cloud has that call.
So this module is the thing that stands in for it — the part of a cloud lab platform that
is the same on every cloud, with the SDK work behind a per-cloud driver.

What lives here:

    shared here      : the environment id and tag conventions, template resolution,
                       image resolution, the runstate wait loop, refusals
    per-cloud driver : the SDK calls — network, instances, read-back, teardown, power

Three rules this module exists to hold.

**The environment id is deterministic, and it is minted before anything is created.**
``povenv-<pov name>`` — and the POV name is already unique among non-destroyed rows, so
the id is too. Every resource carries it as a tag, so a create that dies at VM three of
five still leaves a complete, findable, reapable set. ``pov_env_service`` says "failing
without the id is how an orphan is created"; on a platform that mints ids for you the fix
is to persist the id early, and on one that does not it is to not need the platform to
tell you.

**One network per POV, and no inbound rules on it.** Two customers' evaluations must not
share a broadcast domain, and the teardown of a VPC you created is exact in a way that
"delete the instances I remember" is not. Nothing needs to reach in: the dashboard agent,
the Gateway and the Resource Broker all connect *outbound*, so the security group opens
nothing and the customer's front door is PRA. That is also why there is no NAT gateway —
instances get a public address for egress instead, which for a handful of VMs costs a
fraction of a NAT's standing charge and, with no inbound rules, exposes nothing.

**A cloud environment has no idle timer.** Nothing here suspends anything on its own;
``pov_reconcile`` drives the schedule and enqueues a normal ``pov_env_power`` job. This
module only ever does what it was asked to do, once.
"""
from __future__ import annotations

import asyncio
import importlib
import ipaddress
import logging
import re
import time

logger = logging.getLogger(__name__)

# Every resource a POV environment owns carries these. The environment tag is what the
# teardown, the reconcile sweep and any future cost attribution all select on, so it is
# applied at CREATE in the same API call as the resource — never in a second call that
# could fail and leave an untagged, unfindable thing running.
TAG_ENVIRONMENT = "povEnvironment"
TAG_MANAGED_BY = "povManagedBy"
TAG_ROLE = "povRole"
TAG_NAME = "Name"
MANAGED_BY = "vm-dashboard"

# The estate-wide tag every other dashboard-provisioned resource carries. Written IN
# ADDITION to `povManagedBy`, never instead of it: this one is what `/costs` sums as the
# "dashboard" scope, and a billable dashboard-created resource missing it is invisible
# there — the bug behind the SSM interface endpoints, which `tests/test_managed_by_tag_values`
# was written about. `povManagedBy` stays the SELECTOR, because a teardown filtered on the
# estate-wide tag would select a demo instance's VMs too.
TAG_ESTATE = "managed-by"

ENV_ID_PREFIX = "povenv-"

# The environment's private network when a template does not name one. A /16 split into
# /24s leaves room for a POV to grow without the template author thinking about it.
DEFAULT_NETWORK_CIDR = "10.20.0.0/16"

DEFAULT_DISK_GB = 30

# What `set_runstate` accepts on a cloud. Deliberately shorter than Skytap's four: a cloud
# instance stops, it does not suspend to RAM, and offering "suspended" would be a word the
# platform silently reinterprets. `pov_env_service` writes whatever comes back.
VALID_RUNSTATES = ("running", "stopped")

# How long to wait for every instance in an environment to reach a runstate. Generous:
# this covers a cold Windows boot as well as a Linux one, and the caller is a job with its
# own progress reporting, not a request.
RUNSTATE_TIMEOUT_S = 900
RUNSTATE_POLL_S = 10

_DRIVER_MODULE = {
    "aws": "pov_cloud_aws",
    "azure": "pov_cloud_azure",
    "gcp": "pov_cloud_gcp",
    "oci": "pov_cloud_oci",
}

# The POV name rule, restated. Imported from nowhere on purpose: this module has to be
# able to answer "is this a well-formed environment id?" without importing the API layer.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


class CloudEnvError(Exception):
    """A cloud POV environment could not be built, read or torn down.

    The message names the remedy. Raised rather than returned because every caller is a
    job step that must stop — but note the codebase rule these obey: an expected outcome
    ("these credentials do not work") is a RETURN VALUE from `verify`, not an exception a
    handler stringifies into a response body.
    """


def driver(cloud: str):
    """The SDK driver for one cloud. Imported on use, like ``lab_platforms.adapter``."""
    name = (cloud or "").strip().lower()
    if name not in _DRIVER_MODULE:
        raise CloudEnvError(
            f"{cloud!r} has no POV cloud driver; built today: "
            f"{', '.join(sorted(_DRIVER_MODULE))}")
    try:
        return importlib.import_module(f".{_DRIVER_MODULE[name]}", package=__package__)
    except ImportError as exc:  # pragma: no cover - packaging, not runtime
        raise CloudEnvError(f"the {name} POV driver could not be imported: {exc}") from exc


def env_id_for(name: str) -> str:
    """The environment id a POV called ``name`` will always have.

    Deterministic so that a provision which fails partway is still reapable: the tag on
    whatever got created is derivable from the row's own name, with no round trip to a
    platform that may never have told us anything.
    """
    slug = (name or "").strip().lower()
    if not _NAME_RE.match(slug):
        raise CloudEnvError(
            "a POV name must be 2-63 characters of lowercase letters, digits and "
            "hyphens, starting with a letter or digit")
    return ENV_ID_PREFIX + slug


def is_env_id(value: str) -> bool:
    return (value or "").startswith(ENV_ID_PREFIX)


def base_tags(env_id: str) -> dict:
    """The tags every resource in ``env_id`` carries."""
    return {TAG_ENVIRONMENT: env_id, TAG_MANAGED_BY: MANAGED_BY,
            TAG_ESTATE: MANAGED_BY}


def subnet_cidr(network_cidr: str) -> str:
    """The one subnet carved out of an environment's network.

    A single subnet rather than one per tier: a POV is a handful of VMs that must all see
    each other, and multi-subnet routing is a topology decision no template author has
    asked for. Returns the first /24, or the network itself when it is already smaller.
    """
    net = ipaddress.ip_network(network_cidr or DEFAULT_NETWORK_CIDR, strict=False)
    if net.prefixlen >= 24:
        return str(net)
    return str(next(net.subnets(new_prefix=24)))


# ── template and image resolution ────────────────────────────────────────────

def resolve_image(row, cloud: str, region: str) -> str:
    """The cloud image id a template VM boots, or a refusal that names the remedy.

    ``image_id`` is a literal and wins. ``image_ref`` is a ``registered_images`` row, and
    resolving it late — here, at provision — is the point: an image promoted again after
    the template was written should be picked up without editing the template.

    **A region mismatch is refused, not ignored.** An AMI is region-scoped, so a template
    resolving to an image promoted into another region would fail at ``RunInstances`` with
    an "image not found" that reads exactly like a deleted image.
    """
    from ..database import RegisteredImage, SessionLocal

    literal = (getattr(row, "image_id", "") or "").strip()
    if literal:
        return literal

    ref = (getattr(row, "image_ref", "") or "").strip()
    if not ref:
        raise CloudEnvError(
            f"template VM {row.name!r} names neither an image nor a catalog image; "
            f"edit the template and pick one")

    db = SessionLocal()
    try:
        img = db.get(RegisteredImage, ref)
        if img is None:
            raise CloudEnvError(
                f"template VM {row.name!r} points at catalog image {ref}, which no longer "
                f"exists; edit the template and pick another")
        if (img.source_cloud or "").lower() == cloud:
            found, found_region = (img.source_image_id or ""), (img.source_region or "")
        else:
            promo = (img.promotions_dict.get(cloud) or {})
            found = (promo.get("image_id") or promo.get("self_link") or "")
            found_region = promo.get("region") or ""
        if not found:
            raise CloudEnvError(
                f"catalog image {img.name!r} has not been promoted to {cloud}; promote it "
                f"from the Images page, or give template VM {row.name!r} a literal "
                f"image id")
        if found_region and region and found_region != region:
            raise CloudEnvError(
                f"catalog image {img.name!r} is in {found_region} and this POV builds in "
                f"{region}; promote it into {region}, or build the POV there")
        return found
    finally:
        db.close()


def vm_specs(template_row, vm_rows, cloud: str, region: str, *,
             roles: tuple = ("target",), bootstrap: str = "") -> list:
    """Turn template rows into the flat dicts a driver builds from.

    ``roles`` selects which of them. The default excludes the broker, and that is the
    ordering constraint this whole path is arranged around: **the agent's policy grants
    the POV's target addresses, and those do not exist until the targets do.** So the
    targets are built first, read back, and only then is the broker created — with the
    finished policy and a fresh enrolment code already in its user-data.

    That is also why a cloud broker cannot use ``inject_bootstrap``. Cloud-init runs
    user-data on FIRST boot; handing it to an instance that is already up does nothing at
    all, silently. The payload has to be there at ``RunInstances``.
    """
    specs = []
    for row in vm_rows:
        role = (row.role or "target").strip().lower()
        if role not in roles:
            continue
        specs.append({
            "name": row.name,
            "role": role,
            "os_family": (row.os_family or "linux").strip().lower(),
            "image_id": resolve_image(row, cloud, region),
            "instance_type": row.instance_type,
            "disk_gb": int(row.disk_gb or DEFAULT_DISK_GB),
            "user_data": bootstrap if role == "broker" else "",
        })
    return specs


def load_template(template_id: str, cloud: str):
    """``(template_row, [vm_rows])`` for a cloud template, or a refusal.

    Opens its own session. The lab-platform contract passes no database handle — an
    adapter is a platform client as far as its callers know — and a cloud's "platform" is
    partly this dashboard's own tables.
    """
    from ..database import PovCloudTemplate, PovCloudTemplateVM, SessionLocal

    db = SessionLocal()
    try:
        row = db.get(PovCloudTemplate, (template_id or "").strip())
        if row is None:
            raise CloudEnvError(f"no cloud POV template with id {template_id!r}")
        if (row.cloud or "").lower() != cloud:
            raise CloudEnvError(
                f"template {row.name!r} builds on {row.cloud}, not {cloud}; a template is "
                f"not portable between clouds")
        vms = (db.query(PovCloudTemplateVM)
                 .filter(PovCloudTemplateVM.template_id == row.id)
                 .order_by(PovCloudTemplateVM.sort_order,
                           PovCloudTemplateVM.name).all())
        if not vms:
            raise CloudEnvError(
                f"template {row.name!r} has no VMs; add at least one before building "
                f"a POV from it")
        db.expunge_all()
        return row, vms
    finally:
        db.close()


def recorded_region(env_id: str, cloud: str) -> str:
    """The region a POV was built in, from the row that recorded it.

    The lab-platform reads take an environment id and nothing else, and on a cloud the id
    alone does not say where to look. Re-deriving it from current config would be the
    mistake ``expiry_reaper`` refuses to make — "a destroy aimed at the wrong project is
    the worst version of this bug" — so it comes off the POV row, and only falls back to
    the configured default for an environment this dashboard has no row for (the
    "everything on the platform" listing).
    """
    from ..database import PovEnvironment, SessionLocal

    db = SessionLocal()
    try:
        row = (db.query(PovEnvironment)
                 .filter(PovEnvironment.platform_environment_id == env_id)
                 .first())
        if row is not None and row.region:
            return row.region
    finally:
        db.close()
    return driver(cloud).default_region()


def recorded_project(env_id: str, cloud: str) -> str:
    """The project a POV was built in, from the row that recorded it. "" if unknown.

    The sibling of :func:`recorded_region`, and it exists for the sharper half of the same
    reason. A region read from current config aims a teardown at the wrong place; a
    PROJECT read from current config aims it at somebody else's estate. ``expiry_reaper``
    states the rule outright — a destroy aimed at the wrong project is the worst version
    of this bug — which is why this reads the row and the caller falls back to the default
    only when there is no row to read.
    """
    from ..database import PovEnvironment, SessionLocal

    db = SessionLocal()
    try:
        row = (db.query(PovEnvironment)
                 .filter(PovEnvironment.platform == cloud,
                         PovEnvironment.platform_environment_id == env_id)
                 .first())
        return (row.project_id or "") if row is not None else ""
    except Exception:  # noqa: BLE001 - a read must not die on a bad row
        logger.warning("could not read the recorded project for %s", env_id, exc_info=True)
        return ""
    finally:
        db.close()

def known_regions(cloud: str) -> list:
    """Regions worth listing environments in: the default, plus any a POV row names.

    Not every region the provider has. A cloud-wide sweep is thirty-odd API calls per page
    load to find nothing, and this dashboard only ever creates a POV in a region it then
    records.
    """
    from ..database import PovEnvironment, SessionLocal

    out = [driver(cloud).default_region()]
    db = SessionLocal()
    try:
        rows = (db.query(PovEnvironment.region)
                  .filter(PovEnvironment.platform == cloud)
                  .distinct().all())
        for (region,) in rows:
            if region and region not in out:
                out.append(region)
    except Exception:  # noqa: BLE001 - a listing must not die on a bad row
        logger.warning("could not read recorded %s POV regions", cloud, exc_info=True)
    finally:
        db.close()
    return [r for r in out if r]


# ── the lab-platform surface, cloud-parameterised ────────────────────────────

async def list_templates(cloud: str) -> list:
    """The cloud POV templates for this cloud, in the shape READ_CONTRACT asks for."""
    from ..database import PovCloudTemplate, PovCloudTemplateVM, SessionLocal

    db = SessionLocal()
    try:
        rows = (db.query(PovCloudTemplate)
                  .filter(PovCloudTemplate.cloud == cloud)
                  .order_by(PovCloudTemplate.name).all())
        counts = {}
        for tid, in db.query(PovCloudTemplateVM.template_id).all():
            counts[tid] = counts.get(tid, 0) + 1
        return [{
            "id": r.id,
            "name": r.name,
            "description": r.description or "",
            # A real measured count, unlike the platform listings where an absent array
            # means "not measured" and must render as "—".
            "vm_count": counts.get(r.id, 0),
            "region": r.region or "",
        } for r in rows]
    finally:
        db.close()


async def get_template(cloud: str, template_id: str) -> dict:
    row, vms = load_template(template_id, cloud)
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description or "",
        "region": row.region or "",
        "network_cidr": row.network_cidr or DEFAULT_NETWORK_CIDR,
        "vm_count": len(vms),
        "vms": [{
            "id": v.id,
            "name": v.name,
            "role": v.role or "target",
            "os_family": v.os_family or "linux",
            "image_ref": v.image_ref or "",
            "image_id": v.image_id or "",
            "instance_type": v.instance_type,
            "disk_gb": v.disk_gb or DEFAULT_DISK_GB,
        } for v in vms],
    }


async def create_environment(cloud: str, template_id: str, name: str, *,
                             bootstrap: str = "", **_ignored) -> dict:
    """Build a POV environment: one network, then every VM the template names.

    Ordered so that a failure is always recoverable. The network is created first and
    tagged in the same call; the VMs are launched after it. Everything wears the
    environment tag from the instant it exists, so ``delete_environment`` on a half-built
    environment finds and removes exactly what got made — which is why this function does
    no cleanup of its own on failure. Rolling back here would race the reaper and, on a
    partial failure, is the one path most likely to be interrupted itself.
    """
    mod = driver(cloud)
    env_id = env_id_for(name)
    row, vm_rows = load_template(template_id, cloud)
    region = (row.region or "").strip() or mod.default_region()
    cidr = (row.network_cidr or "").strip() or DEFAULT_NETWORK_CIDR

    if sum(1 for v in vm_rows if (v.role or "") == "broker") > 1:
        raise CloudEnvError(
            f"template {row.name!r} declares more than one broker VM; exactly one VM "
            f"carries the dashboard agent, the Gateway and the Resource Broker")

    # Targets only. The broker VM is created later, by `create_broker_vm`, because its
    # user-data has to carry a policy naming the targets' addresses — which do not exist
    # until the targets do. An environment therefore shows one fewer VM than its template
    # until the broker lands, which is the honest reading of "the broker is not installed
    # yet" rather than a VM sitting there doing nothing.
    specs = vm_specs(row, vm_rows, cloud, region)
    if not specs:
        raise CloudEnvError(
            f"template {row.name!r} declares only a broker VM. A POV needs at least one "
            f"target for the broker to reach, or there is nothing to demonstrate.")

    logger.info("POV %s: creating network in %s (%s)", env_id, region, cidr)
    network = await mod.create_network(env_id, region, cidr, subnet_cidr(cidr))
    logger.info("POV %s: launching %d VMs", env_id, len(specs))
    await mod.create_vms(env_id, region, specs, network)

    built = await mod.read_environment(env_id, region)
    return built or {
        "id": env_id, "name": name, "runstate": "", "region": region,
        "vm_count": len(specs), "vms": [], "url": "",
    }



async def create_broker_vm(cloud: str, env_id: str, template_id: str,
                           bootstrap: str) -> dict:
    """Build (or rebuild) this environment's broker VM, with its bootstrap in user-data.

    Separate from ``create_environment`` because of the ordering the policy forces — see
    :func:`vm_specs`. Called by ``pov_broker`` once the targets are up and their addresses
    are known.

    **Rebuilding is a terminate and a fresh launch, deliberately.** On Skytap a re-broker
    has to remember to delete the agent's state volume, because an agent that already
    enrolled never redeems a second code and a surviving volume gives a container that
    starts fine and 401s forever. Here the volume dies with the instance, so "press Broker
    again" is clean by construction rather than by remembering.
    """
    mod = driver(cloud)
    row, vm_rows = load_template(template_id, cloud)
    region = recorded_region(env_id, cloud)
    specs = vm_specs(row, vm_rows, cloud, region, roles=("broker",),
                     bootstrap=bootstrap)
    if not specs:
        raise CloudEnvError(
            f"template {row.name!r} declares no broker VM, so there is nowhere for the "
            f"agent to run. Edit the template and mark one VM as the broker.")

    network = await mod.read_network(env_id, region)
    if not network:
        raise CloudEnvError(
            f"the network for {env_id} could not be read back, so a broker VM cannot be "
            f"placed in it. Check the environment still exists in {region}.")

    await mod.remove_vms(env_id, region, [specs[0]["name"]])
    created = await mod.create_vms(env_id, region, specs, network)
    return created[0] if created else {}


async def list_environments(cloud: str) -> list:
    """Every POV environment this cloud's credentials can see.

    Asked once or once per region, depending on what the provider's listing is scoped to.
    A driver setting ``LISTS_ALL_REGIONS`` has a subscription-wide catalogue — Azure's
    resource groups — and asking it per region would return the same environments N times.
    One that does not (EC2's DescribeInstances is regional) is asked for each region the
    dashboard has reason to look in.

    The per-region path is the weaker of the two: it can only look where a POV row has
    already been recorded, so an orphan in some other region stays invisible. That is why
    the flag exists rather than a uniform loop — where a provider CAN answer globally, the
    orphan sweep should be complete.
    """
    mod = driver(cloud)
    if getattr(mod, "LISTS_ALL_REGIONS", False):
        return await mod.list_environments("")
    out = []
    for region in known_regions(cloud):
        try:
            out.extend(await mod.list_environments(region))
        except Exception:  # noqa: BLE001 - one bad region must not empty the page
            logger.warning("could not list %s POV environments in %s", cloud, region,
                           exc_info=True)
    return out


async def get_environment(cloud: str, env_id: str) -> dict:
    env_id = (env_id or "").strip()
    if not env_id:
        raise CloudEnvError("an environment id is required")
    found = await driver(cloud).read_environment(env_id, recorded_region(env_id, cloud))
    if found is None:
        raise CloudEnvError(f"no {cloud} POV environment tagged {env_id}")
    return found


async def delete_environment(cloud: str, env_id: str) -> None:
    """Remove everything tagged with this environment id.

    Idempotent, like every other adapter's delete: an environment already gone is a
    successful teardown, because the caller is a destroy job that must be able to run
    twice — the reaper and the Destroy button can both reach it.
    """
    env_id = (env_id or "").strip()
    if not is_env_id(env_id):
        raise CloudEnvError(
            f"{env_id!r} is not a POV environment id, and a tag-scoped delete against an "
            f"arbitrary value would select resources this dashboard does not own")
    await driver(cloud).delete_environment(env_id, recorded_region(env_id, cloud))


async def set_runstate(cloud: str, env_id: str, runstate: str) -> dict:
    target = (runstate or "").strip().lower()
    if target not in VALID_RUNSTATES:
        raise CloudEnvError(
            f"{cloud} POV environments can be {' or '.join(VALID_RUNSTATES)}, not "
            f"{runstate!r} — a cloud instance has no suspend-to-RAM")
    await driver(cloud).power(env_id, recorded_region(env_id, cloud),
                              "start" if target == "running" else "stop")
    # Best effort. The caller wants the new runstate for the row, and a read that has not
    # caught up yet is not a reason to fail an action that already succeeded.
    return await _read_or_none(cloud, env_id) or {
        "id": env_id, "runstate": "", "vms": [], "vm_count": 0}


async def _read_or_none(cloud: str, env_id: str):
    """``get_environment``, but a not-yet-visible environment is None rather than a raise.

    Used by the two callers that are polling rather than answering a question. A cloud's
    tag index is eventually consistent, so for a few seconds after ``run_instances`` a
    describe filtered on the environment tag can legitimately come back empty — and
    treating that as "the environment does not exist" would fail a provision at the exact
    moment it had just succeeded.
    """
    try:
        return await get_environment(cloud, env_id)
    except CloudEnvError:
        return None


async def wait_for_runstate(cloud: str, env_id: str, target: str,
                            timeout_s: int = RUNSTATE_TIMEOUT_S) -> dict:
    """Poll until every VM in the environment reports ``target``.

    Every VM, not the first: an environment is one unit, and reporting "running" while a
    Windows target is still coming up would send the wire-up at a VM with no address yet.
    """
    target = (target or "").strip().lower()
    deadline = time.monotonic() + max(1, int(timeout_s))
    last = None
    while True:
        last = await _read_or_none(cloud, env_id)
        if last is not None:
            states = {(v.get("runstate") or "") for v in last.get("vms") or []}
            if states and states == {target}:
                return last
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(RUNSTATE_POLL_S)

    if last is None:
        raise CloudEnvError(
            f"nothing tagged {env_id} was visible in {cloud} within "
            f"{timeout_s // 60} minutes. If the build reported success, check the "
            f"account and region the credentials point at.")
    seen = sorted({(v.get("runstate") or "?") for v in last.get("vms") or []})
    raise CloudEnvError(
        f"{env_id} did not reach {target!r} within {timeout_s // 60} minutes; its VMs "
        f"report {seen}. Check the {cloud} console, then retry the action.")
