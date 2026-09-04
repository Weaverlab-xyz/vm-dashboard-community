"""
BeyondTrust Password Safe resource registration via the BeyondTrust/passwordsafe
Terraform provider.

Optional, per-VM-deploy add-on (mirrors entitle_registration_service.py): when an
operator opts in, a freshly built VM is onboarded into Password Safe as a **managed
system** with **one managed account** — the ``adminuser`` account the bt-ready
provisioners baked into the image.

Onboarding shapes (``method`` on register_managed_system):
  - ``ssh`` — traditional managed system keyed by host_name/ip on an SSH platform; the
    VM's own private key is pushed and SSH key enforcement manages it (needs SSH
    line-of-sight, i.e. a Resource Broker / Jumpoint per VPC).
  - ``ssm`` — the cloud-native "AWS Systems Manager" Password Safe custom plugin:
    Password Safe manages the Linux EC2 instance over AWS SSM SendCommand, so the
    managed system carries ``dns_name = {instance-id}:{region}`` and the account name
    follows ``{name};{suffix}``; no private key is pushed (Change Password mints it).
  - ``azurevm`` — the cloud-native "Azure VM SSH Rotation" Password Safe custom plugin:
    Password Safe writes the key onto the VM via Azure VM Run Command, so the managed
    system carries ``dns_name = tenantId/subscriptionId/resourceGroup/vmName`` and the
    account name is the plain Linux user (``adminuser``); no private key is pushed
    (Change Password mints it).
  - ``gcpvm`` — the cloud-native "GCP VM SSH Rotation" Password Safe custom plugin:
    Password Safe writes the public key into the GCE instance's ``ssh-keys`` metadata
    (the guest agent propagates it to ``authorized_keys``), so the managed system carries
    ``dns_name = projectId/zone/instanceName`` and the account name is the plain Linux
    user (``adminuser``); no private key is pushed (Change Password mints it). ``ssm``,
    ``azurevm`` and ``gcpvm`` are the cloud-API "plugin" methods (see ``_PLUGIN_METHODS``)
    — no SSH reachability required.
  - ``certificate`` — the "Certificate" custom plugin: the managed credential is a PKCS#12
    passphrase and the bundle it opens lives in Secrets Safe, so the managed system carries
    the whole certificate profile (CA backend, key shape, subject, Secrets Safe destination,
    optional Entra publisher) in ``dns_name`` and nothing is seeded.

Shaped like entitle_registration_service / terraform_pra_service: inline HCL written
to an ephemeral workdir, ``terraform apply``, ids pulled from outputs, the full
``terraform.tfstate`` returned (scrubbed of secrets) so a later ``deregister`` can
``terraform destroy`` it. Secrets ride ``TF_VAR_*`` so they never land in the HCL.

Auth reuses the Password Safe OAuth client the ps-cli / public-API integration is
configured with, plus the provider-required run-as user:
  pscli_api_url            provider ``url``
  pscli_client_id          provider ``client_id``
  pscli_client_secret      provider ``client_secret``
  pscli_api_account_name   provider ``api_account_name`` (REQUIRED run-as user)

Provider/resource schema confirmed against BeyondTrust/passwordsafe v1.3.0:
  - provider requires url + api_account_name (client_id/client_secret for OAuth);
  - passwordsafe_managed_system_by_workgroup requires workgroup_id (string),
    entity_type_id (number), host_name, platform_id (number);
  - passwordsafe_managed_account requires account_name, system_name, and password
    (sensitive) — SSH-key management is expressed via private_key (+ passphrase) and
    dss_auto_management_flag, so we pass a generated placeholder password and let
    ssh_key_enforcement_mode on the system enforce key-only auth.
"""

import asyncio
import ipaddress
import json
import logging
import os
import re
import secrets
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Terraform binary — baked into the Docker image at build time.
_TERRAFORM = os.environ.get("TERRAFORM_EXECUTABLE", "terraform")
# NOTE: no TF_PLUGIN_CACHE_DIR here, deliberately — see terraform_pra_service. The
# image's read-only provider mirror (TF_CLI_CONFIG_FILE) is inherited from the
# environment; setting the plugin cache too breaks every init.

_REDACTED = "**REDACTED-BY-DASHBOARD**"

# Custom-plugin methods — Password Safe drives the target through a custom plugin
# (AWS SSM / Azure Run Command / GCP instance metadata / a DB client / the PRA Config
# API) rather than SSH, so the managed system carries the plugin's address in
# ``dns_name``/``host_name``, uses a placeholder ip, omits the SSH-only fields
# (remote_client_type / ssh_key_enforcement_mode), and pushes no private key. ``ssh``
# is the traditional method. ``ssm``/``azurevm``/``gcpvm`` are SSH-key-managed (dss
# auto-management on); ``dbssm`` (cloud-DB via the "{engine} SSM Custom Plugin"),
# ``dbazure`` (cloud-DB via the "{engine} Azure Run Command Plugin") and ``pravault``
# (the "PRA Vault Username Password" plugin) are PASSWORD-managed, so their account
# emits dss_auto_management_flag = false.
# ``k8ssa`` (the "Kubernetes Service Account Token" plugin) is password-managed too —
# there the "password" IS the ServiceAccount bearer token.
# ``dbgcp`` (cloud-DB via the "GCP Cloud SQL {engine}" plugins) is password-managed as
# well; unlike its two DB siblings it reaches the instance over Google's control plane
# rather than a jump host, so there is no key material anywhere in its address.
# ``certificate`` (the "Certificate" plugin) is password-managed in the same sense as
# ``k8ssa``: the credential Password Safe holds is the PKCS#12 PASSPHRASE, and the bundle
# it opens lives in Secrets Safe. Its address is the whole certificate profile — a backend
# name plus ``?key=value`` options — because a Password Safe Cloud tenant cannot edit the
# appsettings.json inside the .psplugin, so nothing may be configured anywhere else.
_PLUGIN_METHODS = frozenset({"ssm", "azurevm", "gcpvm", "dbssm", "dbazure", "dbgcp",
                             "pravault", "k8ssa", "certificate"})
# Methods whose managed account is password-managed (no SSH DSS key auto-management).
# ``password`` is the only NON-plugin member: a traditional managed system reached at its
# own address, whose account Password Safe rotates by password because the caller has a
# working login and no key material. See the branch in ``register_managed_system``.
_PASSWORD_MANAGED_METHODS = frozenset({"dbssm", "dbazure", "dbgcp", "pravault", "k8ssa",
                                       "password", "certificate"})

# Password Safe's managed-system address column is 255 chars. The cloud-DB plugin addresses
# pack 6-8 fields into it, and the Azure one is close to the ceiling with realistic values
# (two GUIDs, a flexible-server FQDN, a broker cert path). Over the limit the API rejects or
# truncates the address, and a truncated address fails inside the plugin later as an
# unparseable field rather than as a length problem — so check it here, where the number can
# be named.
_MAX_MANAGED_SYSTEM_ADDRESS = 255


# What to shorten, per method. Generic advice ("shorten the longest field") is useless on
# an address whose fields are all load-bearing, and the certificate profile in particular
# runs long enough that a realistic ADCS address from the plugin's own documentation is
# 269 characters — 14 over the limit — before anyone has typed a real CA name.
_ADDRESS_LENGTH_ADVICE = {
    # The three levers, in the order the plugin's own documentation puts them. Each names
    # what it buys, because "shorten the address" is not actionable on a profile whose
    # every field is load-bearing.
    "certificate": (
        "Drop every option already at its default — secret=cert/{system}/{account}, "
        "retain=1, subject=CN={AccountName}, warn=25, key=rsa3072, pbe=Aes256, "
        "store=SecretsSafe, wait=30, and on AWS region= (it is the ARN's own fourth "
        "field). Then move per-identity values — dns=, ip=, subject=, lifetime=, eku=, "
        "key= — onto the MANAGED ACCOUNT NAME after a '?', where they cost nothing from "
        "this budget and override the system's for that identity alone. Then shorten "
        "folder=, which is addressed by path rather than read as prose. If every option "
        "is still doing work, the profile needs splitting across two managed systems"),
}
_DEFAULT_ADDRESS_LENGTH_ADVICE = (
    "Shorten the longest field — usually the Resource Broker cert path or the resource "
    "group")


def _check_address_length(dns_name: str, method: str) -> None:
    if len(dns_name) > _MAX_MANAGED_SYSTEM_ADDRESS:
        advice = _ADDRESS_LENGTH_ADVICE.get(method, _DEFAULT_ADDRESS_LENGTH_ADVICE)
        raise PSResourceError(
            f"{method} managed-system address is {len(dns_name)} characters, "
            f"{len(dns_name) - _MAX_MANAGED_SYSTEM_ADDRESS} over Password Safe's "
            f"{_MAX_MANAGED_SYSTEM_ADDRESS}-character limit. {advice} — a truncated "
            f"address fails later inside the plugin as an unparseable field, not as a "
            f"length problem.")

# The public REST create/update-managed-account path the Terraform provider uses caps
# ``Password`` at 128 characters (400 "Password cannot exceed 128 characters."). This is a
# limit of THAT path only — a plugin's rotation write-back
# (``ManagedAccount_CredentialsNew_Password``) carries multi-KB values, which is how the
# SSH-key plugins store 3.2 KB PEMs. So a credential too long to SEED here is still
# perfectly storable once the plugin rotates it; a k8s ServiceAccount bearer token
# (800–1,200 characters) is exactly that case. Seeding one anyway fails the apply outright,
# so ``register_managed_system`` drops an over-long seed for a placeholder and reports it.
_MAX_SEED_PASSWORD_LEN = 128

# ── Kubernetes Service Account Token address grammar ──────────────────────────
#
# Transcribed from the plugin's Factories/ParameterFactory.cs so a bad address is
# rejected here, at registration, instead of at the first scheduled rotation. The
# plugin rejects an unrecognised option rather than ignoring it (a silently dropped
# option inside a checksum-sealed package is neither diagnosable nor fixable), so
# this validator has to be exact rather than permissive.
#
# Password Safe truncates the address field at 255 characters; the plugin refuses at
# 249 so a truncated address never reaches the cluster lookup.
_K8SSA_MAX_ADDRESS = 249
# Semicolon-separated fields that precede the options, per prefix.
_K8SSA_POSITIONAL = {"eks": 3, "aks": 4, "gke": 4, "k8s": 2}
# Option keys, lowercased exactly as the plugin's ApplyOption switch compares them.
_K8SSA_OPTION_KEYS = frozenset({
    "mode", "ttl", "ns", "dnsendpoint", "allowhostnamemismatch", "servername",
    "rolearn", "aadappid", "ca",
})
# Options the plugin accepts only on one prefix; anywhere else it raises.
_K8SSA_OPTION_PROVIDER = {"rolearn": "eks", "aadappid": "aks", "ca": "k8s"}
_K8SSA_MODES = frozenset({"bound", "longlived"})
_RFC1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


