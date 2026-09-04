r"""
Password Safe onboarding for the "Certificate" custom platform plugin.

The plugin makes an x.509 certificate a managed credential: the managed account holds the
PKCS#12 **passphrase**, and a Secrets Safe file secret holds the **bundle** that passphrase
opens. Password Safe is a registrar and broker, never a certificate authority — the plugin
generates the keypair in its own process, sends only a PKCS#10 CSR, and the CA never sees
the private key.

This module is the dashboard's half: it composes the certificate profile, resolves the
functional account and platform, and drives ``ps_resource_service.register_managed_system``
with ``method="certificate"``. The address grammar itself lives next door in
``ps_resource_service`` beside the other custom-plugin grammars, because that is where the
255-character cap and the registration path already are.

**Why every value rides a standard Password Safe field.** ``appsettings.json`` ships INSIDE
the ``.psplugin``, so its values are global to every managed system, cannot be changed
without repackaging, and on **Password Safe Cloud** cannot be reached at all. Deriving the
whole profile from the Network Address is what lets one installed plugin serve an ADCS
template, a GCP CA pool and a self-signed Entra credential at the same time — and it is the
only shape that works on a Cloud tenant, which is what this dashboard targets.

The two credentials ride ONE functional account, both fields split on the **last** colon:

    Username   <ca-account>:<bi-run-as-user>       CORP\svc-adcs-enroll:certauth-svc
    Password   <ca-secret>:<bi-api-key>            S0me:P@ssword:9f2c1b7e4a...

Splitting from the right is deliberate: a BeyondInsight username and an API registration
key contain no colon, but a certificate authority password may contain anything at all.

Nothing secret belongs in the address or the account name. Neither is a protected field and
both are visible anywhere Password Safe displays the object; CA names, templates, ARNs,
project ids, tenant ids and object ids are identifiers and belong there, passwords and keys
never do.
"""

import logging
import re
from typing import Optional
from urllib.parse import urlsplit

from . import ps_resource_service

logger = logging.getLogger(__name__)


class CertPSError(Exception):
    """Raised when Certificate-platform onboarding cannot proceed."""


def _cfg(key: str, default: str = "") -> str:
    try:
        from . import config_service
        val = config_service.get(key)
        if val not in (None, ""):
            return str(val)
    except Exception:
        pass
    from ..config import settings
    val = getattr(settings, key, None)
    return default if val in (None, "") else str(val)


def _cfg_bool(key: str, default: bool = False) -> bool:
    try:
        from . import config_service
        return config_service.get_bool(key, default)
    except Exception:
        from ..config import settings
        return bool(getattr(settings, key, default))


def enabled() -> bool:
    return _cfg_bool("cert_lab_enabled", False)


def default_biurl() -> str:
    """The BeyondInsight base URL the plugin calls to write the bundle.

    Falls back to the ORIGIN of the ps-cli API URL, which is the same tenant by
    construction — ``https://tenant/BeyondTrust/api/public/v3`` becomes
    ``https://tenant``. The plugin appends its own API path, so passing the full API URL
    here yields a doubled path and a 404 on the first credential change."""
    explicit = _cfg("cert_ps_biurl")
    if explicit:
        return explicit.rstrip("/")
    api_url = _cfg("pscli_api_url")
    if not api_url:
        return ""
    parts = urlsplit(api_url if "://" in api_url else f"https://{api_url}")
    return f"{parts.scheme}://{parts.netloc}" if parts.netloc else ""


def workgroup() -> str:
    return _cfg("cert_ps_workgroup") or _cfg("passwordsafe_workgroup")


# ── composing the address ─────────────────────────────────────────────────────
#
# Option order is fixed rather than incidental. The address is the one thing an
# administrator reads back in the console to see what a managed system does, and a stable
# order — backend identity, then what the certificate looks like, then where the bundle
# goes, then who is told about it — makes two systems comparable at a glance. It also makes
# the composed string deterministic, so a re-registration produces a byte-identical address
# and a diff means a real change.
_OPTION_ORDER = (
    # backend identity
    "ca", "template", "impersonate", "validate",
    "arn", "region", "sigalg", "templatearn", "wait",
    "project", "location", "pool", "issuer", "certtemplate",
    # what the certificate is
    "lifetime", "key", "keysize", "curve", "hash", "subject", "dns", "ip", "san", "eku",
    "bundle", "pbe", "warn", "warndays", "warnminutes",
    # where the bundle goes
    "store", "biurl", "folder", "secret", "owner",
    # who is told about it
    "publisher", "tenant", "appid", "spid", "retain",
)

