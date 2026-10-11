"""Directory health: is each directory up and answering.

What these pin:

- the status rules: the worst check wins, and an unknown check never outranks a real
  answer;
- each provider's checks from its control plane's own words (AWS stage and DC status,
  GCP domain state, Entra DS replica sets and health alerts), Entra Connect sync freshness,
  and the on-prem LDAP probe, including a domain or base DN that does not match;
- an on-prem check queues ONE single-host directory probe on the agent, never a second
  while one is pending, and at most hourly; the probe's result is folded in when the
  agent completes the job;
- an offline or too-old agent queues nothing and says why;
- the module never reads identities: no user, group, role, MFA or credential call, so it
  cannot drift into the posture data Identity Security Insights owns.

Uses a real temp SQLite database; no provider is called.

Run: python tests/test_directory_health.py   (or under pytest)
"""
import asyncio
import json
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="dir-health-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-directory-health-tests")

from web_dashboard.database import (Base, DirectoryHealth, Job, ManagedDirectory,  # noqa: E402
                                    RemoteAgent, SessionLocal, engine)
from web_dashboard.services import directory_health_service as H  # noqa: E402

Base.metadata.create_all(bind=engine)

_SRC = os.path.join(_ROOT, "web_dashboard", "services", "directory_health_service.py")


def _agent(db, *, version="2.8.0", seen=True):
    a = RemoteAgent(id=str(uuid.uuid4()), name=f"agent-{uuid.uuid4().hex[:6]}",
                    agent_version=version, is_active=True, public_key="pk",
                    last_seen_at=datetime.utcnow() if seen else None)
    db.add(a)
    db.commit()
    return a


def _onprem(db, agent, *, provider="onprem_ad", name="corp.example.com",
            base_dn="DC=corp,DC=example,DC=com"):
    row = ManagedDirectory(name=name, cloud="local", provider=provider, source="registered",
                           status="available", host=f"dc-{uuid.uuid4().hex[:8]}.corp.example.com",
                           port=636, use_ldaps=True,
                           base_dn=base_dn, agent_id=agent.id, created_by="t")
    db.add(row)
    db.commit()
    return row


def _statuses(checks):
    return {c["key"]: c["status"] for c in checks}


# ── status rules ──────────────────────────────────────────────────────────────

def test_the_worst_check_wins_and_unknown_never_outranks_an_answer():
    c = lambda s: {"status": s}  # noqa: E731
    assert H.overall([c("ok"), c("fail"), c("warn")]) == "fail"
    assert H.overall([c("ok"), c("warn")]) == "warn"
    assert H.overall([c("ok"), c("unknown")]) == "ok"
    assert H.overall([c("unknown")]) == "unknown"
    assert H.overall([]) == "unknown"


# ── cloud control planes ──────────────────────────────────────────────────────

def test_aws_stage_and_domain_controllers():
    checks = H.aws_checks({"Stage": "Active"}, [
        {"DomainControllerId": "dc-1", "DnsIpAddr": "10.0.1.10", "Status": "Active",
         "AvailabilityZone": "us-east-2a"},
        {"DomainControllerId": "dc-2", "DnsIpAddr": "10.0.2.10", "Status": "Impaired",
         "StatusReason": "replication stopped"},
    ])
    s = _statuses(checks)
    assert s["control_plane"] == "ok" and s["dc:dc-1"] == "ok" and s["dc:dc-2"] == "fail"
    assert "replication stopped" in checks[-1]["detail"]
    assert H.overall(checks) == "fail"


def test_aws_impaired_and_missing_directories_fail():
    impaired = H.aws_checks({"Stage": "Impaired", "StageReason": "DNS unreachable"}, [])
    assert impaired[0]["status"] == "fail" and "DNS unreachable" in impaired[0]["detail"]
    assert H.aws_checks(None, [])[0]["status"] == "fail"
    assert H.aws_checks({"Stage": "Creating"}, [])[0]["status"] == "warn"


def test_gcp_domain_state():
    assert H.gcp_checks({"state": "READY", "locations": ["us-central1"]})[0]["status"] == "ok"
    assert H.gcp_checks({"state": "REPAIRING"})[0]["status"] == "warn"
    down = H.gcp_checks({"state": "DOWN", "statusMessage": "both DCs unreachable"})[0]
    assert down["status"] == "fail" and "both DCs unreachable" in down["detail"]
    assert H.gcp_checks(None)[0]["status"] == "fail"


