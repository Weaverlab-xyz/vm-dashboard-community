"""The Rancher node's Entitle registration service: register / deregister.

`test_entitle_rancher.py` pins the layer below this one — the generated HCL and the
`access:secret` token split. `k8s_service.register_rancher_in_entitle` itself, the worker
entry that drives it, and the route that queues it had **no test at all** before this
file, which matters because two of its behaviours are load-bearing and neither is
obvious from reading the call site:

  * **It is not idempotent.** A second register overwrites `entitle_rancher_tfstate`
    while the first integration stays alive in Entitle — and that state was the only
    handle `deregister` had on it, so the original becomes unreachable. The node row's
    Register button is hidden once an integration exists precisely because of this, and
    `test_rancher_entitle_button.py` pins that guard. What is pinned HERE is the
    behaviour the guard exists to protect against, so that if anyone ever makes this
    function idempotent the two tests disagree loudly instead of the guard quietly
    becoming pointless.
  * **Deregister clears both keys even when the terraform destroy fails.** That looks
    like a bug and is not: the alternative leaves a row pointing at state nobody can
    act on, so the operator can never get back to a clean slate. Pinned so nobody
    "fixes" it into exactly that corner.

Stubs the heavy module-load deps the same lightweight way `test_entitle_agent_rbac.py`
does; no DB, no terraform, no Entitle. Runs under pytest or standalone:
    python tests/test_rancher_entitle_register.py
"""
import asyncio
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONF = {}


def _stub(name, **attrs):
    """Install a fake module AND rebind it on its parent package.

    The second half matters: k8s_service reaches its collaborators with
    `from . import x`, which resolves by getattr on the package first — so replacing
    only the sys.modules entry leaves an already-imported real module in play.
    """
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    parent_name, _, leaf = name.rpartition(".")
    parent = sys.modules.get(parent_name)
    if parent is not None:
        setattr(parent, leaf, module)
    return module


# Heavy module-load deps, stubbed so k8s_service imports with no app or DB engine.
_conf = types.ModuleType("web_dashboard.config")


class _Settings:
    def __getattr__(self, _k):
        return ""


_conf.settings = _Settings()
sys.modules.setdefault("web_dashboard.config", _conf)
sys.modules.setdefault("sqlalchemy", types.ModuleType("sqlalchemy"))
_orm = types.ModuleType("sqlalchemy.orm")
_orm.Session = object
sys.modules.setdefault("sqlalchemy.orm", _orm)
_db = types.ModuleType("web_dashboard.database")
_db.Job = type("Job", (), {})
_db.K8sCluster = type("K8sCluster", (), {})
sys.modules.setdefault("web_dashboard.database", _db)

from web_dashboard.services import k8s_service as k  # noqa: E402

_stub("web_dashboard.services.config_service",
      get=lambda key, default="", workgroup=None: CONF.get(key, default),
      get_bool=lambda key, default=False: bool(CONF.get(key, default)),
      set=lambda key, value: CONF.__setitem__(key, value),
      delete=lambda key: CONF.pop(key, None))


def _registered(**over):
    """Config for a bootstrapped node that can be registered."""
    CONF.clear()
    CONF.update({"rancher_server_url": "https://rancher.example",
                 "rancher_api_token": "token-abc:secret-xyz",
                 "rancher_verify_tls": False,
                 "entitle_rancher_app_slug": "rancher"})
    CONF.update(over)


def _entitle(register=None, deregister=None):
    """Stub entitle_registration_service with recording register/deregister."""
    calls = []

    async def _register_rancher(**kwargs):
        calls.append(("register", kwargs))
        if register is not None:
            return register(**kwargs)
        return {"integration_id": "int-1", "tf_state_json": "{state-1}"}

    async def _deregister(state, ctx=None):
        calls.append(("deregister", state))
        if deregister is not None:
            return deregister(state)

    _stub("web_dashboard.services.entitle_registration_service",
          register_rancher=_register_rancher, deregister=_deregister)
    return calls


# ── Register ──────────────────────────────────────────────────────────────────

