"""The dashboard's cloud credentials, federated through its own SPIFFE identity.

docs/design/dashboard-workload-identity.md, Slice 3. ``services/dashboard_identity`` keeps
a JWT-SVID for ``spiffe://<td>/dashboard`` in a file per audience; this module turns those
files into AWS, Azure and GCP credentials, so a self-hosted install with no platform
identity can drop the long-lived keys it stores.

**The order every call site follows**, and it is the existing order with one rung added:

  1. a Workload Credentials lease (``workload_credential_lease``) — unchanged;
  2. **a stored key, which still wins** — an operator retires it on purpose to switch;
  3. **federation, here** — only when :func:`active` says every condition holds;
  4. whatever the SDK finds ambiently (env, instance metadata, ADC) — unchanged.

So switching this on changes nothing until a stored key is cleared, and clearing one with
federation half-configured fails with :func:`reason` rather than with a cloud's 403.

The shapes mirror ``workload_credential_lease`` on purpose (``aws_credentials`` returns
what its ``credentials("aws")`` returns; ``*_subprocess_env`` likewise), so a call site
reads the same whichever dynamic rung answered. Terraform and Packer run as local
subprocesses of the app and worker, so the token files and the GCP config written beside
them are readable by them directly.

OCI is not here: its token exchange is tied to identity domains and is not worth the code.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

from . import config_service, dashboard_identity

logger = logging.getLogger(__name__)

AWS_ROLE_ARN = "aws_federation_role_arn"
GCP_SERVICE_ACCOUNT = "gcp_federation_service_account"
AWS_SESSION_NAME = "vm-dashboard"
GCP_TOKEN_URL = "https://sts.googleapis.com/v1/token"
GCP_CONFIG_FILE = "gcp-external-account.json"
JWT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"
SOURCE = "SPIFFE federation"
CLOUDS = ("aws", "azure", "gcp")

# Re-assume this long before STS's stated expiry. The role session is an hour by
# default; five minutes covers a slow call that starts just under the wire.
_AWS_MARGIN_S = 300
_aws_cache: dict = {"key": "", "creds": None, "expires_at": 0.0}
_aws_lock = threading.Lock()

_AUDIENCE_SETTING = {"aws": dashboard_identity.AUD_AWS,
                     "azure": dashboard_identity.AUD_AZURE,
                     "gcp": dashboard_identity.AUD_GCP}


class FederationError(Exception):
    """Federation is configured for a cloud but could not produce a credential. The
    message names the cause and never carries a token."""


def _cfg(key: str) -> str:
    val = config_service.get(key)
    if val:
        return str(val)
    from ..config import settings
    return str(getattr(settings, key, "") or "")


def token_path(cloud: str) -> str:
    return os.path.join(dashboard_identity.token_dir(), f"{cloud}.jwt")


def gcp_config_path() -> str:
    return os.path.join(dashboard_identity.token_dir(), GCP_CONFIG_FILE)


def _stored_key(cloud: str) -> bool:
    if cloud == "aws":
        return bool(_cfg("aws_access_key_id") and _cfg("aws_secret_access_key"))
    if cloud == "azure":
        return bool(_cfg("azure_client_secret"))
    if cloud == "gcp":
        return bool(_cfg("gcp_service_account_json") or _cfg("gcp_credentials_json"))
    return False


def reason(cloud: str) -> str:
    """Why federation is NOT in use for ``cloud``, or "" when it is. Pure apart from
    config reads and one stat; the order of the checks is the order an operator fixes
    them in."""
    if cloud not in CLOUDS:
        return f"{cloud} has no federated path"
    if not dashboard_identity.enabled():
        return "the dashboard's SPIFFE identity is off"
    if _stored_key(cloud):
        return "a stored key is configured, and it wins"
    aud = config_service.get(_AUDIENCE_SETTING[cloud])
    if cloud == "gcp":
        if not (aud or "").strip():
            return "no GCP workload identity provider is set"
        if not _cfg("gcp_project_id"):
            return "gcp_project_id is not set (a federated credential carries no project)"
    elif not config_service.get_bool(_AUDIENCE_SETTING[cloud]):
        return f"the {cloud.upper() if cloud == 'aws' else cloud.title()} audience is not ticked"
    if cloud == "aws" and not _cfg(AWS_ROLE_ARN):
        return "no AWS role ARN to assume is set"
    if cloud == "azure" and not (_cfg("azure_client_id") and _cfg("azure_tenant_id")):
        return "the Azure client id and tenant id are not set"
    if not os.path.exists(token_path(cloud)):
        return f"{token_path(cloud)} does not exist yet"
    return ""


def active(cloud: str) -> bool:
    return reason(cloud) == ""


def source(cloud: str) -> str:
    """What Settings shows for ``cloud``: which rung its calls will use."""
    if _stored_key(cloud):
        return "stored key"
    why = reason(cloud)
    if not why:
        return SOURCE
    if why == "the dashboard's SPIFFE identity is off":
        return "not configured"
    return f"not federated: {why}"


def _read_token(cloud: str) -> str:
    try:
        with open(token_path(cloud), encoding="ascii") as fh:
            token = fh.read().strip()
    except OSError as exc:
        raise FederationError(f"cannot read the dashboard's {cloud} token: {exc}") from exc
    if token.count(".") != 2:
        raise FederationError(f"the dashboard's {cloud} token file holds no JWT")
    return token


# ── AWS ───────────────────────────────────────────────────────────────────────

def _sts_client(region: str):
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    # AssumeRoleWithWebIdentity is an unsigned call: the web identity token IS the
    # authentication. Unsigned explicitly, so a stray AWS_* in the environment can never
    # be what authenticates it.
    return boto3.client("sts", region_name=region or "us-east-1",
                        config=Config(signature_version=UNSIGNED))


def aws_credentials(now: Optional[float] = None) -> Optional[dict]:
    """``{access_key_id, secret_access_key, session_token}`` from the role the
    dashboard's identity may assume, or None when federation is not active for AWS.

    Cached per process until five minutes before STS's expiry, keyed on the role, so a
    changed role is picked up on the next call. Raises FederationError when active but
    STS refuses — a deployment that retired its key must not fall through to whatever
    is left in the environment.
    """
    if not active("aws"):
        return None
    now = time.time() if now is None else now
    role = _cfg(AWS_ROLE_ARN)
    with _aws_lock:
        if (_aws_cache["creds"] and _aws_cache["key"] == role
                and now < _aws_cache["expires_at"] - _AWS_MARGIN_S):
            return dict(_aws_cache["creds"])
    token = _read_token("aws")
    try:
        resp = _sts_client(_cfg("aws_region")).assume_role_with_web_identity(
            RoleArn=role, RoleSessionName=AWS_SESSION_NAME, WebIdentityToken=token)
    except Exception as exc:  # noqa: BLE001 -- botocore's error zoo; one message out
        code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code")
        if code:
            raise FederationError(
                f"AWS refused the dashboard's SPIFFE identity for {role}: {code}. Check the "
                f"role's trust policy pins this dashboard's issuer and subject.") from exc
        raise FederationError(
            f"could not ask AWS STS to assume {role}: {type(exc).__name__}") from exc
    c = resp["Credentials"]
    exp = c["Expiration"]
    expires_at = exp.timestamp() if hasattr(exp, "timestamp") else now + 3600
    creds = {"access_key_id": c["AccessKeyId"], "secret_access_key": c["SecretAccessKey"],
             "session_token": c["SessionToken"]}
    with _aws_lock:
        _aws_cache.update(key=role, creds=creds, expires_at=expires_at)
    return dict(creds)


def aws_subprocess_env() -> Optional[dict]:
    creds = aws_credentials()
    if not creds:
        return None
    return {"AWS_ACCESS_KEY_ID": creds["access_key_id"],
            "AWS_SECRET_ACCESS_KEY": creds["secret_access_key"],
            "AWS_SESSION_TOKEN": creds["session_token"]}


# ── Azure ─────────────────────────────────────────────────────────────────────

def azure_credential(tenant_id: str, client_id: str, client_secret: str = ""):
    """THE constructor for an Azure credential in this app. A secret when there is one —
    exactly as before; otherwise, when federation is active, an assertion credential
    that re-reads the dashboard's token file on every token request, so it never holds
    a token past the file's own rotation. Raises FederationError when neither is
    available, naming why."""
    from azure.identity import ClientAssertionCredential, ClientSecretCredential
    if client_secret:
        return ClientSecretCredential(tenant_id=tenant_id, client_id=client_id,
                                      client_secret=client_secret)
    why = reason("azure")
    if why:
        raise FederationError(f"no Azure client secret, and SPIFFE federation is not "
                              f"usable: {why}")
    return ClientAssertionCredential(tenant_id=tenant_id or _cfg("azure_tenant_id"),
                                     client_id=client_id or _cfg("azure_client_id"),
                                     func=lambda: _read_token("azure"))


def azure_subprocess_env() -> Optional[dict]:
    """Terraform's azurerm provider (and its state backend) reading the token file."""
    if not active("azure"):
        return None
    env = {"ARM_USE_OIDC": "true", "ARM_OIDC_TOKEN_FILE_PATH": token_path("azure"),
           "ARM_CLIENT_ID": _cfg("azure_client_id"), "ARM_TENANT_ID": _cfg("azure_tenant_id")}
    sub = _cfg("azure_subscription_id")
    if sub:
        env["ARM_SUBSCRIPTION_ID"] = sub
    return env


