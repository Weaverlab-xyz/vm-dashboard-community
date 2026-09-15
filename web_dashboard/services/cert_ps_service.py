r"""
Password Safe onboarding for the Certificate custom platform plugin family.

The plugin makes an x.509 certificate a managed credential: the managed account holds the
PKCS#12 **passphrase**, and a Secrets Safe file secret holds the **bundle** that passphrase
opens. Password Safe is a registrar and broker, never a certificate authority — the plugin
generates the keypair in its own process, sends only a PKCS#10 CSR, and the CA never sees
the private key.

**There are TWO packages, and therefore two platforms.** The family ships as two
``.psplugin`` files over a shared core assembly, with different plugin ids, so both
install side by side and neither overwrites the other:

    "Certificate"     an end-entity certificate  — all nine backends
    "Subordinate CA"  a subordinate authority    — the four that can sign one

That split is the point rather than packaging tidiness. A subordinate CA is a *delegation
of issuing authority*, not one more credential, so the team that may request a leaf should
not automatically be the team that may request an issuer — and two platforms express that
in Password Safe's own access model instead of by convention. The two are the same code;
what differs is a ``PluginMetadata`` attribute, a small declarative profile, and two
defaults (``isca`` and ``bundle``), so they cannot drift apart in behaviour.

The practical consequence for this module is that **a functional account is platform-bound
and a managed system inherits its platform**, so one CA serving both packages needs one
functional account per package. Hence ``package`` on nearly every function here, and the
second pair of columns on ``CertLab``.

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

The two credentials ride ONE functional account, and there are two ways the BeyondInsight
half arrives. The plugin reads whichever it is given and **prefers the first**:

    OAuth (preferred), nothing packed — four values in four fields:
      Username   <ca-account>            CORP\svc-adcs-enroll
      Password   <ca-secret>             S0me:P@ssword
      API key    <bi-oauth-client-id>
      API secret <bi-oauth-client-secret>

    Packed (fallback), both fields split on the LAST colon:
      Username   <ca-account>:<bi-run-as-user>   CORP\svc-adcs-enroll:certauth-svc
      Password   <ca-secret>:<bi-api-key>        S0me:P@ssword:9f2c1b7e4a...

``ECredentialType`` is a flags enum and ``CredentialParameter`` carries ``ApiKey`` and
``ApiSecret`` as fields of their own, so one account can be ``Password, ApiKey`` at once.
That path removes three things: no run-as user has to exist, nothing is packed — so a CA
secret *or a CA account name* may contain a colon, which was not expressible before — and
the plugin presents a short-lived bearer token instead of a long-lived static key.

On the packed path, splitting from the right is deliberate: a BeyondInsight username and an
API registration key contain no colon, but a certificate authority password may contain
anything at all. It is kept because whether the Password Safe console offers an API key
credential type for a **plugin-supplied** platform is unverified; ``cert_ps_bi_auth`` pins
either path for an operator who has found out.

``ensure_functional_account`` composes that account during the CA build, because the CA
half is returned exactly once — by the apply — and exists nowhere else afterwards.
``reference`` mode keeps the operator-maintained account, which is what a CA this
dashboard did not build needs.

Nothing secret belongs in the address or the account name. Neither is a protected field and
both are visible anywhere Password Safe displays the object; CA names, templates, ARNs,
project ids, tenant ids and object ids are identifiers and belong there, passwords and keys
never do.
"""

import json
import logging
import re
from typing import Optional
from urllib.parse import urlsplit

