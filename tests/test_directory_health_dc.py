"""Directory health: the domain controller deep check for on-prem Active Directory.

What these pin:

- the report comes back from the job's log lines as base64 chunks, and anything malformed,
  incomplete or oversized is refused rather than half-read (it came from a DC through the
  agent, so it is untrusted);
- the thresholds: replication failures and age, FSMO holders, stopped services, clock skew,
  SYSVOL state, LDAPS certificate expiry, and an unreadable DC reading as unknown;
- an on-prem AD check queues ONE builtin:ad-dc-health Config Management run over WinRM 5986,
  never a second while one is in flight, not again inside the interval, and never for
  LDAP, a directory with no Password Safe account, or an agent without Config Management;
- a forced deep check is a separate switch from Check now's force, and the API ties it
  to directories:write, because the run checks out a domain credential;
- the result is folded in when the agent completes the job;
- the playbook is a closed built-in, matches its example, hides the credential, and reads
  no identities.

Uses a real temp SQLite database; no provider or agent is called.

Run: python tests/test_directory_health_dc.py   (or under pytest)
"""
import asyncio
import base64
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

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="dir-health-dc-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-dc-health-tests")

from web_dashboard.database import (Base, DirectoryHealth, Job, ManagedDirectory,  # noqa: E402
                                    RemoteAgent, SessionLocal, engine)
from web_dashboard.services import directory_health_service as H  # noqa: E402
from web_dashboard.services import job_service  # noqa: E402

Base.metadata.create_all(bind=engine)

NOW = datetime(2026, 10, 12, 12, 0, 0)
_PLAYBOOK = os.path.join(_ROOT, "web_dashboard", "services", "builtin_playbooks",
                         "ad-dc-health.yml")
_EXAMPLE = os.path.join(_ROOT, "examples", "playbooks", "directory", "ad-dc-health.yml")


def _agent(db, *, job_types=None):
    a = RemoteAgent(id=str(uuid.uuid4()), name=f"agent-{uuid.uuid4().hex[:6]}",
                    agent_version="2.8.0", is_active=True, public_key="pk",
                    last_seen_at=datetime.utcnow(),
                    allowed_job_types=json.dumps(job_types) if job_types else None)
    db.add(a)
    db.commit()
    return a


def _ad(db, agent, *, provider="onprem_ad", managed=True):
    row = ManagedDirectory(
        name="corp.example.com", cloud="local", provider=provider, source="registered",
        status="available", host=f"dc-{uuid.uuid4().hex[:8]}.corp.example.com", port=636,
        use_ldaps=True, base_dn="DC=corp,DC=example,DC=com", agent_id=agent.id,
        credentials_ref=('psmanaged:{"system_id": 1, "account_id": 2, '
                         '"account_name": "svc-health"}') if managed else None,
        created_by="t")
    db.add(row)
    db.commit()
    return row


def _deep_jobs(db, row):
    return [j for j in db.query(Job).filter(Job.job_type == "agent_ansible").all()
            if (j.metadata_dict or {}).get("target_id") == row.id]


def _keys(checks):
    return {c["key"]: c["status"] for c in checks}


def _report(**over):
    base = {
        "v": 1, "domain": "corp.example.com", "errors": [],
        "fsmo": {"PDCEmulator": "dc01.corp.example.com", "RIDMaster": "dc01.corp.example.com",
                 "InfrastructureMaster": "dc01.corp.example.com",
                 "SchemaMaster": "dc01.corp.example.com",
                 "DomainNamingMaster": "dc01.corp.example.com"},
        "dcs": [{"name": "dc01.corp.example.com", "site": "HQ", "gc": True,
                 "services": {s: "Running" for s in H.CORE_SERVICES}, "offset_s": 0.4,
                 "sysvol_state": 4, "ldaps_not_after": "2027-06-01T00:00:00Z", "errors": []},
                {"name": "dc02.corp.example.com", "site": "HQ", "gc": True,
                 "services": {s: "Running" for s in H.CORE_SERVICES}, "offset_s": -1.0,
                 "sysvol_state": 4, "ldaps_not_after": "2027-06-01T00:00:00Z", "errors": []}],
        "replication": [{"server": "dc01.corp.example.com", "partner": "DC02",
                         "last_success": "2026-10-12T11:50:00Z", "last_result": 0,
                         "failures": 0}],
    }
    base.update(over)
    return base


