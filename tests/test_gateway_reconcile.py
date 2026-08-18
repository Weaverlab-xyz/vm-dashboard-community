"""The Gateways tab must agree with the cloud, and must never guess when it can't ask.

The bug this file exists to pin: `list_gateways` served the registry table verbatim. Every
writer in `gateway_service` records what the dashboard *did* — an ensure adopts a row as
`running`, a teardown marks it `deleted` — and several things remove a gateway host without
going through either: the Containers tab's own Stop button, an AWS idle teardown that found
the host already gone (it returned before the line that marks the row), a console deletion.
The row then read `running` forever, and the observed symptom was a Gateways tab reporting
two running gateways beside a Containers tab correctly reporting zero containers. It is not
only cosmetic: `live_egress_ips` reads the same rows to build the Rancher/Portainer node
firewall allow lists, so a dead host's /32 kept being re-applied while the gateway that
replaced it went unallowed.

The reconcile pass closes that, and the property everything rests on is the one a naive
implementation gets wrong: **a lookup that fails must leave the rows alone.** `missing` has
to mean "we looked and it wasn't there", never "we couldn't tell" — otherwise one throttled
API call retires live inventory and pulls a working gateway out of every firewall. Hence
`test_a_failed_lookup_changes_nothing` and `test_one_clouds_failure_does_not_stop_another`.

The two outcomes are asymmetric on purpose, because the two lifecycles are:
  * managed  + host gone → `deleted`. That is what its idle teardown would have written;
    that host coming and going with demand is normal, not an anomaly to report.
  * requested + host gone → `missing`. Nobody asked for it to go, so the row stays visible.

Uses a temporary SQLite file and the real ORM; the per-cloud cloud lookups are stubbed,
since what is under test is the decision made from their answers.

Runs under pytest, or standalone:  python tests/test_gateway_reconcile.py
"""
import asyncio
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="gw-reconcile-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-gateway-reconcile-tests")

try:
    from web_dashboard.database import Base, Gateway, SessionLocal, engine
    from web_dashboard.services import gateway_service as gs
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

Base.metadata.create_all(bind=engine)

LOOKUPS = []        # every stubbed per-cloud lookup, as a cloud name

# Most tests here stub `_live_hosts` wholesale, since what they are about is the decision
# made from its answer. The two that are about `_live_hosts` itself put the real one back
# and stub the layer under it instead.
_real_live_hosts = gs._live_hosts


def _run(coro):
    return asyncio.run(coro)


def _stub_clouds(answers):
    """Stub the per-cloud liveness lookup. ``answers`` maps cloud → either a
    ``{name: info}`` dict or an Exception instance to raise."""
    async def _live_hosts(cloud, rows):
        LOOKUPS.append(cloud)
        got = answers.get(cloud, {})
        if isinstance(got, BaseException):
            raise got
        return got
    gs._live_hosts = _live_hosts


def _reset(*rows):
    LOOKUPS.clear()
    db = SessionLocal()
    try:
        db.query(Gateway).delete()
        for r in rows:
            db.add(r)
        db.commit()
    finally:
        db.close()


def _gw(name, cloud="aws", status="running", managed=False, region="us-east-2",
        egress_ip="203.0.113.7", host_id="i-abc"):
    return Gateway(cloud=cloud, region=region, name=name, status=status,
                   managed=managed, host_id=host_id, egress_ip=egress_ip,
                   created_by="system" if managed else "admin")


def _reload(name):
    db = SessionLocal()
    try:
        return db.query(Gateway).filter(Gateway.name == name).first()
    finally:
        db.close()


def _reconcile(cloud=""):
    db = SessionLocal()
    try:
        _run(gs.reconcile(db, cloud))
    finally:
        db.close()


# ── the drift the tab actually showed ─────────────────────────────────────────

def test_a_requested_gateway_whose_host_is_gone_reads_missing():
    _reset(_gw("gw-us-east-2-01"))
    _stub_clouds({"aws": {}})          # the lookup succeeded and found nothing
    _reconcile()
    row = _reload("gw-us-east-2-01")
    assert row.status == "missing", row.status
    assert row.error and "no longer in the cloud" in row.error


def test_a_managed_gateway_whose_host_is_gone_reads_deleted():
    """Not `missing`: the managed host is reference-counted and removed when idle, so
    "gone" is the state its own teardown would have written. Reporting it as an anomaly
    would put a permanent warning row on the page for normal behaviour."""
    _reset(_gw("dashboard-sandbox-jumpoint-host", managed=True))
    _stub_clouds({"aws": {}})
    _reconcile()
    assert _reload("dashboard-sandbox-jumpoint-host").status == "deleted"


def test_a_gone_gateway_loses_its_egress_ip():
    """The /32 outlived the host it described, and `live_egress_ips` keeps re-applying it
    to every source-restricted node until something clears it."""
    _reset(_gw("gw-1"), _gw("mgd", managed=True))
    _stub_clouds({"aws": {}})
    _reconcile()
    assert _reload("gw-1").egress_ip is None
    assert _reload("mgd").egress_ip is None
    db = SessionLocal()
    try:
        assert gs.live_egress_ips(db, "aws") == []
    finally:
        db.close()


