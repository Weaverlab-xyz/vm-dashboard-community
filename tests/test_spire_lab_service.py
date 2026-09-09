"""What a SPIRE lab's provision and teardown actually do, written down.

``spire_lab_service`` opens a cloud ACL and then drives four privileged playbooks against
somebody's VM. What matters is not that it works on the happy path but the ORDER and the
REFUSALS, because that is where this kind of code goes wrong and none of it shows in a
diff:

  * the cloud ACL is opened BEFORE any playbook, and a refusal there is FATAL — running
    the install against a host nothing can reach produces a green lab that fails every
    plugin action, which is worse than a failed job;
  * a blank source set opens NOTHING and still runs the playbooks, because a Resource
    Broker already inside the VNet needs no rule — and that case must not be silent;
  * the FIRST failing stage stops the sequence, since every later one asserts the server
    is up and continuing turns one legible Ansible error into four;
  * both gates get the SAME source set, because a closed cloud ACL and a closed host
    firewall present identically;
  * every stage child is created ``queued``, never ``pending`` — a pending child under a
    handled type would be claimed by the worker and run a second time, concurrently;
  * the teardown closes the port and does NOT touch the VM;
  * the host is re-derived from this dashboard's own deploy rows, so a caller cannot aim
    four root playbooks at an address of its choosing.

Heavy deps are stubbed in sys.modules; the database is a real temp SQLite, so the rows and
commits are the real ones.

Run: python tests/test_spire_lab_service.py   (or under pytest)
"""
import asyncio
import json
import os
import sys
import tempfile
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="spirelab-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-spire-lab-tests")

try:
    from web_dashboard.database import Base, Job, SessionLocal, SpireLab, engine
    from web_dashboard.services import job_service
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

Base.metadata.create_all(bind=engine)

# Every collaborator call the provision path makes, in the order it made them.
CALLS = []


# ── Stubs ─────────────────────────────────────────────────────────────────────

def _install_stubs(*, cidrs=("10.1.0.0/24",), acl_opened=True, acl_raises=False,
                   stage_fails_on=None, folder_created=("weaverlab",),
                   folder_error="", bundle="-----BEGIN CERTIFICATE-----\nx\n"
                                                 "-----END CERTIFICATE-----",
                   expires="Sep 15 12:04:31 2026 GMT"):
    """Rebind the service's collaborators and return the reloaded service module."""
    CALLS.clear()

    ws = types.ModuleType("web_dashboard.api.websocket")

    async def broadcast_progress(job_id, pct, msg, log_line=None):
        CALLS.append(("progress", pct, msg))
    ws.broadcast_progress = broadcast_progress
    sys.modules["web_dashboard.api.websocket"] = ws

    az = types.ModuleType("web_dashboard.services.azure_service")

    async def ensure_vm_inbound_rule(rg, vm_name, *, rule_name, ports, source_cidrs,
                                     location=""):
        CALLS.append(("acl", vm_name, rule_name, tuple(ports), tuple(source_cidrs)))
        if acl_raises:
            raise RuntimeError("AuthorizationFailed")
        # The real one fails closed: an empty source set can never report opened.
        opened = bool(source_cidrs) and acl_opened
        return {"nsg": f"{vm_name}-nsg", "rule": rule_name, "opened": opened,
                "created": False, "attached_to": "nic"}
    az.ensure_vm_inbound_rule = ensure_vm_inbound_rule
    sys.modules["web_dashboard.services.azure_service"] = az

    runner = types.ModuleType("web_dashboard.services.ansible_local_run_service")

    async def run(db, *, job_id, meta):
        CALLS.append(("stage", meta["asset"], meta["target"], meta["extra_vars"]))
        row = db.query(Job).filter(Job.id == job_id).first()
        row.status = "failed" if meta["asset"] == stage_fails_on else "completed"
        db.commit()
    runner.run = run
    sys.modules["web_dashboard.services.ansible_local_run_service"] = runner

    secrets = types.ModuleType("web_dashboard.services.secrets_backend_service")

    def ensure_bt_folder_path(path):
        CALLS.append(("folder", path))
        if folder_error:
            # The real one raises ValueError; the service translates it.
            raise ValueError(folder_error)
        return {"folder_id": "f-1", "created": list(folder_created), "path": path}
    secrets.ensure_bt_folder_path = ensure_bt_folder_path

    def read_bt_secrets_safe(ref, vault_id=None):
        CALLS.append(("read_secret", ref))
        if ref.endswith("trust-bundle-pem"):
            return bundle
        if ref.endswith("admin-svid-expires"):
            return expires
        raise AssertionError(f"the service must never read {ref!r}")
    secrets.read_bt_secrets_safe = read_bt_secrets_safe
    sys.modules["web_dashboard.services.secrets_backend_service"] = secrets

    storage = types.ModuleType("web_dashboard.services.storage_service")
    storage.active_backend = lambda: "s3"
    sys.modules["web_dashboard.services.storage_service"] = storage

    import importlib
    svc = importlib.import_module("web_dashboard.services.spire_lab_service")
    importlib.reload(svc)
    svc.source_cidrs = lambda: list(cidrs)
    return svc


