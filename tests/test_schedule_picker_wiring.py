"""A page that shows the change-window picker must actually send the booking.

The picker is three pieces and they are in three different places:

  1. ``{{ schedule_picker() }}``          the markup            (template)
  2. ``...scheduleState()``               the Alpine state      (template)
  3. ``...this.schedulePayload()``        the request body      (template)
  4. ``change_window_service.schedule_kwargs``  the server side (router)

Miss the third and the operator gets a working-looking control that books nothing:
they pick Saturday 02:00, press Deploy, and it deploys immediately. Nothing errors,
nothing logs, and the page looks correct — the failure is only visible by noticing
that a job which should be `scheduled` is `pending`.

That is not hypothetical. The Config Management page had FOUR post sites for one form
and three of them would have been missed by hand; the bulk endpoint's server-side
field copy dropped the same three fields for the same reason. So this pins the chain
structurally rather than trusting each page to be wired by eye.

Parses with ``ast``/regex rather than importing, so it runs with nothing installed.

Run: python tests/test_schedule_picker_wiring.py   (or under pytest)
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TPL = os.path.join(_ROOT, "web_dashboard", "templates")
_API = os.path.join(_ROOT, "web_dashboard", "api")

_PICKER_IMPORT = "partials/schedule_picker.html"
_PICKER_CALL = re.compile(r"\{\{\s*schedule_picker\(")

# Routers for which the RAW `resolve()` is the right call, not `schedule_kwargs`.
#
# `api/jobs.py` is the reschedule endpoint, and the difference is not stylistic. It
# ASSIGNS to an existing job's columns rather than passing kwargs to `create_job`, so
# it needs the triple — and "release this to run now" must SET `scheduled_for = None`,
# which is the exact opposite of `schedule_kwargs` omitting the key. Routed through
# the helper, releasing a booked change would silently leave it booked.
_RESOLVE_IS_CORRECT = {"api/jobs.py"}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _templates():
    for root, _dirs, files in os.walk(_TPL):
        for name in files:
            if name.endswith(".html"):
                full = os.path.join(root, name)
                yield os.path.relpath(full, _TPL).replace(os.sep, "/"), full


def _pages_with_picker():
    """Templates that RENDER the picker (importing the macro is not enough)."""
    out = {}
    for rel, full in _templates():
        if rel.startswith("partials/"):
            continue
        src = _read(full)
        if _PICKER_CALL.search(src):
            out[rel] = src
    return out


# ── The chain, per page ───────────────────────────────────────────────────────

def test_the_picker_is_rendered_somewhere():
    """Guards the guard: a bug in the detection above would make every test below
    pass by finding nothing to check."""
    pages = _pages_with_picker()
    assert len(pages) >= 4, (
        f"only {len(pages)} page(s) render the schedule picker — the detector is "
        f"probably broken, since the rollout covers the cloud pages and "
        f"Config Management")


def test_every_page_with_the_picker_imports_the_macro():
    bad = [rel for rel, src in _pages_with_picker().items()
           if _PICKER_IMPORT not in src]
    assert not bad, ("these render schedule_picker() without importing it, so the "
                     "page 500s on render:\n  " + "\n  ".join(bad))


def test_every_page_with_the_picker_spreads_the_state():
    """Without `...scheduleState()` the control renders and every expression in it
    is undefined, which in Alpine means the whole component fails to initialise —
    taking the rest of the page with it."""
    bad = [rel for rel, src in _pages_with_picker().items()
           if "...scheduleState()" not in src]
    assert not bad, ("these render the picker but never spread scheduleState():\n  "
                     + "\n  ".join(bad))


def test_every_page_with_the_picker_sends_the_booking():
    """THE one this file exists for.

    A page that renders the picker must spread `schedulePayload()` into at least one
    request body. This cannot check that EVERY post site on a page includes it —
    some pages post several unrelated things — but a page with none is
    unambiguously broken, and that is the failure that ships silently.
    """
    # Matches the SPREAD, not the bare name: every one of these pages carries a
    # comment pointing at `schedulePayload()`, so a substring check on the name
    # passes even after the real call is deleted. (It did, until the negative
    # control caught it — the same self-trip as quoting a pattern a grep-based
    # check looks for.)
    spread = re.compile(r"\.\.\.\s*this\.schedulePayload\s*\(\)")
    bad = [rel for rel, src in _pages_with_picker().items()
           if not spread.search(src)]
    assert not bad, (
        "these render the change-window picker and never send the result, so an "
        "operator can pick a window and the job still runs immediately:\n  "
        + "\n  ".join(bad))


def test_every_page_with_the_picker_loads_the_window_list():
    """Without `loadChangeWindows()` the "Change window" mode shows an empty select
    and the "no windows are defined" hint, on an install that has plenty."""
    bad = [rel for rel, src in _pages_with_picker().items()
           if "loadChangeWindows()" not in src]
    assert not bad, ("these render the picker but never populate it:\n  "
                     + "\n  ".join(bad))


def test_every_page_with_the_picker_gates_its_submit():
    """`scheduleReady()` in the submit button's :disabled. Without it, choosing
    "At a time" and leaving the time blank posts three empty strings — which the
    server reads as "run now", so the form silently does the opposite of what the
    operator selected."""
    bad = [rel for rel, src in _pages_with_picker().items()
           if "scheduleReady()" not in src]
    assert not bad, ("these render the picker without gating submit on "
                     "scheduleReady():\n  " + "\n  ".join(bad))


# ── The server half ───────────────────────────────────────────────────────────

def test_the_shared_helper_is_what_routers_call():
    """Routers must go through `change_window_service.schedule_kwargs`, not
    re-resolve a window themselves.

    `resolve()` returns a triple and raises ScheduleError; every caller that used it
    directly would need the same three-line translation into a 400 and the same
    "empty dict for an immediate run" rule. One of them would get it wrong, and the
    way it would be wrong is a dict of Nones — which changes the create_job call for
    UNSCHEDULED jobs on that page.
    """
    offenders = []
    for name in sorted(os.listdir(_API)):
        if not name.endswith(".py"):
            continue
        if f"api/{name}" in _RESOLVE_IS_CORRECT:
            continue
        src = _read(os.path.join(_API, name))
        if "change_window_service.resolve(" in src:
            offenders.append(f"api/{name}")
    assert not offenders, (
        "these call change_window_service.resolve() directly instead of "
        "schedule_kwargs():\n  " + "\n  ".join(offenders))


def test_a_persisted_request_body_excludes_the_booking():
    """A router that stores a whole request into job metadata must exclude the three
    scheduling fields.

    They are queue state, not job parameters. Left in, a payload replayed by a
    recurring schedule would carry the booking that produced it — a job reading as
    scheduled for a time in the past, forever — and `models/schedule.py` exists to
    make that exclusion one named constant rather than three string literals.
    """
    offenders = []
    for name in sorted(os.listdir(_API)):
        if not name.endswith(".py"):
            continue
        src = _read(os.path.join(_API, name))
        if "schedule_kwargs(" not in src:
            continue
        for m in re.finditer(r'"req":\s*\w+\.model_dump\(([^)]*)\)', src):
            if "SCHEDULE_FIELDS" not in m.group(1):
                line = src[:m.start()].count("\n") + 1
                offenders.append(f"api/{name}:{line}")
    assert not offenders, (
        "these persist a request body that still carries its change-window "
        "booking:\n  " + "\n  ".join(offenders))


# ── Destroys ─────────────────────────────────────────────────────────────────

def test_every_cloud_destroy_accepts_a_booking():
    """A teardown is the operation a change window most needs to cover.

    `prod_window.rego` already makes that argument — it is the one guardrail policy
    that deliberately applies to teardowns, because "no changes on a Sunday" which let
    the destroys through would be half a freeze. And because destroys are gated
    actions, a workgroup with a required window REFUSES one; without a way to book it,
    that refusal is a dead end with no form that can act on it.

    All four are worker-claimed, so a booking needs no new machinery — only the three
    parameters. They are query parameters rather than a body because these are DELETE
    routes.
    """
    import ast

    # The ENDPOINT per router, named rather than discovered by job type. Azure has two
    # functions creating `azure_destroy` — the route and a fallback helper for VMs with
    # no deploy job — and only the route takes request parameters. The helper is
    # checked separately below.
    endpoints = {
        "aws.py": "destroy_instance",
        "azure.py": "destroy_vm",
        "gcp.py": "destroy_instance",
        "oci.py": "destroy_instance",
    }
    missing = []
    for name, fn_name in endpoints.items():
        src = _read(os.path.join(_API, name))
        fns = [n for n in ast.walk(ast.parse(src))
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == fn_name]
        if not fns:
            missing.append(f"{name}: no {fn_name}")
            continue
        fn = fns[0]
        params = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
        for needed in ("run_at", "run_timezone", "change_window_id"):
            if needed not in params:
                missing.append(f"{name}:{fn_name} has no {needed}")
        body = ast.unparse(fn)
        if "schedule_kwargs(" not in body:
            missing.append(f"{name}:{fn_name} never resolves the booking")
        if "scheduled=_sched" not in body:
            missing.append(
                f"{name}:{fn_name} does not tell the admission gate about the booking, "
                f"so a workgroup-constrained destroy cannot accept its own offer")
    assert not missing, "\n  " + "\n  ".join(missing)


def test_the_azure_fallback_destroy_is_bookable_too():
    """`_destroy_without_deploy_job` handles VMs with no deploy job — VDI seats and
    cloud-recovered ones — and `destroy_vm` returns through it EARLY, before the
    admission gate. So it needs the booking handed to it explicitly, or a scheduled
    destroy of one of those VMs would silently run immediately."""
    import ast

    src = _read(os.path.join(_API, "azure.py"))
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_destroy_without_deploy_job")
    params = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    assert "sched" in params, (
        "the fallback destroy takes no booking, so a scheduled destroy of a VDI seat "
        "or a recovered VM would run immediately")
    assert "sched" in ast.unparse(fn), "the booking is accepted and then ignored"


def test_the_refusal_offer_can_be_acted_on_in_the_ui():
    """`bookInstead` is what turns a change-window refusal into one click.

    Without it the operator reads "the next window opens Saturday 02:00" on a destroy
    and has nowhere to go: there is no destroy form to re-submit from.
    """
    app_js = _read(os.path.join(_ROOT, "web_dashboard", "static", "js", "app.js"))
    assert "window.bookInstead" in app_js, "the shared offer handler is gone"

    missing = []
    for page in ("aws", "azure", "oci"):
        src = _read(os.path.join(_ROOT, "web_dashboard", "templates", page, "index.html"))
        if "bookInstead(" not in src:
            missing.append(page)
    assert not missing, (
        "these pages can refuse a destroy but never offer to book it: "
        + ", ".join(missing))


def _run():
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
