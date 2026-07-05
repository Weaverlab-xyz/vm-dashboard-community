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


def _bt_owner() -> str:
    return _cs().get("secrets_bt_owner", "") or ""


def _bt_owner_id() -> str:
    """Return secrets_bt_owner as a stringified positive integer, or raise
    ValueError with operator guidance. ps-cli's `-o` accepts only numeric
    BeyondInsight User IDs — pasting a username causes create-secret to
    exit 0 with 'Can't create the secret, owners can only contain valid
    integer values' on stdout, which is the trap commit history calls out."""
    raw = _bt_owner().strip()
    if not raw:
        raise ValueError(
            "BeyondTrust secret owner is not configured. Set 'Secret Owner' "
            "on the Secrets Backend page (secrets_bt_owner) to the numeric "
            "BeyondInsight User ID (e.g. 2) that owns dashboard-created "
            "secrets — find it in BeyondInsight under Configuration → "
            "Role Based Access → User Management."
        )
    if not raw.isdigit():
        raise ValueError(
            f"BeyondTrust secret owner {raw!r} is not a numeric User ID. "
            f"ps-cli's -o flag accepts only integer IDs; usernames silently "
            f"fail. Set 'Secret Owner' on the Secrets Backend page to the "
            f"numeric BeyondInsight User ID (e.g. 2)."
        )
    return raw


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


# Non-JSON ps-cli stdout starting with one of these tokens means the call
# failed even though the process exited 0 — observed in the wild on
# `secrets create-secret` when owner-id validation fails ("Can't create the
# secret, owners can only contain valid integer values"). We surface those
# as a real ValueError rather than returning the message to callers that
# expect data.
_PSCLI_STDOUT_ERROR_PREFIXES = (
    "can't ", "cant ", "cannot ", "error", "exception", "forbidden",
    "unauthorized", "not found", "invalid", "failed", "missing setting",
    "unable to",
)


def _ps_run(args: list, timeout: int = 30):
    result = subprocess.run(
        # -y auto-confirms destructive subcommands (delete, etc.) that would
        # otherwise call input() and EOF against our closed stdin. Cheap
        # to leave on globally — read paths ignore it.
        ["ps-cli", "-y", "--format", "json"] + args,
        stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=timeout,
        env=_pscli_env(),
    )
    if result.returncode != 0:
        # Widen the stderr/stdout window to 2000 chars so callers see the
        # full ps-cli traceback rather than a 300-char prefix that gets
        # truncated mid-line — issue #14 reported the message was unreadable.
        raise ValueError(f"ps-cli error: {(result.stderr or result.stdout)[:2000]}")
    stdout = result.stdout.strip()
    if not stdout:
        return []
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        # Some create/delete subcommands return a plain string on success
        # (e.g. "Secret deleted successfully: <guid>", "Safe created: <guid>");
        # others emit an error sentence to stdout while still exiting 0. We
        # treat the latter as a real failure so callers don't silently treat
        # an error message as a success payload.
        first_word = stdout.lower().lstrip()
        if first_word.startswith(_PSCLI_STDOUT_ERROR_PREFIXES):
            raise ValueError(f"ps-cli error: {stdout[:2000]}")
        return stdout


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


def read_aws_sm(ref: str, vault_id: str | None = None) -> str:
    """Read an AWS SM secret. When vault_id is given and a SecretVault row
    exists for it, the row's endpoint is interpreted as the region.
    Otherwise the legacy `aws_region` config is used."""
    import boto3
    region = None
    if vault_id:
        from ..database import SessionLocal, SecretVault
        db = SessionLocal()
        try:
            row = db.query(SecretVault).filter(
                SecretVault.id == vault_id,
                SecretVault.backend == "aws_sm",
            ).first()
        finally:
            db.close()
        if row:
            region = row.endpoint
    if region is None:
        region, _ = _aws_cfg()
    client = boto3.client("secretsmanager", region_name=region)
    resp = client.get_secret_value(SecretId=ref)
    return resp.get("SecretString", "")


def aws_sm_arn(ref: str, vault_id: str | None = None) -> str:
    """Return the full ARN of an AWS SM secret **without fetching its value**.

    The ECS Ansible runner injects secrets via ``secrets valueFrom`` (the task
    execution role fetches the value at launch), which needs the secret's full
    ARN — including the random suffix Secrets Manager appends — so a describe is
    required. Region/vault resolution mirrors read_aws_sm()."""
    import boto3
    region = None
    if vault_id:
        from ..database import SessionLocal, SecretVault
        db = SessionLocal()
        try:
            row = db.query(SecretVault).filter(
                SecretVault.id == vault_id,
                SecretVault.backend == "aws_sm",
            ).first()
        finally:
            db.close()
        if row:
            region = row.endpoint
    if region is None:
        region, _ = _aws_cfg()
    client = boto3.client("secretsmanager", region_name=region)
    return client.describe_secret(SecretId=ref)["ARN"]