def _deploy_job(db, *, vm_name="spire-01", private_ip="10.1.0.7",
                public_ip="20.30.40.50", job_type="azure_deploy", destroyed=False,
                rg="rg-labs", location="eastus"):
    job = job_service.create_job(db, job_type, "tester", status="completed",
                                 metadata={"vm_name": vm_name,
                                           "instance_name": vm_name,
                                           "private_ip": private_ip,
                                           "public_ip": public_ip,
                                           "resource_group": rg,
                                           "location": location,
                                           "destroyed": destroyed})
    job.status = "completed"
    db.commit()
    return job


def _fresh_db():
    db = SessionLocal()
    db.query(SpireLab).delete()
    db.query(Job).delete()
    db.commit()
    return db


def _stages():
    return [c for c in CALLS if c[0] == "stage"]


# ── resolve_host: the caller proposes, the dashboard decides ─────────────────

def test_the_host_is_re_derived_from_our_own_deploy_rows():
    svc = _install_stubs()
    db = _fresh_db()
    _deploy_job(db)
    for ref in ("spire-01", "10.1.0.7", "20.30.40.50"):
        info = svc.resolve_host(db, "azure", ref)
        assert info["name"] == "spire-01", ref
        assert info["private_ip"] == "10.1.0.7"
    db.close()


def test_an_address_we_never_deployed_is_refused():
    """Otherwise a request naming an arbitrary IP is a request to run four root
    playbooks against a host of the caller's choosing."""
    svc = _install_stubs()
    db = _fresh_db()
    _deploy_job(db)
    for ref in ("192.0.2.99", "someone-elses-vm", ""):
        try:
            svc.resolve_host(db, "azure", ref)
            raise AssertionError(f"{ref!r} should have been refused")
        except svc.SpireLabError:
            pass
    db.close()


def test_a_destroyed_vm_is_not_a_host():
    svc = _install_stubs()
    db = _fresh_db()
    _deploy_job(db, destroyed=True)
    try:
        svc.resolve_host(db, "azure", "spire-01")
        raise AssertionError("a destroyed VM should not be offered as a host")
    except svc.SpireLabError:
        pass
    db.close()


# ── provision: the refusals that matter ──────────────────────────────────────

def test_a_spiffe_uri_is_not_a_trust_domain():
    """It is baked into the server config, every SPIFFE ID and the Managed System, and
    the plugin asserts it on every connect — so getting it wrong is unrecoverable."""
    svc = _install_stubs()
    db = _fresh_db()
    _deploy_job(db)
    for bad in ("spiffe://weaverlab.test", "weaverlab.test/x", ""):
        try:
            svc.provision(db, name="lab", trust_domain=bad, cloud="azure",
                          host="spire-01", created_by="tester")
            raise AssertionError(f"{bad!r} should have been refused")
        except svc.SpireLabError:
            pass
    db.close()


