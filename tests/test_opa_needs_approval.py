"""`needs_approval`, end to end through the real policy engine.

This PR turns `needs_approval` from a logged remark into an enforced verdict. Nothing
else in the tree evaluates that path against real Rego, and it cannot:

  - `test_admission_service.py` stubs `_opa` and hands `evaluate` a hand-built dict, so
    it proves the Python branches but never that OPA produces the shape they read.
  - `test_opa_policies.py` runs real OPA, but only over the three SHIPPED policies —
    and all three emit `deny` only. Nothing ships that emits `needs_approval`.

So without this file, the first time an enforced `needs_approval` meets actual Rego is
on somebody's custom policy, in production, on the one verdict whose job is to stop a
change. The gap is narrow and specific: `evaluate` reads `body.get("needs_approval")`
off OPA's JSON, and a partial set of strings has to survive the round-trip into
`approval_reasons` for any of the enforcement branches below to ever run.

The fixture policies live in a temp dir, reached through the documented
`ADMISSION_POLICY_DIR` override rather than by dropping files into
`terraform/policy/admission/` — they are test inputs, not guardrails anyone ships, and
putting them in the shipped directory would also (rightly) fail
`test_opa_policies.py`'s coverage assertion.

**Skips unless OPA is on PATH.** CI installs it — see the `opa` job in
.github/workflows/tests.yml, whose `tests/test_opa_*.py` glob is why this file is
named as it is. `OPA_REQUIRED=1` turns the skip into a failure there.

Run: python tests/test_opa_needs_approval.py   (or under pytest)
"""
import os
import shutil
import sys
import tempfile
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-opa-needs-approval")

CONF = {}  # drives the config_service stub


def _skip(reason):
    try:
        import pytest
        pytest.skip(reason, allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {reason}")
        sys.exit(0)


# ── Fixture policies ─────────────────────────────────────────────────────────
#
# Deliberately minimal and readable: a reviewer should be able to see at a glance what
# each one decides. `soft_gate` is the one that matters — it is the only Rego anywhere
# in this repo that emits `needs_approval`, which is the whole reason this file exists.

SOFT_GATE = """\
# Emits `needs_approval`, not `deny` — the verdict this PR makes enforceable.
package admission.soft_gate

import rego.v1

needs_approval contains msg if {
	startswith(input.request.instance_type, "x1e.")
	msg := sprintf("%s is large enough to want a second pair of eyes", [input.request.instance_type])
}
"""

HARD_BLOCK = """\
# A plain `deny`, present only to prove deny still outranks needs_approval when a real
# bundle emits both at once.
package admission.hard_block

import rego.v1

deny contains msg if {
	input.request.region == "eu-west-1"
	msg := "eu-west-1 is closed"
}
"""

_POLICY_DIR = tempfile.mkdtemp(prefix="opa-needs-approval-")
for _name, _src in (("soft_gate", SOFT_GATE), ("hard_block", HARD_BLOCK)):
    with open(os.path.join(_POLICY_DIR, f"{_name}.rego"), "w", encoding="utf-8") as fh:
        fh.write(_src)

# Must be set BEFORE admission_service is imported: ADMISSION_POLICY_DIR is read at
# import time and bound as `evaluate`'s default argument, so `enforce` — which calls
# `evaluate` without one — only sees it if it is in the environment by then.
os.environ["ADMISSION_POLICY_DIR"] = _POLICY_DIR


def _install_stubs():
    """Everything except `_opa`, which stays REAL — that is the point of this file.

    Mirrors `test_admission_service._install_stubs` so the two read alike; the single
    deliberate difference is the absent OPA stub.
    """
    confmod = types.ModuleType("web_dashboard.config")
    confmod.settings = type("S", (), {"__getattr__": lambda *_: False})()
    sys.modules["web_dashboard.config"] = confmod

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda k, default="": (CONF[k] if k in CONF else default)

    def get_bool(k, default=False):
        if k not in CONF:
            return default
        return str(CONF[k]).strip().lower() in ("1", "true", "yes", "on")

    cfg.get_bool = get_bool
    sys.modules["web_dashboard.services.config_service"] = cfg

    js = types.ModuleType("web_dashboard.services.job_service")
    js.audits = []
    js.log_audit = lambda db, user, action, details=None: js.audits.append(
        (user, action, details))
    sys.modules["web_dashboard.services.job_service"] = js

    fa = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code=None, detail=None):
            self.status_code = status_code
            self.detail = detail
            super().__init__(str(detail))

    fa.HTTPException = HTTPException
    sys.modules["fastapi"] = fa


