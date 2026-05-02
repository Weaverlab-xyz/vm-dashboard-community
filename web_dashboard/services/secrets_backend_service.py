"""
Secrets backend adapters for external secret managers.

Each backend exposes three synchronous operations:
  test(cfg)          — verify connectivity; raises ValueError with a human-readable message on failure
  write(key, value)  — write the secret; returns the reference string stored in the DB
  read(ref)          — read the secret back by its reference string

Callers (config_service and the migration API) use asyncio.to_thread() to run
these blocking SDK calls off the event loop.

The "database" backend is handled entirely by config_service itself and does
not appear here.
"""
import json
import logging
import os
import subprocess

logger = logging.getLogger(__name__)


# ── Config helpers ────────────────────────────────────────────────────────────

def _cs():
    from . import config_service
    return config_service


def _aws_cfg() -> tuple[str, str]:
    cs = _cs()
    from ..config import settings
    region = cs.get("secrets_aws_region") or settings.aws_region or ""
    prefix = cs.get("secrets_aws_prefix") or "dashboard"
    return region, prefix


def _azure_kv_cfg() -> tuple[str, str, str, str]:
    cs = _cs()
    url = cs.get("secrets_azure_kv_url", "").rstrip("/")
    tenant = cs.get("azure_tenant_id", "")
    client_id = cs.get("azure_client_id", "")
    client_secret = cs.get("azure_client_secret", "")
    return url, tenant, client_id, client_secret


def _gcp_cfg() -> tuple[str, str, str]:
    cs = _cs()
    project = cs.get("secrets_gcp_project", "")
    prefix = cs.get("secrets_gcp_prefix") or "dashboard"
    sa_json = cs.get("gcp_service_account_json", "")
    return project, prefix, sa_json


def _bt_cfg() -> tuple[str, str]:
    cs = _cs()
    host = (cs.get("secrets_bt_host") or cs.get("bt_api_host", "")).rstrip("/")
    folder = cs.get("secrets_bt_folder") or "Dashboard"
    return host, folder


def _pscli_env() -> dict:
    cs = _cs()
    env = dict(os.environ)
    for cfg_key, env_key in [
        ("pscli_api_url",     "PSCLI_API_URL"),
        ("pscli_client_id",   "PSCLI_CLIENT_ID"),
        ("pscli_client_secret", "PSCLI_CLIENT_SECRET"),
    ]:
        val = cs.get(cfg_key, "")
        if val:
            env[env_key] = val
    return env


def _ps_run(args: list, timeout: int = 30) -> list:
    result = subprocess.run(
        ["ps-cli", "--format", "json"] + args,
        capture_output=True, text=True, timeout=timeout,
        env=_pscli_env(),
    )
    if result.returncode != 0:
        raise ValueError(f"ps-cli error: {(result.stderr or result.stdout)[:300]}")
    return json.loads(result.stdout) if result.stdout.strip() else []


# ── AWS Secrets Manager ───────────────────────────────────────────────────────

def test_aws_sm() -> dict:
    region, _ = _aws_cfg()
    if not region:
        raise ValueError("AWS region is not configured. Set it in Setup → AWS or Secrets → AWS Secrets Manager.")
    import boto3
    client = boto3.client("secretsmanager", region_name=region)
    client.list_secrets(MaxResults=1)
    return {"ok": True, "message": f"Connected to AWS Secrets Manager in region {region}."}


def write_aws_sm(key: str, value: str) -> str:
    region, prefix = _aws_cfg()
    secret_name = f"{prefix}/{key}".lstrip("/")
    import boto3
    client = boto3.client("secretsmanager", region_name=region)
    try:
        client.create_secret(
            Name=secret_name,
            Description=f"VM Dashboard managed secret: {key}",
            SecretString=value,
        )
    except client.exceptions.ResourceExistsException:
        client.put_secret_value(SecretId=secret_name, SecretString=value)
    logger.info("AWS SM: wrote secret %s", secret_name)
    return secret_name


def read_aws_sm(ref: str) -> str:
    region, _ = _aws_cfg()
    import boto3
    client = boto3.client("secretsmanager", region_name=region)
    resp = client.get_secret_value(SecretId=ref)
    return resp.get("SecretString", "")


# ── Azure Key Vault ───────────────────────────────────────────────────────────

def _azure_kv_client():
    from azure.identity import ClientSecretCredential
    from azure.keyvault.secrets import SecretClient
    url, tenant, client_id, client_secret = _azure_kv_cfg()
    if not url:
        raise ValueError("Azure Key Vault URL is not configured. Set it in Secrets → Azure Key Vault.")
    cred = ClientSecretCredential(
        tenant_id=tenant,
        client_id=client_id,
        client_secret=client_secret,
    )
    return SecretClient(vault_url=url, credential=cred), url


def test_azure_kv() -> dict:
    client, url = _azure_kv_client()
    next(client.list_properties_of_secrets(max_page_size=1), None)
    return {"ok": True, "message": f"Connected to Azure Key Vault at {url}."}