# A value carrying one of these would silently redraw the address's own structure — '&'
# and ';' start a new option, '=' shifts the key/value boundary, '?' a second profile.
# The plugin never percent-decodes, so there is no escape available: the only correct
# answer is to refuse the value here, where it can still be retyped.
_ADDRESS_METACHARS = ("&", ";", "?")


def compose_address(backend: str, options: dict) -> str:
    """Build a certificate profile address from a backend name and an option mapping.

    Blank values are DROPPED rather than emitted empty, which is what keeps the address
    inside its 255-character budget: an option left at its default costs nothing when it
    is absent and 10-30 characters when it is spelled out."""
    backend = (backend or "").strip()
    if not backend:
        raise CertPSError(
            "a certificate profile needs a backend — adcs, awspca, gcpcas or selfsigned")

    clean: dict = {}
    for key, value in (options or {}).items():
        key = ps_resource_service._CERT_ALIASES.get(
            (key or "").strip().lower(), (key or "").strip().lower())
        text = "" if value is None else str(value).strip()
        if not text:
            continue
        bad = [c for c in _ADDRESS_METACHARS if c in text]
        if bad:
            raise CertPSError(
                f"the value for {key}= contains {' and '.join(repr(c) for c in bad)}, which "
                f"the address grammar reads as a separator. Values are never "
                f"percent-decoded, so there is no way to escape it — use a value without it.")
        clean[key] = text

    ordered = [k for k in _OPTION_ORDER if k in clean]
    ordered += sorted(k for k in clean if k not in _OPTION_ORDER)
    if not ordered:
        return backend
    return backend + "?" + "&".join(f"{k}={clean[k]}" for k in ordered)


def store_options() -> dict:
    """The Secrets Safe destination every profile shares, from config."""
    return {"biurl": default_biurl(),
            "folder": _cfg("cert_ps_folder", "Certificates"),
            "secret": _cfg("cert_ps_secret_template"),
            "owner": _cfg("cert_ps_owner_group_id")}


def profile_defaults() -> dict:
    """Certificate-shape defaults from config. Blank means 'let the plugin default it',
    which is also the shortest address."""
    return {"lifetime": _cfg("cert_default_lifetime"),
            "key": _cfg("cert_default_key"),
            "eku": _cfg("cert_default_eku"),
            "warn": _cfg("cert_default_warn"),
            "subject": _cfg("cert_default_subject")}


def build_address(backend: str, backend_options: dict,
                  overrides: Optional[dict] = None) -> str:
    """The composer callers should use: config defaults, then the backend's own required
    options, then whatever the form overrode. Validated before it is returned, so a bad
    profile fails here rather than at the first scheduled rotation."""
    options = profile_defaults()
    options.update(store_options())
    options.update(backend_options or {})
    options.update(overrides or {})
    address = compose_address(backend, options)
    ps_resource_service._validate_certificate_dns_name(address)
    ps_resource_service._check_address_length(address, "certificate")
    return address


def address_preview(backend: str, backend_options: dict,
                    overrides: Optional[dict] = None) -> dict:
    """Compose without raising, for a UI that wants to show the address and its length
    while it is still being edited. ``error`` is None when the profile is valid."""
    try:
        address = build_address(backend, backend_options, overrides)
        error = None
    except (CertPSError, ps_resource_service.PSResourceError) as exc:
        try:
            address = compose_address(backend, {**profile_defaults(), **store_options(),
                                                **(backend_options or {}),
                                                **(overrides or {})})
        except Exception:
            address = ""
        error = str(exc)
    return {"address": address, "length": len(address),
            "limit": ps_resource_service._MAX_MANAGED_SYSTEM_ADDRESS, "error": error}


