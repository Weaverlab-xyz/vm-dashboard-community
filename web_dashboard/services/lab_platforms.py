"""The lab-platform registry: which platforms a POV environment can run on.

A POV environment is a template instantiated whole on some lab platform. Skytap is the
first one, and naming the feature after it would be the mistake this module exists to
avoid — the "cloud abstraction" elsewhere in this codebase is a dozen parallel literal
tuples and an ``if/elif`` chain per feature, because no interface was drawn while there was
still only one implementation.

Only a thin slice of the feature is platform-specific:

    platform-specific : auth, template listing, environment create/delete, runstate,
                        bootstrap injection, share links, idle suspend, stored credentials
    shared            : the broker/agent wait, the tenant-component install, the entire
                        PAM wire-up, the reaping manifest, expiry, the tables, the jobs,
                        the UI

So this module holds the registry and the contract, and nothing else. It follows the
conventions this codebase already uses rather than introducing new ones: a ``VALID_*``
tuple, an importlib dispatch dict (as ``ot_service._CELL_VM_SERVICE`` does), and a
capability table — no base classes, because there are none anywhere in this repo.

Pure: no I/O, no database, no adapter imports at module scope. Importing the registry must
not import every adapter's HTTP stack.
"""
from __future__ import annotations

import importlib
import logging

logger = logging.getLogger(__name__)

# The clouds that can act as a lab platform. Kept apart from VALID_PLATFORMS so callers
# can ask "is this a public cloud?" without a literal tuple of their own — the exact habit
# this module's docstring is about.
#
# **This tuple lists the clouds that are BUILT, not the clouds that are planned.** A
# fifth needs a driver module and a CAPABILITIES row before its name may appear here; adding a name early would make `pov_cloud_platform = "azure"` a selectable
# option that fails at import time, which is the "half-added platform" the registry's own
# test exists to catch.
CLOUD_PLATFORMS = ("aws", "azure", "gcp", "oci")

VALID_PLATFORMS = ("skytap",) + CLOUD_PLATFORMS

# adapter module name, relative to this package.
_ADAPTER_MODULE = {
    "skytap": "skytap_service",
    "aws": "pov_aws_service",
    "azure": "pov_azure_service",
    "gcp": "pov_gcp_service",
    "oci": "pov_oci_service",
}

# The functions an adapter must expose. Split by slice, deliberately: a contract that
# claims functions nobody has written yet cannot be enforced, and a test that skips the
# check because half the contract is aspirational protects nothing.
#
# READ_CONTRACT is required today and asserted by tests/test_lab_platforms.py.
READ_CONTRACT = (
    "configured",         # () -> bool: are this platform's credentials present?
    "list_templates",     # () -> [{id, name, description, vm_count, region}]
    "list_environments",  # () -> [{id, name, runstate, vm_count, region, url}]
    "get_environment",    # (env_id) -> {..., vms: [{id, name, os_family, private_ip}]}
)

# WRITE_CONTRACT lands with the provision slice. Listed now so the shape is agreed before
# a second adapter exists to argue with it — but NOT asserted, because asserting it would
# force stub functions whose only behaviour is to raise.
WRITE_CONTRACT = (
    "create_environment",   # (template_id, name, **opts) -> env dict
    "set_runstate",         # (env_id, runstate) -> env dict
    "inject_bootstrap",     # (env_id, vm_id, payload) -> None
    "create_share",         # (env_id, password, expires_at) -> {url, id, expires_at}
    "delete_share",         # (env_id, share_id) -> None
    "stored_credentials",   # (env_id, vm_id) -> [{text, notes}]
    "delete_environment",   # (env_id) -> None
    # OPTIONAL, and absent from Skytap on purpose. () -> str: what the environment
    # id WILL be, given the POV name. A platform that mints its own id cannot
    # answer; one whose "environment" is a tag this dashboard chooses can, and
    # `run_env_provision` records it before the first create so a partial failure
    # is still reapable. Callers must use getattr, never assume it.
    "environment_id_for",   # (name) -> str
)