class PSResourceError(Exception):
    """Raised when a Password Safe registration Terraform operation fails."""


def _cfg(key: str) -> str:
    try:
        from . import config_service
        val = config_service.get(key)
        if val:
            return val
    except Exception:
        pass
    from ..config import settings
    return getattr(settings, key, "") or ""


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", (name or "").lower()) or "system"


def _line(key: str, val) -> str:
    """One aligned HCL attribute line (``  key = val``), padding the key so the
    ``=`` lines up across the block — matches the hand-aligned style the tests assert."""
    return f"  {key:<24} = {val}"


def _validate_k8ssa_dns_name(dns_name: str) -> None:
    """Raise PSResourceError unless ``dns_name`` is an address the plugin will parse.

    Mirrors ParameterFactory.ParseAddress: length cap, a known prefix, at least that
    prefix's positional field count, then every trailing field is either the bare mode
    shorthand or ``key=value`` with a recognised, provider-appropriate key. Blank
    trailing fields are skipped, as the plugin skips them, so a trailing ';' is fine."""
    addr = (dns_name or "").strip()
    if not addr:
        raise PSResourceError(
            "Kubernetes ServiceAccount Token onboarding requires a dns_name of the form "
            "'eks;<region>;<cluster>', 'aks;<subscriptionId>;<resourceGroup>;<cluster>', "
            "'gke;<projectId>;<location>;<cluster>' or 'k8s;<apiServerUrl>'")
    if len(addr) > _K8SSA_MAX_ADDRESS:
        raise PSResourceError(
            f"managed system address is {len(addr)} characters, "
            f"{len(addr) - _K8SSA_MAX_ADDRESS} over the {_K8SSA_MAX_ADDRESS} character "
            f"limit the plugin enforces (Password Safe truncates the field at 255)")

    fields = [f.strip() for f in addr.split(";")]
    prefix = fields[0].lower()
    positional = _K8SSA_POSITIONAL.get(prefix)
    if positional is None:
        raise PSResourceError(
            f"managed system address prefix {fields[0]!r} is not recognised — use one of "
            f"eks; aks; gke; k8s;")
    if len(fields) < positional:
        raise PSResourceError(
            f"managed system address {addr!r} has {len(fields)} field(s), expected at "
            f"least {positional} for a {prefix!r} address")
    if any(not f for f in fields[1:positional]):
        raise PSResourceError(
            f"managed system address {addr!r} has an empty positional field — every one of "
            f"the first {positional} fields must be set for a {prefix!r} address")

    for field in fields[positional:]:
        if not field:
            continue
        if field.lower() in _K8SSA_MODES:
            continue
        key, sep, value = field.partition("=")
        if not sep or not key:
            raise PSResourceError(
                f"{field!r} in managed system address {addr!r} is not a recognised option — "
                f"options are 'bound', 'longlived', or key=value with one of: "
                f"{', '.join(sorted(_K8SSA_OPTION_KEYS))}")
        key = key.strip().lower()
        if key not in _K8SSA_OPTION_KEYS:
            raise PSResourceError(
                f"{key!r} in managed system address {addr!r} is not a recognised option key — "
                f"valid keys: {', '.join(sorted(_K8SSA_OPTION_KEYS))}")
        required = _K8SSA_OPTION_PROVIDER.get(key)
        if required and required != prefix:
            raise PSResourceError(
                f"the {key!r} option applies only to {required!r} addresses, but {addr!r} is "
                f"a {prefix!r} address")
        if key == "mode" and value.strip().lower() not in _K8SSA_MODES:
            raise PSResourceError(
                f"token mode {value!r} is not valid — use 'mode=longlived' or 'mode=bound' "
                f"(or the bare shorthand ';bound')")
        # The plugin only requires ttl > 0 here; the API server's own 600s floor is
        # applied by whoever builds the address, not by the parser.
        if key == "ttl" and (not value.strip().isdigit() or int(value.strip()) <= 0):
            raise PSResourceError(
                f"bound token TTL {value!r} is not a positive whole number of seconds "
                f"(example: ttl=43200)")
        if key == "ns" and not _RFC1123_LABEL.match(value.strip()):
            raise PSResourceError(
                f"default namespace {value!r} is not a valid Kubernetes name — lowercase "
                f"letters, digits and hyphens, starting and ending alphanumeric")


# The managed system's ``timeout`` in SECONDS, per the plugin's own configuration
# table ("Timeout | 60 | Key generation plus a CA round trip is slower than a password
# change"). Like every custom-plugin platform this is read BY THE PLUGIN rather than
# used as a socket timeout, and the plugin families disagree on the unit — the AWS SSM
# DB plugins read milliseconds while this one, the GCP Cloud SQL and the Azure Run
# Command plugins read seconds. A constant rather than a config key: there is one right
# answer, and a per-install knob here is a way to get a 60-millisecond timeout by
# accident.
_CERTIFICATE_PLUGIN_TIMEOUT_SECONDS = 60

# ── Certificate plugin address grammar ────────────────────────────────────────
#
# Transcribed from the Certificate plugin's own documentation (Beekeeper-Certificate.md,
# "Managed system" / "Certificate profile" / "Backend-specific options"). The address is
# not a hostname: it is the WHOLE certificate profile — a backend name, optionally
# followed by '?' and '&'-separated key=value options. That design exists because
# appsettings.json ships INSIDE the .psplugin, so a Password Safe Cloud tenant cannot edit
# it and every value has to ride a standard Password Safe field.
#
# The plugin logs an unrecognised option as a warning rather than refusing it, which is
# exactly the failure mode worth catching here instead: a mistyped 'lifetim=30d' has no
# effect at all and leaves a certificate issued against a default nobody chose, hours
# later, on a schedule. So this validator rejects what the plugin would merely mention.
#
# Values are NEVER percent-decoded, deliberately, so an ADCS CA name's backslashes and
# spaces, an ARN's colons and slashes, and a literal '%' all survive as typed. ';' is
# accepted in place of '&' for consoles that treat '&' awkwardly, and a bare option with
# no '=' reads as true.

# Backend names, normalised by lowercasing and dropping punctuation — the plugin does the
# same, so 'AWS-PCA' and 'awspca' are one thing.
_CERT_BACKENDS = {
    "adcs": "adcs", "ad": "adcs", "microsoftca": "adcs",
    "awspca": "awspca", "aws": "awspca", "awsprivateca": "awspca", "acmpca": "awspca",
    "gcpcas": "gcpcas", "gcp": "gcpcas", "cas": "gcpcas", "googlecas": "gcpcas",
    "selfsigned": "selfsigned", "self": "selfsigned",
    "selfsignedtest": "selfsignedtest", "test": "selfsignedtest",
}
# Options each backend cannot do without. The plugin fails at construction on a missing
# one; naming it here means the operator sees it while the address is still editable.
_CERT_REQUIRED = {
    "adcs": ("ca", "template"),
    "awspca": ("arn",),
    "gcpcas": ("project", "location", "pool"),
    "selfsigned": (),
    "selfsignedtest": (),
}
# Option aliases → canonical key, exactly as the plugin's alias table reads.
_CERT_ALIASES = {
    "ttl": "lifetime", "keyalg": "key", "subjectdn": "subject", "sans": "san",
    "ekus": "eku", "format": "bundle", "warnpercent": "warn", "secretname": "secret",
    "ownergroup": "owner", "tenantid": "tenant", "applicationid": "appid",
    "serviceprincipalid": "spid",
}
# Profile + store + publisher options, valid on any backend.
_CERT_COMMON_KEYS = frozenset({
    "lifetime", "key", "keysize", "curve", "hash", "subject", "dns", "ip", "san", "eku",
    "bundle", "pbe", "warn", "warndays", "warnminutes",
    "store", "biurl", "folder", "secret", "owner",
    "publisher", "tenant", "appid", "spid", "retain",
})
# Options the plugin reads only on one backend. Accepting 'region=' on a gcpcas address
# would silently do nothing, which is the same class of bug the whole validator exists for.
_CERT_BACKEND_KEYS = {
    "ca": "adcs", "template": "adcs", "impersonate": "adcs", "validate": "adcs",
    "arn": "awspca", "region": "awspca", "sigalg": "awspca",
    "templatearn": "awspca", "wait": "awspca",
    "project": "gcpcas", "location": "gcpcas", "pool": "gcpcas",
    "issuer": "gcpcas", "certtemplate": "gcpcas",
}
_CERT_KEY_ALGS = frozenset({
    "rsa2048", "rsa3072", "rsa4096", "ecdsa-p256", "ecdsa-p384", "ecdsa-p521"})
_CERT_HASHES = frozenset({"sha256", "sha384", "sha512"})
_CERT_BUNDLES = frozenset({"pkcs12", "pembundle"})
_CERT_PBE = frozenset({"aes256", "legacy"})
_CERT_STORES = frozenset({"secretssafe", "filesystem"})
_CERT_PUBLISHERS = {"none": None, "entraapp": "appid", "app": "appid",
                    "entrasp": "spid", "sp": "spid"}
# '12h', '30d', '2w', '1y', '90m'; a bare number means DAYS.
_CERT_LIFETIME = re.compile(r"^\d+[mhdwy]?$", re.IGNORECASE)
# The plugin caps the warning percentage at 90; a higher value would fire permanently.
_CERT_MAX_WARN_PERCENT = 90


def _cert_normalise_backend(raw: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (raw or "").strip().lower())


def parse_certificate_address(dns_name: str) -> dict:
    """Split a Certificate-plugin address into ``{"backend": str, "options": dict}``.

    Shared by the validator and by callers that need to read a value back out of an
    address they did not compose. Option keys are canonicalised through
    ``_CERT_ALIASES`` and lowercased; VALUES are returned exactly as typed, because the
    plugin never percent-decodes them and an ADCS CA name or an ARN depends on that."""
    addr = (dns_name or "").strip()
    head, sep, tail = addr.partition("?")
    options: dict = {}
    if sep:
        for field in re.split(r"[&;]", tail):
            field = field.strip()
            if not field:
                continue
            key, eq, value = field.partition("=")
            key = key.strip().lower()
            key = _CERT_ALIASES.get(key, key)
            # A bare option with no '=' reads as true, per the plugin's own parser.
            options[key] = value if eq else "true"
    return {"backend": head.strip(), "options": options}