def _lines(report, size=40):
    """The report as Ansible's default callback prints the playbook's debug loop."""
    b64 = base64.b64encode(json.dumps(report).encode()).decode()
    chunks = [b64[i:i + size] for i in range(0, len(b64), size)]
    out = ["TASK [Print the report for the dashboard] ****"]
    for i, c in enumerate(chunks, 1):
        out += [f"ok: [dc] => (item={i}) => {{",
                f'    "msg": "DIRHEALTH_B64:{i}/{len(chunks)}:{c}"', "}"]
    return out


# ── reading the report back ───────────────────────────────────────────────────

def test_the_report_is_reassembled_from_the_job_log():
    report = _report()
    assert H.parse_report(_lines(report)) == report


def test_a_broken_report_is_refused_not_half_read():
    lines = _lines(_report())
    chunk_lines = [line for line in lines if "DIRHEALTH_B64:" in line]
    assert H.parse_report(lines[:-3]) is None, "a missing last chunk was accepted"
    assert H.parse_report([line for line in lines if line not in chunk_lines[1:2]]) is None
    assert H.parse_report(["DIRHEALTH_B64:1/1:!!!notbase64"]) is None
    bad = base64.b64encode(json.dumps({"v": 2}).encode()).decode()
    assert H.parse_report([f"DIRHEALTH_B64:1/1:{bad}"]) is None, "an unknown version was read"
    wrong = base64.b64encode(json.dumps({"v": 1, "dcs": "nope"}).encode()).decode()
    assert H.parse_report([f"DIRHEALTH_B64:1/1:{wrong}"]) is None
    assert H.parse_report(["no report here"]) is None


def test_an_oversized_report_is_refused():
    big = "A" * 4000
    n = H.MAX_REPORT_BYTES // 4000 + 2
    assert H.parse_report(f"DIRHEALTH_B64:{i}/{n}:{big}" for i in range(1, n + 1)) is None


def test_only_the_latest_report_counts():
    old, new = _report(domain="old.example.com"), _report()
    assert H.parse_report(_lines(old) + _lines(new)) == new


# ── thresholds ────────────────────────────────────────────────────────────────

def test_a_healthy_domain_is_healthy():
    checks = H.dc_health_checks(_report(), now=NOW)
    assert set(_keys(checks).values()) == {"ok"}, checks
    assert "ad_dc:dc01.corp.example.com" in _keys(checks)


def _replication(**link):
    entry = {"server": "dc01", "partner": "DC02", "last_success": "2026-10-12T11:50:00Z",
             "last_result": 0, "failures": 0}
    entry.update(link)
    return _keys(H.dc_health_checks(_report(replication=[entry]), now=NOW))["replication"]


def test_replication_thresholds():
    assert _replication() == "ok"
    assert _replication(failures=1) == "warn"
    assert _replication(last_result=8524) == "warn"
    assert _replication(last_success="2026-10-12T07:00:00Z") == "warn"
    assert _replication(failures=3) == "fail"
    assert _replication(last_success="2026-10-10T12:00:00Z") == "fail"
    assert _replication(last_success="") == "fail"


def test_a_single_dc_has_nothing_to_replicate():
    report = _report(replication=[], dcs=_report()["dcs"][:1])
    assert _keys(H.dc_health_checks(report, now=NOW))["replication"] == "ok"


def test_an_fsmo_role_on_a_former_dc_fails():
    fsmo = dict(_report()["fsmo"], RIDMaster="old-dc.corp.example.com")
    check = [c for c in H.dc_health_checks(_report(fsmo=fsmo), now=NOW)
             if c["key"] == "fsmo"][0]
    assert check["status"] == "fail" and "RIDMaster" in check["detail"]


def _dc(**over):
    dc = dict(_report()["dcs"][0], **over)
    return [c for c in H.dc_health_checks(_report(dcs=[dc]), now=NOW)
            if c["key"].startswith("ad_dc:")][0]