# ── Ephemeral AWS SM secrets (managed-account checkout on the ECS runner) ──────

def write_aws_sm_ephemeral(name: str, value: str, exec_role_arn: str = "",
                           kms_key_id: str = "") -> str:
    """Create a short-lived, RBAC-locked AWS SM secret and return its ARN.

    - **Tagged** (ephemeral_secrets.TAG_KEY) so the GC sweeper can find and reap it.
    - **CMK** (kms_key_id, optional) is the real read-restriction: reading requires
      kms:Decrypt, so a key policy granting Decrypt to only the execution role locks
      the value even against account IAM admins. "" → the account default SM key.
    - **Resource policy** scopes secretsmanager:GetSecretValue to exec_role_arn (the
      ECS task execution identity that fetches the valueFrom at launch).
    """
    from . import ephemeral_secrets as _eph
    region, _ = _aws_cfg()
    import boto3, json as _json
    client = boto3.client("secretsmanager", region_name=region)
    kwargs = {
        "Name": name,
        "SecretString": value,
        "Description": "VM Dashboard ephemeral ansible managed-account credential",
        "Tags": [{"Key": _eph.TAG_KEY, "Value": _eph.TAG_VALUE}],
    }
    if kms_key_id:
        kwargs["KmsKeyId"] = kms_key_id
    try:
        resp = client.create_secret(**kwargs)
        arn = resp["ARN"]
    except client.exceptions.ResourceExistsException:
        # A prior run of the same job/index leaked; overwrite and reuse.
        client.put_secret_value(SecretId=name, SecretString=value)
        arn = client.describe_secret(SecretId=name)["ARN"]
    if exec_role_arn:
        policy = {"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow",
            "Principal": {"AWS": exec_role_arn},
            "Action": "secretsmanager:GetSecretValue",
            "Resource": "*",   # a resource policy is attached to *this* secret
        }]}
        client.put_resource_policy(SecretId=arn, ResourcePolicy=_json.dumps(policy),
                                   BlockPublicPolicy=True)
    logger.info("AWS SM: wrote ephemeral secret %s (rbac→%s)", name, exec_role_arn or "default")
    return arn


def delete_aws_sm(name_or_arn: str) -> None:
    """Force-delete an AWS SM secret (no recovery window — so the name frees
    immediately and billing stops). Used to clean up an ephemeral after the run."""
    region, _ = _aws_cfg()
    import boto3
    client = boto3.client("secretsmanager", region_name=region)
    client.delete_secret(SecretId=name_or_arn, ForceDeleteWithoutRecovery=True)
    logger.info("AWS SM: force-deleted %s", name_or_arn)


def list_aws_sm_ephemeral() -> list:
    """List ephemeral secrets (by tag) as ``[{"id": arn, "created_ts": epoch}]``
    for the GC sweeper."""
    from . import ephemeral_secrets as _eph
    region, _ = _aws_cfg()
    import boto3
    client = boto3.client("secretsmanager", region_name=region)
    out = []
    paginator = client.get_paginator("list_secrets")
    filters = [{"Key": "tag-key", "Values": [_eph.TAG_KEY]}]
    for page in paginator.paginate(Filters=filters):
        for s in page.get("SecretList", []):
            created = s.get("CreatedDate")
            out.append({"id": s["ARN"], "created_ts": created.timestamp() if created else 0})
    return out


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


def _azure_kv_client_for(vault_id: str):
    """Return (SecretClient, url) for a named SecretVault row.

    Falls back to the singleton _azure_kv_client() if the vault row is
    missing — same path the resolver takes when the vault registry is
    empty. Keeps the multi-vault scheme additive rather than breaking.
    """
    from azure.identity import ClientSecretCredential
    from azure.keyvault.secrets import SecretClient
    from ..database import SessionLocal, SecretVault
    db = SessionLocal()
    try:
        row = db.query(SecretVault).filter(
            SecretVault.id == vault_id,
            SecretVault.backend == "azure_kv",
        ).first()
    finally:
        db.close()
    if not row:
        return _azure_kv_client()
    url = row.endpoint
    # credentials_ref support is deferred to Phase 5.5; for now the dashboard's
    # primary Azure SP credentials are used for every vault. Document so the
    # operator knows.
    _, tenant, client_id, client_secret = _azure_kv_cfg()
    cred = ClientSecretCredential(
        tenant_id=tenant,
        client_id=client_id,
        client_secret=client_secret,
    )
    return SecretClient(vault_url=url, credential=cred), url