def _validate_certificate_dns_name(dns_name: str) -> None:
    """Raise PSResourceError unless ``dns_name`` is a certificate profile the plugin parses.

    Grammar and value checks only; the shared 255-character cap runs separately in the
    caller via ``_check_address_length``."""
    addr = (dns_name or "").strip()
    if not addr:
        raise PSResourceError(
            "Certificate onboarding requires a Network Address carrying the certificate "
            "profile — a backend name plus options, e.g. "
            "'gcpcas?project=<p>&location=us-central1&pool=<pool>&lifetime=24h'. The "
            "package deliberately ships no default backend, so an empty address fails "
            "with 'No certificate authority backend is configured' at the first action.")

    parsed = parse_certificate_address(addr)
    raw_backend = parsed["backend"]
    backend = _CERT_BACKENDS.get(_cert_normalise_backend(raw_backend))
    if backend is None:
        raise PSResourceError(
            f"certificate backend {raw_backend!r} is not recognised — use one of: adcs, "
            f"awspca, gcpcas, selfsigned (or the aliases ad/microsoft-ca, "
            f"aws/aws-private-ca/acm-pca, gcp/cas/google-cas, self)")
    if backend == "selfsignedtest":
        raise PSResourceError(
            "the 'selfsignedtest' backend generates and persists its own CA private key "
            "UNENCRYPTED beside the plugin and is for harness use only — never register a "
            "managed system against it. Use 'selfsigned' for the Entra publisher case, "
            "which needs no certificate authority either.")

    options = parsed["options"]
    for key in sorted(options):
        if key in _CERT_COMMON_KEYS:
            continue
        owner = _CERT_BACKEND_KEYS.get(key)
        if owner is None:
            raise PSResourceError(
                f"{key!r} in the certificate address is not a recognised option. The "
                f"plugin logs an unrecognised option as a warning and carries on with a "
                f"DEFAULT, so a mistyped name issues a certificate nobody chose — check "
                f"the alias table before assuming an option does not exist.")
        if owner != backend:
            raise PSResourceError(
                f"the {key!r} option applies only to a {owner!r} address, but this is a "
                f"{backend!r} address — the plugin would ignore it")

    missing = [k for k in _CERT_REQUIRED[backend] if not (options.get(k) or "").strip()]
    if missing:
        raise PSResourceError(
            f"a {backend!r} certificate address requires "
            f"{', '.join(k + '=' for k in missing)} — without it the backend fails at "
            f"construction rather than mid-rotation")

    _validate_certificate_options(backend, options)


def _validate_certificate_options(backend: str, options: dict) -> None:
    """Value-level checks for a parsed certificate profile."""
    def _val(key: str) -> str:
        return (options.get(key) or "").strip()

    lifetime = _val("lifetime")
    if lifetime and not _CERT_LIFETIME.match(lifetime):
        raise PSResourceError(
            f"lifetime {lifetime!r} is not valid — use a number with an optional unit "
            f"(90m, 12h, 30d, 2w, 1y). A bare number means DAYS.")
    if lifetime and backend == "adcs":
        logger.info(
            "PS: 'lifetime=%s' is set on an ADCS certificate address, where the TEMPLATE "
            "decides validity — the plugin ignores it", lifetime)

    key_alg = _val("key").lower()
    if key_alg and key_alg not in _CERT_KEY_ALGS:
        raise PSResourceError(
            f"key {key_alg!r} is not valid — use one of: "
            f"{', '.join(sorted(_CERT_KEY_ALGS))}")

    for name, allowed in (("hash", _CERT_HASHES), ("bundle", _CERT_BUNDLES),
                          ("pbe", _CERT_PBE), ("store", _CERT_STORES)):
        value = _val(name).lower()
        if value and value not in allowed:
            raise PSResourceError(
                f"{name} {value!r} is not valid — use one of: {', '.join(sorted(allowed))}")

    warn = _val("warn")
    if warn and (not warn.isdigit() or int(warn) > _CERT_MAX_WARN_PERCENT):
        raise PSResourceError(
            f"warn {warn!r} is not valid — it is a PERCENTAGE of the certificate's own "
            f"lifetime, 0 to {_CERT_MAX_WARN_PERCENT}. Use warndays= or warnminutes= for "
            f"an absolute floor.")
    for name in ("warndays", "warnminutes", "wait", "retain"):
        value = _val(name)
        if value and not value.isdigit():
            raise PSResourceError(f"{name} {value!r} is not a whole number")

    publisher = _val("publisher").lower()
    if publisher:
        if publisher not in _CERT_PUBLISHERS:
            raise PSResourceError(
                f"publisher {publisher!r} is not valid — use entraapp (or app), entrasp "
                f"(or sp), or none")
        target_key = _CERT_PUBLISHERS[publisher]
        if target_key:
            if not _val("tenant"):
                raise PSResourceError(
                    "an Entra publisher needs tenant= — the directory (tenant) id or a "
                    "verified domain")
            if not _val(target_key):
                what = ("the app registration's OBJECT id, not its application (client) id"
                        if target_key == "appid" else
                        "the service principal's OWN object id, which is NOT the object "
                        "id of its associated application")
                raise PSResourceError(
                    f"publisher={publisher} needs {target_key}= — {what}")
    elif _val("tenant") or _val("appid") or _val("spid") or _val("retain"):
        raise PSResourceError(
            "tenant=/appid=/spid=/retain= only do anything with publisher= set; without "
            "it the publisher is None and the certificate is never registered with the "
            "relying party")

    # Two values the plugin can also take from appsettings.json, which ships INSIDE the
    # .psplugin — so on Password Safe Cloud the address is their only possible source.
    # Warned rather than refused, because an on-premises administrator with filesystem
    # access on the plugin host may legitimately have set them there.
    if _val("store").lower() in ("", "secretssafe"):
        for name, why in (("biurl", "the BeyondInsight base URL the bundle is written to"),
                          ("owner", "the group id owning created secrets; Secrets Safe "
                                    "requires an owner")):
            if not _val(name):
                logger.warning(
                    "PS: certificate address sets no %s= (%s). The plugin falls back to "
                    "appsettings.json, which cannot be edited on a Password Safe Cloud "
                    "tenant — on Cloud the first credential change will fail.", name, why)

# ── AWS SSM DB plugin address grammar ─────────────────────────────────────────
#
# Transcribed from the "{engine} SSM Custom Plugin" v24.2.x action source (the vendor
# zip's Actions/*.cs — all four actions parse identically within an engine), for the
# same reason as its two sibling grammars: the plugin splits every host candidate on
# ';' and indexes FIXED positions, so a wrong segment count — or a host field holding
# a bare IP/hostname — dies as "Index was outside the bounds of the array" before any
# AWS call. The layout is PER-ENGINE: mssql has no database segment, and mysql alone
# carries a trailing ssl flag:
#
#   mssql (5):  instanceId;region;dbEndpoint;certPath;assumeRole
#   psql  (6):  instanceId;region;dbEndpoint;databaseName;certPath;assumeRole
#   mysql (7):  instanceId;region;dbEndpoint;databaseName;certPath;assumeRole;ssl
#
# assumeRole is Substring(0,12)'d UNCONDITIONALLY (that is how "arn:aws:iam:" is
# detected), so any value under 12 characters — such as "local", this codebase's old
# default — crashes every action with "Index and length must refer to a location
# within the string". The documented placeholder is "NoAssumeRole" (exactly 12);
# anything not starting with "arn:aws:iam:" means "use the broker's own AWS
# credentials". The mysql ssl segment enables TLS only on the literal "sslTRUE" and
# silently disables it for anything else, so only the two canonical spellings are
# accepted here — a shifted or mistyped flag must fail at registration, not quietly
# downgrade the connection.
_DBSSM_SEGMENT_ENGINES = {5: "mssql", 6: "psql", 7: "mysql"}
_DBSSM_FORMATS = (
    "'instanceId;region;dbEndpoint;certPath;assumeRole' (mssql), "
    "'instanceId;region;dbEndpoint;databaseName;certPath;assumeRole' (psql) or "
    "'instanceId;region;dbEndpoint;databaseName;certPath;assumeRole;sslTRUE|sslFALSE' "
    "(mysql)")
_DBSSM_MIN_ASSUME_ROLE_LEN = 12
_DBSSM_SSL_FLAGS = frozenset({"sslTRUE", "sslFALSE"})
_DBSSM_INSTANCE_ID = re.compile(r"^i-[0-9a-f]{8,17}$")

# The managed system's ``timeout`` in MILLISECONDS, not seconds. Every SSM DB action
# fires SendCommand, sleeps 1s, reads GetCommandInvocation once, and then — only if the
# status is still "InProgress" — does `Thread.Sleep(timeout)` and reads it a second time,
# where `timeout` is the managed system's own Timeout field. Password Safe defaults that
# field to 30, so an unset value buys the command 30 MILLISECONDS to finish; the second
# read still says "InProgress", which the action treats as neither Failed nor Success and
# falls through to report SUCCESS. That is worse than a timeout: Password Safe stores the
# new password it never confirmed. A shell round-trip to a jump host plus a psql ALTER
# USER against RDS is a few seconds, so 30s is the smallest value that reliably lands in
# the Success branch. This is the vendor article's "timeout in milliseconds is used to
# give more time for Systems Manager to provide status" note, made structural — a
# constant rather than a config key, because there is one right answer and a per-install
# knob here is a way to get the 30ms behaviour back by accident.
_DBSSM_PLUGIN_TIMEOUT_MS = 30000


def _validate_dbssm_dns_name(dns_name: str) -> None:
    """Raise PSResourceError unless ``dns_name`` is an address the plugin will parse.

    Grammar only — the shared 255-character check runs separately in the caller. The
    engine is inferred from the segment count (5/6/7 are disjoint), then each position
    is checked for the mistakes that otherwise surface as a mid-rotation parse crash:
    an empty positional field, a first field that is not an EC2 instance id, an
    assumeRole segment the plugin's Substring(0,12) would throw on, and a mysql ssl
    flag that is neither canonical spelling."""
    addr = (dns_name or "").strip()
    if not addr:
        raise PSResourceError(
            f"DB SSM onboarding requires a dns_name of the form {_DBSSM_FORMATS}")
    fields = addr.split(";")
    engine = _DBSSM_SEGMENT_ENGINES.get(len(fields))
    if engine is None:
        raise PSResourceError(
            f"managed system address {addr!r} has {len(fields)} ';'-separated field(s); "
            f"the {{engine}} SSM plugins index fixed positions, so it must be exactly 5 "
            f"(mssql), 6 (psql) or 7 (mysql) — {_DBSSM_FORMATS}")
    names = (["instanceId", "region", "dbEndpoint"]
             + (["databaseName"] if engine != "mssql" else [])
             + ["certPath", "assumeRole"]
             + (["ssl"] if engine == "mysql" else []))
    for pos, (field_name, value) in enumerate(zip(names, fields), start=1):
        if not value.strip():
            raise PSResourceError(
                f"field {pos} ({field_name}) of managed system address {addr!r} is "
                f"empty — every position is consumed, so an empty one does not error "
                f"here; it produces a broken command at the first rotation instead")
    if not _DBSSM_INSTANCE_ID.match(fields[0]):
        raise PSResourceError(
            f"field 1 of managed system address {addr!r} must be the EC2 instance id "
            f"of the SSM jump host that runs the DB client (i-xxxxxxxxxxxxxxxxx), not "
            f"{fields[0]!r}")
    assume_role = fields[names.index("assumeRole")]
    if len(assume_role) < _DBSSM_MIN_ASSUME_ROLE_LEN:
        raise PSResourceError(
            f"assumeRole segment {assume_role!r} is shorter than "
            f"{_DBSSM_MIN_ASSUME_ROLE_LEN} characters — the plugin calls Substring(0,12) "
            f"on it unconditionally and crashes with 'Index and length must refer to a "
            f"location within the string'. Use a full IAM role ARN or the literal "
            f"'NoAssumeRole'")
    if engine == "mysql" and fields[6] not in _DBSSM_SSL_FLAGS:
        raise PSResourceError(
            f"field 7 of a mysql address must be 'sslTRUE' or 'sslFALSE', not "
            f"{fields[6]!r} — the plugin enables TLS only on the literal 'sslTRUE' and "
            f"silently disables it for anything else, so only the canonical spellings "
            f"are accepted")