def test_a_node_that_is_not_running_is_refused_rather_than_registered():
    """An integration pointed at nothing is worse than no integration: it appears in
    Entitle's catalogue and fails every grant. The API route pre-flights this too, but
    the deploy-time auto-register path calls straight through to here."""
    for missing in ("rancher_server_url", "rancher_api_token"):
        _registered(**{missing: ""})
        _entitle()
        try:
            asyncio.run(k.register_rancher_in_entitle("register"))
        except k.K8sError as exc:
            assert "not running" in str(exc), missing
            continue
        raise AssertionError(f"registered with no {missing}")


def test_registering_records_both_the_id_and_the_state():
    """The id drives the UI's chip; the state is the ONLY handle deregister has. Losing
    either leaves an integration nothing can remove."""
    _registered()
    _entitle()
    asyncio.run(k.register_rancher_in_entitle("register"))
    assert CONF["entitle_rancher_integration_id"] == "int-1"
    assert CONF["entitle_rancher_tfstate"] == "{state-1}"


def test_the_token_and_url_are_passed_through_untouched():
    """register_rancher splits the bearer into Rancher's access:secret pair itself, so
    this layer must not pre-process it."""
    _registered()
    calls = _entitle()
    asyncio.run(k.register_rancher_in_entitle("register"))
    kwargs = calls[0][1]
    assert kwargs["api_token"] == "token-abc:secret-xyz"
    assert kwargs["server_url"] == "https://rancher.example"


def test_public_by_default_means_no_agent_token():
    """entitle_rancher_private is the switch between Entitle's cloud dialling the node
    directly and brokering through the shared agent. Default is direct."""
    _registered()
    calls = _entitle()
    asyncio.run(k.register_rancher_in_entitle("register"))
    assert calls[0][1]["private"] is False


def test_the_private_switch_is_honoured():
    _registered(entitle_rancher_private=True)
    calls = _entitle()
    asyncio.run(k.register_rancher_in_entitle("register"))
    assert calls[0][1]["private"] is True


def test_a_failed_registration_records_nothing():
    """A half-recorded registration is the one state the UI cannot reason about: a
    stamped id with no state would show the chip and hide Register while deregister had
    nothing to destroy."""
    _registered()

    def _boom(**kwargs):
        raise RuntimeError("entitle 502")

    _entitle(register=_boom)
    try:
        asyncio.run(k.register_rancher_in_entitle("register"))
    except Exception:
        pass
    assert not CONF.get("entitle_rancher_integration_id")
    assert not CONF.get("entitle_rancher_tfstate")


def test_registering_twice_overwrites_the_state_and_orphans_the_first():
    """**Pinning a hazard, not endorsing it.** The second register replaces the state
    that addressed the first integration, so the first can never be deregistered.

    This is exactly why the node row hides Register once an integration exists
    (see test_rancher_entitle_button). If this function is ever made idempotent —
    checking for an existing id, or destroying before re-creating — this test should
    fail and be deleted along with the UI guard, rather than the guard silently
    becoming decoration.
    """
    _registered()
    seq = iter([{"integration_id": "int-1", "tf_state_json": "{state-1}"},
                {"integration_id": "int-2", "tf_state_json": "{state-2}"}])
    calls = _entitle(register=lambda **kw: next(seq))
    asyncio.run(k.register_rancher_in_entitle("register"))
    asyncio.run(k.register_rancher_in_entitle("register"))
    assert CONF["entitle_rancher_tfstate"] == "{state-2}"
    # Nothing destroyed the first one, and its state is gone from config.
    assert [c[0] for c in calls] == ["register", "register"]


# ── Deregister ────────────────────────────────────────────────────────────────

def test_deregistering_destroys_the_recorded_state_and_clears_both_keys():
    _registered(entitle_rancher_integration_id="int-1",
                entitle_rancher_tfstate="{state-1}")
    calls = _entitle()
    asyncio.run(k.register_rancher_in_entitle("deregister"))
    assert ("deregister", "{state-1}") in calls
    assert CONF["entitle_rancher_tfstate"] == ""
    assert CONF["entitle_rancher_integration_id"] == ""


