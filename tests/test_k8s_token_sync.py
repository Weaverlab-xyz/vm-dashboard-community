"""Behavioral tests for k8s_token_sync — the watermark reconciler that keeps a PRA Vault
token account in step with the ServiceAccount token Password Safe rotates.

The highest-value test here is ``test_the_watermark_records_the_pre_checkout_date``. The
change date must be read BEFORE the checkout and that value recorded on success; storing
a post-checkout re-read would mean recording token *B*'s date having pushed token *A* —
a silent, permanent desync no later pass can detect, because every later pass then sees
"nothing changed". One wasted checkout is the price of it being self-correcting instead.

Also pinned: a failure never advances the watermark (the difference between "retries next
pass" and "silently stale forever"), the rotate-on-release circuit breaker, the free
verification of the previous push, and that neither the token nor its full digest reaches
a job result.

Every collaborator is a stub in sys.modules — no app, DB or Password Safe.
Runs under pytest or standalone:  python tests/test_k8s_token_sync.py
"""
import asyncio
import importlib.util
import json
import os
import sys
import types
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzYSJ9.c2lnbmF0dXJl"


class _Col:
    def __eq__(self, other):
        return True
    def __hash__(self):
        return 0
    def isnot(self, other):
        return True
    def in_(self, other):
        return True


class _Meta(type):
    def __getattr__(cls, name):
        return _Col()