def test_dc_thresholds():
    stopped = _dc(services=dict(_report()["dcs"][0]["services"], Netlogon="Stopped"))
    assert stopped["status"] == "fail" and "Netlogon" in stopped["detail"]
    assert _dc(offset_s=90)["status"] == "warn"
    assert _dc(offset_s=-300)["status"] == "fail"
    assert _dc(sysvol_state=2)["status"] == "warn"
    assert _dc(sysvol_state=5)["status"] == "fail"
    assert _dc(ldaps_not_after="2026-11-01T00:00:00Z")["status"] == "warn"
    assert _dc(ldaps_not_after="2026-10-15T00:00:00Z")["status"] == "fail"
    expired = _dc(ldaps_not_after="2026-09-01T00:00:00Z")
    assert expired["status"] == "fail" and "expired" in expired["detail"]


def test_an_unreadable_dc_is_unknown_and_says_why():
    dc = {"name": "dc09.corp.example.com", "site": "Branch", "services": {},
          "offset_s": None, "sysvol_state": None, "ldaps_not_after": "",
          "errors": ["cim: Access is denied."]}
    check = _dc(**dc)
    assert check["status"] == "unknown" and "Access is denied" in check["detail"]


def test_report_text_is_cleaned():
    dc = _dc(name="dc01\x1b[31m.corp", errors=["ldaps: x" * 200])
    assert "\x1b" not in dc["label"] and len(dc["detail"]) < 400


# ── queueing ──────────────────────────────────────────────────────────────────

def test_an_ad_check_queues_one_builtin_winrm_run():
    db = SessionLocal()
    agent = _agent(db)
    row = _ad(db, agent)
    asyncio.run(H.check(db, row))
    jobs = _deep_jobs(db, row)
    assert len(jobs) == 1, f"expected one deep check, found {len(jobs)}"
    meta = jobs[0].metadata_dict
    assert meta["asset"] == "builtin:ad-dc-health" and meta["run_kind"] == "directory"
    assert meta["transport"] == "winrm" and meta["target_port"] == 5986
    assert meta["extra_vars"] == {} and jobs[0].agent_id == agent.id

    # Pending, and inside the interval: nothing new either way, even forced.
    asyncio.run(H.check(db, row, force=True))
    asyncio.run(H.check(db, row, force=True, force_deep=True))
    assert len(_deep_jobs(db, row)) == 1
    db.close()


def test_check_now_alone_does_not_force_the_deep_check():
    db = SessionLocal()
    row = _ad(db, _agent(db))
    asyncio.run(H.check(db, row))
    health = db.get(DirectoryHealth, row.id)
    health.deep_job_id = None                              # the first one finished
    db.commit()
    asyncio.run(H.check(db, row, force=True))
    assert len(_deep_jobs(db, row)) == 1, "Check now without write queued a deep check"
    asyncio.run(H.check(db, row, force=True, force_deep=True))
    assert len(_deep_jobs(db, row)) == 2
    db.close()


def test_the_api_ties_the_forced_deep_check_to_write():
    src = open(os.path.join(_ROOT, "web_dashboard", "api", "directories.py"),
               encoding="utf-8").read()
    body = src.split("async def check_directory_health(", 1)[1].split("\n@router", 1)[0]
    assert 'force_deep=has_explicit_permission(user, "directories", "write")' in body


def test_it_runs_again_after_the_interval():
    db = SessionLocal()
    row = _ad(db, _agent(db))
    asyncio.run(H.check(db, row))
    health = db.get(DirectoryHealth, row.id)
    health.deep_job_id = None
    health.deep_queued_at = datetime.utcnow() - timedelta(hours=H.DEFAULT_DEEP_INTERVAL_HOURS,
                                                          minutes=1)
    db.commit()
    asyncio.run(H.check(db, row))
    assert len(_deep_jobs(db, row)) == 2
    db.close()


def test_nothing_is_queued_where_it_cannot_run_and_the_page_says_why():
    db = SessionLocal()
    ldap = _ad(db, _agent(db), provider="ldap")
    result = asyncio.run(H.check(db, ldap))
    assert not _deep_jobs(db, ldap) and "deep_health" not in _keys(result["checks"])

    no_account = _ad(db, _agent(db), managed=False)
    result = asyncio.run(H.check(db, no_account))
    deep = [c for c in result["checks"] if c["key"] == "deep_health"][0]
    assert not _deep_jobs(db, no_account) and "Password Safe" in deep["detail"]

    no_cm = _ad(db, _agent(db, job_types=["agent_discover"]))
    result = asyncio.run(H.check(db, no_cm))
    deep = [c for c in result["checks"] if c["key"] == "deep_health"][0]
    assert not _deep_jobs(db, no_cm) and "Config Management" in deep["detail"]
    db.close()


