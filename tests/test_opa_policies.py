"""The shipped Rego actually parses and decides, under the real OPA.

Every other admission test stubs `_opa` — it replaces the module in `sys.modules` and
hard-codes `opa_available`. That is right for testing the DECISION logic (precedence,
fail-closed, the limits document) because none of it needs a subprocess. It also means
nothing in the tree has ever run the policies themselves.

The gap matters because of how this feature fails. `enforce` is **fail-closed**: any
OPA error denies the action. So a `.rego` with a syntax error, a renamed package, or a
field the Python side stopped sending does not degrade gracefully — it turns every
gated deploy into a 403, and the first person to find out is an operator whose estate
has stopped deploying. There is no unit test that can catch it, because the failure is
in the interaction between two languages and a subprocess.

So this file evaluates the real files in `terraform/policy/admission/` against the real
binary, asserting each rule both fires and stays quiet on the right input.

**Skips unless OPA is on PATH**, so it costs nothing locally and in the SQLite job. CI
installs it — see the `opa` job in .github/workflows/tests.yml, pinned to the same
version the Dockerfile ships.

    OPA_BINARY=/path/to/opa python tests/test_opa_policies.py

Run: python tests/test_opa_policies.py   (or under pytest)
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-opa-policies")


def _skip(reason):
    try:
        import pytest
        pytest.skip(reason, allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {reason}")
        sys.exit(0)


# Probe the optional third-party deps by NAME, then import the first-party modules
# unguarded — the shape tests/test_import_guard_narrowness.py requires. A `try` around
# the first-party import would swallow a genuinely broken `_opa` or `admission_service`
# as if it were a missing package, and this file would then exit 0 having tested
# nothing: the exact silent no-op that gate was written for. `fastapi` is
# admission_service's own import; `cryptography` is config_service's.
try:
    import cryptography  # noqa: F401
    import fastapi  # noqa: F401
except ModuleNotFoundError as exc:
    _skip(f"optional dependency missing: {exc.name}")

from web_dashboard.services import _opa  # noqa: E402

if not _opa.opa_available():
    # `OPA_REQUIRED=1` turns the skip into a failure. The CI job sets it, because a
    # skip-on-missing test is worth nothing in the one place it is supposed to run: if
    # the install step ever half-fails, this file would skip, the job would stay green,
    # and the blind spot this PR closes would quietly reopen.
    if os.environ.get("OPA_REQUIRED") == "1":
        raise SystemExit(
            "OPA_REQUIRED=1 but no opa binary was found. The CI job is meant to have "
            f"installed one (OPA_BINARY={_opa.OPA_BIN!r}) — check the install step "
            "rather than relaxing this."
        )
    _skip("no opa binary on PATH (set OPA_BINARY, or see the `opa` CI job) — "
          "this file only tests what the real engine does")

from web_dashboard.services import admission_service as adm  # noqa: E402

POLICY_DIR = adm.ADMISSION_POLICY_DIR

#: A request that no shipped rule objects to. Each test perturbs one thing.
CLEAN = {
    "actor": {"username": "operator", "is_admin": False},
    "request": {"region": "us-east-1", "instance_type": "t3.micro",
                "name": "web-01", "count": 1, "batch": False},
    "limits": {"allowed_regions": [], "denied_instance_types": [], "prod_window": []},
    "now": {"iso": "2026-09-24T12:00:00", "weekday": "thu", "hour": 12},
}


def _ctx(**over):
    """CLEAN with a deep-ish override of `request` / `limits` / `now`."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CLEAN.items()}
    for key, patch in over.items():
        out[key] = {**out[key], **patch}
    return out


def _decide(action="aws:ec2:deploy", **over):
    return adm.evaluate(action, _ctx(**over), policy_dir=POLICY_DIR)


#: rule_id → an input that rule must object to. Keyed by the .rego FILENAME, which is
#: what `list_rules` reports; the value proves the PACKAGE inside answers to that same
#: name. See `test_every_shipped_policy_is_reachable` for why both halves matter.
PROVOCATIONS = {
    "allowed_regions": dict(
        request={"region": "eu-west-1"}, limits={"allowed_regions": ["us-east-1"]}),
    "instance_size_caps": dict(
        request={"instance_type": "x1e.32xlarge"},
        limits={"denied_instance_types": ["x1e.32xlarge"]}),
    "prod_window": dict(
        limits={"prod_window": ["sat"]}, now={"weekday": "sat"}),
}


# ── The engine and the files agree ───────────────────────────────────────────

def test_every_shipped_policy_is_reachable():
    """Each .rego on disk actually decides something, under the name it is listed by.

    Two failures hide here, and neither shows up anywhere else in the tree:

    **A file that doesn't compile.** OPA loads the directory as one bundle, so a single
    syntax error fails every query — and `enforce` is fail-closed, so that is a 403 on
    every gated action, not a degraded rule.

    **A file whose package doesn't match its filename.** `_opa.list_packages` reports
    *filenames*; a decision only ever names a *package*. The convention that they match
    is load-bearing and entirely unchecked, so renaming the package inside a file (or
    fixing a typo in it) leaves the rule listed in Settings as active while it silently
    never evaluates again. A guardrail that has quietly stopped guarding is worse than
    one that was never enabled.

    Asserting the table covers the directory also means a new policy cannot ship
    without a test: adding the file fails here until it is listed.
    """
    on_disk = set(adm.list_rules(POLICY_DIR))
    assert on_disk, "no .rego files found — the policy directory moved?"
    assert on_disk == set(PROVOCATIONS), (
        "policy files and this test's table have diverged — a new .rego needs a "
        f"provocation here: on disk {sorted(on_disk)}, tested {sorted(PROVOCATIONS)}")

    for rule_id, provocation in sorted(PROVOCATIONS.items()):
        result = _decide(**provocation)
        assert rule_id in result["rules"], (
            f"{rule_id}.rego is on disk but never decided anything — check that it "
            f"declares `package admission.{rule_id}`: {result}")