def test_entra_ds_replica_sets_and_alerts_are_reported_as_given():
    checks = H.azure_checks({"properties": {"provisioningState": "Succeeded", "replicaSets": [
        {"replicaSetId": "rs1", "location": "eastus", "serviceStatus": "Running",
         "healthAlerts": [{"id": "a1", "name": "Secure LDAP certificate expiring",
                           "issue": "expires in 20 days", "severity": "Warning"}]},
        {"replicaSetId": "rs2", "location": "westus", "serviceStatus": "Running",
         "healthAlerts": [{"id": "a2", "name": "NSG blocks management ports",
                           "issue": "port 5986 is blocked", "severity": "High"}]},
    ]}})
    s = _statuses(checks)
    assert s["control_plane"] == "ok" and s["replica:rs1"] == "ok"
    assert s["alert:a1"] == "warn" and s["alert:a2"] == "fail"
    assert H.overall(checks) == "fail"
    assert H.azure_checks(None)[0]["status"] == "fail"


# ── identity providers ────────────────────────────────────────────────────────

def test_entra_connect_sync_freshness():
    now = datetime(2026, 10, 11, 12, 0, 0)
    fresh = H.sync_check({"enabled": True, "last_sync": "2026-10-11T11:40:00Z"}, now=now)
    stale = H.sync_check({"enabled": True, "last_sync": "2026-10-11T10:30:00Z"}, now=now)
    stopped = H.sync_check({"enabled": True, "last_sync": "2026-10-11T06:00:00Z"}, now=now)
    assert (fresh["status"], stale["status"], stopped["status"]) == ("ok", "warn", "fail")
    assert H.sync_check({"enabled": False}, now=now) is None, \
        "a cloud-only tenant has no sync to check"
    assert H.sync_check({"enabled": True, "last_sync": ""}, now=now)["status"] == "warn"


def test_an_idp_check_reuses_the_cached_sign_in():
    """idp_test forgets the cached sign-in, which for a Password Safe-managed credential is
    a checkout every interval. The health check must go through idp_call instead."""
    src = open(_SRC, encoding="utf-8").read()
    assert "idp_test(" not in src
    assert 'idp_call(row, "test")' in src


# ── on-prem, through the agent ────────────────────────────────────────────────

def test_probe_result_mapping():
    db = SessionLocal()
    row = _onprem(db, _agent(db))
    finding = {"kind": "directory", "domain": "corp.example.com",
               "base_dn": "DC=corp,DC=example,DC=com", "dc_hostname": "dc01.corp.example.com",
               "functional_level": "Windows Server 2016"}
    ok = H.probe_check(row, {"findings": [finding]})
    # Exact strings rather than `host in detail`: CodeQL reads a hostname substring
    # test as URL sanitization (py/incomplete-url-substring-sanitization).
    target = f"{row.host}:{row.port}"
    assert ok == {"key": "ldap_probe", "label": "LDAP answers", "status": "ok",
                  "detail": f"{target} answered (DC dc01.corp.example.com, "
                            f"Windows Server 2016)."}
    assert H.probe_check(row, {"findings": []})["status"] == "fail"
    assert H.probe_check(row, None, failed=True, error="refused by policy")["status"] == "fail"
    wrong = H.probe_check(row, {"findings": [{**finding, "domain": "other.example.com"}]})
    assert wrong["status"] == "warn"
    assert wrong["detail"] == f"{target} answered for domain other.example.com, not corp.example.com."

    ldap = _onprem(db, _agent(db), provider="ldap", name="ldap.example.com",
                   base_dn="dc=example, dc=com")
    same = {"kind": "directory", "base_dn": "DC=example,DC=com"}
    assert H.probe_check(ldap, {"findings": [same]})["status"] == "ok"
    other = {"kind": "directory", "base_dn": "o=elsewhere"}
    assert H.probe_check(ldap, {"findings": [other]})["status"] == "warn"
    db.close()


def _probe_jobs(db, row):
    return [j for j in db.query(Job).filter(Job.job_type == "agent_discover").all()
            if (j.metadata_dict or {}).get("hostnames") == [row.host]]


def test_an_onprem_check_queues_one_single_host_probe():
    db = SessionLocal()
    agent = _agent(db)
    row = _onprem(db, agent)
    result = asyncio.run(H.check(db, row))
    jobs = _probe_jobs(db, row)
    assert len(jobs) == 1, f"expected one probe job, found {len(jobs)}"
    meta = jobs[0].metadata_dict
    assert meta["scan_kind"] == "directory" and meta["max_hosts"] == 1
    assert meta["ports"]["directory"] == [636] and meta["cidrs"] == []
    assert jobs[0].agent_id == agent.id
    assert result["probe_pending"] and _statuses(result["checks"])["agent"] == "ok"

    # A second pass while that probe is pending, and a forced one, queue nothing new.
    asyncio.run(H.check(db, row))
    asyncio.run(H.check(db, row, force=True))
    assert len(_probe_jobs(db, row)) == 1, "a second probe was queued while one is pending"
    db.close()


