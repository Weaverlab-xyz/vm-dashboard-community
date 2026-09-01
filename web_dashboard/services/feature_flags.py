"""The feature map, in one place, for the app AND the job worker.

``/api/features`` used to build this inline in ``main.py``. It moved here because
``services/dashboard_collect`` needs the same map — it collects a tile exactly when the
dashboard would show it — and ``dash-worker`` runs ``python -m web_dashboard.jobs_worker``
and must NOT import ``main``: that would construct the FastAPI app, its routers and its
lifespan in a process that serves no requests.

The alternative was a second copy of the gating rules in the collector, and a feature map
that disagrees with the one the page reads is the drift ``main.py``'s warmer comment block
and ``tests/test_cache_warmer_parity`` exist to prevent. One source, two callers.

Nothing here imports the api layer or any cloud SDK: it is config_service and settings
only, so the worker pays nothing to import it.
"""
import os

from ..config import settings
from . import config_service


VALID_PROFILES = ("demo", "pov")

# Flags that belong to exactly ONE install profile. Anything absent from both tuples is
# profile-neutral (auth, notifications, secret scanning, the auto-delete timer, ...) and
# available in either kind of instance.
#
# The split is drawn around **which BeyondTrust tenant a feature would use**, not around
# taste. Every demo-only entry below either provisions demo infrastructure or reaches for
# the global `bt_*` / `pscli_*` / `entitle_*` singletons. A POV instance resolves tenants
# from a registry instead, so letting these run there is how a demo deploy ends up
# onboarding into a customer's Password Safe -- silently, because both paths "work".
_DEMO_ONLY = (
    "vmware_enabled",
    "portainer_enabled",
    "epml_enabled",
    # On-prem hypervisors. Listed here because their deploys and Web Jumps resolve the
    # global PRA tenant. NOTE for a later slice: the plan floats reusing a Proxmox/vSphere
    # connection as an on-prem POV lab platform. That is a real option, and it needs this
    # entry revisited deliberately rather than quietly deleted -- the tenancy argument
    # above has to be answered first.
    "proxmox_enabled",
    "vsphere_enabled",
    "hyperv_enabled",
    "nutanix_enabled",
    "xcpng_enabled",
    "vdesktops_enabled",
    "cloud_database_enabled",
    "k8s_management_enabled",
    "cloud_functions_enabled",
    "cost_explorer_enabled",
)

# POV-only. `pra_enabled`, `password_safe_enabled`, `remote_agents_enabled`,
# `ansible_enabled` and the Entitle flags are deliberately NOT here: a POV instance needs
# all of them, so they are neutral rather than POV-owned.
_POV_ONLY = (
    "pov_environments_enabled",
    # Lab platforms. A demo instance has no use for one, and letting it hold Skytap
    # credentials would put customer-lab access on the instance that is deliberately
    # not the one doing customer work.
    "skytap_enabled",
    # The public cloud a POV may run on. Same argument as Skytap above, with more
    # force: this one puts a cloud CREDENTIAL on the instance, and a demo instance
    # already has its own.
    "pov_cloud_enabled",
)

_PROFILE_OF = {f: "demo" for f in _DEMO_ONLY}
_PROFILE_OF.update({f: "pov" for f in _POV_ONLY})

# Pages that belong to one profile but are NOT feature flags. The four cloud consoles and
# the image registry are gated on credential PRESENCE, not on a toggle -- which is how they
# survived the mask above and kept rendering on a POV instance, pointing at pages that can
# never hold data there (the wizard deliberately writes no cloud credentials on a POV; see
# api/setup.py `_apply_config`).
#
# They are here rather than in _DEMO_ONLY because a flag needs a default, and a
# `cloud_pages_enabled` defaulting to False would hide AWS on every demo instance that
# already exists while one defaulting to True would be a toggle nobody should ever turn
# off. A profile is not a preference, so this maps a NAME to its owning profile and reads
# nothing from config at all.
_PROFILE_PAGES = {"cloud_pages": "demo"}


def install_profile() -> str:
    """This instance's profile, defaulting to ``demo``.

    An unrecognised value resolves to ``demo`` rather than raising: the profile is read on
    the request path by :func:`enabled`, and a typo in one config row must not take the
    whole app down. It also means the mask can only ever fall back to *today's* behaviour.
    """
    raw = (config_service.get("install_profile") or settings.install_profile or "demo")
    raw = raw.strip().lower()
    return raw if raw in VALID_PROFILES else "demo"


def profile_masks(flag: str) -> bool:
    """Whether this instance's profile makes ``flag`` unavailable regardless of config."""
    owner = _PROFILE_OF.get(flag)
    return owner is not None and owner != install_profile()