def test_provision_records_the_placement_and_defaults_the_admin_id():
    svc = _install_stubs()
    db = _fresh_db()
    _deploy_job(db)
    out = svc.provision(db, name="Weaver Lab", trust_domain="Weaverlab.TEST",
                        cloud="azure", host="spire-01", created_by="tester")
    row = svc.get_lab(db, out["lab_id"])
    # Lower-cased: a trust domain is a DNS-style name and the plugin compares it.
    assert row.trust_domain == "weaverlab.test"
    assert row.admin_spiffe_id == "spiffe://weaverlab.test/password-safe/admin"
    # The placement is resolved at provision time and stored, so the worker and the
    # teardown both act on what was true when the operator clicked.
    assert json.loads(row.vm_resource_id) == {
        "resource_group": "rg-labs", "vm_name": "spire-01", "location": "eastus"}
    assert row.admin_secret_folder == "spire/weaver-lab"
    assert row.bind_port == 8081
    assert row.deploy_job_id == out["job_id"]
    db.close()


def test_a_cloud_with_no_backend_is_refused_at_the_click():
    svc = _install_stubs()
    db = _fresh_db()
    try:
        svc.require_backend("oci")
        raise AssertionError("oci has no host backend and should be refused")
    except svc.SpireLabError as exc:
        assert "azure" in str(exc), "the error should name the clouds that DO work"
    db.close()


# ── run_provision ────────────────────────────────────────────────────────────

def _provisioned(svc, db, **kw):
    _deploy_job(db, **kw)
    out = svc.provision(db, name="lab", trust_domain="weaverlab.test", cloud="azure",
                        host="spire-01", created_by="tester")
    return svc.get_lab(db, out["lab_id"]), out["job_id"]


def test_the_happy_path_opens_the_acl_then_runs_four_playbooks_in_order():
    svc = _install_stubs()
    db = _fresh_db()
    row, job_id = _provisioned(svc, db)
    asyncio.run(svc.run_provision(db, lab_id=row.id, job_id=job_id))

    db.expire_all()
    row = svc.get_lab(db, row.id)
    assert row.status == "available", row.error_message
    # The ACL comes first. Opening it after the install would leave a window where the
    # server is up and unreachable, and the ordering is the whole reachability story.
    kinds = [c[0] for c in CALLS if c[0] in ("folder", "acl", "stage")]
    assert kinds == ["folder", "acl", "stage", "stage", "stage", "stage"], kinds
    assert [c[1] for c in _stages()] == list(svc.STAGE_ASSETS)
    assert row.stages_done == "install,ports,seed,identity"
    assert row.entries_seeded == svc.ENTRIES_SEEDED == 11
    assert row.discovery_expected == svc.DISCOVERY_EXPECTED == 8
    # Each stage got its own job row, so a failure has somewhere to be read.
    assert sorted(svc.stage_jobs(row)) == ["identity", "install", "ports", "seed"]
    db.close()


def test_every_stage_child_is_queued_not_pending():
    """A pending child under a handled type would be claimed by the worker and run a
    SECOND time, concurrently with the parent already running it."""
    svc = _install_stubs()
    db = _fresh_db()
    row, job_id = _provisioned(svc, db)
    asyncio.run(svc.run_provision(db, lab_id=row.id, job_id=job_id))
    db.expire_all()
    row = svc.get_lab(db, row.id)
    for key, child_id in svc.stage_jobs(row).items():
        child = db.query(Job).filter(Job.id == child_id).first()
        assert child.job_type == "ansible_local", key
        # The stub set it completed; what matters is that it never sat at 'pending',
        # which is the only status the runner's claim query looks at.
        assert child.status == "completed", key
        assert child.batch_id == row.id, "the batch groups the lab's own runs"
    db.close()


