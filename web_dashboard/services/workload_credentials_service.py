"""BeyondTrust Workload Credentials (WC / "SMoP") client.

The dashboard's third credential posture, alongside the static keys in
``app_config`` and the Entitle machine-identity gate. WC mints **short-lived**
AWS and Azure credentials on demand, so the standing cloud secret stops existing
rather than merely being time-boxed.

Nothing here changes behaviour until an operator turns it on:
``workload_credentials_enabled`` gates the whole module and every per-cloud flag
defaults off. A community install with no BeyondTrust products never reaches
this code.

Shape notes, because two of them are easy to get wrong
------------------------------------------------------
**Synchronous, deliberately.** ``secrets_backend_service``'s dispatch tables are
sync (callers push them off the event loop with ``asyncio.to_thread``), and
``aws_service._aws_kwargs`` is sync too. An async HTTP layer would force a bridge
at both. Timeouts stay short because that thread pool is small and one slow
external call has wedged this app before.

**Two auth modes, and the second one stores nothing.** ``wlc_auth_mode`` is
either ``pat`` (a stored Personal Access Token) or ``workload`` (this container's
own cloud identity, trusted by a **Workload Identity** registered in
Pathfinder). The second removes the last standing credential this feature
needed, and it is **not Azure-only** — ``wlc_identity_platform`` selects which
of Azure, GCP or AWS vouches for the container, because
``docs/cloud-hosting.md`` documents the dashboard running on all three. See the
Auth section below.

**The API version is a header, not a path.** ``bt-secrets-api-version`` is
mandatory; omit it and requests fail in a way that reads like an auth problem.
The default matches the shipping Terraform provider's ``DefaultAPIVersion``.

The path grammar mirrors the provider's ``BuildPath``::

    /site/{site-id}/secrets[/{path-version}]{endpoint}

with an optional ``?folder=`` query for anything addressed by folder + name.

**Confirmed against a live site, 2026-08-21.** This was the one part that could not be
settled from the provider, which manages configuration and never calls ``generate``; the
vendor wiki documented two incompatible shapes. A real issuance from
``POST /dynamic/{name}/generate?folder={folder}`` returned the credential nested under a
``secret`` object in camelCase, with ``accessKeyId`` / ``secretAccessKey`` /
``sessionToken`` / ``leaseId`` / ``expiration`` plus ``credentialType`` and ``type``. The
requested TTL of 3600 came back intact, so AWS's one-hour role-chaining limit does not
bite at that value even though this is a three-hop chain. See
``tests/test_workload_credentials.py`` for the recorded payload.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Matches the Terraform provider's client.DefaultAPIVersion. Date-based; a newer
# value changes response shapes, so it is config-overridable rather than pinned.
DEFAULT_API_VERSION = "2026-04-28"
DEFAULT_API_URL = "https://api.beyondtrust.io"

# Short on purpose — see the module docstring on the thread pool.
_TIMEOUT_SECONDS = 15.0


class WorkloadCredentialsError(Exception):
    """Any failure talking to Workload Credentials.

    Raised rather than returning an empty value. A credential fetch that fails
    quietly is indistinguishable from "this cloud is on the static tier", which
    is the most confusing state this feature could produce.
    """


# ── Config ────────────────────────────────────────────────────────────────────

def _cfg(key: str, fallback: str = "") -> str:
    """Config value with the usual DB then settings precedence."""
    try:
        from . import config_service
        val = config_service.get(key)
        if val:
            return val
    except Exception:
        pass
    try:
        from ..config import settings
        return str(getattr(settings, key, "") or fallback)
    except Exception:
        return fallback


def _raw_cfg(key: str) -> str:
    """The **stored** value for ``key``, with external references left unresolved.

    ``_cfg`` goes through ``config_service.get``, which transparently resolves a
    ``wlc://`` reference by calling this module — fine for every key except the one
    this module authenticates with. Only the DB is consulted (no ``settings``
    fallback): a reference can only get there by being migrated, and an environment
    variable is never one.
    """
    try:
        from . import config_service
        return config_service.get_raw(key) or ""
    except Exception:
        return ""


def pat_is_self_referential() -> bool:
    """True when ``wlc_pat`` holds a ``wlc://`` reference — the token that unlocks
    Workload Credentials, stored inside Workload Credentials.

    Reachable, and only this way: migrate the PAT into the ``wlc`` backend while on
    workload-identity auth (where nothing reads it), then switch back to ``pat``.
    Resolving it would then call in here, which would resolve it again; the recursion
    bottoms out in ``config_service``'s catch-all and yields ``""``, so the install
    reports a *missing* PAT for one that is plainly set. Detected instead, and
    refused with the actual reason.
    """
    return _raw_cfg("wlc_pat").startswith("wlc://")


def _enabled() -> bool:
    try:
        from . import config_service
        return config_service.get_bool("workload_credentials_enabled", default=False)
    except Exception:
        return False


def configured() -> bool:
    """True when the master flag is on and the required values are set.

    Checked before any request so a half-configured install produces one clear
    message instead of an HTTP error per call site. What "required" means depends
    on the auth mode — see :func:`_missing`.
    """
    if not _enabled():
        return False
    return not _missing()


def _missing() -> list:
    """Which required settings are blank, for the error message.

    **Auth-mode aware.** A message naming ``wlc_pat`` on an install running on a
    workload identity sends an operator hunting for a token they are deliberately
    not holding, which is the exact confusion this mode exists to end.
    """
    out = []
    if not _cfg("wlc_site_id"):
        out.append("wlc_site_id")
    if auth_mode() == AUTH_MODE_WORKLOAD:
        if not _cfg("wlc_service_name"):
            out.append("wlc_service_name")
        if identity_platform() in FILE_PLATFORMS:
            # The audience is not this container's to choose on these platforms —
            # it is baked into the projected token by whoever configured the
            # service account. What CAN be missing is the token itself.
            if not identity_token_path():
                out.append("wlc_identity_token_file")
        elif not identity_audience():
            out.append("wlc_identity_audience")
    elif not (_raw_cfg("wlc_pat") or _cfg("wlc_pat")):
        # Raw first, and not only to avoid the resolve: a self-referential PAT IS
        # set, and reporting it as missing would send the operator to paste in a
        # token that is already there. `_auth_headers` names the real problem.
        out.append("wlc_pat")
    return out


def missing_settings() -> list:
    """:func:`_missing`, for callers outside this module.

    Its three callers each named ``wlc_pat`` in a literal of their own, which is
    wrong the moment an install authenticates with a workload identity instead —
    and a "set wlc_pat" message is the worst possible advice there. They ask here
    now.
    """
    return _missing()


# ── Auth ──────────────────────────────────────────────────────────────────────
#
# Two ways to present this dashboard to Workload Credentials, and the second one
# is why this section is long.
#
# ``pat``    A Personal Access Token minted in Pathfinder and stored encrypted in
#            ``app_config``. Long-lived, and the one standing credential this
#            feature never removed: WC collapsed three cloud keys into one
#            platform token rather than into nothing.
#
# ``workload``
#            **Nothing stored at all.** The container's own cloud identity
#            produces a short-lived OIDC token at call time, and Pathfinder
#            accepts it because a **Workload Identity** registered there names
#            that identity's issuer and a constraint on its claims. There is no
#            secret in ``app_config``, none in the deployment template, and
#            nothing to rotate. Which cloud issues the token is
#            ``wlc_identity_platform``; see the platform block below.
#
# **Registering the trust is a GUI action in Pathfinder and has no client here,
# on purpose.** Administration → Workload Identities takes the issuer, the
# constraint on ``sub`` and the site. A dashboard that could register its own
# trust would be holding a credential that creates credentials, which is the
# thing this mode exists to get rid of. The registration's **Service Name** is
# the only part that comes back here: it travels on every request as
# ``X-BT-Service-Name``, telling the platform which registration to evaluate the
# token against.
#
# Pathfinder registers three issuer categories — GitHub Actions, Azure Entra ID
# and a **Custom IDP** with explicit claim conditions. An earlier version of this
# module wired only the Azure one, on the reasoning that "the thing being
# authenticated is an Azure-hosted container".
#
# **That reasoning was too narrow, and `docs/cloud-hosting.md` is the refutation:**
# this dashboard is documented to run as a managed container on Azure Container
# Apps, GCP Cloud Run **or** AWS ECS. Wiring only Azure left two of the three
# documented hosting options unable to use the mode that stores nothing — not
# because the mechanism is Azure's, but because nothing here asked the other
# platforms for a token. Every major cloud issues OIDC tokens to a workload
# identity; the Custom IDP registration type is what accepts them.
#
# So the mode is ``workload`` now rather than ``entra``, and it takes a platform.
# ``entra`` is still accepted as a stored value — see :func:`normalise_auth_mode`.

AUTH_MODE_PAT = "pat"
AUTH_MODE_WORKLOAD = "workload"
# Deprecated spelling of AUTH_MODE_WORKLOAD, kept because installs have it stored
# in app_config. Never written any more; always normalised away on read.
AUTH_MODE_ENTRA = "entra"
VALID_AUTH_MODES = (AUTH_MODE_PAT, AUTH_MODE_WORKLOAD)

# ── Identity platforms ───────────────────────────────────────────────────────
#
# What differs per platform is only WHERE the token comes from and HOW its expiry
# is read. What it IS — an OIDC JWT this container did not have to be given — is
# the same everywhere, which is why one mode covers all of them.
#
#   azure  IDENTITY_ENDPOINT/IDENTITY_HEADER (Container Apps, App Service) else
#          IMDS. Returns a JSON envelope carrying access_token and expires_on.
#   gcp    The metadata server's instance identity endpoint. Cloud Run, GCE and
#          GKE all serve it. Returns the JWT as PLAIN TEXT, so the expiry has to
#          come out of the token itself.
#   aws    A projected web-identity token FILE. There is no OIDC endpoint on
#          IMDS: EC2 and ECS hand out SigV4 credentials and a signed instance
#          identity document, neither of which is a JWT. EKS is what projects a
#          real one (IRSA, or Pod Identity) — so on plain ECS this platform has
#          nothing to read and `pat` remains the only mode. `_missing` says so.
#   file   Any other projected token on disk, the Kubernetes ServiceAccount
#          token being the one every cluster already mounts.
PLATFORM_AZURE = "azure"
PLATFORM_GCP = "gcp"
PLATFORM_AWS = "aws"
PLATFORM_FILE = "file"
VALID_IDENTITY_PLATFORMS = (PLATFORM_AZURE, PLATFORM_GCP, PLATFORM_AWS,
                            PLATFORM_FILE)
# The platforms whose token is a file rather than an HTTP call. Re-read every
# time rather than memoised: the platform rotates these in place, the read is
# local, and a memo would be the only thing capable of serving a stale one.
FILE_PLATFORMS = (PLATFORM_AWS, PLATFORM_FILE)

# Azure's link-local instance-metadata endpoint. The FALLBACK, not the default:
# Container Apps and App Service inject a per-replica ``IDENTITY_ENDPOINT`` plus
# an ``IDENTITY_HEADER`` secret instead, and 169.254.169.254 is not reachable
# from a Container App at all. Preferring the injected pair is what makes this
# work on the runtime the reference install actually uses.
_IMDS_TOKEN_URL = "http://169.254.169.254/metadata/identity/oauth2/token"
_IDENTITY_API_VERSION = "2019-08-01"

# GCP's metadata server. `format=full` includes the instance details a Custom IDP
# claim condition can assert on, so the registration can be about THIS revision
# rather than merely the project.
_GCP_IDENTITY_URL = ("http://metadata.google.internal/computeMetadata/v1/instance/"
                     "service-accounts/default/identity")
# IRSA sets the first; EKS Pod Identity the second. Both are the mechanism
# sts:AssumeRoleWithWebIdentity already consumes, so a cluster configured for
# either is configured for this.
_AWS_TOKEN_FILE_VARS = ("AWS_WEB_IDENTITY_TOKEN_FILE",
                        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE")
# The projected ServiceAccount token every Kubernetes cluster mounts.
_K8S_TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"  # noqa: S105

# Re-fetch this long before the platform's stated expiry. Entra tokens run about
# an hour; five minutes of margin covers a slow call that starts just under the
# wire. Unlike a dynamic secret, fetching one of these is FREE and unmetered, so
# the margin can be generous — nothing here is billed.
_TOKEN_MARGIN_SECONDS = 300

# Process-local, and that is correct here where it would be wrong for a lease.
# ``workload_credential_lease`` lives in the database because each issuance is
# BILLED and three processes must not buy three credentials. A managed-identity
# token costs nothing, so a per-process memo is just a cache; the worst a second
# process can do is fetch its own copy.
_token_cache: dict = {"key": "", "token": "", "expires_at": 0.0}


def normalise_auth_mode(raw: str) -> str:
    """``pat`` or ``workload``, from whatever is stored or submitted. Pure.

    **``entra`` normalises to ``workload``.** Installs configured before this mode
    covered more than Azure have that value in ``app_config``, and a rename that
    silently demoted them to stored-token auth would break the one mode that has
    no stored token to fall back on. Accepted on read, never written.

    Anything unrecognised reads as ``pat``. Falling back to the stored-token path
    rather than to the identity path is deliberate: a typo should degrade to the
    mode whose failure is a plain 401, not to one that goes looking for a metadata
    endpoint and reports something about a cloud on an install that never
    mentioned one.
    """
    mode = (raw or AUTH_MODE_PAT).strip().lower()
    if mode == AUTH_MODE_ENTRA:
        return AUTH_MODE_WORKLOAD
    return mode if mode in VALID_AUTH_MODES else AUTH_MODE_PAT


def auth_mode() -> str:
    """This install's auth mode, normalised. See :func:`normalise_auth_mode`."""
    return normalise_auth_mode(_cfg("wlc_auth_mode"))


