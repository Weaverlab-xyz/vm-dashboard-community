"""Behavioural tests for the auto-delete sweep's destructive half.

This is the file that has to be right. Everything it asserts is a way the feature could
destroy the wrong thing, destroy the right thing twice, or fail to stop when told:

  * a pass that isn't fully armed enqueues NOTHING, however overdue the fleet is;
  * the job it enqueues is byte-for-byte the one the cloud's own DELETE endpoint creates
     — same job_type, same metadata keys — because a divergent reimplementation is how a
     destroy ends up pointed at the wrong region or project;
  * at-most-once: a second sweep immediately after must enqueue nothing;
  * one resource that refuses or errors is counted, not fatal;
  * the per-pass cap holds, and bounds the damage rate.

Fakes are hand-rolled rather than mocked, in the style of tests/test_cloud_run_job_reaper.py
— a fake that records what was created is the only way to assert the *shape* of a job row
without a live DB.

Runs under pytest, or standalone:  python tests/test_expiry_reaper.py
"""
import os
import sys
import types
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONF = {}


class _Col:
    """One column of a stub model, inside a filter expression.

    Every operator returns a plain True: the fake sessions below never interpret a filter
    — each test scripts the query's *result* instead — so a column only has to survive
    being compared. Assertions are about the reaper's decisions, not about SQLAlchemy.
    """
    def __eq__(self, other): return True
    def __ne__(self, other): return True
    def __lt__(self, other): return True
    def __le__(self, other): return True
    def __gt__(self, other): return True
    def __ge__(self, other): return True
    def __hash__(self): return id(self)
    def in_(self, *a, **k): return True
    def notin_(self, *a, **k): return True


class _Model(type):
    """Metaclass making *any* attribute of a stub model a column.

    Deliberately open rather than a fixed list of columns. expiry_reaper builds its filters
    inside functions that catch broad exceptions on purpose (a sweep must never take the app
    down), so a column this stub had not been told about would raise AttributeError *inside*
    that handler — where it reads as a clean "nothing to enqueue" and the test passes for
    exactly the wrong reason. Being open means a new filter can never fail that way.
    """
    def __getattr__(cls, name):
        return _Col()


def _install_stubs():
    sa = types.ModuleType("sqlalchemy")
    sa.__path__ = []
    sa.text = lambda s: s
    # job_service.list_jobs builds `~and_(...)`; only construction + inversion matter here.
    sa.and_ = lambda *a, **k: type("_Expr", (), {"__invert__": lambda s: s})()
    sa_orm = types.ModuleType("sqlalchemy.orm")
    sa_orm.Session = type("Session", (), {})
    sa.orm = sa_orm
    sa_exc = types.ModuleType("sqlalchemy.exc")
    sa_exc.IntegrityError = type("IntegrityError", (Exception,), {})
    sa.exc = sa_exc
    sys.modules.setdefault("sqlalchemy", sa)
    sys.modules.setdefault("sqlalchemy.orm", sa_orm)
    sys.modules.setdefault("sqlalchemy.exc", sa_exc)

    db = types.ModuleType("web_dashboard.database")
    # NotificationDelivery/Endpoint are here because the reaper reaches
    # notification_service to queue its expiring/reaped messages. Without them that
    # import raises, the emit is swallowed by _notify's guard, and the notification
    # path in these tests silently does nothing.
    for name in ("CloudDatabase", "Job", "K8sCluster", "VirtualDesktop", "User",
                 "AuditLog", "JobLog", "NotificationDelivery", "NotificationEndpoint"):
        setattr(db, name, _Model(name, (), {}))
    db.get_db = lambda: None
    db._is_sqlite = True
    sys.modules.setdefault("web_dashboard.database", db)

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key, default="", workgroup=None: CONF.get(key, default)
    cfg.get_bool = lambda key, default=False: (
        str(CONF.get(key, default)).strip().lower() in ("1", "true", "yes", "on")
    )
    cfg.set = lambda key, value: CONF.__setitem__(key, value)
    sys.modules.setdefault("web_dashboard.services.config_service", cfg)

    conf_mod = types.ModuleType("web_dashboard.config")
    conf_mod.settings = types.SimpleNamespace()
    sys.modules.setdefault("web_dashboard.config", conf_mod)