def test_a_failed_destroy_still_clears_the_keys():
    """Deliberate, and the opposite of what it looks like. Keeping the keys after a
    failed destroy leaves the row pointing at state nobody can act on — the UI would
    show a chip and offer only Deregister, forever. Clearing them lets the operator get
    back to a clean slate and remove the leftover in Entitle by hand.
    """
    _registered(entitle_rancher_integration_id="int-1",
                entitle_rancher_tfstate="{state-1}")

    def _boom(state):
        raise RuntimeError("terraform destroy failed")

    _entitle(deregister=_boom)
    asyncio.run(k.register_rancher_in_entitle("deregister"))
    assert CONF["entitle_rancher_tfstate"] == ""
    assert CONF["entitle_rancher_integration_id"] == ""


def test_deregistering_when_nothing_is_registered_is_a_no_op():
    """The teardown path calls this unconditionally, and a node that was never
    registered must not fail its own teardown."""
    _registered()
    calls = _entitle()
    asyncio.run(k.register_rancher_in_entitle("deregister"))
    assert [c[0] for c in calls] == [], "terraform was invoked with no state"
    assert CONF["entitle_rancher_tfstate"] == ""


def test_deregister_does_not_need_a_running_node():
    """The node is usually already gone — teardown deregisters before deleting the VM,
    but a manual deregister may follow a node that vanished."""
    CONF.clear()
    CONF.update({"entitle_rancher_tfstate": "{state-1}",
                 "entitle_rancher_integration_id": "int-1"})
    _entitle()
    asyncio.run(k.register_rancher_in_entitle("deregister"))
    assert CONF["entitle_rancher_integration_id"] == ""


# ── The worker entry ──────────────────────────────────────────────────────────

class _Jobs:
    def __init__(self):
        self.running = []
        self.completed = []
        self.failed = []
        # Kept, not discarded: the reachability warning is the one outcome that only
        # exists in the RESULT, so a test that cannot read it can only fall back to
        # grepping the source — which then breaks the moment the line moves.
        self.results = {}

    def set_running(self, db, job_id):
        self.running.append(job_id)

    def set_completed(self, db, job_id, result=None):
        self.completed.append(job_id)
        self.results[job_id] = result or {}

    def set_failed(self, db, job_id, msg):
        self.failed.append((job_id, msg))


def _worker_stubs(firewall=None):
    jobs = _Jobs()
    _stub("web_dashboard.services.job_service",
          set_running=jobs.set_running, set_completed=jobs.set_completed,
          set_failed=jobs.set_failed,
          update_progress=lambda db, job_id, pct, msg: None)

    async def _broadcast(job_id, pct, msg):
        return None

    _stub("web_dashboard.api.websocket", broadcast_progress=_broadcast)

    # Stubbed rather than left to reach the real module: without a db session it would
    # raise, get swallowed by the best-effort guard, and the test would pass while
    # asserting nothing about the firewall leg.
    jobs.firewall_calls = []

    async def _refresh(db, placement=None):
        jobs.firewall_calls.append(placement)
        if firewall is not None:
            return firewall()
        return {}

    _stub("web_dashboard.services.rancher_node_service",
          refresh_rancher_firewall=_refresh)
    return jobs


def test_the_worker_completes_the_job_on_success():
    _registered()
    _entitle()
    jobs = _worker_stubs()
    asyncio.run(k.run_rancher_entitle_register(None, job_id="j1", action="register"))
    assert jobs.completed == ["j1"] and not jobs.failed


def test_the_worker_fails_the_job_with_the_reason():
    """The job detail view shows error_message and nothing else, so a swallowed failure
    is a registration that silently never happened — which is the state this whole
    control exists to make visible."""
    _registered(rancher_api_token="")
    _entitle()
    jobs = _worker_stubs()
    asyncio.run(k.run_rancher_entitle_register(None, job_id="j2", action="register"))
    assert jobs.completed == []
    assert len(jobs.failed) == 1 and "not running" in jobs.failed[0][1]


# ── Allow-listing Entitle on the node firewall ───────────────────────────────
# A `private = false` integration is dialled directly by Entitle's cloud, so its
# egress addresses hit the node's allow-list like a Gateway's /32 does. Registration
# talks to Entitle's API and never to the node, so it succeeds whether or not the
# node admits Entitle -- which is why the register job has to do this and has to say
# when it could not.

