"""Auto-inject the dashboard's Password Safe OAuth credentials into an Ansible runner as
``PASSWORD_SAFE_*`` environment variables, so an in-playbook ``beyondtrust.secrets_safe``
lookup works with no per-run setup.

The lookup plugin (``examples/playbooks/password-safe/``) runs on the Ansible *controller*
— i.e. inside the runner container — and reads ``PASSWORD_SAFE_API_URL`` /
``PASSWORD_SAFE_CLIENT_ID`` / ``PASSWORD_SAFE_CLIENT_SECRET`` from the process environment.
Those map onto the dashboard's existing ps-cli OAuth client config
(``pscli_api_url`` / ``pscli_client_id`` / ``pscli_client_secret``) that ``btapi_service``
already uses; we reuse it here (mirroring ``btapi_service._pscli_env`` and the
``ps_api_service`` URL normalization).

The client secret rides the SAME per-run connection-credential channel each runner backend
already uses for the SSH private key / DB password — ECS ``runTask`` override env, Cloud Run
plain env, ACI ``secure_value``, a ``0600`` file locally — NOT the cloud secret store, so no
ephemeral store minting is involved. Callers MUST add the client secret
(``runner_env()[SECRET_KEY]``) to their output scrub set.

``build_runner_env`` / ``_normalize_api_url`` are pure (no imports) so they unit-test without
``config_service``; ``runner_env`` is the thin wired entry point.
"""
import logging

logger = logging.getLogger(__name__)

# The env var carrying the OAuth client secret — the one sensitive value of the three.
# Callers append its value to their scrub set so it can never leak into job output.
SECRET_KEY = "PASSWORD_SAFE_CLIENT_SECRET"


def _normalize_api_url(raw: str) -> str:
    """Normalize a ps-cli API URL to the public-API base the lookup expects. Mirrors
    ``ps_api_service._base_url``: ps-cli configs store either the bare host or the full
    ``/BeyondTrust/api/public/v3`` path — accept both."""
    host = (raw or "").strip().rstrip("/")
    if not host:
        return ""
    if not host.lower().startswith("http"):
        host = f"https://{host}"
    if "/beyondtrust/api/public/" not in host.lower():
        host = f"{host}/BeyondTrust/api/public/v3"
    return host


def build_runner_env(*, enabled: bool, api_url_raw: str,
                     client_id: str, client_secret: str) -> dict:
    """Pure core of :func:`runner_env`. Returns the ``PASSWORD_SAFE_*`` env dict, or ``{}``
    when disabled or any credential is blank (callers treat ``{}`` as "do not inject")."""
    if not enabled:
        return {}
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    api_url = _normalize_api_url(api_url_raw)
    if not (api_url and client_id and client_secret):
        return {}
    return {
        "PASSWORD_SAFE_API_URL": api_url,
        "PASSWORD_SAFE_CLIENT_ID": client_id,
        SECRET_KEY: client_secret,
    }


def runner_env() -> dict:
    """Return the ``PASSWORD_SAFE_*`` env for the runner, or ``{}`` when unavailable.

    ``{}`` means "do not inject" — BeyondTrust is disabled, or any ``pscli_*`` credential
    is unset. A non-empty result always carries all three keys.
    """
    from . import config_service as cs
    return build_runner_env(
        enabled=cs.get_bool("beyondtrust_enabled", True),
        api_url_raw=cs.get("pscli_api_url"),
        client_id=cs.get("pscli_client_id"),
        client_secret=cs.get("pscli_client_secret"),
    )