def identity_platform() -> str:
    """Which platform vouches for this container; ``azure`` when unset.

    Defaulting to Azure rather than refusing is right HERE and wrong in the agent
    worker, which refuses. The difference is that the worker runs on somebody
    else's host and cannot know what it is, while this value is a setting an
    operator chose on a panel — an unset one means an install that predates the
    setting, and every one of those is the Azure install this mode used to be.
    """
    platform = (_cfg("wlc_identity_platform") or PLATFORM_AZURE).strip().lower()
    return platform if platform in VALID_IDENTITY_PLATFORMS else PLATFORM_AZURE


def identity_audience() -> str:
    """What the token is minted FOR — its ``aud`` claim.

    ``wlc_identity_audience`` with ``wlc_entra_resource`` behind it, because the
    older key holds exactly this value on every install that set it. The Azure
    panel calls it an App ID URI and GCP calls it an audience string; it is the
    same field, so it is read from one place rather than branched on.
    """
    return _cfg("wlc_identity_audience") or _cfg("wlc_entra_resource")


def identity_token_path(platform: str = "", env: Optional[dict] = None) -> str:
    """The projected-token file for a file-based platform, or ``""``. Pure.

    An explicit ``wlc_identity_token_file`` wins; otherwise the variables the
    platform itself sets. Empty means this container has no projected token —
    which on AWS is the ordinary state of an ECS task and is why ``_missing``
    treats it as a configuration problem rather than letting the read fail later.
    """
    import os
    env = os.environ if env is None else env
    platform = platform or identity_platform()
    explicit = _cfg("wlc_identity_token_file")
    if explicit:
        return explicit
    if platform == PLATFORM_AWS:
        for var in _AWS_TOKEN_FILE_VARS:
            path = (env.get(var) or "").strip()
            if path:
                return path
        return ""
    if platform == PLATFORM_FILE:
        return _K8S_TOKEN_FILE
    return ""