from . import ps_resource_service
from .ps_resource_service import (CERT_PACKAGE_LEAF, CERT_PACKAGE_SUBCA, CERT_PACKAGES,
                                  cert_normalise_package)

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
    # backend identity. `url=` leads the five service backends for the same reason `ca=`
    # leads ADCS: it is the thing an operator looks for first when reading the address
    # back to work out WHICH certificate authority a managed system talks to.
    "ca", "template", "impersonate", "validate",
    "url", "label", "profile", "subcaprofile", "eeprofile", "issuerdn",
    "mount", "role", "auth", "approlemount", "fingerprint", "insecure",
    "arn", "region", "sigalg", "templatearn", "wait",
    "project", "location", "pool", "issuer", "certtemplate",
    # what KIND of thing is being issued — first, because isca= changes the meaning of
    # everything after it, and an operator reading the address back should see that
    # before the certificate's shape rather than appended at the tail
    "isca", "pathlen", "permitdns", "permitemail", "permitip", "excludedns",
    # what the certificate is
    "lifetime", "key", "keysize", "curve", "hash", "subject", "dns", "ip", "san", "eku",
    "bundle", "pbe", "warn", "warndays", "warnminutes",
    # where the bundle goes
    "store", "biurl", "folder", "secret", "owner",
    # who is told about it, and what happens to what it replaces
    "publisher", "tenant", "appid", "spid", "retain",
    "revokeondisable", "revokeonrenewal",
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
            "a certificate profile needs a backend — one of adcs, est, ejbca, vaultpki, "
            "stepca, digicert, sectigo, awspca, gcpcas or selfsigned. Neither package "
            "ships a default, deliberately: an unset value fails with a configuration "
            "error rather than silently falling back to the self-signed test CA")

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


def profile_defaults(package: str = CERT_PACKAGE_LEAF) -> dict:
    """Certificate-shape defaults from config. Blank means 'let the plugin default it',
    which is also the shortest address.

    ``subject`` and ``eku`` are dropped on the subordinate package. A subject template is
    per-identity and a subordinate has no EKU at all — see ``build_address``."""
    defaults = {"lifetime": _cfg("cert_default_lifetime"),
                "key": _cfg("cert_default_key"),
                "eku": _cfg("cert_default_eku"),
                "warn": _cfg("cert_default_warn"),
                "subject": _cfg("cert_default_subject")}
    if cert_normalise_package(package) == CERT_PACKAGE_SUBCA:
        # A subordinate's own lifetime is the exposure window after a key leak, and it is
        # sized against the ROTATION INTERVAL rather than against a leaf's cadence. The
        # leaf default (24h out of the box) is meaningless here and a 1y one would be
        # actively wrong, so the subordinate takes its own key and falls through to the
        # plugin's default when that is unset.
        defaults["lifetime"] = _cfg("cert_subca_default_lifetime")
        defaults.pop("eku", None)
    return defaults


def build_address(backend: str, backend_options: dict,
                  overrides: Optional[dict] = None,
                  package: str = CERT_PACKAGE_LEAF) -> str:
    """The composer callers should use: config defaults, then the backend's own required
    options, then whatever the form overrode. Validated before it is returned, so a bad
    profile fails here rather than at the first scheduled rotation.

    ``package`` decides which platform this address is destined for, and with it what
    ``isca=`` and ``bundle=`` already default to — which is why a subordinate profile
    composed here normally carries NEITHER. Saying what the package already says costs 10
    characters for ``isca=true`` and 17 for ``bundle=PemBundle``, out of a 255-character
    budget the safety-critical option (``permitdns=``) is competing for."""
    package = cert_normalise_package(package)
    options = profile_defaults(package)
    options.update(store_options())
    options.update(backend_options or {})
    options.update(overrides or {})

    # A subordinate CA carries no extended key usage — the plugin asserts none, because an
    # EKU on a CA certificate constrains everything issued beneath it. The validator
    # refuses eku= on a subordinate for that reason, and rightly: on a hand-typed address
    # it is a visible mistake. But `cert_default_eku` is a CONFIG default that applies to
    # every profile, so left alone it would block the sub-CA path with an option the
    # operator never chose for it. Drop it from the defaults layer only — an eku= passed
    # explicitly in backend_options or overrides still reaches the validator and is still
    # refused, because there it is a real contradiction.
    issuing_ca = (str(options.get("isca", "")).strip().lower() == "true"
                  or (package == CERT_PACKAGE_SUBCA
                      and str(options.get("isca", "")).strip().lower() != "false"))
    if issuing_ca and "eku" not in (backend_options or {}) \
            and "eku" not in (overrides or {}):
        options.pop("eku", None)
    # Same reasoning, one layer up: the package IS the isca default, so emitting it again
    # spends budget to restate it. Only dropped when it agrees with the package — an
    # explicit isca=false on the subordinate package is a real override and stays.
    if package == CERT_PACKAGE_SUBCA and \
            str(options.get("isca", "")).strip().lower() == "true":
        options.pop("isca", None)

    address = compose_address(backend, options)
    ps_resource_service._validate_certificate_dns_name(address, package)
    ps_resource_service._check_address_length(address, "certificate")
    return address