# The template-authoring slice. Separate from WRITE_CONTRACT rather than appended to it
# because it is guarded by a capability of its own: a platform can be perfectly usable for
# POVs while offering no way to save an environment back as a template, and folding these
# into the list above would make that platform look like it failed a contract it was never
# asked to meet.
AUTHOR_CONTRACT = (
    "get_template",              # (template_id) -> {..., vms: [...]}
    "create_template",           # (env_id, name, description) -> template dict
    "delete_template",           # (template_id) -> None
    "publish_service",           # (env_id, vm_id, iface_id, port) -> {id, external_ip, ...}
    "delete_published_service",  # (env_id, vm_id, iface_id, service_id) -> None
)

# What each platform can actually do. This table is the part that keeps the abstraction
# honest: where a platform lacks something the feature must degrade **explicitly and say
# so**, rather than failing late with a confusing error.
#
# `bootstrap_injection` is one INTENT with different mechanisms, which is why it is an enum
# rather than a boolean:
#   "metadata"    - the platform hands data to the guest and the guest fetches it
#                   (Skytap: per-VM user_data, read at http://169.254.169.254/skytap).
#                   Injected AFTER power-on, into a VM that already exists.
#   "cloud_init"  - the platform RUNS it, once, on first boot (EC2 user-data, Azure
#                   customData, GCE metadata startup-script, OCI user_data). Read as a
#                   weaker "metadata" at your peril: it must be supplied AT CREATE, which
#                   is why a cloud POV builds its broker VM last rather than injecting
#                   into one that is already up.
#   "remote_exec" - the platform runs a script on the guest on our behalf
#                   (CloudShare's execute-script call)
#   None          - neither; the enroll code has to be baked into the template, which is
#                   single-use per template rebuild and must be documented as such
CAPABILITIES = {
    "skytap": {
        "label": "Skytap",
        "templates": True,
        "runstate": True,
        "idle_suspend": True,          # suspend_on_idle, in seconds, per environment
        "scheduled_suspend": False,    # it has its own timer; see the aws row below
        "bootstrap_injection": "metadata",
        "share_link": True,            # publish_sets: password + expiration_date
        "stored_credentials": True,    # …/vms/{id}/credentials
        # A cheap authenticated read that proves the credential AND what it can see, so an
        # operator can settle "is Skytap working?" from Settings instead of from a 502 on
        # the POV page. Deliberately a capability rather than a READ_CONTRACT entry: not
        # every platform will have a read that proves a credential without a side effect,
        # and inventing one is how a Verify starts reporting green for a token that does
        # not work — the reason `bt_tenant_verify` leaves Entitle unverifiable.
        "verify": True,                # adapter.verify() -> (ok, message)
        # Skytap scopes templates and environments by project. A platform without projects
        # simply has no such field to offer, which is why the POV create form asks this
        # before rendering one.
        "projects": True,              # /v2/projects/{id}/{templates,configurations}
        # Can VMs be added to an environment that already exists? Skytap alone can, by
        # merging a template into it. A cloud POV's VM set is whatever its own template
        # service created, so the button is hidden there rather than offered and refused —
        # which is what this table is for.
        "vm_add": True,                # PUT /configurations/{id}.json, template_id+vm_ids
        # Can an environment be saved back as a template? This is what makes the template
        # BUILDER possible at all — see services/pov_template_builder and
        # docs/profiles/pov/skytap.md#building-a-template. Skytap has no "edit a template"
        # call, so authoring is always instantiate → change → bake, never edit in place.
        "template_authoring": True,    # POST /v2/templates {configuration_id}
        # Can a guest port be NAT-ed to a public ip:port? The builder needs exactly one of
        # these, for the length of one build, to reach a brand-new VM on a private lab
        # network and install the metadata runner. A platform without it degrades to the
        # generate-and-paste path rather than failing — the builder asks before publishing.
        "published_services": True,    # …/interfaces/{id}/services
    },
    # AWS as a lab platform. Read this row against Skytap's above: almost every difference
    # is a thing AWS does NOT have, and the point of the table is that each one degrades
    # somewhere visible instead of failing inside a provision job.
    "aws": {
        "label": "AWS",
        # The template is a dashboard-owned topology spec (`pov_cloud_templates`), not
        # something the cloud holds. True because the FEATURE exists, which is what every
        # caller is actually asking.
        "templates": True,
        "runstate": True,              # start / stop, via EC2. Not suspend: see below
        # **No public cloud has an idle timer.** Skytap's `suspend_on_idle` is the single
        # biggest lever on its spend and it has no analogue here, so the dashboard has to
        # supply one — hence the key beside it.
        "idle_suspend": False,
        # The dashboard suspends and resumes this platform on a SCHEDULE, driven from the
        # reconcile pass. A schedule rather than an inactivity timer because "idle" on a
        # cloud has no honest definition from outside the guest: every candidate signal
        # (PRA session, agent heartbeat, console hit) has a blind spot that either leaves
        # a POV running all month or suspends one mid-demo.
        "scheduled_suspend": True,
        # EC2 user-data, run by cloud-init on FIRST boot. Read the enum value rather
        # than folding it into "metadata": Skytap stores a payload for a guest that is
        # already up to fetch, while this one is only ever delivered at RunInstances.
        # `pov_broker` branches on exactly that difference — a cloud broker VM is CREATED
        # with its bootstrap, after the targets, because the policy inside names their
        # addresses.
        "bootstrap_injection": "cloud_init",
        # No publish sets, and nothing like them. A cloud POV's customer-facing front door
        # is PRA — which makes PRA mandatory here where it is optional on Skytap. The POV
        # page reads this and says "PRA only" rather than offering a Share button that
        # cannot work.
        "share_link": False,
        # AWS holds no guest credentials. The platform login a Resource Broker install
        # needs comes from the template's own key pair / Vault account instead.
        "stored_credentials": False,
        "verify": True,                # a cheap DescribeRegions; see the adapter
        # An AWS account is not a project. The environment's scoping is its own VPC plus
        # the povEnvironment tag, both created per POV, so there is nothing for the create
        # form to ask.
        "projects": False,
        "vm_add": False,
        # Baking N AMIs off a running environment is a later slice, and a deliberate one:
        # every baked template is a standing storage bill. Templates are edited in the
        # dashboard today, which is a different thing from authoring one on the platform.
        "template_authoring": False,
        # No NAT-a-guest-port primitive, and no need for one: cloud-init installs the
        # agent without anybody reaching in.
        "published_services": False,
    },
    # Azure as a lab platform. Read this row against "aws" above: the only capability that
    # differs is `stored_credentials`, and it differs because Azure FORCED it — `os_profile`
    # requires an admin account at VM creation, for Linux as well as Windows, so a POV
    # built here has a platform login whether anybody wanted one or not.
    "azure": {
        "label": "Azure",
        "templates": True,
        "runstate": True,
        "idle_suspend": False,
        "scheduled_suspend": True,
        "bootstrap_injection": "cloud_init",
        "share_link": False,
        # True, unlike AWS. The generated admin credential is stored per environment and
        # returned in the contract's `[{text, notes}]` shape, so `pov_credentials` parses
        # it and the Resource Broker install has the login it needs.
        "stored_credentials": True,
        "verify": True,
        # An Azure subscription is not a project, and the environment's scoping is the
        # resource group this creates per POV — so there is nothing for the create form
        # to ask.
        "projects": False,
        "vm_add": False,
        "template_authoring": False,
        "published_services": False,
    },
    # GCP as a lab platform. The only capability that differs from both siblings is
    # `projects`: a GCP project is a real boundary an environment is built INSIDE, and
    # recording which one is what lets a teardown weeks later aim at the right place.
    "gcp": {
        "label": "GCP",
        "templates": True,
        "runstate": True,
        "idle_suspend": False,
        "scheduled_suspend": True,
        "bootstrap_injection": "cloud_init",
        "share_link": False,
        # False, like AWS. GCE holds no guest login to read back — unlike Azure, which is
        # forced to mint one at VM creation.
        "stored_credentials": False,
        "verify": True,
        # TRUE, alone among the clouds. `api/pov.provision` asks the adapter for the
        # configured project and records it on the row, and `pov_cloud_gcp` reads it back
        # rather than re-deriving it from current config.
        "projects": True,
        "vm_add": False,
        "template_authoring": False,
        "published_services": False,
    },
    # OCI as a lab platform. Shaped like AWS — no environment object, so the teardown
    # unpicks resource types — with the compartment recorded the way GCP records its
    # project.
    "oci": {
        "label": "OCI",
        "templates": True,
        "runstate": True,
        "idle_suspend": False,
        "scheduled_suspend": True,
        "bootstrap_injection": "cloud_init",
        "share_link": False,
        # False. OCI holds no guest login to read back; only Azure is forced to mint one.
        "stored_credentials": False,
        "verify": True,
        # TRUE, as for GCP. A compartment is a real container an environment goes into,
        # and recording which one is what lets a teardown weeks later aim at the right
        # place. A compartment is NOT used AS the environment — see pov_cloud_oci on why
        # the obvious analogy to an Azure resource group is the wrong one.
        "projects": True,
        "vm_add": False,
        "template_authoring": False,
        "published_services": False,
    },
}


