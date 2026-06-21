"""
Validation for operator-chosen SSH-key-secret overrides at VM launch.

Every cloud VM deploy uses an SSH key secret from that provider's secret manager
(AWS Secrets Manager / Azure Key Vault / GCP Secret Manager). The deploy form lets
the operator optionally **override** the configured default with any other secret in
that store. Whatever they choose must be a JSON object carrying a ``public_key`` —
this module enforces that so a bad choice fails fast at launch with a clear message,
rather than producing a VM nobody can reach.
"""
import json


class SshKeySecretError(ValueError):
    """Raised when a chosen SSH-key secret isn't a JSON object with a public_key."""


def validate_public_key_secret(raw: str, *, secret_name: str = "") -> dict:
    """Parse ``raw`` and require a JSON object with a non-empty ``public_key``.

    Returns the parsed dict (which may also carry ``private_key`` for keypair
    secrets). Raises :class:`SshKeySecretError` otherwise. ``secret_name`` is only
    used to make the error message actionable.
    """
    where = f" '{secret_name}'" if secret_name else ""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise SshKeySecretError(
            f"SSH key secret{where} must be a JSON object with a 'public_key' field."
        ) from exc
    if not isinstance(data, dict) or not (data.get("public_key") or data.get("publicKey")):
        raise SshKeySecretError(
            f"SSH key secret{where} must contain a non-empty 'public_key' field."
        )
    return data
