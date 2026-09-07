"""MCP tools must apply the same RBAC their HTTP twins apply, and fail closed without a caller.

This file exists because `api/mcp_server.py` used to authenticate a Personal Access Token
and then ignore who it belonged to. `_mcp_user` was set by the ASGI wrapper and read by
nothing, so any active user's token listed every job in the estate and `get_job` returned
the raw deploy payload — `bt_tf_state` (the Terraform state of that VM's PRA Shell Jump),
`ps_registration_tf_state`, `ssh_secret_name`. A PAT is not an MCP credential: `api/auth.py`
accepts the same token across the REST API, where every endpoint scopes it properly. MCP was
the one surface where it escaped.

What is pinned here, in rough order of how badly it would hurt to lose:

  * **Fail closed.** With no caller in the ContextVar, EVERY tool returns the
    unauthenticated error and touches no data. The tool list is discovered by reflection,
    so a tool added later cannot quietly skip this.
  * **The two admin rules stay two.** The four cloud consoles key on `is_admin`;
    inventory, databases, k8s and functions key on `is_effective_admin`, a superset that
    also honours a session-permissions row and a live Entitle JIT grant. A user who is one
    and not the other is the test case that catches a well-meaning unification —
    `tests/test_dashboard_stats_api.py` pins the same split for the dashboard tiles.
  * **Redaction is an allowlist**, and no allowlisted key reads as a credential.
  * Two latent bugs found while doing the above: `list_azure_vms` filtered on job type
    `"azure_vm_deploy"`, which is not a job type anywhere in this repo (the real one is
    `azure_deploy`), so every single-VM Azure deploy was invisible to it; and `list_amis`
    passed the caller's `None` into `aws_service.list_amis(region: str)`, a required
    positional.

Calls the tool functions directly rather than through the MCP transport: what is under test
is the scoping, not the SDK. `_MCPAuth` — the piece that populates the ContextVar and applies
the feature gate — is driven as a plain ASGI callable, which needs no transport either.

Run: python tests/test_mcp_rbac.py   (or under pytest)
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="mcp-rbac-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-mcp-rbac-tests")

try:
    from web_dashboard.database import Base, Job, SessionLocal, engine
    from web_dashboard.api import mcp_server as mcp
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

Base.metadata.create_all(bind=engine)


class _User:
    """Enough of a User for the scoping helpers.

    `is_admin` and `is_effective_admin` are independent on purpose — that difference is
    the whole point of several tests below.
    """

    def __init__(self, username="alice", is_admin=False, effective=None,
                 workgroups=(), permissions=None):
        self.username = username
        self.is_admin = is_admin
        self.is_effective_admin = is_admin if effective is None else effective
        self.workgroups_list = list(workgroups)
        # {} means "unrestricted" in api/auth.require_permission — mirrored, not improved.
        self.effective_permissions_dict = permissions if permissions is not None else {}


ADMIN = _User("root", is_admin=True)
ALICE = _User("alice", workgroups=["team-a"])          # non-admin, team-a
BOB = _User("bob", workgroups=["team-b"])              # non-admin, team-b
# The case that catches a unified admin rule: JIT-elevated but not a stored admin.
JIT = _User("jit", is_admin=False, effective=True)


def _run(coro):
    return asyncio.run(coro)


def _as(user, coro_fn, *args, **kwargs):
    """Run a tool with `user` installed as the MCP caller, then restore."""
    token = mcp._mcp_user.set(user)
    try:
        return _run(coro_fn(*args, **kwargs))
    finally:
        mcp._mcp_user.reset(token)


def _reset():
    db = SessionLocal()
    try:
        db.query(Job).delete()
        db.commit()
    finally:
        db.close()


def _job(job_id, job_type, created_by, workgroup, extra=None, status="completed"):
    db = SessionLocal()
    try:
        db.add(Job(id=job_id, job_type=job_type, status=status,
                   created_by=created_by, workgroup=workgroup,
                   extra_data=json.dumps(extra or {}),
                   created_at=datetime.utcnow(), completed_at=datetime.utcnow()))
        db.commit()
    finally:
        db.close()


def _tools():
    """Every registered MCP tool function, by reflection.

    Reflection rather than a hardcoded list so a tool added later is covered by the
    fail-closed test without anyone remembering to add it here.
    """
    out = {}
    for name in dir(mcp):
        fn = getattr(mcp, name)
        if asyncio.iscoroutinefunction(fn) and not name.startswith("_") \
                and name not in ("main",):
            out[name] = fn
    return out


# ── The load-bearing one: no caller ⇒ no data ─────────────────────────────────

def test_every_tool_fails_closed_without_a_caller():
    _reset()
    _job("j1", "ec2_deploy", "alice", "team-a", {"instance_id": "i-1"})

    tools = _tools()
    assert len(tools) >= 15, f"expected the full tool surface, found {sorted(tools)}"

    # Arguments for the tools that require one; everything else takes none.
    args = {"get_job": ("j1",), "list_containers": (1,)}

    for name, fn in sorted(tools.items()):
        out = _run(fn(*args.get(name, ())))
        blob = json.dumps(out, default=str)
        assert "Not authenticated" in blob, f"{name} did not fail closed: {blob[:200]}"
        assert "i-1" not in blob, f"{name} leaked data with no caller: {blob[:200]}"


def test_every_tool_guards_on_caller_structurally():
    """The behavioural test above proves the tools fail closed; this says *why* when one
    stops. A new tool that forgets `_caller()` fails here by name, which is a much shorter
    path to the fix than a leaked-data assertion in a loop.

    Same idea as tests/test_dashboard_stats_api.py asserting structurally that the stats
    endpoint makes no cloud call: a promise worth enforcing rather than describing.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(mcp))
    missing = []
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and any(
            isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "tool"
            for d in node.decorator_list
        ):
            if "'_caller'" not in ast.dump(node):
                missing.append(node.name)
    assert not missing, f"MCP tools with no _caller() guard: {missing}"