def read_azure_kv(ref: str, vault_id: str | None = None) -> str:
    if vault_id:
        client, _ = _azure_kv_client_for(vault_id)
    else:
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


# ── Ephemeral GCP SM secrets (managed-account checkout on the Cloud Run runner) ─

def write_gcp_sm_ephemeral(name: str, value: str, runner_sa: str) -> str:
    """Create a **labelled** ephemeral GCP SM secret, add the value, and bind
    ``roles/secretmanager.secretAccessor`` on **only this secret** to ``runner_sa``
    (the SA the Cloud Run job runs as) — no project-level accessor, so only the
    runner can read it. ``name`` is the (non-secret) resource id we generate; the
    credential is ``value``. Returns the resource id."""
    from . import ephemeral_secrets as _eph
    from google.api_core.exceptions import AlreadyExists
    project, _, _ = _gcp_cfg()
    client = _gcp_client()
    parent = f"projects/{project}"
    resource = f"{parent}/secrets/{name}"
    try:
        client.create_secret(request={
            "parent": parent, "secret_id": name,
            "secret": {"replication": {"automatic": {}},
                       "labels": {_eph.TAG_KEY: _eph.TAG_VALUE}},
        })
    except AlreadyExists:
        pass
    client.add_secret_version(request={"parent": resource, "payload": {"data": value.encode()}})
    if runner_sa:
        policy = client.get_iam_policy(request={"resource": resource})
        from google.iam.v1 import policy_pb2
        policy.bindings.append(policy_pb2.Binding(
            role="roles/secretmanager.secretAccessor",
            members=[f"serviceAccount:{runner_sa}"]))
        client.set_iam_policy(request={"resource": resource, "policy": policy})
    logger.info("GCP SM: wrote ephemeral secret %s (rbac→%s)", name, runner_sa or "none")
    return name


def delete_gcp_sm(ref: str) -> None:
    """Delete an ephemeral GCP SM secret by its (non-secret) resource id. Used for
    post-run cleanup."""
    project, _, _ = _gcp_cfg()
    client = _gcp_client()
    client.delete_secret(request={"name": f"projects/{project}/secrets/{ref}"})
    logger.info("GCP SM: deleted %s", ref)


def list_gcp_sm_ephemeral() -> list:
    """List ephemeral secrets (by label) as ``[{"id": secret_id, "created_ts": epoch}]``
    for the GC sweeper."""
    from . import ephemeral_secrets as _eph
    project, _, _ = _gcp_cfg()
    client = _gcp_client()
    parent = f"projects/{project}"
    out = []
    for s in client.list_secrets(request={
        "parent": parent, "filter": f"labels.{_eph.TAG_KEY}={_eph.TAG_VALUE}"
    }):
        sid = s.name.split("/secrets/")[-1]
        out.append({"id": sid, "created_ts": s.create_time.timestamp() if s.create_time else 0})
    return out


