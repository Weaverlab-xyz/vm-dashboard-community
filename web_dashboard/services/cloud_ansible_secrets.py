"""Hardened secret injection for the CLOUD Ansible runners (ECS / ACI / Cloud Run).

Each requested secret var is delivered to the task as its **own** environment
variable through the provider's secret channel — ECS ``secrets valueFrom`` (AWS
Secrets Manager), Cloud Run secret-env (GCP Secret Manager), ACI ``secure_value``
(inline, hidden from the portal). A non-secret **manifest** maps each env name to
the Ansible var it should become; the task command decodes the manifest, reads the
secret envs, writes a 0600 vars file, and runs ``ansible-playbook -e @file``.

Because ECS/GCP reference a store secret (not an inline value), a secret used on
those runners **must already live in that cloud's store** (an ``aws_sm://`` /
``gcp_sm://`` reference). ACI takes the resolved value inline, so any secret works.

This module is pure: the manifest + the shell snippet are here (unit-tested); the
provider I/O (resolving values / ARNs / SM names) lives in ``api/config_mgmt``.
"""
import base64
import json
from typing import Iterable

MANIFEST_ENV = "SECRET_MANIFEST_B64"
SECRET_ENV_PREFIX = "DASH_SECRET_"
VARS_FILE = "/tmp/dash_secret_vars.json"


def env_name(i: int) -> str:
    return f"{SECRET_ENV_PREFIX}{i}"


def build_manifest(var_names: Iterable[str]) -> tuple[list[str], str]:
    """Assign a per-var env name and build the (base64) manifest.

    Returns ``(env_names, manifest_b64)`` where ``env_names[i]`` is the task-env
    name carrying the value for ``var_names[i]``, and the manifest is a base64
    JSON list of ``{"env": <env>, "var": <ansible_var>}``.
    """
    names = list(var_names)
    entries = [{"env": env_name(i), "var": v} for i, v in enumerate(names)]
    env_names = [e["env"] for e in entries]
    manifest_b64 = base64.b64encode(json.dumps(entries).encode()).decode()
    return env_names, manifest_b64


# Shell that materialises the vars file from the manifest + secret envs, 0600.
# Uses python3 (present in the ansible runner images). No secret ever appears in
# the command string — only env references.
_BUILD_VARS = (
    "python3 -c \""
    "import os,json,base64;"
    "m=json.loads(base64.b64decode(os.environ['" + MANIFEST_ENV + "']));"
    "open('" + VARS_FILE + "','w').write(json.dumps({e['var']:os.environ[e['env']] for e in m}));"
    "os.chmod('" + VARS_FILE + "',0o600)"
    "\""
)


def command_prefix() -> str:
    """Shell snippet (with a trailing ``&& ``) to run before ansible-playbook when
    secrets are present — builds the 0600 vars file from the manifest."""
    return _BUILD_VARS + " && "


def extra_vars_arg() -> str:
    """The ``ansible-playbook`` flag to consume the built vars file."""
    return f"-e @{VARS_FILE} "


# ── Per-provider secret resolution (pure; provider I/O is injected) ─────────────
# ECS/Cloud Run reference a *store* secret (the task identity fetches the value at
# launch — it never touches the task env or command line), so a secret used there
# must already live in that cloud's store. ACI takes the resolved value inline.
#   runner -> (required ref prefix, human store name, backend id)
CLOUD_STORE = {
    "ecs": ("aws_sm://", "AWS Secrets Manager", "aws_sm"),
    "gcp": ("gcp_sm://", "GCP Secret Manager", "gcp_sm"),
}


class StoreMismatch(Exception):
    """An ECS/GCP secret does not live in that cloud's store. Carries the pieces to
    build an actionable 'move it there' message (the API layer maps this to 400)."""

    def __init__(self, var: str, runner: str, store_name: str, prefix: str):
        self.var, self.runner, self.store_name, self.prefix = var, runner, store_name, prefix
        super().__init__(
            f"The secret for '{var}' must live in {store_name} to run on the "
            f"{runner.upper()} cloud runner. Move it there via Secrets → migrate, "
            f"then reference it as {prefix}<name>.")


def secret_bindings(secret_vars: dict | None, secret_become_source: str) -> list:
    """Ordered ``(ansible_var, source)`` list: the named vars then the become
    password. The SSH-key secret rides the existing SSH_KEY_B64 channel, not the
    manifest, so it is intentionally excluded."""
    bindings = [(v, str(s).strip()) for v, s in (secret_vars or {}).items()
                if s and str(s).strip()]
    if secret_become_source and secret_become_source.strip():
        bindings.append(("ansible_become_password", secret_become_source.strip()))
    return bindings


def _store_raw(src, *, is_reference, get_raw):
    """The stored ref string for a source (the ref itself, or a registry key's raw
    unresolved value) — used to tell which backend it points at."""
    return src if is_reference(src) else get_raw(src)


def validate_stores(runner: str, secret_vars: dict | None, secret_become_source: str,
                    *, is_reference, get_raw) -> None:
    """Raise StoreMismatch if any named/become secret isn't in the runner's cloud
    store. No-op for ACI (inline) and local. Pure prefix check — no backend I/O."""
    spec = CLOUD_STORE.get(runner)
    if not spec:
        return
    prefix, store_name, _ = spec
    for var, src in secret_bindings(secret_vars, secret_become_source):
        raw = _store_raw(src, is_reference=is_reference, get_raw=get_raw)
        if not (raw or "").startswith(prefix):
            raise StoreMismatch(var, runner, store_name, prefix)


def resolve_entries(runner: str, secret_vars: dict | None, secret_become_source: str,
                    *, is_reference, get, get_raw, resolve_reference, parse_ref,
                    aws_sm_arn) -> tuple:
    """Build ``(secret_entries, manifest_b64, inline_values)`` for a cloud run.

    Entry shape per runner: ECS ``{env, arn}``, GCP ``{env, secret_name}``, ACI
    ``{env, value}``. ``inline_values`` are the ACI-resolved plaintext values to add
    to the output scrub set (ECS/GCP never resolve the value here — the hardening).
    Raises StoreMismatch for an ECS/GCP secret outside that cloud's store."""
    bindings = secret_bindings(secret_vars, secret_become_source)
    if not bindings:
        return [], "", []
    env_names, manifest_b64 = build_manifest([v for v, _ in bindings])
    spec = CLOUD_STORE.get(runner)
    entries, inline_values = [], []
    for env, (var, src) in zip(env_names, bindings):
        if spec is None:  # ACI — resolve value, inject inline via secure_value
            val = resolve_reference(src) if is_reference(src) else get(src)
            entries.append({"env": env, "value": val})
            if val:
                inline_values.append(val)
            continue
        prefix, store_name, backend = spec
        raw = _store_raw(src, is_reference=is_reference, get_raw=get_raw)
        if not (raw or "").startswith(prefix):
            raise StoreMismatch(var, runner, store_name, prefix)
        vault_id, sref = parse_ref(raw, backend)
        if backend == "aws_sm":
            entries.append({"env": env, "arn": aws_sm_arn(sref, vault_id)})
        else:  # gcp_sm — Cloud Run resolves the short name in-project
            entries.append({"env": env, "secret_name": sref})
    return entries, manifest_b64, inline_values