#: Stand-in for the real resolver's warning. A LITERAL, deliberately: reaching into
#: the module for it made the stub self-referential — the second `_egress()` call
#: re-imported the FAKE installed by the first, whose lambda has no docstring, so the
#: warning silently became None and any test after the first "unknown ranges" one saw
#: a clean bill of health. The real sentence is pinned in test_rancher_entitle_button.
_EGRESS_GAP = "ranges are not known ... set entitle_source_cidrs"


def _egress(cidrs=(), region="us"):
    _stub("web_dashboard.services.entitle_egress",
          cidrs=lambda: list(cidrs),
          configured=lambda: bool(cidrs),
          region=lambda: region,
          unconfigured_warning=lambda: "" if cidrs else _EGRESS_GAP)


def test_the_firewall_is_reapplied_after_a_register():
    """Otherwise the ranges only land on the next node DEPLOY, and the registration
    the operator just performed still cannot grant."""
    _registered()
    _entitle()
    jobs = _worker_stubs()
    _egress(cidrs=["203.0.113.0/24"])
    asyncio.run(k.run_rancher_entitle_register(None, job_id="j1", action="register"))
    assert len(jobs.firewall_calls) == 1
    assert jobs.completed == ["j1"]


def test_the_firewall_is_reapplied_after_a_deregister_too():
    """Symmetric: the ranges were only ever open while the integration existed."""
    _registered(entitle_rancher_integration_id="int-1",
                entitle_rancher_tfstate="{state-1}")
    _entitle()
    jobs = _worker_stubs()
    _egress(cidrs=["203.0.113.0/24"])
    asyncio.run(k.run_rancher_entitle_register(None, job_id="j2", action="deregister"))
    assert len(jobs.firewall_calls) == 1


def test_a_firewall_failure_does_not_fail_a_real_registration():
    """The integration exists either way; failing the job would report a registration
    that DID happen as not having happened. The outcome goes in the result instead."""
    _registered()
    _entitle()

    def _boom():
        raise RuntimeError("403 on compute.firewalls.update")

    jobs = _worker_stubs(firewall=_boom)
    _egress(cidrs=["203.0.113.0/24"])
    asyncio.run(k.run_rancher_entitle_register(None, job_id="j3", action="register"))
    assert jobs.completed == ["j3"], "a firewall hiccup must not fail the job"


def test_registering_with_unknown_ranges_warns_in_the_job_result():
    """The one case where the job completes and the feature still does not work: the
    integration is live and healthy-looking while being unable to grant. Nothing
    downstream will say so, so the result has to."""
    _registered()
    _entitle()
    jobs = _worker_stubs()
    _egress(cidrs=[])
    asyncio.run(k.run_rancher_entitle_register(None, job_id="j4", action="register"))
    assert jobs.completed == ["j4"]
    assert "entitle_source_cidrs" in jobs.results["j4"]["reachability_warning"]


def test_the_reachability_action_reapplies_the_firewall_without_re_registering():
    """The repair for a node whose integration was created before the deploy path
    re-merged the allow-list. `register_rancher_in_entitle` is NOT idempotent, so the
    fix cannot be "register again" — that strands the live integration in Entitle. The
    integration id must come out untouched.
    """
    _registered(entitle_rancher_integration_id="int-1",
                entitle_rancher_tfstate="{state-1}")
    calls = _entitle()
    jobs = _worker_stubs()
    _egress(cidrs=["203.0.113.0/24"])
    asyncio.run(k.run_rancher_entitle_register(None, job_id="j6", action="reachability"))
    assert jobs.completed == ["j6"]
    assert len(jobs.firewall_calls) == 1, "the whole point of the action"
    assert not calls, "a repair must never create a second integration"
    assert CONF["entitle_rancher_integration_id"] == "int-1"
    assert jobs.results["j6"]["entitle_source_cidrs"] == ["203.0.113.0/24"]


def test_the_reachability_action_still_reports_unknown_ranges():
    """It is the action an operator reaches for BECAUSE grants time out, so staying
    silent about the one thing that would explain it is the worst possible moment."""
    _registered(entitle_rancher_integration_id="int-1")
    _entitle()
    jobs = _worker_stubs()
    _egress(cidrs=[])
    asyncio.run(k.run_rancher_entitle_register(None, job_id="j7", action="reachability"))
    assert "reachability_warning" in jobs.results["j7"]


