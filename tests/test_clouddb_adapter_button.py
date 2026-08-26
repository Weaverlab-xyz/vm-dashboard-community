"""The Databases page's "Function (DB grant)" row action.

The pairing logic itself is pinned in test_clouddb_adapter_pairing.py. What is pinned
here is the wiring that makes it reachable safely from a button — the parts where a
mistake costs a terraform apply against real cloud resources, or a credential staged in
a cloud secret store for a function nothing will ever call:

  * the duplicate guard, because cloud_function_service.deploy does NOT look a name up
  * the pre-flights all running BEFORE anything is queued
  * the region the function lands in being the DATABASE's, not the default one
  * the button offering exactly what the endpoint accepts
"""
import os
import re
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
sys.path.insert(0, _ROOT)

_PAGE = os.path.join(_ROOT, "web_dashboard", "templates", "databases", "index.html")
_API = os.path.join(_ROOT, "web_dashboard", "api", "cloud_databases.py")
_FNSVC = os.path.join(_ROOT, "web_dashboard", "services", "cloud_function_service.py")
_RCFG = os.path.join(_ROOT, "web_dashboard", "services", "region_config.py")

CONF = {}


def _stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


# Stubbed unconditionally, for the same reason the sibling suites do it: gating on
# whether a dependency happens to be installed made a previous suite pass locally and
# fail in CI.
_stub("web_dashboard.services.config_service",
      get=lambda key, default="": CONF.get(key, default),
      get_bool=lambda key, default=False: bool(CONF.get(key, default)),
      set=lambda key, value: CONF.__setitem__(key, value),
      delete=lambda key: CONF.pop(key, None))

try:
    import pydantic  # noqa: F401
except ImportError:
    _stub("web_dashboard.config", settings=types.SimpleNamespace())


def _read(path):
    return open(path, encoding="utf-8").read()


def _endpoint():
    """The adapter-pair handler's source. Anchored on the route decorator, not the
    path string — that also appears in the module's route list up top."""
    api = _read(_API)
    return api.split('@router.post("/{db_id}/adapter-pair"')[1]


# -- The duplicate guard -------------------------------------------------------
# deploy() inserts a CloudFunction unconditionally, with no existence check on the
# name, and every deploy gets a fresh (empty) terraform directory. So a second pairing
# of the same database leaves a duplicate row wedged in 'deploying' and an apply that
# dies on "already exists". The name is deterministic, so the caller has to ask first.

def test_the_lookup_excludes_only_deleted_functions():
    """'failed' and 'deploying' both still own the name — the remedy is deleting the
    function on the Functions page, which is what the 409 must say."""
    body = _read(_FNSVC).split("def find_by_names(")[1].split("\ndef ")[0]
    assert 'CloudFunction.status != "deleted"' in body


def test_the_lookup_is_not_scoped_by_cloud_or_region():
    """An Azure Function App name is a global DNS label, so a same-named function in
    another region collides at apply too. Scoping the lookup would hide it."""
    body = _read(_FNSVC).split("def find_by_names(")[1].split("\ndef ")[0]
    assert "CloudFunction.cloud" not in body
    assert "CloudFunction.region" not in body


def test_an_empty_name_set_costs_no_query():
    """The Databases page calls this on every load, including with no pairable row."""
    from web_dashboard.services import cloud_function_service as fnsvc

    class _Boom:
        def query(self, *a, **k):
            raise AssertionError("queried for an empty name set")

    assert fnsvc.find_by_names(_Boom(), []) == {}
    assert fnsvc.find_by_names(_Boom(), None) == {}


def test_the_endpoint_refuses_a_duplicate_rather_than_redeploying():
    block = _endpoint()
    assert "find_by_names(" in block
    # A 409 naming the function, not a silent second deploy.
    assert "already has the adapter function" in block


# -- Pre-flight ordering -------------------------------------------------------

def test_every_preflight_runs_before_anything_is_queued():
    """A queued-then-failed job is never how an operator should learn the answer."""
    block = _endpoint()
    queued = block.index("start_pairing(")
    for needle in ("_require_function_write(",
                   "cloud_functions_enabled",
                   "adapter_ineligible_reason(row)",
                   'row.status != "available"',
                   "find_by_names(",
                   "deployable_regions(",
                   "terraform_available()",
                   "package_location("):
        assert needle in block, needle
        assert block.index(needle) < queued, needle


def test_deploying_the_adapter_needs_the_cloud_function_scope():
    """Otherwise a holder of cloud_database:write alone has a way around the scope
    that POST /api/functions enforces."""
    guard = _read(_API).split("def _require_function_write(")[1].split("\ndef ")[0]
    assert "cloud_function" in guard
    assert "is_effective_admin" in guard      # admin and NULL-permission users pass
    assert "403" in guard


def test_both_product_flags_are_checked():
    """The endpoint lives on the databases router but deploys a Cloud Function, and
    the two features are switched on independently."""
    block = _endpoint()
    assert "_require_enabled()" in block          # cloud_database_enabled
    assert "cloud_functions_enabled" in block


# -- The region is the database's ----------------------------------------------

def test_the_endpoint_refuses_a_region_with_no_configuration():
    """Without a per-region config set, resolve_region falls every network field back
    to the flat keys and the function comes up on the DEFAULT region's network while
    the row, the job and the operator all say otherwise."""
    block = _endpoint()
    assert "deployable_regions(row.cloud)" in block
    assert "Multi-region" in block


def test_the_region_is_normalised_before_the_membership_test():
    """deployable_regions returns normalised regions; an Azure row can carry
    'East US 2'."""
    block = _endpoint()
    assert "region_catalog.normalize(row.cloud, row.region)" in block


