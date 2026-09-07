"""Cloud VMs the dashboard did not deploy: discovery, power, and the destroy that must not.

Every cloud console here is job-driven — it starts from completed `*_deploy` jobs and
fetches live state for exactly those identifiers — so a VM launched in the cloud's own
console, by Terraform, or before this dashboard existed is invisible. Discovery makes it
visible and powerable. It must never make it destroyable.

The load-bearing tests, in order of what they would cost if they were wrong:

  * **The destroy guard.** `azure_service.get_vm` answers "regardless of tags" by design —
    that is what lets the destroy fan-out find a VDI seat or a VM whose job row was pruned.
    Before this, any VM in a listed resource group could be destroyed by name; discovery is
    what makes such a name easy to find. The guard is the thing that makes "power yes,
    destroy no" true rather than a claim about which buttons get rendered.
  * **The two sets are disjoint.** Unmanaged means no deploy job AND no dashboard tag. Drop
    the tag half and a VDI seat appears in both lists — and then starts refusing the
    destroy that is legitimately its own.
  * **Power resolves its locator from discovery, not the request.** AWS needs a region and
    Azure a resource group, which the deploy job used to supply. Taking either from the
    caller would turn /power/stop into "stop anything of this name anywhere the credentials
    reach".
  * **Untagged means admin-only**, which is not a new rule — it is what `_assert_can_act`
    already says about an untagged managed resource.

Run: python tests/test_unmanaged_vms.py   (or under pytest)
"""
import ast
import asyncio
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="unmanaged-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-unmanaged-tests")

try:
    from web_dashboard.database import Base, engine
    from web_dashboard.services import unmanaged_vms
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

# feature_flags.flags() reads app_config, so the schema has to exist before any test
# touches it — the flag test does.
Base.metadata.create_all(bind=engine)

DASHBOARD_TAGS = {"managed-by": "vm-dashboard", "workgroup": "team-a"}
LEGACY_TAGS = {"ManagedBy": "vm-cli-dashboard"}
SOMEBODY_ELSES = {"Name": "prod-db-01", "owner": "dba-team"}


def _aws(instance_id, tags):
    return {"instance_id": instance_id, "region": "us-east-1", "state": "running",
            "tags": tags}


# ── What counts as unmanaged ──────────────────────────────────────────────────

def test_a_vm_with_a_deploy_job_is_not_unmanaged():
    assert unmanaged_vms.is_unmanaged(_aws("i-1", SOMEBODY_ELSES), {"i-1"}, "aws") is False
    assert unmanaged_vms.is_unmanaged(_aws("i-2", SOMEBODY_ELSES), {"i-1"}, "aws") is True


def test_a_dashboard_tagged_vm_is_not_unmanaged_even_with_no_job():
    """The half that keeps the two sets disjoint. A VDI pool seat and a VM whose job row was
    pruned are both this dashboard's and both already appear in the managed listing; without
    the tag check they would show in both, and then be refused the destroy that is theirs."""
    for tags in (DASHBOARD_TAGS, LEGACY_TAGS):
        assert unmanaged_vms.is_unmanaged(_aws("i-9", tags), set(), "aws") is False
    assert unmanaged_vms.is_unmanaged(_aws("i-9", SOMEBODY_ELSES), set(), "aws") is True


def test_a_destroyed_deploy_job_still_claims_its_vm():
    """A VM whose destroy failed part-way is still this dashboard's problem. Listing it as
    somebody else's would send the operator to the wrong repair — and past the guard."""
    assert unmanaged_vms.is_unmanaged(_aws("i-1", SOMEBODY_ELSES), {"i-1"}, "aws") is False


def test_partition_marks_every_row_and_drops_the_managed_ones():
    rows = [_aws("i-1", SOMEBODY_ELSES), _aws("i-2", DASHBOARD_TAGS),
            _aws("i-3", {"workgroup": "Team-B"})]
    out = unmanaged_vms.partition(rows, {"i-1"}, "aws")
    assert [r["instance_id"] for r in out] == ["i-3"]
    row = out[0]
    # Stated, never implied: a consumer that infers "managed" from a missing job_id gets it
    # wrong the first time a managed row arrives with a pruned job.
    assert row["managed"] is False and row["cloud"] == "aws"
    assert row["job_id"] is None and row["deployed_by"] is None
    assert row["workgroup"] == "team-b", "the workgroup tag is read and lowercased"