def address_preview(backend: str, backend_options: dict,
                    overrides: Optional[dict] = None,
                    package: str = CERT_PACKAGE_LEAF) -> dict:
    """Compose without raising, for a UI that wants to show the address and its length
    while it is still being edited. ``error`` is None when the profile is valid."""
    package = cert_normalise_package(package)
    try:
        address = build_address(backend, backend_options, overrides, package)
        error = None
    except (CertPSError, ps_resource_service.PSResourceError) as exc:
        try:
            address = compose_address(backend, {**profile_defaults(package),
                                                **store_options(),
                                                **(backend_options or {}),
                                                **(overrides or {})})
        except Exception:
            address = ""
        error = str(exc)
    return {"address": address, "length": len(address),
            "limit": ps_resource_service._MAX_MANAGED_SYSTEM_ADDRESS,
            "package": package, "platform": platform_name(package), "error": error}


# ── the two Password Safe objects ─────────────────────────────────────────────

# Substring tokens each package's platform name must contain. Matched the same way
# ps_vm_hook does — all tokens, not a contiguous phrase — because a Password Safe admin
# renaming an imported plugin platform is a real event that has silently switched
# onboarding off before ("Azure VM SSH Rotation" -> "Azure Waagent VM SSH Rotation").
#
# Per package, and "subordinate" rather than "certificate" for the second one, because
# the platform is called **Subordinate CA** and does not contain the word "certificate"
# at all. A single ("certificate",) here would reject every functional account on the new
# platform as being on the wrong one — a check meant to catch a renamed platform
# rejecting the correctly-named one.
_PLATFORM_TOKENS = {
    CERT_PACKAGE_LEAF: ("certificate",),
    CERT_PACKAGE_SUBCA: ("subordinate",),
}
# Per package: the config key naming its platform, that platform's out-of-the-box name,
# and the config key naming its reference-mode functional account.
#
# Separate keys rather than one with a suffix rule: an operator who renamed one platform
# has not necessarily renamed the other, and deriving the second name from the first would
# invent a name nobody chose.
_PLATFORM_CONFIG = {
    CERT_PACKAGE_LEAF: ("cert_ps_platform", "Certificate",
                        "cert_ps_functional_account"),
    CERT_PACKAGE_SUBCA: ("cert_ps_subca_platform", "Subordinate CA",
                         "cert_ps_subca_functional_account"),
}


def platform_name(package: str = CERT_PACKAGE_LEAF) -> str:
    """The Password Safe platform name for a package, resolved live through
    ``GET /Platforms`` by the caller — so a renamed platform needs only its new name in
    config."""
    key, default, _ = _PLATFORM_CONFIG[cert_normalise_package(package)]
    return _cfg(key, default)


def package_label(package: str = CERT_PACKAGE_LEAF) -> str:
    """What to call a package in a message aimed at a human."""
    return ("Subordinate CA" if cert_normalise_package(package) == CERT_PACKAGE_SUBCA
            else "Certificate")


_FA_MODE_REFERENCE = "reference"


def functional_account_mode() -> str:
    """``create`` (mint one per CA) or ``reference`` (an operator names one).

    Normalised to exactly one of those two, and anything that is not ``reference`` is
    ``create`` — the same shape the cloud-DB onboarding uses. A typo in the mode must
    not silently disable the thing that makes the feature work, and callers comparing
    against a raw config string would each have to re-decide that."""
    val = _cfg("cert_ps_functional_account_mode", "create").strip().lower()
    return _FA_MODE_REFERENCE if val == _FA_MODE_REFERENCE else "create"


def bi_run_as_user() -> str:
    """The BeyondInsight run-as user that is the USERNAME's second half.

    Falls back to ``pscli_api_account_name`` — the run-as user this install already
    configured for the Password Safe terraform provider. It is the same tenant and
    almost always the same identity, so asking for it twice invites them to drift.

    Used by the PACKED path only. The OAuth one has no run-as user at all, which is one
    of the three things it removes."""
    return _cfg("cert_ps_bi_run_as_user") or _cfg("pscli_api_account_name")


BI_AUTH_OAUTH = "oauth"
BI_AUTH_APIKEY = "apikey"