def read_gcp_sm(ref: str, vault_id: str | None = None) -> str:
    """Read a GCP SM secret. When vault_id is given and a SecretVault row
    exists for it, the row's endpoint is interpreted as the GCP project
    id. Otherwise the legacy `gcp_project_id` config is used."""
    project = None
    if vault_id:
        from ..database import SessionLocal, SecretVault
        db = SessionLocal()
        try:
            row = db.query(SecretVault).filter(
                SecretVault.id == vault_id,
                SecretVault.backend == "gcp_sm",
            ).first()
        finally:
            db.close()
        if row:
            project = row.endpoint
    if project is None:
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
    owner = _bt_owner_id()
    if not folder:
        raise ValueError(
            "BeyondTrust folder is not configured. Set 'Folder Name' on the "
            "Secrets Backend page (secrets_bt_folder)."
        )
    # ps-cli's `-fn <name>` silently no-ops when the folder doesn't exist
    # (exit 0, nothing persisted), so we resolve to a concrete FolderId up
    # front and pass `-fid` instead. That also surfaces a clear error to
    # operators who picked a folder name that ps-cli can't see.
    folder_id = _resolve_bt_folder_id(folder)
    if not folder_id:
        raise ValueError(
            f"BeyondTrust folder {folder!r} is not visible to ps-cli. "
            f"`folders list` returned no folder by that name. Either pick "
            f"an existing folder on the Secrets Backend page or create one "
            f"first (BeyondTrust Safes are not Folders — Dashboard the Safe "
            f"and Dashboard the Folder are different things)."
        )
    title = _bt_secret_title(key)
    # Store as a Text-type secret rather than a credential (-p PASSWORD).
    # Dashboard values are JSON blobs that can exceed BeyondTrust's password
    # length limit (notably gcp_service_account_json), and text secrets carry
    # arbitrary content without the credential-style username pairing.
    args = ["secrets", "create-secret", "-t", title, "--text", value,
            "-o", owner, "-ot", "User", "-fid", folder_id]
    _ps_run(args, timeout=30)
    # ps-cli has been observed to exit 0 from `create-secret` even when the
    # underlying write was silently dropped. Round-trip the title through
    # `secrets get` to confirm the row is actually there and lives in the
    # folder we targeted, then promote any mismatch to a real error so the
    # migration UI shows a red row instead of green-on-fail.
    check = _ps_run(["secrets", "get", "-t", title])
    if not isinstance(check, list) or not check:
        raise ValueError(
            f"BeyondTrust secret {title!r} was not persisted (post-write "
            f"`secrets get` returned no entry). ps-cli exited 0 but did not "
            f"actually create the secret — typically a permission issue on "
            f"folder {folder!r}, or the linked user can't write to it."
        )
    actual_folder_id = check[0].get("FolderId")
    if actual_folder_id != folder_id:
        raise ValueError(
            f"BeyondTrust secret {title!r} landed in folder "
            f"{check[0].get('Folder')!r} (id={actual_folder_id}) instead of "
            f"the configured folder {folder!r} (id={folder_id})."
        )
    logger.info("BT Safe: wrote+verified secret %s (folder=%s/%s, owner=%s)",
                title, folder, folder_id, owner)
    return title


def read_bt_secrets_safe(ref: str, vault_id: str | None = None) -> str:
    # BT Secrets Safe is hosted by the PSCLI install; vault_id has no
    # routing effect today (the ps-cli wrapper auths once, hits one host).
    # The arg is accepted for shape parity with the other read_* functions
    # so read_sync can dispatch uniformly.
    _ = vault_id
    data = _ps_run(["secrets", "get", "-t", ref, "-d"])
    if not isinstance(data, list) or not data:
        return ""
    entry = data[0]
    # Text-type secrets put the payload in Text (sometimes returned as
    # FileContent/Content depending on ps-cli version); older
    # credential-type secrets used Password. Probe both so a backend that
    # still holds legacy credential entries keeps working.
    for field in ("Text", "FileContent", "Content", "Password"):
        val = entry.get(field)
        if val:
            return val
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


def read_sync(backend: str, ref: str, vault_id: str | None = None) -> str:
    fn = _READ_FN.get(backend)
    if not fn:
        raise ValueError(f"Cannot read from backend: {backend}")
    return fn(ref, vault_id=vault_id)


# ── Secret metadata (last-changed) for staleness — best-effort, read-only ──────
#
# Each describe_* returns the backend's own "when did this value last change"
# timestamp as a naive-UTC datetime (or None when the backend can't report it).
# Used by secret_hygiene so an externally-rotated secret reads by its real
# rotation date, not the date its reference was pasted into the dashboard.

def _naive_utc(dt):
    """Coerce an aware/naive datetime (or ISO string) to naive UTC; None-safe."""
    from datetime import datetime as _dt, timezone as _tz
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = _dt.fromisoformat(dt.replace("Z", "+00:00"))
        except Exception:
            return None
    to_dt = getattr(dt, "ToDatetime", None)  # google.protobuf Timestamp
    if callable(to_dt):
        dt = to_dt()
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone(_tz.utc).replace(tzinfo=None)
    return dt


def describe_aws_sm(ref: str, vault_id: str | None = None):
    import boto3
    region = None
    if vault_id:
        from ..database import SessionLocal, SecretVault
        db = SessionLocal()
        try:
            row = db.query(SecretVault).filter(
                SecretVault.id == vault_id, SecretVault.backend == "aws_sm").first()
        finally:
            db.close()
        if row:
            region = row.endpoint
    if region is None:
        region, _ = _aws_cfg()
    resp = boto3.client("secretsmanager", region_name=region).describe_secret(SecretId=ref)
    return _naive_utc(resp.get("LastChangedDate") or resp.get("LastRotatedDate")
                      or resp.get("CreatedDate"))