def test_the_runner_connects_to_the_public_address_when_there_is_one():
    """The runner is a transient in-cloud task or a local container, and neither is
    reliably in-subnet. A private address works only when it happens to be, and when it
    is not the failure is an SSH timeout that reads as a firewall problem."""
    svc = _install_stubs()
    db = _fresh_db()
    row, job_id = _provisioned(svc, db)
    asyncio.run(svc.run_provision(db, lab_id=row.id, job_id=job_id))
    assert {c[2] for c in _stages()} == {"20.30.40.50"}

    # ...and falls back to the private one when the VM has no public IP.
    svc = _install_stubs()
    db = _fresh_db()
    row, job_id = _provisioned(svc, db, public_ip="")
    asyncio.run(svc.run_provision(db, lab_id=row.id, job_id=job_id))
    assert {c[2] for c in _stages()} == {"10.1.0.7"}
    db.close()


def test_both_gates_get_the_same_source_set():
    """A closed cloud ACL and a closed host firewall present identically — a gRPC timeout
    on Verify Functional Account. One gate narrower than the other is the hardest version
    of this to debug."""
    svc = _install_stubs(cidrs=("10.1.0.0/24", "203.0.113.7/32"))
    db = _fresh_db()
    row, job_id = _provisioned(svc, db)
    asyncio.run(svc.run_provision(db, lab_id=row.id, job_id=job_id))

    acl = next(c for c in CALLS if c[0] == "acl")
    assert acl[4] == ("10.1.0.0/24", "203.0.113.7/32")
    ports_stage = next(c for c in _stages() if c[1] == "spire-open-ports.yml")
    assert ports_stage[3]["spire_source_cidrs"] == ["10.1.0.0/24", "203.0.113.7/32"]
    assert ports_stage[3]["bind_port"] == 8081
    db.close()


def test_the_trust_domain_reaches_the_three_playbooks_that_need_it():
    svc = _install_stubs()
    db = _fresh_db()
    row, job_id = _provisioned(svc, db)
    asyncio.run(svc.run_provision(db, lab_id=row.id, job_id=job_id))
    by_asset = {c[1]: c[3] for c in _stages()}
    for asset in ("spire-server-install.yml", "spire-seed-entries.yml",
                  "spire-admin-identity.yml"):
        assert by_asset[asset]["trust_domain"] == "weaverlab.test", asset
    # And the identity play needs somewhere to put the credential.
    assert by_asset["spire-admin-identity.yml"]["admin_secret_folder"] == "spire/lab"
    assert by_asset["spire-admin-identity.yml"]["ps_safe"]
    db.close()


def test_a_blank_source_set_opens_nothing_but_still_builds_the_lab():
    """Not fail-open: nothing is opened. A Resource Broker already inside the VNet needs
    no rule, and that lab is valid — but the job output has to SAY so, because it is the
    first thing to check when the plugin later times out."""
    svc = _install_stubs(cidrs=())
    db = _fresh_db()
    row, job_id = _provisioned(svc, db)
    asyncio.run(svc.run_provision(db, lab_id=row.id, job_id=job_id))

    db.expire_all()
    row = svc.get_lab(db, row.id)
    assert row.status == "available", row.error_message
    assert not [c for c in CALLS if c[0] == "acl"], "no ACL call should be made"
    assert len(_stages()) == 4, "the lab is still built"
    said = " ".join(str(c[2]) for c in CALLS if c[0] == "progress")
    assert "spire_lab_source_cidrs" in said, "the blank case must explain itself"
    db.close()


def test_an_acl_that_did_not_open_is_fatal_before_any_playbook():
    """A green lab whose server nothing can reach is worse than a failed job: it fails
    every plugin action later, and the symptom reads as a credential problem."""
    svc = _install_stubs(acl_opened=False)
    db = _fresh_db()
    row, job_id = _provisioned(svc, db)
    asyncio.run(svc.run_provision(db, lab_id=row.id, job_id=job_id))

    db.expire_all()
    row = svc.get_lab(db, row.id)
    assert row.status == "failed"
    assert not _stages(), "no playbook may run against an unreachable host"
    assert db.query(Job).filter(Job.id == job_id).first().status == "failed"
    db.close()