def _kv_name(key: str) -> str:
    return key.replace("_", "-")


def write_azure_kv(key: str, value: str) -> str:
    client, _ = _azure_kv_client()
    name = _kv_name(key)
    client.set_secret(name, value)
    logger.info("Azure KV: wrote secret %s", name)
    return name


def read_azure_kv(ref: str) -> str:
    client, _ = _azure_kv_client()
    secret = client.get_secret(ref)
    return secret.value or ""


# ── GCP Secret Manager ────────────────────────────────────────────────────────

def _gcp_client():
    from google.cloud import secretmanager
    _, _, sa_json = _gcp_cfg()
    if sa_json:
        from google.oauth2 import service_account
        info = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(info)
        return secretmanager.SecretManagerServiceClient(credentials=creds)
    return secretmanager.SecretManagerServiceClient()


def test_gcp_sm() -> dict:
    project, _, _ = _gcp_cfg()
    if not project:
        raise ValueError("GCP project is not configured. Set it in Setup → GCP or Secrets → GCP Secret Manager.")
    client = _gcp_client()
    parent = f"projects/{project}"
    next(client.list_secrets(request={"parent": parent, "page_size": 1}), None)
    return {"ok": True, "message": f"Connected to GCP Secret Manager for project {project}."}


def _gcp_secret_id(key: str) -> str:
    project, prefix, _ = _gcp_cfg()
    raw = f"{prefix}-{key}" if prefix else key
    return raw.replace("_", "-")


def write_gcp_sm(key: str, value: str) -> str:
    from google.api_core.exceptions import AlreadyExists
    project, _, _ = _gcp_cfg()
    client = _gcp_client()
    secret_id = _gcp_secret_id(key)
    parent = f"projects/{project}"
    try:
        client.create_secret(request={
            "parent": parent,
            "secret_id": secret_id,
            "secret": {"replication": {"automatic": {}}},
        })
    except AlreadyExists:
        pass
    client.add_secret_version(request={
        "parent": f"{parent}/secrets/{secret_id}",
        "payload": {"data": value.encode()},
    })
    logger.info("GCP SM: wrote secret %s", secret_id)
    return secret_id


def read_gcp_sm(ref: str) -> str:
    project, _, _ = _gcp_cfg()
    client = _gcp_client()
    name = f"projects/{project}/secrets/{ref}/versions/latest"
    resp = client.access_secret_version(request={"name": name})
    return resp.payload.data.decode()


# ── BeyondTrust Secrets Safe ──────────────────────────────────────────────────

def _bt_secret_title(key: str) -> str:
    folder = _bt_cfg()[1]
    return f"{folder}/{key}"


def test_bt_secrets_safe() -> dict:
    host, folder = _bt_cfg()
    if not host:
        raise ValueError(
            "BeyondTrust host is not configured. Set it in Secrets → BeyondTrust Secrets Safe."
        )
    _ps_run(["secrets", "list"])
    return {"ok": True, "message": f"Connected to BeyondTrust Secrets Safe at {host} (folder: {folder})."}


def write_bt_secrets_safe(key: str, value: str) -> str:
    _, folder = _bt_cfg()
    title = _bt_secret_title(key)
    _ps_run(["secrets", "create", "-t", title, "-v", value, "--folder", folder], timeout=30)
    logger.info("BT Safe: wrote secret %s", title)
    return title


def read_bt_secrets_safe(ref: str) -> str:
    data = _ps_run(["secrets", "get", "-t", ref, "-d"])
    if isinstance(data, list) and data:
        return data[0].get("Password", "")
    return ""


# ── Dispatch table ────────────────────────────────────────────────────────────

_TEST_FN = {
    "aws_sm":          test_aws_sm,
    "azure_kv":        test_azure_kv,
    "gcp_sm":          test_gcp_sm,
    "bt_secrets_safe": test_bt_secrets_safe,
}

_WRITE_FN = {
    "aws_sm":          write_aws_sm,
    "azure_kv":        write_azure_kv,
    "gcp_sm":          write_gcp_sm,
    "bt_secrets_safe": write_bt_secrets_safe,
}

_READ_FN = {
    "aws_sm":          read_aws_sm,
    "azure_kv":        read_azure_kv,
    "gcp_sm":          read_gcp_sm,
    "bt_secrets_safe": read_bt_secrets_safe,
}


def test_sync(backend: str) -> dict:
    fn = _TEST_FN.get(backend)
    if not fn:
        raise ValueError(f"Unknown backend: {backend}")
    return fn()


def write_sync(backend: str, key: str, value: str) -> str:
    fn = _WRITE_FN.get(backend)
    if not fn:
        raise ValueError(f"Cannot write to backend: {backend}")
    return fn(key, value)


def read_sync(backend: str, ref: str) -> str:
    fn = _READ_FN.get(backend)
    if not fn:
        raise ValueError(f"Cannot read from backend: {backend}")
    return fn(ref)