_install_stubs()

try:
    from web_dashboard.services import _opa as _real_opa
except Exception as exc:  # pragma: no cover — deps absent outside CI
    _skip(f"_opa import unavailable: {exc}")

if not _real_opa.opa_available():
    # See test_opa_policies.py for why CI makes this fatal rather than silent.
    if os.environ.get("OPA_REQUIRED") == "1":
        raise SystemExit(
            "OPA_REQUIRED=1 but no opa binary was found. The CI job is meant to have "
            f"installed one (OPA_BINARY={_real_opa.OPA_BIN!r}) — check the install "
            "step rather than relaxing this."
        )
    _skip("no opa binary on PATH (set OPA_BINARY, or see the `opa` CI job) — "
          "this file only tests what the real engine does")

try:
    from web_dashboard.services import admission_service as adm
except Exception as exc:  # pragma: no cover
    _skip(f"admission_service import unavailable: {exc}")

HTTPException = sys.modules["fastapi"].HTTPException
_js = sys.modules["web_dashboard.services.job_service"]

ACTION = "aws:ec2:deploy"
BIG = {"region": "us-east-1", "instance_type": "x1e.32xlarge", "name": "web-01"}
SMALL = {"region": "us-east-1", "instance_type": "t3.micro", "name": "web-01"}


class _Actor:
    def __init__(self, username="alice", admin=False):
        self.username = username
        self.is_effective_admin = admin


class _Db:
    """A non-None stand-in. `enforce` only passes `db` to the audit helpers, which are
    stubbed; it must not be None or auditing silently no-ops and the assertions below
    would pass without anything being recorded."""


def _reset(**conf):
    CONF.clear()
    CONF.update(conf)
    _js.audits.clear()


def _on(**extra):
    """Config with the engine on, the action gated, and approval enforcement on."""
    base = {"admission_control_enabled": "true",
            "admission_gated_actions": ACTION,
            "admission_enforce_needs_approval": "true"}
    base.update(extra)
    return base


def _enforce(request=BIG, **kw):
    # No `workgroup` key in `request`: that makes the change-window gate (which runs
    # first, and is a separate feature) return immediately, so these assertions are
    # about the approval path only.
    kw.setdefault("actor", _Actor())
    kw.setdefault("db", _Db())
    return adm.enforce(ACTION, request=dict(request), **kw)


# ── The round-trip nothing else covers ───────────────────────────────────────

def test_real_opa_produces_a_needs_approval_verdict():
    """The whole premise. `evaluate` reads `needs_approval` off OPA's JSON and sorts it
    into `approval_reasons`; every branch tested below is dead code if this shape does
    not survive the subprocess."""
    r = adm.evaluate(ACTION, {"request": BIG})
    assert r["decision"] == "needs_approval", r
    assert r["reasons"] == [], ("a needs_approval verdict must not leak into `reasons`,"
                               " which is the deny channel: " + repr(r))
    assert len(r["approval_reasons"]) == 1, r
    assert "second pair of eyes" in r["approval_reasons"][0], r
    assert "soft_gate" in r["rules"], r


def test_a_request_the_soft_rule_ignores_is_allowed():
    """The baseline. Without this the tests below could pass because everything is
    refused rather than because the verdict works."""
    r = adm.evaluate(ACTION, {"request": SMALL})
    assert r["decision"] == "allow", r


def test_deny_still_outranks_needs_approval_in_a_real_bundle():
    """Precedence is asserted in test_admission_service against a hand-built dict. Here
    two separate policy FILES emit the two verdicts at once, which is the arrangement an
    operator actually ends up with."""
    r = adm.evaluate(ACTION, {"request": {**BIG, "region": "eu-west-1"}})
    assert r["decision"] == "deny", r
    assert sorted(r["rules"]) == ["hard_block", "soft_gate"], r
    assert r["approval_reasons"], "the soft reason should still be reported: " + repr(r)