def test_a_missing_row_is_excluded_from_the_firewall_but_stays_on_the_page():
    """The two readers want different things from a `missing` row: the firewall must
    forget it, the operator must still see it."""
    _reset(_gw("gw-1", status="missing", egress_ip="198.51.100.4"))
    db = SessionLocal()
    try:
        assert gs.live_egress_ips(db, "aws") == []
        assert [g["name"] for g in gs.list_gateways(db)] == ["gw-1"]
    finally:
        db.close()


# ── the safety property: unverifiable is not gone ─────────────────────────────

def test_a_failed_lookup_changes_nothing():
    """The property the whole pass rests on. A throttled or unauthorized API call must not
    be able to retire a live gateway and strip its /32 from every node firewall."""
    _reset(_gw("gw-1"), _gw("mgd", managed=True))
    _stub_clouds({"aws": RuntimeError("Rate exceeded")})
    _reconcile()
    for name in ("gw-1", "mgd"):
        row = _reload(name)
        assert row.status == "running", f"{name} was downgraded on a failed lookup"
        assert row.egress_ip == "203.0.113.7", f"{name} lost its /32 on a failed lookup"


def test_one_clouds_failure_does_not_stop_another():
    _reset(_gw("aws-gw", cloud="aws"), _gw("gcp-gw", cloud="gcp", region="us-central1"))
    _stub_clouds({"aws": RuntimeError("Rate exceeded"), "gcp": {}})
    _reconcile()
    assert _reload("aws-gw").status == "running"
    assert _reload("gcp-gw").status == "missing"


def test_a_timeout_leaves_every_row_alone():
    """A cloud gone slow costs the tab a stale reading, not the request — and not the
    inventory."""
    async def _hang(cloud, rows):
        await asyncio.sleep(30)
        return {}
    _reset(_gw("gw-1"))
    gs._live_hosts = _hang
    original = gs._RECONCILE_TIMEOUT_S
    gs._RECONCILE_TIMEOUT_S = 0.05
    try:
        _reconcile()
    finally:
        gs._RECONCILE_TIMEOUT_S = original
    assert _reload("gw-1").status == "running"


def test_a_row_mid_flight_is_never_touched():
    """`provisioning` legitimately has no host yet and `deleting` is about to have none;
    verifying either would race the job that owns the row."""
    _reset(_gw("prov", status="provisioning"), _gw("del", status="deleting"),
           _gw("err", status="error"), _gw("gone", status="deleted"))
    _stub_clouds({"aws": {}})
    _reconcile()
    assert _reload("prov").status == "provisioning"
    assert _reload("del").status == "deleting"
    assert _reload("err").status == "error"
    assert _reload("gone").status == "deleted"
    assert LOOKUPS == [], "a cloud was queried for rows that are not verifiable"


def test_nothing_verifiable_queries_no_cloud_at_all():
    """A page load with no settled rows must not fan out to the cloud SDKs."""
    _reset(_gw("prov", status="provisioning"))
    _stub_clouds({"aws": {}})
    _reconcile()
    assert LOOKUPS == []


# ── host up, gateway down (the AWS-only split) ────────────────────────────────

def test_the_aws_task_check_is_asked_once_per_region():
    """The ECS calls behind it are cluster-wide, so one round trip has to answer for every
    gateway sharing that cluster — otherwise N gateways cost 2N calls inside a deadline."""
    from web_dashboard.services import jumpoint_host_service as jhs
    calls = []

    async def _live(cloud, targets):
        return {name: {"host_id": "i-" + name} for _, name in targets}

    async def _by_host(region, host_ids):
        calls.append((region, sorted(host_ids)))
        return {h: True for h in host_ids}

    real_live, real_by_host = jhs.live_gateway_hosts, jhs.gateway_tasks_by_host
    jhs.live_gateway_hosts, jhs.gateway_tasks_by_host = _live, _by_host
    gs._live_hosts = _real_live_hosts
    _reset(_gw("a", region="us-east-2"), _gw("b", region="us-east-2"),
           _gw("c", region="us-east-1"))
    try:
        _reconcile()
    finally:
        jhs.live_gateway_hosts, jhs.gateway_tasks_by_host = real_live, real_by_host
    assert sorted(calls) == [("us-east-1", ["i-c"]), ("us-east-2", ["i-a", "i-b"])], calls