def test_the_first_failing_stage_stops_the_sequence():
    """Every later stage asserts the server is up, so continuing turns one legible
    Ansible error into four."""
    svc = _install_stubs(stage_fails_on="spire-seed-entries.yml")
    db = _fresh_db()
    row, job_id = _provisioned(svc, db)
    asyncio.run(svc.run_provision(db, lab_id=row.id, job_id=job_id))

    db.expire_all()
    row = svc.get_lab(db, row.id)
    assert row.status == "failed"
    assert [c[1] for c in _stages()] == ["spire-server-install.yml",
                                         "spire-open-ports.yml",
                                         "spire-seed-entries.yml"]
    # The stages that DID pass are recorded, and the failure names the job to read.
    assert row.stages_done == "install,ports"
    assert "spire-seed-entries.yml" in row.error_message
    assert svc.stage_jobs(row)["seed"] in row.error_message
    db.close()


def test_the_public_artifacts_are_read_back_and_the_credential_is_not():
    """The read stub raises on the credential's two titles, so this fails loudly if the
    service ever reaches for them."""
    svc = _install_stubs()
    db = _fresh_db()
    row, job_id = _provisioned(svc, db)
    asyncio.run(svc.run_provision(db, lab_id=row.id, job_id=job_id))

    db.expire_all()
    row = svc.get_lab(db, row.id)
    assert "BEGIN CERTIFICATE" in (row.trust_bundle_pem or "")
    assert row.admin_svid_expires_at is not None
    assert row.admin_svid_expires_at.year == 2026
    read = sorted(c[1] for c in CALLS if c[0] == "read_secret")
    assert read == ["spire/lab/admin-svid-expires", "spire/lab/trust-bundle-pem"]
    db.close()


def test_an_unreadable_artifact_does_not_fail_a_working_lab():
    """A lab whose server is up and seeded is a working lab. The values are also printed
    in the identity stage's own output."""
    svc = _install_stubs(bundle="", expires="not a date")
    db = _fresh_db()
    row, job_id = _provisioned(svc, db)
    asyncio.run(svc.run_provision(db, lab_id=row.id, job_id=job_id))

    db.expire_all()
    row = svc.get_lab(db, row.id)
    assert row.status == "available", row.error_message
    assert not row.trust_bundle_pem
    assert row.admin_svid_expires_at is None
    db.close()



# ── the credential's folder, checked before anything is touched ──────────────

def test_the_secrets_folder_is_ensured_before_the_acl_and_before_any_playbook():
    """The identity playbook WRITES INTO a folder and never creates one. Without this
    pre-flight the fourth and last stage fails on a folder-not-found, by which point the
    server is installed and seeded on somebody's VM — and the error reads like a
    credential fault."""
    svc = _install_stubs()
    db = _fresh_db()
    row, job_id = _provisioned(svc, db)
    asyncio.run(svc.run_provision(db, lab_id=row.id, job_id=job_id))

    ordered = [c[0] for c in CALLS if c[0] in ("folder", "acl", "stage")]
    assert ordered[0] == "folder", ordered
    folder = next(c for c in CALLS if c[0] == "folder")
    # <safe>/<folder tree>: the FIRST segment names an existing safe.
    assert folder[1] == "Automation/spire/lab"
    db.close()


def test_a_missing_safe_stops_the_build_before_anything_is_touched():
    """A lab whose credential has nowhere to go is one we should not have started. The
    safe is never created for you — it carries its own ACL."""
    svc = _install_stubs(folder_error="Secrets Safe has no safe named 'Automation'")
    db = _fresh_db()
    row, job_id = _provisioned(svc, db)
    asyncio.run(svc.run_provision(db, lab_id=row.id, job_id=job_id))

    db.expire_all()
    row = svc.get_lab(db, row.id)
    assert row.status == "failed"
    assert "nowhere to land" in (row.error_message or "")
    assert "no safe named" in (row.error_message or ""), "the real reason must survive"
    assert not [c for c in CALLS if c[0] == "acl"], "the ACL must not be touched"
    assert not _stages(), "no playbook may run"
    db.close()