def bi_client_credentials() -> tuple:
    """``(client_id, client_secret, source_key)`` for the BeyondInsight OAuth registration
    the plugin authenticates to Secrets Safe with, or ``("", "", "")``.

    A dedicated ``cert_ps_bi_client_id``/``_secret`` wins, and falls back to the
    ``pscli_*`` pair the dashboard signs itself in with — same tenant by construction, so
    an install that has configured Password Safe at all already has a working pair and
    needs nothing new to reach the OAuth path. Prefer the dedicated one in anything that
    matters: this registration is handed to a plugin running on a Resource Broker and only
    needs write on one Secrets Safe folder, where the dashboard's own is the client it
    administers the whole tenant with.

    **Half a dedicated pair raises rather than falling through.** Falling through would
    silently authenticate as a DIFFERENT registration than the one named in config, and
    the only visible symptom would be a permission error on a folder the operator believes
    they granted."""
    cid, secret = _cfg("cert_ps_bi_client_id").strip(), _cfg("cert_ps_bi_client_secret")
    if cid and secret:
        return cid, secret, "cert_ps_bi_client_id"
    if cid or secret:
        raise CertPSError(
            "only half of the Certificate Lab's BeyondInsight OAuth registration is set "
            "— cert_ps_bi_client_id and cert_ps_bi_client_secret are a pair. Set both, or "
            "clear both to fall back to the dashboard's own pscli_client_id/secret")
    cid, secret = _cfg("pscli_client_id").strip(), _cfg("pscli_client_secret")
    return (cid, secret, "pscli_client_id") if cid and secret else ("", "", "")


def bi_auth_mode() -> str:
    """``oauth`` | ``apikey`` | ``auto`` — which BeyondInsight credential a MINTED
    functional account carries. Normalised; anything unrecognised is ``auto``.

    The plugin reads both and prefers OAuth, so this exists for the one thing the
    dashboard cannot see from here: whether the Password Safe console will accept an API
    key credential type on a plugin-supplied platform at all. Where it will not, the
    account has to carry the packed registration key instead, and that is what ``apikey``
    pins. ``oauth`` pins the other way, for an operator who has verified it."""
    val = _cfg("cert_ps_bi_auth", "auto").strip().lower()
    return val if val in (BI_AUTH_OAUTH, BI_AUTH_APIKEY) else "auto"


def resolve_bi_credential() -> dict:
    """Which BeyondInsight identity the minted functional account will carry.

    Returns ``{"auth": "oauth"|"apikey", "client_id", "client_secret", "api_key",
    "run_as", "source"}`` — the unused half of which is always "".

    The ``auto`` ladder is ordered so that **an install that works keeps working**. An
    explicitly configured ``cert_ps_bi_api_key`` outranks the *inherited* ``pscli_*``
    pair, because it was set by hand for the packed path and the console's support for an
    API-key functional account on a plugin platform is unverified — flipping a working
    install to an unproven path on an upgrade is exactly the silent regression this
    feature cannot afford, since a failed credential action surfaces hours later at a
    rotation. A dedicated ``cert_ps_bi_client_id`` outranks it in turn: nobody sets that
    by accident.

        1. cert_ps_bi_client_id + secret          → oauth   (chosen for this feature)
        2. cert_ps_bi_api_key + a run-as user     → apikey  (the configured packed path)
        3. pscli_client_id + secret               → oauth   (inherited, needs no setup)
    """
    mode = bi_auth_mode()
    api_key, run_as = _cfg("cert_ps_bi_api_key"), bi_run_as_user()

    def _packed():
        return {"auth": BI_AUTH_APIKEY, "client_id": "", "client_secret": "",
                "api_key": api_key, "run_as": run_as, "source": "cert_ps_bi_api_key"}

    if mode == BI_AUTH_APIKEY:
        # Resolved before the OAuth pair is even looked at: an operator who pinned the
        # packed path has said the API-key credential type is not available to them, and
        # a stray half-set client id must not stand in the way of the path they pinned.
        if not api_key:
            raise CertPSError(
                "cert_ps_bi_auth is 'apikey', but no BeyondInsight API key is configured "
                "— set cert_ps_bi_api_key, or switch cert_ps_bi_auth to 'oauth'")
        if not run_as:
            raise CertPSError(_NO_RUN_AS)
        return _packed()

    cid, secret, source = bi_client_credentials()

    def _oauth():
        return {"auth": BI_AUTH_OAUTH, "client_id": cid, "client_secret": secret,
                "api_key": "", "run_as": "", "source": source}

    if mode == BI_AUTH_OAUTH:
        if not cid:
            raise CertPSError(
                "cert_ps_bi_auth is 'oauth', but no BeyondInsight OAuth registration is "
                "configured — set cert_ps_bi_client_id and cert_ps_bi_client_secret, or "
                "configure the dashboard's own pscli_client_id/secret, which this falls "
                "back to")
        return _oauth()

    if source == "cert_ps_bi_client_id":
        return _oauth()
    if api_key:
        if not run_as:
            raise CertPSError(_NO_RUN_AS)
        return _packed()
    if cid:
        return _oauth()
    raise CertPSError(
        "no BeyondInsight credential is configured for the plugin to write the PKCS#12 "
        "bundle into Secrets Safe. Either set cert_ps_bi_client_id and "
        "cert_ps_bi_client_secret — an API registration permitting the client-credentials "
        "grant, which the functional account carries in its API key and secret fields — "
        "or set cert_ps_bi_api_key for the older packed form")