def test_an_unanswerable_region_leaves_serving_unset():
    """`gateway_tasks_by_host` returns None when it cannot tell — a blank task family, an
    unreadable cluster. None must not read as "serving nothing"."""
    from web_dashboard.services import jumpoint_host_service as jhs

    async def _live(cloud, targets):
        return {name: {"host_id": "i-" + name} for _, name in targets}

    async def _by_host(region, host_ids):
        return None

    real_live, real_by_host = jhs.live_gateway_hosts, jhs.gateway_tasks_by_host
    jhs.live_gateway_hosts, jhs.gateway_tasks_by_host = _live, _by_host
    gs._live_hosts = _real_live_hosts
    _reset(_gw("a"))
    try:
        _reconcile()
    finally:
        jhs.live_gateway_hosts, jhs.gateway_tasks_by_host = real_live, real_by_host
    assert _reload("a").status == "running"


def test_a_host_with_no_gateway_task_reads_degraded():
    """On AWS the gateway is an ECS task on the container instance, and the host outlives
    the task dying — which is the other half of "running beside zero containers"."""
    _reset(_gw("gw-1"))
    _stub_clouds({"aws": {"gw-1": {"host_id": "i-abc", "serving": False}}})
    _reconcile()
    row = _reload("gw-1")
    assert row.status == "degraded", row.status
    assert row.error and "no gateway task" in row.error


def test_an_unknown_serving_state_never_downgrades():
    """`serving` is None wherever it can't be determined — GCP/Azure, or a blank ECS task
    family. Only a definite No downgrades a row."""
    for serving in (None, True):
        _reset(_gw("gw-1"))
        _stub_clouds({"aws": {"gw-1": {"host_id": "i-abc", "serving": serving}}})
        _reconcile()
        assert _reload("gw-1").status == "running", f"serving={serving!r} downgraded a row"


def test_a_stopped_host_still_counts_as_present():
    """Present, not missing: a stopped host still exists, still bills its volume, and can
    be started. Calling it gone is the one direction of error that loses information."""
    _reset(_gw("gw-1"))
    _stub_clouds({"aws": {"gw-1": {"host_id": "i-abc", "state": "stopped"}}})
    _reconcile()
    assert _reload("gw-1").status == "running"


# ── recovery ──────────────────────────────────────────────────────────────────

def test_a_gateway_that_comes_back_recovers():
    """`missing` is verifiable so the row can recover; otherwise a host recreated out of
    band would stay flagged until someone deleted the row by hand."""
    _reset(_gw("gw-1", status="missing", egress_ip=None))
    _stub_clouds({"aws": {"gw-1": {"host_id": "i-new", "serving": True}}})
    _reconcile()
    row = _reload("gw-1")
    assert row.status == "running"
    assert row.error is None
    assert row.host_id == "i-new", "the row kept a pointer to the host that went away"


def test_a_missing_gateway_keeps_its_name_reserved():
    """The name is unique per (cloud, name) in the registry, not just in the cloud —
    freeing it would let a redeploy add a second row under the same name, and
    record_egress_ip/mark_deleted both take the first match."""
    _reset(_gw("gw-1", status="missing"))
    db = SessionLocal()
    try:
        assert "gw-1" in gs.existing_names(db, "aws")
    finally:
        db.close()


def test_a_recreated_hosts_new_address_replaces_the_old_one():
    """A gateway host that was recreated keeps its NAME and takes a FRESH address, so a row
    that only learns an IP at ensure time hands the node firewalls a /32 admitting the host
    that went away."""
    _reset(_gw("gw-1", egress_ip="203.0.113.7"))
    _stub_clouds({"aws": {"gw-1": {"host_id": "i-abc", "egress_ip": "198.51.100.9"}}})
    _reconcile()
    assert _reload("gw-1").egress_ip == "198.51.100.9"
    db = SessionLocal()
    try:
        assert gs.live_egress_ips(db, "aws") == ["198.51.100.9"]
    finally:
        db.close()


def test_a_cloud_reporting_no_address_does_not_blank_a_good_one():
    """An Azure gateway has no public IP; reporting none must not erase what we know."""
    _reset(_gw("gw-1", cloud="azure", egress_ip="203.0.113.7"))
    _stub_clouds({"azure": {"gw-1": {"host_id": "gw-1", "egress_ip": ""}}})
    _reconcile()
    assert _reload("gw-1").egress_ip == "203.0.113.7"


def test_a_reconcile_with_no_change_does_not_write():
    """Every page load runs this; a commit per load would be churn on rows nobody touched."""
    _reset(_gw("gw-1"))
    _stub_clouds({"aws": {"gw-1": {"host_id": "i-abc", "serving": True}}})
    db = SessionLocal()
    commits = []
    real_commit = db.commit
    db.commit = lambda: commits.append(1) or real_commit()
    try:
        _run(gs.reconcile(db, ""))
    finally:
        db.close()
    assert commits == [], "an unchanged reconcile still committed"


def test_the_cloud_filter_only_queries_that_cloud():
    _reset(_gw("aws-gw", cloud="aws"), _gw("gcp-gw", cloud="gcp", region="us-central1"))
    _stub_clouds({"aws": {}, "gcp": {}})
    _reconcile("gcp")
    assert LOOKUPS == ["gcp"], LOOKUPS
    assert _reload("aws-gw").status == "running"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
