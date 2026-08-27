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

VALID_PLATFORMS = ("skytap",)

# adapter module name, relative to this package.
_ADAPTER_MODULE = {
    "skytap": "skytap_service",
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
)

# What each platform can actually do. This table is the part that keeps the abstraction
# honest: where a platform lacks something the feature must degrade **explicitly and say
# so**, rather than failing late with a confusing error.
#
# `bootstrap_injection` is one INTENT with different mechanisms, which is why it is an enum
# rather than a boolean:
#   "metadata"    - the platform hands data to the guest and the guest fetches it
#                   (Skytap: per-VM user_data, read at http://169.254.169.254/skytap)
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
        "bootstrap_injection": "metadata",
        "share_link": True,            # publish_sets: password + expiration_date
        "stored_credentials": True,    # …/vms/{id}/credentials
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
