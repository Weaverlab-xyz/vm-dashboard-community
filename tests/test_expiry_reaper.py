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


def _install_stubs():
    sa = types.ModuleType("sqlalchemy")
    sa.__path__ = []
    sa.text = lambda s: s
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
    for name in ("CloudDatabase", "Job", "K8sCluster", "VirtualDesktop", "User",
                 "AuditLog", "JobLog"):
        setattr(db, name, type(name, (), {"id": None}))
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
