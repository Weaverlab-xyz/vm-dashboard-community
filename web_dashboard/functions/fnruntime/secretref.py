"""Credential resolution by REFERENCE, so no workload ever needs a plaintext env var.

Every cloud can hand a function a secret without the value passing through Terraform
state, the function's describe output, or the dashboard's job metadata — but each
does it differently, and only two of the three do it for you:

===========  ==============================================================
Azure        ``@Microsoft.KeyVault(SecretUri=...)`` in an app setting. The
             PLATFORM resolves it before the worker starts.
GCP          ``secret_environment_variables``. The PLATFORM resolves it too.
AWS          Nothing. Lambda has no platform-resolved env-var secret, so the
             function reads Secrets Manager itself at cold start.
===========  ==============================================================

So the rule is one line: **the value env var wins if it is set** (Azure and GCP
already put it there), **otherwise resolve an id** (AWS). A workload calls
:func:`resolve` and stops caring which cloud it landed on.

``boto3`` is imported lazily, inside the AWS branch only. It is the one non-stdlib
import in ``fnruntime``, it is reachable only on Lambda — where the runtime ships it —
and on the other two clouds the value env var is already set, so the branch is never
entered. Keeping it here rather than copied into each workload is the point: one
implementation, one test, one place for the JSON-payload rules to be right.
"""
import json
import os
import time

# Read-through cache, keyed by secret id. Bounded by a TTL rather than held for the
# life of the container: an admin credential that rotates must not be pinned to a
# stale value by a warm function that happens never to recycle, and re-reading on
# every invocation is a Secrets Manager call (and a throttling quota) per grant.
_TTL_SECONDS = 300
_CACHE = {}

# Tried in order when a Secrets Manager payload is a JSON object rather than a bare
# string. Covers what AWS's own rotation templates write (``password``) and what the
# dashboard's staging path writes, without the caller having to say so.
_JSON_KEYS = ("password", "admin_password", "secret", "api_key", "value")


def _env(name: str) -> str:
    return (os.environ.get(name, "") or "").strip()


def _from_payload(payload: str, secret_id: str) -> str:
    """The credential inside a Secrets Manager payload.

    A bare string is the credential. A JSON object is the RDS/rotation shape, so
    pull the conventional key out of it — and when there is exactly one key, take
    that whatever it is called. Anything else raises rather than guessing: a wrong
    credential fails later, somewhere else, looking like a permissions problem.
    """
    text = payload.strip()
    if not text.startswith("{"):
        return text
    try:
        parsed = json.loads(text)
    except ValueError:
        return text
    if not isinstance(parsed, dict):
        return text
    for key in _JSON_KEYS:
        if parsed.get(key):
            return str(parsed[key])
    if len(parsed) == 1:
        return str(list(parsed.values())[0])
    # Names only — never the values.
    raise RuntimeError(
        f"secret {secret_id!r} is a JSON object with no recognised credential key "
        f"(has: {', '.join(sorted(parsed))}); expected one of {', '.join(_JSON_KEYS)}")


def _read_aws(secret_id: str) -> str:
    now = time.time()
    hit = _CACHE.get(secret_id)
    if hit and hit[0] > now:
        return hit[1]
    import boto3  # noqa: PLC0415 — Lambda ships it; the other clouds never get here
    client = boto3.client("secretsmanager", region_name=_env("AWS_REGION") or None)
    value = _from_payload(
        client.get_secret_value(SecretId=secret_id).get("SecretString") or "", secret_id)
    _CACHE[secret_id] = (now + _TTL_SECONDS, value)
    return value


def id_env_for(value_env: str) -> str:
    """The conventional id variable for ``value_env`` — ``FN_X`` → ``FN_X_SECRET_ID``.

    The dashboard derives the same name when it wires an AWS function up, which is
    what lets one ``secret_environment`` entry work on all three clouds without a
    per-workload mapping table.
    """
    return f"{value_env}_SECRET_ID"


def resolve(value_env: str, *id_envs: str) -> str:
    """The credential for ``value_env``, or ``""`` if none is configured.

    ``id_envs`` are extra id variables to accept ahead of the conventional
    :func:`id_env_for` name — workloads that shipped with a differently-spelled one
    pass it here so existing deployments keep resolving.

    Returns ``""`` rather than raising when nothing is set: "no credential" is a
    condition several workloads handle themselves (dry run needs none), and their
    error messages say more about what to do than a generic one could.
    """
    direct = _env(value_env)
    if direct:
        return direct
    for name in (*id_envs, id_env_for(value_env)):
        secret_id = _env(name)
        if secret_id:
            return _read_aws(secret_id)
    return ""


def clear_cache() -> None:
    """Drop cached values. For tests, and for a caller that has just been refused
    by the target and wants to rule out a rotated credential."""
    _CACHE.clear()