# ── Jobs: owner-scoped unless can_audit_jobs ──────────────────────────────────

def test_list_jobs_scopes_to_creator_for_non_admin():
    _reset()
    _job("j1", "ec2_deploy", "alice", "team-a")
    _job("j2", "ec2_deploy", "bob", "team-b")

    mine = _as(ALICE, mcp.list_jobs)
    assert [j["id"] for j in mine] == ["j1"], mine

    everything = _as(ADMIN, mcp.list_jobs)
    assert {j["id"] for j in everything} == {"j1", "j2"}


def test_list_jobs_workgroup_arg_cannot_widen_scope():
    """The `workgroup` argument is a filter, not an authorization boundary — asking for
    somebody else's workgroup must narrow to nothing, never reveal it."""
    _reset()
    _job("j1", "ec2_deploy", "alice", "team-a")
    _job("j2", "ec2_deploy", "bob", "team-b")

    assert _as(ALICE, mcp.list_jobs, None, "team-b") == []


def test_get_job_is_not_an_existence_oracle():
    _reset()
    _job("j2", "ec2_deploy", "bob", "team-b")

    denied = _as(ALICE, mcp.get_job, "j2")
    missing = _as(ALICE, mcp.get_job, "does-not-exist")
    # Same keys and same wording, so the two cases are indistinguishable. The id itself
    # differs because each response echoes the one the caller supplied — that is not a
    # disclosure, and asserting byte-equality would only pin the echo.
    assert set(denied) == set(missing) == {"error"}, (denied, missing)
    assert denied["error"].replace("j2", "X") == missing["error"].replace(
        "does-not-exist", "X"), (denied, missing)


def test_jobs_permission_is_enforced_but_empty_perms_stay_unrestricted():
    _reset()
    _job("j1", "ec2_deploy", "alice", "team-a")

    no_jobs_perm = _User("carol", permissions={"aws": ["read"]})
    out = _as(no_jobs_perm, mcp.list_jobs)
    assert "error" in out[0] and "jobs:read" in out[0]["error"], out

    # {} = NULL = unrestricted, exactly as api/auth.require_permission treats it.
    legacy = _User("legacy", workgroups=["team-a"], permissions={})
    assert isinstance(_as(legacy, mcp.list_jobs), list)


# ── Redaction ─────────────────────────────────────────────────────────────────

# Distinctive sentinels, not realistic values: the assertion below looks for each value
# as a substring of the whole response, and a short or numeric value ("42") collides with
# the digits in a timestamp and fails at random.
_SECRETS = {
    "bt_tf_state": "SENTINEL-bt-tf-state",
    "ps_registration_tf_state": "SENTINEL-ps-registration-tf-state",
    "ssh_secret_name": "SENTINEL-ssh-secret-name",
    "admin_password_ref": "SENTINEL-admin-password-ref",
    "admin_password_backend": "SENTINEL-admin-password-backend",
    "bt_shell_jump_id": "SENTINEL-bt-shell-jump-id",
}


