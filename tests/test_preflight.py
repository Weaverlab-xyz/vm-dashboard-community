"""The aggregate integration preflight: what it probes, and what it must never touch.

Eight endpoints in this tree each answer "can you reach X?" for one X, from one panel, in
one shape — three admin styles, two failure conventions, two names for the same field.
``services/preflight`` is the missing aggregate. These tests pin the two things that make
it safe to put behind a refresh button.

**The exclusions are the point.** ``notifications/endpoints/{id}/test`` SENDS A REAL
MESSAGE, and ``connections/{id}/test`` stamps its result onto the hypervisor row. A sweep
that included either would post to every configured Slack and webhook, or write to the
database, every time somebody reloaded a health page. Those two tests below are the ones
that matter; the rest is mechanics.

**`ok` is three-valued and the third value is not a failure.** Not-configured and
configured-but-unprobeable both mean "no answer", and counting them as failures is how an
instance that simply does not use Skytap ends up rendered as broken.

Run: python tests/test_preflight.py   (or under pytest)
"""
import ast
import asyncio
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-preflight")

try:
    from web_dashboard.services import preflight
except Exception as exc:  # pragma: no cover — deps absent outside CI
    try:
        import pytest
        pytest.skip(f"import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

_SVC_SRC = open(os.path.join(_ROOT, "web_dashboard/services/preflight.py"),
                encoding="utf-8").read()
_API_SRC = open(os.path.join(_ROOT, "web_dashboard/api/preflight.py"),
                encoding="utf-8").read()


def _run(coro):
    return asyncio.run(coro)


def _stub(name, **attrs):
    """Install a fake service module so a probe's lazy `from . import X` finds it."""
    mod = types.ModuleType(f"web_dashboard.services.{name}")
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[f"web_dashboard.services.{name}"] = mod
    import web_dashboard.services as pkg
    setattr(pkg, name, mod)
    return mod


# ── The exclusions. These are the safety property. ────────────────────────────

def test_the_sweep_never_sends_a_real_notification():
    """`notifications/endpoints/{id}/test` sends a REAL message — that is its whole value.

    Folding it into a sweep would turn a page refresh into a broadcast to every configured
    Slack, Teams and webhook endpoint. It stays a button somebody chooses to press.
    """
    banned = ("test_send", "notification_service", "notify_service")
    found = [b for b in banned if b in _SVC_SRC.replace("notifications/endpoints", "")]
    assert not found, (
        f"the preflight sweep reaches the notification sender {found} — a refresh would "
        "post to every configured endpoint")
    assert not any(k.startswith("notification") for k, _ in preflight.PROBES)


def test_the_sweep_never_writes_a_hypervisor_connection_result():
    """`connections/{id}/test` dials a hypervisor and STAMPS the outcome on the row.

    The connections page already renders that stamp, so a second surface would be two
    sources for one fact. And an agent-bound connection cannot be dialled from here at
    all — its liveness is the agent's status, where it already lives.
    """
    banned = ("record_result", "hypervisor_connection_service", "hcs.")
    found = [b for b in banned if b in _SVC_SRC]
    assert not found, f"the preflight sweep writes connection state {found}"
    assert not any(k.startswith("connection") for k, _ in preflight.PROBES)


def test_the_endpoint_is_a_read():
    """GET, no body. What makes it safe behind a refresh button — and why the two
    side-effecting probes are excluded outright rather than gated on a parameter, which
    would be one copied URL away from not being safe."""
    tree = ast.parse(_API_SRC)
    routes = [d for n in ast.walk(tree)
              if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
              for d in n.decorator_list
              if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)]
    assert routes, "no routes found"
    verbs = {d.func.attr for d in routes}
    assert verbs == {"get"}, f"preflight should expose reads only, found {verbs}"


def test_the_endpoint_requires_admin():
    """Each result names an integration and carries an upstream error message."""
    assert "require_admin" in _API_SRC
    assert "Depends(require_admin)" in _API_SRC, (
        "use the dependency, not a hand-rolled JWT decode — it consults "
        "is_effective_admin and refuses accessors")


# ── The tri-state ─────────────────────────────────────────────────────────────

def test_not_configured_is_not_a_failure():
    _stub("skytap_service", configured=lambda: False)
    r = _run(preflight._probe_skytap())
    assert r.configured is False
    assert r.ok is None, "an integration this instance does not use is not broken"


def test_configured_but_unprobeable_is_not_a_failure_either():
    """A POV cloud adapter with no credential check says nothing about whether the cloud
    works — so `ok` is None, not False."""
    _stub("lab_platforms",
          selected_cloud=lambda: "aws",
          supports=lambda cloud, cap: False,
          adapter=lambda c: None)
    r = _run(preflight._probe_pov_cloud())
    assert r.configured is True
    assert r.ok is None
    assert "no credential check" in r.detail


def test_a_reachable_integration_reports_ok_with_its_message():
    async def _verify():
        return True, "Authenticated as acme-project."
    _stub("skytap_service", configured=lambda: True, verify=_verify)
    r = _run(preflight._probe_skytap())
    assert r.ok is True and "acme-project" in r.detail


def test_an_unreachable_integration_carries_the_upstream_wording():
    """The upstream's own text is what separates a revoked token from a wrong host."""
    async def _verify():
        return False, "401 Unauthorized: token revoked"
    _stub("skytap_service", configured=lambda: True, verify=_verify)
    r = _run(preflight._probe_skytap())
    assert r.ok is False
    assert "token revoked" in r.detail


# ── The runner cannot be sunk by one probe ────────────────────────────────────

def test_one_exploding_probe_does_not_sink_the_sweep():
    """Every integration's SDK raises its own type; an unfamiliar one is still an answer
    to "can you reach it?", so it is reported rather than propagated."""
    async def boom():
        raise RuntimeError("some SDK's own exception type")
    r = _run(preflight._run_one("skytap", boom))
    assert r.ok is False
    assert "some SDK's own exception" in r.detail


def test_a_hanging_probe_times_out_instead_of_holding_the_page():
    async def hang():
        await asyncio.sleep(60)
    original = preflight.PROBE_TIMEOUT_S
    preflight.PROBE_TIMEOUT_S = 0.05
    try:
        r = _run(preflight._run_one("storage", hang))
    finally:
        preflight.PROBE_TIMEOUT_S = original
    assert r.ok is False
    assert "No answer within" in r.detail


def test_the_probes_run_concurrently():
    """Independent round-trips against different providers. In series the page waits for
    the sum, which on six integrations is the difference between usable and not."""
    assert "asyncio.gather" in _SVC_SRC, "run_all should gather, not loop and await"


def test_every_registered_probe_is_a_real_function():
    """A registry entry pointing at nothing would fail only when someone opened the page."""
    for key, probe in preflight.PROBES:
        assert callable(probe), f"{key} is not callable"
        assert asyncio.iscoroutinefunction(probe), f"{key} must be async"
        assert key in preflight._LABELS, f"{key} has no display label"


# ── The summary ───────────────────────────────────────────────────────────────

def test_the_summary_does_not_count_unprobed_as_failing():
    R = preflight.Result
    results = [
        R("a", "A", True, True, ""),
        R("b", "B", True, False, "nope"),
        R("c", "C", False, None, "not configured"),
        R("d", "D", True, None, "no credential check"),
    ]
    s = preflight.summarize(results)
    assert s == {"total": 4, "ok": 1, "failing": 1, "not_probed": 2, "configured": 3}, s


def _run_tests():
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
    sys.exit(_run_tests())