class LabPlatformError(Exception):
    """The requested platform is unknown, unconfigured, or cannot do what was asked."""


def valid(platform: str) -> bool:
    return (platform or "").strip().lower() in VALID_PLATFORMS


def normalize(platform: str) -> str:
    p = (platform or "").strip().lower()
    if p not in VALID_PLATFORMS:
        raise LabPlatformError(
            f"unknown lab platform {platform!r}; expected one of "
            f"{', '.join(VALID_PLATFORMS)}")
    return p


def adapter(platform: str):
    """Import and return a platform's adapter module.

    Imported on use rather than at module scope so that reading the registry — which the
    UI and the tests do constantly — does not drag in every adapter's HTTP client.
    """
    name = normalize(platform)
    try:
        return importlib.import_module(f".{_ADAPTER_MODULE[name]}", package=__package__)
    except ImportError as exc:  # pragma: no cover - a packaging error, not a runtime one
        raise LabPlatformError(
            f"the {name} adapter could not be imported: {exc}") from exc


def capabilities(platform: str) -> dict:
    return dict(CAPABILITIES[normalize(platform)])


def supports(platform: str, capability: str) -> bool:
    """Whether a platform supports a capability.

    Callers must ask before using an optional one. A platform with no ``share_link`` should
    surface "PRA only" in the UI, not raise a 500 from inside a provision job.
    """
    return bool(CAPABILITIES[normalize(platform)].get(capability))