# ── enforce(): the upgrade-safety default ────────────────────────────────────

def test_with_the_setting_off_the_action_proceeds():
    """The default, and the reason it is the default: the docs previously described
    this verdict as advisory, so an operator may be using it as a soft signal today.
    Enforcing it on upgrade would turn working deploys into 403s with their policy
    untouched."""
    _reset(**_on(admission_enforce_needs_approval="false"))
    assert _enforce() == {}
    assert _js.audits == [], ("nothing was gated, so nothing should be audited: "
                              + repr(_js.audits))


def test_the_setting_defaults_to_off_when_absent():
    """Not the same assertion as above: that one sets the key to false, this one leaves
    it unset, which is the state of every existing install on upgrade."""
    _reset(admission_control_enabled="true", admission_gated_actions=ACTION)
    assert _enforce() == {}


# ── enforce(): the two enforced outcomes ─────────────────────────────────────

def test_an_approvable_seam_gets_an_approval_required_job():
    _reset(**_on())
    assert _enforce(approvable=True) == {"approval_required": True}


def test_the_requirement_is_audited_as_needs_approval_not_denied():
    """A distinct audit action, because the change was ADMITTED — as a job nobody may
    run yet. Filing it under `:denied` would make the trail describe something that did
    not happen and hide the approval that follows."""
    _reset(**_on())
    _enforce(approvable=True)
    actions = [a for _u, a, _d in _js.audits]
    assert actions == [f"{ACTION}:needs_approval"], actions
    assert f"{ACTION}:denied" not in actions


def test_a_seam_that_cannot_express_approval_is_refused():
    """`approvable` defaults to False, so a caller that has not opted in is refused
    rather than admitted. Admitting an action a policy said needs a second person is
    the one outcome nobody asked for."""
    _reset(**_on())
    try:
        _enforce()  # approvable omitted
    except HTTPException as exc:
        assert exc.status_code == 403, exc.status_code
        assert exc.detail["error"] == "needs_approval", exc.detail
        assert any("second pair of eyes" in r for r in exc.detail["reasons"]), exc.detail
    else:
        raise AssertionError("a non-approvable seam admitted an action needing approval")


def test_a_real_deny_still_reports_itself_as_a_policy_deny():
    """`error` drives what the UI says, so the two refusals must stay distinguishable:
    `needs_approval` means "find a surface that can request approval", `policy` means
    "a rule refused this". Reporting one as the other sends the operator looking for
    the wrong fix."""
    _reset(**_on())
    try:
        _enforce(request={**SMALL, "region": "eu-west-1"}, approvable=True)
    except HTTPException as exc:
        assert exc.detail["error"] == "policy", exc.detail
        assert [a for _u, a, _d in _js.audits] == [f"{ACTION}:denied"]
    else:
        raise AssertionError("eu-west-1 should have been denied")


# ── enforce(): still inert unless opted into ─────────────────────────────────

def test_an_ungated_action_is_untouched_even_with_the_policy_present():
    """The policy directory is loaded for gated actions only. An operator who has not
    listed an action gets no verdict on it, enforced or otherwise."""
    _reset(**_on(admission_gated_actions="azure:vm:deploy"))
    assert _enforce() == {}
    assert _js.audits == []


def test_the_engine_switch_still_governs_the_opa_path():
    """`admission_control_enabled` off means no OPA at all — including no
    needs_approval — even with approval enforcement switched on."""
    _reset(**_on(admission_control_enabled="false"))
    assert _enforce() == {}


def test_an_allowed_request_is_not_gated_when_enforcement_is_on():
    """The regression that would matter most: switching the setting on must not turn
    every gated deploy into an approval request."""
    _reset(**_on())
    assert _enforce(request=SMALL, approvable=True) == {}
    assert _js.audits == []


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
    try:
        sys.exit(_run())
    finally:
        shutil.rmtree(_POLICY_DIR, ignore_errors=True)