# ── the two Password Safe objects ─────────────────────────────────────────────

# Substring tokens a "Certificate" platform's name must contain. Matched the same way
# ps_vm_hook does — all tokens, not a contiguous phrase — because a Password Safe admin
# renaming an imported plugin platform is a real event that has silently switched
# onboarding off before ("Azure VM SSH Rotation" -> "Azure Waagent VM SSH Rotation").
_PLATFORM_TOKENS = ("certificate",)


async def resolve_functional_account(name: str = "") -> dict:
    """The functional account carrying BOTH credentials, with its platform checked.

    The managed system inherits its platform from the functional account, so an account on
    the wrong platform onboards green and then fails every credential action."""
    from . import ps_api_service, ps_vm_hook
    name = (name or _cfg("cert_ps_functional_account")).strip()
    if not name:
        raise CertPSError(
            "no Password Safe functional account is configured for the Certificate "
            "platform — set cert_ps_functional_account. It carries two credentials on one "
            "account: name '<ca-account>:<bi-run-as-user>', password "
            "'<ca-secret>:<bi-api-key>', both split on the LAST colon.")
    fa = await ps_api_service.get_functional_account(name)
    pname = fa.get("platform_name") or ""
    if pname and not ps_vm_hook._platform_name_ok(pname, *_PLATFORM_TOKENS):
        raise CertPSError(
            f"functional account {name!r} is on platform {pname!r}, which is not a "
            f"Certificate platform — the managed system inherits the functional account's "
            f"platform, so this would onboard against the wrong plugin")
    if ":" not in name:
        # Legal, but only for an on-premises administrator with filesystem access on the
        # plugin host: without the second half the Secrets Safe connection has to come
        # from appsettings.json, which a Cloud tenant cannot supply.
        logger.warning(
            "PS: certificate functional account %r carries no ':' — the BeyondInsight API "
            "user is missing, so the plugin can only reach Secrets Safe through "
            "appsettings.json. On a Password Safe Cloud tenant the first credential change "
            "will fail with FailedCredentials.", name)
    return fa


async def ensure_secrets_safe_folder(folder_path: str = "") -> dict:
    """Create the Secrets Safe folder the bundles land in, if it is not already there.

    **The plugin deliberately creates nothing** and fails with "Secrets Safe folder '...'
    was not found" — scattering certificate bundles into an unexpected folder with
    unexpected permissions is worse than a clear error. So the folder is the dashboard's
    job, and it is the one piece of Secrets Safe state onboarding has to establish.

    ``folder_path`` is ``<safe>/<folder>/<folder>``: the FIRST segment names an existing
    **safe**, and everything after it is a folder tree created beneath it. A safe is not
    created here on purpose — it carries its own ACL, and the Secrets Safe folder's
    permissions are half the access boundary on the certificate (the managed account's
    access policy is the other half; the weaker of the two is the real one).

    Returns ``{"folder_id", "created", "path"}``. Idempotent."""
    import asyncio
    from . import secrets_backend_service
    path = (folder_path or _cfg("cert_ps_folder", "Certificates")).strip().strip("/")
    segments = [s.strip() for s in path.split("/") if s.strip()]
    if not segments:
        raise CertPSError("no Secrets Safe folder configured — set cert_ps_folder")

    safes = await asyncio.to_thread(secrets_backend_service.list_bt_safes)
    safe = next((s for s in (safes or [])
                 if (s.get("name") or "").casefold() == segments[0].casefold()), None)
    if not safe:
        known = ", ".join(sorted((s.get("name") or "") for s in (safes or []))) or "none"
        raise CertPSError(
            f"Secrets Safe has no safe named {segments[0]!r} — the first segment of "
            f"cert_ps_folder names an existing safe. Create it under Secrets → Safes and "
            f"grant the run-as user write access, then re-run; the folder tree beneath it "
            f"is created for you. Safes visible to the API user: {known}")

    parent_id, created = str(safe.get("id") or ""), []
    for segment in segments[1:]:
        # Re-read each round: a folder created a moment ago has to be visible before its
        # own child can be parented to it, and `folders list` is unscoped, so matching on
        # BOTH name and parent_id is what keeps two same-named folders in different safes
        # from being confused for one another.
        folders = await asyncio.to_thread(secrets_backend_service.list_bt_folders)
        match = next((f for f in (folders or [])
                      if (f.get("name") or "").casefold() == segment.casefold()
                      and str(f.get("parent_id") or "") == parent_id), None)
        if match:
            parent_id = str(match.get("id") or "")
            continue
        made = await asyncio.to_thread(
            secrets_backend_service.create_bt_folder, parent_id, segment)
        new_id = str((made or {}).get("id") or "")
        if not new_id:
            raise CertPSError(
                f"Secrets Safe accepted the creation of folder {segment!r} but returned no "
                f"id, so its children cannot be parented — check the run-as user's access "
                f"to the {segments[0]!r} safe")
        parent_id, _ = new_id, created.append(segment)
    return {"folder_id": parent_id, "created": created, "path": path}


