"""The Cloud Functions deploy form's region picker, and the region it commits to.

The "Deploy a function" modal used to take the region as **free text**, seeded once
from the first package-store-ready cloud (normally AWS, so ``us-east-2``) and never
touched again — the Cloud ``<select>`` carried no change handler. Switching Cloud to
azure therefore left an AWS region in the box, and nothing downstream caught it:
``deploy()`` only checked the string was non-empty before writing it into the
``CloudFunction`` row and into the Terraform vars.

Two distinct failures, which is why there are two halves here:

  * a region belonging to the *wrong cloud* dies at ``terraform apply`` — minutes
    later, and a long way from the form that caused it; and
  * a well-formed region with **no per-region config set** is worse than an error.
    Every regional id a vpc-mode function needs resolves through
    ``region_config.resolve_region``, which falls each field back to the flat config
    keys — so the function comes up attached to the **default region's** subnet while
    the row, the job and the operator all say otherwise.

So three things are pinned:

  * ``api/cloud_functions.deploy_options`` serves ``configured_regions``
    (``region_config.deployable_regions``) alongside the full catalog — the picker
    offers the former, the Multi-region editor configures the latter.
  * ``cloud_function_service.deploy`` canonicalises and validates the region before it
    resolves the network or writes a row, mirroring
    ``api/cloud_databases._resolve_db_region``. A blank region keeps its own error, and
    a well-formed region we simply do not enumerate is still accepted — this is a
    format guard, not an allow-list.
  * the template binds Region to that picker rather than to a text box, and cannot
    submit a blank one.

Config-reading collaborators are replaced with spies (the house pattern from
test_gateway_region_picker.py), so no DB or cloud SDK is needed. That matters more than
usual here: ``config_service._load_cache`` opens a real session and does not swallow
the failure, so an unpatched config read is an error rather than a miss.

Run: python tests/test_cloud_function_region_picker.py   (or under pytest)
"""
import contextlib
import inspect
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

try:
    from web_dashboard.api import cloud_functions
    from web_dashboard.services import cloud_function_service as cfs
    from web_dashboard.services import region_catalog, region_config
    from web_dashboard.services.cloud_function_service import CloudFunctionError