def test_an_untagged_vm_has_no_workgroup_and_is_therefore_admin_only():
    """Not a new rule — it is what _assert_can_act already says about an untagged managed
    resource. The dashboard never assigns a workgroup to a VM it did not deploy."""
    assert unmanaged_vms.workgroup_of(SOMEBODY_ELSES) == ""
    rows = unmanaged_vms.partition([_aws("i-1", SOMEBODY_ELSES)], set(), "aws")
    assert rows[0]["workgroup"] is None
    assert unmanaged_vms.visible_to(rows, ["team-a"]) == [], "a non-admin must not see it"
    assert len(unmanaged_vms.visible_to(rows, None)) == 1, "an admin sees it"


def test_visibility_follows_the_workgroup_tag():
    rows = unmanaged_vms.partition(
        [_aws("i-1", {"workgroup": "team-a"}), _aws("i-2", {"workgroup": "team-b"})],
        set(), "aws")
    assert [r["instance_id"] for r in unmanaged_vms.visible_to(rows, ["team-a"])] == ["i-1"]
    assert len(unmanaged_vms.visible_to(rows, None)) == 2


def test_every_cloud_has_an_identifier_key():
    """A cloud missing from ID_KEY raises mid-request rather than at import."""
    assert set(unmanaged_vms.ID_KEY) == {"aws", "azure", "gcp", "oci"}
    for cloud, key in unmanaged_vms.ID_KEY.items():
        row = {key: "x", "tags": {}}
        assert unmanaged_vms.is_unmanaged(row, set(), cloud) is True
        assert unmanaged_vms.is_unmanaged(row, {"x"}, cloud) is False


# ── The destroy guard ─────────────────────────────────────────────────────────

def test_destroy_is_refused_on_a_vm_the_dashboard_did_not_deploy():
    try:
        unmanaged_vms.assert_not_unmanaged(SOMEBODY_ELSES, "VM 'prod-db-01'")
        raise AssertionError("expected a refusal")
    except unmanaged_vms.UnmanagedVMError as exc:
        msg = str(exc)
        assert "prod-db-01" in msg
        # The reason has to say what IS available, or the operator reads it as a bug.
        assert "power" in msg.lower(), msg


def test_destroy_is_allowed_on_a_dashboard_tagged_vm_with_no_job():
    """The case the fallback exists for: a VDI pool seat, or a VM whose job row was pruned.
    Both go through azure_service.deploy_vm, which tags them."""
    for tags in (DASHBOARD_TAGS, LEGACY_TAGS):
        unmanaged_vms.assert_not_unmanaged(tags, "VM 'seat-01'")      # must not raise


def test_the_azure_destroy_fallback_actually_calls_the_guard():
    """The guard only guards if it is wired in. `azure_service.get_vm` answers regardless of
    tags — that is deliberate and load-bearing for the VDI path — so the ownership question
    has to be asked at the destroy site, before the terminate job is created."""
    src = open(os.path.join(_ROOT, "web_dashboard/api/azure.py"), encoding="utf-8").read()
    fn = next(f for f in ast.walk(ast.parse(src))
              if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
              and f.name == "_destroy_without_deploy_job")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
    guarded = [n for n in calls if isinstance(n.func, ast.Attribute)
               and n.func.attr == "assert_not_unmanaged"]
    assert guarded, "the destroy fan-out must ask whether the VM is the dashboard's"

    created = [n for n in calls if isinstance(n.func, ast.Attribute)
               and n.func.attr == "create_job"]
    assert created, "expected a destroy job to be created in this function"
    assert guarded[0].lineno < created[0].lineno, \
        "the guard must run BEFORE the destroy job is created"


def test_get_vm_returns_tags_so_the_guard_has_something_to_ask():
    src = open(os.path.join(_ROOT, "web_dashboard/services/azure_service.py"),
               encoding="utf-8").read()
    fn = next(f for f in ast.walk(ast.parse(src)) if isinstance(f, ast.FunctionDef)
              and f.name == "_get_vm_sync")
    returned = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    keys = {k.value for r in returned if isinstance(r.value, ast.Dict)
            for k in r.value.keys if isinstance(k, ast.Constant)}
    assert "tags" in keys, "the guard cannot ask a question the lookup does not answer"


# ── Power resolves its locator from discovery, never from the caller ──────────

def _power_source_of(module: str, cloud_key: str):
    src = open(os.path.join(_ROOT, f"web_dashboard/api/{module}.py"), encoding="utf-8").read()
    fn = next(f for f in ast.walk(ast.parse(src)) if isinstance(f, ast.FunctionDef)
              and f.name == "_power_endpoint")
    return src, fn


