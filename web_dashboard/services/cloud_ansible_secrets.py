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
