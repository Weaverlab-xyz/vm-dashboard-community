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


def flags() -> dict:
    """Read feature flags from config_service (DB) with env-var fallback.
    Called per-request so wizard changes are visible without a restart."""
    return {
        "vmware_enabled":       config_service.get_bool("vmware_enabled",        settings.vmware_enabled),
        "portainer_enabled":    config_service.get_bool("portainer_enabled",     settings.portainer_enabled),
        "ansible_enabled":      config_service.get_bool("ansible_enabled",       settings.ansible_enabled),
        "entitle_enabled":      config_service.get_bool("entitle_enabled",       settings.entitle_enabled),
        # The three BeyondTrust products gate independently — a Password Safe-only
        # deployment should not render Gateway tabs or EPM-L sections it cannot use.
        "password_safe_enabled": config_service.get_bool("password_safe_enabled", settings.password_safe_enabled),
        "pra_enabled":          config_service.get_bool("pra_enabled",           settings.pra_enabled),
        "epml_enabled":         config_service.get_bool("epml_enabled",          settings.epml_enabled),
        "proxmox_enabled":      config_service.get_bool("proxmox_enabled",       settings.proxmox_enabled),
        "vsphere_enabled":      config_service.get_bool("vsphere_enabled",       settings.vsphere_enabled),
        "hyperv_enabled":       config_service.get_bool("hyperv_enabled",        settings.hyperv_enabled),
        "nutanix_enabled":      config_service.get_bool("nutanix_enabled",       settings.nutanix_enabled),
        "xcpng_enabled":        config_service.get_bool("xcpng_enabled",         settings.xcpng_enabled),
        "vdesktops_enabled":    config_service.get_bool("vdesktops_enabled",     settings.vdesktops_enabled),
        "cloud_database_enabled": config_service.get_bool("cloud_database_enabled", settings.cloud_database_enabled),
        "entitle_registration_enabled": config_service.get_bool("entitle_registration_enabled", settings.entitle_registration_enabled),
        "k8s_management_enabled": config_service.get_bool("k8s_management_enabled", settings.k8s_management_enabled),
        "cloud_functions_enabled": config_service.get_bool("cloud_functions_enabled", settings.cloud_functions_enabled),
        "cost_explorer_enabled": config_service.get_bool("cost_explorer_enabled", settings.cost_explorer_enabled),
        "remote_agents_enabled": config_service.get_bool("remote_agents_enabled", settings.remote_agents_enabled),
        "admission_control_enabled": config_service.get_bool("admission_control_enabled", settings.admission_control_enabled),
        # Auto-delete timer — gates the Expires column on /inventory and the dashboard's
        # "expiring soon" warning. Deletion has its own second gate
        # (resource_expiry_enforce), read server-side only.
        "resource_expiry_enabled": config_service.get_bool("resource_expiry_enabled", settings.resource_expiry_enabled),
        # Was missing, so Settings → Integrations rendered the Notifications toggle
        # permanently off: the switch saved fine, but its initial state is read from
        # /api/features and this key never reached it.
        "notifications_enabled": config_service.get_bool("notifications_enabled", settings.notifications_enabled),
        # Entitle user-JIT Phase 4 UI affordances — surfaces the
        # "Request access" nav link + portal URL when both are configured.
        "entitle_user_jit_enabled":   config_service.get_bool("entitle_user_jit_enabled", settings.entitle_user_jit_enabled),
        "entitle_request_portal_url": config_service.get("entitle_request_portal_url",   settings.entitle_request_portal_url),
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
    aws_configured = cloud_configured("aws")
    azure_configured = cloud_configured("azure")
    gcp_configured = cloud_configured("gcp")
    # OCI is "configured" iff the API-key signing quad (tenancy + user + key +
    # region) is present — mirrors the AWS/Azure/GCP credential-presence check.
    oci_configured = cloud_configured("oci")
    # Portainer needs both the toggle AND a URL — enabled-but-unconfigured should
    # hide the dashboard tile rather than show a permanently "unavailable" one.
    portainer_configured = raw["portainer_enabled"] and bool(
        config_service.get("portainer_url") or settings.portainer_url
    )
    return {
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