# ── folding the result in ─────────────────────────────────────────────────────

def test_the_result_is_folded_in_when_the_agent_completes():
    db = SessionLocal()
    row = _ad(db, _agent(db))
    asyncio.run(H.check(db, row))
    job = _deep_jobs(db, row)[0]
    # apply_deep judges against the real clock, not NOW: the replication link has to be
    # recent and the LDAPS certificates in date as of today. The fixture's fixed
    # 2027-06-01 expiry turns this into a failing DC from May 2027 on.
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    recent = (datetime.utcnow() - timedelta(minutes=10)).strftime(fmt)
    valid = (datetime.utcnow() + timedelta(days=365)).strftime(fmt)
    report = _report(replication=[dict(_report()["replication"][0], last_success=recent)],
                     dcs=[dict(dc, ldaps_not_after=valid) for dc in _report()["dcs"]])
    for line in _lines(report, size=60):
        job_service.append_job_log(db, job.id, line)
    assert H.apply_deep(db, job, status="completed")
    health = db.get(DirectoryHealth, row.id)
    keys = _keys(json.loads(health.checks))
    assert health.deep_job_id is None
    assert keys["replication"] == "ok" and keys["fsmo"] == "ok"
    assert keys["ad_dc:dc01.corp.example.com"] == "ok"
    assert "deep_health" not in keys and "ldap_probe" in keys, keys

    # The next pass carries the results forward rather than resetting them.
    result = asyncio.run(H.check(db, row))
    assert _keys(result["checks"])["replication"] == "ok"
    db.close()


def test_a_failed_or_silent_run_is_a_failing_check():
    db = SessionLocal()
    row = _ad(db, _agent(db))
    asyncio.run(H.check(db, row))
    job = _deep_jobs(db, row)[0]
    H.apply_deep(db, job, status="failed", error="WinRM: 401 unauthorized")
    deep = [c for c in json.loads(db.get(DirectoryHealth, row.id).checks)
            if c["key"] == "deep_health"][0]
    assert deep["status"] == "fail" and "401" in deep["detail"]

    row2 = _ad(db, _agent(db))
    asyncio.run(H.check(db, row2))
    job2 = _deep_jobs(db, row2)[0]
    H.apply_deep(db, job2, status="completed")              # no report in the log
    assert db.get(DirectoryHealth, row2.id).status == "fail"
    db.close()


def test_other_config_management_runs_are_left_alone():
    db = SessionLocal()

    class _Job:
        id = str(uuid.uuid4())
    assert H.apply_deep(db, _Job(), status="completed") is False
    db.close()


# ── the playbook ──────────────────────────────────────────────────────────────

def test_the_playbook_is_a_closed_builtin_matching_its_example():
    from web_dashboard.services import agent_ansible_bundle as aab
    assert "ad-dc-health" in aab.BUILTIN_PLAYBOOKS
    with open(_EXAMPLE, encoding="utf-8") as fh:
        assert aab.builtin_playbook(H.DEEP_ASSET) == fh.read()


def test_the_playbook_hides_the_credential_and_reads_no_identities():
    import yaml
    with open(_PLAYBOOK, encoding="utf-8") as fh:
        src = fh.read()
    tasks = yaml.safe_load(src)[0]["tasks"]
    for task in tasks:
        if "dir_bind_password" in json.dumps(task):
            assert task.get("no_log") is True, f"{task['name']} carries the password"
    for cmdlet in ("Get-ADUser", "Get-ADGroup", "Get-ADGroupMember", "Get-ADComputer",
                   "Get-ADServiceAccount", "Get-ADObject", "Set-AD", "New-AD", "Remove-AD"):
        assert cmdlet not in src, f"dc-health uses {cmdlet}"
    assert "$Ansible.Changed = $false" in src
    assert re.search(r"DIRHEALTH_B64:\{\{ idx \+ 1 \}\}/", src)


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