def test_get_job_redacts_the_deploy_payload():
    _reset()
    payload = dict(_SECRETS)
    payload.update({"instance_id": "i-abc", "region": "us-east-2", "public_ip": "1.2.3.4"})
    _job("j1", "ec2_deploy", "alice", "team-a", payload)

    out = _as(ALICE, mcp.get_job, "j1")
    blob = json.dumps(out)
    for key, value in _SECRETS.items():
        assert key not in blob, f"{key} survived redaction"
        assert value not in blob, f"the value of {key} survived redaction"
    assert out["extra_data"]["instance_id"] == "i-abc"
    assert out["extra_data"]["region"] == "us-east-2"


def test_allowlist_and_denylist_never_disagree():
    """A key added carelessly to the allowlist that reads as a credential would be
    dropped by _is_sensitive_key anyway — assert nobody has created that contradiction."""
    bad = [k for k in mcp._SAFE_EXTRA_KEYS if mcp._is_sensitive_key(k)]
    assert not bad, f"allowlisted keys look sensitive: {bad}"


def test_safe_extra_drops_unknown_keys():
    out = mcp._safe_extra({"instance_id": "i-1", "something_new_and_secret": "x"})
    assert out == {"instance_id": "i-1"}
    assert mcp._safe_extra("not a dict") == {}
    assert mcp._safe_extra(None) == {}


def test_list_amis_never_passes_a_none_region():
    """aws_service.list_amis takes `region` as a required positional. Passing the
    caller's None straight through was a TypeError waiting for the first argument-less
    call, which is the common case from an AI client."""
    import web_dashboard.services.aws_service as aws_service
    from web_dashboard.api import aws as api_aws

    seen = {}

    async def _fake(region):
        seen["region"] = region
        return [{"id": "ami-1"}]

    orig_list, orig_region = aws_service.list_amis, api_aws._aws_region
    aws_service.list_amis = _fake
    api_aws._aws_region = lambda: "eu-west-1"
    try:
        out = _as(ADMIN, mcp.list_amis)
        assert out == {"amis": [{"id": "ami-1"}]}, out
        assert seen["region"] == "eu-west-1", seen
        _as(ADMIN, mcp.list_amis, "us-east-2")
        assert seen["region"] == "us-east-2", seen
    finally:
        aws_service.list_amis = orig_list
        api_aws._aws_region = orig_region


# ── Cloud instances: workgroup-scoped on is_admin ─────────────────────────────

def test_ec2_is_workgroup_scoped():
    _reset()
    _job("j1", "ec2_deploy", "alice", "team-a", {"instance_id": "i-a"})
    _job("j2", "ec2_deploy", "bob", "team-b", {"instance_id": "i-b"})

    got = _as(ALICE, mcp.list_ec2_instances)["instances"]
    assert [i["instance_id"] for i in got] == ["i-a"], got
    assert len(_as(ADMIN, mcp.list_ec2_instances)["instances"]) == 2


def test_azure_finds_single_vm_deploys():
    """`azure_vm_deploy` was never a job type — the real one is `azure_deploy`."""
    _reset()
    _job("j1", "azure_deploy", "alice", "team-a",
         {"vm_name": "vm-a", "resource_group": "rg1"})
    got = _as(ADMIN, mcp.list_azure_vms)["vms"]
    assert [v["vm_name"] for v in got] == ["vm-a"], got


def test_gcp_and_oci_are_workgroup_scoped():
    _reset()
    _job("g1", "gce_deploy", "alice", "team-a", {"instance_name": "gce-a"})
    _job("g2", "gce_deploy", "bob", "team-b", {"instance_name": "gce-b"})
    _job("o1", "oci_deploy", "alice", "team-a", {"ocid": "ocid-a"})
    _job("o2", "oci_deploy", "bob", "team-b", {"ocid": "ocid-b"})

    gcp = _as(ALICE, mcp.list_gcp_instances)["instances"]
    assert [i["instance_name"] for i in gcp] == ["gce-a"], gcp
    oci = _as(ALICE, mcp.list_oci_instances)["instances"]
    assert [i["ocid"] for i in oci] == ["ocid-a"], oci