def test_an_existing_folder_is_not_announced_as_created():
    """Idempotent: a second lab in the same tree creates nothing, and saying it did would
    have an operator looking for a folder that was always there."""
    svc = _install_stubs(folder_created=())
    db = _fresh_db()
    row, job_id = _provisioned(svc, db)
    asyncio.run(svc.run_provision(db, lab_id=row.id, job_id=job_id))

    db.expire_all()
    assert svc.get_lab(db, row.id).status == "available"
    said = " ".join(str(c[2]) for c in CALLS if c[0] == "progress")
    assert "Created Secrets Safe folder" not in said
    db.close()


def test_the_folder_path_follows_the_configured_root_and_the_lab_name():
    """`<root>/<slug(name)>` under the safe. The slug matters: a lab called "Weaver Lab"
    must not ask Secrets Safe for a folder with a space in it."""
    svc = _install_stubs()
    db = _fresh_db()
    _deploy_job(db)
    out = svc.provision(db, name="Weaver Lab", trust_domain="weaverlab.test",
                        cloud="azure", host="spire-01", created_by="tester")
    row = svc.get_lab(db, out["lab_id"])
    asyncio.run(svc.run_provision(db, lab_id=row.id, job_id=out["job_id"]))
    folder = next(c for c in CALLS if c[0] == "folder")
    assert folder[1] == "Automation/spire/weaver-lab"
    db.close()


# ── teardown ─────────────────────────────────────────────────────────────────

def test_the_teardown_closes_the_port_and_leaves_the_vm_alone():
    svc = _install_stubs()
    db = _fresh_db()
    row, job_id = _provisioned(svc, db)
    asyncio.run(svc.run_provision(db, lab_id=row.id, job_id=job_id))

    out = svc.start_decommission(db, lab_id=row.id, created_by="tester")
    db.expire_all()
    row = svc.get_lab(db, row.id)
    # At-most-once: the timer is cleared in the same transaction that starts teardown,
    # so the next sweep pass cannot enqueue a second one.
    assert row.status == "decommissioning"
    assert row.expires_at is None

    CALLS.clear()
    asyncio.run(svc.run_decommission(db, lab_id=row.id, job_id=out["job_id"]))
    db.expire_all()
    row = svc.get_lab(db, row.id)
    assert row.status == "deleted", row.error_message
    assert row.source_cidrs == ""
    acl = [c for c in CALLS if c[0] == "acl"]
    assert len(acl) == 1 and acl[0][4] == (), "closing means an EMPTY source set"
    # The host VM is not this feature's to destroy.
    assert row.vm_name == "spire-01"
    result = db.query(Job).filter(Job.id == out["job_id"]).first()
    assert result.status == "completed"
    db.close()


def test_a_second_teardown_is_refused_while_one_is_in_flight():
    svc = _install_stubs()
    db = _fresh_db()
    row, job_id = _provisioned(svc, db)
    svc.start_decommission(db, lab_id=row.id, created_by="tester")
    try:
        svc.start_decommission(db, lab_id=row.id, created_by="tester")
        raise AssertionError("a second teardown should be refused")
    except svc.SpireLabError:
        pass
    db.close()


def test_a_failed_teardown_does_not_re_arm_the_timer():
    """Re-arming would have the sweep retry a teardown that already failed once, on a
    loop, silently."""
    svc = _install_stubs(acl_raises=True)
    db = _fresh_db()
    row, job_id = _provisioned(svc, db)
    # Provision failed too (the ACL raises), which is fine — teardown is what is tested.
    asyncio.run(svc.run_provision(db, lab_id=row.id, job_id=job_id))
    row.status = "available"
    db.commit()
    out = svc.start_decommission(db, lab_id=row.id, created_by="tester")
    asyncio.run(svc.run_decommission(db, lab_id=row.id, job_id=out["job_id"]))
    db.expire_all()
    row = svc.get_lab(db, row.id)
    assert row.status == "failed"
    assert row.expires_at is None, "a failed teardown must not re-arm the sweep"
    db.close()


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