_NO_RUN_AS = ("no BeyondInsight run-as user is configured — set cert_ps_bi_run_as_user "
              "or pscli_api_account_name. It is the second half of the functional "
              "account's username on the packed path; the OAuth one needs no run-as user "
              "at all")


def _ca_credential(cloud: str, outputs: dict) -> tuple:
    """``(principal, secret)`` — the CA half of the two credentials, per cloud.

    The two modules deliberately do not name these the same way (see
    ``cert_lab_service._read_outputs`` for why), and the SHAPES differ too: AWS's secret
    output is the password half already, while GCP's is a JSON key file that merely
    CONTAINS it. Pasting that whole file is the most common setup mistake there is, so
    the extraction happens here, once, rather than in anybody's fingers."""
    if (cloud or "").lower() == "aws":
        return (str(outputs.get("enroll_access_key_id") or ""),
                str(outputs.get("enroll_secret_access_key") or ""))

    email = str(outputs.get("service_account_email") or "")
    raw = outputs.get("service_account_key_json") or ""
    if not raw:
        return email, ""
    try:
        key = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError as exc:
        raise CertPSError(
            "the CA build's service account key is not JSON, so the private key cannot "
            f"be taken out of it: {exc}") from exc
    private_key = str(key.get("private_key") or "")
    if not private_key.startswith("-----BEGIN"):
        raise CertPSError(
            "the service account key JSON has no `private_key` field — the functional "
            "account password is that FIELD, PEM armour and all, never the whole file")
    return email, private_key


