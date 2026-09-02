"""The AKS half of the rotator's identity mapping — the Azure role assignment.

Why this file exists. On 2026-09-02 a register against AKS cluster `k8s-aks-central`
failed at step 5 (the proving rotation) with a 400 whose body was the rotation plugin's
own log:

    Functional account maps to cluster identity 'oid 6f80ab07-…'.
    Error: HTTP 403 (Forbidden): serviceaccounts "pra-access" is forbidden: User
    "6f80ab07-…" cannot get resource "serviceaccounts" in API group "" in the namespace
    "pra-access": User does not have access to the resource in Azure. Update role
    assignment to allow access.

Everything the dashboard is responsible for had worked: the ClusterRole and
ClusterRoleBinding were applied, the managed system and account were created, the
address parsed, the identity authenticated. **The binding is not what authorises a
Microsoft Entra principal on these clusters.** `terraform/k8s_cluster/azure_aks` sets
`azure_rbac_enabled`, which delegates Kubernetes API authorisation to Azure role
assignments, and the role such an identity is usually given — "Azure Kubernetes Service
Cluster User Role" — is CONTROL-plane only: it grants `listClusterUserCredential`, i.e.
the right to fetch a kubeconfig, and not one Kubernetes verb. Hence a first rotation
that 403s while looking perfectly configured.

Same shape as the EKS `authenticationMode` discovery (see `_ensure_eks_access_entry`):
on both clouds the cluster-side identity mapping is invisible from inside the cluster
and fails only at the first rotation, never at register.

Runs under pytest or standalone:  python tests/test_ps_k8s_aks_role_assignment.py
"""
import asyncio
import importlib.util
import json
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── module scaffolding (no app, no DB, no Azure SDK) ─────────────────────────────

def _pkgs():
    pkg = sys.modules.setdefault("web_dashboard", types.ModuleType("web_dashboard"))
    pkg.__path__ = [os.path.join(_ROOT, "web_dashboard")]
    svc = sys.modules.setdefault("web_dashboard.services",
                                 types.ModuleType("web_dashboard.services"))
    svc.__path__ = [os.path.join(_ROOT, "web_dashboard", "services")]
    sys.modules.setdefault("sqlalchemy", types.ModuleType("sqlalchemy"))
    _orm = types.ModuleType("sqlalchemy.orm")
    _orm.Session = object
    sys.modules.setdefault("sqlalchemy.orm", _orm)
    _db = types.ModuleType("web_dashboard.database")

    class _Col:
        """Stands in for a SQLAlchemy column in class-level comparisons."""
        def __eq__(self, other):
            return True
        def __hash__(self):
            return 0

    class _Meta(type):
        def __getattr__(cls, name):
            return _Col()

    _db.Job = _Meta("Job", (), {})
    _db.K8sCluster = _Meta("K8sCluster", (), {})
    sys.modules.setdefault("web_dashboard.database", _db)
    _cfg = types.ModuleType("web_dashboard.config")
    _cfg.settings = types.SimpleNamespace()
    sys.modules.setdefault("web_dashboard.config", _cfg)