async def register(*, system_name: str, account_name: str, address: str,
                   functional_account: str = "",
                   ensure_folder: bool = True) -> dict:
    """Onboard one certificate identity: a managed system carrying the profile, and one
    managed account that becomes the certificate.

    Returns ``{managed_system_id, managed_account_id, tf_state_json, address, folder}``.

    **One managed account per certificate identity, and — with an Entra publisher — one
    per app registration.** Graph's PATCH replaces the whole ``keyCredentials`` collection,
    so the publisher reads the existing entries and carries them forward; two rotations
    against the same app registration can each read the collection and clobber the other's
    key. Password Safe serialises rotations per managed account, so that mapping is what
    makes it safe, and it is easy to break by accident when copying a platform instance."""
    from . import ps_api_service

    if not system_name or not account_name:
        raise CertPSError("a certificate identity needs both a system name and an account "
                          "name — the account name becomes the subject CN by default")

    # Validate before touching anything: a rejected address costs nothing here and a
    # Secrets Safe folder created for a registration that then fails is litter.
    ps_resource_service._validate_certificate_dns_name(address)
    ps_resource_service._check_address_length(address, "certificate")

    fa = await resolve_functional_account(functional_account)
    platform_id = await ps_api_service.get_platform_id(_cfg("cert_ps_platform", "Certificate"))
    workgroup_id = await ps_api_service.get_workgroup_id(workgroup())

    # Only for the SecretsSafe store — `store=FileSystem` is the harness path and writes
    # nowhere a folder would help.
    options = ps_resource_service.parse_certificate_address(address)["options"]
    folder = None
    if ensure_folder and (options.get("store") or "SecretsSafe").lower() != "filesystem":
        folder = await ensure_secrets_safe_folder(options.get("folder") or "")

    reg = await ps_resource_service.register_managed_system(
        name=system_name, host_name=system_name,
        functional_account_id=fa["id"], platform_id=platform_id,
        workgroup_id=workgroup_id, ip_address="127.0.0.1", port=0,
        managed_account_name=account_name, method="certificate", dns_name=address)
    # No `initial_password`, deliberately: the credential is a PKCS#12 passphrase Password
    # Safe generates from the account's password policy and hands to the plugin on the
    # first Change Password. Until that runs the account holds a placeholder that opens
    # nothing, and Test password correctly FAILS with "no bundle exists" — which is step 1
    # of the demonstration, not a fault.
    reg["address"] = address
    reg["folder"] = folder
    logger.info("PS: registered certificate identity %s/%s (system %s, account %s)",
                system_name, account_name, reg.get("managed_system_id"),
                reg.get("managed_account_id"))
    return reg


async def deregister(tf_state_json: str) -> None:
    """Remove a managed system + account this module registered (best-effort).

    Leaves the Secrets Safe folder and any bundles in it alone. A certificate that was
    issued still exists at the CA until it expires — the plugin does not consult CRLs and
    neither does this — so deleting the record of it silently would be the wrong default."""
    await ps_resource_service.deregister(tf_state_json)
