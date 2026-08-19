"""End-to-end scenario for the agent-brokered hypervisor inventory sync.

One ordered scenario rather than independent cases, because a paged sync IS a sequence:
page 2 only means anything after page 1, and deletion is only detectable across two
passes. Runs against a real throwaway SQLite database — the behaviour worth pinning here
(the cursor chain, the page cap, the prune) is all storage behaviour.

The subtle checkpoint is "a VM on an earlier page survives the final page's prune".
Pruning per page rather than per pass would delete most of a large inventory on every
sync, and it would look perfectly correct in any single-page test.

The five checkpoints after it are the other side of the same prune. It is what makes a
DELETED VM disappear, so an empty pass has to be able to empty the cache — and applied
to a page an agent returned empty because it could not READ the host, it is a silent
wipe of a good inventory, stamped as a successful sync. Both are the identical empty
list. The agent says which one it is; tests/test_hyperv_inventory_envelope.py pins the
producer end of that, where Hyper-V could not say at all.

Runs under pytest, or standalone:  python tests/test_hypervisor_sync.py
"""
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(tempfile.mkdtemp(), "sync.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

try:
    from web_dashboard.database import (Base, HypervisorConnection, Job, RemoteAgent,
                                        SessionLocal, engine)
    from web_dashboard.services import config_service, job_service
    from web_dashboard.services import hypervisor_connection_service as hcs
    from web_dashboard.services import hypervisor_sync_service as hss
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: {exc}")
    sys.exit(0)

_STEPS = []


def _ok(message):
    _STEPS.append(message)


def _one_pass(db, cid, page):
    """Run one whole sync pass for a connection and return its refreshed row.

    The same ritual the deletion-detection block does by hand — clear the cadence, drop
    the old job rows, enqueue, apply one page — because the empty-page guard needs five
    passes and five copies of it would bury what each one is asserting.

    The sleep is load-bearing: the prune deletes rows whose ``synced_at`` predates the
    pass, so a pass that starts inside the previous one's clock tick prunes nothing and
    every assertion below it passes for the wrong reason.
    """
    time.sleep(0.05)
    row = db.query(HypervisorConnection).filter(HypervisorConnection.id == cid).one()
    row.last_sync_at = None
    row.last_error = None
    db.commit()
    db.query(Job).filter(Job.job_type == "agent_hypervisor").delete()
    db.commit()
    assert hss.enqueue_due_syncs(db) == 1, "the pass did not queue"
    hss.apply_page(db, db.query(Job).filter(
        Job.job_type == "agent_hypervisor").one(), page)
    db.refresh(row)
    return row


def test_the_agent_sync_scenario():
    start = len(_STEPS)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    config_service.set("remote_agents_enabled", "true")

    agent = RemoteAgent(name=f"site-{os.getpid()}", public_key="k", is_active=True,
                        last_seen_at=datetime.utcnow(), enrolled_at=datetime.utcnow())
    db.add(agent)
    db.commit()
    db.refresh(agent)

    conn = hcs.create(db, kind="nutanix", name="hq-prism", created_by="t",
                      agent_id=agent.id, agent_connection_name="hq-prism")
    cid = conn["id"]
    assert conn["has_secret"] is False
    _ok("an agent-bound connection stores no credential")

    assert hss.enqueue_due_syncs(db) == 1
    job = db.query(Job).filter(Job.job_type == "agent_hypervisor").one()
    assert job.status == "queued" and job.agent_id == agent.id
    assert job.cloud_resource_id == cid
    meta = job.metadata_dict
    assert meta["verb"] == "inventory_sync" and meta["connection_ref"] == "hq-prism"
    # The whole design in one assertion: the job names a connection, not an endpoint.
    for banned in ("host", "port", "username", "password", "url"):
        assert banned not in meta, f"the sync payload carries {banned}"
    _ok("the queued sync names a connection and carries no endpoint or credential")

    assert hss.enqueue_due_syncs(db) == 0, "an open job must suppress a duplicate"
    _ok("the duplicate-enqueue guard holds")

    page1 = {"vms": [{"vm_id": "a", "name": "web01\x1b[31m", "power_state": "ON",
                      "vcpus": 4, "mem_mib": 8192, "password": "leak"},
                     {"vm_id": "b", "name": "db01", "power_state": "OFF"}],
             "next_cursor": "250", "complete": False}
    applied = hss.apply_page(db, job, page1)
    assert "leak" not in repr(applied), "an undeclared field survived the projection"
    assert "\x1b" not in applied["vms"][0]["name"], "an ANSI escape reached the browser"
    assert {r["vm_id"] for r in hss.list_vms(db, cid)} == {"a", "b"}
    _ok("page 1 is applied, projected and sanitised")

    jobs = db.query(Job).filter(Job.job_type == "agent_hypervisor").all()
    assert len(jobs) == 2
    follow = [j for j in jobs if j.id != job.id][0]
    assert follow.metadata_dict["cursor"] == "250"
    assert follow.metadata_dict["sync_page"] == 2
    assert follow.batch_id == (job.batch_id or job.id), "pages must share a batch"
    _ok("page 2 is chained, carrying the cursor and sharing a batch id")

    # A VM absent from page 2 is not deleted — it was on page 1. Pruning per page would
    # wipe most of a large inventory on every single sync.
    hss.apply_page(db, follow, {"vms": [{"vm_id": "c", "name": "app01"}],
                                "next_cursor": "", "complete": True})
    assert {r["vm_id"] for r in hss.list_vms(db, cid)} == {"a", "b", "c"}
    _ok("a VM on an earlier page survives the final page's prune")

    row = db.query(HypervisorConnection).filter(HypervisorConnection.id == cid).one()
    assert row.last_sync_at is not None and not row.last_error
    _ok("the connection is stamped with last_sync_at")

    # Deletion is only detectable across PASSES: a later sync that never mentions "b".
    time.sleep(0.01)
    row.last_sync_at = None
    db.commit()
    db.query(Job).filter(Job.job_type == "agent_hypervisor").delete()
    db.commit()
    assert hss.enqueue_due_syncs(db) == 1
    second = db.query(Job).filter(Job.job_type == "agent_hypervisor").one()
    hss.apply_page(db, second, {"vms": [{"vm_id": "a", "name": "web01"}],
                                "next_cursor": "", "complete": True})
    assert {r["vm_id"] for r in hss.list_vms(db, cid)} == {"a"}
    _ok("a later pass prunes VMs that no longer exist")

    # ── and an empty page must not be able to do the same thing by accident ──────
    #
    # The bug: a Hyper-V host whose `Get-VM` printed nothing — an unloaded module, or a
    # service account that cannot see the VMs — handed back a page identical to this
    # one. The prune above then deleted every row for the connection and the connection
    # was stamped as successfully synced. The page said "No VMs" and nothing anywhere
    # said why. Confirmed live on Hyper-V, against a page that had been listing rows.
    row = _one_pass(db, cid, {"vms": [], "next_cursor": "", "complete": True})
    assert {r["vm_id"] for r in hss.list_vms(db, cid)} == {"a"}, (
        "an unvouched-for empty page wiped a populated cache")
    _ok("an empty page nobody vouched for leaves a populated cache alone")

    # And it says so where the operator can find it. The job itself COMPLETED — the
    # agent did exactly what it was asked — so there is no failed job row carrying a
    # reason, and the hypervisor page has no error line at all, only rows or no rows.
    assert "no VMs at all" in (row.last_error or ""), row.last_error
    assert "1 cached VM" in (row.last_error or ""), (
        f"the reason must count what it kept: {row.last_error!r}")
    assert row.last_sync_at is None, (
        "a refused pass must not stamp last_sync_at, or the retry waits out the "
        "interval it was supposed to skip")
    _ok("the refusal reaches the connection as a counted, operator-facing reason")

    # The half that must survive the guard: pruning by synced_at is what makes a DELETED
    # VM disappear, so a genuinely empty host still has to converge to zero rows. It
    # costs nothing here, because the difference is one field the agent sets — never an
    # inference from the empty list, which is the same list in both cases.
    row = _one_pass(db, cid, {"vms": [], "next_cursor": "", "complete": True,
                              "enumerated": True})
    assert hss.list_vms(db, cid) == [], "a confirmed empty host did not prune to zero"
    assert row.last_sync_at is not None and not row.last_error
    _ok("a host the agent confirms is empty still prunes all the way to zero")

    # With nothing cached there is nothing to lose, so an unvouched-for empty page is an
    # ordinary sync rather than a standing error on a connection that has never had a VM
    # on it — which is every agent-bound connection on its first pass.
    row = _one_pass(db, cid, {"vms": [], "next_cursor": "", "complete": True})
    assert row.last_sync_at is not None and not row.last_error, row.last_error
    _ok("an empty page against an empty cache is an ordinary sync, not an error")

    # The guard reads what the whole PASS wrote, not the page it ends on. Reading the
    # page instead would strand the tail of every paged sync: pages 2..N are each
    # allowed to be empty, and the last page of a large vCenter routinely is.
    _one_pass(db, cid, {"vms": [{"vm_id": "a", "name": "web01"}],
                        "next_cursor": "", "complete": True, "enumerated": True})
    _one_pass(db, cid, {"vms": [{"vm_id": "b", "name": "db01"}],
                        "next_cursor": "250", "complete": False})
    tail = [j for j in db.query(Job).filter(Job.job_type == "agent_hypervisor").all()
            if (j.metadata_dict or {}).get("sync_page") == 2][0]
    hss.apply_page(db, tail, {"vms": [], "next_cursor": "", "complete": True})
    assert {r["vm_id"] for r in hss.list_vms(db, cid)} == {"b"}, (
        "a pass that wrote rows must still prune, even when its last page is empty")
    _ok("a pass that wrote rows still prunes, even when its last page is empty")

    # A lying agent must not be able to make the dashboard enqueue forever.
    capped = job_service.create_job(
        db, job_type="agent_hypervisor", created_by="t",
        metadata={**meta, "sync_page": hss.MAX_SYNC_PAGES}, agent_id=agent.id)
    job_service.set_cloud_resource_id(db, capped.id, cid)
    before = db.query(Job).filter(Job.job_type == "agent_hypervisor").count()
    hss.apply_page(db, capped, {"vms": [], "next_cursor": "9999", "complete": False})
    after = db.query(Job).filter(Job.job_type == "agent_hypervisor").count()
    assert after == before, "the page cap did not stop the chain"
    _ok("the page cap stops an endless chain")

    # An offline agent is skipped with a visible reason rather than queued: a job that
    # waits three days is worse than a refusal an operator can see.
    agent.last_seen_at = datetime.utcnow() - timedelta(hours=2)
    row = db.query(HypervisorConnection).filter(HypervisorConnection.id == cid).one()
    row.last_sync_at = None
    db.commit()
    db.query(Job).filter(Job.job_type == "agent_hypervisor").delete()
    db.commit()
    assert hss.enqueue_due_syncs(db) == 0
    row = db.query(HypervisorConnection).filter(HypervisorConnection.id == cid).one()
    assert "not online" in (row.last_error or "")
    _ok("an offline agent is skipped with a visible reason, not silently queued")

    # The manual sync is the SAME function the timed pass calls, minus the cadence
    # check, so it refuses for the same reasons. The agent is still offline here.
    job, reason = hss.sync_now(db, row)
    assert job is None and "not online" in reason
    _ok("a manual sync refuses an offline agent, with the reason the button shows")

    agent.last_seen_at = datetime.utcnow()
    db.commit()
    job, reason = hss.sync_now(db, row)
    assert job is not None and reason == ""
    again, _ = hss.sync_now(db, row)
    assert again is None, "a second sync must not queue while one is still open"
    _ok("a manual sync queues once, ignoring the cadence but not the in-flight guard")

    # `scope` was String(128): a longer VMX path raised StringDataRightTruncation inside
    # _upsert on PostgreSQL, which api/agent.py swallows — so a whole page of VMs
    # vanished with a log line and no user-visible failure.
    deep = "C:/VMs/" + ("d" * 200) + "/vm.vmx"
    hss._upsert(db, cid, [{"vm_id": "OS01", "name": "labelled",
                           "guest_os": "windows9-64", "scope": deep}],
                datetime.utcnow())
    stored = [v for v in hss.list_vms(db, cid) if v["vm_id"] == "OS01"][0]
    assert stored["guest_os"] == "windows9-64"
    assert stored["scope"] == deep, "a deep VMX path must survive the round trip"
    _ok("the guest OS code and a long VMX path both round-trip through the cache")

    db.close()
    ran = len(_STEPS) - start
    assert ran == 18, f"expected 18 checkpoints, ran {ran}"


if __name__ == "__main__":
    failed = None
    try:
        test_the_agent_sync_scenario()
    except Exception as exc:  # noqa: BLE001
        failed = exc
    for step in _STEPS:
        print(f"ok   {step}")
    if failed is not None:
        print(f"FAIL test_the_agent_sync_scenario: {failed}")
        sys.exit(1)
    sys.exit(0)
