"""Credential resolution by REFERENCE, so no workload ever needs a plaintext env var.

Every platform can hand a function a secret without the value passing through
Terraform state, the function's describe output, or the dashboard's job metadata —
but each does it differently, and only two of the four do it for you:

===========  ==============================================================
Azure        ``@Microsoft.KeyVault(SecretUri=...)`` in an app setting. The
             PLATFORM resolves it before the worker starts.
GCP          ``secret_environment_variables``. The PLATFORM resolves it too.
AWS          Nothing. Lambda has no platform-resolved env-var secret, so the
             function reads Secrets Manager itself at cold start.
Self-hosted  A FILE. OpenFaaS mounts every secret at
             ``/var/openfaas/secrets/<name>`` and sets no env var at all; a
             plain Kubernetes Secret volume behaves the same way.
===========  ==============================================================

So the rule is three lines, tried in order: **the value env var wins if it is set**
(Azure and GCP already put it there), **else read the file a ``_FILE`` var names**
(self-hosted), **else resolve an id** (AWS). A workload calls :func:`resolve` and
stops caring which platform it landed on.

The file channel is why nothing in ``auth`` needed to change to run on a cluster we
do not own: without it the shared secret resolves to ``""``, ``auth.verify`` fails
CLOSED with a 500, and the function is dead in a way that looks like a deployment
bug rather than a missing channel.

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

# Prefixes of a PLATFORM reference that is still sitting in the env var verbatim.
#
# Azure resolves an ``@Microsoft.KeyVault(...)`` app setting with the app's Key Vault
# reference identity, and when that identity cannot read the secret it leaves the
# setting exactly as written — no error, no empty value, nothing in the app's logs.
# Without this check the workload then sends the literal reference text as its
# credential, and the first thing that notices is the TARGET: a Portainer adapter
# whose vault grant was missing reported Portainer answering 401, which reads as a
# revoked API token and sends you to entirely the wrong place.
#
# A reference is never a credential, so refusing is not a judgement call: there is no
# case where passing this string on does anything but fail further away.
_UNRESOLVED_PREFIXES = ("@Microsoft.KeyVault(", "@Microsoft.AppConfiguration(")


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


def _refuse_unresolved(value_env: str, value: str) -> None:
    """Raise when ``value`` is a platform reference the platform never resolved.

    Names the setting and the remedy rather than the value: the reference itself is
    not a secret, but it is also not information the operator is missing — what they
    need to know is which identity is short a grant.
    """
    for prefix in _UNRESOLVED_PREFIXES:
        if value.startswith(prefix):
            raise RuntimeError(
                f"{value_env} is still the literal {prefix}…) reference, so the "
                f"platform did not resolve it: the Function App's Key Vault reference "
                f"identity cannot read that secret. Grant it get on the vault (or "
                f"check that the secret name in the reference exists). Until then "
                f"this function holds no credential at all — sending the reference on "
                f"would only make the target report a bad one.")


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


def _read_file(path: str, file_env: str) -> str:
    """The credential in the file at ``path``.

    ``file_env`` is the name of the ``_FILE`` variable, NOT the value variable, and
    every message below quotes it. Naming the value variable instead would send the
    operator to set ``FN_X`` — which supplies the credential as a plaintext env var,
    the one thing this module exists to avoid.

    A DIRECTORY is accepted and resolved to the single file inside it. That is not
    convenience: which of the two shapes arrives depends on how the runtime mounted
    the secret — OpenFaaS names one file per secret, a Kubernetes Secret volume
    mounted without ``items`` names a directory with one file per key — and an
    adapter has no way to know which it got. Exactly one file, though: a directory
    with two keys has no single answer, and picking the first alphabetically would
    hand out whichever secret happened to sort earlier.

    RAISES rather than returning ``""``. A ``_FILE`` var that is set names a
    credential the operator meant to supply, so "the path is wrong" and "nothing is
    configured" are different conditions and must not collapse into the same silent
    one — the same argument :func:`_refuse_unresolved` makes for Azure's
    never-resolved references. ``auth.verify`` catches this, fails closed, and logs
    ``shared_secret_unresolvable``, so the outcome is a 500 that says which setting
    to look at instead of a 401 nobody can explain.
    """
    target = path
    if os.path.isdir(path):
        try:
            names = sorted(n for n in os.listdir(path) if not n.startswith(".."))
        except OSError as exc:
            raise RuntimeError(
                f"{file_env} names the directory {path!r}, which cannot be listed "
                f"({type(exc).__name__}). Check the volume is mounted.") from exc
        # Kubernetes projects a Secret volume through a ``..data`` symlink plus one
        # symlink per key; the ``..``-prefixed entries above are that machinery, not
        # keys, which is why they are filtered rather than counted.
        files = [n for n in names if os.path.isfile(os.path.join(path, n))]
        if len(files) != 1:
            raise RuntimeError(
                f"{file_env} names the directory {path!r}, which holds "
                f"{len(files)} files ({', '.join(files) or 'none'}); a secret "
                f"directory must hold exactly one, or which file is the credential "
                f"is a guess. Mount the single key, or point {file_env} at the file.")
        target = os.path.join(path, files[0])
    try:
        with open(target, "r", encoding="utf-8") as handle:
            value = handle.read()
    except OSError as exc:
        raise RuntimeError(
            f"{file_env} names {target!r}, which could not be read "
            f"({type(exc).__name__}). On OpenFaaS the secret must be listed in the "
            f"function's `secrets:` for it to be mounted at all.") from exc
    # Stripped because every way of writing one of these adds a trailing newline —
    # `kubectl create secret --from-file`, a heredoc, an editor — and a secret with
    # a newline on the end compares unequal to the same secret without one, which
    # presents as a wrong credential rather than a malformed one.
    value = value.strip()
    if not value:
        raise RuntimeError(
            f"{file_env} names {target!r}, which is empty. An empty credential "
            f"would be indistinguishable from an unconfigured one.")
    return value


def file_env_for(value_env: str) -> str:
    """The conventional file variable for ``value_env`` — ``FN_X`` → ``FN_X_FILE``.

    Same convention as :func:`id_env_for`, so the dashboard derives the name when it
    writes the Function's environment and no per-workload mapping table is needed.
    """
    return f"{value_env}_FILE"


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

    Two things it DOES raise on, and they are the same kind of thing: an UNRESOLVED
    platform reference (see :data:`_UNRESOLVED_PREFIXES`) and a ``_FILE`` var
    naming a file that is missing, ambiguous or empty (see :func:`_read_file`).
    Neither is "nothing is set" — both are a misconfiguration wearing a
    credential's clothes, and returning ``""`` for them would report the operator's
    mistake as the workload's.
    """
    direct = _env(value_env)
    if direct:
        _refuse_unresolved(value_env, direct)
        return direct
    # Before the id channel, because a self-hosted runtime has no Secrets Manager to
    # fall through to and reaching the AWS branch there costs a boto3 import that
    # cannot succeed — an ImportError in place of the real "no credential mounted".
    file_env = file_env_for(value_env)
    file_path = _env(file_env)
    if file_path:
        return _read_file(file_path, file_env)
    for name in (*id_envs, id_env_for(value_env)):
        secret_id = _env(name)
        if secret_id:
            return _read_aws(secret_id)
    return ""


def clear_cache() -> None:
    """Drop cached values. For tests, and for a caller that has just been refused
    by the target and wants to rule out a rotated credential."""
    _CACHE.clear()