def clear_token_cache() -> None:
    """Forget the memoised Entra token.

    Called when the Workload Credentials panel is saved, for the same reason the
    lease memo is cleared there: the resource or the identity may have just
    changed, and an operator watching their own edit do nothing for up to an hour
    would reasonably conclude the mode is broken.
    """
    _token_cache.update({"key": "", "token": "", "expires_at": 0.0})


def build_identity_request(resource: str, client_id: str = "",
                           env: Optional[dict] = None,
                           platform: str = PLATFORM_AZURE) -> tuple:
    """``(url, headers, params)`` for the platform's token endpoint. Pure.

    Only the two HTTP platforms reach here. The file-based ones have no request
    to build, and calling this for them is a programming error rather than a
    configuration one — hence the raise rather than a quiet empty tuple.

    **Azure:** ``IDENTITY_ENDPOINT`` + ``IDENTITY_HEADER`` when the runtime
    injects them (Container Apps, App Service), otherwise IMDS. ``client_id``
    selects a **user-assigned** identity and is omitted for a system-assigned one
    — sending it blank is not the same thing, it asks for an identity with no
    client id and fails.

    **GCP:** one endpoint on the metadata server, on Cloud Run and GCE and GKE
    alike. There is no client-id equivalent: the service account is attached to
    the revision, so which identity answers is a deployment fact rather than a
    parameter.
    """
    import os
    env = os.environ if env is None else env
    platform = (platform or PLATFORM_AZURE).strip().lower()

    if platform == PLATFORM_GCP:
        return (_GCP_IDENTITY_URL, {"Metadata-Flavor": "Google"},
                {"audience": resource, "format": "full"})
    if platform != PLATFORM_AZURE:
        raise WorkloadCredentialsError(
            f"identity platform {platform!r} reads a projected token file and "
            "has no token endpoint to call")

    params = {"api-version": _IDENTITY_API_VERSION, "resource": resource}
    if client_id:
        params["client_id"] = client_id
    endpoint = (env.get("IDENTITY_ENDPOINT") or "").strip()
    header = (env.get("IDENTITY_HEADER") or "").strip()
    if endpoint and header:
        return endpoint, {"X-IDENTITY-HEADER": header}, params
    return _IMDS_TOKEN_URL, {"Metadata": "true"}, params


