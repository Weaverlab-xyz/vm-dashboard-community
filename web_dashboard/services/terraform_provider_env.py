"""Provider credentials for the Terraform subprocess, by cloud.

Shared by the cloud-database and Kubernetes provisioning services (and any
future per-cloud Terraform driver). The wizard-stored (encrypted) credentials
win; when unset, each helper returns ``None`` so terraform falls back to whatever
the container environment provides (env vars / EC2 instance profile / GCP ADC /
``az`` CLI auth) — the same env-injection discipline as the packer flow.
"""
from __future__ import annotations

from typing import Optional

from ..config import settings
from . import config_service


def _cfg(key: str) -> str:
    val = config_service.get(key)
    if val:
        return val
    return getattr(settings, key, "") or ""


def aws_env() -> Optional[dict]:
    """AWS provider creds (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY); ``None``
    when unset → terraform uses the container env / instance profile."""
    key_id = _cfg("aws_access_key_id")
    secret = _cfg("aws_secret_access_key")
    if key_id and secret:
        return {"AWS_ACCESS_KEY_ID": key_id, "AWS_SECRET_ACCESS_KEY": secret}
    return None


def gcp_env() -> Optional[dict]:
    """GCP provider creds via GOOGLE_CREDENTIALS (inline SA JSON) + GOOGLE_PROJECT;
    ``None`` when unset → terraform falls back to ADC."""
    creds = _cfg("gcp_service_account_json") or _cfg("gcp_credentials_json")
    project = _cfg("gcp_project") or _cfg("gcp_project_id")
    env: dict = {}
    if creds:
        env["GOOGLE_CREDENTIALS"] = creds
    if project:
        env["GOOGLE_PROJECT"] = project
    return env or None


def azure_env() -> Optional[dict]:
    """Azure provider creds via the ARM_* env vars; ``None`` when unset →
    terraform falls back to the container env / az CLI auth."""
    env: dict = {}
    for cfg_key, arm_key in (
        ("azure_client_id", "ARM_CLIENT_ID"),
        ("azure_client_secret", "ARM_CLIENT_SECRET"),
        ("azure_tenant_id", "ARM_TENANT_ID"),
        ("azure_subscription_id", "ARM_SUBSCRIPTION_ID"),
    ):
        val = _cfg(cfg_key)
        if val:
            env[arm_key] = val
    return env or None


def provider_env(cloud: str) -> Optional[dict]:
    """Dispatch the terraform-subprocess provider credentials by cloud."""
    if cloud == "gcp":
        return gcp_env()
    if cloud == "azure":
        return azure_env()
    return aws_env()