_install_stubs()
try:
    from web_dashboard.services import expiry_policy as pol, expiry_reaper as reaper
except Exception as exc:  # pragma: no cover
    try:
        import pytest
        pytest.skip(f"expiry_reaper import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


NOW = datetime(2026, 7, 29, 12, 0, 0)
HOUR = 3600


# ── fakes ────────────────────────────────────────────────────────────────────

class _FakeJob:
    """A deploy Job row. `metadata_dict` is a real property pair, like the ORM's."""
    def __init__(self, jid, job_type, meta, expires_at=None, workgroup="sandbox"):
        self.id = jid
        self.job_type = job_type
        self.workgroup = workgroup
        self.expires_at = expires_at
        self._meta = dict(meta)

    @property
    def metadata_dict(self):
        return dict(self._meta)

    @metadata_dict.setter
    def metadata_dict(self, v):
        self._meta = dict(v)


class _FakeRow:
    def __init__(self, rid, expires_at=None):
        self.id = rid
        self.expires_at = expires_at


class _FakeDB:
    """Records created jobs so tests can assert their exact shape.

    `query(Model).filter(...).first()` is resolved from `by_id`, which is the only lookup
    the reaper does — it never scans.
    """
    def __init__(self, by_id=None):
        self.by_id = by_id or {}
        self.created = []          # [(job_type, metadata, created_by, workgroup)]
        self.commits = 0
        self.rollbacks = 0
        self._pending_id = None

    # query chain
    def query(self, *a, **k):
        return self

    def filter(self, *a, **k):
        return self

    def first(self):
        # Overridden by _patch, which installs a query() that resolves the single row each
        # test has in flight. Present so the class is usable standalone.
        return None

    def all(self):
        return []

    def count(self):
        return 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def execute(self, *a, **k):
        return self


def _patch(db, targets_by_id, *, create_fails_for=(), decommission_fails_for=()):
    """Wire the reaper's DB touchpoints to the fake. Returns nothing; mutates modules.

    Patching at the reaper's seams (its row lookup and job creation) rather than faking a
    whole Session keeps the assertions about the reaper's decisions, not about SQLAlchemy.
    """
    from web_dashboard.services import job_service

    def _create_job(_db, job_type=None, created_by=None, vm_path=None, workgroup=None,
                    metadata=None, batch_id=None, status="pending", expires_at=None):
        if job_type in create_fails_for:
            raise RuntimeError(f"simulated failure creating {job_type}")
        db.created.append((job_type, dict(metadata or {}), created_by, workgroup))
        return types.SimpleNamespace(id=f"destroy-{len(db.created)}")

    job_service.create_job = _create_job
    job_service.log_audit = lambda *a, **k: None
    job_service.append_job_log = lambda *a, **k: None
    job_service.update_progress = lambda *a, **k: None
    job_service.set_completed = lambda *a, **k: None
    job_service.set_failed = lambda *a, **k: None

    # The reaper looks its deploy job up through db.query(Job).filter(...).first(); the
    # fake resolves that from `targets_by_id` keyed on whatever id it was asked for.
    class _Q:
        def __init__(self, store):
            self.store = store
            self.wanted = None

        def filter(self, *a, **k):
            return self

        def first(self):
            return self.store

        def all(self):
            return []

    def _query(model):
        # Single-row store: every test has exactly one resource in flight.
        only = next(iter(targets_by_id.values()), None)
        return _Q(only)

    db.query = _query

    fake_cds = types.ModuleType("web_dashboard.services.cloud_database_service")
    def _db_decom(_db, rid, created_by=""):
        if "database" in decommission_fails_for:
            raise RuntimeError("simulated clouddb decommission failure")
        db.created.append(("clouddb_decommission", {"db_id": rid}, created_by, None))
        return {"ok": True, "db_id": rid, "job_id": f"destroy-{len(db.created)}"}
    fake_cds.start_decommission = _db_decom
    sys.modules["web_dashboard.services.cloud_database_service"] = fake_cds

    fake_k8s = types.ModuleType("web_dashboard.services.k8s_service")
    def _k8s_decom(_db, rid, created_by=""):
        if "k8s" in decommission_fails_for:
            raise RuntimeError("simulated k8s decommission failure")
        db.created.append(("k8s_decommission", {"cluster_id": rid}, created_by, None))
        return {"ok": True, "cluster_id": rid, "job_id": f"destroy-{len(db.created)}"}
    fake_k8s.start_decommission = _k8s_decom
    sys.modules["web_dashboard.services.k8s_service"] = fake_k8s


def _armed(**over):
    """Feature on, both arming clocks long elapsed, deletion live."""
    CONF.clear()
    CONF.update({
        "resource_expiry_enabled": "1",
        "resource_expiry_enforce": "1",
        "resource_expiry_dry_run": "0",
        "resource_expiry_default_hours": "24",
        "resource_expiry_armed_at": (NOW - timedelta(hours=5)).isoformat(),
        "resource_expiry_enforce_since": (NOW - timedelta(hours=5)).isoformat(),
    })
    CONF.update({k: str(v) for k, v in over.items()})


def _target(**over):
    base = {"id": "job:d1", "kind": "vm", "cloud": "aws", "name": "vm-1",
            "region": "us-east-1", "job_id": "d1",
            "expires_at": (NOW - timedelta(hours=3)).isoformat(),
            "overdue_s": 3 * HOUR}
    base.update(over)
    return base


# ── the job the reaper enqueues must match the endpoint's ────────────────────

def test_an_ec2_reap_matches_the_aws_delete_endpoint():
    """api/aws.py::destroy_instance creates ec2_destroy with exactly these three keys."""
    _armed()
    dep = _FakeJob("d1", "ec2_deploy",
                   {"instance_id": "i-abc", "region": "us-east-2", "instance_name": "vm-1"},
                   expires_at=NOW - timedelta(hours=3))
    db = _FakeDB()
    _patch(db, {"d1": dep})
    out = reaper._reap_one(db, _target())
    assert "error" not in out, out
    jt, meta, actor, wg = db.created[0]
    assert jt == "ec2_destroy"
    assert meta == {"instance_id": "i-abc", "deploy_job_id": "d1", "region": "us-east-2"}
    assert actor == reaper.REAPER_ACTOR and wg == "sandbox"


def test_an_azure_reap_matches_the_azure_delete_endpoint():
    _armed()
    dep = _FakeJob("d1", "azure_deploy",
                   {"vm_name": "az-1", "resource_group": "rg-prod", "location": "eastus"})
    db = _FakeDB()
    _patch(db, {"d1": dep})
    reaper._reap_one(db, _target(cloud="azure"))
    jt, meta, _, _ = db.created[0]
    assert jt == "azure_destroy"
    assert meta == {"vm_name": "az-1", "deploy_job_id": "d1", "resource_group": "rg-prod"}


def test_a_gce_reap_matches_the_gcp_delete_endpoint():
    _armed()
    dep = _FakeJob("d1", "gce_deploy",
                   {"instance_name": "gce-1", "zone": "us-east1-b", "project_id": "proj-x"})
    db = _FakeDB()
    _patch(db, {"d1": dep})
    reaper._reap_one(db, _target(cloud="gcp"))
    jt, meta, _, _ = db.created[0]
    assert jt == "gce_destroy"
    assert meta == {"instance_name": "gce-1", "deploy_job_id": "d1",
                    "zone": "us-east1-b", "project_id": "proj-x"}


def test_an_oci_reap_matches_the_oci_delete_endpoint():
    _armed()
    dep = _FakeJob("d1", "oci_deploy", {"instance_ocid": "ocid1.instance.oc1..xyz"})
    db = _FakeDB()
    _patch(db, {"d1": dep})
    reaper._reap_one(db, _target(cloud="oci"))
    jt, meta, _, _ = db.created[0]
    assert jt == "oci_destroy"
    assert meta == {"instance_ocid": "ocid1.instance.oc1..xyz", "deploy_job_id": "d1"}


def test_a_database_and_cluster_reap_go_through_start_decommission():
    """Asserting the FUNCTION called, not a re-derived job row: a reimplementation that
    skipped start_decommission would also skip the PRA tunnel + vault + Password Safe
    teardown it performs, and leak exactly what this feature exists to stop leaking."""
    _armed()
    db = _FakeDB()
    _patch(db, {"x": _FakeRow("x")})
    reaper._reap_one(db, _target(id="clouddb:x", kind="database", cloud="aws"))
    assert db.created[-1][0] == "clouddb_decommission"
    assert db.created[-1][1] == {"db_id": "x"}
    reaper._reap_one(db, _target(id="k8s:x", kind="k8s", cloud="gcp"))
    assert db.created[-1][0] == "k8s_decommission"
    assert db.created[-1][1] == {"cluster_id": "x"}


# ── refusals: a missing key must never become a guessed default ──────────────

def test_a_deploy_job_missing_its_region_is_refused_not_guessed():
    """A VM deployed before multi-region support recorded no region. Falling back to the
    configured default could aim the destroy at the wrong region; refusing surfaces it for
    a manual delete instead."""
    _armed()
    stamped = NOW - timedelta(hours=3)
    dep = _FakeJob("d1", "ec2_deploy", {"instance_id": "i-abc"},     # no region
                   expires_at=stamped)
    db = _FakeDB()
    _patch(db, {"d1": dep})
    out = reaper._reap_one(db, _target())
    assert db.created == [], "a destroy was enqueued without a recorded region"
    assert "region" in out["error"]
    # A refusal must LEAVE THE TIMER IN PLACE. Clearing it would silently turn the
    # resource into "never expires" and drop it out of the report — so the one resource
    # the operator most needs to know about would be the one that stopped being mentioned.
    assert dep.expires_at == stamped, "a refused reap cleared the expiry"


def test_a_deploy_job_missing_its_resource_id_is_refused():
    _armed()
    for jt, meta in (("ec2_deploy", {"region": "us-east-1"}),
                     ("azure_deploy", {"resource_group": "rg"}),
                     ("gce_deploy", {"zone": "z", "project_id": "p"}),
                     ("oci_deploy", {})):
        db = _FakeDB()
        _patch(db, {"d1": _FakeJob("d1", jt, meta)})
        out = reaper._reap_one(db, _target())
        assert db.created == [], f"{jt}: enqueued a destroy with no resource id"
        assert "error" in out, jt


def test_an_already_destroyed_deploy_job_is_refused():
    """The `destroyed` marker is the existing is-it-alive predicate; a reap must respect
    it even if a stale expires_at survived."""
    _armed()
    dep = _FakeJob("d1", "ec2_deploy",
                   {"instance_id": "i-abc", "region": "us-east-1", "destroyed": True})
    db = _FakeDB()
    _patch(db, {"d1": dep})
    out = reaper._reap_one(db, _target())
    assert db.created == [] and "already destroyed" in out["error"]


# ── at-most-once ─────────────────────────────────────────────────────────────

def test_a_reap_clears_the_expiry_so_a_second_pass_cannot_repeat_it():
    """The whole idempotency story. Clearing expires_at removes the resource from
    expired()'s view, so no second destroy can be enqueued — no new column, no
    extra_data filtering."""
    _armed()
    dep = _FakeJob("d1", "ec2_deploy", {"instance_id": "i-abc", "region": "us-east-1"},
                   expires_at=NOW - timedelta(hours=3))
    db = _FakeDB()
    _patch(db, {"d1": dep})
    reaper._reap_one(db, _target())
    assert len(db.created) == 1
    assert dep.expires_at is None, "expires_at survived the reap — a second pass would repeat it"
    assert dep.metadata_dict["expiry_reap_job_id"] == "destroy-1"
    assert dep.metadata_dict["expiry_reaped_at"]
    assert db.commits >= 1


def test_a_row_reap_clears_the_expiry_too():
    _armed()
    row = _FakeRow("x", expires_at=NOW - timedelta(hours=3))
    db = _FakeDB()
    _patch(db, {"x": row})
    reaper._reap_one(db, _target(id="clouddb:x", kind="database"))
    assert row.expires_at is None


# ── one failure must not abandon the pass ────────────────────────────────────

def test_a_failed_enqueue_is_reported_not_raised():
    _armed()
    db = _FakeDB()
    _patch(db, {"d1": _FakeJob("d1", "ec2_deploy",
                               {"instance_id": "i-abc", "region": "us-east-1"})},
           create_fails_for=("ec2_destroy",))
    out = reaper._reap_one(db, _target())
    assert "error" in out and "simulated" in out["error"]
    assert db.rollbacks >= 1


def test_a_failed_decommission_is_reported_not_raised():
    _armed()
    db = _FakeDB()
    _patch(db, {"x": _FakeRow("x")}, decommission_fails_for=("database",))
    out = reaper._reap_one(db, _target(id="clouddb:x", kind="database"))
    assert "error" in out


def test_a_missing_deploy_job_is_reported():
    _armed()
    db = _FakeDB()
    _patch(db, {})                                     # nothing resolves
    out = reaper._reap_one(db, _target())
    assert db.created == [] and "error" in out


# ── the arming gates ─────────────────────────────────────────────────────────

def test_deletion_is_inert_until_both_flags_are_set():
    _armed(resource_expiry_dry_run=1)
    since = (NOW - timedelta(hours=5)).timestamp()
    assert pol.deletion_active(since, NOW.timestamp()) is False, "dry-run did not block"
    _armed(resource_expiry_enforce=0)
    assert pol.deletion_active(since, NOW.timestamp()) is False, "enforce=0 did not block"
    _armed()
    assert pol.deletion_active(since, NOW.timestamp()) is True


def test_deletion_waits_out_its_own_arming_delay():
    """The backlog guard. An operator watches in report-only for a week, 40 resources pass
    their expiry, then they uncheck one box — this is what stops all 40 becoming eligible
    in that instant."""
    _armed()
    now = NOW.timestamp()
    assert pol.deletion_active(None, now) is False, "no enforce_since means not armed"
    assert pol.deletion_active(now - 60, now) is False, "acted 1 minute after enabling"
    assert pol.deletion_active(now - (pol.ARM_DELAY_MINUTES - 1) * 60, now) is False
    assert pol.deletion_active(now - pol.ARM_DELAY_MINUTES * 60, now) is True


def test_withdrawing_enforcement_clears_the_clock():
    """Turning report-only back on withdraws consent, so turning it off again must restart
    the full delay rather than resuming a clock that ran while the feature was inert."""
    _armed()
    db = _FakeDB()
    _patch(db, {})
    assert reaper._note_enforcement(db, NOW) is not None
    CONF["resource_expiry_dry_run"] = "1"
    assert reaper._note_enforcement(db, NOW) is None
    assert not CONF.get("resource_expiry_enforce_since")
    # Re-enabling starts a fresh clock from now, so nothing is immediately eligible.
    CONF["resource_expiry_dry_run"] = "0"
    started = reaper._note_enforcement(db, NOW)
    assert started == NOW
    assert pol.deletion_active(started.timestamp(), NOW.timestamp()) is False


def test_a_first_pass_records_enforcement_without_acting():
    _armed()
    CONF.pop("resource_expiry_enforce_since", None)
    db = _FakeDB()
    _patch(db, {})
    since = reaper._note_enforcement(db, NOW)
    assert since == NOW
    assert CONF["resource_expiry_enforce_since"] == NOW.isoformat()
    assert pol.deletion_active(since.timestamp(), NOW.timestamp()) is False


# ── the cap ──────────────────────────────────────────────────────────────────

def test_max_per_pass_bounds_the_damage_rate():
    """Even with every brake released, the blast radius per pass is bounded — and every
    reap lands on /jobs and in the audit log, so there is time and signal to intervene."""
    _armed(resource_expiry_max_per_pass=3)
    assert pol.max_per_pass() == 3
    _armed(resource_expiry_max_per_pass=10**6)
    assert pol.max_per_pass() == pol.MAX_PER_PASS_CEILING


# ── the auto-delete warning (resource.expiring) ──────────────────────────────
#
# _notify is stubbed here: it only builds an event and hands it to the outbox, and the
# outbox has its own tests. What matters in the reaper is the LATCH — warn once, and
# only burn the latch if something was actually queued.

class _WarnRow:
    """A row carrying both halves of the timer: the deadline and the warned-once latch."""
    def __init__(self, rid, expires_at, warned_at=None):
        self.id = rid
        self.expires_at = expires_at
        self.expiry_warned_at = warned_at
        self.created_at = None


def _warn_setup(queued=1, *, warned_at=None, hours_left=6):
    """One warnable VM, `hours_left` from its expiry, with _notify recording calls."""
    expires = NOW + timedelta(hours=hours_left)
    row = _WarnRow("abc", expires, warned_at)
    db = _FakeDB()
    _patch(db, {"job:abc": row})
    sent = []

    def _fake_notify(_db, event_type, *, title, body, target, url="", fields=None,
                     dedupe_bucket=""):
        sent.append((event_type, target["id"]))
        return queued

    reaper._notify = _fake_notify
    item = {"id": "job:abc", "kind": "vm", "cloud": "aws", "name": "vm-1",
            "source": "provisioned", "state": "active", "workgroup": "sandbox",
            "region": "us-east-1", "job_id": "abc",
            "expires_at": expires.isoformat()}
    return db, row, item, sent


def test_a_resource_entering_the_window_is_warned_once():
    CONF.clear()
    CONF["resource_expiry_enabled"] = "1"
    db, row, item, sent = _warn_setup()

    assert reaper._warn_expiring(db, [item], now_ts=NOW.timestamp()) == 1
    assert sent == [("resource.expiring", "job:abc")]
    assert row.expiry_warned_at is not None, "the latch was not stamped"


def test_a_second_sweep_does_not_warn_again():
    """THE property. The sweep runs every 30 minutes; without the latch an operator
    would get the same warning 48 times before the resource was destroyed."""
    CONF.clear()
    CONF["resource_expiry_enabled"] = "1"
    db, row, item, sent = _warn_setup(warned_at=NOW)

    assert reaper._warn_expiring(db, [item], now_ts=NOW.timestamp()) == 0
    assert sent == [], "a resource with expiry_warned_at set was warned again"


def test_the_latch_is_not_burned_when_nothing_was_queued():
    """Notifications off, no endpoint configured, or storm-suppressed. The resource must
    stay eligible to warn later rather than being silently marked as told."""
    CONF.clear()
    CONF["resource_expiry_enabled"] = "1"
    db, row, item, sent = _warn_setup(queued=0)

    assert reaper._warn_expiring(db, [item], now_ts=NOW.timestamp()) == 0
    assert sent, "_notify should still have been offered the event"
    assert row.expiry_warned_at is None, (
        "the latch was burned even though nothing was queued — this resource would "
        "never be warned about again")


def test_a_resource_outside_the_window_is_not_warned():
    CONF.clear()
    CONF["resource_expiry_enabled"] = "1"
    # Default warn window is 24h; this one has 100h left.
    db, row, item, sent = _warn_setup(hours_left=100)

    assert reaper._warn_expiring(db, [item], now_ts=NOW.timestamp()) == 0
    assert sent == []
    assert row.expiry_warned_at is None


def test_something_already_past_expiry_is_the_reapers_business_not_the_warners():
    """"Expires soon" about a resource being destroyed right now is noise, and would
    also burn the latch on the way past."""
    CONF.clear()
    CONF["resource_expiry_enabled"] = "1"
    db, row, item, sent = _warn_setup(hours_left=-2)

    assert reaper._warn_expiring(db, [item], now_ts=NOW.timestamp()) == 0
    assert sent == []


def test_a_resource_with_no_timer_is_never_warned():
    """Same retroactivity guarantee the reaper has: every row predating the feature
    backfilled to NULL, so enabling notifications must not warn about all of them."""
    CONF.clear()
    CONF["resource_expiry_enabled"] = "1"
    db, row, item, sent = _warn_setup()
    item["expires_at"] = None

    assert reaper._warn_expiring(db, [item], now_ts=NOW.timestamp()) == 0
    assert sent == []


# ── the enqueue guard: exactly one row per tick ───────────────────────────────
#
# The regression these cover was found on the live install, not here: 5 of 55 sweep rows
# were duplicate pairs 0.13–0.4s apart. Both `gunicorn -w 2` loops tick together, and the
# active-pass check is a LIVENESS test — with an empty pass completing in ~0s and three
# worker replicas polling every 2s, the first row is often already `completed` before the
# second app worker looks. No unit test could have caught it, because each check is
# individually correct; only the pair is wrong.

class _EnqueueDB(_FakeDB):
    """Scripts the enqueue's lookups in order: active-pass check, then recency check.

    Consuming one scripted answer per ``.first()`` makes the NUMBER of lookups observable
    too, which is the only honest way to assert the force-sweep opt-out: skipping the
    recency check must mean "it never looked", not "it looked and found nothing".
    """
    def __init__(self, firsts=()):
        super().__init__()
        self._firsts = list(firsts)
        self.lookups = 0

    def query(self, *a, **k):
        return self

    def filter(self, *a, **k):
        return self

    def first(self):
        self.lookups += 1
        return self._firsts.pop(0) if self._firsts else None


def _enqueue_patch(db):
    """Record enqueued sweeps on the fake. Lighter than _patch, which also replaces
    db.query — here the session itself is the thing under test."""
    from web_dashboard.services import job_service

    def _create_job(_db, job_type=None, created_by=None, **kw):
        db.created.append((job_type, created_by))
        return types.SimpleNamespace(id=f"sweep-{len(db.created)}")

    job_service.create_job = _create_job


def test_an_idle_tick_enqueues_exactly_one_sweep():
    CONF.clear()
    CONF["resource_expiry_enabled"] = "1"
    db = _EnqueueDB()
    _enqueue_patch(db)

    job_id = reaper.enqueue_sweep_if_due(db)
    assert job_id == "sweep-1"
    assert db.created == [("expiry_sweep", "system")]
    assert db.lookups == 2, "both the active-pass and recency checks must run"


def test_a_pass_still_in_flight_blocks_a_second():
    """The original guard, unchanged: a queued/pending/running sweep is enough."""
    CONF.clear()
    CONF["resource_expiry_enabled"] = "1"
    db = _EnqueueDB(firsts=(("sweep-0",),))
    _enqueue_patch(db)

    assert reaper.enqueue_sweep_if_due(db) is None
    assert db.created == []
    assert db.lookups == 1, "an active pass should short-circuit before the recency check"


def test_a_pass_that_just_completed_blocks_the_duplicate_tick():
    """THE regression test. Nothing is active — the first row already finished — and the
    old guard therefore said "go", producing the second row seen live 0.4s after the
    first. The recency check is what refuses it."""
    CONF.clear()
    CONF["resource_expiry_enabled"] = "1"
    db = _EnqueueDB(firsts=(None, ("sweep-0",)))     # not active, but very recent
    _enqueue_patch(db)

    assert reaper.enqueue_sweep_if_due(db) is None
    assert db.created == [], "a duplicate sweep was enqueued for the same tick"
    assert db.lookups == 2


def test_the_operator_force_path_skips_the_recency_check():
    """api/expiry.py passes min_gap_seconds=0. A human who just pressed Run Sweep means
    now, and the window would otherwise refuse them for up to half an interval while
    reporting "already queued or running" — which would be false."""
    CONF.clear()
    CONF["resource_expiry_enabled"] = "1"
    db = _EnqueueDB(firsts=(None, ("sweep-0",)))     # a recent row it must ignore
    _enqueue_patch(db)

    assert reaper.enqueue_sweep_if_due(db, min_gap_seconds=0) == "sweep-1"
    assert db.lookups == 1, "the recency row must not even be consulted"


def test_the_force_path_still_respects_an_active_pass():
    """min_gap_seconds=0 waives the recency window and nothing else — two concurrent
    passes remain two destroy enqueues for the same resource."""
    CONF.clear()
    CONF["resource_expiry_enabled"] = "1"
    db = _EnqueueDB(firsts=(("sweep-0",),))
    _enqueue_patch(db)

    assert reaper.enqueue_sweep_if_due(db, min_gap_seconds=0) is None
    assert db.created == []


# ── sweep-history retention ───────────────────────────────────────────────────

class _PruneDB(_FakeDB):
    """Records the delete()s a prune issued, keyed by the model it was querying."""
    def __init__(self, ids=()):
        super().__init__()
        self._rows = [(i,) for i in ids]
        self.model = None
        self.deleted = {}
        self.limited = None
        self.fail_on_delete = False

    def query(self, model, *a, **k):
        self.model = getattr(model, "__name__", None)
        return self

    def filter(self, *a, **k):
        return self

    def limit(self, n):
        self.limited = n
        return self

    def all(self):
        return list(self._rows)

    def delete(self, *a, **k):
        if self.fail_on_delete:
            raise RuntimeError("db exploded mid-prune")
        self.deleted[self.model] = len(self._rows)
        return len(self._rows)


def test_a_prune_drops_the_rows_and_their_live_output():
    """job_logs carries no foreign key to jobs, so nothing cascades — a prune that
    forgot it would leave unreachable log rows keyed to a job id that no longer exists."""
    CONF.clear()
    CONF["resource_expiry_sweep_retention_days"] = "7"
    db = _PruneDB(ids=("old-1", "old-2"))

    assert reaper.prune_sweep_history(db) == 2
    assert db.deleted.get("JobLog") == 2, "Live Output was orphaned, not deleted"
    assert db.deleted.get("Job") == 2
    assert db.commits == 1


def test_a_prune_is_bounded_per_pass():
    """The first prune on a long-lived install could face thousands of rows; an unbounded
    delete would put that on the sweep's critical path."""
    CONF.clear()
    CONF["resource_expiry_sweep_retention_days"] = "7"
    db = _PruneDB(ids=("old-1",))
    reaper.prune_sweep_history(db)
    assert db.limited == reaper._PRUNE_BATCH


def test_retention_off_prunes_nothing_at_all():
    """0 = keep forever, and it must not even issue the SELECT."""
    CONF.clear()
    CONF["resource_expiry_sweep_retention_days"] = "0"
    db = _PruneDB(ids=("old-1",))

    assert reaper.prune_sweep_history(db) == 0
    assert db.model is None and db.deleted == {}


def test_nothing_past_the_window_is_a_clean_no_op():
    CONF.clear()
    CONF["resource_expiry_sweep_retention_days"] = "7"
    db = _PruneDB(ids=())

    assert reaper.prune_sweep_history(db) == 0
    assert db.deleted == {} and db.commits == 0


def test_a_failed_prune_rolls_back_and_does_not_raise():
    """A sweep that did its work must not be reported as failed because housekeeping
    afterwards hit the database wrong."""
    CONF.clear()
    CONF["resource_expiry_sweep_retention_days"] = "7"
    db = _PruneDB(ids=("old-1",))
    db.fail_on_delete = True

    assert reaper.prune_sweep_history(db) == 0
    assert db.rollbacks == 1 and db.commits == 0


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