def test_aws_and_azure_power_take_their_locator_from_discovery():
    """A caller-supplied region or resource group would make /power/stop mean "stop anything
    of this name anywhere the credentials reach". The locator comes from the discovery
    listing — the set the caller was already allowed to see."""
    for module, locator in (("aws", "region"), ("azure", "resource_group")):
        _, fn = _power_source_of(module, locator)
        found = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "find"]
        assert found, f"{module} power must resolve an unmanaged VM through discovery"
        # The request model must not have grown a field that could carry the locator.
        src = open(os.path.join(_ROOT, f"web_dashboard/api/{module}.py"),
                   encoding="utf-8").read()
        model = next(c for c in ast.walk(ast.parse(src)) if isinstance(c, ast.ClassDef)
                     and c.name == "PowerOpRequest")
        fields = {t.target.id for t in model.body if isinstance(t, ast.AnnAssign)}
        assert locator not in fields, \
            f"{module}: PowerOpRequest must not accept a caller-supplied {locator}"


def test_discovery_returns_nothing_when_the_flag_is_off():
    """find() is what power leans on, so the flag has to hold there too — not only on the
    listing route. Otherwise turning discovery off would leave the power path open."""
    from web_dashboard.api import unmanaged as api_unmanaged

    async def _never():                                  # pragma: no cover — must not run
        raise AssertionError("called the cloud with discovery disabled")

    original = api_unmanaged.enabled
    api_unmanaged.enabled = lambda: False
    try:
        got = asyncio.run(api_unmanaged.find(
            "aws", "i-1", job_type="ec2_deploy", fetch_live=_never, cache_key="k"))
        assert got is None
    finally:
        api_unmanaged.enabled = original


def test_the_unmanaged_route_carries_no_destroy_verb():
    """Destroy is absent here rather than hidden. A hidden button is a UI promise; an
    endpoint that does not exist is a guarantee."""
    src = open(os.path.join(_ROOT, "web_dashboard/api/unmanaged.py"), encoding="utf-8").read()
    tree = ast.parse(src)

    # Nothing in here may name a teardown primitive, on any cloud.
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute)}
    called |= {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Name)}
    for verb in ("terminate_vm", "terminate_instance", "delete_instance", "destroy",
                 "create_job", "begin_delete"):
        assert verb not in called, f"{verb} must not be reachable from unmanaged discovery"

    # And it must define no mutating route. The module builds handlers rather than
    # decorating them, so check both shapes.
    decorators = {d.func.attr for f in ast.walk(tree)
                  if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                  for d in f.decorator_list
                  if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)}
    assert not (decorators & {"post", "put", "delete", "patch"}), decorators
    methods = {c.value for n in ast.walk(tree) if isinstance(n, ast.Call)
               for kw in n.keywords if kw.arg == "methods"
               and isinstance(kw.value, (ast.List, ast.Tuple))
               for c in kw.value.elts if isinstance(c, ast.Constant)}
    assert methods <= {"GET"}, f"discovery is read-only; found {methods}"


def test_no_api_route_is_bound_to_a_fetcher():
    """Building this feature attached `@router.get("/instances")` to the discovery fetcher
    on two clouds, because the new block was inserted between the decorator and the handler
    it belonged to. Nothing failed at import — both listings simply started returning the
    wrong thing. A decorator separated from its function is invisible in review and cheap
    to assert against."""
    from web_dashboard.main import app

    misbound = [(r.path, r.name) for r in app.routes
                if getattr(r, "path", "").startswith("/api/")
                and getattr(r, "name", "").startswith(("_fetch", "_discovery"))]
    assert not misbound, f"routes bound to a helper rather than a handler: {misbound}"

    # And the four listings each still answer with their own handler.
    by_path = {getattr(r, "path", ""): getattr(r, "name", "") for r in app.routes}
    for path, expected in (("/api/aws/instances", "list_instances"),
                           ("/api/azure/vms", "list_vms"),
                           ("/api/gcp/instances", "list_instances"),
                           ("/api/oci/instances", "list_instances")):
        assert by_path.get(path) == expected, (path, by_path.get(path))


def test_all_four_clouds_expose_discovery():
    from web_dashboard.main import app
    paths = {getattr(r, "path", "") for r in app.routes}
    for cloud in ("aws", "azure", "gcp", "oci"):
        assert f"/api/{cloud}/unmanaged" in paths, cloud


# ── The flag ──────────────────────────────────────────────────────────────────

def test_the_flag_exists_default_off_and_is_served_to_the_ui():
    from web_dashboard.config import settings
    from web_dashboard.services import feature_flags

    assert settings.cloud_unmanaged_discovery_enabled is False, "must ship off"
    assert "cloud_unmanaged_discovery_enabled" in feature_flags.flags()
    # The settings panel renders from feature_map(); a flag missing here is a toggle that
    # is permanently off however the operator sets it.
    assert "cloud_unmanaged_discovery" in feature_flags.feature_map()


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
