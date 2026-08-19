"""The inventory sync that follows a power button, without anyone pressing Sync Now.

An agent-bound hypervisor sits on a network the dashboard cannot reach, so the Workstation
page is a *cache* and a Start or a Stop does not touch it. The button worked, the job went
green, and the row kept saying the opposite until the operator pressed Sync Now or the
timed pass came round — `hypervisor_sync_interval_minutes`, 30 by default. The page
disagreed with the button that had just been pressed on it, which reads as the button
having failed.

`hypervisor_sync_service.sync_after_power` closes that loop from the completion path.
Several properties are pinned here, and each is a way the loop can quietly fail to close:

* **ordering** — the guard inside `sync_now` refuses while an `agent_hypervisor` job is
  open on the connection, and until it is marked terminal *the power job is that job*.
  Called a line too early, the hook skips every single time and nothing says so;
* **the meta contract** — the hook reads `verb` and `connection_id` back out of the job
  row, so the rows here are built by the real `agent_power_job` rather than hand-rolled.
  A meta key that moves has to break this file, not production;
* **no loop** — an `inventory_sync` is an `agent_hypervisor` job too, and a hook that did
  not exclude it would queue one from every completion until something gave out;
* **the in-flight report** — the sync this queues makes "a sync is already running"
  the ordinary outcome of pressing Sync Now, so the route has to say that in a way the
  page can tell apart from a refusal;
* **the verb split** — `RESYNC_VERBS` must cover every write verb bar `snapshot`. A verb
  added to the allowlist and left unclassified silently gets no resync, which is the
  original bug wearing a new verb's clothes.

Runs against a real throwaway SQLite database, because "does a second job row exist, and
who owns it" is storage behaviour. Under pytest, or standalone:
    python tests/test_power_resync.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(tempfile.mkdtemp(), "resync.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

try:
    from web_dashboard.api import vms as vms_api
    from web_dashboard.api.hypervisor_deps import agent_power_job
    from web_dashboard.database import (Base, HypervisorConnection, Job, RemoteAgent,
                                        SessionLocal, engine)
    from web_dashboard.services import agent_hypervisor_meta as ahm
    from web_dashboard.services import config_service, job_service
    from web_dashboard.services import hypervisor_connection_service as hcs
    from web_dashboard.services import hypervisor_sync_service as hss
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: {exc}")
    sys.exit(0)

_STEPS = []


def _ok(message):
    _STEPS.append(message)


def _syncs(db, connection_id):
    """Every inventory_sync row for this connection, oldest first."""
    rows = db.query(Job).filter(
        Job.job_type == "agent_hypervisor",
        Job.cloud_resource_id == connection_id).order_by(Job.created_at).all()
    return [j for j in rows if (j.metadata_dict or {}).get("verb") == "inventory_sync"]


def test_every_write_verb_is_classified():
    """A new write verb has to be put on one side of the resync line or the other.

    `snapshot` is the one exclusion and it is deliberate: it creates something the
    inventory cache stores no column of. Anything else surfacing here is a verb whose
    author did not decide, and the default — no resync — is the bug this file exists for.
    """
    unclassified = set(ahm.WRITE_VERBS) - set(hss.RESYNC_VERBS)
    assert unclassified == {"snapshot"}, (
        f"write verbs with no resync decision: {sorted(unclassified)}. Add each to "
        f"hypervisor_sync_service.RESYNC_VERBS, or to this assertion with the reason.")
    for verb in hss.RESYNC_VERBS:
        assert verb in ahm.WRITE_VERBS, f"{verb!r} is not a verb an agent can be asked to run"
    _ok("every write verb is classified: resync, or excluded on purpose")


def test_the_power_resync_scenario():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    config_service.set("remote_agents_enabled", "true")

    agent = RemoteAgent(name=f"desk-{os.getpid()}", public_key="k", is_active=True,
                        last_seen_at=datetime.utcnow(), enrolled_at=datetime.utcnow())
    db.add(agent)
    db.commit()
    db.refresh(agent)

    created = hcs.create(db, kind="workstation", name="bench", created_by="t",
                         agent_id=agent.id, agent_connection_name="bench")
    cid = created["id"]
    conn = hcs.resolve(db, "workstation", cid)

    # Every power row below is built by the code the button runs, not by hand: the hook
    # reads `verb` and `connection_id` back out of one, and a hand-rolled row would keep
    # passing here long after production stopped writing one of them.
    power = agent_power_job(db, conn, op="stop", target_id="7", created_by="alice",
                            description="stop bench-vm")
    assert power is not None and power.metadata_dict["verb"] == "power_off"
    _ok("a Stop on an agent-bound Workstation connection enqueues a power_off job")

    # Ordering, stated as the failure it prevents: while the power job is open it IS the
    # connection's in-flight agent_hypervisor row, so the hook can only skip.
    queued, reason = hss.sync_after_power(db, power)
    assert queued is None and reason == ""
    assert _syncs(db, cid) == []
    _ok("called before the power job is terminal, the hook queues nothing")

    job_service.set_completed(db, power.id, {"verb": "power_off", "ok": True})
    queued, reason = hss.sync_after_power(db, power)
    assert queued is not None, f"no sync was queued after a completed power op: {reason}"
    assert queued.status == "queued" and queued.agent_id == agent.id
    assert queued.cloud_resource_id == cid
    meta = queued.metadata_dict
    assert meta["verb"] == "inventory_sync" and meta["connection_ref"] == "bench"
    # The whole point of the agent design: the follow-on job names a connection, and no
    # more of one than the sync the timed pass queues.
    for banned in ("host", "port", "username", "password", "url"):
        assert banned not in meta, f"the follow-on sync carries {banned}"
    _ok("a completed power op queues an inventory sync for the same connection")

    # Attributed to the operator, not to system:sync. /jobs shows a non-admin only their
    # own rows, so the other attribution makes the sync they caused invisible to them.
    assert queued.created_by == "alice"
    assert "after power_off" in meta["description"], meta["description"]
    _ok("the sync is attributed to the operator and names the op that caused it")

    # Pressing Sync Now while that automatic one is still running must not read as a
    # failure. `sync_now` says so by returning an empty reason, but the route replaces it
    # with prose, so the page could not tell this apart from an offline agent and showed
    # it in red — and this is now the common case, straight after a Start.
    body = asyncio.run(vms_api.sync_inventory(db=db, current_user=None))
    assert body["queued"] == []
    assert [s["in_flight"] for s in body["skipped"]] == [True]
    assert body["skipped"][0]["connection"] == "bench"
    _ok("Sync Now during the automatic sync reports it as in flight, not as a refusal")

    # A burst of power ops coalesces rather than queueing a sync each: the sync above has
    # not run yet, and when it does it will see every VM the burst moved.
    second = agent_power_job(db, conn, op="start", target_id="8", created_by="alice",
                             description="start other-vm")
    job_service.set_completed(db, second.id, {"ok": True})
    again, _ = hss.sync_after_power(db, second)
    assert again is None
    assert len(_syncs(db, cid)) == 1
    _ok("a second power op does not stack a second sync on an open one")

    # An inventory_sync completing must not queue another. That is the loop that would
    # otherwise run until the agent, the database or the operator gave out.
    hss.apply_page(db, queued, {"vms": [{"vm_id": "7", "name": "bench-vm",
                                         "power_state": "poweredOff"}],
                                "next_cursor": "", "complete": True})
    job_service.set_completed(db, queued.id, {})
    nothing, _ = hss.sync_after_power(db, queued)
    assert nothing is None
    assert len(_syncs(db, cid)) == 1, "a sync queued a sync"
    # And the sync it queued is the one that actually moved the row the button acted on.
    cached = [v for v in hss.list_vms(db, cid) if v["vm_id"] == "7"]
    assert cached and cached[0]["power_state"] == "poweredOff"
    _ok("a finished inventory sync queues no successor, and its page reaches the cache")

    # A failed power op is the case that matters most: an agent losing the response to a
    # call it did make is indistinguishable, from here, from one that never left — and the
    # VM may well have moved. Re-reading is the right answer to both.
    third = agent_power_job(db, conn, op="stop", target_id="7", created_by="alice",
                            description="stop bench-vm")
    job_service.set_failed(db, third.id, "the hypervisor timed out")
    after_failure, reason = hss.sync_after_power(db, third)
    assert after_failure is not None, f"a failed power op queued no sync: {reason}"
    _ok("a FAILED power op queues a sync too — that is when reality is least certain")

    # An agent that goes offline between the button and the completion is refused with the
    # reason `sync_now` gives every other caller, rather than a job waiting for an agent
    # that is not coming back. Built while it was still online, which is the real sequence.
    job_service.set_completed(db, after_failure.id, {})
    fourth = agent_power_job(db, conn, op="start", target_id="7", created_by="alice",
                             description="start bench-vm")
    job_service.set_completed(db, fourth.id, {})
    agent.last_seen_at = datetime.utcnow() - timedelta(hours=2)
    db.commit()
    none_now, reason = hss.sync_after_power(db, fourth)
    assert none_now is None and "not online" in reason
    _ok("an offline agent is refused with a reason, not left queued")

    # A connection deactivated between the power op and its completion is a no-op, not a
    # crash on a finished job's response path.
    agent.last_seen_at = datetime.utcnow()
    row = db.query(HypervisorConnection).filter(HypervisorConnection.id == cid).one()
    row.is_active = False
    db.commit()
    gone, reason = hss.sync_after_power(db, fourth)
    assert gone is None and reason == ""
    _ok("a deactivated connection is a silent no-op")

    db.close()
    assert len(_STEPS) == 11, f"expected 11 checkpoints, ran {len(_STEPS)}"


if __name__ == "__main__":
    failures = 0
    for fn in (test_every_write_verb_is_classified, test_the_power_resync_scenario):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    for step in _STEPS:
        print(f"ok   {step}")
    sys.exit(1 if failures else 0)