async def ensure_functional_account(row, outputs: dict,
                                    package: str = CERT_PACKAGE_LEAF) -> dict:
    """Mint the functional account carrying BOTH of the plugin's credentials.

    Returns ``{"mode", "package", "account_name", "id"}`` — plus ``auth`` when one was
    minted. ``id`` is None in reference mode, where nothing is created and the operator's
    own ``cert_ps_functional_account`` still applies.

    **Two shapes, because there are two ways the BeyondInsight half can arrive** and the
    plugin reads whichever it is given, preferring the first (``resolve_bi_credential``
    picks):

        oauth   name = the CA account, password = the CA secret, both WHOLE, and the
                OAuth client id/secret in the account's API key and secret fields
        apikey  name = <ca-account>:<run-as>, password = <ca-secret>:<api-key>, each
                split by the plugin on its LAST colon

    The account NAME therefore differs between them, which matters on a re-wire: an
    account minted on one path is not the same object as one minted on the other, so
    switching path on a CA that already has one leaves the first behind (see
    ``cert_lab_service.rewire_functional_account``, which says so).

    **One per package, because a functional account is platform-bound.** A managed system
    inherits its functional account's platform, so an account on "Certificate" cannot
    carry a managed system on "Subordinate CA" — it would onboard green and then fail
    every credential action. The CA credential is the same one either way; what differs
    is the platform the account is created on. That is also why the two cannot be
    collapsed: the whole reason for two platforms is that their access control is
    separable, and one shared functional account would put both back under one grant.

    **This has to happen during the CA build.** The enrollment credential exists in the
    apply's outputs and nowhere else a human can reach: the GCP key is returned once by
    the API and the AWS secret access key likewise, so by the time an operator opens
    BeyondInsight the only copies left are the remote terraform state and this dict.
    Minting it here is what removes the manual step, and it is also the only path that
    works — ``ps-cli`` caps a functional-account password at 1,000 characters and a GCP
    ``private_key`` PEM is about 1,700, while the REST API used here accepts 3,216.

    **The composed password must never leave this function except in the POST body.**
    Not a log line, not a progress broadcast, not job metadata, not ``error_message``:
    the progress path persists every line verbatim into ``JobLog`` with no redaction.
    """
    from . import ps_api_service

    package = cert_normalise_package(package)
    mode = functional_account_mode()
    if mode == _FA_MODE_REFERENCE:
        # The subordinate package gets its own key, and falls back to the leaf one only
        # if nothing names a separate account. An operator running one platform has one
        # account; an operator running both almost certainly has two, since the point of
        # the split is that the grants differ.
        named = (_cfg(_PLATFORM_CONFIG[package][2])
                 or _cfg(_PLATFORM_CONFIG[CERT_PACKAGE_LEAF][2]))
        return {"mode": mode, "package": package, "account_name": named, "id": None}

    principal, secret = _ca_credential(row.cloud, outputs)
    if not principal or not secret:
        raise CertPSError(
            "the CA build returned no enrollment credential, so no functional account "
            "can be composed — the pool exists, but its identity does not")

    cred = resolve_bi_credential()
    # ``cred`` carries a client secret and an API key, so **nothing read out of it may
    # reach a log line, the account name, or the returned dict.** Taint analysis is
    # per-dict rather than per-key and it is right to be: the two halves of this credential
    # differ only by which key they are under. Everything below that is safe to log is
    # therefore either a branch LITERAL or re-read from config, never carried out of here.
    if cred["auth"] == BI_AUTH_OAUTH:
        # Four values in four fields. Nothing is packed, so nothing is split — which is
        # why a CA secret, or a CA account NAME, may contain a colon on this path and
        # could not be expressed at all on the other one.
        name_suffix, packed_secret = "", secret
        auth = BI_AUTH_OAUTH
        auth_label = ("oauth (cert_ps_bi_client_id)" if _cfg("cert_ps_bi_client_id")
                      else "oauth (pscli_client_id)")
    else:
        # Re-read rather than taken off ``cred``: identical by construction — it is where
        # the resolver got it — and it keeps the account name, which IS logged, clear of
        # the dict that holds the secrets.
        run_as, api_key = bi_run_as_user(), cred["api_key"]
        # Splitting on the LAST colon is what lets a CA credential contain one. It buys
        # the BeyondInsight halves nothing, and a colon in either of them silently moves
        # the split point and mis-parses BOTH fields.
        for label, value in (("run-as user", run_as), ("API key", api_key)):
            if ":" in value:
                raise CertPSError(
                    f"the BeyondInsight {label} contains ':', which is the delimiter both "
                    f"fields are split on — the CA half may contain colons, this half may "
                    f"not. The OAuth path (cert_ps_bi_client_id) packs nothing and has "
                    f"neither constraint")
        name_suffix, packed_secret = f":{run_as}", f"{secret}:{api_key}"
        auth = BI_AUTH_APIKEY
        auth_label = "apikey (cert_ps_bi_api_key)"

    platform = platform_name(package)
    platform_id = await ps_api_service.get_platform_id(platform)
    account_name = f"{principal}{name_suffix}"
    # Uniqueness tenant-side is (platform, domain, account name, display name). The
    # account name is already unique per CA — the enrollment identity is minted per row
    # for exactly that reason — and the PLATFORM differs between the two packages, so the
    # same name on both is not a collision. The display name still carries the row and
    # now the package too, because it is also what makes a RETRY resolve back to this
    # account rather than fail or mint a second one.
    suffix = "-subca" if package == CERT_PACKAGE_SUBCA else ""
    fa_id = await ps_api_service.create_functional_account_on_platform(
        platform_id=int(platform_id),
        account_name=account_name,
        display_name=f"{row.name}-certauth{suffix}-{str(row.id)[:8]}",
        password=packed_secret,
        api_key=cred["client_id"], api_secret=cred["client_secret"],
        description=(f"{package_label(package)} Lab enrollment identity for CA "
                     f"{row.name} (lab_id={row.id}, {row.cloud})"))
    # The AUTH is logged, the credential is not. Which of the two paths an account was
    # minted on is the first thing worth knowing when the plugin later reports that it
    # has no BeyondInsight credential, and the account name no longer says so on its own.
    # ``auth_label`` is a branch literal naming the config key it came from — not a read
    # of ``cred``, which holds the secrets alongside it.
    logger.info("PS: minted %s functional account %r (id %s) on platform %r for CA %s, "
                "BeyondInsight auth=%s", package_label(package), account_name,
                fa_id, platform, row.id, auth_label)
    return {"mode": "create", "package": package, "account_name": account_name,
            "id": str(fa_id), "auth": auth}