def _load(name):
    _pkgs()
    path = os.path.join(_ROOT, "web_dashboard", "services", f"{name}.py")
    spec = importlib.util.spec_from_file_location(
        f"web_dashboard.services.{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


az = _load("azure_service")
svc = _load("ps_k8s_token_service")


class _Resp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


class _FakeClient:
    """One-shot httpx.AsyncClient stand-in that records the call it was given."""
    calls = []

    def __init__(self, response, *a, **kw):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        _FakeClient.calls.append(("GET", url, None, headers))
        return self._response

    async def put(self, url, json=None, headers=None):
        _FakeClient.calls.append(("PUT", url, json, headers))
        return self._response


def _with_arm(response, fn):
    """Run ``fn`` with azure_service's credentials + httpx replaced."""
    _FakeClient.calls = []
    fake_httpx = types.ModuleType("httpx")
    fake_httpx.AsyncClient = lambda *a, **kw: _FakeClient(response)
    sys.modules["httpx"] = fake_httpx

    async def _creds():
        return (types.SimpleNamespace(
            get_token=lambda *a, **kw: types.SimpleNamespace(token="arm-token")),
            "sub-1111")
    orig = az._ensure_creds
    az._ensure_creds = _creds
    try:
        return fn()
    finally:
        az._ensure_creds = orig
        sys.modules.pop("httpx", None)


# ── the role definitions ─────────────────────────────────────────────────────────

def test_the_aks_data_plane_role_guids_are_the_well_known_ones():
    """Built-in role GUIDs are identical in every tenant, and a wrong one does not
    fail loudly — ARM happily creates an assignment for a role that grants something
    else entirely (or nothing at all)."""
    assert az.AKS_RBAC_ROLE_IDS == {
        "reader": "7f6c6a51-bcf8-42ba-9220-52d62157d7db",
        "writer": "a7ffa36f-339b-4b5c-8bdf-e2c188b2c0eb",
        "admin": "3498e952-d568-435e-9b2c-8d77e338d7f7",
        "clusteradmin": "b1ff04bb-8a4e-4dc4-8eb5-8693973ce19b",
    }


def test_the_cluster_resource_id_needs_all_three_parts():
    assert az.aks_cluster_resource_id("s", "rg", "c").endswith(
        "/resourceGroups/rg/providers/Microsoft.ContainerService/managedClusters/c")
    for args in (("", "rg", "c"), ("s", "", "c"), ("s", "rg", "")):
        try:
            az.aks_cluster_resource_id(*args)
            raise AssertionError(f"{args} should not build a scope")
        except az.AzureError:
            pass


# ── reading the authorisation mode ───────────────────────────────────────────────

def test_azure_rbac_is_read_under_both_spellings():
    """ARM returns ``enableAzureRBAC``; the CLI prints ``enableAzureRbac``. Keying on
    one spelling reports every cluster as NOT using Azure RBAC, which is the answer
    that skips the grant and leaves the rotator unauthorised."""
    for key in ("enableAzureRBAC", "enableAzureRbac"):
        resp = _Resp(200, {"properties": {"aadProfile": {key: True}}})
        assert _with_arm(resp, lambda: asyncio.run(
            az.aks_azure_rbac_enabled("rg", "c"))) is True, key
    resp = _Resp(200, {"properties": {"aadProfile": {"enableAzureRBAC": False}}})
    assert _with_arm(resp, lambda: asyncio.run(
        az.aks_azure_rbac_enabled("rg", "c"))) is False


def test_an_unreadable_cluster_is_none_not_false():
    resp = _Resp(403, text="AuthorizationFailed")
    assert _with_arm(resp, lambda: asyncio.run(
        az.aks_azure_rbac_enabled("rg", "c"))) is None


# ── the assignment itself ────────────────────────────────────────────────────────

_OID = "6f80ab07-1334-493c-b119-f5ff7e829a9d"


def _assign(response, **kw):
    args = {"scope": "/subscriptions/sub-1111/rg/cluster/namespaces/pra-access",
            "role": "writer", "principal_id": _OID}
    args.update(kw)
    return _with_arm(response, lambda: asyncio.run(az.ensure_role_assignment(**args)))


def test_the_grant_is_an_upsert_keyed_on_scope_principal_and_role():
    out = _assign(_Resp(201, {"id": "/x"}))
    method, url, body, headers = _FakeClient.calls[-1]
    assert method == "PUT"
    assert f"/providers/Microsoft.Authorization/roleAssignments/{out['name']}" in url
    assert "api-version=" in url
    assert headers["Authorization"] == "Bearer arm-token"
    props = body["properties"]
    assert props["roleDefinitionId"].endswith(az.AKS_RBAC_ROLE_IDS["writer"])
    assert props["principalId"] == _OID
    # Without principalType ARM resolves the principal through Graph, and a service
    # principal the caller cannot read 400s with "principal not found".
    assert props["principalType"] == "ServicePrincipal"
    assert out["created"] is True
    # Deterministic: the same three inputs must address the same assignment, or a
    # repeat grant piles up duplicates nothing can revoke by name.
    again = _assign(_Resp(200, {"id": "/x"}))
    assert again["name"] == out["name"]


def test_a_raw_role_guid_is_accepted_and_a_bad_name_is_not():
    out = _assign(_Resp(201), role="b1ff04bb-8a4e-4dc4-8eb5-8693973ce19b")
    assert out["role_id"] == "b1ff04bb-8a4e-4dc4-8eb5-8693973ce19b"
    try:
        _assign(_Resp(201), role="Contributor-ish")
        raise AssertionError("an unknown role name must be refused")
    except az.AzureError as exc:
        assert "writer" in str(exc)


def test_an_existing_equivalent_assignment_is_not_a_failure():
    """Azure 409s when an equivalent assignment already exists under another name —
    someone made it by hand, or at a wider scope. Nothing to do, and re-registering
    must not fail on it."""
    out = _assign(_Resp(409, text='{"error":{"code":"RoleAssignmentExists"}}'))
    assert out["created"] is False


def test_a_refused_delegation_raises_so_the_caller_can_say_so():
    try:
        _assign(_Resp(403, text="AuthorizationFailed: roleAssignments/write"))
        raise AssertionError("a 403 must not read as success")
    except az.AzureError as exc:
        assert "403" in str(exc)


def test_a_principal_that_is_not_a_guid_is_refused():
    """The object-id / client-id trap, one layer down: ARM accepts an application id
    and creates an assignment that grants nothing, because assignments bind to the
    object id."""
    try:
        _assign(_Resp(201), principal_id="my-service-principal")
        raise AssertionError("a non-GUID principal must be refused")
    except az.AzureError as exc:
        assert "OBJECT id" in str(exc)


# ── the token service's use of it ────────────────────────────────────────────────

def _row(**kw):
    base = dict(id="c-9", cloud="azure", name="k8s-aks-central", region="centralus",
                deploy_job_id=None, ps_token_account_id=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _ensure(cfg, *, rbac_enabled=True, assign=None, namespace="pra-access"):
    """Run _ensure_aks_role_assignment with a stub azure_service; return the warnings
    and the calls the stub recorded."""
    store = {"azure_subscription_id": "sub-1111",
             "k8s_ps_rotator_aks_sp_object_id": _OID}
    store.update(cfg)
    calls = []

    fake_az = types.ModuleType("web_dashboard.services.azure_service")
    fake_az.aks_cluster_resource_id = az.aks_cluster_resource_id

    async def _enabled(rg, name):
        calls.append(("rbac?", rg, name))
        return rbac_enabled
    fake_az.aks_azure_rbac_enabled = _enabled

    async def _assign(*, scope, role, principal_id):
        calls.append(("assign", scope, role, principal_id))
        if assign is not None:
            raise assign
        return {"created": True, "name": "n", "scope": scope, "role_id": role}
    fake_az.ensure_role_assignment = _assign
    sys.modules["web_dashboard.services.azure_service"] = fake_az

    orig_cfg, orig_bool, orig_tf = svc._cfg, svc._cfg_bool, svc._deploy_tf_variables
    svc._cfg = lambda key, default="": store.get(key, default)
    svc._cfg_bool = lambda key, default=False: bool(store.get(key, default))
    svc._deploy_tf_variables = lambda db, row: store.get("_tf", {})
    warnings = []
    try:
        asyncio.run(svc._ensure_aks_role_assignment(
            None, _row(), warnings, namespace=namespace,
            cluster_name=store.get("_name", ""),
            resource_group=store.get("_rg", "dashboard-sandbox-rg")))
    finally:
        svc._cfg, svc._cfg_bool, svc._deploy_tf_variables = orig_cfg, orig_bool, orig_tf
        sys.modules.pop("web_dashboard.services.azure_service", None)
    return warnings, calls


def test_writer_lands_on_the_namespace_and_admin_on_the_cluster():
    """Reader and Writer are the two roles Azure documents as assignable at
    ``<cluster>/namespaces/<name>``; anything wider has to go on the cluster, and
    quietly appending /namespaces to those would create an assignment at a scope
    Azure does not honour for them."""
    _w, calls = _ensure({"k8s_ps_rotator_aks_role": "writer"})
    scope = [c for c in calls if c[0] == "assign"][0][1]
    assert scope.endswith("/managedClusters/k8s-aks-central/namespaces/pra-access")

    _w, calls = _ensure({"k8s_ps_rotator_aks_role": "admin"})
    scope = [c for c in calls if c[0] == "assign"][0][1]
    assert scope.endswith("/managedClusters/k8s-aks-central")


def test_a_missing_object_id_warns_with_the_command_that_fixes_it():
    warnings, calls = _ensure({"k8s_ps_rotator_aks_sp_object_id": ""})
    assert not [c for c in calls if c[0] == "assign"]
    assert len(warnings) == 1
    w = warnings[0]
    assert "k8s_ps_rotator_aks_sp_object_id" in w
    assert "az role assignment create" in w
    # --assignee resolves through Graph and fails outright on a service principal the
    # operator cannot read ("Cannot find user or service principal in graph database").
    assert "--assignee-object-id" in w
    assert "--assignee " not in w
    assert "--assignee-principal-type ServicePrincipal" in w


def test_a_cluster_that_does_not_use_azure_rbac_gets_no_assignment():
    """There, the ClusterRoleBinding really is what authorises the rotator, and an
    Azure data-plane role assignment would be a permission grant with no purpose."""
    warnings, calls = _ensure({}, rbac_enabled=False)
    assert not [c for c in calls if c[0] == "assign"]
    assert warnings == []


def test_an_unreadable_authorisation_mode_still_grants():
    """None is "could not tell", and reading it as False is how a cluster that DOES
    use Azure RBAC would be left unauthorised on the strength of one failed GET."""
    _warnings, calls = _ensure({}, rbac_enabled=None)
    assert [c for c in calls if c[0] == "assign"]


def test_the_switch_turns_the_grant_off_without_touching_the_rest():
    _warnings, calls = _ensure({"k8s_ps_rotator_aks_assign_role": False})
    assert calls == []


def test_a_failed_grant_becomes_a_warning_carrying_the_command():
    """Non-fatal, exactly like the EKS access entry: registration still completes and
    Password Safe's own Verify Functional Account can then be run, but the remedy has
    to reach the operator."""
    warnings, calls = _ensure({}, assign=RuntimeError("403 AuthorizationFailed"))
    assert [c for c in calls if c[0] == "assign"]
    assert len(warnings) == 1
    assert "403 AuthorizationFailed" in warnings[0]
    assert "az role assignment create" in warnings[0]
    assert _OID in warnings[0]


def test_a_created_assignment_warns_about_the_propagation_delay():
    """Register rotates at step 5, and a new role assignment can take five minutes to
    reach the authorisation server — so the 403 that follows a first registration is
    not the same failure as this one."""
    warnings, _calls = _ensure({})
    assert len(warnings) == 1
    assert "five minutes" in warnings[0]


def test_the_azure_leg_runs_before_the_manifest_is_applied():
    """Source-level, and the same argument as the EKS entry's position: applying a
    binding for an identity the cloud has never heard of is the state that looks
    configured and fails at the first rotation."""
    import inspect
    src = inspect.getsource(svc._apply_rbac)
    assert src.index("_ensure_aks_role_assignment") < src.index("apply_ps_rotator_rbac")


def test_a_missing_binding_subject_reaches_the_failed_job():
    """`_apply_rbac`'s note is returned, and a later step raising means there is no
    return value (see `_failure_message`) — so the one line that says the rotator has
    no binding at all has to be copied into the warnings the failure path reads."""
    fake_k8s = types.ModuleType("web_dashboard.services.k8s_service")

    async def _apply(db, cid, *, mode, subject_name=""):
        return ("rotator ClusterRole applied; no binding subject is configured for "
                "local — run Verify Functional Account in Password Safe")
    fake_k8s.apply_ps_rotator_rbac = _apply
    sys.modules["web_dashboard.services.k8s_service"] = fake_k8s

    class _DB:
        def query(self, model):
            class _Q:
                def filter(self, *a, **kw):
                    return self
                def first(self):
                    return _row(cloud="local")
            return _Q()

    orig_bool = svc._cfg_bool
    svc._cfg_bool = lambda key, default=False: True if key == \
        "k8s_ps_rotator_apply_rbac" else default
    warnings = []
    try:
        note = asyncio.run(svc._apply_rbac(_DB(), "c-9", mode="longlived",
                                           warnings=warnings))
    finally:
        svc._cfg_bool = orig_bool
        sys.modules.pop("web_dashboard.services.k8s_service", None)
    assert "no binding subject" in note
    assert warnings == [note]


if __name__ == "__main__":
    fns = [v for name, v in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