def describe_azure_kv(ref: str, vault_id: str | None = None):
    client, _ = (_azure_kv_client_for(vault_id) if vault_id else _azure_kv_client())
    props = client.get_secret(ref).properties
    return _naive_utc(getattr(props, "updated_on", None) or getattr(props, "created_on", None))


def describe_gcp_sm(ref: str, vault_id: str | None = None):
    project = None
    if vault_id:
        from ..database import SessionLocal, SecretVault
        db = SessionLocal()
        try:
            row = db.query(SecretVault).filter(
                SecretVault.id == vault_id, SecretVault.backend == "gcp_sm").first()
        finally:
            db.close()
        if row:
            project = row.endpoint
    if project is None:
        project, _, _ = _gcp_cfg()
    ver = _gcp_client().get_secret_version(
        request={"name": f"projects/{project}/secrets/{ref}/versions/latest"})
    return _naive_utc(getattr(ver, "create_time", None))


def describe_bt_secrets_safe(ref: str, vault_id: str | None = None):
    _ = vault_id
    data = _ps_run(["secrets", "get", "-t", ref, "-d"])
    if not isinstance(data, list) or not data:
        return None
    entry = data[0]
    for field in ("LastChangeDate", "ChangeDate", "ModifiedDate", "LastModified",
                  "Modified", "UpdatedOn", "DateModified"):
        val = entry.get(field)
        if val:
            return _naive_utc(val)
    return None


_DESCRIBE_FN = {
    "aws_sm":          describe_aws_sm,
    "azure_kv":        describe_azure_kv,
    "gcp_sm":          describe_gcp_sm,
    "bt_secrets_safe": describe_bt_secrets_safe,
}


def describe_sync(backend: str, ref: str, vault_id: str | None = None):
    """Best-effort last-changed datetime for a secret; None on any failure so the
    caller falls back to the dashboard's own record."""
    fn = _DESCRIBE_FN.get(backend)
    if not fn:
        return None
    try:
        return fn(ref, vault_id=vault_id)
    except Exception as exc:  # noqa: BLE001 — never let metadata lookup break staleness
        logger.warning("describe %s failed for %s: %s", backend, ref[:40], exc)
        return None


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
    """Return BeyondTrust Folders visible to the ps-cli user.

    Subcommand: `ps-cli folders list`. Bare `ps-cli list` is not a valid
    top-level command (commit ebab6ef shipped that by mistake; ps-cli's
    argparse rejects it with "invalid choice: 'list'"). `folders list`
    returns every folder the API user can see, with no Safe-scoping
    parameter, so the `safe` kwarg is accepted for API parity but is
    only echoed back on each entry — actual filtering happens client
    side if at all.
    """
    raw = _ps_run(["folders", "list"])
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for f in raw:
        out.append({
            "name":        f.get("Name") or f.get("FolderName") or "",
            "safe":        f.get("SafeName") or safe or "",
            "id":          f.get("Id") or f.get("FolderId"),
            "parent_id":   f.get("ParentId") or f.get("ParentFolderId") or "",
        })
    return out


def _resolve_bt_folder_id(name: str) -> str:
    """Resolve a folder name to its ps-cli FolderId via `folders list`.
    Returns "" if no folder by that name is visible to the API user."""
    if not name:
        return ""
    for f in list_bt_folders():
        if f.get("name") == name:
            return f.get("id") or ""
    return ""


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
    `read_bt_secrets_safe` path can fetch it by title.

    `ps-cli secrets list` has no folder filter — the folder is encoded in the
    secret title (`<Folder>/<Title>`), matching how get/delete address them.
    We list everything and filter by the `Folder` field client-side.
    """
    _, default_folder = _bt_cfg()
    target_folder = folder or default_folder
    raw = _ps_run(["secrets", "list"])
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for s in raw:
        item_folder = s.get("Folder") or ""
        if target_folder and item_folder != target_folder:
            continue
        title = s.get("Title") or s.get("Name") or ""
        out.append({
            "name":        title,
            "folder":      item_folder or target_folder,
            "description": s.get("Description", ""),
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


def read_database(ref: str, vault_id: str | None = None) -> str:
    # The 'database' backend doesn't route through vault_id (the DB is the
    # single store). vault_id arg accepted for shape parity with read_sync.
    _ = vault_id
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