async def resolve_functional_account(name: str = "",
                                     package: str = CERT_PACKAGE_LEAF) -> dict:
    """The functional account carrying BOTH credentials, with its platform checked.

    The managed system inherits its platform from the functional account, so an account on
    the wrong platform onboards green and then fails every credential action. With two
    platforms in play that stops being a typo-catcher and becomes the check that keeps a
    subordinate-CA identity off the leaf platform."""
    from . import ps_api_service, ps_vm_hook
    package = cert_normalise_package(package)
    label = package_label(package)
    name = ((name or "").strip() or _cfg(_PLATFORM_CONFIG[package][2])
            or _cfg(_PLATFORM_CONFIG[CERT_PACKAGE_LEAF][2])).strip()
    if not name:
        if functional_account_mode() != _FA_MODE_REFERENCE:
            # Create mode: the account should have been minted during the CA build, so
            # the remedy is to finish that, not to go and make one by hand.
            raise CertPSError(
                f"this CA has no {label} functional account yet — the build could not "
                f"create one, or it has never issued on this package. Use 'Wire up "
                f"Password Safe' on the CA to retry it, which needs a BeyondInsight "
                f"credential set (cert_ps_bi_client_id and secret, or cert_ps_bi_api_key).")
        raise CertPSError(
            f"no Password Safe functional account is configured for the {label} platform "
            f"— set {_PLATFORM_CONFIG[package][2]}. It carries two credentials on one "
            f"account: either the CA account and secret whole, with the BeyondInsight "
            f"OAuth client id and secret in the account's API key and secret fields; or "
            f"name '<ca-account>:<bi-run-as-user>' and password '<ca-secret>:<bi-api-key>', "
            f"both split on the LAST colon.")
    fa = await ps_api_service.get_functional_account(name)
    pname = fa.get("platform_name") or ""
    if pname and not ps_vm_hook._platform_name_ok(pname, *_PLATFORM_TOKENS[package]):
        raise CertPSError(
            f"functional account {name!r} is on platform {pname!r}, which is not a "
            f"{label} platform — the managed system inherits the functional account's "
            f"platform, so this would onboard against the wrong package. The two are "
            f"separate plugins with separate access control, and a leaf and an issuer are "
            f"not interchangeable credentials")
    if ":" not in name and not _oauth_is_available():
        # A colon-free name is the NORMAL shape on the OAuth path — the BeyondInsight half
        # rides the account's API key and secret fields, which this API does not return,
        # so the name says nothing about it and the warning would be false. It is only
        # meaningful where OAuth is not on the table at all: there a missing second half
        # leaves the Secrets Safe connection to appsettings.json, which is legal for an
        # on-premises administrator with filesystem access on the plugin host and
        # unreachable for a Cloud tenant.
        logger.warning(
            "PS: certificate functional account %r carries no ':' and no BeyondInsight "
            "OAuth registration is configured — the API user is missing, so the plugin "
            "can only reach Secrets Safe through appsettings.json. On a Password Safe "
            "Cloud tenant the first credential change will fail with FailedCredentials.",
            name)
    return fa


def _oauth_is_available() -> bool:
    """Whether an OAuth registration is configured at all — so a colon-free account name
    can be read as the OAuth shape rather than as a missing half. A half-set pair raises
    out of ``bi_client_credentials``; here that is simply 'no', because this is a warning
    and the real refusal happens where the account is minted."""
    if bi_auth_mode() == BI_AUTH_APIKEY:
        return False
    try:
        return bool(bi_client_credentials()[0])
    except CertPSError:
        return False


