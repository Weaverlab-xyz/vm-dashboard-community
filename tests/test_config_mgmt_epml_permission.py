"""Security regression: binding an EPM-L installation token to a run variable must
require ``secrets:use``.

``epml_token_var`` names an Ansible variable that the dashboard fills at dispatch with a
freshly minted BeyondTrust EPM for Linux installation token
(``services/ansible_credentials.py``). That is spending a credential and belongs under
the same rule as every other credential-bearing field on the run form: it needs
``secrets:use``, and its use is recorded. The field sat outside ``wants_secret``, so it
took neither — the run form's own gating was the only thing expressing the rule, and a
UI-side rule is not an enforced one.

Two of the three dispatch paths mint the token and are gated here. The third,
``_run_cloud_localhost``, does not carry the field at all — so it needs no gate, and the
last test pins the metadata fact that makes that true rather than the absence of a check.

Skips ONLY if the app deps (fastapi/pydantic/sqlalchemy) aren't installed — any other
import failure is a real regression and must fail loudly. Runs under pytest, or standalone:
    python tests/test_config_mgmt_epml_permission.py
"""
import asyncio
import os
import sys
import tempfile
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("DATABASE_URL",
                      "sqlite:///" + os.path.join(tempfile.mkdtemp(), "epmlperm.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

# The ONLY legitimate reason to skip is a bare interpreter with no app deps.
try:
    import fastapi  # noqa: F401
    import pydantic  # noqa: F401
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover — no app deps installed
    try:
        import pytest
        pytest.skip(f"app deps unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

from fastapi import HTTPException  # noqa: E402
from web_dashboard.api import config_mgmt as cm  # noqa: E402
from web_dashboard.services import ansible_run_gate as gate  # noqa: E402

TOKEN_VAR = "epml_installation_token"


# ── users ────────────────────────────────────────────────────────────────────

def _user(*, admin=False, perms=None):
    """A stand-in for the ORM User that _can_use_secrets reads. ``perms={}`` is the
    legacy NULL-permission account and is unrestricted; ``perms={"config_mgmt": [...]}`
    is a restricted one that can run but may not spend secrets."""
    return types.SimpleNamespace(username="operator", is_effective_admin=admin,
                                 effective_permissions_dict=perms if perms is not None else {})


def _no_secrets_user():
    return _user(perms={"config_mgmt": ["read", "write"]})


def _with_secrets_user():
    return _user(perms={"config_mgmt": ["read", "write"], "secrets": ["use"]})


# ── driving the local / cloud-runner path (/run) ─────────────────────────────

_audits = []


def _patch_run_path():
    """Stub everything /run touches before and after the permission gate, so the test
    exercises the real route rather than a re-implementation of it."""
    cm.ansible_local_service.get_configured_targets = lambda db: []
    cm.storage_service.active_backend = lambda: "s3"
    cm._cfg = lambda key: {"ansible_runner": "local"}.get(key, "")
    cm.job_service.create_job = lambda *a, **k: types.SimpleNamespace(id="job-1")
    cm.job_service.log_audit = lambda db, who, action, details=None: _audits.append(
        {"action": action, "details": details or {}})
    del _audits[:]


def _run(payload, user):
    return asyncio.run(cm.run_playbook(payload, None, user))


def _epml_only(**over):
    """A run whose ONLY credential-bearing field is the EPM-L token var — the exact
    shape that slipped past the gate."""
    base = dict(asset="epml-activate.yml", target="10.0.0.5", cloud="aws",
                asset_backend="s3", epml_token_var=TOKEN_VAR)
    base.update(over)
    return cm.RunRequest(**base)


# ── the gate module ──────────────────────────────────────────────────────────

def test_the_gate_refuses_a_minted_token_without_the_permission():
    r = gate.check_permission(wants_secret=False, can_use_secrets=False,
                              has_managed=False, password_safe_enabled=True,
                              wants_epml_token=True)
    assert r is not None and r.status == 403, "a minted EPM-L token is not gated"
    assert "secrets:use" in r.detail, r.detail


def test_the_epml_refusal_names_the_field_the_operator_actually_set():
    """Reusing the Secrets-Management sentence would tell an operator who picked no
    secret that they used one — a refusal they cannot act on."""
    r = gate.check_permission(wants_secret=False, can_use_secrets=False,
                              has_managed=False, password_safe_enabled=True,
                              wants_epml_token=True)
    assert "EPM for Linux" in r.detail, r.detail


def test_the_permission_holder_is_not_refused_a_minted_token():
    assert gate.check_permission(wants_secret=False, can_use_secrets=True,
                                 has_managed=False, password_safe_enabled=True,
                                 wants_epml_token=True) is None


def test_check_credentials_forwards_the_epml_arm():
    """The SPIRE lab calls the composed helper, so an arm the composition drops is a
    hole that only shows up on the other caller."""
    r = gate.check_credentials(wants_secret=False, can_use_secrets=False,
                               has_managed=False, password_safe_enabled=True,
                               wants_epml_token=True)
    assert r is not None and r.status == 403, "check_credentials dropped wants_epml_token"


# ── the /run route ───────────────────────────────────────────────────────────

def test_a_run_binding_an_epml_token_needs_secrets_use():
    """The rule this file exists for: the permission is checked on the server, not
    only by the form that omits the input."""
    _patch_run_path()
    try:
        result = _run(_epml_only(), _no_secrets_user())
    except HTTPException as e:
        assert e.status_code == 403, f"expected 403, got {e.status_code}: {e.detail}"
        assert "secrets:use" in str(e.detail), e.detail
        return
    raise AssertionError(
        f"a caller without secrets:use queued an EPM-L token run: {result}")


def test_the_same_run_is_allowed_with_secrets_use():
    """The gate must refuse the permission, not the feature."""
    _patch_run_path()
    assert _run(_epml_only(), _with_secrets_user())["job_id"] == "job-1"


def test_an_admin_still_bypasses():
    _patch_run_path()
    assert _run(_epml_only(), _user(admin=True))["job_id"] == "job-1"


def test_a_run_with_no_credential_at_all_is_still_ungated():
    """The gate must not widen: a plain playbook run has always needed no permission
    beyond config_mgmt:write, and a regression there would break every ordinary run."""
    _patch_run_path()
    assert _run(_epml_only(epml_token_var=""), _no_secrets_user())["job_id"] == "job-1"


def test_minting_a_token_is_audited():
    """Without an audit row there is no record the token was ever minted."""
    _patch_run_path()
    _run(_epml_only(), _with_secrets_user())
    rows = [a for a in _audits if a["action"] == "ansible_secret_use"]
    assert rows, "an EPM-L token run wrote no ansible_secret_use audit row"
    details = rows[0]["details"]
    assert details.get("epml_token_var") == TOKEN_VAR, details
    assert any("epml" in k for k in details.get("kinds", [])), details


def test_the_audit_records_the_var_name_and_never_a_token():
    """The job row's rule — the NAME travels, the token does not — applies to the audit
    trail too, which is read by more people than the job metadata is."""
    _patch_run_path()
    _run(_epml_only(), _with_secrets_user())
    details = [a for a in _audits if a["action"] == "ansible_secret_use"][0]["details"]
    # The only EPM-L value recorded anywhere in the row is the variable name itself.
    assert TOKEN_VAR in str(details)
    assert "PAT_" not in str(details), details


# ── the paths that do not mint ───────────────────────────────────────────────

def test_the_cloud_localhost_path_still_cannot_mint_a_token():
    """_run_cloud_localhost needs no epml gate for one reason only: it writes an
    explicit metadata dict that omits epml_token_var, so no token is ever minted for a
    k8s/database/portainer run. If that dict grows the key, this fails — and whoever
    adds it has to add the permission check with it."""
    import ast
    import inspect
    src = inspect.getsource(cm._run_cloud_localhost)
    keys = [n.value for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert "epml_token_var" not in keys, (
        "_run_cloud_localhost now references epml_token_var — if it carries the field "
        "into job metadata it mints a real token, and needs the same wants_epml_token "
        "gate as /run and the agent path")


def test_the_agent_path_gates_the_token_too():
    """agent_ansible_meta.RUN_META_KEYS carries epml_token_var, so the sealed bundle
    mints a real one — the agent path is a second way to spend the credential, not a
    copy of the first."""
    from web_dashboard.services import agent_ansible_meta
    assert "epml_token_var" in agent_ansible_meta.RUN_META_KEYS, (
        "the premise moved: if the agent path no longer carries the field, this gate "
        "and its test should go with it")
    src = __import__("inspect").getsource(cm._run_agent_ansible)
    assert "wants_epml_token" in src, (
        "_run_agent_ansible mints an EPM-L token but does not pass wants_epml_token to "
        "the permission gate")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
