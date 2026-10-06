"""Where a Windows VM's built-in administrator password is kept.

Every Windows build generates (Azure) or recovers (AWS) the local administrator's
password, and that password has to live somewhere it can be read back — a Windows VM
nobody can log into is useless. It must NOT live in the dashboard's own database.

That was the old behaviour: the password went to the global ``secrets_backend``, whose
default is ``"database"`` — ``config_service.set()`` into ``app_config``. Every other
secret the dashboard holds is a credential FOR the dashboard; this one is a credential
for a machine the dashboard built, and it belongs in a secret manager whose access is
governed and audited on its own terms.

:func:`resolve_backend` picks the store, in this order:

  1. ``windows_admin_secret_backend``, when an operator set one explicitly.
  2. BeyondTrust Secrets Safe, when the Password Safe API client is configured. With
     Password Safe in the picture the password goes into Password Safe — and when the
     VM is then onboarded as a managed system (``ps_vm_hook.register_windows``), this
     build-time copy is retired once Password Safe has rotated the account.
  3. The global ``secrets_backend``, when it is an EXTERNAL manager.
  4. The cloud's own vault: Azure Key Vault for an Azure VM, AWS Secrets Manager for EC2,
     GCP Secret Manager for GCE.
  5. Nothing — :class:`WindowsSecretError`. The build refuses rather than fall back to
     the database.

``"database"`` is refused at every step, including an explicit
``windows_admin_secret_backend = database``.

The same rules hold for the other machine credential the dashboard creates: a managed
Active Directory's administrator (``directory_service``), stored under an ``ad-admin-``
key through :func:`store`'s ``prefix``.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Never a valid answer from resolve_backend. Named so the guard and its test agree.
FORBIDDEN_BACKEND = "database"

_EXTERNAL_BACKENDS = ("bt_secrets_safe", "azure_kv", "aws_sm", "gcp_sm", "wlc")

# The cloud-native fallback per cloud (step 4).
_CLOUD_NATIVE = {"azure": "azure_kv", "aws": "aws_sm", "gcp": "gcp_sm"}


class WindowsSecretError(Exception):
    """No acceptable secret manager, or a write to it failed."""


def _cfg(key: str) -> str:
    from . import config_service
    from ..config import settings
    return str(config_service.get(key) or getattr(settings, key, "") or "").strip()


def _bt_configured() -> bool:
    """Secrets Safe is usable without a live call: ps-cli has its OAuth client and the
    secret owner is a numeric user id (``secrets_backend_service._bt_owner_id`` rejects
    anything else at write time, so a non-numeric one is as good as unset)."""
    return (all(_cfg(k) for k in ("pscli_api_url", "pscli_client_id", "pscli_client_secret"))
            and _cfg("secrets_bt_owner").isdigit())


def _azure_kv_configured() -> bool:
    return bool(_cfg("secrets_azure_kv_url"))


def _aws_sm_configured() -> bool:
    from ..config import settings
    return bool(_cfg("secrets_aws_region") or _cfg("aws_region")
                or getattr(settings, "aws_region", ""))


def _gcp_sm_configured() -> bool:
    # The same project resolution secrets_backend_service._gcp_cfg uses.
    return bool(_cfg("secrets_gcp_project") or _cfg("gcp_project") or _cfg("gcp_project_id"))


_CONFIGURED = {
    "bt_secrets_safe": _bt_configured,
    "azure_kv":        _azure_kv_configured,
    "aws_sm":          _aws_sm_configured,
    "gcp_sm":          _gcp_sm_configured,
}


def _usable(backend: str) -> bool:
    if backend not in _EXTERNAL_BACKENDS:
        return False
    check = _CONFIGURED.get(backend)
    # wlc has no cheap precondition here; an operator who chose it as the global backend
    # has configured it, and a failed write still fails the build.
    return check() if check else True


def resolve_backend(cloud: str) -> str:
    """The secrets backend a Windows admin password for ``cloud`` is written to."""
    explicit = _cfg("windows_admin_secret_backend").lower()
    if explicit:
        if explicit == FORBIDDEN_BACKEND:
            raise WindowsSecretError(
                "windows_admin_secret_backend is set to 'database'. Windows admin passwords "
                "are never kept in the dashboard database — choose Password Safe "
                "(bt_secrets_safe), Azure Key Vault, AWS Secrets Manager or GCP Secret "
                "Manager, or leave it blank to pick one automatically.")
        if explicit not in _EXTERNAL_BACKENDS:
            raise WindowsSecretError(
                f"windows_admin_secret_backend {explicit!r} is not a known secrets backend.")
        return explicit

    if _bt_configured():
        return "bt_secrets_safe"

    global_backend = _cfg("secrets_backend").lower()
    if global_backend and global_backend != FORBIDDEN_BACKEND and _usable(global_backend):
        return global_backend

    native = _CLOUD_NATIVE.get((cloud or "").lower())
    if native and _usable(native):
        return native

    hint = {"azure": "Azure Key Vault (Secrets → Azure Key Vault URL)",
            "aws": "AWS Secrets Manager (an AWS region)",
            "gcp": "GCP Secret Manager (a GCP project)"}.get((cloud or "").lower(), "")
    raise WindowsSecretError(
        "No secret manager is configured for the Windows administrator password, and it "
        "is never kept in the dashboard database. Configure Password Safe (ps-cli client "
        "and a numeric Secret Owner on the Secrets page)"
        + (f" or {hint}" if hint else "") + ", then retry the build.")


WINDOWS_PREFIX = "windows-admin"
DIRECTORY_PREFIX = "ad-admin"


def secret_key(vm_name: str, suffix: str, prefix: str = WINDOWS_PREFIX) -> str:
    return f"{prefix}-{vm_name}-{suffix}"


def store(cloud: str, vm_name: str, suffix: str, password: str, *,
          prefix: str = WINDOWS_PREFIX) -> tuple[str, str]:
    """Write the password; return ``(backend, ref)`` for job metadata.

    ``vm_name`` names the machine (or directory) the credential belongs to; ``prefix``
    says what kind of credential it is.

    Raises :class:`WindowsSecretError` when there is nowhere acceptable to write it or the
    write fails — callers store BEFORE creating the VM, so a failure here costs nothing."""
    from . import secrets_backend_service
    backend = resolve_backend(cloud)
    try:
        ref = secrets_backend_service.write_sync(
            backend, secret_key(vm_name, suffix, prefix), password)
    except Exception as e:  # noqa: BLE001 — every SDK raises its own type
        raise WindowsSecretError(
            f"Failed to store the {prefix} password for {vm_name} in "
            f"secrets backend '{backend}': {e}") from e
    return backend, ref


def read(backend: str, ref: str) -> str:
    from . import secrets_backend_service
    return secrets_backend_service.read_sync(backend, ref)


def delete(backend: str, ref: str) -> str:
    """Remove a stored password. Best-effort: returns "" on success, else the error text,
    so a destroy never fails over a secret it could not clean up."""
    if not backend or not ref:
        return ""
    from . import secrets_backend_service
    fn = secrets_backend_service._DELETE_FN.get(backend)
    if fn is None:
        return f"backend {backend!r} has no delete operation"
    try:
        fn(ref)
        return ""
    except Exception as e:  # noqa: BLE001
        # Nothing about the secret is logged (CodeQL traces the reference and the backend
        # name back to the password). The caller records the returned text on the row.
        logger.warning("could not delete a Windows admin secret (%s)", type(e).__name__)
        return str(e)