async def ensure_secrets_safe_folder(folder_path: str = "") -> dict:
    """Create the Secrets Safe folder the bundles land in, if it is not already there.

    **The plugin deliberately creates nothing** and fails with "Secrets Safe folder '...'
    was not found" — scattering certificate bundles into an unexpected folder with
    unexpected permissions is worse than a clear error. So the folder is the dashboard's
    job, and it is the one piece of Secrets Safe state onboarding has to establish.

    ``folder_path`` is ``<safe>/<folder>/<folder>``: the FIRST segment names an existing
    **safe**, and everything after it is a folder tree created beneath it. A safe is not
    created on purpose — it carries its own ACL, and the Secrets Safe folder's permissions
    are half the access boundary on the certificate (the managed account's access policy
    is the other half; the weaker of the two is the real one).

    Returns ``{"folder_id", "created", "path"}``. Idempotent.

    The tree-walk itself now lives in ``secrets_backend_service.ensure_bt_folder_path``,
    because the SPIRE lab needs the identical thing and a second copy of it would have
    drifted. This keeps the cert-specific parts: the ``cert_ps_folder`` default, and
    ``CertPSError`` so callers here catch what they always did.
    """
    import asyncio
    from . import secrets_backend_service
    path = (folder_path or _cfg("cert_ps_folder", "Certificates")).strip().strip("/")
    if not [seg for seg in path.split("/") if seg.strip()]:
        raise CertPSError("no Secrets Safe folder configured — set cert_ps_folder")
    try:
        return await asyncio.to_thread(
            secrets_backend_service.ensure_bt_folder_path, path)
    except ValueError as exc:
        raise CertPSError(str(exc)) from exc


async def register(*, system_name: str, account_name: str, address: str,
                   functional_account: str = "",
                   package: str = CERT_PACKAGE_LEAF,
                   ensure_folder: bool = True) -> dict:
    """Onboard one certificate identity: a managed system carrying the profile, and one
    managed account that becomes the certificate — or, on the subordinate package, one
    managed account that becomes an issuing authority.

    Returns ``{managed_system_id, managed_account_id, tf_state_json, address, folder,
    package, platform}``.

    **One managed account per certificate identity, and — with an Entra publisher — one
    per app registration.** Graph's PATCH replaces the whole ``keyCredentials`` collection,
    so the publisher reads the existing entries and carries them forward; two rotations
    against the same app registration can each read the collection and clobber the other's
    key. Password Safe serialises rotations per managed account, so that mapping is what
    makes it safe, and it is easy to break by accident when copying a platform instance."""
    from . import ps_api_service

    package = cert_normalise_package(package)
    if not system_name or not account_name:
        raise CertPSError("a certificate identity needs both a system name and an account "
                          "name — the account name becomes the subject CN by default")

    # Validate before touching anything: a rejected address costs nothing here and a
    # Secrets Safe folder created for a registration that then fails is litter.
    ps_resource_service._validate_certificate_dns_name(address, package)
    ps_resource_service._check_address_length(address, "certificate")

    fa = await resolve_functional_account(functional_account, package)
    platform = platform_name(package)
    platform_id = await ps_api_service.get_platform_id(platform)
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
        managed_account_name=account_name, method="certificate", dns_name=address,
        # Not derivable from platform_id, which is an opaque tenant-side number: without
        # it the second validation pass would read a subordinate profile — which normally
        # carries no isca= at all, that being the package's own default — as a leaf one
        # and refuse its name-constraint options.
        cert_package=package)
    # No `initial_password`, deliberately: the credential is a PKCS#12 passphrase Password
    # Safe generates from the account's password policy and hands to the plugin on the
    # first Change Password. Until that runs the account holds a placeholder that opens
    # nothing, and Test password correctly FAILS with "no bundle exists" — which is step 1
    # of the demonstration, not a fault.
    reg["address"] = address
    reg["folder"] = folder
    reg["package"] = package
    reg["platform"] = platform
    logger.info("PS: registered %s identity %s/%s on platform %r (system %s, account %s)",
                package_label(package), system_name, account_name, platform,
                reg.get("managed_system_id"), reg.get("managed_account_id"))
    return reg


async def deregister(tf_state_json: str) -> None:
    """Remove a managed system + account this module registered (best-effort).

    Leaves the Secrets Safe folder and any bundles in it alone. A certificate that was
    issued still exists at the CA until it expires — the plugin does not consult CRLs and
    neither does this — so deleting the record of it silently would be the wrong default."""
    await ps_resource_service.deregister(tf_state_json)