def _validate_ip_field(ip_address: str, method: str) -> None:
    """Raise PSResourceError unless ``ip_address`` is empty or a literal IP.

    Password Safe validates the managed system's IPAddress as an *address*, not as free
    text: anything else is rejected on create with ``Bad IP value: '<value>' in
    'IPAddress' field`` and takes the whole apply down with it (live 2026-08-27, after a
    six-minute RDS apply). A plugin's ';'-packed address therefore cannot live in this
    field however convenient it would be — it belongs in DnsName, which has no such
    validation. Checked before Terraform runs so the mistake costs a validation error
    rather than a provisioned database with no onboarding."""
    if not ip_address:
        return
    try:
        ipaddress.ip_address(ip_address.strip())
    except ValueError:
        raise PSResourceError(
            f"{method} onboarding cannot use {ip_address!r} as the managed system's ip: "
            f"Password Safe validates IPAddress as a literal IP and rejects anything else "
            f"with \"Bad IP value: '<value>' in 'IPAddress' field\". The plugin's packed "
            f"address goes in dns_name; leave ip_address empty to get the placeholder."
        ) from None


# ── Azure Run Command DB plugin timeout ───────────────────────────
#
# The managed system's ``timeout``, in SECONDS — the same unit the GCP Cloud SQL plugins
# read (_DBGCP_PLUGIN_TIMEOUT_SECONDS below) and the opposite of the AWS SSM plugins
# reading the very same field (_DBSSM_PLUGIN_TIMEOUT_MS above).
#
# The unit is not guesswork. This branch registered NO timeout at all until 2026-09-02,
# so the field sat at Password Safe's default of 30 — and the plugin's own diagnostic
# printed "Run action with timeout 30000 msec", i.e. the field multiplied by 1000. It
# then aborted a Verify Functional Account 31 seconds in with .NET's "Thread was
# interrupted from a waiting state", its watchdog interrupting the waiting thread at the
# 30s ceiling. A single Azure VM Run Command round trip is routinely 20-60s on its own,
# so 30 was never survivable; it failed as a timeout rather than as the AWS shape's
# false SUCCESS only because this plugin family actually enforces the wait.
#
# 180 for the same reason GCP is 180, plus one specific to this family: every database
# shares one jump VM, Azure permits one action-style Run Command per VM, and the plugin
# answers a 409 by retrying 5 times at 15s. That ladder alone is 75s+ before the psql
# call, so any value under ~90 makes the plugin's own contention handling unreachable.
_DBAZURE_PLUGIN_TIMEOUT_SECONDS = 180


# ── GCP Cloud SQL address grammar ─────────────────────────────────────────────
#
# Transcribed from the plugin's Factories/AddressFormat.cs + ParameterFactory.cs, for
# the same reason the k8ssa grammar above is: the plugin treats an unrecognised option
# as an ERROR rather than ignoring it, and an option silently dropped inside a sealed
# package is undiagnosable from the operator's side. Five positional fields, then
# optional key=value options:
#
#   channel;project:region:instance;dbName|-;audience|-;sslTRUE|sslFALSE|-[;option=value]
#
# Field 2 is the Cloud SQL *instance connection name*, not a hostname — project, region
# and instance are derived from it rather than entered separately.
#
# Password Safe truncates the address field at 255; the plugin refuses at 249, and a
# truncated address does not error — it silently becomes a DIFFERENT, WRONG address. So
# this is the tighter of the two limits on purpose (_MAX_MANAGED_SYSTEM_ADDRESS is 255).
_DBGCP_MAX_ADDRESS = 249
_DBGCP_POSITIONAL = 5
_DBGCP_FORMAT = ("channel;project:region:instance;dbName|-;audience|-;"
                 "sslTRUE|sslFALSE|-[;option=value]")
_DBGCP_CHANNELS = frozenset({"admin-api", "data-api", "cloud-run"})
# Both control-plane channels talk to a Google API over TLS and never open a database
# connection, so a database name, an audience or an SSL flag on one is a configuration
# error rather than a no-op — the plugin rejects rather than letting anyone believe they
# turned TLS off.
_DBGCP_CONTROL_PLANE = frozenset({"admin-api", "data-api"})
_DBGCP_OPTION_KEYS = frozenset({"host", "fasecret", "iam", "ver", "verifier"})
# Options the plugin accepts only on one channel; anywhere else it raises.
_DBGCP_OPTION_CHANNEL = {"fasecret": "data-api", "ver": "cloud-run"}
# Two options are legal everywhere as a KEY but not in every VALUE, and both used to
# parse on any channel and quietly do nothing on the control-plane ones. The SILENCE is
# the defect in both cases:
#
# * "verifier=on" claims the new password was pre-hashed on the Resource Broker so the
#   plaintext never reaches the wire. Only cloud-run can honour that; accepting it on
#   data-api/admin-api told an operator their password was protected when it was not.
#   "verifier=off" stays legal everywhere, because it promises nothing.
# * "iam=false" on data-api leaves the address with NO way to authenticate: executeSql
#   has no plaintext-password field and fasecret= is SQL Server only. It onboarded
#   cleanly and then failed with an opaque Google 401 at the first rotation.
#
# The plugin refuses both at pre-flight now, so refuse them here — at the click, where
# the operator is still looking at the thing that produced the address.
_DBGCP_VERIFIER_ON = frozenset({"on", "true", "yes", "1"})
_DBGCP_IAM_FALSE = frozenset({"off", "false", "no", "0"})
_DBGCP_SSL_FLAGS = frozenset({"ssltrue", "sslfalse"})

# The managed system's ``timeout``, in SECONDS — the opposite unit to the one the AWS
# SSM plugins read out of the very same field (_DBSSM_PLUGIN_TIMEOUT_MS above). Password
# Safe stores one integer and each plugin decides what it means, so the unit is a
# per-plugin fact and not a property of the field.
#
# Leaving it unset is not neutral: Password Safe defaults the field to 30, which these
# plugins read as 30 seconds. That is survivable on data-api, whose own statement
# timeout is a non-configurable 30 seconds anyway, but it is too short for cloud-run —
# a Direct-VPC cold start on Cloud Run is documented at "a minute or more", so the first
# rotation after an idle period is the one that times out, and a rotation that times out
# may already have applied the change. 180 is the plugin's own default and covers both.
#
# (The 30 an operator sees in a plugin diagnostic as "timeout 30000 msec" is this
# default, printed after the plugin's own seconds-to-milliseconds conversion. It is not
# evidence that anything registered 30000, and it is not a millisecond convention.)
_DBGCP_PLUGIN_TIMEOUT_SECONDS = 180
# project:region:instance — three non-empty segments, and no ';' smuggled through.
_DBGCP_CONNECTION_NAME = re.compile(r"^[^:;]+:[^:;]+:[^:;]+$")

# fasecret= names the Secret Manager VERSION holding the functional account's database
# password, for the one combination that has no IAM token to authenticate with
# (data-api + SQL Server). It must be REGIONAL. The global form — which is what the
# plugin article's own example prints, and what secrets_backend_service.write_gcp_sm
# creates — is rejected by the Data API at rotation time with:
#
#   The provided Secret ID [...] does not match the expected format
#   [projects/*/locations/*/secrets/*/versions/*]
#
# That is a runtime failure on a credential change, discovered from a Password Safe
# error days after the address was written, so it is checked HERE instead. See
# gcp_service._write_regional_secret_sync, which produces exactly this shape.
_DBGCP_FA_SECRET = re.compile(
    r"^projects/[^/;]+/locations/([^/;]+)/secrets/[^/;]+/versions/[^/;]+$")
# The global form specifically, so it gets its own message. It is what every Secret
# Manager quickstart produces, so "not a resource name" would be actively misleading —
# and moving the secret does NOT help, because Google refuses it on the endpoint it was
# created through: "A secret created using Secret Manager's global endpoint are not
# supported even if it's stored in the same region."
_DBGCP_FA_SECRET_GLOBAL = re.compile(
    r"^projects/[^/;]+/secrets/[^/;]+(/versions/[^/;]+)?$")


def _validate_dbgcp_fa_secret(value: str, connection_name: str) -> None:
    """Raise unless ``value`` is a regional Secret Manager version IN THE INSTANCE'S
    REGION.

    Two checks, because Google enforces two rules and reports neither usefully at the
    time it matters. The shape must be regional (above), and the region must be the
    instance's own — the Data API reads the secret through the instance's regional
    endpoint, so a us-central1 instance cannot be handed a us-east1 secret. Checking the
    region here turns a first-rotation failure into an onboarding-time refusal, and the
    address already carries the instance's region in field 2, so there is nothing to
    look up."""
    match = _DBGCP_FA_SECRET.match(value)
    if not match:
        if _DBGCP_FA_SECRET_GLOBAL.match(value):
            raise PSResourceError(
                f"fasecret={value!r} is a GLOBAL Secret Manager secret, and the Cloud SQL "
                f"Data API accepts only a REGIONAL one — "
                f"'projects/<project>/locations/<region>/secrets/<name>/versions/<v>'. "
                f"This is the form every Secret Manager quickstart produces, and MOVING "
                f"the secret does not help: Google refuses a secret created through the "
                f"global endpoint even when it is stored in the right region. Create a "
                f"new regional secret instead (gcp_service.write_regional_secret), not "
                f"one from the Secrets page.")
        raise PSResourceError(
            f"fasecret={value!r} is not a REGIONAL Secret Manager version. "
            f"The Cloud SQL Data API accepts only "
            f"'projects/<project>/locations/<region>/secrets/<name>/versions/<v>' "
            f"and rejects anything else with \"does not match the expected format\". "
            f"Stage it with a regional secret (gcp_service.write_regional_secret), not "
            f"the Secrets page.")
    secret_region = (match.group(1) or "").strip().lower()
    parts = (connection_name or "").split(":")
    instance_region = (parts[1] if len(parts) > 2 else "").strip().lower()
    if instance_region and secret_region and secret_region != instance_region:
        raise PSResourceError(
            f"fasecret= names a secret in {secret_region!r} but the instance is in "
            f"{instance_region!r}. The Data API reads the secret through the INSTANCE'S "
            f"regional endpoint, so it must live in the instance's own region — a "
            f"mismatch is refused at the first rotation, days after this address was "
            f"written, by an error that names neither region.")


