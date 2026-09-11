"""Which deploy job a cloud Ansible run resolves its SSH key from.

`_find_cloud_deploy_meta` is one `if`, and it was wrong in a way nothing could see:

    if (meta.get("public_ip") or meta.get("private_ip")) == ip:

The `or` short-circuits. A deploy job that recorded BOTH addresses compares only its
public one, so a run aimed at that VM's PRIVATE address never matched — and the miss is
silent. No build key is found, the caller falls back to the per-cloud global key the host
may not trust, the cloud runner writes an EMPTY /tmp/ssh_key, and the operator sees
`Permission denied (publickey)`: a message about the host rejecting a key, for a run that
never had one.

This is the highest-blast-radius line in the SPIRE-lab credential change, because it
governs key resolution for every cloud Config-Management run, not just a lab. So it gets
real rows and a real query rather than a source-text assertion.

Run: python tests/test_ansible_deploy_key_match.py   (or under pytest)
"""
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="deploykey-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-deploy-key-match")

try:
    from web_dashboard.database import Base, Job, SessionLocal, engine
    from web_dashboard.services import job_service
    from web_dashboard.services import ansible_local_run_service as svc
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

Base.metadata.create_all(bind=engine)


def _db():
    db = SessionLocal()
    db.query(Job).delete()
    db.commit()
    return db


def _deploy(db, *, job_type="azure_deploy", public_ip="", private_ip="",
            destroyed=False, name="vm-01", **extra):
    meta = {"vm_name": name, "instance_name": name, "public_ip": public_ip,
            "private_ip": private_ip, "destroyed": destroyed}
    meta.update(extra)
    job = job_service.create_job(db, job_type, "tester", status="completed",
                                 metadata=meta)
    job.status = "completed"
    db.commit()
    return job


# ── the short-circuit bug this file exists for ───────────────────────────────

def test_the_private_address_matches_even_when_a_public_one_is_recorded():
    """The regression. A dashboard-deployed VM with BOTH addresses, addressed by its
    private one — which is what a SPIRE lab does whenever the host has no public IP
    reachable from the runner."""
    db = _db()
    _deploy(db, public_ip="20.30.40.50", private_ip="10.99.2.4",
            ssh_key_secret_override="azureVM-keypair-lab")
    meta = svc._find_cloud_deploy_meta(db, "azure", "10.99.2.4")
    assert meta, "a private-IP target must still find its own deploy job"
    assert meta["ssh_key_secret_override"] == "azureVM-keypair-lab"
    db.close()


def test_the_public_address_still_matches():
    db = _db()
    _deploy(db, public_ip="20.30.40.50", private_ip="10.99.2.4")
    assert svc._find_cloud_deploy_meta(db, "azure", "20.30.40.50")
    db.close()


def test_a_private_only_deploy_matches_its_private_address():
    db = _db()
    _deploy(db, public_ip="", private_ip="10.99.2.4")
    assert svc._find_cloud_deploy_meta(db, "azure", "10.99.2.4")
    db.close()


def test_an_unrelated_address_matches_nothing():
    db = _db()
    _deploy(db, public_ip="20.30.40.50", private_ip="10.99.2.4")
    assert svc._find_cloud_deploy_meta(db, "azure", "10.0.0.9") == {}
    db.close()


def test_a_blank_recorded_address_never_matches():
    """`ip` is guaranteed truthy by the guard, but a row with NO addresses must not
    become a wildcard now that both fields are compared."""
    db = _db()
    _deploy(db, public_ip="", private_ip="")
    assert svc._find_cloud_deploy_meta(db, "azure", "10.99.2.4") == {}
    db.close()


def test_a_blank_target_matches_nothing():
    db = _db()
    _deploy(db, public_ip="20.30.40.50", private_ip="10.99.2.4")
    assert svc._find_cloud_deploy_meta(db, "azure", "") == {}
    db.close()


# ── the pre-existing rules, still holding ────────────────────────────────────

def test_a_destroyed_vm_is_skipped():
    """Its keypair secret may be gone, and its address may already belong to another
    VM — an address is not an identity once the row is destroyed."""
    db = _db()
    _deploy(db, private_ip="10.99.2.4", destroyed=True)
    assert svc._find_cloud_deploy_meta(db, "azure", "10.99.2.4") == {}
    db.close()


def test_the_cloud_selects_the_job_type():
    db = _db()
    _deploy(db, job_type="ec2_deploy", private_ip="10.99.2.4")
    assert svc._find_cloud_deploy_meta(db, "aws", "10.99.2.4")
    # ...and an azure run must not pick up an AWS deploy at the same address.
    assert svc._find_cloud_deploy_meta(db, "azure", "10.99.2.4") == {}
    db.close()


def test_an_unknown_cloud_matches_nothing():
    db = _db()
    _deploy(db, private_ip="10.99.2.4")
    assert svc._find_cloud_deploy_meta(db, "oci", "10.99.2.4") == {}
    assert svc._find_cloud_deploy_meta(db, "", "10.99.2.4") == {}
    db.close()


def test_the_most_recent_matching_deploy_wins():
    """An address reused by a rebuild must resolve to the CURRENT VM's keypair, not the
    first row that happens to mention it."""
    db = _db()
    _deploy(db, private_ip="10.99.2.4", ssh_key_secret_override="old-keypair")
    _deploy(db, private_ip="10.99.2.4", ssh_key_secret_override="new-keypair")
    meta = svc._find_cloud_deploy_meta(db, "azure", "10.99.2.4")
    assert meta["ssh_key_secret_override"] == "new-keypair"
    db.close()


def test_a_pending_deploy_is_not_a_source_of_truth():
    db = _db()
    job = _deploy(db, private_ip="10.99.2.4")
    job.status = "running"
    db.commit()
    assert svc._find_cloud_deploy_meta(db, "azure", "10.99.2.4") == {}
    db.close()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