def selected_cloud() -> str:
    """The one public cloud this instance may run POVs on, or "".

    Read live rather than cached: it is a Settings row, and the create form has to reflect
    a change without a restart. An unrecognised value resolves to "" rather than raising,
    matching ``feature_flags.install_profile`` — a typo in one config row must not take
    the POV page down, and falling back to "no cloud" can only ever subtract.
    """
    from . import config_service, feature_flags   # local: stay import-cheap
    from ..config import settings
    # The flag first. It is what the demo profile masks, so a demo instance answers "no
    # cloud" here whatever is stored — and an operator who switches the feature off keeps
    # their choice of cloud for when they switch it back on.
    if not feature_flags.enabled("pov_cloud_enabled"):
        return ""
    raw = (config_service.get("pov_cloud_platform")
           or getattr(settings, "pov_cloud_platform", "") or "")
    raw = raw.strip().lower()
    return raw if raw in CLOUD_PLATFORMS else ""


def selectable_platforms() -> list[str]:
    """The platforms a POV may be created on: Skytap, plus at most one cloud.

    **This is where "one cloud at a time" is enforced**, and it is one list rather than a
    check scattered over the form, the API and the job — a rule that lives in three places
    is a rule that disagrees with itself. ``api/pov`` renders the platform selector from
    this and refuses a provision on anything absent from it.

    Deliberately NOT folded into :func:`configured_platforms`. "Not the selected cloud" and
    "no credentials" are different failures with different remedies, and collapsing them
    gives an operator who just added an Azure key a 409 that says the key is missing.
    """
    return [p for p in VALID_PLATFORMS
            if p not in CLOUD_PLATFORMS or p == selected_cloud()]


def selectable(platform: str) -> bool:
    """Whether ``platform`` may be used for a new POV on this instance."""
    try:
        return normalize(platform) in selectable_platforms()
    except LabPlatformError:
        return False



def configured_platforms() -> list[str]:
    """Platforms whose credentials are present. Best-effort: an adapter that raises while
    answering must not take the list down with it."""
    out = []
    for name in VALID_PLATFORMS:
        try:
            if adapter(name).configured():
                out.append(name)
        except Exception:  # noqa: BLE001
            logger.warning("lab platform %s failed its configured() check", name,
                           exc_info=True)
    return out