def _validate_dbgcp_dns_name(dns_name: str) -> None:
    """Raise PSResourceError unless ``dns_name`` is an address the plugin will parse.

    Mirrors the plugin's own pre-flight: length cap, known channel, a well-formed
    instance connection name, then the per-channel rule for each remaining positional
    field, then every trailing field as a recognised ``key=value`` option. Blank
    trailing fields are skipped, as the plugin skips them, so a trailing ';' is fine."""
    addr = (dns_name or "").strip()
    if not addr:
        raise PSResourceError(
            f"GCP Cloud SQL onboarding requires a dns_name of the form '{_DBGCP_FORMAT}'")
    if len(addr) > _DBGCP_MAX_ADDRESS:
        raise PSResourceError(
            f"managed system address is {len(addr)} characters, "
            f"{len(addr) - _DBGCP_MAX_ADDRESS} over the {_DBGCP_MAX_ADDRESS} character "
            f"limit the plugin enforces (Password Safe truncates the field at 255, and a "
            f"truncated address silently becomes a different, wrong address). The instance "
            f"connection name, the audience, and a 'fasecret' secret version (a full "
            f"regional resource name, ~110 characters) are the long fields.")

    fields = [f.strip() for f in addr.split(";")]
    channel = fields[0].lower()
    if channel not in _DBGCP_CHANNELS:
        raise PSResourceError(
            f"managed system address channel {fields[0]!r} is not recognised — use one of "
            f"{', '.join(sorted(_DBGCP_CHANNELS))}")
    if len(fields) < _DBGCP_POSITIONAL:
        raise PSResourceError(
            f"managed system address {addr!r} has {len(fields)} field(s), expected at least "
            f"{_DBGCP_POSITIONAL} — '{_DBGCP_FORMAT}'")

    if not _DBGCP_CONNECTION_NAME.match(fields[1]):
        raise PSResourceError(
            f"instance connection name {fields[1]!r} is not of the form "
            f"'project:region:instance' — get it with "
            f"\"gcloud sql instances describe <instance> --format='value(connectionName)'\"")

    database, audience, ssl_flag = fields[2], fields[3], fields[4]
    if channel == "admin-api":
        if database != "-":
            raise PSResourceError(
                f"the 'admin-api' channel opens no database connection, so field 3 must be "
                f"'-', not {database!r}")
    elif not database or database == "-":
        raise PSResourceError(
            f"the {channel!r} channel requires a database name in field 3")

    if channel == "cloud-run":
        if not audience or audience == "-":
            raise PSResourceError(
                "the 'cloud-run' channel requires the Cloud Run custom audience in field 4")
        low = audience.lower()
        if low.startswith("http://") or low.startswith("https://"):
            rest = audience.split("://", 1)[1]
            if any(c in rest for c in "/?#"):
                raise PSResourceError(
                    f"Cloud Run audience {audience!r} must be a bare origin — a path, query "
                    f"or fragment is rejected, because the audience doubles as the request "
                    f"target and is used verbatim as the token audience")
        if ssl_flag.lower() not in _DBGCP_SSL_FLAGS:
            raise PSResourceError(
                f"field 5 must be 'sslTRUE' or 'sslFALSE' on the 'cloud-run' channel, "
                f"not {ssl_flag!r}")
    else:
        if audience != "-":
            raise PSResourceError(
                f"field 4 (audience) applies only to the 'cloud-run' channel, so it must be "
                f"'-' on {channel!r}, not {audience!r}")
        if ssl_flag != "-":
            raise PSResourceError(
                f"the Cloud SQL Admin and Data APIs are always TLS, so field 5 must be '-' "
                f"on {channel!r}, not {ssl_flag!r} — the plugin rejects a value here rather "
                f"than letting anyone believe they disabled it")

    seen_options = set()
    for field in fields[_DBGCP_POSITIONAL:]:
        if not field:
            continue
        key, sep, value = field.partition("=")
        key = key.strip().lower()
        seen_options.add(key)
        if not sep or not key or key not in _DBGCP_OPTION_KEYS:
            raise PSResourceError(
                f"{field!r} in managed system address {addr!r} is not a recognised option — "
                f"options are key=value with one of: "
                f"{', '.join(sorted(_DBGCP_OPTION_KEYS))}. The plugin treats an unknown "
                f"option as an error, not as something to ignore.")
        required = _DBGCP_OPTION_CHANNEL.get(key)
        if required and required != channel:
            raise PSResourceError(
                f"the {key!r} option applies only to the {required!r} channel, but {addr!r} "
                f"is a {channel!r} address")
        if not value.strip():
            raise PSResourceError(
                f"the {key!r} option in managed system address {addr!r} has no value")
        value = value.strip()
        if key == "verifier" and value.lower() in _DBGCP_VERIFIER_ON and channel != "cloud-run":
            raise PSResourceError(
                f"'verifier=on' applies only to the 'cloud-run' channel, but {addr!r} is a "
                f"{channel!r} address. The option says the new password is pre-hashed on "
                f"the Resource Broker so the plaintext never reaches the wire, and only "
                f"cloud-run can honour that — the Cloud SQL APIs take the password in the "
                f"statement text. Accepting it here would report a protection that is not "
                f"happening. Drop it, or use 'verifier=off', which is legal everywhere.")
        if key == "iam" and value.lower() in _DBGCP_IAM_FALSE and channel == "data-api":
            raise PSResourceError(
                f"'iam=false' on the 'data-api' channel leaves {addr!r} with no way to "
                f"authenticate a database session at all: executeSql has no "
                f"plaintext-password field, and 'fasecret=' — the only other credential "
                f"the Data API can read — applies to SQL Server. Such an address onboards "
                f"cleanly and fails at the first rotation with an opaque Google 401. Use "
                f"'iam=true' on PostgreSQL/MySQL, 'fasecret=' on SQL Server, or the "
                f"'cloud-run' channel, which carries a real login.")
        if key == "fasecret":
            _validate_dbgcp_fa_secret(value, fields[1])

    # The two ways the Data API can authenticate a DATABASE session, and they are
    # alternatives: iam= says "mint an OAuth token per connection", fasecret= says "read
    # this stored password". Together they are not a stricter configuration, they are a
    # contradiction — and the way it happens in practice is a SQL Server address that
    # kept the postgres/mysql default. The engine is not in the address, so that
    # specific mistake cannot be named here; the contradiction can.
    if "iam" in seen_options and "fasecret" in seen_options:
        raise PSResourceError(
            f"managed system address {addr!r} carries both 'iam' and 'fasecret', which "
            f"are the two alternative ways the Data API authenticates a database "
            f"session — an OAuth token minted per connection, or a stored password. "
            f"SQL Server has no IAM database authentication at all, so it takes "
            f"'fasecret' alone; PostgreSQL and MySQL take 'iam=true' alone.")


def _ssm_account_name(name: str, suffix: str) -> str:
    """SSM custom-plugin managed-account name, ``{name};{suffix}``. The suffix is
    ``local`` for IAM-user mode, or the cross-account AssumeRole ARN for EC2 mode."""
    return f"{name or 'adminuser'};{suffix or 'local'}"


# A per-call override of the Password Safe credentials, for the POV feature. Four keys
# rather than three: the run-as user is part of the tenant's identity here, because the
# passwordsafe provider block requires it and it differs per tenant.
TENANT_KEYS = ("pscli_api_url", "pscli_client_id", "pscli_client_secret",
               "pscli_api_account_name")


def tenant_creds(api_url: str, client_id: str, client_secret: str,
                 api_account_name: str) -> dict:
    """The override dict :func:`_tf_env` accepts. A convenience, and one spelling."""
    return {"pscli_api_url": api_url, "pscli_client_id": client_id,
            "pscli_client_secret": client_secret,
            "pscli_api_account_name": api_account_name}


def _tf_env(extra_vars: Optional[dict] = None, tenant: Optional[dict] = None) -> dict:
    """Environment for Terraform calls. The provider OAuth credentials + the run-as
    user ride TF_VAR_* (the destroy path needs them too), as do per-apply secrets.

    ``tenant`` overrides those four for one call. A **partial** override is refused
    rather than merged: a managed system created against one customer's tenant with
    another's client id is the silent cross-tenant mistake the registry exists to prevent.
    """
    env = dict(os.environ)
    env["TF_IN_AUTOMATION"] = "1"
    env["TF_INPUT"] = "0"
    env["TF_CLI_ARGS"] = "-no-color"
    override = {}
    if tenant and all(str(tenant.get(k) or "").strip() for k in TENANT_KEYS):
        override = {k: str(tenant[k]).strip() for k in TENANT_KEYS}
    elif tenant:
        raise PSResourceError(
            "a Password Safe tenant override was supplied with only part of its "
            "credentials (URL, client id, client secret and run-as account are all "
            "required). Refusing rather than falling back to the configured tenant.")
    for cfg_key, tf_var in (
        ("pscli_api_url",          "TF_VAR_ps_url"),
        ("pscli_client_id",        "TF_VAR_ps_client_id"),
        ("pscli_client_secret",    "TF_VAR_ps_client_secret"),
        ("pscli_api_account_name", "TF_VAR_ps_api_account_name"),
    ):
        val = override.get(cfg_key) if override else _cfg(cfg_key)
        if val:
            env[tf_var] = val
    for var, val in (extra_vars or {}).items():
        if val is not None:
            env[f"TF_VAR_{var}"] = str(val)
    return env