def parse_jwt_expiry(token: str, now_epoch: float) -> float:
    """The ``exp`` claim of a JWT, as an epoch. Pure.

    **This does not verify anything, and must not be read as doing so.** The
    signature is not checked and no claim is trusted: the only consumer is the
    re-fetch memo, and the worst a forged ``exp`` can do is make this container
    ask its own metadata server for another token. The party that verifies this
    token is Pathfinder, which has the issuer's keys; this process never does.

    Anything unreadable becomes ``now``, for the same reason
    :func:`parse_identity_token` does it — the memo is an optimisation and must
    never be the reason a request fails.
    """
    import base64
    import json as _json

    parts = (token or "").split(".")
    if len(parts) < 2:
        return now_epoch
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)          # base64url needs its padding back
    try:
        claims = _json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return float(claims["exp"])
    except Exception:                              # noqa: BLE001 — see the docstring
        return now_epoch


def parse_identity_token(payload: Any, now_epoch: float) -> tuple:
    """``(token, expires_at_epoch)`` from a managed-identity token response. Pure.

    ``expires_on`` is an absolute epoch **as a string** from IMDS and Container
    Apps; ``expires_in`` is a relative fallback. An unreadable expiry becomes
    ``now`` rather than an error, so the token is used once and re-fetched next
    call — the cache is an optimisation and must never be the reason a request
    fails.
    """
    if not isinstance(payload, dict):
        raise WorkloadCredentialsError(
            "managed identity returned "
            f"{type(payload).__name__}, expected a JSON object")
    token = payload.get("access_token") or payload.get("accessToken") or ""
    if not token:
        raise WorkloadCredentialsError(
            "managed identity response carried no access_token")
    expires_at = now_epoch
    raw = _first(payload, "expires_on", "expiresOn")
    if raw is not None:
        try:
            expires_at = float(str(raw).strip())
        except (TypeError, ValueError):
            expires_at = now_epoch
    else:
        raw_in = _first(payload, "expires_in", "expiresIn")
        try:
            expires_at = now_epoch + float(str(raw_in).strip())
        except (TypeError, ValueError):
            expires_at = now_epoch
    return str(token), expires_at