def profile_page_allowed(name: str) -> bool:
    """Whether a profile-owned PAGE renders on this instance. See :data:`_PROFILE_PAGES`.

    The page equivalent of :func:`profile_masks`, and it has to obey the same rule that
    commit 28cfc67 established for flags: both the nav link and the route must resolve
    through one reader, or you get a link to a page that 404s -- or worse, a page with no
    link that still serves. ``main._profile_page_gate`` and :func:`flags` are those two
    readers, and this is the function they share.

    An unknown name is allowed, matching :func:`profile_masks`: a name nobody claims is
    profile-neutral, not forbidden.
    """
    owner = _PROFILE_PAGES.get(name)
    return owner is None or owner == install_profile()


def enabled(flag: str, default: bool = False) -> bool:
    """Resolve a feature flag. **The one place this happens.**

    Both readers must come through here or they drift: ``main._feature_gate`` decides
    whether a router 404s, and :func:`flags` decides whether the nav link and the Settings
    toggle render. A mask applied in only one of them yields a page you can see and cannot
    use, or the reverse -- which is exactly the shape of bug
    ``tests/test_cache_warmer_parity`` was written about.

    The mask only ever subtracts. A profile can refuse a feature; it can never turn one on
    that config left off.
    """
    if profile_masks(flag):
        return False
    return config_service.get_bool(flag, default)


def flags() -> dict:
    """Read feature flags from config_service (DB) with env-var fallback.
    Called per-request so wizard changes are visible without a restart."""
    return {
        "vmware_enabled":       enabled("vmware_enabled",        settings.vmware_enabled),
        "portainer_enabled":    enabled("portainer_enabled",     settings.portainer_enabled),
        "ansible_enabled":      enabled("ansible_enabled",       settings.ansible_enabled),
        "entitle_enabled":      enabled("entitle_enabled",       settings.entitle_enabled),
        # The three BeyondTrust products gate independently — a Password Safe-only
        # deployment should not render Gateway tabs or EPM-L sections it cannot use.
        "password_safe_enabled": enabled("password_safe_enabled", settings.password_safe_enabled),
        "pra_enabled":          enabled("pra_enabled",           settings.pra_enabled),
        "epml_enabled":         enabled("epml_enabled",          settings.epml_enabled),
        "proxmox_enabled":      enabled("proxmox_enabled",       settings.proxmox_enabled),
        "vsphere_enabled":      enabled("vsphere_enabled",       settings.vsphere_enabled),
        "hyperv_enabled":       enabled("hyperv_enabled",        settings.hyperv_enabled),
        "nutanix_enabled":      enabled("nutanix_enabled",       settings.nutanix_enabled),
        "xcpng_enabled":        enabled("xcpng_enabled",         settings.xcpng_enabled),
        "vdesktops_enabled":    enabled("vdesktops_enabled",     settings.vdesktops_enabled),
        "cloud_database_enabled": enabled("cloud_database_enabled", settings.cloud_database_enabled),
        "entitle_registration_enabled": enabled("entitle_registration_enabled", settings.entitle_registration_enabled),
        "k8s_management_enabled": enabled("k8s_management_enabled", settings.k8s_management_enabled),
        "cloud_functions_enabled": enabled("cloud_functions_enabled", settings.cloud_functions_enabled),
        "cost_explorer_enabled": enabled("cost_explorer_enabled", settings.cost_explorer_enabled),
        "remote_agents_enabled": enabled("remote_agents_enabled", settings.remote_agents_enabled),
        # POV environments. Masked off entirely on a demo instance — see _POV_ONLY.
        "pov_environments_enabled": enabled("pov_environments_enabled", settings.pov_environments_enabled),
        "skytap_enabled": enabled("skytap_enabled", settings.skytap_enabled),
        "pov_cloud_enabled": enabled("pov_cloud_enabled",
                                     settings.pov_cloud_enabled),
        "admission_control_enabled": enabled("admission_control_enabled", settings.admission_control_enabled),
        # Auto-delete timer — gates the Expires column on /inventory and the dashboard's
        # "expiring soon" warning. Deletion has its own second gate
        # (resource_expiry_enforce), read server-side only.
        "resource_expiry_enabled": enabled("resource_expiry_enabled", settings.resource_expiry_enabled),
        # Was missing, so Settings → Integrations rendered the Notifications toggle
        # permanently off: the switch saved fine, but its initial state is read from
        # /api/features and this key never reached it.
        "notifications_enabled": enabled("notifications_enabled", settings.notifications_enabled),
        # Entitle user-JIT Phase 4 UI affordances — surfaces the
        # "Request access" nav link + portal URL when both are configured.
        "entitle_user_jit_enabled":   enabled("entitle_user_jit_enabled", settings.entitle_user_jit_enabled),
        "entitle_request_portal_url": config_service.get("entitle_request_portal_url",   settings.entitle_request_portal_url),
        # Not a feature flag — a profile-owned page group. Shipped in this dict because
        # _nav_links.html already spreads it, so the nav link and main._profile_page_gate
        # keep reading the same answer. See _PROFILE_PAGES.
        "cloud_pages":          profile_page_allowed("cloud_pages"),
    }