def test_agent_brokered_mode_does_not_warn():
    """In private mode the agent reaches the node from inside, so having no inbound
    ranges is the correct state and warning about it would be noise."""
    _registered(entitle_rancher_private=True)
    _entitle()
    jobs = _worker_stubs()
    _egress(cidrs=[])
    asyncio.run(k.run_rancher_entitle_register(None, job_id="j5", action="register"))
    assert jobs.completed == ["j5"]


# ── The range resolver ───────────────────────────────────────────────────────

def _real_egress():
    """The REAL resolver, re-imported.

    `_egress()` above installs a fake `entitle_egress` for the worker tests, and it
    stays in sys.modules — so importing here without evicting it first would hand back
    the stub and the assertions would be testing the lambdas. Test order makes that a
    coin flip rather than a reliable failure, which is worse.
    """
    import importlib
    sys.modules.pop("web_dashboard.services.entitle_egress", None)
    parent = sys.modules.get("web_dashboard.services")
    if parent is not None and hasattr(parent, "entitle_egress"):
        delattr(parent, "entitle_egress")
    _stub("web_dashboard.services.config_service",
          get=lambda key, default="", workgroup=None: CONF.get(key, default),
          get_bool=lambda key, default=False: bool(CONF.get(key, default)),
          set=lambda key, value: CONF.__setitem__(key, value))
    return importlib.import_module("web_dashboard.services.entitle_egress")


def test_the_region_comes_from_the_api_url():
    """A second config key for the same fact is a second thing to get wrong, and the
    API URL is already the regional one."""
    eg = _real_egress()
    CONF.clear()
    CONF["entitle_api_url"] = "https://api.eu.entitle.io/v1"
    assert eg.region() == "eu"
    # An unrecognised or bare host falls back rather than guessing a region.
    CONF["entitle_api_url"] = "https://api.entitle.io"
    assert eg.region() == "us"
    CONF["entitle_api_url"] = ""
    assert eg.region() == "us"


def test_the_us_region_resolves_to_the_published_deployment_addresses():
    """The default region, so an install that never touched entitle_api_url gets a
    working allow-list rather than the warning."""
    eg = _real_egress()
    CONF.clear()
    assert eg.region() == "us"
    us = eg.cidrs()
    assert us and all("/" in c for c in us), us
    assert eg.configured() is True
    assert eg.unconfigured_warning() == ""


def test_a_region_with_no_published_list_reads_as_unknown_not_as_fine():
    """"Empty" must never be mistaken for "no ranges needed" — that is the difference
    between a warning and a registration that silently cannot grant."""
    eg = _real_egress()
    CONF.clear()
    CONF["entitle_api_url"] = "https://api.eu.entitle.io/v1"
    assert eg.region() == "eu"
    assert eg.cidrs() == []
    assert eg.configured() is False
    warn = eg.unconfigured_warning()
    assert "entitle_source_cidrs" in warn and "time out" in warn, warn
    assert "'eu'" in warn, "name the region so the operator knows which list to find"


def test_the_operator_override_replaces_the_published_list_rather_than_extending_it():
    """A tenant on a different Entitle DEPLOYMENT egresses from different addresses, so
    merging the two would admit hosts that never call while still being incomplete.
    The override has to win outright."""
    eg = _real_egress()
    CONF.clear()
    published = set(eg.cidrs())
    assert published, "expected a published US list to override"
    CONF["entitle_source_cidrs"] = "198.51.100.7/32, 203.0.113.0/24"
    assert eg.cidrs() == ["198.51.100.7/32", "203.0.113.0/24"], "sorted + deduped"
    assert not published & set(eg.cidrs()), "the published list must not survive"
    assert eg.unconfigured_warning() == ""


def test_both_actions_are_the_declared_ones():
    """The route validates `action` against this tuple, and the UI sends exactly these
    two strings."""
    assert k.VALID_ENTITLE_CLUSTER_ACTIONS == ("register", "deregister")


if __name__ == "__main__":
    fns = [v for kk, v in sorted(globals().items())
           if kk.startswith("test_") and callable(v)]
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