# Per-platform advice for a token fetch that failed. The platforms' own wording
# is thin (`identity_not_found`), and the cause is nearly always a deployment
# fact rather than anything in this app — so each line names the fact.
_IDENTITY_HINTS = {
    PLATFORM_AZURE: (" — check that a managed identity is assigned to this "
                     "container and that the audience is an App ID URI the "
                     "tenant will issue for"),
    PLATFORM_GCP: (" — check that a service account is attached to this Cloud "
                   "Run revision or instance, and that the audience matches the "
                   "one the Custom IDP registration in Pathfinder expects"),
    PLATFORM_AWS: (" — AWS serves a workload identity token as a projected FILE, "
                   "and only EKS projects one (IRSA, or Pod Identity). ECS and "
                   "plain EC2 get SigV4 credentials and an instance identity "
                   "document, neither of which is an OIDC token, so a dashboard "
                   "on ECS has to stay on a stored Personal Access Token"),
    PLATFORM_FILE: (" — no projected token was found. Set wlc_identity_token_file "
                    "to the path your platform mounts it at"),
}


def identity_error_message(status_code: int, body: Any,
                           platform: str = PLATFORM_AZURE) -> str:
    """A message for a failed token fetch that names the likely cause.

    ``status_code`` 0 means the fetch never got as far as a request — the
    file-based platforms' way of failing, where there is no HTTP to report.
    """
    detail = ""
    if isinstance(body, dict):
        detail = str(_first(body, "error_description", "Message", "message",
                            "error") or "")
    elif isinstance(body, str):
        detail = body[:200]
    platform = (platform or PLATFORM_AZURE).strip().lower()
    hint = ""
    if status_code in (0, 400, 404):
        hint = _IDENTITY_HINTS.get(platform, "")
    suffix = f": {detail}" if detail else ""
    where = f" (HTTP {status_code})" if status_code else ""
    return (f"could not get a workload identity token on {platform}{where}"
            f"{hint}{suffix}")