def cloud_configured(cloud: str) -> bool:
    """Whether ``cloud`` has usable credentials.

    Extracted from :func:`feature_map` so callers that gate on "can I put a resource
    in this cloud?" — the managed Portainer/Rancher node routes, for one — answer that
    question the same way the tiles do, rather than each re-deriving it from a
    different key and disagreeing.
    """
    cloud = (cloud or "").strip().lower()
    if cloud == "aws":
        return bool(config_service.get("aws_access_key_id")
                    or os.environ.get("AWS_ACCESS_KEY_ID", ""))
    if cloud == "azure":
        return bool(
            (config_service.get("azure_client_id") or settings.azure_client_id)
            and (config_service.get("azure_subscription_id")
                 or settings.azure_subscription_id))
    if cloud == "gcp":
        return bool(config_service.get("gcp_project_id") or settings.gcp_project_id)
    if cloud == "oci":
        return bool(
            (config_service.get("oci_tenancy_ocid") or settings.oci_tenancy_ocid)
            and (config_service.get("oci_user_ocid") or settings.oci_user_ocid)
            and (config_service.get("oci_private_key") or settings.oci_private_key)
            and (config_service.get("oci_region") or settings.oci_region))
    return False


def feature_map() -> dict:
    """The map ``/api/features`` serves: raw flags plus the credential-presence checks.

    AWS/Azure/GCP/OCI are not gated by a feature flag — they are "configured" iff
    credentials are present, which is what hides their tiles on a bare install."""
    raw = flags()
    # AWS/Azure/GCP aren't gated by a feature flag — they're "configured" iff
    # credentials are present. The dashboard uses these to hide tiles on bare installs.
    #
    # …and on a POV instance they must ALSO answer to the page group, because a POV may
    # now hold one cloud's credentials for its lab platform. Without this, selecting a POV
    # cloud provider would sprout AWS tiles on the POV dashboard linking to /aws — which
    # still 404s, deliberately. That is the dead-tile shape `_PROFILE_PAGES` was added to
    # fix, arriving by a different route.
    #
    # `cloud_configured` itself stays honest: it answers "are there credentials", which is
    # exactly what the POV cloud adapter needs to know, and is a different question from
    # "should this instance show a console".
    consoles = profile_page_allowed("cloud_pages")
    aws_configured = consoles and cloud_configured("aws")
    azure_configured = consoles and cloud_configured("azure")
    gcp_configured = consoles and cloud_configured("gcp")
    # OCI is "configured" iff the API-key signing quad (tenancy + user + key +
    # region) is present — mirrors the AWS/Azure/GCP credential-presence check.
    oci_configured = consoles and cloud_configured("oci")
    # Portainer needs both the toggle AND a URL — enabled-but-unconfigured should
    # hide the dashboard tile rather than show a permanently "unavailable" one.
    portainer_configured = raw["portainer_enabled"] and bool(
        config_service.get("portainer_url") or settings.portainer_url
    )
    return {
        # Not a feature flag: the instance's profile, so the UI can say *why* a
        # demo-only integration is unavailable rather than showing a dead toggle.
        "install_profile": install_profile(),
        "pov_environments": raw["pov_environments_enabled"],
        "skytap":          raw["skytap_enabled"],
        "pov_cloud":       raw["pov_cloud_enabled"],
        "vmware":       raw["vmware_enabled"],
        # Named to match the Settings panel keys, so settings.html's flag map needs no
        # translation layer. There is deliberately no combined "beyondtrust" key: an
        # OR would tell a caller the integration is on when only one of three products
        # is, and every consumer wants a specific product.
        "password_safe": raw["password_safe_enabled"],
        "pra":          raw["pra_enabled"],
        "epml":         raw["epml_enabled"],
        "portainer":    raw["portainer_enabled"],
        # Distinct from the enabled toggle: the dashboard tile hides unless
        # Portainer is both enabled AND has a URL configured.
        "portainer_configured": portainer_configured,
        "ansible":      raw["ansible_enabled"],
        "entitle":      raw["entitle_enabled"],
        "aws":          aws_configured,
        "azure":        azure_configured,
        "gcp":          gcp_configured,
        "oci":          oci_configured,
        "proxmox":      raw["proxmox_enabled"],
        "vsphere":      raw["vsphere_enabled"],
        "hyperv":       raw["hyperv_enabled"],
        "nutanix":      raw["nutanix_enabled"],
        "xcpng":        raw["xcpng_enabled"],
        "cost":         raw["cost_explorer_enabled"],
        "admission":    raw["admission_control_enabled"],
        "cloud_database": raw["cloud_database_enabled"],
        "k8s_management": raw["k8s_management_enabled"],
        "cloud_functions": raw["cloud_functions_enabled"],
        "resource_expiry": raw["resource_expiry_enabled"],
        "remote_agents": raw["remote_agents_enabled"],
        "notifications": raw["notifications_enabled"],
    }