def test_a_clean_request_is_allowed():
    """The baseline every other case is a perturbation of. If this denies, the tests
    below would pass for the wrong reason."""
    assert _decide()["decision"] == "allow"


# ── allowed_regions ──────────────────────────────────────────────────────────

def test_a_region_outside_the_allow_list_is_denied():
    r = _decide(request={"region": "eu-west-1"},
                limits={"allowed_regions": ["us-east-1"]})
    assert r["decision"] == "deny", r
    assert "allowed_regions" in r["rules"], r
    assert "eu-west-1" in r["reasons"][0], r["reasons"]


def test_a_region_inside_the_allow_list_is_allowed():
    assert _decide(request={"region": "us-east-1"},
                   limits={"allowed_regions": ["us-east-1"]})["decision"] == "allow"


def test_an_empty_allow_list_is_inert():
    """Blank config must not mean "allow nothing" — that would deny every deploy the
    moment an operator enabled the gate without filling anything in."""
    assert _decide(request={"region": "eu-west-1"},
                   limits={"allowed_regions": []})["decision"] == "allow"


def test_the_region_rule_exempts_teardowns():
    """Documented behaviour: a region cap is about where you may CREATE things. It
    must not strand an existing resource in a now-disallowed region."""
    r = _decide(action="aws:ec2:destroy", request={"region": "eu-west-1"},
                limits={"allowed_regions": ["us-east-1"]})
    assert r["decision"] == "allow", r


# ── instance_size_caps ───────────────────────────────────────────────────────

def test_a_denied_instance_type_is_denied():
    r = _decide(request={"instance_type": "x1e.32xlarge"},
                limits={"denied_instance_types": ["x1e.32xlarge"]})
    assert r["decision"] == "deny", r
    assert "instance_size_caps" in r["rules"], r


def test_an_allowed_instance_type_passes():
    assert _decide(request={"instance_type": "t3.micro"},
                   limits={"denied_instance_types": ["x1e.32xlarge"]}
                   )["decision"] == "allow"


def test_the_size_rule_exempts_teardowns():
    r = _decide(action="aws:ec2:destroy", request={"instance_type": "x1e.32xlarge"},
                limits={"denied_instance_types": ["x1e.32xlarge"]})
    assert r["decision"] == "allow", r


# ── prod_window (the change freeze) ──────────────────────────────────────────

def test_a_frozen_weekday_is_denied():
    r = _decide(limits={"prod_window": ["sat", "sun"]},
                now={"weekday": "sat"})
    assert r["decision"] == "deny", r
    assert "prod_window" in r["rules"], r


def test_an_unfrozen_weekday_passes():
    assert _decide(limits={"prod_window": ["sat", "sun"]},
                   now={"weekday": "wed"})["decision"] == "allow"


def test_the_freeze_applies_to_teardowns_too():
    """The one rule that deliberately does NOT exempt teardowns — a freeze that let
    the destroys through would be half a freeze. Its own comment says so, and this is
    what keeps that true."""
    r = _decide(action="aws:ec2:destroy", limits={"prod_window": ["sat"]},
                now={"weekday": "sat"})
    assert r["decision"] == "deny", r
    assert "prod_window" in r["rules"], r


# ── The contract between Python and Rego ─────────────────────────────────────

def test_the_weekday_python_sends_is_the_shape_rego_matches():
    """`_now_doc` computes the weekday in Python so the Rego needs no date math. The
    two agree only by convention — lowercase `mon`..`sun` — and nothing else checks it.

    Built from the real function rather than a literal, so a change to either side
    fails here instead of silently never freezing.
    """
    from datetime import datetime

    saturday = datetime(2026, 9, 26, 3, 0)      # a real Saturday
    doc = adm._now_doc(saturday)
    assert doc["weekday"] == "sat", doc
    r = adm.evaluate("aws:ec2:deploy",
                     {**CLEAN, "limits": {**CLEAN["limits"], "prod_window": ["sat"]},
                      "now": doc},
                     policy_dir=POLICY_DIR)
    assert r["decision"] == "deny", (
        "Python's weekday string and the Rego's comparison have drifted — the freeze "
        "would silently never fire: " + repr(r))


def test_several_rules_denying_aggregate_their_reasons():
    """`evaluate` collects across rules; a caller shows all of them. Worth one real
    evaluation because the aggregation walks OPA's actual response shape."""
    r = _decide(request={"region": "eu-west-1", "instance_type": "x1e.32xlarge"},
                limits={"allowed_regions": ["us-east-1"],
                        "denied_instance_types": ["x1e.32xlarge"],
                        "prod_window": ["thu"]})
    assert r["decision"] == "deny", r
    assert len(r["rules"]) >= 2, r["rules"]
    assert len(r["reasons"]) >= 2, r["reasons"]


def test_a_missing_request_field_does_not_break_evaluation():
    """Not every action sends every field — a destroy has no instance_type, a k8s
    provision no image. A rule that assumed one would fail closed and 403 the action."""
    r = adm.evaluate("aws:ec2:destroy",
                     {"actor": {"username": "x", "is_admin": False},
                      "request": {},
                      "limits": CLEAN["limits"],
                      "now": CLEAN["now"]},
                     policy_dir=POLICY_DIR)
    assert r["decision"] == "allow", r


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