def test_cloud_tools_key_on_is_admin_not_effective_admin():
    """THE unification test. JIT is effective-admin but not is_admin, so the four cloud
    consoles still scope it to its workgroups — matching api/aws.py:_accessible_workgroups.
    Deleting this test is how the two rules quietly become one."""
    _reset()
    _job("j1", "ec2_deploy", "alice", "team-a", {"instance_id": "i-a"})
    _job("j2", "ec2_deploy", "bob", "team-b", {"instance_id": "i-b"})

    assert mcp._cloud_workgroups(JIT) == [], "JIT must NOT be admin for the cloud consoles"
    assert mcp._cloud_workgroups(ADMIN) is None
    got = _as(JIT, mcp.list_ec2_instances)["instances"]
    assert got == [], f"effective-admin must not widen a cloud console: {got}"


def test_effective_admin_does_widen_creator_scoped_resources():
    """The other half of the same split: creator-scoped resources DO honour the JIT grant."""
    rows = [{"created_by": "alice", "id": 1}, {"created_by": "bob", "id": 2}]
    assert mcp._creator_scoped(rows, JIT) == rows
    assert mcp._creator_scoped(rows, ALICE) == [rows[0]]
    assert mcp._creator_scoped(rows, ADMIN) == rows


# ── Summary counts are scoped too ─────────────────────────────────────────────

def test_dashboard_summary_counts_are_scoped():
    _reset()
    _job("j1", "ec2_deploy", "alice", "team-a")
    _job("j2", "ec2_deploy", "bob", "team-b")

    assert _as(ALICE, mcp.dashboard_summary)["total_jobs"] == 1
    assert _as(ADMIN, mcp.dashboard_summary)["total_jobs"] == 2


# ── _MCPAuth: the gate and the ContextVar ─────────────────────────────────────

def _drive(app, headers):
    """Run an ASGI app for one HTTP request; return (status, body, captured_user)."""
    seen = {}

    async def _inner(scope, receive, send):
        seen["user"] = mcp._mcp_user.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    sent = []

    async def _send(msg):
        sent.append(msg)

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "headers": [(k, v) for k, v in headers]}
    _run(app(_inner)(scope, _receive, _send) if callable(app) else None)
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body, seen.get("user")


def _auth_app():
    return lambda inner: mcp._MCPAuth(inner)


def test_gate_404s_before_auth_when_disabled(monkeypatch=None):
    from web_dashboard.services import feature_flags
    orig = feature_flags.enabled
    feature_flags.enabled = lambda flag, *a, **k: False
    try:
        status, body, user = _drive(_auth_app(), [(b"authorization", b"Bearer vmcli_x")])
        assert status == 404, status
        assert b"not enabled" in body
        assert user is None, "a disabled server must not resolve a caller"
    finally:
        feature_flags.enabled = orig


def test_missing_or_malformed_token_401s():
    from web_dashboard.services import feature_flags
    orig = feature_flags.enabled
    feature_flags.enabled = lambda flag, *a, **k: True
    try:
        for headers in ([], [(b"authorization", b"Bearer nope")],
                        [(b"authorization", b"Basic abc")]):
            status, _, user = _drive(_auth_app(), headers)
            assert status == 401, (headers, status)
            assert user is None
    finally:
        feature_flags.enabled = orig


def test_valid_pat_populates_the_context_var():
    """The wrapper's whole job. If this breaks, every tool fails closed rather than
    going unscoped — which is the point of the _caller() guard, not a substitute for it."""
    import hashlib
    import uuid as _uuid
    from web_dashboard.database import PersonalAccessToken, User as DBUser
    from web_dashboard.services import feature_flags

    raw = "vmcli_" + "a" * 64
    db = SessionLocal()
    try:
        uid = str(_uuid.uuid4())
        db.add(DBUser(id=uid, username="pat-owner", hashed_password="x", is_active=True))
        db.add(PersonalAccessToken(
            id=str(_uuid.uuid4()), user_id=uid, name="test",
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            is_active=True, expires_at=datetime.utcnow() + timedelta(days=1)))
        db.commit()
    finally:
        db.close()

    orig = feature_flags.enabled
    feature_flags.enabled = lambda flag, *a, **k: True
    try:
        status, _, user = _drive(
            _auth_app(), [(b"authorization", f"Bearer {raw}".encode())])
        assert status == 200, status
        assert user is not None and user.username == "pat-owner", user
    finally:
        feature_flags.enabled = orig


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