def _provider_header(extra_vars: str = "") -> str:
    api_version = _cfg("passwordsafe_api_version") or "3.1"
    return f"""\
terraform {{
  required_providers {{
    passwordsafe = {{
      source  = "BeyondTrust/passwordsafe"
      version = "~> 1.0"
    }}
  }}
}}

variable "ps_url"              {{ sensitive = false }}
variable "ps_client_id"        {{ sensitive = true }}
variable "ps_client_secret"    {{ sensitive = true }}
variable "ps_api_account_name" {{ sensitive = false }}
{extra_vars}
provider "passwordsafe" {{
  url              = var.ps_url
  client_id        = var.ps_client_id
  client_secret    = var.ps_client_secret
  api_account_name = var.ps_api_account_name
  api_version      = {json.dumps(api_version)}
}}
"""


def _generate_managed_system_hcl(*, name: str, host_name: str, ip_address: str, port: int,
                                 functional_account_id: int, platform_id: int,
                                 entity_type_id: int, workgroup_id: str,
                                 managed_account_name: str, ssh_key_enforcement_mode: int,
                                 application_host_id: int = 0, method: str = "ssh",
                                 dns_name: str = "", emit_private_key: bool = True,
                                 dss_auto_management: bool = True,
                                 use_own_credentials: bool = False,
                                 timeout_value: int = 0) -> str:
    """HCL onboarding a VM as a managed system + its account. Two shapes via ``method``:

    * ``ssh`` (default) — traditional managed system keyed by host_name/ip on an SSH
      platform; the account's SSH private key + placeholder password ride sensitive
      TF_VARs and ``ssh_key_enforcement_mode`` enforces key-only auth.
    * ``ssm`` / ``azurevm`` / ``gcpvm`` — the cloud-native custom plugins ("AWS Systems
      Manager" / "Azure VM SSH Rotation" / "GCP VM SSH Rotation"): the managed system
      carries the plugin's address in ``dns_name`` (``{instance-id}:{region}`` for ssm,
      ``tenantId/subscriptionId/resourceGroup/vmName`` for azurevm, ``projectId/zone/
      instanceName`` for gcpvm — the field the plugin parses), a placeholder ip, and the
      custom-plugin platform (inherited from the functional account). No private key is
      pushed — Password Safe mints the SSH key via Change Password (over SSM SendCommand /
      Azure Run Command / GCE metadata) — so ``emit_private_key`` is False and the
      private-key TF_VAR is omitted entirely (a declared-but-unset required var fails
      apply under TF_INPUT=0).

    ``application_host_id`` (>0) routes management through a specific application host
    (the traditional Resource Broker path); 0 leaves it to the functional account's platform."""
    label = _safe_name(name)
    extra_vars = 'variable "ps_account_password"    { sensitive = true }\n'
    if emit_private_key:
        extra_vars += 'variable "ps_account_private_key" { sensitive = true }\n'
    header = _provider_header(extra_vars)

    sys_lines = [
        _line("workgroup_id", json.dumps(str(workgroup_id))),
        _line("entity_type_id", int(entity_type_id)),
        _line("host_name", json.dumps(host_name)),
    ]
    if method in _PLUGIN_METHODS and dns_name:
        sys_lines.append(_line("dns_name", json.dumps(dns_name)))
    if ip_address:
        sys_lines.append(_line("ip_address", json.dumps(ip_address)))
    sys_lines += [
        _line("platform_id", int(platform_id)),
        _line("port", int(port)),
        _line("functional_account_id", int(functional_account_id)),
        _line("auto_management_flag", "true"),
    ]
    if timeout_value and int(timeout_value) > 0:
        # Read by the PLUGIN, not by Password Safe as a socket timeout — a custom-plugin
        # platform never opens the connection itself. Deliberately unit-less here,
        # because the plugin families disagree: the AWS SSM plugins read milliseconds
        # (_DBSSM_PLUGIN_TIMEOUT_MS) while the GCP Cloud SQL and Azure Run Command
        # plugins read seconds (_DBGCP_PLUGIN_TIMEOUT_SECONDS,
        # _DBAZURE_PLUGIN_TIMEOUT_SECONDS). Naming this parameter after either unit is
        # how one caller ends up passing the other one's number.
        sys_lines.append(_line("timeout", int(timeout_value)))
    if method == "ssh":
        # Named for `ssh` rather than "not a plugin" because `password` is also
        # non-plugin and must NOT carry these: a Windows managed system has no remote
        # client type and no key-enforcement mode, and that method serves both families.
        sys_lines.append(_line("remote_client_type", '"ssh"'))
        sys_lines.append(_line("ssh_key_enforcement_mode", int(ssh_key_enforcement_mode)))
    if application_host_id and int(application_host_id) > 0:
        sys_lines.append(_line("application_host_id", int(application_host_id)))
        sys_lines.append(_line("is_application_host", "false"))
    sys_lines.append(_line("description", '"Auto-onboarded by Infrastructure Management Dashboard"'))

    acct_lines = [
        _line("system_name", f"passwordsafe_managed_system_by_workgroup.{label}.managed_system_name"),
        _line("account_name", json.dumps(managed_account_name)),
        _line("password", "var.ps_account_password"),
    ]
    if emit_private_key:
        acct_lines.append(_line("private_key", "var.ps_account_private_key"))
    acct_lines += [
        _line("dss_auto_management_flag", "true" if dss_auto_management else "false"),
        _line("auto_management_flag", "true"),
        _line("api_enabled", "true"),
    ]
    if use_own_credentials:
        # "Change Password Using Own Credentials". The DB custom plugins expose two change
        # actions and Password Safe picks between them from THIS flag: with it the account
        # rotates itself (Postgres ALTER USER on self / MySQL ALTER USER CURRENT_USER() /
        # SQL Server ALTER LOGIN … OLD_PASSWORD), which needs no privilege on the target;
        # without it Password Safe calls the via-functional-account action, which needs a
        # privileged DB login (CREATEROLE / CREATE USER / ALTER ANY LOGIN) that a
        # dashboard-provisioned server does not have. Omitted rather than emitted false so
        # existing managed accounts keep whatever they were onboarded with.
        acct_lines.append(_line("use_own_credentials", "true"))

    sys_block = "\n".join(sys_lines)
    acct_block = "\n".join(acct_lines)
    return header + f"""
resource "passwordsafe_managed_system_by_workgroup" {json.dumps(label)} {{
{sys_block}
}}

resource "passwordsafe_managed_account" {json.dumps(label)} {{
{acct_block}
}}

output "managed_system_id" {{
  value = passwordsafe_managed_system_by_workgroup.{label}.managed_system_id
}}

output "managed_account_id" {{
  value = passwordsafe_managed_account.{label}.id
}}
"""


# ── Terraform plumbing ────────────────────────────────────────────────────────

def _run_tf(args: list, work_dir: str, env: dict, timeout: int = 180) -> subprocess.CompletedProcess:
    """Run one terraform subcommand in ``work_dir``.

    ``init`` still takes ``terraform.plugin_cache_lock``. In the published image it is
    belt-and-braces — the provider comes from a read-only mirror nothing can write — but
    off-image (dev, or a run without /etc/terraform.tfrc) init downloads into a shared
    cache again, and parallel inits race to place the same binary (ETXTBSY). Same
    reasoning as terraform_pra_service._run_tf — see the longer note there."""
    def _go() -> subprocess.CompletedProcess:
        return subprocess.run(
            [_TERRAFORM] + args, cwd=work_dir, capture_output=True, text=True,
            timeout=timeout, env=env)

    if args and args[0] == "init":
        from .terraform import plugin_cache_lock
        with plugin_cache_lock():
            return _go()
    return _go()


def _scrub_state(tf_state_json: Optional[str]) -> Optional[str]:
    """Redact secret attribute values (password / private_key / passphrase / token)
    from state before it is stashed in the job. Destroy is by id, so values aren't
    needed. Fails CLOSED — drop the state rather than stash a plaintext secret."""
    if not tf_state_json:
        return None
    try:
        state = json.loads(tf_state_json)
        for res in state.get("resources", []):
            for inst in res.get("instances", []):
                attrs = inst.get("attributes") or {}
                for k in ("password", "private_key", "passphrase", "token"):
                    if attrs.get(k):
                        attrs[k] = _REDACTED
        return json.dumps(state)
    except Exception as exc:  # noqa: BLE001
        logger.error("PS: failed to scrub Terraform state — dropping it: %s", exc)
        return None


def _apply_hcl_sync(hcl: str, tf_vars: dict, tenant: Optional[dict] = None) -> dict:
    env = _tf_env(tf_vars, tenant)
    with tempfile.TemporaryDirectory(prefix="ps_tf_") as work_dir:
        Path(work_dir, "main.tf").write_text(hcl)
        init = _run_tf(["init", "-upgrade=false"], work_dir, env, timeout=60)
        if init.returncode != 0:
            raise PSResourceError(
                f"terraform init failed: {init.stderr.strip() or init.stdout.strip()}")
        apply = _run_tf(["apply", "-auto-approve"], work_dir, env, timeout=180)
        if apply.returncode != 0:
            raise PSResourceError(
                f"terraform apply failed: {apply.stderr.strip() or apply.stdout.strip()}")
        out = _run_tf(["output", "-json"], work_dir, env, timeout=30)
        outputs: dict = {}
        if out.returncode == 0 and out.stdout.strip():
            try:
                outputs = {k: v.get("value") for k, v in json.loads(out.stdout).items()}
            except (json.JSONDecodeError, AttributeError):
                pass
        state_path = Path(work_dir, "terraform.tfstate")
        tf_state_json = state_path.read_text() if state_path.exists() else None
        return {
            "managed_system_id": str(outputs.get("managed_system_id") or "") or None,
            "managed_account_id": str(outputs.get("managed_account_id") or "") or None,
            "tf_state_json": _scrub_state(tf_state_json),
        }


def _destroy_sync(tf_state_json: str, tenant: Optional[dict] = None) -> None:
    """Off-board: restore stored state + provider-only config and destroy (the
    managed account, then the managed system)."""
    try:
        json.loads(tf_state_json)
    except json.JSONDecodeError as e:
        raise PSResourceError(f"tf_state_json is not valid JSON: {e}") from e
    env = _tf_env(None, tenant)
    with tempfile.TemporaryDirectory(prefix="ps_tf_destroy_") as work_dir:
        Path(work_dir, "main.tf").write_text(_provider_header())
        Path(work_dir, "terraform.tfstate").write_text(tf_state_json)
        init = _run_tf(["init", "-upgrade=false"], work_dir, env, timeout=60)
        if init.returncode != 0:
            raise PSResourceError(
                f"terraform init (destroy) failed: {init.stderr.strip() or init.stdout.strip()}")
        destroy = _run_tf(["destroy", "-auto-approve", "-refresh=false"], work_dir, env, timeout=180)
        if destroy.returncode != 0:
            raise PSResourceError(
                f"terraform destroy failed: {destroy.stderr.strip() or destroy.stdout.strip()}")


# ── Public async API ──────────────────────────────────────────────────────────

