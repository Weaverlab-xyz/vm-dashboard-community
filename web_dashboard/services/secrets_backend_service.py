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


def _ps_run(args: list, timeout: int = 30):
    result = subprocess.run(
        ["ps-cli", "--format", "json"] + args,
        # Closed stdin so destructive subcommands (delete-safe, etc.) that
        # would otherwise prompt for confirmation fail fast rather than
        # hanging until the timeout fires.
        stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=timeout,
        env=_pscli_env(),
    )
    if result.returncode != 0:
        # Widen the stderr/stdout window to 2000 chars so callers see the
        # full ps-cli traceback rather than a 300-char prefix that gets
        # truncated mid-line — issue #14 reported the message was unreadable.
        raise ValueError(f"ps-cli error: {(result.stderr or result.stdout)[:2000]}")
    if not result.stdout.strip():
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        # Some create/delete subcommands return a plain string (e.g. "Safe
        # created: <guid>") instead of JSON. Return raw text in that case
        # so callers that care can parse it.
        return result.stdout.strip()


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
    # list_secrets returns a ListSecretsPager which is iterable but not an
    # iterator — calling next() on it directly raises "ListSecretsPager object
    # is not an iterator" (issue #14). Wrap in iter() to advance one element,
    # or stop after the first hit. We don't need the result; we just need the
    # API call to succeed without raising for credential / permission errors.
    pager = client.list_secrets(request={"parent": parent, "page_size": 1})
    for _ in pager:
        break
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
    # ps-cli needs OAuth credentials (PSCLI_API_URL / CLIENT_ID / CLIENT_SECRET).
    # Without them, ps-cli's App() constructor fails during plugin init with an
    # opaque cement-framework traceback (issue #14). Surface a clear error
    # before invoking the binary so the user knows what to configure.
    cs = _cs()
    api_url = cs.get("pscli_api_url", "")
    client_id = cs.get("pscli_client_id", "")
    client_secret = cs.get("pscli_client_secret", "")
    missing = [name for name, val in (
        ("pscli_api_url", api_url),
        ("pscli_client_id", client_id),
        ("pscli_client_secret", client_secret),
    ) if not val]
    if missing:
        raise ValueError(
            "ps-cli credentials are not configured: missing "
            + ", ".join(missing)
            + ". Set these on the Setup wizard or in Settings → BeyondTrust."
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


# ── Secret browse / CRUD (issue: Secrets page expansion) ─────────────────────
#
# The functions below give the Secrets page full CRUD over individual secrets
# across every configured backend. Every secret value is constrained to a JSON
# string by `validate_json_value` so all backends behave uniformly and the UI
# can ship one editor for every store.
#
# For BeyondTrust Secrets Safe (BSS) we read safes/folders/secrets but do NOT
# create/delete safes or folders — ps-cli doesn't expose those operations.
# Hierarchy management for BSS must happen in BeyondInsight; we surface a
# read-only view here so operators can browse + run CRUD on secrets within
# whatever folders BeyondInsight already owns.


def validate_json_value(value: str) -> None:
    """Raise ValueError if `value` is not parseable as JSON.

    All backends accept arbitrary strings, but the dashboard's Secrets page
    constrains the editor to JSON so the value can be parsed back into a
    structured object regardless of backend. This guard runs at the API edge
    before any backend write."""
    try:
        json.loads(value)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Secret value is not valid JSON: {exc}") from exc


# ── AWS SM — list + delete ────────────────────────────────────────────────────

def list_aws_sm() -> list[dict]:
    region, prefix = _aws_cfg()
    import boto3
    client = boto3.client("secretsmanager", region_name=region)
    out: list[dict] = []
    next_token = None
    while True:
        kwargs: dict = {"MaxResults": 100}
        if prefix:
            kwargs["Filters"] = [{"Key": "name", "Values": [prefix]}]
        if next_token:
            kwargs["NextToken"] = next_token
        resp = client.list_secrets(**kwargs)
        for s in resp.get("SecretList", []):
            out.append({
                "name":        s["Name"],
                "description": s.get("Description", ""),
                "updated_at":  (s.get("LastChangedDate") or s.get("CreatedDate")).isoformat() if (s.get("LastChangedDate") or s.get("CreatedDate")) else "",
                "ref":         s["Name"],  # backend-specific id used for read/delete
            })
        next_token = resp.get("NextToken")
        if not next_token:
            break
    return out


def delete_aws_sm(ref: str) -> None:
    region, _ = _aws_cfg()
    import boto3
    client = boto3.client("secretsmanager", region_name=region)
    # ForceDeleteWithoutRecovery skips the 7-day retention window — the
    # dashboard's "Delete" button is an intentional admin action, not a
    # mistake-recovery affordance.
    client.delete_secret(SecretId=ref, ForceDeleteWithoutRecovery=True)
    logger.info("AWS SM: deleted secret %s", ref)


# ── Azure KV — list + delete ──────────────────────────────────────────────────

def list_azure_kv() -> list[dict]:
    client, _ = _azure_kv_client()
    out: list[dict] = []
    for prop in client.list_properties_of_secrets():
        out.append({
            "name":        prop.name,
            "description": "",
            "updated_at":  prop.updated_on.isoformat() if prop.updated_on else "",
            "ref":         prop.name,
        })
    return out


def delete_azure_kv(ref: str) -> None:
    client, _ = _azure_kv_client()
    # begin_delete_secret returns a poller; we wait so the UI's subsequent
    # refresh doesn't show the deleted secret still present.
    poller = client.begin_delete_secret(ref)
    try:
        poller.wait()
    except Exception:  # noqa: BLE001 — KV can throw on soft-delete edge cases
        pass
    logger.info("Azure KV: deleted secret %s", ref)


# ── GCP SM — list + delete ────────────────────────────────────────────────────

def list_gcp_sm() -> list[dict]:
    project, prefix, _ = _gcp_cfg()
    client = _gcp_client()
    parent = f"projects/{project}"
    out: list[dict] = []
    for secret in client.list_secrets(request={"parent": parent}):
        # secret.name is "projects/PROJECT/secrets/<id>"
        secret_id = secret.name.rsplit("/", 1)[-1]
        if prefix and not secret_id.startswith(prefix):
            continue
        out.append({
            "name":        secret_id,
            "description": (secret.labels or {}).get("description", ""),
            "updated_at":  secret.create_time.isoformat() if secret.create_time else "",
            "ref":         secret_id,
        })
    return out


def delete_gcp_sm(ref: str) -> None:
    project, _, _ = _gcp_cfg()
    client = _gcp_client()
    name = f"projects/{project}/secrets/{ref}"
    client.delete_secret(request={"name": name})
    logger.info("GCP SM: deleted secret %s", ref)


# ── BeyondTrust Secrets Safe — browse hierarchy + secret CRUD ────────────────

def list_bt_safes() -> list[dict]:
    """Return BeyondTrust Safes (top-level containers).

    Subcommand: `ps-cli list-safes`. The returned objects expose Name +
    Description + Id; callers thread the Id through to create-folder
    (`-pid`) and delete-safe (`-id`) as the upstream parent reference.
    """
    raw = _ps_run(["list-safes"])
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for s in raw:
        out.append({
            "name":        s.get("Name") or s.get("SafeName") or "",
            "description": s.get("Description", ""),
            "id":          s.get("Id") or s.get("SafeId"),
        })
    return out


def create_bt_safe(name: str, description: str = "") -> dict:
    """Create a new BeyondTrust Safe. Returns {name, id, description}."""
    if not name:
        raise ValueError("Safe name is required.")
    args = ["create-safe", "-n", name]
    if description:
        args.extend(["-d", description])
    raw = _ps_run(args, timeout=30)
    safe_id = ""
    if isinstance(raw, dict):
        safe_id = raw.get("Id") or raw.get("SafeId") or ""
    elif isinstance(raw, list) and raw:
        safe_id = raw[0].get("Id") or raw[0].get("SafeId") or ""
    logger.info("BT Safe: created safe %s (%s)", name, safe_id)
    return {"name": name, "id": safe_id, "description": description}


def update_bt_safe(safe_id: str, new_name: str) -> dict:
    """Rename a BeyondTrust Safe by GUID."""
    if not safe_id or not new_name:
        raise ValueError("safe_id and new_name are required.")
    _ps_run(["update-safe", "-id", safe_id, "-n", new_name], timeout=30)
    logger.info("BT Safe: renamed safe %s → %s", safe_id, new_name)
    return {"id": safe_id, "name": new_name}


def delete_bt_safe(safe_id: str) -> None:
    """Delete a BeyondTrust Safe by GUID."""
    if not safe_id:
        raise ValueError("safe_id is required.")
    _ps_run(["delete-safe", "-id", safe_id], timeout=30)
    logger.info("BT Safe: deleted safe %s", safe_id)


def list_bt_folders(safe: str = "") -> list[dict]:
    """Return BeyondTrust Folders. When `safe` is supplied, restrict the
    result to that Safe (matched against the parent path the CLI returns).

    Subcommand: `ps-cli list -p <path>`. Without a path filter the CLI
    walks every accessible folder.
    """
    args = ["list"]
    if safe:
        # -p restricts the list to a parent path. Top-level path is the
        # Safe name itself.
        args.extend(["-p", safe])
    raw = _ps_run(args)
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for f in raw:
        out.append({
            "name":        f.get("Name") or f.get("FolderName") or "",
            "safe":        f.get("SafeName") or safe or "",
            "id":          f.get("Id") or f.get("FolderId"),
            "parent_id":   f.get("ParentId") or "",
        })
    return out


def create_bt_folder(parent_id: str, name: str) -> dict:
    """Create a new BeyondTrust Folder under a Safe or another Folder.

    `parent_id` is the Safe GUID (for a top-level folder) or the Folder
    GUID (for a nested folder). Returns {name, id, parent_id}.
    """
    if not parent_id or not name:
        raise ValueError("parent_id and folder name are required.")
    raw = _ps_run(["create", "-pid", parent_id, "-n", name], timeout=30)
    folder_id = ""
    if isinstance(raw, dict):
        folder_id = raw.get("Id") or raw.get("FolderId") or ""
    elif isinstance(raw, list) and raw:
        folder_id = raw[0].get("Id") or raw[0].get("FolderId") or ""
    logger.info("BT Safe: created folder %s under %s (%s)", name, parent_id, folder_id)
    return {"name": name, "id": folder_id, "parent_id": parent_id}


def delete_bt_folder(folder_id: str) -> None:
    """Delete a BeyondTrust Folder by GUID. The folder must be empty; ps-cli
    will refuse to delete a folder that still has child folders or secrets,
    and the error gets surfaced through _ps_run."""
    if not folder_id:
        raise ValueError("folder_id is required.")
    _ps_run(["delete", "-id", folder_id], timeout=30)
    logger.info("BT Safe: deleted folder %s", folder_id)


def list_bt_secrets_safe(folder: str = "") -> list[dict]:
    """Return secrets in a folder (or in the configured default folder if
    none specified). Each item carries `ref = "Folder/Title"` so the existing
    `read_bt_secrets_safe` path can fetch it by title."""
    _, default_folder = _bt_cfg()
    target_folder = folder or default_folder
    args = ["secrets", "list"]
    if target_folder:
        args.extend(["--folder", target_folder])
    raw = _ps_run(args)
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for s in raw:
        title = s.get("Title") or s.get("Name") or ""
        out.append({
            "name":        title,
            "folder":      s.get("Folder") or target_folder,
            "description": s.get("Description", ""),
            # ref needs the folder prefix when one is set so it matches the
            # `_bt_secret_title` format used by writes; if listing surfaced a
            # plain title without a folder, just use the title.
            "ref":         f"{target_folder}/{title}" if target_folder else title,
        })
    return out


def delete_bt_secrets_safe(ref: str) -> None:
    # ps-cli accepts the Title for `secrets delete`; the title may include
    # the folder prefix that the listing returned.
    _ps_run(["secrets", "delete", "-t", ref], timeout=30)
    logger.info("BT Safe: deleted secret %s", ref)


# ── Database backend (config_service-backed) ──────────────────────────────────
#
# When `database` is the active backend, secret values live in the
# Fernet-encrypted `app_config` table managed by `config_service`. The
# operator-facing CRUD here lets them list/create/edit/delete those rows
# the same way as cloud-backed secrets.

def list_database() -> list[dict]:
    """List secrets stored in the Fernet-encrypted app_config table. Excludes
    obvious non-secret config rows (region, prefix, flag-style keys) by
    convention — only return rows that look like operator-managed secrets."""
    from . import config_service
    out: list[dict] = []
    try:
        rows = config_service.list_all()
    except AttributeError:
        # Fallback if config_service doesn't expose list_all (older versions)
        return out
    for row in rows:
        key = row.get("key") if isinstance(row, dict) else getattr(row, "key", "")
        if not key:
            continue
        # Filter to entries that smell like secrets — exclude pure config keys
        # like *_url, *_region, *_enabled, etc. Operators can still see them
        # on Settings; the Secrets page is for credential material.
        skip_suffixes = ("_url", "_region", "_prefix", "_enabled", "_path", "_host", "_port", "_account_name")
        if any(key.endswith(s) for s in skip_suffixes):
            continue
        out.append({
            "name":        key,
            "description": "",
            "updated_at":  "",
            "ref":         key,
        })
    return out


def write_database(key: str, value: str) -> str:
    from . import config_service
    config_service.set(key, value)
    logger.info("DB: wrote secret %s", key)
    return key


def read_database(ref: str) -> str:
    from . import config_service
    return config_service.get(ref) or ""


def delete_database(ref: str) -> None:
    from . import config_service
    config_service.delete(ref)
    logger.info("DB: deleted secret %s", ref)


# ── Dispatch tables for list / delete + JSON-validated write ─────────────────

_LIST_FN = {
    "aws_sm":          list_aws_sm,
    "azure_kv":        list_azure_kv,
    "gcp_sm":          list_gcp_sm,
    "bt_secrets_safe": list_bt_secrets_safe,
    "database":        list_database,
}

_DELETE_FN = {
    "aws_sm":          delete_aws_sm,
    "azure_kv":        delete_azure_kv,
    "gcp_sm":          delete_gcp_sm,
    "bt_secrets_safe": delete_bt_secrets_safe,
    "database":        delete_database,
}

# Database read/write are added to the existing dispatch maps below so the
# rest of the code can treat database as just another backend.
_WRITE_FN["database"] = write_database
_READ_FN["database"]  = read_database


def list_sync(backend: str, **filters) -> list[dict]:
    """Return all secrets in `backend`. For BSS, `filters['folder']` narrows."""
    fn = _LIST_FN.get(backend)
    if not fn:
        raise ValueError(f"Cannot list backend: {backend}")
    if backend == "bt_secrets_safe":
        return fn(folder=filters.get("folder", ""))
    return fn()


def delete_sync(backend: str, ref: str) -> None:
    fn = _DELETE_FN.get(backend)
    if not fn:
        raise ValueError(f"Cannot delete from backend: {backend}")
    fn(ref)


def write_sync_validated(backend: str, key: str, value: str) -> str:
    """Write wrapper that enforces JSON value validation. Use this from the
    Secrets page CRUD path; the existing `write_sync` is kept for callers that
    write internal infrastructure values (Terraform output, deploy artefacts)
    where JSON shape isn't enforced."""
    validate_json_value(value)
    return write_sync(backend, key, value)
