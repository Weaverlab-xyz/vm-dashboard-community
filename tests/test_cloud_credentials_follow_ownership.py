"""Credentials and images of a cloud VM are for its own workstream, like destroy and power.

Destroy and power already called ``_assert_can_act``. These eight routes did not, so a user
in workstream A could read the SSH key or administrator password of workstream B's machine,
or image it (a full copy of its disk; on Azure, ``generalize`` also leaves the VM unusable):

  AWS    GET  /instances/{id}/ssh-key      GET /instances/{id}/admin-password
         POST /instances/{id}/create-image
  Azure  GET  /vms/{name}/ssh-key          GET /vms/{name}/admin-password
         POST /vms/{name}/create-image
  GCP    GET  /instances/{name}/admin-password   POST /instances/{name}/create-image

Each case below runs the real route against a real database row tagged into ``team-a``:
alice (in team-a) gets past the check, bob (team-b) is refused before anything is read or
queued, and an untagged machine is for administrators only.

Runs standalone:
    python tests/test_cloud_credentials_follow_ownership.py
"""
import os
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="cloud-creds-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "cloud-creds-test-secret")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from web_dashboard.api import auth  # noqa: E402
from web_dashboard.api import aws as api_aws  # noqa: E402
from web_dashboard.api import azure as api_azure  # noqa: E402
from web_dashboard.api import gcp as api_gcp  # noqa: E402
from web_dashboard.api.auth import full_access_map  # noqa: E402
from web_dashboard.database import Base, Job, SessionLocal, engine  # noqa: E402

Base.metadata.create_all(bind=engine)


class _User:
    accessor_env_id = None
    pov_env_ids_list: list = []

    def __init__(self, username, workgroups=(), admin=False):
        self.id = username
        self.username = username
        self.workgroups_list = list(workgroups)
        self.is_admin = self.is_effective_admin = admin
        self.effective_permissions_dict = full_access_map()


ALICE = _User("alice", ["team-a"])
BOB = _User("bob", ["team-b"])
ROOT = _User("root", admin=True)


def _job(job_type, workgroup, **meta):
    db = SessionLocal()
    try:
        j = Job(id=str(uuid.uuid4()), job_type=job_type, status="completed",
                workgroup=workgroup, created_by="alice",
                cloud_resource_id=meta.get("vm_name"))
        j.metadata_dict = meta
        db.add(j)
        db.commit()
    finally:
        db.close()


_job("ec2_deploy", "team-a", instance_id="i-a", key_name="kp-a",
     admin_password_backend="vault", admin_password_ref="ref-a")
_job("ec2_deploy", None, instance_id="i-untagged", key_name="kp-u",
     admin_password_backend="vault", admin_password_ref="ref-u")
_job("azure_deploy", "team-a", vm_name="vm-a",
     admin_password_backend="vault", admin_password_ref="ref-vm-a")
_job("gce_deploy", "team-a", instance_name="gce-a",
     admin_password_backend="vault", admin_password_ref="ref-gce-a")


# Nothing below may reach a cloud or a secrets backend: each stub returns a value, so a
# request that gets past the ownership check answers 200 and one refused by it answers 403.
async def _fake_key(*_a, **_k):
    return "-----BEGIN PRIVATE KEY-----stub"


async def _no_instances(*_a, **_k):
    return []


api_aws.aws_service.get_keypair_private_key = _fake_key
api_aws.aws_service.describe_instances = _no_instances
api_azure.azure_service.resolve_azure_ssh_public_key = _fake_key
api_azure.azure_service.resolve_azure_ssh_private_key = _fake_key
api_azure.azure_service.verify_ssh_keypair = lambda *_a: {"matches": True}
api_azure._cfg = lambda key, *a, **k: "https://kv.example" if key == "azure_key_vault_url" else "x"
api_gcp._gcp_project = lambda: "proj"
api_gcp._gcp_zone = lambda: "zone-a"

from web_dashboard.services import secrets_backend_service, windows_admin_secret  # noqa: E402

windows_admin_secret.read = lambda backend, ref: "S3cret!"
secrets_backend_service.read_sync = lambda backend, ref: "S3cret!"


def _client(user):
    app = FastAPI()
    for mod in (api_aws, api_azure, api_gcp):
        app.include_router(mod.router)
    app.dependency_overrides[auth.get_current_user] = lambda: user
    return TestClient(app)


def _prefix(mod):
    return mod.router.prefix


CASES = (
    ("GET", f"{_prefix(api_aws)}/instances/i-a/ssh-key", None),
    ("GET", f"{_prefix(api_aws)}/instances/i-a/admin-password", None),
    ("POST", f"{_prefix(api_aws)}/instances/i-a/create-image", {"name": "img-a"}),
    ("GET", f"{_prefix(api_azure)}/vms/vm-a/ssh-key", None),
    ("GET", f"{_prefix(api_azure)}/vms/vm-a/admin-password", None),
    ("POST", f"{_prefix(api_azure)}/vms/vm-a/create-image", {"name": "img-a"}),
    ("GET", f"{_prefix(api_gcp)}/instances/gce-a/admin-password", None),
    ("POST", f"{_prefix(api_gcp)}/instances/gce-a/create-image", {"image_name": "img-a"}),
)


def _call(user, method, url, body):
    c = _client(user)
    return c.request(method, url, json=body) if body is not None else c.request(method, url)


def _image_jobs():
    db = SessionLocal()
    try:
        return db.query(Job).filter(Job.job_type.in_(
            ("ec2_create_image", "azure_create_image", "gce_capture_image"))).count()
    finally:
        db.close()


def test_another_workstream_is_refused_before_anything_is_read_or_queued():
    before = _image_jobs()
    for method, url, body in CASES:
        r = _call(BOB, method, url, body)
        assert r.status_code == 403, f"{method} {url}: bob got {r.status_code} {r.text[:200]}"
        assert "S3cret" not in r.text and "PRIVATE KEY" not in r.text, url
    assert _image_jobs() == before, "a refused create-image still queued a job"


def test_the_owning_workstream_and_an_admin_still_get_through():
    for user in (ALICE, ROOT):
        for method, url, body in CASES:
            r = _call(user, method, url, body)
            assert r.status_code == 200, (
                f"{method} {url}: {user.username} got {r.status_code} {r.text[:200]}")


def test_an_untagged_machine_is_admin_only():
    url = f"{_prefix(api_aws)}/instances/i-untagged/admin-password"
    r = _call(ALICE, "GET", url, None)
    assert r.status_code == 403 and "only an admin can read its credentials" in r.text, r.text
    assert _call(ROOT, "GET", url, None).status_code == 200


def test_the_ssh_key_read_is_audited():
    from web_dashboard.database import AuditLog
    _call(ALICE, "GET", f"{_prefix(api_aws)}/instances/i-a/ssh-key", None)
    db = SessionLocal()
    try:
        assert db.query(AuditLog).filter(AuditLog.action == "aws_instance_ssh_key_read").count()
    finally:
        db.close()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