def test_region_overrides_never_shadows_the_default_region():
    """The rule resolve_region documents, now shared rather than restated: a region
    entry cannot shadow the historical flat defaults."""
    from web_dashboard.services import region_config
    CONF.clear()
    CONF["gcp_region"] = "us-central1"
    CONF["gcp_region_configs"] = (
        '{"us-central1": {"subnetwork": "shadow"}, '
        '"us-east1": {"subnetwork": "use1-subnet"}}')
    assert region_config.region_overrides("gcp", "us-central1") == {}
    assert region_config.region_overrides("gcp", "us-east1") == {
        "subnetwork": "use1-subnet"}


def test_region_overrides_is_empty_rather_than_raising_for_a_region_less_cloud():
    """_resolved_network asks about every cloud, including the ones with no
    per-region dimension."""
    from web_dashboard.services import region_config
    CONF.clear()
    for cloud in ("oci", "local", ""):
        assert region_config.region_overrides(cloud, "anywhere") == {}, cloud


def test_region_overrides_is_empty_for_an_unconfigured_region():
    from web_dashboard.services import region_config
    CONF.clear()
    CONF["gcp_region"] = "us-central1"
    assert region_config.region_overrides("gcp", "europe-west1") == {}


def test_resolve_region_reads_the_shared_rule():
    """One implementation, so the two answers cannot drift."""
    body = _read(_RCFG).split("def resolve_region(")[1].split("\ndef ")[0]
    assert "region_overrides(cloud, region)" in body


# -- The button offers exactly what the endpoint accepts -----------------------

def _action_cell():
    page = _read(_PAGE)
    cell = page.split('<div class="flex flex-wrap justify-end items-center gap-1">')[1]
    return cell.split("</td>")[0]


def test_the_button_is_gated_on_the_cloud_functions_flag():
    """The adapter is a Cloud Function; offering it on an install without that
    feature would only ever produce a 409."""
    cell = _action_cell()
    assert "{% if cloud_functions_enabled %}" in cell.split("pairAdapter")[0]
    assert "{% endif %}" in cell.split("pairAdapter")[1]


def test_the_button_is_hidden_rather_than_disabled_for_an_unpairable_row():
    """Same as Register in Password Safe / Register in Entitle on this page."""
    cell = _action_cell()
    show = re.search(r'@click="pairAdapter\(d\)"\s+x-show="([^"]+)"', cell)
    assert show, cell
    expr = show.group(1)
    assert "d.adapter_viable" in expr
    assert "d.status === 'available'" in expr
    assert "!d.adapter_fn_id" in expr      # already paired -> the badge, not the button


def test_the_row_actions_still_wrap_instead_of_running_off_the_card():
    cell = _action_cell()
    assert "whitespace-nowrap" not in cell
    assert "ml-1" not in cell                 # the flex gap does the spacing


def test_the_confirm_names_the_region_and_who_owns_the_approval():
    """The adapter is deployed ready to act, and the copy has to say what that does
    NOT mean: it initiates nothing. It is an HTTP endpoint behind fnruntime.auth's
    fail-closed bearer gate, which only the Entitle integration holds, and Entitle
    owns the approval — an account exists only for the life of an approved request.
    Wording it as "armed, it will create real accounts" invited exactly the wrong
    conclusion, that deploying dry-run would be the safer default. It would not: it
    would make an approved request silently do nothing.
    """
    body = _read(_PAGE).split("async pairAdapter(d)")[1].split("},")[0]
    assert "d.region" in body
    assert "d.cloud" in body
    assert "no accounts by itself" in body
    assert "Entitle owns that" in body
    assert "approved" in body
    # Not a scare-quote about the adapter acting on its own.
    assert "ARMED" not in body


def test_the_handler_posts_to_the_endpoint_and_refreshes():
    body = _read(_PAGE).split("async pairAdapter(d)")[1].split("statusBadge(")[0]
    assert "/adapter-pair" in body
    assert "this.refresh()" in body
    assert "Jobs" in body                  # tells the operator where progress lives


def test_the_armed_choice_survives_the_job_queue():
    """dry_run travels API -> job metadata -> worker -> build_environment, and every
    hop defaults to True. A hop that drops it would leave the button deploying a
    silently no-op adapter — which is the one outcome the confirmation promises it is
    not.
    """
    assert "dry_run=False" in _endpoint()

    pairing = _read(os.path.join(_ROOT, "web_dashboard", "services",
                                 "cloud_db_adapter_service.py"))
    start = pairing.split("def start_pairing(")[1].split("async def ")[0]
    assert '"dry_run": bool(dry_run)' in start, "start_pairing drops dry_run"

    worker = _read(os.path.join(_ROOT, "web_dashboard", "jobs_worker.py"))
    dispatch = worker.split('job_type == "clouddb_adapter_pair"')[1][:600]
    assert 'dry_run=meta.get("dry_run", True)' in dispatch, \
        "the worker does not forward the queued dry_run choice"


def test_every_key_the_button_reads_is_one_the_row_projection_declares():
    """_serialize is pure and takes no Session, so adapter_fn_id/adapter_status are
    declared there and filled in by list_databases. A key only list_databases supplies
    would read as undefined on a page served any other way."""
    svc = _read(os.path.join(_ROOT, "web_dashboard", "services",
                             "cloud_database_service.py"))
    projection = svc.split("def _serialize(")[1].split("\ndef ")[0]
    cell = _action_cell()
    for key in sorted(set(re.findall(r"\bd\.(adapter_[a-z_]+)", cell))):
        assert f'"{key}"' in projection, key


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