def _projected_token(path: str, platform: str) -> str:
    """A token the platform wrote to disk for this container.

    Not a stored credential despite being a file: it is audience-bound,
    minutes-long, and rotated in place by the kubelet or the pod-identity agent.
    Nothing here put it there and nothing here can renew it — which is the whole
    property, and the reason it is re-read rather than memoised.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError as exc:
        raise WorkloadCredentialsError(
            f"no projected identity token at {path} — on {platform} this file is "
            "written by the platform, so an absent one means the workload "
            f"identity is not wired up: {exc}") from exc
    if not token:
        raise WorkloadCredentialsError(
            f"the projected identity token at {path} is empty")
    return token


def _workload_token() -> str:
    """A bearer token for this container's own identity, from whichever platform
    vouches for it. Memoised until expiry, except where a memo could go stale.

    Every branch ends in an OIDC JWT this process was given rather than holds.
    What differs is the transport — a JSON envelope, plain text, or a file — and
    the transport is the only thing this function knows about the platform.
    """
    import time

    import httpx

    platform = identity_platform()
    audience = identity_audience()
    if platform in FILE_PLATFORMS:
        path = identity_token_path(platform)
        if not path:
            raise WorkloadCredentialsError(identity_error_message(0, "", platform))
        # No memo: the file is local and the platform rotates it underneath us.
        return _projected_token(path, platform)

    if not audience:
        raise WorkloadCredentialsError(
            "workload identity auth needs wlc_identity_audience — what the token "
            "is requested for, which becomes its `aud` claim")
    client_id = _cfg("wlc_entra_client_id") if platform == PLATFORM_AZURE else ""

    # Keyed on what the token is FOR, and now on WHICH platform issued it. Without
    # this, changing the audience or switching identities keeps serving a token
    # minted for the old one, and the failure lands at BeyondTrust as an opaque 401.
    key = f"{platform}|{audience}|{client_id}"
    now = time.time()
    if (_token_cache.get("key") == key and _token_cache.get("token")
            and _token_cache.get("expires_at", 0.0) > now):
        return str(_token_cache["token"])

    url, headers, params = build_identity_request(audience, client_id,
                                                  platform=platform)
    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            resp = client.get(url, headers=headers, params=params)
    except httpx.HTTPError as exc:
        raise WorkloadCredentialsError(
            "no workload identity endpoint reachable — a workload identity needs "
            f"one assigned to this container: {exc}") from exc

    if resp.status_code >= 400:
        try:
            parsed = resp.json()
        except ValueError:
            parsed = resp.text
        raise WorkloadCredentialsError(
            identity_error_message(resp.status_code, parsed, platform))

    if platform == PLATFORM_GCP:
        # PLAIN TEXT, not JSON. Parsing this as JSON fails on a perfectly good
        # token and reads like a broken service account.
        token = (resp.text or "").strip()
        if token.count(".") < 2:
            raise WorkloadCredentialsError(
                "the GCP metadata server returned something that is not an "
                "identity token — check that a service account is attached to "
                "this revision")
        expires_at = parse_jwt_expiry(token, now)
    else:
        try:
            payload = resp.json()
        except ValueError as exc:
            raise WorkloadCredentialsError(
                "workload identity endpoint returned non-JSON "
                f"(HTTP {resp.status_code})") from exc
        token, expires_at = parse_identity_token(payload, now)

    _token_cache.update({"key": key, "token": token,
                         "expires_at": expires_at - _TOKEN_MARGIN_SECONDS})
    return token



def _auth_headers() -> dict:
    """Authorization, plus whatever routes the request to an identity.

    In ``entra`` mode ``X-BT-Service-Name`` is not optional decoration: without
    it the platform holds a valid token and no statement of which registered
    Workload Identity it is supposed to satisfy.
    """
    if auth_mode() == AUTH_MODE_WORKLOAD:
        return {
            "Authorization": f"Bearer {_workload_token()}",
            "X-BT-Service-Name": _cfg("wlc_service_name"),
        }
    if pat_is_self_referential():
        raise WorkloadCredentialsError(
            "wlc_pat is stored in Workload Credentials itself (wlc://…), which "
            "cannot be read without it. Either set the auth mode back to "
            "workload identity, where the PAT is not used at all, or paste the "
            "token back into Settings -> Workload Credentials.")
    return {"Authorization": f"Bearer {_cfg('wlc_pat')}"}


# ── Pure helpers (stdlib only — unit-testable without config_service) ─────────

def build_secrets_path(site_id: str, endpoint: str, path_version: str = "") -> str:
    """The ``/site/{id}/secrets`` path for ``endpoint``.

    Mirrors the Terraform provider's ``Client.BuildPath``, including the optional
    path-version segment, so a deployment needing a pinned path version behaves
    the same here as it does in Terraform.
    """
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint
    if path_version:
        return f"/site/{site_id}/secrets/{path_version}{endpoint}"
    return f"/site/{site_id}/secrets{endpoint}"


def build_auth_path(site_id: str, endpoint: str) -> str:
    """The platform-auth path, used for workload-identity registration.

    A separate grammar from :func:`build_secrets_path` — the auth service lives
    at ``/site/{id}/platform/auth`` and takes no path version.
    """
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint
    return f"/site/{site_id}/platform/auth{endpoint}"


def _first(mapping: dict, *names: str) -> Any:
    """First present, non-empty value among ``names``."""
    for name in names:
        val = mapping.get(name)
        if val not in (None, ""):
            return val
    return None


def parse_expiration(value: Any) -> Optional[datetime]:
    """A lease expiry as naive UTC, or None if unparseable.

    Naive UTC to match every timestamp column in ``database.py``. Returns None
    rather than raising: the caller treats an unreadable expiry as "refresh now",
    which is the safe direction.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def parse_generated(payload: Any) -> dict:
    """Normalise a ``generate`` response into a flat, predictable dict.

    Returns ``{"values": {...}, "lease_id": str, "expires_at": datetime|None}``.

    Two tolerances, both taken from BeyondTrust's own GitHub Action rather than
    invented here: field names are accepted in **camelCase or PascalCase**, and
    ``leaseId`` / ``expiration`` are read from either the ``secret`` object or the
    response root. The published docs disagree with each other on both, so
    accepting the union is cheaper than betting on one and failing opaquely.
    """
    if not isinstance(payload, dict):
        raise WorkloadCredentialsError(
            f"generate returned {type(payload).__name__}, expected a JSON object")

    secret = payload.get("secret")
    if not isinstance(secret, dict):
        # Some shapes put the credential at the root. Accept that, but only when
        # it actually looks like a credential — otherwise the error below says far
        # more than a dict of metadata masquerading as one would.
        secret = payload if any(
            k in payload for k in ("accessKeyId", "AccessKeyId", "clientId", "ClientId")
        ) else {}

    lease_id = _first(secret, "leaseId", "LeaseId") or _first(payload, "leaseId", "LeaseId")
    expiration = (_first(secret, "expiration", "Expiration")
                  or _first(payload, "expiration", "Expiration"))

    access_key = _first(secret, "accessKeyId", "AccessKeyId")
    client_id = _first(secret, "clientId", "ClientId")

    if access_key:
        # AWS: the assumed-role triple.
        values = {
            "access_key_id":     access_key,
            "secret_access_key": _first(secret, "secretAccessKey", "SecretAccessKey"),
            "session_token":     _first(secret, "sessionToken", "SessionToken"),
        }
        optional = ()
    elif client_id:
        # Azure: service-principal client credentials.
        values = {
            "client_id":     client_id,
            "client_secret": _first(secret, "clientSecret", "ClientSecret"),
            "tenant_id":     _first(secret, "tenantId", "TenantId"),
            "key_id":        _first(secret, "keyId", "KeyId"),
        }
        # key_id is only needed to correlate a revoke; absence is not a failure.
        optional = ("key_id",)
    else:
        # Names only — never the values.
        raise WorkloadCredentialsError(
            "generate response contained no recognised credential fields "
            f"(saw: {', '.join(sorted(secret)) or 'nothing'})")

    absent = [k for k, v in values.items() if v in (None, "") and k not in optional]
    if absent:
        raise WorkloadCredentialsError(
            f"generate response is missing {', '.join(absent)}")

    return {
        "values":     values,
        "lease_id":   str(lease_id) if lease_id else "",
        "expires_at": parse_expiration(expiration),
    }


