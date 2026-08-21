"""The Cloud Functions deploy form and the required-settings refusal, in agreement.

``cloud_function_service._check_required_env`` refuses a deploy that omits a
workload's ``REQUIRED_ENV`` outright — the adapters read their target from the
environment, so one deployed without it is inert for the life of the function and
finding out costs a real cloud build. That refusal is right; the form around it was
not:

  * The amber warning offered a way out that does not exist — "or let the pairing
    job fill them in for you". No job ever fills in a hand-deployed function. The
    automatic path (``cloud_db_adapter_service.run_pairing``) deploys its **own**
    adapter under ``adapter_name(row)``, a name derived from the database id, and
    passes ``build_environment(...)`` at deploy time. A function deployed from this
    form with a blank Environment is not "pending pairing", it is refused — and if it
    somehow were not, it would sit there inert forever.
  * Missing settings were the one precondition the button did not carry. Cloud
    (``cloudBlocked()``) and Region (``!form.region``) both disable Deploy; a missing
    ``FN_DB_HOST`` posted, round-tripped, and came back as an error toast.

So two things are pinned here: the copy does not promise deferral, and the form
refuses locally on the same rule the service refuses on — blank counts as absent, and
``A|B`` is satisfied by either.

The JS is checked by reading the template, the repo's idiom for form wiring (see
test_cloud_function_region_picker.py) — there is no JS runtime in CI.

Run: python tests/test_cloud_function_required_env_form.py   (or under pytest)
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def _template():
    return open(os.path.join(_ROOT, "web_dashboard", "templates", "functions",
                             "index.html"), encoding="utf-8").read()


def _modal():
    src = _template()
    return src[src.index("<!-- Deploy modal -->"):src.index("<!-- Invoke modal -->")]


def _script():
    src = _template()
    return src[src.index("function functionsPage()"):]


# ── the copy does not offer a way out that does not exist ─────────────────────

def test_the_warning_does_not_promise_a_later_job_will_fill_these_in():
    """The reported bug: the box said the pairing job could do it for you, and then
    the deploy was refused for not having done it yourself."""
    warning = _modal()
    assert "fill them in for you" not in warning, (
        "the warning still offers to defer the required settings to a job — nothing "
        "fills in a hand-deployed function, and the deploy is refused without them")
    assert "required here" in warning, (
        "the warning does not say the settings are required on this form")


def test_the_warning_still_points_at_the_automatic_path():
    """Deleting the misleading clause must not delete the real alternative: the
    operator who wants this done for them should skip the form, not fight it."""
    warning = _modal()
    assert "Register in Entitle" in warning, (
        "the warning no longer names the automatic pairing path")
    assert "own adapter" in warning, (
        "the warning does not say the pairing deploys its OWN adapter — the reason "
        "it cannot finish one started here")


def test_the_pairing_path_really_does_deploy_its_own_function():
    """What makes the new copy true rather than differently wrong: the pairing job
    names its function from the database id and passes the environment itself, so it
    can never be the second half of a deploy started on this form."""
    try:
        from web_dashboard.services import cloud_db_adapter_service as pairing
    except Exception as exc:  # pragma: no cover — deps absent outside CI
        try:
            import pytest
        except ModuleNotFoundError:
            print(f"  (skipped: {exc})")
            return
        pytest.skip(f"service import unavailable: {exc}")
    source = open(pairing.__file__, encoding="utf-8").read()
    run = source[source.index("async def run_pairing"):]
    assert "name=adapter_name(row)" in run, (
        "run_pairing no longer names its own function — if it adopted one instead, "
        "the form's copy about deferral would need revisiting")
    assert "environment=environment" in run, (
        "run_pairing no longer passes the environment at deploy time")


# ── the form refuses on the same rule the service refuses on ─────────────────

def test_the_deploy_button_carries_the_missing_settings_precondition():
    modal = _modal()
    button = modal[modal.index("submitDeploy()"):]
    assert "missingEnv().length" in button[:300], (
        "the Deploy button does not guard against missing required settings — the one "
        "precondition you would still discover from an error toast")
    # The two that were already there must survive alongside it.
    assert "cloudBlocked()" in button[:300], "the cloud guard was dropped"
    assert "!form.region" in button[:300], "the region guard was dropped"


def test_the_form_says_which_settings_are_still_missing():
    assert "missingEnv().map(" in _modal(), (
        "nothing names the still-missing settings next to the field — a disabled "
        "button with no reason is worse than the toast it replaced")


def test_the_client_check_mirrors_the_service_rule():
    """Both halves of ``_check_required_env``'s semantics, or the button disagrees
    with the refusal and one of them is a lie."""
    script = _script()
    body = script[script.index("missingEnv()"):]
    body = body[:body.index("\n    },")]
    assert "split('|')" in body, (
        "the client check does not handle alternation — FN_DB_NAMES alone would leave "
        "Deploy disabled on a deploy the service accepts")
    assert re.search(r"String\(env\[\w+\] *\|\| *''\)\.trim\(\)", body), (
        "the client check does not treat a blank value as absent — 'FN_DB_HOST=' "
        "would enable a Deploy the service refuses")
    assert "this.requiredEnv()" in body, (
        "the client check does not read the workload's declared requirements, so it "
        "would need its own hand-maintained list")


def test_the_requirements_still_come_from_the_workload_declaration():
    """Neither the warning, the field, nor the guard may carry its own list: the
    catalog serves ``required_env`` from the module's ``REQUIRED_ENV``."""
    script = _script()
    assert "required_env" in script, "the page no longer reads the catalog's required_env"
    assert "FN_DB_ENGINE" not in script, (
        "a required setting is hard-coded in the page JS again — it would drift from "
        "the workload module that declares it")


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