def test_the_probe_result_is_folded_in_when_the_agent_completes():
    db = SessionLocal()
    row = _onprem(db, _agent(db))
    asyncio.run(H.check(db, row))
    job = _probe_jobs(db, row)[0]
    done = H.apply_probe(db, job, status="completed", result={"findings": [
        {"kind": "directory", "domain": "corp.example.com", "dc_hostname": "dc01"}]})
    assert done
    health = db.get(DirectoryHealth, row.id)
    assert health.pending_job_id is None and health.status == "ok"
    assert _statuses(json.loads(health.checks))["ldap_probe"] == "ok"

    # The next pass keeps the last probe result rather than resetting it to unknown, and
    # does not probe again inside the hour.
    result = asyncio.run(H.check(db, row))
    assert _statuses(result["checks"])["ldap_probe"] == "ok"
    assert len(_probe_jobs(db, row)) == 1

    # An hour on, it probes again.
    health = db.get(DirectoryHealth, row.id)
    health.probe_queued_at = datetime.utcnow() - timedelta(minutes=61)
    db.commit()
    asyncio.run(H.check(db, row))
    assert len(_probe_jobs(db, row)) == 2
    db.close()


def test_a_failed_probe_job_is_a_failing_check():
    db = SessionLocal()
    row = _onprem(db, _agent(db))
    asyncio.run(H.check(db, row))
    job = _probe_jobs(db, row)[0]
    H.apply_probe(db, job, status="failed", result=None, error="host not in targets")
    health = db.get(DirectoryHealth, row.id)
    assert health.status == "fail"
    assert "host not in targets" in json.loads(health.checks)[-1]["detail"]
    db.close()


def test_other_discovery_jobs_are_left_alone():
    db = SessionLocal()

    class _Job:
        id = str(uuid.uuid4())
    assert H.apply_probe(db, _Job(), status="completed", result={"findings": []}) is False
    db.close()


def test_an_offline_or_old_agent_queues_nothing_and_says_why():
    db = SessionLocal()
    offline = _onprem(db, _agent(db, seen=False))
    result = asyncio.run(H.check(db, offline))
    s = _statuses(result["checks"])
    assert s["agent"] == "fail" and s["ldap_probe"] == "unknown"
    assert not _probe_jobs(db, offline)

    old = _onprem(db, _agent(db, version="2.7.0"))
    result = asyncio.run(H.check(db, old))
    probe = [c for c in result["checks"] if c["key"] == "ldap_probe"][0]
    assert probe["status"] == "unknown" and "2.8" in probe["detail"]
    assert not _probe_jobs(db, old)
    db.close()


def test_a_directory_being_built_is_not_checked():
    db = SessionLocal()
    row = ManagedDirectory(name="new.example.com", cloud="aws", provider="aws_managed_ad",
                           source="provisioned", status="provisioning", created_by="t")
    db.add(row)
    db.commit()
    result = asyncio.run(H.check(db, row))
    assert result["status"] == "unknown" and "provisioning" in result["checks"][0]["detail"]
    db.close()


def test_links_are_listed_as_unmonitored_not_checked():
    db = SessionLocal()
    link = ManagedDirectory(name="corp-link", cloud="gcp", provider="dns_link",
                            source="provisioned", status="available", created_by="t")
    db.add(link)
    db.commit()
    snap = H.snapshot(db, [link])
    assert snap["directories"] == []
    assert [u["directory_id"] for u in snap["unmonitored"]] == [link.id]
    db.close()


def test_the_snapshot_lists_worst_first():
    db = SessionLocal()
    a, b = _onprem(db, _agent(db), name="a.example.com"), _onprem(db, _agent(db), name="b.example.com")
    db.add(DirectoryHealth(directory_id=a.id, status="ok", checks="[]",
                           checked_at=datetime.utcnow()))
    db.add(DirectoryHealth(directory_id=b.id, status="fail", checks="[]",
                           checked_at=datetime.utcnow()))
    db.commit()
    snap = H.snapshot(db, [a, b])
    assert [d["name"] for d in snap["directories"]] == ["b.example.com", "a.example.com"]
    assert snap["counts"]["fail"] == 1 and snap["counts"]["ok"] == 1
    db.close()


# ── the line this module must not cross ───────────────────────────────────────

def test_the_module_never_reads_identities():
    """Identity posture is Identity Security Insights' data. A check that lists users,
    groups, roles, sign-in methods or credentials here would be a second, disagreeing copy
    of it — so the provider calls this module may make are pinned."""
    src = open(_SRC, encoding="utf-8").read()
    calls = set(re.findall(r'idp_call\(row,\s*"(\w+)"', src))
    assert calls == {"test", "sync_status"}, calls
    for forbidden in ("list_users", "list_groups", "group_members", "user_groups",
                      "authenticationMethods", "roleAssignments", "directoryRoles",
                      "passwordCredentials", "signIns", "ad-audit-"):
        assert forbidden not in src, f"directory_health_service reads {forbidden}"


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