def refresh_due(expires_at: Optional[datetime], issued_at: Optional[datetime],
                margin_pct: int, now: Optional[datetime] = None) -> bool:
    """Whether a lease should be regenerated now.

    True once less than ``margin_pct`` of the original TTL remains. A missing or
    unparseable expiry is always due — refreshing an unknown lease costs one
    metered issuance, whereas trusting it risks every cloud call failing.

    ``margin_pct`` is clamped to 1..99 so a mis-set 0 or 100 cannot mean either
    "never refresh" or "refresh on every check" — the latter would bill per call.
    """
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    if expires_at is None or now >= expires_at:
        return True
    pct = min(99, max(1, int(margin_pct or 50)))
    if issued_at is None:
        # No issue time to measure against: treat the window as an hour.
        return (expires_at - now).total_seconds() <= (3600 * pct / 100.0)
    ttl = (expires_at - issued_at).total_seconds()
    if ttl <= 0:
        return True
    return (expires_at - now).total_seconds() <= (ttl * pct / 100.0)


def static_value_from(payload: Any) -> str:
    """The stored string for a static-secret read.

    A Workload Credentials static secret is a **map** of key to value, not a
    scalar — the Terraform provider models it as ``secret_wo = { token = "..." }``
    and its ephemeral read exposes ``.secret["password"]``. So the value comes
    back re-serialised as JSON, which is also exactly what the dashboard's
    Secrets page stores for every other backend (all values are JSON by
    convention, enforced by ``validate_json_value``).
    """
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("secret", "value", "data"):
            inner = payload.get(key)
            if isinstance(inner, str):
                return inner
            if isinstance(inner, (dict, list)):
                return json.dumps(inner)
    return json.dumps(payload)


def static_write_body(value: str) -> dict:
    """The request body for creating or updating a static secret.

    The dashboard stores every secret as a JSON document and WC stores a map, so
    a JSON object maps across directly. Anything that is not an object (a bare
    string, a number, a list) is wrapped under ``value`` rather than rejected —
    the Secrets page accepts any valid JSON, and failing on a scalar would be a
    worse trade than a predictable wrapper key.
    """
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        parsed = value
    if not isinstance(parsed, dict):
        parsed = {"value": value}
    return {"secret": parsed}


def error_message_from(status_code: int, body: Any) -> str:
    """A useful message for an error response.

    WC guarantees a machine-actionable ``Code`` and human-readable ``Message`` on
    errors (backend 0.1.46 onward), so prefer those over the raw body — and cap
    the fallback, because an HTML error page would otherwise land verbatim in a
    job's ``error_message``, which is the only failure detail the UI renders.
    """
    if isinstance(body, dict):
        message = _first(body, "Message", "message", "error", "detail")
        code = _first(body, "Code", "code")
        if message:
            suffix = f", code {code}" if code else ""
            return f"Workload Credentials error (HTTP {status_code}{suffix}): {message}"
    if isinstance(body, str) and body.strip():
        return f"Workload Credentials error (HTTP {status_code}): {body[:200]}"
    return f"Workload Credentials error (HTTP {status_code})"


def is_conflict(exc: Exception) -> bool:
    """Whether an error is a 409 — the name is taken, or a ``cas`` version clash."""
    text = str(exc).lower()
    return "409" in text or "conflict" in text or "exist" in text


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _headers(merge_patch: bool = False) -> dict:
    out = {
        "bt-secrets-api-version": _cfg("wlc_api_version") or DEFAULT_API_VERSION,
        "Accept": "application/json",
    }
    # Authorization comes from the auth mode, which may mean a live call to the
    # platform's token endpoint — so it is built per request rather than held.
    out.update(_auth_headers())
    if merge_patch:
        # Updates are JSON Merge Patch (RFC 7396): a null deletes a field and an
        # omitted field is left alone. These routes reject a plain
        # application/json body.
        out["Content-Type"] = "application/merge-patch+json"
    return out


def _request(method: str, endpoint: str, *, folder: str = "",
             body: Any = None, query: Optional[dict] = None) -> Any:
    """One Workload Credentials call.

    Raises :class:`WorkloadCredentialsError` on anything that is not a 2xx, using
    the server's coded message when it sends one.
    """
    if not configured():
        missing = _missing()
        detail = (" (missing: " + ", ".join(missing) + ")") if missing else \
                 " (workload_credentials_enabled is off)"
        raise WorkloadCredentialsError(
            "Workload Credentials is not configured" + detail)

    import httpx

    base = (_cfg("wlc_api_base_url") or DEFAULT_API_URL).rstrip("/")
    path = build_secrets_path(_cfg("wlc_site_id"), endpoint, _cfg("wlc_api_path_version"))
    params = dict(query or {})
    if folder:
        params["folder"] = folder

    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            resp = client.request(method, base + path,
                                  headers=_headers(merge_patch=(method == "PATCH")),
                                  params=params or None, json=body)
    except httpx.HTTPError as exc:
        raise WorkloadCredentialsError(f"Workload Credentials unreachable: {exc}") from exc

    if resp.status_code >= 400:
        try:
            parsed = resp.json()
        except ValueError:
            parsed = resp.text
        raise WorkloadCredentialsError(error_message_from(resp.status_code, parsed))
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError as exc:
        raise WorkloadCredentialsError(
            f"Workload Credentials returned non-JSON (HTTP {resp.status_code})") from exc