except Exception as exc:  # pragma: no cover — deps absent outside CI
    try:
        import pytest
        pytest.skip(f"service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


@contextlib.contextmanager
def _patched(module, **attrs):
    """Swap module attributes for the duration of a test, then put them back."""
    saved = {k: getattr(module, k) for k in attrs}
    for k, v in attrs.items():
        setattr(module, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(module, k, v)


# The per-cloud defaults and config sets the tests resolve against: one cloud with a
# second configured region, one with none.
_DEFAULTS = {"aws": "us-east-2", "azure": "centralus", "gcp": "us-central1"}
_SETS = {
    "aws": {"eu-west-1": {"vpc_id": "vpc-e"}},
    "azure": {"westus2": {"resource_group": "rg-w"}},
    "gcp": {},
}


class _Cfg:
    """``config_service`` stand-in for ``_require_enabled`` — the flag is on."""
    get_bool = staticmethod(lambda key, default=False: True)


def _options(defaults=None, sets=None):
    """``deploy_options`` with the feature flag on and the package-store probe stubbed
    (it would otherwise reach for cloud credentials). ``deployable_regions`` itself is
    NOT stubbed — the point is to pin what the real one offers."""
    defaults = defaults or _DEFAULTS
    sets = sets if sets is not None else _SETS
    with _patched(cloud_functions, config_service=_Cfg), \
         _patched(cfs, package_location=lambda *a, **k: {"uri": "s3://b/k"},
                  terraform_available=lambda: True), \
         _patched(region_catalog, default_region=lambda c: defaults[c]), \
         _patched(region_config, load_region_configs=lambda c: dict(sets.get(c, {}))):
        return {c["cloud"]: c
                for c in cloud_functions.deploy_options(user=None)["clouds"]}


class _Stop(Exception):
    """Sentinel: stop ``deploy`` at the network step, before it can touch a DB."""


def _stop_at_network(seen):
    """A ``_resolved_network`` stand-in that records the region it was handed."""
    def spy(cloud, region, **kwargs):
        seen.append(region)
        raise _Stop
    return spy


def _deploy(**kwargs):
    """``deploy`` with the arguments every test shares. ``db`` is None on purpose —
    nothing may reach it before the region is settled."""
    return cfs.deploy(None, name="broker", workload="echo_diag",
                      created_by="tester", **kwargs)


# ── what the deploy form is offered ───────────────────────────────────────────

def test_options_offers_the_configured_regions_not_the_whole_catalog():
    """The picker's list is ``deployable_regions``: the configured default first, then
    every region with a config set. Offering the catalog here is the silent-placement
    bug — those regions have no subnet of their own."""
    opts = _options()
    assert opts["aws"]["configured_regions"] == ["us-east-2", "eu-west-1"]
    assert opts["azure"]["configured_regions"] == ["centralus", "westus2"]
    assert opts["gcp"]["configured_regions"] == ["us-central1"]


def test_options_still_carries_the_full_catalog_for_labels():
    """``regions`` stays: it is what the Multi-region editor may configure, and what
    the picker joins against for display labels. It must be the LARGER list."""
    for cloud, entry in _options().items():
        assert len(entry["regions"]) > len(entry["configured_regions"]), cloud
        assert all("id" in r and "label" in r for r in entry["regions"]), cloud


def test_options_answers_for_every_cloud_functions_run_on():
    """A cloud missing from the payload leaves the picker empty for it, which reads as
    "no regions configured" rather than as a bug in this endpoint."""
    assert sorted(_options()) == sorted(cfs.VALID_CLOUDS)


def test_options_never_offers_an_empty_region_list():
    """No ``<cloud>_region_configs`` anywhere — a single-region install still gets the
    one region it deploys to today. The picker relies on this: ``deploy()`` refuses a
    blank region, so an empty list would be a form that cannot be submitted."""
    opts = _options(sets={})
    for cloud, entry in opts.items():
        assert entry["configured_regions"] == [_DEFAULTS[cloud]], cloud


def test_options_normalises_what_it_offers_but_default_region_is_raw():
    """Why the form must seed from ``configured_regions[0]`` and never from
    ``default_region``: the latter is the raw config value, so an operator who typed
    "West US 2" into ``azure_location`` would have had the form post that back as a
    region id. ``deployable_regions`` normalises; ``default_region`` does not."""
    opts = _options(defaults={**_DEFAULTS, "azure": "West US 2"}, sets={})
    assert opts["azure"]["configured_regions"] == ["westus2"]
    assert opts["azure"]["default_region"] == "West US 2"


# ── what the deploy path commits to ───────────────────────────────────────────

def test_deploy_refuses_a_region_from_another_cloud():
    """The reported bug, end of the line: the modal showed Cloud=azure with the AWS
    default still in the box. It has to fail here, not at apply."""
    with _patched(cfs, available_workloads=lambda: ("echo_diag",)):
        for cloud, region in (("azure", "us-east-2"), ("aws", "centralus"),
                              ("gcp", "us-east-2"), ("aws", "us-central1")):
            try:
                _deploy(cloud=cloud, region=region)
            except CloudFunctionError as exc:
                assert region in str(exc), (cloud, region, str(exc))
            else:
                raise AssertionError(f"{cloud} accepted {region!r}")


def test_deploy_normalises_before_it_resolves_the_network():
    """Azure regions are stored space-free and lowercase. The normalised string has to
    reach ``_resolved_network`` too, or the per-region config lookup misses its entry
    and the function lands on the default region's subnet."""
    seen = []
    with _patched(cfs, available_workloads=lambda: ("echo_diag",),
                  _resolved_network=_stop_at_network(seen)):
        try:
            _deploy(cloud="azure", region="West US 2")
        except _Stop:
            pass
    assert seen == ["westus2"], seen


def test_deploy_still_requires_a_region():
    """Deliberately unlike ``_resolve_db_region``, which resolves blank to the
    configured default: the picker always sends a concrete region, so a blank one means
    a broken caller and should say so."""
    with _patched(cfs, available_workloads=lambda: ("echo_diag",)):
        for blank in ("", None):
            try:
                _deploy(cloud="aws", region=blank)
            except CloudFunctionError as exc:
                assert "region is required" in str(exc), str(exc)
            else:
                raise AssertionError(f"{blank!r} was accepted as a region")


def test_deploy_accepts_a_well_formed_region_we_do_not_enumerate():
    """The catalog is a convenience list, not an allow-list — operators run regions we
    have not enumerated, and an API caller must still be able to deploy there."""
    for cloud, region in (("aws", "ap-southeast-4"), ("aws", "us-gov-west-1"),
                          ("gcp", "me-west1"), ("azure", "polandcentral")):
        assert region not in region_catalog.region_ids(cloud), region
        seen = []
        with _patched(cfs, available_workloads=lambda: ("echo_diag",),
                      _resolved_network=_stop_at_network(seen)):
            try:
                _deploy(cloud=cloud, region=region)
            except _Stop:
                pass
        assert seen == [region], (cloud, region, seen)


def test_the_region_guard_runs_before_anything_is_written():
    """Ordering is the whole point: the row, the bearer secret (staged into Key Vault
    on Azure) and the uploaded package all exist by the time Terraform sees the region,
    so a bad one has to fail while there is still nothing to clean up."""
    src = inspect.getsource(cfs.deploy)
    guard = src.index("region_catalog.resolve(cloud, region)")
    assert guard < src.index("_resolved_network("), "the guard runs after the network"
    assert guard < src.index("db.add(row)"), "the guard runs after the row is written"


# ── the form is actually wired to the picker ──────────────────────────────────

def _template():
    return open(os.path.join(_ROOT, "web_dashboard", "templates", "functions",
                             "index.html"), encoding="utf-8").read()


def _modal():
    src = _template()
    return src[src.index("<!-- Deploy modal -->"):src.index("<!-- Invoke modal -->")]


def test_the_form_binds_the_region_to_a_picker_not_a_text_box():
    form = _modal()
    assert 'x-model="form.region"' in form, "the deploy form lost its region binding"
    assert '<input x-model="form.region"' not in form, (
        "the region is free text again — a region with no config set puts the function "
        "on the default region's network")
    assert 'x-for="r in regionChoices()"' in form, (
        "the region select is not fed by regionChoices()")


def test_the_cloud_select_moves_the_region_with_it():
    """This is the reported bug: no change handler on the Cloud select, so azure kept
    showing us-east-2."""
    assert '@change="cloudChanged()"' in _modal(), (
        "the cloud select has no change handler — the region would keep the previous "
        "cloud's value")


def test_the_picker_reads_the_configured_regions():
    src = _template()
    assert "configured_regions" in src, (
        "the picker reads something other than the configured region sets — the full "
        "catalog would offer regions with no subnet")
    assert "syncRegion()" in src, "nothing keeps form.region inside the offered list"


def test_the_picker_has_no_blank_option():
    """The gateway form's ``(configured default)`` escape hatch must NOT be copied
    here: ``deploy()`` refuses a blank region, so it would be an option that always
    fails."""
    assert '<option value=""' not in _modal(), (
        "a blank region option is a guaranteed 'region is required'")


def test_the_form_cannot_submit_a_blank_region():
    """With free text gone there is no typing your way out: if /api/functions/options
    fails, ``this.clouds`` is empty, ``cloudBlocked()`` returns '' and the button would
    otherwise still post an empty region."""
    button = _modal()
    start = button.index("submitDeploy()")
    assert "!form.region" in button[start:start + 200], (
        "the Deploy button does not guard against an empty region picker")


def test_a_cloud_restricted_workload_moves_the_cloud_too():
    """``availableClouds()`` filters the Cloud options by workload, so picking an
    AWS-only workload while Cloud=azure would leave the Cloud select rendering blank
    next to a list of azure regions."""
    assert cfs.clouds_for("local_account_broker") == ("aws",), (
        "no cloud-restricted workload left — this test guards their interaction with "
        "the region picker")
    assert '@change="workloadChanged()"' in _modal(), (
        "the workload select has no change handler")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
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