async def register_managed_system(*, name: str, host_name: str, private_key: str = "",
                                   functional_account_id: int, platform_id: int,
                                   workgroup_id: str, ip_address: str = "", port: int = 22,
                                   entity_type_id: int = 1, managed_account_name: str = "adminuser",
                                   ssh_key_enforcement_mode: int = 2,
                                   application_host_id: int = 0, method: str = "ssh",
                                   dns_name: str = "", account_suffix: str = "",
                                   initial_password: str = "",
                                   use_own_credentials: bool = False,
                                   tenant: Optional[dict] = None) -> dict:
    """Onboard a VM as a Password Safe managed system + managed account.
    Returns ``{managed_system_id, managed_account_id, tf_state_json,
    initial_password_seeded}``.

    ``method="ssm"`` uses the AWS Systems Manager custom plugin: ``dns_name`` must be
    ``{instance-id}:{region}``, the account name becomes ``{managed_account_name};{suffix}``
    (suffix ``local`` for IAM-user mode or an AssumeRole ARN for EC2 mode), no private key
    is pushed, and ip defaults to a ``127.0.0.1`` placeholder.

    ``method="azurevm"`` uses the Azure VM SSH Rotation custom plugin: ``dns_name`` must be
    ``tenantId/subscriptionId/resourceGroup/vmName`` (four slash-separated parts, the field
    the plugin parses), the account name is the plain Linux user (no suffix), no private key
    is pushed, and ip defaults to a ``127.0.0.1`` placeholder.

    ``method="gcpvm"`` uses the GCP VM SSH Rotation custom plugin: ``dns_name`` must be
    ``projectId/zone/instanceName`` (three slash-separated parts, the field the plugin parses),
    the account name is the plain Linux user (no suffix), no private key is pushed, and ip
    defaults to a ``127.0.0.1`` placeholder.

    ``method="dbssm"`` uses the cloud-DB "{engine} SSM Custom Plugin": ``dns_name`` must be
    the per-engine ``;``-packed address — ``instanceId;region;dbEndpoint;certPath;assumeRole``
    for mssql (5 fields, NO database segment), plus a ``databaseName`` fourth field for psql
    (6), plus a trailing ``sslTRUE|sslFALSE`` for mysql (7); ``port`` is the real DB port
    (never appended to the address), ``managed_account_name`` is the dedicated DB user, and
    the account is password-managed (no SSH DSS key). The packed address rides ``dns_name``
    alone; ``ip_address`` defaults to the same ``127.0.0.1`` placeholder as every other
    plugin method, and a non-IP value is refused up front — Password Safe rejects a create
    with no ip ("The field 'IPAddress' is required.") and equally rejects one that is not a
    literal IP ("Bad IP value: '<address>' in 'IPAddress' field"), both seen live.

    ``method="dbazure"`` uses the cloud-DB "{engine} Azure Run Command Plugin": ``dns_name``
    must be ``vmName;resourceGroup;subscriptionId;tenantId;dbHost;dbName;certPath;sslTRUE|sslFALSE``
    (eight ``;``-separated parts — the jump VM identity plus the DB host/name and the broker
    cert path/SSL flag), ``port`` is the real DB port, ``managed_account_name`` is the dedicated
    DB user the functional-account DB login rotates, and the account is password-managed.

    ``method="pravault"`` uses the "PRA Vault Username Password" / "PRA Vault Token" plugins:
    ``host_name`` must be the PRA appliance URL and ``managed_account_name`` the exact PRA
    Vault account name; ``dns_name`` defaults to ``host_name`` (the Username Password
    platform requires a DnsName on create); the account is password-managed. A caller that
    must NOT share a managed system with the other PRA Vault callers — one onboarding
    against a different platform, above all — passes the URL as ``dns_name`` and a unique
    label as ``host_name``, because the managed system is named after its HostName and the
    account attaches to its system by name (see the branch below).

    ``method="k8ssa"`` uses the "Kubernetes Service Account Token" plugin: ``dns_name`` must
    be a cluster address (``eks;<region>;<cluster>``, ``aks;<subscriptionId>;<resourceGroup>;
    <cluster>``, ``gke;<projectId>;<location>;<cluster>`` or ``k8s;<apiServerUrl>``) plus
    optional trailing ``;key=value`` options, at most 249 characters;
    ``managed_account_name`` is ``<namespace>/<serviceaccount>``. The account is
    password-managed — the credential IS the bearer token — but a bearer token cannot be
    seeded (see ``initial_password``), so the first rotation is what populates it.

    ``method="certificate"`` uses the "Certificate" plugin: ``dns_name`` carries the whole
    certificate profile — ``<backend>?key=value&...``, e.g.
    ``gcpcas?project=<p>&location=us-central1&pool=<pool>&lifetime=24h&biurl=<url>&owner=<gid>``
    — because the plugin's appsettings.json ships inside the .psplugin and a Password Safe
    Cloud tenant cannot edit it. ``host_name`` is a human asset label (substituted into
    ``{system}`` in the bundle's secret title), ``port`` is forced to 0, and the account is
    password-managed where the "password" IS the PKCS#12 passphrase — which is generated by
    the account's own password policy on the first Change Password, so it is never seeded
    here. The certificate itself lands in a Secrets Safe file secret; both halves are needed
    and both are governed.

    ``method="password"`` is the traditional (non-plugin) managed system reached at its own
    ``host_name``/``ip_address``, whose account is PASSWORD-managed: no ``private_key``, no
    DSS auto-management, and none of the SSH client fields — so it serves a Windows guest as
    readily as a Linux one. For callers that hold a working login and no key material, which
    is every lab guest reached through the platform's own stored credentials.

    ``method="ssh"`` (default) keeps the traditional key-managed flow and requires
    ``private_key``.

    ``initial_password`` seeds the managed account with a credential the caller already
    holds, instead of the throwaway placeholder. Only meaningful for a password-managed
    method, and only up to ``_MAX_SEED_PASSWORD_LEN`` — the create API rejects anything
    longer with a 400 that fails the whole apply, so an over-long value is DROPPED for a
    placeholder rather than passed through. The returned ``initial_password_seeded`` says
    which happened: on False, Password Safe holds a placeholder that authenticates to
    nothing until the account is rotated, and it is the caller's job to make that rotation
    happen. A k8s ServiceAccount token is always over the cap."""
    method = (method or "ssh").lower()
    # The provider requires a password even for a key-managed account; supply a strong
    # placeholder it never uses (the real credential is the SSH key, managed by Password Safe).
    # An over-long seed is dropped rather than passed through: the create API rejects it with
    # a 400 that fails the whole apply, so sending it would cost the managed system too.
    seeded = bool(initial_password) and len(initial_password) <= _MAX_SEED_PASSWORD_LEN
    if initial_password and not seeded:
        # Logs the overage, never the length: a length is derived from the credential and
        # narrows a guess at it, and "over the cap" is the whole actionable content. The
        # cap itself is not interpolated either — passing a value whose IDENTIFIER matches
        # /password/ into a log sink trips CodeQL's name-based sensitive-data heuristic,
        # and the number is already in the constant, the docstring and the design note.
        logger.info(
            "PS: not seeding the %s managed account for %r — the credential is longer than "
            "the create API's maximum; the first rotation will populate it", method, name)
    tf_vars = {"ps_account_password":
               initial_password if seeded else secrets.token_urlsafe(24)}
    if method == "ssm":
        if not dns_name or ":" not in dns_name:
            raise PSResourceError(
                "SSM onboarding requires a dns_name of the form '{instance-id}:{region}'")
        hcl = _generate_managed_system_hcl(
            name=name, host_name=host_name, ip_address=ip_address or "127.0.0.1", port=port,
            functional_account_id=functional_account_id, platform_id=platform_id,
            entity_type_id=entity_type_id, workgroup_id=workgroup_id,
            managed_account_name=_ssm_account_name(managed_account_name, account_suffix),
            ssh_key_enforcement_mode=ssh_key_enforcement_mode,
            application_host_id=application_host_id,
            method="ssm", dns_name=dns_name, emit_private_key=False)
    elif method == "azurevm":
        if not dns_name or dns_name.count("/") != 3:
            raise PSResourceError(
                "Azure VM SSH Rotation onboarding requires a dns_name of the form "
                "'tenantId/subscriptionId/resourceGroup/vmName'")
        hcl = _generate_managed_system_hcl(
            name=name, host_name=host_name, ip_address=ip_address or "127.0.0.1", port=port,
            functional_account_id=functional_account_id, platform_id=platform_id,
            entity_type_id=entity_type_id, workgroup_id=workgroup_id,
            managed_account_name=managed_account_name,
            ssh_key_enforcement_mode=ssh_key_enforcement_mode,
            application_host_id=application_host_id,
            method="azurevm", dns_name=dns_name, emit_private_key=False)
    elif method == "gcpvm":
        if not dns_name or dns_name.count("/") != 2:
            raise PSResourceError(
                "GCP VM SSH Rotation onboarding requires a dns_name of the form "
                "'projectId/zone/instanceName'")
        hcl = _generate_managed_system_hcl(
            name=name, host_name=host_name, ip_address=ip_address or "127.0.0.1", port=port,
            functional_account_id=functional_account_id, platform_id=platform_id,
            entity_type_id=entity_type_id, workgroup_id=workgroup_id,
            managed_account_name=managed_account_name,
            ssh_key_enforcement_mode=ssh_key_enforcement_mode,
            application_host_id=application_host_id,
            method="gcpvm", dns_name=dns_name, emit_private_key=False)
    elif method == "dbssm":
        # Cloud-DB via the "{engine} SSM Custom Plugin": Password Safe reaches the
        # private RDS instance by running the DB client on a jump host over SSM.
        # dns_name is the ';'-packed per-engine address (see the grammar above), the
        # real DB port applies, and the account is PASSWORD-managed (no SSH key).
        # The ip is the same 127.0.0.1 placeholder every other plugin method uses.
        # Two live 400s close off both alternatives: registering with NO ip is "The
        # field 'IPAddress' is required." (2026-08-25), and putting the packed address
        # in the ip field is "Bad IP value: '<address>' in 'IPAddress' field"
        # (2026-08-27) — Password Safe validates IPAddress as a literal IP, so a value
        # that is both an address the plugin parses and an IP cannot exist. The plugin
        # reads the packed address off DnsName, which has no such validation; the
        # earlier "a bare IP crashes the parse" reading came from systems whose DnsName
        # ALSO carried the old pre-per-engine six-field address, which explains the
        # crash on its own.
        _validate_dbssm_dns_name(dns_name)
        _check_address_length(dns_name, "dbssm")
        _validate_ip_field(ip_address, "DB SSM")
        hcl = _generate_managed_system_hcl(
            name=name, host_name=host_name, ip_address=ip_address or "127.0.0.1", port=port,
            functional_account_id=functional_account_id, platform_id=platform_id,
            entity_type_id=entity_type_id, workgroup_id=workgroup_id,
            managed_account_name=managed_account_name,
            ssh_key_enforcement_mode=ssh_key_enforcement_mode,
            application_host_id=application_host_id,
            method="dbssm", dns_name=dns_name, emit_private_key=False,
            dss_auto_management=False, use_own_credentials=use_own_credentials,
            timeout_value=_DBSSM_PLUGIN_TIMEOUT_MS)
    elif method == "dbazure":
        # Cloud-DB via the "{engine} Azure Run Command Plugin": Password Safe reaches
        # the private Azure DB by running the DB client on a jump VM over Azure VM Run
        # Command. dns_name is eight ``;``-separated fields the plugin parses, ip is a
        # placeholder, the real DB port applies, and the account is PASSWORD-managed
        # (a dedicated managed user the functional-account DB login rotates).
        if not dns_name or dns_name.count(";") != 7:
            raise PSResourceError(
                "DB Azure Run Command onboarding requires a dns_name of the form "
                "'vmName;resourceGroup;subscriptionId;tenantId;dbHost;dbName;certPath;"
                "sslTRUE|sslFALSE'")
        _check_address_length(dns_name, "dbazure")
        hcl = _generate_managed_system_hcl(
            name=name, host_name=host_name, ip_address=ip_address or "127.0.0.1", port=port,
            functional_account_id=functional_account_id, platform_id=platform_id,
            entity_type_id=entity_type_id, workgroup_id=workgroup_id,
            managed_account_name=managed_account_name,
            ssh_key_enforcement_mode=ssh_key_enforcement_mode,
            application_host_id=application_host_id,
            method="dbazure", dns_name=dns_name, emit_private_key=False,
            dss_auto_management=False, use_own_credentials=use_own_credentials,
            timeout_value=_DBAZURE_PLUGIN_TIMEOUT_SECONDS)
    elif method == "dbgcp":
        # Cloud-DB via the "GCP Cloud SQL {engine}" plugins. Unlike its two DB siblings
        # there is no jump host: the plugin reaches a private-IP instance through the
        # Cloud SQL Data API, so the address carries no host, no cert path and no key
        # material — five positional fields plus optional key=value options. ip is a
        # placeholder, the real DB port applies, and the account is PASSWORD-managed
        # (a dedicated managed user the functional account's IAM identity rotates).
        _validate_dbgcp_dns_name(dns_name)
        hcl = _generate_managed_system_hcl(
            name=name, host_name=host_name, ip_address=ip_address or "127.0.0.1", port=port,
            functional_account_id=functional_account_id, platform_id=platform_id,
            entity_type_id=entity_type_id, workgroup_id=workgroup_id,
            managed_account_name=managed_account_name,
            ssh_key_enforcement_mode=ssh_key_enforcement_mode,
            application_host_id=application_host_id,
            method="dbgcp", dns_name=dns_name, emit_private_key=False,
            dss_auto_management=False, use_own_credentials=use_own_credentials,
            timeout_value=_DBGCP_PLUGIN_TIMEOUT_SECONDS)
    elif method == "pravault":
        # "PRA Vault *" plugins: Password Safe PATCHes the rotated credential into a
        # PRA Vault account via the PRA Config API. The managed account name is the
        # exact PRA Vault account name, and the PRA appliance URL rides in BOTH
        # host_name and dns_name by default: the "PRA Vault Username Password" platform's
        # create API rejects a system without a DnsName (live 400 "DnsName is required" —
        # the field is required on the platform, unlike "PRA Vault Token"), and the
        # plugin walks the populated host fields in Password Safe's order, so a second
        # copy of the URL is at worst never read. Password-managed.
        #
        # **A caller that needs its own managed system must pass the URL as ``dns_name``
        # and a unique label as ``host_name``.** Password Safe names a workgroup-created
        # managed system after its HostName, and ``passwordsafe_managed_account`` attaches
        # to its system BY NAME (the provider has no system_id argument — see
        # ``_generate_managed_system_hcl``). So every caller that puts the appliance URL in
        # host_name lands on one shared system name, and an account created against it goes
        # to whichever same-named system Password Safe resolves first — across PLATFORMS.
        # Measured live: the k8s mirror created its "PRA Vault Token" system, and its
        # account was then created on the pre-existing cloud-DB "PRA Vault Username
        # Password" system of the same name, which the SyncedAccounts platform guard
        # caught (ps_api_service.link_synced_account) after the registration had already
        # reported the account created.
        if not host_name:
            raise PSResourceError(
                "PRA Vault onboarding requires host_name set to the PRA appliance URL "
                "(or to a unique system label, with the URL in dns_name)")
        hcl = _generate_managed_system_hcl(
            name=name, host_name=host_name, ip_address=ip_address or "127.0.0.1",
            port=port or 443,
            functional_account_id=functional_account_id, platform_id=platform_id,
            entity_type_id=entity_type_id, workgroup_id=workgroup_id,
            managed_account_name=managed_account_name,
            ssh_key_enforcement_mode=ssh_key_enforcement_mode,
            application_host_id=application_host_id,
            method="pravault", dns_name=dns_name or host_name, emit_private_key=False,
            dss_auto_management=False)
    elif method == "k8ssa":
        # "Kubernetes Service Account Token" plugin: dns_name carries the cluster
        # address plus trailing ;key=value options, and the managed account name is
        # "<namespace>/<serviceaccount>". host_name stays a human label and ip_address
        # the 127.0.0.1 placeholder — the plugin iterates every host Password Safe
        # supplies and skips the ones that do not parse as a cluster address, so the
        # two non-addresses cost nothing. Port is irrelevant (the API server port is
        # part of the endpoint URL). Password-managed: the credential is the bearer
        # token itself, so no private key and no DSS auto-management.
        _validate_k8ssa_dns_name(dns_name)
        hcl = _generate_managed_system_hcl(
            name=name, host_name=host_name, ip_address=ip_address or "127.0.0.1",
            port=port or 443,
            functional_account_id=functional_account_id, platform_id=platform_id,
            entity_type_id=entity_type_id, workgroup_id=workgroup_id,
            managed_account_name=managed_account_name,
            ssh_key_enforcement_mode=ssh_key_enforcement_mode,
            application_host_id=application_host_id,
            method="k8ssa", dns_name=dns_name, emit_private_key=False,
            dss_auto_management=False)
    elif method == "certificate":
        # "Certificate" plugin: dns_name carries the WHOLE certificate profile — the CA
        # backend plus its options, the Secrets Safe destination, and any relying-party
        # publisher — because appsettings.json ships inside the .psplugin and a Password
        # Safe Cloud tenant cannot edit it. host_name stays a human label (the asset name
        # the plugin substitutes into {system} in the bundle's secret title) and
        # ip_address the 127.0.0.1 placeholder, since there is no host to reach: the CA
        # endpoint is named in the profile's own options.
        #
        # Port 0, deliberately — the platform does not use one, and the CLI packager's
        # 5432 default (inherited from the PostgreSQL plugin it was written for) is the
        # documented mistake. ``timeout`` is read by the PLUGIN, not as a socket timeout;
        # key generation plus a CA round trip is slower than a password change.
        #
        # Password-managed, and NEVER seeded: the credential is the PKCS#12 passphrase,
        # which Password Safe generates from the account's password policy and hands to
        # the plugin on the first Change Password. Seeding one here would hand the account
        # a passphrase that opens nothing.
        _validate_certificate_dns_name(dns_name)
        _check_address_length(dns_name, "certificate")
        _validate_ip_field(ip_address, "Certificate")
        if use_own_credentials:
            # The plugin declares "Change Managed Account Credentials (using own
            # credentials)" as NotSupported in GetActionDetails: a certificate identity
            # holds no CA credential and cannot enroll for itself. Setting the flag would
            # make Password Safe call the action that always fails.
            raise PSResourceError(
                "a Certificate managed account cannot change its own credentials — "
                "enrollment authority belongs to the functional account, and the plugin "
                "reports NotSupported for the own-credentials action")
        hcl = _generate_managed_system_hcl(
            name=name, host_name=host_name, ip_address=ip_address or "127.0.0.1", port=0,
            functional_account_id=functional_account_id, platform_id=platform_id,
            entity_type_id=entity_type_id, workgroup_id=workgroup_id,
            managed_account_name=managed_account_name,
            ssh_key_enforcement_mode=ssh_key_enforcement_mode,
            application_host_id=application_host_id,
            method="certificate", dns_name=dns_name, emit_private_key=False,
            dss_auto_management=False,
            timeout_value=_CERTIFICATE_PLUGIN_TIMEOUT_SECONDS)
    elif method == "password":
        # Same shape as `ssh` minus the key. A caller with a working login and no key
        # material used to fall through to the branch below and be refused for a "VM
        # keypair secret" it does not have and never could -- which read as a
        # misconfiguration rather than as a method that did not exist yet.
        hcl = _generate_managed_system_hcl(
            name=name, host_name=host_name, ip_address=ip_address, port=port,
            functional_account_id=functional_account_id, platform_id=platform_id,
            entity_type_id=entity_type_id, workgroup_id=workgroup_id,
            managed_account_name=managed_account_name,
            ssh_key_enforcement_mode=ssh_key_enforcement_mode,
            application_host_id=application_host_id,
            method="password", emit_private_key=False, dss_auto_management=False)
    else:
        if not private_key:
            raise PSResourceError(
                "no SSH private key available for the managed account — Password Safe "
                "manages the account by key; check the VM keypair secret. A caller that "
                "manages the account by PASSWORD wants method='password'.")
        hcl = _generate_managed_system_hcl(
            name=name, host_name=host_name, ip_address=ip_address, port=port,
            functional_account_id=functional_account_id, platform_id=platform_id,
            entity_type_id=entity_type_id, workgroup_id=workgroup_id,
            managed_account_name=managed_account_name,
            ssh_key_enforcement_mode=ssh_key_enforcement_mode,
            application_host_id=application_host_id, method="ssh", emit_private_key=True)
        tf_vars["ps_account_private_key"] = private_key
    out = await asyncio.to_thread(_apply_hcl_sync, hcl, tf_vars, tenant)
    out["initial_password_seeded"] = seeded
    return out


async def deregister(tf_state_json: str, tenant: Optional[dict] = None) -> None:
    """Off-board a managed system + account previously registered (best-effort).

    ``tenant`` must be the SAME one it was registered against — a destroy pointed at
    another tenant authenticates fine, removes nothing, and reports success."""
    await asyncio.to_thread(_destroy_sync, tf_state_json, tenant)
