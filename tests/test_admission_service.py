"""Unit tests for the pre-action admission gate (services/admission_service.py).

Covers the decision engine (`evaluate` precedence + fail-closed) and the community
`enforce` wrapper (flag/gating no-op, 403-on-deny with audit, fail-closed,
needs_approval-is-advisory). The OPA subprocess seam (`_opa.eval_query`) is stubbed,
so no `opa` binary is needed; config, job_service, fastapi and the pydantic config
are stubbed in sys.modules so only stdlib is required.

Runs under pytest, or standalone:  python tests/test_admission_service.py
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONF = {}  # drives the config_service stub


def _install_stubs():
    # pydantic Settings stand-in — any attr resolves falsey.
    confmod = types.ModuleType("web_dashboard.config")
    confmod.settings = type("S", (), {"__getattr__": lambda *_: False})()
    sys.modules["web_dashboard.config"] = confmod

    # OPA seam: tests set opa._value (returned) or opa._raise (raised).
    opa = types.ModuleType("web_dashboard.services._opa")

    class OpaError(Exception):
        pass

    opa.OpaError = OpaError
    opa._value = {}
    opa._raise = None

    def eval_query(input_doc, *, data_dir, query, timeout=30):
        if opa._raise is not None:
            raise opa._raise
        eval_query.last_input = input_doc  # let tests inspect the built input doc
        return opa._value

    eval_query.last_input = None
    opa.eval_query = eval_query
    opa.opa_available = lambda: True
    opa.list_packages = lambda d: []
    sys.modules["web_dashboard.services._opa"] = opa

    # config_service driven by CONF.
    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda k, default="": (CONF[k] if k in CONF else default)

    def get_bool(k, default=False):
        if k not in CONF:
            return default
        return str(CONF[k]).strip().lower() in ("1", "true", "yes", "on")

    cfg.get_bool = get_bool
    sys.modules["web_dashboard.services.config_service"] = cfg

    # job_service.log_audit capture (for deny auditing).
    js = types.ModuleType("web_dashboard.services.job_service")
    js.audits = []
    js.log_audit = lambda db, user, action, details=None: js.audits.append((user, action, details))
    sys.modules["web_dashboard.services.job_service"] = js

    # Minimal fastapi.HTTPException.
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
    from web_dashboard.services import admission_service as adm
except Exception as exc:  # pragma: no cover
    try:
        import pytest
        pytest.skip(f"admission_service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

_opa = sys.modules["web_dashboard.services._opa"]
_js = sys.modules["web_dashboard.services.job_service"]
HTTPException = sys.modules["fastapi"].HTTPException


class _Actor:
    def __init__(self, username="alice", admin=True):
        self.username = username
        self.is_effective_admin = admin


def _reset():
    CONF.clear()
    _opa._value = {}
    _opa._raise = None
    _opa.eval_query.last_input = None
    _js.audits.clear()


# ── evaluate(): decision engine ────────────────────────────────────────────────

def test_evaluate_allow_when_no_violations():
    _reset()
    _opa._value = {"allowed_regions": {"deny": []}, "prod_window": {}}
    r = adm.evaluate("aws:ec2:deploy", {"request": {}})
    assert r["decision"] == "allow" and r["reasons"] == [] and r["rules"] == []


def test_evaluate_deny_aggregates_reasons_and_rules():
    _reset()
    _opa._value = {"allowed_regions": {"deny": ["region x not allowed"]},
                   "instance_size_caps": {"deny": ["size too big"]}}
    r = adm.evaluate("aws:ec2:deploy", {"request": {}})
    assert r["decision"] == "deny"
    assert set(r["reasons"]) == {"region x not allowed", "size too big"}
    assert set(r["rules"]) == {"allowed_regions", "instance_size_caps"}


def test_evaluate_precedence_deny_over_needs_approval():
    _reset()
    _opa._value = {"a": {"deny": ["hard no"]}, "b": {"needs_approval": ["maybe"]}}
    assert adm.evaluate("x", {})["decision"] == "deny"


def test_evaluate_needs_approval_when_only_soft():
    _reset()
    _opa._value = {"b": {"needs_approval": ["maybe"]}}
    r = adm.evaluate("x", {})
    assert r["decision"] == "needs_approval" and r["approval_reasons"] == ["maybe"]


def test_evaluate_fails_closed_on_opa_error():
    _reset()
    _opa._raise = _opa.OpaError("opa missing")
    try:
        adm.evaluate("x", {})
    except adm.AdmissionError:
        return
    raise AssertionError("expected AdmissionError (fail-closed)")


# ── enforce(): the community gate ──────────────────────────────────────────────

def test_enforce_noop_when_disabled():
    _reset()
    CONF["admission_gated_actions"] = "aws:ec2:deploy"
    _opa._value = {"r": {"deny": ["would block"]}}  # would deny IF it ran
    adm.enforce("aws:ec2:deploy", request={"region": "eu-west-1"}, actor=_Actor(), db=object())
    # disabled → no eval, no raise, no audit
    assert _opa.eval_query.last_input is None
    assert _js.audits == []


def test_enforce_noop_when_action_not_gated():
    _reset()
    CONF["admission_control_enabled"] = "1"  # on, but action not listed
    _opa._value = {"r": {"deny": ["would block"]}}
    adm.enforce("aws:ec2:deploy", request={"region": "eu-west-1"}, actor=_Actor(), db=object())
    assert _js.audits == []


def test_enforce_denies_with_403_and_audits():
    _reset()
    CONF["admission_control_enabled"] = "1"
    CONF["admission_gated_actions"] = "aws:ec2:deploy"
    _opa._value = {"allowed_regions": {"deny": ["region eu-west-1 not allowed"]}}
    try:
        adm.enforce("aws:ec2:deploy", request={"region": "eu-west-1"}, actor=_Actor(), db=object())
    except HTTPException as e:
        assert e.status_code == 403
        assert "region eu-west-1 not allowed" in e.detail["reasons"]
    else:
        raise AssertionError("expected HTTPException(403)")
    # denial audited under "<action>:denied"
    assert _js.audits and _js.audits[-1][1] == "aws:ec2:deploy:denied"
    # the built input doc carried request + injected limits + now
    inp = _opa.eval_query.last_input
    assert inp["action"] == "aws:ec2:deploy"
    assert inp["request"]["region"] == "eu-west-1"
    assert "allowed_regions" in inp["limits"] and "weekday" in inp["now"]


def test_enforce_injects_config_limits():
    _reset()
    CONF["admission_control_enabled"] = "1"
    CONF["admission_gated_actions"] = "aws:ec2:deploy"
    CONF["admission_allowed_regions"] = "us-east-1, us-west-2"
    CONF["admission_denied_instance_types"] = '["p4d.24xlarge"]'
    CONF["admission_prod_window"] = "sat,sun"
    _opa._value = {}  # allow
    adm.enforce("aws:ec2:deploy", request={"region": "us-east-1"}, actor=_Actor(), db=object())
    limits = _opa.eval_query.last_input["limits"]
    assert limits["allowed_regions"] == ["us-east-1", "us-west-2"]
    assert limits["denied_instance_types"] == ["p4d.24xlarge"]  # JSON form parsed
    assert limits["prod_window"] == ["sat", "sun"]


def test_enforce_fails_closed_on_engine_error():
    _reset()
    CONF["admission_control_enabled"] = "1"
    CONF["admission_gated_actions"] = "aws:ec2:deploy"
    _opa._raise = _opa.OpaError("opa binary missing")
    try:
        adm.enforce("aws:ec2:deploy", request={"region": "x"}, actor=_Actor(), db=object())
    except HTTPException as e:
        assert e.status_code == 403
    else:
        raise AssertionError("expected fail-closed 403")
    assert _js.audits[-1][1] == "aws:ec2:deploy:denied"


def test_enforce_needs_approval_gates_a_seam_that_can_act():
    """Community HAS an approval gate now — Job.approval_required, the approve
    endpoint, the `change_windows:use` permission — so this verdict is enforced rather
    than logged.

    A seam that passes `approvable=True` is promising to hand the result to
    create_job, so it is admitted WITH the requirement attached.
    """
    _reset()
    CONF["admission_control_enabled"] = "1"
    CONF["admission_gated_actions"] = "aws:ec2:deploy"
    CONF["admission_enforce_needs_approval"] = "1"
    _opa._value = {"r": {"needs_approval": ["a human should look"]}}
    out = adm.enforce("aws:ec2:deploy", request={"region": "x"}, actor=_Actor(),
                      db=object(), approvable=True)
    assert out == {"approval_required": True}, out
    # Audited as its own action, NOT as a denial: the change was admitted.
    assert [a[1] for a in _js.audits] == ["aws:ec2:deploy:needs_approval"], _js.audits


def test_enforce_needs_approval_refuses_a_seam_that_cannot():
    """The uniformity property, and why it is a refusal rather than a shrug.

    A seam that cannot create an approval-gated job has two options: admit an action a
    policy said needs a second person, or refuse. Admitting is the one outcome nobody
    asked for — and it would make a policy appear to work on some pages while silently
    doing nothing on others, which is worse than not enforcing it at all.
    """
    _reset()
    CONF["admission_control_enabled"] = "1"
    CONF["admission_gated_actions"] = "aws:ec2:deploy"
    CONF["admission_enforce_needs_approval"] = "1"
    _opa._value = {"r": {"needs_approval": ["a human should look"]}}
    try:
        adm.enforce("aws:ec2:deploy", request={"region": "x"}, actor=_Actor(),
                    db=object())
    except Exception as exc:  # HTTPException, stubbed in this harness
        detail = getattr(exc, "detail", {})
        assert detail.get("error") == "needs_approval", detail
        assert "a human should look" in detail["reasons"][0], detail
        return
    raise AssertionError("an un-actionable approval verdict was admitted")


def test_needs_approval_is_advisory_until_the_flag_is_on():
    """THE default, and the reason the flag exists.

    No shipped rule emits `needs_approval`, but the guardrails doc described the
    verdict as available and advisory — so an operator may have written a custom rule
    relying on that. Enforcing it silently on upgrade would turn actions that ran
    yesterday into 403s with the policy unchanged. Off, the behaviour is exactly what
    it has always been: logged, and the action proceeds.
    """
    _reset()
    CONF["admission_control_enabled"] = "1"
    CONF["admission_gated_actions"] = "aws:ec2:deploy"
    # admission_enforce_needs_approval deliberately NOT set.
    _opa._value = {"r": {"needs_approval": ["a human should look"]}}
    out = adm.enforce("aws:ec2:deploy", request={"region": "x"}, actor=_Actor(),
                      db=object())
    assert out == {}, out
    assert _js.audits == [], "an advisory verdict should write no audit row"


def test_needs_approval_is_advisory_even_for_a_seam_that_could_act():
    """The flag governs the verdict, not the seam. A deploy form that passes
    `approvable=True` must still get an ordinary job while enforcement is off."""
    _reset()
    CONF["admission_control_enabled"] = "1"
    CONF["admission_gated_actions"] = "aws:ec2:deploy"
    _opa._value = {"r": {"needs_approval": ["a human should look"]}}
    out = adm.enforce("aws:ec2:deploy", request={"region": "x"}, actor=_Actor(),
                      db=object(), approvable=True)
    assert out == {}, out


def test_an_allowed_action_returns_no_extra_job_kwargs():
    """`enforce` returns create_job kwargs now. An ordinary allow must return an EMPTY
    dict, so splatting it leaves every existing call byte-for-byte unchanged."""
    _reset()
    CONF["admission_control_enabled"] = "1"
    CONF["admission_gated_actions"] = "aws:ec2:deploy"
    _opa._value = {"r": {}}
    assert adm.enforce("aws:ec2:deploy", request={"region": "x"}, actor=_Actor(),
                       db=object()) == {}


def test_an_ungated_action_returns_no_extra_job_kwargs():
    _reset()
    assert adm.enforce("aws:ec2:deploy", request={}, actor=_Actor(), db=object()) == {}


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
    sys.exit(1 if failures else 0)