# ── Operations ────────────────────────────────────────────────────────────────

def test_connection() -> dict:
    """Verify credentials and reachability.

    ``GET /session`` validates the current authentication, so success here means
    the site id, the API version and whatever the auth mode presents — a stored
    PAT, or this container's identity token against its registered Workload
    Identity — are all good, without creating anything or incurring a metered
    credential issuance.

    In ``entra`` mode this is also the only cheap way to tell a token-fetch
    failure (the container has no usable identity) from a rejection (the platform
    has no matching registration): the first names the metadata endpoint, the
    second is an HTTP 401 from BeyondTrust.
    """
    _request("GET", "/session")
    base = (_cfg("wlc_api_base_url") or DEFAULT_API_URL).rstrip("/")
    return {"ok": True,
            "message": f"Connected to Workload Credentials at {base} (site {_cfg('wlc_site_id')})."}


# Confirmed against a live site: collections come back as {"data": [...]}. The other
# keys are kept as fallbacks rather than removed — this API is still pre-release and may
# rename things, so tolerating that costs nothing, whereas a wrong guess here returns an
# empty list rather than an error, which is silent.
_COLLECTION_KEYS = ("data", "secrets", "static", "items", "folders")


def _collection(payload) -> list:
    """The list inside a collection response, whatever it is keyed under."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in _COLLECTION_KEYS:
        val = payload.get(key)
        if isinstance(val, list):
            return val
    return []


def list_folders() -> list:
    return _collection(_request("GET", "/folders"))


def list_static(folder: str = "") -> list:
    return _collection(_request("GET", "/static", folder=folder))


def read_static(name: str, folder: str = "") -> str:
    return static_value_from(_request("GET", "/static/" + name, folder=folder))


def write_static(name: str, value: str, folder: str = "") -> None:
    """Create or update a static secret.

    POST creates and 409s when the name is taken, so a conflict falls through to
    PATCH. Mirrors ``write_aws_sm``'s create-then-put shape, which is what lets
    the Secrets page behave identically across every backend.
    """
    body = static_write_body(value)
    try:
        _request("POST", "/static/" + name, folder=folder, body=body)
    except WorkloadCredentialsError as exc:
        if not is_conflict(exc):
            raise
        _request("PATCH", "/static/" + name, folder=folder, body=body)


def delete_static(name: str, folder: str = "") -> None:
    _request("DELETE", "/static/" + name, folder=folder)


def static_metadata(name: str, folder: str = "") -> dict:
    """Metadata (timestamps, tags, version) without reading the value.

    Used for staleness reporting, so the age shown is WC's own last-changed date
    rather than when the reference happened to be pasted into the dashboard.
    """
    data = _request("GET", "/static/" + name + "/metadata", folder=folder)
    return data if isinstance(data, dict) else {}


def generate(name: str, folder: str = "") -> dict:
    """Mint a credential from a dynamic secret. **This is the metered call.**

    Returns the :func:`parse_generated` shape. Every caller must cache the result
    for the lease's lifetime — pricing is per issuance, so calling this per
    request is both a cost and a rate-limit problem.
    """
    payload = _request("POST", "/dynamic/" + name + "/generate", folder=folder)
    result = parse_generated(payload)
    # Log the request, never the result. Reading any field back out of `result`
    # puts a credential-bearing object on a wide-audience sink one edit away from
    # leaking, and a lease id is a correlation handle to a LIVE credential. The
    # lease id belongs in the lease row and in Workload Credentials' own audit
    # log; what an operator needs here is the issuance count, which the folder
    # and name give them.
    logger.info("WC: generated a credential from dynamic secret %s/%s",
                folder or "(root)", name)
    return result


def get_lease(lease_id: str) -> dict:
    data = _request("GET", "/leases/id/" + lease_id)
    return data if isinstance(data, dict) else {}


def revoke_lease(lease_id: str) -> None:
    """Release a lease early.

    Only Azure leases are revocable; AWS returns ``400 lease_not_revocable``
    because STS credentials cannot be withdrawn before they expire. That refusal
    is expected rather than an error, so it is swallowed — callers revoke
    unconditionally and let the provider decide.
    """
    if not lease_id:
        return
    try:
        _request("DELETE", "/leases/id/" + lease_id)
    except WorkloadCredentialsError as exc:
        if "not_revocable" in str(exc):
            # Same reasoning as generate(): a lease id identifies a live
            # credential, so it stays out of the application log.
            logger.debug("WC: lease is not revocable (expected for AWS)")
            return
        raise