def _load():
    pkg_web = sys.modules.setdefault("web_dashboard", types.ModuleType("web_dashboard"))
    pkg_web.__path__ = [os.path.join(_ROOT, "web_dashboard")]
    pkg_svc = sys.modules.setdefault("web_dashboard.services",
                                     types.ModuleType("web_dashboard.services"))
    pkg_svc.__path__ = [os.path.join(_ROOT, "web_dashboard", "services")]

    sys.modules.setdefault("sqlalchemy", types.ModuleType("sqlalchemy"))
    sys.modules["sqlalchemy"].text = lambda s: s
    _orm = types.ModuleType("sqlalchemy.orm")
    _orm.Session = object
    sys.modules.setdefault("sqlalchemy.orm", _orm)

    _dbm = types.ModuleType("web_dashboard.database")
    _dbm.Job = _Meta("Job", (), {})
    _dbm.K8sCluster = _Meta("K8sCluster", (), {})
    _dbm._is_sqlite = True
    sys.modules["web_dashboard.database"] = _dbm

    settings_stub = types.ModuleType("web_dashboard.config")
    settings_stub.settings = types.SimpleNamespace()
    sys.modules["web_dashboard.config"] = settings_stub

    # job_service is imported at module level by k8s_token_sync.
    m_job = types.ModuleType("web_dashboard.services.job_service")
    m_job.ACTIVE_STATUSES = ("queued", "pending", "running")
    m_job.set_running = lambda db, jid: None
    m_job.set_failed = lambda db, jid, msg: _JOBS.append(("failed", msg))
    m_job.set_completed = lambda db, jid, result=None: _JOBS.append(("completed", result))
    m_job.update_progress = lambda db, jid, pct, msg: None
    m_job.create_job = lambda db, job_type, created_by, metadata=None: types.SimpleNamespace(
        id="new-job")
    sys.modules["web_dashboard.services.job_service"] = m_job

    path = os.path.join(_ROOT, "web_dashboard", "services", "k8s_token_sync.py")
    spec = importlib.util.spec_from_file_location(
        "web_dashboard.services.k8s_token_sync", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_JOBS = []
sync = _load()


class FakeDB:
    def __init__(self, row):
        self.row = row
    def query(self, model):
        db = self
        class _Q:
            def filter(self, *a, **kw):
                return self
            def first(self):
                return db.row
            def all(self):
                return [db.row] if db.row is not None else []
        return _Q()
    def commit(self):
        pass
    def rollback(self):
        pass
    def execute(self, *a, **kw):
        return None


def _row(**kw):
    base = dict(id="c-1", name="gke-demo", cloud="gcp", status="registered",
                ps_token_account_id="201", ps_pra_vault_account_id="202",
                pra_jump_id="9", pra_vault_account_id="77")
    base.update(kw)
    return types.SimpleNamespace(**base)


def _install(*, src_date="2026-08-09T14:03:22.123Z", tgt_date="2026-08-09T10:00:00Z",
             push=None, cfg=None, state=None, absent=False):
    """Stub config_service + ps_api_service. Returns (store, recorder)."""
    store = {}
    if state is not None:
        store["k8s_token_sync_c-1"] = json.dumps(state)
    store.update(cfg or {})
    rec = {"pushes": 0, "push_kwargs": [], "reads": 0}

    m_cfg = types.ModuleType("web_dashboard.services.config_service")
    m_cfg.get = lambda key, workgroup=None: store.get(key, "")
    m_cfg.get_bool = lambda key, default=False: (
        store[key] not in ("0", "", "false") if key in store else default)
    m_cfg.set = lambda key, value, workgroup=None: store.__setitem__(key, value)
    m_cfg.delete = lambda key, workgroup=None: store.pop(key, None)
    sys.modules["web_dashboard.services.config_service"] = m_cfg

    m_ps = types.ModuleType("web_dashboard.services.ps_api_service")
    m_ps.configured = lambda: True
    dates = {"src": src_date, "tgt": tgt_date}

    async def _states(ids):
        rec["reads"] += 1
        if absent:
            return {"201": {}, "202": {}}
        return {
            "201": {"account_id": "201", "last_change_date": dates["src"],
                    "platform_id": "1008", "system_id": "77"},
            "202": {"account_id": "202", "last_change_date": dates["tgt"],
                    "platform_id": "1010", "system_id": "78"},
        }
    m_ps.get_managed_account_states = _states

    async def _push(**kw):
        rec["pushes"] += 1
        rec["push_kwargs"].append(kw)
        if push == "fail":
            raise RuntimeError("Password Safe refused the credential request (403). "
                               "The API identity needs the Requestor role")
        # A real push runs the target's plugin, which stamps its LastChangeDate — that
        # movement is what the next pass reads as the receipt.
        if push != "no_pra_write":
            dates["tgt"] = f"2026-08-09T15:0{rec['pushes']}:00Z"
        if push == "rotate_between":
            # Simulate Password Safe rotating between the pre-checkout read and now.
            dates["src"] = "2026-08-09T14:59:59.999Z"
        return {"sha256": "abc123def456", "pushed": True, "skipped": ""}
    m_ps.rotate_pra_vault_token = _push
    sys.modules["web_dashboard.services.ps_api_service"] = m_ps

    pkg_api = types.ModuleType("web_dashboard.api")
    pkg_api.__path__ = []
    m_ws = types.ModuleType("web_dashboard.api.websocket")
    async def _bp(job_id, pct, msg, log_line=None):
        rec.setdefault("logs", []).append((pct, msg, log_line))
    m_ws.broadcast_progress = _bp
    sys.modules["web_dashboard.api"] = pkg_api
    sys.modules["web_dashboard.api.websocket"] = m_ws
    return store, rec, dates


def _state(store):
    return json.loads(store.get("k8s_token_sync_c-1") or "{}")


# ── the change-date trigger ──────────────────────────────────────────────────────

def test_an_unchanged_change_date_costs_no_push():
    store, rec, _ = _install(state={"synced_change": "2026-08-09T14:03:22.123Z",
                                    "token_sha256": "abc123def456"})
    out = asyncio.run(sync.sync_cluster(FakeDB(_row()), "c-1"))
    assert out["changed"] is False and rec["pushes"] == 0
    assert _state(store)["state"] == "ok"


def test_a_changed_change_date_pushes():
    store, rec, _ = _install(state={"synced_change": "2026-08-01T00:00:00Z"})
    out = asyncio.run(sync.sync_cluster(FakeDB(_row()), "c-1"))
    assert out["pushed"] is True and rec["pushes"] == 1
    assert _state(store)["synced_change"] == "2026-08-09T14:03:22.123Z"


def test_a_first_ever_sync_pushes_unconditionally():
    # PRA's value came from the provision-time mint; Password Safe may already have
    # rotated. Assuming "in sync" would leave a dead token with no signal.
    store, rec, _ = _install(state=None)
    out = asyncio.run(sync.sync_cluster(FakeDB(_row()), "c-1"))
    assert out["pushed"] is True and rec["pushes"] == 1


def test_the_change_date_is_compared_as_an_opaque_string():
    # Tenants emit both "…123Z" and "…+00:00"; a parse that fails either never fires or
    # fires every pass. Same instant, different text ⇒ treated as changed, not equal.
    store, rec, _ = _install(src_date="2026-08-09T14:03:22.123+00:00",
                             state={"synced_change": "2026-08-09T14:03:22.123Z"})
    out = asyncio.run(sync.sync_cluster(FakeDB(_row()), "c-1"))
    assert out["pushed"] is True


def test_force_pushes_even_when_nothing_changed():
    store, rec, _ = _install(state={"synced_change": "2026-08-09T14:03:22.123Z"})
    out = asyncio.run(sync.sync_cluster(FakeDB(_row()), "c-1", force=True))
    assert out["pushed"] is True


# ── THE ordering invariant ───────────────────────────────────────────────────────

def test_the_watermark_records_the_pre_checkout_date():
    """A rotation landing between the read and the push must NOT be recorded as synced.

    Recording the later date would mean 'PRA holds token B' when PRA holds token A, and
    every subsequent pass would agree nothing changed — a permanent desync. Recording the
    pre-checkout date leaves the next pass to notice the difference and re-push."""
    store, rec, dates = _install(push="rotate_between",
                                 state={"synced_change": "2026-08-01T00:00:00Z"})
    asyncio.run(sync.sync_cluster(FakeDB(_row()), "c-1"))
    assert dates["src"] == "2026-08-09T14:59:59.999Z", "the fake should have moved the date"
    assert _state(store)["synced_change"] == "2026-08-09T14:03:22.123Z", (
        "the recorded watermark must be the value read BEFORE the checkout")

    # And the next pass therefore re-syncs rather than believing it is up to date.
    out = asyncio.run(sync.sync_cluster(FakeDB(_row()), "c-1"))
    assert out.get("pushed") is True


# ── failures never advance the watermark ─────────────────────────────────────────

def test_a_failed_push_keeps_the_old_watermark_and_backs_off():
    store, rec, _ = _install(push="fail", state={"synced_change": "2026-08-01T00:00:00Z"})
    out = asyncio.run(sync.sync_cluster(FakeDB(_row()), "c-1"))
    st = _state(store)
    assert out["state"] == "error"
    assert st["synced_change"] == "2026-08-01T00:00:00Z", (
        "advancing on failure is the difference between retrying and silently stale")
    assert st["fail_count"] == 1 and st["next_attempt_at"]
    assert "Requestor" in st["error"], "the tenant-side grant must reach the operator"


def test_repeated_failures_park_the_cluster():
    store, rec, _ = _install(push="fail",
                             cfg={"k8s_token_sync_max_failures": "2"},
                             state={"synced_change": "x", "fail_count": 1})
    out = asyncio.run(sync.sync_cluster(FakeDB(_row()), "c-1"))
    st = _state(store)
    assert out["stopped"] is True and st["next_attempt_at"] is None, (
        "a 403 from a missing Smart Rule never fixes itself — parking it keeps the "
        "audit log and the job list readable")


def test_a_backing_off_cluster_is_skipped_but_force_overrides():
    future = (datetime.utcnow() + timedelta(hours=1)).isoformat()
    store, rec, _ = _install(state={"synced_change": "x", "next_attempt_at": future})
    out = asyncio.run(sync.sync_cluster(FakeDB(_row()), "c-1"))
    assert out["skipped"] == "backing off" and rec["pushes"] == 0
    out = asyncio.run(sync.sync_cluster(FakeDB(_row()), "c-1", force=True))
    assert out["pushed"] is True


def test_a_transport_failure_propagates_instead_of_charging_backoff():
    store, rec, _ = _install()
    m_ps = sys.modules["web_dashboard.services.ps_api_service"]

    async def _boom(ids):
        raise RuntimeError("OAuth token request failed (503)")
    m_ps.get_managed_account_states = _boom
    try:
        asyncio.run(sync.sync_cluster(FakeDB(_row()), "c-1"))
    except RuntimeError:
        pass
    else:
        raise AssertionError("Password Safe being down must fail the pass, not the cluster")
    assert "fail_count" not in _state(store)


# ── a deleted account is sticky, not backed off ──────────────────────────────────

def test_a_deleted_managed_account_parks_without_clearing_the_binding():
    row = _row()
    store, rec, _ = _install(absent=True, state={"synced_change": "x"})
    out = asyncio.run(sync.sync_cluster(FakeDB(row), "c-1"))
    assert out["state"] == "unregistered"
    assert _state(store)["next_attempt_at"] is None
    assert "fail_count" not in _state(store) or not _state(store)["fail_count"]
    assert row.ps_token_account_id == "201", (
        "a permissions blip presenting as 404 must not erase the operator's binding")


# ── the previous push is verified for free on the next pass ──────────────────────

def test_a_target_change_date_that_moved_verifies_the_previous_push():
    store, rec, _ = _install(tgt_date="2026-08-09T14:05:00Z",
                             state={"synced_change": "2026-08-09T14:03:22.123Z",
                                    "pra_verified": "pending",
                                    "tgt_change_at_push": "2026-08-09T10:00:00Z"})
    asyncio.run(sync.sync_cluster(FakeDB(_row()), "c-1"))
    assert _state(store)["pra_verified"] == "yes"


def test_a_target_change_date_that_did_not_move_reports_the_pra_write_failed():
    store, rec, _ = _install(push="no_pra_write", tgt_date="2026-08-09T10:00:00Z",
                             state={"synced_change": "2026-08-09T14:03:22.123Z",
                                    "pra_verified": "pending",
                                    "tgt_change_at_push": "2026-08-09T10:00:00Z"})
    out = asyncio.run(sync.sync_cluster(FakeDB(_row()), "c-1"))
    st = _state(store)
    assert out["pra_verified"] == "no" and st["state"] == "error"
    assert "change log" in st["error"], "point the operator at Password Safe's own record"


def test_a_fresh_push_is_recorded_pending_not_verified():
    # Password Safe QUEUES change operations, so "accepted, not yet reflected" is normal.
    store, rec, _ = _install(state={"synced_change": "2026-08-01T00:00:00Z"})
    asyncio.run(sync.sync_cluster(FakeDB(_row()), "c-1"))
    st = _state(store)
    assert st["pra_verified"] == "pending"
    assert st["tgt_change_at_push"] == "2026-08-09T10:00:00Z"


# ── the rotate-on-release circuit breaker ────────────────────────────────────────

def test_the_hourly_cap_trips_and_names_the_likely_cause():
    store, rec, _ = _install(
        cfg={"k8s_token_sync_max_per_hour": "2"},
        state={"synced_change": "old", "rate_count": 2,
               "rate_window_start": datetime.utcnow().isoformat()})
    out = asyncio.run(sync.sync_cluster(FakeDB(_row()), "c-1"))
    assert rec["pushes"] == 0 and out["state"] == "error"
    assert "Change Password After Release" in out["error"], (
        "an endless rotate→sync→rotate loop has one likely cause — name it")


def test_the_rate_window_expires_after_an_hour():
    old = (datetime.utcnow() - timedelta(hours=2)).isoformat()
    store, rec, _ = _install(cfg={"k8s_token_sync_max_per_hour": "2"},
                             state={"synced_change": "old", "rate_count": 5,
                                    "rate_window_start": old})
    out = asyncio.run(sync.sync_cluster(FakeDB(_row()), "c-1"))
    assert out.get("pushed") is True and _state(store)["rate_count"] == 1


# ── the push targets the right platform ──────────────────────────────────────────

def test_the_push_names_the_expected_target_platform():
    store, rec, _ = _install(state=None,
                             cfg={"k8s_ps_pravault_token_platform": "PRA Vault Token"})
    asyncio.run(sync.sync_cluster(FakeDB(_row()), "c-1"))
    kw = rec["push_kwargs"][0]
    assert kw["expect_target_platform"] == "PRA Vault Token", (
        "writing a k8s bearer token to an account on another plugin's platform is the "
        "one failure that puts a secret somewhere it does not belong")
    assert kw["source_account_id"] == 201 and kw["target_account_id"] == 202


# ── nothing leaks ────────────────────────────────────────────────────────────────

def test_neither_the_token_nor_a_full_digest_reaches_the_result_or_the_logs():
    store, rec, _ = _install(state=None)
    _JOBS.clear()
    asyncio.run(sync.run(FakeDB(_row()), job_id="j-1", meta={}))
    completed = [r for kind, r in _JOBS if kind == "completed"]
    blob = json.dumps(completed)
    assert TOKEN not in blob
    for segment in TOKEN.split("."):
        assert segment not in blob
    # The digest is a 12-hex prefix, never the whole hash.
    assert '"sha256": "abc123def456"' in blob
    assert len("abc123def456") == 12
    for _pct, msg, line in rec.get("logs", []):
        assert TOKEN not in str(msg) and TOKEN not in str(line or "")


# ── scope + pass mechanics ───────────────────────────────────────────────────────

def test_an_unregistered_cluster_is_out_of_scope_not_an_error():
    store, rec, _ = _install()
    out = asyncio.run(sync.sync_cluster(FakeDB(_row(ps_pra_vault_account_id=None)), "c-1"))
    assert out["skipped"] == "not registered" and rec["pushes"] == 0


def test_the_pass_reports_counts_and_skips_when_disabled():
    store, rec, _ = _install(cfg={"k8s_token_sync_enabled": "0"}, state=None)
    out = asyncio.run(sync.sync_once(FakeDB(_row())))
    assert "skipped" in out and out["scanned"] == 0

    store, rec, _ = _install(state=None)
    out = asyncio.run(sync.sync_once(FakeDB(_row())))
    assert out["scanned"] == 1 and out["pushed"] == 1 and out["failed"] == 0


def test_the_interval_is_floored_at_five_minutes():
    store, rec, _ = _install(cfg={"k8s_token_sync_interval_minutes": "1"})
    assert sync.sweep_interval_seconds() == 300


def test_enqueue_refuses_while_a_pass_is_active_and_when_disabled():
    store, rec, _ = _install(cfg={"k8s_token_sync_enabled": "0"})
    assert sync.enqueue_sweep_if_due(FakeDB(None)) is None

    store, rec, _ = _install()
    # FakeDB.query(...).first() returns the row, which stands in for "an active job
    # exists" for the Job query — so the guard must refuse.
    assert sync.enqueue_sweep_if_due(FakeDB(_row())) is None


if __name__ == "__main__":
    fns = [v for name, v in sorted(globals().items()) if name.startswith("test_")]
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