# ── GCP ───────────────────────────────────────────────────────────────────────

def gcp_external_account_info() -> dict:
    """The ``external_account`` credential config GCP's libraries and CLIs read. Not a
    secret: it names a file, an audience and an endpoint."""
    info = {
        "type": "external_account",
        "audience": (config_service.get(dashboard_identity.AUD_GCP) or "").strip(),
        "subject_token_type": JWT_TOKEN_TYPE,
        "token_url": GCP_TOKEN_URL,
        "credential_source": {"file": token_path("gcp")},
    }
    sa = _cfg(GCP_SERVICE_ACCOUNT).strip()
    if sa:
        info["service_account_impersonation_url"] = (
            "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
            f"{sa}:generateAccessToken")
    return info


def gcp_credentials(scopes=("https://www.googleapis.com/auth/cloud-platform",)):
    """google-auth credentials from the dashboard's identity, or None when not active."""
    if not active("gcp"):
        return None
    from google.auth import identity_pool
    return identity_pool.Credentials.from_info(gcp_external_account_info(),
                                               scopes=list(scopes))


def write_gcp_config() -> None:
    """Keep the config file beside the token for subprocesses (Terraform, Packer), or
    remove it when GCP federation is not configured. Called from the token refresh."""
    path = gcp_config_path()
    if (config_service.get(dashboard_identity.AUD_GCP) or "").strip():
        directory, name = os.path.split(path)
        dashboard_identity._write_atomic(directory, name,
                                         json.dumps(gcp_external_account_info(), indent=2),
                                         0o640)
    else:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def gcp_subprocess_env() -> Optional[dict]:
    if not active("gcp") or not os.path.exists(gcp_config_path()):
        return None
    return {"GOOGLE_APPLICATION_CREDENTIALS": gcp_config_path(),
            "GOOGLE_PROJECT": _cfg("gcp_project_id")}


def sources() -> dict:
    return {cloud: source(cloud) for cloud in CLOUDS}


def clear_cache() -> None:
    with _aws_lock:
        _aws_cache.update(key="", creds=None, expires_at=0.0)
